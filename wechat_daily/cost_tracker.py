"""Per-API-call token usage + cost tracking.

Each Anthropic call (Opus extract, Sonnet link summary, …) calls
:func:`log_call` to append a JSON record to ``debug/costs.jsonl``. At the end
of a run, the CLI passes the in-memory list to :func:`summarize` which
returns a Rich ``Table`` aggregated by (date, stage, model).

Pricing is read from :data:`wechat_daily.config.MODEL_PRICES`. Cost is
estimated from the Anthropic SDK's ``response.usage`` object
(``input_tokens`` / ``output_tokens`` / ``cache_creation_input_tokens`` /
``cache_read_input_tokens``). Adaptive-thinking tokens are billed as output
and already roll into ``output_tokens``, so we don't separate them.

The ``input_chars`` field on each record stores the raw character count of
the prompt fed to the model — used to compute a ``tok/char`` ratio in the
summary, useful as a proxy for tokenizer efficiency across models.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DEBUG_DIR, MODEL_PRICES


# ── Usage normalization ─────────────────────────────────────────────────────────

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def usage_to_dict(usage: Any) -> dict[str, int]:
    """Coerce an Anthropic SDK ``Usage`` object (or dict, or ``None``) → int dict.

    Unknown / missing fields default to 0 in the returned dict's effective
    semantics (omitted keys = 0 in :func:`estimate_cost`).
    """
    if usage is None:
        return {}
    if isinstance(usage, dict):
        src = usage
    else:
        src = {k: getattr(usage, k, None) for k in _USAGE_FIELDS}

    # DeepSeek (OpenAI-compatible) usage → Anthropic-shaped fields. Its dict has
    # prompt_tokens / completion_tokens / prompt_cache_{hit,miss}_tokens (and
    # nested *_details dicts we must not int()). completion_tokens already
    # includes reasoning tokens, matching how adaptive-thinking rolls into
    # output_tokens. Handle this before the generic int() coercion below.
    if "prompt_tokens" in src or "completion_tokens" in src:
        hit = int(src.get("prompt_cache_hit_tokens") or 0)
        miss = src.get("prompt_cache_miss_tokens")
        if miss is None:
            miss = int(src.get("prompt_tokens") or 0) - hit
        return {
            "input_tokens": max(0, int(miss)),
            "output_tokens": int(src.get("completion_tokens") or 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": hit,
        }

    # Gemini SDK `usage_metadata` → Anthropic-shaped fields. It carries
    # prompt_token_count / candidates_token_count / cached_content_token_count.
    # prompt_token_count is the *total* input incl. cached, so subtract the
    # cached part out to input_tokens and bill it as a cache read.
    if "prompt_token_count" in src or "candidates_token_count" in src:
        cached = int(src.get("cached_content_token_count") or 0)
        prompt = int(src.get("prompt_token_count") or 0)
        return {
            "input_tokens": max(0, prompt - cached),
            "output_tokens": int(src.get("candidates_token_count") or 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": cached,
        }

    return {k: int(v) for k, v in src.items() if v is not None}


# ── Cost estimation ─────────────────────────────────────────────────────────────


def estimate_cost(model: str, usage: dict[str, int]) -> float:
    """Estimate USD cost of one Anthropic call from usage counts.

    Unknown models return ``0.0`` — we'd rather under-report than crash a
    daily run. The CLI displays the raw model name so over-reporting wouldn't
    silently mislead.
    """
    prices = MODEL_PRICES.get(model)
    if not prices:
        return 0.0
    n_input = usage.get("input_tokens", 0)
    n_output = usage.get("output_tokens", 0)
    n_cache_write = usage.get("cache_creation_input_tokens", 0)
    n_cache_read = usage.get("cache_read_input_tokens", 0)
    return (
        n_input * prices["input"]
        + n_output * prices["output"]
        + n_cache_write * prices["cache_write_5m"]
        + n_cache_read * prices["cache_read"]
    ) / 1_000_000.0


# ── Record + log ────────────────────────────────────────────────────────────────


@dataclass
class CostRecord:
    """One Anthropic API call's token usage + estimated cost."""

    ts: str
    date: str
    stage: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    duration_s: float
    estimated_cost_usd: float
    input_chars: int | None = None
    prices: dict[str, float] = field(default_factory=dict)


def log_call(
    *,
    date: str,
    stage: str,
    model: str,
    usage: Any,
    duration_s: float,
    input_chars: int | None = None,
    debug_dir: Path | None = None,
) -> CostRecord:
    """Append one cost record to ``debug/costs.jsonl`` and return it.

    *date* — report date (``YYYY-MM-DD``) the call belongs to.
    *stage* — free-form tag (``extract`` / ``extract-compare`` / ``link``).
    *usage* — Anthropic SDK ``Usage`` object (or dict, or ``None``).
    *duration_s* — wall-clock seconds the call took (caller measures).
    *input_chars* — len of prompt text for tok/char tokenizer-efficiency ratio.
    """
    u = usage_to_dict(usage)
    cost = estimate_cost(model, u)
    record = CostRecord(
        ts=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        date=date,
        stage=stage,
        model=model,
        input_tokens=u.get("input_tokens", 0),
        output_tokens=u.get("output_tokens", 0),
        cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0),
        cache_read_input_tokens=u.get("cache_read_input_tokens", 0),
        duration_s=round(duration_s, 2),
        estimated_cost_usd=round(cost, 6),
        input_chars=input_chars,
        prices=dict(MODEL_PRICES.get(model, {})),
    )
    path = (debug_dir or DEBUG_DIR) / "costs.jsonl"
    path.parent.mkdir(exist_ok=True, parents=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    return record


# ── Summary table ───────────────────────────────────────────────────────────────


def _aggregate(records: Iterable[CostRecord]) -> list[dict]:
    """Group records by (date, stage, model) and sum the numeric fields.

    Returned in input order of first-seen group key.
    """
    groups: dict[tuple[str, str, str], dict] = {}
    order: list[tuple[str, str, str]] = []
    for r in records:
        key = (r.date, r.stage, r.model)
        if key not in groups:
            groups[key] = {
                "date": r.date, "stage": r.stage, "model": r.model,
                "input_tokens": 0, "output_tokens": 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                "duration_s": 0.0, "estimated_cost_usd": 0.0,
                "input_chars": 0, "calls": 0,
            }
            order.append(key)
        g = groups[key]
        g["input_tokens"] += r.input_tokens
        g["output_tokens"] += r.output_tokens
        g["cache_creation_input_tokens"] += r.cache_creation_input_tokens
        g["cache_read_input_tokens"] += r.cache_read_input_tokens
        g["duration_s"] += r.duration_s
        g["estimated_cost_usd"] += r.estimated_cost_usd
        if r.input_chars:
            g["input_chars"] += r.input_chars
        g["calls"] += 1
    return [groups[k] for k in order]


def summarize(records: Iterable[CostRecord]):
    """Return a Rich ``Table`` summarizing usage and cost.

    Records are grouped by (date, stage, model). The ``tok/char`` column is
    ``input_tokens / input_chars`` when both are known — useful as a proxy
    for tokenizer efficiency across models.
    """
    from rich.table import Table

    table = Table(title="模型用量与成本估算")
    table.add_column("日期", style="cyan")
    table.add_column("阶段", style="dim")
    table.add_column("模型")
    table.add_column("次数", justify="right", style="dim")
    table.add_column("输入tok", justify="right")
    table.add_column("输出tok", justify="right")
    table.add_column("缓存读", justify="right", style="dim")
    table.add_column("tok/char", justify="right")
    table.add_column("时长(s)", justify="right")
    table.add_column("成本(USD)", justify="right", style="bold")

    total_cost = 0.0
    rows = _aggregate(records)
    for g in rows:
        if g["input_chars"]:
            tok_per_char = f"{g['input_tokens'] / g['input_chars']:.3f}"
        else:
            tok_per_char = "—"
        table.add_row(
            g["date"], g["stage"], g["model"],
            str(g["calls"]),
            f"{g['input_tokens']:,}",
            f"{g['output_tokens']:,}",
            f"{g['cache_read_input_tokens']:,}",
            tok_per_char,
            f"{g['duration_s']:.1f}",
            f"${g['estimated_cost_usd']:.4f}",
        )
        total_cost += g["estimated_cost_usd"]

    if rows:
        table.add_row(
            "", "", "[bold]合计", "",
            "", "", "", "", "",
            f"[bold]${total_cost:.4f}",
        )
    return table
