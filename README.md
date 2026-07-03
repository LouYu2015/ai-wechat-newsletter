# WeChat 群聊日报生成器

自动从微信群聊记录生成每日日报：群内版（真实昵称 + PDF）与公开版（匿名化 + GitHub Pages）。

## 功能概览

- **双版本产出**：群内版保留真实昵称，公开版经过三级隐私处理后发布到 GitHub Pages
- **Markdown 提取**：用 Claude Fable 从聊天记录中流式生成 Markdown 日报；公开/内部版本通过后期处理分流
- **模型 AB 对比**：主版本（Fable 5，发布 + 喂续写）跑完后，再用 Opus 4.6 旁路生成一份对比日报（仅本地 PDF/debug，不发布、不喂续写），便于并排比质量与成本
- **链接摘要**：用 DeepSeek V4 Pro（关 thinking）抓取并摘要群内分享的链接，作为 `[网页摘要]` 喂给报告生成；两版日报共用同一批摘要
- **三级隐私模型**：`/optout`（不出现）/ 默认匿名（稳定派生）/ `/alias`（自定义公开别名）
- **泄漏检测**：公开版发布前，用 Claude Haiku 二次确认真实昵称是否为人名引用
- **7 天滚动归档**：超过 7 天的 PDF 自动整理到 `archive/YYYY/MM/` 子目录

## 环境要求

- Python 3.11+
- `chatlog-mac/keys.json`（微信数据库解密密钥，不随代码发布）
- 系统级 CLI 依赖（非 pip 包）：
  - `zstd` —— 解压消息内容（`message_parser.decompress`），**必需**
  - `ffmpeg` —— 解码 wxgf/HEVC 格式图片首帧，仅图片解码时需要

```bash
brew install zstd ffmpeg     # macOS

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## API Key 配置

首次运行时会提示输入，自动保存到 `.env`；也可手动创建：

```
ANTHROPIC_API_KEY=your_anthropic_api_key_here
DEEPSEEK_API_KEY=your_deepseek_api_key_here   # claude 路径需要（链接摘要 + 对比版日报）
GEMINI_API_KEY=your_gemini_api_key_here       # 仅 --summary gemini 时需要
```

## 配置目标群聊

编辑 `wechat_daily/config.py`，修改以下两个常量：

```python
GROUP_CHAT_ID = "26389512912@chatroom"
GROUP_TABLE   = "Msg_1f5cd6985e2d31687fc076061b1fa6da"
```

### 查找 GROUP_CHAT_ID

```bash
sqlite3 ~/Documents/chatlog/contact/contact.db \
  "SELECT username, nick_name FROM contact WHERE nick_name LIKE '%群名关键字%';"
```

输出示例：`26389512912@chatroom|AI生产力训练营`，`username` 即为 `GROUP_CHAT_ID`。

### 计算 GROUP_TABLE

```bash
echo -n "26389512912@chatroom" | md5
# 输出：1f5cd6985e2d31687fc076061b1fa6da
# GROUP_TABLE = "Msg_" + 上面的 MD5
```

## 使用方法

```bash
source .venv/bin/activate

# 生成缺失日报（默认走 Batch API：全部 token 5 折，无流式预览，
# 通常几分钟到几十分钟完成；主版 + 对比版同批提交）
python3 main.py

# 生成后推送公开版到 GitHub Pages
python3 main.py -y

# 也为当天不完整日期生成日报
python3 main.py --allow-incomplete

# 不用批量，走传统流式生成（有实时预览，标准价计费）
python3 main.py --no-batch
```

**`-y` 标志**：推送上一次运行生成的本地 commit 到公开仓库。本次生成的 commit 下次带 `-y` 才推送，留出人工审核窗口。

### 批量模式的断点续接

批次提交后会把 `batch_state.json`（含 `version` 字段）写进当天的 debug
目录，因此**电脑休眠、Ctrl-C、进程崩溃都不丢批次**——重新运行同一命令即可
续接（自动跳过链接增强，不重复花 DeepSeek 的钱）。相关行为：

- 消息集合与提交时不一致（典型场景：`--allow-incomplete` 中途退出后重跑，
  群里又进了新消息）会提示「当时 N 条 → 现在 M 条」并询问续接还是重提；
- `--resume` 无条件续接、`--resubmit` 放弃旧批次（尽力取消）重新提交，
  两者跳过询问；
- 已取回过结果的批次重跑时可选「复用结果」（服务端保留 29 天，重取免费）；
- 提交时的完整输入（含图片与链接摘要）快照为 `batch_content.json`，
  批次消费后自动删除；纯文本审计版 `batch_input.txt` 长期保留。

## 输出文件

| 路径 | 说明 |
|------|------|
| `archive/YYYY-MM-DD 群聊日报.pdf` | 主版本（Fable 5）群内版 PDF（真实昵称） |
| `debug/YYYY/MM/DD/group.md` | 主版本群内版 Markdown 原文 |
| `debug/YYYY/MM/DD/extract.{md,input.txt,thinking.md}` | 主版本原始 Markdown 日报（用作下日续写素材）+ 输入快照 + thinking 摘要 |
| `archive/YYYY-MM-DD 群聊日报 (opus-4-6).pdf` | **对比版**（Opus 4.6）群内版 PDF（仅本地，不发布、不喂续写） |
| `debug/YYYY/MM/DD/group.opus-4-6.md` | 对比版群内版 Markdown 原文 |
| `debug/YYYY/MM/DD/extract.opus-4-6.{md,input.txt,thinking.md}` | 对比版原始 Markdown + 输入快照 + thinking 摘要 |
| `debug/YYYY/MM/DD/batch_state.json` | 批量模式批次状态（断点续接凭据，含 schema version） |
| `debug/YYYY/MM/DD/batch_input.txt` | 批量模式提交输入的纯文本审计快照 |
| `debug/YYYY/MM/DD/batch_content.json` | 提交输入的完整块列表快照（含图片；批次消费后自动删除） |
| `debug/costs.jsonl` | 每次模型调用的 token 用量 + 价格估算（JSON Lines，批量调用带 `batch` 标记） |
| `data/public_repo/_posts/` | 公开版 Jekyll Markdown（本地 commit，待推送） |

每次跑完会在终端打出按 (日期, 阶段, 模型) 聚合的成本汇总表，阶段含 `link`（DeepSeek 链接摘要）/ `extract`（Fable 5 主版本）/ `extract-compare`（Opus 4.6 对比版），批量调用的模型列标注「(batch 5折)」，可直接并排比成本。对比版生成失败不影响主版本。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

测试是 hermetic 的：不读 `.env`、不连数据库、不调 API；`tests/conftest.py` 把 git 子进程从宿主全局配置里隔离开。本地或 CI 都按上面这条命令跑即可。

## 代码检查（ruff）

Lint 工具用 [ruff](https://docs.astral.sh/ruff/)，配置在仓库根的 `ruff.toml`
（规则集：pyflakes `F` + pycodestyle `E`/`W` + isort `I`，不启用 E501 行宽和
自动格式化）。安装与调用：

```bash
.venv/bin/pip install -r requirements-dev.txt

# 检查全仓（CI / 提交前跑这条）
.venv/bin/ruff check .

# 自动修复可安全修复的问题（未用 import、import 排序等）
.venv/bin/ruff check --fix .
```

import 的规范（Google Python 风格；`I` / `TID252` 由 ruff 强制，模块化
风格靠约定与 review 维持）：

- `from __future__ import annotations` 最前；随后三组、组间空行——
  **标准库 → 第三方 → 本项目**，组内按字母序；import 之间不夹代码；
- **一律绝对导入**：禁止 `from .config import …`（TID252 会报错并可
  `--fix` 自动转换）；
- **只 import 模块，不 import 名字**：写 `from wechat_daily import config`
  + `config.CLAUDE_MODEL`，`import datetime` + `datetime.datetime`，
  `import rich.progress` + `rich.progress.Progress`——调用点自带来源，
  测试 monkeypatch 只需 patch 定义处一份（如 `wechat_daily.config.X`）。
  例外：`typing` / `collections.abc` 的名字可直接导入（Google 惯例）；
  `from x import y` 当 `y` 本身是模块时合法（如 `from PIL import Image`）；
  命名冲突可用别名（如 `import markdown as md_lib`，因函数参数占了
  `markdown` 这个名字）；
- 函数体内的延迟 import（如 `import anthropic`）是刻意的启动加速。

### Claude Code 云端环境备注

云端 sandbox 启动时**没有 venv**，系统 Python 也**只有标准库**。直接 `python3 -m pytest tests/` 会在 collect 阶段炸：`test_llm_extractor.py` 和 `test_url_enricher.py` 都需要 `httpx`，没装就 `ModuleNotFoundError`（这两个文件之外的纯 Python 测试 collect 不到 httpx，看着像能跑，但等于跳过了 1/4 的覆盖）。

跑全套的标准动作：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

`pytest` / `ruff` 等 dev-only 工具在 `requirements-dev.txt` 里，与运行时依赖分开。`weasyprint` 会拖一堆系统级字体/lxml 依赖、`sqlcipher3` 会编 C 扩展，首次安装 ~1–2 分钟，之后 `.venv/` 复用即可。装完整套测试秒级跑完。

不要用系统 Python `pip install`——sandbox 里它会落到 `/usr/local`，下次重启 sandbox 就丢了，而且会污染全局环境。venv 装在工作目录下，下次进同一个工作目录直接复用。

## 容器开发沙箱（dev.sh）

在隔离的 Docker 容器里跑本项目与 Claude Code：**只读**挂载微信加密数据库、**读写**挂载项目目录。完整说明见 [`docker/README.md`](docker/README.md)。

```bash
./dev.sh             # 构建/复用镜像并进入 Claude Code（默认 YOLO 模式）
./dev.sh bash        # 进入 shell 调试
./dev.sh --continue  # 以 - 开头的参数透传给 claude（如 --continue / --resume）
REBUILD=1 ./dev.sh   # 不带缓存重建镜像
```

容器内干活前需要知道的关键事实：

- **微信库只读**：宿主机 `~/Library/Containers/com.tencent.xinWeChat/.../xwechat_files` 以 `:ro` 挂到容器内**同路径**（`$HOME=/root` 下）。`wechat_daily/config.py` 用 `Path.home()` 定位，故挂载点必须落在 `$HOME`；改不动原始库。
- **⚠️ 容器读到的库可能比宿主机陈旧（甚至差数小时）**：Docker Desktop on macOS 的绑定挂载（gRPC-FUSE/VirtioFS）存在已知缓存不一致——**宿主机写入文件后，容器内未必能看到更新**（[docker/for-mac#4861](https://github.com/docker/for-mac/issues/4861)、[#7274](https://github.com/docker/for-mac/issues/7274)）。微信在宿主机持续写库，但容器经只读绑定挂载读到的可能是一份被缓存冻住的旧版本。实测出现过「宿主机已读到 21:25 的消息、19 分钟后启动的容器却只读到 17:46」。**这与时区、解密、`immutable` 都无关，是 Docker 文件共享层的缓存问题。** 要可靠拿到最新聊天记录，请**在宿主机跑**；容器适合改代码 / 跑测试 / 构建，不适合依赖「当下最新」的聊天数据。
- **⚠️ 容器时钟是 UTC，宿主机是本地时区（如西雅图 PDT）**：数据库里存的是绝对 epoch，内容本身无歧义；但容器内 `datetime.fromtimestamp()` / `strptime(...).timestamp()` 走的是 **UTC** 本地时钟，与日报里显示的 `[HH:MM]`、`config.py` 里按**宿主机本地时区**书写的时间窗（如 `BLOCKED_TIME_RANGES`）**会差出时区偏移**。在容器里算「现在」「某条消息几点」「时间窗边界」时极易错位（实测因此误判过窗口、又一度误猜成 UTC 问题）。涉及本地时间的核对请在**宿主机**做，或在容器里显式按宿主机时区换算。
- **Python 用 `/opt/venv`**：镜像里已装好依赖，直接 `python` / `pytest` 即可；**不要** source 项目里 macOS 的 `.venv`（那是宿主机的）。
- **不能 push**：容器不带 SSH 私钥，只做 `git commit` 与构建测试；`git push` 在宿主机/外部完成。
- **首次登录**：进容器后执行一次 `/login`，登录态存在持久卷 `wechat-dev-claude`，之后免登录。

## 公开版隐私模型

群友在微信群内发送以下指令（单独成行，下次跑日报时生效）：

| 指令 | 说明 |
|------|------|
| `/alias <名字>` | 设置公开版显示别名（最多 6 个汉字 / 12 个英文字符） |
| `/alias` | 清空别名，恢复默认匿名名 |
| `/optout` | 退出公开版，发言完全移除 |
| `/optin` | 重新参与公开版 |

指令执行结果会在每期群内版日报末尾的「本期指令执行记录」章节中公布。

## 辅助脚本

```bash
# 从零重建别名数据库（从历史消息完整回放所有指令）
python3 -m scripts.rebuild_aliases

# 把 aliases.json 升到新 token 格式（一次性迁移，按需运行）
python3 -m scripts.migrate_token_format

# 查询群聊记录（匿名化纯文本，可选解码图片）
python3 scripts/query_chatlog.py --since 2026-06-01 --until 2026-06-05
python3 scripts/query_chatlog.py --keyword 显卡 --context 2 --limit 50
python3 scripts/query_chatlog.py --since "2026-06-04 18:00" --decode-images
```

`query_chatlog.py` 用与日报相同的匿名机制输出纯文本（发送者及正文 @提及均替换为匿名别名，optout 用户隐藏）。参数：

| 参数 | 说明 |
|------|------|
| `--since` / `--until` | 时间范围，`YYYY-MM-DD` 或 `'YYYY-MM-DD HH:MM'`；`--until` 给日期含当天；均可选 |
| `--keyword` | 正文/引用子串匹配（大小写不敏感），跑在匿名化之前的原文上 |
| `--context N` | 关键词命中时附带前后各 N 条（默认 0） |
| `--limit N` | 数量上限，取最新 N 条（默认 20；0 表示不限） |
| `--decode-images` | 解码图片到临时目录，文本中嵌入图片路径 |
| `--image-dir DIR` | 指定图片输出目录（默认自动建临时目录），隐含 `--decode-images` |

## 项目结构

```
wechat_daily/
├── config.py            # 常量、路径、env 加载
├── models.py            # DailyReport 等数据类
├── wechat_db.py         # SQLCipher 连接（只读，immutable 模式）
├── contacts.py          # wxid → 昵称映射
├── chatroom_members.py  # 群成员名单
├── message_parser.py    # 消息解析
├── image_decoder.py     # 图片附件解码（dat → 原图）
├── chat_extractor.py    # 按日期提取消息
├── url_enricher.py      # 链接卡片抓取与摘要（DeepSeek 摘要，喂给 LLM 的 [网页摘要] 来源）
├── deepseek_client.py   # DeepSeek（OpenAI 兼容）流式客户端（链接摘要）
├── aliases.py           # 别名数据库、指令扫描、备份
├── privacy.py           # token 化（惰性分配）、optout 遮蔽、泄漏检测
├── roster.py            # token → 真实昵称变体花名册（喂给 LLM 解代称）
├── prior_report.py      # 历史日报加载（跨日续写 / 去重的 <previous_reports> 素材）
├── prompts.py           # 日报生成的系统提示 + 用户指令常量（流式/批量共享）
├── llm_extractor.py     # 请求构建 + 响应收尾 + 流式生成（--no-batch 路径）
├── batch_extractor.py   # Batch API 生成（默认路径）：提交/轮询/断点续接/5 折记账
├── renderer.py          # Markdown 后期处理：标记剥离、token 替换、群内版 / 公开版渲染
├── pdf.py               # Markdown → PDF
├── archiver.py          # 7 天滚动归档
├── cost_tracker.py      # Anthropic 调用 token 用量 + 价格估算（写 debug/costs.jsonl）
├── publisher.py         # 公开仓库 commit / push / 预览
└── cli.py               # 主流程编排
```

## License

[MIT](LICENSE)
