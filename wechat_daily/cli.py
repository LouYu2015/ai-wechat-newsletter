"""CLI entry point and main pipeline orchestration."""

from __future__ import annotations

import argparse
import collections
import datetime
import os
import pathlib
import sys

import dotenv
import rich.console
import rich.live
import rich.panel
import rich.progress
import rich.text

from wechat_daily import (
    aliases,
    archiver,
    batch_extractor,
    chat_extractor,
    config,
    contacts,
    cost_tracker,
    coverage,
    lanes_ui,
    llm_extractor,
    pdf,
    prior_report,
    privacy,
    publisher,
    renderer,
    roster,
    url_enricher,
)

console = rich.console.Console()


# ── API key helpers ─────────────────────────────────────────────────────────────


def _ensure_anthropic_key() -> str:
    """Ensure ANTHROPIC_API_KEY is present; prompt and persist to .env if not."""
    env_path = config.PROJECT_ROOT / ".env"
    dotenv.load_dotenv(env_path)
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key

    console.print("[yellow]需要 Anthropic API Key（将保存到 .env 文件）[/yellow]")
    key = console.input("[bold]请输入 ANTHROPIC_API_KEY: [/bold]").strip()
    if key:
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        lines = [
            line for line in existing.splitlines() if not line.startswith("ANTHROPIC_API_KEY=")
        ]
        lines.append(f"ANTHROPIC_API_KEY={key}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ["ANTHROPIC_API_KEY"] = key
        console.print("[green]ANTHROPIC_API_KEY 已保存到 .env[/green]")
    return key


def _ensure_deepseek_key() -> str:
    """Ensure DEEPSEEK_API_KEY is in the environment (link summaries + compare
    report both need it); prompt and persist to .env if missing."""
    env_path = config.PROJECT_ROOT / ".env"
    dotenv.load_dotenv(env_path)
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key

    console.print("[yellow]需要 DeepSeek API Key（链接摘要 + 对比版日报，将保存到 .env）[/yellow]")
    key = console.input("[bold]请输入 DEEPSEEK_API_KEY: [/bold]").strip()
    if key:
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        lines = [line for line in existing.splitlines() if not line.startswith("DEEPSEEK_API_KEY=")]
        lines.append(f"DEEPSEEK_API_KEY={key}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ["DEEPSEEK_API_KEY"] = key
        console.print("[green]DEEPSEEK_API_KEY 已保存到 .env[/green]")
    return key


# ── Single-date pipeline (Claude structured path) ───────────────────────────────


def _run_db_pipeline(
    date_str: str,
    anthropic_key: str,
    alias_db: aliases.AliasDB,
    contact_map: contacts.ContactMap,
    prior_days: int = 3,
    prior_title_days: int = 7,
    prompt_on_missing_prior: bool = True,
    run_compare: bool = True,
    cost_records: list[cost_tracker.CostRecord] | None = None,
    use_batch: bool = True,
    force_resume: bool = False,
    force_resubmit: bool = False,
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
    *run_compare* — also produce the Opus 4.6 AB-compare report (bypass, local
    only). ``False`` (``--no-compare``) skips it to save cost.
    *use_batch* — Batch API (default: 50% pricing, no live streaming); the
    ``--no-batch`` flag flips this off for the legacy streaming path.
    *force_resume* / *force_resubmit* — skip the interactive question when a
    resumable batch state exists (``--resume`` / ``--resubmit``).
    """

    target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()

    # ── A: Extract raw messages ─────────────────────────────────────────────
    console.rule(f"[bold]数据库提取  [cyan]{date_str}[/cyan]")
    messages = chat_extractor.extract_messages(date_str, contact_map)
    # 窗口起点变长后（重叠段 ≥20 条），可能出现「当天一条没有、只有前夜重叠段」的
    # 情况。以「当天 [D-1 21:00, D 21:00) 内有无消息」为准判断空日，而非整个窗口
    # （日界线与 chat_extractor.extract_messages 的 day_start/day_end 一致，只是
    # 不带 ±1h buffer）。
    day_midnight = datetime.datetime(target_date.year, target_date.month, target_date.day)
    day_cutoff = datetime.timedelta(hours=config.DAY_CUTOFF_HOUR)
    day_start_ts = int((day_midnight - datetime.timedelta(days=1) + day_cutoff).timestamp())
    day_end_ts = int((day_midnight + day_cutoff).timestamp())
    if not any(day_start_ts <= m.create_time < day_end_ts for m in messages):
        console.print(f"[yellow]  {date_str} 当天无消息，跳过[/yellow]")
        return

    # ── A1 (batch): fingerprint + resumable-state decision ─────────────────
    # Fingerprint over RAW messages (pre-enrichment): link summaries are
    # nondeterministic, so hashing the final prompt would never match on a
    # re-run (--allow-incomplete re-runs legitimately grow the message set;
    # the mismatch prompt below spells out the consequence either way).
    batch_state: batch_extractor.BatchState | None = None
    fingerprint: tuple[int, str] | None = None
    if use_batch:
        fingerprint = batch_extractor.raw_messages_fingerprint(messages)
        batch_state = _decide_batch_state(
            date_str,
            fingerprint,
            anthropic_key,
            force_resume=force_resume,
            force_resubmit=force_resubmit,
        )

    # ── A2: Fetch/summarize link-card targets before tokenization ───────────
    # Skipped when resuming a submitted batch — the LLM input is already
    # server-side; re-summarizing links would only burn DeepSeek tokens.
    link_count = url_enricher.count_link_targets(messages)
    if link_count and batch_state is not None:
        console.print(
            f"[dim]续接批次：跳过链接增强"
            f"（{link_count} 个链接的摘要已包含在已提交的输入里）[/dim]\n"
        )
    elif link_count:
        console.rule(f"[bold]链接增强  [dim]({config.LINK_SUMMARY_MODEL}, no thinking)[/dim]")
        link_lanes = lanes_ui.Lanes(
            "链接增强",
            total=link_count,
            subtitle=config.LINK_SUMMARY_MODEL,
            status_labels={"summary": "摘要", "short": "太短", "failed": "失败"},
        )

        def _link_usage_cb(usage, duration_s: float, input_chars: int) -> None:
            record = cost_tracker.log_call(
                date=date_str,
                stage="link",
                model=config.LINK_SUMMARY_MODEL,
                usage=usage,
                duration_s=duration_s,
                input_chars=input_chars,
            )
            if cost_records is not None:
                cost_records.append(record)

        with rich.live.Live(link_lanes, console=console, refresh_per_second=12, transient=False):
            stats = url_enricher.enrich_link_messages(
                messages,
                anthropic_key,
                reporter=link_lanes,
                usage_cb=_link_usage_cb,
            )
            link_lanes.freeze()
        console.print(
            f"[green]链接增强完毕[/green] "
            f"[dim]链接 {stats.total} 个；摘要 {stats.summarized} 个；"
            f"太短 {stats.short} 个；失败 {stats.failed} 个[/dim]\n"
        )

    # ── B: Tokenize + optout masking ────────────────────────────────────────
    console.rule("[bold]隐私处理（token化 + optout遮蔽）")
    total_msgs = len(messages)
    with rich.progress.Progress(
        rich.progress.SpinnerColumn(),
        rich.progress.TextColumn("[bold blue]token化中..."),
        rich.progress.TextColumn("[dim]{task.completed}/{task.total} 条[/dim]"),
        rich.progress.TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as _prog:
        _task = _prog.add_task("", total=total_msgs)

        def _tok_cb(current: int, _total: int) -> None:
            _prog.update(_task, completed=current)

        tokenized, token_map = privacy.tokenize_messages(
            messages, contact_map, alias_db, progress_cb=_tok_cb
        )
    chat_history = privacy.format_tokenized_messages(tokenized)
    roster_entries = roster.build_roster(token_map, contact_map, alias_db)
    roster_text = roster.format_roster(roster_entries)
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
        prior_reports = prior_report.load_prior_reports(date_str, n_days=prior_days)
        gaps = prior_report.missing_prior_dates(date_str, n_days=prior_days)
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
            choice = (
                console.input(
                    "[bold]继续生成今日日报？"
                    "继续 (c) / 跳过此日 (s) / 全部退出 (a) (默认 c): [/bold]"
                )
                .strip()
                .lower()
            )
            if choice == "s":
                console.print(f"[yellow]按用户要求跳过 {date_str}。[/yellow]\n")
                return
            if choice == "a":
                raise KeyboardInterrupt("用户中止")
        elif gaps and not prior_reports:
            # All prior days missing — first-run / long pause; silent continue.
            console.print(f"[dim]最近 {prior_days} 天均无历史日报，按独立日报生成。[/dim]")

    # Titles-only window covers older days (e.g. 4–7 back) on top of the
    # full-body window. Clamp to >= prior_days so we never narrow it; skip
    # dates already loaded with full bodies to avoid duplication.
    title_window = max(prior_title_days, prior_days)
    if title_window > prior_days:
        covered = {d for d, _ in prior_reports}
        prior_report_titles = prior_report.load_prior_report_titles(
            date_str,
            n_days=title_window,
            skip_dates=covered,
        )
        if prior_report_titles:
            console.print(
                f"[dim]载入额外 {len(prior_report_titles)} 天标题大纲："
                f"{', '.join(d for d, _ in prior_report_titles)}[/dim]"
            )
    if prior_days > 0 or prior_report_titles:
        console.print()

    # 覆盖水位线 = 提交时刻本期覆盖到的最后一条消息（messages 已按 create_time
    # 升序，故取末条）。批量路径由 submit_batch 在「提交时刻」写入（resume 不覆写，
    # 避免虚报）；流式路径没有 submit_batch，在此直接记录。
    last_message_ts = messages[-1].create_time

    # ── C: Report generation — Batch API (default) or legacy streaming ─────
    compare_report = None
    if use_batch:
        reports = _run_batch_extraction(
            date_str=date_str,
            anthropic_key=anthropic_key,
            chat_history=chat_history,
            tokenized=tokenized,
            roster_text=roster_text,
            prior_reports=prior_reports,
            prior_report_titles=prior_report_titles,
            run_compare=run_compare,
            state=batch_state,
            fingerprint=fingerprint,
            last_message_ts=last_message_ts,
            cost_records=cost_records,
        )
        if not reports or "main" not in reports:
            console.print("[bold red]批量提取失败：主版本日报未生成[/bold red]")
            return
        report = reports["main"]
        compare_report = reports.get("compare")
    else:
        coverage.record(date_str, last_message_ts)
        report = _run_streaming_extraction(
            date_str=date_str,
            chat_history=chat_history,
            tokenized=tokenized,
            anthropic_key=anthropic_key,
            roster_text=roster_text,
            prior_reports=prior_reports,
            prior_report_titles=prior_report_titles,
            cost_records=cost_records,
            model=config.CLAUDE_MODEL,
            stage="extract",
            debug_suffix="",
            accent="blue",
            rule_title=f"Claude 结构化提取  [dim]({config.CLAUDE_MODEL})[/dim]",
            spinner_label="Claude 正在分析群聊...",
            show_decode_stats=True,
        )
        if report is None:
            console.print("[bold red]结构化提取失败[/bold red]")
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
        e
        for e in alias_db.command_log()
        if datetime.datetime.fromtimestamp(e["ts"]).date() == target_date
    ]
    group_md = renderer.render_group(report, alias_db, contact_map, day_log, token_map=token_map)

    debug_day = config.debug_dir_for(date_str)
    debug_day.mkdir(exist_ok=True, parents=True)
    md_path = debug_day / "group.md"
    md_path.write_text(group_md, encoding="utf-8")

    pdf_path = archiver.get_pdf_path(date_str)
    with rich.progress.Progress(
        rich.progress.SpinnerColumn(),
        rich.progress.TextColumn("[bold blue]{task.description}"),
        rich.progress.TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Markdown → PDF...", total=None)
        pdf.convert_to_pdf(group_md, pdf_path)
        progress.update(task, description=f"PDF 已保存: {pdf_path.name}")

    console.print(f"[green]群内版:[/green] [cyan]{pdf_path}[/cyan]")
    console.print(f"[green]Markdown:[/green] [dim]{md_path}[/dim]\n")

    # ── D2: Opus 4.6 对比版（旁路，仅本地，不发布、不喂续写）─────────────────
    # 放在主版本群内版渲染之后、公开版/泄漏检测之前：只要主版本本身生成成功就
    # 产出对比版，不受公开版泄漏检测早退的影响。失败不致命。批量模式下对比版
    # 与主版本同批生成，这里只剩渲染；流式模式下在此处单独跑一次提取。
    if use_batch:
        if compare_report is not None:
            _render_compare_report(
                compare_report,
                date_str=date_str,
                alias_db=alias_db,
                contact_map=contact_map,
                token_map=token_map,
                target_date=target_date,
            )
    elif run_compare:
        compare_report = _run_streaming_extraction(
            date_str=date_str,
            chat_history=chat_history,
            tokenized=tokenized,
            anthropic_key=anthropic_key,
            roster_text=roster_text,
            prior_reports=prior_reports,
            prior_report_titles=prior_report_titles,
            cost_records=cost_records,
            model=config.COMPARE_REPORT_MODEL,
            stage="extract-compare",
            debug_suffix=_COMPARE_DEBUG_SUFFIX,
            accent="magenta",
            rule_title=f"对比提取  [dim]({config.COMPARE_REPORT_MODEL}) — 仅本地，不发布[/dim]",
            spinner_label="Opus 对比生成...",
            show_decode_stats=False,
        )
        if compare_report is None:
            console.print("[yellow]Opus 对比生成失败，跳过。[/yellow]\n")
        else:
            _render_compare_report(
                compare_report,
                date_str=date_str,
                alias_db=alias_db,
                contact_map=contact_map,
                token_map=token_map,
                target_date=target_date,
            )

    # ── E: Render public version + leak check ───────────────────────────────
    console.rule("[bold]渲染公开版 + 泄漏检测")
    public_md = renderer.render_public(report, alias_db, token_map=token_map)

    try:
        privacy.leak_check(public_md, alias_db)
    except privacy.LeakDetected as e:
        console.print(f"[bold red]泄漏检测失败，公开版已中止:[/bold red] {e}")
        _save_leak_debug(date_str, str(e), public_md)
        console.print("[yellow]群内版 PDF 不受影响。[/yellow]")
        return

    console.print("[green]泄漏检测通过[/green]\n")

    # ── F: Write public post + local commit (NO push — -y handles that) ─────
    post_path = publisher.write_post(date_str, public_md)
    committed = publisher.commit(date_str)
    if committed:
        console.print(f"[green]公开版已本地 commit:[/green] [dim]{post_path}[/dim]")
    else:
        console.print(f"[dim]公开版内容未变，跳过 commit: {post_path}[/dim]")

    # Preview (open browser so author can spot-check before next -y push)
    preview_path = publisher.preview(date_str, public_md, open_browser=True)
    console.print(f"[dim]预览: {preview_path}[/dim]")

    console.print(
        "\n[dim]公开版已本地 commit（未推送）。"
        "下次运行带 -y 可推送到 GitHub，GitHub Pages 将自动构建。[/dim]"
    )


_COMPARE_DEBUG_SUFFIX = ".opus-4-6"


def _run_streaming_extraction(
    *,
    date_str: str,
    chat_history: str,
    tokenized: list,
    anthropic_key: str,
    roster_text: str,
    prior_reports: list[tuple[str, str]],
    prior_report_titles: list[tuple[str, str]],
    cost_records: list[cost_tracker.CostRecord] | None,
    model: str,
    stage: str,
    debug_suffix: str,
    accent: str,
    rule_title: str,
    spinner_label: str,
    show_decode_stats: bool,
):
    """One streaming report extraction with the live thinking/body panel.

    Shared by the main (Fable) and compare (Opus) runs in ``--no-batch``
    mode — same prompt, native inline images; only model/labels differ.
    Returns the DailyReport, or ``None`` on failure (caller decides whether
    that's fatal). Only the main run lets unexpected exceptions propagate;
    the compare run (``stage="extract-compare"``) swallows them — the
    canonical report has already shipped by then.
    """
    import tempfile
    import time as _time

    from wechat_daily import image_decoder

    console.rule(f"[bold]{rule_title}")

    headers: list[rich.text.Text] = []  # permanent header / status lines
    thinking_lines: collections.deque[str] = collections.deque(maxlen=8)
    body_lines: collections.deque[str] = collections.deque(maxlen=8)
    progress = rich.progress.Progress(
        rich.progress.SpinnerColumn(),
        rich.progress.TextColumn(f"[bold {accent}]{spinner_label}"),
        rich.progress.TextColumn("[dim]{task.description}[/dim]"),
        rich.progress.TimeElapsedColumn(),
        console=console,
    )
    task = progress.add_task("连接中...", total=None)
    state = {"text": 0, "thinking": 0, "thinking_partial": "", "body_partial": ""}

    def _tail_texts(
        lines: collections.deque[str], partial: str, n: int, style: str
    ) -> list[rich.text.Text]:
        visible = list(lines)
        if partial:
            visible.append(partial)
        width = max(1, console.width)
        rows: list[rich.text.Text] = []
        for s in visible:
            rows.extend(rich.text.Text(s, style=style).wrap(console, width))
        return rows[-n:]

    def render() -> rich.console.Group:
        parts: list = list(headers)
        th = _tail_texts(thinking_lines, state["thinking_partial"], 8, "dim italic")
        bd = _tail_texts(body_lines, state["body_partial"], 8, "")
        if th:
            parts.append(rich.text.Text("─ thinking ─", style="dim cyan"))
            parts.extend(th)
        if bd:
            parts.append(rich.text.Text("─ 正文 ─", style=accent))
            parts.extend(bd)
        parts.append(progress)
        return rich.console.Group(*parts)

    with rich.live.Live(render(), console=console, refresh_per_second=10, transient=False) as live:

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

        def header_cb(_kind: str, level: int, title: str, _attempt: int) -> None:
            headers.append(rich.text.Text(f"{'  ' * level}{title}", style="bold"))
            refresh()

        def attempt_cb(attempt: int) -> None:
            headers.append(rich.text.Text(f"--- 第 {attempt}/3 次重试 ---", style="yellow"))
            thinking_lines.clear()
            body_lines.clear()
            state["thinking_partial"] = ""
            state["body_partial"] = ""
            refresh()

        t_start = _time.perf_counter()

        def _usage_cb(usage, input_chars: int) -> None:
            record = cost_tracker.log_call(
                date=date_str,
                stage=stage,
                model=model,
                usage=usage,
                duration_s=_time.perf_counter() - t_start,
                input_chars=input_chars,
            )
            if cost_records is not None:
                cost_records.append(record)

        try:
            with tempfile.TemporaryDirectory(prefix="wechat_daily_imgs_") as td:
                decoder = image_decoder.ImageDecoder(pathlib.Path(td))
                chat_blocks = privacy.format_tokenized_messages_blocks(tokenized, decoder)
                if show_decode_stats:
                    n_images = sum(1 for m in tokenized if m.image_md5)
                    n_decoded = sum(1 for b in chat_blocks if b.get("type") == "image")
                    if n_images:
                        # Undecodable images (decode error, or a blank/wxgf frame
                        # the decoder rejected) fall back to a bare [图片]
                        # placeholder — the model never sees a silent white
                        # image. Surface the shortfall so the author knows.
                        n_missing = n_images - n_decoded
                        if n_missing:
                            headers.append(
                                rich.text.Text(
                                    f"图片解码 {n_decoded}/{n_images} 张成功"
                                    f"（{n_missing} 张无法解码，已降级为 [图片] 占位）",
                                    style="yellow",
                                )
                            )
                        else:
                            headers.append(
                                rich.text.Text(
                                    f"图片解码 {n_decoded}/{n_images} 张成功",
                                    style="dim",
                                )
                            )
                        refresh()
                return llm_extractor.extract_report(
                    date_str,
                    chat_history,
                    anthropic_key,
                    progress_cb,
                    roster_text=roster_text or None,
                    thinking_cb=thinking_cb,
                    header_cb=header_cb,
                    attempt_cb=attempt_cb,
                    text_cb=text_cb,
                    usage_cb=_usage_cb,
                    chat_blocks=chat_blocks,
                    prior_reports=prior_reports or None,
                    prior_report_titles=prior_report_titles or None,
                    model=model,
                    debug_suffix=debug_suffix,
                )
        except llm_extractor.ExtractionError as e:
            console.print(f"[red]提取失败（{model}）：{e}[/red]")
            return None
        except Exception as e:
            if stage != "extract-compare":
                raise
            console.print(f"[yellow]对比生成异常，跳过：{e}[/yellow]\n")
            return None


def _render_compare_report(
    compare_report,
    *,
    date_str: str,
    alias_db: aliases.AliasDB,
    contact_map: contacts.ContactMap,
    token_map: dict,
    target_date,
) -> None:
    """Render the Opus compare report → group markdown + PDF (local only).

    No public path, no leak check, never feeds next-day continuity — the
    ``(opus-4-6)`` PDF sits alongside the canonical one for AB reading.
    """
    day_log = [
        e
        for e in alias_db.command_log()
        if datetime.datetime.fromtimestamp(e["ts"]).date() == target_date
    ]
    compare_group_md = renderer.render_group(
        compare_report,
        alias_db,
        contact_map,
        day_log,
        token_map=token_map,
    )

    debug_day = config.debug_dir_for(date_str)
    debug_day.mkdir(exist_ok=True, parents=True)
    md_path = debug_day / f"group{_COMPARE_DEBUG_SUFFIX}.md"
    md_path.write_text(compare_group_md, encoding="utf-8")

    config.ARCHIVE_DIR.mkdir(exist_ok=True)
    # Always exactly one "(opus-4-6)" file per date, overwriting stale ones on
    # re-runs (matching how the canonical path overwrites debug/{date}.md).
    compare_pdf_path = config.ARCHIVE_DIR / f"{date_str} 群聊日报 (opus-4-6).pdf"
    with rich.progress.Progress(
        rich.progress.SpinnerColumn(),
        rich.progress.TextColumn("[bold magenta]{task.description}"),
        rich.progress.TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Opus Markdown → PDF...", total=None)
        pdf.convert_to_pdf(compare_group_md, compare_pdf_path)
        progress.update(task, description=f"PDF 已保存: {compare_pdf_path.name}")

    console.print(f"[magenta]Opus 对比版:[/magenta] [cyan]{compare_pdf_path}[/cyan]")
    console.print(f"[magenta]Opus Markdown:[/magenta] [dim]{md_path}[/dim]\n")


# ── Batch extraction (default mode) ─────────────────────────────────────────────


def _decide_batch_state(
    date_str: str,
    fingerprint: tuple[int, str],
    anthropic_key: str,
    *,
    force_resume: bool,
    force_resubmit: bool,
):
    """Load this date's batch state and decide resume vs fresh submission.

    Returns the :class:`~wechat_daily.batch_extractor.BatchState` to resume
    (link enrichment will be skipped), or ``None`` for a fresh submission.

    Decision table (default intent when a state file exists is RESUME):
    - no state file → fresh
    - ``--resubmit`` → cancel pending batch (best-effort), fresh
    - consumed → ask reuse (re-fetch results, free) vs fresh; ``--resume``
      forces reuse
    - pending + fingerprint match (or ``--resume``) → silent resume
    - pending + mismatch → warn with message-count delta, ask
    """
    try:
        state = batch_extractor.load_state(date_str)
    except batch_extractor.BatchStateError as e:
        console.print(f"[yellow]{e}[/yellow]")
        if force_resubmit:
            return None
        choice = (
            console.input("[bold]状态文件不可用。重新提交 (r) / 中止 (a) (默认 r): [/bold]")
            .strip()
            .lower()
        )
        if choice == "a":
            raise KeyboardInterrupt("用户中止")
        return None

    if state is None:
        return None

    if force_resubmit:
        if not state.consumed:
            batch_extractor.cancel_batch(batch_extractor.make_client(anthropic_key), state.batch_id)
            console.print(f"[dim]已请求取消旧批次 {state.batch_id}[/dim]")
        return None

    count, sha = fingerprint

    if state.consumed:
        if force_resume:
            return state
        choice = (
            console.input(
                f"[bold]该日期批次 {state.batch_id} 之前已取回过结果"
                f"（提交于 {state.submitted_at}）。\n"
                "复用批次结果 (u)，重新取回、零成本 / 重新提交生成 (n) (默认 u): [/bold]"
            )
            .strip()
            .lower()
        )
        return None if choice == "n" else state

    if force_resume or sha == state.raw_msg_sha256:
        console.print(
            f"[dim]检测到进行中的批次 {state.batch_id}（提交于 {state.submitted_at}），续接。[/dim]"
        )
        return state

    delta = count - state.raw_msg_count
    delta_txt = f"当时 {state.raw_msg_count} 条消息，现在 {count} 条" + (
        f"（新增 {delta} 条）" if delta > 0 else "（消息集合有变化）"
    )
    console.print(
        f"[yellow]检测到未完成批次 {state.batch_id}"
        f"（提交于 {state.submitted_at}，{delta_txt}）。[/yellow]"
    )
    choice = (
        console.input(
            "[bold]续接 (c)：日报只覆盖提交时刻的快照，之后的消息不会出现在本期\n"
            "重新提交 (r)：取消旧批次，用当前全部消息重新生成 (默认 c): [/bold]"
        )
        .strip()
        .lower()
    )
    if choice == "r":
        batch_extractor.cancel_batch(batch_extractor.make_client(anthropic_key), state.batch_id)
        console.print(f"[dim]已请求取消旧批次 {state.batch_id}[/dim]")
        return None
    return state


def _run_batch_extraction(
    *,
    date_str: str,
    anthropic_key: str,
    chat_history: str,
    tokenized: list,
    roster_text: str,
    prior_reports: list[tuple[str, str]],
    prior_report_titles: list[tuple[str, str]],
    run_compare: bool,
    state,
    fingerprint: tuple[int, str],
    last_message_ts: int,
    cost_records: list[cost_tracker.CostRecord] | None,
) -> dict:
    """Generate main + compare reports through one Message Batch (50% price).

    Returns ``{custom_id: DailyReport}`` (may be missing entries on
    per-request failure; caller treats a missing ``main`` as fatal) or ``{}``
    on batch-level failure. On resume (*state* given) the prompt was already
    submitted; the locally rebuilt input only backs the rare retry round —
    it lacks the nondeterministic link summaries, an accepted degradation.
    """
    import tempfile
    import time as _time

    from wechat_daily import image_decoder

    console.rule("[bold]批量提取  [dim](Batch API，5 折计费，无流式预览)[/dim]")

    requests = {"main": config.CLAUDE_MODEL}
    if run_compare:
        requests["compare"] = config.COMPARE_REPORT_MODEL
    if state is not None:
        # Resume: regenerate exactly what was submitted, whatever today's
        # flags say (--no-compare doesn't drop an already-paid-for request).
        requests = dict(state.requests)

    debug_day = config.debug_dir_for(date_str)
    debug_day.mkdir(exist_ok=True, parents=True)
    input_snapshot = debug_day / "batch_input.txt"

    client = batch_extractor.make_client(anthropic_key)
    t_start = _time.perf_counter()

    def _usage_cb(custom_id: str, model: str, usage) -> None:
        record = cost_tracker.log_call(
            date=date_str,
            stage="extract" if custom_id == "main" else "extract-compare",
            model=model,
            usage=usage,
            duration_s=_time.perf_counter() - t_start,
            input_chars=len(debug_text),
            batch=True,
        )
        if cost_records is not None:
            cost_records.append(record)

    # Resume: prefer the submit-time content snapshot — the retry round then
    # replays the EXACT submitted input (images + link summaries included),
    # and image re-decoding is skipped entirely. Falls back to a local
    # rebuild (missing the nondeterministic link summaries) when the
    # snapshot is gone — e.g. reusing an already-consumed batch.
    user_content = None
    if state is not None:
        user_content = batch_extractor.load_content_snapshot(date_str)

    with tempfile.TemporaryDirectory(prefix="wechat_daily_imgs_") as td:
        if user_content is not None:
            debug_text = (
                input_snapshot.read_text(encoding="utf-8")
                if input_snapshot.exists()
                else batch_extractor.snapshot_debug_text(user_content)
            )
            console.print(
                "[dim]已加载提交时的输入快照（含图片与链接摘要），重试轮与原输入字节一致。[/dim]"
            )
        else:
            decoder = image_decoder.ImageDecoder(pathlib.Path(td))
            chat_blocks = privacy.format_tokenized_messages_blocks(tokenized, decoder)
            n_images = sum(1 for m in tokenized if m.image_md5)
            n_decoded = sum(1 for b in chat_blocks if b.get("type") == "image")
            if n_images:
                n_missing = n_images - n_decoded
                style = "yellow" if n_missing else "dim"
                note = f"（{n_missing} 张无法解码，已降级为 [图片] 占位）" if n_missing else ""
                console.print(f"[{style}]图片解码 {n_decoded}/{n_images} 张成功{note}[/{style}]")

            user_content, debug_text = llm_extractor.build_extract_user_content(
                date_str=date_str,
                tokenized_chat=chat_history,
                roster_text=roster_text or None,
                chat_blocks=chat_blocks,
                prior_reports=prior_reports or None,
                prior_report_titles=prior_report_titles or None,
            )

            if state is None:
                # Human-readable audit copy of the submitted input; the
                # machine-replayable block-list snapshot is written by
                # submit_batch (and cleaned up on consumption).
                input_snapshot.write_text(debug_text, encoding="utf-8")
            elif input_snapshot.exists():
                debug_text = input_snapshot.read_text(encoding="utf-8")
            else:
                console.print(
                    "[dim]未找到提交时的输入快照，重试轮输入与 debug sidecar "
                    "将缺少链接摘要（不影响已生成结果）。[/dim]"
                )

        progress = rich.progress.Progress(
            rich.progress.SpinnerColumn(),
            rich.progress.TextColumn("[bold blue]Batch 处理中..."),
            rich.progress.TextColumn("[dim]{task.description}[/dim]"),
            rich.progress.TimeElapsedColumn(),
            console=console,
        )
        task = progress.add_task("提交/连接中...", total=None)

        def _status_cb(batch, elapsed: float, note: str) -> None:
            if batch is None:
                progress.update(task, description=note)
                return
            c = batch.request_counts
            progress.update(
                task,
                description=(
                    f"处理中 {c.processing} / 成功 {c.succeeded} / "
                    f"失败 {c.errored} · 每 {batch_extractor.POLL_INTERVAL_S:.0f}s 轮询"
                ),
            )

        try:
            with progress:
                outcome = batch_extractor.run_batch(
                    client=client,
                    date_str=date_str,
                    debug_text=debug_text,
                    user_content=user_content,
                    fingerprint=fingerprint,
                    requests=requests,
                    last_message_ts=last_message_ts,
                    state=state,
                    status_cb=_status_cb,
                    usage_cb=_usage_cb,
                    note_cb=lambda msg: progress.console.print(f"[dim]{msg}[/dim]"),
                    # 死连接池不会自愈：睡眠恢复/连续失败时 poll 用这个工厂造一个
                    # 全新的 client（=新连接池），等价于用户 ctrl+c 重跑那招。
                    rebuild_client=lambda: batch_extractor.make_client(anthropic_key),
                )
        except KeyboardInterrupt:
            st = state or batch_extractor.load_state(date_str)
            if st is not None:
                console.print(
                    f"\n[yellow]批次 {st.batch_id} 仍在云端处理，"
                    "重新运行同一命令即可续接（--resume 跳过询问）。[/yellow]"
                )
            raise
        except batch_extractor.BatchNotFound as e:
            console.print(f"[bold red]{e}[/bold red]")
            console.print("[yellow]用 --resubmit 重新提交，或删除状态文件后重跑。[/yellow]")
            return {}
        except batch_extractor.BatchTimeout as e:
            console.print(f"[bold red]{e}[/bold red]")
            console.print("[yellow]状态文件已保留，稍后重跑可继续尝试取结果。[/yellow]")
            return {}

    for custom_id, reason in outcome.errors.items():
        style = "bold red" if custom_id == "main" else "yellow"
        console.print(f"[{style}]{custom_id} 请求失败：{reason}[/{style}]")

    return outcome.reports


def _save_leak_debug(date_str: str, error: str, public_md: str) -> None:
    import json

    debug_day = config.debug_dir_for(date_str)
    debug_day.mkdir(exist_ok=True, parents=True)
    path = debug_day / "leak.json"
    path.write_text(
        json.dumps({"error": error, "public_md": public_md}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    console.print(f"[dim]泄漏详情已保存至 {path}[/dim]")


# ── Main entry ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="微信群聊日报生成器")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="也为最后一天（尚未过当日 21:00+1小时）生成不完整日报",
    )
    parser.add_argument(
        "--no-batch",
        action="store_true",
        help=(
            "不用 Batch API，走传统流式生成（有实时预览，但按标准价计费）。"
            "默认走批量：全部 token 5 折，通常几分钟到几十分钟完成，"
            "支持休眠/退出后重跑续接。"
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="存在未完成/已完成的批次时无条件续接（跳过询问；配合批量模式）。",
    )
    parser.add_argument(
        "--resubmit",
        action="store_true",
        help="放弃已存在的批次（尽力取消未完成请求），用当前消息重新提交。",
    )
    parser.add_argument(
        "-y",
        action="store_true",
        help="推送上次生成的公开版到 GitHub Pages（不影响本次生成流程）",
    )
    parser.add_argument(
        "--prior-days",
        type=int,
        default=3,
        help="喂给模型的过往日报**完整正文**天数（用于跨日去重 / 续写）；0 关闭。默认 3。",
    )
    parser.add_argument(
        "--prior-title-days",
        type=int,
        default=7,
        help=(
            "喂给模型的过往日报**标题大纲**总天数（前 --prior-days 天用完整正文，"
            "再往前的天数仅传 ## / ### 标题，便于扩大跨日去重 / [[ref:]] 窗口而不爆 token）；"
            "小于 --prior-days 会被静默上调。默认 7。"
        ),
    )
    parser.add_argument(
        "--no-prior-prompt",
        action="store_true",
        help="最近若干天日报有缺失时不交互询问，按现有材料继续（适合自动化场景）。",
    )
    parser.add_argument(
        "--no-compare",
        action="store_true",
        help=(
            "跳过 Opus 4.6 对比版（旁路，仅本地 PDF/debug、不发布）。"
            "对比版与主版本同价位、且每天跑全天聊天记录，成本不低；只想要发布版时用此项关闭。"
        ),
    )
    args = parser.parse_args()
    if args.resume and args.resubmit:
        parser.error("--resume 与 --resubmit 互斥，请二选一")

    console.print(
        rich.panel.Panel.fit(
            "[bold cyan]微信群聊日报生成器[/bold cyan]\n"
            "[dim]WeChat Group Chat Daily Report Generator[/dim]",
            border_style="cyan",
        )
    )

    try:
        # Step 1: API keys
        console.rule("[bold]Step 1  API Key 配置")
        anthropic_key = _ensure_anthropic_key()
        # Link summaries run on DeepSeek; make sure the key is present before start.
        _ensure_deepseek_key()
        console.print("[green]API Keys 就绪[/green]\n")

        # Step 2: Push PREVIOUS run's pending commits (-y semantics per §7.6)
        if args.y:
            console.rule("[bold]Step 2  推送上次未推送的公开版")
            try:
                pushed = publisher.push_pending()
                console.print(
                    "[green]已推送。[/green]\n" if pushed else "[dim]无待推送 commit。[/dim]\n"
                )
            except RuntimeError as e:
                console.print(f"[yellow]推送失败: {e}[/yellow]\n")

        # Step 3: Load contacts + alias DB; scan commands incrementally
        console.rule("[bold]Step 3  加载联系人与别名数据库")
        contact_map = contacts.ContactMap.from_db()
        alias_db = aliases.AliasDB.load()
        with rich.progress.Progress(
            rich.progress.SpinnerColumn(),
            rich.progress.TextColumn("[bold blue]正在扫描新指令 (/alias /optout /optin)..."),
            rich.progress.TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as _prog:
            _prog.add_task("", total=None)
            alias_db.scan_commands(contact_map)
        alias_db.save()
        console.print("[green]别名数据库已更新[/green]\n")

        # Step 4: Archive old PDFs
        moved = archiver.archive_old_files()
        if moved:
            console.print(f"[dim]已将 {moved} 个旧日报归档至年/月子目录[/dim]\n")

        # Step 5: Find missing dates
        console.rule("[bold]Step 4  检测缺失日期")
        missing = chat_extractor.find_missing_dates(allow_incomplete=args.allow_incomplete)
        if not missing:
            console.print("[green]archive 已是最新，无需生成新日报。[/green]")
            return
        console.print(
            f"发现 [cyan]{len(missing)}[/cyan] 个缺失日期: "
            + ", ".join(f"[cyan]{d}[/cyan]" for d in missing)
            + "\n"
        )

        # Step 6: Generate reports
        cost_records: list[cost_tracker.CostRecord] = []
        for date_str in missing:
            _run_db_pipeline(
                date_str,
                anthropic_key,
                alias_db,
                contact_map,
                prior_days=args.prior_days,
                prior_title_days=args.prior_title_days,
                prompt_on_missing_prior=not args.no_prior_prompt,
                run_compare=not args.no_compare,
                cost_records=cost_records,
                use_batch=not args.no_batch,
                force_resume=args.resume,
                force_resubmit=args.resubmit,
            )
            # Persist any tokens lazily allocated during this date's pipeline
            # so subsequent runs keep the same names.
            alias_db.save()

        if cost_records:
            console.print()
            console.print(cost_tracker.summarize(cost_records))

        console.print(
            rich.panel.Panel.fit(
                f"[bold green]完成！[/bold green]\n共生成 [cyan]{len(missing)}[/cyan] 份日报",
                border_style="green",
                title="Success",
            )
        )

    except KeyboardInterrupt:
        console.print("\n[yellow]用户中断[/yellow]")
        sys.exit(1)
    except FileNotFoundError as e:
        console.print(f"\n[bold red]文件未找到:[/bold red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]错误:[/bold red] {e}")
        raise
