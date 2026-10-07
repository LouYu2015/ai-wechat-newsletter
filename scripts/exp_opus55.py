"""Prompt-tuning harness: Opus 4.6 (current prompt) vs Opus 5.5 (new prompts).

Three steps, so every tuning round only pays for the generation itself:

    TZ=America/Los_Angeles python -m scripts.exp_opus55 prepare --date 2026-10-06
    TZ=America/Los_Angeles python -m scripts.exp_opus55 submit  --date 2026-10-06 \\
        --run baseline:claude-opus-4-6:high --run v1:claude-opus-5-5:high
    TZ=America/Los_Angeles python -m scripts.exp_opus55 fetch   --date 2026-10-06

``prepare`` runs the real input pipeline once (messages, DeepSeek link
summaries, tokenization, roster, prior reports, native images) and freezes it
under ``debug/YYYY/MM/DD/exp55/``. ``submit`` builds one Message Batch from that
frozen input — prompt variant × model × effort per request — so all variants
see byte-identical chat input. ``fetch`` polls, then writes each run's
markdown, thinking, group/public render, PDF and cost to ``runs/<custom_id>/``.

Prompt variants live in ``exp55/prompts/<name>/{system.md,instructions.md}``
(``instructions.md`` goes after the chat log; ``{date_str}`` is substituted).
The name ``baseline`` means the production prompts in :mod:`wechat_daily.prompts`
(since 2026-10 that is the v6 prompt this harness produced; the 10-03 – 10-06
Opus 4.6 baselines were run against the previous prompt).

Nothing here touches ``archive/``, ``data/public_repo/`` or the un-suffixed
``debug/`` sidecars that feed next-day continuity.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import pickle
import tempfile
import time

import rich.console

from wechat_daily import (
    aliases,
    batch_extractor,
    chat_extractor,
    config,
    contacts,
    cost_tracker,
    image_decoder,
    image_export,
    llm_extractor,
    models,
    pdf,
    prior_report,
    privacy,
    prompts,
    renderer,
    roster,
    url_enricher,
)

console = rich.console.Console()


def exp_dir(date_str: str) -> pathlib.Path:
    return config.debug_dir_for(date_str) / "exp55"


# ── prepare ──────────────────────────────────────────────────────────────────────


def cmd_prepare(args) -> None:
    date_str = args.date
    out = exp_dir(date_str)
    out.mkdir(parents=True, exist_ok=True)
    img_dir = out / "images"
    img_dir.mkdir(exist_ok=True)

    anthropic_key = config.get_anthropic_key()
    contact_map = contacts.ContactMap.from_db()
    alias_db = aliases.AliasDB.load()
    messages = chat_extractor.extract_messages(date_str, contact_map)
    console.print(f"消息 {len(messages)} 条")

    n_links = url_enricher.count_link_targets(messages)
    if n_links:
        console.print(f"链接摘要 {n_links} 个 …")
        stats = url_enricher.enrich_link_messages(messages, anthropic_key)
        console.print(f"摘要 {stats.summarized} / 太短 {stats.short} / 失败 {stats.failed}")

    tokenized, token_map = privacy.tokenize_messages(messages, contact_map, alias_db)
    roster_text = roster.format_roster(roster.build_roster(token_map, contact_map, alias_db))
    prior_reports = prior_report.load_prior_reports(date_str, n_days=3)
    prior_titles = prior_report.load_prior_report_titles(
        date_str, n_days=7, skip_dates={d for d, _ in prior_reports}
    )
    console.print(f"历史日报：完整 {len(prior_reports)} 天，标题 {len(prior_titles)} 天")

    with tempfile.TemporaryDirectory(prefix="exp55_imgs_") as td:
        decoder = image_decoder.ImageDecoder(pathlib.Path(td))
        chat_blocks, id_map = privacy.format_tokenized_messages_blocks(
            tokenized, decoder, image_ids=True
        )
        # Pre-fit every decodable image for the PDF, so fetch can render any run.
        for img_id, md5 in id_map.items():
            src = decoder.decode(md5)
            if src is not None:
                image_export.fit_for_report(src, img_dir / f"{img_id}.jpg")
    console.print(f"图片 {len(id_map)} 张")

    # Build once with the production instructions, then strip them back off so
    # each variant can append its own suffix.
    user_content, _ = llm_extractor.build_extract_user_content(
        date_str=date_str,
        tokenized_chat=privacy.format_tokenized_messages(tokenized),
        roster_text=roster_text or None,
        chat_blocks=chat_blocks,
        prior_reports=prior_reports or None,
        prior_report_titles=prior_titles or None,
    )
    tail = user_content[-1]["text"]
    marker = "\n</chat_log>\n"
    user_content[-1] = {"type": "text", "text": tail[: tail.index(marker) + len(marker)]}

    (out / "content.json").write_text(
        json.dumps(user_content, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (out / "input.txt").write_text(batch_extractor.snapshot_debug_text(user_content), "utf-8")
    with (out / "render_state.pkl").open("wb") as fh:
        pickle.dump({"token_map": token_map}, fh)
    console.print(f"[green]已冻结输入[/green] {out}")


# ── submit ───────────────────────────────────────────────────────────────────────


def load_prompt(date_str: str, name: str) -> tuple[str, str]:
    if name == "baseline":
        return prompts.SYSTEM_PROMPT, prompts.USER_INSTRUCTIONS.format(date_str=date_str)
    d = exp_dir(date_str) / "prompts" / name
    system = (d / "system.md").read_text("utf-8").strip()
    instr = (d / "instructions.md").read_text("utf-8").strip().replace("{date_str}", date_str)
    return system, instr


def build_params(date_str: str, prompt_name: str, model: str, effort: str) -> dict:
    content = json.loads((exp_dir(date_str) / "content.json").read_text("utf-8"))
    system, instr = load_prompt(date_str, prompt_name)
    content.append({"type": "text", "text": "\n---\n\n" + instr})
    return {
        "model": model,
        "max_tokens": llm_extractor.MAX_OUTPUT_TOKENS,
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": effort},
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }


def custom_id(prompt_name: str, model: str, effort: str, tag: str = "") -> str:
    short = {"claude-opus-4-6": "o46", "claude-opus-5-5": "o55"}.get(model, model)
    cid = f"{prompt_name}-{short}-{effort}" + (f"-{tag}" if tag else "")
    return cid.replace(".", "_")


def cmd_submit(args) -> None:
    date_str = args.date
    client = batch_extractor.make_client(config.get_anthropic_key())
    reqs, meta = [], {}
    for spec in args.run:
        prompt_name, model, effort, *rest = spec.split(":")
        cid = custom_id(prompt_name, model, effort, rest[0] if rest else "")
        reqs.append(
            {"custom_id": cid, "params": build_params(date_str, prompt_name, model, effort)}
        )
        meta[cid] = {"prompt": prompt_name, "model": model, "effort": effort}
    batch = client.messages.batches.create(requests=reqs)
    rec = {
        "batch_id": batch.id,
        "submitted_at": time.time(),
        "requests": meta,
        "consumed": False,
    }
    bdir = exp_dir(date_str) / "batches"
    bdir.mkdir(exist_ok=True)
    (bdir / f"{batch.id}.json").write_text(json.dumps(rec, indent=2), "utf-8")
    console.print(f"[green]已提交[/green] {batch.id}: {', '.join(meta)}")


# ── fetch ────────────────────────────────────────────────────────────────────────


def cost_of(model: str, usage: dict) -> float:
    p = config.MODEL_PRICES[model]
    usd = (
        usage.get("input_tokens", 0) * p["input"]
        + usage.get("output_tokens", 0) * p["output"]
        + usage.get("cache_creation_input_tokens", 0) * p["cache_write_5m"]
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
    ) / 1e6
    return usd * 0.5  # Batch API


def render_run(date_str: str, run_dir: pathlib.Path, markdown: str) -> None:
    out = exp_dir(date_str)
    with (out / "render_state.pkl").open("rb") as fh:
        token_map = pickle.load(fh)["token_map"]
    contact_map = contacts.ContactMap.from_db()
    alias_db = aliases.AliasDB.load()
    report = models.DailyReport(date=date_str, markdown=markdown)
    wanted = renderer.image_ref_ids(markdown)
    image_paths = {
        i: out / "images" / f"{i}.jpg" for i in wanted if (out / "images" / f"{i}.jpg").exists()
    }
    missing = [i for i in wanted if i not in image_paths]
    target = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    day_log = [
        e
        for e in alias_db.command_log()
        if datetime.datetime.fromtimestamp(e["ts"]).date() == target
    ]
    group_md = renderer.render_group(
        report, alias_db, contact_map, day_log, token_map=token_map, image_paths=image_paths
    )
    (run_dir / "group.md").write_text(group_md, "utf-8")
    public_md = renderer.render_public(report, alias_db, token_map=token_map)
    (run_dir / "public.md").write_text(public_md, "utf-8")
    leak = "ok"
    try:
        privacy.leak_check(public_md, alias_db)
    except privacy.LeakDetected as e:
        leak = f"LEAK: {e}"
    pdf.convert_to_pdf(group_md, run_dir / f"{date_str} {run_dir.name}.pdf")
    console.print(f"  图片 {len(image_paths)}/{len(wanted)}（缺 {missing}）泄漏检测 {leak}")


def cmd_fetch(args) -> None:
    date_str = args.date
    client = batch_extractor.make_client(config.get_anthropic_key())
    bdir = exp_dir(date_str) / "batches"
    files = [bdir / f"{args.batch}.json"] if args.batch else sorted(bdir.glob("*.json"))
    for f in files:
        rec = json.loads(f.read_text("utf-8"))
        if rec["consumed"] and not args.batch:
            continue
        bid = rec["batch_id"]
        while True:
            b = client.messages.batches.retrieve(bid)
            if b.processing_status == "ended":
                break
            c = b.request_counts
            console.print(f"{bid} 处理中 {c.processing} / 完成 {c.succeeded} …")
            time.sleep(args.poll)
        elapsed = time.time() - rec["submitted_at"]
        for result in client.messages.batches.results(bid):
            cid = result.custom_id
            meta = rec["requests"][cid]
            run_dir = exp_dir(date_str) / "runs" / cid
            run_dir.mkdir(parents=True, exist_ok=True)
            if result.result.type != "succeeded":
                console.print(f"[red]{cid} {result.result.type}[/red] {result.result}")
                continue
            msg = result.result.message
            md, thinking = llm_extractor.harvest_message_text(msg)
            usage = cost_tracker.usage_to_dict(msg.usage)
            usd = cost_of(meta["model"], usage)
            (run_dir / "extract.md").write_text(md, "utf-8")
            (run_dir / "thinking.md").write_text(thinking, "utf-8")
            (run_dir / "usage.json").write_text(
                json.dumps(
                    {
                        **meta,
                        "stop_reason": msg.stop_reason,
                        "usage": usage,
                        "usd": usd,
                        "elapsed_s": elapsed,
                        "chars": len(md),
                    },
                    indent=2,
                ),
                "utf-8",
            )
            cost_tracker.log_call(
                date=date_str,
                stage=f"exp55:{cid}",
                model=meta["model"],
                usage=msg.usage,
                duration_s=elapsed,
                batch=True,
            )
            console.print(
                f"[bold]{cid}[/bold] stop={msg.stop_reason} in={usage.get('input_tokens')} "
                f"out={usage.get('output_tokens')} ${usd:.3f} 正文 {len(md)} 字"
            )
            if md.strip():
                render_run(date_str, run_dir, md)
        rec["consumed"] = True
        f.write_text(json.dumps(rec, indent=2), "utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("prepare", "submit", "fetch"):
        sp = sub.add_parser(name)
        sp.add_argument("--date", required=True)
        if name == "submit":
            sp.add_argument(
                "--run",
                action="append",
                required=True,
                help="prompt:model:effort[:tag]（tag 区分重复采样）",
            )
        if name == "fetch":
            sp.add_argument("--batch", default=None)
            sp.add_argument("--poll", type=float, default=30.0)
    args = p.parse_args()
    {"prepare": cmd_prepare, "submit": cmd_submit, "fetch": cmd_fetch}[args.cmd](args)


if __name__ == "__main__":
    main()
