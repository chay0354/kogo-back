from rest_framework import serializers
from apps.core.models import City, Branch, Room, BranchFile


class CitySerializer(serializers.ModelSerializer):
    class Meta:
        model = City
        fields = ['id', 'name', 'created_at', 'updated_at']


class RoomSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    
    class Meta:
        model = Room
        fields = ['id', 'name', 'capacity', 'purpose', 'notes', 'branch', 'branch_name', 'is_active', 'created_at', 'updated_at']


class BranchListSerializer(serializers.ModelSerializer):
    """Simple branch list for dropdowns"""
    city_name = serializers.CharField(source='city.name', read_only=True, allow_null=True)

    class Meta:
        model = Branch
        fields = ['id', 'name', 'city', 'city_name']


class BranchSerializer(serializers.ModelSerializer):
    """Full branch serializer with all fields"""
    city_name = serializers.CharField(source='city.name', read_only=True, allow_null=True)
    
    class Meta:
        model = Branch
        fields = [
            'id', 'name', 'address', 'phone', 'email', 'manager_name', 
            'city', 'city_name', 
            'branch_codes', 'cleaning_managers', 'cleaning_cost', 'monthly_cost',
            'wifi_name', 'wifi_code', 'bluetooth_codes', 'custom_details',
            'is_external', 'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class BranchDetailSerializer(serializers.ModelSerializer):
    """Branch serializer with nested rooms"""
    city_name = serializers.CharField(source='city.name', read_only=True, allow_null=True)
    rooms = RoomSerializer(many=True, read_only=True)
    rooms_count = serializers.SerializerMethodField()

    class Meta:
        model = Branch
        fields = [
            'id', 'name', 'address', 'phone', 'email', 'manager_name',
            'city', 'city_name',
            'branch_codes', 'cleaning_managers', 'cleaning_cost', 'monthly_cost',
            'wifi_name', 'wifi_code', 'bluetooth_codes', 'custom_details',
            'is_external', 'is_active', 'created_at', 'updated_at',
            'rooms', 'rooms_count'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_rooms_count(self, obj):
        return obj.rooms.filter(is_active=True).count()


class BranchWithStatsSerializer(serializers.ModelSerializer):
    """Branch serializer with statistics for list view"""
    city_name = serializers.CharField(source='city.name', read_only=True, allow_null=True)
    families_count = serializers.IntegerField(read_only=True)
    courses_count = serializers.IntegerField(read_only=True)
    instructors_count = serializers.IntegerField(read_only=True)
    rooms_count = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = Branch
        fields = [
            'id', 'name', 'address', 'city_name', 
            'branch_codes', 'monthly_cost', 'is_external', 'is_active',
            'families_count', 'courses_count', 'instructors_count', 'rooms_count'
        ]


class BranchFileSerializer(serializers.ModelSerializer):
    """Serializer for branch files"""
    file_url = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    
    class Meta:
        model = BranchFile
        fields = [
            'id', 'branch', 'branch_name', 'file_name', 'file_type', 
            'file', 'file_url', 'file_size', 'mime_type', 
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'file_url']
    
    def get_file_url(self, obj):
        request = self.context.get('request')
        if obj.file and hasattr(obj.file, 'url'):
            if request is not None:
                return request.build_absolute_uri(obj.file.url)
            return obj.file.url
        return None
    
    def validate_file(self, value):
        """Validate file size and type"""
        # Max file size: 50MB
        max_size = 50 * 1024 * 1024
        if value.size > max_size:
            raise serializers.ValidationError("File size cannot exceed 50MB")
        
        # Allowed file types
        allowed_types = [
            'application/pdf',
            'application/msword',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'application/vnd.ms-excel',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'image/jpeg',
            'image/jpg',
            'image/png',
            'image/gif',
            'image/webp',
            'video/mp4',
            'video/webm',
            'video/quicktime',
        ]
        
        if value.content_type not in allowed_types:
            raise serializers.ValidationError(
                f"File type {value.content_type} is not supported. "
                "Allowed types: PDF, DOC, XLSX, images (JPEG, PNG, GIF, WEBP), videos (MP4, WEBM, MOV)"
            )
        
        return value
    
    def create(self, validated_data):
        # Automatically set file metadata
        file = validated_data.get('file')
        if file:
            validated_data['file_size'] = file.size
            validated_data['mime_type'] = file.content_type
            validated_data['file_name'] = file.name
            
            # Determine file type based on mime type
            if file.content_type.startswith('video/'):
                validated_data['file_type'] = 'video'
            else:
                validated_data['file_type'] = 'document'
        
        return super().create(validated_data)
