"""
Celery configuration for kogomalo project
"""
import os
from celery import Celery
from celery.schedules import crontab

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('kogomalo')

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered Django apps.
app.autodiscover_tasks()

# Celery Beat Schedule - Periodic Tasks
app.conf.beat_schedule = {
    'nightly-snapshot-refresh': {
        'task': 'apps.core.tasks.refresh_current_month_snapshots',
        'schedule': crontab(hour=2, minute=0),  # Run at 2:00 AM every night
        'options': {
            'description': 'Nightly refresh of current month snapshots',
        }
    },
    'monthly-finalization': {
        'task': 'apps.core.tasks.finalize_previous_month',
        'schedule': crontab(day_of_month=1, hour=3, minute=0),  # Run at 3:00 AM on the 1st of each month
        'options': {
            'description': 'Auto-finalize previous month snapshots on the 1st',
        }
    },
}

# Celery Beat timezone
app.conf.timezone = 'Asia/Jerusalem'  # Or use 'UTC' if you prefer


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Debug task to test Celery is working"""
    print(f'Request: {self.request!r}')
