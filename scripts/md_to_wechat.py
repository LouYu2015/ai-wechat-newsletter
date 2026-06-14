#!/usr/bin/env python3
"""把 draft markdown 转成微信公众号可粘贴的内联样式 HTML。

样式取自参考文章 https://mp.weixin.qq.com/s/umkGnI8FEkFgFKFBm0-pBQ
（群聊周报 Vol.1）的真实排版，全部内联，无超链接。

用法：
    python scripts/md_to_wechat.py <input.md> [output.html]

输出 HTML 用浏览器打开后全选复制，粘贴进公众号编辑器即可。
H1 标题不进正文，单独打印到 stderr，复制到「标题」栏。
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

# ── 样式常量（取自参考文章）─────────────────────────────────────────
S_PARA = "margin:0 0 18px;line-height:1.75;font-size:16px;color:#3f3f3f;letter-spacing:.3px;"
S_H2 = ("margin:38px 0 18px;font-size:20px;font-weight:bold;color:#1a1a1a;"
        "line-height:1.5;border-left:4px solid #2b6cd6;padding-left:12px;")
S_H3 = "margin:30px 0 14px;font-size:17px;font-weight:bold;color:#2b6cd6;line-height:1.5;"
S_QUOTE_BOX = ("margin:18px 0;padding:14px 18px;background:#f2f7ff;"
               "border:1px solid #d3e3fb;border-radius:10px;")
S_QUOTE_P = "margin:0;line-height:1.7;font-size:15px;color:#2f5b9e;"
S_QUOTE_P2 = "margin:10px 0 0;line-height:1.7;font-size:15px;color:#2f5b9e;"
S_UL = "margin:0 0 18px;padding-left:22px;"
S_LI = "margin:0 0 10px;line-height:1.75;font-size:16px;color:#3f3f3f;"
S_MENTION = ("display:inline-block;padding:0 7px;margin:0 1px;background:#eaf2ff;"
             "border:1px solid #cfe0fb;border-radius:6px;color:#2b6cd6;font-size:14px;")
S_CODE = ("font-family:ui-monospace,Menlo,Consolas,monospace;background:#f2f3f5;"
          "color:#476582;padding:1px 5px;border-radius:4px;font-size:14px;")
S_SUP = "color:#2b6cd6;font-size:12px;"
S_HR = "margin:34px 0;border-top:1px solid #ececec;"
S_REF_TITLE = "font-size:15px;color:#3f3f3f;"
S_REF_URL = "font-size:13px;color:#8a8a8a;word-break:break-all;"

# 顶部「素材声明」灰框样式。框内文字从 md 的第一个 `>` 引用块读取。
S_INTRO_BOX = ("margin:0 0 24px;padding:12px 16px;background:#f6f7f9;"
               "border-radius:10px;font-size:14px;color:#8a8a8a;line-height:1.7;")


# ── 行内转换 ────────────────────────────────────────────────────────
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_FOOT_RE = re.compile(r"\[(\d+)\]")


def _mention(name: str) -> str:
    return f'<span style="{S_MENTION}">{html.escape(name)}</span>'


def _code(content: str) -> str:
    return f'<span style="{S_CODE}">{html.escape(content)}</span>'


def inline(text: str) -> str:
    """行内 markdown → 内联样式 HTML。无超链接。"""
    # 1. 抽取反引号片段（@提及 → 药丸；其余 → 行内代码），占位保护
    stash: list[str] = []

    def _stash_code(m: re.Match) -> str:
        content = m.group(1)
        repl = _mention(content) if content.startswith("@") else _code(content)
        stash.append(repl)
        return f"\x00{len(stash) - 1}\x00"

    text = _CODE_RE.sub(_stash_code, text)

    # 1.5 自动链接 <https://…> → 纯文本 URL（公众号不支持超链接，去掉尖括号）
    text = re.sub(r"<(https?://[^>\s]+)>", r"\1", text)

    # 2. 转义其余文本
    text = html.escape(text, quote=False)

    # 3. markdown 链接 → 仅保留文字（公众号不支持超链接）
    text = _LINK_RE.sub(lambda m: m.group(1), text)

    # 4. 加粗
    text = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)

    # 5. 脚注标记 [n] → 上标
    text = _FOOT_RE.sub(lambda m: f'<sup style="{S_SUP}">[{m.group(1)}]</sup>', text)

    # 6. 回填占位
    text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
    return text


# ── 块级解析 ────────────────────────────────────────────────────────
def convert(md: str) -> tuple[str, str]:
    """返回 (title, body_html)。"""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    title = ""
    i = 0
    n = len(lines)
    first_bq_used = False
    refs_mode = False

    def is_bullet(s: str) -> bool:
        return s.lstrip()[:2] in ("- ", "* ")

    def is_marker(s: str) -> bool:
        s = s.lstrip()
        return (s.startswith("#") or s.startswith(">") or is_bullet(s)
                or s.strip() == "---" or not s.strip())

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 分隔线
        if stripped == "---":
            out.append(f'<section style="{S_HR}"></section>')
            i += 1
            continue

        # 标题
        hm = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if hm:
            level = len(hm.group(1))
            htext = hm.group(2).strip()
            if level == 1:
                title = htext
            elif level == 2:
                refs_mode = "参考链接" in htext
                out.append(f'<h2 style="{S_H2}">{inline(htext)}</h2>')
            else:
                out.append(f'<h3 style="{S_H3}">{inline(htext)}</h3>')
            i += 1
            continue

        # 引用块
        if stripped.startswith(">"):
            buf: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                content = re.sub(r"^>\s?", "", lines[i].strip())
                buf.append(content)
                i += 1
            # 按空行分段
            paras: list[str] = []
            cur: list[str] = []
            for b in buf:
                if b.strip():
                    cur.append(b)
                elif cur:
                    paras.append(" ".join(cur))
                    cur = []
            if cur:
                paras.append(" ".join(cur))

            if not first_bq_used:
                first_bq_used = True
                # 顶部素材声明框：内容来自 md 第一个引用块的各段
                ps = []
                for k, p in enumerate(paras):
                    mt = "margin:0;" if k == 0 else "margin:8px 0 0;"
                    ps.append(f'<p style="{mt}word-break:break-all;">{inline(p)}</p>')
                out.append(f'<section style="{S_INTRO_BOX}">{"".join(ps)}</section>')
            else:
                ps = []
                for k, p in enumerate(paras):
                    style = S_QUOTE_P if k == 0 else S_QUOTE_P2
                    ps.append(f'<p style="{style}">{inline(p)}</p>')
                out.append(f'<section style="{S_QUOTE_BOX}">{"".join(ps)}</section>')
            continue

        # 列表（支持 `- ` 和 `* ` 两种 bullet）
        if is_bullet(stripped):
            items: list[str] = []
            while i < n and is_bullet(lines[i].strip()):
                items.append(lines[i].strip()[2:])
                i += 1
            lis = "".join(
                f'<li style="{S_LI}"><section>{inline(it)}</section></li>'
                for it in items
            )
            out.append(f'<ul style="{S_UL}">{lis}</ul>')
            continue

        # 普通段落：聚合连续非标记行
        para_lines: list[str] = []
        while i < n and lines[i].strip() and not is_marker(lines[i]):
            para_lines.append(lines[i].strip())
            i += 1

        if refs_mode and para_lines and re.match(r"^\[\d+\]", para_lines[0]):
            # 参考链接条目：首行标题，其余为 URL
            ref_title = html.escape(para_lines[0], quote=False)
            urls = "".join(
                f'<br><span style="{S_REF_URL}">{html.escape(u, quote=False)}</span>'
                for u in para_lines[1:]
            )
            out.append(
                f'<p style="margin:0 0 14px;line-height:1.6;">'
                f'<span style="{S_REF_TITLE}">{ref_title}</span>{urls}</p>'
            )
        else:
            body = "<br>".join(inline(p) for p in para_lines)
            out.append(f'<p style="{S_PARA}">{body}</p>')

    return title, "\n".join(out)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = Path(sys.argv[1])
    md = src.read_text(encoding="utf-8")
    title, body = convert(md)

    page = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>公众号预览</title></head>'
        '<body style="margin:0;background:#fff;">'
        '<div style="max-width:677px;margin:0 auto;padding:24px 16px;'
        'font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\',sans-serif;">'
        f'{body}'
        '</div></body></html>'
    )

    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".wechat.html")
    out.write_text(page, encoding="utf-8")
    print(f"标题（复制到「标题」栏）：\n{title}\n", file=sys.stderr)
    print(f"已生成：{out}", file=sys.stderr)
    print(out)


if __name__ == "__main__":
    main()
