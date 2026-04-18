"""Markdown → PDF conversion (group version only)."""

from __future__ import annotations

import re
from pathlib import Path

import markdown as md_lib
from markdown.extensions.toc import TocExtension


def _toc_slugify(value: str, separator: str) -> str:
    return value.strip().encode('utf-8').hex()


def _get_pdf_css() -> str:
    return """
    @font-face {
        font-family: 'ChineseFont';
        src: local('PingFang SC'),
             local('STHeiti Medium'),
             local('Heiti SC'),
             url('/System/Library/Fonts/STHeiti Medium.ttc') format('truetype'),
             url('/System/Library/Fonts/Hiragino Sans GB.ttc') format('truetype'),
             url('/Library/Fonts/Arial Unicode.ttf') format('truetype');
    }

    @page {
        size: A4;
        margin: 18mm 14mm;
    }

    body {
        font-family: 'ChineseFont', 'PingFang SC', 'STHeiti', 'Heiti SC',
                     'Hiragino Sans GB', 'Arial Unicode MS', sans-serif;
        font-size: 30pt;
        line-height: 1.75;
        color: #1a1a1a;
        word-break: break-word;
        overflow-wrap: break-word;
    }

    h1 { font-size: 40pt; font-weight: bold; color: #1a56db; margin-top: 24pt;
         margin-bottom: 14pt; border-bottom: 2pt solid #1a56db; padding-bottom: 6pt; }
    h2 { font-size: 36pt; font-weight: bold; color: #1a56db; margin-top: 20pt;
         margin-bottom: 10pt; border-bottom: 1pt solid #93c5fd; padding-bottom: 4pt; }
    h3 { font-size: 33pt; font-weight: bold; color: #1e40af; margin-top: 16pt; margin-bottom: 8pt; }

    p { margin: 10pt 0; }
    ul, ol { margin: 8pt 0; padding-left: 30pt; }
    li { margin: 6pt 0; }
    ol ol, ol ul, ul ol, ul ul { margin: 3pt 0; padding-left: 28pt; font-size: 0.88em; }

    blockquote {
        border-left: 4pt solid #93c5fd; border-radius: 0 20pt 20pt 0;
        margin: 12pt 0; padding: 8pt 16pt; color: #374151; background: #f0f5ff;
    }

    code { font-family: 'Courier New', 'Menlo', monospace; font-size: 16pt;
           background: #f0f0f0; padding: 1pt 5pt; border-radius: 3pt; }
    pre { background: #f0f0f0; padding: 12pt; font-size: 15pt;
          overflow-x: auto; border-radius: 4pt; }

    table { border-collapse: collapse; width: 100%; margin: 12pt 0; }
    th, td { border: 1pt solid #ccc; padding: 7pt 12pt; text-align: left; }
    th { background: #f0f0f0; font-weight: bold; }

    hr { border: none; border-top: 1pt solid #ddd; margin: 14pt 0; }
    a { color: #1a56db; text-decoration: none; }

    .toc { background: #f0f5ff; border: 1pt solid #93c5fd; border-radius: 6pt;
           padding: 14pt 20pt; margin: 16pt 0 24pt 0; }
    .toc ul { margin: 4pt 0; padding-left: 20pt; }
    .toc li { margin: 5pt 0; }

    .back-to-toc { float: right; font-weight: normal; line-height: 1; }
    .back-to-toc a { font-size: 0.6em; color: #1a56db; background: #e8f0fe;
                     border: 0.5pt solid #93c5fd; border-radius: 100pt; padding: 3pt 8pt; }
    """


def convert_to_pdf(markdown_text: str, output_path: Path) -> None:
    """Convert Markdown text to PDF with Chinese font support."""
    from weasyprint import HTML, CSS

    converter = md_lib.Markdown(extensions=[
        "tables", "fenced_code", "nl2br",
        TocExtension(slugify=_toc_slugify, toc_depth="2-3"),
    ])
    html_body = converter.convert(markdown_text)
    html_body = html_body.replace('<div class="toc">', '<div class="toc" id="toc">', 1)
    html_body = re.sub(
        r'(</h[23]>)',
        r'<span class="back-to-toc"><a href="#toc">↑ 目录</a></span>\1',
        html_body,
    )

    full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"></head>
<body>
{html_body}
</body>
</html>"""

    HTML(string=full_html).write_pdf(
        str(output_path),
        stylesheets=[CSS(string=_get_pdf_css())],
    )
