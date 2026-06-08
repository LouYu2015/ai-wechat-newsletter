"""CLI entry point and main pipeline orchestration."""

from __future__ import annotations

import argparse
import os
import sys
from collections import deque
from datetime import datetime, timedelta, date as date_type
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, TimeElapsedColumn,
)

from .aliases import AliasDB
from .archiver import archive_old_files, get_pdf_path
from .chat_extractor import extract_messages, find_missing_dates
from .config import (
    ARCHIVE_DIR, CLAUDE_MODEL, DEBUG_DIR, DEEPSEEK_REPORT_MODEL,
    GEMINI_CAPTION_MODEL, GEMINI_SUMMARY_MODEL, LINK_SUMMARY_MODEL,
    PROJECT_ROOT, debug_dir_for, get_deepseek_key, get_gemini_key,
)
from .contacts import ContactMap
from . import cost_tracker
from .llm_extractor import (
    ExtractionError, extract_report, extract_report_deepseek,
    generate_markdown_with_gemini,
)
from .pdf import convert_to_pdf
from .image_captioner import caption_images, count_image_targets
from .message_parser import MSG_TAP, MSG_SYSTEM
from .prior_report import (
    load_prior_report_titles,
    load_prior_reports,
    missing_prior_dates,
)
from .privacy import (
    LeakDetected, format_tokenized_messages, format_tokenized_messages_blocks,
    leak_check, tokenize_messages,
)
from .publisher import commit, preview, push_pending, write_post
from .renderer import render_group, render_public
from .roster import build_roster, format_roster
from .url_enricher import count_link_targets, enrich_link_messages

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


def _ensure_deepseek_key() -> str:
    """Ensure DEEPSEEK_API_KEY is in the environment (link summaries + compare
    report both need it); prompt and persist to .env if missing."""
    env_path = PROJECT_ROOT / ".env"
    load_dotenv(env_path)
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key

    console.print(
        "[yellow]需要 DeepSeek API Key（链接摘要 + 对比版日报，将保存到 .env）[/yellow]"
    )
    key = console.input("[bold]请输入 DEEPSEEK_API_KEY: [/bold]").strip()
    if key:
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        lines = [
            line for line in existing.splitlines()
            if not line.startswith("DEEPSEEK_API_KEY=")
        ]
        lines.append(f"DEEPSEEK_API_KEY={key}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ["DEEPSEEK_API_KEY"] = key
        console.print("[green]DEEPSEEK_API_KEY 已保存到 .env[/green]")
    return key


# ── Single-date pipeline (Claude structured path) ───────────────────────────────

def _run_db_pipeline(
    date_str: str,
    anthropic_key: str,
    alias_db: AliasDB,
    contact_map: ContactMap,
    prior_days: int = 3,
    prior_title_days: int = 7,
    prompt_on_missing_prior: bool = True,
    cost_records: list[cost_tracker.CostRecord] | None = None,
) -> None:
    """Full pipeline for one date: extract → tokenize → LLM → render → PDF + public.

    *prior_days* — how many prior daily reports to feed the model **in full**
    for cross-day de-dup / continuation. ``0`` disables the feature.
    *prior_title_days* — how many prior days to feed as a **titles-only**
    outline (``##`` / ``###`` headers, no body). The titles window is clamped
    to ``>= prior_days``; the extra days (those beyond *prior_days*) extend
    the de-dup / ``[[ref:]]`` window cheaply. ``0`` disables it.
    *prompt_on_missing_prior* — when *some* of the last *prior_days* are
    available but others aren't (suggesting a recent gap, not a fresh start),
    ask the user whether to proceed.
    """

    target_date = datetime.strptime(date_str, '%Y-%m-%d').date()

    # ── A: Extract raw messages ─────────────────────────────────────────────
    console.rule(f"[bold]数据库提取  [cyan]{date_str}[/cyan]")
    messages = extract_messages(date_str, contact_map)
    if not messages:
        console.print(f"[yellow]  {date_str} 当天无消息，跳过[/yellow]")
        return

    # ── A2: Fetch/summarize link-card targets before tokenization ───────────
    link_count = count_link_targets(messages)
    if link_count:
        console.rule(
            f"[bold]链接增强  [dim]({LINK_SUMMARY_MODEL}, no thinking)[/dim]"
        )
        link_progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]处理链接..."),
            TextColumn("[dim]{task.description}[/dim]"),
            TimeElapsedColumn(),
            console=console,
        )
        link_task = link_progress.add_task(f"0/{link_count}", total=link_count)
        link_lines: deque[str] = deque(maxlen=8)
        link_state = {"partial": "", "current": 0}

        def _link_render() -> Group:
            parts: list = []
            visible = list(link_lines)
            if link_state["partial"]:
                visible.append(link_state["partial"])
            width = max(1, console.width)
            rows: list[Text] = []
            for s in visible:
                rows.extend(Text(s).wrap(console, width))
            rows = rows[-8:]
            if rows:
                parts.append(Text("─ 摘要 ─", style="cyan"))
                parts.extend(rows)
            parts.append(link_progress)
            return Group(*parts)

        with Live(_link_render(), console=console, refresh_per_second=10, transient=False) as live:
            def _refresh() -> None:
                live.update(_link_render())

            def _link_progress_cb(current: int, total: int, phase: str, label: str) -> None:
                if current != link_state["current"]:
                    if link_state["partial"]:
                        link_lines.append(link_state["partial"])
                        link_state["partial"] = ""
                    link_state["current"] = current
                link_progress.update(
                    link_task,
                    completed=max(0, current - 1),
                    description=f"{current}/{total} {phase}: {label}",
                )
                _refresh()

            def _link_delta_cb(delta: str) -> None:
                combined = link_state["partial"] + delta
                if "\n" in combined:
                    *complete, remainder = combined.split("\n")
                    for line in complete:
                        link_lines.append(line)
                    link_state["partial"] = remainder
                else:
                    link_state["partial"] = combined
                _refresh()

            def _link_usage_cb(usage, duration_s: float, input_chars: int) -> None:
                record = cost_tracker.log_call(
                    date=date_str, stage="link", model=LINK_SUMMARY_MODEL,
                    usage=usage, duration_s=duration_s, input_chars=input_chars,
                )
                if cost_records is not None:
                    cost_records.append(record)

            stats = enrich_link_messages(
                messages,
                anthropic_key,
                progress_cb=_link_progress_cb,
                summary_delta_cb=_link_delta_cb,
                usage_cb=_link_usage_cb,
            )
            link_progress.update(
                link_task,
                completed=stats.total,
                description=(
                    f"完成：抓取 {stats.fetched}，摘要 {stats.summarized}，"
                    f"太短 {stats.short}，失败 {stats.failed}"
                ),
            )
            _refresh()
        console.print(
            f"[green]链接增强完毕[/green] "
            f"[dim]链接 {stats.total} 个；摘要 {stats.summarized} 个；"
            f"太短 {stats.short} 个；失败 {stats.failed} 个[/dim]\n"
        )

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
    n_images = sum(1 for m in tokenized if m.image_md5)
    console.print(
        f"[green]token化完毕[/green] "
        f"[dim]{len(chat_history):,} 字符，{chat_history.count(chr(10)) + 1} 行；"
        f"花名册 {len(roster_entries)} 条；图片 {n_images} 张[/dim]\n"
    )

    # ── B2: Load prior days' reports for cross-day de-dup / continuation ────
    prior_reports: list[tuple[str, str]] = []
    prior_report_titles: list[tuple[str, str]] = []
    if prior_days > 0:
        prior_reports = load_prior_reports(date_str, n_days=prior_days)
        gaps = missing_prior_dates(date_str, n_days=prior_days)
        if prior_reports:
            console.print(
                f"[dim]载入前 {len(prior_reports)} 天完整日报："
                f"{', '.join(d for d, _ in prior_reports)}[/dim]"
            )
        if gaps and prior_reports and prompt_on_missing_prior:
            # Partial gap (some prior days available, some not) — likely a
            # missed run or genuinely empty day. Ask before continuing.
            console.print(
                f"[yellow]最近 {prior_days} 天里 {len(gaps)} 天缺日报：{', '.join(gaps)}[/yellow]"
            )
            choice = console.input(
                "[bold]继续生成今日日报？[c]ontinue / [s]kip 此日 / [a]bort 全部退出 (默认 c): [/bold]"
            ).strip().lower()
            if choice == "s":
                console.print(f"[yellow]按用户要求跳过 {date_str}。[/yellow]\n")
                return
            if choice == "a":
                raise KeyboardInterrupt("用户中止")
        elif gaps and not prior_reports:
            # All prior days missing — first-run / long pause; silent continue.
            console.print(
                f"[dim]最近 {prior_days} 天均无历史日报，按独立日报生成。[/dim]"
            )

    # Titles-only window covers older days (e.g. 4–7 back) on top of the
    # full-body window. Clamp to >= prior_days so we never narrow it; skip
    # dates already loaded with full bodies to avoid duplication.
    title_window = max(prior_title_days, prior_days)
    if title_window > prior_days:
        covered = {d for d, _ in prior_reports}
        prior_report_titles = load_prior_report_titles(
            date_str, n_days=title_window, skip_dates=covered,
        )
        if prior_report_titles:
            console.print(
                f"[dim]载入额外 {len(prior_report_titles)} 天标题大纲："
                f"{', '.join(d for d, _ in prior_report_titles)}[/dim]"
            )
    if prior_days > 0 or prior_report_titles:
        console.print()

    # ── C: Claude structured extraction ─────────────────────────────────────
    console.rule(f"[bold]Claude 结构化提取  [dim]({CLAUDE_MODEL})[/dim]")
    headers: list[Text] = []  # permanent header / status lines
    thinking_lines: deque[str] = deque(maxlen=8)
    body_lines: deque[str] = deque(maxlen=8)
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]Claude 正在分析群聊..."),
        TextColumn("[dim]{task.description}[/dim]"),
        TimeElapsedColumn(),
        console=console,
    )
    task = progress.add_task("连接中...", total=None)
    state = {
        "text": 0,
        "thinking": 0,
        "thinking_partial": "",
        "body_partial": "",
    }

    def _tail_texts(lines: deque[str], partial: str, n: int, style: str) -> list[Text]:
        visible = list(lines)
        if partial:
            visible.append(partial)
        width = max(1, console.width)
        rows: list[Text] = []
        for s in visible:
            wrapped = Text(s, style=style).wrap(console, width)
            rows.extend(wrapped)
        return rows[-n:]

    def render() -> Group:
        parts: list = list(headers)
        th = _tail_texts(thinking_lines, state["thinking_partial"], 8, "dim italic")
        bd = _tail_texts(body_lines, state["body_partial"], 8, "")
        if th:
            parts.append(Text("─ thinking ─", style="dim cyan"))
            parts.extend(th)
        if bd:
            parts.append(Text("─ 正文 ─", style="cyan"))
            parts.extend(bd)
        parts.append(progress)
        return Group(*parts)

    with Live(render(), console=console, refresh_per_second=10, transient=False) as live:
        def refresh() -> None:
            live.update(render())

        def _update_progress(attempt: int) -> None:
            label = f" (第 {attempt}/3 次)" if attempt > 1 else ""
            phase = "思考中" if state["text"] == 0 and state["thinking"] > 0 else "已接收"
            progress.update(
                task,
                description=(
                    f"{phase} 正文 {state['text']:,} 字节 / "
                    f"thinking {state['thinking']:,} 字节{label}"
                ),
            )
            refresh()

        def progress_cb(received: int, attempt: int) -> None:
            state["text"] = received
            _update_progress(attempt)

        def thinking_cb(received: int, attempt: int) -> None:
            state["thinking"] = received
            _update_progress(attempt)

        def text_cb(kind: str, delta: str, attempt: int) -> None:
            partial_key = "thinking_partial" if kind == "thinking" else "body_partial"
            target = thinking_lines if kind == "thinking" else body_lines
            combined = state[partial_key] + delta
            if "\n" in combined:
                *complete, remainder = combined.split("\n")
                for line in complete:
                    target.append(line)
                state[partial_key] = remainder
            else:
                state[partial_key] = combined
            refresh()

        def header_cb(kind: str, level: int, title: str, attempt: int) -> None:
            indent = "  " * level
            headers.append(Text(f"{indent}{title}", style="bold"))
            refresh()

        def attempt_cb(attempt: int) -> None:
            headers.append(Text(f"--- 第 {attempt}/3 次重试 ---", style="yellow"))
            thinking_lines.clear()
            body_lines.clear()
            state["thinking_partial"] = ""
            state["body_partial"] = ""
            refresh()

        import tempfile, time as _time
        from .image_decoder import ImageDecoder

        # Closure for cost logging: extract_report calls usage_cb at the end
        # with (usage, input_chars). We measure duration around the call.
        t_extract_start = _time.perf_counter()

        def _extract_usage_cb(usage, input_chars: int) -> None:
            record = cost_tracker.log_call(
                date=date_str, stage="extract", model=CLAUDE_MODEL,
                usage=usage,
                duration_s=_time.perf_counter() - t_extract_start,
                input_chars=input_chars,
            )
            if cost_records is not None:
                cost_records.append(record)

        try:
            with tempfile.TemporaryDirectory(prefix="wechat_daily_imgs_") as td:
                decoder = ImageDecoder(Path(td))
                chat_blocks = format_tokenized_messages_blocks(tokenized, decoder)
                n_decoded = sum(1 for b in chat_blocks if b.get("type") == "image")
                if n_images:
                    headers.append(Text(
                        f"图片解码 {n_decoded}/{n_images} 张成功",
                        style="dim",
                    ))
                    refresh()
                report = extract_report(
                    date_str, chat_history, anthropic_key, progress_cb,
                    roster_text=roster_text or None,
                    thinking_cb=thinking_cb,
                    header_cb=header_cb,
                    attempt_cb=attempt_cb,
                    text_cb=text_cb,
                    usage_cb=_extract_usage_cb,
                    chat_blocks=chat_blocks,
                    prior_reports=prior_reports or None,
                    prior_report_titles=prior_report_titles or None,
                )
        except ExtractionError as e:
            console.print(f"[bold red]结构化提取失败:[/bold red] {e}")
            return

    md_len = len(report.markdown)
    n_h3 = report.markdown.count("\n### ") + (1 if report.markdown.startswith("### ") else 0)
    n_hidden = report.markdown.count("[章节不公开")
    console.print(
        f"[green]提取完毕[/green] [dim]{md_len:,} 字符，"
        f"{n_h3} 个 ### 子话题，{n_hidden} 个标记不公开[/dim]\n"
    )

    # ── D: Render group version → PDF ───────────────────────────────────────
    console.rule("[bold]渲染群内版 Markdown → PDF")

    # Only include commands from today's date in the instruction log
    day_log = [
        e for e in alias_db.command_log()
        if datetime.fromtimestamp(e['ts']).date() == target_date
    ]
    group_md = render_group(report, alias_db, contact_map, day_log, token_map=token_map)

    debug_day = debug_dir_for(date_str)
    debug_day.mkdir(exist_ok=True, parents=True)
    md_path = debug_day / "group.md"
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

    # ── D2: DeepSeek 对比提取（旁路，仅本地，不发布、不喂续写）────────────────
    # 放在主版本群内版渲染之后、公开版/泄漏检测之前：只要主版本本身生成成功就
    # 产出对比版，不受公开版泄漏检测早退的影响。失败不致命。
    if get_deepseek_key():
        _run_compare_extraction_deepseek(
            date_str=date_str,
            chat_history=chat_history,
            tokenized=tokenized,
            roster_text=roster_text,
            prior_reports=prior_reports,
            prior_report_titles=prior_report_titles,
            alias_db=alias_db,
            contact_map=contact_map,
            token_map=token_map,
            target_date=target_date,
            cost_records=cost_records,
        )
    else:
        console.print("[yellow]未配置 DEEPSEEK_API_KEY，跳过 DeepSeek 对比版。[/yellow]\n")

    # ── E: Render public version + leak check ───────────────────────────────
    console.rule("[bold]渲染公开版 + 泄漏检测")
    public_md = render_public(report, alias_db, token_map=token_map)

    try:
        leak_check(public_md, alias_db)
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


_COMPARE_DEBUG_SUFFIX = ".deepseek-v4-pro"


def _caption_images_for_deepseek(
    date_str: str,
    tokenized: list,
    cost_records: list[cost_tracker.CostRecord] | None,
) -> dict[str, str]:
    """Caption images with Gemini so the text-only DeepSeek path can "see" them.

    Returns ``{image_md5: caption}``. Empty (best-effort, never fatal) when
    there are no images, no ``GEMINI_API_KEY``, or the stage errors — DeepSeek
    then just gets bare ``[图片]`` placeholders. The canonical Claude run is
    unaffected either way (it uses native inline images).
    """
    import tempfile, time as _time
    from .image_decoder import ImageDecoder

    n_images = count_image_targets(tokenized)
    if not n_images:
        return {}
    if not get_gemini_key():
        console.print(
            "[yellow]未配置 GEMINI_API_KEY，DeepSeek 对比版图片仅作 [图片] 占位。[/yellow]\n"
        )
        return {}

    console.rule(
        f"[bold]图片描述  [dim]({GEMINI_CAPTION_MODEL}) — 供 DeepSeek 对比版[/dim]"
    )
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]描述图片..."),
        TextColumn("[dim]{task.description}[/dim]"),
        TimeElapsedColumn(),
        console=console,
    )
    task = progress.add_task(f"0/{n_images}", total=n_images)

    def _progress_cb(current: int, total: int, label: str) -> None:
        progress.update(task, completed=current, description=f"{current}/{total} {label}")

    def _usage_cb(usage, duration_s: float, input_chars: int) -> None:
        record = cost_tracker.log_call(
            date=date_str, stage="caption", model=GEMINI_CAPTION_MODEL,
            usage=usage, duration_s=duration_s, input_chars=input_chars,
        )
        if cost_records is not None:
            cost_records.append(record)

    captions: dict[str, str] = {}
    try:
        with progress, tempfile.TemporaryDirectory(prefix="wechat_daily_caps_") as td:
            decoder = ImageDecoder(Path(td))
            captions, stats = caption_images(
                tokenized, decoder, get_gemini_key(),
                progress_cb=_progress_cb, usage_cb=_usage_cb,
            )
    except Exception as e:
        console.print(f"[yellow]图片描述失败，跳过（DeepSeek 看占位符）：{e}[/yellow]\n")
        return {}

    console.print(
        f"[green]图片描述完毕[/green] [dim]图片 {stats.total} 张；"
        f"成功 {stats.captioned}；跳过 {stats.skipped}；失败 {stats.failed}[/dim]\n"
    )
    return captions


def _run_compare_extraction_deepseek(
    *,
    date_str: str,
    chat_history: str,
    tokenized: list,
    roster_text: str,
    prior_reports: list[tuple[str, str]],
    prior_report_titles: list[tuple[str, str]],
    alias_db: AliasDB,
    contact_map: ContactMap,
    token_map: dict,
    target_date,
    cost_records: list[cost_tracker.CostRecord] | None,
) -> None:
    """Second-pass report generation with DeepSeek V4 Pro (AB-test compare).

    Writes debug sidecars with ``.deepseek-v4-pro`` suffix and a PDF named
    ``{date} 群聊日报 (deepseek-v4-pro).pdf``. Never feeds back into next-day
    continuity (which reads the un-suffixed ``debug/extract-{date}.md``) and
    never touches the public repo. Any failure prints a warning and returns
    silently — the canonical Opus 4.6 run has already shipped.
    """
    import time as _time

    # Vision preprocessing: DeepSeek can't see images, so caption them with
    # Gemini and inline the descriptions as `[图片：…]` for this path only.
    captions = _caption_images_for_deepseek(date_str, tokenized, cost_records)
    if captions:
        chat_history = format_tokenized_messages(tokenized, captions)

    console.rule(
        f"[bold]对比提取  [dim]({DEEPSEEK_REPORT_MODEL}) — 仅本地，不发布[/dim]"
    )

    headers: list[Text] = []
    thinking_lines: deque[str] = deque(maxlen=8)
    body_lines: deque[str] = deque(maxlen=8)
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold magenta]DeepSeek 对比生成..."),
        TextColumn("[dim]{task.description}[/dim]"),
        TimeElapsedColumn(),
        console=console,
    )
    task = progress.add_task("连接中...", total=None)
    state = {"text": 0, "thinking": 0, "thinking_partial": "", "body_partial": ""}

    def _tail(lines: deque[str], partial: str, n: int, style: str) -> list[Text]:
        visible = list(lines)
        if partial:
            visible.append(partial)
        width = max(1, console.width)
        rows: list[Text] = []
        for s in visible:
            rows.extend(Text(s, style=style).wrap(console, width))
        return rows[-n:]

    def render() -> Group:
        parts: list = list(headers)
        th = _tail(thinking_lines, state["thinking_partial"], 8, "dim italic")
        bd = _tail(body_lines, state["body_partial"], 8, "")
        if th:
            parts.append(Text("─ thinking ─", style="dim cyan"))
            parts.extend(th)
        if bd:
            parts.append(Text("─ 正文 ─", style="magenta"))
            parts.extend(bd)
        parts.append(progress)
        return Group(*parts)

    with Live(render(), console=console, refresh_per_second=10, transient=False) as live:
        def refresh() -> None:
            live.update(render())

        def _update_progress(attempt: int) -> None:
            label = f" (第 {attempt}/3 次)" if attempt > 1 else ""
            phase = "思考中" if state["text"] == 0 and state["thinking"] > 0 else "已接收"
            progress.update(
                task,
                description=(
                    f"{phase} 正文 {state['text']:,} 字节 / "
                    f"thinking {state['thinking']:,} 字节{label}"
                ),
            )
            refresh()

        def progress_cb(received: int, attempt: int) -> None:
            state["text"] = received
            _update_progress(attempt)

        def thinking_cb(received: int, attempt: int) -> None:
            state["thinking"] = received
            _update_progress(attempt)

        def text_cb(kind: str, delta: str, attempt: int) -> None:
            key = "thinking_partial" if kind == "thinking" else "body_partial"
            target = thinking_lines if kind == "thinking" else body_lines
            combined = state[key] + delta
            if "\n" in combined:
                *complete, remainder = combined.split("\n")
                for line in complete:
                    target.append(line)
                state[key] = remainder
            else:
                state[key] = combined
            refresh()

        def header_cb(_kind: str, level: int, title: str, _attempt: int) -> None:
            headers.append(Text(f"{'  ' * level}{title}", style="bold"))
            refresh()

        def attempt_cb(attempt: int) -> None:
            headers.append(Text(f"--- 第 {attempt}/3 次重试 ---", style="yellow"))
            thinking_lines.clear()
            body_lines.clear()
            state["thinking_partial"] = ""
            state["body_partial"] = ""
            refresh()

        t_start = _time.perf_counter()

        def _usage_cb(usage, input_chars: int) -> None:
            record = cost_tracker.log_call(
                date=date_str, stage="extract-compare", model=DEEPSEEK_REPORT_MODEL,
                usage=usage,
                duration_s=_time.perf_counter() - t_start,
                input_chars=input_chars,
            )
            if cost_records is not None:
                cost_records.append(record)

        try:
            compare_report = extract_report_deepseek(
                date_str, chat_history,
                model=DEEPSEEK_REPORT_MODEL,
                debug_suffix=_COMPARE_DEBUG_SUFFIX,
                roster_text=roster_text or None,
                prior_reports=prior_reports or None,
                prior_report_titles=prior_report_titles or None,
                progress_cb=progress_cb,
                thinking_cb=thinking_cb,
                header_cb=header_cb,
                attempt_cb=attempt_cb,
                text_cb=text_cb,
                usage_cb=_usage_cb,
                image_caption_mode=bool(captions),
            )
        except ExtractionError as e:
            console.print(f"[yellow]DeepSeek 对比生成失败，跳过：{e}[/yellow]\n")
            return
        except Exception as e:
            console.print(f"[yellow]DeepSeek 对比生成异常，跳过：{e}[/yellow]\n")
            return

    # Render group version for compare (no public path, no leak check).
    day_log = [
        e for e in alias_db.command_log()
        if datetime.fromtimestamp(e['ts']).date() == target_date
    ]
    compare_group_md = render_group(
        compare_report, alias_db, contact_map, day_log, token_map=token_map,
    )

    debug_day = debug_dir_for(date_str)
    debug_day.mkdir(exist_ok=True, parents=True)
    md_path = debug_day / f"group{_COMPARE_DEBUG_SUFFIX}.md"
    md_path.write_text(compare_group_md, encoding="utf-8")

    ARCHIVE_DIR.mkdir(exist_ok=True)
    # Always exactly one "(deepseek-v4-pro)" file per date, overwriting stale
    # ones on re-runs (matching how the canonical path overwrites debug/{date}.md).
    compare_pdf_path = ARCHIVE_DIR / f"{date_str} 群聊日报 (deepseek-v4-pro).pdf"
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold magenta]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("DeepSeek Markdown → PDF...", total=None)
        convert_to_pdf(compare_group_md, compare_pdf_path)
        progress.update(task, description=f"PDF 已保存: {compare_pdf_path.name}")

    console.print(f"[magenta]DeepSeek 对比版:[/magenta] [cyan]{compare_pdf_path}[/cyan]")
    console.print(f"[magenta]DeepSeek Markdown:[/magenta] [dim]{md_path}[/dim]\n")


def _save_leak_debug(date_str: str, error: str, public_md: str) -> None:
    import json
    debug_day = debug_dir_for(date_str)
    debug_day.mkdir(exist_ok=True, parents=True)
    path = debug_day / "leak.json"
    path.write_text(
        json.dumps({"error": error, "public_md": public_md},
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

    debug_day = debug_dir_for(date_str)
    debug_day.mkdir(exist_ok=True, parents=True)
    md_path = debug_day / "group.md"
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
    parser.add_argument(
        "--prior-days", type=int, default=3,
        help="喂给模型的过往日报**完整正文**天数（用于跨日去重 / 续写）；0 关闭。默认 3。",
    )
    parser.add_argument(
        "--prior-title-days", type=int, default=7,
        help=(
            "喂给模型的过往日报**标题大纲**总天数（前 --prior-days 天用完整正文，"
            "再往前的天数仅传 ## / ### 标题，便于扩大跨日去重 / [[ref:]] 窗口而不爆 token）；"
            "小于 --prior-days 会被静默上调。默认 7。"
        ),
    )
    parser.add_argument(
        "--no-prior-prompt", action="store_true",
        help="最近若干天日报有缺失时不交互询问，按现有材料继续（适合自动化场景）。",
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
        # Claude path now depends on DeepSeek for link summaries + the compare
        # report; make sure the key is present before generation starts.
        if args.summary == "claude":
            _ensure_deepseek_key()
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
        cost_records: list[cost_tracker.CostRecord] = []
        for date_str in missing:
            if args.summary == "claude":
                _run_db_pipeline(
                    date_str, anthropic_key, alias_db, contact_map,
                    prior_days=args.prior_days,
                    prior_title_days=args.prior_title_days,
                    prompt_on_missing_prior=not args.no_prior_prompt,
                    cost_records=cost_records,
                )
            else:
                _run_gemini_pipeline(date_str, gemini_key, contact_map, alias_db)
            # Persist any tokens lazily allocated during this date's pipeline
            # so subsequent runs keep the same names.
            alias_db.save()

        if cost_records:
            console.print()
            console.print(cost_tracker.summarize(cost_records))

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
