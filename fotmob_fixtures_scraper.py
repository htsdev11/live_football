#!/usr/bin/env python3
"""
FotMob completed + upcoming fixtures scraper.

Fetches one calendar day per request from FotMob's public daily matches
endpoint, flattens the league groups, and classifies each match as:
completed, live, upcoming, or cancelled.

Install:
    python -m pip install requests

Examples:
    # Previous 30 days + next 30 days (including today)
    python fotmob_fixtures_scraper.py

    # A specific date range
    python fotmob_fixtures_scraper.py --start-date 2026-11-01 --end-date 2026-11-20

    # Only selected league IDs
    python fotmob_fixtures_scraper.py --league-ids 47,87,54

Output folder:
    fotmob_fixtures/
        all_matches.json
        completed_matches.json
        upcoming_matches.json
        live_matches.json
        cancelled_matches.json
        all_matches.csv
        completed_matches.csv
        upcoming_matches.csv
        live_matches.csv
        cancelled_matches.csv
        summary.json

This is an unofficial integration. Keep request volume modest and check
FotMob's applicable terms before running automated collection.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


API_URL = "https://www.fotmob.com/api/data/matches"

FINISHED_CODES = {"FT", "AET", "PEN", "AP", "WO", "AWD"}
LIVE_CODES = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP"}

CSV_FIELDS = [
    "match_id",
    "date_queried",
    "kickoff_utc",
    "kickoff_display",
    "league_id",
    "league_primary_id",
    "league_name",
    "league_country_code",
    "home_team",
    "home_score",
    "away_team",
    "away_score",
    "category",
    "status_code",
    "score_str",
    "started",
    "finished",
    "cancelled",
]


def build_session() -> requests.Session:
    """Create a polite HTTP session with retry handling."""
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=4,
        pool_maxsize=4,
    )

    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.fotmob.com/",
        }
    )
    return session


def fetch_matches_for_date(
    session: requests.Session,
    match_date: date,
    timeout: int,
) -> dict[str, Any]:
    """Fetch FotMob's match list for one calendar date."""
    date_param = match_date.strftime("%Y%m%d")

    response = session.get(
        API_URL,
        params={"date": date_param},
        timeout=timeout,
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        content_type = response.headers.get("Content-Type", "")
        raise RuntimeError(
            f"FotMob returned non-JSON content for {date_param} "
            f"(HTTP {response.status_code}, Content-Type: {content_type})."
        ) from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("leagues"), list):
        raise RuntimeError(
            f"Unexpected response structure for {date_param}: "
            "expected a JSON object containing a 'leagues' list."
        )

    return payload


def classify_match(status: dict[str, Any]) -> tuple[str, bool, bool, bool, str | None]:
    """Classify a fixture using FotMob status flags, with code fallbacks."""
    reason = status.get("reason")
    if not isinstance(reason, dict):
        reason = {}

    status_code = (
        status.get("reason_short")
        or reason.get("short")
        or status.get("short")
    )
    status_code = str(status_code).upper() if status_code is not None else None

    cancelled_value = status.get("cancelled")
    cancelled = (
        bool(cancelled_value)
        if cancelled_value is not None
        else status_code in {"CANC", "CANCELLED", "POSTP"}
    )

    finished_value = status.get("finished")
    finished = (
        bool(finished_value)
        if finished_value is not None
        else status_code in FINISHED_CODES
    )

    started_value = status.get("started")
    started = (
        bool(started_value)
        if started_value is not None
        else (finished or status_code in LIVE_CODES)
    )

    if cancelled:
        category = "cancelled"
    elif finished:
        category = "completed"
    elif started:
        category = "live"
    else:
        category = "upcoming"

    return category, started, finished, cancelled, status_code


def team_record(team: Any) -> dict[str, Any]:
    """Keep the useful team fields without inventing missing scores."""
    if not isinstance(team, dict):
        team = {}

    return {
        "id": team.get("id"),
        "name": team.get("name"),
        "score": team.get("score"),
    }


def normalize_match(
    match: dict[str, Any],
    league: dict[str, Any],
    match_date: date,
) -> dict[str, Any]:
    """Flatten one match while preserving its original status payload."""
    raw_status = match.get("status")
    if not isinstance(raw_status, dict):
        raw_status = {}

    category, started, finished, cancelled, status_code = classify_match(raw_status)

    league_id = (
        league.get("id")
        or match.get("leagueId")
        or league.get("primaryId")
    )

    return {
        "source": "fotmob",
        "date_queried": match_date.isoformat(),
        "match_id": match.get("id") or match.get("matchId"),
        "league": {
            "id": league_id,
            "primary_id": league.get("primaryId"),
            "name": league.get("name"),
            "country_code": league.get("ccode") or league.get("countryCode"),
        },
        "round": match.get("tournamentStage") or match.get("round"),
        "kickoff_utc": raw_status.get("utcTime") or match.get("utcTime"),
        "kickoff_display": match.get("time"),
        "time_ts": match.get("timeTS"),
        "home": team_record(match.get("home")),
        "away": team_record(match.get("away")),
        "status": {
            "category": category,
            "code": status_code,
            "score_str": raw_status.get("scoreStr"),
            "started": started,
            "finished": finished,
            "cancelled": cancelled,
            "raw": raw_status,
        },
    }


def csv_row(match: dict[str, Any]) -> dict[str, Any]:
    """Convert a normalized match into a flat CSV row."""
    league = match.get("league") or {}
    home = match.get("home") or {}
    away = match.get("away") or {}
    status = match.get("status") or {}

    return {
        "match_id": match.get("match_id"),
        "date_queried": match.get("date_queried"),
        "kickoff_utc": match.get("kickoff_utc"),
        "kickoff_display": match.get("kickoff_display"),
        "league_id": league.get("id"),
        "league_primary_id": league.get("primary_id"),
        "league_name": league.get("name"),
        "league_country_code": league.get("country_code"),
        "home_team": home.get("name"),
        "home_score": home.get("score"),
        "away_team": away.get("name"),
        "away_score": away.get("score"),
        "category": status.get("category"),
        "status_code": status.get("code"),
        "score_str": status.get("score_str"),
        "started": status.get("started"),
        "finished": status.get("finished"),
        "cancelled": status.get("cancelled"),
    }


def write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_csv(path: Path, matches: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for match in matches:
            writer.writerow(csv_row(match))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch completed, upcoming, live and cancelled FotMob matches."
    )

    parser.add_argument(
        "--start-date",
        help="First date to fetch, in YYYY-MM-DD format. Use with --end-date.",
    )
    parser.add_argument(
        "--end-date",
        help="Last date to fetch, in YYYY-MM-DD format. Use with --start-date.",
    )
    parser.add_argument(
        "--past-days",
        type=int,
        default=30,
        help="Days before today to include when no explicit date range is given (default: 30).",
    )
    parser.add_argument(
        "--future-days",
        type=int,
        default=30,
        help="Days after today to include when no explicit date range is given (default: 30).",
    )
    parser.add_argument(
        "--out-dir",
        default="fotmob_fixtures",
        help="Output directory (default: fotmob_fixtures).",
    )
    parser.add_argument(
        "--league-ids",
        help="Optional comma-separated FotMob league IDs, e.g. 47,87,54.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Pause between daily requests in seconds (default: 0.5).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=25,
        help="HTTP timeout in seconds (default: 25).",
    )

    args = parser.parse_args()

    if bool(args.start_date) != bool(args.end_date):
        parser.error("Provide both --start-date and --end-date, or neither.")

    if args.past_days < 0 or args.future_days < 0:
        parser.error("--past-days and --future-days cannot be negative.")

    if args.delay < 0:
        parser.error("--delay cannot be negative.")

    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero.")

    try:
        if args.start_date:
            args.start_date = date.fromisoformat(args.start_date)
            args.end_date = date.fromisoformat(args.end_date)
            if args.end_date < args.start_date:
                parser.error("--end-date must be on or after --start-date.")
        else:
            today = datetime.now(timezone.utc).date()
            args.start_date = today - timedelta(days=args.past_days)
            args.end_date = today + timedelta(days=args.future_days)
    except ValueError:
        parser.error("Dates must use YYYY-MM-DD format.")

    if args.league_ids:
        args.league_ids = {
            item.strip()
            for item in args.league_ids.split(",")
            if item.strip()
        }
    else:
        args.league_ids = None

    return args


def main() -> int:
    args = parse_args()
    output_dir = Path(args.out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    session = build_session()
    all_matches: list[dict[str, Any]] = []
    date_results: list[dict[str, Any]] = []
    api_errors: list[dict[str, str]] = []
    seen_match_ids: set[str] = set()

    current_date = args.start_date
    while current_date <= args.end_date:
        date_label = current_date.isoformat()

        try:
            payload = fetch_matches_for_date(
                session=session,
                match_date=current_date,
                timeout=args.timeout,
            )

            day_count = 0
            for league in payload.get("leagues", []):
                if not isinstance(league, dict):
                    continue

                league_matches = league.get("matches", [])
                if not isinstance(league_matches, list):
                    continue

                for raw_match in league_matches:
                    if not isinstance(raw_match, dict):
                        continue

                    normalized = normalize_match(
                        raw_match,
                        league,
                        current_date,
                    )

                    if args.league_ids is not None:
                        league_info = normalized["league"]
                        candidate_ids = {
                            str(league_info.get("id")),
                            str(league_info.get("primary_id")),
                        }
                        if not candidate_ids.intersection(args.league_ids):
                            continue

                    match_id = normalized.get("match_id")
                    if match_id is not None:
                        dedupe_key = str(match_id)
                        if dedupe_key in seen_match_ids:
                            continue
                        seen_match_ids.add(dedupe_key)

                    all_matches.append(normalized)
                    day_count += 1

            date_results.append(
                {
                    "date": date_label,
                    "matches": day_count,
                    "error": None,
                }
            )
            logging.info("%s: %d matches", date_label, day_count)

        except Exception as exc:
            message = str(exc)
            api_errors.append({"date": date_label, "error": message})
            date_results.append(
                {
                    "date": date_label,
                    "matches": 0,
                    "error": message,
                }
            )
            logging.exception("Failed to fetch %s", date_label)

        current_date += timedelta(days=1)
        if current_date <= args.end_date and args.delay:
            time.sleep(args.delay)

    session.close()

    all_matches.sort(
        key=lambda item: (
            item.get("kickoff_utc") or "",
            str(item.get("match_id") or ""),
        )
    )

    categories = {
        "completed": [],
        "upcoming": [],
        "live": [],
        "cancelled": [],
    }
    for match in all_matches:
        category = match.get("status", {}).get("category")
        if category in categories:
            categories[category].append(match)

    generated_at = datetime.now(timezone.utc).isoformat()
    totals = {
        "fixtures": len(all_matches),
        "completed_matches": len(categories["completed"]),
        "upcoming_matches": len(categories["upcoming"]),
        "live_matches": len(categories["live"]),
        "cancelled_matches": len(categories["cancelled"]),
        "api_errors": len(api_errors),
    }

    common_metadata = {
        "source": "FotMob",
        "generated_at_utc": generated_at,
        "date_range": {
            "start": args.start_date.isoformat(),
            "end": args.end_date.isoformat(),
        },
        "totals": totals,
    }

    write_json(
        output_dir / "all_matches.json",
        {**common_metadata, "matches": all_matches},
    )

    output_names = {
        "completed": "completed_matches",
        "upcoming": "upcoming_matches",
        "live": "live_matches",
        "cancelled": "cancelled_matches",
    }

    for category, file_stem in output_names.items():
        records = categories[category]
        write_json(
            output_dir / f"{file_stem}.json",
            {**common_metadata, "matches": records},
        )
        write_csv(output_dir / f"{file_stem}.csv", records)

    write_csv(output_dir / "all_matches.csv", all_matches)

    summary = {
        **common_metadata,
        "date_results": date_results,
        "api_errors": api_errors,
    }
    write_json(output_dir / "summary.json", summary)

    print("\n========== FOTMOB SCRAPER SUMMARY ==========")
    print(f"Date range:       {args.start_date} to {args.end_date}")
    print(f"Total fixtures:   {totals['fixtures']}")
    print(f"Completed:        {totals['completed_matches']}")
    print(f"Upcoming:         {totals['upcoming_matches']}")
    print(f"Live:             {totals['live_matches']}")
    print(f"Cancelled:        {totals['cancelled_matches']}")
    print(f"API errors:       {totals['api_errors']}")
    print(f"Output folder:    {output_dir.resolve()}")
    print("============================================\n")

    return 1 if api_errors and not all_matches else 0


if __name__ == "__main__":
    sys.exit(main())
