from django.contrib import admin
from .models import Enrollment, LessonEnrollment, LessonAttendance


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ['child', 'course', 'is_active', 'enrolled_at']
    list_filter = ['is_active', 'course']
    search_fields = ['child__first_name', 'child__last_name', 'course__name']


@admin.register(LessonEnrollment)
class LessonEnrollmentAdmin(admin.ModelAdmin):
    list_display = ['child', 'lesson', 'status', 'start_date', 'end_date', 'enrolled_at']
    list_filter = ['status', 'lesson__day_of_week']
    search_fields = ['child__first_name', 'child__last_name', 'lesson__course__name']


@admin.register(LessonAttendance)
class LessonAttendanceAdmin(admin.ModelAdmin):
    list_display = ['child', 'lesson', 'status', 'created_at']
    list_filter = ['status', 'lesson__day_of_week']
    search_fields = ['child__first_name', 'child__last_name', 'lesson__course__name']

