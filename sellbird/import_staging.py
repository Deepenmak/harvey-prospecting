"""Turn a file someone hands you into staged rows.

A CSV exported from somebody's CRM, a Sellbird export, or a bare list of
addresses pasted into a box. None of them share column names, so the first
job is working out which column means what — and the second is making sure
that guess is visible and correctable, because an import that silently reads
the wrong column spends real credits on nonsense.

Nothing here calls Apollo or writes a prospect. It parses, maps and stages.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re

from sellbird.import_store import ROW_FIELDS

logger = logging.getLogger("sellbird.imports")

# Guard rails on what can be staged in one go. The file is read in the
# browser and posted as text, so these are the only limits that matter.
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_ROWS = 20_000
MAX_COLUMNS = 200

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Header names seen in the wild, lowercased and stripped of punctuation.
# First match wins, so the more specific alias comes first.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "email": ("email", "email address", "work email", "business email",
              "e mail", "emailaddress", "primary email", "mail"),
    "first_name": ("first name", "firstname", "given name", "first"),
    "last_name": ("last name", "lastname", "surname", "family name", "last"),
    "title": ("title", "job title", "position", "role", "designation",
              "headline"),
    "company": ("company", "company name", "organization", "organisation",
                "account", "employer", "company name for emails"),
    "domain": ("domain", "website", "company domain", "company website",
               "url", "primary domain", "site"),
    "linkedin_url": ("linkedin", "linkedin url", "linkedin profile",
                     "person linkedin url", "profile url"),
}
# A single "name" column is split rather than dropped.
FULL_NAME_ALIASES = ("name", "full name", "contact", "contact name", "person")


class StagingError(ValueError):
    """The file cannot be staged, and the message says why in plain words."""


def _norm_header(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(value or "").lower()).strip()


def map_headers(headers: list[str]) -> dict[str, str]:
    """Guess which column holds which field. Header names only — no data.

    Returns {column: field}. Columns it cannot place are simply absent; they
    stay in the row's raw JSON and are never lost.
    """
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for header in headers:
        norm = _norm_header(header)
        if not norm:
            continue
        for field, aliases in FIELD_ALIASES.items():
            if field in taken:
                continue
            if norm in aliases or any(norm == a for a in aliases):
                mapping[header] = field
                taken.add(field)
                break
    # Second pass: a header that merely contains an alias ("Work Email 1").
    for header in headers:
        if header in mapping:
            continue
        norm = _norm_header(header)
        for field, aliases in FIELD_ALIASES.items():
            if field in taken:
                continue
            if any(a in norm for a in aliases):
                mapping[header] = field
                taken.add(field)
                break
    # A lone "name" column, when there is no first name anywhere.
    if "first_name" not in taken:
        for header in headers:
            if header not in mapping and _norm_header(header) in FULL_NAME_ALIASES:
                mapping[header] = "full_name"
                break
    return mapping


def _split_name(value: str) -> tuple[str, str]:
    parts = [p for p in str(value or "").split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _domain_of(value: str) -> str:
    value = str(value or "").strip().lower()
    if not value:
        return ""
    value = re.sub(r"^https?://", "", value).split("/")[0]
    return value[4:] if value.startswith("www.") else value


def parse_csv(text: str) -> tuple[list[str], list[dict]]:
    """Headers and raw rows. Delimiter is sniffed, so semicolon and tab
    exports (Excel in much of Europe) work without being told."""
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    if not headers:
        raise StagingError("that file has no header row")
    if len(headers) > MAX_COLUMNS:
        raise StagingError(f"{len(headers)} columns is more than Sellbird reads")
    rows = []
    for i, row in enumerate(reader):
        if i >= MAX_ROWS:
            raise StagingError(f"more than {MAX_ROWS:,} rows; split the file")
        rows.append({k: v for k, v in row.items() if k is not None})
    if not rows:
        raise StagingError("that file has a header but no rows")
    return headers, rows


def parse_emails(text: str) -> tuple[list[str], list[dict]]:
    """A pasted list of addresses: one per line, or separated by commas."""
    found = []
    seen = set()
    for match in EMAIL_RE.finditer(text):
        address = match.group(0).lower()
        if address not in seen:
            seen.add(address)
            found.append({"email": address})
    if not found:
        raise StagingError("no email address in that text")
    if len(found) > MAX_ROWS:
        raise StagingError(f"more than {MAX_ROWS:,} addresses; split the list")
    return ["email"], found


def parse_json(text: str) -> tuple[list[str], list[dict]]:
    """A Sellbird export, or any JSON list of objects."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise StagingError(f"that isn't valid JSON ({e.msg}, line {e.lineno})")
    if isinstance(data, dict):
        for key in ("contacts", "rows", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list) or not data:
        raise StagingError('expected a list of contacts, or {"contacts": [...]}')
    if len(data) > MAX_ROWS:
        raise StagingError(f"more than {MAX_ROWS:,} rows; split the file")
    rows = [r for r in data if isinstance(r, dict)]
    if not rows:
        raise StagingError("that list holds no contact objects")
    headers: list[str] = []
    for row in rows[:50]:
        for key in row:
            if key not in headers:
                headers.append(key)
    return headers, rows


def detect_format(name: str, text: str) -> str:
    """What this text is, decided by its first line rather than its name.

    The telling detail is whether that line reads as column headings or as
    data: a header never consists of email addresses, and a pasted list
    always does.
    """
    stripped = text.lstrip()
    if stripped[:1] in "[{" or name.lower().endswith(".json"):
        return "json"
    first = stripped.split("\n", 1)[0].strip()
    cells = [c.strip() for c in re.split(r"[,;\t]", first) if c.strip()]
    if cells and all(EMAIL_RE.fullmatch(c) for c in cells):
        return "paste"
    if any(d in first for d in (",", ";", "\t")):
        return "csv"
    # A single column: a heading, or just the first address of a list.
    return "paste" if (not cells or EMAIL_RE.fullmatch(cells[0])) else "csv"


def parse(text: str, fmt: str = "", name: str = "") -> tuple[str, list[str], list[dict]]:
    """(format, headers, raw rows) for whatever arrived."""
    if not isinstance(text, str) or not text.strip():
        raise StagingError("there is nothing in that file")
    if len(text.encode("utf-8", "ignore")) > MAX_TEXT_BYTES:
        raise StagingError("that file is larger than 8 MB; split it")
    fmt = (fmt or "").strip().lower() or detect_format(name, text)
    if fmt == "json":
        return ("json", *parse_json(text))
    if fmt == "paste":
        return ("paste", *parse_emails(text))
    try:
        return ("csv", *parse_csv(text))
    except StagingError:
        # A one-column paste of addresses is a CSV with no header. Try that
        # before telling the user their file is unreadable.
        return ("paste", *parse_emails(text))


def rows_from(raw_rows: list[dict], mapping: dict[str, str]) -> list[dict]:
    """Apply a column mapping. The source row is kept verbatim in ``_raw``.

    A row with neither an address nor a company is dropped: there is nothing
    Apollo could do with it and nothing to show a user.
    """
    staged = []
    for raw in raw_rows:
        row = {f: "" for f in ROW_FIELDS}
        for column, field in mapping.items():
            value = str(raw.get(column, "") or "").strip()
            if not value:
                continue
            if field == "full_name":
                first, last = _split_name(value)
                row["first_name"] = row["first_name"] or first
                row["last_name"] = row["last_name"] or last
            elif field in row:
                row[field] = value
        # Fields the file didn't carry but the data implies.
        if row["email"]:
            address = EMAIL_RE.search(row["email"])
            row["email"] = address.group(0).lower() if address else ""
        if row["domain"]:
            row["domain"] = _domain_of(row["domain"])
        if not row["domain"] and row["email"] and "@" in row["email"]:
            row["domain"] = row["email"].split("@", 1)[1]
        if not (row["email"] or row["company"] or row["domain"]):
            continue
        row["_raw"] = raw
        staged.append(row)
    return staged


def sample_for_analysis(raw_rows: list[dict], headers: list[str],
                        limit: int = 10) -> list[dict]:
    """A handful of rows for Claude to look at. Never the whole file: the
    point is to read the shape, and a prompt holding ten thousand strangers'
    addresses is a liability, not a better answer."""
    return [{h: str(r.get(h, ""))[:120] for h in headers[:40]}
            for r in raw_rows[:limit]]
