"""Create (or re-read) the single API user and print its token.

This replaces the login flow while the project runs with one fixed client:
the token is minted here, pasted into Postman or a client config once, and sent
as ``Authorization: Token <key>``. The `/api/auth/token/` endpoint stays in the
code for when per-user login is switched on again.

    manage.py create_api_token                     # create or re-read, print the key
    manage.py create_api_token --rotate            # discard the old key, issue a new one
    manage.py create_api_token --username client   # a different single user
    manage.py create_api_token --role staff        # staff instead of superuser

The user gets an unusable password by default: there is no login to protect, so
no password needs to exist. Pass --set-password to make the account usable in
the Django admin as well.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from rest_framework.authtoken.models import Token


class Command(BaseCommand):
    help = 'Create or re-read the single API user and print its auth token.'

    def add_arguments(self, parser):
        parser.add_argument('--username', default='api',
                            help='Username for the API client (default: api).')
        parser.add_argument('--role', choices=('reader', 'staff', 'admin'),
                            default='admin',
                            help='reader: GET only; staff: + manual edits and '
                                 'internal reads; admin: + overrides (default).')
        parser.add_argument('--rotate', action='store_true',
                            help='Delete the existing token and issue a new one.')
        parser.add_argument('--set-password',
                            help='Also set a password, for Django admin access.')
        parser.add_argument('--quiet', action='store_true',
                            help='Print only the token key.')

    def handle(self, *args, **options):
        username = options['username'].strip()
        if not username:
            raise CommandError('--username cannot be empty.')

        user_model = get_user_model()
        user, created = user_model.objects.get_or_create(username=username)

        role = options['role']
        user.is_staff = role in ('staff', 'admin')
        user.is_superuser = role == 'admin'
        user.is_active = True
        if options.get('set_password'):
            user.set_password(options['set_password'])
        elif created:
            # No login flow is in use, so no usable password is needed.
            user.set_unusable_password()
        user.save()

        if options['rotate']:
            Token.objects.filter(user=user).delete()
        token, token_created = Token.objects.get_or_create(user=user)

        if options['quiet']:
            self.stdout.write(token.key)
            return

        self.stdout.write(self.style.SUCCESS(
            f'{"Created" if created else "Reusing"} user {username!r} (role: {role})',
        ))
        self.stdout.write(
            f'{"Issued new" if token_created else "Existing"} token:\n\n'
            f'  {token.key}\n',
        )
        self.stdout.write('Send it on every request as:\n')
        self.stdout.write(f'  Authorization: Token {token.key}\n')
        self.stdout.write(self.style.WARNING(
            'Treat this key as a credential: keep it out of version control, '
            'logs and URLs, and rotate it with --rotate if it leaks.',
        ))
