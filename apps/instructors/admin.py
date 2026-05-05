from django.contrib import admin
from .models import (
    Instructor, InstructorSalaryTier, InstructorBranch, InstructorBonus
)


class InstructorSalaryTierInline(admin.TabularInline):
    model = InstructorSalaryTier
    extra = 1


class InstructorBranchInline(admin.TabularInline):
    model = InstructorBranch
    extra = 1


@admin.register(Instructor)
class InstructorAdmin(admin.ModelAdmin):
    list_display = ['full_name', 'phone', 'email', 'primary_branch', 'salary_model_type', 'is_active']
    list_filter = ['is_active', 'salary_model_type', 'primary_branch']
    search_fields = ['first_name', 'last_name', 'phone', 'email']
    inlines = [InstructorSalaryTierInline, InstructorBranchInline]


@admin.register(InstructorSalaryTier)
class InstructorSalaryTierAdmin(admin.ModelAdmin):
    list_display = ['instructor', 'min_students', 'max_students', 'salary_per_lesson']
    list_filter = ['instructor']


@admin.register(InstructorBranch)
class InstructorBranchAdmin(admin.ModelAdmin):
    list_display = ['instructor', 'branch', 'created_at']
    list_filter = ['branch']


@admin.register(InstructorBonus)
class InstructorBonusAdmin(admin.ModelAdmin):
    list_display = ['instructor', 'bonus_type', 'amount', 'bonus_date', 'description']
    list_filter = ['bonus_type', 'bonus_date']
    search_fields = ['instructor__first_name', 'instructor__last_name', 'description']
    date_hierarchy = 'bonus_date'

