"""Django admin for the football domain.

Conventions used throughout:

* Provider-owned fields are read-only. Corrections go through ``MatchOverride``,
  so a cron sync cannot silently undo an edit made here.
* Every list view declares ``list_select_related`` (and annotates counts where
  it shows them), because these tables run to tens of thousands of rows.
* Status, coverage state and ingestion state render as colored badges that work
  against both the light and dark admin themes.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.db.models import Avg, Count, Q
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.timesince import timesince
from django.utils.timezone import now

from datetime import timedelta

from core.models import (
    AuditLog,
    CoverageFeature,
    CoverageState,
    IngestionError,
    IngestionRun,
    IngestionState,
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
    MatchRawSnapshot,
    MatchShot,
    Player,
    PlayerMatchStat,
    Season,
    Standing,
    Team,
    TeamMatchStat,
    TeamPlayer,
    Transfer,
)

admin.site.site_header = 'Football Data Platform'
admin.site.site_title = 'Football admin'
admin.site.index_title = 'FotMob ingestion and match data'


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

#: Badge colors chosen to stay legible on the light and dark admin themes.
CATEGORY_COLORS = {
    MatchCategory.LIVE: '#c62828',
    MatchCategory.COMPLETED: '#2e7d32',
    MatchCategory.UPCOMING: '#1565c0',
    MatchCategory.DISRUPTED: '#ef6c00',
}

STATE_COLORS = {
    IngestionState.SUCCESS: '#2e7d32',
    IngestionState.PARTIAL: '#ef6c00',
    IngestionState.FAILED: '#c62828',
    IngestionState.RUNNING: '#1565c0',
}

COVERAGE_COLORS = {
    CoverageState.AVAILABLE: '#2e7d32',
    CoverageState.EMPTY: '#757575',
    CoverageState.UNAVAILABLE: '#455a64',
    CoverageState.FAILED: '#c62828',
    CoverageState.NOT_FETCHED: '#9e9e9e',
}


def badge(label: str, color: str, title: str = '') -> str:
    return format_html(
        '<span title="{}" style="background:{};color:#fff;padding:2px 8px;'
        'border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap;">'
        '{}</span>',
        title or label, color, label,
    )


def muted(text: str) -> str:
    return format_html('<span style="opacity:.5;">{}</span>', text)


def admin_link(instance, label: str | None = None) -> str:
    """A link to another object's change page, or plain text when it has none."""
    if instance is None:
        return '—'
    url = reverse(
        f'admin:{instance._meta.app_label}_{instance._meta.model_name}_change',
        args=[instance.pk],
    )
    return format_html('<a href="{}">{}</a>', url, label or str(instance))


def changelist_link(model, label: str, **filters) -> str:
    url = reverse(f'admin:{model._meta.app_label}_{model._meta.model_name}_changelist')
    query = '&'.join(f'{key}={value}' for key, value in filters.items())
    return format_html('<a href="{}?{}">{}</a>', url, query, label)


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


@admin.register(League)
class LeagueAdmin(admin.ModelAdmin):
    list_display = ('name', 'fotmob_id', 'country_code', 'season_count',
                    'match_count', 'is_active', 'last_seen')
    list_filter = ('is_active', 'country_code', 'league_type')
    search_fields = ('name', 'fotmob_id', 'country_code')
    ordering = ('name',)
    list_per_page = 50
    readonly_fields = ('fotmob_id', 'parent_fotmob_id', 'last_seen_at',
                       'created_at', 'updated_at', 'logo_preview')
    fieldsets = (
        ('Identity', {'fields': ('fotmob_id', 'parent_fotmob_id', 'name',
                                 'country_code', 'league_type')}),
        ('Media', {'fields': ('logo_url', 'logo_preview')}),
        ('Status', {'fields': ('is_active', 'last_seen_at', 'created_at',
                               'updated_at')}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _matches=Count('matches', distinct=True),
            _seasons=Count('seasons', distinct=True),
        )

    @admin.display(description='Seasons', ordering='_seasons')
    def season_count(self, obj):
        return obj._seasons

    @admin.display(description='Matches', ordering='_matches')
    def match_count(self, obj):
        if not obj._matches:
            return '—'
        return changelist_link(Match, f'{obj._matches:,}', league__id__exact=obj.pk)

    @admin.display(description='Last seen', ordering='last_seen_at')
    def last_seen(self, obj):
        return f'{timesince(obj.last_seen_at)} ago' if obj.last_seen_at else '—'

    @admin.display(description='Logo')
    def logo_preview(self, obj):
        if not obj.logo_url:
            return '—'
        return format_html('<img src="{}" style="height:48px;">', obj.logo_url)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('crest', 'name', 'fotmob_id', 'country_code', 'squad_size',
                    'match_count', 'last_seen')
    list_display_links = ('crest', 'name')
    list_filter = ('country_code',)
    search_fields = ('name', 'short_name', 'fotmob_id')
    ordering = ('name',)
    list_per_page = 50
    readonly_fields = ('fotmob_id', 'last_seen_at', 'created_at', 'updated_at')

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _squad=Count('squad', distinct=True),
            _matches=Count('home_matches', distinct=True) + Count(
                'away_matches', distinct=True),
        )

    @admin.display(description='')
    def crest(self, obj):
        if not obj.logo_url:
            return ''
        return format_html('<img src="{}" style="height:22px;width:22px;'
                           'object-fit:contain;">', obj.logo_url)

    @admin.display(description='Squad', ordering='_squad')
    def squad_size(self, obj):
        if not obj._squad:
            return '—'
        return changelist_link(TeamPlayer, obj._squad, team__id__exact=obj.pk)

    @admin.display(description='Matches', ordering='_matches')
    def match_count(self, obj):
        return f'{obj._matches:,}' if obj._matches else '—'

    @admin.display(description='Last seen', ordering='last_seen_at')
    def last_seen(self, obj):
        return f'{timesince(obj.last_seen_at)} ago' if obj.last_seen_at else '—'


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):
    list_display = ('year_name', 'league', 'is_current', 'match_count')
    list_filter = ('is_current', 'league__country_code')
    search_fields = ('league__name', 'year_name')
    autocomplete_fields = ('league',)
    list_select_related = ('league',)
    ordering = ('-year_name',)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_matches=Count('matches'))

    @admin.display(description='Matches', ordering='_matches')
    def match_count(self, obj):
        return obj._matches or '—'


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    """Players exist only because coverage ingestion saw them in a lineup."""

    list_display = ('name', 'fotmob_id', 'position', 'country_code', 'teams',
                    'appearances', 'average_rating', 'stat_rows')
    list_filter = ('position', 'country_code')
    search_fields = ('name', 'fotmob_id')
    ordering = ('name',)
    list_per_page = 50
    readonly_fields = ('fotmob_id', 'created_at', 'updated_at', 'recent_appearances')
    fieldsets = (
        (None, {'fields': ('fotmob_id', 'name', 'position', 'country_code')}),
        ('Appearances', {'fields': ('recent_appearances',)}),
        ('Timestamps', {'fields': ('created_at', 'updated_at'),
                        'classes': ('collapse',)}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related(
            'team_links__team',
        ).annotate(
            _apps=Count('lineup_entries', distinct=True),
            _stats=Count('match_stats', distinct=True),
            _rating=Avg('lineup_entries__rating'),
        )

    @admin.display(description='Teams')
    def teams(self, obj):
        # Reads the prefetched links; slicing here would re-query per row.
        names = [link.team.name for link in obj.team_links.all()][:3]
        return ', '.join(names) if names else '—'

    @admin.display(description='Apps', ordering='_apps')
    def appearances(self, obj):
        return obj._apps or '—'

    @admin.display(description='Avg rating', ordering='_rating')
    def average_rating(self, obj):
        if obj._rating is None:
            return '—'
        value = round(float(obj._rating), 2)
        color = '#2e7d32' if value >= 7 else '#ef6c00' if value >= 6 else '#c62828'
        return badge(f'{value:.2f}', color)

    @admin.display(description='Stat rows', ordering='_stats')
    def stat_rows(self, obj):
        if not obj._stats:
            return '—'
        return changelist_link(PlayerMatchStat, obj._stats, player__id__exact=obj.pk)

    @admin.display(description='Recent appearances')
    def recent_appearances(self, obj):
        rows = (
            LineupPlayer.objects
            .filter(player=obj)
            .select_related('lineup__match__home_team', 'lineup__match__away_team',
                            'lineup__team')
            .order_by('-lineup__match__kickoff_utc')[:10]
        )
        if not rows:
            return 'No ingested lineups yet. Run sync_fotmob_coverage for a match.'
        return format_html_join(
            '', '<div style="padding:2px 0;">{} — {} v {} · {} · rating {}</div>',
            (
                (row.lineup.team.name,
                 row.lineup.match.home_team.name,
                 row.lineup.match.away_team.name,
                 'starter' if row.is_starter else 'bench',
                 row.rating if row.rating is not None else '—')
                for row in rows
            ),
        )


@admin.register(TeamPlayer)
class TeamPlayerAdmin(admin.ModelAdmin):
    """Squad links, created whenever a player appears in a team's lineup."""

    list_display = ('player_name', 'team_name', 'shirt', 'position', 'appearances',
                    'is_active', 'last_updated')
    list_display_links = ('player_name',)
    list_filter = ('is_active', 'player__position', 'team__country_code')
    search_fields = ('player__name', 'team__name', 'player__fotmob_id',
                     'team__fotmob_id')
    autocomplete_fields = ('team', 'player')
    list_select_related = ('team', 'player')
    list_editable = ('is_active',)
    ordering = ('team__name', 'player__name')
    list_per_page = 50
    actions = ('mark_active', 'mark_inactive')

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _apps=Count('player__lineup_entries', distinct=True),
        )

    @admin.display(description='Player', ordering='player__name')
    def player_name(self, obj):
        return admin_link(obj.player, obj.player.name)

    @admin.display(description='Team', ordering='team__name')
    def team_name(self, obj):
        crest = (
            format_html('<img src="{}" style="height:18px;width:18px;'
                        'vertical-align:middle;object-fit:contain;margin-right:6px;">',
                        obj.team.logo_url)
            if obj.team.logo_url else ''
        )
        return format_html('{}{}', crest, admin_link(obj.team, obj.team.name))

    @admin.display(description='#', ordering='shirt_number')
    def shirt(self, obj):
        return obj.shirt_number or '—'

    @admin.display(description='Position')
    def position(self, obj):
        return obj.player.position or '—'

    @admin.display(description='Apps', ordering='_apps')
    def appearances(self, obj):
        return obj._apps or '—'

    @admin.display(description='Updated', ordering='updated_at')
    def last_updated(self, obj):
        return f'{timesince(obj.updated_at)} ago'

    @admin.action(description='Mark selected squad links active')
    def mark_active(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f'{updated} squad link(s) marked active.')

    @admin.action(description='Mark selected squad links inactive')
    def mark_inactive(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f'{updated} squad link(s) marked inactive.')


# ---------------------------------------------------------------------------
# Matches
# ---------------------------------------------------------------------------


class KickoffWindowFilter(admin.SimpleListFilter):
    """Jump to a date window.

    The changelist is ordered by kickoff descending and the data extends a
    month into the future, so the unfiltered first page shows the far end of
    the window rather than today.
    """

    title = 'kickoff window'
    parameter_name = 'window'

    def lookups(self, request, model_admin):
        return (
            ('today', 'Today (UTC)'),
            ('tomorrow', 'Tomorrow'),
            ('yesterday', 'Yesterday'),
            ('next7', 'Next 7 days'),
            ('last7', 'Last 7 days'),
            ('future', 'All future'),
            ('past', 'All past'),
        )

    def queryset(self, request, queryset):
        today = now().date()
        windows = {
            'today': (today, today),
            'tomorrow': (today + timedelta(days=1), today + timedelta(days=1)),
            'yesterday': (today - timedelta(days=1), today - timedelta(days=1)),
            'next7': (today, today + timedelta(days=7)),
            'last7': (today - timedelta(days=7), today),
        }
        value = self.value()
        if value in windows:
            start, end = windows[value]
            return queryset.filter(kickoff_utc__date__gte=start,
                                   kickoff_utc__date__lte=end)
        if value == 'future':
            return queryset.filter(kickoff_utc__gte=now())
        if value == 'past':
            return queryset.filter(kickoff_utc__lt=now())
        return queryset


class LiveMatchInline(admin.StackedInline):
    model = LiveMatch
    extra = 0
    readonly_fields = ('created_at', 'updated_at')


class MatchOverrideInline(admin.TabularInline):
    model = MatchOverride
    extra = 0
    fields = ('field_name', 'value', 'reason', 'priority', 'is_active',
              'expires_at', 'actor')
    autocomplete_fields = ('actor',)


class MatchEventInline(admin.TabularInline):
    model = MatchEvent
    extra = 0
    can_delete = False
    fields = ('minute', 'added_time', 'event_type', 'player_name',
              'assist_player_name', 'is_home', 'score_home', 'score_away')
    readonly_fields = fields
    ordering = ('minute', 'sequence')

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = ('kickoff', 'fixture', 'scoreline', 'status_badge', 'league',
                    'coverage_badges', 'freshness')
    list_display_links = ('fixture',)
    list_filter = (KickoffWindowFilter, 'category', 'status_code', 'source',
                   'is_archived', 'league__country_code')
    search_fields = ('fotmob_id', 'home_team__name', 'away_team__name',
                     'league__name', 'round_name')
    date_hierarchy = 'kickoff_utc'
    autocomplete_fields = ('league', 'season', 'home_team', 'away_team')
    list_select_related = ('league', 'home_team', 'away_team', 'live', 'coverage')
    inlines = (LiveMatchInline, MatchEventInline, MatchOverrideInline)
    ordering = ('-kickoff_utc',)
    list_per_page = 50
    save_on_top = True
    actions = ('sync_coverage', 'archive_matches', 'restore_matches')

    readonly_fields = ('fotmob_id', 'last_seen_at', 'source_updated_at',
                       'created_at', 'updated_at', 'archived_at',
                       'coverage_detail', 'provider_league_id',
                       'provider_primary_league_id')
    fieldsets = (
        ('Identity', {'fields': ('fotmob_id', 'source', 'league', 'season',
                                 'round_name', 'provider_league_id',
                                 'provider_primary_league_id')}),
        ('Teams and score', {'fields': (('home_team', 'away_team'),
                                        ('home_score', 'away_score'),
                                        ('home_penalty_score', 'away_penalty_score'),
                                        ('home_red_cards', 'away_red_cards'))}),
        ('Timing', {'fields': ('kickoff_utc', 'kickoff_display', 'duration_minutes',
                               'first_half_started_utc', 'second_half_started_utc')}),
        ('Status', {'fields': ('category', 'status_code', 'status_reason_short',
                               'status_reason_long')}),
        ('Coverage', {'fields': ('coverage_detail',)}),
        ('Bookkeeping', {'fields': ('is_archived', 'archived_at', 'last_seen_at',
                                    'source_updated_at', 'created_at', 'updated_at'),
                         'classes': ('collapse',)}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('coverage__features')

    @admin.display(description='Kickoff (UTC)', ordering='kickoff_utc')
    def kickoff(self, obj):
        return obj.kickoff_utc.strftime('%Y-%m-%d %H:%M') if obj.kickoff_utc else '—'

    @admin.display(description='Fixture', ordering='home_team__name')
    def fixture(self, obj):
        return format_html(
            '<strong>{}</strong> <span style="opacity:.6;">v</span> <strong>{}</strong>',
            obj.home_team.name, obj.away_team.name,
        )

    @admin.display(description='Score')
    def scoreline(self, obj):
        if obj.home_score is None or obj.away_score is None:
            # Never render an unknown score as 0-0.
            return muted('—')
        text = f'{obj.home_score} – {obj.away_score}'
        if obj.home_penalty_score is not None and obj.away_penalty_score is not None:
            text += f' ({obj.home_penalty_score}–{obj.away_penalty_score} pens)'
        return format_html('<strong>{}</strong>', text)

    @admin.display(description='Status', ordering='category')
    def status_badge(self, obj):
        color = CATEGORY_COLORS.get(obj.category, '#616161')
        label = obj.status_code
        live = getattr(obj, 'live', None)
        if obj.category == MatchCategory.LIVE and live is not None:
            label = live.minute_display or f"{live.minute_elapsed or 0}'"
        return badge(label, color, obj.get_category_display())

    @admin.display(description='Coverage')
    def coverage_badges(self, obj):
        coverage = getattr(obj, 'coverage', None)
        if coverage is None:
            return muted('not fetched')
        features = sorted(coverage.features.all(), key=lambda f: f.feature)
        if not features:
            return muted('—')
        return format_html_join(
            ' ',
            '<span title="{}: {} ({} entries)" style="display:inline-block;'
            'width:10px;height:10px;border-radius:50%;background:{};"></span>',
            (
                (f.feature, f.state, f.entry_count,
                 COVERAGE_COLORS.get(f.state, '#9e9e9e'))
                for f in features
            ),
        )

    @admin.display(description='Seen', ordering='last_seen_at')
    def freshness(self, obj):
        if not obj.last_seen_at:
            return '—'
        age = now() - obj.last_seen_at
        color = '#2e7d32' if age.total_seconds() < 3600 else '#ef6c00' if (
            age.days < 1) else '#c62828'
        return badge(f'{timesince(obj.last_seen_at)}', color, 'Time since last sync')

    @admin.display(description='Stored coverage')
    def coverage_detail(self, obj):
        coverage = getattr(obj, 'coverage', None)
        if coverage is None:
            return 'No coverage fetched. Run: manage.py sync_fotmob_coverage ' \
                   f'--match-id {obj.fotmob_id}'
        rows = [
            (f.feature, f.state, f.entry_count,
             COVERAGE_COLORS.get(f.state, '#9e9e9e'))
            for f in sorted(coverage.features.all(), key=lambda f: f.feature)
        ]
        header = format_html(
            '<p>Source level: <strong>{}</strong> · last success: {}</p>',
            coverage.source_coverage_level or 'unknown',
            coverage.last_success_at or 'never',
        )
        body = format_html_join(
            '', '<div style="padding:1px 0;"><span style="display:inline-block;'
            'width:140px;">{}</span><span style="color:{3};font-weight:600;">{}</span>'
            ' <span style="opacity:.7;">({} entries)</span></div>',
            ((feature, state, count, color) for feature, state, count, color in rows),
        )
        return format_html('{}{}', header, body)

    @admin.action(description='Fetch coverage now for selected matches')
    def sync_coverage(self, request, queryset):
        """Dispatch a real coverage sync; this is what creates player rows."""
        from api import service  # local import keeps core independent of api

        match_ids = list(queryset.values_list('fotmob_id', flat=True)[:50])
        if not match_ids:
            self.message_user(request, 'Nothing selected.', messages.WARNING)
            return
        if service.job_in_progress('sync_fotmob_coverage'):
            self.message_user(
                request, 'A coverage sync is already running; try again when it '
                'finishes.', messages.WARNING,
            )
            return
        service.dispatch_job(
            'sync_fotmob_coverage', {'match_ids': match_ids, 'dry_run': False},
        )
        self.message_user(
            request,
            format_html(
                'Coverage sync started for {} match(es) in the background. {}',
                len(match_ids),
                changelist_link(IngestionRun, 'Watch the run',
                                job='sync_fotmob_coverage'),
            ),
            messages.SUCCESS,
        )

    @admin.action(description='Archive selected matches (soft delete)')
    def archive_matches(self, request, queryset):
        updated = queryset.filter(is_archived=False).update(
            is_archived=True, archived_at=now(),
        )
        self.message_user(request, f'{updated} match(es) archived; none deleted.')

    @admin.action(description='Restore selected matches')
    def restore_matches(self, request, queryset):
        updated = queryset.filter(is_archived=True).update(
            is_archived=False, archived_at=None,
        )
        self.message_user(request, f'{updated} match(es) restored.')


@admin.register(LiveMatch)
class LiveMatchAdmin(admin.ModelAdmin):
    list_display = ('match', 'minute_display', 'score', 'status_code', 'stale')
    search_fields = ('match__fotmob_id', 'match__home_team__name',
                     'match__away_team__name')
    list_select_related = ('match', 'match__home_team', 'match__away_team')
    readonly_fields = ('match', 'created_at', 'updated_at')

    @admin.display(description='Score')
    def score(self, obj):
        if obj.home_score is None or obj.away_score is None:
            return '—'
        return f'{obj.home_score} – {obj.away_score}'

    @admin.display(description='Freshness', boolean=False)
    def stale(self, obj):
        return (badge('stale', '#c62828') if obj.is_stale()
                else badge('fresh', '#2e7d32'))


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


class CoverageFeatureInline(admin.TabularInline):
    model = CoverageFeature
    extra = 0
    can_delete = False
    readonly_fields = ('feature', 'state_badge', 'entry_count', 'fetched_at', 'note')
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description='State')
    def state_badge(self, obj):
        return badge(obj.state, COVERAGE_COLORS.get(obj.state, '#9e9e9e'))


@admin.register(MatchCoverage)
class MatchCoverageAdmin(admin.ModelAdmin):
    list_display = ('match', 'source_coverage_level', 'available_features',
                    'last_success_at', 'last_error')
    list_filter = ('source_coverage_level',)
    search_fields = ('match__fotmob_id', 'match__home_team__name',
                     'match__away_team__name')
    list_select_related = ('match', 'match__home_team', 'match__away_team')
    inlines = (CoverageFeatureInline,)
    readonly_fields = ('match', 'last_fetch_at', 'last_success_at', 'created_at',
                       'updated_at')

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('features')

    @admin.display(description='Available')
    def available_features(self, obj):
        rows = [f for f in obj.features.all() if f.state == CoverageState.AVAILABLE]
        if not rows:
            return '—'
        return format_html_join(
            ' ', '{}', ((badge(f'{f.feature} {f.entry_count}', '#2e7d32'),)
                        for f in rows),
        )


@admin.register(MatchEvent)
class MatchEventAdmin(admin.ModelAdmin):
    list_display = ('match', 'timestamp', 'event_type', 'player_name', 'side',
                    'running_score')
    list_filter = ('event_type', 'is_goal', 'is_home')
    search_fields = ('match__fotmob_id', 'player_name', 'assist_player_name')
    list_select_related = ('match', 'match__home_team', 'match__away_team', 'team')
    autocomplete_fields = ('match', 'team', 'player')
    ordering = ('-match__kickoff_utc', 'minute')

    @admin.display(description='Minute', ordering='minute')
    def timestamp(self, obj):
        if obj.minute is None:
            return '—'
        return f"{obj.minute}+{obj.added_time}'" if obj.added_time else f"{obj.minute}'"

    @admin.display(description='Side')
    def side(self, obj):
        if obj.is_home is None:
            return '—'
        return obj.match.home_team.name if obj.is_home else obj.match.away_team.name

    @admin.display(description='Score')
    def running_score(self, obj):
        if obj.score_home is None or obj.score_away is None:
            return '—'
        return f'{obj.score_home} – {obj.score_away}'


class LineupPlayerInline(admin.TabularInline):
    model = LineupPlayer
    extra = 0
    fields = ('shirt_number', 'name', 'position', 'is_starter', 'is_captain',
              'rating', 'minutes_played', 'player')
    autocomplete_fields = ('player',)
    ordering = ('-is_starter', 'shirt_number')


@admin.register(MatchLineup)
class MatchLineupAdmin(admin.ModelAdmin):
    list_display = ('match', 'team', 'formation', 'coach_name', 'starters',
                    'bench', 'team_rating')
    search_fields = ('match__fotmob_id', 'team__name', 'coach_name')
    list_select_related = ('match', 'team', 'match__home_team', 'match__away_team')
    autocomplete_fields = ('match', 'team')
    inlines = (LineupPlayerInline,)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _starters=Count('players', filter=Q(players__is_starter=True)),
            _bench=Count('players', filter=Q(players__is_starter=False)),
            _rating=Avg('players__rating', filter=Q(players__is_starter=True)),
        )

    @admin.display(description='Starters', ordering='_starters')
    def starters(self, obj):
        return obj._starters

    @admin.display(description='Bench', ordering='_bench')
    def bench(self, obj):
        return obj._bench

    @admin.display(description='Avg starter rating', ordering='_rating')
    def team_rating(self, obj):
        return f'{obj._rating:.2f}' if obj._rating else '—'


@admin.register(LineupPlayer)
class LineupPlayerAdmin(admin.ModelAdmin):
    list_display = ('name', 'team_name', 'match', 'shirt_number', 'position',
                    'is_starter', 'is_captain', 'rating', 'minutes_played')
    list_filter = ('is_starter', 'is_captain', 'position')
    search_fields = ('name', 'lineup__team__name', 'lineup__match__fotmob_id')
    list_select_related = ('lineup', 'lineup__team', 'lineup__match',
                           'lineup__match__home_team', 'lineup__match__away_team')
    autocomplete_fields = ('lineup', 'player')
    ordering = ('-rating',)

    @admin.display(description='Team', ordering='lineup__team__name')
    def team_name(self, obj):
        return obj.lineup.team.name

    @admin.display(description='Match')
    def match(self, obj):
        return admin_link(obj.lineup.match, str(obj.lineup.match))


@admin.register(PlayerMatchStat)
class PlayerMatchStatAdmin(admin.ModelAdmin):
    list_display = ('player_name', 'stat_key', 'raw_value', 'numeric_value',
                    'team', 'match', 'period')
    list_filter = ('period', 'stat_key')
    search_fields = ('player_name', 'stat_key', 'match__fotmob_id', 'team__name')
    list_select_related = ('match', 'team', 'player')
    autocomplete_fields = ('match', 'team', 'player')
    ordering = ('player_name', 'stat_key')
    list_per_page = 100


@admin.register(TeamMatchStat)
class TeamMatchStatAdmin(admin.ModelAdmin):
    list_display = ('match', 'team', 'stat_group', 'stat_key', 'raw_value',
                    'numeric_value', 'period')
    list_filter = ('period', 'stat_group')
    search_fields = ('stat_key', 'match__fotmob_id', 'team__name')
    list_select_related = ('match', 'team')
    autocomplete_fields = ('match', 'team')


@admin.register(MatchShot)
class MatchShotAdmin(admin.ModelAdmin):
    list_display = ('match', 'minute', 'player_name', 'team', 'event_type',
                    'situation', 'expected_goals', 'on_target')
    list_filter = ('event_type', 'situation', 'is_on_target', 'is_blocked')
    search_fields = ('player_name', 'match__fotmob_id', 'team__name')
    list_select_related = ('match', 'team')
    autocomplete_fields = ('match', 'team')
    ordering = ('-expected_goals',)

    @admin.display(description='On target', boolean=True)
    def on_target(self, obj):
        return bool(obj.is_on_target)


@admin.register(MatchMomentum)
class MatchMomentumAdmin(admin.ModelAdmin):
    list_display = ('match', 'minute', 'value')
    search_fields = ('match__fotmob_id',)
    list_select_related = ('match',)
    autocomplete_fields = ('match',)


@admin.register(MatchCommentary)
class MatchCommentaryAdmin(admin.ModelAdmin):
    list_display = ('match', 'minute', 'entry_type', 'excerpt')
    list_filter = ('entry_type',)
    search_fields = ('match__fotmob_id', 'text')
    list_select_related = ('match',)
    autocomplete_fields = ('match',)

    @admin.display(description='Text')
    def excerpt(self, obj):
        return obj.text[:120] + ('…' if len(obj.text) > 120 else '')


@admin.register(MatchRawSnapshot)
class MatchRawSnapshotAdmin(admin.ModelAdmin):
    list_display = ('match', 'fetched_at', 'has_fixture', 'has_details')
    search_fields = ('match__fotmob_id',)
    list_select_related = ('match',)
    readonly_fields = ('match', 'fixture_payload', 'details_payload', 'fetched_at',
                       'created_at', 'updated_at')

    @admin.display(description='Fixture payload', boolean=True)
    def has_fixture(self, obj):
        return obj.fixture_payload is not None

    @admin.display(description='Details payload', boolean=True)
    def has_details(self, obj):
        return obj.details_payload is not None


# ---------------------------------------------------------------------------
# Standings and transfers
# ---------------------------------------------------------------------------


@admin.register(Standing)
class StandingAdmin(admin.ModelAdmin):
    list_display = ('rank', 'team', 'league', 'season', 'played', 'wins', 'draws',
                    'losses', 'goal_difference', 'points')
    list_filter = ('league__country_code', 'season')
    search_fields = ('team__name', 'league__name')
    list_select_related = ('team', 'league', 'season')
    autocomplete_fields = ('team', 'league', 'season')
    ordering = ('league__name', 'rank')

    @admin.display(description='GD')
    def goal_difference(self, obj):
        diff = obj.goals_for - obj.goals_against
        return f'+{diff}' if diff > 0 else str(diff)


@admin.register(Transfer)
class TransferAdmin(admin.ModelAdmin):
    list_display = ('player', 'from_team', 'to_team', 'transfer_date',
                    'transfer_type', 'fee_text')
    list_filter = ('transfer_type',)
    search_fields = ('player__name', 'from_team__name', 'to_team__name')
    list_select_related = ('player', 'from_team', 'to_team')
    autocomplete_fields = ('player', 'from_team', 'to_team')
    date_hierarchy = 'transfer_date'


# ---------------------------------------------------------------------------
# Ingestion observability
# ---------------------------------------------------------------------------


class IngestionErrorInline(admin.TabularInline):
    model = IngestionError
    extra = 0
    can_delete = False
    fields = ('category', 'scope', 'external_ref', 'message', 'created_at')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(IngestionRun)
class IngestionRunAdmin(admin.ModelAdmin):
    list_display = ('started', 'job', 'state_badge', 'summary', 'requests',
                    'duration', 'dry_run')
    list_filter = ('job', 'state', 'trigger', 'dry_run')
    search_fields = ('correlation_id',)
    date_hierarchy = 'started_at'
    ordering = ('-started_at',)
    inlines = (IngestionErrorInline,)
    readonly_fields = [field.name for field in IngestionRun._meta.fields]

    def has_add_permission(self, request):
        return False

    @admin.display(description='Started', ordering='started_at')
    def started(self, obj):
        return format_html(
            '{}<br><span style="opacity:.6;font-size:11px;">{} ago</span>',
            obj.started_at.strftime('%Y-%m-%d %H:%M:%S'), timesince(obj.started_at),
        )

    @admin.display(description='State', ordering='state')
    def state_badge(self, obj):
        return badge(obj.state, STATE_COLORS.get(obj.state, '#616161'),
                     obj.get_trigger_display())

    @admin.display(description='Counts')
    def summary(self, obj):
        counts = {k: v for k, v in (obj.counts or {}).items() if v}
        if not counts:
            return '—'
        return format_html_join(
            ' ', '<span style="margin-right:8px;"><strong>{}</strong> {}</span>',
            ((value, key.replace('_', ' ')) for key, value in counts.items()),
        )

    @admin.display(description='Requests')
    def requests(self, obj):
        if obj.provider_errors:
            return format_html('{} <span style="color:#c62828;">({} errors)</span>',
                               obj.provider_requests, obj.provider_errors)
        return obj.provider_requests

    @admin.display(description='Duration')
    def duration(self, obj):
        seconds = obj.duration_seconds
        if seconds is None:
            return badge('running', '#1565c0')
        return f'{seconds:.1f}s' if seconds < 120 else f'{seconds / 60:.1f}m'


@admin.register(IngestionError)
class IngestionErrorAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'run', 'category', 'scope', 'external_ref',
                    'excerpt', 'replayable')
    list_filter = ('category', 'scope')
    search_fields = ('external_ref', 'message')
    list_select_related = ('run',)
    readonly_fields = [field.name for field in IngestionError._meta.fields]

    def has_add_permission(self, request):
        return False

    @admin.display(description='Message')
    def excerpt(self, obj):
        return obj.message[:100] + ('…' if len(obj.message) > 100 else '')

    @admin.display(description='Payload kept', boolean=True)
    def replayable(self, obj):
        return obj.payload is not None


@admin.register(MatchOverride)
class MatchOverrideAdmin(admin.ModelAdmin):
    list_display = ('match', 'field_name', 'value', 'effective', 'priority',
                    'actor', 'expires_at')
    list_filter = ('is_active', 'field_name')
    search_fields = ('match__fotmob_id', 'reason', 'field_name')
    list_select_related = ('match', 'actor')
    autocomplete_fields = ('match', 'actor')

    @admin.display(description='Effective', boolean=True)
    def effective(self, obj):
        return obj.is_effective()


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'actor', 'action', 'model_name', 'object_id',
                    'reason')
    list_filter = ('action', 'model_name')
    search_fields = ('object_id', 'reason', 'request_id')
    date_hierarchy = 'created_at'
    list_select_related = ('actor',)
    readonly_fields = [field.name for field in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
