"""Replay one day through the extractor with a swappable system prompt.

Use case: trying out a tweak to the Opus 4.7 system-prompt appendix
(`_OPUS_4_7_APPENDIX` in :mod:`wechat_daily.llm_extractor`) without touching
main code or re-running the whole pipeline. Reads the saved input from
``debug/extract-{DATE}.opus-4-7.input.txt`` (truncated at 50K chars by the
extractor's debug sink — good enough to compare *thinking shape* and *output
voice* across prompt variants; not a fair benchmark of absolute coverage,
since the chat tail is cut off).

To experiment with a new appendix:
1. Set ``APPENDIX_OVERRIDE`` below to your candidate string (leave ``None``
   to replay with the currently-shipped prompt).
2. Run ``python scripts/probe_extractor_prompt.py``.
3. Diff ``debug/probe-{DATE}.opus-4-7.{md,thinking.md}`` against the canonical
   ``extract-{DATE}.opus-4-7.{md,thinking.md}``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from wechat_daily.llm_extractor import (
    _OPUS_4_7_APPENDIX,
    _SYSTEM_PROMPT_OPUS_4_6,
    _USER_INSTRUCTIONS,
)

load_dotenv(PROJECT_ROOT / ".env")

# ── Knobs ────────────────────────────────────────────────────────────────────
DATE = "2026-05-19"

# Set to a string to override the shipped 4.7 appendix for this probe;
# leave None to replay with the current shipped prompt.
APPENDIX_OVERRIDE: str | None = None
# ─────────────────────────────────────────────────────────────────────────────

DEBUG = PROJECT_ROOT / "debug"
INPUT_PATH = DEBUG / f"extract-{DATE}.opus-4-7.input.txt"
OUT_MD = DEBUG / f"probe-{DATE}.opus-4-7.md"
OUT_THINK = DEBUG / f"probe-{DATE}.opus-4-7.thinking.md"

SYSTEM_PROMPT = _SYSTEM_PROMPT_OPUS_4_6 + (APPENDIX_OVERRIDE or _OPUS_4_7_APPENDIX)


def main() -> None:
    raw = INPUT_PATH.read_text(encoding="utf-8")
    # The saved input was truncated at 50K chars mid-chat — it does NOT contain
    # the closing </chat_log> tag or the user instructions tail. Reattach a
    # graceful close + the standard instructions so the model gets a well-formed
    # prompt. We're not trying to reproduce the original output verbatim; we
    # only need to compare prompt variants on the same input.
    if "</chat_log>" not in raw:
        raw = raw + "\n[... 聊天记录在此处被截断 ...]\n</chat_log>\n\n---\n\n"
        raw = raw + _USER_INSTRUCTIONS.format(date_str=DATE)

    import anthropic
    import httpx

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY missing")

    client = anthropic.Anthropic(api_key=api_key, timeout=httpx.Timeout(600.0, connect=30.0))

    body_parts: list[str] = []
    thinking_parts: list[str] = []
    last_print = 0
    last_t_print = 0

    variant = "override" if APPENDIX_OVERRIDE else "shipped"
    print(
        f"[probe] model=claude-opus-4-7 effort=xhigh appendix={variant} "
        f"input={len(raw):,} chars",
        flush=True,
    )
    with client.messages.stream(
        model="claude-opus-4-7",
        max_tokens=128000,
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": "xhigh"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": raw}],
    ) as stream:
        for event in stream:
            etype = getattr(event, "type", None)
            delta = getattr(event, "text", None)
            if isinstance(delta, str) and etype == "text":
                body_parts.append(delta)
                tot = sum(len(p) for p in body_parts)
                if tot - last_print > 2000:
                    print(f"  body: {tot:,} chars", flush=True)
                    last_print = tot
                continue
            if etype == "content_block_delta":
                d = getattr(event, "delta", None)
                if d is not None and getattr(d, "type", None) == "thinking_delta":
                    t = getattr(d, "thinking", "")
                    if isinstance(t, str) and t:
                        thinking_parts.append(t)
                        tot = sum(len(p) for p in thinking_parts)
                        if tot - last_t_print > 1500:
                            print(f"  thinking: {tot:,} chars", flush=True)
                            last_t_print = tot
        final = stream.get_final_message()

    body = "".join(body_parts)
    thinking = "".join(thinking_parts)
    OUT_MD.write_text(body, encoding="utf-8")
    OUT_THINK.write_text(thinking, encoding="utf-8")
    usage = getattr(final, "usage", None)
    print(
        f"\n[probe] done. stop_reason={final.stop_reason} "
        f"body={len(body):,} thinking={len(thinking):,}",
        flush=True,
    )
    if usage:
        print(f"[probe] usage: {usage}", flush=True)
    print(f"[probe] wrote: {OUT_MD.name}, {OUT_THINK.name}", flush=True)


if __name__ == "__main__":
    main()
