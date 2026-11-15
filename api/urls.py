"""All API URL patterns, mounted by the project at ``/api/``.

Static segments (``live/``, ``completed/``, ...) are declared before the
``<int:fotmob_id>`` detail routes so a literal path never falls through to the
detail view. There is intentionally no versioned sub-package.
"""

from django.urls import path

from api import views

app_name = 'api'

urlpatterns = [
    # 5.1 Authentication and session identity
    path('auth/token/', views.AuthTokenAPIView.as_view(), name='auth-token'),
    path('auth/token/rotate/', views.AuthTokenRotateAPIView.as_view(),
         name='auth-token-rotate'),
    path('auth/logout/', views.AuthLogoutAPIView.as_view(), name='auth-logout'),
    path('auth/me/', views.AuthMeAPIView.as_view(), name='auth-me'),

    # 5.2 Matches: category lists first, then detail routes
    path('matches/', views.MatchListAPIView.as_view(), name='match-list'),
    path('matches/live/', views.MatchLiveAPIView.as_view(), name='match-live'),
    path('matches/completed/', views.MatchCompletedAPIView.as_view(),
         name='match-completed'),
    path('matches/upcoming/', views.MatchUpcomingAPIView.as_view(),
         name='match-upcoming'),
    path('matches/cancelled/', views.MatchCancelledAPIView.as_view(),
         name='match-cancelled'),
    path('matches/by-date/<str:match_date>/', views.MatchByDateAPIView.as_view(),
         name='match-by-date'),

    path('matches/<int:fotmob_id>/', views.MatchDetailAPIView.as_view(),
         name='match-detail'),
    path('matches/<int:fotmob_id>/events/', views.MatchEventsAPIView.as_view(),
         name='match-events'),
    path('matches/<int:fotmob_id>/goals/', views.MatchGoalsAPIView.as_view(),
         name='match-goals'),
    path('matches/<int:fotmob_id>/lineups/', views.MatchLineupsAPIView.as_view(),
         name='match-lineups'),
    path('matches/<int:fotmob_id>/stats/', views.MatchStatsAPIView.as_view(),
         name='match-stats'),
    path('matches/<int:fotmob_id>/player-stats/',
         views.MatchPlayerStatsAPIView.as_view(), name='match-player-stats'),
    path('matches/<int:fotmob_id>/shots/', views.MatchShotsAPIView.as_view(),
         name='match-shots'),
    path('matches/<int:fotmob_id>/momentum/', views.MatchMomentumAPIView.as_view(),
         name='match-momentum'),
    path('matches/<int:fotmob_id>/commentary/', views.MatchCommentaryAPIView.as_view(),
         name='match-commentary'),
    path('matches/<int:fotmob_id>/coverage/', views.MatchCoverageAPIView.as_view(),
         name='match-coverage'),
    path('matches/<int:fotmob_id>/overrides/', views.MatchOverrideAPIView.as_view(),
         name='match-overrides'),

    # 5.3 Leagues
    path('leagues/', views.LeagueListAPIView.as_view(), name='league-list'),
    path('leagues/<int:fotmob_id>/', views.LeagueDetailAPIView.as_view(),
         name='league-detail'),
    path('leagues/<int:fotmob_id>/matches/', views.LeagueMatchesAPIView.as_view(),
         name='league-matches'),
    path('leagues/<int:fotmob_id>/standings/', views.LeagueStandingsAPIView.as_view(),
         name='league-standings'),

    # 5.3 Teams
    path('teams/', views.TeamListAPIView.as_view(), name='team-list'),
    path('teams/<int:fotmob_id>/', views.TeamDetailAPIView.as_view(), name='team-detail'),
    path('teams/<int:fotmob_id>/matches/', views.TeamMatchesAPIView.as_view(),
         name='team-matches'),
    path('teams/<int:fotmob_id>/players/', views.TeamPlayersAPIView.as_view(),
         name='team-players'),

    # 5.3 Seasons
    path('seasons/', views.SeasonListAPIView.as_view(), name='season-list'),
    path('seasons/<int:pk>/', views.SeasonDetailAPIView.as_view(), name='season-detail'),
    path('seasons/<int:pk>/matches/', views.SeasonMatchesAPIView.as_view(),
         name='season-matches'),

    # 5.3 Players (phase 2 read-only)
    path('players/', views.PlayerListAPIView.as_view(), name='player-list'),
    path('players/<int:pk>/', views.PlayerDetailAPIView.as_view(), name='player-detail'),
    path('players/<int:pk>/matches/', views.PlayerMatchesAPIView.as_view(),
         name='player-matches'),

    # 5.4 Internal ingestion observability (staff only)
    path('internal/sync-runs/', views.SyncRunListAPIView.as_view(), name='sync-run-list'),
    path('internal/sync-runs/<int:pk>/', views.SyncRunDetailAPIView.as_view(),
         name='sync-run-detail'),
    path('internal/sync-jobs/', views.SyncJobTriggerAPIView.as_view(),
         name='sync-job-trigger'),
    path('internal/health/', views.IngestionHealthAPIView.as_view(),
         name='ingestion-health'),
]
