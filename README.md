# Football Data Platform — Django REST API + FotMob ingestion

Implements the architecture in
`FotMob_Django_REST_API_Architecture_Flat_API_App_Updated.docx` (v1.2): a token
authenticated DRF API over locally stored FotMob data, plus idempotent
ingestion commands driven by cron.

**Core principle:** clients read the database. No API request ever fetches from
FotMob on the request path.

## Layout

```
manage.py
football/                     project config (settings, urls -> api.urls at /api/)
api/                          flat API app
    urls.py                   every route
    views.py                  every APIView
    serializers.py            input / query / output serializers
    pagination.py             explicit pagination classes
    permissions.py            reader / staff editor / administrator
    exceptions.py             {code,message,details,request_id} error envelope
    service.py                FotMob client + normalizers + upserts + queries
    tests.py                  API + ingestion suite (116 tests, no network)
    management/commands/      sync_fotmob_fixtures, sync_fotmob_live,
                              sync_fotmob_coverage, daily_fixture_reconcile,
                              weekly_backfill, create_api_token
core/                         persisted football models + admin
fotmob_fixtures_scraper.py        standalone JSON/CSV QA tool (unchanged)
fotmob_live_coverage_scraper.py   standalone JSON/CSV QA tool (unchanged)
postman_collection.json       Postman v2.1.0 collection, every route
postman_environment.json      Postman environment (local defaults)
```

`api/models.py` defines no models on purpose: all entities live in `core.models`
so there is exactly one source of truth for FotMob match identity.

## Setup

```bash
.venv/bin/python manage.py migrate
```

```bash
.venv/bin/python manage.py createsuperuser
```

```bash
.venv/bin/python manage.py test api
```

## Authentication

The API requires a token on every route. There is **no login or registration
flow in use right now**: the project runs with one fixed user and one
long-lived token, minted on the server.

```bash
.venv/bin/python manage.py create_api_token
```

That creates (or re-reads) the user `api` with the `admin` role and prints its
key. Send it on every request:

```bash
curl -s http://localhost:8000/api/matches/ -H 'Authorization: Token <key>'
```

Useful flags: `--rotate` to replace a leaked key, `--username` / `--role`
(`reader`, `staff`, `admin`) to mint a narrower client, `--quiet` to print only
the key, `--set-password` if the account also needs Django admin access. The
user is created with an unusable password, since no login protects it.

`/api/auth/token/`, `/api/auth/logout/`, `/api/auth/token/rotate/` and
`/api/auth/me/` remain implemented and tested for when per-user login is
switched back on; only `/api/auth/me/` is exercised today, as a token check.

Roles:

| Role (`--role`) | Read | Create / edit | Delete | Ingestion |
| --- | --- | --- | --- | --- |
| authenticated reader | yes | no | no | no |
| staff editor (`is_staff`) | yes | manual matches + allowed fields | soft delete, audited | no |
| administrator (`is_superuser`) | yes | plus overrides | yes, audited | staff-only trigger |
| cron service account | direct ORM via management commands (no token) | provider upserts | — | yes |

## Endpoints

All under `/api/`. Lists return `{count, next, previous, results}`; default page
size 50, maximum 200.

| Method | Path |
| --- | --- |
| POST | `auth/token/`, `auth/token/rotate/`, `auth/logout/` |
| GET | `auth/me/` |
| GET / POST | `matches/` |
| GET | `matches/live/`, `matches/completed/`, `matches/upcoming/`, `matches/cancelled/` |
| GET | `matches/by-date/<YYYY-MM-DD>/?timezone=Asia/Karachi` |
| GET / PUT / PATCH / DELETE | `matches/<fotmob_id>/` |
| GET | `matches/<id>/events|goals|lineups|stats|player-stats|shots|momentum|commentary|coverage/` |
| GET / POST | `matches/<id>/overrides/` (administrator) |
| GET / POST | `leagues/`, `teams/`, `seasons/` |
| GET / PUT / PATCH / DELETE | `leagues/<id>/`, `teams/<id>/`, `seasons/<pk>/` |
| GET | `leagues/<id>/matches|standings/`, `teams/<id>/matches|players/`, `seasons/<pk>/matches/` |
| GET | `players/`, `players/<pk>/`, `players/<pk>/matches/` |
| GET | `internal/sync-runs/`, `internal/sync-runs/<pk>/`, `internal/health/` (staff) |
| POST | `internal/sync-jobs/` (staff; starts a scraper in a worker, 202 Accepted) |

Match filters: `status`, `status_code`, `date`, `date_from`, `date_to`,
`league_id`, `team_id`, `season_id`, `search`, `ordering`, `page`, `page_size`.
Ordering is limited to a whitelist, and date ranges to 90 days.

Name lookup is a searchable list (`?search=arsenal`), never a detail route:
names are non-unique, localized and mutable. A single match always needs its ID.

### Coverage semantics

Every feature endpoint answers with an envelope, not a bare list:

```json
{"available": true, "state": "available", "fetched_at": "...",
 "source_coverage_level": "xG", "entry_count": 2, "data": [...]}
```

`state` distinguishes `not_fetched`, `available`, `empty`, `unavailable` and
`failed`. A successful HTTP 200 on `matchDetails` never implies lineups, stats
or commentary exist, and a later empty response keeps the last known good data.


## Postman collection

`postman_collection.json` (schema v2.1.0) covers all 38 routes and every method
in 10 folders, including a **Scrapers (run ingestion)** folder that starts the
fixture, live, coverage, reconcile and backfill jobs over HTTP and polls their
run records; `postman_environment.json` holds the local defaults.

`base_url` and `token` are the only variables. Every other value — match,
league, team, season, player and run IDs, dates and request bodies — is written
literally into that request's path, query string or body, so each request is
self-contained and editable where you read it.

1. Import both files and select the **Football API — local** environment. Point
   `base_url` at your server (default `http://localhost:8000`).
2. Put the key from `manage.py create_api_token` in the `token` variable; the
   environment ships with the local dev key already filled in. Collection-level
   auth sends `Authorization: Token {{token}}` on every request.
3. Run **Token check → Verify token (me)** to confirm it works.
4. The IDs default to the sample FA Cup fixture as it exists in the current dev
   database (match `6469292`, league `953430`, team `674304`, sync run `2`).
   Seasons and players have no rows yet, so those requests answer 404 until
   fixtures carrying a season name and coverage lineups are ingested. If your
   database holds different data, run
   **Matches → List matches** and copy an ID from its results into the URL you
   want — it also logs the first match, league and team ID to the Postman
   console.

There are no login or logout requests: the single token covers everything.
Because that token carries the `admin` role, every request in the collection is
permitted, including the staff-only internal routes and the administrator-only
overrides. The `(staff)` and `(admin)` labels stay in the names so the required
privilege is visible if narrower tokens are issued later, and the test scripts
still accept the 403 path.

Optional query parameters are included but disabled — enable them in Postman
rather than typing them: `status`, `status_code`, `date`, `date_from`,
`date_to`, `league_id`, `team_id`, `season_id`, `search`, `ordering`.

Test scripts assert the contract, not just status codes: the pagination
envelope, the 200-row page cap, `null` scores on upcoming matches,
`duration_minutes` of 120 for AET, the five coverage `state` values, and
commentary never reporting `available` with empty data. The **Error cases**
folder checks 401 vs 403, 404, the ordering whitelist, reversed and
over-90-day date ranges and a malformed date, each against the
`{code, message, details, request_id}` envelope.

Note the asymmetry in the write examples: `POST /api/matches/` and
`POST /api/seasons/` take **local primary keys** for `league`, `home_team`,
`away_team`, not FotMob IDs as the read routes do. The bodies use placeholder
PKs; adjust them for your database.

Run the whole collection from the Postman runner, or headlessly:

```bash
newman run postman_collection.json -e postman_environment.json
```

## Ingestion

Commands reuse `api/service.py`; the two standalone scrapers stay as file-only
QA tools. Shared flags: `--dry-run`, `--request-gap`, `--trigger`, `--json`.

```bash
.venv/bin/python manage.py sync_fotmob_fixtures --past-days 1 --future-days 3
```

```bash
.venv/bin/python manage.py sync_fotmob_live
```

```bash
.venv/bin/python manage.py sync_fotmob_coverage --details-for live --days-back 1
```

```bash
.venv/bin/python manage.py daily_fixture_reconcile --past-days 30 --future-days 30
```

Each run writes an `IngestionRun` row with created/updated/unchanged/skipped/
invalid/partial counters, provider request and error totals, duration and
quarantined payloads, exposed at `/api/internal/sync-runs/`.

### Where player data comes from

There is no player feed and no player scraper. FotMob exposes squads and player
statistics only inside `matchDetails`, so **players are a by-product of coverage
ingestion**: a `Player` row appears the first time that player shows up in an
ingested lineup, match event or `playerStats` block. An empty `/api/players/` or
`/api/teams/<id>/players/` means coverage has not been synced yet, not that the
endpoint is broken.

One match, the quickest way to get real data:

```bash
.venv/bin/python manage.py sync_fotmob_coverage --match-id 5868121
```

Many matches, walking recently completed fixtures whose coverage is missing:

```bash
.venv/bin/python manage.py sync_fotmob_coverage --details-for all --days-back 2 --limit 50
```

One matchDetails call per match writes:

| Table | From | Example (match 5868121) |
| --- | --- | --- |
| `Player` | lineups, events, player stats | 46 rows, with position line and country |
| `TeamPlayer` | lineup membership | 46 squad links, read by `teams/<id>/players/` |
| `LineupPlayer` | lineups | shirt, position, rating, minutes, captain |
| `PlayerMatchStat` | `content.playerStats` | 905 rows across 54 stat keys |
| `MatchEvent.player` | timeline | goals and cards linked to their player |

Coverage depth follows FotMob's own `source_coverage_level`. A top-division
fixture is `xG` and carries everything above; a `lower`-level fixture may have
no lineups at all, which is reported as `state: "empty"` rather than an error.
Check `/api/matches/<id>/coverage/` before assuming data is missing.

Read it back with `/api/matches/<id>/lineups/` (ratings and minutes) and
`/api/matches/<id>/player-stats/` (every stat key, optionally filtered with
`?player_id=`). Fractions keep both parts: `accurate_passes` is `"23/32"` with
`numeric_value: 23.0`.

### Running a scraper over HTTP

The same jobs can be started by staff through
`POST /api/internal/sync-jobs/`, which is what the Postman **Scrapers (run
ingestion)** folder uses:

```bash
curl -s -X POST http://localhost:8000/api/internal/sync-jobs/ -H 'Authorization: Token <key>' -H 'Content-Type: application/json' -d '{"job":"sync_fotmob_live","days_back":0,"dry_run":true}'
```

The job runs in a background worker and the response is **202** straight away
with a `status_url`, so no upstream fetching ever happens inside a response.
Poll `/api/internal/sync-runs/?job=<job>` until `state` leaves `running`.

- `job`: `sync_fotmob_fixtures`, `sync_fotmob_live`, `sync_fotmob_coverage`,
  `daily_fixture_reconcile`, `weekly_backfill`
- `dry_run` **defaults to `true`** — a request must opt in to writing. A dry run
  still fetches and classifies, so it is the safe way to check a window.
- window: `past_days` / `future_days` (0–60), or `start_date` + `end_date`
  (≤ 121 days); `days_back` (0–14) for live and coverage
- scope: `league_ids`, `match_ids` (coverage only, ≤ 50 each), `limit` (≤ 500),
  `details_for` (`live` / `all`), `store_raw`
- starting the same job twice returns **409**; a `running` row left by a crashed
  process stops blocking after two hours

Cron does not use this route — it runs the management commands directly with
`flock`. The worker thread is a development convenience: for production
volumes, put a real queue worker behind `service.dispatch_job`. On SQLite a long
write job can also make concurrent requests wait on the database lock.

Guarantees in the write path:

* one match per FotMob ID, enforced by a unique constraint and `select_for_update`
  inside an atomic transaction; reruns of the same window add no duplicates
* a null score never overwrites a known score; an upcoming fixture is never 0-0
* `FT` stores `duration_minutes=90`, `AET`/`PEN` store 120, and a finished match
  reports no live minute
* POSTPONED / CANCELLED / ABANDONED / INTERRUPTED stay distinct status codes
  under the `disrupted` category (served by `matches/cancelled/`)
* a stale older snapshot cannot reopen a completed match
* a `LiveMatch` projection is only expired once the provider confirms a terminal
  state; an upstream failure never deletes a match
* fields with an active `MatchOverride` are not overwritten by cron

## Cron installation

`manage.py` commands run as the server user, use the ORM directly and need no
API token. `flock` prevents overlapping runs on a single host; use a
distributed lock for multiple replicas.

```cron

*/2 * * * * cd "$APP" && flock -n /tmp/fotmob_live.lock "$VENV" manage.py sync_fotmob_live >> logs/live.log 2>&1
*/15 * * * * cd "$APP" && flock -n /tmp/fotmob_fixtures.lock "$VENV" manage.py sync_fotmob_fixtures --past-days 1 --future-days 3 >> logs/fixtures.log 2>&1
*/5 * * * * cd "$APP" && flock -n /tmp/fotmob_coverage.lock "$VENV" manage.py sync_fotmob_coverage >> logs/coverage.log 2>&1
15 2 * * * cd "$APP" && flock -n /tmp/fotmob_reconcile.lock "$VENV" manage.py daily_fixture_reconcile --past-days 30 --future-days 30 >> logs/reconcile.log 2>&1
30 3 * * 0 cd "$APP" && flock -n /tmp/fotmob_backfill.lock "$VENV" manage.py weekly_backfill >> logs/backfill.log 2>&1
```

These cadences are starting targets, not safe rates: a 61-day window held ~13.6k
fixtures in testing, so wide refreshes belong in the daily job, not the
2-minute one. Tune with observed upstream response times and FotMob's terms.

## Before enabling scheduled writes

1. Run both standalone scrapers in JSON/CSV mode and archive representative
   completed, upcoming, live, cancelled and AET samples.
2. `manage.py test api` green (classification, coverage levels, auth,
   permissions, pagination, idempotency, time boundaries).
3. `--dry-run` a real window and check the created/unchanged counts.
4. Run the same window twice and confirm zero duplicates.
5. Enable cron gradually: fixtures, then live, then coverage, then reconcile.

## Deployment notes

* Settings still carry the development `SECRET_KEY`, `DEBUG = True`, an empty
  `ALLOWED_HOSTS` and SQLite. Move secrets to the environment, set
  `DEBUG = False`, serve over HTTPS and switch to PostgreSQL before production.
* `FOTMOB` in settings controls request pacing, timeout, retries, user agent and
  the live staleness threshold.
* DRF throttle scopes: `auth_token` 10/min, `search` 60/min, `internal` 30/min.
* FotMob's JSON endpoints are unofficial and may change without notice.
