#!/usr/bin/env python3
"""
WeChat Group Chat Daily Report Generator
Processes a WeChat screen recording and generates an AI-powered daily report.
"""

import argparse
import os
import re
import sys
import glob
import json
import time
import shutil
import sqlite3
import tempfile
import subprocess
import fractions
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, BarColumn,
    TaskProgressColumn, TimeElapsedColumn,
)
from rich.panel import Panel

import markdown as md_lib
from markdown.extensions.toc import TocExtension

# ── Constants ──────────────────────────────────────────────────────────────────
DOWNLOADS_DIR = Path.home() / "Downloads"
OUTPUT_DIR = Path.cwd()
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_SUMMARY_MODEL = "gemini-3.0-pro"
CLAUDE_MODEL = "claude-opus-4-6"

# ── Database Constants ──────────────────────────────────────────────────────────
CHATLOG_DIR = Path.home() / "Documents/chatlog"          # pre-decrypted fallback
CHATLOG_MAC_DIR = Path(__file__).parent / "chatlog-mac"  # keys.json lives here
WECHAT_DATA_DIR = (
    Path.home()
    / "Library/Containers/com.tencent.xinWeChat"
    / "Data/Documents/xwechat_files"
)
GROUP_CHAT_ID = "26389512912@chatroom"
# MD5 of GROUP_CHAT_ID (echo -n "26389512912@chatroom" | md5)
GROUP_TABLE = "Msg_1f5cd6985e2d31687fc076061b1fa6da"

# local_type values
_MSG_TEXT     = 1
_MSG_IMAGE    = 3
_MSG_VOICE    = 34
_MSG_VIDEO    = 43
_MSG_STICKER  = 47
_MSG_CARD     = 42
_MSG_SYSTEM   = 10000
_MSG_QUOTE    = 244813135921   # AppMsg subtype=57 (quoted reply)
_MSG_LINK_OPEN = 4294967345   # AppMsg subtype=1
_MSG_LINK_CARD = 21474836529  # AppMsg subtype=5
_MSG_FILE     = 25769803825   # AppMsg subtype=6
_MSG_GIF      = 34359738417   # AppMsg subtype=8
_MSG_FORWARD  = 81604378673   # AppMsg subtype=19
_MSG_MINIAPP  = 154618822705  # AppMsg subtype=36
_MSG_TAP      = 266287972401  # AppMsg subtype=62

SUMMARY_PROMPT = """为以下群聊消息编写一个每日总结，让对 AI 前沿发展感兴趣的人士了解群里的最新动态。总结中要包含具体的群友名称。其中重点关注最新的行业新闻、AI 工具和方法论，同时也要捕捉群里的人情味与有趣瞬间。

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

## 闲聊与花絮

在正文内容之后，必须添加一个"闲聊与花絮"章节，记录当天群里有意思的非专业内容，让读者感受到群里的温度和氛围。这个章节要包含：
- **有趣的吐槽或玩笑**：群友的幽默发言、梗、双关、让人会心一笑的对话
- **有意思的闲聊话题**：不属于 AI 行业动态，但引发了热烈讨论或有趣互动的话题
- **群友的个人分享**：生活经历、踩坑记录、个人小成就等有人情味的内容
- **精彩的互怼或争论**：观点不同的群友之间轻松的交锋（如有）

写作要求：语气轻松活泼，像朋友之间聊天一样，不必正经。每个小点用 2-4 句话概述，点到为止，不要过度展开。如果当天闲聊内容较少，可以只写 1-2 条；如果没有值得一提的闲聊，可以不写此章节。

文章需要言简意赅，但是保留重要、有用的信息。

所有引用群友观点或发言的地方，必须使用 Markdown 引用框（> 语法），每人一段。例如：

> 某某人：认为这个工具很好，并指出"某某原因"

> 某某人：不同意上面的观点，因为"某某原因"

最开始要有一段导读，介绍今天内容的亮点（包括有趣的闲聊花絮）。导读之后，单独一行写 [TOC]，程序将在此处自动插入目录。"""

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

def load_or_prompt_api_keys(need_anthropic: bool = False) -> tuple[str, str]:
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
    if need_anthropic and not anthropic_key:
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
        if anthropic_key:
            lines.append(f"ANTHROPIC_API_KEY={anthropic_key}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        console.print("[green]API Keys 已保存到 .env[/green]")

    return gemini_key, anthropic_key


# ── Encrypted DB Connection ────────────────────────────────────────────────────

# Module-level connection cache (rel_path → connection object)
_db_conns: dict[str, Any] = {}


def _find_db_storage() -> Path | None:
    """Return the WeChat db_storage directory, or None if not found."""
    if not WECHAT_DATA_DIR.exists():
        return None
    result = subprocess.run(
        ['find', str(WECHAT_DATA_DIR), '-name', 'db_storage', '-type', 'd',
         '-maxdepth', '5'],
        capture_output=True, text=True, timeout=5,
    )
    first = result.stdout.strip().split('\n')[0]
    return Path(first) if first else None


def _get_conn(rel_path: str) -> Any:
    """Return a (cached) DB connection for *rel_path*.

    Priority:
    1. Encrypted source via sqlcipher3 (never writes to disk)
    2. Pre-decrypted file in ~/Documents/chatlog/ (fallback)

    Raises FileNotFoundError if neither is available.
    """
    if rel_path in _db_conns:
        return _db_conns[rel_path]

    # ── Try encrypted source ──────────────────────────────────────────────
    keys_file = CHATLOG_MAC_DIR / "keys.json"
    if keys_file.exists():
        try:
            import sqlcipher3
            keys = json.loads(keys_file.read_text())
            if rel_path in keys:
                db_storage = _find_db_storage()
                if db_storage:
                    src = db_storage / rel_path
                    if src.exists():
                        enc_key = keys[rel_path]['enc_key']
                        conn = sqlcipher3.connect(str(src))
                        conn.execute(f"PRAGMA key = \"x'{enc_key}'\"")
                        conn.execute("PRAGMA cipher_page_size = 4096")
                        conn.execute("PRAGMA cipher_compatibility = 4")
                        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                        _db_conns[rel_path] = conn
                        console.print(
                            f"[dim]已连接加密源: {rel_path}[/dim]"
                        )
                        return conn
        except Exception as exc:
            console.print(
                f"[yellow]加密连接失败 ({rel_path}): {exc}，回退到已解密文件[/yellow]"
            )

    # ── Fallback: pre-decrypted file ──────────────────────────────────────
    dec = CHATLOG_DIR / rel_path
    if dec.exists():
        conn = sqlite3.connect(str(dec))
        _db_conns[rel_path] = conn
        console.print(f"[dim]使用已解密文件: {rel_path}[/dim]")
        return conn

    raise FileNotFoundError(
        f"找不到数据库 {rel_path}。\n"
        "请确保 chatlog-mac/keys.json 存在，或先运行 decrypt_wechat.sh 解密。"
    )


# ── Database Chat Extraction ───────────────────────────────────────────────────

def _decompress_if_needed(data) -> str:
    """Decompress zstd-compressed message content or return as string."""
    if isinstance(data, bytes) and data[:4] == b'\x28\xb5\x2f\xfd':
        result = subprocess.run(
            ['zstd', '-d', '-', '--stdout'],
            input=data, capture_output=True,
        )
        return result.stdout.decode('utf-8', errors='replace')
    if isinstance(data, bytes):
        return data.decode('utf-8', errors='replace')
    return data or ''


def _parse_sender_content(raw: str) -> tuple[str, str]:
    """Parse 'sender_id:\\ncontent' format. Returns (sender_id, content)."""
    pos = raw.find(':\n')
    if pos > 0:
        candidate = raw[:pos]
        if ' ' not in candidate and len(candidate) < 60:
            return candidate, raw[pos + 2:]
    return '', raw


def _xml_text(xml_str: str, tag: str) -> str:
    """Extract first matching tag's text content from an XML string."""
    m = re.search(rf'<{tag}>(.*?)</{tag}>', xml_str, re.DOTALL)
    return m.group(1).strip() if m else ''


def _format_quoted(refermsg: str) -> str:
    """Render a <refermsg> block as a short readable string."""
    ref_type = _xml_text(refermsg, 'type')
    displayname = _xml_text(refermsg, 'displayname')
    content = _xml_text(refermsg, 'content')
    prefix = f"{displayname}: " if displayname else ""

    if ref_type == '1':
        text = content[:100] + ('…' if len(content) > 100 else '')
        return prefix + text
    elif ref_type == '3':
        return prefix + '[图片]'
    elif ref_type == '34':
        return prefix + '[语音]'
    elif ref_type == '43':
        return prefix + '[视频]'
    elif ref_type == '47':
        return prefix + '[表情包]'
    elif ref_type == '49':
        title = _xml_text(content, 'title') if content else ''
        return prefix + (title if title else '[消息]')
    else:
        return prefix + '[消息]'


def _load_contact_map() -> dict[str, str]:
    """Return a dict mapping WeChat username → display nickname."""
    conn = _get_conn("contact/contact.db")
    cur = conn.cursor()
    cur.execute(
        "SELECT username, nick_name FROM contact "
        "WHERE nick_name IS NOT NULL AND nick_name != ''"
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def extract_chat_from_db(date_str: str) -> str:
    """Extract and format messages for *date_str* (YYYY-MM-DD) ±1 hour from SQLite."""
    contact_map = _load_contact_map()

    # Time window: 23:00 of previous day → 01:00 of next day
    date = datetime.strptime(date_str, '%Y-%m-%d')
    start_ts = int((date - timedelta(hours=1)).timestamp())
    end_ts   = int((date + timedelta(days=1, hours=1)).timestamp())

    # Collect rows from both shards; newer messages live in message_0.db
    rows: list[tuple] = []
    for rel in ["message/message_0.db", "message/message_1.db"]:
        try:
            conn = _get_conn(rel)
        except FileNotFoundError:
            continue
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master "
            f"WHERE type='table' AND name='{GROUP_TABLE}'"
        )
        if not cur.fetchone():
            continue
        cur.execute(
            f"SELECT create_time, local_type, message_content "
            f"FROM {GROUP_TABLE} "
            f"WHERE create_time >= ? AND create_time < ? "
            f"ORDER BY create_time",
            (start_ts, end_ts),
        )
        rows.extend(cur.fetchall())

    rows.sort(key=lambda x: x[0])

    lines: list[str] = []
    for create_time, local_type, message_content in rows:
        ts = datetime.fromtimestamp(create_time).strftime('%H:%M')
        raw = _decompress_if_needed(message_content)

        if local_type == _MSG_TEXT:
            sender_id, content = _parse_sender_content(raw)
            name = contact_map.get(sender_id, sender_id)
            content = content.strip()
            if name and content:
                lines.append(f"[{ts}] {name}: {content}")

        elif local_type == _MSG_QUOTE:
            sender_id, xml = _parse_sender_content(raw)
            name = contact_map.get(sender_id, sender_id)
            title = _xml_text(xml, 'title').strip()
            if name and title:
                line = f"[{ts}] {name}: {title}"
                refermsg_m = re.search(r'<refermsg>(.*?)</refermsg>', xml, re.DOTALL)
                if refermsg_m:
                    line += f"\n  > 引用 {_format_quoted(refermsg_m.group(1))}"
                lines.append(line)

        elif local_type == _MSG_TAP:
            # No sender prefix; title contains the full "A 拍了拍 B" string
            title = _xml_text(raw, 'title').strip()
            if title:
                lines.append(f"[{ts}] {title}")

        elif local_type == _MSG_SYSTEM:
            text = raw.strip()
            if text:
                lines.append(f"[{ts}] [系统] {text}")

        elif local_type in (_MSG_LINK_CARD, _MSG_LINK_OPEN):
            sender_id, xml = _parse_sender_content(raw)
            name = contact_map.get(sender_id, sender_id) or sender_id
            title = _xml_text(xml, 'title')
            prefix = f"[{ts}] {name}: " if name else f"[{ts}] "
            lines.append(prefix + ("[链接] " + title if title else "[链接]"))

        elif local_type == _MSG_FILE:
            sender_id, xml = _parse_sender_content(raw)
            name = contact_map.get(sender_id, sender_id) or sender_id
            title = _xml_text(xml, 'title')
            prefix = f"[{ts}] {name}: " if name else f"[{ts}] "
            lines.append(prefix + ("[文件] " + title if title else "[文件]"))

        elif local_type == _MSG_FORWARD:
            sender_id, xml = _parse_sender_content(raw)
            name = contact_map.get(sender_id, sender_id) or sender_id
            title = _xml_text(xml, 'title')
            prefix = f"[{ts}] {name}: " if name else f"[{ts}] "
            lines.append(prefix + ("[合并转发] " + title if title else "[合并转发]"))

        elif local_type == _MSG_MINIAPP:
            sender_id, xml = _parse_sender_content(raw)
            name = contact_map.get(sender_id, sender_id) or sender_id
            title = _xml_text(xml, 'title')
            prefix = f"[{ts}] {name}: " if name else f"[{ts}] "
            lines.append(prefix + ("[小程序] " + title if title else "[小程序]"))

        elif local_type == _MSG_IMAGE:
            sender_id, _ = _parse_sender_content(raw)
            name = contact_map.get(sender_id, sender_id)
            if name:
                lines.append(f"[{ts}] {name}: [图片]")

        elif local_type == _MSG_VOICE:
            sender_id, _ = _parse_sender_content(raw)
            name = contact_map.get(sender_id, sender_id)
            if name:
                lines.append(f"[{ts}] {name}: [语音]")

        elif local_type == _MSG_VIDEO:
            sender_id, _ = _parse_sender_content(raw)
            name = contact_map.get(sender_id, sender_id)
            if name:
                lines.append(f"[{ts}] {name}: [视频]")

        elif local_type == _MSG_STICKER:
            sender_id, _ = _parse_sender_content(raw)
            name = contact_map.get(sender_id, sender_id)
            if name:
                lines.append(f"[{ts}] {name}: [表情包]")

        elif local_type in (_MSG_CARD, _MSG_GIF):
            sender_id, _ = _parse_sender_content(raw)
            name = contact_map.get(sender_id, sender_id)
            label = '[名片]' if local_type == _MSG_CARD else '[GIF]'
            if name:
                lines.append(f"[{ts}] {name}: {label}")

    return '\n'.join(lines)


def find_missing_dates(allow_incomplete: bool = False) -> list[str]:
    """Return sorted list of dates (YYYY-MM-DD) that lack an archive PDF.

    If *allow_incomplete* is True, also include the last day in the DB even
    if it hasn't reached the midnight + 1h threshold.
    """
    # Existing archive dates
    archive_dir = OUTPUT_DIR / "archive"
    existing: set[str] = set()
    if archive_dir.exists():
        for pdf in archive_dir.rglob("*.pdf"):
            m = re.match(r'^(\d{4}-\d{2}-\d{2})\b', pdf.stem)
            if m:
                existing.add(m.group(1))

    # Latest message timestamp across all DB shards
    last_ts = 0
    for rel in ["message/message_0.db", "message/message_1.db"]:
        try:
            conn = _get_conn(rel)
        except FileNotFoundError:
            continue
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master "
            f"WHERE type='table' AND name='{GROUP_TABLE}'"
        )
        if not cur.fetchone():
            continue
        cur.execute(f"SELECT MAX(create_time) FROM {GROUP_TABLE}")
        row = cur.fetchone()
        if row and row[0]:
            last_ts = max(last_ts, row[0])

    if not last_ts:
        return []

    # A day D is complete when the DB contains a message at D+1 01:00 or later.
    # Equivalent: (last_message_time - 1h).date() - 1 day
    last_dt = datetime.fromtimestamp(last_ts)
    last_complete = (last_dt - timedelta(hours=1)).date() - timedelta(days=1)
    if allow_incomplete:
        last_complete = max(last_complete, last_dt.date())

    if not existing:
        return [last_complete.strftime('%Y-%m-%d')]

    max_archive = datetime.strptime(max(existing), '%Y-%m-%d').date()
    missing: list[str] = []
    current = max_archive + timedelta(days=1)
    while current <= last_complete:
        date_str = current.strftime('%Y-%m-%d')
        if date_str not in existing:
            missing.append(date_str)
        current += timedelta(days=1)
    return missing


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


# ── Gemini Report Generation ───────────────────────────────────────────────────

def generate_report_with_gemini(chat_history: str, api_key: str) -> str:
    """Generate daily report using Gemini with streaming and retry on errors."""
    from google import genai as google_genai
    from google.genai import types

    client = google_genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=300000),
    )

    prompt = f"{SUMMARY_PROMPT}\n\n--- 聊天记录 ---\n\n{chat_history}"

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        report_text = ""
        char_count = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]Gemini 正在生成日报..."),
            TextColumn("[dim]{task.description}[/dim]"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            attempt_label = f" (第 {attempt}/{max_retries} 次)" if attempt > 1 else ""
            task = progress.add_task(f"0 字{attempt_label}", total=None)

            try:
                response_stream = client.models.generate_content_stream(
                    model=GEMINI_SUMMARY_MODEL,
                    contents=[types.Part.from_text(text=prompt)],
                    config=types.GenerateContentConfig(
                        temperature=0.7,
                        max_output_tokens=65536,
                    ),
                )

                for chunk in response_stream:
                    if chunk.text:
                        report_text += chunk.text
                        char_count += len(chunk.text)
                        progress.update(task, description=f"{char_count:,} 字已生成{attempt_label}")

                return report_text  # success

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

    return report_text  # unreachable, satisfies type checker


def generate_report_with_claude(chat_history: str, api_key: str) -> str:
    """Generate daily report using Claude with streaming and retry on connection errors."""
    import anthropic
    import httpx

    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=httpx.Timeout(600.0, connect=30.0),
    )

    user_message = f"{SUMMARY_PROMPT}\n\n--- 聊天记录 ---\n\n{chat_history}"

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

def _toc_slugify(value: str, separator: str) -> str:
    """Hex-encode UTF-8 bytes to produce a safe, unique ASCII HTML ID."""
    return value.strip().encode('utf-8').hex()


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
        border-left: 4pt solid #93c5fd;
        border-radius: 0 20pt 20pt 0;
        margin: 12pt 0;
        padding: 8pt 16pt;
        color: #374151;
        background: #f0f5ff;
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

    a {
        color: #1a56db;
        text-decoration: none;
    }

    .toc {
        background: #f0f5ff;
        border: 1pt solid #93c5fd;
        border-radius: 6pt;
        padding: 14pt 20pt;
        margin: 16pt 0 24pt 0;
    }

    .toc ul {
        margin: 4pt 0;
        padding-left: 20pt;
    }

    .toc li {
        margin: 5pt 0;
    }

    .back-to-toc {
        float: right;
        font-weight: normal;
        line-height: 1;
    }

    .back-to-toc a {
        font-size: 0.6em;
        color: #1a56db;
        background: #e8f0fe;
        border: 0.5pt solid #93c5fd;
        border-radius: 100pt;
        padding: 3pt 8pt;
    }
    """


def _get_report_date() -> str:
    """Return today's date, or yesterday's if it is just past midnight (before 04:00)."""
    now = datetime.now()
    if now.hour < 4:
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        console.print(
            f"[yellow]当前时间 {now.strftime('%H:%M')}（刚过午夜），"
            f"使用昨天日期: {yesterday}[/yellow]"
        )
        return yesterday
    return now.strftime("%Y-%m-%d")


def archive_old_files() -> None:
    """Move PDFs older than 7 days from archive/ into archive/YYYY/MM/ subdirs."""
    archive_dir = OUTPUT_DIR / "archive"
    if not archive_dir.exists():
        return

    cutoff = (datetime.now() - timedelta(days=7)).date()
    moved = 0

    for pdf in sorted(archive_dir.glob("*.pdf")):
        m = re.match(r'^(\d{4}-\d{2}-\d{2})\b', pdf.stem)
        if not m:
            continue
        file_date = datetime.strptime(m.group(1), '%Y-%m-%d').date()
        if file_date >= cutoff:
            continue

        dest_dir = archive_dir / file_date.strftime('%Y') / file_date.strftime('%m')
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / pdf.name
        if dest.exists():
            counter = 2
            while dest.exists():
                dest = dest_dir / f"{pdf.stem} ({counter}){pdf.suffix}"
                counter += 1
        pdf.rename(dest)
        moved += 1

    if moved:
        console.print(f"[dim]已将 {moved} 个旧日报归档至年/月子目录[/dim]")


def _get_pdf_path(recording_date: str) -> Path:
    """Return a unique PDF path inside archive/, never overwriting an existing file."""
    archive_dir = OUTPUT_DIR / "archive"
    archive_dir.mkdir(exist_ok=True)
    stem = f"{recording_date} 群聊日报"
    path = archive_dir / f"{stem}.pdf"
    counter = 2
    while path.exists():
        path = archive_dir / f"{stem} ({counter}).pdf"
        counter += 1
    return path


DEBUG_DIR = Path(__file__).parent / "debug"


def save_debug_markdown(date_str: str, markdown_text: str) -> Path:
    """Save markdown to debug/YYYY-MM-DD.md for inspection."""
    DEBUG_DIR.mkdir(exist_ok=True)
    path = DEBUG_DIR / f"{date_str}.md"
    path.write_text(markdown_text, encoding="utf-8")
    return path


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

        converter = md_lib.Markdown(extensions=[
            "tables", "fenced_code", "nl2br",
            TocExtension(slugify=_toc_slugify, toc_depth="2-3"),
        ])
        html_body = converter.convert(markdown_text)

        # Add id to TOC div so headings can link back to it
        html_body = html_body.replace('<div class="toc">', '<div class="toc" id="toc">', 1)
        # Insert a "back to TOC" arrow inside each H2 / H3 heading
        html_body = re.sub(
            r'(</h[23]>)',
            r'<span class="back-to-toc"><a href="#toc">↑ 目录</a></span>\1',
            html_body,
        )

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

def _run_db_pipeline(date_str: str, summary_model: str,
                     gemini_key: str, anthropic_key: str) -> None:
    """Extract from DB for *date_str*, generate report, and save PDF."""
    # Step A: Extract from database
    console.rule(f"[bold]数据库提取  [cyan]{date_str}[/cyan]")
    chat_history = extract_chat_from_db(date_str)
    console.print(
        f"[green]提取完毕[/green] "
        f"[dim]{len(chat_history):,} 字符，"
        f"{chat_history.count(chr(10)) + 1} 行[/dim]\n"
    )
    if not chat_history.strip():
        console.print(f"[yellow]  {date_str} 当天无消息，跳过[/yellow]")
        return

    # Step B: Generate report
    if summary_model == "claude":
        console.rule(f"[bold]Claude 生成日报  [dim]({CLAUDE_MODEL})[/dim]")
        report_markdown = generate_report_with_claude(chat_history, anthropic_key)
    else:
        console.rule(f"[bold]Gemini 生成日报  [dim]({GEMINI_SUMMARY_MODEL})[/dim]")
        report_markdown = generate_report_with_gemini(chat_history, gemini_key)
    console.print(f"[green]日报生成完毕[/green] [dim]{len(report_markdown):,} 字符[/dim]\n")

    # Step C: PDF
    console.rule("[bold]导出 PDF")
    pdf_path = _get_pdf_path(date_str)
    convert_to_pdf(report_markdown, pdf_path)
    md_path = save_debug_markdown(date_str, report_markdown)
    console.print(f"[green]已保存:[/green] [cyan]{pdf_path}[/cyan]")
    console.print(f"[green]Markdown:[/green] [dim]{md_path}[/dim]\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="微信群聊日报生成器")
    parser.add_argument(
        "--summary", choices=["gemini", "claude"], default="claude",
        help="总结模型：claude（默认，claude-opus-4-6）或 gemini（gemini-3.0-pro）",
    )
    parser.add_argument(
        "--video", action="store_true",
        help="使用屏幕录像 + Gemini OCR 模式（默认为数据库模式）",
    )
    parser.add_argument(
        "--allow-incomplete", action="store_true",
        help="也为最后一天（尚未过午夜+1小时）生成不完整日报",
    )
    args = parser.parse_args()

    console.print(Panel.fit(
        "[bold cyan]微信群聊日报生成器[/bold cyan]\n"
        "[dim]WeChat Group Chat Daily Report Generator[/dim]",
        border_style="cyan",
    ))

    try:
        # Step 1: API Keys
        console.rule("[bold]Step 1  API Key 配置")
        gemini_key, anthropic_key = load_or_prompt_api_keys(need_anthropic=(args.summary == "claude"))
        console.print("[green]API Keys 就绪[/green]\n")

        # ── DB mode (default) ─────────────────────────────────────────────────
        if not args.video:
            archive_old_files()
            console.rule("[bold]Step 2  检测缺失日期")
            missing = find_missing_dates(allow_incomplete=args.allow_incomplete)
            if not missing:
                console.print("[green]archive 已是最新，无需生成新日报。[/green]")
                return
            console.print(
                f"发现 [cyan]{len(missing)}[/cyan] 个缺失日期: "
                + ", ".join(f"[cyan]{d}[/cyan]" for d in missing) + "\n"
            )
            for date_str in missing:
                _run_db_pipeline(date_str, args.summary, gemini_key, anthropic_key)

            console.print(Panel.fit(
                f"[bold green]完成！[/bold green]\n"
                f"共生成 [cyan]{len(missing)}[/cyan] 份日报",
                border_style="green",
                title="Success",
            ))
            return

        # ── Video mode (opt-in with --video) ─────────────────────────────────

        # Determine date early so we can check for a cached debug file
        recording_date = _get_report_date()
        debug_dir = OUTPUT_DIR / "debug"
        debug_dir.mkdir(exist_ok=True)
        debug_filename = f"gemini_output_{recording_date}.txt"
        debug_file = debug_dir / debug_filename

        chat_history = ""

        if debug_file.exists():
            # Skip video processing — use cached extraction result
            console.rule("[bold]Step 2  使用缓存的聊天记录")
            chat_history = debug_file.read_text(encoding="utf-8")
            console.print(
                f"[green]已加载缓存文件:[/green] [cyan]debug/{debug_filename}[/cyan] "
                f"[dim]({len(chat_history):,} 字符)[/dim]\n"
            )
        else:
            # Step 2: Find video
            console.rule("[bold]Step 2  查找屏幕录像")
            video_path = find_latest_screen_recording()
            size_mb = video_path.stat().st_size / 1024 / 1024
            console.print(
                f"[green]找到文件:[/green] [cyan]{video_path.name}[/cyan] "
                f"[dim]({size_mb:.1f} MB)[/dim]"
            )

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
                (debug_dir / debug_filename).write_text(chat_history, encoding="utf-8")
                console.print(
                    f"[green]聊天记录提取完毕[/green] "
                    f"[dim]{len(chat_history):,} 字符[/dim] "
                    f"[dim](已保存至 debug/{debug_filename})[/dim]\n"
                )

        # Step 5: Generate report
        if args.summary == "claude":
            console.rule(f"[bold]Step 5  Claude 生成日报 [dim]({CLAUDE_MODEL})[/dim]")
            report_markdown = generate_report_with_claude(chat_history, anthropic_key)
        else:
            console.rule(f"[bold]Step 5  Gemini 生成日报 [dim]({GEMINI_SUMMARY_MODEL})[/dim]")
            report_markdown = generate_report_with_gemini(chat_history, gemini_key)
        console.print(
            f"[green]日报生成完毕[/green] "
            f"[dim]{len(report_markdown):,} 字符[/dim]\n"
        )

        # Step 6: PDF
        console.rule("[bold]Step 6  导出 PDF")
        console.print(f"[green]日报日期:[/green] [cyan]{recording_date}[/cyan]\n")
        pdf_path = _get_pdf_path(recording_date)
        convert_to_pdf(report_markdown, pdf_path)
        md_path = save_debug_markdown(recording_date, report_markdown)
        console.print(f"[dim]Markdown: {md_path}[/dim]")
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
