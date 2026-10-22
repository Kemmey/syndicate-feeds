#!/usr/bin/env python3
"""Health-check every catalog feed and update state/feed-health.json.

Rules (see README "Health rules"):
  valid RSS/Atom/RDF/JSON Feed  -> ok, failure count reset
  301/308 to a valid feed       -> ok, canonical URL recorded (repaired)
  404/410                       -> quarantined, counts towards pruning
  200 HTML                      -> autodiscover <link rel=alternate>; a valid
                                   discovered feed is recorded as canonical,
                                   otherwise "html-no-feed" (counts like 404)
  200 but not a feed            -> "not-a-feed" (counts like 404)
  401/403/429/451, 2xx not 200  -> kept, no penalty (access restricted/throttled/challenged)
  timeout, connection error,
  5xx, other 4xx, empty body   -> kept, retried next run, no penalty
  inactive (no recent items)    -> kept; inactivity is not failure

A failing run only counts towards pruning if it is at least --min-days-apart
days after the previous counted failing run, so reruns never double count.
After --prune-after counted failures the feed moves to source/exclusions.json.

If this run's failure rate is above --max-failure-rate, or more than
--max-rate-multiplier times the previous run's (non-zero) rate while also
above --relative-floor, nothing is written except the report, and the script
exits with status 2 so the workflow publishes nothing.

Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402

USER_AGENT = "SyndicateFeedCatalog/1.0 (+https://github.com/Kemmey/syndicate-feeds)"
MAX_BYTES = 2 * 1024 * 1024
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json",
              "application/rdf+xml", "application/json")

# Result classes
OK = "ok"                    # valid feed
RESTRICTED = "restricted"    # 401/403/429/451: keep, no penalty
RETRY = "retry"              # timeout/connection/5xx/other: keep, no penalty
FAIL = "fail"                # 404/410/html-no-feed/not-a-feed: quarantine


# --------------------------------------------------------------------------
# HTTP


class _RedirectRecorder(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.hops: list[tuple[int, str]] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops.append((code, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SSL = ssl.create_default_context()


def fetch(url: str, timeout: float) -> dict:
    """GET url. Returns {status, finalURL, permanentURL, body, contentType, error}."""
    recorder = _RedirectRecorder()
    opener = urllib.request.build_opener(recorder, urllib.request.HTTPSHandler(context=_SSL))
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/feed+json, "
                  "application/xml;q=0.9, text/xml;q=0.9, application/json;q=0.8, */*;q=0.5",
        "Accept-Encoding": "gzip, deflate",
    })
    result = {"status": None, "finalURL": url, "permanentURL": None, "body": b"",
              "contentType": "", "error": None}
    try:
        with opener.open(req, timeout=timeout) as resp:
            result["status"] = resp.status
            result["finalURL"] = resp.geturl()
            result["contentType"] = resp.headers.get("Content-Type", "")
            raw = resp.read(MAX_BYTES)
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            result["body"] = _decode(raw, encoding)
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["finalURL"] = exc.geturl() or url
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
            ssl.SSLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        result["error"] = f"{type(reason).__name__}: {reason}"[:200]
    except Exception as exc:  # http.client oddities (IncompleteRead, BadStatusLine…)
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
    # Canonical = URL reached through the leading run of permanent redirects.
    permanent = None
    for code, newurl in recorder.hops:
        if code in (301, 308):
            permanent = newurl
        else:
            break
    result["permanentURL"] = permanent
    return result


def _decode(raw: bytes, encoding: str) -> bytes:
    try:
        if "gzip" in encoding or raw[:2] == b"\x1f\x8b":
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            return d.decompress(raw, MAX_BYTES)
        if "deflate" in encoding:
            try:
                return zlib.decompressobj().decompress(raw, MAX_BYTES)
            except zlib.error:
                return zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw, MAX_BYTES)
    except (zlib.error, OSError, EOFError):
        return raw
    return raw


# --------------------------------------------------------------------------
# Feed parsing


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


_LANG = re.compile(r"^([a-zA-Z]{2,3})(?:[-_].*)?$")


def _lang(value: str | None) -> str:
    m = _LANG.match((value or "").strip())
    return m.group(1).lower() if m else ""


def parse_feed(body: bytes) -> dict | None:
    """Return {kind, siteURL, language, lenient} if body is a feed, else None."""
    stripped = body.lstrip()
    if stripped[:1] in (b"{", b"["):
        try:
            doc = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            return None
        if isinstance(doc, dict) and str(doc.get("version", "")).startswith("https://jsonfeed.org/version/"):
            return {"kind": "json", "title": str(doc.get("title") or ""), "siteURL": doc.get("home_page_url") or "",
                    "language": _lang(doc.get("language")), "lenient": False}
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return _sniff(body)
    name = _local(root.tag)
    xml_lang = root.get("{http://www.w3.org/XML/1998/namespace}lang")
    if name == "rss":
        channel = next((c for c in root if _local(c.tag) == "channel"), None)
        if channel is None:
            return None
        site = lang = title = ""
        for child in channel:
            n = _local(child.tag)
            if n == "title" and not title:
                title = child.text or ""
            elif n == "link" and not site and (child.text or "").strip():
                site = child.text.strip()
            elif n == "language" and not lang:
                lang = _lang(child.text)
        return {"kind": "rss", "title": title, "siteURL": site, "language": lang or _lang(xml_lang), "lenient": False}
    if name == "feed":
        site = title = ""
        for child in root:
            if _local(child.tag) == "title" and not title:
                title = "".join(child.itertext())
            if _local(child.tag) == "link" and child.get("rel", "alternate") == "alternate" and child.get("href"):
                site = child.get("href")
                break
        return {"kind": "atom", "title": title, "siteURL": site, "language": _lang(xml_lang), "lenient": False}
    if name == "rdf":
        site = lang = title = ""
        for child in root:
            if _local(child.tag) == "channel":
                for c in child:
                    if _local(c.tag) == "title" and not title:
                        title = c.text or ""
                    elif _local(c.tag) == "link" and (c.text or "").strip():
                        site = c.text.strip()
                    elif _local(c.tag) == "language":
                        lang = _lang(c.text)
        return {"kind": "rdf", "title": title, "siteURL": site, "language": lang, "lenient": False}
    return None


_SNIFF = re.compile(rb"<(rss|feed|rdf:RDF)\b", re.IGNORECASE)


def _sniff(body: bytes) -> dict | None:
    """Malformed or truncated XML that is clearly a feed (unescaped "&" etc.)."""
    head = body[:4096]
    if not _SNIFF.search(head) or re.search(rb"<html\b", head, re.IGNORECASE):
        return None
    text = body[:200000].decode("utf-8", errors="replace")
    site = ""
    m = re.search(r"<channel\b.*?<link>\s*([^<\s]+)\s*</link>", text, re.DOTALL | re.IGNORECASE)
    if m:
        site = m.group(1)
    else:
        m = re.search(r"<link\b[^>]*rel=[\"']alternate[\"'][^>]*href=[\"']([^\"']+)", text, re.IGNORECASE)
        if m:
            site = m.group(1)
    lang = ""
    m = re.search(r"<language>\s*([^<]+?)\s*</language>", text, re.IGNORECASE) or \
        re.search(r"xml:lang=[\"']([^\"']+)", text)
    if m:
        lang = _lang(m.group(1))
    m = re.search(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", text, re.DOTALL | re.IGNORECASE)
    title = m.group(1) if m else ""
    return {"kind": "lenient", "title": title, "siteURL": site, "language": lang, "lenient": True}


class _LinkFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rels = a.get("rel", "").lower().split()
        if "alternate" in rels and a.get("type", "").lower().split(";")[0].strip() in FEED_TYPES and a.get("href"):
            self.links.append(a["href"].strip())


def discover(body: bytes, base: str) -> list[str]:
    finder = _LinkFinder()
    try:
        finder.feed(body[:500000].decode("utf-8", errors="replace"))
    except Exception:
        pass
    out = []
    for href in finder.links:
        url = urljoin(base, href)
        if catalog.is_http_url(url) and url not in out:
            out.append(url)
    return out[:3]


def looks_html(res: dict) -> bool:
    return "html" in res["contentType"].lower() or bool(re.search(rb"<(!doctype html|html)\b", res["body"][:2048], re.IGNORECASE))


# --------------------------------------------------------------------------
# Check one feed


def check(url: str, timeout: float) -> dict:
    res = fetch(url, timeout)
    out = {"httpStatus": res["status"], "canonicalURL": None, "siteURL": "", "language": "", "title": "", "note": ""}
    if res["error"]:
        kind = "timeout" if "timed out" in res["error"].lower() or "Timeout" in res["error"] else "connection error"
        return {**out, "result": RETRY, "note": f"{kind}: {res['error']}"}
    status = res["status"]
    if status in (404, 410):
        return {**out, "result": FAIL, "note": f"HTTP {status}"}
    if status in (401, 403, 429, 451):
        return {**out, "result": RESTRICTED, "note": f"HTTP {status}"}
    if status is None or status >= 500 or status >= 400:
        return {**out, "result": RETRY, "note": f"HTTP {status}"}
    if status != 200:
        # 202/203/204 and friends: typically a bot challenge or deferred response.
        return {**out, "result": RESTRICTED, "note": f"HTTP {status} (not a direct response)"}
    if not res["body"].strip():
        return {**out, "result": RETRY, "note": "empty response"}
    feed = parse_feed(res["body"])
    if feed:
        out.update(siteURL=feed["siteURL"], language=feed["language"], title=feed["title"])
        if feed["lenient"]:
            out["note"] = "malformed XML (accepted leniently)"
        if res["permanentURL"]:
            out["canonicalURL"] = res["permanentURL"]
            out["note"] = (out["note"] + "; " if out["note"] else "") + "permanent redirect"
        return {**out, "result": OK}
    if looks_html(res):
        for candidate in discover(res["body"], res["finalURL"]):
            sub = fetch(candidate, timeout)
            if sub["status"] == 200 and not sub["error"]:
                f = parse_feed(sub["body"])
                if f:
                    out.update(siteURL=f["siteURL"] or res["finalURL"], language=f["language"], title=f["title"],
                               canonicalURL=sub["permanentURL"] or candidate,
                               note="HTML page; feed autodiscovered")
                    return {**out, "result": OK}
        return {**out, "result": FAIL, "note": "html-no-feed"}
    return {**out, "result": FAIL, "note": "not-a-feed"}


# --------------------------------------------------------------------------
# Main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-failure-rate", type=float, default=0.20,
                    help="abort if quarantine-class failures / checked exceeds this (default 0.20)")
    ap.add_argument("--max-rate-multiplier", type=float, default=2.0,
                    help="abort if the rate exceeds this multiple of the previous run's non-zero rate (default 2.0)")
    ap.add_argument("--relative-floor", type=float, default=0.05,
                    help="the relative rule only applies once the rate is at least this (default 0.05)")
    ap.add_argument("--prune-after", type=int, default=3, help="counted failing runs before pruning (default 3)")
    ap.add_argument("--min-days-apart", type=int, default=25,
                    help="days between failing runs for both to count (default 25)")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--limit", type=int, default=0, help="check only the first N feeds (testing; writes nothing)")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    today = now.date()
    stamp = now.isoformat().replace("+00:00", "Z")

    sources = catalog.load_sources()
    state = catalog.load_state()
    feeds_state: dict = state["feeds"]
    dry = args.limit > 0
    if dry:
        sources = sources[: args.limit]

    def target(src):
        prev = feeds_state.get(src["key"], {})
        return prev.get("canonicalURL") or src["feedURL"]

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda s: (s, check(target(s), args.timeout)), sources))

    new_feeds, repaired, quarantined, pruned, restricted, retry, recovered = [], [], [], [], [], [], []
    failures = 0
    next_state: dict = {}
    for src, r in results:
        key = src["key"]
        prev = feeds_state.get(key)
        if prev is None:
            new_feeds.append(src)
        e = dict(prev or {})
        e.update(id=src["id"], feedURL=src["feedURL"], lastChecked=stamp, httpStatus=r["httpStatus"],
                 lastResult=r["result"], note=r["note"])
        e.setdefault("status", "new")
        e.setdefault("consecutiveFailures", 0)
        e.setdefault("failureRuns", [])
        if r["result"] == OK:
            if e.get("status") == "quarantined":
                recovered.append(src)
            old_canonical = e.get("canonicalURL")
            if r["canonicalURL"]:
                canon = r["canonicalURL"]
                if catalog.normalise_url(canon) == key and canon == src["feedURL"]:
                    canon = None
                if canon and canon != old_canonical:
                    e["canonicalURL"] = canon
                    repaired.append((src, canon))
            e["status"] = "repaired" if e.get("canonicalURL") else "ok"
            e["consecutiveFailures"] = 0
            e["failureRuns"] = []
            e["lastOK"] = stamp
            if r["siteURL"]:
                e["siteURL"] = r["siteURL"].strip()
            if r["language"]:
                e["language"] = r["language"]
            feed_title = catalog.clean_title(r["title"])
            if feed_title:
                e["feedTitle"] = feed_title[:120]
        elif r["result"] == FAIL:
            failures += 1
            runs = e["failureRuns"]
            counts = not runs or (today - dt.date.fromisoformat(runs[-1])).days >= args.min_days_apart
            if counts:
                runs.append(today.isoformat())
                e["consecutiveFailures"] = len(runs)
            e["status"] = "quarantined"
            if e["consecutiveFailures"] >= args.prune_after:
                pruned.append((src, r["note"]))
                continue
            quarantined.append((src, r["note"], counts))
        elif r["result"] == RESTRICTED:
            restricted.append((src, r["note"]))
            if e["status"] == "new":
                e["status"] = "unverified"
        else:
            retry.append((src, r["note"]))
            if e["status"] == "new":
                e["status"] = "unverified"
        order = ("id", "feedURL", "status", "consecutiveFailures", "lastChecked", "lastOK", "failureRuns",
                 "canonicalURL", "siteURL", "language", "feedTitle", "httpStatus", "lastResult", "note")
        e = {k: e[k] for k in order if k in e and (e[k] not in (None, "") or k == "httpStatus")}
        next_state[key] = e

    checked = len(results)
    rate = failures / checked if checked else 0.0
    prev_rate = (state.get("lastRun") or {}).get("failureRate")
    abort_reason = None
    if rate > args.max_failure_rate:
        abort_reason = f"failure rate {rate:.1%} exceeds --max-failure-rate {args.max_failure_rate:.0%}"
    elif prev_rate and rate >= args.relative_floor and rate > args.max_rate_multiplier * prev_rate:
        abort_reason = (f"failure rate {rate:.1%} is more than {args.max_rate_multiplier:g}x the previous "
                        f"run's {prev_rate:.1%}")

    report = render_report(stamp, checked, rate, prev_rate, new_feeds if state["feeds"] else [],
                           len(new_feeds) if not state["feeds"] else None,
                           repaired, quarantined, pruned, restricted, retry, recovered, abort_reason, dry)
    print(report)
    if not dry:
        os.makedirs(os.path.dirname(catalog.REPORT_PATH), exist_ok=True)
        with open(catalog.REPORT_PATH, "w", encoding="utf-8") as fh:
            fh.write(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    if abort_reason:
        print(f"ABORTED: {abort_reason}. State, exclusions and dist left untouched.", file=sys.stderr)
        return 2
    if dry:
        return 0

    if pruned:
        excl = catalog.load_exclusions()
        for src, note in pruned:
            excl["excluded"].append({"feedURL": src["feedURL"],
                                     "reason": f"pruned after {args.prune_after} consecutive failures ({note})",
                                     "date": today.isoformat()})
        catalog.write_json(catalog.EXCLUSIONS_PATH, excl)

    state = {"lastRun": {"at": stamp, "checked": checked, "failures": failures, "failureRate": round(rate, 4)},
             "feeds": dict(sorted(next_state.items()))}
    catalog.write_json(catalog.STATE_PATH, state)
    return 0


def _line(src) -> str:
    return f"{src['title'] or '(untitled)'} — <{src['feedURL']}>"


def render_report(stamp, checked, rate, prev_rate, new_feeds, initial_count, repaired, quarantined, pruned,
                  restricted, retry, recovered, abort_reason, dry) -> str:
    L = [f"# Feed catalog maintenance — {stamp}", ""]
    if dry:
        L += ["_Test run (--limit): nothing was written._", ""]
    if abort_reason:
        L += [f"**ABORTED — nothing published.** {abort_reason}.", ""]
    L += ["| | Count |", "|---|---:|",
          f"| Feeds checked | {checked} |",
          f"| New upstream/custom feeds | {initial_count if initial_count is not None else len(new_feeds)}"
          f"{' (first run)' if initial_count is not None else ''} |",
          f"| URLs repaired (canonical recorded) | {len(repaired)} |",
          f"| Quarantined (404/410/no feed) | {len(quarantined)} |",
          f"| Removed (pruned) | {len(pruned)} |",
          f"| Recovered from quarantine | {len(recovered)} |",
          f"| Kept with 401/403/429/451 | {len(restricted)} |",
          f"| Kept, retry next run (timeout/connection/5xx) | {len(retry)} |",
          f"| Failure rate | {rate:.1%}{f' (previous {prev_rate:.1%})' if prev_rate is not None else ''} |",
          ""]

    def section(title, rows, limit=60):
        if not rows:
            return
        L.append(f"## {title}")
        L.append("")
        for row in rows[:limit]:
            L.append(f"- {row}")
        if len(rows) > limit:
            L.append(f"- … and {len(rows) - limit} more")
        L.append("")

    section("New feeds", [_line(s) for s in new_feeds])
    section("Repaired", [f"{_line(s)} → <{c}>" for s, c in repaired])
    section("Quarantined", [f"{_line(s)} — {n}{'' if counted else ' (rerun, not counted again)'}"
                            for s, n, counted in quarantined])
    section("Removed (pruned)", [f"{_line(s)} — {n}" for s, n in pruned])
    section("Recovered", [_line(s) for s in recovered])
    return "\n".join(L).rstrip() + "\n"


if __name__ == "__main__":
    sys.exit(main())
