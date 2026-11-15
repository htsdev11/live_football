"""API and ingestion tests covering the section 9.2 test matrix.

No test performs a real FotMob request: the HTTP client is replaced by a fake
that returns recorded payload shapes, which is also how the verification gates
in section 8.10 are meant to run in CI.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from api import service
from core.models import (
    CoverageFeature,
    CoverageFeatureChoices,
    CoverageState,
    IngestionRun,
    IngestionState,
    IngestionTrigger,
    League,
    LineupPlayer,
    LiveMatch,
    Match,
    MatchCategory,
    MatchCoverage,
    MatchEvent,
    MatchOverride,
    MatchStatusCode,
    Player,
    PlayerMatchStat,
    Season,
    SourceChoices,
    Team,
    TeamPlayer,
)

User = get_user_model()


# ---------------------------------------------------------------------------
# Payload builders (shapes taken from the two tested scrapers' exports)
# ---------------------------------------------------------------------------


def fixture_payload(
    match_id: int = 6469292,
    *,
    started: bool = False,
    finished: bool = False,
    cancelled: bool = False,
    reason_short: str = '',
    reason_long: str = '',
    live_short: str = '',
    live_long_key: str = '',
    home_score=None,
    away_score=None,
    league_id: int = 132,
    primary_id: int = 132,
    utc_time: str = '2026-11-09T19:30:00.000Z',
) -> dict:
    """One day payload containing a single match."""
    status: dict = {'utcTime': utc_time}
    if started:
        status['started'] = True
    if finished:
        status['finished'] = True
    if cancelled:
        status['cancelled'] = True
    if reason_short or reason_long:
        status['reason'] = {'short': reason_short, 'long': reason_long}
    if live_short or live_long_key:
        status['liveTime'] = {'short': live_short, 'longKey': live_long_key,
                              'long': live_long_key}
    return {
        'leagues': [{
            'id': league_id,
            'primaryId': primary_id,
            'name': 'FA Cup',
            'ccode': 'ENG',
            'season': '2026/2027',
            'matches': [{
                'id': match_id,
                'time': '09.11.2026 19:30',
                'tournamentStage': 'First Round',
                'home': {'id': 674304, 'name': 'Plymouth Parkway', 'score': home_score},
                'away': {'id': 879829, 'name': 'Berkhamsted', 'score': away_score},
                'status': status,
            }],
        }],
    }


def details_payload(*, with_lineups: bool = True, with_stats: bool = True,
                    with_momentum: bool = True, with_ticker: bool = False,
                    with_player_stats: bool = True,
                    coverage_level: str = 'xG') -> dict:
    """A matchDetails payload in FotMob's real shape.

    Verified against a live response (LaLiga match 5868121, coverage level xG):
    the timeline lives at ``content.matchFacts.events.events``, ratings at
    ``performance.rating``, positions as numeric ``usualPlayingPositionId``
    line indexes, and per-player stats in ``content.playerStats``.
    """
    content: dict = {
        'matchFacts': {
            'momentum': {'main': {'data': [
                {'minute': 1, 'value': 12.5}, {'minute': 2, 'value': -8.0},
            ]}} if with_momentum else {},
            'events': {
                'ongoing': False,
                'penaltyShootoutEvents': None,
                'events': [
                    {'eventId': 14722034, 'type': 'Goal', 'time': 23,
                     'overloadTime': None, 'isHome': True,
                     'player': {'id': 5001, 'name': 'A Striker'},
                     'newScore': [1, 0], 'assistStr': 'B Winger'},
                    {'eventId': 14722230, 'type': 'Card', 'time': 67,
                     'overloadTime': None, 'isHome': False, 'card': 'Yellow',
                     'player': {'id': 6001, 'name': 'B Defender'}},
                    {'type': 'Substitution', 'time': 46, 'overloadTime': 0,
                     'isHome': False, 'player': {'id': None},
                     'swap': [{'name': 'Bench Two', 'id': '6002'},
                              {'name': 'Keeper Two', 'id': '6001'}]},
                ],
            },
        },
        'shotmap': {'shots': [{
            'id': 991, 'playerName': 'A Striker', 'isHome': True, 'min': 23,
            'x': 88.2, 'y': 40.1, 'expectedGoals': 0.31, 'situation': 'OpenPlay',
            'eventType': 'Goal', 'isOnTarget': True, 'teamId': 674304,
        }]},
    }
    if with_lineups:
        content['lineup'] = {
            'matchId': 6469292,
            'lineupType': 'standard',
            'homeTeam': {
                'id': 674304, 'name': 'Plymouth Parkway', 'formation': '4-4-2',
                'coach': {'id': 2642, 'name': 'Home Coach'},
                'starters': [{
                    'id': 5001, 'name': 'Keeper One', 'shirtNumber': '1',
                    'countryCode': 'ENG', 'usualPlayingPositionId': 0,
                    'positionId': 11, 'isCaptain': True,
                    'performance': {'rating': 7.2, 'playerOfTheMatch': True},
                }],
                'subs': [{
                    'id': 5002, 'name': 'Bench One', 'shirtNumber': '14',
                    'countryCode': 'ENG', 'usualPlayingPositionId': 3,
                    'performance': {'rating': 6.1},
                }],
            },
            'awayTeam': {
                'id': 879829, 'name': 'Berkhamsted', 'formation': '4-3-3',
                'coach': {'id': 99, 'name': 'Away Coach'},
                'starters': [{
                    'id': 6001, 'name': 'Keeper Two', 'shirtNumber': '1',
                    'countryCode': 'WAL', 'usualPlayingPositionId': 0,
                }],
                'subs': [],
            },
        }
    if with_player_stats:
        content['playerStats'] = {
            '5001': {
                'id': 5001, 'name': 'Keeper One', 'teamId': 674304,
                'teamName': 'Plymouth Parkway', 'isGoalkeeper': True,
                'shirtNumber': '1', 'usualPosition': 0, 'shotmap': [],
                'stats': [{
                    'title': 'Top stats', 'key': 'top_stats',
                    'stats': {
                        'FotMob rating': {'key': 'rating_title',
                                          'stat': {'value': 7.2, 'type': 'double'}},
                        'Minutes played': {'key': 'minutes_played',
                                           'stat': {'value': 90, 'type': 'integer'}},
                        'Saves': {'key': 'saves',
                                  'stat': {'value': 3, 'type': 'integer'}},
                        'Accurate passes': {
                            'key': 'accurate_passes',
                            'stat': {'value': 23, 'total': 32,
                                     'type': 'fractionWithPercentage'},
                        },
                        'Shotmap': {'key': None,
                                    'stat': {'value': 1, 'type': 'boolean'}},
                    },
                }],
            },
            '6001': {
                'id': 6001, 'name': 'Keeper Two', 'teamId': 879829,
                'isGoalkeeper': True, 'stats': [],
            },
        }
    if with_stats:
        content['stats'] = {'Periods': {'All': {'stats': [{
            'title': 'Top stats',
            'stats': [
                {'key': 'expected_goals', 'title': 'Expected goals (xG)',
                 'stats': ['1.42', '0.63']},
                {'key': 'total_shots', 'title': 'Total shots', 'stats': [14, 7]},
            ],
        }]}}}
    if with_ticker:
        content['liveticker'] = {
            'ltcUrl': 'https://data.fotmob.com/webcl/ltc/gsm/6469292_en.json.gz',
            'teams': ['Plymouth Parkway', 'Berkhamsted'],
        }
    return {
        'general': {'coverageLevel': coverage_level},
        'header': {
            'status': {'finished': True, 'reason': {'short': 'AET',
                                                    'long': 'After Extra Time'}},
            # The real header groups goals and red cards by player surname and
            # is NOT the timeline; the collector must prefer matchFacts.
            'events': {
                'homeTeamGoals': {'Striker': [
                    {'eventId': 14722034, 'type': 'Goal', 'time': 23,
                     'isHome': True, 'player': {'id': 5001, 'name': 'A Striker'}},
                ]},
                'awayTeamGoals': {},
                'homeTeamRedCards': {},
                'awayTeamRedCards': {},
            },
        },
        'content': content,
    }


def ticker_payload(with_entries: bool = True) -> dict:
    if not with_entries:
        # The shape FotMob returns with metadata only: no real commentary.
        return {'matchId': 6469292, 'teams': ['Plymouth Parkway', 'Berkhamsted']}
    return {
        'commentary': [
            {'id': 'c1', 'time': "90+3'", 'commentary': 'Full time whistle.'},
            {'id': 'c2', 'time': "23'", 'commentary': 'Goal for the hosts.'},
        ],
    }


class FakeClient:
    """Stands in for :class:`api.service.FotMobClient` without any network use."""

    def __init__(self, day_payloads=None, details=None, ticker=None, fail_dates=None):
        self.day_payloads = day_payloads or {}
        self.details = details
        self.ticker = ticker
        self.fail_dates = fail_dates or set()
        self.request_count = 0
        self.error_count = 0
        self.detail_calls: list[int] = []

    def fetch_matches_for_date(self, day: date) -> dict:
        self.request_count += 1
        if day in self.fail_dates:
            self.error_count += 1
            raise service.FotMobError('http_503')
        return self.day_payloads.get(day, {'leagues': []})

    def fetch_match_details(self, fotmob_id: int) -> dict:
        self.request_count += 1
        self.detail_calls.append(fotmob_id)
        if self.details is None:
            self.error_count += 1
            raise service.FotMobError('match_details_unavailable')
        return self.details

    def fetch_live_ticker(self, details, teams):
        if not ((details.get('content') or {}).get('liveticker') or {}).get('ltcUrl'):
            return None
        return self.ticker

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


# ---------------------------------------------------------------------------
# Normalization and classification
# ---------------------------------------------------------------------------


class StatusClassificationTests(TestCase):
    def test_not_started_has_no_score_or_minute(self):
        result = service.classify_status({'utcTime': '2026-11-09T19:30:00Z'})
        self.assertEqual(result.category, MatchCategory.UPCOMING)
        self.assertEqual(result.status_code, MatchStatusCode.NOT_STARTED)
        self.assertIsNone(result.minute_elapsed)

    def test_full_time_records_90_minute_duration(self):
        result = service.classify_status({
            'finished': True, 'started': True,
            'reason': {'short': 'FT', 'long': 'Full-Time'},
        })
        self.assertEqual(result.category, MatchCategory.COMPLETED)
        self.assertEqual(result.status_code, MatchStatusCode.FULL_TIME)
        self.assertEqual(result.duration_minutes, 90)
        self.assertIsNone(result.minute_elapsed)

    def test_after_extra_time_is_120_not_90(self):
        """The defect called out in section 3.2 must not reappear."""
        result = service.classify_status({
            'finished': True, 'started': True,
            'reason': {'short': 'AET', 'long': 'After Extra Time'},
        })
        self.assertEqual(result.status_code, MatchStatusCode.AFTER_EXTRA_TIME)
        self.assertEqual(result.duration_minutes, 120)
        self.assertIsNone(result.minute_elapsed)

    def test_penalties_distinguished_from_full_time(self):
        result = service.classify_status({
            'finished': True, 'reason': {'short': 'PEN', 'long': 'After Penalties'},
        })
        self.assertEqual(result.status_code, MatchStatusCode.AFTER_PENALTIES)

    def test_postponed_is_not_cancelled(self):
        postponed = service.classify_status({
            'cancelled': True, 'reason': {'short': 'Postp', 'long': 'Postponed'},
        })
        cancelled = service.classify_status({
            'cancelled': True, 'reason': {'short': 'Canc', 'long': 'Cancelled'},
        })
        self.assertEqual(postponed.status_code, MatchStatusCode.POSTPONED)
        self.assertEqual(cancelled.status_code, MatchStatusCode.CANCELLED)
        self.assertEqual(postponed.category, MatchCategory.DISRUPTED)

    def test_abandoned_and_interrupted_kept_apart(self):
        abandoned = service.classify_status({
            'started': True, 'liveTime': {'short': 'AB', 'longKey': 'abandoned'},
        })
        interrupted = service.classify_status({
            'started': True, 'liveTime': {'short': 'IR', 'longKey': 'interrupted'},
        })
        self.assertEqual(abandoned.status_code, MatchStatusCode.ABANDONED)
        self.assertEqual(interrupted.status_code, MatchStatusCode.INTERRUPTED)

    def test_stoppage_time_minute_is_summed(self):
        result = service.classify_status({
            'started': True, 'liveTime': {'short': "45+2'"},
        })
        self.assertEqual(result.category, MatchCategory.LIVE)
        self.assertEqual(result.minute_elapsed, 47)

    def test_half_time_reports_45(self):
        result = service.classify_status({'started': True, 'liveTime': {'short': 'HT'}})
        self.assertEqual(result.status_code, MatchStatusCode.HALF_TIME)
        self.assertEqual(result.minute_elapsed, 45)


class FixtureNormalizationTests(TestCase):
    def test_upcoming_match_keeps_null_scores(self):
        payload = fixture_payload(home_score=0, away_score=0)
        fixture, _ = next(iter(service.iter_fixtures(payload, date(2026, 11, 9))))
        self.assertEqual(fixture['category'], MatchCategory.UPCOMING)
        self.assertIsNone(fixture['home_score'])
        self.assertIsNone(fixture['away_score'])

    def test_completed_match_keeps_real_scores(self):
        payload = fixture_payload(finished=True, started=True, reason_short='FT',
                                  home_score=2, away_score=1)
        fixture, _ = next(iter(service.iter_fixtures(payload, date(2026, 11, 9))))
        self.assertEqual((fixture['home_score'], fixture['away_score']), (2, 1))

    def test_league_primary_id_is_preserved(self):
        payload = fixture_payload(league_id=9999, primary_id=132)
        fixture, _ = next(iter(service.iter_fixtures(payload, date(2026, 11, 9))))
        self.assertEqual(fixture['provider_league_id'], 9999)
        self.assertEqual(fixture['provider_primary_league_id'], 132)

    def test_record_without_identifiers_is_rejected(self):
        payload = {'leagues': [{'id': 1, 'name': 'X', 'matches': [
            {'home': {'name': 'A'}, 'away': {'name': 'B'}, 'status': {}},
        ]}]}
        fixture, raw = next(iter(service.iter_fixtures(payload, date(2026, 11, 9))))
        self.assertIsNone(fixture)
        self.assertIn('home', raw)

    def test_kickoff_is_timezone_aware_utc(self):
        payload = fixture_payload()
        fixture, _ = next(iter(service.iter_fixtures(payload, date(2026, 11, 9))))
        self.assertEqual(
            fixture['kickoff_utc'],
            datetime(2026, 11, 9, 19, 30, tzinfo=dt_timezone.utc),
        )


class CoverageNormalizationTests(TestCase):
    def test_xg_level_match_reports_all_features(self):
        normalized = service.normalize_coverage(
            details_payload(with_ticker=True), ticker_payload(True),
        )
        features = normalized['features']
        self.assertEqual(normalized['source_coverage_level'], 'xG')
        for key in (CoverageFeatureChoices.EVENTS, CoverageFeatureChoices.LINEUPS,
                    CoverageFeatureChoices.STATS, CoverageFeatureChoices.SHOTS,
                    CoverageFeatureChoices.MOMENTUM,
                    CoverageFeatureChoices.COMMENTARY):
            self.assertEqual(features[key][0], CoverageState.AVAILABLE, key)
        self.assertEqual(len(normalized['events']), 3)
        self.assertEqual(len(normalized['lineups']), 2)
        self.assertEqual(len(normalized['momentum']), 2)

    def test_lower_coverage_level_reports_empty_not_available(self):
        normalized = service.normalize_coverage(details_payload(
            with_lineups=False, with_stats=False, with_momentum=False,
            coverage_level='lower',
        ))
        features = normalized['features']
        self.assertEqual(features[CoverageFeatureChoices.LINEUPS][0], CoverageState.EMPTY)
        self.assertEqual(features[CoverageFeatureChoices.STATS][0], CoverageState.EMPTY)
        self.assertEqual(features[CoverageFeatureChoices.MOMENTUM][0], CoverageState.EMPTY)

    def test_commentary_unavailable_when_no_ticker_is_offered(self):
        normalized = service.normalize_coverage(details_payload(with_ticker=False), None)
        self.assertEqual(
            normalized['features'][CoverageFeatureChoices.COMMENTARY][0],
            CoverageState.UNAVAILABLE,
        )

    def test_metadata_only_ticker_is_not_commentary(self):
        """HTTP 200 on the ticker does not mean commentary entries exist."""
        normalized = service.normalize_coverage(
            details_payload(with_ticker=True), ticker_payload(False),
        )
        self.assertEqual(
            normalized['features'][CoverageFeatureChoices.COMMENTARY][0],
            CoverageState.EMPTY,
        )
        self.assertEqual(normalized['commentary'], [])

    def test_timeline_comes_from_match_facts_not_the_header(self):
        """`header.events` groups goals by player name and is not a timeline."""
        normalized = service.normalize_coverage(details_payload())
        types = [event['event_type'] for event in normalized['events']]
        self.assertEqual(types, ['Goal', 'Card', 'Substitution'])
        self.assertEqual([e['minute'] for e in normalized['events']], [23, 67, 46])

    def test_goal_event_carries_player_id_score_and_assist(self):
        goal = service.normalize_coverage(details_payload())['events'][0]
        self.assertTrue(goal['is_goal'])
        self.assertEqual(goal['player_name'], 'A Striker')
        self.assertEqual(goal['fotmob_player_id'], 5001)
        self.assertEqual(goal['assist_player_name'], 'B Winger')
        self.assertEqual((goal['score_home'], goal['score_away']), (1, 0))

    def test_substitution_uses_the_swap_pair(self):
        sub = service.normalize_coverage(details_payload())['events'][2]
        self.assertEqual(sub['event_type'], 'Substitution')
        self.assertEqual(sub['player_name'], 'Keeper Two')
        self.assertEqual(sub['fotmob_player_id'], 6001)
        self.assertIn('Bench Two', sub['assist_player_name'])

    def test_lineup_rating_is_read_from_performance(self):
        """The real payload nests the match rating under `performance`."""
        lineups = service.normalize_coverage(details_payload())['lineups']
        keeper = lineups[0]['players'][0]
        self.assertEqual(keeper['rating'], 7.2)
        self.assertTrue(keeper['is_captain'])
        self.assertTrue(keeper['is_player_of_the_match'])

    def test_numeric_position_id_becomes_a_line_label(self):
        lineups = service.normalize_coverage(details_payload())['lineups']
        home = lineups[0]['players']
        self.assertEqual(home[0]['position'], 'GK')
        self.assertEqual(home[1]['position'], 'ATT')
        self.assertEqual(home[0]['country_code'], 'ENG')

    def test_minutes_played_comes_from_player_stats(self):
        lineups = service.normalize_coverage(details_payload())['lineups']
        self.assertEqual(lineups[0]['players'][0]['minutes_played'], 90)

    def test_player_stats_are_flattened_with_fractions_kept(self):
        rows = service.normalize_coverage(details_payload())['player_stats']
        by_key = {row['stat_key']: row for row in rows}
        self.assertEqual(by_key['minutes_played']['raw_value'], '90')
        self.assertEqual(by_key['rating_title']['numeric_value'], 7.2)
        self.assertEqual(by_key['accurate_passes']['raw_value'], '23/32')
        self.assertEqual(by_key['accurate_passes']['numeric_value'], 23.0)
        self.assertTrue(by_key['saves']['is_goalkeeper'])
        self.assertEqual({row['player_fotmob_id'] for row in rows}, {5001})

    def test_player_stats_feature_is_reported(self):
        features = service.normalize_coverage(details_payload())['features']
        self.assertEqual(features[CoverageFeatureChoices.PLAYER_STATS][0],
                         CoverageState.AVAILABLE)
        empty = service.normalize_coverage(
            details_payload(with_player_stats=False),
        )['features']
        self.assertEqual(empty[CoverageFeatureChoices.PLAYER_STATS][0],
                         CoverageState.EMPTY)

    def test_team_stats_are_flattened_per_side(self):
        normalized = service.normalize_coverage(details_payload())
        keys = {row['stat_key'] for row in normalized['team_stats']}
        self.assertIn('expected_goals', keys)
        xg_row = next(r for r in normalized['team_stats']
                      if r['stat_key'] == 'expected_goals')
        self.assertEqual((xg_row['home_raw'], xg_row['away_raw']), ('1.42', '0.63'))


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


class FixtureIngestionTests(TestCase):
    def setUp(self):
        self.day = date(2026, 11, 9)

    def run_fixtures(self, payload, **kwargs):
        client = FakeClient({self.day: payload})
        run = service.ingest_fixtures([self.day], client=client, **kwargs)
        return run, client

    def test_first_run_creates_entities(self):
        run, _ = self.run_fixtures(fixture_payload())
        self.assertEqual(run.state, IngestionState.SUCCESS)
        self.assertEqual(run.counts['created'], 1)
        self.assertEqual(Match.objects.count(), 1)
        self.assertEqual(League.objects.count(), 1)
        self.assertEqual(Team.objects.count(), 2)
        self.assertTrue(Season.objects.filter(year_name='2026/2027').exists())

    def test_rerunning_same_window_creates_no_duplicates(self):
        self.run_fixtures(fixture_payload())
        run, _ = self.run_fixtures(fixture_payload())
        self.assertEqual(Match.objects.count(), 1)
        self.assertEqual(run.counts['created'], 0)
        self.assertEqual(run.counts['unchanged'], 1)

    def test_same_match_on_two_dates_is_deduplicated(self):
        other = date(2026, 11, 10)
        client = FakeClient({
            self.day: fixture_payload(),
            other: fixture_payload(),
        })
        service.ingest_fixtures([self.day, other], client=client)
        self.assertEqual(Match.objects.count(), 1)

    def test_score_correction_is_applied(self):
        self.run_fixtures(fixture_payload(finished=True, started=True,
                                          reason_short='FT', home_score=1, away_score=1))
        self.run_fixtures(fixture_payload(finished=True, started=True,
                                          reason_short='FT', home_score=2, away_score=1))
        match = Match.objects.get()
        self.assertEqual((match.home_score, match.away_score), (2, 1))

    def test_stale_snapshot_cannot_reopen_a_completed_match(self):
        self.run_fixtures(fixture_payload(finished=True, started=True,
                                          reason_short='AET', home_score=2, away_score=1))
        # A later pass over an older window still reports "not started".
        self.run_fixtures(fixture_payload())
        match = Match.objects.get()
        self.assertEqual(match.category, MatchCategory.COMPLETED)
        self.assertEqual(match.status_code, MatchStatusCode.AFTER_EXTRA_TIME)
        self.assertEqual(match.duration_minutes, 120)
        self.assertEqual((match.home_score, match.away_score), (2, 1))

    def test_null_score_never_overwrites_a_known_score(self):
        self.run_fixtures(fixture_payload(finished=True, started=True,
                                          reason_short='FT', home_score=3, away_score=0))
        self.run_fixtures(fixture_payload(finished=True, started=True,
                                          reason_short='FT'))
        match = Match.objects.get()
        self.assertEqual((match.home_score, match.away_score), (3, 0))

    def test_invalid_record_is_quarantined_not_dropped_silently(self):
        payload = {'leagues': [{'id': 1, 'name': 'X', 'matches': [
            {'home': {'name': 'A'}, 'away': {'name': 'B'}, 'status': {}},
        ]}]}
        run, _ = self.run_fixtures(payload)
        self.assertEqual(run.counts['invalid'], 1)
        error = run.errors.get()
        self.assertEqual(error.category, 'validation')
        self.assertIsNotNone(error.payload)

    def test_upstream_failure_is_recorded_and_data_preserved(self):
        self.run_fixtures(fixture_payload(finished=True, started=True,
                                          reason_short='FT', home_score=1, away_score=0))
        client = FakeClient({}, fail_dates={self.day})
        run = service.ingest_fixtures([self.day], client=client)
        self.assertEqual(run.state, IngestionState.FAILED)
        self.assertEqual(Match.objects.get().home_score, 1)

    def test_dry_run_writes_nothing(self):
        run, _ = self.run_fixtures(fixture_payload(), dry_run=True)
        self.assertEqual(Match.objects.count(), 0)
        self.assertEqual(run.counts['created'], 1)
        self.assertTrue(run.dry_run)

    def test_league_filter_skips_other_competitions(self):
        run, _ = self.run_fixtures(fixture_payload(), league_ids={47})
        self.assertEqual(Match.objects.count(), 0)
        self.assertEqual(run.counts['skipped'], 1)

    def test_active_override_is_not_overwritten_by_provider(self):
        self.run_fixtures(fixture_payload())
        match = Match.objects.get()
        corrected = datetime(2026, 11, 9, 21, 0, tzinfo=dt_timezone.utc)
        match.kickoff_utc = corrected
        match.save(update_fields=['kickoff_utc'])
        MatchOverride.objects.create(
            match=match, field_name='kickoff_utc', value=corrected.isoformat(),
            reason='Club confirmed a later start.',
        )
        self.run_fixtures(fixture_payload())
        self.assertEqual(Match.objects.get().kickoff_utc, corrected)


class LiveIngestionTests(TestCase):
    def setUp(self):
        self.day = timezone.now().date()

    def test_live_match_gets_projection(self):
        client = FakeClient({self.day: fixture_payload(
            started=True, live_short="67'", home_score=1, away_score=0,
            utc_time=timezone.now().isoformat(),
        )})
        service.ingest_live([self.day], client=client)
        projection = LiveMatch.objects.get()
        self.assertEqual(projection.minute_elapsed, 67)
        self.assertEqual(projection.home_score, 1)
        self.assertFalse(projection.is_stale())

    def test_projection_expires_when_match_finishes(self):
        now = timezone.now().isoformat()
        service.ingest_live([self.day], client=FakeClient({self.day: fixture_payload(
            started=True, live_short="88'", home_score=1, away_score=0, utc_time=now,
        )}))
        self.assertEqual(LiveMatch.objects.count(), 1)
        service.ingest_live([self.day], client=FakeClient({self.day: fixture_payload(
            started=True, finished=True, reason_short='FT',
            home_score=1, away_score=0, utc_time=now,
        )}))
        self.assertEqual(LiveMatch.objects.count(), 0)
        match = Match.objects.get()
        self.assertEqual(match.category, MatchCategory.COMPLETED)
        self.assertEqual(match.home_score, 1)

    def test_stale_projection_is_only_closed_after_verification(self):
        now = timezone.now().isoformat()
        service.ingest_live([self.day], client=FakeClient({self.day: fixture_payload(
            started=True, live_short="30'", home_score=0, away_score=0, utc_time=now,
        )}))
        LiveMatch.objects.update(
            updated_at=timezone.now() - timedelta(hours=3),
        )
        # Fixture list no longer carries the match and details are unavailable.
        unverified = FakeClient({self.day: {'leagues': []}}, details=None)
        service.ingest_live([self.day], client=unverified)
        self.assertEqual(LiveMatch.objects.count(), 1)

        # Now the provider confirms a finished match.
        verified = FakeClient({self.day: {'leagues': []}}, details=details_payload())
        LiveMatch.objects.update(updated_at=timezone.now() - timedelta(hours=3))
        run = service.ingest_live([self.day], client=verified)
        self.assertEqual(LiveMatch.objects.count(), 0)
        self.assertEqual(run.counts['live_closed'], 1)


class CoverageIngestionTests(TestCase):
    def setUp(self):
        self.day = date(2026, 11, 9)
        service.ingest_fixtures(
            [self.day],
            client=FakeClient({self.day: fixture_payload(
                finished=True, started=True, reason_short='AET',
                home_score=2, away_score=1,
            )}),
        )
        self.match = Match.objects.get()

    def ingest(self, details, ticker=None):
        client = FakeClient({}, details=details, ticker=ticker)
        return service.ingest_coverage(matches=[self.match], client=client), client

    def test_coverage_persists_all_features(self):
        run, _ = self.ingest(details_payload(with_ticker=True), ticker_payload(True))
        self.assertEqual(run.counts['coverage_fetched'], 1)
        self.assertEqual(run.counts['coverage_rich'], 1)
        self.assertEqual(self.match.events.count(), 3)
        self.assertEqual(self.match.events.filter(is_goal=True).count(), 1)
        self.assertEqual(self.match.lineups.count(), 2)
        self.assertEqual(self.match.shots.count(), 1)
        self.assertEqual(self.match.momentum.count(), 2)
        self.assertEqual(self.match.commentary.count(), 2)
        self.assertEqual(self.match.team_stats.count(), 4)
        coverage = MatchCoverage.objects.get(match=self.match)
        self.assertEqual(coverage.source_coverage_level, 'xG')
        self.assertIsNotNone(coverage.last_success_at)

    def test_coverage_creates_player_rows_squad_links_and_stats(self):
        """Players exist only as a side effect of coverage ingestion."""
        self.assertEqual(Player.objects.count(), 0)
        self.ingest(details_payload())

        # One Player per distinct FotMob player id seen in lineups and events.
        # A substitution links the player coming on; the one going off is kept
        # in the assist column rather than linked, so 6002 is not a row.
        self.assertEqual(
            set(Player.objects.values_list('fotmob_id', flat=True)),
            {5001, 5002, 6001},
        )
        keeper = Player.objects.get(fotmob_id=5001)
        self.assertEqual(keeper.name, 'Keeper One')
        self.assertEqual(keeper.position, 'GK')
        self.assertEqual(keeper.country_code, 'ENG')

        entry = LineupPlayer.objects.get(player=keeper)
        self.assertEqual(float(entry.rating), 7.2)
        self.assertEqual(entry.minutes_played, 90)
        self.assertTrue(entry.is_starter)
        self.assertTrue(entry.is_captain)

        # Appearing in a lineup creates the squad link the teams endpoint reads.
        self.assertTrue(TeamPlayer.objects.filter(
            team=self.match.home_team, player=keeper, is_active=True,
        ).exists())

        stats = PlayerMatchStat.objects.filter(match=self.match, player=keeper)
        self.assertEqual(
            set(stats.values_list('stat_key', flat=True)),
            {'rating_title', 'minutes_played', 'saves', 'accurate_passes'},
        )
        self.assertEqual(stats.get(stat_key='accurate_passes').raw_value, '23/32')
        # Events link to their player too.
        self.assertEqual(
            self.match.events.get(is_goal=True).player.fotmob_id, 5001,
        )

    def test_rerunning_coverage_does_not_duplicate_player_rows(self):
        self.ingest(details_payload())
        self.ingest(details_payload())
        self.assertEqual(Player.objects.count(), 3)
        self.assertEqual(TeamPlayer.objects.count(), 3)
        self.assertEqual(
            PlayerMatchStat.objects.filter(match=self.match).count(), 4,
        )

    def test_player_metadata_is_not_blanked_by_a_sparser_payload(self):
        self.ingest(details_payload())
        sparse = details_payload()
        del sparse['content']['lineup']['homeTeam']['starters'][0]['countryCode']
        self.ingest(sparse)
        self.assertEqual(Player.objects.get(fotmob_id=5001).country_code, 'ENG')

    def test_fetch_success_is_not_the_same_as_rich_data(self):
        run, _ = self.ingest(details_payload(
            with_lineups=False, with_stats=False, with_momentum=False,
            coverage_level='lower',
        ))
        self.assertEqual(run.counts['coverage_fetched'], 1)
        self.assertEqual(run.counts['coverage_rich'], 0)

    def test_rerun_does_not_duplicate_events(self):
        self.ingest(details_payload())
        self.ingest(details_payload())
        self.assertEqual(MatchEvent.objects.filter(match=self.match).count(), 3)
        self.assertEqual(self.match.momentum.count(), 2)

    def test_empty_later_fetch_keeps_last_known_good(self):
        self.ingest(details_payload(with_lineups=True))
        self.ingest(details_payload(with_lineups=False))
        feature = CoverageFeature.objects.get(
            coverage__match=self.match, feature=CoverageFeatureChoices.LINEUPS,
        )
        self.assertEqual(feature.state, CoverageState.AVAILABLE)
        self.assertIn('last-known-good', feature.note)
        self.assertEqual(self.match.lineups.count(), 2)

    def test_failed_fetch_records_error_without_wiping_coverage(self):
        self.ingest(details_payload())
        run, _ = self.ingest(None)
        # Nothing succeeded in this run, so the run itself is a failure...
        self.assertEqual(run.state, IngestionState.FAILED)
        self.assertEqual(run.counts['partial'], 1)
        # ...but the previously stored coverage survives untouched.
        coverage = MatchCoverage.objects.get(match=self.match)
        self.assertTrue(coverage.last_error)
        self.assertEqual(self.match.events.count(), 3)

    def test_priority_selection_prefers_in_progress_matches(self):
        Match.objects.update(category=MatchCategory.LIVE,
                             kickoff_utc=timezone.now() - timedelta(minutes=30))
        selected = list(service.coverage_priority_matches(limit=10))
        self.assertEqual(selected, [Match.objects.get()])


# ---------------------------------------------------------------------------
# API: authentication, permissions, reads and writes
# ---------------------------------------------------------------------------


class ApiTestCase(TestCase):
    """Common fixtures: one reader, one staff editor, one administrator."""

    @classmethod
    def setUpTestData(cls):
        cls.reader = User.objects.create_user('reader', password='reader-pass-123')
        cls.staff = User.objects.create_user('editor', password='editor-pass-123',
                                             is_staff=True)
        cls.admin = User.objects.create_superuser('root', password='root-pass-123')
        cls.reader_token = Token.objects.create(user=cls.reader).key
        cls.staff_token = Token.objects.create(user=cls.staff).key
        cls.admin_token = Token.objects.create(user=cls.admin).key

        day = date(2026, 11, 9)
        service.ingest_fixtures([day], client=FakeClient({day: fixture_payload(
            finished=True, started=True, reason_short='AET',
            home_score=2, away_score=1,
        )}))
        cls.match = Match.objects.get()
        service.ingest_coverage(
            matches=[cls.match],
            client=FakeClient({}, details=details_payload(with_ticker=True),
                              ticker=ticker_payload(True)),
        )
        cls.upcoming = Match.objects.create(
            fotmob_id=7000001,
            league=cls.match.league,
            home_team=cls.match.away_team,
            away_team=cls.match.home_team,
            kickoff_utc=timezone.now() + timedelta(days=2),
            category=MatchCategory.UPCOMING,
            status_code=MatchStatusCode.NOT_STARTED,
        )

    def client_for(self, token: str | None = None) -> APIClient:
        client = APIClient()
        if token:
            client.credentials(HTTP_AUTHORIZATION=f'Token {token}')
        return client


class AuthenticationTests(ApiTestCase):
    def test_token_issued_for_valid_credentials(self):
        response = self.client_for().post(
            reverse('api:auth-token'),
            {'username': 'reader', 'password': 'reader-pass-123'}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('token', response.data)
        self.assertEqual(response.data['user']['roles'], ['reader'])

    def test_invalid_credentials_rejected_with_error_envelope(self):
        response = self.client_for().post(
            reverse('api:auth-token'),
            {'username': 'reader', 'password': 'wrong'}, format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'validation_error')
        self.assertIn('request_id', response.data)

    def test_missing_token_is_401(self):
        response = self.client_for().get(reverse('api:match-list'))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data['code'], 'authentication_required')

    def test_invalid_token_is_401(self):
        response = self.client_for('not-a-real-token').get(reverse('api:match-list'))
        self.assertEqual(response.status_code, 401)

    def test_revoked_token_is_401(self):
        client = self.client_for(self.reader_token)
        self.assertEqual(client.post(reverse('api:auth-logout')).status_code, 204)
        self.assertEqual(client.get(reverse('api:match-list')).status_code, 401)

    def test_me_reports_roles_and_actions(self):
        response = self.client_for(self.staff_token).get(reverse('api:auth-me'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('staff_editor', response.data['roles'])
        self.assertIn('soft_delete', response.data['allowed_actions'])

    def test_token_rotation_invalidates_the_old_token(self):
        client = self.client_for(self.reader_token)
        response = client.post(reverse('api:auth-token-rotate'))
        self.assertEqual(response.status_code, 200)
        new_token = response.data['token']
        self.assertNotEqual(new_token, self.reader_token)
        self.assertEqual(client.get(reverse('api:match-list')).status_code, 401)
        self.assertEqual(
            self.client_for(new_token).get(reverse('api:match-list')).status_code, 200,
        )


class MatchReadTests(ApiTestCase):
    def test_list_is_paginated_with_count_and_results(self):
        response = self.client_for(self.reader_token).get(reverse('api:match-list'))
        self.assertEqual(response.status_code, 200)
        for key in ('count', 'next', 'previous', 'results'):
            self.assertIn(key, response.data)
        self.assertEqual(response.data['count'], 2)

    def test_page_size_is_capped_at_200(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-list'), {'page_size': 5000},
        )
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.data['results']), 200)

    def test_detail_by_fotmob_id(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-detail', args=[self.match.fotmob_id]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['id'], self.match.fotmob_id)
        self.assertEqual(response.data['completion_reason'], 'AET')
        self.assertEqual(response.data['duration_minutes'], 120)
        self.assertEqual(response.data['home']['score'], 2)
        self.assertTrue(response.data['coverage']['features']['lineups']['available'])

    def test_unknown_match_is_404_with_envelope(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-detail', args=[999999999]),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['code'], 'not_found')

    def test_upcoming_match_has_null_scores(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-detail', args=[self.upcoming.fotmob_id]),
        )
        self.assertIsNone(response.data['home']['score'])
        self.assertIsNone(response.data['away']['score'])

    def test_search_matches_team_name(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-list'), {'search': 'plymouth'},
        )
        self.assertEqual(response.data['count'], 2)

    def test_short_search_is_rejected(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-list'), {'search': 'a'},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'validation_error')

    def test_unknown_ordering_field_is_rejected(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-list'), {'ordering': 'home_team__name'},
        )
        self.assertEqual(response.status_code, 400)

    def test_ordering_descending_is_stable(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-list'), {'ordering': '-kickoff_utc'},
        )
        ids = [row['id'] for row in response.data['results']]
        self.assertEqual(ids, [self.upcoming.fotmob_id, self.match.fotmob_id])

    def test_date_range_filter(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-list'),
            {'date_from': '2026-11-09', 'date_to': '2026-11-09'},
        )
        self.assertEqual(response.data['count'], 1)

    def test_reversed_date_range_is_rejected(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-list'),
            {'date_from': '2026-11-10', 'date_to': '2026-11-01'},
        )
        self.assertEqual(response.status_code, 400)

    def test_excessive_date_range_is_rejected(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-list'),
            {'date_from': '2020-01-01', 'date_to': '2026-01-01'},
        )
        self.assertEqual(response.status_code, 400)

    def test_by_date_endpoint(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-by-date', args=['2026-11-09']),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['timezone'], 'UTC')

    def test_by_date_accepts_display_timezone(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-by-date', args=['2026-11-09']),
            {'timezone': 'Asia/Karachi'},
        )
        self.assertEqual(response.data['timezone'], 'Asia/Karachi')

    def test_by_date_rejects_bad_format_and_unknown_timezone(self):
        client = self.client_for(self.reader_token)
        self.assertEqual(
            client.get(reverse('api:match-by-date', args=['09-11-2026'])).status_code,
            400,
        )
        self.assertEqual(
            client.get(reverse('api:match-by-date', args=['2026-11-09']),
                       {'timezone': 'Mars/Olympus'}).status_code,
            400,
        )

    def test_empty_result_is_a_valid_empty_page(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-list'), {'search': 'no-such-team'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 0)
        self.assertEqual(response.data['results'], [])

    def test_category_endpoints_are_separated(self):
        client = self.client_for(self.reader_token)
        completed = client.get(reverse('api:match-completed'))
        upcoming = client.get(reverse('api:match-upcoming'))
        live = client.get(reverse('api:match-live'))
        cancelled = client.get(reverse('api:match-cancelled'))
        self.assertEqual(completed.data['count'], 1)
        self.assertEqual(upcoming.data['count'], 1)
        self.assertEqual(live.data['count'], 0)
        self.assertEqual(cancelled.data['count'], 0)
        self.assertIn('stale_projections', live.data)

    def test_archived_match_is_hidden_from_reads(self):
        self.upcoming.is_archived = True
        self.upcoming.save(update_fields=['is_archived'])
        response = self.client_for(self.reader_token).get(reverse('api:match-list'))
        self.assertEqual(response.data['count'], 1)
        self.upcoming.is_archived = False
        self.upcoming.save(update_fields=['is_archived'])


class CoverageEndpointTests(ApiTestCase):
    def test_feature_endpoints_use_the_availability_envelope(self):
        client = self.client_for(self.reader_token)
        for name in ('match-events', 'match-goals', 'match-lineups', 'match-stats',
                     'match-shots', 'match-momentum', 'match-commentary'):
            response = client.get(reverse(f'api:{name}', args=[self.match.fotmob_id]))
            self.assertEqual(response.status_code, 200, name)
            for key in ('available', 'state', 'fetched_at', 'entry_count', 'data',
                        'source_coverage_level'):
                self.assertIn(key, response.data, f'{name}.{key}')
            self.assertTrue(response.data['available'], name)

    def test_goals_endpoint_returns_only_goals(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-goals', args=[self.match.fotmob_id]),
        )
        self.assertEqual(len(response.data['data']), 1)
        self.assertTrue(response.data['data'][0]['is_goal'])

    def test_coverage_summary_lists_every_feature(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-coverage', args=[self.match.fotmob_id]),
        )
        self.assertEqual(response.status_code, 200)
        features = response.data['features']
        self.assertEqual(features['stats']['available'], True)
        self.assertEqual(response.data['source_coverage_level'], 'xG')

    def test_match_without_coverage_reports_not_fetched(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-coverage', args=[self.upcoming.fotmob_id]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['features']['lineups']['available'])
        self.assertEqual(response.data['features']['lineups']['state'], 'not_fetched')

    def test_feature_endpoint_on_uncovered_match_is_unavailable_not_error(self):
        response = self.client_for(self.reader_token).get(
            reverse('api:match-lineups', args=[self.upcoming.fotmob_id]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['available'])
        self.assertEqual(response.data['data'], [])


class WritePermissionTests(ApiTestCase):
    def manual_payload(self) -> dict:
        return {
            'fotmob_id': 9100001,
            'source': SourceChoices.MANUAL,
            'league': self.match.league_id,
            'home_team': self.match.home_team_id,
            'away_team': self.match.away_team_id,
            'kickoff_utc': '2026-12-01T18:00:00Z',
            'category': MatchCategory.UPCOMING,
            'status_code': MatchStatusCode.NOT_STARTED,
        }

    def test_reader_cannot_create_a_match(self):
        response = self.client_for(self.reader_token).post(
            reverse('api:match-list'), self.manual_payload(), format='json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'permission_denied')

    def test_staff_can_create_a_manual_match_and_it_is_audited(self):
        response = self.client_for(self.staff_token).post(
            reverse('api:match-list'), self.manual_payload(), format='json',
        )
        self.assertEqual(response.status_code, 201)
        created = Match.objects.get(fotmob_id=9100001)
        self.assertEqual(created.source, SourceChoices.MANUAL)
        from core.models import AuditLog
        entry = AuditLog.objects.get(object_id=str(created.pk), action='create')
        self.assertEqual(entry.actor, self.staff)

    def test_manual_match_cannot_be_created_as_provider_sourced(self):
        payload = self.manual_payload()
        payload['source'] = SourceChoices.FOTMOB
        response = self.client_for(self.staff_token).post(
            reverse('api:match-list'), payload, format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_upcoming_match_cannot_be_created_with_a_score(self):
        payload = self.manual_payload()
        payload['home_score'] = 1
        response = self.client_for(self.staff_token).post(
            reverse('api:match-list'), payload, format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_provider_field_edit_is_rejected(self):
        response = self.client_for(self.staff_token).patch(
            reverse('api:match-detail', args=[self.match.fotmob_id]),
            {'home_score': 5}, format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('home_score', response.data['details'])
        self.assertEqual(Match.objects.get(pk=self.match.pk).home_score, 2)

    def test_allowed_field_edit_succeeds(self):
        response = self.client_for(self.staff_token).patch(
            reverse('api:match-detail', args=[self.match.fotmob_id]),
            {'round_name': 'First Round Replay', 'reason': 'Corrected round label'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Match.objects.get(pk=self.match.pk).round_name,
                         'First Round Replay')

    def test_delete_is_a_soft_archive(self):
        response = self.client_for(self.staff_token).delete(
            reverse('api:match-detail', args=[self.upcoming.fotmob_id]),
        )
        self.assertEqual(response.status_code, 204)
        archived = Match.objects.get(pk=self.upcoming.pk)
        self.assertTrue(archived.is_archived)
        self.assertIsNotNone(archived.archived_at)
        self.upcoming.is_archived = False
        self.upcoming.archived_at = None
        self.upcoming.save(update_fields=['is_archived', 'archived_at'])

    def test_only_admin_can_create_an_override(self):
        url = reverse('api:match-overrides', args=[self.match.fotmob_id])
        payload = {'field_name': 'kickoff_utc',
                   'value': '2026-11-09T21:00:00Z',
                   'reason': 'Club confirmed a later start.'}
        self.assertEqual(
            self.client_for(self.staff_token).post(url, payload, format='json')
            .status_code, 403,
        )
        response = self.client_for(self.admin_token).post(url, payload, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(MatchOverride.objects.get().actor, self.admin)

    def test_override_must_target_a_provider_field(self):
        response = self.client_for(self.admin_token).post(
            reverse('api:match-overrides', args=[self.match.fotmob_id]),
            {'field_name': 'round_name', 'value': 'x', 'reason': 'test'}, format='json',
        )
        self.assertEqual(response.status_code, 400)


class ReferenceEndpointTests(ApiTestCase):
    def test_league_endpoints(self):
        client = self.client_for(self.reader_token)
        league_id = self.match.league.fotmob_id
        self.assertEqual(client.get(reverse('api:league-list')).data['count'], 1)
        self.assertEqual(
            client.get(reverse('api:league-detail', args=[league_id])).data['id'],
            league_id,
        )
        self.assertEqual(
            client.get(reverse('api:league-matches', args=[league_id])).data['count'], 2,
        )
        self.assertEqual(
            client.get(reverse('api:league-standings', args=[league_id])).data['count'], 0,
        )

    def test_team_endpoints(self):
        client = self.client_for(self.reader_token)
        team_id = self.match.home_team.fotmob_id
        self.assertEqual(client.get(reverse('api:team-list')).data['count'], 2)
        self.assertEqual(
            client.get(reverse('api:team-matches', args=[team_id])).data['count'], 2,
        )
        self.assertEqual(
            client.get(reverse('api:team-players', args=[team_id])).status_code, 200,
        )

    def test_team_delete_blocked_while_matches_reference_it(self):
        response = self.client_for(self.staff_token).delete(
            reverse('api:team-detail', args=[self.match.home_team.fotmob_id]),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'conflict')

    def test_season_endpoints_and_uniqueness(self):
        client = self.client_for(self.staff_token)
        season = Season.objects.get()
        self.assertEqual(
            client.get(reverse('api:season-detail', args=[season.pk])).status_code, 200,
        )
        duplicate = client.post(reverse('api:season-list'), {
            'league': season.league_id, 'year_name': season.year_name,
        }, format='json')
        self.assertEqual(duplicate.status_code, 400)

    def test_reader_cannot_create_a_league(self):
        response = self.client_for(self.reader_token).post(
            reverse('api:league-list'), {'fotmob_id': 55, 'name': 'New'}, format='json',
        )
        self.assertEqual(response.status_code, 403)


class InternalEndpointTests(ApiTestCase):
    def test_reader_cannot_read_sync_runs(self):
        response = self.client_for(self.reader_token).get(reverse('api:sync-run-list'))
        self.assertEqual(response.status_code, 403)

    def test_staff_sees_run_history_and_health(self):
        response = self.client_for(self.staff_token).get(reverse('api:sync-run-list'))
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.data['count'], 1)
        self.assertIn('health', response.data)
        row = response.data['results'][0]
        for key in ('job', 'state', 'counts', 'provider_requests', 'error_count'):
            self.assertIn(key, row)

    def test_run_detail_includes_errors(self):
        run = IngestionRun.objects.first()
        response = self.client_for(self.staff_token).get(
            reverse('api:sync-run-detail', args=[run.pk]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('errors', response.data)

    def test_sync_job_trigger_dispatches_without_fetching_inline(self):
        from unittest.mock import patch

        with patch.object(service, 'dispatch_job', return_value='ingest-test') as spy:
            response = self.client_for(self.staff_token).post(
                reverse('api:sync-job-trigger'),
                {'job': 'sync_fotmob_live', 'days_back': 1, 'dry_run': True},
                format='json',
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.data['accepted'])
        self.assertEqual(response.data['job'], 'sync_fotmob_live')
        self.assertEqual(response.data['status_url'],
                         '/api/internal/sync-runs/?job=sync_fotmob_live')
        spy.assert_called_once()
        self.assertEqual(spy.call_args.args[0], 'sync_fotmob_live')
        self.assertTrue(spy.call_args.args[1]['dry_run'])

    def test_sync_job_defaults_to_dry_run(self):
        from unittest.mock import patch

        with patch.object(service, 'dispatch_job', return_value='w') as spy:
            self.client_for(self.staff_token).post(
                reverse('api:sync-job-trigger'), {'job': 'sync_fotmob_fixtures'},
                format='json',
            )
        self.assertTrue(spy.call_args.args[1]['dry_run'])

    def test_sync_job_trigger_is_audited(self):
        from unittest.mock import patch

        from core.models import AuditLog

        with patch.object(service, 'dispatch_job', return_value='w'):
            self.client_for(self.staff_token).post(
                reverse('api:sync-job-trigger'), {'job': 'sync_fotmob_live'},
                format='json',
            )
        self.assertTrue(AuditLog.objects.filter(action='trigger_sync',
                                                actor=self.staff).exists())

    def test_overlapping_run_of_the_same_job_is_refused(self):
        from unittest.mock import patch

        IngestionRun.objects.create(
            job='sync_fotmob_live', state=IngestionState.RUNNING,
            correlation_id='inflight',
        )
        with patch.object(service, 'dispatch_job') as spy:
            response = self.client_for(self.staff_token).post(
                reverse('api:sync-job-trigger'), {'job': 'sync_fotmob_live'},
                format='json',
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'conflict')
        spy.assert_not_called()

    def test_a_different_job_may_start_while_one_runs(self):
        from unittest.mock import patch

        IngestionRun.objects.create(
            job='sync_fotmob_live', state=IngestionState.RUNNING,
            correlation_id='inflight',
        )
        with patch.object(service, 'dispatch_job', return_value='w') as spy:
            response = self.client_for(self.staff_token).post(
                reverse('api:sync-job-trigger'), {'job': 'sync_fotmob_coverage'},
                format='json',
            )
        self.assertEqual(response.status_code, 202)
        spy.assert_called_once()

    def test_stale_running_row_does_not_block_forever(self):
        IngestionRun.objects.create(
            job='sync_fotmob_live', state=IngestionState.RUNNING,
            correlation_id='crashed',
            started_at=timezone.now() - timedelta(
                minutes=service.RUNNING_LOCK_MINUTES + 5),
        )
        self.assertFalse(service.job_in_progress('sync_fotmob_live'))

    def test_reader_cannot_trigger_a_job(self):
        response = self.client_for(self.reader_token).post(
            reverse('api:sync-job-trigger'), {'job': 'sync_fotmob_live'}, format='json',
        )
        self.assertEqual(response.status_code, 403)

    def test_health_endpoint_reports_counts(self):
        response = self.client_for(self.staff_token).get(
            reverse('api:ingestion-health'),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['matches_total'], 2)
        self.assertIn('completed', response.data['matches_by_category'])

    def test_unknown_job_and_out_of_range_params_are_rejected(self):
        client = self.client_for(self.staff_token)
        cases = [
            {'job': 'rm_-rf'},
            {'job': 'sync_fotmob_fixtures', 'past_days': 9999},
            {'job': 'sync_fotmob_fixtures', 'start_date': '2026-11-01'},
            {'job': 'sync_fotmob_fixtures', 'start_date': '2026-11-10',
             'end_date': '2026-11-01'},
            {'job': 'sync_fotmob_fixtures', 'start_date': '2020-01-01',
             'end_date': '2026-01-01'},
            {'job': 'sync_fotmob_live', 'match_ids': [1, 2]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                response = client.post(reverse('api:sync-job-trigger'), payload,
                                       format='json')
                self.assertEqual(response.status_code, 400)


class RunJobTests(TestCase):
    """`run_job` is what the dispatcher thread executes."""

    def setUp(self):
        self.day = timezone.now().date()
        self.payload = fixture_payload(
            started=True, live_short="55'", home_score=1, away_score=0,
            utc_time=timezone.now().isoformat(),
        )

    def run_with_fake(self, job, params):
        from unittest.mock import patch

        fake = FakeClient({self.day: self.payload}, details=details_payload())
        with patch.object(service, 'FotMobClient', return_value=fake):
            return service.run_job(job, params, trigger=IngestionTrigger.API), fake

    def test_live_job_writes_through_the_service_layer(self):
        run, _ = self.run_with_fake('sync_fotmob_live', {'dry_run': False,
                                                         'days_back': 0})
        self.assertEqual(run.job, 'sync_fotmob_live')
        self.assertEqual(run.trigger, IngestionTrigger.API)
        self.assertEqual(Match.objects.count(), 1)
        self.assertEqual(LiveMatch.objects.count(), 1)

    def test_dry_run_job_writes_nothing(self):
        run, _ = self.run_with_fake('sync_fotmob_live', {'dry_run': True,
                                                         'days_back': 0})
        self.assertEqual(Match.objects.count(), 0)
        self.assertTrue(run.dry_run)

    def test_fixtures_job_honours_an_explicit_date_range(self):
        from unittest.mock import patch

        fake = FakeClient({})
        with patch.object(service, 'FotMobClient', return_value=fake):
            run = service.run_job('sync_fotmob_fixtures', {
                'start_date': date(2026, 11, 9), 'end_date': date(2026, 11, 11),
                'dry_run': True,
            })
        self.assertEqual(run.params['days'],
                         ['2026-11-09', '2026-11-10', '2026-11-11'])

    def test_coverage_job_accepts_explicit_match_ids(self):
        from unittest.mock import patch

        service.ingest_fixtures([self.day], client=FakeClient({self.day: self.payload}))
        match = Match.objects.get()
        fake = FakeClient({}, details=details_payload())
        with patch.object(service, 'FotMobClient', return_value=fake):
            service.run_job('sync_fotmob_coverage', {
                'match_ids': [match.fotmob_id], 'dry_run': False,
            })
        self.assertEqual(fake.detail_calls, [match.fotmob_id])
        self.assertEqual(match.events.count(), 3)

    def test_unknown_job_raises(self):
        with self.assertRaises(ValueError):
            service.run_job('drop_everything', {})


class DispatchJobTests(TransactionTestCase):
    """The worker thread needs real commits, so this is not a TestCase.

    A plain TestCase wraps each test in a transaction the thread's own
    connection cannot see, which would make the write invisible here.
    """

    def test_dispatch_job_runs_the_job_in_a_worker_thread(self):
        from unittest.mock import patch

        day = timezone.now().date()
        payload = fixture_payload(
            started=True, live_short="55'", home_score=1, away_score=0,
            utc_time=timezone.now().isoformat(),
        )
        fake = FakeClient({day: payload})
        with patch.object(service, 'FotMobClient', return_value=fake):
            name = service.dispatch_job(
                'sync_fotmob_live', {'dry_run': False, 'days_back': 0},
            )
            self.assertEqual(name, 'ingest-sync_fotmob_live')
            for _ in range(100):
                if Match.objects.count():
                    break
                time.sleep(0.05)
        self.assertEqual(Match.objects.count(), 1, 'worker thread did not commit')
        self.assertEqual(LiveMatch.objects.count(), 1)
        run = IngestionRun.objects.get(job='sync_fotmob_live')
        self.assertEqual(run.trigger, IngestionTrigger.API)
        self.assertEqual(run.state, IngestionState.SUCCESS)

class TimeBoundaryTests(TestCase):
    def test_utc_midnight_boundary_belongs_to_the_later_day(self):
        day = date(2026, 11, 10)
        service.ingest_fixtures([day], client=FakeClient({day: fixture_payload(
            match_id=7777, utc_time='2026-11-10T00:00:00.000Z',
        )}))
        match = Match.objects.get()
        self.assertEqual(match.kickoff_utc.date(), day)

    def test_half_start_times_are_converted_to_utc(self):
        payload = fixture_payload(started=True, live_short="60'")
        payload['leagues'][0]['matches'][0]['status']['halfs'] = {
            'firstHalfStarted': '09.11.2026 19:30:00',
            'secondHalfStarted': '09.11.2026 20:30:00',
        }
        fixture, _ = next(iter(service.iter_fixtures(payload, date(2026, 11, 9))))
        self.assertEqual(
            fixture['first_half_started_utc'],
            datetime(2026, 11, 9, 19, 30, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(
            fixture['second_half_started_utc'],
            datetime(2026, 11, 9, 20, 30, tzinfo=dt_timezone.utc),
        )


class CommandTests(TestCase):
    """Management commands wire into the same service layer."""

    def test_commands_are_registered(self):
        from django.core.management import get_commands

        commands = get_commands()
        for name in ('sync_fotmob_fixtures', 'sync_fotmob_live',
                     'sync_fotmob_coverage', 'daily_fixture_reconcile',
                     'weekly_backfill'):
            self.assertEqual(commands.get(name), 'api', name)

    def test_fixture_command_rejects_a_half_specified_range(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command('sync_fotmob_fixtures', '--start-date', '2026-11-01')

    def test_fixture_command_dry_run_uses_the_service_layer(self):
        from unittest.mock import patch

        from django.core.management import call_command

        day = date(2026, 11, 9)
        fake = FakeClient({day: fixture_payload()})
        with patch.object(service, 'FotMobClient', return_value=fake):
            call_command('sync_fotmob_fixtures', '--date', '2026-11-09',
                         '--dry-run', '--json')
        self.assertEqual(Match.objects.count(), 0)
        run = IngestionRun.objects.get(job='sync_fotmob_fixtures')
        self.assertTrue(run.dry_run)
        self.assertEqual(run.counts['created'], 1)
