from django.contrib import admin
from .models import ScheduleEvent, SubscriptionLog, SubscriptionReminder


@admin.register(ScheduleEvent)
class ScheduleEventAdmin(admin.ModelAdmin):
    list_display = ['name', 'event_date', 'start_time', 'end_time', 'event_type', 'branch', 'is_active']
    list_filter = ['event_type', 'branch', 'is_active', 'event_date']
    search_fields = ['name', 'location']


@admin.register(SubscriptionLog)
class SubscriptionLogAdmin(admin.ModelAdmin):
    list_display = ['child', 'action_type', 'previous_status', 'new_status', 'created_at']
    list_filter = ['action_type', 'created_at']
    search_fields = ['child__first_name', 'child__last_name']


@admin.register(SubscriptionReminder)
class SubscriptionReminderAdmin(admin.ModelAdmin):
    list_display = ['child', 'reminder_type', 'days_before_end', 'status', 'sent_at']
    list_filter = ['status', 'reminder_type']
    search_fields = ['child__first_name', 'child__last_name', 'phone_number']

