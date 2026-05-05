from rest_framework import serializers
from apps.scheduling.models import ScheduleEvent


class ScheduleEventSerializer(serializers.ModelSerializer):
    """Serializer for schedule events"""
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    studio_name = serializers.CharField(source='studio.name', read_only=True, allow_null=True)
    city_name = serializers.CharField(source='city.name', read_only=True, allow_null=True)
    assigned_instructor_names = serializers.SerializerMethodField()
    
    class Meta:
        model = ScheduleEvent
        fields = [
            'id', 'name', 'event_date', 'start_time', 'end_time',
            'event_type', 'is_daily_event', 'branch', 'branch_name',
            'studio', 'studio_name', 'city', 'city_name', 'location', 
            'assigned_instructors', 'assigned_instructor_names',
            'color', 'notes', 'files', 'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_assigned_instructor_names(self, obj):
        """Get list of assigned instructor names"""
        return [f"{instructor.first_name} {instructor.last_name}" 
                for instructor in obj.assigned_instructors.all()]
    
    def validate(self, data):
        """Validate that timed events have start and end times, and city is required"""
        is_daily = data.get('is_daily_event', False)
        start_time = data.get('start_time')
        end_time = data.get('end_time')
        city = data.get('city')
        
        # City is required
        if not city:
            raise serializers.ValidationError({
                'city': 'עיר נדרשת'
            })
        
        # Validate times for non-daily events
        if not is_daily:
            if not start_time:
                raise serializers.ValidationError({
                    'start_time': 'שעת התחלה נדרשת לאירועים שאינם יומיים'
                })
            if not end_time:
                raise serializers.ValidationError({
                    'end_time': 'שעת סיום נדרשת לאירועים שאינם יומיים'
                })
        
        return data


class ScheduleEventListSerializer(serializers.ModelSerializer):
    """Simplified serializer for event list views"""
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    studio_name = serializers.CharField(source='studio.name', read_only=True, allow_null=True)
    city_name = serializers.CharField(source='city.name', read_only=True, allow_null=True)
    
    class Meta:
        model = ScheduleEvent
        fields = [
            'id', 'name', 'event_date', 'start_time', 'end_time',
            'event_type', 'is_daily_event', 'branch_name', 'studio_name',
            'city_name', 'location', 'color', 'notes', 'is_active'
        ]

