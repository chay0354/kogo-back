"""
Django settings for Kogomalo project.
"""
import os
from pathlib import Path
from decouple import config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY', default='django-insecure-development-key-change-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config('DEBUG', default=True, cast=bool)

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1').split(',')
# Add Cloudflare Tunnel domain for webhook testing
ALLOWED_HOSTS.append('just-doors-versus-presence.trycloudflare.com')
# Allow all Cloudflare Tunnel domains (they change randomly)
ALLOWED_HOSTS.append('.trycloudflare.com')
# Add fly.io domains
ALLOWED_HOSTS.append('.fly.dev')
ALLOWED_HOSTS.append('.fly.io')

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

# Database
# Auto-detect database from environment (PostgreSQL in production, SQLite in development)
DATABASE_URL = config('DATABASE_URL', default='')

if DATABASE_URL:
    # Production: Use PostgreSQL from DATABASE_URL
    import dj_database_url
    DATABASES = {
        'default': dj_database_url.parse(DATABASE_URL, conn_max_age=600)
    }
    # Supabase transaction pooler (port 6543) and psycopg v3: disable prepared statements
    if ':6543' in DATABASE_URL:
        DATABASES['default'].setdefault('OPTIONS', {})
        DATABASES['default']['OPTIONS']['prepare_threshold'] = None
else:
    # Development: Use SQLite
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
            # Avoid "database is locked" under concurrent dev requests by waiting for the lock.
            'OPTIONS': {
                'timeout': 30,  # seconds
            },
        }
    }

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
CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:3000,http://localhost:3001'
).split(',')

CORS_ALLOW_CREDENTIALS = True

# Proxy and HTTPS settings for Fly.io
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

# CSRF settings
CSRF_TRUSTED_ORIGINS = [
    'https://kogomalo.fly.dev',
    'https://*.fly.dev',
    'http://localhost:3000',
    'http://localhost:3001',
]

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

# Logging configuration for Celery
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.FileHandler',
            'filename': BASE_DIR / 'logs' / 'celery.log',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'celery': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
        },
        'apps.core.tasks': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
        },
    },
}
