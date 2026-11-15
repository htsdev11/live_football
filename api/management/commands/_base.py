"""Shared argument parsing and reporting for the FotMob sync commands."""

from __future__ import annotations

import json
from datetime import date, datetime

from django.core.management.base import BaseCommand, CommandError

from core.models import IngestionRun, IngestionState, IngestionTrigger


class FotMobCommand(BaseCommand):
    """Base class with the flags the architecture document requires."""

    #: Set by subclasses; used for log lines only.
    job_name = ''

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Fetch and classify without writing to the database.',
        )
        parser.add_argument(
            '--request-gap', type=float, default=None,
            help='Minimum seconds between upstream requests (overrides settings).',
        )
        parser.add_argument(
            '--trigger', choices=[c[0] for c in IngestionTrigger.choices],
            default=IngestionTrigger.CRON,
            help='Who started this run; cron is the default.',
        )
        parser.add_argument(
            '--json', action='store_true', dest='as_json',
            help='Print the run summary as JSON.',
        )

    # -- helpers ------------------------------------------------------------

    def parse_date(self, value: str) -> date:
        for fmt in ('%Y-%m-%d', '%Y%m%d'):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        raise CommandError(f'Invalid date {value!r}; use YYYY-MM-DD or YYYYMMDD.')

    def client_kwargs(self, options: dict) -> dict:
        kwargs = {}
        if options.get('request_gap') is not None:
            if options['request_gap'] < 0:
                raise CommandError('--request-gap cannot be negative.')
            kwargs['request_gap'] = options['request_gap']
        return kwargs

    def report(self, run: IngestionRun, options: dict) -> None:
        summary = {
            'job': run.job,
            'state': run.state,
            'correlation_id': run.correlation_id,
            'dry_run': run.dry_run,
            'duration_seconds': run.duration_seconds,
            'counts': run.counts,
            'provider_requests': run.provider_requests,
            'provider_errors': run.provider_errors,
            'errors': run.errors.count(),
        }
        if options.get('as_json'):
            self.stdout.write(json.dumps(summary, indent=2, default=str))
        else:
            self.stdout.write(f'{run.job} [{run.state}] run={run.correlation_id}')
            for key, value in (run.counts or {}).items():
                self.stdout.write(f'  {key:<18} {value}')
            self.stdout.write(
                f'  provider_requests  {run.provider_requests} '
                f'(errors: {run.provider_errors})',
            )
        if run.state == IngestionState.SUCCESS:
            self.stdout.write(self.style.SUCCESS('state=success'))
        elif run.state == IngestionState.PARTIAL:
            self.stderr.write(self.style.WARNING('state=partial'))
        else:
            self.stderr.write(self.style.ERROR(f'state={run.state}'))
