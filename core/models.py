"""Persisted football domain models.

Design rules taken from the architecture document, section 3:

* FotMob IDs are the upstream identity. ``fotmob_id`` is unique per entity and
  one match row exists per FotMob match ID, enforced in the database.
* All datetimes are stored timezone-aware in UTC. Conversion to a user
  timezone happens in API presentation only.
* A missing score stays ``NULL``. An upcoming fixture is never stored as 0-0.
* Provider-owned fields are listed per model in ``PROVIDER_FIELDS`` so the API
  layer can reject accidental edits without hard-coding field names in views.
* Coverage richness, fetch success and availability are three separate
  concepts: see :class:`MatchCoverage` and :class:`CoverageFeature`.

The ``api`` app reads and writes these models through ``api/service.py``; it
must not redefine any of them in ``api/models.py``.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class SourceChoices(models.TextChoices):
    FOTMOB = 'fotmob', 'FotMob'
    MANUAL = 'manual', 'Manual'


class MatchCategory(models.TextChoices):
    """Coarse state family. See architecture document section 3.2."""

    UPCOMING = 'upcoming', 'Upcoming'
    LIVE = 'live', 'In progress'
    COMPLETED = 'completed', 'Completed'
    DISRUPTED = 'disrupted', 'Disrupted'


class MatchStatusCode(models.TextChoices):
    """Granular status. Disruption reasons are kept distinct on purpose."""

    NOT_STARTED = 'NS', 'Not started'
    LIVE = 'LIVE', 'Live'
    HALF_TIME = 'HT', 'Half time'
    EXTRA_TIME = 'ET', 'Extra time'
    PENALTY_SHOOTOUT = 'PEN_SHOOTOUT', 'Penalty shootout'
    FULL_TIME = 'FT', 'Full time'
    AFTER_EXTRA_TIME = 'AET', 'After extra time'
    AFTER_PENALTIES = 'PEN', 'After penalties'
    WALKOVER = 'WO', 'Walkover'
    AWARDED = 'AWD', 'Awarded'
    POSTPONED = 'POSTPONED', 'Postponed'
    CANCELLED = 'CANCELLED', 'Cancelled'
    ABANDONED = 'ABANDONED', 'Abandoned'
    INTERRUPTED = 'INTERRUPTED', 'Interrupted'


#: Status codes that mean the match is being played right now.
IN_PROGRESS_CODES = frozenset({
    MatchStatusCode.LIVE,
    MatchStatusCode.HALF_TIME,
    MatchStatusCode.EXTRA_TIME,
    MatchStatusCode.PENALTY_SHOOTOUT,
})

#: Status codes that mean the match reached a sporting conclusion.
COMPLETED_CODES = frozenset({
    MatchStatusCode.FULL_TIME,
    MatchStatusCode.AFTER_EXTRA_TIME,
    MatchStatusCode.AFTER_PENALTIES,
    MatchStatusCode.WALKOVER,
    MatchStatusCode.AWARDED,
})

#: Status codes that mean the fixture did not run as scheduled.
DISRUPTED_CODES = frozenset({
    MatchStatusCode.POSTPONED,
    MatchStatusCode.CANCELLED,
    MatchStatusCode.ABANDONED,
    MatchStatusCode.INTERRUPTED,
})


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Reference entities
# ---------------------------------------------------------------------------


class League(TimeStampedModel):
    """A competition. Provider-owned; admins may override display metadata."""

    PROVIDER_FIELDS = ('fotmob_id', 'parent_fotmob_id', 'country_code')

    fotmob_id = models.BigIntegerField(unique=True)
    #: FotMob's ``primaryId``: the parent competition for a stage/group league.
    parent_fotmob_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    name = models.CharField(max_length=255)
    country_code = models.CharField(max_length=16, blank=True)
    league_type = models.CharField(max_length=64, blank=True)
    logo_url = models.URLField(max_length=500, blank=True)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('name', 'id')
        indexes = [
            models.Index(fields=('name',)),
            models.Index(fields=('country_code',)),
        ]

    def __str__(self) -> str:
        return f'{self.name} ({self.fotmob_id})'


class Season(TimeStampedModel):
    PROVIDER_FIELDS = ('league', 'year_name')

    league = models.ForeignKey(League, related_name='seasons', on_delete=models.CASCADE)
    year_name = models.CharField(max_length=32)
    is_current = models.BooleanField(default=False)

    class Meta:
        ordering = ('-year_name', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('league', 'year_name'), name='uniq_season_league_year',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.league.name} {self.year_name}'


class Team(TimeStampedModel):
    PROVIDER_FIELDS = ('fotmob_id', 'country_code')

    fotmob_id = models.BigIntegerField(unique=True)
    name = models.CharField(max_length=255)
    short_name = models.CharField(max_length=128, blank=True)
    country_code = models.CharField(max_length=16, blank=True)
    logo_url = models.URLField(max_length=500, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('name', 'id')
        indexes = [models.Index(fields=('name',))]

    def __str__(self) -> str:
        return f'{self.name} ({self.fotmob_id})'


class Player(TimeStampedModel):
    PROVIDER_FIELDS = ('fotmob_id',)

    fotmob_id = models.BigIntegerField(unique=True, null=True, blank=True)
    name = models.CharField(max_length=255)
    position = models.CharField(max_length=64, blank=True)
    country_code = models.CharField(max_length=16, blank=True)

    class Meta:
        ordering = ('name', 'id')
        indexes = [models.Index(fields=('name',))]

    def __str__(self) -> str:
        return self.name


class TeamPlayer(TimeStampedModel):
    team = models.ForeignKey(Team, related_name='squad', on_delete=models.CASCADE)
    player = models.ForeignKey(Player, related_name='team_links', on_delete=models.CASCADE)
    shirt_number = models.CharField(max_length=8, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('team', 'player'), name='uniq_team_player',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.player.name} @ {self.team.name}'


class Standing(TimeStampedModel):
    league = models.ForeignKey(League, related_name='standings', on_delete=models.CASCADE)
    season = models.ForeignKey(
        Season, related_name='standings', on_delete=models.CASCADE,
        null=True, blank=True,
    )
    team = models.ForeignKey(Team, related_name='standings', on_delete=models.CASCADE)
    group_name = models.CharField(max_length=64, blank=True)
    rank = models.PositiveIntegerField(null=True, blank=True)
    played = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    draws = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    goals_for = models.IntegerField(default=0)
    goals_against = models.IntegerField(default=0)
    points = models.IntegerField(default=0)

    class Meta:
        ordering = ('rank', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('league', 'season', 'team', 'group_name'),
                name='uniq_standing_row',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.team.name} #{self.rank}'


class Transfer(TimeStampedModel):
    player = models.ForeignKey(Player, related_name='transfers', on_delete=models.CASCADE)
    from_team = models.ForeignKey(
        Team, related_name='transfers_out', on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    to_team = models.ForeignKey(
        Team, related_name='transfers_in', on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    transfer_date = models.DateField(null=True, blank=True)
    fee_text = models.CharField(max_length=128, blank=True)
    transfer_type = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ('-transfer_date', 'id')

    def __str__(self) -> str:
        return f'{self.player.name} transfer'


# ---------------------------------------------------------------------------
# Match and live projection
# ---------------------------------------------------------------------------


class MatchQuerySet(models.QuerySet):
    def visible(self) -> 'MatchQuerySet':
        """Exclude soft-deleted rows. Deletes are archives, never row drops."""
        return self.filter(is_archived=False)

    def with_relations(self) -> 'MatchQuerySet':
        return self.select_related('league', 'season', 'home_team', 'away_team', 'live')


class Match(TimeStampedModel):
    """One fixture, keyed by its FotMob match ID.

    ``category`` is the coarse state family used by the list endpoints;
    ``status_code`` keeps the exact provider state so POSTPONED, CANCELLED,
    ABANDONED and INTERRUPTED are never conflated.
    """

    #: Fields owned by the ingestion jobs. Admin edits need an override row.
    PROVIDER_FIELDS = (
        'fotmob_id', 'league', 'season', 'home_team', 'away_team',
        'kickoff_utc', 'category', 'status_code', 'home_score', 'away_score',
        'home_red_cards', 'away_red_cards', 'provider_league_id',
        'provider_primary_league_id', 'duration_minutes',
    )

    fotmob_id = models.BigIntegerField(unique=True)
    source = models.CharField(
        max_length=16, choices=SourceChoices.choices, default=SourceChoices.FOTMOB,
    )

    league = models.ForeignKey(League, related_name='matches', on_delete=models.PROTECT)
    season = models.ForeignKey(
        Season, related_name='matches', on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    #: Raw provider league IDs are preserved even when they disagree.
    provider_league_id = models.BigIntegerField(null=True, blank=True)
    provider_primary_league_id = models.BigIntegerField(null=True, blank=True)
    round_name = models.CharField(max_length=255, blank=True)

    home_team = models.ForeignKey(Team, related_name='home_matches', on_delete=models.PROTECT)
    away_team = models.ForeignKey(Team, related_name='away_matches', on_delete=models.PROTECT)

    kickoff_utc = models.DateTimeField(null=True, blank=True, db_index=True)
    kickoff_display = models.CharField(max_length=32, blank=True)

    category = models.CharField(
        max_length=16, choices=MatchCategory.choices,
        default=MatchCategory.UPCOMING, db_index=True,
    )
    status_code = models.CharField(
        max_length=24, choices=MatchStatusCode.choices,
        default=MatchStatusCode.NOT_STARTED,
    )
    #: Provider wording, e.g. "After extra time" or "Postponed".
    status_reason_short = models.CharField(max_length=32, blank=True)
    status_reason_long = models.CharField(max_length=255, blank=True)

    #: NULL means the score is not known yet. Never defaulted to zero.
    home_score = models.IntegerField(null=True, blank=True)
    away_score = models.IntegerField(null=True, blank=True)
    home_penalty_score = models.IntegerField(null=True, blank=True)
    away_penalty_score = models.IntegerField(null=True, blank=True)
    home_red_cards = models.IntegerField(null=True, blank=True)
    away_red_cards = models.IntegerField(null=True, blank=True)

    #: Regulation length of a completed match: 90 for FT, 120 for AET/PEN.
    duration_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    first_half_started_utc = models.DateTimeField(null=True, blank=True)
    second_half_started_utc = models.DateTimeField(null=True, blank=True)

    #: Set every time an ingestion job saw this fixture upstream.
    last_seen_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)

    is_archived = models.BooleanField(default=False, db_index=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    objects = MatchQuerySet.as_manager()

    class Meta:
        ordering = ('kickoff_utc', 'id')
        verbose_name_plural = 'matches'
        indexes = [
            models.Index(fields=('category', 'kickoff_utc')),
            models.Index(fields=('league', 'kickoff_utc')),
            models.Index(fields=('status_code',)),
            models.Index(fields=('last_seen_at',)),
        ]

    def __str__(self) -> str:
        return f'{self.home_team_id} v {self.away_team_id} ({self.fotmob_id})'

    @property
    def is_in_progress(self) -> bool:
        return self.category == MatchCategory.LIVE

    @property
    def score_known(self) -> bool:
        return self.home_score is not None and self.away_score is not None


class LiveMatch(TimeStampedModel):
    """Ephemeral projection of an in-progress match.

    System-managed. It is expired once the match is verified as completed or
    disrupted; final scores live on :class:`Match`, never only here.
    """

    match = models.OneToOneField(Match, related_name='live', on_delete=models.CASCADE)
    minute_elapsed = models.PositiveSmallIntegerField(null=True, blank=True)
    #: Provider display string, e.g. "45+2" or "HT".
    minute_display = models.CharField(max_length=16, blank=True)
    home_score = models.IntegerField(null=True, blank=True)
    away_score = models.IntegerField(null=True, blank=True)
    home_red_cards = models.IntegerField(null=True, blank=True)
    away_red_cards = models.IntegerField(null=True, blank=True)
    status_code = models.CharField(max_length=24, blank=True)
    last_source_fetch_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = 'live matches'

    def __str__(self) -> str:
        return f'live {self.match_id}'

    def is_stale(self, threshold_minutes: int | None = None) -> bool:
        """True when the projection has not been refreshed recently."""
        if threshold_minutes is None:
            threshold_minutes = settings.FOTMOB.get('LIVE_STALE_MINUTES', 15)
        reference = self.last_source_fetch_at or self.updated_at
        if reference is None:
            return True
        return timezone.now() - reference > timedelta(minutes=threshold_minutes)


# ---------------------------------------------------------------------------
# Coverage: availability is tracked per feature
# ---------------------------------------------------------------------------


class CoverageFeatureChoices(models.TextChoices):
    EVENTS = 'events', 'Events'
    GOALS = 'goals', 'Goals'
    LINEUPS = 'lineups', 'Lineups'
    PLAYER_STATS = 'player_stats', 'Player statistics'
    STATS = 'stats', 'Team statistics'
    SHOTS = 'shots', 'Shots'
    MOMENTUM = 'momentum', 'Momentum'
    COMMENTARY = 'commentary', 'Commentary'


class CoverageState(models.TextChoices):
    """Four distinct outcomes; HTTP 200 alone only proves ``fetched``."""

    NOT_FETCHED = 'not_fetched', 'Not fetched'
    AVAILABLE = 'available', 'Available'
    EMPTY = 'empty', 'Fetched but empty'
    UNAVAILABLE = 'unavailable', 'Not offered by source'
    FAILED = 'failed', 'Fetch failed'


class MatchCoverage(TimeStampedModel):
    """Per-match coverage snapshot metadata. System-managed."""

    match = models.OneToOneField(Match, related_name='coverage', on_delete=models.CASCADE)
    #: FotMob's own ``coverageLevel``: xG, ratings, lower, ...
    source_coverage_level = models.CharField(max_length=32, blank=True)
    last_fetch_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    payload_version = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name_plural = 'match coverages'

    def __str__(self) -> str:
        return f'coverage {self.match_id}'

    def feature_summary(self) -> dict[str, dict]:
        return {
            feature.feature: {
                'state': feature.state,
                'available': feature.state == CoverageState.AVAILABLE,
                'entry_count': feature.entry_count,
                'fetched_at': feature.fetched_at,
            }
            for feature in self.features.all()
        }


class CoverageFeature(TimeStampedModel):
    coverage = models.ForeignKey(
        MatchCoverage, related_name='features', on_delete=models.CASCADE,
    )
    feature = models.CharField(max_length=24, choices=CoverageFeatureChoices.choices)
    state = models.CharField(
        max_length=16, choices=CoverageState.choices, default=CoverageState.NOT_FETCHED,
    )
    #: Real entry count, so richness is measurable independently of HTTP status.
    entry_count = models.PositiveIntegerField(default=0)
    fetched_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ('feature',)
        constraints = [
            models.UniqueConstraint(
                fields=('coverage', 'feature'), name='uniq_coverage_feature',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.feature}={self.state}'


class MatchEvent(TimeStampedModel):
    """Normalized timeline entry. Deduplicated by a stable source key."""

    match = models.ForeignKey(Match, related_name='events', on_delete=models.CASCADE)
    upstream_event_id = models.CharField(max_length=64, blank=True)
    #: Deterministic key used for idempotent upserts when no upstream ID exists.
    dedupe_key = models.CharField(max_length=128)
    event_type = models.CharField(max_length=32)
    is_goal = models.BooleanField(default=False)
    team = models.ForeignKey(
        Team, related_name='match_events', on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    is_home = models.BooleanField(null=True, blank=True)
    player = models.ForeignKey(
        Player, related_name='events', on_delete=models.SET_NULL, null=True, blank=True,
    )
    player_name = models.CharField(max_length=255, blank=True)
    assist_player_name = models.CharField(max_length=255, blank=True)
    minute = models.IntegerField(null=True, blank=True)
    added_time = models.IntegerField(null=True, blank=True)
    sequence = models.PositiveIntegerField(default=0)
    score_home = models.IntegerField(null=True, blank=True)
    score_away = models.IntegerField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ('minute', 'added_time', 'sequence', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('match', 'dedupe_key'), name='uniq_match_event_key',
            ),
        ]
        indexes = [models.Index(fields=('match', 'is_goal'))]

    def __str__(self) -> str:
        return f'{self.event_type} {self.minute}'


class MatchLineup(TimeStampedModel):
    match = models.ForeignKey(Match, related_name='lineups', on_delete=models.CASCADE)
    team = models.ForeignKey(Team, related_name='match_lineups', on_delete=models.CASCADE)
    is_home = models.BooleanField()
    formation = models.CharField(max_length=32, blank=True)
    coach_name = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('match', 'team'), name='uniq_match_lineup',
            ),
        ]

    def __str__(self) -> str:
        return f'lineup {self.match_id}/{self.team_id}'


class LineupPlayer(TimeStampedModel):
    lineup = models.ForeignKey(
        MatchLineup, related_name='players', on_delete=models.CASCADE,
    )
    player = models.ForeignKey(
        Player, related_name='lineup_entries', on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    name = models.CharField(max_length=255)
    shirt_number = models.CharField(max_length=8, blank=True)
    position = models.CharField(max_length=64, blank=True)
    is_starter = models.BooleanField(default=False)
    is_captain = models.BooleanField(default=False)
    rating = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    minutes_played = models.PositiveSmallIntegerField(null=True, blank=True)
    upstream_player_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ('-is_starter', 'shirt_number', 'id')

    def __str__(self) -> str:
        return self.name


class StatPeriod(models.TextChoices):
    ALL = 'all', 'Whole match'
    FIRST_HALF = '1h', 'First half'
    SECOND_HALF = '2h', 'Second half'
    EXTRA_TIME = 'et', 'Extra time'


class TeamMatchStat(TimeStampedModel):
    match = models.ForeignKey(Match, related_name='team_stats', on_delete=models.CASCADE)
    team = models.ForeignKey(Team, related_name='match_stats', on_delete=models.CASCADE)
    stat_group = models.CharField(max_length=64, blank=True)
    stat_key = models.CharField(max_length=128)
    period = models.CharField(
        max_length=8, choices=StatPeriod.choices, default=StatPeriod.ALL,
    )
    raw_value = models.CharField(max_length=64, blank=True)
    numeric_value = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ('stat_group', 'stat_key', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('match', 'team', 'stat_key', 'period'),
                name='uniq_team_match_stat',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.stat_key}={self.raw_value}'


class PlayerMatchStat(TimeStampedModel):
    match = models.ForeignKey(Match, related_name='player_stats', on_delete=models.CASCADE)
    team = models.ForeignKey(
        Team, related_name='player_match_stats', on_delete=models.CASCADE,
        null=True, blank=True,
    )
    player = models.ForeignKey(
        Player, related_name='match_stats', on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    player_name = models.CharField(max_length=255, blank=True)
    stat_key = models.CharField(max_length=128)
    period = models.CharField(
        max_length=8, choices=StatPeriod.choices, default=StatPeriod.ALL,
    )
    raw_value = models.CharField(max_length=64, blank=True)
    numeric_value = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ('player_name', 'stat_key', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('match', 'player_name', 'stat_key', 'period'),
                name='uniq_player_match_stat',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.player_name} {self.stat_key}'


class MatchShot(TimeStampedModel):
    match = models.ForeignKey(Match, related_name='shots', on_delete=models.CASCADE)
    team = models.ForeignKey(
        Team, related_name='shots', on_delete=models.SET_NULL, null=True, blank=True,
    )
    upstream_shot_id = models.CharField(max_length=64, blank=True)
    dedupe_key = models.CharField(max_length=128)
    player_name = models.CharField(max_length=255, blank=True)
    is_home = models.BooleanField(null=True, blank=True)
    minute = models.IntegerField(null=True, blank=True)
    x = models.FloatField(null=True, blank=True)
    y = models.FloatField(null=True, blank=True)
    expected_goals = models.FloatField(null=True, blank=True)
    expected_goals_on_target = models.FloatField(null=True, blank=True)
    situation = models.CharField(max_length=64, blank=True)
    shot_type = models.CharField(max_length=64, blank=True)
    event_type = models.CharField(max_length=64, blank=True)
    is_on_target = models.BooleanField(null=True, blank=True)
    is_blocked = models.BooleanField(null=True, blank=True)

    class Meta:
        ordering = ('minute', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('match', 'dedupe_key'), name='uniq_match_shot_key',
            ),
        ]

    def __str__(self) -> str:
        return f'shot {self.minute} {self.player_name}'


class MatchMomentum(TimeStampedModel):
    match = models.ForeignKey(Match, related_name='momentum', on_delete=models.CASCADE)
    minute = models.IntegerField()
    value = models.FloatField()

    class Meta:
        ordering = ('minute', 'id')
        verbose_name_plural = 'match momentum points'
        constraints = [
            models.UniqueConstraint(
                fields=('match', 'minute'), name='uniq_match_momentum_minute',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.minute}:{self.value}'


class MatchCommentary(TimeStampedModel):
    """Live ticker entries. Only written when real entries exist."""

    match = models.ForeignKey(Match, related_name='commentary', on_delete=models.CASCADE)
    dedupe_key = models.CharField(max_length=128)
    minute = models.CharField(max_length=16, blank=True)
    minute_sort = models.IntegerField(null=True, blank=True)
    text = models.TextField()
    entry_type = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ('minute_sort', 'id')
        verbose_name_plural = 'match commentary entries'
        constraints = [
            models.UniqueConstraint(
                fields=('match', 'dedupe_key'), name='uniq_match_commentary_key',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.minute} {self.text[:40]}'


class MatchRawSnapshot(TimeStampedModel):
    """Bounded raw payload store for replay and debugging.

    One row per match, overwritten on each successful fetch, so megabyte
    payloads are not duplicated across normalized rows.
    """

    match = models.OneToOneField(
        Match, related_name='raw_snapshot', on_delete=models.CASCADE,
    )
    fixture_payload = models.JSONField(null=True, blank=True)
    details_payload = models.JSONField(null=True, blank=True)
    fetched_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f'raw {self.match_id}'


# ---------------------------------------------------------------------------
# Ingestion observability
# ---------------------------------------------------------------------------


class IngestionJob(models.TextChoices):
    FIXTURES = 'sync_fotmob_fixtures', 'Fixture sync'
    LIVE = 'sync_fotmob_live', 'Live sync'
    COVERAGE = 'sync_fotmob_coverage', 'Coverage sync'
    RECONCILE = 'daily_fixture_reconcile', 'Daily reconcile'
    BACKFILL = 'weekly_backfill', 'Weekly backfill'


class IngestionState(models.TextChoices):
    RUNNING = 'running', 'Running'
    SUCCESS = 'success', 'Success'
    PARTIAL = 'partial', 'Partial'
    FAILED = 'failed', 'Failed'


class IngestionTrigger(models.TextChoices):
    CRON = 'cron', 'Cron'
    MANUAL = 'manual', 'Manual'
    API = 'api', 'API'


class IngestionRun(models.Model):
    job = models.CharField(max_length=40, choices=IngestionJob.choices, db_index=True)
    trigger = models.CharField(
        max_length=16, choices=IngestionTrigger.choices, default=IngestionTrigger.CRON,
    )
    state = models.CharField(
        max_length=16, choices=IngestionState.choices, default=IngestionState.RUNNING,
    )
    correlation_id = models.CharField(max_length=40, db_index=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    params = models.JSONField(default=dict, blank=True)
    #: created / updated / unchanged / skipped / partial / invalid counters.
    counts = models.JSONField(default=dict, blank=True)
    provider_requests = models.PositiveIntegerField(default=0)
    provider_errors = models.PositiveIntegerField(default=0)
    dry_run = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ('-started_at', '-id')
        indexes = [models.Index(fields=('job', '-started_at'))]

    def __str__(self) -> str:
        return f'{self.job} {self.correlation_id} ({self.state})'

    @property
    def duration_seconds(self) -> float | None:
        if not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class IngestionError(models.Model):
    class Category(models.TextChoices):
        HTTP = 'http', 'Upstream HTTP'
        PARSE = 'parse', 'Parse or shape'
        VALIDATION = 'validation', 'Validation'
        DATABASE = 'database', 'Database'
        UNKNOWN = 'unknown', 'Unknown'

    run = models.ForeignKey(IngestionRun, related_name='errors', on_delete=models.CASCADE)
    category = models.CharField(
        max_length=16, choices=Category.choices, default=Category.UNKNOWN,
    )
    scope = models.CharField(max_length=64, blank=True)
    external_ref = models.CharField(max_length=64, blank=True)
    message = models.TextField()
    #: Quarantined payload so a malformed record can be replayed without
    #: re-scraping upstream.
    payload = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-id',)

    def __str__(self) -> str:
        return f'{self.category}: {self.message[:60]}'


# ---------------------------------------------------------------------------
# Manual corrections and audit
# ---------------------------------------------------------------------------


class MatchOverride(TimeStampedModel):
    """An approved manual correction that provider syncs must not silently undo."""

    match = models.ForeignKey(Match, related_name='overrides', on_delete=models.CASCADE)
    field_name = models.CharField(max_length=64)
    value = models.JSONField()
    reason = models.TextField()
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name='match_overrides',
        on_delete=models.SET_NULL, null=True, blank=True,
    )
    priority = models.PositiveSmallIntegerField(default=100)
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-priority', '-id')
        constraints = [
            models.UniqueConstraint(
                fields=('match', 'field_name'),
                condition=models.Q(is_active=True),
                name='uniq_active_override_per_field',
            ),
        ]

    def __str__(self) -> str:
        return f'override {self.match_id}.{self.field_name}'

    def is_effective(self) -> bool:
        if not self.is_active:
            return False
        return self.expires_at is None or self.expires_at > timezone.now()


class AuditLog(models.Model):
    """Who changed what, when and why. Written for every admin write."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name='audit_entries',
        on_delete=models.SET_NULL, null=True, blank=True,
    )
    action = models.CharField(max_length=32)
    model_name = models.CharField(max_length=64)
    object_id = models.CharField(max_length=64)
    changes = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    request_id = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('-id',)

    def __str__(self) -> str:
        return f'{self.action} {self.model_name}#{self.object_id}'
