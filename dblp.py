#!/usr/bin/env python3
"""Fetch a DBLP BibTeX entry by paper title and print selected fields."""

import curses
import json
import re
import sys
import textwrap
import urllib.parse
import urllib.request


FIELD_ORDER = [
    "author",
    "title",
    "journal",
    "booktitle",
    "series",
    "volume",
    "number",
    "pages",
    "year",
    "doi",
]
MAX_HITS = 10
JOURNAL_EXPANSIONS = {
    "commun acm": "Communications of the ACM",
    "j cryptol": "Journal of Cryptology",
}


def http_get(url: str) -> str:
    """Perform a GET request with a minimal User-Agent and return the body as text."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "dblp-bibtex-fetcher/0.1"}
    )
    with urllib.request.urlopen(request) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset)


def search_hits(title: str) -> list[dict]:
    """Query DBLP for the title and return a list of hit dicts."""
    query = urllib.parse.quote_plus(title)
    url = f"https://dblp.org/search/publ/api?q={query}&format=json&h={MAX_HITS}"
    payload = http_get(url)
    data = json.loads(payload)
    hits = data.get("result", {}).get("hits", {})
    raw_hits = hits.get("hit") or []
    if isinstance(raw_hits, dict):
        raw_hits = [raw_hits]
    return raw_hits


def describe_hit(info: dict) -> str:
    """Create a one-line description for a hit to show in the menu."""
    venue = info.get("venue") or info.get("journal") or info.get("booktitle") or ""
    year = info.get("year", "")
    title = info.get("title", "")
    doi = info.get("doi")
    main = f"{venue} {year}".strip()
    suffix = f" — {doi}" if doi else ""
    if main:
        return f"{main}: {title}{suffix}"
    return f"{title}{suffix}"


def choose_hit(hits: list[dict]) -> dict:
    """Prompt the user to select a hit with up/down arrows."""
    if len(hits) == 1:
        return hits[0]

    descriptions = [describe_hit(hit.get("info", {})) for hit in hits]

    def choose_with_numbers() -> dict:
        print("Multiple entries found. Type the number to select:")
        for i, desc in enumerate(descriptions, start=1):
            print(f"  {i}. {desc}")
        while True:
            try:
                choice = input(f"Select [1-{len(hits)}]: ").strip() or "1"
            except EOFError:
                return hits[0]
            if choice.lower() == "q":
                raise KeyboardInterrupt
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(hits):
                    return hits[idx]
            print("Invalid selection. Try again.")

    def selector(stdscr):
        curses.curs_set(0)
        idx = 0
        while True:
            stdscr.clear()
            height, width = stdscr.getmaxyx()
            stdscr.addstr(0, 0, "Select DBLP entry (Up/Down, Enter):")
            for i, desc in enumerate(descriptions):
                if i + 1 >= height:
                    break
                line = textwrap.shorten(desc, width=width - 4, placeholder="...")
                prefix = "> " if i == idx else "  "
                attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
                stdscr.addstr(i + 1, 0, prefix + line, attr)
            key = stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                idx = (idx - 1) % len(hits)
            elif key in (curses.KEY_DOWN, ord("j")):
                idx = (idx + 1) % len(hits)
            elif key in (curses.KEY_ENTER, ord("\n"), 10, 13):
                return hits[idx]
            elif key in (27, ord("q"), ord("Q")):
                raise KeyboardInterrupt

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return choose_with_numbers()
    try:
        return curses.wrapper(selector)
    except curses.error:
        return choose_with_numbers()


def find_entry_key(title: str) -> str:
    """Return the DBLP key for the chosen publication hit for the title."""
    hits = search_hits(title)
    if not hits:
        raise ValueError(f"No DBLP entry found for title: {title}")
    chosen = choose_hit(hits)
    info = chosen.get("info", {})
    key = info.get("key")
    if not key:
        raise ValueError("DBLP response did not include an entry key.")
    return key


def fetch_bibtex_entry(key: str) -> str:
    """Fetch the BibTeX entry text for a given DBLP key."""
    bib_url = f"https://dblp.org/rec/{key}.bib"
    return http_get(bib_url)


def parse_entry_header(bibtex: str) -> tuple[str, str]:
    """Extract the entry type and key from the BibTeX header line."""
    match = re.search(r"@\s*([A-Za-z0-9_:-]+)\s*{\s*([^,\s]+)", bibtex)
    if not match:
        return "entry", "unknown"
    entry_key = match.group(2)
    if entry_key.startswith("DBLP:"):
        entry_key = entry_key.split(":", 1)[1]
    if "/" in entry_key:
        entry_key = entry_key.rsplit("/", 1)[1]
    return match.group(1), entry_key


def _extract_braced_value(text: str, start_index: int) -> str | None:
    """Read a brace-wrapped value starting at the opening brace index."""
    if start_index >= len(text) or text[start_index] != "{":
        return None
    depth = 0
    value_chars: list[str] = []
    i = start_index + 1
    while i < len(text):
        char = text[i]
        if char == "{":
            depth += 1
            value_chars.append(char)
        elif char == "}":
            if depth == 0:
                return "".join(value_chars).strip()
            depth -= 1
            value_chars.append(char)
        else:
            value_chars.append(char)
        i += 1
    return None


def _extract_quoted_value(text: str, start_index: int) -> str | None:
    """Read a quote-wrapped value starting at the opening quote index."""
    if start_index >= len(text) or text[start_index] != '"':
        return None
    value_chars: list[str] = []
    i = start_index + 1
    while i < len(text):
        char = text[i]
        if char == '"' and text[i - 1] != "\\":
            return "".join(value_chars).strip()
        value_chars.append(char)
        i += 1
    return None


def extract_field_value(bibtex: str, field_name: str) -> str | None:
    """Pull a single field value out of the BibTeX text, handling braces."""
    pattern = re.compile(rf"{re.escape(field_name)}\s*=\s*([{{\"])", re.IGNORECASE)
    match = pattern.search(bibtex)
    if not match:
        return None
    delimiter = match.group(1)
    start_index = match.end() - 1  # points at the delimiter
    if delimiter == "{":
        return _extract_braced_value(bibtex, start_index)
    return _extract_quoted_value(bibtex, start_index)


def render_entry(entry_type: str, entry_key: str, fields: dict[str, str | None]) -> str:
    """Format the BibTeX entry with only the requested fields."""
    width = max(len(field) for field in FIELD_ORDER)
    lines = ["%", f"@{entry_type}{{{entry_key},"]
    for name in FIELD_ORDER:
        value = fields.get(name)
        if not value:
            continue
        value_oneline = re.sub(r"\s+", " ", value).strip()
        prefix = f"  {name:<{width}} = {{"
        lines.append(prefix + value_oneline + "},")
    lines.append("}")
    return "\n".join(lines)


def _normalize_journal_name(value: str) -> str:
    """Normalize a journal name for matching against expansions."""
    no_braces = re.sub(r"[{}]", "", value)
    no_punct = re.sub(r"[.,:;]", "", no_braces)
    return re.sub(r"\s+", " ", no_punct).strip().lower()


def expand_journal_name(value: str | None) -> str | None:
    """Expand known abbreviated journal names to full titles."""
    if not value:
        return value
    normalized = _normalize_journal_name(value)
    return JOURNAL_EXPANSIONS.get(normalized, value)


def merge_crossref_fields(
    bibtex: str, fields: dict[str, str | None]
) -> dict[str, str | None]:
    """Populate missing fields from a crossref entry if available."""
    if fields.get("booktitle") or fields.get("series"):
        return fields
    crossref = extract_field_value(bibtex, "crossref")
    if not crossref:
        return fields
    try:
        crossref_bibtex = fetch_bibtex_entry(crossref)
    except Exception:
        return fields
    for name in ("booktitle", "series", "year"):
        if not fields.get(name):
            fields[name] = extract_field_value(crossref_bibtex, name)
    return fields


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: python dblp.py "Paper Title"', file=sys.stderr)
        sys.exit(1)
    title = " ".join(sys.argv[1:])
    try:
        key = find_entry_key(title)
        bibtex = fetch_bibtex_entry(key)
        entry_type, entry_key = parse_entry_header(bibtex)
        fields = {name: extract_field_value(bibtex, name) for name in FIELD_ORDER}
        fields = merge_crossref_fields(bibtex, fields)
        if "journal" in fields:
            fields["journal"] = expand_journal_name(fields["journal"])
        print(render_entry(entry_type, entry_key, fields))
    except KeyboardInterrupt:
        print("\nSelection cancelled.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # pragma: no cover - CLI error handling
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
