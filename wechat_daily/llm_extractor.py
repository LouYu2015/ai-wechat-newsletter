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

# Body markdown ## / ### header line.
_BODY_HEADER_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")

from .config import CLAUDE_MODEL, GEMINI_SUMMARY_MODEL, DEBUG_DIR
from .models import DailyReport

# ── System prompt ────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
你是一个专门分析 AI 技术讨论群聊天记录的助手。基于经过匿名化处理的群聊记录，输出当天的 Markdown 日报。

聊天记录、花名册、处理要点、写作风格、导读要求都在用户消息里。本系统提示只规定输出格式。

## 输出格式

直接输出 Markdown，不要前言、不要 ```markdown``` 包裹、不要顶级 `#` 标题（程序会另行加上）。结构如下：

1. **导读**：1–2 段。具体写法见用户消息末尾的「导读要求」。不要写 `[TOC]`。
2. **二级章节**：默认从下面四类中按当天内容选用，没有的就不写，顺序按重要性自定：
   - `## 行业新闻`（模型发布、调价、融资、政策、产品动态等外部消息）
   - `## 工具`（具体软件 / SDK / 插件 / 脚本的使用与对比）
   - `## 方法论`（实战经验、踩坑、prompt 与 agent 设计、工作流）
   - `## 闲聊花絮`（梗、金句、有趣插曲；**始终用此固定名**，方便老读者定位）

   **允许抽专题**：若某单一话题当天占讨论量 1/3 以上且产生 3+ 个相关子话题，可以将其单独抽为 `##` 专题章节，或把对应大类改名为更具体的标题。例：
   - `## Claude Opus 4.7 发布`（吸收当天围绕该发布的所有讨论）
   - `## RAG 与长上下文之争`（一场跨多条消息的方法论辩论）

   **专题命名硬约束**：
   - 4–10 字短名词短语；可含版本号、产品名、争议焦点
   - **禁止** `关于…的讨论`、`今日 X 相关`、`X 的那些事` 这类描述句式
   - `## 闲聊花絮` 不抽专题、不改名（即便当天笑点集中在某一件事上）

   **何时不抽专题**（反例）：几个话题只是"都跟模型有关""都涉及 agent"但来源不同、讨论不连续，仍归入 `## 行业新闻` / `## 方法论`，不要凑出 `## 模型动态` `## Agent 漫谈` 这类聚合名。

   **挑话题时值得留意的角度**（仅作扫描提示，不必每条都成章）：新模型 / 工具发布与定价；实战经验、踩坑、配置技巧；论文与长文讨论；行业动态（融资、调价、政策、人事）；群友自荐的作品、demo、开源项目；具体数据点（benchmark、用户量、收入）；金句、梗与有趣插曲。
3. **三级子话题**：每个二级章节下若干 `### 子话题`，结构如下：
   - 第一行：`### 标题`
   - 简介：用一个或少量段落概括要点
   - 引用：可选，按需采用下面两种形式之一或组合，每条之间留空行

   **数量与长度**：章节数量没有上限，按当天内容自然分章。**宁可多个独立短章节，也不要为节省篇幅把不同话题强行揉到一节里**。一个三五句话、零 blockquote 的子话题完全合法；只要它是一个独立话题，就单独成节。合并的唯一理由是两段讨论本身是连续的（同一组人接着聊同一件事），而不是"看起来类似"或"都很短"。
4. **tags 行**：全文最末，先一行 `---` 分隔，再一行 `tags: 标签1, 标签2, 标签3`。标签英文小写、连字符（如 `model-release`、`long-context`）。

### 引用形式

引用有两种合法形式，自由组合，按"哪种更合适"选用：

- **行内摘抄**：把关键短语嵌进简介句子里，**用「」包裹原话**。适合保留有特色的措辞、比喻、判断，而完整句子价值不大的场景。长度建议一句以内（超出改用 blockquote）。例：
  - `沉稳的大象 评价 DeepSeek V4 是「博览众家、超人执行力调出来的大杂烩」` —— 保留比喻
  - `活泼的企鹅 把对抗式 review 形容为「写完雇个杠精先喷一轮」` —— 保留梗
  - `沉稳的大象 把两年 1000 倍拆成「10 × 10 × 10」` —— 保留具体表述
  - `活泼的企鹅 觉得 AI 写的文章「废话太多」，扫一眼就过` —— 短判断嵌入叙述
- **Blockquote** `> token：原话或原话的一部分`：适合金句、多人对答、有深度的长回复，需要原汁原味的发言。

数量按需把握，原则是**每条引用都要承担新信息**：

- 通常 0–4 条 blockquote；高密度多人讨论可上探到 6 条；资讯转发/简短话题常常 0 条。
- 硬天花板 8 条 blockquote，避免极端情况整段倒灌聊天记录。
- 行内摘抄不计入上述数量，但同样遵守"承担新信息"原则。

**反冗余规则**：简介负责"是什么"，引用负责"怎么说的"——blockquote 不应与简介内容重复。如果一句话既写在简介又单独 blockquote，二选一：要么揉进简介当行内摘抄，要么删掉简介里的复述、让 blockquote 独立成段。

## 关于跨日章节引用（特殊占位符）

用户消息里可能出现两种历史日报上下文块（顺序：先老后新）：

- `<previous_report_titles>`：更早几天（通常 4–7 天前）每篇日报的 `##` / `###` **标题大纲**，没有正文。仅用于去重判断和 `[[ref:]]` 引用目标，**不要尝试推测或复述这些章节的正文**。
- `<previous_reports>`：最近 1–3 天的完整 Markdown 正文。既是去重 / 续写素材，也是 `[[ref:]]` 引用目标。

两种块里出现过的 `### 标题`都可作为跨日引用目标。引用语法：

    [[ref:YYYY-MM-DD|章节标题]]

例：``关于这点，昨天的 [[ref:2026-05-09|Claude Opus 4.7 发布]] 已经写过要点``。

格式要求：
- `章节标题` 原样照抄过去日报里的 `### 标题` 文本（不带 `###`、不要改写、不要翻译）。
- `|` 是 ASCII 竖线，不要写成 `丨`、全角或别的符号。`YYYY-MM-DD` 是 ASCII 数字与连字符。
- **不要在占位符里塞 URL**——程序会自动渲染为公开版的可点链接，PDF 版只显示「章节标题」。
- 必须在占位符外用自然语言指明日期（"昨天 / 前天 / 5 月 3 日 / 上周三 / 上周 X"等），方便 PDF 读者定位旧期。
- 如果引用的是过去**多个**章节，每个都用单独的 `[[ref:…]]`，不要合并。

## 关于「章节不公开」标记

每个 `### 子话题`写完正文后，**重新审视**该章节是否适合公开发布，还是只能在群内发布。**默认公开发布**；只在以下三类之一时才标记不公开：
1. **隐私顾虑**（仅以下情形）：内容包含未脱敏的真名/手机号/住址/「雇主+职位」等可直接定位到具体真人的信息；或当事人在群内明确表示过不希望某事被传播。**仅凭"链接里可能推断出发言人身份"或"职业+地点的常见组合"不构成隐藏理由**——分享者主动在 ~500 人群里发的内容，已默认接受这种程度的可识别性；真正在意隐私的群友会自行使用 `/optout`。
2. **Opt-out 波及**：核心内容依赖被隐藏的发言，剩余上下文仍可能让人推知被隐藏内容。
3. **公众环境风险**：在公开互联网语境下可能引起误解、争议、对当事人或相关方产生负面影响。

需要不公开时，在该 `### 子话题`末尾**单独一行**写：

```
[章节不公开：简短原因]
```

格式必须严格：方括号、`章节不公开`四字、中文或英文冒号、原因不含 `]`、整行单独一行。**拿不准时，倾向于公开**——隐藏的成本（读者看不到内容）通常高于"理论上可能被识别"的隐私成本。

## 简短示例

```
今天 沉稳的大象 分享了 Claude Opus 4.7 的发布要点，活泼的企鹅 围绕长上下文写了一篇评测。

## 行业新闻

### Claude Opus 4.7 发布
新版本主推工具调用稳定性与长上下文表现，价格不变。沉稳的大象 实测 200K 召回「明显比 4.6 稳」。

### 某客户案例
（正文…）

[章节不公开：涉及保密客户的敏感信息]

### Anthropic 调价

API 价格表小幅调整，Sonnet 输入降 10%，输出不变。讨论很短，没人特别意外。

## 方法论

### 用 sub-agent 做并行搜索的小技巧
活泼的企鹅 把 search 与 write 拆到两个 agent 后明显更快，并补充了一些踩坑经验。

> 活泼的企鹅：关键是 search agent 不要让它直接写文件，否则上下文会被搜索结果污染，写出来的东西总是跑题

> 活泼的企鹅：reviewer 单独开一个 agent，不共享 context window，往往能挑出主 agent 自己看不出的毛病

---

tags: model-release, long-context, agent
```
"""

# ── User-message instructions (placed AFTER the long chat input) ────────────────

_USER_INSTRUCTIONS = """\
# 处理要点（写日报前请确认遵守）

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

`<group_roster>` 标签内列出每个 token 对应的真实昵称与已知群昵称变体。聊天记录里可能出现**未列入花名册**但明显指代某位群友的代称（外号、谐音、缩写），请基于上下文与花名册推断对应 token。拿不准时使用「某群友」，**绝不要保留任何真实昵称或代称**。

## 关于隐私占位符

部分群友已申请隐私保护，其发言以 `[此消息已隐藏]` 或 `[HH:MM–HH:MM] [某群友连续发言 N 条已隐藏]` 标记。处理这些标记时：
1. 不要试图推测或还原被隐藏的内容。
2. 若某段讨论的关键输入来自被隐藏消息，用「有群友提出了一个观点，引发了讨论」这类模糊表述。
3. 若某条回复明显在回应被隐藏的消息，保留回复，但不推断被回应内容。
4. 闲聊花絮：若互动核心来自被隐藏消息，整条跳过。

## 关于链接

聊天记录里分享的网页/文章卡片会以 `[链接] [标题](URL)` 的 Markdown 形式出现。当某个 `### 子话题`涉及该资源时，**在简介或引用中以 `[标题](URL)` 形式给出超链接**，让读者能直接跳转到原文。URL **原样保留**，不要改写、缩短或删除查询参数（公众号链接删参数后会失效）。如果某条链接卡片只是被随手转发、并不构成话题，可以不引用。

部分链接下方会有 `[网页摘要] ...` 行，是程序预先抓取并摘要的网页正文，可作为理解话题、决定是否值得写、写成什么样的依据。**不要把网页摘要原文搬进日报**——它是参考资料，不是引用素材。

## 关于图片

聊天记录里的图片消息会先以 `[图片]` 占位符出现，**紧随其后会附上对应的图片**（同一条用户消息里）。请基于图片内容理解事件、产品、文章截图等；引用时用自然语言描述（"分享了某文章截图"、"贴了一张产品演示"），不要把整张图当成发言原话。图片中若包含真实人名、微信昵称、手机号、二维码、私人证件信息等隐私要素，请省略或泛化，遵循与文本同等的隐私规则。无法清晰识别的图片可以不写。

intro 中提到的话题，下方都要有对应章节；信息不足可简短描述，但不省略。

## 关于跨日延续与去重

用户消息里可能出现两种历史上下文块：

- `<previous_reports>`：最近 1–3 天的**完整**日报正文。
- `<previous_report_titles>`：更早几天（通常 4–7 天前）的日报**标题大纲**，只有 `##` / `###` 行，没有正文。

处理今天的内容时按下面规则：

1. **重叠时段去重**（仅对 `<previous_reports>` 适用）：今天聊天记录的开头和结尾各有约 1 小时与相邻天重叠。如果某条话题已在前一天日报中**完整**写过，今天直接跳过，不要重复报道——尤其是深夜（00:00 前后）冒出的资讯/链接。
2. **同话题、无新进展 → 不写**：今天群里只是简单回提前几天的话题（"对啊我也觉得"、"+1"、"昨天那个我看了"），不构成续写素材，跳过整个话题。对 `<previous_report_titles>` 里的旧话题同样适用——如果今天只是顺嘴一提某个 4–7 天前已经写过的事，跳过即可。
3. **同话题、有新进展 → 续写**：今天有新观点、新数据、新发言人加入，或者讨论走向了新分支。处理方式：
   - 章节标题可以与旧报**不同**（突出今日新角度），但在简介里用 [[ref:YYYY-MM-DD|原章节标题]] 引导读者先读旧章节。引用 `<previous_report_titles>` 里的标题同样合法。
   - 视情况用 1–2 句话回顾要点，让读者不必翻旧报也能理解今天。**只能基于今天的聊天记录回顾**——`<previous_report_titles>` 里没有正文，**不要凭标题揣测旧章节细节**；对前 3 天的内容也**不要逐句复述**。
   - 然后续写今天的新进展。
4. **跨日金句不再重引**：前 3 天已 blockquote 的发言，今天即使再次被提起也不要再 blockquote 同一句；可改为行内摘抄 + 「之前那句被 [[ref:…|XX]] 引过」。（对 `<previous_report_titles>` 里的天数无此约束——只有标题，无从判断是否引过哪一句。）
5. **日期措辞**：占位符外要用自然中文描述日期（"昨天"、"前天"、"5 月 3 日"、"上周三 / 上周五"），PDF 读者据此定位旧期。
6. **两个块都没有时**：忽略本节，按当日内容独立成报。

---

# 写作风格

你的读者有两类：

1. **群友本人**——他们就是被引用的人，会回看自己说过什么，其他人有什么回应，想看到因为消息太多而错过的信息。
2. **群外类似背景的中文程序员/AI 实战派**——日常写代码、用 agent、关心模型迭代，订阅科技博主的文章和新闻，对其他人的技术分享十分好奇。

他们已知、不需要科普的概念：LLM、agent、RAG、context window、prompt cache、tool use、subagent、harness、skills、evals、MCP、Claude Code / Codex / Cursor 等常见工具与模型版本号。

他们想看：
- 具体版本号、参数、配额、价格变化、踩坑细节、经验分享；
- 别人的判断与立场——不是平等罗列，而是有深度、有价值；
- 群里冒出的金句、梗，那些虽然没有技术性但是有趣的闲聊；
- 值得点开阅读的链接，并说清为什么值得。

**关于篇幅**：全文长度由当天内容自然决定，没有上限、没有目标字数、没有章节数上限。**不要因为感觉"太长了""章节太多了"而跳过话题、压缩深度、合并不同话题或砍掉有意思的引用**。读者的预期是"不漏内容"，不是"读得快"——宁可长一点也不要丢失信息密度。一个有 15 个独立子话题的日报，只要每节都承担新信息，就是合格的。

---

# 导读要求

导读必须**给出至少一个编辑判断**——什么内容最值得关注、为什么。可用「最值得读的是……」「真正能落地的是……」这类语气。

下面三种开头可作参考（不强求选一种，也可自创）：
- **结论先行**：「今天最值得关注的是……」
- **主题串联**：把当天 2–3 条主线串起来，再带上有趣的闲聊。
- **反差悬念**：「今天群里炸出了某事」「今天有人贴了 X，没想到引出了关于 Y 的讨论」。

---

请基于上面 {date_str} 的群聊与花名册生成今天的日报。
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
    text_cb=None,
    usage_cb=None,
    chat_blocks: list[dict] | None = None,
    prior_reports: list[tuple[str, str]] | None = None,
    prior_report_titles: list[tuple[str, str]] | None = None,
    model: str | None = None,
    debug_suffix: str = "",
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

    *header_cb(kind, level, title, attempt)* fires when a ``##`` / ``###``
    markdown header is detected in the visible body (kind always ``"body"``;
    level 0 for ``##``, 1 for ``###``).

    *text_cb(kind, delta, attempt)* fires for every streamed delta with the
    raw text — ``kind="thinking"`` for adaptive-thinking deltas,
    ``kind="body"`` for visible body text.

    *attempt_cb(attempt)* fires when a retry begins (attempt >= 2), so
    front-ends can render a separator in their log.

    *usage_cb(usage, input_chars)* fires once on successful completion with
    the response's ``usage`` object (Anthropic SDK shape — has
    ``input_tokens``, ``output_tokens``, etc.) and the prompt's character
    count. Used by the CLI to log token usage and estimate cost without this
    module having to know about :mod:`wechat_daily.cost_tracker`.

    *model* overrides :data:`CLAUDE_MODEL` for this call — used by the
    compare-mode pipeline to drive both Opus 4.6 and 4.7 through the same
    extractor. *debug_suffix* (e.g. ``".opus-4-7"``) is interpolated into
    the ``debug/extract-{date}{suffix}.md`` / ``.input.txt`` / ``.thinking.md``
    sidecar filenames so the secondary run doesn't clobber the canonical
    run's files. Default ``""`` preserves the historical filenames that
    :func:`prior_report.load_prior_reports` reads from.
    """
    if model is None:
        model = CLAUDE_MODEL
    import anthropic  # for APIStatusError below

    if client is None:
        client = _default_client(api_key)

    # Long input first (Anthropic best practice for multi-doc prompts):
    # roster → previous_report_titles → previous_reports → chat_log are
    # wrapped in XML tags at the top of the user message; processing rules /
    # audience profile / 导读 requirements follow the chat log so the model
    # reads them with the data fresh.
    #
    # Title-only block goes before full-body block: it covers the older days
    # (e.g. 4–7 back), so the model reads "old → new" as it scans down.
    from .prior_report import (
        format_prior_report_titles_block,
        format_prior_reports_block,
    )

    parts: list[str] = []
    if roster_text:
        parts.append(f"<group_roster>\n{roster_text}\n</group_roster>\n\n")
    if prior_report_titles:
        parts.append(format_prior_report_titles_block(prior_report_titles) + "\n")
    if prior_reports:
        parts.append(format_prior_reports_block(prior_reports) + "\n")
    parts.append(f'<chat_log date="{date_str}">\n')
    prefix = "".join(parts)
    suffix = (
        f"\n</chat_log>\n\n---\n\n"
        + _USER_INSTRUCTIONS.format(date_str=date_str)
    )

    user_content: str | list[dict]
    if chat_blocks is not None:
        user_content = [
            {"type": "text", "text": prefix},
            *chat_blocks,
            {"type": "text", "text": suffix},
        ]
        # Flat string only used for debug sidecar dump.
        debug_text = (
            prefix
            + "".join(
                b["text"] for b in chat_blocks if b.get("type") == "text"
            )
            + suffix
        )
    else:
        user_content = prefix + tokenized_chat + suffix
        debug_text = user_content

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

            def _flush_body_headers(buf: str) -> str:
                """Emit body headers for any complete lines in *buf*; return remainder."""
                if "\n" not in buf or not header_cb:
                    return buf
                *complete, remainder = buf.split("\n")
                for line in complete:
                    m = _BODY_HEADER_RE.match(line)
                    if m:
                        level = len(m.group(1)) - 2  # ## → 0, ### → 1
                        header_cb("body", level, m.group(2).strip(), attempt)
                return remainder

            with client.messages.stream(
                model=model,
                max_tokens=128000,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
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
                        text_line_buf = _flush_body_headers(text_line_buf + delta)
                        if text_cb:
                            text_cb("body", delta, attempt)
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
                                if text_cb:
                                    text_cb("thinking", t, attempt)
                                if thinking_cb:
                                    thinking_cb(thinking_received, attempt)
                response = stream.get_final_message()

            markdown = "".join(buffer_parts)
            thinking_text = "".join(thinking_parts)

            if response.stop_reason == "refusal":
                _save_failure(date_str, debug_text, markdown, "Claude 拒绝处理该内容", debug_suffix)
                raise ExtractionError("Claude 拒绝处理该内容（stop_reason=refusal）")

            if response.stop_reason == "max_tokens":
                _save_failure(date_str, debug_text, markdown, "响应被 max_tokens 截断", debug_suffix)
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
                _save_failure(date_str, debug_text, markdown, "响应为空", debug_suffix)
                raise ExtractionError("响应为空")

            _save_extract(date_str, markdown, debug_text, thinking_text, debug_suffix)
            if usage_cb:
                usage_cb(getattr(response, "usage", None), len(debug_text))
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

    _save_failure(date_str, debug_text, None, str(last_exc), debug_suffix)
    raise last_exc  # type: ignore[misc]


def _save_extract(
    date_str: str,
    markdown: str,
    user_content: str,
    thinking_text: str = "",
    debug_suffix: str = "",
) -> None:
    """Save successful extraction to debug/.

    *debug_suffix* (e.g. ``".opus-4-7"``) is appended after the date so a
    compare-mode run can write alongside the canonical run without
    clobbering the files that ``prior_report`` reads next day.
    """
    DEBUG_DIR.mkdir(exist_ok=True, parents=True)
    (DEBUG_DIR / f"extract-{date_str}{debug_suffix}.md").write_text(markdown, encoding="utf-8")
    # Sidecar: full LLM input (roster + tokenized chat) for post-mortem audit.
    (DEBUG_DIR / f"extract-{date_str}{debug_suffix}.input.txt").write_text(
        user_content[:50000], encoding="utf-8",
    )
    if thinking_text:
        (DEBUG_DIR / f"extract-{date_str}{debug_suffix}.thinking.md").write_text(
            thinking_text, encoding="utf-8",
        )


def _save_failure(
    date_str: str,
    user_content: str,
    partial_markdown: str | None,
    reason: str,
    debug_suffix: str = "",
) -> None:
    """Persist failure details to debug/ for post-mortem inspection."""
    import json
    DEBUG_DIR.mkdir(exist_ok=True, parents=True)
    path = DEBUG_DIR / f"extract-{date_str}{debug_suffix}.FAILED.json"
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
