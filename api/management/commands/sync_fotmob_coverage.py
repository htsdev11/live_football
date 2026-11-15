"""Coverage sync (scraper A's matchDetails pass), every 5 minutes.

    manage.py sync_fotmob_coverage --details-for live --days-back 1

Only prioritized matches are fetched: in-progress games first, then recently
completed matches whose coverage is missing or stale. Empty or failed features
never erase previously stored good data.
"""

from __future__ import annotations

from django.core.management.base import CommandError

from api import service
from api.management.commands._base import FotMobCommand
from core.models import Match


class Command(FotMobCommand):
    help = 'Fetch matchDetails coverage for prioritized matches.'
    job_name = 'sync_fotmob_coverage'

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument('--details-for', choices=('live', 'all'), default='live',
                            help='live: in-progress only; all: also fill coverage gaps.')
        parser.add_argument('--days-back', type=int, default=1,
                            help='How far back to consider matches (default: 1).')
        parser.add_argument('--limit', type=int, default=50,
                            help='Maximum matches per run (default: 50).')
        parser.add_argument('--match-id', action='append', dest='match_ids', type=int,
                            help='Explicit FotMob match ID; repeatable.')
        parser.add_argument('--store-raw', action='store_true',
                            help='Keep the raw matchDetails payload for replay.')

    def handle(self, *args, **options):
        if options['limit'] < 1:
            raise CommandError('--limit must be at least 1.')

        matches = None
        if options.get('match_ids'):
            matches = list(
                Match.objects.visible()
                .select_related('home_team', 'away_team')
                .filter(fotmob_id__in=options['match_ids'])
            )
            missing = set(options['match_ids']) - {m.fotmob_id for m in matches}
            if missing:
                self.stderr.write(self.style.WARNING(
                    f'Unknown match IDs skipped: {sorted(missing)}',
                ))

        with service.FotMobClient(**self.client_kwargs(options)) as client:
            run = service.ingest_coverage(
                matches=matches,
                details_for=options['details_for'],
                days_back=max(0, options['days_back']),
                limit=options['limit'],
                trigger=options['trigger'],
                dry_run=options['dry_run'],
                store_raw=options['store_raw'],
                client=client,
            )
        self.report(run, options)
