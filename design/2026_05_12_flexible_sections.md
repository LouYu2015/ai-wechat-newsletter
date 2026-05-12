# 2026-05-12 二级章节：四分法 → 默认 + 专题

把 `llm_extractor.py` 系统提示词中的"二级章节必须四选一"放松为"四类作为默认，允许在合适时机抽专题"。本文记录决策的来龙去脉，便于后续回看。

## 一、为什么改

### 5/6 设计文档的判断

`design/2026_05_06_prompt_optimization.md` 把章节四分法列为"已经做得好、建议保留"，理由是与 The Pragmatic Engineer 的 "In this issue:" 风格契合。这个判断在当时（5/1–5/5 的日报样本）是合理的：那五天每天的话题分布相对均衡，四分法能装得下。

### 之后两周暴露的两个问题

1. **重磅日内容被强行拆散**。例如某次主流模型发布会让 8–10 个相关子话题分散到 `## 行业新闻`（发布要点、定价）、`## 工具`（首批集成的 IDE/SDK）、`## 方法论`（实测踩坑），读者扫读时跨三个 `##` 才能拼出全貌，且每个 `##` 下其它非发布话题被挤到角落。
2. **平淡日凑足四类显得注水**。当天只有 2 条工具讨论、1 条新闻时，模型仍倾向于把任何一段闲聊塞进 `## 闲聊花絮`、把一条转链塞进 `## 行业新闻`，制造"虚假完整感"。

5/6 文档的"保留四分法"判断没错——它在均衡日确实好用——但它默认了**所有日子都是均衡日**，对极端日没有兜底。

### 跨期一致性 vs 内容主导

调研社群日报 / chat digest 读者偏好时，反复出现的关键词是**"省时间 / 易扫读 / 不漏重要事"**（[Slack AI digest](https://slack.com/features/ai)、[INMA 15 elements](https://www.inma.org/blogs/conference/post.cfm/these-15-elements-make-a-newsletter-successful)）。这两件事在不同日子需要不同结构：

- 均衡日：跨期一致的四类胜出（老读者从 TOC 一眼定位"今天工具区有什么"）。
- 重磅日：专题化胜出（"Opus 4.7 发布"作为独立 `##` 比拆到三处更易扫）。

学术上 [iterative topic taxonomy induction](https://arxiv.org/html/2510.15125)、[TopicGPT](https://aclanthology.org/2025.acl-long.902.pdf) 等也指出 emergent labeling 在内容主导场景下覆盖率 / 连贯性优于固定 taxonomy，但纯 emergent 在跨期一致性上吃亏——这正是"默认 + 例外"想吃下的两头。

## 二、最终方案

`llm_extractor.py:34-55` 改为：

1. **默认四类保留**（`行业新闻` / `工具` / `方法论` / `闲聊花絮`），每个后面括号补一句作用域提示，降低边界模糊（之前模型把"某 SaaS 调价"放进 `## 工具` 的情况）。
2. **触发专题的硬条件**：单一话题占当日讨论 1/3 以上 **且** 产生 3+ 个相关子话题。两个条件都要满足——避免"今天有 3 个长帖都跟 RAG 有关"就抽专题（它们可能本就属于 `## 方法论`）。
3. **专题命名硬约束**：4–10 字短名词短语；禁 `关于…的讨论` / `今日 X 相关` / `X 的那些事`。命名长度上限是为了保持 TOC 扫读节奏与四类（2–4 字）接近。
4. **`## 闲聊花絮` 不抽专题、不改名**。哪怕当天所有梗都围绕同一件事，也固定用这个名字——这是给老读者保留的最稳定锚点。
5. **反例**：来源不同、讨论不连续的话题不要凑聚合名（`## 模型动态` `## Agent 漫谈`），仍归默认四类。
6. **挑话题角度的扫描提示**（非强制成章）：新模型/工具发布与定价、踩坑、论文长文、行业动态、群友自荐作品、具体数据点、金句梗。这一段不影响章节结构，仅作"不漏点"清单。

## 三、不做的事

- **不改 `renderer.py`**。它只看 `##` / `###` 通用结构，对名字无依赖；公开版剥除、空大类清理都用名字无关的扫描——专题化天然兼容。
- **不改 Jekyll 站点**。`_config.yml` 里的 `categories: Daily` 是 post-level 的，与正文 `##` 无关。
- **不维护"专题白名单"**。模型每天即兴命名即可；只要遵守命名硬约束，跨期出现 `## Claude Opus 4.7 发布` 与 `## DeepSeek V4 发布` 各一次完全合法。
- **不改标签生成**。tags 行仍由模型自由生成（与 5/6 文档 P2-2 提到的"tag vocabulary"是独立问题）。

## 四、可能的回退条件

如果接下来的样本里出现以下情况之一，应重新评估：

- 模型对"占讨论量 1/3 以上"判断系统性偏松，导致平淡日也抽出无意义专题。
- 专题名违反 4–10 字 / 禁描述句式约束。
- 老读者反馈"找不到熟悉的入口"。

回退路径很轻：把 prompt 里那段专题化规则注释掉即可恢复严格四分法，无须改任何程序。

## 五、相关文件

- 修改：`wechat_daily/llm_extractor.py:34-55`（系统提示词二级章节段）
- 上游设计：`design/2026_04_30_markdown_output_redesign.md`（最早定下四分法的契约）
- 直接前置：`design/2026_05_06_prompt_optimization.md` 一、"已经做得好、建议保留"第 1 条

## 六、参考资料

- [Slack AI features](https://slack.com/features/ai)
- [INMA: 15 elements of a successful newsletter](https://www.inma.org/blogs/conference/post.cfm/these-15-elements-make-a-newsletter-successful)
- [The Pragmatic Engineer about](https://newsletter.pragmaticengineer.com/about)
- [Iterative Topic Taxonomy Induction with LLMs](https://arxiv.org/html/2510.15125)
- [LLM-Guided Semantic-Aware Clustering for Topic Modeling](https://aclanthology.org/2025.acl-long.902.pdf)
