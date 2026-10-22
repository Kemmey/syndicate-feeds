# Syndicate feed catalog

The public, independently curated RSS/Atom/JSON Feed catalog behind the
**Syndicate** RSS reader's feed discovery. One JSON file, health-checked,
published straight from this repository:

**<https://raw.githubusercontent.com/Kemmey/syndicate-feeds/master/dist/feeds-v1.json>**

The URL is served with an `ETag`, so clients can poll it cheaply with
`If-None-Match` and get `304 Not Modified` when nothing changed.

## Built on Awesome RSS Feeds by Plenary

This catalog is built on **[Awesome RSS Feeds](https://github.com/plenaryapp/awesome-rss-feeds)**
by [Plenary](https://github.com/plenaryapp), a hand-picked collection of
recommended feeds by topic and news sources by country, published under CC0.
Thank you, Plenary.

This repository is a fork of it. The upstream OPML files under
`recommended/` and `countries/` are **imported data and are never edited
here** — so syncing with upstream never conflicts with our changes. Everything Syndicate
adds, removes or repairs lives in a separate overlay (`source/`, `state/`) and
is applied when the catalog is built.

## How it works

```
recommended/with_category/*.opml ─┐   (upstream, unedited)
countries/with_category/*.opml  ──┤
source/custom-feeds.json  ────────┤   our additions and overrides
source/exclusions.json  ──────────┤   removed for good
state/feed-health.json  ──────────┘   written by validate.py
                │
        scripts/update.py
                │
        dist/feeds-v1.json   (GENERATED — never edit by hand)
```

| Path | What it is |
| --- | --- |
| `source/categories.json` | Maps each upstream topic file (e.g. `UI - UX.opml`) to a stable category id, title and SF Symbol. Ids never change once published. Country lists all map to `news`. |
| `source/regions.json` | Maps each upstream country file to its ISO 3166-1 alpha-2 code. |
| `source/custom-feeds.json` | Feeds Syndicate adds on top of upstream (or metadata overrides for an upstream feed with the same URL). |
| `source/exclusions.json` | Feeds deliberately removed. Matched by normalised URL, so an upstream re-sync can never bring them back. |
| `state/feed-health.json` | Per-feed health record written by `validate.py`: status, consecutive failures, failing-run dates, canonical URL, site URL, language, last HTTP status. |
| `scripts/validate.py` | Health-checks every feed and updates the state. |
| `scripts/update.py` | Builds `dist/feeds-v1.json`. |
| `reports/latest.md` | The last maintenance report. |
| `dist/feeds-v1.json` | The published catalog. Generated; any hand edit is overwritten. |

Upstream feeds that appear in several lists are merged into one entry (their
categories and regions combined). Feeds are deduplicated by normalised URL:
scheme and a leading `www.` are ignored, as are a trailing slash and the
fragment.

## Health rules

`scripts/validate.py` fetches every feed (GET, at most 2 MB, 15 s timeout,
24 in parallel, `User-Agent: SyndicateFeedCatalog/1.0 (+https://github.com/Kemmey/syndicate-feeds)`):

| Result | What happens |
| --- | --- |
| Valid RSS, Atom, RDF or JSON Feed | Kept. Failure count reset. Site link, language and title are recorded from the feed. |
| `301` / `308` to a valid feed | Kept; the target is recorded as the canonical URL and published instead. The feed id does not change. |
| `200` with an HTML page | The page is searched for `<link rel="alternate">` feed links. A discovered URL that validates becomes the canonical URL. Otherwise: *html-no-feed*, treated like `404`. |
| `404` / `410` | Quarantined (left out of the catalog) and the failure is counted. |
| `401` / `403` / `429` / `451`, other non-`200` success | Kept. Access restrictions, throttling and bot challenges are not the feed's fault. No penalty. |
| Timeout, connection error, `5xx`, other `4xx`, empty body | Kept, retried next run. No penalty. |
| Valid but inactive | Kept. Inactivity is not failure. |

**Pruning.** A quarantined feed is removed for good (moved to
`source/exclusions.json` with the reason *pruned after 3 consecutive
failures*) only after **three consecutive failing runs at least 25 days
apart** — on the intended monthly cadence that is about three months of
failure. A rerun within 25 days never counts twice. One successful check
resets the count and lifts the quarantine.

**Safety stop.** If more than 20 % of the feeds fail with a quarantine-class
result in one run (`--max-failure-rate`), or the rate is more than twice the
previous run's (`--max-rate-multiplier`, applied once the rate is at least
5 %), the run is treated as a network problem rather than dead feeds:
`validate.py` exits non-zero, writes only the report, and nothing is published.

Every run writes `reports/latest.md` (also shown as the GitHub Actions job
summary): feeds checked, new feeds, URLs repaired, quarantined, removed, and
the feeds kept despite `403`/`429` or retried.

## Running it

Python 3.9+ standard library only — nothing to install.

```sh
python3 scripts/validate.py          # check feeds, update state/ and reports/
python3 scripts/update.py            # rebuild dist/feeds-v1.json
python3 scripts/validate.py --limit 40   # quick test on 40 feeds; writes nothing
python3 scripts/validate.py --help       # thresholds and other options
```

On GitHub, the **Update catalog** workflow (Actions tab → *Update catalog* →
*Run workflow*) runs both scripts and commits `dist/`, `state/`, `reports/` and
`source/exclusions.json` when they changed. It only runs when started by hand.

## Syncing with upstream

Use GitHub's **Sync fork** button (or `git pull upstream master`), then run the
workflow. New upstream feeds are picked up automatically and listed under
*New feeds* in the report. A new upstream topic or country file makes the
scripts stop with a message until it is mapped in `source/categories.json` or
`source/regions.json`. The only upstream file this fork changes is this
README, so a sync can at most conflict here.

## Proposing a feed

Open a pull request that adds an entry to `source/custom-feeds.json`:

```json
{
  "title": "Swift.org",
  "feedURL": "https://www.swift.org/atom.xml",
  "siteURL": "https://www.swift.org",
  "summary": "News and announcements from the Swift project.",
  "categories": ["ios-development", "programming"],
  "regions": [],
  "language": "en"
}
```

`title`, `feedURL` and at least one category id from `source/categories.json`
are required. Use `regions` (ISO 3166-1 alpha-2) only for country-specific
news. To remove a feed, add it to `source/exclusions.json` with a reason.

## Catalog format (`dist/feeds-v1.json`)

```json
{
  "schemaVersion": 1,
  "generated": "Generated by scripts/update.py — do not edit by hand.",
  "publishedAt": "2026-10-22T19:00:00Z",
  "attribution": {"title": "Awesome RSS Feeds by Plenary", "url": "https://github.com/plenaryapp/awesome-rss-feeds", "license": "CC0-1.0"},
  "categories": [{"id": "apple", "title": "Apple", "symbol": "apple.logo"}],
  "regions": [{"id": "DE", "title": "Germany"}],
  "feeds": [
    {"id": "1a2b3c4d5e6f", "title": "ZEIT ONLINE", "feedURL": "https://...", "siteURL": "https://...",
     "summary": "…", "categories": ["news"], "regions": ["DE"], "language": "de"}
  ]
}
```

- `id` — first 12 hex digits of SHA-1 over the feed's original normalised URL.
  Stable: canonical-URL repairs change `feedURL`, never `id`.
- `categories` — ids from the top-level `categories` list (always at least one).
- `regions` — ISO 3166-1 alpha-2 codes from the top-level `regions` list; an
  empty array means not country-specific. Region titles are English; clients
  localise from the code.
- `siteURL`, `summary` (≤ 200 characters) and `language` (ISO 639 code) are
  omitted when unknown — never `null`.
- Feeds are sorted by title, categories and regions by title.
- `publishedAt` only changes when the content does.
- Quarantined and excluded feeds are absent.

Breaking changes would ship as `feeds-v2.json` alongside v1.

## License

[CC0 1.0 Universal](LICENSE), like the upstream collection. Syndicate's own
additions — the overlay, scripts and generated catalog — are dedicated to the
public domain under CC0 as well.
