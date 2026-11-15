"""Input, query-parameter and output serializers.

Every APIView validates its query parameters through a serializer here, and
every response body is shaped by one. Provider-owned fields are read-only on
the write serializers: corrections go through the override flow instead.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import authenticate
from django.utils import timezone
from rest_framework import serializers

from core.models import (
    CoverageFeature,
    CoverageState,
    IngestionError,
    IngestionRun,
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
    MatchOverride,
    MatchShot,
    MatchStatusCode,
    Player,
    PlayerMatchStat,
    Season,
    SourceChoices,
    Standing,
    Team,
    TeamMatchStat,
)

MAX_DATE_RANGE_DAYS = 90


# ---------------------------------------------------------------------------
# Query-parameter serializers
# ---------------------------------------------------------------------------


class MatchFilterSerializer(serializers.Serializer):
    """Validates and bounds every supported match query parameter."""

    status = serializers.ChoiceField(
        choices=['live', 'completed', 'upcoming', 'cancelled', 'disrupted'],
        required=False,
    )
    status_code = serializers.ChoiceField(
        choices=MatchStatusCode.choices, required=False,
    )
    date = serializers.DateField(required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    league_id = serializers.IntegerField(required=False, min_value=1)
    team_id = serializers.IntegerField(required=False, min_value=1)
    season_id = serializers.IntegerField(required=False, min_value=1)
    search = serializers.CharField(required=False, max_length=120, allow_blank=True)
    ordering = serializers.ChoiceField(
        choices=['kickoff_utc', '-kickoff_utc', 'id', '-id', 'league', '-league'],
        required=False,
    )

    def validate_search(self, value: str) -> str:
        value = value.strip()
        if value and len(value) < 2:
            raise serializers.ValidationError('Search needs at least 2 characters.')
        return value

    def validate(self, attrs: dict) -> dict:
        start, end = attrs.get('date_from'), attrs.get('date_to')
        if start and end:
            if end < start:
                raise serializers.ValidationError(
                    {'date_to': 'date_to must be on or after date_from.'},
                )
            if (end - start) > timedelta(days=MAX_DATE_RANGE_DAYS):
                raise serializers.ValidationError(
                    {'date_to': f'Date range cannot exceed {MAX_DATE_RANGE_DAYS} days.'},
                )
        return attrs


class TimezoneQuerySerializer(serializers.Serializer):
    """Optional presentation timezone for the by-date endpoint."""

    timezone = serializers.CharField(required=False, max_length=64)

    def validate_timezone(self, value: str):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            return str(ZoneInfo(value))
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            raise serializers.ValidationError(f'Unknown timezone {value!r}.')


class SyncRunFilterSerializer(serializers.Serializer):
    job = serializers.CharField(required=False, max_length=40)
    state = serializers.CharField(required=False, max_length=16)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TokenRequestSerializer(serializers.Serializer):
    username = serializers.CharField(write_only=True)
    password = serializers.CharField(write_only=True, style={'input_type': 'password'})

    def validate(self, attrs: dict) -> dict:
        user = authenticate(
            request=self.context.get('request'),
            username=attrs['username'],
            password=attrs['password'],
        )
        if user is None or not user.is_active:
            raise serializers.ValidationError('Invalid credentials.')
        attrs['user'] = user
        return attrs


class CurrentUserSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    username = serializers.CharField(read_only=True)
    is_staff = serializers.BooleanField(read_only=True)
    is_superuser = serializers.BooleanField(read_only=True)
    roles = serializers.SerializerMethodField()
    allowed_actions = serializers.SerializerMethodField()

    def get_roles(self, user) -> list[str]:
        roles = ['reader']
        if user.is_staff:
            roles.append('staff_editor')
        if user.is_superuser:
            roles.append('administrator')
        return roles

    def get_allowed_actions(self, user) -> list[str]:
        actions = ['read']
        if user.is_staff:
            actions += ['create_manual_match', 'edit_allowed_fields', 'soft_delete',
                        'read_sync_runs']
        if user.is_superuser:
            actions += ['manage_overrides', 'trigger_sync']
        return actions


# ---------------------------------------------------------------------------
# Reference output serializers
# ---------------------------------------------------------------------------


class LeagueSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source='fotmob_id', read_only=True)

    class Meta:
        model = League
        fields = ('id', 'name', 'country_code', 'league_type', 'logo_url',
                  'parent_fotmob_id', 'is_active')


class LeagueWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = League
        fields = ('fotmob_id', 'name', 'country_code', 'league_type', 'logo_url',
                  'is_active', 'parent_fotmob_id')
        extra_kwargs = {
            # Provider-owned: set at creation, never silently re-pointed later.
            'fotmob_id': {'required': True},
        }

    def update(self, instance, validated_data):
        for field_name in League.PROVIDER_FIELDS:
            if field_name in validated_data and \
                    getattr(instance, field_name) != validated_data[field_name]:
                raise serializers.ValidationError({
                    field_name: 'Provider-owned field; use an override instead.',
                })
        return super().update(instance, validated_data)


class TeamSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source='fotmob_id', read_only=True)

    class Meta:
        model = Team
        fields = ('id', 'name', 'short_name', 'country_code', 'logo_url')


class TeamWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Team
        fields = ('fotmob_id', 'name', 'short_name', 'country_code', 'logo_url')

    def update(self, instance, validated_data):
        for field_name in Team.PROVIDER_FIELDS:
            if field_name in validated_data and \
                    getattr(instance, field_name) != validated_data[field_name]:
                raise serializers.ValidationError({
                    field_name: 'Provider-owned field; use an override instead.',
                })
        return super().update(instance, validated_data)


class SeasonSerializer(serializers.ModelSerializer):
    league = LeagueSerializer(read_only=True)

    class Meta:
        model = Season
        fields = ('id', 'league', 'year_name', 'is_current')


class SeasonWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Season
        fields = ('id', 'league', 'year_name', 'is_current')

    def validate(self, attrs: dict) -> dict:
        league = attrs.get('league') or getattr(self.instance, 'league', None)
        year_name = attrs.get('year_name') or getattr(self.instance, 'year_name', None)
        queryset = Season.objects.filter(league=league, year_name=year_name)
        if self.instance is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError(
                {'year_name': 'This league already has that season.'},
            )
        return attrs


class PlayerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Player
        fields = ('id', 'fotmob_id', 'name', 'position', 'country_code')


class StandingSerializer(serializers.ModelSerializer):
    team = TeamSerializer(read_only=True)

    class Meta:
        model = Standing
        fields = ('id', 'team', 'group_name', 'rank', 'played', 'wins', 'draws',
                  'losses', 'goals_for', 'goals_against', 'points')


# ---------------------------------------------------------------------------
# Match serializers
# ---------------------------------------------------------------------------


class LiveStateSerializer(serializers.ModelSerializer):
    is_stale = serializers.SerializerMethodField()
    last_updated_at = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = LiveMatch
        fields = ('minute_elapsed', 'minute_display', 'status_code', 'is_stale',
                  'last_updated_at', 'last_source_fetch_at')

    def get_is_stale(self, obj: LiveMatch) -> bool:
        return obj.is_stale()


class MatchListSerializer(serializers.ModelSerializer):
    """List payload. Scores stay null when unknown."""

    id = serializers.IntegerField(source='fotmob_id', read_only=True)
    league = LeagueSerializer(read_only=True)
    home = serializers.SerializerMethodField()
    away = serializers.SerializerMethodField()
    status = serializers.CharField(source='category', read_only=True)
    completion_reason = serializers.CharField(source='status_reason_short', read_only=True)
    live = LiveStateSerializer(read_only=True)
    season_id = serializers.IntegerField(read_only=True, allow_null=True)
    data_updated_at = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Match
        fields = ('id', 'source', 'league', 'season_id', 'round_name', 'kickoff_utc',
                  'status', 'status_code', 'completion_reason', 'home', 'away',
                  'live', 'data_updated_at')

    def get_home(self, obj: Match) -> dict:
        return _side(obj, home=True)

    def get_away(self, obj: Match) -> dict:
        return _side(obj, home=False)


def _side(match: Match, home: bool) -> dict:
    team = match.home_team if home else match.away_team
    return {
        'id': team.fotmob_id,
        'name': team.name,
        'logo_url': team.logo_url,
        'score': match.home_score if home else match.away_score,
        'red_cards': match.home_red_cards if home else match.away_red_cards,
    }


class CoverageFeatureSerializer(serializers.ModelSerializer):
    available = serializers.SerializerMethodField()

    class Meta:
        model = CoverageFeature
        fields = ('feature', 'state', 'available', 'entry_count', 'fetched_at', 'note')

    def get_available(self, obj: CoverageFeature) -> bool:
        return obj.state == CoverageState.AVAILABLE


class MatchCoverageSerializer(serializers.ModelSerializer):
    features = serializers.SerializerMethodField()

    class Meta:
        model = MatchCoverage
        fields = ('source_coverage_level', 'last_fetch_at', 'last_success_at',
                  'last_error', 'features')

    def get_features(self, obj: MatchCoverage) -> dict:
        return {
            item.feature: {
                'available': item.state == CoverageState.AVAILABLE,
                'state': item.state,
                'entry_count': item.entry_count,
                'fetched_at': item.fetched_at,
            }
            for item in obj.features.all()
        }


class MatchDetailSerializer(MatchListSerializer):
    coverage = serializers.SerializerMethodField()
    duration_minutes = serializers.IntegerField(read_only=True)
    has_override = serializers.SerializerMethodField()

    class Meta(MatchListSerializer.Meta):
        fields = MatchListSerializer.Meta.fields + (
            'kickoff_display', 'status_reason_long', 'duration_minutes',
            'provider_league_id', 'provider_primary_league_id',
            'first_half_started_utc', 'second_half_started_utc',
            'last_seen_at', 'source_updated_at', 'coverage', 'has_override',
        )

    def get_coverage(self, obj: Match) -> dict:
        coverage = getattr(obj, 'coverage', None)
        if coverage is None:
            return {
                'source_coverage_level': '',
                'last_fetch_at': None,
                'last_success_at': None,
                'last_error': '',
                'features': {},
            }
        return MatchCoverageSerializer(coverage).data

    def get_has_override(self, obj: Match) -> bool:
        return any(item.is_effective() for item in obj.overrides.all())


class MatchWriteSerializer(serializers.ModelSerializer):
    """Admin create/edit. Provider-owned fields are rejected on update."""

    class Meta:
        model = Match
        fields = (
            'fotmob_id', 'source', 'league', 'season', 'home_team', 'away_team',
            'kickoff_utc', 'kickoff_display', 'category', 'status_code',
            'status_reason_short', 'status_reason_long', 'home_score', 'away_score',
            'home_red_cards', 'away_red_cards', 'duration_minutes', 'round_name',
        )

    def validate(self, attrs: dict) -> dict:
        home = attrs.get('home_team') or getattr(self.instance, 'home_team', None)
        away = attrs.get('away_team') or getattr(self.instance, 'away_team', None)
        if home and away and home == away:
            raise serializers.ValidationError(
                {'away_team': 'A team cannot play itself.'},
            )
        category = attrs.get('category') or getattr(self.instance, 'category', None)
        if category == MatchCategory.UPCOMING and (
            attrs.get('home_score') is not None or attrs.get('away_score') is not None
        ):
            raise serializers.ValidationError(
                {'home_score': 'An upcoming match cannot carry a score.'},
            )
        return attrs

    def validate_source(self, value: str) -> str:
        if self.instance is None and value != SourceChoices.MANUAL:
            raise serializers.ValidationError(
                'Manually created matches must use source="manual".',
            )
        return value

    def create(self, validated_data):
        validated_data.setdefault('source', SourceChoices.MANUAL)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if instance.source == SourceChoices.FOTMOB:
            blocked = [
                name for name in Match.PROVIDER_FIELDS
                if name in validated_data
                and getattr(instance, name) != validated_data[name]
            ]
            if blocked:
                raise serializers.ValidationError({
                    name: 'Provider-managed field; create a MatchOverride instead.'
                    for name in blocked
                })
        return super().update(instance, validated_data)


class MatchOverrideSerializer(serializers.ModelSerializer):
    class Meta:
        model = MatchOverride
        fields = ('id', 'match', 'field_name', 'value', 'reason', 'priority',
                  'is_active', 'expires_at', 'actor', 'created_at')
        read_only_fields = ('actor', 'created_at')

    def validate_field_name(self, value: str) -> str:
        if value not in Match.PROVIDER_FIELDS:
            raise serializers.ValidationError(
                'Overrides only apply to provider-managed fields.',
            )
        return value


# ---------------------------------------------------------------------------
# Coverage child serializers
# ---------------------------------------------------------------------------


class MatchEventSerializer(serializers.ModelSerializer):
    team_id = serializers.IntegerField(source='team.fotmob_id', read_only=True,
                                       allow_null=True)

    class Meta:
        model = MatchEvent
        fields = ('id', 'upstream_event_id', 'event_type', 'is_goal', 'is_home',
                  'team_id', 'player_name', 'assist_player_name', 'minute',
                  'added_time', 'sequence', 'score_home', 'score_away')


class LineupPlayerSerializer(serializers.ModelSerializer):
    class Meta:
        model = LineupPlayer
        fields = ('name', 'shirt_number', 'position', 'is_starter', 'is_captain',
                  'rating', 'minutes_played', 'upstream_player_id')


class MatchLineupSerializer(serializers.ModelSerializer):
    team = TeamSerializer(read_only=True)
    starters = serializers.SerializerMethodField()
    bench = serializers.SerializerMethodField()

    class Meta:
        model = MatchLineup
        fields = ('team', 'is_home', 'formation', 'coach_name', 'starters', 'bench')

    def get_starters(self, obj: MatchLineup) -> list:
        rows = [p for p in obj.players.all() if p.is_starter]
        return LineupPlayerSerializer(rows, many=True).data

    def get_bench(self, obj: MatchLineup) -> list:
        rows = [p for p in obj.players.all() if not p.is_starter]
        return LineupPlayerSerializer(rows, many=True).data


class TeamMatchStatSerializer(serializers.ModelSerializer):
    team_id = serializers.IntegerField(source='team.fotmob_id', read_only=True)

    class Meta:
        model = TeamMatchStat
        fields = ('team_id', 'stat_group', 'stat_key', 'period', 'raw_value',
                  'numeric_value')


class PlayerMatchStatSerializer(serializers.ModelSerializer):
    team_id = serializers.IntegerField(source='team.fotmob_id', read_only=True,
                                       allow_null=True)
    player_id = serializers.IntegerField(source='player.fotmob_id', read_only=True,
                                         allow_null=True)

    class Meta:
        model = PlayerMatchStat
        fields = ('player_id', 'player_name', 'team_id', 'stat_key', 'period',
                  'raw_value', 'numeric_value')


class MatchShotSerializer(serializers.ModelSerializer):
    team_id = serializers.IntegerField(source='team.fotmob_id', read_only=True,
                                       allow_null=True)

    class Meta:
        model = MatchShot
        fields = ('id', 'upstream_shot_id', 'team_id', 'player_name', 'is_home',
                  'minute', 'x', 'y', 'expected_goals', 'expected_goals_on_target',
                  'situation', 'shot_type', 'event_type', 'is_on_target', 'is_blocked')


class MatchMomentumSerializer(serializers.ModelSerializer):
    class Meta:
        model = MatchMomentum
        fields = ('minute', 'value')


class MatchCommentarySerializer(serializers.ModelSerializer):
    class Meta:
        model = MatchCommentary
        fields = ('minute', 'minute_sort', 'text', 'entry_type')


class FeatureEnvelopeSerializer(serializers.Serializer):
    """``{available, state, fetched_at, source_coverage_level, entry_count, data}``."""

    available = serializers.BooleanField()
    state = serializers.CharField()
    fetched_at = serializers.DateTimeField(allow_null=True)
    source_coverage_level = serializers.CharField(allow_blank=True)
    entry_count = serializers.IntegerField()
    data = serializers.JSONField()


# ---------------------------------------------------------------------------
# Ingestion observability
# ---------------------------------------------------------------------------


class IngestionErrorSerializer(serializers.ModelSerializer):
    class Meta:
        model = IngestionError
        fields = ('id', 'category', 'scope', 'external_ref', 'message', 'created_at')


class IngestionRunSerializer(serializers.ModelSerializer):
    duration_seconds = serializers.FloatField(read_only=True, allow_null=True)
    error_count = serializers.SerializerMethodField()

    class Meta:
        model = IngestionRun
        fields = ('id', 'job', 'trigger', 'state', 'correlation_id', 'started_at',
                  'finished_at', 'duration_seconds', 'params', 'counts',
                  'provider_requests', 'provider_errors', 'dry_run', 'notes',
                  'error_count')

    def get_error_count(self, obj: IngestionRun) -> int:
        return obj.errors.count()


class IngestionRunDetailSerializer(IngestionRunSerializer):
    errors = IngestionErrorSerializer(many=True, read_only=True)

    class Meta(IngestionRunSerializer.Meta):
        fields = IngestionRunSerializer.Meta.fields + ('errors',)


class SyncJobRequestSerializer(serializers.Serializer):
    """Staff-only ingestion trigger. Cron does not use this path.

    Every parameter is bounded, so a stray request cannot ask for a
    thousand-day window or an unlimited coverage sweep.
    """

    job = serializers.ChoiceField(choices=[
        'sync_fotmob_fixtures', 'sync_fotmob_live', 'sync_fotmob_coverage',
        'daily_fixture_reconcile', 'weekly_backfill',
    ])
    #: Safe by default: a request must opt in to writing.
    dry_run = serializers.BooleanField(required=False, default=True)

    # Date window (fixtures, reconcile, backfill)
    past_days = serializers.IntegerField(required=False, min_value=0, max_value=60)
    future_days = serializers.IntegerField(required=False, min_value=0, max_value=60)
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)

    # Live and coverage
    days_back = serializers.IntegerField(required=False, min_value=0, max_value=14)
    details_for = serializers.ChoiceField(choices=('live', 'all'), required=False)
    match_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False,
        max_length=50, allow_empty=False,
    )

    league_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False,
        max_length=50, allow_empty=False,
    )
    limit = serializers.IntegerField(required=False, min_value=1, max_value=500)
    store_raw = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs: dict) -> dict:
        start, end = attrs.get('start_date'), attrs.get('end_date')
        if bool(start) != bool(end):
            raise serializers.ValidationError(
                {'end_date': 'Provide both start_date and end_date, or neither.'},
            )
        if start and end:
            if end < start:
                raise serializers.ValidationError(
                    {'end_date': 'end_date must be on or after start_date.'},
                )
            if (end - start).days > 120:
                raise serializers.ValidationError(
                    {'end_date': 'A single run may cover at most 121 days.'},
                )
        if attrs['job'] != 'sync_fotmob_coverage' and attrs.get('match_ids'):
            raise serializers.ValidationError(
                {'match_ids': 'match_ids only applies to sync_fotmob_coverage.'},
            )
        return attrs
