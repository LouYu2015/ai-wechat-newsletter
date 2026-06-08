---
name: write-article
description: 写群聊日报/文章的工作流——用 query_chatlog.py 从微信群聊记录里捞素材，写成 Markdown 草稿，再用 ask_deepseek.py 调 DeepSeek 润色。当用户要「写日报」「写文章」「根据群聊记录起草稿子」「润色这篇稿子」时使用。配套发布用 wechat-publish skill。
---

# 写文章（群聊日报）工作流

从微信群聊记录起草一篇文章/日报，分三步：**取素材 → 写草稿 → 润色**。
两个核心脚本都在 `scripts/` 下，仅用项目内依赖，先激活 venv：

```bash
source .venv/bin/activate
```

> 路径以项目根目录为基准（`scripts/query_chatlog.py`、`scripts/ask_deepseek.py`）

---

## 1. 取素材 —— `scripts/query_chatlog.py`

从群聊数据库导出**已匿名化**的纯文本聊天记录到 stdout（发送者和 @提及都按项目匿名机制替换；optout 用户按日报逻辑隐藏）。运行信息（命中数、图片目录）打到 stderr。

```bash
# 某个时间段的全部消息
python scripts/query_chatlog.py --since 2026-06-01 --until 2026-06-05

# 关键词检索，命中处附带前后各 2 条上下文，最多 50 条命中
python scripts/query_chatlog.py --keyword 显卡 --context 2 --limit 50

# 带精确时刻；解码图片到临时目录并在文本里嵌入图片路径
python scripts/query_chatlog.py --since "2026-06-04 18:00" --decode-images
```

参数速查：

| 参数 | 含义 |
|---|---|
| `--since TIME` | 起始（含）。`YYYY-MM-DD` 或 `'YYYY-MM-DD HH:MM'`；不填不限 |
| `--until TIME` | 结束。`YYYY-MM-DD` 视为含当天；不填不限 |
| `--keyword KW` | 对正文/引用做大小写不敏感子串匹配 |
| `--context N` | 关键词命中时附带前后各 N 条（默认 0） |
| `--limit N` | 取最新 N 条（默认 20；`0` 不限）。有关键词时作用于命中数 |
| `--decode-images` | 解码图片到临时目录，文本中嵌入路径 |
| `--image-dir DIR` | 指定图片输出目录（隐含 `--decode-images`） |

**典型用法**：先 `--since/--until` 圈一天的范围通读，再用 `--keyword` 把某个话题的上下文捞全，把要点整理进草稿。

**捞原话的纪律（重要）**：
- 文章里 `>` 引用框必须是群友**原话**。`data/public_repo/_posts` 下的日报是**二次转述/概括**，不能当原话引用——一定回 `query_chatlog.py --keyword` 把原始那句捞出来核对，原话往往比日报版本更生猛、更好用。
- 匿名器有个**夹字 bug**：它偶尔把原文里的普通词误当昵称包进 `⟨⟩`，比如 `豁达的猎豹⟨原子弹⟩`＝原文的"原子弹"、`坦荡的老虎⟨X。⟩`＝"CodeX。"。引用前要逐句**还原**，不能直接粘。
- 关键证据常藏在**图片**里（System Card 截图、跑分图等）：用 `--decode-images --image-dir /tmp/xxx` 解码，再用 Read 工具读图取证；这类截图（纯文字、无敏感信息）可直接做正文配图。

## 2. 写草稿

把素材整理成 Markdown，存到 `data/draft/<名字>.md`。
排版/标记约定（@提及药丸、引用框、脚注等）见 `wechat-publish` skill 的「markdown 约定」表——按那套写，后续才能直接发公众号。

## 3. 润色 —— `scripts/ask_deepseek.py`

命令行调 DeepSeek（OpenAI 兼容接口），读 `.env` 里的 `DEEPSEEK_API_KEY`。默认**流式**输出（边生成边打印）；回复到 stdout，usage 到 stderr。仅用标准库。

```bash
# 直接传 prompt
python scripts/ask_deepseek.py "用更口语的语气润色这段话：……"

# 从 stdin 读整篇（适合管道/长文）——润色但不改事实
cat data/draft/today.md | python scripts/ask_deepseek.py \
    --system "你是中文编辑，润色文字但不改变事实与数据，保留 Markdown 结构" -

# 关闭流式、换经济款模型
python scripts/ask_deepseek.py --model deepseek-v4-flash --no-stream "……"
```

参数速查：

| 参数 | 含义 |
|---|---|
| `prompt` | 用户消息；传 `-` 或省略则从 stdin 读 |
| `--system TEXT` | system prompt（可选），用来固定「编辑」角色和约束 |
| `--model NAME` | 默认 `deepseek-v4-pro`（旗舰）；经济快速款 `deepseek-v4-flash` |
| `--temperature F` | 采样温度，默认 `1.0` |
| `--no-stream` | 一次性返回（默认流式） |

**润色注意**：
- 润色长稿走 stdin 管道，避免命令行转义麻烦：把草稿 `cat` 进去，输出重定向回新文件人工核对后再覆盖。
- 模型名 `deepseek-chat`/`deepseek-reasoner` 是旧别名（2026/07/24 弃用），统一用 `deepseek-v4-*`。
- DeepSeek 的中文文风比 Claude 地道，**值得专门用它润色**；但它"手很贱"，必须用 `--system` 把禁区锁死。验证过好用的整篇润色系统提示词配方：

  > 你是顶级中文公众号编辑，把带 AI 翻译腔的中文改得地道、凝练、有口语节奏。**只润色叙述性文字**，以下一字不能改：① 所有 `>` 引用框内容（群友原话+英文原文，逐字保留含口语错别字）；② 所有英文原文；③ 所有数字/百分比/金额/版本号；④ 所有人名机构名产品名；⑤ 所有 `@昵称`（连反引号）；⑥ 所有 markdown 结构（标题/引用/加粗/列表/分割线/参考链接整段）。不新增不删改事实与论断。去翻译腔四病：物理动作动词抽象挪用、形容词加冒号起手式、抽象名词做主语、有地道中文却硬留的英文。直接输出完整 markdown，无前言。

- **DeepSeek 润色的两个坑**：① 它常把结尾整段（如参考链接 [4][5]）**吞掉**——跑完一定 diff 头尾、补回丢失内容；② 它会把直引号 `"` 换成弯引号 `""`（公众号里 OK，不用回改）。
- **定点改写 / 审中文比喻**：句子不通顺、或拿不准某个比喻/成语用得对不对时，**单独**喂给 DeepSeek 问（一事一议，省 context），让它给 2-4 个版本或判断"可保留/要改"。这次实测它能准确指出"盲人摸象配拼图是混用比喻""跳过≠removed 应译移除""带进棺材的悬案语义冗余"等问题。

---

## 写「群友实测」深度稿的额外约定

「群友实测」是**单话题深挖**（区别于日报的流水账、周报的三话题结构），参考已发布的 `data/draft/群友实测01-*.md`、`群友实测02-*.md`。要点：

- **结构**：钩子 → 起承转合（群里体感 → 反调/悬念 → 外部硬证据破案 → 我的判断 → 实用启示）→ 总结。第一人称"我"通常是**旁观整理者**，不要冒认别人（如群里某人）做过的事（读 System Card、写解读等）。
- **外部背书**：用 WebSearch/WebFetch 给群里的体感找外部印证（官方文档、Zvi 等评测博主、benchmark），差异化就在"群内原声 × 外部硬料逐条对上"。引用外国人名要**加一句身份背景**（如"Figma 的 CEO Dylan Field""前 OpenAI 研究员 Nick Cammarata"），公众号读者不认识他们。
- **加粗克制**：每节只留 1-2 句"想截图"的关键判断加粗，术语高亮/零碎强调一律不加粗——加粗太密会稀释重点。
- **昵称用全称**：统一 `@洒脱的鸳鸯`，不要简写成"鸳鸯"；正文人物若有真名梗（如"鸭哥"）按匿名要求替换成日报里的 AI 昵称。
- **实用启示按信息量给，不凑数**（3-4 条有用的即可）。
- **首图**：本系列用吉祥物做一个贴主题的动作（DeepSeek=鲸鱼；Claude 的吉祥物是 **Clawd**，一只橙色 8-bit 像素螃蟹）。Claude 给概念描述 + 生图提示词，用户自己用 Nano Banana Pro / ChatGPT 生成。
- 排版/发布走 `wechat-publish` skill。

## 完整流程示例

```bash
source .venv/bin/activate

# 1. 捞当天素材
python scripts/query_chatlog.py --since 2026-06-06 --until 2026-06-06 > /tmp/raw.txt

# 2. （Claude 据 /tmp/raw.txt 整理草稿，写到 data/draft/2026-06-06.md）

# 3. 润色
cat data/draft/2026-06-06.md | python scripts/ask_deepseek.py \
    --system "你是中文编辑，润色文字但不改事实，保留 Markdown 结构" - \
    > data/draft/2026-06-06.polished.md

# 4. 人工核对 polished 稿，确认后用它发布（见 wechat-publish skill）
```
