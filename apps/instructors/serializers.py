from rest_framework import serializers
from apps.instructors.models import (
    Instructor, InstructorSalaryTier, InstructorBranch, InstructorBonus
)
from apps.core.models import (
    Branch, 
    InstructorMonthlySnapshot, LessonMonthlySnapshot, BranchMonthlySnapshot
)


class InstructorSalaryTierSerializer(serializers.ModelSerializer):
    """Serializer for salary tiers"""
    
    class Meta:
        model = InstructorSalaryTier
        fields = ['id', 'min_students', 'max_students', 'salary_per_lesson', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class BranchSimpleSerializer(serializers.ModelSerializer):
    """Simple branch serializer for nested display"""
    
    class Meta:
        model = Branch
        fields = ['id', 'name']


class InstructorBranchSerializer(serializers.ModelSerializer):
    """Serializer for instructor-branch associations"""
    branch = BranchSimpleSerializer(read_only=True)
    branch_id = serializers.UUIDField(write_only=True)
    
    class Meta:
        model = InstructorBranch
        fields = ['id', 'branch', 'branch_id', 'created_at']
        read_only_fields = ['id', 'created_at']


class InstructorBonusSerializer(serializers.ModelSerializer):
    """Serializer for instructor bonuses"""
    instructor_name = serializers.CharField(source='instructor.full_name', read_only=True)
    
    class Meta:
        model = InstructorBonus
        fields = [
            'id', 'instructor', 'instructor_name', 'bonus_type', 'amount', 
            'bonus_date', 'description', 'notes', 'period_start', 'period_end',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class InstructorDropdownSerializer(serializers.ModelSerializer):
    """Lightweight serializer for instructor pickers (no financial metrics)."""
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = Instructor
        fields = ['id', 'full_name', 'first_name', 'last_name', 'fixed_salary_per_lesson']


class InstructorListSerializer(serializers.ModelSerializer):
    """Serializer for instructor list with basic info"""
    full_name = serializers.CharField(read_only=True)
    primary_branch_name = serializers.CharField(source='primary_branch.name', read_only=True, allow_null=True)
    branches = serializers.SerializerMethodField()
    
    # Financial metrics (will be added by viewset)
    lessons_count = serializers.IntegerField(read_only=True, required=False)
    students_count = serializers.IntegerField(read_only=True, required=False)
    revenue = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, required=False)
    salary = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, required=False)
    profit = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, required=False)
    salary_is_finalized = serializers.BooleanField(read_only=True, required=False)
    cancelled_count = serializers.IntegerField(read_only=True, required=False)
    avg_attendance_rate = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True, required=False)
    bonuses_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, required=False)
    
    class Meta:
        model = Instructor
        fields = [
            'id', 'first_name', 'last_name', 'full_name', 'phone', 'email',
            'specialization', 'primary_branch', 'primary_branch_name', 'branches',
            'salary_model_type', 'fixed_salary_per_lesson', 'is_active',
            'lessons_count', 'students_count', 'revenue', 'salary', 'profit',
            'cancelled_count', 'avg_attendance_rate', 'salary_is_finalized', 'bonuses_amount',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'full_name', 'created_at', 'updated_at']
    
    def get_branches(self, obj):
        """Get all branches the instructor is assigned to"""
        branches = []

        # Include primary branch (if exists)
        if obj.primary_branch_id and obj.primary_branch:
            branches.append({'id': str(obj.primary_branch_id), 'name': obj.primary_branch.name})

        # Include additional assigned branches (uses prefetch cache; .select_related here would re-query per row)
        branch_assignments = obj.branch_assignments.all()
        for ba in branch_assignments:
            branches.append({'id': str(ba.branch.id), 'name': ba.branch.name})

        # De-dupe by id while preserving order
        seen = set()
        deduped = []
        for b in branches:
            if b['id'] in seen:
                continue
            seen.add(b['id'])
            deduped.append(b)
        return deduped


class InstructorDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for single instructor view"""
    full_name = serializers.CharField(read_only=True)
    primary_branch_name = serializers.CharField(source='primary_branch.name', read_only=True, allow_null=True)
    branches = InstructorBranchSerializer(source='branch_assignments', many=True, read_only=True)
    salary_tiers = InstructorSalaryTierSerializer(many=True, read_only=True)
    bonuses = InstructorBonusSerializer(many=True, read_only=True)
    
    # Financial summary (will be added by viewset)
    total_students = serializers.IntegerField(read_only=True, required=False)
    total_revenue = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, required=False)
    total_salary = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, required=False)
    total_profit = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, required=False)
    
    # Lessons and courses (will be added by viewset)
    lessons = serializers.SerializerMethodField()
    courses = serializers.SerializerMethodField()
    
    class Meta:
        model = Instructor
        fields = [
            'id', 'first_name', 'last_name', 'full_name', 'phone', 'email',
            'specialization', 'primary_branch', 'primary_branch_name', 'branches',
            'salary_model_type', 'fixed_salary_per_lesson', 'salary_tiers',
            'is_active', 'bonuses', 'total_students', 'total_revenue',
            'total_salary', 'total_profit', 'lessons', 'courses',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'full_name', 'created_at', 'updated_at']
    
    def get_lessons(self, obj):
        """Get lessons data - will be populated by viewset"""
        return self.context.get('lessons', [])
    
    def get_courses(self, obj):
        """Get unique courses taught - will be populated by viewset"""
        return self.context.get('courses', [])


class InstructorCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for creating and updating instructors"""
    salary_tiers = InstructorSalaryTierSerializer(many=True, required=False)
    branch_ids = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        allow_empty=True
    )
    
    class Meta:
        model = Instructor
        fields = [
            'id', 'first_name', 'last_name', 'phone', 'email', 'specialization',
            'primary_branch', 'salary_model_type', 'fixed_salary_per_lesson',
            'salary_tiers', 'branch_ids', 'is_active'
        ]
        read_only_fields = ['id']
    
    def validate_phone(self, value):
        """Ensure phone number is unique"""
        instructor_id = self.instance.id if self.instance else None
        if Instructor.objects.filter(phone=value).exclude(id=instructor_id).exists():
            raise serializers.ValidationError("מדריך עם מספר טלפון זה כבר קיים במערכת")
        return value
    
    def validate_salary_tiers(self, value):
        """Validate salary tier structure"""
        if not value:
            return value
        
        # Sort by min_students
        sorted_tiers = sorted(value, key=lambda x: x['min_students'])
        
        # Check first tier starts from 1
        if sorted_tiers[0]['min_students'] != 1:
            raise serializers.ValidationError("המדרגה הראשונה חייבת להתחיל מ-1 תלמיד")
        
        # Check for gaps and overlaps
        for i in range(len(sorted_tiers) - 1):
            current = sorted_tiers[i]
            next_tier = sorted_tiers[i + 1]
            
            if current['max_students'] is None:
                raise serializers.ValidationError("רק המדרגה האחרונה יכולה להיות ללא מקסימום")
            
            if current['max_students'] + 1 != next_tier['min_students']:
                raise serializers.ValidationError("אין רצף בין המדרגות - קיים פער או חפיפה")
        
        # Last tier can have null max_students
        return value
    
    def validate(self, data):
        """Cross-field validation"""
        salary_model_type = data.get('salary_model_type', self.instance.salary_model_type if self.instance else 'fixed_per_lesson')
        
        if salary_model_type == 'fixed_per_lesson':
            if 'fixed_salary_per_lesson' not in data and not (self.instance and self.instance.fixed_salary_per_lesson):
                raise serializers.ValidationError({
                    'fixed_salary_per_lesson': 'שדה זה נדרש עבור מודל שכר קבוע'
                })
        
        if salary_model_type == 'tiered_by_students':
            if 'salary_tiers' not in data and not (self.instance and self.instance.salary_tiers.exists()):
                raise serializers.ValidationError({
                    'salary_tiers': 'חובה להגדיר מדרגות שכר עבור מודל מדורג'
                })
        
        return data
    
    def create(self, validated_data):
        """Create instructor with salary tiers and branch associations"""
        salary_tiers_data = validated_data.pop('salary_tiers', [])
        branch_ids = validated_data.pop('branch_ids', [])
        
        instructor = Instructor.objects.create(**validated_data)
        
        # Create salary tiers
        for tier_data in salary_tiers_data:
            InstructorSalaryTier.objects.create(instructor=instructor, **tier_data)
        
        # Create branch associations (always include primary branch)
        branch_ids_set = {str(bid) for bid in branch_ids if bid}
        if instructor.primary_branch_id:
            branch_ids_set.add(str(instructor.primary_branch_id))

        for branch_id in branch_ids_set:
            InstructorBranch.objects.get_or_create(instructor=instructor, branch_id=branch_id)
        
        return instructor
    
    def update(self, instance, validated_data):
        """Update instructor with salary tiers and branch associations"""
        salary_tiers_data = validated_data.pop('salary_tiers', None)
        branch_ids = validated_data.pop('branch_ids', None)
        
        # Update basic fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        # Update salary tiers if provided
        if salary_tiers_data is not None:
            # Delete existing tiers
            instance.salary_tiers.all().delete()
            # Create new tiers
            for tier_data in salary_tiers_data:
                InstructorSalaryTier.objects.create(instructor=instance, **tier_data)
        
        # Update branch associations if provided
        if branch_ids is not None:
            # Delete existing associations
            instance.branch_assignments.all().delete()
            # Create new associations (always include primary branch)
            branch_ids_set = {str(bid) for bid in branch_ids if bid}
            if instance.primary_branch_id:
                branch_ids_set.add(str(instance.primary_branch_id))

            for branch_id in branch_ids_set:
                InstructorBranch.objects.get_or_create(instructor=instance, branch_id=branch_id)
        
        return instance


class InstructorMonthlySnapshotSerializer(serializers.ModelSerializer):
    """Serializer for instructor monthly snapshots"""
    instructor_name = serializers.CharField(source='instructor.full_name', read_only=True)
    
    class Meta:
        model = InstructorMonthlySnapshot
        fields = [
            'id', 'instructor', 'instructor_name', 'month', 'total_lessons',
            'total_students', 'total_revenue', 'total_salary', 'profit',
            'cancelled_count', 'avg_attendance_rate', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class LessonMonthlySnapshotSerializer(serializers.ModelSerializer):
    """Serializer for lesson monthly snapshots"""
    instructor_name = serializers.CharField(source='instructor.full_name', read_only=True)
    course_name = serializers.CharField(source='course.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    
    class Meta:
        model = LessonMonthlySnapshot
        fields = [
            'id', 'lesson', 'instructor', 'instructor_name', 'course', 'course_name',
            'branch', 'branch_name', 'month', 'enrolled_students', 'revenue',
            'instructor_salary', 'profit', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class BranchMonthlySnapshotSerializer(serializers.ModelSerializer):
    """Serializer for branch monthly snapshots"""
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    
    class Meta:
        model = BranchMonthlySnapshot
        fields = [
            'id', 'branch', 'branch_name', 'month', 'total_students',
            'total_revenue', 'instructor_costs', 'profit', 'active_courses_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
