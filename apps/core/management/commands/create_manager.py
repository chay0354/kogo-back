from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model

from apps.core.models import UserProfile


User = get_user_model()


class Command(BaseCommand):
    help = "Create an initial Manager user (internal system bootstrap)."

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True, help='Manager email')
        parser.add_argument('--password', required=True, help='Manager password')
        parser.add_argument('--first-name', default='', help='First name')
        parser.add_argument('--last-name', default='', help='Last name')
        parser.add_argument('--no-staff', action='store_true', help='Do not set is_staff')

    def handle(self, *args, **options):
        email = (options['email'] or '').strip().lower()
        password = options['password']
        first_name = options.get('first_name', '')
        last_name = options.get('last_name', '')
        is_staff = not bool(options.get('no_staff', False))

        if not email:
            raise CommandError('Email is required')
        if User.objects.filter(email__iexact=email).exists() or User.objects.filter(username=email).exists():
            raise CommandError('User already exists')

        user = User(username=email, email=email, first_name=first_name, last_name=last_name, is_active=True, is_staff=is_staff)
        user.set_password(password)
        user.save()

        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save()

        self.stdout.write(self.style.SUCCESS(f'Created manager: {email}'))


