"""FotMob live-match coverage scraper (file-output test version).

This version deliberately does NOT import Django, access models, or write to a
 database. It fetches FotMob fixtures and live-match coverage, then exports the
results to JSON, CSV, or both so the data shape can be reviewed before a Django
model is designed.

Examples:
    python live_coverage_scraper.py
    python live_coverage_scraper.py --format both
    python live_coverage_scraper.py --dates 20261109 20261110 --format json
    python live_coverage_scraper.py --details-for all --format csv

The FotMob endpoints are unofficial and may change. Use a reasonable polling
interval and review FotMob's terms before production use.
"""

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

logger = logging.getLogger("fotmob_live_coverage")

API = "https://www.fotmob.com/api/data"
TEAM_LOGO = "https://images.fotmob.com/image_resources/logo/teamlogo/{id}.png"
LEAGUE_LOGO = "https://images.fotmob.com/image_resources/logo/leaguelogo/{id}.png"
REQUEST_GAP = 0.35
DEFAULT_TIMEOUT = 20
DEFAULT_RETRIES = 4

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.fotmob.com",
    "Referer": "https://www.fotmob.com/",
}


def _retry_after_seconds(value, fallback):
    """Parse Retry-After as either seconds or an HTTP date."""
    if not value:
        return fallback
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt_timezone.utc)
            return max(0.0, (when - datetime.now(dt_timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return fallback


def get_json(
    session,
    endpoint,
    params=None,
    retries=DEFAULT_RETRIES,
    timeout=DEFAULT_TIMEOUT,
    request_gap=REQUEST_GAP,
):
    """Fetch a FotMob JSON endpoint with bounded retries and polite pacing.

    Returns parsed JSON on success and None on failure. `endpoint` can be a
    relative API endpoint (e.g. "matches") or an absolute URL.
    """
    url = endpoint if endpoint.startswith(("http://", "https://")) else (
        f"{API}/{endpoint.lstrip('/')}"
    )

    for attempt in range(1, retries + 1):
        if request_gap:
            time.sleep(request_gap + random.uniform(0, request_gap * 0.35))
        try:
            response = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            logger.warning(
                "FotMob request failed (%s/%s) %s: %s",
                attempt, retries, url, exc,
            )
            if attempt < retries:
                time.sleep(min(20, 1.5 * (2 ** (attempt - 1)) + random.random()))
                continue
            return None

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                logger.warning("FotMob returned non-JSON for %s", response.url)
                return None

        if response.status_code == 429:
            wait = _retry_after_seconds(
                response.headers.get("Retry-After"),
                min(30, 2 * attempt),
            )
            logger.warning("FotMob rate limited (429); waiting %.1fs", wait)
        elif response.status_code in (403, 500, 502, 503, 504):
            wait = min(30, 1.5 * (2 ** (attempt - 1)) + random.random())
            logger.warning(
                "FotMob HTTP %s (%s/%s); waiting %.1fs",
                response.status_code, attempt, retries, wait,
            )
        else:
            logger.warning(
                "FotMob HTTP %s (not retrying): %s",
                response.status_code, response.url,
            )
            return None

        if attempt < retries:
            time.sleep(wait)

    return None


def _live_minute(live_time):
    short = (live_time or {}).get("short") or ""
    if short.upper() == "HT":
        return 45
    # FotMob commonly represents stoppage time as "45+2'"; summing the
    # numbers gives the elapsed match minute (47 in that example).
    parts = [int(part) for part in re.findall(r"\d+", short)]
    return sum(parts) if parts else 0


def _status(status):
    """Return normalized status fields while retaining FotMob's reason text."""
    status = status or {}
    reason = status.get("reason") or {}
    live_time = status.get("liveTime") or {}
    short = live_time.get("short") or ""
    long_key = (live_time.get("longKey") or "").lower()

    if status.get("cancelled"):
        return "POSTPONED", 0, reason.get("short") or "", reason.get("long") or ""

    if status.get("finished"):
        # Keep the normalized state FT, while reason_short can distinguish AET/PEN.
        return "FT", 90, reason.get("short") or "FT", reason.get("long") or "Full-Time"

    if status.get("started") or status.get("ongoing"):
        if long_key == "abandoned" or short.upper() == "AB":
            return "ABANDONED", _live_minute(live_time), short or "AB", live_time.get("long") or "Abandoned"
        if long_key == "interrupted" or short.upper() == "IR":
            return "INTERRUPTED", _live_minute(live_time), short or "IR", live_time.get("long") or "Interrupted"
        state = "HT" if short.upper() == "HT" else "LIVE"
        return state, _live_minute(live_time), "", ""

    return "NS", 0, "", ""


def _kickoff(match_data):
    status = match_data.get("status") or {}
    value = status.get("utcTime")
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)
        except (TypeError, ValueError):
            pass
    timestamp = match_data.get("timeTS")
    if timestamp:
        try:
            return datetime.fromtimestamp(float(timestamp) / 1000, tz=dt_timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    return None


def _half_start(match_data, key):
    """Convert FotMob's displayed local half-start time to UTC where possible."""
    raw = ((match_data.get("status") or {}).get("halfs") or {}).get(key)
    kickoff = _kickoff(match_data)
    local_time = match_data.get("time")
    if not raw or not kickoff or not local_time:
        return None
    try:
        local_kickoff = datetime.strptime(local_time, "%d.%m.%Y %H:%M").replace(
            tzinfo=dt_timezone.utc
        )
        offset = local_kickoff - kickoff
        converted = datetime.strptime(raw, "%d.%m.%Y %H:%M:%S").replace(
            tzinfo=dt_timezone.utc
        ) - offset
        return converted.isoformat()
    except ValueError:
        return None


def _score(value):
    return 0 if value is None else value


def normalize_match(match_data, league_entry, date_str, include_raw=True):
    """Convert a FotMob fixture into a stable, export-friendly dictionary."""
    match_data = match_data or {}
    league_entry = league_entry or {}
    home = match_data.get("home") or {}
    away = match_data.get("away") or {}
    status_code, minute, reason_short, reason_long = _status(
        match_data.get("status") or {}
    )
    kickoff = _kickoff(match_data)
    live_time = (match_data.get("status") or {}).get("liveTime") or {}

    result = {
        "source": "fotmob",
        "date_queried": date_str,
        "match_id": match_data.get("id"),
        "league": {
            "id": league_entry.get("primaryId") or league_entry.get("id"),
            "name": league_entry.get("name") or "",
            "country": league_entry.get("ccode") or league_entry.get("country") or "",
            "logo_url": None,
        },
        "season": league_entry.get("season") or league_entry.get("selectedSeason"),
        "round": match_data.get("tournamentStage") or "",
        "kickoff_utc": kickoff.isoformat() if kickoff else None,
        "status": {
            "code": status_code,
            "minute_elapsed": minute,
            "minute_display": "HT" if status_code == "HT" else live_time.get("short") or "",
            "reason_short": reason_short,
            "reason_long": reason_long,
            "finished": bool((match_data.get("status") or {}).get("finished")),
            "started": bool((match_data.get("status") or {}).get("started")),
            "ongoing": bool((match_data.get("status") or {}).get("ongoing")),
        },
        "home": {
            "id": home.get("id"),
            "name": home.get("name") or "",
            "score": _score(home.get("score")),
            "red_cards": _score(home.get("redCards")),
            "logo_url": TEAM_LOGO.format(id=home["id"]) if home.get("id") else None,
        },
        "away": {
            "id": away.get("id"),
            "name": away.get("name") or "",
            "score": _score(away.get("score")),
            "red_cards": _score(away.get("redCards")),
            "logo_url": TEAM_LOGO.format(id=away["id"]) if away.get("id") else None,
        },
        "halves": {
            "first_half_started_utc": _half_start(match_data, "firstHalfStarted"),
            "second_half_started_utc": _half_start(match_data, "secondHalfStarted"),
        },
        "coverage": {
            "fetched": False,
            "events": [],
            "lineups": {},
            "stats": {},
            "momentum": {},
            "commentary": {},
            "details_error": None,
        },
    }

    league_id = result["league"]["id"]
    if league_id:
        result["league"]["logo_url"] = LEAGUE_LOGO.format(id=league_id)

    if include_raw:
        result["raw_match"] = match_data
    return result


def _fetch_liveticker(session, match_id, details, match_data, request_gap=REQUEST_GAP):
    content = details.get("content") or {}
    ticker_meta = content.get("liveticker") or {}
    teams = ticker_meta.get("teams") or [
        (match_data.get("home") or {}).get("name"),
        (match_data.get("away") or {}).get("name"),
    ]
    teams = [team for team in teams if team]
    ticker_url = ticker_meta.get("ltcUrl") or (
        f"https://data.fotmob.com/webcl/ltc/gsm/{match_id}_en.json.gz"
    )
    return get_json(
        session,
        "ltc",
        {
            "ltcUrl": ticker_url,
            "teams": json.dumps(teams, ensure_ascii=False, separators=(",", ":")),
        },
        retries=2,
        request_gap=request_gap,
    )


def fetch_match_coverage(session, match_data, request_gap=REQUEST_GAP, include_raw=True):
    """Fetch matchDetails and, when available, the live ticker for one match."""
    match_id = match_data.get("id")
    if not match_id:
        return {"fetched": False, "details_error": "missing_match_id"}

    details = get_json(
        session,
        "matchDetails",
        {"matchId": match_id},
        request_gap=request_gap,
    )
    if not isinstance(details, dict) or not details:
        return {"fetched": False, "details_error": "match_details_unavailable"}

    content = details.get("content") or {}
    match_facts = content.get("matchFacts") or {}
    header = details.get("header") or {}
    commentary = _fetch_liveticker(
        session, match_id, details, match_data, request_gap=request_gap
    )

    events = header.get("events") or content.get("events") or []
    if not events and isinstance(commentary, dict):
        events = commentary.get("events") or []

    coverage = {
        "fetched": True,
        "events": events,
        "lineups": content.get("lineup") or {},
        "stats": content.get("stats") or {},
        "momentum": match_facts.get("momentum") or {},
        "commentary": commentary if commentary is not None else (
            content.get("liveticker") or {}
        ),
        "details_error": None,
    }
    if include_raw:
        coverage["raw_details"] = details
    return coverage


def _default_dates(days_back=1):
    """Return UTC dates from N days ago through today, oldest first."""
    today = datetime.now(dt_timezone.utc).date()
    return [
        (today - timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(days_back, -1, -1)
    ]


def scrape_live_coverage(
    dates=None,
    details_for="live",
    include_raw=True,
    request_gap=REQUEST_GAP,
    limit=None,
):
    """Fetch fixtures and coverage into memory; this function never writes a DB.

    Args:
        dates: List of YYYYMMDD strings. Defaults to yesterday and today (UTC).
        details_for: "live" fetches coverage for LIVE/HT matches only; "all"
            fetches coverage for every listed match (more API requests).
        include_raw: Include raw fixture and matchDetails payloads in JSON output.
        request_gap: Minimum delay between API requests, before small jitter.
        limit: Optional maximum number of fixtures to process (useful for tests).

    Returns a report dictionary suitable for JSON or CSV export.
    """
    if details_for not in ("live", "all"):
        raise ValueError("details_for must be 'live' or 'all'")

    dates = dates or _default_dates(days_back=1)
    matches = []
    date_results = []
    api_errors = []
    seen_ids = set()

    with requests.Session() as session:
        session.headers.update(HEADERS)

        for date_str in dates:
            payload = get_json(
                session,
                "matches",
                {"date": date_str},
                request_gap=request_gap,
            )
            if not isinstance(payload, dict):
                api_errors.append({"date": date_str, "endpoint": "matches", "error": "request_failed"})
                date_results.append({"date": date_str, "fixtures": 0, "live": 0})
                continue

            date_fixture_count = 0
            date_live_count = 0
            for league_entry in payload.get("leagues") or []:
                for match_data in league_entry.get("matches") or []:
                    match_id = match_data.get("id")
                    if match_id is not None:
                        try:
                            normalized_id = int(match_id)
                        except (TypeError, ValueError):
                            normalized_id = str(match_id)
                        if normalized_id in seen_ids:
                            continue
                        seen_ids.add(normalized_id)

                    record = normalize_match(
                        match_data,
                        league_entry,
                        date_str,
                        include_raw=include_raw,
                    )
                    date_fixture_count += 1
                    is_live = record["status"]["code"] in ("LIVE", "HT")
                    if is_live:
                        date_live_count += 1

                    should_fetch_details = details_for == "all" or is_live
                    if should_fetch_details:
                        record["coverage"] = fetch_match_coverage(
                            session,
                            match_data,
                            request_gap=request_gap,
                            include_raw=include_raw,
                        )
                        if not record["coverage"].get("fetched"):
                            api_errors.append({
                                "match_id": match_id,
                                "endpoint": "matchDetails",
                                "error": record["coverage"].get("details_error"),
                            })
                    matches.append(record)

                    if limit is not None and len(matches) >= limit:
                        break
                if limit is not None and len(matches) >= limit:
                    break

            date_results.append({
                "date": date_str,
                "fixtures": date_fixture_count,
                "live": date_live_count,
            })
            if limit is not None and len(matches) >= limit:
                break

    live_matches = [
        item for item in matches if item["status"]["code"] in ("LIVE", "HT")
    ]
    coverage_fetched = sum(1 for item in matches if item["coverage"].get("fetched"))

    return {
        "source": "FotMob",
        "generated_at_utc": datetime.now(dt_timezone.utc).isoformat(),
        "requested_dates": list(dates),
        "details_for": details_for,
        "totals": {
            "fixtures": len(matches),
            "live_matches": len(live_matches),
            "coverage_fetched": coverage_fetched,
            "coverage_failed": sum(
                1 for item in matches
                if item["coverage"].get("details_error")
            ),
            "api_errors": len(api_errors),
        },
        "date_results": date_results,
        "api_errors": api_errors,
        "matches": matches,
    }


def _json_default(value):
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


def write_json(report, output_path):
    """Write the complete nested report as UTF-8 JSON."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=_json_default)
        handle.write("\n")
    temp_path.replace(path)
    return path


def _json_cell(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write_csv(report, output_path):
    """Write one row per match; nested coverage fields are JSON-encoded cells."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "date_queried", "match_id", "league_id", "league_name", "league_country",
        "round", "kickoff_utc", "status", "minute_elapsed", "minute_display",
        "reason_short", "reason_long", "home_team_id", "home_team", "home_score",
        "home_red_cards", "away_team_id", "away_team", "away_score", "away_red_cards",
        "coverage_fetched", "events_json", "lineups_json", "stats_json",
        "momentum_json", "commentary_json", "details_error", "raw_match_json",
        "raw_details_json",
    ]

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in report.get("matches", []):
            league = item.get("league") or {}
            status = item.get("status") or {}
            home = item.get("home") or {}
            away = item.get("away") or {}
            coverage = item.get("coverage") or {}
            writer.writerow({
                "date_queried": item.get("date_queried"),
                "match_id": item.get("match_id"),
                "league_id": league.get("id"),
                "league_name": league.get("name"),
                "league_country": league.get("country"),
                "round": item.get("round"),
                "kickoff_utc": item.get("kickoff_utc"),
                "status": status.get("code"),
                "minute_elapsed": status.get("minute_elapsed"),
                "minute_display": status.get("minute_display"),
                "reason_short": status.get("reason_short"),
                "reason_long": status.get("reason_long"),
                "home_team_id": home.get("id"),
                "home_team": home.get("name"),
                "home_score": home.get("score"),
                "home_red_cards": home.get("red_cards"),
                "away_team_id": away.get("id"),
                "away_team": away.get("name"),
                "away_score": away.get("score"),
                "away_red_cards": away.get("red_cards"),
                "coverage_fetched": coverage.get("fetched", False),
                "events_json": _json_cell(coverage.get("events", [])),
                "lineups_json": _json_cell(coverage.get("lineups", {})),
                "stats_json": _json_cell(coverage.get("stats", {})),
                "momentum_json": _json_cell(coverage.get("momentum", {})),
                "commentary_json": _json_cell(coverage.get("commentary", {})),
                "details_error": coverage.get("details_error"),
                "raw_match_json": _json_cell(item.get("raw_match", {})),
                "raw_details_json": _json_cell(coverage.get("raw_details", {})),
            })
    return path


def export_report(report, output_path, output_format="json"):
    """Export JSON, CSV, or both. Returns the paths written."""
    if output_format not in ("json", "csv", "both"):
        raise ValueError("output_format must be 'json', 'csv', or 'both'")

    base = Path(output_path)
    stem = base.with_suffix("") if base.suffix.lower() in (".json", ".csv") else base
    written = []

    if output_format in ("json", "both"):
        json_path = stem.with_suffix(".json")
        written.append(write_json(report, json_path))
    if output_format in ("csv", "both"):
        csv_path = stem.with_suffix(".csv")
        written.append(write_csv(report, csv_path))
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fetch FotMob live match coverage to JSON/CSV without database writes."
    )
    parser.add_argument(
        "--dates", nargs="+", help="UTC date(s) in YYYYMMDD format; defaults to yesterday and today."
    )
    parser.add_argument(
        "--days-back", type=int, default=1,
        help="When --dates is omitted, include this many days before today (default: 1).",
    )
    parser.add_argument(
        "--format", choices=("json", "csv", "both"), default="json",
        help="Export format (default: json). JSON is best for nested event/coverage data.",
    )
    parser.add_argument(
        "--output", default="data/fotmob_live_coverage.json",
        help="Output file path; extension is selected by --format.",
    )
    parser.add_argument(
        "--details-for", choices=("live", "all"), default="live",
        help="Fetch detailed coverage for LIVE/HT matches only, or all fixtures (default: live).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Optional fixture limit for a small test run.",
    )
    parser.add_argument(
        "--request-gap", type=float, default=REQUEST_GAP,
        help="Delay between requests in seconds (default: 0.35).",
    )
    parser.add_argument(
        "--omit-raw", action="store_true",
        help="Omit raw FotMob fixture/details payloads to reduce output size.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    dates = args.dates or _default_dates(days_back=max(0, args.days_back))
    for date_str in dates:
        if not re.fullmatch(r"\d{8}", date_str):
            parser.error(f"Invalid date {date_str!r}; expected YYYYMMDD.")

    report = scrape_live_coverage(
        dates=dates,
        details_for=args.details_for,
        include_raw=not args.omit_raw,
        request_gap=max(0.0, args.request_gap),
        limit=args.limit,
    )
    paths = export_report(report, args.output, args.format)

    print("\nFotMob scrape complete")
    print(json.dumps(report["totals"], indent=2))
    for path in paths:
        print(f"Saved: {path.resolve()}")

    return 0 if report["totals"]["api_errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
