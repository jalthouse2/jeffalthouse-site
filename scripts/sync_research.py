#!/usr/bin/env python3
"""
sync_research.py

Pulls the "Publications" and "Popular Media" sections out of Jeff's CV
Google Doc and regenerates the auto-managed block of research.html.

How it works
------------
Google Docs will export any doc that is shared as "Anyone with the link
can view" as plain HTML at:

    https://docs.google.com/document/d/<DOC_ID>/export?format=html

That export keeps real <a href="..."> links, bold/italic formatting, and
paragraph order, which is what makes automatic parsing possible at all —
a plain-text export would lose the hyperlinks entirely, and a PDF export
scrambles column order on multi-column layouts (which is what happened
when Jeff first uploaded a CV PDF here: the "year" column and the
"entry" column got interleaved by the PDF text extractor). HTML export
avoids both problems.

The script looks for a fixed list of section headings (see SECTIONS
below) inside the document, in order, and treats everything between one
heading and the next as that section's bullet list. If your CV's
section headings ever change wording, update SECTIONS to match, or the
script will stop finding that section (it will skip it and print a
warning rather than silently producing something wrong).

Usage
-----
    python scripts/sync_research.py

Environment variables:
    CV_DOC_ID      Google Doc ID of the English CV (required)
    RESEARCH_HTML  Path to research.html (default: research.html)

Exits with status 0 and does nothing to the file if nothing changed, so
the GitHub Action can decide whether there's anything to commit.
"""

import os
import re
import sys
import urllib.request
from html.parser import HTMLParser

CV_DOC_ID = os.environ.get("CV_DOC_ID", "")
RESEARCH_HTML = os.environ.get("RESEARCH_HTML", "research.html")

START_MARKER = "<!-- RESEARCH-AUTO-START: content below is regenerated automatically from the CV. Do not hand-edit between these markers — edits will be overwritten on the next sync. -->"
END_MARKER = "<!-- RESEARCH-AUTO-END -->"

# (heading text as it appears in the CV, <h3> label to use on the site)
# Order matters: the script reads the doc top-to-bottom and treats
# everything between one heading and the next as belonging to the
# first heading.
SECTIONS = [
    ("Articles in Peer-Reviewed Journals", "Articles in Peer-Reviewed Journals"),
    ("Book Chapters in Edited Volumes", "Book Chapters in Edited Volumes"),
    ("Reports for NGOs, Public & Multilateral Institutions", "Reports for NGOs, Public & Multilateral Institutions"),
    ("Working Papers", "Working Papers"),
    ("Podcasts and Radio", "Media — Podcasts & Radio"),
    ("Blogs", "Media — Blogs"),
]

# Heading that marks the end of the last section we care about (Blogs).
# Once the parser sees this, it stops collecting entries.
STOP_HEADING = "PRESENTATIONS AT ACADEMIC CONFERENCES"


class DocTextExtractor(HTMLParser):
    """Turns the Google Docs HTML export into a flat list of
    (kind, text_with_inline_html) blocks, one per paragraph/list item,
    in document order. kind is 'p' for a normal paragraph/heading and
    'li' for a bulleted entry."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self._buf = []
        self._tag_stack = []
        self._current_kind = None
        self._in_body = False
        self._span_stack = []

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self._in_body = True
        if not self._in_body:
            return
        if tag in ("p", "li"):
            self._buf = []
            self._current_kind = "li" if tag == "li" else "p"
        elif tag == "a":
            href = dict(attrs).get("href", "")
            # Google wraps outbound links in a redirect; unwrap it.
            m = re.search(r"[?&]q=([^&]+)", href)
            if m:
                import urllib.parse
                href = urllib.parse.unquote(m.group(1))
            self._buf.append(f'<a href="{href}" target="_blank" rel="noopener">')
        elif tag in ("b", "strong"):
            self._buf.append("<strong>")
        elif tag in ("i", "em"):
            self._buf.append("<em>")
        elif tag == "span":
            # Google Docs' HTML export represents bold/italic as inline
            # CSS on <span> elements (e.g. style="font-weight:700"),
            # not as semantic <b>/<i> tags. Detect that here.
            style = dict(attrs).get("style", "") or ""
            opens = []
            if re.search(r"font-weight\s*:\s*(700|800|900|bold)", style, re.I):
                opens.append("strong")
            if re.search(r"font-style\s*:\s*italic", style, re.I):
                opens.append("em")
            for tagname in opens:
                self._buf.append(f"<{tagname}>")
            self._span_stack.append(opens)

    def handle_endtag(self, tag):
        if tag in ("p", "li") and self._current_kind is not None:
            text = "".join(self._buf).strip()
            if text:
                self.blocks.append((self._current_kind, text))
            self._buf = []
            self._current_kind = None
        elif tag == "a":
            self._buf.append("</a>")
        elif tag in ("b", "strong"):
            self._buf.append("</strong>")
        elif tag in ("i", "em"):
            self._buf.append("</em>")
        elif tag == "span":
            if self._span_stack:
                opens = self._span_stack.pop()
                for tagname in reversed(opens):
                    self._buf.append(f"</{tagname}>")

    def handle_data(self, data):
        if self._current_kind is not None:
            self._buf.append(data)


def fetch_doc_html(doc_id):
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=html"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Doc export returned HTTP {resp.status}. "
                                f"Is the doc shared as 'Anyone with the link can view'?")
        return resp.read().decode("utf-8", errors="replace")


def normalize_heading(text):
    # Strip HTML tags and collapse whitespace for heading comparison.
    return re.sub(r"<[^>]+>", "", text).strip()


def clean_entry_text(text):
    # Collapse the odd leading bullet glyph some exports leave behind,
    # and normalize whitespace inside the (already-tagged) entry text.
    text = re.sub(r"^[\u2022\u25cf\u25aa\-\*]\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def build_section_html(label, entries):
    items = "\n".join(f"        <li>{e}</li>" for e in entries)
    return (
        f'    <section class="block">\n'
        f'      <h3>{label}</h3>\n'
        f'      <ul class="pub-list">\n'
        f'{items}\n'
        f'      </ul>\n'
        f'    </section>'
    )


def parse_sections(blocks):
    """Walk the flat block list and bucket entries under each heading
    in SECTIONS, in order."""
    heading_texts = [h for h, _ in SECTIONS]
    current = None
    buckets = {h: [] for h in heading_texts}
    stopped = False

    for kind, raw in blocks:
        plain = normalize_heading(raw)
        if plain.upper().startswith(STOP_HEADING.upper()):
            stopped = True
            break
        matched_heading = next(
            (h for h in heading_texts if plain.rstrip(" :").lower() == h.rstrip(" :").lower()),
            None,
        )
        if matched_heading:
            current = matched_heading
            continue
        if current and kind == "li":
            buckets[current].append(clean_entry_text(raw))

    if not stopped:
        print("Warning: did not find the stop heading "
              f"'{STOP_HEADING}' — double check SECTIONS still matches "
              "the doc's current headings.", file=sys.stderr)

    return buckets


def main():
    if not CV_DOC_ID:
        print("CV_DOC_ID environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    html = fetch_doc_html(CV_DOC_ID)

    parser = DocTextExtractor()
    parser.feed(html)

    buckets = parse_sections(parser.blocks)

    missing = [h for h, entries in buckets.items() if not entries]
    if missing:
        print(f"Warning: no entries found for: {', '.join(missing)}. "
              "Section headings in the doc may have changed wording — "
              "check SECTIONS in this script against the CV.", file=sys.stderr)

    section_html_blocks = []
    for heading, label in SECTIONS:
        entries = buckets.get(heading, [])
        if entries:
            section_html_blocks.append(build_section_html(label, entries))

    new_block = "\n\n".join(section_html_blocks)
    new_content = f"{START_MARKER}\n{new_block}\n    {END_MARKER}"

    with open(RESEARCH_HTML, "r", encoding="utf-8") as f:
        page = f.read()

    if START_MARKER not in page or END_MARKER not in page:
        print(f"Could not find the RESEARCH-AUTO markers in {RESEARCH_HTML}.",
              file=sys.stderr)
        sys.exit(1)

    pattern = re.compile(
        re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
        re.DOTALL,
    )
    updated_page = pattern.sub(new_content, page)

    if updated_page == page:
        print("No changes — research.html already matches the CV.")
        return

    with open(RESEARCH_HTML, "w", encoding="utf-8") as f:
        f.write(updated_page)
    print("research.html updated from CV.")


if __name__ == "__main__":
    main()
