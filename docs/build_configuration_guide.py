"""Build the illustrated guide as self-contained HTML and a printable PDF.

Run in an isolated documentation environment with markdown-it-py and playwright
installed, and run `python -m playwright install chromium` first. This renderer
uses only the checked-in Markdown and screenshots; it never logs into Controller.
"""

import base64
from pathlib import Path
import re

from markdown_it import MarkdownIt
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "CONTROLLER_CONFIGURATION_GUIDE.md"
HTML = SOURCE.with_suffix(".html")
PDF = SOURCE.with_suffix(".pdf")
STYLE = """
* {box-sizing: border-box}
body {font: 15px/1.55 'DejaVu Sans', Arial, sans-serif; color: #1f2937; margin:0; background:#f1f5f9}
main {max-width:1050px; margin:32px auto; padding:52px 68px; background:white}
h1 {font-size:34px; line-height:1.2; color:#123552; margin:0 0 28px}
h2 {font-size:24px; line-height:1.25; color:#123552; margin:48px 0 18px; border-bottom:2px solid #dae5ed; padding-bottom:10px}
h3 {font-size:18px; color:#1f4e69; margin:28px 0 12px}
p {margin:12px 0} a {color:#145b9b; text-decoration:none}
li {margin:7px 0} li > p {margin:7px 0}
table {border-collapse:collapse; width:100%; margin:20px 0; font-size:13px; table-layout:fixed}
th,td {border:1px solid #ccd7e2; padding:10px 12px; text-align:left; vertical-align:top; overflow-wrap:anywhere}
th {background:#e8f0f6; color:#143e5b} tr:nth-child(even) td {background:#f8fafc}
td:first-child,th:first-child {width:30%}
blockquote {margin:22px 0; padding:10px 18px; background:#fff8e8; border-left:4px solid #c18a21}
code {font-family:'DejaVu Sans Mono',monospace; font-size:.88em; overflow-wrap:anywhere; background:#f0f4f8; padding:1px 3px}
pre {background:#f0f4f8; padding:16px; border:1px solid #dbe4ec; white-space:pre-wrap; overflow-wrap:anywhere; line-height:1.45}
pre code {padding:0}
figure {margin:22px 0; break-inside:avoid}
img {display:block; width:100%; height:auto; border:1px solid #d5dfe8; border-radius:4px}
figcaption {font-size:12px; line-height:1.45; color:#566477; margin-top:8px}
@media print {
  body {background:white; font-size:10pt; line-height:1.45}
  main {max-width:none; margin:0; padding:0}
  h1 {font-size:27pt}
  h2 {font-size:19pt; break-before:page; margin:0 0 16px}
  h3 {font-size:13pt; break-after:avoid}
  p,li {orphans:3; widows:3}
  table {font-size:9pt; break-inside:avoid}
  th,td {padding:7px 8px}
  tr, pre, blockquote {break-inside:avoid}
  thead {display:table-header-group}
  figure img {max-height:175mm; object-fit:contain; object-position:top}
  figcaption {font-size:8.5pt}
}
"""


def build():
    renderer = MarkdownIt("commonmark").enable("table")
    rendered = renderer.render(SOURCE.read_text())
    # Keep navigation stable between the Markdown, HTML and PDF editions.
    def heading(match):
        level, title = match.group(1), match.group(2)
        slug = re.sub(r"[^a-z0-9 -]", "", title.lower()).replace(" ", "-")
        return f'<h{level} id="{slug}">{title}</h{level}>'
    rendered = re.sub(r"<h([123])>(.*?)</h\1>", heading, rendered)
    def embedded_image(match):
        image_path = (ROOT / match.group(1)).resolve()
        if not image_path.is_relative_to(ROOT / "screenshots"):
            raise ValueError("Image is outside the documentation screenshot directory")
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        return f'src="data:image/png;base64,{encoded}"'
    rendered = re.sub(r'src="(screenshots/[^\"]+\.png)"', embedded_image, rendered)
    rendered = re.sub(r'<p>(<img [^>]+>)</p>\s*<p><em>(.*?)</em></p>',
                      r'<figure>\1<figcaption>\2</figcaption></figure>', rendered, flags=re.S)
    HTML.write_text('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>Frappe Controller configuration guide</title>'
                    f'<style>{STYLE}</style></head><body><main>{rendered}</main></body></html>')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 1000})
        page.goto(HTML.as_uri(), wait_until="load")
        checks = page.evaluate('''() => ({
          images: document.images.length,
          broken: [...document.images].filter(i => !i.complete || !i.naturalWidth).length,
          brokenAnchors: [...document.querySelectorAll('a[href^="#"]')]
            .filter(a => !document.getElementById(a.getAttribute('href').slice(1))).length
        })''')
        if checks["images"] != 10 or checks["broken"] or checks["brokenAnchors"]:
            raise RuntimeError(f"Guide validation failed: {checks}")
        page.pdf(path=str(PDF), format="A4", print_background=True,
            display_header_footer=True,
            header_template='<span></span>',
            footer_template='<div style="font:8px sans-serif;width:100%;margin:0 16mm;color:#566477;display:flex;justify-content:space-between"><span>Frappe Controller configuration guide · 3 October 2026</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>',
            margin={"top": "16mm", "right": "16mm", "bottom": "18mm", "left": "16mm"})
        browser.close()
    print(f"Built {HTML.name} and {PDF.name}; {checks['images']} embedded screenshots; all anchors valid")


if __name__ == "__main__":
    build()
