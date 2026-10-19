# Institutional Intelligence (MVP)

A self-hosted personal news system that asks one question of every story: **does this change my options, costs, risks or opportunities, and through what mechanism?**
It is deterministic and inspectable. There is **no AI model, no proprietary service, no API key, no scheduled fetching and no telemetry**.
Licence: **AGPL-3.0-or-later** (see `LICENSE`). Component inventory: `THIRD_PARTY.md`.

## Quick start
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp config/sources.example.toml config/sources.toml     # then edit: add feeds for YOUR country and institutions
python -m app                                          # http://127.0.0.1:8000
```
1. Open **Goals and profile**, tick your situation and activities, set your country, add goals, save.
2. Press **Refresh** on the Current Edition page. Check the **Sources** page for per-source results.
Settings: `INEWS_DB` (default `data/inews.sqlite3`), `INEWS_CONFIG_DIR` (default `config`), `INEWS_HOST`/`INEWS_PORT` (default `127.0.0.1:8000`). Do not expose it to a network without adding authentication: it is a single-user app.

## What happens on Refresh (the only time anything is fetched)
1. The backend atomically claims the refresh (`BEGIN IMMEDIATE`); blocked if cooling down, already running, or inside the politeness gap.
2. Every enabled source is fetched once (honest `User-Agent`, timeout, no retries).
3. **If any source fails** the refresh is reported as a failure, the current edition is untouched, nothing is ingested and **no cooldown starts**.
4. Otherwise items are normalized, deduplicated (same URL, same normalized title, TF-IDF near-duplicate), scored against your profile and goals, and compared to the current edition.
5. New or materially updated qualifying stories publish a **new immutable edition** (old one stays in the archive). A story is "materially updated" only if a new article adds a date or figure not already in the story; repeated commentary does not count.
6. Nothing new qualified, all sources succeeded → a **3-hour cooldown** is stored in SQLite (`state.cooldown_until`). Reloads, other tabs and restarts all read it; the backend refuses to fetch until it expires.

A separate `min_seconds_between_fetches` (default 60 s, `config/scoring.toml`) spaces out rounds after failures to respect source rate limits; it is not the 3-hour rule. A crashed refresh's lock expires after 10 minutes.

## How relevance is computed (all editable)
`relevance = 100 × gate × Σ weight × factor` for impact, exposure, goal, time, reach, action (`config/scoring.toml`, `config/rules.toml`, `app/scoring.py`).
Gate is 0 unless one of your activities, situations or goals links to the pathway (location alone never counts), which is why unrelated stories are rejected, not just ranked low.
The score is an **estimate**, shown with its breakdown. **Likelihood for you** (exposure) and **evidence confidence** (source tier, wording, corroboration) are separate from it. Each item separates *evidence* (from the source), *inference* (rule + your profile) and *unknowns*.
Rules are explicit statements ("if text mentions X, this mechanism may reach people with activities Y"). They are the project's inferences, they are phrased conditionally, and you should edit them. The wording-based "confirmed/announced/forecast/allegation" label is a **heuristic**, not verification.
The **Institutional map** ships category-level functions only. Add researched institutions to `config/institutions.toml`; **every relationship must cite a source URL** or the app refuses to start.

## Opportunity timing
Evidence-gated: only records you add under `data/timing/*.toml` (format in `example.toml.example`) are evaluated. Published calendars/deadlines are shown as such; monthly price series need ≥3 near-complete years and a consistent best month to be called a "demonstrated seasonal pattern". Otherwise: **Insufficient evidence**. No biological or personal-state inference of any kind.

## Privacy and data
Everything is stored in one SQLite file on your machine; no cookies, no third-party requests except the feeds you configure. **Goals and profile** has JSON export and scoped delete (profile / editions / everything). The app stores titles, links, dates and ≤300-character excerpts, never full articles. Retrieved content keeps its publisher's licence; the application code being open source does not make it open data.

## Backup, restore, upgrade, recovery
* Backup: stop the app and copy `data/inews.sqlite3*`, or `sqlite3 data/inews.sqlite3 ".backup backup.sqlite3"`; also keep `config/` and `data/timing/`. Export JSON from the profile page as a portable copy.
* Restore: stop the app, put the file back at `INEWS_DB`, start.
* Upgrade: `git pull && pip install -r requirements.txt`; schema migrations run automatically at start (`PRAGMA user_version`). Back up first. Roll back by restoring the backup and the previous code version.
* Corruption: `sqlite3 data/inews.sqlite3 "PRAGMA integrity_check"`; restore from backup. Profile data can be recovered from an exported JSON (re-enter via the form).

## Re-evaluating after profile changes
The 3-hour cooldown blocks fetching, not thinking. **Re-evaluate stored articles with my current profile** re-scores what is already in your database (no network, cooldown untouched) and, if something newly qualifies, publishes a new edition; if not, it tells you how many stories were rejected and why.

## Partial refreshes and the South Africa / Nigeria starter kit
`partial_publish = true` (default, `config/scoring.toml`): if some sources fail but at least one works, the refresh publishes from the working ones, labels the edition **PARTIAL**, lists what is missing, and still never starts the cooldown. Set it to `false` for the strict all-or-nothing behaviour.
`config/sources.toml` (feeds verified by fetching on 19 Oct 2026), `config/sources.candidates.toml` (unverified, switched off, not loaded), the extra institutions at the end of `config/institutions.toml`, and `python -m app.seed config/profile.seed.json` (starter profile and goals; no street address) are a starting point for a student/migrant in South Africa with Nigerian ties.

## TLS certificate errors
Verification is always on. If a source fails with `CERTIFICATE_VERIFY_FAILED`, the Sources page explains likely causes. To trust an extra CA (corporate proxy root, or a missing intermediate), set `INEWS_CA_BUNDLE=/path/to/ca.pem` before starting. Otherwise set `enabled = false` for that source.

## Tests
`pip install -r requirements-dev.txt && python -m pytest`: 39 tests cover ranking, filtering, dedup (including a test that fails if near-duplicate matching is disabled), material vs repetitive updates, edition immutability, cooldown persistence across instances/clients/time, failures and partial failures, concurrent refresh, stale locks, empty results, export/delete, cross-origin rejection, feed parsing and timing evidence gating. All feed content in tests is synthetic.

**Verifying live feeds.** The tests, and my build environment's restricted network, prove the logic under simulated feeds and against a local HTTP server; they do **not** prove that any real feed URL in `sources.example.toml` is reachable or correctly formatted. Verify on your own machine: press Refresh, then read the **Sources** page. Any URL that fails shows its error there.

## Open-source status against the brief's acceptance tests (section 14.7)
| # | Requirement | Status |
|---|---|---|
| 1 | Builds from published source with documented steps | Done; verified in a clean venv-style install here. Public repository not yet created (you must publish it). |
| 2 | Dependency licences identified and compatible | Done in `THIRD_PARTY.md`; one transitive package (sgmllib3k) unverified; certifi is dev-only MPL-2.0. |
| 3 | Core works without proprietary API keys | Done. |
| 4 | AI analysis via local model or non-AI fallback | The **non-AI mode is the whole product**. No model adapter or model documentation is included yet. |
| 5 | No secret transmission of personal data | By design only configured feeds are contacted, with no profile data sent. **An automated egress test is not written yet.** |
| 6 | Scoring/institutional logic inspectable and editable | Done (TOML + one Python module). |
| 7 | Install/config/test/update/recovery documented | Done (above). |
| 8 | No closed binary or proprietary service essential | Done. |
| 9 | Licence, inventory, model info, limitations published | Licence, inventory, limitations done; model info N/A until a model is added. |
| 10 | Works when optional integrations unavailable | There are none; feed failures are surfaced, not hidden. |

## Known limitations and incomplete features
* **Optional local AI adapter is not implemented.** Planned design: evidence-constrained summaries with citation validation, never assigning scores or relationships; evaluate OLMo-family models against the OSI Open Source AI Definition on a fixed document set first.
* **Writing style** is stored but does not yet change wording; **detail level** only toggles brief vs fuller cards.
* Matching is lexical (keywords + TF-IDF). It misses paraphrases and can false-match ambiguous words (e.g. "grid", "grant"). Edit the rules and check the rejected list.
* Date extraction understands ISO and "1 November 2026"-style dates only. Event dates are never independently verified; the feed's publication date is shown.
* Feeds often carry short summaries, so many items will have "no amount stated" and lower confidence. Official feeds help most.
* `sources.example.toml` URLs are unverified starting points; add feeds for your own country. Conditional GET/ETag and robots.txt handling are not implemented (feeds are fetched once per Refresh).
* No authentication; single user; sync server-rendered pages. No pin/annotate on editions (all editions are saved automatically). No Docker file yet. No CI for licence scanning.
