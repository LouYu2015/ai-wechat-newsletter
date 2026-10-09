"""A/B harness: OpenAI models on the frozen exp55 inputs (vs Opus 5.5 v6 runs).

Reuses what ``scripts.exp_opus55 prepare`` froze under ``debug/YYYY/MM/DD/exp55/``
(content.json, images, render_state.pkl) and the production prompt, so the
OpenAI runs see byte-identical chat input to ``runs/v6-o55-high``:

    TZ=America/Los_Angeles python -m scripts.exp_openai count  --date 2026-10-06 --model gpt-6.1-sol
    TZ=America/Los_Angeles python -m scripts.exp_openai submit --date 2026-10-06 \\
        --run gpt-6.1-sol:high --run gpt-6-astra:high
    TZ=America/Los_Angeles python -m scripts.exp_openai fetch  --date 2026-10-06

Calls the Responses API through the Batch API (50% off) with stdlib ``urllib``,
like :mod:`wechat_daily.deepseek_client` — no openai SDK dependency. Output
goes to ``exp55/runs/<custom_id>/`` in the same layout as exp_opus55.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
import uuid

import rich.console

from scripts import exp_opus55
from wechat_daily import config

console = rich.console.Console()

API = "https://api.openai.com/v1"
MAX_OUTPUT_TOKENS = 128_000

# Standard short-context (≤272K) prices, USD per 1M tokens (developers.openai.com, 2026-10-09).
PRICES = {
    "gpt-6.1-sol": {"input": 2.00, "cached": 0.10, "output": 10.00},
    "gpt-6-sol": {"input": 2.00, "cached": 0.20, "output": 10.00},
    "gpt-6-astra": {"input": 10.00, "cached": 1.00, "output": 50.00},
}


def api_key() -> str:
    config.load_env()
    return os.getenv("CHATGPT_API_KEY", "")


def request(method: str, path: str, body: bytes | None = None, ctype: str = "application/json"):
    req = urllib.request.Request(f"{API}{path}", data=body, method=method)
    req.add_header("Authorization", f"Bearer {api_key()}")
    if body is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} → HTTP {e.code}: {e.read().decode()[:2000]}") from None
    return raw


def request_json(method: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    return json.loads(request(method, path, body))


# ── request building ─────────────────────────────────────────────────────────────


def to_openai_content(blocks: list[dict]) -> list[dict]:
    """Anthropic content blocks → Responses API input parts."""
    out = []
    for b in blocks:
        if b["type"] == "text":
            out.append({"type": "input_text", "text": b["text"]})
        elif b["type"] == "image":
            src = b["source"]
            out.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{src['media_type']};base64,{src['data']}",
                }
            )
        else:
            raise ValueError(f"unsupported block type {b['type']}")
    return out


def build_body(date_str: str, model: str, effort: str, prompt_name: str = "baseline") -> dict:
    content = json.loads((exp_opus55.exp_dir(date_str) / "content.json").read_text("utf-8"))
    system, instr = exp_opus55.load_prompt(date_str, prompt_name)
    content.append({"type": "text", "text": "\n---\n\n" + instr})
    return {
        "model": model,
        "instructions": system,
        "input": [{"role": "user", "content": to_openai_content(content)}],
        "reasoning": {"effort": effort, "summary": "auto"},
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "store": False,
    }


def custom_id(prompt_name: str, model: str, effort: str, tag: str = "") -> str:
    name = "v6" if prompt_name == "baseline" else prompt_name
    return (f"{name}-{model}-{effort}" + (f"-{tag}" if tag else "")).replace(".", "_")


def parse_spec(spec: str) -> tuple[str, str, str, str]:
    """``[prompt:]model:effort[:tag]`` — prompt defaults to the production one."""
    parts = spec.split(":")
    if not parts[0].startswith(("gpt-", "o")):
        prompt_name, parts = parts[0], parts[1:]
    else:
        prompt_name = "baseline"
    model, effort, *rest = parts
    return prompt_name, model, effort, rest[0] if rest else ""


def cost_of(model: str, usage: dict, batch: bool = True) -> float:
    p = PRICES[model]
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
    usd = (
        (usage.get("input_tokens", 0) - cached) * p["input"]
        + cached * p["cached"]
        + usage.get("output_tokens", 0) * p["output"]
    ) / 1e6
    return usd * (0.5 if batch else 1.0)


# ── commands ─────────────────────────────────────────────────────────────────────


def cmd_count(args) -> None:
    body = build_body(args.date, args.model, "high")
    body = {k: body[k] for k in ("model", "instructions", "input")}
    r = request_json("POST", "/responses/input_tokens", body)
    console.print(f"{args.model} 输入 token：{r}")


def cmd_submit(args) -> None:
    date_str = args.date
    lines, meta = [], {}
    for spec in args.run:
        prompt_name, model, effort, tag = parse_spec(spec)
        cid = custom_id(prompt_name, model, effort, tag)
        lines.append(
            json.dumps(
                {
                    "custom_id": cid,
                    "method": "POST",
                    "url": "/v1/responses",
                    "body": build_body(date_str, model, effort, prompt_name),
                },
                ensure_ascii=False,
            )
        )
        meta[cid] = {"prompt": prompt_name, "model": model, "effort": effort}
    jsonl = ("\n".join(lines) + "\n").encode()

    boundary = uuid.uuid4().hex
    form = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nbatch\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="exp.jsonl"\r\n'
            f"Content-Type: application/jsonl\r\n\r\n"
        ).encode()
        + jsonl
        + f"\r\n--{boundary}--\r\n".encode()
    )
    f = json.loads(request("POST", "/files", form, f"multipart/form-data; boundary={boundary}"))
    batch = request_json(
        "POST",
        "/batches",
        {"input_file_id": f["id"], "endpoint": "/v1/responses", "completion_window": "24h"},
    )
    rec = {
        "provider": "openai",
        "batch_id": batch["id"],
        "submitted_at": time.time(),
        "requests": meta,
        "consumed": False,
    }
    bdir = exp_opus55.exp_dir(date_str) / "batches_openai"
    bdir.mkdir(exist_ok=True)
    (bdir / f"{batch['id']}.json").write_text(json.dumps(rec, indent=2), "utf-8")
    console.print(
        f"[green]已提交[/green] {batch['id']}（{len(jsonl) / 1e6:.1f} MB）: {', '.join(meta)}"
    )


def harvest(resp: dict) -> tuple[str, str]:
    md, thinking = [], []
    for item in resp.get("output", []):
        if item["type"] == "message":
            md += [c.get("text", "") for c in item["content"] if c["type"] == "output_text"]
        elif item["type"] == "reasoning":
            thinking += [s.get("text", "") for s in item.get("summary", [])]
    return "".join(md), "\n\n".join(thinking)


def save_run(date_str: str, cid: str, meta: dict, resp: dict, elapsed: float) -> None:
    run_dir = exp_opus55.exp_dir(date_str) / "runs" / cid
    run_dir.mkdir(parents=True, exist_ok=True)
    md, thinking = harvest(resp)
    usage = resp.get("usage", {})
    usd = cost_of(meta["model"], usage, batch=meta.get("tier", "batch") in ("batch", "flex"))
    (run_dir / "extract.md").write_text(md, "utf-8")
    (run_dir / "thinking.md").write_text(thinking, "utf-8")
    (run_dir / "usage.json").write_text(
        json.dumps(
            {
                **meta,
                "status": resp.get("status"),
                "incomplete_details": resp.get("incomplete_details"),
                "usage": usage,
                "usd": usd,
                "elapsed_s": elapsed,
                "chars": len(md),
            },
            indent=2,
        ),
        "utf-8",
    )
    reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
    console.print(
        f"[bold]{date_str} {cid}[/bold] {resp.get('status')} in={usage.get('input_tokens')} "
        f"out={usage.get('output_tokens')}（思考 {reasoning}）${usd:.3f} 正文 {len(md)} 字 "
        f"{elapsed:.0f}s"
    )
    if md.strip():
        exp_opus55.render_run(date_str, run_dir, md)


def cmd_run(args) -> None:
    """Synchronous fallback for keys without the Files scope (no Batch API).

    ``--tier flex`` is the same 50% discount as Batch; if flex capacity is
    unavailable the request is retried once at standard price.
    """
    date_str = args.date
    prompt_name, model, effort, tag = parse_spec(args.spec)
    cid = custom_id(prompt_name, model, effort, tag)
    body = build_body(date_str, model, effort, prompt_name)
    tier = args.tier
    t0 = time.time()
    try:
        resp = request_json("POST", "/responses", {**body, "service_tier": tier})
    except SystemExit as e:
        if tier != "flex" or "flex_unavailable" not in str(e):
            raise
        console.print(f"{cid}: flex 不可用，改用标准价")
        tier = "default"
        resp = request_json("POST", "/responses", {**body, "service_tier": tier})
    meta = {
        "prompt": prompt_name,
        "model": model,
        "effort": effort,
        "tier": resp.get("service_tier", tier),
    }
    save_run(date_str, cid, meta, resp, time.time() - t0)


def cmd_fetch(args) -> None:
    date_str = args.date
    bdir = exp_opus55.exp_dir(date_str) / "batches_openai"
    for f in sorted(bdir.glob("*.json")):
        rec = json.loads(f.read_text("utf-8"))
        if rec["consumed"]:
            continue
        bid = rec["batch_id"]
        while True:
            b = request_json("GET", f"/batches/{bid}")
            if b["status"] in ("completed", "failed", "expired", "cancelled"):
                break
            console.print(f"{bid} {b['status']} {b.get('request_counts')} …")
            time.sleep(args.poll)
        if b.get("error_file_id"):
            err = request("GET", f"/files/{b['error_file_id']}/content").decode()
            console.print(f"[red]错误[/red] {err[:3000]}")
        if b["status"] != "completed" or not b.get("output_file_id"):
            console.print(f"[red]{bid} {b['status']}[/red] {b.get('errors')}")
            continue
        elapsed = (b.get("completed_at") or time.time()) - rec["submitted_at"]
        raw = request("GET", f"/files/{b['output_file_id']}/content").decode()
        for line in raw.splitlines():
            row = json.loads(line)
            cid = row["custom_id"]
            meta = rec["requests"][cid]
            resp = (row.get("response") or {}).get("body") or {}
            if row.get("error") or resp.get("status") not in ("completed", "incomplete"):
                console.print(f"[red]{cid}[/red] {row.get('error') or resp.get('error')}")
                continue
            save_run(date_str, cid, meta, resp, elapsed)
        rec["consumed"] = True
        f.write_text(json.dumps(rec, indent=2), "utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("count", "submit", "fetch", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--date", required=True)
        if name == "count":
            sp.add_argument("--model", default="gpt-6.1-sol")
        if name == "submit":
            sp.add_argument(
                "--run", action="append", required=True, help="[prompt:]model:effort[:tag]"
            )
        if name == "fetch":
            sp.add_argument("--poll", type=float, default=30.0)
        if name == "run":
            sp.add_argument("spec", help="[prompt:]model:effort[:tag]")
            sp.add_argument("--tier", default="flex", choices=["flex", "default"])
    args = p.parse_args()
    {"count": cmd_count, "submit": cmd_submit, "fetch": cmd_fetch, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
