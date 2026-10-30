#!/usr/bin/env python3
"""Grade Syndicate's article reader against the catalog's live sites (#417).

Runs after a catalog rebuild (``update.py`` calls it). It builds and runs
``syndicate-reader-probe`` — a CLI in the RumskrotSyndicate repository that
links the same reader module the app ships — over ``dist/feeds-v1.json``
plus Kim's own library (``Fixtures/KimLibrary.opml`` in that repository),
three newest articles per feed, and writes:

* ``reports/reader-quality-<date>.json`` — every feed and article, graded;
* ``reports/reader-quality-<date>.md`` and ``reports/reader-quality-latest.md``
  — the summary and the worst feeds first.

It also publishes the reader's site rules. ``source/reader-rules.json`` is
the one hand-edited copy; when its rules differ from the published file the
revision is bumped and the file is written to the website repository
(``static/apps/rumskrot-syndicate/reader-rules.v1.json``, served at
https://danskrumskrot.com/apps/rumskrot-syndicate/reader-rules.v1.json) and
to the app's bundled fallback (``SiteRules.json``). Committing and pushing
those repositories stays a deliberate step.

The probe needs macOS and a Swift toolchain; elsewhere (the GitHub
workflow) this step is skipped with a message. Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import os
import platform
import shutil
import subprocess
import sys

import catalog

RULES_SOURCE = os.path.join(catalog.SOURCE_DIR, "reader-rules.json")
WEBSITE_RULES = os.path.join("static", "apps", "rumskrot-syndicate", "reader-rules.v1.json")
APP_RULES = os.path.join("Packages", "SyndicateKit", "Sources", "SyndicateReader", "Resources", "SiteRules.json")
KIM_OPML = os.path.join("Fixtures", "KimLibrary.opml")
PACKAGE = os.path.join("Packages", "SyndicateKit")
SCHEMA_VERSION = 1


def sibling(name: str, env: str) -> str:
    return os.environ.get(env) or os.path.join(os.path.dirname(catalog.ROOT), name)


def validate_rules(document: dict) -> None:
    """The same checks the app makes, minus selector parsing (the probe's
    --rules run does that with the app's own parser)."""
    assert document.get("schemaVersion") == SCHEMA_VERSION, "schemaVersion must be 1"
    assert isinstance(document.get("revision"), int) and document["revision"] >= 0, "revision"
    rules = document.get("rules")
    assert isinstance(rules, list) and len(rules) <= 1000, "rules"
    for rule in rules:
        domain = rule.get("domain", "")
        assert domain == domain.lower() and "." in domain and " " not in domain, f"domain {domain!r}"
        assert rule.get("content"), f"{domain}: content selectors required"
        for key in ("content", "title", "clean"):
            for selector in rule.get(key) or []:
                assert isinstance(selector, str) and 0 < len(selector) <= 200, f"{domain}: {selector!r}"


def render_rules(document: dict) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def publish_rules(syndicate: str, website: str) -> str:
    """Write the rules where the app and the website read them, bumping the
    revision when the rules changed. Returns a one-line summary."""
    source = catalog.read_json(RULES_SOURCE)
    if source is None:
        return "no source/reader-rules.json; rules not published"
    validate_rules(source)

    published_path = os.path.join(website, WEBSITE_RULES)
    published = catalog.read_json(published_path)
    if published and published.get("rules") == source["rules"] and published.get("revision") == source["revision"]:
        return f"rules unchanged (revision {source['revision']})"
    if published and published.get("rules") != source["rules"]:
        source["revision"] = max(source["revision"], published.get("revision", 0) + 1)

    text = render_rules(source)
    for path in (RULES_SOURCE, published_path, os.path.join(syndicate, APP_RULES)):
        if os.path.isdir(os.path.dirname(path)):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
    return f"rules published: revision {source['revision']}, {len(source['rules'])} sites"


def run_probe(syndicate: str, out_json: str, rules: str | None) -> None:
    env = dict(os.environ)
    package = os.path.join(syndicate, PACKAGE)
    subprocess.run(["swift", "build", "--package-path", package, "-c", "release",
                    "--product", "syndicate-reader-probe"], check=True, env=env,
                   stdout=subprocess.DEVNULL)
    bin_path = subprocess.run(["swift", "build", "--package-path", package, "-c", "release", "--show-bin-path"],
                              check=True, capture_output=True, text=True, env=env).stdout.strip()
    command = [os.path.join(bin_path, "syndicate-reader-probe"), "--catalog", catalog.DIST_PATH,
               "--items", "3", "--concurrency", "8", "--out", out_json]
    opml = os.path.join(syndicate, KIM_OPML)
    if os.path.exists(opml):
        command += ["--opml", opml]  # MANDATORY when present: Kim's own feeds
    if rules:
        command += ["--rules", rules]
    subprocess.run(command, check=True, env=env, stdout=subprocess.DEVNULL)


def _short_note(note: str) -> str:
    """Error dumps carry session-task ids and nested user info; keep only the readable reason."""
    match = re.search(r'NSLocalizedDescription=([^,]+)', note)
    if note.startswith("Error Domain=") and match:
        return match.group(1).strip()
    return note[:160]


def compact(report: dict) -> dict:
    """The committed report stays small and public-safe: grades, counts and short notes.

    This repository is public. Feeds from a personal OPML (``source`` ``opml:…``)
    count in the totals only; their rows are left out. Article titles are
    dropped (the URL identifies the article)."""
    report["results"] = [feed for feed in report["results"] if not _is_private(feed)]
    for feed in report["results"]:
        for article in feed["articles"]:
            article.pop("title", None)
            article["notes"] = sorted({_short_note(n) for n in article.get("notes", [])})
    return report


def _is_private(feed: dict) -> bool:
    return str(feed.get("source", "")).startswith("opml:")


def write_compact(report: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(compact(report), fh, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        fh.write("\n")


def summarise(report: dict, previous: dict | None) -> str:
    lines = [f"# Reader quality — {report['generatedAt'][:10]}", "",
             f"Engine `{report['engine']}`, {report['itemsPerFeed']} newest articles per feed, "
             f"rules: {report['rules']}.", "",
             "| Source | Feeds | Articles | good | weak | poor | failed | unreachable | mean score |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for key in sorted(report["articles"]):
        a, f = report["articles"][key], report["feeds"].get(key, {})
        v = a["verdicts"]
        lines.append(f"| {key} | {f.get('total', 0)} | {a['total']} | {v.get('good', 0)} | {v.get('weak', 0)} | "
                     f"{v.get('poor', 0)} | {v.get('failed', 0)} | {v.get('unreachable', 0)} | {a['meanScore']} |")
    if previous:
        before, after = previous["articles"]["all"], report["articles"]["all"]
        lines += ["", f"Previous run {previous['generatedAt'][:10]}: mean score {before['meanScore']} → "
                      f"{after['meanScore']}, good {before['verdicts'].get('good', 0)} → {after['verdicts'].get('good', 0)}."]
    strategies = report["articles"]["all"]["strategies"]
    lines += ["", "Who found the body: " + ", ".join(f"{k} {v}" for k, v in sorted(strategies.items())) + ".",
              "", "## Worst feeds (unreachable left out)", "",
              "| Verdict | Score | Host | Feed | Notes |", "| --- | ---: | --- | --- | --- |"]
    worst = [f for f in report["results"]
             if f["verdict"] in ("failed", "poor", "weak") and not _is_private(f)]
    for feed in worst[:60]:
        notes = sorted({n for a in feed["articles"] for n in a["notes"]})
        lines.append(f"| {feed['verdict']} | {feed['score']} | {feed['host']} | {feed['title'][:50]} | "
                     f"{'; '.join(notes)[:160]} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--syndicate", default=sibling("RumskrotSyndicate", "SYNDICATE_REPO"),
                        help="RumskrotSyndicate checkout (default: sibling directory or $SYNDICATE_REPO)")
    parser.add_argument("--website", default=sibling("danskrumskrot", "WEBSITE_REPO"),
                        help="danskrumskrot website checkout (default: sibling directory or $WEBSITE_REPO)")
    parser.add_argument("--rules-only", action="store_true", help="publish the rules, skip the probe")
    args = parser.parse_args(argv)

    print(publish_rules(args.syndicate, args.website))
    if args.rules_only:
        return 0
    if platform.system() != "Darwin" or shutil.which("swift") is None \
            or not os.path.isdir(os.path.join(args.syndicate, PACKAGE)):
        print("reader probe skipped: needs macOS, Swift and a RumskrotSyndicate checkout")
        return 0

    reports = os.path.join(catalog.ROOT, "reports")
    previous_path = max((os.path.join(reports, n) for n in os.listdir(reports)
                         if n.startswith("reader-quality-") and n.endswith(".json")), default=None)
    previous = catalog.read_json(previous_path) if previous_path else None

    date = dt.date.today().isoformat()
    out_json = os.path.join(reports, f"reader-quality-{date}.json")
    run_probe(args.syndicate, out_json, RULES_SOURCE if os.path.exists(RULES_SOURCE) else None)
    report = catalog.read_json(out_json)
    write_compact(report, out_json)
    summary = summarise(report, previous if previous_path != out_json else None)
    for name in (f"reader-quality-{date}.md", "reader-quality-latest.md"):
        with open(os.path.join(reports, name), "w", encoding="utf-8") as fh:
            fh.write(summary)
    print(f"wrote reports/reader-quality-{date}.json and .md: mean score {report['articles']['all']['meanScore']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
