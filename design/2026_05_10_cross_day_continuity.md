# 2026-05-10 跨日延续与去重

让每天的日报不再彼此独立——前一天写过的内容今天不再重复，今天若续聊则可以引用并续写昨日章节。

## 一、问题与动机

### 重叠时段的重复报道

`chat_extractor.py:24-26` 的窗口是 `[date-1h, date+1d+1h]`，意味着深夜（00:00 前后）和凌晨各有约 1 小时是相邻两天日报共享的。当前两天独立处理，深夜冒出的资讯/链接很容易被两份日报各写一遍——`design/2026_05_06_prompt_optimization.md` 的 P0-7 已经指出了这个跨日重复问题。

### 日报缺乏"系列感"

现有日报每天独立成报。某话题如果跨多天讨论（模型测评连聊三天、某事件两天前埋下伏笔今天爆发），读者从日报里看不到这种延续——每天都从零讲起，已经看过昨天的读者要被迫读重复内容。

## 二、设计

### 总体思路

把过去 N 天（默认 3）的日报喂给模型，让模型在写今日报告时同时具备：
- **去重视野**：知道昨天写过什么，重叠时段的内容不重复报道。
- **续写能力**：今天如果继续讨论旧话题，可以引导读者读旧章节再补新进展。

### 数据来源

`debug/extract-{date}.md` —— LLM 原始输出。这是**最适合**的输入形式：
- 仍带 token（「沉稳的大象」），与今日 prompt 共用同一套命名。
- 仍带 `[章节不公开]` 标记，模型能看到全部内容（包括公开版被剥离的部分），有利于做完整去重。
- 与 token 持久化机制（`aliases.py`）配合，跨天指代同一人。

### 输入位置

User message 顶部：`<group_roster>` → `<previous_reports>` → `<chat_log>` → 处理规则。
长上下文先于约束（Anthropic best practice），避免约束被淹没。

### 跨日章节引用：占位符 + 后处理

模型用 `[[ref:YYYY-MM-DD|章节标题]]` 写跨日引用。Renderer 按版本展开：

| 版本 | 渲染结果 | 备注 |
|------|--------|------|
| 群内 PDF | `「章节标题」` | 日期由模型在自然语言中描述（"昨天"/"上周三"） |
| 公开 Web | `[「章节标题」](/daily/YYYY/MM/DD/daily/#slug)` | 检测目标 _posts 文件存在；不存在则降级为纯文本 |

为什么不让模型直接写 URL：
- 模型容易拼错 Jekyll permalink（`/daily/...` 还是 `/posts/...`、是否末尾斜杠）。
- Group 版需要的格式与 public 版不同；让模型只携带语义、由 renderer 生成最终形式。
- 校验目标文件存在性是 renderer 的职责，模型不需要知道。

### 缺失处理（Partial vs. Full miss）

**全部缺**（最近 N 天均无 extract）：判定为首次运行 / 长期暂停，静默继续生成独立日报。

**部分缺**（有些天有、有些天没有）：暗示跑漏了或那天没消息。交互提示 `[c]ontinue / [s]kip / [a]bort`，默认 continue。`--no-prior-prompt` 关掉提示，适合 cron 自动化。

## 三、实现

### 新增文件

- `wechat_daily/prior_report.py`：`load_prior_reports(date_str, n_days)` 从 disk 读取，按日期升序返回 `[(date, markdown), ...]`；缺失静默跳过。`format_prior_reports_block(...)` 包装成 `<previous_reports>` XML。
- `tests/test_prior_report.py`、扩展 `test_llm_extractor.py` 与 `test_renderer.py`。
- `design/2026_05_10_cross_day_continuity.md`（本文）。

### 改动文件

- `wechat_daily/llm_extractor.py`：
  - `extract_report(...)` 增加 `prior_reports` 参数。
  - System prompt 加 "## 关于跨日章节引用（特殊占位符）" 一节，定义 `[[ref:...]]` 语法。
  - User instructions 加 "## 关于跨日延续与去重" 一节（重叠去重 / 同话题判定 / 续写写法 / 金句不重引 / 日期措辞 / 无 prior 时 fall-through）。
- `wechat_daily/renderer.py`：
  - 新增 `_REF_RE`、`_slugify_heading`、`_expand_refs_group`、`_expand_refs_public`。
  - `render_group` 在隐藏标注后调用 `_expand_refs_group`。
  - `render_public` 在隐藏剥离后调用 `_expand_refs_public`，传入 `PUBLIC_REPO_DIR / "_posts"` 做存在性校验。
- `wechat_daily/cli.py`：
  - `--prior-days N`（默认 3，0 关闭）和 `--no-prior-prompt` 两个新 flag。
  - `_run_db_pipeline` 在 LLM 调用前载入前 N 天，按需提示用户，最后传入 `extract_report`。

## 四、决策记录

### Q1：回溯几天？

最终选 **3 天**。1 天能解决重叠去重，但跨周末 / 跨多日话题判断不全。7 天太贵，多数老内容当天用不上。

### Q2：喂什么形式？

**完整 markdown**。只喂标题/导读会退化成纯去重，无法做"今天的发言是不是新观点"的判断——丧失这个功能的核心价值。Token 成本中等（每份典型 5-15K），可接受。

### Q3：跨日引用怎么写？

**特殊占位符 + renderer 后处理**。让模型只写语义（哪天哪节），不让模型写 URL（容易写错、跨版本不一致）。

### Q4：群内 PDF 怎么呈现？

**只渲染章节标题**，日期由模型在占位符外用自然中文描述（"昨天 / 5 月 3 日 / 上周三"）。试过 `「标题」（YYYY-MM-DD 日报）` 形式但太硬，影响 PDF 阅读体验。

### Q5：公开版链接是否校验？

**校验 `_posts/YYYY/MM/YYYY-MM-DD-daily.md` 存在**。不存在则降级为纯文本，避免发布带 404 的链接。锚点 slug 用 best-effort kramdown 模拟（CJK 保留、ASCII 标点剥离），匹配不上就退化到 page-level 跳转，不影响发布。

### Q6：缺失日报怎么处理？

**部分缺失提示用户、全部缺失静默继续**。`--no-prior-prompt` 用于自动化场景。

## 五、未来考虑

- **Slug 准确性**：当前 `_slugify_heading` 是 best-effort，无法 100% 复刻 kramdown 规则。如果常用标题里的标点导致跳转不到位，可在 renderer 给 `### 标题` 主动加 `{#manual-id}` 并把同一规则共享给 ref 展开。
- **跨日 token 漂移**：今日某用户的 token 与昨日不一致（理论上 aliases.py 持久化保证不会发生，但若数据库重置就会）会让模型把"沉稳的大象"和"开朗的企鹅"当成两个人。运行前可加一个 sanity check：今日 token 集合对昨日 token 集合的覆盖率应 >0.6（连续运行的常态）。
- **`<previous_reports>` 的 prompt cache**：如果切换到 prompt caching，前 N 天的内容是天然的稳定前缀，可以打 cache breakpoint 大幅省 token。
