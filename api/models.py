"""The api app deliberately defines no models.

All football entities live in ``core.models`` (League, Season, Team, Player,
TeamPlayer, Standing, Transfer, Match, LiveMatch, MatchCoverage and the
ingestion audit tables). Duplicating them here would create a second source of
truth for FotMob match identity, which the architecture explicitly forbids.

Import them from core when you need them:

    from core.models import Match, LiveMatch
"""
