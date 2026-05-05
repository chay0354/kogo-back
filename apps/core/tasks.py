"""
Celery periodic tasks for snapshot management
"""
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, name='apps.core.tasks.refresh_current_month_snapshots')
def refresh_current_month_snapshots(self):
    """
    Nightly task to refresh current month snapshots
    Runs every night at 2:00 AM
    
    This recalculates all snapshot data for the current month:
    - InstructorMonthlySnapshot
    - LessonMonthlySnapshot
    - BranchMonthlySnapshot
    
    Status: NOT finalized (can be updated again)
    """
    from apps.instructors.utils import generate_monthly_snapshots
    
    try:
        today = timezone.now().date()
        current_month = today.strftime('%Y-%m')
        
        logger.info(f'Starting nightly snapshot refresh for {current_month}')
        
        # Generate snapshots for current month (not finalized)
        result = generate_monthly_snapshots(current_month, finalize=False)
        
        logger.info(
            f'Successfully refreshed snapshots for {current_month}: '
            f'{result["instructors_created"]} instructors, '
            f'{result["lessons_created"]} lessons, '
            f'{result["branches_created"]} branches'
        )
        
        return {
            'success': True,
            'month': current_month,
            'summary': result
        }
        
    except Exception as e:
        logger.error(f'Error refreshing current month snapshots: {str(e)}', exc_info=True)
        # Re-raise so Celery knows the task failed
        raise


@shared_task(bind=True, name='apps.core.tasks.finalize_previous_month')
def finalize_previous_month(self):
    """
    Monthly task to finalize previous month snapshots
    Runs on the 1st of each month at 3:00 AM
    
    This marks all previous month's snapshots as finalized:
    - InstructorMonthlySnapshot (is_finalized=True)
    - LessonMonthlySnapshot (is_finalized=True)
    - BranchMonthlySnapshot (is_finalized=True)
    
    Once finalized, snapshots become immutable historical records.
    """
    from apps.instructors.utils import generate_monthly_snapshots
    
    try:
        # Calculate previous month
        today = timezone.now().date()
        first_of_month = today.replace(day=1)
        last_month_end = first_of_month - timedelta(days=1)
        previous_month = last_month_end.strftime('%Y-%m')
        
        logger.info(f'Starting monthly finalization for {previous_month}')
        
        # Generate and finalize snapshots for previous month
        result = generate_monthly_snapshots(previous_month, finalize=True)
        
        logger.info(
            f'Successfully finalized snapshots for {previous_month}: '
            f'{result["instructors_created"]} instructors, '
            f'{result["lessons_created"]} lessons, '
            f'{result["branches_created"]} branches'
        )
        
        # Send notification (optional - can implement email/Slack notification)
        _send_finalization_notification(previous_month, result)
        
        return {
            'success': True,
            'month': previous_month,
            'summary': result
        }
        
    except Exception as e:
        logger.error(f'Error finalizing previous month snapshots: {str(e)}', exc_info=True)
        # Re-raise so Celery knows the task failed
        raise


def _send_finalization_notification(month, result):
    """
    Optional: Send notification about successful finalization
    Can be extended to send emails or Slack messages
    """
    logger.info(
        f'[NOTIFICATION] Month {month} has been finalized:\n'
        f'  - Instructors: {result["instructors_created"]}\n'
        f'  - Lessons: {result["lessons_created"]}\n'
        f'  - Branches: {result["branches_created"]}'
    )
    # TODO: Implement email notification to managers
    # TODO: Implement Slack notification
    pass


@shared_task(bind=True, name='apps.core.tasks.test_celery')
def test_celery(self):
    """
    Test task to verify Celery is working
    Can be called manually: test_celery.delay()
    """
    logger.info('Test Celery task is running!')
    return {'success': True, 'message': 'Celery is working!'}
