"""
Django settings for Kogomalo project.
"""
import os
from pathlib import Path
import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from decouple import config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY', default='django-insecure-development-key-change-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
# On Vercel, default DEBUG to False unless explicitly enabled.
_DEBUG_DEFAULT = 'false' if os.environ.get('VERCEL') else 'true'
DEBUG = config('DEBUG', default=_DEBUG_DEFAULT, cast=bool)

ALLOWED_HOSTS = [h.strip() for h in config('ALLOWED_HOSTS', default='localhost,127.0.0.1').split(',') if h.strip()]
# Add Cloudflare Tunnel domain for webhook testing
ALLOWED_HOSTS.append('just-doors-versus-presence.trycloudflare.com')
# Allow all Cloudflare Tunnel domains (they change randomly)
ALLOWED_HOSTS.append('.trycloudflare.com')
# Add fly.io domains
ALLOWED_HOSTS.append('.fly.dev')
ALLOWED_HOSTS.append('.fly.io')
# Vercel preview + production hostnames (*.vercel.app)
if os.environ.get('VERCEL') and '.vercel.app' not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append('.vercel.app')

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # Third party apps
    'rest_framework',
    'rest_framework.authtoken',
    'corsheaders',
    'django_celery_beat',
    
    # Local apps
    'apps.core',
    # 'apps.branches',  # REMOVED - duplicate of core
    'apps.instructors',
    'apps.courses',
    'apps.customers',
    'apps.enrollments',
    'apps.scheduling',
    'apps.store',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database — PostgreSQL only (Supabase). SQLite is not supported.
DATABASE_URL = (config('DATABASE_URL', default='') or '').strip()
if not DATABASE_URL:
    raise ImproperlyConfigured(
        'DATABASE_URL is required. Use your Supabase Postgres URI '
        '(Dashboard → Project Settings → Database → Connection string / URI).'
    )

_default_db = dj_database_url.parse(DATABASE_URL, conn_max_age=600)
if _default_db['ENGINE'] == 'django.db.backends.sqlite3':
    raise ImproperlyConfigured(
        'SQLite is not supported. Set DATABASE_URL to a PostgreSQL (Supabase) connection string.'
    )

DATABASES = {'default': _default_db}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'he'
TIME_ZONE = 'Asia/Jerusalem'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files (User uploads)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# File upload settings
FILE_UPLOAD_MAX_MEMORY_SIZE = 52428800  # 50MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 52428800  # 50MB

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# CORS Settings
CORS_ALLOWED_ORIGINS = [
    o.strip()
    for o in config(
        'CORS_ALLOWED_ORIGINS',
        default='http://localhost:3000,http://localhost:3001',
    ).split(',')
    if o.strip()
]

# Vercel preview + production hosts (*.vercel.app). Not gated on os.environ["VERCEL"] — that is not
# always visible to the app process; without the regex, preflight fails for split front/back deploys.
# Set CORS_DISABLE_VERCEL_REGEX=true to turn off (then list every origin in CORS_ALLOWED_ORIGINS).
CORS_ALLOWED_ORIGIN_REGEXES = []
if not config('CORS_DISABLE_VERCEL_REGEX', default=False, cast=bool):
    CORS_ALLOWED_ORIGIN_REGEXES = [
        r'^https://[\w.-]+\.vercel\.app$',
    ]

CORS_ALLOW_CREDENTIALS = True

# Proxy and HTTPS settings for Fly.io
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

# CSRF settings (must be full origins; add your Vercel frontend via CSRF_TRUSTED_ORIGINS_EXTRA)
CSRF_TRUSTED_ORIGINS = [
    'https://kogomalo.fly.dev',
    'https://*.fly.dev',
    'http://localhost:3000',
    'http://localhost:3001',
]
_csrf_extra = config('CSRF_TRUSTED_ORIGINS_EXTRA', default='')
if _csrf_extra:
    CSRF_TRUSTED_ORIGINS.extend(
        [o.strip() for o in _csrf_extra.split(',') if o.strip()]
    )

# Django REST Framework
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'apps.core.authentication.CookieTokenAuthentication',
        'rest_framework.authentication.TokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_FILTER_BACKENDS': [
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
}

# Authentication backends (support login with email + password)
AUTHENTICATION_BACKENDS = [
    'apps.core.auth_backends.EmailBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# Tranzila Payment Gateway Settings
TRANZILA_TERMINAL = config('TRANZILA_TERMINAL', default='mock-terminal')  # Main terminal for iframe payments
TRANZILA_TOKEN_TERMINAL = config('TRANZILA_TOKEN_TERMINAL', default=TRANZILA_TERMINAL)  # Separate terminal for REST API token charges
TRANZILA_SUPPLIER = config('TRANZILA_SUPPLIER', default='mock-supplier')

# RESTful API v1 credentials (required for token-based charges)
TRANZILA_PUBLIC_KEY = config('TRANZILA_PUBLIC_KEY', default='')  # Goes in X-tranzila-api-app-key header
TRANZILA_SECRET_KEY = config('TRANZILA_SECRET_KEY', default='')  # Used to generate X-tranzila-api-access-token signature

# Webhook security
TRANZILA_WEBHOOK_SECRET = config('TRANZILA_WEBHOOK_SECRET', default='mock-webhook-secret')

# Endpoints
TRANZILA_API_BASE_URL = config('TRANZILA_API_BASE_URL', default='https://api.tranzila.com')
TRANZILA_BASE_URL = config('TRANZILA_BASE_URL', default='https://direct.tranzila.com')  # Iframe endpoint
TRANZILA_ENVIRONMENT = config('TRANZILA_ENVIRONMENT', default='development')

# Supabase (URL + publishable key for client-style access; service role only via env/secrets, never in frontend)
SUPABASE_URL = config('SUPABASE_URL', default='')
SUPABASE_PUBLISHABLE_KEY = config('SUPABASE_PUBLISHABLE_KEY', default='')
SUPABASE_SERVICE_ROLE_KEY = config('SUPABASE_SERVICE_ROLE_KEY', default='')

# ManyChat (WhatsApp) — server only; never expose in frontend
MANYCHAT_KEY = config('MANYCHAT_KEY', default='')
# Optional: ManyChat custom field id for phone mirror (e.g. Client_Phone) — enables findByCustomField
MANYCHAT_PHONE_FIELD_ID = config('MANYCHAT_PHONE_FIELD_ID', default='')
# Optional: published Automation Flow namespace that contains the approved WhatsApp Template
# fired automatically when a subscription registration completes (any phone, no 24h window).
MANYCHAT_REGISTRATION_FLOW_NS = config('MANYCHAT_REGISTRATION_FLOW_NS', default='')
# Same as above, but for trial-lesson confirmations (הרשם לניסיון).
MANYCHAT_TRIAL_FLOW_NS = config('MANYCHAT_TRIAL_FLOW_NS', default='')
# 10:00 on trial lesson day (test-lesson-10am automation).
MANYCHAT_TRIAL_10AM_FLOW_NS = config('MANYCHAT_TRIAL_10AM_FLOW_NS', default='')
# after-test automation — hours after trial lesson end (default 2).
MANYCHAT_TRIAL_AFTER_TEST_FLOW_NS = config('MANYCHAT_TRIAL_AFTER_TEST_FLOW_NS', default='')
TRIAL_AFTER_TEST_HOURS = int(config('TRIAL_AFTER_TEST_HOURS', default=2))
# When Tranzila webhook returns Response != 000 for a subscription enrollment payment.
MANYCHAT_PAYMENT_FAILED_FLOW_NS = config('MANYCHAT_PAYMENT_FAILED_FLOW_NS', default='')
# Hour (24h, Israel) to send test-lesson-10am on the trial lesson date.
TRIAL_10AM_REMINDER_HOUR = int(config('TRIAL_10AM_REMINDER_HOUR', default=10))
# Shared secret — Vercel Cron / external scheduler must send this in the X-Cron-Token header.
CRON_TOKEN = config('CRON_TOKEN', default='')

# ==========================
# CELERY CONFIGURATION
# ==========================

# Celery broker (Redis)
CELERY_BROKER_URL = config('CELERY_BROKER_URL', default='redis://localhost:6379/0')

# Celery result backend (Redis)
CELERY_RESULT_BACKEND = config('CELERY_RESULT_BACKEND', default='redis://localhost:6379/0')

# Celery settings
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Asia/Jerusalem'  # Match your local timezone
CELERY_ENABLE_UTC = False

# Celery Beat (periodic tasks)
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'  # Optional: if you want to manage schedules via Django admin

# Task result expiration
CELERY_RESULT_EXPIRES = 3600  # Results expire after 1 hour

# Task execution settings
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes hard time limit
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60  # 25 minutes soft time limit

# Log settings
CELERY_WORKER_LOG_FORMAT = '[%(asctime)s: %(levelname)s/%(processName)s] %(message)s'
CELERY_WORKER_TASK_LOG_FORMAT = '[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s'

# Logging configuration for Celery.
# Vercel: /var/task is read-only — dictConfig instantiates every handler in 'handlers', so the file
# handler must be omitted entirely when VERCEL is set, not only removed from logger handler lists.
_celery_log_handlers = ['console']
_logging_handlers = {
    'console': {
        'class': 'logging.StreamHandler',
        'formatter': 'verbose',
    },
}
if not os.environ.get('VERCEL'):
    _logs_dir = BASE_DIR / 'logs'
    try:
        _logs_dir.mkdir(parents=True, exist_ok=True)
        _logging_handlers['file'] = {
            'class': 'logging.FileHandler',
            'filename': str(BASE_DIR / 'logs' / 'celery.log'),
            'formatter': 'verbose',
        }
        _celery_log_handlers = ['console', 'file']
    except OSError:
        pass

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': _logging_handlers,
    'loggers': {
        'celery': {
            'handlers': _celery_log_handlers,
            'level': 'INFO',
        },
        'apps.core.tasks': {
            'handlers': _celery_log_handlers,
            'level': 'INFO',
        },
    },
}
