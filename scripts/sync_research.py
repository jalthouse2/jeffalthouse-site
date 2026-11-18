#!/usr/bin/env python3
"""
sync_research.py

Pulls the sub-sections of the "PUBLICATIONS" part of Jeff's CV
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
# Headings are matched case-insensitively, and "&" / "and" are treated
# as the same word, so "Reports for NGOs, public and multilateral
# institutions" matches the entry below.
SECTIONS = [
    ("Articles in Peer-Reviewed Journals", "Articles in Peer-Reviewed Journals"),
    ("Book Chapters in Edited Volumes", "Book Chapters in Edited Volumes"),
    ("Reports for NGOs, Public & Multilateral Institutions", "Reports for NGOs, Public & Multilateral Institutions"),
    ("Working Papers", "Working Papers"),
    ("Popular Media", "Popular Media"),
]

# The script only reads the part of the CV that starts at this
# top-level heading, and stops at the next top-level (ALL-CAPS)
# heading after it (currently "TEACHING EXPERIENCE"). That keeps it
# from ever picking up teaching or job bullets, whatever their wording.
START_HEADING = "PUBLICATIONS"

# Entries to keep off the site until a given date (YYYY-MM-DD, UTC),
# e.g. papers not yet announced. Match is a case-insensitive piece of
# the citation text (the title is safest). Once the date has passed,
# the entry appears on the next sync and its line here can be deleted.
HOLD_UNTIL = [
    ("The changing dynamics of financial subordination", "2026-12-16"),
    ("New nature-economy models are needed", "2026-12-16"),
]


class DocTextExtractor(HTMLParser):
    """Turns the Google Docs HTML export into a flat list of
    (kind, text_with_inline_html) blocks, one per paragraph/list item,
    in document order. kind is 'p' for a normal paragraph/heading and
    'li' for a bulleted entry."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.raw_blocks = []
        self._buf = []
        self._raw_buf = []
        self._tag_stack = []
        self._current_kind = None
        self._in_body = False
        self._span_stack = []
        self._in_style = False
        self._style_buf = []
        self.class_styles = {}
        # Table state: the CV lays entries out as two-column table rows
        # (year | citation). Each <tr> becomes one ("row", [cell, ...])
        # block, where each cell is its paragraphs joined by spaces.
        self._row = None
        self._cell = None
        self._raw_row = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "style":
            self._in_style = True
            self._style_buf = []
            return
        if tag == "body":
            self._in_body = True
        if not self._in_body:
            return
        if tag == "tr":
            self._row, self._raw_row = [], []
            return
        if tag == "td":
            self._cell = []
            return
        if tag in ("p", "li"):
            self._buf = []
            self._raw_buf = []
            self._current_kind = "li" if tag == "li" else "p"
        elif self._current_kind is not None:
            self._raw_buf.append(self.get_starttag_text() or f"<{tag}>")

        if tag == "a":
            href = attrs_dict.get("href", "")
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
            # Google Docs' HTML export represents bold/italic either as
            # inline CSS on the <span> (style="font-weight:700") or,
            # more commonly, as a numbered CSS class defined in a
            # <style> block at the top of the document (class="c3",
            # with ".c3{font-weight:700}" declared separately). Check
            # both.
            classes = attrs_dict.get("class", "").split()
            style_parts = [self.class_styles.get(c, "") for c in classes]
            style_parts.append(attrs_dict.get("style", "") or "")
            style = ";".join(style_parts)
            opens = []
            if re.search(r"font-weight\s*:\s*(700|800|900|bold)", style, re.I):
                opens.append("strong")
            if re.search(r"font-style\s*:\s*italic", style, re.I):
                opens.append("em")
            for tagname in opens:
                self._buf.append(f"<{tagname}>")
            self._span_stack.append(opens)

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False
            css_text = "".join(self._style_buf)
            # Match rules like ".c3{...}" or ".c3,.c8{...}"
            for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css_text):
                for cls in selector.split(","):
                    cls = cls.strip().lstrip(".")
                    if not cls:
                        continue
                    self.class_styles[cls] = (
                        self.class_styles.get(cls, "") + ";" + body
                    )
            return
        if tag == "td" and self._cell is not None:
            if self._row is not None:
                self._row.append(" ".join(self._cell))
            self._cell = None
            return
        if tag == "tr" and self._row is not None:
            if any(c.strip() for c in self._row):
                self.blocks.append(("row", self._row))
                self.raw_blocks.append(" | ".join(self._raw_row))
            self._row = self._raw_row = None
            return
        if tag in ("p", "li") and self._current_kind is not None:
            text = "".join(self._buf).strip()
            raw_text = "".join(self._raw_buf).strip()
            if text and self._cell is not None:
                self._cell.append(text)
                if self._raw_row is not None:
                    self._raw_row.append(raw_text)
            elif text:
                self.blocks.append((self._current_kind, text))
                self.raw_blocks.append(raw_text)
            self._buf = []
            self._raw_buf = []
            self._current_kind = None
            return
        if self._current_kind is not None:
            self._raw_buf.append(f"</{tag}>")
        if tag == "a":
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
        if self._in_style:
            self._style_buf.append(data)
            return
        if self._current_kind is not None:
            self._buf.append(data)
            self._raw_buf.append(data)


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
        f'      <h2>{label}</h2>\n'
        f'      <ul class="pub-list">\n'
        f'{items}\n'
        f'      </ul>\n'
        f'    </section>'
    )


def _heading_key(text):
    t = normalize_heading(text).rstrip(" :").lower()
    t = t.replace("&", " and ")
    return re.sub(r"\s+", " ", t).strip()


def _is_top_heading(plain):
    # Top-level CV headings are written in ALL CAPS ("TEACHING
    # EXPERIENCE"); section sub-headings and entries are not.
    letters = [c for c in plain if c.isalpha()]
    return len(letters) >= 4 and all(c.isupper() for c in letters)


def parse_sections(blocks, raw_blocks=None):
    """Walk the flat block list and bucket entries under each heading
    in SECTIONS. Only the part of the doc between START_HEADING and the
    next ALL-CAPS heading is read. An entry is either a table row (the
    last non-empty cell is the citation; the first is the year) or a
    bulleted list item. Each bucketed entry is a (cleaned_text,
    raw_html) pair — raw_html is kept only for diagnostics."""
    if raw_blocks is None:
        raw_blocks = [""] * len(blocks)

    keys = {_heading_key(h): h for h, _ in SECTIONS}
    buckets = {h: [] for h, _ in SECTIONS}
    current = None
    in_pubs = False
    found_start = False

    for (kind, raw), raw_html in zip(blocks, raw_blocks):
        if kind in ("p", "li"):
            plain = normalize_heading(raw)
            key = _heading_key(raw)
            if not in_pubs:
                if kind == "p" and key == _heading_key(START_HEADING):
                    in_pubs = found_start = True
                continue
            if kind == "p" and _is_top_heading(plain):
                break  # reached the next top-level CV section
            if key in keys:
                current = keys[key]
                continue
            if kind == "p":
                continue  # stray non-entry paragraph
            cleaned = clean_entry_text(raw)
        else:  # table row
            if not in_pubs:
                continue
            cells = [c for c in raw if re.sub(r"<[^>]+>", "", c).strip()]
            if not cells:
                continue
            cleaned = clean_entry_text(cells[-1])

        if not cleaned or not current:
            continue

        # Google Docs sometimes leaves a trailing link (e.g. just
        # "[PDF]") stranded in its own paragraph/row. Glue it onto the
        # previous entry rather than adding a spurious entry.
        text_only = re.sub(r"<[^>]+>", "", cleaned).strip()
        if re.fullmatch(r"\[?(PDF|Link)\]?", text_only, re.I) and buckets[current]:
            prev_cleaned, prev_raw = buckets[current][-1]
            buckets[current][-1] = (prev_cleaned.rstrip() + " " + cleaned,
                                    prev_raw + " || ORPHAN_MERGED || " + raw_html)
            continue

        buckets[current].append((cleaned, raw_html))

    if not found_start:
        print(f"Warning: did not find the '{START_HEADING}' heading in the "
              "CV — nothing was read.", file=sys.stderr)

    return buckets


def main():
    if not CV_DOC_ID:
        print("CV_DOC_ID environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    html = fetch_doc_html(CV_DOC_ID)

    parser = DocTextExtractor()
    parser.feed(html)

    buckets = parse_sections(parser.blocks, parser.raw_blocks)

    missing = [h for h, entries in buckets.items() if not entries]
    if missing:
        print(f"Warning: no entries found for: {', '.join(missing)}. "
              "Section headings in the doc may have changed wording — "
              "check SECTIONS in this script against the CV.", file=sys.stderr)

    for heading, entries in buckets.items():
        for cleaned, raw_html in entries:
            mentions_link_word = re.search(r"\[(PDF|Link)\]", cleaned, re.I)
            has_anchor = "<a " in cleaned
            if mentions_link_word and not has_anchor:
                snippet = re.sub(r"<[^>]+>", "", cleaned)[:90]
                print(f"Warning: entry in '{heading}' mentions "
                      f"{mentions_link_word.group(0)} but has no hyperlink "
                      f"attached — check this citation in the CV doc: "
                      f"\"{snippet}...\"", file=sys.stderr)
                print(f"  Raw source HTML for that entry: {raw_html}",
                      file=sys.stderr)

    if all(not entries for entries in buckets.values()):
        print("No publication entries found at all — leaving research.html "
              "untouched.", file=sys.stderr)
        sys.exit(1)

    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

    def on_hold(entry):
        text = re.sub(r"<[^>]+>", "", entry).lower()
        for needle, until in HOLD_UNTIL:
            if needle.lower() in text and today < until:
                print(f"Holding back until {until}: {needle}")
                return True
        return False

    section_html_blocks = []
    for heading, label in SECTIONS:
        entries = [cleaned for cleaned, _raw in buckets.get(heading, [])
                   if not on_hold(cleaned)]
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
