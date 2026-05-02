"""Markdown extraction: tokenized chat → DailyReport(markdown) via Claude streaming.

Plain-text streaming output, no tool use. The prompt fixes the markdown
structure (intro + ## type sections + ### topic three-part blocks + tags
footer + per-section `[章节不公开：原因]` hide markers). The renderer parses
the markdown and produces both the group and public versions.
"""

from __future__ import annotations

import re
import time

import httpx

# Bold-wrapped line in summarized thinking, e.g. "**Analyzing the chat**".
_THINKING_HEADER_RE = re.compile(r"^\s*\*\*(.+?)\*\*\s*$")
# Body markdown ## / ### header line.
_BODY_HEADER_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")

from .config import CLAUDE_MODEL, GEMINI_SUMMARY_MODEL, DEBUG_DIR
from .models import DailyReport

# ── System prompt ────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
你是一个专门分析 AI 技术讨论群聊天记录的助手。从经过匿名化处理的群聊记录中，写出当天的 Markdown 日报。

## 关于匿名化（最重要的约束，必须严格遵守）

聊天记录中所有群友的名字都已替换为稳定的「token」，格式为「形容词的动物」（如「沉稳的大象」、「活泼的企鹅」）。这些是虚构名称，不是真实昵称。

**硬性规则**：
- 输出中指代群友时**只能使用这些 token**，绝对禁止出现任何真实人名、英文名、昵称、外号、谐音、缩写。
- 若聊天记录中看到看起来像真实人名或代称的词语（英文名、未替换的昵称、外号、谐音梗、姓氏缩写等），不要直接引用，请利用花名册映射回对应 token；无法确定时改用「某群友」或省略该引用。
- 即使引用原话，原话中的人名/代称也要先替换为 token 或「某群友」再引用。

## 关于 `token⟨原文⟩` 标记（同名消歧）

为避免群友昵称与产品/模型/公司名同形（如某群友昵称恰好叫「DeepSeek」），系统自动替换的昵称会以 `token⟨原文⟩` 的形式同时呈现。请基于上下文二选一：

- 指代**群友本人**（被 @、与「说/问/回复/分享」等人类行为搭配）→ 输出 token，丢弃 `⟨原文⟩`。
- 指代**非人类实体**（AI 模型、产品、工具、公司、协议、版本号）→ 输出原文，丢弃 token。
- 拿不准时一律按群友处理（输出 token）。
- **绝对禁止在最终输出中保留 `⟨` 或 `⟩` 字符。**

## 关于花名册

用户消息开头会附带一份**群友花名册**，列出每个 token 对应的真实昵称与已知群昵称变体。聊天记录里可能出现**未列入花名册**但明显指代某位群友的代称（外号、谐音、缩写），请基于上下文与花名册推断对应 token。拿不准时使用「某群友」，**绝不要保留任何真实昵称或代称**。

## 关于隐私占位符

部分群友已申请隐私保护，其发言以 `[此消息已隐藏]` 或 `[HH:MM–HH:MM] [某群友连续发言 N 条已隐藏]` 标记。处理这些标记时：
1. 不要试图推测或还原被隐藏的内容。
2. 若某段讨论的关键输入来自被隐藏消息，用「有群友提出了一个观点，引发了讨论」这类模糊表述。
3. 若某条回复明显在回应被隐藏的消息，保留回复，但不推断被回应内容。
4. 闲聊花絮：若互动核心来自被隐藏消息，整条跳过。

## 关于内容完整性

每段正文必须是**完整句子**，不要在句中截断。若遇到无法直接引用的内容（乱码、格式异常、示例片段），请用描述性语言代替：
- 不写「给出了示例（如」→ 写「给出了一系列无意义的中文示例，表明模型输出质量严重下降」。
- 不写「退化到」→ 写「出现严重退化，中文输出明显不可用」。

intro 中提到的话题，下方都要有对应章节；信息不足可简短描述，但不省略。

## 输出格式

直接输出 Markdown，不要前言、不要 ```markdown``` 包裹、不要顶级 `#` 标题（程序会另行加上）。结构如下：

1. **导读**：1–2 段，使用 token 介绍当天亮点（包括闲聊花絮）。不要写 `[TOC]`。
2. **二级章节**：从下面四类中按当天内容选用，没有的就不写，顺序按重要性自定：
   - `## 行业新闻`
   - `## 工具`
   - `## 方法论`
   - `## 闲聊花絮`
3. **三级子话题**：每个二级章节下若干 `### 子话题`，结构如下：
   - 第一行：`### 标题`
   - 简介：1–3 句话概括要点（段落或要点列表均可）
   - 引用：可选，按需采用下面两种形式之一或组合，每条之间留空行

### 引用形式

引用有两种合法形式，自由组合，按"哪种更合适"选用：

- **行内摘抄**：把关键短语嵌进简介句子里，**用「」包裹原话或近似原话的短语**，前面带上 token。适合保留有特色的措辞、比喻、判断，而完整句子价值不大的场景。长度建议一句以内（超出改用 blockquote）。例：
  - `沉稳的大象 评价 DeepSeek V4 是「博览众家、超人执行力调出来的大杂烩」` —— 保留比喻
  - `活泼的企鹅 把对抗式 review 形容为「写完雇个杠精先喷一轮」` —— 保留梗
  - `沉稳的大象 把两年 1000 倍拆成「10 × 10 × 10」` —— 保留具体表述
  - `活泼的企鹅 觉得 AI 写的文章「废话太多」，扫一眼就过` —— 短判断嵌入叙述
- **Blockquote** `> token：原话或近似引用`：适合金句、有语气/梗、多人对答、需要原汁原味的发言。

数量按需把握，原则是**每条引用都要承担新信息**：

- 通常 0–4 条 blockquote；高密度多人讨论可上探到 6 条；资讯转发/简短话题常常 0 条。
- 硬天花板 8 条 blockquote，避免极端情况整段倒灌聊天记录。
- 行内摘抄不计入上述数量，但同样遵守"承担新信息"原则。

**反冗余规则（重要）**：简介负责"是什么"，引用负责"怎么说的"——blockquote 不应与简介内容重复。如果一句话既写在简介又单独 blockquote，二选一：要么揉进简介当行内摘抄，要么删掉简介里的复述、让 blockquote 独立成段。
4. **tags 行**：全文最末，先一行 `---` 分隔，再一行 `tags: 标签1, 标签2, 标签3`。标签英文小写、连字符（如 `model-release`、`long-context`）。

## 关于「章节不公开」标记

每个 `### 子话题`写完正文后，**重新审视**该章节是否适合公开发布。**默认放出**；只在以下三类之一时才标记不公开：
1. **隐私顾虑**：内容涉及可与群外信息交叉识别的私人线索（职业、地点、独特经历），即便已匿名化也可能推断出具体个人。
2. **Opt-out 波及**：核心内容依赖被隐藏的发言，剩余上下文仍可能让人推知被隐藏内容。
3. **公众环境风险**：在公开互联网语境下可能引起误解、争议、对当事人或相关方产生负面影响。

需要不公开时，在该 `### 子话题`末尾**单独一行**写：

```
[章节不公开：简短原因]
```

格式必须严格：方括号、`章节不公开`四字、中文或英文冒号、原因不含 `]`、整行单独一行。拿不准时标记不公开。

## 简短示例

```
今天 沉稳的大象 分享了 Claude Opus 4.7 的发布要点，活泼的企鹅 围绕长上下文写了一篇评测。

## 行业新闻

### Claude Opus 4.7 发布
新版本主推工具调用稳定性与长上下文表现，价格不变。沉稳的大象 实测 200K 召回「明显比 4.6 稳」。

### 某客户案例
（正文…）

[章节不公开：涉及未签约客户的敏感信息]

## 方法论

### 用 sub-agent 做并行搜索的小技巧
活泼的企鹅 把 search 与 write 拆到两个 agent 后明显更快，并补充了一些踩坑经验。

> 活泼的企鹅：关键是 search agent 不要让它直接写文件，否则上下文会被搜索结果污染，写出来的东西总是跑题

> 活泼的企鹅：reviewer 单独开一个 agent，不共享 context window，往往能挑出主 agent 自己看不出的毛病

---

tags: model-release, long-context, agent
```
"""

# ── Streaming extraction ─────────────────────────────────────────────────────────


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
    thinking_cb=None,
    header_cb=None,
    attempt_cb=None,
) -> DailyReport:
    """Stream a markdown daily report from Claude; return DailyReport(date, markdown).

    *client* may be injected for testing (any object with ``messages.stream``
    matching the Anthropic SDK surface). If None, a default client is built
    from *api_key*.

    *roster_text* is the rendered 群友花名册 (see ``wechat_daily.roster``);
    when provided it's prepended to the user message so the model can resolve
    informal references back to tokens.

    *thinking_cb(received_bytes, attempt)* is invoked as adaptive-thinking
    content streams in (separate from the visible text body).

    *header_cb(kind, level, title, attempt)* fires when a structural header
    is detected in the stream: ``kind="thinking"`` for ``**bold**``-wrapped
    lines in summarized thinking (level=0); ``kind="body"`` for ``##`` /
    ``###`` markdown lines in the visible body (level 0 for ``##``, 1 for
    ``###``).

    *attempt_cb(attempt)* fires when a retry begins (attempt >= 2), so
    front-ends can render a separator in their log.
    """
    import anthropic  # for APIStatusError below

    if client is None:
        client = _default_client(api_key)

    chat_block = f"以下是 {date_str} 的匿名化群聊记录，请生成日报：\n\n{tokenized_chat}"
    user_content = f"{roster_text}\n\n---\n\n{chat_block}" if roster_text else chat_block

    max_retries = 3
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        if attempt > 1 and attempt_cb:
            attempt_cb(attempt)
        try:
            buffer_parts: list[str] = []
            thinking_parts: list[str] = []
            received = 0
            thinking_received = 0
            text_line_buf = ""
            thinking_line_buf = ""

            def _flush_lines(buf: str, kind: str) -> str:
                """Emit headers for any complete lines in *buf*; return remainder."""
                if "\n" not in buf or not header_cb:
                    return buf
                *complete, remainder = buf.split("\n")
                for line in complete:
                    if kind == "thinking":
                        m = _THINKING_HEADER_RE.match(line)
                        if m:
                            header_cb("thinking", 0, m.group(1).strip(), attempt)
                    else:
                        m = _BODY_HEADER_RE.match(line)
                        if m:
                            level = len(m.group(1)) - 2  # ## → 0, ### → 1
                            header_cb("body", level, m.group(2).strip(), attempt)
                return remainder

            with client.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=128000,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            ) as stream:
                for event in stream:
                    etype = getattr(event, "type", None)

                    # Visible text body — SDK helper TextEvent (has .text + type=="text").
                    delta = getattr(event, "text", None)
                    if isinstance(delta, str) and etype == "text":
                        buffer_parts.append(delta)
                        received += len(delta)
                        text_line_buf = _flush_lines(text_line_buf + delta, "body")
                        if progress_cb:
                            progress_cb(received, attempt)
                        continue

                    # Adaptive-thinking deltas come through as raw
                    # content_block_delta events with delta.type == "thinking_delta".
                    if etype == "content_block_delta":
                        d = getattr(event, "delta", None)
                        if d is not None and getattr(d, "type", None) == "thinking_delta":
                            t = getattr(d, "thinking", "")
                            if isinstance(t, str) and t:
                                thinking_parts.append(t)
                                thinking_received += len(t)
                                thinking_line_buf = _flush_lines(
                                    thinking_line_buf + t, "thinking",
                                )
                                if thinking_cb:
                                    thinking_cb(thinking_received, attempt)
                response = stream.get_final_message()

            markdown = "".join(buffer_parts)
            thinking_text = "".join(thinking_parts)

            if response.stop_reason == "refusal":
                _save_failure(date_str, user_content, markdown, "Claude 拒绝处理该内容")
                raise ExtractionError("Claude 拒绝处理该内容（stop_reason=refusal）")

            if response.stop_reason == "max_tokens":
                _save_failure(date_str, user_content, markdown, "响应被 max_tokens 截断")
                raise ExtractionError("响应被 max_tokens 截断，请增大 max_tokens 后重试")

            # Fallback: if the streamed buffer is empty but the final response
            # contains text blocks, harvest them. This shouldn't happen in
            # practice but guards against SDK event-shape changes.
            if not markdown:
                markdown = "".join(
                    getattr(b, "text", "") for b in (response.content or [])
                    if getattr(b, "type", None) == "text"
                )

            if not markdown.strip():
                _save_failure(date_str, user_content, markdown, "响应为空")
                raise ExtractionError("响应为空")

            _save_extract(date_str, markdown, user_content, thinking_text)
            return DailyReport(date=date_str, markdown=markdown)

        except ExtractionError:
            raise
        except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError) as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(5 * attempt)
        except anthropic.APIStatusError as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(30)

    _save_failure(date_str, user_content, None, str(last_exc))
    raise last_exc  # type: ignore[misc]


def _save_extract(
    date_str: str,
    markdown: str,
    user_content: str,
    thinking_text: str = "",
) -> None:
    """Save successful extraction to debug/."""
    DEBUG_DIR.mkdir(exist_ok=True, parents=True)
    (DEBUG_DIR / f"extract-{date_str}.md").write_text(markdown, encoding="utf-8")
    # Sidecar: full LLM input (roster + tokenized chat) for post-mortem audit.
    (DEBUG_DIR / f"extract-{date_str}.input.txt").write_text(
        user_content[:50000], encoding="utf-8",
    )
    if thinking_text:
        (DEBUG_DIR / f"extract-{date_str}.thinking.md").write_text(
            thinking_text, encoding="utf-8",
        )


def _save_failure(
    date_str: str,
    user_content: str,
    partial_markdown: str | None,
    reason: str,
) -> None:
    """Persist failure details to debug/ for post-mortem inspection."""
    import json
    DEBUG_DIR.mkdir(exist_ok=True, parents=True)
    path = DEBUG_DIR / f"extract-{date_str}.FAILED.json"
    payload = {
        "reason": reason,
        "partial_markdown": partial_markdown,
        "input_preview": user_content[:3000],
    }
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
