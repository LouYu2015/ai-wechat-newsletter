# 公开日报与隐私保护系统设计

**日期**: 2026-04-17
**状态**: 决议已达成，待实现
**范围**: 从「单用户手动发群」演进为「双版本生成 + 公开 GitHub Pages 站点 + 三级隐私控制」

---

## 1. 背景与目标

当前日报机器人每天从本地加密数据库读取群聊记录，调用 Claude Opus 生成一份含真实昵称、含闲聊花絮的 Markdown / PDF，由作者手动发送到微信群。

本次改造的目标：

- **双版本产出**：群内版（真实昵称、含花絮，当前发送方式不变）与公开版（匿名化、按三级隐私模型过滤）。
- **三级隐私模型**：`optout`（不出现）/ 默认匿名（稳定派生）/ 公开别名（用户通过 `/alias` 指令主动设置）。
- **一致性保证**：两版日报在「信息选择」层面完全一致，避免读者因阅读其中一个版本而遗漏另一个版本中的关键信息。
- **公开发布**：公开版推送到独立的公开仓库 `git@github.com:LouYu2015/AI-chatgroup-daily.git`，启用 GitHub Pages，以 CC BY-NC 4.0 共享。
- **模块化与可测试**：把现有单体 `main.py` 拆成若干职责单一、可单元测试的模块。

**非目标**（本期不做）：

- 实时微信机器人回复通道（`/alias` 不做即时确认，下次跑日报时生效）。
- 全文搜索、订阅等高级前端功能（Jekyll 主题自带的 TOC/标签/分类已够用，搜索留待后续）。
- 对群主/管理员的特殊权限区分（所有成员共享同一套隐私指令）。
- 公开版 PDF（公开发布只走 GitHub Pages，不产出 PDF）。

---

## 2. 核心方案：结构化提取 + 分叉渲染

### 2.1 为什么不是「一次调用 + 后处理」

在前期讨论中比较过三种方案：

| 方案 | 两版内容一致性 | 花絮质量 | optout 处理 | 工程复杂度 |
|---|---|---|---|---|
| 一次调用 + 后处理 | 正式部分高，花絮差 | 差（token 替换后失去梗的语感） | 困难 | 低 |
| 两次独立调用 | 低（LLM 选择题材本身会漂移） | 中等，但两版各说各话 | 中等 | 低 |
| **结构化提取 + 分叉渲染** | **最高** | **最好** | **最干净** | 中 |

选择结构化提取方案的核心论据：**信息选择与语言渲染解耦**。同一段聊天记录，要讲哪些新闻、引用谁的观点、收录哪段花絮，这些「选择」只在结构化提取阶段发生一次；群内版与公开版的差异，只体现在「渲染」阶段（token 如何映射到具体名字、哪些条目被跳过）。

### 2.2 两阶段流程

```
┌────────────────────────────────────────────────────────────────┐
│ 阶段 1：共享提取（一次 Claude 调用）                          │
│                                                                │
│   加密微信 DB                                                  │
│       │                                                        │
│       ▼                                                        │
│   [chat_extractor]  按日期切片，产出带 wxid 的消息列表         │
│       │                                                        │
│       ▼                                                        │
│   [privacy] 应用 optout 遮蔽 + 全量 token 化                   │
│       │        （wxid → token，正文中提及的名字也替换）        │
│       ▼                                                        │
│   [llm_extractor] Claude 结构化提取（JSON 中间表示）           │
│       │                                                        │
│       ▼                                                        │
│   中间 JSON（只含 token，不含真实名字）                        │
└────────────────────────────────────────────────────────────────┘
                        │
            ┌───────────┴───────────┐
            ▼                       ▼
┌──────────────────────┐  ┌──────────────────────┐
│ 阶段 2a：群内版渲染  │  │ 阶段 2b：公开版渲染  │
│                      │  │                      │
│ token → real_name    │  │ token → public_alias │
│                      │  │        / default_anon│
│ 保留所有章节         │  │ 按 public_safe 过滤  │
│                      │  │                      │
│ Markdown + PDF       │  │ Jekyll Markdown      │
│      │               │  │      │               │
│      ▼               │  │      ▼               │
│ archive/ 归档        │  │ 公开仓库本地 commit  │
│                      │  │ + HTML 预览          │
└──────────────────────┘  └──────────────────────┘
```

### 2.3 关键设计原则

1. **身份锚点永远是 `wxid`**。所有映射表、指令、遮蔽逻辑都以 `wxid` 为主键。
2. **Token 化发生在 LLM 调用之前**。Claude 看到的任何人名都已是稳定 token，因此它输出的 JSON 天然「模板化」。
3. **对话正文中的名字提及也必须 token 化**。不只是发言人字段，还要扫描每条消息的正文与引用块内部。
4. **Claude 的结构化输出是第一等公民**。不让 Claude 直接生成 Markdown，而是产出带明确字段的 JSON；Markdown 由确定性的 Python 渲染器生成。
5. **泄漏检测是发布前的硬闸门**。公开版 Markdown 写入公开仓库之前，必须对所有已知真实昵称做全文残留扫描。

---

## 3. 模块划分

从当前约 1200 行的 `main.py` 拆出以下模块：

```
ai-wechat-overview/
├── wechat_daily/
│   ├── __init__.py
│   ├── config.py            # 常量、路径、env 加载
│   ├── wechat_db.py         # sqlcipher3 连接、密钥加载（只读）
│   ├── contacts.py          # wxid → 备注昵称映射
│   ├── message_parser.py    # local_type + blob → 结构化消息
│   ├── chat_extractor.py    # 按日期切片组装消息列表
│   ├── aliases.py           # 别名/optout 持久化、指令扫描、默认匿名生成、备份
│   ├── privacy.py           # token 化、optout 遮蔽、引用块遮蔽、泄漏检测
│   ├── llm_extractor.py     # Claude 结构化提取（JSON schema + tool use）
│   ├── renderer.py          # 中间 JSON → Markdown（群内版 / 公开版两个入口）
│   ├── pdf.py               # Markdown → PDF（WeasyPrint，仅群内版用）
│   ├── archiver.py          # 群内 PDF 的 7 天滚动归档
│   ├── publisher.py         # 公开仓库本地 clone 管理、commit、预览、-y 推送
│   └── cli.py               # argparse + 主流程编排
├── tests/
│   ├── test_message_parser.py
│   ├── test_aliases.py
│   ├── test_privacy.py
│   ├── test_renderer.py
│   ├── test_archiver.py
│   ├── test_publisher.py
│   └── fixtures/            # 合成聊天数据、合成 aliases.json 等
├── scripts/
│   ├── rebuild_aliases.py   # 从历史消息完整重建 aliases.json
│   └── redact.py            # 事后撤回工具
├── design/
│   └── 2026_04_17_public_daily_design.md   # 本文件
├── data/                    # 运行时状态，全目录 gitignored
│   ├── aliases.json
│   ├── aliases.cursor
│   ├── anon_salt.txt
│   └── aliases.backup/
├── main.py                  # 保留为薄壳：调用 wechat_daily.cli.main()
└── .gitignore               # 新增 data/ 条目
```

### 3.1 各模块职责摘要

- **`config.py`** — 路径常量、模型名、群 ID（从 `.env` 读取，不硬编码）。
- **`wechat_db.py`** — 打开加密 SQLite（内存模式，不落盘），提供 `with open_db() as conn:` 上下文管理器。
- **`contacts.py`** — 读取 `contact.db`，生成 `ContactMap`；提供 `by_wxid(wxid) -> str` 与 `all_nicknames() -> list[str]`（泄漏检测要用）。
- **`message_parser.py`** — 输入数据库记录，输出 `Message` dataclass；封装所有 `_MSG_*` 常量与 blob 解压、引用块解析逻辑。**不依赖**别名/隐私层。
- **`chat_extractor.py`** — 编排 `wechat_db + contacts + message_parser`，按日期区间返回 `list[Message]`，顺带返回缺失日期列表。
- **`aliases.py`** — 见 §4。
- **`privacy.py`** — 见 §5。
- **`llm_extractor.py`** — 把 token 化后的 chat_history + 系统提示组成请求，调用 Claude，校验 JSON 结构，返回 `DailyReport` 对象。
- **`renderer.py`** — 两个公开函数：`render_group(...)` 与 `render_public(...)`；共享底层 `_render_sections`。
- **`publisher.py`** — 见 §7。
- **`cli.py`** — 解析 `--date`、`--allow-incomplete`、`-y` 等参数，编排整条管线。

---

## 4. 别名数据库（`aliases.py`）

### 4.1 持久化格式

文件路径：`data/aliases.json`（`data/` 整目录加入 `.gitignore`）。

```jsonc
{
  "version": 1,
  "updated_at": "2026-04-17T10:00:00+08:00",
  "users": {
    "<wxid>": {
      "default_anon": "沉稳的大象07",       // 稳定派生，从不变
      "real_name_seen": "某备注名",         // 最近一次看到的 contact_map 值，用于调试
      "public_alias": "Duckie",             // null 表示未设置
      "optout": false,                      // true 表示从公开版完全移除
      "last_command_ts": 1745000000,
      "last_command": "/alias Duckie"       // 审计用
    }
  },
  "alias_reservations": [
    // 曾被使用但已释放的 public_alias，30 天内保留不可复用
    { "alias": "OldName", "released_by_wxid": "<wxid>", "released_at": 1744000000 }
  ]
}
```

### 4.2 默认匿名名生成（加盐，防逆向）

为避免攻击者通过「已知的群友 wxid 列表 + 公开的 `default_anon`」反推身份，匿名名派生必须加盐：

```python
# data/anon_salt.txt 首次运行时随机生成，之后永不变；gitignored
def default_anon(wxid: str, salt: bytes) -> str:
    h = hashlib.sha256(salt + wxid.encode()).digest()
    adj = ADJECTIVES[h[0] % len(ADJECTIVES)]
    animal = ANIMALS[h[1] % len(ANIMALS)]
    suffix = h[2] % 100
    return f"{adj}的{animal}{suffix:02d}"
```

盐的规则：

- 首次运行 `aliases.py` 时检测 `data/anon_salt.txt` 不存在，用 `secrets.token_bytes(32)` 生成并写入。
- 盐文件**永远不改动**；备份在 `data/aliases.backup/anon_salt.txt`（同步保留）。
- 代码库 `.gitignore` 覆盖整个 `data/`，盐不会进入任何 git 历史。
- 若盐丢失：所有人的 `default_anon` 会重生成、漂移；但 `aliases.json` 里已持久化的 `default_anon` 字段仍然是权威值，**不会被重新计算覆盖**——也就是说，盐只在「首次为某个 wxid 计算匿名名」时起作用，之后读的是 `aliases.json` 的缓存。盐丢失只影响**新入群用户**，老用户不受影响。

### 4.3 指令扫描

每次运行日报前，扫描数据库里所有 `_MSG_TEXT` 消息、从 `data/aliases.cursor` 记录的时间戳起增量推进：

```
/alias 小明        → public_alias = "小明"
/alias             → public_alias = null （恢复默认匿名，旧名进入 30 天预留期）
/optout            → optout = true
/optin             → optout = false
```

同一 wxid 多条指令按 `create_time` 升序回放，最后一条胜出。扫描完成后更新 cursor。

### 4.4 `public_alias` 冲突与预留规则（Q4 决议）

**先到先得 + 释放后 30 天预留**：

1. **先到先得**：若 A 已持有 `public_alias = "X"`，B 设置 `/alias X` **被拒绝**，拒绝记录写入当期群内版指令日志。
2. **释放后预留**：A 执行 `/alias`（清空）或 `/alias Y`（更名）时，"X" 不立即释放，而是进入 `alias_reservations`，记录 `released_at`。
3. **预留期**：`alias_reservations` 中 `released_at < now - 30 days` 的条目在每次运行开始时被清理；其他人在 30 天内 `/alias X` 会被拒绝。
4. **原主回收**：A 本人在 30 天内可以通过 `/alias X` 重新夺回自己刚释放的别名（即「释放 → 回收」被允许）。

**其他校验**：

- `public_alias` 字符集白名单：`[\p{L}\p{N}_\-·]{1,16}`，过长或含表情直接拒绝。
- `public_alias` 不能与任何人的 `default_anon` 冲撞（防伪装）。
- `public_alias` 不能是保留词（如 `admin`、`bot`、`anonymous` 等）。

### 4.5 30 天自动备份（Q2 决议：仅本地）

每次运行日报、写入新 `aliases.json` **之前**：

1. 若 `data/aliases.backup/YYYY-MM-DD.json` 当天不存在，把现有 `aliases.json` 原样复制。
2. 同步把 `anon_salt.txt` 备份到 `data/aliases.backup/anon_salt.txt`（单文件，不按日期，首次之后永不覆盖）。
3. 删除 `data/aliases.backup/` 内 30 天前的文件。

备份**仅保留本地**，不 push 任何仓库。整个 `data/` 目录被 `.gitignore` 覆盖。

加载时若 `aliases.json` 解析失败，自动回退到 `aliases.backup/` 中最近一个可解析的快照，并在控制台高亮提示。

### 4.6 Q5 决议：增量扫描 + 兜底重建

- `cli` 每次运行先执行增量指令扫描（用 `data/aliases.cursor`）。
- `scripts/rebuild_aliases.py` 从零重放全部历史消息，重建 `aliases.json`（保留现有的 `default_anon`，因为它们应以 `aliases.json` 为准，重建时不重新派生）。兜底用。

---

## 5. 隐私处理管线（`privacy.py`）

### 5.1 Token 化

Token 与 `default_anon` 共享同一套派生函数——**token 就是 `default_anon`**。好处：公开版里如果某人没设 `public_alias`，他在 Markdown 里显示的就是 token 本身，无需再做一层映射。

Token 采用「有人味的假名」而非 `USER_7f3a` 这类机器标识，以免 LLM 生成的文字变生硬，失去「他 / 她 / 这位群友」这类代词。

### 5.2 Token 化的覆盖范围

必须在三处覆盖所有可能出现真实名字的位置：

1. **消息发言人字段**：`[HH:MM] <token>: ...`。
2. **消息正文中的提及**：按 `contact_map.all_nicknames()` 做**长度降序**字符串替换。
3. **引用块内部**：`_MSG_QUOTE` 被解析出的 `quoted.speaker` 与 `quoted.content` 字段也分别 token 化。

长度降序的原因：避免「先替换短昵称把长昵称的子串误替」。

### 5.3 Optout 遮蔽

Optout 用户的消息保留时间戳、替换内容：

```
[14:23] [此消息已隐藏]
[14:23–14:27] [某群友连续发言 5 条已隐藏]   # 连续多条合并
```

其他人引用了 optout 用户时，引用块内容替换为 `[引用内容已隐藏]`，回复本身保留。

`_MSG_TAP`（拍了拍）：发起人或对象任一方是 optout 用户，整行替换为 `[某人做了个动作]`。

### 5.4 Prompt 侧的配合

`llm_extractor` 的系统提示中明确告知 Claude：

> 部分群友已申请隐私保护，其发言以 `[此消息已隐藏]` 标记。处理这些标记时：
> 1. 不要试图推测或还原被隐藏的内容。
> 2. 若某段讨论的关键输入来自被隐藏消息，用「有群友提出了一个观点，引发了讨论」这类模糊表述。
> 3. 若某条回复明显在回应被隐藏的消息（如「说得对」「同意上面」），保留回复，但不推断被回应内容。
> 4. 花絮章节：若互动的核心发言来自被隐藏消息，整条跳过。

### 5.5 泄漏检测（硬闸门）

`renderer.render_public(...)` 输出的 Markdown、在写入公开仓库之前，`privacy.leak_check(...)` 做全文扫描：

- 对所有 `contact_map.all_nicknames()` 的真实昵称做 `str.find`，任意命中即抛 `LeakDetected`。
- 对所有 opted-out 用户的 `default_anon` 也做同样扫描（它们不应出现在公开版）。
- 异常落盘到 `debug/leak-YYYY-MM-DD.json`，公开版发布中止；**群内版不受影响**。

---

## 6. 中间 JSON 与渲染

### 6.1 中间 JSON Schema（`DailyReport`）

```jsonc
{
  "date": "2026-04-17",
  "intro": "今天群里讨论了 ...（使用 token 指代群友）",
  "sections": [
    {
      "type": "news",             // news | tool | methodology | anecdote
      "title": "某款新模型发布",
      "body": "要点 1；要点 2；...",
      "comments": [
        { "token": "沉稳的大象07", "text": "这个模型在长上下文场景下明显更稳" }
      ],
      "tags": ["model-release", "long-context"],

      // 公开适宜性评估（适用于所有 section type）
      "public_safe": true,
      "public_safe_reason": null
    },
    {
      "type": "anecdote",
      "title": "某个有趣的互动",
      "body": "事情经过（只用 token）",
      "comments": [],
      "tags": [],
      "public_safe": false,
      "public_safe_reason": "笑点依赖当事人具体身份，换成匿名 token 后失去笑点"
    }
  ]
}
```

关键字段：

- **没有 TOC 字段**：目录由渲染器自动插入 `[TOC]` 标记。
- **`comments` 里只存 token**：不存 wxid。
- **`public_safe` 覆盖所有 section type**（Q3 决议）：见 §6.2。
- **`tags` 用于后续聚合**：写入 Jekyll front matter 供主题消费。

### 6.2 `public_safe` 判定标准（Q3 决议）

Claude 在提取时对每个 section 自评 `public_safe`。**应标记为 `false` 的情形**：

1. **隐私顾虑**：内容涉及可与群外信息交叉识别的私人线索（职业、地点、独特经历），即便已匿名化也可能推断出具体个人。
2. **Opt-out 波及**：section 的核心依赖某位 opted-out 群友的发言，即使占位符已遮蔽，剩余上下文仍可能让人推知被隐藏内容。
3. **公众环境中的风险**：内容在公开互联网语境下可能引起误解、争议、或对当事人 / 相关方产生负面影响（如涉及第三方的评价、敏感话题的玩笑、可能被断章取义的观点）。

**应标记为 `true` 的情形**：信息点本身中立、客观、公开讨论，匿名化后依然成立。

公开版渲染时，`public_safe: false` 的 section **整条跳过**。

Prompt 中给 Claude 的判定指引：

> 你的任务不是过度审查，而是为群友在公开环境的形象负责。默认 `public_safe = true`；只在明显命中上述三类之一时标 `false`，并在 `public_safe_reason` 简要说明原因。当拿不准时，选 `false`。

### 6.3 Claude 调用方式

用 Anthropic SDK 的 **tool use**：把 JSON schema 作为工具定义传入，强制结构化返回。调用结构：

- `system`：任务说明 + 隐私占位符约定 + `public_safe` 判定标准。
- `user`：token 化后的 chat_history 纯文本。

失败时（schema 校验不通过）自动补救重试一次；仍失败则中止当天并保留原始响应到 `debug/`。

### 6.4 渲染器

**`render_group(report, alias_db, contact_map) -> str`**：

- token → `contact_map.by_wxid(alias_db.wxid_of_token(token))` 真实备注名。
- 所有 section 全部渲染（忽略 `public_safe` 字段）。
- 底部追加「指令执行日志」章节（见 §6.5）。

**`render_public(report, alias_db) -> str`**：

- token → `public_alias or default_anon`。
- 跳过所有 `public_safe: false` 的 section。
- 顶部生成 Jekyll front matter（见 §7.3）。
- 不包含「指令执行日志」章节。

两者共享内部 `_render_sections(sections, token_resolver, *, filter_unsafe)`，差异只在两个参数上。

### 6.5 群内版的「指令执行日志」章节（Q8 决议）

**每期固定追加**，含三部分：

```markdown
## 本期指令执行记录

### 今日生效指令
- 10:12  <真实昵称>：设置公开别名为「Duckie」  ✓
- 11:03  <真实昵称>：申请 optout，后续发言将从公开版移除  ✓
- 12:45  <真实昵称>：尝试设置公开别名「Duckie」  ✗ 已被占用
- （若无指令则显示：今日无指令）

### 可用指令说明
- `/alias <名字>`：设置在公开版日报中的显示别名。长度 1–16 字符，支持中英文/数字/`_`/`-`/`·`。
- `/alias`：清空别名，恢复默认匿名名。旧名释放后 30 天内其他人不可占用。
- `/optout`：不参与公开版。后续发言将从公开版完全移除，其他群友对你的引用也会被遮蔽。
- `/optin`：重新参与公开版（此前已发布日报不会自动补回）。

### 规则提示
- 所有指令需**单独成行**发送；行尾多余内容会被忽略。
- 指令不会实时回复，在下一份日报中统一生效并公布执行结果。
- 若设置的别名与他人冲突，**先到先得**；被拒绝的指令会显示在本章节。
```

---

## 7. 公开发布（GitHub Pages via Jekyll）

### 7.1 仓库与主题（Q6、Q7 决议）

- **内容仓库**：`git@github.com:LouYu2015/AI-chatgroup-daily.git`（已存在）。
- **静态生成**：GitHub Pages 内置的 Jekyll。
- **主题**：[**Chirpy**](https://github.com/cotes2020/jekyll-theme-chirpy)（通过 `remote_theme: cotes2020/jekyll-theme-chirpy` 集成，不需 vendor）。选择理由：
  - 响应式、现代视觉，中英文混排排版好。
  - 支持暗色模式、Tab 键切换。
  - 自带站内搜索（无需第三方服务）。
  - 侧边栏 TOC，对长日报友好。
  - 原生支持 `categories` 与 `tags`，契合本项目的 `tools`/`topics` 聚合需求。
  - RSS 开箱即用。

`README.md` 与 `LICENSE` 由作者**手动**在公开仓库预先提交。发布程序只管写 `_posts/` 目录。

### 7.2 公开仓库初始化（手动步骤，代码实现完成后执行）

作者需在公开仓库 `git@github.com:LouYu2015/AI-chatgroup-daily.git` 预先提交：

1. **`LICENSE`**：CC BY-NC 4.0 全文（从 https://creativecommons.org/licenses/by-nc/4.0/legalcode 获取中英双语版本）。
2. **`README.md`**：项目简介 + 指令说明（`/alias` `/optout` `/optin`） + 隐私模型说明 + 联系方式。
3. **`_config.yml`**：Jekyll + Chirpy 配置（见 §7.4）。
4. **`index.html`**：Chirpy 主页模板。
5. **`_tabs/`**：Chirpy 所需的 About / Categories / Tags / Archives 等导航页（按官方模板）。
6. **`.github/workflows/pages-deploy.yml`**：GitHub Pages 构建工作流（Chirpy 官方模板）。
7. **`assets/`**：favicon 等（按需）。

这一步完成后，`publisher` 只负责写入 `_posts/` 下的 Markdown 文件。

### 7.3 目录结构（公开仓库内）

```
AI-chatgroup-daily/
├── README.md
├── LICENSE
├── _config.yml
├── index.html
├── _tabs/
│   ├── about.md
│   ├── categories.md
│   ├── tags.md
│   └── archives.md
├── _posts/
│   └── 2026/
│       └── 04/
│           └── 2026-04-17-daily.md    # Jekyll 接受嵌套目录
├── aliases.md                          # 选填：公开别名目录
├── assets/
└── .github/workflows/pages-deploy.yml
```

### 7.4 Jekyll Front Matter

```yaml
---
title: "某日导读：X 事件、Y 工具、Z 方法论"
date: 2026-04-17 12:00:00 +0800
categories: [Daily]
tags: [model-release, agent, context-curation]
layout: post
toc: true
license: CC BY-NC 4.0
---
```

### 7.5 `_config.yml` 关键字段（作者手动写入）

```yaml
title: AI 群聊日报
tagline: 每日 AI 技术讨论精选（匿名化公开版）
url: https://louyu2015.github.io
baseurl: /AI-chatgroup-daily

remote_theme: cotes2020/jekyll-theme-chirpy
plugins:
  - jekyll-feed
  - jekyll-seo-tag
  - jekyll-paginate

lang: zh-CN
timezone: Asia/Shanghai

defaults:
  - scope: { path: "", type: posts }
    values:
      layout: post
      toc: true
      comments: false       # 初期关闭评论，避免外部评论系统引入依赖
```

### 7.6 发布流程

每次 `cli` 跑完一天的日报：

1. **写入 Markdown**：`publisher.write_post(date, markdown)` 在本地 `_posts/YYYY/MM/YYYY-MM-DD-daily.md`。
2. **本地 commit**：`publisher.commit(date)` 执行
   - `git add _posts/YYYY/MM/YYYY-MM-DD-daily.md`
   - `git commit -m "Add daily report for YYYY-MM-DD"`
   - **不签 Co-Authored-By**（Claude 是工具）。
3. **生成本地 HTML 预览**：`publisher.preview(date)` 用 `markdown` 库生成独立 HTML 到 `debug/preview-YYYY-MM-DD.html` 并 `open` 打开。预览只是「粗略看看」，不跑完整 Jekyll 构建。
4. **延迟推送**：本次运行不 push，只打印：
   ```
   公开版已本地 commit（未推送）：_posts/2026/04/2026-04-17-daily.md
   下次运行带 -y 可推送到 GitHub，GitHub Pages 将自动构建。
   ```

下次 `cli.main(-y)`：

1. `publisher.push_pending()` 检查 `origin/main` 是否有未推送 commit，若有则 `git push`。
2. 之后正常继续当天/补漏流程。

**不变量**：`-y` 只负责推送上次攒下的本地 commit；不跳过任何检查，不绕过泄漏检测。

### 7.7 撤回工具（`scripts/redact.py`）

```bash
python3 -m wechat_daily.redact --wxid <wxid> --from 2026-04-01 --to 2026-04-14
```

流程：

1. `aliases.json` 中将该 wxid 的 `optout` 设为 `true`。
2. 对范围内每天，从 `debug/extract-YYYY-MM-DD.json` 缓存的中间 JSON 重跑「阶段 2b」（不重新调用 LLM）。
3. 覆盖公开仓库对应 Markdown，commit message 为 `Redact user on request: YYYY-MM-DD`。
4. 同样要 `-y` 才推送。

依赖 `llm_extractor` 每次成功调用后把「输入 tokens + system prompt + 返回 JSON」写入 `debug/extract-YYYY-MM-DD.json`。

---

## 8. 单元测试策略

### 8.1 优先级（按出错代价从高到低）

1. **`privacy.py`**：泄漏检测、token 长度降序替换、引用块遮蔽、optout 合并。**必须达到 100% 分支覆盖**。
2. **`aliases.py`**：指令解析（含 `/alias` 无参数、超长昵称、占用别名、表情符号、30 天预留期边界）、备份轮转、默认匿名稳定性、盐生成与加载。
3. **`renderer.py`**：固定 JSON + 固定 `alias_db` 的 snapshot test，两个版本都校验；验证 `public_safe: false` 的 section 在公开版确实被跳过。
4. **`message_parser.py`**：每种 `_MSG_*` 类型至少一个合成 blob fixture。
5. **`archiver.py`**：模拟不同日期下的 7 天滚动归档。
6. **`publisher.py`**：用临时目录 + 本地 `git init --bare` 作为假 remote，跑完整 commit/push 流程。

### 8.2 Mock 策略

- **数据库**：`tests/fixtures/` 提供**明文** SQLite 合成文件；`wechat_db.py` 接受 `cipher_key=None` 走普通 `sqlite3`，方便测试。
- **Claude API**：`llm_extractor` 接受 `client` 注入，测试传入返回预录 JSON 的 stub。`tests/fixtures/extract-sample.json` 保存样本。
- **联系人映射**：`ContactMap` 支持从 dict 构造。
- **时间**：所有 `datetime.now()` 通过 `clock: Callable[[], datetime]` 参数注入，测试中固定。

### 8.3 集成测试

`tests/test_end_to_end.py`：合成 3 天聊天 + 3 个合成用户（1 个 optout、1 个 public_alias、1 个默认匿名），跑完整管线，断言：

- 群内版含所有 3 个真实备注名。
- 公开版不含任何真实备注名（泄漏检测通过）。
- Optout 用户的 `default_anon` 也不出现在公开版。
- 两版章节序列一致（除了 `public_safe: false` 过滤差异与指令日志）。
- 群内版末尾指令日志包含「可用指令说明」与「规则提示」两个子章节。

---

## 9. 决议记录（原待确认问题）

| # | 问题 | 决议 |
|---|---|---|
| Q1 | 默认匿名名风格 | **形容词+动物+两位数字**，派生函数加 32 字节随机盐（`data/anon_salt.txt`，gitignored，首次运行生成），防逆向。 |
| Q2 | 别名 DB 备份范围 | **仅本地 30 天快照**（`data/aliases.backup/`），`data/` 整目录 gitignored。 |
| Q3 | 公开版内容过滤 | Claude 对**每个 section** 自评 `public_safe`，依据三类标准：隐私、opt-out 波及、公众环境风险。`public_safe: false` 的 section 整条跳过。 |
| Q4 | `public_alias` 冲突 | **先到先得 + 释放后 30 天预留**。原主本人可在预留期内回收。`alias_reservations` 字段追踪。 |
| Q5 | `/alias` 扫描起点 | **增量 + `data/aliases.cursor`**；`scripts/rebuild_aliases.py` 兜底。 |
| Q6 | 公开仓库初始化 | **手动**在公开仓库预先提交 `README.md` / `LICENSE` / `_config.yml` / `_tabs/` / workflow 等。代码实现完成后由作者执行。 |
| Q7 | 公开发布形式 | **只发 GitHub Pages（Jekyll）**，不发 PDF。主题选 **Chirpy**（`remote_theme`）。 |
| Q8 | 指令执行日志 | **每期固定追加**，含「今日生效指令」「可用指令说明」「规则提示」三个子章节。 |

---

## 10. 实现路线图

分 5 个阶段，每阶段独立可运行：

### Phase 0：模块化拆分（不改变行为）

- 把 `main.py` 拆成 §3 的模块，**不引入新逻辑**。
- 现有日报生成链路继续跑通，手动发群不变。
- 补齐 `message_parser` / `archiver` / `chat_extractor` 的单测。

**验收**：`python main.py` 输出与 Phase 0 前逐字节一致。

### Phase 1：别名数据库与指令扫描

- 实现 `aliases.py`（默认匿名+盐、30 天备份、指令扫描、冲突处理、30 天预留）。
- `scripts/rebuild_aliases.py`。
- `cli` 主流程前调用指令扫描；产物暂不消费。

**验收**：合成 fixture 下 `test_aliases.py` 全绿；真实运行产出 `data/aliases.json` 与人工预期一致。

### Phase 2：隐私管线与结构化提取

- 实现 `privacy.py`（token 化、optout 遮蔽、引用块遮蔽、泄漏检测）。
- 改写 `llm_extractor`：从 Markdown 生成切到 JSON 结构化提取（tool use）。
- 新增 `renderer.render_group(...)`；输出与 Phase 0 在语义上一致（不再逐字节）。
- 群内 PDF 继续走现有 `archiver`。
- 群内版末尾新增「指令执行日志」章节（Q8）。

**验收**：端到端跑通，群内 PDF 正常，`debug/extract-*.json` 落盘。

### Phase 3：公开版渲染与发布

- 实现 `renderer.render_public(...)` 含 Jekyll front matter + `public_safe` 过滤。
- 实现 `publisher.py`（clone、commit、HTML 预览、`-y` push）。
- 接入泄漏检测硬闸门。
- 本阶段开始前，作者需完成公开仓库的 §7.2 手动初始化。

**验收**：真实跑一天，`_posts/` 下 Markdown 生成正常，本地浏览器预览，匿名映射 & `public_safe` 过滤正确。人工审核后带 `-y` 推送，GitHub Pages 构建成功、站点可访问。

### Phase 4：撤回工具与运维

- `scripts/redact.py` 基于缓存中间 JSON 的重渲染。
- 公开仓库 `aliases.md` 自动生成（L2 自愿展示别名）。
- 回溯历史日报批量处理（如果决定回补）。

**验收**：任选 wxid + 日期区间跑 `redact`，对应 Markdown 被覆盖，`git log` 可见明确的撤回 commit。

---

## 11. 风险与缓解

| 风险 | 缓解 |
|---|---|
| Claude 幻觉出真实名字（即便输入只含 token） | 泄漏检测硬闸门；命中则中止公开版发布，写 `debug/leak-*.json` 供人工复核。 |
| `public_safe` 自评偏宽松 | Prompt 明确「拿不准时选 false」；早期观察期可临时全局把 `public_safe` 判定阈值调严（例如 anecdote 类型默认 false）。 |
| Claude 返回 JSON 不符合 schema | `pydantic` 严格校验，失败自动补救重试一次，仍失败则中止当天并保留原始响应。 |
| 公开仓库 push 失败 | `publisher.push_pending()` 失败时保留本地 commit，下次 `-y` 重试，不吞异常。 |
| 30 天备份意外损坏 | 加载失败时自动回退到最近可解析的备份，控制台高亮提示。 |
| 盐文件丢失 | 盐只影响新入群用户；老用户的 `default_anon` 以 `aliases.json` 为权威值；备份路径 `data/aliases.backup/anon_salt.txt` 冗余。 |
| 某人滥用 `/alias` 频繁改名 | `alias_reservations` 让每次改名都触发 30 天预留，自然抑制滥用。 |
| `/alias` 指令扫描漏读 | `rebuild_aliases.py` 可随时从零重放；cursor 只是性能优化。 |
| 群友事后不满意匿名化效果 | `/optout` + `scripts/redact.py` 支持历史撤回。 |
| Jekyll/Chirpy 构建失败 | GitHub Actions 失败不会破坏现有站点；本地预览可提前发现前 matter 格式问题。 |

---

## 12. 附：与当前代码的对应

| 当前 `main.py` 的函数 / 区域 | 去处 |
|---|---|
| API key 加载（`load_or_prompt_api_keys`） | `config.py` |
| `_get_conn` / `sqlcipher3` 相关 | `wechat_db.py` |
| `_parse_sender_content`、`_format_quoted`、`_MSG_*` 常量 | `message_parser.py` |
| `contact_map` 构建 | `contacts.py` |
| 按日期切片取消息 | `chat_extractor.py` |
| `find_missing_dates` | `chat_extractor.py`（保持原语义） |
| `SUMMARY_PROMPT` | 拆成 `llm_extractor.py` 的「提取 prompt」+ `renderer.py` 的章节模板 |
| `generate_report_with_claude` | `llm_extractor.py`（结构化提取） |
| Markdown → PDF | `pdf.py`（仅群内版） |
| 归档 7 天滚动 | `archiver.py` |
| `main()` 主流程 | `cli.py` |

所有现有常量（`WECHAT_DATA_DIR`、`GROUP_CHAT_ID`、`GROUP_TABLE` 等）搬家到 `config.py`；不在版本库硬编码的字段（如 `GROUP_CHAT_ID`）改为从 `.env` 读取，`.env.example` 给占位。
