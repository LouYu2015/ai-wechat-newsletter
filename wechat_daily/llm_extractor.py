"""Structured extraction: tokenized chat → DailyReport JSON via Claude tool use.

Uses strict=True on the tool definition for grammar-constrained sampling,
which guarantees schema compliance without needing a separate validator.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import httpx
from anthropic.lib.streaming import InputJsonEvent

from .config import CLAUDE_MODEL, GEMINI_SUMMARY_MODEL, DEBUG_DIR
from .models import DailyReport

# ── System prompt ────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
你是一个专门分析 AI 技术讨论群聊天记录的助手。你的任务是从经过匿名化处理的群聊记录中，提取出结构化的日报内容。

## 关于匿名化（最重要的约束，必须严格遵守）

聊天记录中所有群友的名字都已替换为稳定的「token」，格式为「形容词的动物」（如「沉稳的大象」、「活泼的企鹅」）。这些是虚构名称，不是真实昵称。

**硬性规则**：
- 输出的所有字段中，指代群友时**只能使用这些 token**，绝对禁止出现任何真实人名、英文名、昵称、外号、谐音、缩写。
- 若在聊天记录中看到看起来像真实人名或代称的词语（如英文名 Garry/Alice、未经替换的昵称、群友间的外号「鸭哥」、谐音梗、姓氏缩写等），**不要引用或输出这些词**，请利用下方花名册映射回对应 token；若无法确定对应关系，改用「某群友」代替，或直接省略该引用。
- 即使是直接引用原话（comments 字段），如果原话中含有非 token 格式的人名/代称，也必须替换为对应 token 或「某群友」再引用。

## 关于花名册（用于解析消息中的代称）

用户消息开头会附带一份**群友花名册**，列出每个 token 对应的真实昵称与已知群昵称变体。聊天记录里可能出现**未列入花名册**但明显指代某位群友的代称（外号、谐音、缩写），请基于上下文与花名册推断对应 token。拿不准时使用「某群友」，**绝不要在输出中保留任何真实昵称或代称**。

## 关于隐私占位符

部分群友已申请隐私保护，其发言以 `[此消息已隐藏]` 或 `[HH:MM–HH:MM] [某群友连续发言 N 条已隐藏]` 标记。处理这些标记时：
1. 不要试图推测或还原被隐藏的内容。
2. 若某段讨论的关键输入来自被隐藏消息，用「有群友提出了一个观点，引发了讨论」这类模糊表述。
3. 若某条回复明显在回应被隐藏的消息（如「说得对」「同意上面」），保留回复，但不推断被回应内容。
4. 花絮（anecdote）章节：若互动的核心发言来自被隐藏消息，整条跳过。

## 关于 public_safe 判定

对每个 section 的 `public_safe` 字段进行自评。**应标记为 false 的情形**：
1. **隐私顾虑**：内容涉及可与群外信息交叉识别的私人线索（职业、地点、独特经历），即便已匿名化也可能推断出具体个人。
2. **Opt-out 波及**：section 的核心依赖某位 opted-out 群友的发言，即使占位符已遮蔽，剩余上下文仍可能让人推知被隐藏内容。
3. **公众环境风险**：内容在公开互联网语境下可能引起误解、争议、或对当事人/相关方产生负面影响（如涉及第三方的评价、敏感话题的玩笑、可能被断章取义的观点）。

**默认 public_safe = true**；只在明显命中上述三类之一时标 false，并在 `public_safe_reason` 简要说明原因（null 表示安全）。当拿不准时，选 false。

## section type 说明

- `news`：AI 行业新闻、重要发布、产品动态
- `tool`：具体 AI 工具的介绍、评测、使用体验
- `methodology`：方法论、工作流、提示词技巧等
- `anecdote`：闲聊、玩笑、有趣互动、非技术话题（只在确实有趣时收录，不强求）

## 输出要求

- `intro`：一段导读，用 token 指代群友，介绍今天的亮点（包括闲聊花絮）。不要在 intro 中插入 [TOC] 或任何 Markdown 特殊标记。
- 每个 section 的 `body` 言简意赅，保留重要信息，避免过度展开
- `comments` 只挑选最有代表性的 1–3 条，每条引用原话或近似原话
- `tags` 使用英文小写、连字符，如 `model-release`、`long-context`、`agent`
- 若当天消息很少或质量不足，可输出空 sections 列表

## 关于内容完整性（关键约束）

**每个 `body` 字段必须是完整句子，绝对禁止在句子中间截断。**

若遇到无法直接引用的内容（如乱码、格式异常、示例片段），请用描述性语言替代，例如：
- 不写 "给出了示例（如" → 改写为 "给出了一系列无意义的中文示例，表明模型输出质量严重下降。"
- 不写 "退化到" → 改写为 "出现严重退化，中文输出质量明显不可用。"

**所有在 `intro` 中提及的话题，都必须在 `sections` 中生成对应条目。** 如果某个话题信息不足，可以简短描述，但不能省略。
"""

# ── Tool schema with strict=True ─────────────────────────────────────────────────

_TOOL = {
    "name": "submit_daily_report",
    "description": "提交结构化的群聊日报",
    "strict": True,
    "input_schema": {
        "type": "object",
        "required": ["date", "intro", "sections"],
        "additionalProperties": False,
        "properties": {
            "date": {
                "type": "string",
                "description": "日期 YYYY-MM-DD",
            },
            "intro": {
                "type": "string",
                "description": "导读段落，使用 token 指代群友，不包含 [TOC]",
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "type", "title", "body", "comments",
                        "tags", "public_safe", "public_safe_reason",
                    ],
                    "additionalProperties": False,
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["news", "tool", "methodology", "anecdote"],
                        },
                        "title": {"type": "string"},
                        "body": {
                            "type": "string",
                            "description": "正文，多个要点用换行分隔",
                        },
                        "comments": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["token", "text"],
                                "additionalProperties": False,
                                "properties": {
                                    "token": {
                                        "type": "string",
                                        "description": "群友 token（匿名名）",
                                    },
                                    "text": {
                                        "type": "string",
                                        "description": "评论内容，引用原话",
                                    },
                                },
                            },
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "public_safe": {
                            "type": "boolean",
                            "description": "是否适合公开发布",
                        },
                        "public_safe_reason": {
                            "type": ["string", "null"],
                            "description": "public_safe=false 时说明原因，否则 null",
                        },
                    },
                },
            },
        },
    },
}

# ── Structured extraction ────────────────────────────────────────────────────────

class ExtractionError(Exception):
    """Claude returned an unusable response (refusal or max_tokens cutoff)."""


def _default_client(api_key: str):
    import anthropic
    return anthropic.Anthropic(
        api_key=api_key,
        timeout=httpx.Timeout(600.0, connect=30.0),
    )


def extract_report(
    date_str: str,
    tokenized_chat: str,
    api_key: str,
    progress_cb=None,
    client=None,
    roster_text: str | None = None,
) -> DailyReport:
    """Call Claude with strict tool use; return a validated DailyReport.

    *client* may be injected for testing (any object with ``messages.create``
    that matches the Anthropic SDK surface). If None, a default client is
    constructed using *api_key*.

    *roster_text* is the rendered 群友花名册 (see ``wechat_daily.roster``);
    when provided it's prepended to the user message so the model can resolve
    informal references (谐音、外号、缩写) back to tokens.
    """
    import anthropic  # for APIStatusError below

    if client is None:
        client = _default_client(api_key)

    chat_block = f"以下是 {date_str} 的匿名化群聊记录，请提取日报：\n\n{tokenized_chat}"
    user_content = f"{roster_text}\n\n---\n\n{chat_block}" if roster_text else chat_block

    max_retries = 3
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            received = 0
            with client.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=16000,
                system=_SYSTEM_PROMPT,
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": "submit_daily_report"},
                messages=[{"role": "user", "content": user_content}],
            ) as stream:
                for event in stream:
                    if isinstance(event, InputJsonEvent):
                        received += len(event.partial_json)
                        if progress_cb:
                            progress_cb(received, attempt)
                response = stream.get_final_message()

            # Check stop reason before touching content
            if response.stop_reason == "refusal":
                refusal_content = [
                    {"type": b.type, "text": getattr(b, "text", None)}
                    for b in response.content
                ]
                _save_failure(date_str, user_content, None,
                              "Claude 拒绝处理该内容", refusal_content)
                raise ExtractionError("Claude 拒绝处理该内容（stop_reason=refusal）")

            if response.stop_reason == "max_tokens":
                _save_failure(date_str, user_content, None, "响应被 max_tokens 截断")
                raise ExtractionError("响应被 max_tokens 截断，请增大 max_tokens 后重试")

            tool_block = next(
                (b for b in response.content if b.type == "tool_use"), None
            )
            if not tool_block:
                _save_failure(date_str, user_content, None, "响应中无 tool_use 块")
                raise ExtractionError("响应中无 tool_use 块")

            raw: dict = tool_block.input
            raw["date"] = date_str  # ensure date is always set

            _check_truncation(date_str, raw)

            # Build DailyReport (models.py validates types/enums)
            report = DailyReport.from_dict(raw)
            _save_extract(date_str, raw, user_content)
            return report

        except ExtractionError:
            raise  # don't retry on logic errors
        except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError) as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(5 * attempt)
        except anthropic.APIStatusError as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(30)
        except (KeyError, TypeError, ValueError) as e:
            # strict=True makes schema violations very rare, but handle defensively
            _save_failure(date_str, user_content, locals().get('raw'), str(e))
            raise ExtractionError(f"响应数据无效: {e}") from e

    _save_failure(date_str, user_content, None, str(last_exc))
    raise last_exc  # type: ignore[misc]


_TRUNCATION_ENDINGS = (
    '到', '如', '（如', '的', '了', '是', '、', '，', '：', '：\n', '为',
    '（', '(', '"', "'",
)


def _check_truncation(date_str: str, raw: dict) -> None:
    """Warn (via stderr) if any section body looks like it was cut off mid-sentence.

    With strict=True JSON grammar constraints, Claude can close a string at any
    valid string boundary. When it encounters content it doesn't want to reproduce
    (e.g. garbled AI output examples), it may end the body mid-sentence. This
    function logs a warning so operators notice the issue.
    """
    import sys
    sections = raw.get("sections", [])
    truncated = []
    for s in sections:
        body = s.get("body", "")
        if body and body.endswith(_TRUNCATION_ENDINGS):
            truncated.append(s.get("title", "?")[:40])
    if truncated:
        print(
            f"[llm_extractor] WARNING {date_str}: {len(truncated)} section(s) "
            f"appear truncated mid-sentence: {truncated}",
            file=sys.stderr,
        )


def _save_extract(date_str: str, raw: dict, user_content: str) -> None:
    """Save successful extraction to debug/. Includes truncated input for redact."""
    DEBUG_DIR.mkdir(exist_ok=True, parents=True)
    # Store input preview alongside the report for redact.py and debugging.
    # The preview includes the roster header so we can audit "why didn't the
    # model resolve X" after the fact. DailyReport.from_dict ignores unknown
    # top-level keys, so this is safe.
    payload = dict(raw)
    payload['_input_preview'] = user_content[:5000]
    path = DEBUG_DIR / f"extract-{date_str}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_failure(
    date_str: str,
    user_content: str,
    raw,
    reason: str,
    refusal_content: list | None = None,
) -> None:
    """Persist failure details to debug/ for post-mortem inspection."""
    DEBUG_DIR.mkdir(exist_ok=True, parents=True)
    path = DEBUG_DIR / f"extract-{date_str}.FAILED.json"
    payload: dict = {
        "reason": reason,
        "raw_response": raw,
        "input_preview": user_content[:3000],
    }
    if refusal_content is not None:
        payload["refusal_content"] = refusal_content
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Legacy Gemini path ────────────────────────────────────────────────────────────

_LEGACY_PROMPT = """\
为以下群聊消息编写一个每日总结，让对 AI 前沿发展感兴趣的人士了解群里的最新动态。\
总结中要包含具体的群友名称（注意：名称已匿名化处理，请使用记录中出现的 token 名字）。\
重点关注最新的行业新闻、AI 工具和方法论，同时也要捕捉群里的人情味与有趣瞬间。

## 闲聊与花絮
在正文内容之后，如果有趣内容足够，添加"闲聊与花絮"章节。

文章言简意赅，但保留重要有用信息。所有引用群友发言的地方使用 Markdown 引用框（> 语法）。

最开始写一段导读，介绍今天亮点。导读之后单独一行写 [TOC]，程序将在此处插入目录。"""


def generate_markdown_with_gemini(
    chat_history: str,
    api_key: str,
    progress_cb=None,
) -> str:
    """Legacy Gemini path: returns raw Markdown (no structured extraction)."""
    from google import genai as google_genai
    from google.genai import types

    client = google_genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=300000),
    )
    prompt = f"{_LEGACY_PROMPT}\n\n--- 聊天记录 ---\n\n{chat_history}"

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        report_text = ""
        try:
            response_stream = client.models.generate_content_stream(
                model=GEMINI_SUMMARY_MODEL,
                contents=[types.Part.from_text(text=prompt)],
                config=types.GenerateContentConfig(temperature=0.7, max_output_tokens=65536),
            )
            for chunk in response_stream:
                if chunk.text:
                    report_text += chunk.text
                    if progress_cb:
                        progress_cb(len(report_text), attempt)
            return report_text
        except Exception:
            if attempt < max_retries:
                time.sleep(10 * attempt)
            else:
                raise

    return report_text
