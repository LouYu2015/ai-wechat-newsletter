"""DeepSeek-only prompts for the AB-test compare report (two-step pipeline).

The canonical Opus path (``llm_extractor._SYSTEM_PROMPT`` / ``_USER_INSTRUCTIONS``)
is deliberately NOT reused wholesale here. DeepSeek V4 Pro is a large, lightly
aligned reasoning model, and community + official guidance for the DeepSeek
reasoning family differs from Claude in ways that matter for this task:

- **Few-shot hurts.** Reasoning models tend to mimic the shape of an in-prompt
  example instead of reasoning from the data, so Opus's 「简短示例」 block is
  dropped entirely from the DeepSeek prompt.
- **Instructions in the user turn, key rules last.** Heavy system prompts
  slightly depress reasoning; the highest-value rules go at the very end of the
  user message (recency) as a checklist.
- **It under-plans.** Left to itself DeepSeek emitted ~660 chars of chain of
  thought on this task (vs Opus's ~33k): it restated the rules and started
  writing, so it dropped whole topics and let the intro promise sections the
  body never delivered. The fix is *not* "think harder" (counter-productive for
  reasoning models) but forcing a concrete intermediate artifact — a topic
  inventory + section tree produced in a dedicated first pass, which the second
  pass writes against.
- **It fabricates specifics.** DeepSeek invents plausible numbers (scores,
  counts, ranks), so both passes carry a hard no-fabrication rule.

Two passes (see ``llm_extractor.extract_report_deepseek``):
  1. OUTLINE — enumerate every candidate topic with attribution, dedup vs prior
     reports, decide keep / merge / drop, and lay out the ``##`` / ``###``
     section tree.
  2. WRITE — write the full report against that outline, so coverage and
     intro↔body consistency hold.

The processing rules that must stay in lock-step with the Opus version
(anonymization, ``token⟨原文⟩`` disambiguation, links, images, cross-day dedup,
audience, 导读) are *imported* from ``llm_extractor`` rather than copied, so the
two report versions never drift on those. Only the output-structure spec is
re-authored (to drop the few-shot) plus the DeepSeek-specific additions.
"""

from __future__ import annotations

from .llm_extractor import _USER_INSTRUCTIONS

# Lightweight system prompts — reasoning models prefer the heavy lifting in the
# user turn. Kept to a single role sentence each.
OUTLINE_SYSTEM = (
    "你是中文 AI 技术讨论群日报的资深主编，正在做今天的选题与结构规划。"
    "严格按用户消息的要求输出，只产出大纲，不要写正文。"
)
WRITE_SYSTEM = (
    "你是中文 AI 技术讨论群日报的资深主编兼写手。"
    "严格按用户消息里的格式规范，直接输出 Markdown 正文。"
)

# ── Output-structure spec (DeepSeek variant of Opus's system prompt) ───────────
# Same substance as llm_extractor._SYSTEM_PROMPT MINUS the 「简短示例」 few-shot
# block. Used only in the WRITE pass.
_FORMAT_SPEC = """\
# 输出格式规范

直接输出 Markdown，不要前言、不要 ```markdown``` 包裹、不要顶级 `#` 标题（程序会另行加上）。结构如下：

1. **导读**：1–2 段。具体写法见下方「导读要求」。不要写 `[TOC]`。
2. **二级章节**：从下面四类中按当天内容选用，没有的就不写，顺序按重要性自定：
   - `## 行业新闻`（模型发布、调价、融资、政策、产品动态等外部消息）
   - `## 工具`（具体软件 / SDK / 插件 / 脚本的使用与对比）
   - `## 方法论`（实战经验、踩坑、prompt 与 agent 设计、工作流）
   - `## 闲聊花絮`（梗、金句、有趣插曲；**始终用此固定名**，方便老读者定位）

   **允许抽专题**：若某单一话题当天占讨论量 1/3 以上且产生 3+ 个相关子话题，可单独抽为 `##` 专题章节，或把对应大类改名为更具体的标题。
   **专题命名硬约束**：4–10 字短名词短语；可含版本号、产品名、争议焦点；**禁止** `关于…的讨论`、`今日 X 相关`、`X 的那些事` 这类描述句式；`## 闲聊花絮` 不抽专题、不改名。
   **何时不抽专题**：几个话题只是"都跟模型有关""都涉及 agent"但来源不同、讨论不连续，仍归入 `## 行业新闻` / `## 方法论`，不要凑 `## 模型动态` 这类聚合名。

3. **三级子话题**：每个二级章节下若干 `### 子话题`：第一行 `### 标题`，随后用一个或少量段落概括要点，再按需附引用。
   **数量与长度**：章节数量没有上限，按当天内容自然分章。**宁可多个独立短章节，也不要为省篇幅把不同话题揉到一节里**。一个三五句话、零 blockquote 的子话题完全合法。合并的唯一理由是两段讨论本身连续（同一组人接着聊同一件事），而不是"看起来类似"或"都很短"。
4. **tags 行**：全文最末，先一行 `---`，再一行 `tags: 标签1, 标签2`（英文小写、连字符，如 `model-release`）。

## 引用形式

两种合法形式，按"哪种更合适"选用：
- **行内摘抄**：把关键短语嵌进句子里，**用「」包裹原话**，保留有特色的措辞/比喻/判断。一句以内。
- **Blockquote** `> token：原话`：适合金句、多人对答、有深度的长回复。

数量原则——**每条引用都要承担新信息**：通常 0–4 条 blockquote，高密度讨论上探 6 条，硬天花板 8 条；资讯转发常 0 条。**反冗余**：简介负责"是什么"，引用负责"怎么说的"，blockquote 不应与简介重复。

## 跨日章节引用（占位符）

历史上下文块里出现过的 `### 标题` 可作跨日引用目标，语法：

    [[ref:YYYY-MM-DD|章节标题]]

- `章节标题` 原样照抄旧日报的 `### 标题`（不带 `###`、不改写）。`|` 是 ASCII 竖线，`YYYY-MM-DD` 是 ASCII。
- 不要在占位符里塞 URL。必须在占位符外用自然语言指明日期（"昨天 / 前天 / 上周三"）。
- 引用多个旧章节时每个单独写一个 `[[ref:…]]`。

## 「章节不公开」标记

每个 `### 子话题`写完后重新审视是否适合公开。**默认公开**；只在以下之一时在该子话题末尾**单独一行**写 `[章节不公开：简短原因]`：
1. 隐私顾虑（含未脱敏真名/手机号/住址/「雇主+职位」等可直接定位真人的信息，或当事人明确表示不愿被传播）；
2. Opt-out 波及；
3. 公开互联网语境下可能引起误解、争议或对当事人产生负面影响。
格式严格：方括号、`章节不公开`四字、中英文冒号、原因不含 `]`、整行单独成行。**拿不准时倾向公开**。
"""

# ── DeepSeek-specific additions ────────────────────────────────────────────────

_ANTI_FAB = """\
# 关于事实与数字（务必严格遵守）

- 所有具体数字、比分、排名、段位、版本号、价格、用户量、时长、占比，都必须能在聊天记录或 `[网页摘要]` 里找到出处。
- 找不到出处就改用模糊表述（"不少""数十万量级""大幅提升"），或干脆不写那个数字。
- **严禁补全任何看起来合理、但记录里并不存在的细节。** 宁可少写一个数字，也绝不编造一个数字。
- 同一事实在记录里有冲突的数值时，二选一并以聊天原文为准，不要折中编一个新数。
"""

_OUTLINE_TASK = """\
# 你的任务：只产出今天的【选题大纲】，不要写正文

你现在是主编，这一步只做**选题、归类、谋篇**，不写正文。分三部分输出（纯文本，不要 ``` 包裹整体）：

## A. 话题清单

把今天聊天记录里**所有**值得考虑的话题逐条列出，穷尽优先——宁可多列再标"丢弃"，也不要漏。链接卡片逐条都要过一遍。每条一行：

`- [类别] 标题候选 | 主要参与者(token) | 一句话内容 | 决定：保留 / 合并到「X」/ 丢弃 | 去重：全新 / 续写[[ref]] / 前几天已写跳过`

类别取自 行业新闻 / 工具 / 方法论 / 闲聊花絮（或合理的专题名）。

## B. 归类与专题体检

排章节树之前，先把"今天怎么分章"想清楚并写出来（这部分是你的推理草稿，要落纸，不要省）：

1. **聚类**：把 A 里"保留 / 合并"的话题，按"是否围绕同一主题、同一拨人是否在接着聊"聚成几组，列出每组含哪几条。
2. **逐组定性**：对每一组，粗估它占当天讨论量的比重、数一下能拆出几个子话题，然后明确写出归宿——
   `第X组（话题…）→ 归入「某大类」 / 抽成专题「名」，因为 占比约__%、子话题__个`。
   判据：**某一组占比约 1/3 以上、且能拆出 3 个以上子话题，就抽成 `##` 专题**（4–10 字名词短语）；不到这个量级的并回四大类。先做判断再决定，不要默认"一律不抽"。
3. **跨类自查**：确认**没有把同一主题拆进两个不同大类**（典型反例：教育讨论一半放方法论、一半放闲聊）。同一主题要么整组进一个大类，要么整组抽成一个专题。
4. **结论一行**：`分章结论：四大类 + <抽了专题「X」 / 今天无话题够格抽专题>`。

## C. 章节树（带行文指导）

按 B 的结论排出最终发布结构（顺序按重要性）。**每个 `### 子话题` 紧跟一行以 `↳` 开头的行文指导**，点出这一节**怎么写**——角度切入、详写什么略写什么、有没有值得单独成 blockquote 的金句。它是写正文时的依据，不是标题的一部分；只写编辑取舍，**不要把文风/长度/格式写进去**（那些另有全局规范），每条 ≤ 一行。

```
导读要点：<一句话点出今天最值得读的 1–2 条，给出编辑判断>
## 章节名
### 子话题标题
  ↳ 角度__；详写__、略写__；金句：<有「…」/无>
### 子话题标题
  ↳ ...
## 章节名
### 子话题标题
  ↳ ...
```

**二级章节默认就用四个标准大类**：`## 行业新闻`、`## 工具`、`## 方法论`、`## 闲聊花絮`（没有的就不写）；是否抽专题完全按 B 的判断走。`## 闲聊花絮` 永远用此固定名、放最后、不抽专题。

**硬约束**：导读要点里点到的每条，章节树里必须有对应的 `###`；章节树里的每个 `###` 都应是真实成立的话题，且都带一行 `↳` 行文指导。

只输出大纲（A + B + C 三部分），不要写任何正文段落。
"""

_WRITE_TASK_HEAD = """\
# 你的任务：按下面这份【选题大纲】写出今天的完整日报正文

下面是你上一步已经定好的大纲。**严格照其中的章节树（C 部分）写**（A 话题清单、B 归类体检是你的选题过程，已经定稿，照结论执行即可）：

- 章节树里的每个 `###` 都要写到，顺序一致；
- **每个 `###` 下那行 `↳` 行文指导是这一节的写作依据**：按它指示的角度、详略、金句取舍来写；但 `↳` 是给你的笔记，**不要照抄进正文**，也不要把它当成必须逐字命中的清单。
- 不要新增大纲里没有的话题，也不要遗漏任何一个；
- 导读就按"导读要点"展开，导读里点到的话题，正文必须有对应章节。

<outline>
{outline}
</outline>
"""

_FINAL_CHECKLIST = """\
---

# 交稿前逐条自检（不通过就改了再交，不要输出自检过程）

1. 导读里点到的每个话题，正文是否都有对应 `##` / `###` 章节？没有就补章节或从导读删掉。
2. 大纲章节树里的每个 `###` 是否都写到了？有没有擅自漏写或新增话题？
3. 每个具体数字 / 比分 / 段位 / 版本号是否都有出处？没有就模糊化或删掉。
4. 指代群友是否全程只用 token（「形容词的动物」），没有任何真实人名 / 英文名 / 昵称 / 外号？
5. 链接 URL 是否原样保留（未删查询参数）？tags 行是否在最末？

直接输出最终 Markdown 正文。
"""


def build_outline_instructions(date_str: str) -> str:
    """User-message instruction block for the OUTLINE pass (no report body).

    Reuses the shared processing rules (anonymization / links / images /
    cross-day dedup / audience) so the outline reasons under the same
    constraints as the Opus path, then overrides the closing task with the
    outline-only instruction at the very end (recency).
    """
    return "\n\n".join([
        _USER_INSTRUCTIONS.format(date_str=date_str),
        _ANTI_FAB,
        _OUTLINE_TASK,
    ])


def build_write_instructions(date_str: str, outline: str) -> str:
    """User-message instruction block for the WRITE pass.

    Order: output-format spec → shared processing/style/导读 rules →
    no-fabrication rule → the outline to write against → final self-check
    checklist (last = highest recency). The outline is spliced in with
    ``str.replace`` rather than ``str.format`` because model-generated outline
    text may contain literal braces.
    """
    head = _WRITE_TASK_HEAD.replace("{outline}", outline)
    return "\n\n".join([
        _FORMAT_SPEC,
        _USER_INSTRUCTIONS.format(date_str=date_str),
        _ANTI_FAB,
        head,
        _FINAL_CHECKLIST,
    ])
