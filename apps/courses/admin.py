from django.contrib import admin
from .models import CourseType, Course, Lesson


@admin.register(CourseType)
class CourseTypeAdmin(admin.ModelAdmin):
    list_display = ['name', 'is_active', 'created_at']
    list_filter = ['is_active']
    search_fields = ['name', 'description']


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ['name', 'course_type', 'price', 'capacity', 'min_age', 'max_age', 'is_active']
    list_filter = ['is_active', 'course_type', 'branch']
    search_fields = ['name', 'description']


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ['course', 'day_of_week', 'start_time', 'end_time', 'branch', 'instructor', 'status', 'is_recurring']
    list_filter = ['day_of_week', 'status', 'is_recurring', 'branch']
    search_fields = ['course__name', 'instructor__first_name', 'instructor__last_name']

