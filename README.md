# WeChat 群聊日报生成器

自动处理微信群聊屏幕录像，用 AI 生成每日群聊日报 PDF。

## 功能流程

1. 从 `~/Downloads` 读取最新的 `ScreenRecording*` 视频
2. 用 ffmpeg 将视频减速 5 倍，帧率也降低 5 倍
3. 调用 **Gemini Flash** 从视频中提取聊天记录
4. 调用 **Claude** 根据聊天记录生成每日日报（Markdown 格式）
5. 将日报转换为 PDF（20pt 字体，适合手机阅读）

## 环境要求

- Python 3.11+
- ffmpeg（通过 Homebrew 安装）

```bash
brew install ffmpeg
```

## 安装依赖

创建虚拟环境并安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## API Key 配置

程序首次运行时会提示你输入 API Key，并自动保存到项目目录的 `.env` 文件中。之后运行无需重新输入。

**获取 API Key：**

- **GEMINI_API_KEY**：前往 [Google AI Studio](https://aistudio.google.com/) 获取
- **ANTHROPIC_API_KEY**：前往 [Anthropic Console](https://console.anthropic.com/) 获取

也可以手动创建 `.env` 文件：

```
GEMINI_API_KEY=your_gemini_api_key_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here
```

> **安全提示**：`.env` 文件已被 `.gitignore` 排除，不会被提交到版本控制系统。请勿将 API Key 泄露给他人。

## 使用方法

```bash
source .venv/bin/activate
python3 main.py
```

## 输出

每次运行会在项目目录下生成两类文件：

**`archive/`** — 日报 PDF

文件名格式：`YYYY-MM-DD 群聊日报.pdf`

同一天重复运行时，自动添加序号后缀，不覆盖已有文件：

```
archive/2026-02-21 群聊日报.pdf
archive/2026-02-21 群聊日报 (2).pdf
```

> **日期逻辑**：若在午夜至凌晨 4 点之间运行，自动使用前一天的日期。

**`debug/`** — Gemini 原始提取结果

文件名格式：`gemini_output_YYYY-MM-DD.txt`，保存 Gemini 从视频中提取的聊天记录原文，方便排查提取质量问题。

## License

[MIT](LICENSE)
