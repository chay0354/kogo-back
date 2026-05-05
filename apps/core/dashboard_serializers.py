"""
Dashboard Serializers
Defines the structure of dashboard API responses
"""
from rest_framework import serializers


class KPISerializer(serializers.Serializer):
    """Base KPI serializer"""
    pass


class FinancialKPISerializer(KPISerializer):
    """Financial KPIs"""
    total_revenue = serializers.FloatField()
    total_expenses = serializers.FloatField()
    net_profit = serializers.FloatField()
    collection_rate = serializers.FloatField()


class BranchRevenueSerializer(serializers.Serializer):
    """Revenue by branch"""
    branch_name = serializers.CharField()
    branch_id = serializers.CharField()
    revenue = serializers.FloatField()
    expenses = serializers.FloatField()
    profit = serializers.FloatField()


class MonthlyTrendSerializer(serializers.Serializer):
    """Monthly trend data"""
    month = serializers.CharField()
    revenue = serializers.FloatField()
    expenses = serializers.FloatField()


class InstructorRevenueSerializer(serializers.Serializer):
    """Revenue by instructor"""
    instructor_name = serializers.CharField()
    instructor_id = serializers.CharField()
    revenue = serializers.FloatField()
    salary = serializers.FloatField()
    profit = serializers.FloatField()


class FinancialDataSerializer(serializers.Serializer):
    """Complete financial dashboard data"""
    kpis = FinancialKPISerializer()
    revenue_by_branch = BranchRevenueSerializer(many=True)
    monthly_trends = MonthlyTrendSerializer(many=True)
    revenue_by_instructor = InstructorRevenueSerializer(many=True)


class InstructorsKPISerializer(KPISerializer):
    """Instructors KPIs"""
    active_instructors = serializers.IntegerField()
    total_lessons = serializers.IntegerField()
    total_salary = serializers.FloatField()
    total_profit = serializers.FloatField()


class InstructorDetailSerializer(serializers.Serializer):
    """Instructor detail data"""
    instructor_id = serializers.CharField()
    name = serializers.CharField()
    branch = serializers.CharField()
    lessons = serializers.IntegerField()
    students = serializers.IntegerField()
    revenue = serializers.FloatField()
    salary = serializers.FloatField()
    profit = serializers.FloatField()
    occupancy = serializers.IntegerField()
    attendance = serializers.IntegerField()


class InstructorsDataSerializer(serializers.Serializer):
    """Complete instructors dashboard data"""
    kpis = InstructorsKPISerializer()
    top_performers = serializers.DictField()
    instructor_comparison = InstructorDetailSerializer(many=True)
    instructor_details = InstructorDetailSerializer(many=True)


class StudentsKPISerializer(KPISerializer):
    """Students KPIs"""
    active_students = serializers.IntegerField()
    new_students = serializers.IntegerField()
    trial_lessons = serializers.IntegerField()
    conversion_rate = serializers.FloatField()
    avg_attendance = serializers.FloatField()


class StudentSerializer(serializers.Serializer):
    """Student data"""
    id = serializers.CharField()
    name = serializers.CharField()
    branch = serializers.CharField()
    course = serializers.CharField()
    attendance = serializers.FloatField()
    is_trial = serializers.BooleanField()


class StudentsDataSerializer(serializers.Serializer):
    """Complete students dashboard data"""
    kpis = StudentsKPISerializer()
    student_distribution = serializers.DictField()
    student_list = StudentSerializer(many=True)


class CoursesKPISerializer(KPISerializer):
    """Courses KPIs"""
    total_courses = serializers.IntegerField()
    active_courses = serializers.IntegerField()
    full_capacity = serializers.IntegerField()
    low_occupancy = serializers.IntegerField()


class CourseSerializer(serializers.Serializer):
    """Course data"""
    course_id = serializers.CharField()
    name = serializers.CharField()
    branch = serializers.CharField()
    lessons = serializers.IntegerField()
    students = serializers.IntegerField()
    occupancy = serializers.FloatField()
    revenue = serializers.FloatField()
    profit = serializers.FloatField()


class CoursesDataSerializer(serializers.Serializer):
    """Complete courses dashboard data"""
    kpis = CoursesKPISerializer()
    top_courses = CourseSerializer(many=True)
    low_occupancy_courses = CourseSerializer(many=True)
    course_list = CourseSerializer(many=True)


class BranchesKPISerializer(KPISerializer):
    """Branches KPIs"""
    active_branches = serializers.IntegerField()
    total_students = serializers.IntegerField()
    total_profit = serializers.FloatField()
    avg_room_utilization = serializers.IntegerField()


class BranchDetailSerializer(serializers.Serializer):
    """Branch detail data"""
    branch_id = serializers.CharField()
    name = serializers.CharField()
    students = serializers.IntegerField()
    lessons = serializers.IntegerField()
    rooms = serializers.IntegerField()
    room_utilization = serializers.IntegerField()
    revenue = serializers.FloatField()
    profit = serializers.FloatField()
    profit_margin = serializers.FloatField()


class BranchesDataSerializer(serializers.Serializer):
    """Complete branches dashboard data"""
    kpis = BranchesKPISerializer()
    branch_comparison = serializers.ListField()
    student_distribution = serializers.ListField()
    branch_list = BranchDetailSerializer(many=True)

