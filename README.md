# WeChat 群聊日报生成器

自动从微信群聊记录生成每日日报：群内版（真实昵称 + PDF）与公开版（匿名化 + GitHub Pages）。

## 功能概览

- **双版本产出**：群内版保留真实昵称，公开版经过三级隐私处理后发布到 GitHub Pages
- **Markdown 提取**：用 Claude Opus 从聊天记录中流式生成 Markdown 日报；公开/内部版本通过后期处理分流
- **三级隐私模型**：`/optout`（不出现）/ 默认匿名（稳定派生）/ `/alias`（自定义公开别名）
- **泄漏检测**：公开版发布前，用 Claude Haiku 二次确认真实昵称是否为人名引用
- **7 天滚动归档**：超过 7 天的 PDF 自动整理到 `archive/YYYY/MM/` 子目录

## 环境要求

- Python 3.11+
- `chatlog-mac/keys.json`（微信数据库解密密钥，不随代码发布）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## API Key 配置

首次运行时会提示输入，自动保存到 `.env`；也可手动创建：

```
ANTHROPIC_API_KEY=your_anthropic_api_key_here
GEMINI_API_KEY=your_gemini_api_key_here   # 仅 --summary gemini 时需要
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

# 生成缺失日报（Claude 结构化提取，默认）
python3 main.py

# 生成后推送公开版到 GitHub Pages
python3 main.py -y

# 也为当天不完整日期生成日报
python3 main.py --allow-incomplete

# 使用 Gemini 生成（仅群内版，无公开版）
python3 main.py --summary gemini
```

**`-y` 标志**：推送上一次运行生成的本地 commit 到公开仓库。本次生成的 commit 下次带 `-y` 才推送，留出人工审核窗口。

## 输出文件

| 路径 | 模型 | 说明 |
|------|------|------|
| `archive/YYYY-MM-DD 群聊日报.pdf` | 4.6 | 群内版 PDF（真实昵称，主路径） |
| `archive/YYYY-MM-DD 群聊日报 (opus-4-7).pdf` | 4.7 | 群内版 PDF（对比版，仅本地） |
| `debug/YYYY-MM-DD.md` / `.opus-4-7.md` | 4.6 / 4.7 | 群内版 Markdown 原文 |
| `debug/extract-YYYY-MM-DD.{md,input.txt,thinking.md}` | 4.6 | 原始 Markdown 日报（用作下日续写素材）+ 输入快照 + thinking 摘要 |
| `debug/extract-YYYY-MM-DD.opus-4-7.{md,input.txt,thinking.md}` | 4.7 | 对比版同上 |
| `debug/costs.jsonl` | 全部 | 每次 Anthropic 调用的 token 用量 + 价格估算（JSON Lines） |
| `data/public_repo/_posts/` | **仅 4.6** | 公开版 Jekyll Markdown（本地 commit，待推送） |

每次跑完会在终端打出按 (日期, 阶段, 模型) 聚合的成本汇总表，含 `tok/char` 一列——Opus 4.7 用了新 tokenizer，对相同文本 token 数最高可涨 35%，这列让分词效率差异直观可见。临时省钱可用 `python3 main.py --no-compare` 跳过 4.7。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

测试是 hermetic 的：不读 `.env`、不连数据库、不调 API；`tests/conftest.py` 把 git 子进程从宿主全局配置里隔离开。本地或 CI 都按上面这条命令跑即可。

### Claude Code 云端环境备注

云端 sandbox 启动时**没有 venv**，系统 Python 也**只有标准库**。直接 `python3 -m pytest tests/` 会在 collect 阶段炸：`test_llm_extractor.py` 和 `test_url_enricher.py` 都需要 `httpx`，没装就 `ModuleNotFoundError`（这两个文件之外的纯 Python 测试 collect 不到 httpx，看着像能跑，但等于跳过了 1/4 的覆盖）。

跑全套的标准动作：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/ -q
```

`pytest` 不在 `requirements.txt` 里（它是 dev-only），所以要单独追加。`weasyprint` 会拖一堆系统级字体/lxml 依赖、`sqlcipher3` 会编 C 扩展，首次安装 ~1–2 分钟，之后 `.venv/` 复用即可。装完整套测试秒级跑完。

不要用系统 Python `pip install`——sandbox 里它会落到 `/usr/local`，下次重启 sandbox 就丢了，而且会污染全局环境。venv 装在工作目录下，下次进同一个工作目录直接复用。

## 公开版隐私模型

群友在微信群内发送以下指令（单独成行，下次跑日报时生效）：

| 指令 | 说明 |
|------|------|
| `/alias <名字>` | 设置公开版显示别名（1–16 字符） |
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

# 用保存的 input.txt 回放某天 4.7 提取，方便快速试 system-prompt 变体
python3 scripts/probe_extractor_prompt.py
```

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
├── url_enricher.py      # 链接卡片抓取与摘要（喂给 LLM 的 [网页摘要] 来源）
├── aliases.py           # 别名数据库、指令扫描、备份
├── privacy.py           # token 化（惰性分配）、optout 遮蔽、泄漏检测
├── roster.py            # token → 真实昵称变体花名册（喂给 LLM 解代称）
├── prior_report.py      # 历史日报加载（跨日续写 / 去重的 <previous_reports> 素材）
├── llm_extractor.py     # Claude 流式 Markdown 生成（含 4.6 / 4.7 双 prompt）
├── renderer.py          # Markdown 后期处理：标记剥离、token 替换、群内版 / 公开版渲染
├── pdf.py               # Markdown → PDF
├── archiver.py          # 7 天滚动归档
├── cost_tracker.py      # Anthropic 调用 token 用量 + 价格估算（写 debug/costs.jsonl）
├── publisher.py         # 公开仓库 commit / push / 预览
└── cli.py               # 主流程编排
```

## License

[MIT](LICENSE)
