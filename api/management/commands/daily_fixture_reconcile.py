"""Daily broad-window reconciliation, run once off-peak.

    manage.py daily_fixture_reconcile --past-days 30 --future-days 30

Re-reads a wide date range so reschedules, postponements and late corrections
land. A fixture that simply was not observed is reported as unseen; it is never
deleted or marked cancelled on that basis.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import CommandError
from django.utils import timezone

from api import service
from api.management.commands._base import FotMobCommand
from core.models import Match, MatchCategory


class Command(FotMobCommand):
    help = 'Reconcile a wide fixture window and report unseen or stale matches.'
    job_name = 'daily_fixture_reconcile'

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument('--past-days', type=int, default=30)
        parser.add_argument('--future-days', type=int, default=30)
        parser.add_argument('--unseen-hours', type=int, default=48,
                            help='Flag fixtures not seen upstream for this long.')

    def handle(self, *args, **options):
        if options['past_days'] < 0 or options['future_days'] < 0:
            raise CommandError('--past-days and --future-days cannot be negative.')

        with service.FotMobClient(**self.client_kwargs(options)) as client:
            run = service.reconcile_fixtures(
                past_days=options['past_days'],
                future_days=options['future_days'],
                trigger=options['trigger'],
                dry_run=options['dry_run'],
                client=client,
            )
        self.report(run, options)
        self._report_unseen(options)

    def _report_unseen(self, options: dict) -> None:
        cutoff = timezone.now() - timedelta(hours=max(1, options['unseen_hours']))
        window_start = timezone.now() - timedelta(days=options['past_days'])
        unseen = Match.objects.visible().filter(
            kickoff_utc__gte=window_start,
            last_seen_at__lt=cutoff,
        ).exclude(category=MatchCategory.COMPLETED).count()
        self.stdout.write(
            f'  unseen_recent      {unseen} '
            f'(not observed upstream for {options["unseen_hours"]}h; left untouched)',
        )
