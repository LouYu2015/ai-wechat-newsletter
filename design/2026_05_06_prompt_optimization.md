# 2026-05-06 提示词与流程优化建议

基于近 5 篇公开日报（2026-05-01 至 2026-05-05）、当前 `llm_extractor.py` 与 `url_enricher.py` 提示词、以及 Anthropic 官方 prompt 工程文档与中英文 AI 博客写作惯例的综合分析。

目标读者：对 AI 实战感兴趣的中文程序员（写代码 + 用 agent + 关心模型迭代）。

---

## 一、现状速判

### 已经做得好、建议保留

- 章节四分法（方法论 / 工具 / 行业新闻 / 闲聊花絮）清晰，与 The Pragmatic Engineer 的 "In this issue:" 逻辑契合。
- 双轨引用（行内摘抄 + blockquote）+ 反冗余规则，是 prompt 里最有效的结构。
- 链接以 `[标题](URL)` 嵌入正文，符合 Simon Willison 风格。
- 闲聊花絮的叙事最自然，少了"X 分享了"的句式包袱——说明模型在约束放松、目标具体时表现最好。

### 关键问题（按影响排序）

1. **导读句式高度模板化**：5 天 5 篇全是"X 分享了…，Y 讨论了…，Z 分享了…"。
2. **同一 token 短距离重复**：高产用户在单段内被点名 5+ 次，节奏拖沓。
3. **缺少"判断/立场"**：每条信息平等罗列，缺少 swyx / 宝玉 那种"我觉得这条最重要"的取舍。
4. **链接散落，没有汇总索引**：程序员读者期待末尾"延伸阅读"清单。
5. **跨日资讯重复**：5/4 与 5/5 都重复了 ChatGPT 14 亿 / 140 亿 那组数字。
6. **Prompt 结构未按 Anthropic 推荐顺序**：长输入应放最前，约束应靠后。

---

## 二、Prompt 层建议（`wechat_daily/llm_extractor.py:26-153`）

### P0-1 | 重排结构：长输入放最前

Anthropic 反复强调 *"long documents above instructions"*。当前结构是"系统 prompt（150 行约束）→ 花名册 → 聊天记录"。建议系统 prompt 里只放角色、受众、风格、输出格式；约束放最末，或挪到 user message 末尾。当前 system prompt 太重，4.7 容易过度思考。

### P0-2 | 加入受众画像 + 声音锚点

当前 prompt 只说"分析 AI 技术讨论群聊天记录的助手"，没有读者画像。建议在开头加：

```
受众：对 AI 实战感兴趣的中文程序员（写代码 + 用 agent + 关心模型迭代）。
他们已知：什么是 LLM/agent/RAG/context/prompt cache，不需要科普。
他们想看：具体版本号、踩坑、判断、金句、值得点开的链接。
声音：像在群里给同事发一条总结——懂行、有判断、不端着。
可以用「其实 / 说白了 / 挺有意思的」这类口语；可以表达个人偏好。
```

### P0-3 | 加 1 段反例 + 中文 slop ban list

调研最强的发现：**正例 + 反例 + 显式禁词** 比抽象描述有效得多。建议在 prompt 里加：

```
## 反 AI 味
禁用词：深入探讨、赋能、助力、重塑、颠覆、范式、拥抱变化、
       值得注意的是、综上所述、不仅……而且……、
       让我们来看看、在某种程度上。

禁用句式：
- 「首先……其次……最后……」三段式
- 「既要 X，又要 Y，更要 Z」三排比
- 「—— 这是一个值得思考的问题」这类总结尾巴
- 「在今天的日报中」这种自我介绍式开头
- 同一段内连续三句相同结构

节奏：长短句交错，单句最长 40 字。
```

随后给一段反例（标 `<!-- BAD -->`）和正例（标 `<!-- GOOD -->`），各约 150 字。当前 prompt 只有正例（第 127–152 行那段），加一段刻意写差的对照能显著降低 slop 复发率。

### P0-4 | 导读多样化

把当前的导读规则（第 79 行那一句）改为：

```
**导读**：1–2 段。从下面三种开头任选其一，不要用模板套用：
- 结论先行：「如果你今天只看一条：……」
- 悬念式：「群里有人贴了 X，没想到讨论引出了 Y……」
- 场景式：「周三晚上群里炸了三件事：……」

避免「X 分享了…，Y 讨论了…，Z 分享了…」平铺直叙；
同一 token 在导读中不要出现 3 次以上，必要时用「他」「另一位群友」或省略主语。
```

### P0-5 | 引用 + 摘抄收紧

当前 prompt 已有反冗余规则，但 5/4 出现了"无法跟人类工作"和"stand alone 的状态"两个 blockquote 表达接近的情况。建议补一条：

```
- 同一观点不写两次：简介已经表达过的判断，blockquote 只在「原话有梗 / 有语气」时保留；
  否则删掉 blockquote 或合并入简介。
- blockquote 之间表达的观点必须不同。意思相近的两条压缩为一条。
- 引用原文不超过 50 字；超过则改为转述。
```

### P0-6 | 显式要求"取舍立场"

加一句：

```
- 不是所有话题都要写。当天聊到但信息密度低、无判断、无新意的话题可以略过。
- 导读里至少给出一个**编辑判断**：哪个话题最值得读、为什么。
  可以用「最有意思的是……」「值得点开的是……」之类。
```

这会把日报从"流水账"推向"有取舍的总结"，符合 swyx / 宝玉 的内容风格。

### P1-1 | 减少"X 分享 / 提到 / 表示"句式

```
- 简介里不必每个观点都点名归属：重要观点点名，
  次要补充用「有群友补充……」或省略主语。
- 一段话里不要连续三个动词都是「分享 / 提到 / 表示 / 认为」，
  必要时换为「问」「吐槽」「反驳」「举例」「补充」。
```

### P1-2 | 4.7 思考溢出补丁

```
对简单话题（资讯转发、短闲聊）不要过度思考；
adaptive thinking 的篇幅应与话题复杂度匹配。
```

---

## 三、程序层建议

### P0-7 | 跨日去重：把前 3 天的标题/标签喂给模型

`cli.py` / `llm_extractor.py:170` 在调用前拼一段：

```
## 最近三天已发布日报的章节标题（避免简单复述）
- 2026-05-04: ChatGPT 收入与用户数据；FDE：AI Agent 时代的新角色；……
- 2026-05-03: ……
```

放在用户消息开头，加一条规则："如果某条新闻已在最近三天写过且无新增信息，跳过；如有新增进展，只写新增部分。"

5/4 和 5/5 重复 ChatGPT 财务数据那段就是这个机制能挡掉的。

### P0-8 | 末尾自动生成"延伸阅读"清单

`renderer.py` 后处理：扫正文里所有 `[标题](URL)`，在文末（tags 之前）插入：

```
## 延伸阅读
- [Boris 87 条 Claude Code 技巧](...)
- [AI 脚手架正在商品化](...)
- ...
```

纯结构操作，不用让 LLM 写。程序员读者强需求。

### P1-3 | URL 清洗

公众号链接的 `chksm / sn / key` 必须保留（删了 404），但 `utm_source / utm_medium / utm_campaign / sharer_* / mpshare / scene / srcid` 是可以安全剥的。在 `url_enricher.py` 或 `renderer.py` 加一个白名单 / 黑名单清洗，输出会干净很多。

建议：
- `mp.weixin.qq.com` 域名删 `utm_*`、`sharer_*`、`mpshare`、`scene`、`srcid`，其他参数保留。
- 其他域名只删 `utm_*`。

### P1-4 | 链接摘要 prompt 也加受众/声音设定

`url_enricher.py:27` 当前只 4 行约束，太薄。建议加：
- 受众画像（同 P0-2）。
- 压缩比（150–300 字 ≈ 原文 1/20）。
- 禁止"本文介绍了 / 作者认为"这类元描述句式。

### P2-1 | Reviewer pass（可选，烧 token）

模仿宝玉 / Latent Space 工作流：主提取完成后，用同一模型（或更便宜的 Haiku）跑一遍 reviewer prompt，专门检查：

- slop 词命中
- 是否同 token 在导读重复
- 是否有跨日重复信息
- 信息密度（每段是否都有新信息）

只在命中时返回修订建议，主流程吃掉。烧 1× 输出 token，但能持续把质量带回基线。

### P2-2 | Tag 标准化

当前 tag 自由生成（5/5 出现 `cognitive-overload`、`ai-wellbeing`、`jevons-paradox` 都是 one-off）。维护一个约 50 个标签的稳定 vocabulary（可以从历史日报频率统计），prompt 里注入"优先从下面词表选，不在词表的标签需要至少在过去 3 天用过 1 次"。SEO 与站内导流都会受益。

---

## 四、按优先级落地顺序

| 优先级 | 改动 | 位置 | 预期收益 |
|---|---|---|---|
| P0 | 加受众画像 + 声音锚点 | `llm_extractor.py:26` | 立即降低 AI 味 |
| P0 | 加 slop ban list + 反例 | `llm_extractor.py:26` | 立即降低 AI 味 |
| P0 | 导读多样化规则 | `llm_extractor.py:79` | 解决最大风格问题 |
| P0 | 显式取舍立场 | `llm_extractor.py:79` | 从流水账 → 有判断 |
| P0 | 跨日标题去重 | `cli.py` + `llm_extractor.py` | 消除重复资讯 |
| P0 | 末尾"延伸阅读"清单 | `renderer.py` | 纯程序，零成本 |
| P1 | 引用收紧 + 句式动词多样化 | `llm_extractor.py` | 进一步去 slop |
| P1 | URL utm 清洗 | `renderer.py` / `url_enricher.py` | 视觉干净 |
| P1 | 链接摘要 prompt 加厚 | `url_enricher.py:27` | 摘要更精准 |
| P2 | Reviewer pass | 新模块 | 持续质量基线 |
| P2 | Tag vocabulary | `renderer.py` + prompt | SEO / 导流 |

---

## 五、参考资料

### Prompt 工程

- [Anthropic Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)
- [Anthropic Use XML tags](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/use-xml-tags)
- [Anthropic Extended thinking tips](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/extended-thinking-tips)
- [Anthropic Adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking)
- [Effective context engineering for AI agents — Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

### AI slop 识别

- [Wikipedia: Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing)
- [The Field Guide to AI Slop — Charlie Guo](https://www.ignorance.ai/p/the-field-guide-to-ai-slop)
- [Measuring AI "Slop" in Text (arXiv)](https://arxiv.org/html/2509.19163v1)

### 技术博客写作

- [Simon Willison on Technical Blogging](https://writethatblog.substack.com/p/simon-willison-on-technical-blogging)
- [Latent.Space about](https://www.latent.space/about)
- [宝玉 — 自用的"科技文章翻译 GPT"和它的 Prompt](https://baoyu.io/blog/prompt-engineering/my-translator-bot)
- [Google Tech Writing — Audience](https://developers.google.com/tech-writing/one/audience)
- [阮一峰 — 中文技术文档的写作规范](https://www.ruanyifeng.com/blog/2016/10/document_style_guide.html)
