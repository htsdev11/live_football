"""Weekly, rate-limited backfill of older gaps and previously failed records.

    manage.py weekly_backfill --batch-days 7 --max-days 28

Works backwards from the oldest stored fixture in small batches so a historical
gap does not turn into one huge request burst.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import CommandError
from django.utils import timezone

from api import service
from api.management.commands._base import FotMobCommand
from core.models import IngestionError, Match


class Command(FotMobCommand):
    help = 'Backfill older fixture dates and retry quarantined records in batches.'
    job_name = 'weekly_backfill'

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument('--batch-days', type=int, default=7,
                            help='Dates per batch (default: 7).')
        parser.add_argument('--max-days', type=int, default=28,
                            help='Total dates to cover in this run (default: 28).')
        parser.add_argument('--before', help='Work backwards from this UTC date.')

    def handle(self, *args, **options):
        if options['batch_days'] < 1 or options['max_days'] < 1:
            raise CommandError('--batch-days and --max-days must be at least 1.')

        if options.get('before'):
            anchor = self.parse_date(options['before'])
        else:
            oldest = Match.objects.visible().exclude(
                kickoff_utc__isnull=True,
            ).order_by('kickoff_utc').values_list('kickoff_utc', flat=True).first()
            anchor = (oldest.date() if oldest else timezone.now().date())

        days = [anchor - timedelta(days=offset + 1)
                for offset in range(options['max_days'])]
        batch_size = options['batch_days']

        for index in range(0, len(days), batch_size):
            batch = sorted(days[index:index + batch_size])
            with service.FotMobClient(**self.client_kwargs(options)) as client:
                run = service.ingest_fixtures(
                    batch,
                    job='weekly_backfill',
                    trigger=options['trigger'],
                    dry_run=options['dry_run'],
                    client=client,
                )
            self.report(run, options)

        quarantined = IngestionError.objects.filter(
            category=IngestionError.Category.VALIDATION,
        ).exclude(payload=None).count()
        self.stdout.write(
            f'  quarantined        {quarantined} record(s) available for replay '
            f'without re-scraping',
        )
