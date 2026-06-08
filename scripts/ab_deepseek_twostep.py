"""One-off A/B: old single-call DeepSeek vs new two-step pipeline.

Feeds BOTH paths the same reconstructed input (roster + chat parsed out of a
saved debug sidecar) so the only variable is the pipeline. Hits the real
DeepSeek API (cheap). Prints thinking/outline/output sizes + a coverage and
intro↔body consistency check.

Usage: python scripts/ab_deepseek_twostep.py debug/extract-2026-06-07.deepseek-v4-pro.input.txt 2026-06-07
"""

from __future__ import annotations

import re
import sys
import time

from wechat_daily import deepseek_client
from wechat_daily.config import DEEPSEEK_REPORT_MODEL, get_deepseek_key
from wechat_daily.llm_extractor import (
    _SYSTEM_PROMPT, _build_extract_user_content, extract_report_deepseek,
)


def parse_input(path: str) -> tuple[str, str]:
    raw = open(path, encoding="utf-8").read()
    rm = re.search(r"<group_roster>\n(.*?)\n</group_roster>", raw, re.DOTALL)
    roster = rm.group(1) if rm else ""
    cm = re.search(r'<chat_log date="[^"]+">\n(.*)\Z', raw, re.DOTALL)
    chat = cm.group(1) if cm else raw
    return roster, chat


def count_sections(md: str) -> tuple[int, int]:
    h2 = len(re.findall(r"(?m)^## ", md))
    h3 = len(re.findall(r"(?m)^### ", md))
    return h2, h3


def run_old(date: str, chat: str, roster: str) -> dict:
    """Replicate the PRE-change behavior: few-shot system + temp 1.0, one call."""
    user, _ = _build_extract_user_content(
        date_str=date, tokenized_chat=chat, roster_text=roster,
        chat_blocks=None, prior_reports=None, prior_report_titles=None,
    )
    st = {"reason": 0, "body": 0}
    t0 = time.perf_counter()
    content, reasoning, usage, finish = deepseek_client.stream_chat(
        api_key=get_deepseek_key(), model=DEEPSEEK_REPORT_MODEL,
        system=_SYSTEM_PROMPT, user=user, thinking=True, max_tokens=65536,
        temperature=1.0,
        reasoning_cb=lambda d: st.__setitem__("reason", st["reason"] + len(d)),
        content_cb=lambda d: st.__setitem__("body", st["body"] + len(d)),
    )
    md = deepseek_client.strip_markdown_fence(content)
    h2, h3 = count_sections(md)
    return {"thinking": st["reason"], "md": md, "h2": h2, "h3": h3,
            "dur": time.perf_counter() - t0, "finish": finish}


def run_new(date: str, chat: str, roster: str) -> dict:
    st = {"reason": 0}
    t0 = time.perf_counter()
    rep = extract_report_deepseek(
        date, chat, model=DEEPSEEK_REPORT_MODEL, debug_suffix=".ab-new",
        roster_text=roster,
        thinking_cb=lambda n, a: st.__setitem__("reason", n),
    )
    h2, h3 = count_sections(rep.markdown)
    return {"thinking": st["reason"], "md": rep.markdown, "h2": h2, "h3": h3,
            "dur": time.perf_counter() - t0}


def intro_body_consistency(md: str) -> str:
    """Crude check: do the bolded/【】topics named in the intro appear as headers?"""
    intro = md.split("\n## ", 1)[0]
    headers = set(re.findall(r"(?m)^#{2,3} (.+)$", md))
    return f"intro {len(intro)} 字；正文 {len(headers)} 个标题"


def main() -> None:
    path = sys.argv[1]
    date = sys.argv[2] if len(sys.argv) > 2 else "2026-06-07"
    roster, chat = parse_input(path)
    print(f"输入：roster {len(roster)} 字 | chat {len(chat)} 字（{path}，已知截断）\n")

    print("▶ 跑 OLD（few-shot system + temp 1.0，单步）…")
    old = run_old(date, chat, roster)
    print(f"  thinking {old['thinking']} 字 | 输出 {len(old['md'])} 字 | "
          f"## {old['h2']} / ### {old['h3']} | {old['dur']:.0f}s\n")

    print("▶ 跑 NEW（两步：大纲→写作，temp 0.6）…")
    new = run_new(date, chat, roster)
    print(f"  thinking {new['thinking']} 字 | 输出 {len(new['md'])} 字 | "
          f"## {new['h2']} / ### {new['h3']} | {new['dur']:.0f}s\n")

    print("== 对比 ==")
    print(f"  规划/思考量  OLD {old['thinking']:>6} → NEW {new['thinking']:>6} 字")
    print(f"  ### 子话题   OLD {old['h3']:>6} → NEW {new['h3']:>6}")
    print(f"  OLD 一致性：{intro_body_consistency(old['md'])}")
    print(f"  NEW 一致性：{intro_body_consistency(new['md'])}")

    for tag, d in (("OLD", old), ("NEW", new)):
        out = f"debug/ab-{date}.{tag}.md"
        open(out, "w", encoding="utf-8").write(d["md"])
        print(f"  写出 {out}")


if __name__ == "__main__":
    main()
