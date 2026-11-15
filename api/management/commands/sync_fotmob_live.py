"""Live score/status sync (scraper A's fixture pass), every 2 minutes.

    manage.py sync_fotmob_live

Refreshes UTC yesterday and today, keeps the LiveMatch projection current and
closes stale projections only after verifying the match upstream.
"""

from __future__ import annotations

from api import service
from api.management.commands._base import FotMobCommand


class Command(FotMobCommand):
    help = 'Refresh live scores and statuses for recent FotMob fixtures.'
    job_name = 'sync_fotmob_live'

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument('--days-back', type=int, default=1,
                            help='Days before today to refresh (default: 1).')
        parser.add_argument('--date', action='append', dest='dates',
                            help='Explicit UTC date; repeatable.')

    def handle(self, *args, **options):
        if options.get('dates'):
            days = [self.parse_date(value) for value in options['dates']]
        else:
            days = service.date_window(max(0, options['days_back']), 0)

        with service.FotMobClient(**self.client_kwargs(options)) as client:
            run = service.ingest_live(
                days=days,
                trigger=options['trigger'],
                dry_run=options['dry_run'],
                client=client,
            )
        self.report(run, options)
