"""Generate a real side-by-side PDF preview of candidate Chinese reading fonts.

Rendered via headless Google Chrome (CoreText), because WeasyPrint's fontconfig
on this machine resolves every CJK family to Hiragino and cannot be made to use
any other font. Chrome resolves all macOS system fonts correctly by name.

Run:  PYTHONPATH=. python scripts/font_preview.py
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

import markdown as md_lib
from markdown.extensions.toc import TocExtension

from wechat_daily.pdf import _get_pdf_css, _toc_slugify

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# (css font-family, display title, one-line rationale)
CANDIDATES = [
    (
        "Hiragino Sans GB",
        "Hiragino Sans GB · 冬青黑体（你现在实际在用的）",
        "WeasyPrint 一直兜底到的字体——即你觉得不适合阅读的那个。黑体，偏厚。",
    ),
    ("PingFang SC", "PingFang SC · 苹方", "Apple 为屏幕设计的黑体，比冬青更清爽现代、留白更舒展。"),
    ("Songti SC", "Songti SC · 宋体", "衬线宋体，横细竖粗、书卷气强；长文阅读优雅，像读文章。"),
    ("STSong", "STSong · 华文宋体", "另一款宋体，字形比 Songti SC 更端正稳重，偏报刊正文。"),
    ("Kaiti SC", "Kaiti SC · 楷体", "楷书笔意，手写感强、亲和柔和；适合慢读，但字形较细。"),
]

SAMPLE_MD = """
# 2026-05-23 群聊日报

今天最值得细读的是 <span class="mention">@陈然</span> 一张信息图引发的连锁反应。\
图的论点清晰——$200/月的 coding plan 按 API 价值折算值 $8,000–$10,000，\
IPO 前的万亿补贴窗口终将关闭，趁现在烧 token 跑通商业闭环。

## 方法论

### Token 补贴窗口论

<span class="mention">@陈然</span> 发布了一张多格信息图，核心论点：OpenAI 和 Anthropic \
今年都在朝上市走，它们的 coding plan 按 API 价值折算约 $8,000–$10,000，这是 IPO 前的巨额补贴。\
一旦上市后财务压力增大、模型进步放缓，intelligence per dollar 可能正处于「巅峰时期」。

> <span class="mention">@鸭哥</span>：AI 自我进化 Landscape 是最终产出，它背后有一套 \
> infrastructure 支撑如何对一个陌生的领域进行细致的调研——共享很多 common skills，\
> 但并不完全一致。

要点速览：

1. 证明本身的价值下降，好猜想与关键洞察的价值上升。
2. 数学目标客观、验证成本低，比编程更容易被自动化。

行内代码 `intelligence per dollar` 与一个对照表：

| 阶段 | 耗时 | 加速比 |
|------|------|--------|
| score（优化前） | 73.6μs | — |
| score（优化后） | 7.1μs | 19x |
"""


def _build_html() -> str:
    converter = md_lib.Markdown(
        extensions=[
            "tables",
            "fenced_code",
            "nl2br",
            TocExtension(slugify=_toc_slugify, toc_depth="2-3"),
        ]
    )
    sections = []
    for family, title, note in CANDIDATES:
        body = converter.convert(SAMPLE_MD)
        converter.reset()
        sections.append(f"""
<section class="font-page" style="font-family: '{family}';">
  <div class="font-banner">
    <div class="font-title">{title}</div>
    <div class="font-note">{note}</div>
  </div>
  {body}
</section>""")
    return (
        f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">"""
        f"""<style>{_get_pdf_css()}{EXTRA_CSS}</style></head>"""
        f"""<body>{"".join(sections)}</body></html>"""
    )


EXTRA_CSS = """
.font-page { break-before: page; }
.font-page:first-child { break-before: auto; }
/* keep the label banner in a fixed neutral font so it never changes */
.font-banner { background: #1a56db; border-radius: 8pt; padding: 12pt 18pt;
               margin-bottom: 16pt; font-family: 'PingFang SC', sans-serif; }
.font-banner * { color: #fff; }
.font-title { font-size: 30pt; font-weight: bold; }
.font-note { font-size: 20pt; margin-top: 6pt; }
"""


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "data" / "font_preview.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "preview.html"
        html_path.write_text(_build_html(), encoding="utf-8")
        user_dir = Path(tmp) / "chrome"
        if out.exists():
            out.unlink()
        # On this machine headless Chrome writes the PDF in a few seconds but
        # never exits, so we can't wait on the process. Launch it detached,
        # poll until the PDF is fully written (size stable), then kill Chrome.
        proc = subprocess.Popen(
            [
                CHROME,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                f"--user-data-dir={user_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-extensions",
                "--disable-default-apps",
                "--no-pdf-header-footer",
                f"--print-to-pdf={out}",
                html_path.as_uri(),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            last, stable = -1, 0
            for _ in range(120):  # up to ~60s
                time.sleep(0.5)
                size = out.stat().st_size if out.exists() else -1
                stable = stable + 1 if size == last and size > 0 else 0
                last = size
                if stable >= 3:  # size unchanged for ~1.5s → done
                    break
            else:
                raise SystemExit("Chrome did not produce a PDF in time")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
