"""One-off experiment driver: regenerate a past day's report with tables + images.

Runs the real pipeline for a single, already-published date so the new output
can be read side by side with the canonical one, **without touching any
released artifact**: nothing is written to ``archive/``, ``data/public_repo/``,
or the un-suffixed ``debug/`` sidecars that feed next-day continuity. The
generated markdown, the referenced chat images, and the PDF all land in
``--out`` (default: ``debug/YYYY/MM/DD/exp/``).

    .venv/bin/python -m scripts.exp_rich_report --date 2026-09-03

Streaming path only (``--no-batch`` equivalent) — an experiment wants the live
preview and an answer in minutes, not the batch queue's 50% discount.
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import tempfile
import time

import rich.console

from wechat_daily import (
    aliases,
    chat_extractor,
    config,
    contacts,
    cost_tracker,
    image_decoder,
    image_export,
    llm_extractor,
    pdf,
    prior_report,
    privacy,
    renderer,
    roster,
    url_enricher,
)

console = rich.console.Console()

DEBUG_SUFFIX = ".exp"


def _stream_to(path: pathlib.Path):
    """Tail-able live view of the model's report body (this runs unattended)."""
    path.write_text("", encoding="utf-8")

    def cb(kind: str, delta: str, _attempt: int) -> None:
        if kind != "body":
            return
        with path.open("a", encoding="utf-8") as fh:
            fh.write(delta)

    return cb


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="目标日期 YYYY-MM-DD（须已有聊天记录）")
    parser.add_argument("--out", default=None, help="产物目录（默认 debug/YYYY/MM/DD/exp/）")
    parser.add_argument("--prior-days", type=int, default=3)
    parser.add_argument("--prior-title-days", type=int, default=7)
    parser.add_argument(
        "--no-links", action="store_true", help="跳过链接摘要（省钱，但与原版不可比）"
    )
    parser.add_argument("--model", default=config.CLAUDE_MODEL)
    args = parser.parse_args()

    date_str = args.date
    target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    out_dir = pathlib.Path(args.out) if args.out else config.debug_dir_for(date_str) / "exp"
    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir = out_dir / "images"

    anthropic_key = config.get_anthropic_key()
    if not anthropic_key:
        raise SystemExit("缺少 ANTHROPIC_API_KEY（.env）")
    if not config.get_deepseek_key() and not args.no_links:
        raise SystemExit("缺少 DEEPSEEK_API_KEY（.env）；或加 --no-links 跳过链接摘要")

    console.rule(f"[bold]提取 {date_str}")
    contact_map = contacts.ContactMap.from_db()
    alias_db = aliases.AliasDB.load()  # 不 scan_commands、不 save：实验只读别名库
    messages = chat_extractor.extract_messages(date_str, contact_map)
    console.print(f"消息 {len(messages)} 条")

    n_links = url_enricher.count_link_targets(messages)
    if n_links and not args.no_links:
        console.rule(f"[bold]链接摘要 {n_links} 个  [dim]{config.LINK_SUMMARY_MODEL}[/dim]")
        t0 = time.perf_counter()
        stats = url_enricher.enrich_link_messages(messages, anthropic_key)
        console.print(
            f"摘要 {stats.summarized} / 太短 {stats.short} / 失败 {stats.failed}"
            f"  [dim]{time.perf_counter() - t0:.0f}s[/dim]"
        )

    tokenized, token_map = privacy.tokenize_messages(messages, contact_map, alias_db)
    chat_history = privacy.format_tokenized_messages(tokenized)
    roster_text = roster.format_roster(roster.build_roster(token_map, contact_map, alias_db))

    prior_reports = prior_report.load_prior_reports(date_str, n_days=args.prior_days)
    title_window = max(args.prior_title_days, args.prior_days)
    prior_titles = prior_report.load_prior_report_titles(
        date_str,
        n_days=title_window,
        skip_dates={d for d, _ in prior_reports},
    )
    console.print(
        f"历史日报：完整 {len(prior_reports)} 天，标题 {len(prior_titles)} 天；"
        f"花名册 {roster_text.count(chr(10)) + 1} 行"
    )

    console.rule(f"[bold]生成  [cyan]{args.model}[/cyan]")
    cost: list[cost_tracker.CostRecord] = []
    t0 = time.perf_counter()

    def usage_cb(usage, input_chars: int) -> None:
        cost.append(
            cost_tracker.log_call(
                date=date_str,
                stage="extract-exp",
                model=args.model,
                usage=usage,
                duration_s=time.perf_counter() - t0,
                input_chars=input_chars,
            )
        )

    with tempfile.TemporaryDirectory(prefix="exp_imgs_") as td:
        decoder = image_decoder.ImageDecoder(pathlib.Path(td))
        chat_blocks, id_map = privacy.format_tokenized_messages_blocks(
            tokenized, decoder, image_ids=True
        )
        n_images = sum(1 for m in tokenized if m.image_md5)
        console.print(f"图片解码 {len(id_map)}/{n_images} 张，已编号")

        report = llm_extractor.extract_report(
            date_str,
            chat_history,
            anthropic_key,
            progress_cb=lambda *a: None,
            roster_text=roster_text or None,
            text_cb=_stream_to(out_dir / "stream.md"),
            usage_cb=usage_cb,
            chat_blocks=chat_blocks,
            prior_reports=prior_reports or None,
            prior_report_titles=prior_titles or None,
            model=args.model,
            debug_suffix=DEBUG_SUFFIX,
        )

        # Persist only the images the report actually points at — decoded once
        # more from the DB, since the run's tmpdir dies with this block.
        wanted = renderer.image_ref_ids(report.markdown)
        image_paths: dict[str, pathlib.Path] = {}
        if wanted:
            img_dir.mkdir(parents=True, exist_ok=True)
        for img_id in wanted:
            md5 = id_map.get(img_id)
            if md5 is None:
                continue  # hallucinated handle; renderer warns + drops it
            src = decoder.decode(md5)
            if src is None:
                continue
            dst = img_dir / f"{img_id}.jpg"
            if image_export.fit_for_report(src, dst) is not None:
                image_paths[img_id] = dst

    console.print()
    console.rule("[bold]渲染")
    console.print(f"引用图片 {len(image_paths)}/{len(wanted)} 张：{', '.join(wanted) or '无'}")

    day_log = [
        e
        for e in alias_db.command_log()
        if datetime.datetime.fromtimestamp(e["ts"]).date() == target_date
    ]
    group_md = renderer.render_group(
        report, alias_db, contact_map, day_log, token_map=token_map, image_paths=image_paths
    )
    (out_dir / "group.md").write_text(group_md, encoding="utf-8")

    public_md = renderer.render_public(report, alias_db, token_map=token_map)
    (out_dir / "public.md").write_text(public_md, encoding="utf-8")
    try:
        privacy.leak_check(public_md, alias_db)
        console.print("[green]公开版泄漏检测通过[/green]")
    except privacy.LeakDetected as e:
        console.print(f"[red]公开版泄漏检测失败：{e}[/red]")

    pdf_path = out_dir / f"{date_str} 群聊日报 (exp).pdf"
    pdf.convert_to_pdf(group_md, pdf_path)
    console.print(f"[green]PDF[/green] {pdf_path}")

    if cost:
        console.print(cost_tracker.summarize(cost))
    console.print(f"\n[bold green]产物目录[/bold green] {out_dir}")


if __name__ == "__main__":
    main()
