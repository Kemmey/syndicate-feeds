"""Shared helpers for the Syndicate feed catalog scripts.

Reads the upstream OPML (kept unedited), overlays source/custom-feeds.json,
drops source/exclusions.json and dedupes by normalised feed URL. Both
validate.py and update.py build the catalog through `load_sources()` so they
always agree on which feeds exist and what their ids are.

Python 3 standard library only.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urlunsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_DIR = os.path.join(ROOT, "source")
STATE_PATH = os.path.join(ROOT, "state", "feed-health.json")
DIST_PATH = os.path.join(ROOT, "dist", "feeds-v1.json")
REPORT_PATH = os.path.join(ROOT, "reports", "latest.md")
EXCLUSIONS_PATH = os.path.join(SOURCE_DIR, "exclusions.json")

RECOMMENDED_DIR = os.path.join(ROOT, "recommended", "with_category")
COUNTRIES_DIR = os.path.join(ROOT, "countries", "with_category")


# --------------------------------------------------------------------------
# JSON helpers


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def write_json(path: str, value) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# URL normalisation and ids


def normalise_url(url: str) -> str:
    """Key used for dedupe, exclusions and ids.

    Lowercases scheme and host, treats http and https as the same feed, drops
    a leading "www.", default ports, fragments and a trailing slash on the
    path. The query string is kept as-is (it often identifies the feed).
    NEVER change this function: feed ids are derived from it.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunsplit(("https", netloc, path, parts.query, ""))


def feed_id(url: str) -> str:
    return hashlib.sha1(normalise_url(url).encode("utf-8")).hexdigest()[:12]


def is_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


# --------------------------------------------------------------------------
# Text cleaning

_WS = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = _TAGS.sub(" ", value)
    return _WS.sub(" ", value).strip()


def clean_title(value: str | None) -> str:
    title = clean_text(value)
    if len(title) > 60 and " | " in title:
        head = title.split(" | ", 1)[0].strip()
        if head:
            title = head
    return title


def clean_summary(value: str | None, title: str = "") -> str:
    summary = clean_text(value)
    if not summary or summary.casefold() == title.casefold():
        return ""
    if len(summary) > 200:
        cut = summary[:199]
        if " " in cut[120:]:
            cut = cut[: cut.rfind(" ")]
        summary = cut.rstrip(" ,;:.-–—") + "…"
    return summary


# --------------------------------------------------------------------------
# OPML parsing (upstream files are often malformed XML)

_OUTLINE = re.compile(r"<outline\b([^>]*?)/?>", re.IGNORECASE | re.DOTALL)
_ATTR = re.compile(r"""([A-Za-z_:][\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.DOTALL)


def parse_opml(path: str) -> list[dict]:
    """Return [{title, feedURL, summary}] for every outline with an xmlUrl."""
    with open(path, "rb") as fh:
        raw = fh.read()
    items: list[dict] = []
    try:
        tree = ET.fromstring(raw)
        for node in tree.iter("outline"):
            url = node.get("xmlUrl")
            if url:
                items.append({
                    "title": node.get("title") or node.get("text") or "",
                    "feedURL": url.strip(),
                    "summary": node.get("description") or "",
                })
        return items
    except ET.ParseError:
        pass
    # Regex fallback for malformed files (unescaped "&" is the usual culprit).
    text = raw.decode("utf-8", errors="replace")
    for match in _OUTLINE.finditer(text):
        attrs = {}
        for m in _ATTR.finditer(match.group(1)):
            value = m.group(2) if m.group(2) is not None else m.group(3)
            attrs[m.group(1)] = html.unescape(value)
        url = attrs.get("xmlUrl")
        if url:
            items.append({
                "title": attrs.get("title") or attrs.get("text") or "",
                "feedURL": url.strip(),
                "summary": attrs.get("description") or "",
            })
    return items


# --------------------------------------------------------------------------
# Catalog sources


def load_category_map() -> dict:
    return read_json(os.path.join(SOURCE_DIR, "categories.json"))


def load_region_map() -> dict:
    return read_json(os.path.join(SOURCE_DIR, "regions.json"))


def load_exclusions() -> dict:
    return read_json(EXCLUSIONS_PATH, {"excluded": []})


def load_sources(strict: bool = True) -> list[dict]:
    """Merged, deduped, exclusion-filtered list of source feeds.

    Each entry: id, key (normalised URL), feedURL (as listed), title, summary,
    siteURL, language, categories (set), regions (set), origin.
    Order: upstream recommended, upstream countries, custom (custom wins on
    metadata). Deterministic because input files are processed sorted.
    """
    cat_map = load_category_map()
    reg_map = load_region_map()["regions"]
    known_cats = {c["id"] for c in cat_map["categories"]}
    known_regions = {r["id"] for r in reg_map.values()}
    exclusions = load_exclusions()
    excluded = {normalise_url(e["feedURL"]) for e in exclusions.get("excluded", [])}
    # Whole countries removed for good: their upstream list is skipped and
    # no feed tagged with them is published, wherever else it is listed.
    excluded_regions = {r["id"] for r in exclusions.get("excludedRegions", [])}

    merged: dict[str, dict] = {}
    problems: list[str] = []

    def add(item: dict, categories: set, regions: set, origin: str, override: bool) -> None:
        url = item["feedURL"].strip()
        if not is_http_url(url):
            problems.append(f"skipped non-http URL {url!r} ({origin})")
            return
        key = normalise_url(url)
        if key in excluded:
            return
        title = clean_title(item.get("title"))
        entry = merged.get(key)
        if entry is None:
            entry = merged[key] = {
                "id": feed_id(url), "key": key, "feedURL": url, "title": title,
                "summary": "", "siteURL": "", "language": "",
                "categories": set(), "regions": set(), "origin": origin,
            }
        elif url.startswith("https:") and entry["feedURL"].startswith("http:"):
            # Prefer the https spelling when the same feed is listed twice.
            entry["feedURL"] = url
        if override and title:
            entry["title"] = title
        summary = clean_summary(item.get("summary"), entry["title"])
        if summary and (override or not entry["summary"]):
            entry["summary"] = summary
        for field in ("siteURL", "language"):
            if item.get(field) and (override or not entry[field]):
                entry[field] = item[field]
        entry["categories"] |= categories
        entry["regions"] |= regions

    for name in sorted(os.listdir(RECOMMENDED_DIR), key=str.casefold):
        if not name.endswith(".opml"):
            continue
        upstream_name = name[:-5]
        cat = cat_map["upstream"].get(upstream_name)
        if cat is None:
            problems.append(f"unmapped upstream category {upstream_name!r} — add it to source/categories.json")
            continue
        for item in parse_opml(os.path.join(RECOMMENDED_DIR, name)):
            add(item, {cat}, set(), f"recommended/{upstream_name}", False)

    for name in sorted(os.listdir(COUNTRIES_DIR), key=str.casefold):
        if not name.endswith(".opml"):
            continue
        country = name[:-5]
        region = reg_map.get(country)
        if region is None:
            problems.append(f"unmapped upstream country {country!r} — add it to source/regions.json")
            continue
        if region["id"] in excluded_regions:
            continue
        for item in parse_opml(os.path.join(COUNTRIES_DIR, name)):
            add(item, {cat_map["countryCategory"]}, {region["id"]}, f"countries/{country}", False)

    custom = read_json(os.path.join(SOURCE_DIR, "custom-feeds.json"), {"feeds": []})
    for item in custom.get("feeds", []):
        cats = set(item.get("categories", []))
        regs = set(item.get("regions", []))
        unknown = (cats - known_cats) | (regs - known_regions)
        if unknown or not cats or not item.get("feedURL") or not item.get("title"):
            problems.append(f"invalid custom feed {item.get('feedURL')!r}: unknown ids {sorted(unknown)} or missing fields")
            continue
        if regs & excluded_regions:
            continue
        add(item, cats, regs, "custom", True)

    if problems:
        for p in problems:
            print(f"warning: {p}", file=sys.stderr)
        if strict and any(p.startswith(("unmapped", "invalid")) for p in problems):
            raise SystemExit("error: source problems above must be fixed first")

    return [entry for entry in merged.values() if not (entry["regions"] & excluded_regions)]


def load_state() -> dict:
    state = read_json(STATE_PATH, None) or {}
    state.setdefault("lastRun", None)
    state.setdefault("feeds", {})
    return state
