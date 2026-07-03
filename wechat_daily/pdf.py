"""Markdown → PDF conversion (group version only).

Primary renderer is headless Google Chrome: it uses macOS CoreText, so system
fonts (PingFang etc.) resolve correctly and embed into the PDF. WeasyPrint is
kept as a fallback for when Chrome is unavailable — note that on some machines
WeasyPrint's fontconfig resolves every CJK family to Hiragino, so the fallback
output may not honor the configured font.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

import markdown.extensions.toc

# Chrome-family binaries to try, in order. macOS paths first (font stack targets
# macOS); falls back to anything on PATH for portability.
_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
_CHROME_PATH_NAMES = (
    "google-chrome", "google-chrome-stable", "chromium",
    "chromium-browser", "microsoft-edge",
)


def _find_chrome() -> str | None:
    for path in _CHROME_CANDIDATES:
        if pathlib.Path(path).exists():
            return path
    for name in _CHROME_PATH_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _toc_slugify(value: str, separator: str) -> str:
    return value.strip().encode('utf-8').hex()


def _get_pdf_css() -> str:
    # NOTE: do NOT wrap the body font in an `@font-face` that points at a
    # `local('PingFang SC')` system font. Chrome then treats PingFang as a web
    # font and embeds it as a corrupt Type 3 subset whose glyphs render
    # invisible. Naming the system font directly in `font-family` lets Chrome
    # resolve it via CoreText and embed it correctly.
    return """
    @page {
        size: A4;
        margin: 18mm 14mm;
    }

    body {
        font-family: 'PingFang SC', 'STHeiti', 'Heiti SC',
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

    code { font-family: 'Courier New', 'Menlo', monospace, 'PingFang SC', 'STHeiti', sans-serif;
           font-size: 0.85em; background: #f0f0f0; padding: 1pt 5pt; border-radius: 3pt; }
    pre { background: #f0f0f0; padding: 12pt;
          font-family: 'Courier New', 'Menlo', monospace, 'PingFang SC', 'STHeiti', sans-serif;
          font-size: 0.85em; overflow-x: auto; border-radius: 4pt; }

    table { border-collapse: collapse; width: 100%; margin: 12pt 0; }
    th, td { border: 1pt solid #ccc; padding: 7pt 12pt; text-align: left; }
    th { background: #f0f0f0; font-weight: bold; }

    hr { border: none; border-top: 1pt solid #ddd; margin: 14pt 0; }
    a { color: #1a56db; text-decoration: none; }

    .mention { color: #1a56db; font-weight: 600; text-decoration: none;
               white-space: nowrap; }

    .toc { background: #f0f5ff; border: 1pt solid #93c5fd; border-radius: 6pt;
           padding: 14pt 20pt; margin: 16pt 0 24pt 0; }
    .toc ul { margin: 4pt 0; padding-left: 20pt; }
    .toc li { margin: 5pt 0; }

    .back-to-toc { float: right; font-weight: normal; line-height: 1; }
    .back-to-toc a { font-size: 0.6em; color: #1a56db; background: #e8f0fe;
                     border: 0.5pt solid #93c5fd; border-radius: 100pt; padding: 3pt 8pt; }
    """


def _build_full_html(markdown_text: str) -> str:
    """Convert Markdown to a complete, self-contained HTML document.

    The CSS is inlined in a ``<style>`` tag so the exact same document renders
    identically through Chrome or WeasyPrint.
    """
    converter = markdown.Markdown(extensions=[
        "tables", "fenced_code", "nl2br",
        markdown.extensions.toc.TocExtension(slugify=_toc_slugify, toc_depth="2-3"),
    ])
    html_body = converter.convert(markdown_text)
    html_body = html_body.replace('<div class="toc">', '<div class="toc" id="toc">', 1)
    html_body = re.sub(
        r'(</h[23]>)',
        r'<span class="back-to-toc"><a href="#toc">↑ 目录</a></span>\1',
        html_body,
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><style>{_get_pdf_css()}</style></head>
<body>
{html_body}
</body>
</html>"""


def _render_with_chrome(chrome: str, full_html: str, output_path: pathlib.Path) -> bool:
    """Render via headless Chrome. Returns True on success, False otherwise.

    On macOS, headless Chrome writes the PDF within a few seconds but frequently
    does not exit, so we cannot wait on the process. Instead we poll for the
    output file until its size is stable, then terminate Chrome.
    """
    if output_path.exists():
        output_path.unlink()

    with tempfile.TemporaryDirectory() as tmp:
        html_path = pathlib.Path(tmp) / "report.html"
        html_path.write_text(full_html, encoding="utf-8")
        user_dir = pathlib.Path(tmp) / "chrome"
        try:
            proc = subprocess.Popen([
                chrome, "--headless", "--disable-gpu", "--no-sandbox",
                f"--user-data-dir={user_dir}",
                "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-component-update",
                "--disable-extensions", "--disable-default-apps",
                "--no-pdf-header-footer", f"--print-to-pdf={output_path}",
                html_path.as_uri(),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False

        try:
            last, stable = -1, 0
            for _ in range(120):  # poll up to ~60s
                time.sleep(0.5)
                size = output_path.stat().st_size if output_path.exists() else -1
                stable = stable + 1 if size == last and size > 0 else 0
                last = size
                if stable >= 3:  # size unchanged for ~1.5s → fully written
                    break
            else:
                return False
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return _looks_like_pdf(output_path)


def _looks_like_pdf(path: pathlib.Path) -> bool:
    try:
        with path.open("rb") as fh:
            return path.stat().st_size > 1000 and fh.read(5).startswith(b"%PDF")
    except OSError:
        return False


def _render_with_weasyprint(full_html: str, output_path: pathlib.Path) -> None:
    from weasyprint import HTML

    HTML(string=full_html).write_pdf(str(output_path))


def convert_to_pdf(markdown_text: str, output_path: pathlib.Path) -> None:
    """Convert Markdown text to PDF with Chinese font support.

    Primary path is headless Chrome (faithful macOS font rendering); falls back
    to WeasyPrint when Chrome is missing or fails.
    """
    full_html = _build_full_html(markdown_text)

    chrome = _find_chrome()
    if chrome and _render_with_chrome(chrome, full_html, output_path):
        return

    if chrome:
        print(
            "[pdf] Chrome 渲染失败，回退到 WeasyPrint（字体可能退回 Hiragino）",
            file=sys.stderr,
        )
    _render_with_weasyprint(full_html, output_path)
