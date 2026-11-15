"""Query, normalization and ingestion services.

This module is the only place that talks to FotMob and the only place that
writes provider data into ``core.models``. APIViews call the query helpers;
management commands call the ingestion functions. Neither duplicates fetching
code, and no APIView ever triggers an upstream HTTP request.

Sections
--------
1. FotMob HTTP client (shared by both scraper flows)
2. Normalizers (fixtures, status, coverage)
3. Upserts and ingestion jobs
4. Query services for the read API
"""

from __future__ import annotations

import logging
import random
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone as dt_timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Iterator, Sequence

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from core.models import (
    IN_PROGRESS_CODES,
    AuditLog,
    CoverageFeature,
    CoverageFeatureChoices,
    CoverageState,
    IngestionError,
    IngestionRun,
    IngestionState,
    IngestionTrigger,
    League,
    LineupPlayer,
    LiveMatch,
    Match,
    MatchCategory,
    MatchCommentary,
    MatchCoverage,
    MatchEvent,
    MatchLineup,
    MatchMomentum,
    MatchRawSnapshot,
    MatchShot,
    MatchStatusCode,
    Player,
    PlayerMatchStat,
    Season,
    SourceChoices,
    StatPeriod,
    Team,
    TeamMatchStat,
    TeamPlayer,
)

logger = logging.getLogger('api')

API_BASE = 'https://www.fotmob.com/api/data'
TEAM_LOGO = 'https://images.fotmob.com/image_resources/logo/teamlogo/{id}.png'
LEAGUE_LOGO = 'https://images.fotmob.com/image_resources/logo/leaguelogo/{id}.png'

#: Statuses whose coverage is worth refreshing on the 5-minute cadence.
COVERAGE_PRIORITY_CODES = tuple(IN_PROGRESS_CODES)


# ---------------------------------------------------------------------------
# 1. FotMob HTTP client
# ---------------------------------------------------------------------------


class FotMobError(Exception):
    """Raised for upstream failures that the caller should record."""

    def __init__(self, message: str, category: str = IngestionError.Category.HTTP):
        super().__init__(message)
        self.category = category


def _retry_after_seconds(value: str | None, fallback: float) -> float:
    """Parse ``Retry-After`` given either as seconds or an HTTP date."""
    if not value:
        return fallback
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if when is None:
        return fallback
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt_timezone.utc)
    return max(0.0, (when - datetime.now(dt_timezone.utc)).total_seconds())


class FotMobClient:
    """Bounded-retry JSON client with polite pacing and request counters.

    The two standalone scrapers keep their own copies for file-only QA runs;
    production ingestion uses this one so pacing, retries and error categories
    are defined in a single place.
    """

    #: Codes worth retrying. Other 4xx responses are not transient.
    RETRY_STATUSES = (429, 500, 502, 503, 504)

    def __init__(
        self,
        request_gap: float | None = None,
        timeout: int | None = None,
        retries: int | None = None,
        session: requests.Session | None = None,
    ):
        config = getattr(settings, 'FOTMOB', {})
        self.request_gap = config.get('REQUEST_GAP', 0.5) if request_gap is None else request_gap
        self.timeout = config.get('TIMEOUT', 25) if timeout is None else timeout
        self.retries = config.get('RETRIES', 4) if retries is None else retries
        self.request_count = 0
        self.error_count = 0
        self._owns_session = session is None
        self.session = session or requests.Session()
        self.session.headers.update({
            'User-Agent': config.get(
                'USER_AGENT',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
            ),
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Origin': 'https://www.fotmob.com',
            'Referer': 'https://www.fotmob.com/',
        })

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def __enter__(self) -> 'FotMobClient':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def get_json(self, endpoint: str, params: dict | None = None, retries: int | None = None):
        """Return parsed JSON, or raise :class:`FotMobError` after retries."""
        url = endpoint if endpoint.startswith(('http://', 'https://')) else (
            f'{API_BASE}/{endpoint.lstrip("/")}'
        )
        attempts = self.retries if retries is None else retries
        last_error = 'unknown'

        for attempt in range(1, attempts + 1):
            if self.request_gap:
                time.sleep(self.request_gap + random.uniform(0, self.request_gap * 0.35))
            self.request_count += 1
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                self.error_count += 1
                last_error = f'request_failed: {exc}'
                logger.warning('FotMob request failed (%s/%s) %s: %s',
                               attempt, attempts, url, exc)
                if attempt < attempts:
                    time.sleep(min(20, 1.5 * (2 ** (attempt - 1)) + random.random()))
                    continue
                raise FotMobError(last_error) from exc

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    self.error_count += 1
                    raise FotMobError(
                        f'non_json_response: {response.url}',
                        IngestionError.Category.PARSE,
                    ) from exc

            self.error_count += 1
            if response.status_code not in self.RETRY_STATUSES:
                # Persistent 4xx: stop immediately rather than hammering.
                raise FotMobError(f'http_{response.status_code}')

            if response.status_code == 429:
                wait = _retry_after_seconds(
                    response.headers.get('Retry-After'), min(30, 2 * attempt),
                )
            else:
                wait = min(30, 1.5 * (2 ** (attempt - 1)) + random.random())
            last_error = f'http_{response.status_code}'
            logger.warning('FotMob HTTP %s (%s/%s); waiting %.1fs',
                           response.status_code, attempt, attempts, wait)
            if attempt < attempts:
                time.sleep(wait)

        raise FotMobError(last_error)

    # -- endpoint wrappers ---------------------------------------------------

    def fetch_matches_for_date(self, day: date) -> dict:
        payload = self.get_json('matches', {'date': day.strftime('%Y%m%d')})
        if not isinstance(payload, dict) or not isinstance(payload.get('leagues'), list):
            raise FotMobError(
                f'unexpected_shape_for_{day.isoformat()}',
                IngestionError.Category.PARSE,
            )
        return payload

    def fetch_match_details(self, fotmob_match_id: int) -> dict:
        payload = self.get_json('matchDetails', {'matchId': fotmob_match_id})
        if not isinstance(payload, dict) or not payload:
            raise FotMobError('match_details_unavailable', IngestionError.Category.PARSE)
        return payload

    def fetch_live_ticker(self, details: dict, teams: Sequence[str]) -> dict | None:
        """Fetch the live ticker only when the source advertises one."""
        import json as _json

        ticker_meta = ((details.get('content') or {}).get('liveticker') or {})
        ticker_url = ticker_meta.get('ltcUrl')
        if not ticker_url:
            # No ticker advertised: do not invent a URL and do not call.
            return None
        ticker_teams = ticker_meta.get('teams') or [t for t in teams if t]
        try:
            return self.get_json('ltc', {
                'ltcUrl': ticker_url,
                'teams': _json.dumps(ticker_teams, ensure_ascii=False,
                                     separators=(',', ':')),
            }, retries=2)
        except FotMobError as exc:
            logger.info('Live ticker unavailable: %s', exc)
            return None


# ---------------------------------------------------------------------------
# 2. Normalizers
# ---------------------------------------------------------------------------


@dataclass
class StatusResult:
    category: str
    status_code: str
    reason_short: str = ''
    reason_long: str = ''
    minute_elapsed: int | None = None
    minute_display: str = ''
    duration_minutes: int | None = None


def _digits_sum(text: str) -> int | None:
    parts = [int(part) for part in re.findall(r'\d+', text or '')]
    return sum(parts) if parts else None


def _classify_disruption(*texts: str) -> str:
    blob = ' '.join(t for t in texts if t).lower()
    if 'postpon' in blob:
        return MatchStatusCode.POSTPONED
    if 'abandon' in blob:
        return MatchStatusCode.ABANDONED
    if 'interrupt' in blob or 'suspend' in blob:
        return MatchStatusCode.INTERRUPTED
    return MatchStatusCode.CANCELLED


def classify_status(status: dict | None) -> StatusResult:
    """Map a FotMob status block onto the canonical state machine.

    Fixes the defects called out in the architecture document: a completed
    match never reports ``minute_elapsed`` of 90 for extra time, disruption
    reasons are not collapsed into one bucket, and a live minute is only
    produced while the match is actually in progress.
    """
    status = status or {}
    reason = status.get('reason') if isinstance(status.get('reason'), dict) else {}
    live_time = status.get('liveTime') if isinstance(status.get('liveTime'), dict) else {}

    reason_short = str(reason.get('short') or '')
    reason_long = str(reason.get('long') or '')
    live_short = str(live_time.get('short') or '')
    long_key = str(live_time.get('longKey') or '').lower()
    live_long = str(live_time.get('long') or '')

    cancelled = bool(status.get('cancelled'))
    finished = bool(status.get('finished'))
    started = bool(status.get('started')) or bool(status.get('ongoing'))

    if cancelled:
        code = _classify_disruption(reason_short, reason_long, long_key, live_long)
        return StatusResult(
            category=MatchCategory.DISRUPTED,
            status_code=code,
            reason_short=reason_short or code,
            reason_long=reason_long or live_long,
        )

    if finished:
        token = reason_short.upper()
        if long_key == 'abandoned' or token in {'AB', 'ABD'}:
            return StatusResult(
                category=MatchCategory.DISRUPTED,
                status_code=MatchStatusCode.ABANDONED,
                reason_short=reason_short or 'AB',
                reason_long=reason_long or live_long or 'Abandoned',
            )
        if token == 'AET':
            code, duration = MatchStatusCode.AFTER_EXTRA_TIME, 120
        elif token in {'PEN', 'AP'}:
            code, duration = MatchStatusCode.AFTER_PENALTIES, 120
        elif token == 'WO':
            code, duration = MatchStatusCode.WALKOVER, None
        elif token == 'AWD':
            code, duration = MatchStatusCode.AWARDED, None
        else:
            code, duration = MatchStatusCode.FULL_TIME, 90
        return StatusResult(
            category=MatchCategory.COMPLETED,
            status_code=code,
            reason_short=reason_short or 'FT',
            reason_long=reason_long or 'Full-Time',
            # Elapsed minute is meaningless once finished; duration is stored.
            minute_elapsed=None,
            duration_minutes=duration,
        )

    if started:
        upper_short = live_short.upper()
        if long_key == 'abandoned' or upper_short in {'AB', 'ABD'}:
            return StatusResult(
                category=MatchCategory.DISRUPTED,
                status_code=MatchStatusCode.ABANDONED,
                reason_short=live_short or 'AB',
                reason_long=live_long or 'Abandoned',
                minute_elapsed=_digits_sum(live_short),
                minute_display=live_short,
            )
        if long_key in {'interrupted', 'suspended'} or upper_short in {'IR', 'INT', 'SUSP'}:
            return StatusResult(
                category=MatchCategory.DISRUPTED,
                status_code=MatchStatusCode.INTERRUPTED,
                reason_short=live_short or 'IR',
                reason_long=live_long or 'Interrupted',
                minute_elapsed=_digits_sum(live_short),
                minute_display=live_short,
            )
        if upper_short == 'HT' or long_key == 'halftime':
            return StatusResult(
                category=MatchCategory.LIVE,
                status_code=MatchStatusCode.HALF_TIME,
                minute_elapsed=45,
                minute_display='HT',
            )
        if 'penalt' in long_key or upper_short in {'PEN', 'PSO'}:
            return StatusResult(
                category=MatchCategory.LIVE,
                status_code=MatchStatusCode.PENALTY_SHOOTOUT,
                minute_elapsed=120,
                minute_display=live_short or 'PEN',
            )
        if 'extratime' in long_key.replace(' ', '') or upper_short in {'ET', 'AET?'}:
            return StatusResult(
                category=MatchCategory.LIVE,
                status_code=MatchStatusCode.EXTRA_TIME,
                minute_elapsed=_digits_sum(live_short),
                minute_display=live_short or 'ET',
            )
        return StatusResult(
            category=MatchCategory.LIVE,
            status_code=MatchStatusCode.LIVE,
            minute_elapsed=_digits_sum(live_short),
            minute_display=live_short,
        )

    return StatusResult(
        category=MatchCategory.UPCOMING,
        status_code=MatchStatusCode.NOT_STARTED,
    )


def parse_kickoff(match_data: dict) -> datetime | None:
    """Return a timezone-aware UTC kickoff, or None when unknown."""
    status = match_data.get('status') or {}
    value = status.get('utcTime') or match_data.get('utcTime')
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)
        except (TypeError, ValueError):
            pass
    stamp = match_data.get('timeTS')
    if stamp:
        try:
            return datetime.fromtimestamp(float(stamp) / 1000, tz=dt_timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    return None


def parse_half_start(match_data: dict, key: str) -> datetime | None:
    """Convert FotMob's displayed local half-start time to UTC where possible."""
    raw = ((match_data.get('status') or {}).get('halfs') or {}).get(key)
    kickoff = parse_kickoff(match_data)
    local_time = match_data.get('time')
    if not raw or not kickoff or not local_time:
        return None
    try:
        local_kickoff = datetime.strptime(local_time, '%d.%m.%Y %H:%M').replace(
            tzinfo=dt_timezone.utc,
        )
        offset = local_kickoff - kickoff
        return datetime.strptime(raw, '%d.%m.%Y %H:%M:%S').replace(
            tzinfo=dt_timezone.utc,
        ) - offset
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_fixture(match_data: dict, league_entry: dict, queried_date: date) -> dict | None:
    """Flatten one fixture from the daily ``matches`` payload.

    Returns ``None`` when the record lacks the identifiers needed to store it
    safely (no match ID, or no team IDs); callers quarantine those payloads.
    """
    match_data = match_data or {}
    league_entry = league_entry or {}
    home = match_data.get('home') or {}
    away = match_data.get('away') or {}

    fotmob_id = _as_int(match_data.get('id') or match_data.get('matchId'))
    home_id = _as_int(home.get('id'))
    away_id = _as_int(away.get('id'))
    if not fotmob_id or not home_id or not away_id:
        return None

    status = classify_status(match_data.get('status'))
    is_upcoming = status.category == MatchCategory.UPCOMING

    # A not-started fixture has no score, whatever the payload displays.
    home_score = None if is_upcoming else _as_int(home.get('score'))
    away_score = None if is_upcoming else _as_int(away.get('score'))

    league_id = _as_int(league_entry.get('id') or match_data.get('leagueId'))
    primary_id = _as_int(league_entry.get('primaryId'))

    return {
        'date_queried': queried_date,
        'fotmob_id': fotmob_id,
        'league': {
            'fotmob_id': league_id or primary_id,
            'parent_fotmob_id': primary_id,
            'name': league_entry.get('name') or '',
            'country_code': league_entry.get('ccode') or league_entry.get('countryCode') or '',
            'logo_url': LEAGUE_LOGO.format(id=league_id or primary_id) if (league_id or primary_id) else '',
        },
        'season_year': league_entry.get('season') or league_entry.get('selectedSeason') or '',
        'provider_league_id': league_id,
        'provider_primary_league_id': primary_id,
        'round_name': str(match_data.get('tournamentStage') or match_data.get('round') or ''),
        'home': {
            'fotmob_id': home_id,
            'name': home.get('name') or '',
            'short_name': home.get('shortName') or '',
            'logo_url': TEAM_LOGO.format(id=home_id),
        },
        'away': {
            'fotmob_id': away_id,
            'name': away.get('name') or '',
            'short_name': away.get('shortName') or '',
            'logo_url': TEAM_LOGO.format(id=away_id),
        },
        'kickoff_utc': parse_kickoff(match_data),
        'kickoff_display': str(match_data.get('time') or ''),
        'category': status.category,
        'status_code': status.status_code,
        'status_reason_short': status.reason_short[:32],
        'status_reason_long': status.reason_long[:255],
        'home_score': home_score,
        'away_score': away_score,
        'home_red_cards': None if is_upcoming else _as_int(home.get('redCards')),
        'away_red_cards': None if is_upcoming else _as_int(away.get('redCards')),
        'duration_minutes': status.duration_minutes,
        'minute_elapsed': status.minute_elapsed,
        'minute_display': status.minute_display[:16],
        'first_half_started_utc': parse_half_start(match_data, 'firstHalfStarted'),
        'second_half_started_utc': parse_half_start(match_data, 'secondHalfStarted'),
        'raw': match_data,
    }


def iter_fixtures(payload: dict, queried_date: date) -> Iterator[tuple[dict | None, dict]]:
    """Yield ``(normalized_or_None, raw_match)`` for every match in a day payload."""
    for league_entry in payload.get('leagues') or []:
        if not isinstance(league_entry, dict):
            continue
        matches = league_entry.get('matches')
        if not isinstance(matches, list):
            continue
        for raw_match in matches:
            if not isinstance(raw_match, dict):
                continue
            yield normalize_fixture(raw_match, league_entry, queried_date), raw_match


# -- coverage normalization -------------------------------------------------


def _event_dedupe_key(event: dict, index: int) -> str:
    upstream = event.get('eventId') or event.get('id')
    if upstream:
        return f'id:{upstream}'
    parts = [
        str(event.get('type') or event.get('eventType') or ''),
        str(event.get('time') or event.get('minute') or ''),
        str(event.get('overloadTime') or event.get('timeAddedStr') or ''),
        str((event.get('player') or {}).get('name') if isinstance(event.get('player'), dict)
            else event.get('nameStr') or event.get('player') or ''),
        str(index),
    ]
    return 'k:' + '|'.join(parts)


def _normalize_event(event: dict, index: int) -> dict:
    player = event.get('player')
    player_name = ''
    player_id = None
    if isinstance(player, dict):
        player_name = player.get('name') or ''
        player_id = _as_int(player.get('id'))
    elif isinstance(player, str):
        player_name = player
    player_name = player_name or event.get('nameStr') or event.get('fullName') or ''

    assist = event.get('assistStr') or ''
    if not assist and isinstance(event.get('assistInput'), str):
        assist = event['assistInput']

    # Substitutions carry no `player`: `swap` is [player off, player on].
    swap = event.get('swap')
    if not player_name and isinstance(swap, list) and swap:
        off = swap[0] if isinstance(swap[0], dict) else {}
        on = swap[1] if len(swap) > 1 and isinstance(swap[1], dict) else {}
        player_name = on.get('name') or off.get('name') or ''
        player_id = _as_int(on.get('id')) or _as_int(off.get('id'))
        if not assist and off.get('name'):
            # Reuse the assist column for the player coming off.
            assist = f"off: {off['name']}"

    event_type = str(event.get('type') or event.get('eventType') or '').strip()
    is_goal = event_type.lower() in {'goal', 'owngoal', 'penaltygoal'} or bool(
        event.get('isGoal'),
    )

    new_score = event.get('newScore')
    score_home = score_away = None
    if isinstance(new_score, (list, tuple)) and len(new_score) == 2:
        score_home, score_away = _as_int(new_score[0]), _as_int(new_score[1])

    return {
        'upstream_event_id': str(event.get('eventId') or event.get('id') or '')[:64],
        'dedupe_key': _event_dedupe_key(event, index)[:128],
        'event_type': (event_type or 'unknown')[:32],
        'is_goal': is_goal,
        'is_own_goal': bool(event.get('ownGoal')),
        'is_home': event.get('isHome') if isinstance(event.get('isHome'), bool) else None,
        'player_name': str(player_name)[:255],
        'fotmob_player_id': player_id,
        'assist_player_name': str(assist)[:255],
        'minute': _as_int(event.get('time') or event.get('minute')),
        'added_time': _as_int(
            event.get('overloadTime') or event.get('minutesAddedInput')
            or event.get('timeAdded'),
        ),
        'sequence': index,
        'score_home': score_home,
        'score_away': score_away,
        'details': event if isinstance(event, dict) else {},
    }


def _flatten_header_events(node: Any) -> list[dict]:
    """``header.events`` groups goals and red cards by player name, not time."""
    rows: list[dict] = []
    if not isinstance(node, dict):
        return rows
    for group in node.values():
        if isinstance(group, dict):
            for entries in group.values():
                if isinstance(entries, list):
                    rows += [e for e in entries if isinstance(e, dict)]
        elif isinstance(group, list):
            rows += [e for e in group if isinstance(e, dict)]
    return rows


def _collect_events(details: dict, ticker: dict | None) -> list[dict]:
    """Find the match timeline, preferring the full chronological source.

    FotMob keeps it at ``content.matchFacts.events.events``; ``header.events``
    only holds goals and red cards grouped by player, and the ticker is the
    last resort.
    """
    content = details.get('content') or {}
    match_facts = content.get('matchFacts') or {}
    raw_events: list = []
    shootout: list = []

    facts_events = match_facts.get('events')
    if isinstance(facts_events, dict):
        raw_events = facts_events.get('events') or []
        shootout = facts_events.get('penaltyShootoutEvents') or []
    elif isinstance(facts_events, list):
        raw_events = facts_events

    if not raw_events:
        header_events = (details.get('header') or {}).get('events')
        if isinstance(header_events, list):
            raw_events = header_events
        elif isinstance(header_events, dict):
            raw_events = (
                header_events.get('events')
                if isinstance(header_events.get('events'), list)
                else _flatten_header_events(header_events)
            )
    if not raw_events and isinstance(content.get('events'), list):
        raw_events = content['events']
    if not raw_events and isinstance(ticker, dict):
        raw_events = ticker.get('events') or []

    if not isinstance(raw_events, list):
        return []
    combined = [item for item in raw_events if isinstance(item, dict)]
    combined += [item for item in (shootout or []) if isinstance(item, dict)]
    return [_normalize_event(item, index) for index, item in enumerate(combined)]


def _normalize_team_stats(details: dict) -> list[dict]:
    """Flatten team statistics into ``(group, key, period, home, away)`` rows."""
    content = details.get('content') or {}
    stats_root = content.get('stats') or {}
    rows: list[dict] = []
    seen: set[tuple] = set()

    def visit(node: Any, group: str) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item, group)
            return
        if not isinstance(node, dict):
            return

        title = node.get('title') or node.get('key') or ''
        inner = node.get('stats')
        if isinstance(inner, list):
            visit(inner, str(title) or group)
        for key in ('Periods', 'periods'):
            periods = node.get(key)
            if isinstance(periods, dict):
                for period_name, period_node in periods.items():
                    visit(period_node, f'{group}/{period_name}' if group else str(period_name))

        values = node.get('stats')
        key_name = node.get('key') or node.get('title')
        if (
            isinstance(values, (list, tuple))
            and len(values) == 2
            and not any(isinstance(v, (dict, list)) for v in values)
            and key_name
        ):
            period = StatPeriod.ALL
            lowered = group.lower()
            if '1st half' in lowered or 'first half' in lowered:
                period = StatPeriod.FIRST_HALF
            elif '2nd half' in lowered or 'second half' in lowered:
                period = StatPeriod.SECOND_HALF
            elif 'extra' in lowered:
                period = StatPeriod.EXTRA_TIME
            identity = (str(key_name), period)
            if identity in seen:
                return
            seen.add(identity)
            rows.append({
                'stat_group': str(group)[:64],
                'stat_key': str(key_name)[:128],
                'period': period,
                'home_raw': '' if values[0] is None else str(values[0])[:64],
                'away_raw': '' if values[1] is None else str(values[1])[:64],
            })

    visit(stats_root, '')
    return rows


#: FotMob's ``usualPlayingPositionId`` is a line index, not an exact position.
POSITION_LINES = {0: 'GK', 1: 'DEF', 2: 'MID', 3: 'ATT'}


def _player_stats_index(details: dict) -> dict[int, dict]:
    """Index ``content.playerStats`` by FotMob player ID."""
    raw = (details.get('content') or {}).get('playerStats') or {}
    if not isinstance(raw, dict):
        return {}
    index: dict[int, dict] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        player_id = _as_int(entry.get('id')) or _as_int(key)
        if player_id:
            index[player_id] = entry
    return index


def _iter_player_stat_items(entry: dict) -> Iterator[tuple[str, str, dict]]:
    """Yield ``(key, title, stat)`` for one player's stat groups."""
    for group in entry.get('stats') or []:
        if not isinstance(group, dict):
            continue
        items = group.get('stats')
        if not isinstance(items, dict):
            continue
        for title, item in items.items():
            if not isinstance(item, dict):
                continue
            # A missing `key` marks a UI flag such as "Shotmap", not a statistic.
            key = item.get('key')
            stat = item.get('stat')
            if key and isinstance(stat, dict):
                yield str(key), str(title), stat


def _stat_value(stat: dict) -> tuple[str, float | None]:
    """Flatten one stat value, keeping fractions readable as ``23/32``."""
    value = stat.get('value')
    if value is None:
        return '', None
    total = stat.get('total')
    if total is not None:
        return f'{value}/{total}', _as_float(value)
    if isinstance(value, bool):
        return ('true' if value else 'false'), float(value)
    return str(value), _as_float(value)


def _player_minutes(entry: dict) -> int | None:
    for key, _title, stat in _iter_player_stat_items(entry):
        if key == 'minutes_played':
            return _as_int(stat.get('value'))
    return None


def _normalize_player_stats(details: dict, index: dict[int, dict] | None = None) -> list[dict]:
    """Per-player match statistics from ``content.playerStats``.

    Only players the source actually reported stats for are returned, so an
    unused bench player does not become a row of zeros.
    """
    index = _player_stats_index(details) if index is None else index
    rows: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for player_id, entry in index.items():
        name = str(entry.get('name') or '')[:255]
        if not name:
            continue
        team_id = _as_int(entry.get('teamId'))
        for key, title, stat in _iter_player_stat_items(entry):
            raw_value, numeric = _stat_value(stat)
            if raw_value == '':
                continue
            identity = (player_id, key)
            if identity in seen:
                continue
            seen.add(identity)
            rows.append({
                'player_fotmob_id': player_id,
                'player_name': name,
                'team_fotmob_id': team_id,
                'stat_key': key[:128],
                'stat_title': title[:128],
                'raw_value': raw_value[:64],
                'numeric_value': numeric,
                'is_goalkeeper': bool(entry.get('isGoalkeeper')),
            })
    return rows


def _normalize_lineups(details: dict, stats_index: dict[int, dict] | None = None) -> list[dict]:
    content = details.get('content') or {}
    lineup_root = content.get('lineup') or content.get('lineups') or {}
    if isinstance(lineup_root, list):
        teams = [item for item in lineup_root if isinstance(item, dict)]
    else:
        teams = [
            team for team in (
                lineup_root.get('homeTeam'), lineup_root.get('awayTeam'),
            ) if isinstance(team, dict)
        ]
        if not teams and isinstance(lineup_root.get('lineup'), list):
            teams = [item for item in lineup_root['lineup'] if isinstance(item, dict)]

    normalized: list[dict] = []
    for index, team in enumerate(teams):
        players: list[dict] = []

        def add_player(entry: Any, is_starter: bool) -> None:
            if isinstance(entry, list):
                for item in entry:
                    add_player(item, is_starter)
                return
            if not isinstance(entry, dict):
                return
            name = entry.get('name')
            if isinstance(name, dict):
                name = name.get('fullName') or name.get('firstName')
            if not name:
                return

            # FotMob keeps the match rating under `performance`; older and
            # alternative shapes put it on `rating` directly.
            performance = entry.get('performance')
            performance = performance if isinstance(performance, dict) else {}
            rating = _as_float(performance.get('rating'))
            if rating is None:
                rating_node = entry.get('rating')
                rating = (
                    _as_float(rating_node.get('num'))
                    if isinstance(rating_node, dict) else _as_float(rating_node)
                )

            player_id = _as_int(entry.get('id'))
            minutes = _as_int(entry.get('minutesPlayed'))
            if minutes is None and player_id in (stats_index or {}):
                minutes = _player_minutes(stats_index[player_id])

            position = entry.get('positionStringShort') or entry.get('role') or ''
            if not position:
                position = POSITION_LINES.get(
                    _as_int(entry.get('usualPlayingPositionId')), '',
                )

            players.append({
                'name': str(name)[:255],
                'shirt_number': str(entry.get('shirtNumber') or entry.get('shirt') or '')[:8],
                'position': str(position)[:64],
                'country_code': str(entry.get('countryCode') or '')[:16],
                'is_starter': is_starter,
                'is_captain': bool(entry.get('isCaptain')),
                'is_player_of_the_match': bool(performance.get('playerOfTheMatch')),
                'rating': rating,
                'minutes_played': minutes,
                'upstream_player_id': str(entry.get('id') or '')[:64],
                'fotmob_player_id': player_id,
            })

        starters = team.get('starters') or team.get('startingLineup')
        if starters is None:
            starters = team.get('players')
        add_player(starters, True)
        add_player(team.get('subs') or team.get('bench') or [], False)

        coach = team.get('coach') or team.get('coaches')
        coach_name = ''
        if isinstance(coach, dict):
            coach_name = str(coach.get('name') or '')
        elif isinstance(coach, list) and coach and isinstance(coach[0], dict):
            coach_name = str(coach[0].get('name') or '')

        normalized.append({
            'team_fotmob_id': _as_int(team.get('id') or team.get('teamId')),
            'is_home': bool(team.get('isHome')) if isinstance(team.get('isHome'), bool) else index == 0,
            'formation': str(team.get('formation') or '')[:32],
            'coach_name': coach_name[:255],
            'players': players,
        })
    return [item for item in normalized if item['players']]


def _normalize_shots(details: dict) -> list[dict]:
    content = details.get('content') or {}
    shotmap = content.get('shotmap') or {}
    shots = shotmap.get('shots') if isinstance(shotmap, dict) else shotmap
    if not isinstance(shots, list):
        return []
    rows = []
    for index, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        upstream = str(shot.get('id') or '')
        rows.append({
            'upstream_shot_id': upstream[:64],
            'dedupe_key': (upstream or f'k:{index}:{shot.get("min")}:{shot.get("playerName")}')[:128],
            'player_name': str(shot.get('playerName') or '')[:255],
            'is_home': shot.get('isHome') if isinstance(shot.get('isHome'), bool) else None,
            'team_fotmob_id': _as_int(shot.get('teamId')),
            'minute': _as_int(shot.get('min')),
            'x': _as_float(shot.get('x')),
            'y': _as_float(shot.get('y')),
            'expected_goals': _as_float(shot.get('expectedGoals')),
            'expected_goals_on_target': _as_float(shot.get('expectedGoalsOnTarget')),
            'situation': str(shot.get('situation') or '')[:64],
            'shot_type': str(shot.get('shotType') or '')[:64],
            'event_type': str(shot.get('eventType') or '')[:64],
            'is_on_target': shot.get('isOnTarget') if isinstance(shot.get('isOnTarget'), bool) else None,
            'is_blocked': shot.get('isBlocked') if isinstance(shot.get('isBlocked'), bool) else None,
        })
    return rows


def _normalize_momentum(details: dict) -> list[dict]:
    content = details.get('content') or {}
    match_facts = content.get('matchFacts') or {}
    momentum = match_facts.get('momentum') or content.get('momentum') or {}
    points = momentum.get('main', {}).get('data') if isinstance(momentum, dict) else None
    if points is None and isinstance(momentum, dict):
        points = momentum.get('data')
    if not isinstance(points, list):
        return []
    rows = []
    for point in points:
        if not isinstance(point, dict):
            continue
        minute = _as_int(point.get('minute') or point.get('min'))
        value = _as_float(point.get('value'))
        if minute is None or value is None:
            continue
        rows.append({'minute': minute, 'value': value})
    return rows


def _normalize_commentary(ticker: dict | None) -> list[dict]:
    """Return real commentary entries only; metadata alone counts as empty."""
    if not isinstance(ticker, dict):
        return []
    candidates = ticker.get('commentary') or ticker.get('Commentary') or []
    if isinstance(candidates, dict):
        candidates = candidates.get('commentary') or []
    if not isinstance(candidates, list):
        return []
    rows = []
    for index, entry in enumerate(candidates):
        if not isinstance(entry, dict):
            continue
        text = entry.get('commentary') or entry.get('text') or entry.get('comment')
        if not text:
            continue
        minute = str(entry.get('time') or entry.get('min') or '')[:16]
        rows.append({
            'dedupe_key': (str(entry.get('id') or '') or f'k:{index}:{minute}')[:128],
            'minute': minute,
            'minute_sort': _digits_sum(minute),
            'text': str(text),
            'entry_type': str(entry.get('type') or '')[:64],
        })
    return rows


def normalize_coverage(details: dict, ticker: dict | None = None) -> dict:
    """Normalize matchDetails (+ optional ticker) into per-feature payloads.

    Every feature gets its own state, so an HTTP 200 with no lineups is
    reported as ``empty`` rather than ``available``.
    """
    general = details.get('general') or {}
    header = details.get('header') or {}
    coverage_level = str(
        general.get('coverageLevel')
        or (details.get('content') or {}).get('coverageLevel')
        or '',
    )[:32]

    stats_index = _player_stats_index(details)
    events = _collect_events(details, ticker)
    lineups = _normalize_lineups(details, stats_index)
    player_stats = _normalize_player_stats(details, stats_index)
    team_stats = _normalize_team_stats(details)
    shots = _normalize_shots(details)
    momentum = _normalize_momentum(details)
    commentary = _normalize_commentary(ticker)
    goals = [event for event in events if event['is_goal']]

    ticker_offered = bool(((details.get('content') or {}).get('liveticker') or {}).get('ltcUrl'))

    features = {
        CoverageFeatureChoices.EVENTS: _feature_state(events),
        CoverageFeatureChoices.GOALS: _feature_state(goals),
        CoverageFeatureChoices.LINEUPS: _feature_state(lineups),
        CoverageFeatureChoices.PLAYER_STATS: _feature_state(player_stats),
        CoverageFeatureChoices.STATS: _feature_state(team_stats),
        CoverageFeatureChoices.SHOTS: _feature_state(shots),
        CoverageFeatureChoices.MOMENTUM: _feature_state(momentum),
        CoverageFeatureChoices.COMMENTARY: (
            _feature_state(commentary) if ticker_offered
            else (CoverageState.UNAVAILABLE, 0)
        ),
    }

    return {
        'source_coverage_level': coverage_level,
        'features': features,
        'events': events,
        'lineups': lineups,
        'player_stats': player_stats,
        'team_stats': team_stats,
        'shots': shots,
        'momentum': momentum,
        'commentary': commentary,
        'header': header,
    }


def _feature_state(rows: Sequence) -> tuple[str, int]:
    count = len(rows)
    return (CoverageState.AVAILABLE if count else CoverageState.EMPTY), count


# ---------------------------------------------------------------------------
# 3. Upserts and ingestion jobs
# ---------------------------------------------------------------------------


@dataclass
class RunCounters:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    invalid: int = 0
    partial: int = 0
    coverage_fetched: int = 0
    coverage_rich: int = 0
    live_closed: int = 0
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        data = {
            'created': self.created,
            'updated': self.updated,
            'unchanged': self.unchanged,
            'skipped': self.skipped,
            'invalid': self.invalid,
            'partial': self.partial,
            'coverage_fetched': self.coverage_fetched,
            'coverage_rich': self.coverage_rich,
            'live_closed': self.live_closed,
        }
        data.update(self.extra)
        return data


class RunRecorder:
    """Creates and finalizes an :class:`IngestionRun` with its error rows."""

    def __init__(self, job: str, params: dict, trigger: str = IngestionTrigger.CRON,
                 dry_run: bool = False):
        self.counters = RunCounters()
        self.run = IngestionRun.objects.create(
            job=job,
            trigger=trigger,
            params=_jsonable(params),
            correlation_id=uuid.uuid4().hex[:40],
            dry_run=dry_run,
        )
        self._errors: list[IngestionError] = []

    @property
    def correlation_id(self) -> str:
        return self.run.correlation_id

    def record_error(self, message: str, category: str = IngestionError.Category.UNKNOWN,
                     scope: str = '', external_ref: str = '', payload: Any = None) -> None:
        self._errors.append(IngestionError(
            run=self.run,
            category=category,
            scope=scope[:64],
            external_ref=str(external_ref)[:64],
            message=str(message),
            payload=_jsonable(payload) if payload is not None else None,
        ))
        logger.warning('[%s] %s %s: %s', self.run.job, scope, external_ref, message)

    def finish(self, client: FotMobClient | None = None, state: str | None = None,
               notes: str = '') -> IngestionRun:
        if self._errors:
            IngestionError.objects.bulk_create(self._errors, batch_size=200)
        run = self.run
        run.finished_at = timezone.now()
        run.counts = self.counters.as_dict()
        run.notes = notes
        if client is not None:
            run.provider_requests = client.request_count
            run.provider_errors = client.error_count
        if state is not None:
            run.state = state
        elif not self._errors:
            run.state = IngestionState.SUCCESS
        elif self.counters.created or self.counters.updated or self.counters.unchanged:
            run.state = IngestionState.PARTIAL
        else:
            run.state = IngestionState.FAILED
        run.save(update_fields=[
            'finished_at', 'counts', 'notes', 'state',
            'provider_requests', 'provider_errors',
        ])
        return run


def _jsonable(value: Any) -> Any:
    """Make params/payloads JSON-serializable for the audit tables."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@contextmanager
def ingestion_run(job: str, params: dict, trigger: str = IngestionTrigger.CRON,
                  dry_run: bool = False) -> Iterator[RunRecorder]:
    recorder = RunRecorder(job, params, trigger=trigger, dry_run=dry_run)
    try:
        yield recorder
    except Exception as exc:  # noqa: BLE001 - the run must still be recorded
        recorder.record_error(str(exc), IngestionError.Category.UNKNOWN, scope='run')
        recorder.finish(state=IngestionState.FAILED)
        raise
    else:
        if not recorder.run.finished_at:
            recorder.finish()


def upsert_league(data: dict) -> League | None:
    fotmob_id = data.get('fotmob_id')
    if not fotmob_id:
        return None
    league, _ = League.objects.get_or_create(
        fotmob_id=fotmob_id,
        defaults={
            'name': (data.get('name') or f'League {fotmob_id}')[:255],
            'country_code': (data.get('country_code') or '')[:16],
            'parent_fotmob_id': data.get('parent_fotmob_id'),
            'logo_url': data.get('logo_url') or '',
        },
    )
    changes = {}
    if data.get('name') and league.name != data['name'][:255]:
        changes['name'] = data['name'][:255]
    if data.get('country_code') and league.country_code != data['country_code'][:16]:
        changes['country_code'] = data['country_code'][:16]
    if data.get('parent_fotmob_id') and league.parent_fotmob_id != data['parent_fotmob_id']:
        changes['parent_fotmob_id'] = data['parent_fotmob_id']
    if data.get('logo_url') and not league.logo_url:
        changes['logo_url'] = data['logo_url']
    changes['last_seen_at'] = timezone.now()
    for key, value in changes.items():
        setattr(league, key, value)
    league.save(update_fields=list(changes) + ['updated_at'])
    return league


def upsert_team(data: dict) -> Team | None:
    fotmob_id = data.get('fotmob_id')
    if not fotmob_id:
        return None
    team, _ = Team.objects.get_or_create(
        fotmob_id=fotmob_id,
        defaults={
            'name': (data.get('name') or f'Team {fotmob_id}')[:255],
            'short_name': (data.get('short_name') or '')[:128],
            'logo_url': data.get('logo_url') or '',
        },
    )
    changes: dict[str, Any] = {}
    if data.get('name') and team.name != data['name'][:255]:
        changes['name'] = data['name'][:255]
    if data.get('short_name') and team.short_name != data['short_name'][:128]:
        changes['short_name'] = data['short_name'][:128]
    if data.get('logo_url') and not team.logo_url:
        changes['logo_url'] = data['logo_url']
    changes['last_seen_at'] = timezone.now()
    for key, value in changes.items():
        setattr(team, key, value)
    team.save(update_fields=list(changes) + ['updated_at'])
    return team


def upsert_season(league: League, year_name: str) -> Season | None:
    year_name = str(year_name or '').strip()[:32]
    if not league or not year_name:
        return None
    season, _ = Season.objects.get_or_create(league=league, year_name=year_name)
    return season


def _overridden_fields(match: Match) -> set[str]:
    return {
        override.field_name
        for override in match.overrides.all()
        if override.is_effective()
    }


#: Provider fields that must never be regressed to an empty value.
_STICKY_FIELDS = (
    'home_score', 'away_score', 'home_red_cards', 'away_red_cards',
    'duration_minutes', 'first_half_started_utc', 'second_half_started_utc',
)


def _should_apply_status(existing: Match, incoming_category: str) -> bool:
    """Guard against a stale snapshot reopening a completed match.

    A later window may still report an older state for the same fixture; a
    completed or disrupted match is only reopened when the provider says the
    match is in progress again (a genuine correction), never by a plain
    'not started' snapshot.
    """
    if existing.category in (MatchCategory.COMPLETED, MatchCategory.DISRUPTED):
        return incoming_category in (MatchCategory.COMPLETED, MatchCategory.DISRUPTED,
                                     MatchCategory.LIVE)
    return True


@transaction.atomic
def upsert_match(fixture: dict, store_raw: bool = False) -> tuple[Match, str]:
    """Idempotently upsert one normalized fixture.

    Returns ``(match, outcome)`` where outcome is ``created``, ``updated`` or
    ``unchanged``. Never writes a null over a known score and never overwrites
    a field that has an active admin override.
    """
    league = upsert_league(fixture['league'])
    home = upsert_team(fixture['home'])
    away = upsert_team(fixture['away'])
    if league is None or home is None or away is None:
        raise FotMobError('missing_reference_entities', IngestionError.Category.VALIDATION)
    season = upsert_season(league, fixture.get('season_year'))

    now = timezone.now()
    defaults = {
        'league': league,
        'season': season,
        'home_team': home,
        'away_team': away,
        'source': SourceChoices.FOTMOB,
        'provider_league_id': fixture.get('provider_league_id'),
        'provider_primary_league_id': fixture.get('provider_primary_league_id'),
        'round_name': fixture.get('round_name', ''),
        'kickoff_utc': fixture.get('kickoff_utc'),
        'kickoff_display': fixture.get('kickoff_display', ''),
        'category': fixture['category'],
        'status_code': fixture['status_code'],
        'status_reason_short': fixture.get('status_reason_short', ''),
        'status_reason_long': fixture.get('status_reason_long', ''),
        'home_score': fixture.get('home_score'),
        'away_score': fixture.get('away_score'),
        'home_red_cards': fixture.get('home_red_cards'),
        'away_red_cards': fixture.get('away_red_cards'),
        'duration_minutes': fixture.get('duration_minutes'),
        'first_half_started_utc': fixture.get('first_half_started_utc'),
        'second_half_started_utc': fixture.get('second_half_started_utc'),
        'last_seen_at': now,
    }

    match = (
        Match.objects.select_for_update()
        .filter(fotmob_id=fixture['fotmob_id'])
        .prefetch_related('overrides')
        .first()
    )
    if match is None:
        match = Match.objects.create(fotmob_id=fixture['fotmob_id'], **defaults)
        _sync_live_projection(match, fixture)
        if store_raw:
            _store_raw(match, fixture_payload=fixture.get('raw'))
        return match, 'created'

    protected = _overridden_fields(match)
    apply_status = _should_apply_status(match, fixture['category'])
    changed: list[str] = []

    for field_name, value in defaults.items():
        if field_name in protected or field_name == 'last_seen_at':
            continue
        if field_name in ('category', 'status_code', 'status_reason_short',
                          'status_reason_long') and not apply_status:
            continue
        if field_name in _STICKY_FIELDS and value is None:
            # A failed or partial secondary call must not erase known data.
            continue
        if getattr(match, field_name) != value:
            setattr(match, field_name, value)
            changed.append(field_name)

    match.last_seen_at = now
    match.save(update_fields=changed + ['last_seen_at', 'updated_at'])
    _sync_live_projection(match, fixture)
    if store_raw:
        _store_raw(match, fixture_payload=fixture.get('raw'))
    return match, ('updated' if changed else 'unchanged')


def _store_raw(match: Match, fixture_payload: Any = None, details_payload: Any = None) -> None:
    snapshot, _ = MatchRawSnapshot.objects.get_or_create(match=match)
    fields = ['fetched_at']
    if fixture_payload is not None:
        snapshot.fixture_payload = _jsonable(fixture_payload)
        fields.append('fixture_payload')
    if details_payload is not None:
        snapshot.details_payload = _jsonable(details_payload)
        fields.append('details_payload')
    snapshot.fetched_at = timezone.now()
    snapshot.save(update_fields=fields + ['updated_at'])


def _sync_live_projection(match: Match, fixture: dict) -> bool:
    """Create/refresh the LiveMatch row, or expire it once the match ends.

    Returns True when a projection was expired.
    """
    if match.category == MatchCategory.LIVE:
        LiveMatch.objects.update_or_create(
            match=match,
            defaults={
                'minute_elapsed': fixture.get('minute_elapsed'),
                'minute_display': fixture.get('minute_display', ''),
                'home_score': fixture.get('home_score'),
                'away_score': fixture.get('away_score'),
                'home_red_cards': fixture.get('home_red_cards'),
                'away_red_cards': fixture.get('away_red_cards'),
                'status_code': match.status_code,
                'last_source_fetch_at': timezone.now(),
            },
        )
        return False

    # Only expire when the provider itself confirmed a terminal state.
    if match.category in (MatchCategory.COMPLETED, MatchCategory.DISRUPTED):
        deleted, _ = LiveMatch.objects.filter(match=match).delete()
        return bool(deleted)
    return False


@transaction.atomic
def persist_coverage(match: Match, normalized: dict, store_raw: bool = False,
                     raw_details: Any = None) -> dict:
    """Write coverage children, preserving last-known-good on empty features.

    Returns a summary dict with per-feature states and whether the match has
    substantial data (``is_rich``).
    """
    coverage, _ = MatchCoverage.objects.get_or_create(match=match)
    now = timezone.now()
    if normalized.get('source_coverage_level'):
        coverage.source_coverage_level = normalized['source_coverage_level']
    coverage.last_fetch_at = now
    coverage.last_success_at = now
    coverage.last_error = ''
    coverage.save(update_fields=[
        'source_coverage_level', 'last_fetch_at', 'last_success_at',
        'last_error', 'updated_at',
    ])

    team_by_side = {True: match.home_team, False: match.away_team}

    # Events -----------------------------------------------------------------
    events = normalized.get('events') or []
    if events:
        for row in events:
            team = team_by_side.get(row.get('is_home'))
            MatchEvent.objects.update_or_create(
                match=match,
                dedupe_key=row['dedupe_key'],
                defaults={
                    'upstream_event_id': row['upstream_event_id'],
                    'event_type': row['event_type'],
                    'is_goal': row['is_goal'],
                    'is_home': row['is_home'],
                    'team': team,
                    'player': _player_for({
                        'fotmob_player_id': row.get('fotmob_player_id'),
                        'name': row['player_name'],
                    }),
                    'player_name': row['player_name'],
                    'assist_player_name': row['assist_player_name'],
                    'minute': row['minute'],
                    'added_time': row['added_time'],
                    'sequence': row['sequence'],
                    'score_home': row['score_home'],
                    'score_away': row['score_away'],
                    'details': _jsonable(row['details']),
                },
            )

    # Lineups ----------------------------------------------------------------
    for lineup_data in normalized.get('lineups') or []:
        team = None
        if lineup_data.get('team_fotmob_id'):
            team = Team.objects.filter(fotmob_id=lineup_data['team_fotmob_id']).first()
        if team is None:
            team = team_by_side.get(lineup_data.get('is_home'))
        if team is None:
            continue
        lineup, _ = MatchLineup.objects.update_or_create(
            match=match, team=team,
            defaults={
                'is_home': bool(lineup_data.get('is_home')),
                'formation': lineup_data.get('formation', ''),
                'coach_name': lineup_data.get('coach_name', ''),
            },
        )
        lineup.players.all().delete()
        rows = []
        for entry in lineup_data['players']:
            player = _player_for(entry)
            rows.append(LineupPlayer(
                lineup=lineup,
                player=player,
                name=entry['name'],
                shirt_number=entry['shirt_number'],
                position=entry['position'],
                is_starter=entry['is_starter'],
                is_captain=entry['is_captain'],
                rating=entry['rating'],
                minutes_played=entry['minutes_played'],
                upstream_player_id=entry['upstream_player_id'],
            ))
            # Appearing in a team's lineup is what makes a squad link known.
            if player is not None:
                TeamPlayer.objects.update_or_create(
                    team=team, player=player,
                    defaults={'shirt_number': entry['shirt_number'], 'is_active': True},
                )
        LineupPlayer.objects.bulk_create(rows, batch_size=200)

    # Per-player statistics --------------------------------------------------
    for row in normalized.get('player_stats') or []:
        team = None
        if row.get('team_fotmob_id'):
            team = Team.objects.filter(fotmob_id=row['team_fotmob_id']).first()
        player = _player_for({
            'fotmob_player_id': row['player_fotmob_id'],
            'name': row['player_name'],
            'position': 'GK' if row.get('is_goalkeeper') else '',
        })
        PlayerMatchStat.objects.update_or_create(
            match=match,
            player_name=row['player_name'],
            stat_key=row['stat_key'],
            period=StatPeriod.ALL,
            defaults={
                'team': team,
                'player': player,
                'raw_value': row['raw_value'],
                'numeric_value': row['numeric_value'],
            },
        )

    # Team statistics --------------------------------------------------------
    for row in normalized.get('team_stats') or []:
        for team, raw_value in ((match.home_team, row['home_raw']),
                                (match.away_team, row['away_raw'])):
            TeamMatchStat.objects.update_or_create(
                match=match, team=team, stat_key=row['stat_key'], period=row['period'],
                defaults={
                    'stat_group': row['stat_group'],
                    'raw_value': raw_value,
                    'numeric_value': _numeric_from_raw(raw_value),
                },
            )

    # Shots ------------------------------------------------------------------
    for row in normalized.get('shots') or []:
        team = None
        if row.get('team_fotmob_id'):
            team = Team.objects.filter(fotmob_id=row['team_fotmob_id']).first()
        if team is None:
            team = team_by_side.get(row.get('is_home'))
        MatchShot.objects.update_or_create(
            match=match, dedupe_key=row['dedupe_key'],
            defaults={
                'team': team,
                'upstream_shot_id': row['upstream_shot_id'],
                'player_name': row['player_name'],
                'is_home': row['is_home'],
                'minute': row['minute'],
                'x': row['x'],
                'y': row['y'],
                'expected_goals': row['expected_goals'],
                'expected_goals_on_target': row['expected_goals_on_target'],
                'situation': row['situation'],
                'shot_type': row['shot_type'],
                'event_type': row['event_type'],
                'is_on_target': row['is_on_target'],
                'is_blocked': row['is_blocked'],
            },
        )

    # Momentum ---------------------------------------------------------------
    for row in normalized.get('momentum') or []:
        MatchMomentum.objects.update_or_create(
            match=match, minute=row['minute'], defaults={'value': row['value']},
        )

    # Commentary -------------------------------------------------------------
    for row in normalized.get('commentary') or []:
        MatchCommentary.objects.update_or_create(
            match=match, dedupe_key=row['dedupe_key'],
            defaults={
                'minute': row['minute'],
                'minute_sort': row['minute_sort'],
                'text': row['text'],
                'entry_type': row['entry_type'],
            },
        )

    # Per-feature availability ----------------------------------------------
    summary = {}
    for feature, (state, count) in (normalized.get('features') or {}).items():
        existing = CoverageFeature.objects.filter(coverage=coverage, feature=feature).first()
        if (
            existing is not None
            and state in (CoverageState.EMPTY, CoverageState.FAILED)
            and existing.state == CoverageState.AVAILABLE
        ):
            # Keep last-known-good; record only that the fetch happened.
            existing.fetched_at = now
            existing.note = f'kept last-known-good; latest fetch returned {state}'[:255]
            existing.save(update_fields=['fetched_at', 'note', 'updated_at'])
            summary[feature] = existing.state
            continue
        CoverageFeature.objects.update_or_create(
            coverage=coverage, feature=feature,
            defaults={'state': state, 'entry_count': count, 'fetched_at': now, 'note': ''},
        )
        summary[feature] = state

    is_rich = any(
        summary.get(key) == CoverageState.AVAILABLE
        for key in (CoverageFeatureChoices.LINEUPS, CoverageFeatureChoices.STATS)
    )
    if store_raw and raw_details is not None:
        _store_raw(match, details_payload=raw_details)

    return {'features': summary, 'is_rich': is_rich,
            'coverage_level': coverage.source_coverage_level}


def _player_for(entry: dict) -> Player | None:
    """Get or create the Player row behind a lineup or stat entry.

    Players exist only because coverage ingestion saw them, so metadata is
    filled in opportunistically and never blanked by a later sparser payload.
    """
    fotmob_id = entry.get('fotmob_player_id')
    if not fotmob_id:
        return None
    player, created = Player.objects.get_or_create(
        fotmob_id=fotmob_id,
        defaults={
            'name': entry.get('name') or f'Player {fotmob_id}',
            'position': (entry.get('position') or '')[:64],
            'country_code': (entry.get('country_code') or '')[:16],
        },
    )
    if created:
        return player

    changes = {}
    if entry.get('name') and player.name != entry['name'][:255]:
        changes['name'] = entry['name'][:255]
    if entry.get('position') and not player.position:
        changes['position'] = entry['position'][:64]
    if entry.get('country_code') and not player.country_code:
        changes['country_code'] = entry['country_code'][:16]
    if changes:
        for field_name, value in changes.items():
            setattr(player, field_name, value)
        player.save(update_fields=list(changes) + ['updated_at'])
    return player


_NUMERIC_RE = re.compile(r'-?\d+(?:\.\d+)?')


def _numeric_from_raw(raw: str) -> float | None:
    match = _NUMERIC_RE.search(raw or '')
    return float(match.group()) if match else None


def record_coverage_failure(match: Match, message: str) -> None:
    """Mark a failed fetch without destroying previously good coverage."""
    coverage, _ = MatchCoverage.objects.get_or_create(match=match)
    coverage.last_fetch_at = timezone.now()
    coverage.last_error = str(message)[:255]
    coverage.save(update_fields=['last_fetch_at', 'last_error', 'updated_at'])


# -- job entry points -------------------------------------------------------


def date_window(past_days: int, future_days: int, today: date | None = None) -> list[date]:
    base = today or timezone.now().date()
    return [
        base + timedelta(days=offset)
        for offset in range(-abs(past_days), abs(future_days) + 1)
    ]


def ingest_fixtures(
    days: Sequence[date],
    job: str = 'sync_fotmob_fixtures',
    trigger: str = IngestionTrigger.CRON,
    league_ids: set[int] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    store_raw: bool = False,
    client: FotMobClient | None = None,
) -> IngestionRun:
    """Fetch the daily fixture list for each date and upsert every match.

    Rerunning the same window is idempotent: matches are keyed on FotMob ID.
    """
    owns_client = client is None
    client = client or FotMobClient()
    params = {'days': [d.isoformat() for d in days], 'league_ids': sorted(league_ids or []),
              'limit': limit, 'dry_run': dry_run}
    processed = 0

    with ingestion_run(job, params, trigger=trigger, dry_run=dry_run) as recorder:
        try:
            for day in days:
                try:
                    payload = client.fetch_matches_for_date(day)
                except FotMobError as exc:
                    recorder.record_error(str(exc), exc.category, scope='matches',
                                          external_ref=day.isoformat())
                    continue

                for fixture, raw_match in iter_fixtures(payload, day):
                    if fixture is None:
                        recorder.counters.invalid += 1
                        recorder.record_error(
                            'unusable_fixture_record', IngestionError.Category.VALIDATION,
                            scope='fixture', external_ref=str((raw_match or {}).get('id') or ''),
                            payload=raw_match,
                        )
                        continue

                    if league_ids:
                        candidates = {
                            fixture['provider_league_id'],
                            fixture['provider_primary_league_id'],
                            fixture['league']['fotmob_id'],
                        }
                        if not candidates & league_ids:
                            recorder.counters.skipped += 1
                            continue

                    if dry_run:
                        exists = Match.objects.filter(fotmob_id=fixture['fotmob_id']).exists()
                        if exists:
                            recorder.counters.unchanged += 1
                        else:
                            recorder.counters.created += 1
                    else:
                        try:
                            _, outcome = upsert_match(fixture, store_raw=store_raw)
                        except FotMobError as exc:
                            recorder.counters.invalid += 1
                            recorder.record_error(str(exc), exc.category, scope='upsert',
                                                  external_ref=fixture['fotmob_id'],
                                                  payload=raw_match)
                            continue
                        except Exception as exc:  # noqa: BLE001
                            recorder.counters.invalid += 1
                            recorder.record_error(str(exc), IngestionError.Category.DATABASE,
                                                  scope='upsert',
                                                  external_ref=fixture['fotmob_id'],
                                                  payload=raw_match)
                            continue
                        setattr(recorder.counters, outcome,
                                getattr(recorder.counters, outcome) + 1)

                    processed += 1
                    if limit is not None and processed >= limit:
                        break
                if limit is not None and processed >= limit:
                    break
        finally:
            if owns_client:
                client.close()
        return recorder.finish(client=client)


def ingest_live(
    days: Sequence[date] | None = None,
    trigger: str = IngestionTrigger.CRON,
    dry_run: bool = False,
    client: FotMobClient | None = None,
) -> IngestionRun:
    """Refresh scores/statuses for today and yesterday, then close stale rows.

    Final scores are preserved: the fixture upsert never writes a null over a
    known value, and a LiveMatch row is only removed when the provider reports
    a terminal state for that match.
    """
    days = list(days or date_window(1, 0))
    owns_client = client is None
    client = client or FotMobClient()
    params = {'days': [d.isoformat() for d in days], 'dry_run': dry_run}

    with ingestion_run('sync_fotmob_live', params, trigger=trigger, dry_run=dry_run) as recorder:
        touched_ids: set[int] = set()
        try:
            for day in days:
                try:
                    payload = client.fetch_matches_for_date(day)
                except FotMobError as exc:
                    recorder.record_error(str(exc), exc.category, scope='matches',
                                          external_ref=day.isoformat())
                    continue
                for fixture, raw_match in iter_fixtures(payload, day):
                    if fixture is None:
                        recorder.counters.invalid += 1
                        continue
                    touched_ids.add(fixture['fotmob_id'])
                    if dry_run:
                        recorder.counters.unchanged += 1
                        continue
                    try:
                        _, outcome = upsert_match(fixture)
                    except Exception as exc:  # noqa: BLE001
                        recorder.counters.invalid += 1
                        recorder.record_error(str(exc), IngestionError.Category.DATABASE,
                                              scope='live_upsert',
                                              external_ref=fixture['fotmob_id'],
                                              payload=raw_match)
                        continue
                    setattr(recorder.counters, outcome,
                            getattr(recorder.counters, outcome) + 1)

            # Stale live rows: verify each one individually before closing it.
            stale_threshold = settings.FOTMOB.get('LIVE_STALE_MINUTES', 15)
            cutoff = timezone.now() - timedelta(minutes=stale_threshold)
            stale = LiveMatch.objects.filter(
                updated_at__lt=cutoff,
            ).exclude(match__fotmob_id__in=touched_ids).select_related('match')
            for projection in stale:
                recorder.counters.extra.setdefault('stale_live_seen', 0)
                recorder.counters.extra['stale_live_seen'] += 1
                if dry_run:
                    continue
                closed = _verify_and_close_live(projection, client, recorder)
                if closed:
                    recorder.counters.live_closed += 1
        finally:
            if owns_client:
                client.close()
        return recorder.finish(client=client)


def _verify_and_close_live(projection: LiveMatch, client: FotMobClient,
                           recorder: RunRecorder) -> bool:
    """Close a stale LiveMatch only after the provider confirms it ended."""
    match = projection.match
    try:
        details = client.fetch_match_details(match.fotmob_id)
    except FotMobError as exc:
        recorder.record_error(f'stale_live_unverified: {exc}', exc.category,
                              scope='live_verify', external_ref=match.fotmob_id)
        return False

    header_status = ((details.get('header') or {}).get('status') or {})
    result = classify_status(header_status)
    if result.category in (MatchCategory.COMPLETED, MatchCategory.DISRUPTED):
        match.category = result.category
        match.status_code = result.status_code
        match.status_reason_short = result.reason_short[:32]
        match.status_reason_long = result.reason_long[:255]
        if result.duration_minutes:
            match.duration_minutes = result.duration_minutes
        match.save(update_fields=[
            'category', 'status_code', 'status_reason_short',
            'status_reason_long', 'duration_minutes', 'updated_at',
        ])
        projection.delete()
        return True
    return False


def coverage_priority_matches(limit: int = 50, days_back: int = 1,
                              include_incomplete: bool = True) -> QuerySet:
    """Matches worth a matchDetails call: in-progress first, then gaps."""
    now = timezone.now()
    window_start = now - timedelta(days=abs(days_back))
    base = Match.objects.visible().filter(kickoff_utc__gte=window_start,
                                          kickoff_utc__lte=now + timedelta(hours=6))
    live_q = Q(category=MatchCategory.LIVE)
    if include_incomplete:
        incomplete = Q(category=MatchCategory.COMPLETED) & (
            Q(coverage__isnull=True) | Q(coverage__last_success_at__isnull=True)
            | Q(coverage__last_success_at__lt=now - timedelta(hours=1))
        )
        base = base.filter(live_q | incomplete)
    else:
        base = base.filter(live_q)
    return base.select_related('home_team', 'away_team').order_by(
        '-category', 'kickoff_utc',
    )[:limit]


def ingest_coverage(
    matches: Iterable[Match] | None = None,
    details_for: str = 'live',
    days_back: int = 1,
    limit: int = 50,
    trigger: str = IngestionTrigger.CRON,
    dry_run: bool = False,
    store_raw: bool = False,
    client: FotMobClient | None = None,
) -> IngestionRun:
    """Fetch matchDetails for selected matches and persist per-feature coverage."""
    owns_client = client is None
    client = client or FotMobClient()
    if matches is None:
        matches = coverage_priority_matches(
            limit=limit, days_back=days_back,
            include_incomplete=details_for == 'all',
        )
    params = {'details_for': details_for, 'days_back': days_back, 'limit': limit,
              'dry_run': dry_run}

    with ingestion_run('sync_fotmob_coverage', params, trigger=trigger,
                       dry_run=dry_run) as recorder:
        try:
            for match in matches:
                if dry_run:
                    recorder.counters.skipped += 1
                    continue
                try:
                    details = client.fetch_match_details(match.fotmob_id)
                except FotMobError as exc:
                    recorder.counters.partial += 1
                    recorder.record_error(str(exc), exc.category, scope='matchDetails',
                                          external_ref=match.fotmob_id)
                    record_coverage_failure(match, str(exc))
                    continue

                recorder.counters.coverage_fetched += 1
                ticker = client.fetch_live_ticker(
                    details, [match.home_team.name, match.away_team.name],
                )
                normalized = normalize_coverage(details, ticker)
                try:
                    summary = persist_coverage(match, normalized, store_raw=store_raw,
                                               raw_details=details if store_raw else None)
                except Exception as exc:  # noqa: BLE001
                    recorder.counters.invalid += 1
                    recorder.record_error(str(exc), IngestionError.Category.DATABASE,
                                          scope='persist_coverage',
                                          external_ref=match.fotmob_id)
                    continue
                recorder.counters.updated += 1
                if summary['is_rich']:
                    recorder.counters.coverage_rich += 1
        finally:
            if owns_client:
                client.close()
        return recorder.finish(client=client)


#: Stale `running` rows (from a crashed process) stop blocking after this.
RUNNING_LOCK_MINUTES = 120


def job_in_progress(job: str) -> bool:
    """True when the same write job is already running, ignoring stale rows."""
    cutoff = timezone.now() - timedelta(minutes=RUNNING_LOCK_MINUTES)
    return IngestionRun.objects.filter(
        job=job, state=IngestionState.RUNNING, started_at__gte=cutoff,
    ).exists()


def _days_from_params(params: dict, default_past: int, default_future: int) -> list[date]:
    start, end = params.get('start_date'), params.get('end_date')
    if start and end:
        return [start + timedelta(days=offset)
                for offset in range((end - start).days + 1)]
    return date_window(
        params.get('past_days', default_past),
        params.get('future_days', default_future),
    )


def run_job(job: str, params: dict, trigger: str = IngestionTrigger.API) -> IngestionRun:
    """Execute one ingestion job now and return its run record.

    Shared by the API dispatcher; the management commands call the individual
    ingest functions directly. Fetching still never happens inside a response:
    the API hands this to a worker thread and answers 202.
    """
    params = dict(params or {})
    dry_run = params.get('dry_run', True)

    with FotMobClient() as client:
        if job == 'sync_fotmob_fixtures':
            return ingest_fixtures(
                _days_from_params(params, 1, 3),
                trigger=trigger,
                league_ids=set(params['league_ids']) if params.get('league_ids') else None,
                limit=params.get('limit'),
                dry_run=dry_run,
                store_raw=params.get('store_raw', False),
                client=client,
            )
        if job == 'sync_fotmob_live':
            return ingest_live(
                days=date_window(params.get('days_back', 1), 0),
                trigger=trigger, dry_run=dry_run, client=client,
            )
        if job == 'sync_fotmob_coverage':
            matches = None
            if params.get('match_ids'):
                matches = list(
                    Match.objects.visible()
                    .select_related('home_team', 'away_team')
                    .filter(fotmob_id__in=params['match_ids'])
                )
            return ingest_coverage(
                matches=matches,
                details_for=params.get('details_for', 'live'),
                days_back=params.get('days_back', 1),
                limit=params.get('limit', 50),
                trigger=trigger,
                dry_run=dry_run,
                store_raw=params.get('store_raw', False),
                client=client,
            )
        if job == 'daily_fixture_reconcile':
            return reconcile_fixtures(
                past_days=params.get('past_days', 30),
                future_days=params.get('future_days', 30),
                trigger=trigger, dry_run=dry_run, client=client,
            )
        if job == 'weekly_backfill':
            return ingest_fixtures(
                _days_from_params(params, 7, 0),
                job='weekly_backfill', trigger=trigger, dry_run=dry_run, client=client,
            )
    raise ValueError(f'Unknown ingestion job {job!r}')


def _job_thread(job: str, params: dict, trigger: str) -> None:
    from django.db import connections

    try:
        run = run_job(job, params, trigger=trigger)
        logger.info('%s finished: state=%s counts=%s', job, run.state, run.counts)
    except Exception:  # noqa: BLE001 - a worker thread must not die silently
        logger.exception('%s failed', job)
    finally:
        connections.close_all()


def dispatch_job(job: str, params: dict, trigger: str = IngestionTrigger.API) -> str:
    """Start a job in a worker thread and return the thread name.

    Deliberately not a response-path fetch: the caller gets 202 immediately and
    reads progress from ``/api/internal/sync-runs/``. For production volumes,
    replace this with a real queue worker; cron does not use this path at all.
    """
    import threading

    thread = threading.Thread(
        target=_job_thread, args=(job, dict(params or {}), trigger),
        name=f'ingest-{job}', daemon=True,
    )
    thread.start()
    return thread.name


def reconcile_fixtures(
    past_days: int = 30,
    future_days: int = 30,
    trigger: str = IngestionTrigger.CRON,
    dry_run: bool = False,
    client: FotMobClient | None = None,
) -> IngestionRun:
    """Broad-window reconciliation for reschedules and disruptions.

    An unobserved match is never deleted or cancelled: it is only flagged by
    leaving ``last_seen_at`` untouched, which the sync-run report exposes.
    """
    days = date_window(past_days, future_days)
    return ingest_fixtures(
        days, job='daily_fixture_reconcile', trigger=trigger, dry_run=dry_run,
        client=client,
    )


# ---------------------------------------------------------------------------
# 4. Query services for the read API
# ---------------------------------------------------------------------------

#: Whitelisted ordering fields. Arbitrary ORM names are never accepted.
ORDERING_FIELDS = {
    'kickoff_utc': ('kickoff_utc', 'id'),
    '-kickoff_utc': ('-kickoff_utc', '-id'),
    'id': ('id',),
    '-id': ('-id',),
    'league': ('league__name', 'kickoff_utc', 'id'),
    '-league': ('-league__name', '-kickoff_utc', '-id'),
}

CATEGORY_ALIASES = {
    'live': MatchCategory.LIVE,
    'completed': MatchCategory.COMPLETED,
    'upcoming': MatchCategory.UPCOMING,
    'cancelled': MatchCategory.DISRUPTED,
    'disrupted': MatchCategory.DISRUPTED,
}


def base_match_queryset() -> QuerySet:
    return Match.objects.visible().with_relations()


def build_match_queryset(filters: dict) -> QuerySet:
    """Apply validated filters to the match queryset.

    ``filters`` comes from ``MatchFilterSerializer.validated_data``, so values
    are already type-checked and bounded.
    """
    queryset = base_match_queryset()

    category = filters.get('status')
    if category:
        queryset = queryset.filter(category=CATEGORY_ALIASES.get(category, category))

    status_code = filters.get('status_code')
    if status_code:
        queryset = queryset.filter(status_code=status_code)

    if filters.get('league_id'):
        league_id = filters['league_id']
        queryset = queryset.filter(
            Q(league__fotmob_id=league_id)
            | Q(provider_league_id=league_id)
            | Q(provider_primary_league_id=league_id),
        )

    if filters.get('team_id'):
        team_id = filters['team_id']
        queryset = queryset.filter(
            Q(home_team__fotmob_id=team_id) | Q(away_team__fotmob_id=team_id),
        )

    if filters.get('season_id'):
        queryset = queryset.filter(season_id=filters['season_id'])

    day = filters.get('date')
    if day:
        queryset = queryset.filter(kickoff_utc__date=day)

    if filters.get('date_from'):
        queryset = queryset.filter(
            kickoff_utc__gte=datetime.combine(
                filters['date_from'], datetime.min.time(), tzinfo=dt_timezone.utc,
            ),
        )
    if filters.get('date_to'):
        queryset = queryset.filter(
            kickoff_utc__lt=datetime.combine(
                filters['date_to'] + timedelta(days=1), datetime.min.time(),
                tzinfo=dt_timezone.utc,
            ),
        )

    search = (filters.get('search') or '').strip()
    if search:
        queryset = queryset.filter(
            Q(home_team__name__icontains=search)
            | Q(away_team__name__icontains=search)
            | Q(league__name__icontains=search),
        )

    ordering = filters.get('ordering') or 'kickoff_utc'
    return queryset.order_by(*ORDERING_FIELDS.get(ordering, ORDERING_FIELDS['kickoff_utc']))


def live_match_queryset() -> QuerySet:
    return base_match_queryset().filter(category=MatchCategory.LIVE).order_by(
        'kickoff_utc', 'id',
    )


def get_match(fotmob_id: int) -> Match | None:
    return base_match_queryset().filter(fotmob_id=fotmob_id).first()


def match_data_updated_at(match: Match) -> datetime:
    """Freshest local timestamp for this match, used for ``data_updated_at``."""
    candidates = [match.updated_at]
    live = getattr(match, 'live', None)
    if live is not None:
        candidates.append(live.updated_at)
    coverage = getattr(match, 'coverage', None)
    if coverage is not None and coverage.last_success_at:
        candidates.append(coverage.last_success_at)
    return max(candidate for candidate in candidates if candidate)


def coverage_for(match: Match) -> MatchCoverage | None:
    return MatchCoverage.objects.filter(match=match).prefetch_related('features').first()


def feature_payload(match: Match, feature: str, rows: Sequence | None,
                    serialized: Any = None) -> dict:
    """Wrap a coverage feature in the documented availability envelope."""
    coverage = coverage_for(match)
    state = CoverageState.NOT_FETCHED
    fetched_at = None
    entry_count = 0
    if coverage is not None:
        entry = next(
            (item for item in coverage.features.all() if item.feature == feature), None,
        )
        if entry is not None:
            state = entry.state
            fetched_at = entry.fetched_at
            entry_count = entry.entry_count
    return {
        'available': state == CoverageState.AVAILABLE,
        'state': state,
        'fetched_at': fetched_at,
        'source_coverage_level': coverage.source_coverage_level if coverage else '',
        'entry_count': entry_count if entry_count else (len(rows) if rows else 0),
        'data': serialized if serialized is not None else [],
    }


def write_audit(actor, action: str, instance, changes: dict | None = None,
                reason: str = '', request_id: str = '') -> AuditLog:
    """Record an administrative write. Called by every mutating APIView."""
    return AuditLog.objects.create(
        actor=actor if getattr(actor, 'is_authenticated', False) else None,
        action=action,
        model_name=instance.__class__.__name__,
        object_id=str(getattr(instance, 'pk', '')),
        changes=_jsonable(changes or {}),
        reason=reason,
        request_id=request_id,
    )


def sync_run_queryset() -> QuerySet:
    return IngestionRun.objects.order_by('-started_at', '-id').prefetch_related('errors')


def ingestion_health() -> list[dict]:
    """Last success / last error per job, for the internal status endpoint."""
    report = []
    for job, label in IngestionRun._meta.get_field('job').choices:
        runs = IngestionRun.objects.filter(job=job)
        last = runs.first()
        last_success = runs.filter(state=IngestionState.SUCCESS).first()
        report.append({
            'job': job,
            'label': label,
            'last_run_at': last.started_at if last else None,
            'last_state': last.state if last else None,
            'last_success_at': last_success.started_at if last_success else None,
            'last_counts': last.counts if last else {},
            'provider_errors': last.provider_errors if last else 0,
        })
    return report


def stale_live_count(threshold_minutes: int | None = None) -> int:
    threshold = threshold_minutes or settings.FOTMOB.get('LIVE_STALE_MINUTES', 15)
    cutoff = timezone.now() - timedelta(minutes=threshold)
    return LiveMatch.objects.filter(updated_at__lt=cutoff).count()
