from django.contrib import admin
from .models import (
    City, Branch, Room,
    InstructorMonthlySnapshot, LessonMonthlySnapshot, BranchMonthlySnapshot,
    Business,
    BusinessCategory,
)


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at']
    search_fields = ['name']


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ['name', 'city', 'manager_name', 'phone', 'is_active']
    list_filter = ['is_active', 'city']
    search_fields = ['name', 'manager_name', 'phone']


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ['name', 'branch', 'capacity', 'purpose', 'is_active']
    list_filter = ['is_active', 'branch']
    search_fields = ['name', 'branch__name']


@admin.register(InstructorMonthlySnapshot)
class InstructorMonthlySnapshotAdmin(admin.ModelAdmin):
    list_display = ['instructor', 'month', 'total_lessons', 'total_students', 'total_revenue', 'total_salary', 'profit']
    list_filter = ['month']
    search_fields = ['instructor__first_name', 'instructor__last_name']


@admin.register(LessonMonthlySnapshot)
class LessonMonthlySnapshotAdmin(admin.ModelAdmin):
    list_display = ['lesson', 'instructor', 'course', 'branch', 'month', 'enrolled_students', 'revenue', 'instructor_salary', 'profit']
    list_filter = ['month', 'branch', 'instructor']
    search_fields = ['course__name', 'instructor__first_name', 'instructor__last_name']


@admin.register(BranchMonthlySnapshot)
class BranchMonthlySnapshotAdmin(admin.ModelAdmin):
    list_display = ['branch', 'month', 'total_students', 'total_revenue', 'instructor_costs', 'profit', 'active_courses_count']
    list_filter = ['month', 'branch']


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ['name', 'is_active', 'sort_order']
    list_editable = ['is_active', 'sort_order']
    search_fields = ['name']


@admin.register(BusinessCategory)
class BusinessCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'business', 'is_active', 'sort_order']
    list_filter = ['business', 'is_active']
    search_fields = ['name']
