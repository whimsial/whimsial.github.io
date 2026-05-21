#!/usr/bin/env python3
"""Fetch the publication list for the website.

Pulls the authoritative list of works from ORCID, merges in any hand-maintained
entries from ``_bibliography/extra.bib`` and enriches every item that has a DOI
with author lists / venue (Crossref) and abstracts / open-access PDF links
(Semantic Scholar).

The merged, normalised list is written to ``_data/publications.yml``, which the
Jekyll site renders. The script is resilient: per-paper API failures are
skipped, and if ORCID itself cannot be reached the existing data file is left
untouched.

Configuration via environment variables (all optional):
    ORCID_ID        ORCID iD to fetch (default: 0000-0003-4031-0073)
    CONTACT_EMAIL   Crossref "polite pool" contact email
                    (defaults to author.email in _config.yml)
    S2_API_KEY      Semantic Scholar API key for higher rate limits
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

try:
    import bibtexparser
    from bibtexparser.customization import author as split_authors
    from bibtexparser.customization import convert_to_unicode
except ImportError:  # pragma: no cover
    bibtexparser = None


# --- configuration -----------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "_config.yml"
BIB_PATH = ROOT / "_bibliography" / "extra.bib"
OUT_PATH = ROOT / "_data" / "publications.yml"


def _config_email():
    """Contact email for the Crossref 'polite pool', read from _config.yml."""
    try:
        with CONFIG_PATH.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        email = (config.get("author") or {}).get("email")
        if email:
            return str(email).strip()
    except (OSError, yaml.YAMLError):
        pass
    return "noreply@example.com"


ORCID_ID = os.environ.get("ORCID_ID", "0000-0003-4031-0073")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL") or _config_email()
S2_API_KEY = os.environ.get("S2_API_KEY", "")

USER_AGENT = f"whimsial.github.io publication fetcher (mailto:{CONTACT_EMAIL})"
REQUEST_TIMEOUT = 30

# ORCID work type -> normalised type used by the site
ORCID_TYPE_MAP = {
    "journal-article": "article",
    "preprint": "preprint",
    "conference-paper": "conference",
    "conference-poster": "poster",
    "conference-abstract": "poster",
    "dissertation-thesis": "thesis",
    "book-chapter": "chapter",
    "book": "book",
    "report": "report",
}
# BibTeX entry type -> normalised type
BIB_TYPE_MAP = {
    "article": "article",
    "inproceedings": "conference",
    "conference": "conference",
    "proceedings": "conference",
    "phdthesis": "thesis",
    "mastersthesis": "thesis",
    "incollection": "chapter",
    "book": "book",
    "techreport": "report",
    "misc": "preprint",
    "unpublished": "preprint",
}

# order of keys written for each publication record
RECORD_KEYS = (
    "title", "authors", "year", "venue", "type",
    "doi", "url", "pdf_url", "abstract", "source",
)


# --- helpers -----------------------------------------------------------------

def http_get_json(url, headers=None, timeout=REQUEST_TIMEOUT, retries=3):
    """GET ``url`` and parse JSON, retrying transient failures with backoff."""
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, **(headers or {})}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            last_err = err
            if err.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last_err = err
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise
    raise last_err  # pragma: no cover


def norm_title(title):
    """Lower-cased, alphanumeric-only title for duplicate detection."""
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def norm_doi(doi):
    """Bare, lower-cased DOI with any URL/`doi:` prefix removed."""
    if not doi:
        return None
    doi = doi.strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi or None


def strip_braces(text):
    """Drop BibTeX brace protection and collapse whitespace."""
    if not text:
        return text
    return re.sub(r"\s+", " ", text.replace("{", "").replace("}", "")).strip()


def format_name(name):
    """Normalise an author name to ``First Last`` order."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    if "," in name:
        last, first = (part.strip() for part in name.split(",", 1))
        return f"{first} {last}".strip()
    return name


def empty_record():
    return {key: None for key in RECORD_KEYS} | {"authors": [], "put_code": None}


# --- ORCID -------------------------------------------------------------------

def _summary_doi(summary):
    """Best DOI from an ORCID work summary, preferring a 'self' relationship."""
    ext = (summary.get("external-ids") or {}).get("external-id") or []
    fallback = None
    for entry in ext:
        if (entry.get("external-id-type") or "").lower() != "doi":
            continue
        value = norm_doi(entry.get("external-id-value"))
        if not value:
            continue
        if (entry.get("external-id-relationship") or "").lower() == "self":
            return value
        fallback = fallback or value
    return fallback


def _record_from_orcid(summary):
    record = empty_record()
    title_block = (summary.get("title") or {}).get("title") or {}
    record["title"] = strip_braces(title_block.get("value"))
    record["type"] = ORCID_TYPE_MAP.get((summary.get("type") or "").lower(), "other")
    year = ((summary.get("publication-date") or {}).get("year") or {}).get("value")
    record["year"] = int(year) if year and str(year).isdigit() else None
    journal = summary.get("journal-title") or {}
    record["venue"] = journal.get("value")
    record["doi"] = _summary_doi(summary)
    url = summary.get("url") or {}
    record["url"] = url.get("value")
    record["source"] = "orcid"
    record["put_code"] = summary.get("put-code")
    return record


def fetch_orcid_works(orcid_id):
    """Authoritative list of works from the public ORCID record."""
    data = http_get_json(
        f"https://pub.orcid.org/v3.0/{orcid_id}/works",
        headers={"Accept": "application/json"},
    )
    records = []
    for group in data.get("group", []):
        summaries = group.get("work-summary") or []
        if not summaries:
            continue
        chosen = summaries[0]
        for summary in summaries:  # prefer a summary that carries a DOI
            if _summary_doi(summary):
                chosen = summary
                break
        records.append(_record_from_orcid(chosen))
    return records


def fetch_orcid_contributors(orcid_id, put_code):
    """Author names from a detailed ORCID work record (used when no DOI)."""
    data = http_get_json(
        f"https://pub.orcid.org/v3.0/{orcid_id}/work/{put_code}",
        headers={"Accept": "application/json"},
    )
    contributors = (data.get("contributors") or {}).get("contributor") or []
    names = []
    for contributor in contributors:
        name = (contributor.get("credit-name") or {}).get("value")
        if name:
            names.append(format_name(name))
    return names


# --- BibTeX ------------------------------------------------------------------

def _bib_customize(entry):
    return split_authors(convert_to_unicode(entry))


def _record_from_bib(entry):
    record = empty_record()
    record["title"] = strip_braces(entry.get("title", ""))
    authors = entry.get("author") or []
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(" and ")]
    record["authors"] = [format_name(strip_braces(a)) for a in authors if a.strip()]
    year = entry.get("year")
    record["year"] = int(year) if year and str(year).strip().isdigit() else None
    venue = entry.get("journal") or entry.get("booktitle") or entry.get("publisher")
    record["venue"] = strip_braces(venue) if venue else None
    record["type"] = BIB_TYPE_MAP.get((entry.get("ENTRYTYPE") or "misc").lower(), "other")
    record["doi"] = norm_doi(entry.get("doi"))
    record["url"] = entry.get("url") or None
    record["pdf_url"] = entry.get("pdf") or None
    record["abstract"] = strip_braces(entry.get("abstract")) if entry.get("abstract") else None
    record["source"] = "bib"
    return record


def parse_bib(path):
    """Parse the hand-maintained BibTeX supplement, if present and non-empty."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    if bibtexparser is None:
        print("WARNING: bibtexparser not installed; skipping extra.bib", file=sys.stderr)
        return []
    parser = bibtexparser.bparser.BibTexParser(common_strings=True)
    parser.customization = _bib_customize
    parser.ignore_nonstandard_types = False
    database = bibtexparser.loads(text, parser=parser)
    return [_record_from_bib(entry) for entry in database.entries]


# --- merge & enrich ----------------------------------------------------------

def merge(orcid_records, bib_records):
    """Append BibTeX entries that ORCID does not already contain."""
    merged = list(orcid_records)
    seen_dois = {r["doi"] for r in orcid_records if r["doi"]}
    seen_titles = {norm_title(r["title"]) for r in orcid_records if r["title"]}
    for record in bib_records:
        if record["doi"] and record["doi"] in seen_dois:
            continue
        if record["title"] and norm_title(record["title"]) in seen_titles:
            continue
        merged.append(record)
        if record["doi"]:
            seen_dois.add(record["doi"])
        if record["title"]:
            seen_titles.add(norm_title(record["title"]))
    return merged


def enrich_crossref(record):
    """Fill author list / venue / year from Crossref."""
    try:
        data = http_get_json(f"https://api.crossref.org/works/{record['doi']}")
    except Exception as err:  # noqa: BLE001 - per-paper failures are non-fatal
        print(f"  crossref failed for {record['doi']}: {err}", file=sys.stderr)
        return
    message = data.get("message", {})
    authors = []
    for author in message.get("author", []):
        full = f"{author.get('given', '')} {author.get('family', '')}".strip()
        authors.append(full or author.get("name", ""))
    authors = [a for a in authors if a]
    if authors:
        record["authors"] = authors
    container = message.get("container-title") or []
    if container and not record["venue"]:
        record["venue"] = container[0]
    if not record["year"]:
        parts = (message.get("issued", {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            record["year"] = int(parts[0])


def enrich_semantic_scholar(record):
    """Fill abstract / open-access PDF link from Semantic Scholar."""
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/DOI:{record['doi']}"
        "?fields=abstract,openAccessPdf"
    )
    headers = {"x-api-key": S2_API_KEY} if S2_API_KEY else {}
    try:
        data = http_get_json(url, headers=headers)
    except Exception as err:  # noqa: BLE001 - per-paper failures are non-fatal
        print(f"  semantic scholar failed for {record['doi']}: {err}", file=sys.stderr)
        return
    if data.get("abstract") and not record["abstract"]:
        record["abstract"] = data["abstract"]
    open_access = data.get("openAccessPdf") or {}
    if open_access.get("url"):
        record["pdf_url"] = open_access["url"]


# --- main --------------------------------------------------------------------

def main():
    print(f"Fetching ORCID works for {ORCID_ID} ...")
    try:
        orcid_records = fetch_orcid_works(ORCID_ID)
    except Exception as err:  # noqa: BLE001
        print(f"ERROR: could not fetch ORCID works: {err}", file=sys.stderr)
        print("Leaving any existing publications.yml untouched.", file=sys.stderr)
        return 1
    print(f"  {len(orcid_records)} works from ORCID")

    bib_records = parse_bib(BIB_PATH)
    print(f"  {len(bib_records)} entries from {BIB_PATH.name}")

    records = merge(orcid_records, bib_records)
    print(f"  {len(records)} works after merge/dedup")

    # enrich every work that has a DOI
    for record in records:
        if not record["doi"]:
            continue
        print(f"  enriching {record['doi']} ...")
        enrich_crossref(record)
        time.sleep(0.5)
        enrich_semantic_scholar(record)
        time.sleep(1.0)

    # fill any still-missing author lists from the detailed ORCID record
    for record in records:
        if record["authors"] or record["source"] != "orcid" or not record["put_code"]:
            continue
        try:
            record["authors"] = fetch_orcid_contributors(ORCID_ID, record["put_code"])
        except Exception as err:  # noqa: BLE001
            print(f"  orcid detail failed for {record['put_code']}: {err}", file=sys.stderr)
        time.sleep(0.3)

    # finalise: ensure a link, drop internal fields, sort newest first
    output = []
    for record in records:
        if not record["url"]:
            record["url"] = f"https://doi.org/{record['doi']}" if record["doi"] else None
        output.append({key: record.get(key) for key in RECORD_KEYS})
    output.sort(key=lambda r: (-(r["year"] or 0), (r["title"] or "").lower()))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as handle:
        handle.write(
            "# Auto-generated by scripts/fetch_publications.py - do not edit by hand.\n"
            f"# Source: ORCID ({ORCID_ID}) + _bibliography/extra.bib\n"
        )
        yaml.safe_dump(output, handle, allow_unicode=True, sort_keys=False, width=100)
    print(f"Wrote {len(output)} publications to {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
