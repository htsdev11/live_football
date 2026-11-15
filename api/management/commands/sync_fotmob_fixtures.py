"""Fixture discovery sync (scraper B's logic, writing through the service layer).

Typical cron use, every 15 minutes:

    manage.py sync_fotmob_fixtures --past-days 1 --future-days 3

Daily wide window:

    manage.py sync_fotmob_fixtures --past-days 30 --future-days 30
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import CommandError

from api import service
from api.management.commands._base import FotMobCommand


class Command(FotMobCommand):
    help = 'Fetch FotMob daily fixtures for a date window and upsert matches.'
    job_name = 'sync_fotmob_fixtures'

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument('--start-date', help='First UTC date (YYYY-MM-DD).')
        parser.add_argument('--end-date', help='Last UTC date (YYYY-MM-DD).')
        parser.add_argument('--date', action='append', dest='dates',
                            help='Explicit UTC date; repeatable.')
        parser.add_argument('--past-days', type=int, default=1,
                            help='Days before today when no range is given (default: 1).')
        parser.add_argument('--future-days', type=int, default=3,
                            help='Days after today when no range is given (default: 3).')
        parser.add_argument('--league-ids',
                            help='Comma-separated FotMob league IDs to keep.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Stop after this many fixtures (test runs).')
        parser.add_argument('--store-raw', action='store_true',
                            help='Keep the raw fixture payload for replay.')
        parser.add_argument('--resume', action='store_true',
                            help='Skip dates already covered by a successful run today.')

    def handle(self, *args, **options):
        if bool(options.get('start_date')) != bool(options.get('end_date')):
            raise CommandError('Provide both --start-date and --end-date, or neither.')

        if options.get('dates'):
            days = [self.parse_date(value) for value in options['dates']]
        elif options.get('start_date'):
            start = self.parse_date(options['start_date'])
            end = self.parse_date(options['end_date'])
            if end < start:
                raise CommandError('--end-date must be on or after --start-date.')
            days = [start + timedelta(days=offset)
                    for offset in range((end - start).days + 1)]
        else:
            if options['past_days'] < 0 or options['future_days'] < 0:
                raise CommandError('--past-days and --future-days cannot be negative.')
            days = service.date_window(options['past_days'], options['future_days'])

        league_ids = None
        if options.get('league_ids'):
            try:
                league_ids = {
                    int(part.strip()) for part in options['league_ids'].split(',')
                    if part.strip()
                }
            except ValueError:
                raise CommandError('--league-ids must be a comma-separated list of integers.')

        if options.get('resume'):
            days = self._remaining_days(days)
            if not days:
                self.stdout.write('Nothing to resume; all dates already synced today.')
                return

        with service.FotMobClient(**self.client_kwargs(options)) as client:
            run = service.ingest_fixtures(
                days,
                trigger=options['trigger'],
                league_ids=league_ids,
                limit=options.get('limit'),
                dry_run=options['dry_run'],
                store_raw=options['store_raw'],
                client=client,
            )
        self.report(run, options)

    def _remaining_days(self, days):
        """Drop dates a successful run already covered since midnight UTC."""
        from django.utils import timezone

        from core.models import IngestionRun, IngestionState

        midnight = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        done: set[str] = set()
        for run in IngestionRun.objects.filter(
            job='sync_fotmob_fixtures', state=IngestionState.SUCCESS,
            started_at__gte=midnight,
        ):
            done.update(run.params.get('days') or [])
        return [day for day in days if day.isoformat() not in done]
