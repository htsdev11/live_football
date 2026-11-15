"""All API endpoints, implemented as ``rest_framework.views.APIView`` classes.

Rules applied throughout:

* Explicit ``get``/``post``/``put``/``patch``/``delete`` methods.
* Query parameters validated by serializers from ``api/serializers.py``.
* Lists paginated explicitly with a pagination object from ``api/pagination.py``.
* No endpoint performs an upstream FotMob request; reads come from the database
  written by the scheduled ingestion commands.
* Token authentication and ``IsAuthenticated`` by default; the token issuance
  endpoint is the only anonymous route and it is throttled.
"""

from __future__ import annotations

from datetime import datetime

from django.db.models import Count, F, Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from api import service
from api.exceptions import request_id_for
from api.pagination import SmallPagination, StandardPagination
from api.permissions import IsAdministrator, IsStaffEditor, IsStaffOnly
from api.serializers import (
    CoverageFeatureSerializer,
    CurrentUserSerializer,
    IngestionRunDetailSerializer,
    IngestionRunSerializer,
    LeagueSerializer,
    LeagueWriteSerializer,
    MatchCommentarySerializer,
    MatchCoverageSerializer,
    MatchDetailSerializer,
    MatchEventSerializer,
    MatchFilterSerializer,
    MatchLineupSerializer,
    MatchListSerializer,
    MatchMomentumSerializer,
    MatchOverrideSerializer,
    MatchShotSerializer,
    MatchWriteSerializer,
    PlayerMatchStatSerializer,
    PlayerSerializer,
    SeasonSerializer,
    SeasonWriteSerializer,
    StandingSerializer,
    SyncJobRequestSerializer,
    SyncRunFilterSerializer,
    TeamMatchStatSerializer,
    TeamSerializer,
    TeamWriteSerializer,
    TimezoneQuerySerializer,
    TokenRequestSerializer,
)
from core.models import (
    CoverageFeatureChoices,
    IngestionTrigger,
    League,
    LineupPlayer,
    Match,
    MatchCategory,
    Player,
    Season,
    Standing,
    Team,
)


class BaseAPIView(APIView):
    """Shared defaults: token auth, authenticated reads, explicit pagination."""

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]
    pagination_class = StandardPagination

    @property
    def request_id(self) -> str:
        return request_id_for(self.request)

    def paginated(self, queryset, serializer_class, **serializer_kwargs) -> Response:
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, self.request, view=self)
        serializer = serializer_class(page, many=True, **serializer_kwargs)
        return paginator.get_paginated_response(serializer.data)

    def match_filters(self, overrides: dict | None = None) -> dict:
        serializer = MatchFilterSerializer(data=self.request.query_params)
        serializer.is_valid(raise_exception=True)
        filters = dict(serializer.validated_data)
        if overrides:
            filters.update(overrides)
        return filters

    def get_match_or_404(self, fotmob_id: int) -> Match:
        match = (
            Match.objects.visible()
            .with_relations()
            .prefetch_related('overrides', 'coverage__features')
            .filter(fotmob_id=fotmob_id)
            .first()
        )
        if match is None:
            self.raise_not_found('match', fotmob_id)
        return match

    def raise_not_found(self, resource: str, identifier) -> None:
        from rest_framework.exceptions import NotFound

        raise NotFound(f'No {resource} with id {identifier}.')


# ---------------------------------------------------------------------------
# 5.1 Authentication and session identity
# ---------------------------------------------------------------------------


class AuthTokenAPIView(APIView):
    """POST /api/auth/token/ - exchange credentials for a DRF token."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_scope = 'auth_token'

    def post(self, request):
        serializer = TokenRequestSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']
        token, _ = Token.objects.get_or_create(user=user)
        return Response(
            {
                'token': token.key,
                'user': CurrentUserSerializer(user).data,
                'issued_at': timezone.now(),
            },
            status=http_status.HTTP_200_OK,
        )


class AuthLogoutAPIView(BaseAPIView):
    """POST /api/auth/logout/ - revoke the token used for this request."""

    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)


class AuthMeAPIView(BaseAPIView):
    """GET /api/auth/me/ - identity, roles and allowed actions."""

    def get(self, request):
        return Response(CurrentUserSerializer(request.user).data)


class AuthTokenRotateAPIView(BaseAPIView):
    """POST /api/auth/token/rotate/ - replace the caller's token."""

    throttle_scope = 'auth_token'

    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        token = Token.objects.create(user=request.user)
        return Response({'token': token.key, 'issued_at': timezone.now()})


# ---------------------------------------------------------------------------
# 5.2 Matches
# ---------------------------------------------------------------------------


class MatchListAPIView(BaseAPIView):
    """GET /api/matches/ (filter/search/sort/paginate) and POST for admins."""

    permission_classes = [IsStaffEditor]
    throttle_scope = 'search'

    def get(self, request):
        queryset = service.build_match_queryset(self.match_filters())
        return self.paginated(queryset, MatchListSerializer)

    def post(self, request):
        serializer = MatchWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        match = serializer.save()
        service.write_audit(
            request.user, 'create', match,
            changes=serializer.validated_data,
            reason=str(request.data.get('reason', '')),
            request_id=self.request_id,
        )
        match = self.get_match_or_404(match.fotmob_id)
        return Response(
            MatchDetailSerializer(match).data, status=http_status.HTTP_201_CREATED,
        )


class MatchDetailAPIView(BaseAPIView):
    """GET/PUT/PATCH/DELETE /api/matches/{id}/ by FotMob match ID."""

    permission_classes = [IsStaffEditor]

    def get(self, request, fotmob_id: int):
        match = self.get_match_or_404(fotmob_id)
        data = MatchDetailSerializer(match).data
        data['data_updated_at'] = service.match_data_updated_at(match)
        return Response(data)

    def put(self, request, fotmob_id: int):
        return self._update(request, fotmob_id, partial=False)

    def patch(self, request, fotmob_id: int):
        return self._update(request, fotmob_id, partial=True)

    def _update(self, request, fotmob_id: int, partial: bool) -> Response:
        match = self.get_match_or_404(fotmob_id)
        serializer = MatchWriteSerializer(match, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        service.write_audit(
            request.user, 'update', updated,
            changes={k: str(v) for k, v in serializer.validated_data.items()},
            reason=str(request.data.get('reason', '')),
            request_id=self.request_id,
        )
        return Response(MatchDetailSerializer(self.get_match_or_404(fotmob_id)).data)

    def delete(self, request, fotmob_id: int):
        """Soft delete: the row is archived and the audit trail is retained."""
        match = self.get_match_or_404(fotmob_id)
        if match.is_archived:
            return Response(status=http_status.HTTP_204_NO_CONTENT)
        match.is_archived = True
        match.archived_at = timezone.now()
        match.save(update_fields=['is_archived', 'archived_at', 'updated_at'])
        service.write_audit(
            request.user, 'archive', match,
            changes={'is_archived': True},
            reason=str(request.data.get('reason', '')) if hasattr(request, 'data') else '',
            request_id=self.request_id,
        )
        return Response(status=http_status.HTTP_204_NO_CONTENT)


class _CategoryListAPIView(BaseAPIView):
    """Shared list view for the category-specific match endpoints."""

    category: str = ''
    default_ordering: str = 'kickoff_utc'
    throttle_scope = 'search'

    def get(self, request):
        filters = self.match_filters()
        filters.setdefault('ordering', self.default_ordering)
        filters['status'] = self.category
        queryset = service.build_match_queryset(filters)
        return self.paginated(queryset, MatchListSerializer)


class MatchLiveAPIView(_CategoryListAPIView):
    """GET /api/matches/live/ - in-progress matches with freshness flags."""

    category = MatchCategory.LIVE

    def get(self, request):
        response = super().get(request)
        response.data['stale_projections'] = service.stale_live_count()
        response.data['generated_at'] = timezone.now()
        return response


class MatchCompletedAPIView(_CategoryListAPIView):
    category = MatchCategory.COMPLETED
    default_ordering = '-kickoff_utc'


class MatchUpcomingAPIView(_CategoryListAPIView):
    """Upcoming only: live and disrupted fixtures are excluded by the filter."""

    category = MatchCategory.UPCOMING


class MatchCancelledAPIView(_CategoryListAPIView):
    """Postponed / cancelled / abandoned / interrupted, filterable by status_code."""

    category = MatchCategory.DISRUPTED
    default_ordering = '-kickoff_utc'


class MatchByDateAPIView(BaseAPIView):
    """GET /api/matches/by-date/{date}/ - one UTC day, optional display timezone."""

    throttle_scope = 'search'

    def get(self, request, match_date):
        from rest_framework.exceptions import ValidationError

        tz_serializer = TimezoneQuerySerializer(data=request.query_params)
        tz_serializer.is_valid(raise_exception=True)
        try:
            parsed_date = datetime.strptime(match_date, '%Y-%m-%d').date()
        except ValueError:
            raise ValidationError({'date': 'Use YYYY-MM-DD.'})
        filters = self.match_filters({'date': parsed_date})
        queryset = service.build_match_queryset(filters)
        response = self.paginated(queryset, MatchListSerializer)
        response.data['date'] = match_date
        response.data['timezone'] = tz_serializer.validated_data.get('timezone', 'UTC')
        return response


class MatchEventsAPIView(BaseAPIView):
    """GET /api/matches/{id}/events/ - chronological timeline."""

    pagination_class = SmallPagination

    def get(self, request, fotmob_id: int):
        match = self.get_match_or_404(fotmob_id)
        rows = list(match.events.select_related('team').all())
        payload = service.feature_payload(
            match, CoverageFeatureChoices.EVENTS, rows,
            MatchEventSerializer(rows, many=True).data,
        )
        return Response(payload)


class MatchGoalsAPIView(BaseAPIView):
    """GET /api/matches/{id}/goals/ - scorer, assist and minute."""

    def get(self, request, fotmob_id: int):
        match = self.get_match_or_404(fotmob_id)
        rows = list(match.events.filter(is_goal=True).select_related('team'))
        payload = service.feature_payload(
            match, CoverageFeatureChoices.GOALS, rows,
            MatchEventSerializer(rows, many=True).data,
        )
        return Response(payload)


class MatchLineupsAPIView(BaseAPIView):
    """GET /api/matches/{id}/lineups/ - starters, bench, coach, formation."""

    def get(self, request, fotmob_id: int):
        match = self.get_match_or_404(fotmob_id)
        rows = list(
            match.lineups.select_related('team').prefetch_related(
                Prefetch('players', queryset=LineupPlayer.objects.all()),
            )
        )
        payload = service.feature_payload(
            match, CoverageFeatureChoices.LINEUPS, rows,
            MatchLineupSerializer(rows, many=True).data,
        )
        return Response(payload)


class MatchStatsAPIView(BaseAPIView):
    """GET /api/matches/{id}/stats/ - team and period statistics."""

    def get(self, request, fotmob_id: int):
        match = self.get_match_or_404(fotmob_id)
        rows = list(match.team_stats.select_related('team').all())
        payload = service.feature_payload(
            match, CoverageFeatureChoices.STATS, rows,
            TeamMatchStatSerializer(rows, many=True).data,
        )
        return Response(payload)


class MatchPlayerStatsAPIView(BaseAPIView):
    """GET /api/matches/{id}/player-stats/ - per-player match statistics.

    Backed by FotMob's ``playerStats`` block: ratings, minutes, passing,
    duels, xG and the rest, one row per player and stat key. Only players the
    source reported stats for appear.
    """

    pagination_class = SmallPagination

    def get(self, request, fotmob_id: int):
        match = self.get_match_or_404(fotmob_id)
        rows = list(
            match.player_stats.select_related('team', 'player').all()
        )
        player_filter = (request.query_params.get('player_id') or '').strip()
        if player_filter.isdigit():
            rows = [row for row in rows
                    if row.player and row.player.fotmob_id == int(player_filter)]
        payload = service.feature_payload(
            match, CoverageFeatureChoices.PLAYER_STATS, rows,
            PlayerMatchStatSerializer(rows, many=True).data,
        )
        return Response(payload)


class MatchShotsAPIView(BaseAPIView):
    """GET /api/matches/{id}/shots/ - shotmap where available."""

    pagination_class = SmallPagination

    def get(self, request, fotmob_id: int):
        match = self.get_match_or_404(fotmob_id)
        rows = list(match.shots.select_related('team').all())
        payload = service.feature_payload(
            match, CoverageFeatureChoices.SHOTS, rows,
            MatchShotSerializer(rows, many=True).data,
        )
        return Response(payload)


class MatchMomentumAPIView(BaseAPIView):
    """GET /api/matches/{id}/momentum/ - momentum points if the source has them."""

    def get(self, request, fotmob_id: int):
        match = self.get_match_or_404(fotmob_id)
        rows = list(match.momentum.all())
        payload = service.feature_payload(
            match, CoverageFeatureChoices.MOMENTUM, rows,
            MatchMomentumSerializer(rows, many=True).data,
        )
        return Response(payload)


class MatchCommentaryAPIView(BaseAPIView):
    """GET /api/matches/{id}/commentary/ - entries only when genuinely present."""

    pagination_class = SmallPagination

    def get(self, request, fotmob_id: int):
        match = self.get_match_or_404(fotmob_id)
        rows = list(match.commentary.all())
        payload = service.feature_payload(
            match, CoverageFeatureChoices.COMMENTARY, rows,
            MatchCommentarySerializer(rows, many=True).data,
        )
        return Response(payload)


class MatchCoverageAPIView(BaseAPIView):
    """GET /api/matches/{id}/coverage/ - per-feature availability summary."""

    def get(self, request, fotmob_id: int):
        match = self.get_match_or_404(fotmob_id)
        coverage = service.coverage_for(match)
        if coverage is None:
            return Response({
                'match_id': match.fotmob_id,
                'source_coverage_level': '',
                'last_fetch_at': None,
                'last_success_at': None,
                'last_error': '',
                'features': {
                    feature: {'available': False, 'state': 'not_fetched',
                              'entry_count': 0, 'fetched_at': None}
                    for feature, _ in CoverageFeatureChoices.choices
                },
            })
        data = MatchCoverageSerializer(coverage).data
        data['match_id'] = match.fotmob_id
        data['feature_rows'] = CoverageFeatureSerializer(
            coverage.features.all(), many=True,
        ).data
        return Response(data)


class MatchOverrideAPIView(BaseAPIView):
    """GET/POST /api/matches/{id}/overrides/ - approved manual corrections."""

    permission_classes = [IsAdministrator]

    def get(self, request, fotmob_id: int):
        match = self.get_match_or_404(fotmob_id)
        rows = match.overrides.all()
        return self.paginated(rows, MatchOverrideSerializer)

    def post(self, request, fotmob_id: int):
        match = self.get_match_or_404(fotmob_id)
        payload = dict(request.data)
        payload['match'] = match.pk
        serializer = MatchOverrideSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        override = serializer.save(actor=request.user)
        service.write_audit(
            request.user, 'override', override,
            changes={'field_name': override.field_name, 'value': override.value},
            reason=override.reason, request_id=self.request_id,
        )
        return Response(
            MatchOverrideSerializer(override).data, status=http_status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# 5.3 Leagues, teams, seasons, players
# ---------------------------------------------------------------------------


class LeagueListAPIView(BaseAPIView):
    permission_classes = [IsStaffEditor]
    throttle_scope = 'search'

    def get(self, request):
        queryset = League.objects.all()
        search = (request.query_params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(country_code__iexact=search),
            )
        country = (request.query_params.get('country') or '').strip()
        if country:
            queryset = queryset.filter(country_code__iexact=country)
        return self.paginated(queryset, LeagueSerializer)

    def post(self, request):
        serializer = LeagueWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        league = serializer.save()
        service.write_audit(request.user, 'create', league,
                            changes=serializer.validated_data,
                            request_id=self.request_id)
        return Response(LeagueSerializer(league).data,
                        status=http_status.HTTP_201_CREATED)


class LeagueDetailAPIView(BaseAPIView):
    permission_classes = [IsStaffEditor]

    def get_league(self, fotmob_id: int) -> League:
        return get_object_or_404(League, fotmob_id=fotmob_id)

    def get(self, request, fotmob_id: int):
        return Response(LeagueSerializer(self.get_league(fotmob_id)).data)

    def put(self, request, fotmob_id: int):
        return self._update(request, fotmob_id, partial=False)

    def patch(self, request, fotmob_id: int):
        return self._update(request, fotmob_id, partial=True)

    def _update(self, request, fotmob_id: int, partial: bool) -> Response:
        league = self.get_league(fotmob_id)
        serializer = LeagueWriteSerializer(league, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        service.write_audit(request.user, 'update', updated,
                            changes={k: str(v) for k, v in serializer.validated_data.items()},
                            request_id=self.request_id)
        return Response(LeagueSerializer(updated).data)

    def delete(self, request, fotmob_id: int):
        league = self.get_league(fotmob_id)
        league.is_active = False
        league.save(update_fields=['is_active', 'updated_at'])
        service.write_audit(request.user, 'deactivate', league,
                            changes={'is_active': False}, request_id=self.request_id)
        return Response(status=http_status.HTTP_204_NO_CONTENT)


class LeagueMatchesAPIView(BaseAPIView):
    throttle_scope = 'search'

    def get(self, request, fotmob_id: int):
        get_object_or_404(League, fotmob_id=fotmob_id)
        filters = self.match_filters({'league_id': fotmob_id})
        return self.paginated(service.build_match_queryset(filters), MatchListSerializer)


class LeagueStandingsAPIView(BaseAPIView):
    def get(self, request, fotmob_id: int):
        league = get_object_or_404(League, fotmob_id=fotmob_id)
        queryset = Standing.objects.filter(league=league).select_related('team')
        season_id = request.query_params.get('season_id')
        if season_id and season_id.isdigit():
            queryset = queryset.filter(season_id=int(season_id))
        return self.paginated(queryset, StandingSerializer)


class TeamListAPIView(BaseAPIView):
    permission_classes = [IsStaffEditor]
    throttle_scope = 'search'

    def get(self, request):
        queryset = Team.objects.all()
        search = (request.query_params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(name__icontains=search)
        return self.paginated(queryset, TeamSerializer)

    def post(self, request):
        serializer = TeamWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        team = serializer.save()
        service.write_audit(request.user, 'create', team,
                            changes=serializer.validated_data,
                            request_id=self.request_id)
        return Response(TeamSerializer(team).data, status=http_status.HTTP_201_CREATED)


class TeamDetailAPIView(BaseAPIView):
    permission_classes = [IsStaffEditor]

    def get_team(self, fotmob_id: int) -> Team:
        return get_object_or_404(Team, fotmob_id=fotmob_id)

    def get(self, request, fotmob_id: int):
        return Response(TeamSerializer(self.get_team(fotmob_id)).data)

    def put(self, request, fotmob_id: int):
        return self._update(request, fotmob_id, partial=False)

    def patch(self, request, fotmob_id: int):
        return self._update(request, fotmob_id, partial=True)

    def _update(self, request, fotmob_id: int, partial: bool) -> Response:
        team = self.get_team(fotmob_id)
        serializer = TeamWriteSerializer(team, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        service.write_audit(request.user, 'update', updated,
                            changes={k: str(v) for k, v in serializer.validated_data.items()},
                            request_id=self.request_id)
        return Response(TeamSerializer(updated).data)

    def delete(self, request, fotmob_id: int):
        """Teams are referenced by matches, so deletion is refused when in use."""
        team = self.get_team(fotmob_id)
        in_use = Match.objects.filter(
            Q(home_team=team) | Q(away_team=team),
        ).exists()
        if in_use:
            return Response(
                {
                    'code': 'conflict',
                    'message': 'Team is referenced by stored matches and cannot be deleted.',
                    'details': {},
                    'request_id': self.request_id,
                },
                status=http_status.HTTP_409_CONFLICT,
            )
        service.write_audit(request.user, 'delete', team, request_id=self.request_id)
        team.delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)


class TeamMatchesAPIView(BaseAPIView):
    throttle_scope = 'search'

    def get(self, request, fotmob_id: int):
        get_object_or_404(Team, fotmob_id=fotmob_id)
        filters = self.match_filters({'team_id': fotmob_id})
        return self.paginated(service.build_match_queryset(filters), MatchListSerializer)


class TeamPlayersAPIView(BaseAPIView):
    def get(self, request, fotmob_id: int):
        team = get_object_or_404(Team, fotmob_id=fotmob_id)
        players = Player.objects.filter(
            team_links__team=team, team_links__is_active=True,
        ).distinct()
        return self.paginated(players, PlayerSerializer)


class SeasonListAPIView(BaseAPIView):
    permission_classes = [IsStaffEditor]

    def get(self, request):
        queryset = Season.objects.select_related('league')
        league_id = request.query_params.get('league_id')
        if league_id and league_id.isdigit():
            queryset = queryset.filter(league__fotmob_id=int(league_id))
        return self.paginated(queryset, SeasonSerializer)

    def post(self, request):
        serializer = SeasonWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        season = serializer.save()
        service.write_audit(request.user, 'create', season,
                            changes={k: str(v) for k, v in serializer.validated_data.items()},
                            request_id=self.request_id)
        return Response(SeasonSerializer(season).data,
                        status=http_status.HTTP_201_CREATED)


class SeasonDetailAPIView(BaseAPIView):
    permission_classes = [IsStaffEditor]

    def get(self, request, pk: int):
        season = get_object_or_404(Season.objects.select_related('league'), pk=pk)
        return Response(SeasonSerializer(season).data)

    def put(self, request, pk: int):
        return self._update(request, pk, partial=False)

    def patch(self, request, pk: int):
        return self._update(request, pk, partial=True)

    def _update(self, request, pk: int, partial: bool) -> Response:
        season = get_object_or_404(Season, pk=pk)
        serializer = SeasonWriteSerializer(season, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        service.write_audit(request.user, 'update', updated,
                            changes={k: str(v) for k, v in serializer.validated_data.items()},
                            request_id=self.request_id)
        return Response(SeasonSerializer(updated).data)

    def delete(self, request, pk: int):
        season = get_object_or_404(Season, pk=pk)
        if Match.objects.filter(season=season).exists():
            return Response(
                {
                    'code': 'conflict',
                    'message': 'Season has stored matches; detach them first.',
                    'details': {},
                    'request_id': self.request_id,
                },
                status=http_status.HTTP_409_CONFLICT,
            )
        service.write_audit(request.user, 'delete', season, request_id=self.request_id)
        season.delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)


class SeasonMatchesAPIView(BaseAPIView):
    throttle_scope = 'search'

    def get(self, request, pk: int):
        get_object_or_404(Season, pk=pk)
        filters = self.match_filters({'season_id': pk})
        return self.paginated(service.build_match_queryset(filters), MatchListSerializer)


class PlayerListAPIView(BaseAPIView):
    throttle_scope = 'search'

    def get(self, request):
        queryset = Player.objects.all()
        search = (request.query_params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(name__icontains=search)
        return self.paginated(queryset, PlayerSerializer)


class PlayerDetailAPIView(BaseAPIView):
    def get(self, request, pk: int):
        return Response(PlayerSerializer(get_object_or_404(Player, pk=pk)).data)


class PlayerMatchesAPIView(BaseAPIView):
    """Matches where this player appears in a stored lineup."""

    def get(self, request, pk: int):
        player = get_object_or_404(Player, pk=pk)
        queryset = service.base_match_queryset().filter(
            Q(lineups__players__player=player) | Q(events__player=player),
        ).distinct().order_by('-kickoff_utc', '-id')
        return self.paginated(queryset, MatchListSerializer)


# ---------------------------------------------------------------------------
# 5.4 Internal ingestion observability
# ---------------------------------------------------------------------------


class SyncRunListAPIView(BaseAPIView):
    """GET /api/internal/sync-runs/ - staff-only job history."""

    permission_classes = [IsStaffOnly]
    throttle_scope = 'internal'

    def get(self, request):
        filters = SyncRunFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        queryset = service.sync_run_queryset().annotate(error_total=Count('errors'))
        if filters.validated_data.get('job'):
            queryset = queryset.filter(job=filters.validated_data['job'])
        if filters.validated_data.get('state'):
            queryset = queryset.filter(state=filters.validated_data['state'])
        response = self.paginated(queryset, IngestionRunSerializer)
        response.data['health'] = service.ingestion_health()
        return response


class SyncRunDetailAPIView(BaseAPIView):
    permission_classes = [IsStaffOnly]
    throttle_scope = 'internal'

    def get(self, request, pk: int):
        run = get_object_or_404(service.sync_run_queryset(), pk=pk)
        return Response(IngestionRunDetailSerializer(run).data)


class SyncJobTriggerAPIView(BaseAPIView):
    """POST /api/internal/sync-jobs/ - staff-only ingestion trigger.

    The job runs in a worker thread, so the response returns immediately with
    202: upstream fetching never happens inside the response path. Progress and
    results are read from `/api/internal/sync-runs/`. Cron does not use this
    route; it runs the management commands against the ORM directly.
    """

    permission_classes = [IsStaffOnly]
    throttle_scope = 'internal'

    def post(self, request):
        serializer = SyncJobRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = dict(serializer.validated_data)
        job = payload.pop('job')

        if service.job_in_progress(job):
            return Response(
                {
                    'code': 'conflict',
                    'message': f'{job} is already running; wait for it to finish.',
                    'details': {'job': job},
                    'request_id': self.request_id,
                },
                status=http_status.HTTP_409_CONFLICT,
            )

        worker = service.dispatch_job(job, payload, trigger=IngestionTrigger.API)
        service.write_audit(
            request.user, 'trigger_sync', request.user,
            changes={'job': job, 'params': payload},
            request_id=self.request_id,
        )
        return Response(
            {
                'accepted': True,
                'job': job,
                'params': payload,
                'worker': worker,
                'dry_run': payload.get('dry_run', True),
                'status_url': f'/api/internal/sync-runs/?job={job}',
                'request_id': self.request_id,
                'detail': (
                    'Started in a background worker. Poll status_url for the run '
                    'record: counts, provider requests and errors appear once it '
                    'finishes. Equivalent CLI: '
                    f'`manage.py {job}`.'
                ),
            },
            status=http_status.HTTP_202_ACCEPTED,
        )


class IngestionHealthAPIView(BaseAPIView):
    """GET /api/internal/health/ - last success, counts and stale live data."""

    permission_classes = [IsStaffOnly]
    throttle_scope = 'internal'

    def get(self, request):
        return Response({
            'generated_at': timezone.now(),
            'jobs': service.ingestion_health(),
            'stale_live_projections': service.stale_live_count(),
            'matches_total': Match.objects.visible().count(),
            'matches_by_category': {
                row['category']: row['total']
                for row in Match.objects.visible().values('category').annotate(
                    total=Count('id'),
                )
            },
        })
