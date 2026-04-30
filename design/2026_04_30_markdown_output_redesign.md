# 日报输出格式重构：JSON → Markdown

日期：2026-04-30
状态：待实施

## 背景与动机

当前 `llm_extractor.py` 用 Anthropic tool use + strict JSON schema 让模型产出
结构化 `DailyReport`（intro + sections[]，每个 section 含 type/title/body/
comments[token,text]/tags/public_safe/public_safe_reason）。`renderer.py`
按 `section.type` 自动分组成 `## 行业新闻 / ## 工具 / …`，再渲染 `###` 子标题。

实际运行中**频繁出现 body 字段在句子中间被截断**。`_check_truncation` 注释
明确：截断不是 schema/JSON 转义问题，而是**模型遇到不愿引述的内容（乱码、
敏感片段）时，主动在 strict-string 边界提前 close**。强 schema 反而让模型
没有"用描述性语言代替"这种自然出口。

同时，强 schema 把"评论必须是 `[{token, text}]` 数组"这种结构强加给模型，
牺牲了排版自由度（无法在评论之间穿插小段说明、无法用列表/段落混排）。

## 目标

1. 模型直接输出一整篇 markdown；正文格式由 prompt 约定，不再受 schema 强约束。
2. 通过**章节末尾的 `[章节不公开：原因]` 标记**实现公开/内部版分流，模型在
   写完正文后再决定是否隐藏。
3. 内部版用醒目方式展示被隐藏章节，便于人工审核。
4. 公开版自动剥除被标记章节，并清理变空的大类。

## 输出契约（写进 prompt）

模型输出一整篇 markdown，结构如下：

```
（intro：1–2 段导读，使用 token 指代群友）

## 行业新闻

### 具体话题
（简介：1–3 句话，概括话题与要点）

> 沉稳的大象：原话或近似引用

### 另一个话题
（简介…）

> 活泼的企鹅：…

[章节不公开：涉及未签约客户的敏感案例]

## 工具

### 某工具评测
…

## 方法论

### …

## 闲聊花絮

### …

---

tags: model-release, long-context, agent
```

### 约定要点

- **大类标题用 `##`，子话题用 `###`**。允许的大类（模型按当天内容选用）：
  - `## 行业新闻`
  - `## 工具`
  - `## 方法论`
  - `## 闲聊花絮`
- 每个 `###` 子话题统一采用 **「标题 + 简介 + 引用」** 三段式：
  1. `### 标题`
  2. 一段或多段简介，概括要点
  3. 一条或多条 markdown blockquote `> token：原话或近似引用`（0–3 条）
  4. （可选）`[章节不公开：原因]` 标记，**单独成行，紧贴本章节末尾**
- 文末固定一行 `tags: a, b, c`（英文小写、连字符），位于全文最末，前面用
  `---` 分隔。
- 章节级 `[章节不公开：原因]` 标记的语义判定（替代现行 `public_safe` 自评）
  与现有 prompt 一致：隐私顾虑 / opt-out 波及 / 公众环境风险三类之一即标记。

### 保留的 prompt 内容

下列章节从现行 prompt **照搬**，措辞不变：

- 关于匿名化（token 硬性规则）
- 关于 `token⟨原文⟩` 标记（同名消歧）
- 关于花名册
- 关于隐私占位符（`[此消息已隐藏]`）
- 关于内容完整性（允许用描述性语言代替难以引述的内容）

### 重写的 prompt 内容

- 移除：tool use 的 strict JSON 字段说明、`public_safe` 自评字段说明。
- 新增：上文「输出契约」全文，包含一个完整示例。
- 修改：`public_safe` 改述为「何时在章节末尾添加 `[章节不公开：原因]` 标记」。

## 解析与渲染

### 共同步骤

1. 收到模型输出的整篇 markdown。
2. 提取末尾 `tags:` 行：正则匹配 `^tags:\s*(.+)$`（最后一段非空行），split
   by `,`、trim、得到 tags 列表；从正文剥离该行及其上方的 `---` 分隔。
3. 全文 token 替换：沿用 `_build_token_replacer`（按已知 token 集合一次正则
   替换）。两版的 `resolve_fn` 不同：
   - 内部版：`token → 真实昵称`（`token_map.wxid → contact_map.by_wxid`）
   - 公开版：`token → 公开别名 / 某群友`（`alias_db.public_name_of` /
     opt-out 时返回「某群友」）

### 公开版剥除逻辑

按行扫描 markdown：
- 一旦在某 `##` 或 `###` 标题之后、下一个同级或更高级标题之前的内容里出现
  **任意位置**的 `[章节不公开：…]` 标记 → 整段（标题 + 内容，到下一个同级或
  更高级标题之前）从公开版输出中删除。
- 解析模式：行级正则 `\[章节不公开[：:][^\]]*\]`，用户已确认不处理 `]`
  转义；解析失败由人工审核兜底。
- 剥除完成后，**对每个 `##` 大类做空内容检查**：若其后到下一个 `##` 之前
  没有任何 `###` 子话题，整个 `##` 块也删除。

公开版最终结构：
```
<Jekyll front matter（含 tags、toc:true、layout:post 等）>

<intro 段落>

<剥除后的正文>
```

### 内部版隐藏标记的渲染

按行扫描 markdown，对每个标题（`##` 或 `###`），若**其所在章节内容里**
（标题之后到下一同级或更高级标题之前）出现 `[章节不公开：原因]`：

1. 从该标题文字之外的位置移除 `[章节不公开：…]` 标记本身。
2. 标题文字前加 `🔒 ` 前缀：`### 客户投诉处理` → `### 🔒 客户投诉处理`
3. 标题正下方插入一行醒目横幅：
   ```
   > ⚠️ **公开版隐藏** · 原因：涉及未签约客户的敏感案例
   ```

宽松识别：只要本章节范围内出现匹配 `\[章节不公开[：:][^\]]*\]` 的子串，
即触发隐藏渲染（无论标记出现在标题行、段落里、还是末尾）。原因取首个匹配
中冒号后的内容；多个匹配以第一个为准。

内部版最终结构：
```
# YYYY-MM-DD 群聊日报

<intro 段落>

[TOC]

<带 🔒 标记和横幅的正文>

## 本期指令执行记录
（沿用现有逻辑）

---

tags: model-release, long-context, agent

公开版日报网站：https://...
```

`[TOC]` 在 intro 之后、第一个 `##` 之前自动插入。`tags` 行保留可见（便于
审核模型选词），不参与跳转。

## 数据模型变更

`wechat_daily/models.py`：

- 删除 `Comment`、`Section`、`SectionType`、`VALID_SECTION_TYPES`。
- `DailyReport` 缩为：

```python
class DailyReport:
    def __init__(self, date: str, markdown: str) -> None:
        self.date = date
        self.markdown = markdown
```

`from_dict` 不再需要（不再有 JSON 中间表示）。

## 模块改动清单

### `wechat_daily/llm_extractor.py`

- 删除 `_TOOL` schema、`_check_truncation`、`ExtractionError` 中 strict 相关分支。
- `extract_report` 改为 streaming text 调用：
  - 不再用 `tool_choice`，改 `messages.stream` 直接收 text events。
  - 累积 `event.text` 到字符串 buffer，进度回调照常。
  - `stop_reason == "max_tokens"` 仍报错。
  - `stop_reason == "refusal"` 仍报错。
  - 成功时返回 `DailyReport(date_str, markdown=buffer)`。
- `_save_extract`：保存到 `debug/extract-{date}.md`（不再是 `.json`）。
  保存时同样把 `roster_text + chat_block` 的 input_preview 写入一个 sidecar
  文件 `debug/extract-{date}.input.txt`（取代原来 JSON 中的 `_input_preview`
  字段）。
- `_save_failure`：保留，写 `extract-{date}.FAILED.json`，记录 reason、
  raw（已收到的部分 markdown）、input_preview。
- 重试逻辑、超时配置、httpx 异常处理保留不变。

### `wechat_daily/renderer.py`

完全重写。提供两个入口：

- `render_group(report, alias_db, contact_map, command_log, token_map) -> str`
- `render_public(report, alias_db, token_map) -> str`

内部实现按上文「解析与渲染」执行，共享 helpers：

- `_extract_tags(markdown) -> tuple[str, list[str]]`：剥离尾部 tags 行，返回
  (清理后的正文, tags 列表)
- `_split_sections(markdown) -> list[Section]`：按 `##`/`###` 切片，每片携带
  level、title、content（含子标题）。或者更简单地：按行扫描记录每个标题的
  起止 line index，操作行数组。
- `_strip_hidden_for_public(lines) -> list[lines]`：删除被标记的 `##`/`###`
  块；二次扫描清理空 `##`。
- `_annotate_hidden_for_group(lines) -> list[lines]`：给被标记标题加 🔒 +
  横幅，删除标记本身。
- `_replace_tokens(text, ...)`：沿用 `_build_token_replacer`。

### `wechat_daily/cli.py`

- 调用 `extract_report` 拿到 `DailyReport(date, markdown)` 之后，直接传给
  `render_group` / `render_public`。
- Gemini 旧路径：把返回的 markdown 包进 `DailyReport(date, markdown)`，
  走同一套 renderer。这样 Gemini 路径**自动获得**：
  - tags 行解析（如果模型按 prompt 输出了）
  - 隐藏标记支持（如果模型按 prompt 输出了）
  - token 替换（沿用既有逻辑）
  - 公开版剥除清理
- 注意 Gemini 旧 prompt 没有 tags / 隐藏标记的约定。短期内 Gemini 输出不会
  命中这些规则，但兼容性零成本。是否同步更新 Gemini prompt 由后续决定，本次
  改动不动。

### `scripts/redact.py`

**删除**。README 同步移除「事后撤回」段落。

### `wechat_daily/models.py`

缩水到只剩 `DailyReport`（见上文）。

## 测试改动

- `tests/test_renderer*.py`：全部重写。覆盖：
  - tags 行剥离 + 注入 front matter
  - 隐藏标记 → 公开版剥除整章节
  - 隐藏标记 → 内部版 🔒 + 横幅
  - 一个 `##` 大类下所有 `###` 都被隐藏 → 公开版连 `##` 一起清理
  - token 替换（内部 → 真名；公开 → 公开别名 / 某群友）
  - 标记出现在标题行、段落中、末尾三种位置都能被识别
- `tests/test_llm_extractor*.py`：改为 mock streaming text 响应，验证返回
  `DailyReport(date, markdown)`；验证 max_tokens / refusal 报错。
- `tests/test_redact*.py` 之类：删除。

## README 改动

- 「输出文件」表格：`debug/extract-YYYY-MM-DD.json` 改为 `.md`（+ 简短说明）。
- 「辅助脚本」段落：删除 `redact.py` 行。
- 其他段落不动。

## 待实施时再次确认的细节

- **`extract-{date}.md` 是否仍是 redact.py 的入参格式假设**：redact.py 已删，
  无需考虑。
- **Gemini 路径短期内会通过新 renderer 输出**：renderer 对未见过 tags/
  标记的 markdown 是 no-op（tags 行不存在 → tags=[]；标记不存在 → 不剥除），
  安全。
- **现有 `debug/extract-*.json`**：旧文件保留，不做迁移。`scripts/migrate_token_format.py`
  的 token 格式迁移与本次重构无关，不动。

## 实施顺序建议

1. 重写 prompt（在 `llm_extractor.py` 中）。
2. 重写 `models.py`、`renderer.py`、`llm_extractor.py`。
3. 删除 `scripts/redact.py`。
4. 重写测试。
5. 跑一次真实日期生成，肉眼对比新旧两版输出（隐藏标记、tags、token 替换）。
6. 更新 README。
