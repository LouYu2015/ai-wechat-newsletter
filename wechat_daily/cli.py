"""CLI entry point and main pipeline orchestration."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, date as date_type
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, TimeElapsedColumn,
)

from .aliases import AliasDB
from .archiver import archive_old_files, get_pdf_path
from .chat_extractor import extract_messages, find_missing_dates
from .config import CLAUDE_MODEL, DEBUG_DIR, GEMINI_SUMMARY_MODEL, PROJECT_ROOT
from .contacts import ContactMap
from .llm_extractor import ExtractionError, extract_report, generate_markdown_with_gemini
from .pdf import convert_to_pdf
from .message_parser import MSG_TAP, MSG_SYSTEM
from .privacy import ClaudeLeakConfirmer, LeakDetected, format_tokenized_messages, leak_check, tokenize_messages
from .publisher import commit, preview, push_pending, write_post
from .renderer import render_group, render_public
from .roster import build_roster, format_roster

console = Console()


def _format_raw_messages(messages, contact_map: ContactMap) -> str:
    """Legacy-mode formatter: real nicknames, no anonymization."""
    lines: list[str] = []
    for msg in messages:
        ts = datetime.fromtimestamp(msg.create_time).strftime('%H:%M')
        if msg.local_type == MSG_SYSTEM:
            lines.append(f"[{ts}] [系统] {msg.content}")
            continue
        if msg.local_type == MSG_TAP:
            lines.append(f"[{ts}] {msg.content}")
            continue
        name = contact_map.by_wxid(msg.sender_wxid) if msg.sender_wxid else ''
        line = f"[{ts}] {name}: {msg.content}" if name else f"[{ts}] {msg.content}"
        if msg.quoted:
            line += f"\n  > 引用 {msg.quoted.content}"
        lines.append(line)
    return '\n'.join(lines)


# ── API key helpers ─────────────────────────────────────────────────────────────

def _ensure_api_keys(need_anthropic: bool) -> tuple[str, str]:
    env_path = PROJECT_ROOT / ".env"
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
        existing = env_path.read_text(encoding='utf-8') if env_path.exists() else ''
        lines = [
            line for line in existing.splitlines()
            if not line.startswith("GEMINI_API_KEY=")
            and not line.startswith("ANTHROPIC_API_KEY=")
        ]
        lines.append(f"GEMINI_API_KEY={gemini_key}")
        if anthropic_key:
            lines.append(f"ANTHROPIC_API_KEY={anthropic_key}")
        env_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        console.print("[green]API Keys 已保存到 .env[/green]")

    return gemini_key, anthropic_key


# ── Single-date pipeline (Claude structured path) ───────────────────────────────

def _run_db_pipeline(
    date_str: str,
    anthropic_key: str,
    alias_db: AliasDB,
    contact_map: ContactMap,
) -> None:
    """Full pipeline for one date: extract → tokenize → LLM → render → PDF + public."""

    target_date = datetime.strptime(date_str, '%Y-%m-%d').date()

    # ── A: Extract raw messages ─────────────────────────────────────────────
    console.rule(f"[bold]数据库提取  [cyan]{date_str}[/cyan]")
    messages = extract_messages(date_str, contact_map)
    if not messages:
        console.print(f"[yellow]  {date_str} 当天无消息，跳过[/yellow]")
        return

    # ── B: Tokenize + optout masking ────────────────────────────────────────
    console.rule("[bold]隐私处理（token化 + optout遮蔽）")
    total_msgs = len(messages)
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]token化中..."),
        TextColumn("[dim]{task.completed}/{task.total} 条[/dim]"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as _prog:
        _task = _prog.add_task("", total=total_msgs)

        def _tok_cb(current: int, _total: int) -> None:
            _prog.update(_task, completed=current)

        tokenized, token_map = tokenize_messages(
            messages, contact_map, alias_db, progress_cb=_tok_cb
        )
    chat_history = format_tokenized_messages(tokenized)
    roster_entries = build_roster(token_map, contact_map, alias_db)
    roster_text = format_roster(roster_entries)
    console.print(
        f"[green]token化完毕[/green] "
        f"[dim]{len(chat_history):,} 字符，{chat_history.count(chr(10)) + 1} 行；"
        f"花名册 {len(roster_entries)} 条[/dim]\n"
    )

    # ── C: Claude structured extraction ─────────────────────────────────────
    console.rule(f"[bold]Claude 结构化提取  [dim]({CLAUDE_MODEL})[/dim]")
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]Claude 正在分析群聊..."),
        TextColumn("[dim]{task.description}[/dim]"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("连接中...", total=None)

        def progress_cb(received: int, attempt: int) -> None:
            label = f" (第 {attempt}/3 次)" if attempt > 1 else ""
            progress.update(task, description=f"已接收 {received:,} 字节{label}")

        try:
            report = extract_report(
                date_str, chat_history, anthropic_key, progress_cb,
                roster_text=roster_text or None,
            )
        except ExtractionError as e:
            console.print(f"[bold red]结构化提取失败:[/bold red] {e}")
            return

    n_sections = len(report.sections)
    n_safe = sum(1 for s in report.sections if s.public_safe)
    console.print(
        f"[green]提取完毕[/green] [dim]{n_sections} 个章节，{n_safe} 个公开版可发布[/dim]\n"
    )

    # ── D: Render group version → PDF ───────────────────────────────────────
    console.rule("[bold]渲染群内版 Markdown → PDF")

    # Only include commands from today's date in the instruction log
    day_log = [
        e for e in alias_db.command_log()
        if datetime.fromtimestamp(e['ts']).date() == target_date
    ]
    group_md = render_group(report, alias_db, contact_map, day_log, token_map=token_map)

    DEBUG_DIR.mkdir(exist_ok=True, parents=True)
    md_path = DEBUG_DIR / f"{date_str}.md"
    md_path.write_text(group_md, encoding='utf-8')

    pdf_path = get_pdf_path(date_str)
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Markdown → PDF...", total=None)
        convert_to_pdf(group_md, pdf_path)
        progress.update(task, description=f"PDF 已保存: {pdf_path.name}")

    console.print(f"[green]群内版:[/green] [cyan]{pdf_path}[/cyan]")
    console.print(f"[green]Markdown:[/green] [dim]{md_path}[/dim]\n")

    # ── E: Render public version + leak check ───────────────────────────────
    console.rule("[bold]渲染公开版 + 泄漏检测")
    public_md = render_public(report, alias_db, token_map=token_map)

    confirmer = ClaudeLeakConfirmer(api_key=anthropic_key)
    try:
        leak_check(public_md, contact_map, alias_db, confirmer)
    except LeakDetected as e:
        console.print(f"[bold red]泄漏检测失败，公开版已中止:[/bold red] {e}")
        _save_leak_debug(date_str, str(e), public_md)
        console.print("[yellow]群内版 PDF 不受影响。[/yellow]")
        return

    console.print("[green]泄漏检测通过[/green]\n")

    # ── F: Write public post + local commit (NO push — -y handles that) ─────
    post_path = write_post(date_str, public_md)
    committed = commit(date_str)
    if committed:
        console.print(f"[green]公开版已本地 commit:[/green] [dim]{post_path}[/dim]")
    else:
        console.print(f"[dim]公开版内容未变，跳过 commit: {post_path}[/dim]")

    # Preview (open browser so author can spot-check before next -y push)
    preview_path = preview(date_str, public_md, open_browser=True)
    console.print(f"[dim]预览: {preview_path}[/dim]")

    console.print(
        "\n[dim]公开版已本地 commit（未推送）。"
        "下次运行带 -y 可推送到 GitHub，GitHub Pages 将自动构建。[/dim]"
    )


def _save_leak_debug(date_str: str, error: str, public_md: str) -> None:
    import json
    DEBUG_DIR.mkdir(exist_ok=True, parents=True)
    path = DEBUG_DIR / f"leak-{date_str}.json"
    path.write_text(
        json.dumps({"error": error, "public_md_snippet": public_md[:2000]},
                   ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    console.print(f"[dim]泄漏详情已保存至 {path}[/dim]")


# ── Gemini legacy pipeline ───────────────────────────────────────────────────────

def _run_gemini_pipeline(
    date_str: str,
    gemini_key: str,
    contact_map: ContactMap,
    alias_db: AliasDB,
) -> None:
    """Gemini path: tokenize → legacy Markdown (no structured extraction)."""
    console.rule(f"[bold]数据库提取  [cyan]{date_str}[/cyan]")
    messages = extract_messages(date_str, contact_map)
    if not messages:
        console.print(f"[yellow]  {date_str} 当天无消息，跳过[/yellow]")
        return

    # Legacy path keeps real nicknames — simpler, no tokenization.
    chat_history = _format_raw_messages(messages, contact_map)

    console.rule(f"[bold]Gemini 生成日报  [dim]({GEMINI_SUMMARY_MODEL})[/dim]")
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]Gemini 正在生成日报..."),
        TextColumn("[dim]{task.description}[/dim]"),
        TimeElapsedColumn(),
        console=console, transient=False,
    ) as progress:
        task = progress.add_task("0 字", total=None)

        def cb(n: int, attempt: int) -> None:
            label = f" (第 {attempt}/3 次)" if attempt > 1 else ""
            progress.update(task, description=f"{n:,} 字已生成{label}")

        report_markdown = generate_markdown_with_gemini(chat_history, gemini_key, cb)

    console.print(f"[green]日报生成完毕[/green] [dim]{len(report_markdown):,} 字符[/dim]\n")

    DEBUG_DIR.mkdir(exist_ok=True, parents=True)
    md_path = DEBUG_DIR / f"{date_str}.md"
    md_path.write_text(report_markdown, encoding='utf-8')

    console.rule("[bold]导出 PDF")
    pdf_path = get_pdf_path(date_str)
    with Progress(
        SpinnerColumn(), TextColumn("[bold blue]{task.description}"),
        TimeElapsedColumn(), console=console, transient=False,
    ) as progress:
        task = progress.add_task("Markdown → PDF...", total=None)
        convert_to_pdf(report_markdown, pdf_path)
        progress.update(task, description=f"PDF 已保存: {pdf_path.name}")

    console.print(f"[green]已保存:[/green] [cyan]{pdf_path}[/cyan]")


# ── Main entry ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="微信群聊日报生成器")
    parser.add_argument(
        "--summary", choices=["gemini", "claude"], default="claude",
        help="总结模型：claude（默认，结构化提取）或 gemini（传统 Markdown）",
    )
    parser.add_argument(
        "--allow-incomplete", action="store_true",
        help="也为最后一天（尚未过午夜+1小时）生成不完整日报",
    )
    parser.add_argument(
        "-y", action="store_true",
        help="推送上次生成的公开版到 GitHub Pages（不影响本次生成流程）",
    )
    args = parser.parse_args()

    console.print(Panel.fit(
        "[bold cyan]微信群聊日报生成器[/bold cyan]\n"
        "[dim]WeChat Group Chat Daily Report Generator[/dim]",
        border_style="cyan",
    ))

    try:
        # Step 1: API keys
        console.rule("[bold]Step 1  API Key 配置")
        gemini_key, anthropic_key = _ensure_api_keys(need_anthropic=(args.summary == "claude"))
        console.print("[green]API Keys 就绪[/green]\n")

        # Step 2: Push PREVIOUS run's pending commits (-y semantics per §7.6)
        if args.y:
            console.rule("[bold]Step 2  推送上次未推送的公开版")
            try:
                pushed = push_pending()
                console.print(
                    "[green]已推送。[/green]\n" if pushed
                    else "[dim]无待推送 commit。[/dim]\n"
                )
            except RuntimeError as e:
                console.print(f"[yellow]推送失败: {e}[/yellow]\n")

        # Step 3: Load contacts + alias DB; scan commands incrementally
        console.rule("[bold]Step 3  加载联系人与别名数据库")
        contact_map = ContactMap.from_db()
        alias_db = AliasDB.load()
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]正在扫描新指令 (/alias /optout /optin)..."),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as _prog:
            _prog.add_task("", total=None)
            alias_db.scan_commands(contact_map)
        alias_db.save()
        console.print("[green]别名数据库已更新[/green]\n")

        # Step 4: Archive old PDFs
        moved = archive_old_files()
        if moved:
            console.print(f"[dim]已将 {moved} 个旧日报归档至年/月子目录[/dim]\n")

        # Step 5: Find missing dates
        console.rule("[bold]Step 4  检测缺失日期")
        missing = find_missing_dates(allow_incomplete=args.allow_incomplete)
        if not missing:
            console.print("[green]archive 已是最新，无需生成新日报。[/green]")
            return
        console.print(
            f"发现 [cyan]{len(missing)}[/cyan] 个缺失日期: "
            + ", ".join(f"[cyan]{d}[/cyan]" for d in missing) + "\n"
        )

        # Step 6: Generate reports
        for date_str in missing:
            if args.summary == "claude":
                _run_db_pipeline(date_str, anthropic_key, alias_db, contact_map)
            else:
                _run_gemini_pipeline(date_str, gemini_key, contact_map, alias_db)
            # Persist any tokens lazily allocated during this date's pipeline
            # so subsequent runs keep the same names.
            alias_db.save()

        console.print(Panel.fit(
            f"[bold green]完成！[/bold green]\n共生成 [cyan]{len(missing)}[/cyan] 份日报",
            border_style="green",
            title="Success",
        ))

    except KeyboardInterrupt:
        console.print("\n[yellow]用户中断[/yellow]")
        sys.exit(1)
    except FileNotFoundError as e:
        console.print(f"\n[bold red]文件未找到:[/bold red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]错误:[/bold red] {e}")
        raise
