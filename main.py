#!/usr/bin/env python3
"""
WeChat Group Chat Daily Report Generator
Processes a WeChat screen recording and generates an AI-powered daily report.
"""

import os
import re
import sys
import glob
import json
import time
import shutil
import tempfile
import subprocess
import fractions
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, BarColumn,
    TaskProgressColumn, TimeElapsedColumn,
)
from rich.panel import Panel

import markdown as md_lib

# ── Constants ──────────────────────────────────────────────────────────────────
DOWNLOADS_DIR = Path("/Users/louyu/Downloads")
OUTPUT_DIR = Path.cwd()
GEMINI_MODEL = "gemini-2.5-flash"
CLAUDE_MODEL = "claude-opus-4-6"

CLAUDE_SYSTEM_PROMPT = """为以下群聊消息编写一个每日总结，让对 AI 前沿发展感兴趣的人士了解群里的最新动态。总结中要包含具体的群友名称。其中重点关注最新的行业新闻， AI 工具和方法论。

新闻要包括：
新闻要点
有代表性的群友评论（包含群友名称）

对于 AI 工具，要包含：
工具名称
工具简介
有代表性的群友评价（包括群友名称）

方法论要具体实用并搭配群里的具体例子，包含：
 一句话原则总结
详细方法论
群友的代表性例子（包含群友名称）

文章需要言简意赅，但是保留重要、有用的信息。

最开始要有一个导读和目录。目录要有层级结构：先列出主要类别（如"行业新闻"、"AI 工具"、"方法论"），再在每个类别下缩进列出具体条目。格式示例：

行业新闻
- 某条新闻标题
- 另一条新闻标题
AI 工具
- 工具名称
方法论
- 方法论名称"""

GEMINI_EXTRACTION_PROMPT = """你正在分析一段微信群聊的屏幕录像（已减速处理以便更清晰地查看内容）。

请提取视频中所有可见的聊天消息。对于每条消息，请记录：
1. 时间（如果可见，格式为 HH:MM）
2. 发言人名称（消息气泡上方显示的昵称）
3. 消息内容（完整文本）
4. 对于图片、语音消息或视频：用 [方括号] 简要描述内容

请使用以下格式，每条消息占一行：
[HH:MM] 发言人名称: 消息内容

请务必仔细查看整个视频，提取所有可见的消息。如果某条消息部分遮挡，请提取可见部分。
不要遗漏任何消息，即使消息内容很短。"""

console = Console()


# ── API Key Management ─────────────────────────────────────────────────────────

def load_or_prompt_api_keys() -> tuple[str, str]:
    """Load API keys from .env or prompt user and save them."""
    env_path = Path(".env")
    load_dotenv(env_path)

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    changed = False
    if not gemini_key:
        console.print("[yellow]需要 Gemini API Key（将保存到 .env 文件）[/yellow]")
        gemini_key = console.input("[bold]请输入 GEMINI_API_KEY: [/bold]").strip()
        changed = True
    if not anthropic_key:
        console.print("[yellow]需要 Anthropic API Key（将保存到 .env 文件）[/yellow]")
        anthropic_key = console.input("[bold]请输入 ANTHROPIC_API_KEY: [/bold]").strip()
        changed = True

    if changed:
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        lines = [
            line for line in existing.splitlines()
            if not line.startswith("GEMINI_API_KEY=")
            and not line.startswith("ANTHROPIC_API_KEY=")
        ]
        lines.append(f"GEMINI_API_KEY={gemini_key}")
        lines.append(f"ANTHROPIC_API_KEY={anthropic_key}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        console.print("[green]API Keys 已保存到 .env[/green]")

    return gemini_key, anthropic_key


# ── Video Discovery ────────────────────────────────────────────────────────────

def find_latest_screen_recording() -> Path:
    """Find the most recently modified ScreenRecording file in Downloads."""
    pattern = str(DOWNLOADS_DIR / "ScreenRecording*")
    files = glob.glob(pattern)
    if not files:
        raise FileNotFoundError(
            f"在 {DOWNLOADS_DIR} 中找不到以 ScreenRecording 开头的视频文件"
        )
    latest = max(files, key=os.path.getmtime)
    return Path(latest)


def infer_date_from_video(video_path: Path) -> str:
    """
    Infer the recording date from the filename.

    Priority:
    1. MM-DD-YYYY pattern in filename (e.g. ScreenRecording_02-21-2026 ...)
    2. YYYY-MM-DD pattern in filename
    3. If parsing fails and current time is 0:00–4:00 → yesterday
    4. If parsing fails otherwise → prompt user to enter date
    """
    name = video_path.stem

    # Try MM-DD-YYYY (macOS default format)
    m = re.search(r'(\d{2})-(\d{2})-(\d{4})', name)
    if m:
        month, day, year = m.groups()
        return f"{year}-{month}-{day}"

    # Try YYYY-MM-DD
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', name)
    if m:
        return m.group(0)

    # Filename parsing failed
    now = datetime.now()
    if now.hour < 4:
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        console.print(
            f"[yellow]无法从文件名推断日期。"
            f"当前时间 {now.strftime('%H:%M')}（刚过午夜），使用昨天日期: {yesterday}[/yellow]"
        )
        return yesterday

    # Prompt user
    console.print("[yellow]无法从文件名自动推断日期，请手动输入。[/yellow]")
    while True:
        user_input = console.input(
            "[bold]请输入日期（格式 YYYY-MM-DD，直接回车使用今天）: [/bold]"
        ).strip()
        if not user_input:
            return date.today().strftime("%Y-%m-%d")
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', user_input):
            return user_input
        console.print("[red]格式不正确，请重新输入（例如 2026-02-21）[/red]")


# ── ffmpeg Utilities ───────────────────────────────────────────────────────────

def get_video_info(video_path: Path) -> tuple[fractions.Fraction, int]:
    """Return (fps, total_frames) for the given video file."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)

    for stream in data["streams"]:
        if stream.get("codec_type") == "video":
            fps = fractions.Fraction(stream["r_frame_rate"])
            nb_frames = stream.get("nb_frames")
            if nb_frames:
                total_frames = int(nb_frames)
            else:
                dur = float(stream.get("duration", 0))
                total_frames = int(dur * float(fps))
            return fps, total_frames

    raise ValueError("视频文件中找不到视频流")


def slow_down_video(input_path: Path, output_path: Path) -> None:
    """Slow down video 5x (setpts=5.0) and reduce frame rate 5x."""
    original_fps, total_frames = get_video_info(input_path)
    target_fps = original_fps / 5
    # Total output frames is the same as input frames (same content, stretched)
    output_total_frames = total_frames

    console.print(
        f"  原始帧率: [cyan]{original_fps}[/cyan] fps → "
        f"目标帧率: [cyan]{float(target_fps):.1f}[/cyan] fps  "
        f"([dim]{total_frames:,} 帧[/dim])"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vf", "setpts=5.0*PTS",
        "-r", str(float(target_fps)),
        "-an",                   # no audio needed
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "fast",
        "-progress", "pipe:1",
        "-nostats",
        str(output_path),
    ]

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("处理视频 (5x 减速)...", total=output_total_frames)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

        for line in proc.stdout:
            line = line.strip()
            if line.startswith("frame="):
                try:
                    frame_num = int(line.split("=", 1)[1])
                    progress.update(task, completed=frame_num)
                except ValueError:
                    pass
            elif line == "progress=end":
                progress.update(task, completed=output_total_frames)

        proc.wait()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)


# ── Temp Directory ─────────────────────────────────────────────────────────────

@contextmanager
def temp_directory():
    """Context manager that creates and guarantees cleanup of a temp directory."""
    tmp_root = Path(tempfile.gettempdir())
    for stale in tmp_root.glob("wechat-report-*"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
            console.print(f"[dim]已清理残留临时目录: {stale}[/dim]")

    tmpdir = Path(tempfile.mkdtemp(prefix="wechat-report-"))
    console.print(f"[dim]临时目录: {tmpdir}[/dim]")
    try:
        yield tmpdir
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        console.print("[dim]临时目录已清理[/dim]")


# ── Gemini Chat Extraction ─────────────────────────────────────────────────────

def extract_chat_with_gemini(video_path: Path, api_key: str) -> str:
    """Upload video to Gemini Files API and extract chat history."""
    from google import genai as google_genai
    from google.genai import types

    # 5-minute timeout for large video uploads and slow generation
    client = google_genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=300000),
    )

    # Step 1: Upload
    console.print(f"  上传文件: [cyan]{video_path.name}[/cyan] "
                  f"([dim]{video_path.stat().st_size / 1024 / 1024:.1f} MB[/dim])")

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("上传视频到 Gemini...", total=None)

        video_file = client.files.upload(
            file=str(video_path),
            config=types.UploadFileConfig(
                mime_type="video/mp4",
                display_name=video_path.name,
            ),
        )
        progress.update(task, description=f"上传完成: {video_file.name}")

    # Step 2: Poll until ACTIVE
    start = time.time()
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]等待 Gemini 处理视频... {task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("", total=None)

        while True:
            video_file = client.files.get(name=video_file.name)
            state = video_file.state.name
            elapsed = time.time() - start
            progress.update(task, description=f"[dim]状态={state}, 已等待 {elapsed:.0f}s[/dim]")

            if state == "ACTIVE":
                break
            elif state == "FAILED":
                raise RuntimeError(f"Gemini 文件处理失败: {video_file.name}")

            time.sleep(5)

    console.print("[green]视频处理完毕，开始提取聊天记录...[/green]")

    # Step 3: Extract chat with streaming (retry up to 3 times)
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        extracted_text = ""
        char_count = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]Gemini 提取聊天记录..."),
            TextColumn("[dim]{task.description}[/dim]"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            attempt_label = f" (第 {attempt}/{max_retries} 次)" if attempt > 1 else ""
            task = progress.add_task(f"0 字{attempt_label}", total=None)

            try:
                response_stream = client.models.generate_content_stream(
                    model=GEMINI_MODEL,
                    contents=[
                        types.Part.from_uri(
                            file_uri=video_file.uri,
                            mime_type="video/mp4",
                        ),
                        types.Part.from_text(text=GEMINI_EXTRACTION_PROMPT),
                    ],
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        max_output_tokens=65536,
                        # Disable built-in reasoning: extraction doesn't need it
                        # and reasoning causes multi-minute silent delays before output
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                    ),
                )

                for chunk in response_stream:
                    if chunk.text:
                        extracted_text += chunk.text
                        char_count += len(chunk.text)
                        progress.update(task, description=f"{char_count:,} 字已提取{attempt_label}")

                break  # success

            except Exception as e:
                if attempt < max_retries:
                    wait = 10 * attempt
                    console.print(
                        f"[yellow]Gemini 请求失败 ({e.__class__.__name__})，"
                        f"{wait}s 后重试 ({attempt}/{max_retries})...[/yellow]"
                    )
                    time.sleep(wait)
                else:
                    raise

    # Step 4: Cleanup uploaded file
    try:
        client.files.delete(name=video_file.name)
        console.print(f"[dim]已删除 Gemini 文件: {video_file.name}[/dim]")
    except Exception:
        pass

    return extracted_text


# ── Claude Report Generation ───────────────────────────────────────────────────

def generate_report_with_claude(chat_history: str, api_key: str) -> str:
    """Generate daily report using Claude with streaming and retry on connection errors."""
    import anthropic
    import httpx

    # Set generous timeouts: 30s connect, 10min read (large input + long generation)
    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=httpx.Timeout(600.0, connect=30.0),
    )

    user_message = f"{CLAUDE_SYSTEM_PROMPT}\n\n--- 聊天记录 ---\n\n{chat_history}"

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        report_text = ""
        char_count = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]Claude 正在生成日报..."),
            TextColumn("[dim]{task.description}[/dim]"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            attempt_label = f" (第 {attempt}/{max_retries} 次)" if attempt > 1 else ""
            task = progress.add_task(f"0 字{attempt_label}", total=None)

            try:
                with client.messages.stream(
                    model=CLAUDE_MODEL,
                    max_tokens=8096,
                    messages=[{"role": "user", "content": user_message}],
                ) as stream:
                    for text in stream.text_stream:
                        report_text += text
                        char_count += len(text)
                        progress.update(task, description=f"{char_count:,} 字已生成{attempt_label}")

                return report_text  # success

            except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError,
                    anthropic.APIStatusError) as e:
                if attempt < max_retries:
                    # Overloaded errors (529) need a longer back-off
                    wait = 30 if isinstance(e, anthropic.APIStatusError) else 5 * attempt
                    console.print(
                        f"[yellow]Claude 请求失败 ({e.__class__.__name__})，"
                        f"{wait}s 后重试 ({attempt}/{max_retries})...[/yellow]"
                    )
                    time.sleep(wait)
                else:
                    raise

    return report_text  # unreachable, satisfies type checker


# ── PDF Generation ─────────────────────────────────────────────────────────────

def _get_pdf_css() -> str:
    return """
    @font-face {
        font-family: 'ChineseFont';
        src: local('PingFang SC'),
             local('STHeiti Medium'),
             local('Heiti SC'),
             url('/System/Library/Fonts/STHeiti Medium.ttc') format('truetype'),
             url('/System/Library/Fonts/Hiragino Sans GB.ttc') format('truetype'),
             url('/Library/Fonts/Arial Unicode.ttf') format('truetype');
    }

    @page {
        size: A4;
        margin: 18mm 14mm;
    }

    body {
        font-family: 'ChineseFont', 'PingFang SC', 'STHeiti', 'Heiti SC',
                     'Hiragino Sans GB', 'Arial Unicode MS', sans-serif;
        font-size: 30pt;
        line-height: 1.75;
        color: #1a1a1a;
        word-break: break-word;
        overflow-wrap: break-word;
    }

    h1 {
        font-size: 40pt;
        font-weight: bold;
        color: #1a56db;
        margin-top: 24pt;
        margin-bottom: 14pt;
        border-bottom: 2pt solid #1a56db;
        padding-bottom: 6pt;
    }

    h2 {
        font-size: 36pt;
        font-weight: bold;
        color: #1a56db;
        margin-top: 20pt;
        margin-bottom: 10pt;
        border-bottom: 1pt solid #93c5fd;
        padding-bottom: 4pt;
    }

    h3 {
        font-size: 33pt;
        font-weight: bold;
        color: #1e40af;
        margin-top: 16pt;
        margin-bottom: 8pt;
    }

    p { margin: 10pt 0; }

    ul, ol {
        margin: 8pt 0;
        padding-left: 30pt;
    }

    li { margin: 6pt 0; }

    /* Nested lists: indent sub-items and slightly smaller font */
    ol ol, ol ul, ul ol, ul ul {
        margin: 3pt 0;
        padding-left: 28pt;
        font-size: 0.88em;
    }

    blockquote {
        border-left: 4pt solid #aaa;
        margin: 12pt 0;
        padding: 4pt 14pt;
        color: #444;
        background: #f8f8f8;
    }

    code {
        font-family: 'Courier New', 'Menlo', monospace;
        font-size: 16pt;
        background: #f0f0f0;
        padding: 1pt 5pt;
        border-radius: 3pt;
    }

    pre {
        background: #f0f0f0;
        padding: 12pt;
        font-size: 15pt;
        overflow-x: auto;
        border-radius: 4pt;
    }

    table {
        border-collapse: collapse;
        width: 100%;
        margin: 12pt 0;
    }

    th, td {
        border: 1pt solid #ccc;
        padding: 7pt 12pt;
        text-align: left;
    }

    th {
        background: #f0f0f0;
        font-weight: bold;
    }

    hr {
        border: none;
        border-top: 1pt solid #ddd;
        margin: 14pt 0;
    }
    """


def convert_to_pdf(markdown_text: str, output_path: Path) -> None:
    """Convert Markdown text to PDF with 20pt font and Chinese support."""
    from weasyprint import HTML, CSS

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Markdown → HTML...", total=None)

        converter = md_lib.Markdown(extensions=["tables", "fenced_code", "nl2br"])
        html_body = converter.convert(markdown_text)

        progress.update(task, description="渲染 PDF（可能需要几秒）...")

        full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
</head>
<body>
{html_body}
</body>
</html>"""

        HTML(string=full_html).write_pdf(
            str(output_path),
            stylesheets=[CSS(string=_get_pdf_css())],
        )

        progress.update(task, description=f"PDF 已保存: {output_path.name}")


# ── Main Pipeline ──────────────────────────────────────────────────────────────

def main() -> None:
    console.print(Panel.fit(
        "[bold cyan]微信群聊日报生成器[/bold cyan]\n"
        "[dim]WeChat Group Chat Daily Report Generator[/dim]",
        border_style="cyan",
    ))

    try:
        # Step 1: API Keys
        console.rule("[bold]Step 1  API Key 配置")
        gemini_key, anthropic_key = load_or_prompt_api_keys()
        console.print("[green]API Keys 就绪[/green]\n")

        # Step 2: Find video
        console.rule("[bold]Step 2  查找屏幕录像")
        video_path = find_latest_screen_recording()
        size_mb = video_path.stat().st_size / 1024 / 1024
        recording_date = infer_date_from_video(video_path)
        pdf_filename = f"{recording_date} 群聊日报.pdf"
        console.print(
            f"[green]找到文件:[/green] [cyan]{video_path.name}[/cyan] "
            f"[dim]({size_mb:.1f} MB)[/dim]"
        )
        console.print(f"[green]日报日期:[/green] [cyan]{recording_date}[/cyan]\n")

        chat_history = ""

        with temp_directory() as tmpdir:
            # Step 3: Slow down video
            console.rule("[bold]Step 3  视频处理 (5x 减速)")
            slowed_path = tmpdir / "slowed_recording.mp4"
            slow_down_video(video_path, slowed_path)
            slowed_mb = slowed_path.stat().st_size / 1024 / 1024
            console.print(f"[green]减速视频已生成[/green] [dim]({slowed_mb:.1f} MB)[/dim]\n")

            # Step 4: Gemini extraction
            console.rule("[bold]Step 4  Gemini 提取聊天记录")
            chat_history = extract_chat_with_gemini(slowed_path, gemini_key)
            console.print(
                f"[green]聊天记录提取完毕[/green] "
                f"[dim]{len(chat_history):,} 字符[/dim]\n"
            )

        # Step 5: Claude report
        console.rule("[bold]Step 5  Claude 生成日报")
        report_markdown = generate_report_with_claude(chat_history, anthropic_key)
        console.print(
            f"[green]日报生成完毕[/green] "
            f"[dim]{len(report_markdown):,} 字符[/dim]\n"
        )

        # Step 6: PDF
        console.rule("[bold]Step 6  导出 PDF")
        pdf_path = OUTPUT_DIR / pdf_filename
        convert_to_pdf(report_markdown, pdf_path)
        console.print()

        console.print(Panel.fit(
            f"[bold green]完成！[/bold green]\n"
            f"日报已保存至:\n[cyan]{pdf_path}[/cyan]",
            border_style="green",
            title="Success",
        ))

    except KeyboardInterrupt:
        console.print("\n[yellow]用户中断[/yellow]")
        sys.exit(1)
    except FileNotFoundError as e:
        console.print(f"\n[bold red]文件未找到:[/bold red] {e}")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        console.print(f"\n[bold red]ffmpeg 错误 (返回码 {e.returncode}):[/bold red] {e.cmd}")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]错误:[/bold red] {e}")
        raise


if __name__ == "__main__":
    main()
