"""
Serializers for Customer models
"""
from rest_framework import serializers
from datetime import date
from apps.customers.models import (
    Family, Parent, Child, Payment, RecurringPayment,
    TranzilaTransaction, PaymentDiscountSnapshot, BusinessCustomer
)
# Store models moved to apps.store
from apps.customers.financial_models import Discount
from apps.enrollments.models import LessonEnrollment, LessonAttendance


class ParentSerializer(serializers.ModelSerializer):
    """הורה"""
    full_name = serializers.CharField(read_only=True)
    class Meta:
        model = Parent
        fields = ['id', 'family', 'first_name', 'last_name', 'phone', 'email', 'is_primary', 'full_name']
        read_only_fields = ['id', 'full_name']


class FamilySerializer(serializers.ModelSerializer):
    """משפחה"""
    parents = ParentSerializer(many=True, read_only=True)
    
    class Meta:
        model = Family
        fields = ['id', 'name', 'phone', 'email', 'address', 'parent_id_number', 'branch', 'notes', 'parents']
        read_only_fields = ['id', 'parents']


class EnrollmentDetailSerializer(serializers.Serializer):
    """פרטי רישום לשיעור"""
    lesson_id = serializers.UUIDField()
    enrollment_id = serializers.UUIDField()
    course_name = serializers.CharField()
    course_id = serializers.UUIDField()
    day_of_week = serializers.IntegerField()
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()
    branch_name = serializers.CharField()
    instructor_name = serializers.CharField()
    status = serializers.CharField()


class ChildSerializer(serializers.ModelSerializer):
    """ילד - סידור בסיסי"""
    family_name = serializers.CharField(source='family.name', read_only=True)
    age = serializers.IntegerField(read_only=True)
    is_ghost_visible = serializers.SerializerMethodField()
    
    class Meta:
        model = Child
        fields = [
            'id', 'first_name', 'last_name', 'family', 'family_name',
            'birth_date', 'gender', 'age', 'status', 'is_ghost_visible',
            'subscription_start_date', 'subscription_end_date', 'notes',
            'created_at', 'updated_at'
        ]
    
    def get_is_ghost_visible(self, obj):
        """Check if ghost child should be visible (created within 30 days)"""
        if obj.status != 'ghost':
            return True  # Non-ghost children are always visible
        
        from datetime import timedelta
        from django.utils import timezone
        
        # Ghost children are visible for 30 days from creation
        threshold_date = timezone.now() - timedelta(days=30)
        return obj.created_at >= threshold_date


class ChildWithDetailsSerializer(serializers.ModelSerializer):
    """ילד עם כל הפרטים לדף לקוחות"""
    family_name = serializers.CharField(source='family.name', read_only=True)
    family_phone = serializers.CharField(source='family.phone', read_only=True)
    branch_id = serializers.SerializerMethodField()
    branch_name = serializers.SerializerMethodField()
    age = serializers.IntegerField(read_only=True)
    
    # Parent info
    parent_name = serializers.SerializerMethodField()
    parent_phone = serializers.SerializerMethodField()
    parent_id = serializers.SerializerMethodField()
    
    # Enrollment info
    enrollments = serializers.SerializerMethodField()
    
    # Attendance (computed from annotated counts to avoid N+1)
    attendance_rate = serializers.SerializerMethodField()
    is_ghost_visible = serializers.SerializerMethodField()
    
    class Meta:
        model = Child
        fields = [
            'id', 'first_name', 'last_name', 'full_name',
            'birth_date', 'gender', 'age', 'id_number', 'phone_number',
            'family_id', 'family_name', 'family_phone',
            'branch_id', 'branch_name',
            'parent_name', 'parent_phone', 'parent_id',
            # NEW status fields
            'status', 'paid_until_date', 'trial_classes_attended',
            'absent_irregularly', 'is_ghost_visible',
            # Subscription dates (kept for reference)
            'subscription_start_date', 'subscription_end_date',
            'enrollments', 'attendance_rate',
            'created_at'
        ]

    def _partner_branch_ids(self):
        request = self.context.get('request')
        if not request:
            return None
        from apps.core.scoping import is_scoped_partner, partner_branch_ids
        if not is_scoped_partner(request.user):
            return None
        return partner_branch_ids(request.user)

    def _primary_parent(self, obj):
        """Use prefetched family.parents to avoid per-child parent queries."""
        parents = list(obj.family.parents.all())
        for parent in parents:
            if parent.is_primary:
                return parent
        return parents[0] if parents else None

    def get_branch_id(self, obj):
        partner_ids = self._partner_branch_ids()
        if partner_ids is not None:
            from apps.core.scoping import partner_child_display_branch
            branch_id, _name = partner_child_display_branch(obj, partner_ids)
            return str(branch_id) if branch_id else None
        return str(obj.family.branch_id) if obj.family.branch_id else None

    def get_branch_name(self, obj):
        partner_ids = self._partner_branch_ids()
        if partner_ids is not None:
            from apps.core.scoping import partner_child_display_branch
            _branch_id, name = partner_child_display_branch(obj, partner_ids)
            return name
        return obj.family.branch.name if obj.family.branch else None
    
    def get_parent_name(self, obj):
        """
        שם הורה ראשי
        
        USAGE: Used in ChildWithDetailsSerializer
        Returns primary parent name or first parent name
        """
        parent = self._primary_parent(obj)
        return parent.full_name if parent else None
    
    def get_parent_phone(self, obj):
        """
        טלפון הורה ראשי
        
        USAGE: Used in ChildWithDetailsSerializer
        Returns primary parent phone or fallback to family phone
        """
        parent = self._primary_parent(obj)
        if parent:
            return parent.phone
        return obj.family.phone
    
    def get_parent_id(self, obj):
        """
        מזהה הורה ראשי
        
        USAGE: Used in ChildWithDetailsSerializer
        Returns primary parent ID or first parent ID
        """
        parent = self._primary_parent(obj)
        return str(parent.id) if parent else None
    
    def get_enrollments(self, obj):
        """
        רשימת רישומים פעילים - from both Course and Lesson enrollments
        
        USAGE: Used in ChildWithDetailsSerializer to display child's enrollments
        """
        result = []
        seen_courses = set()
        
        # Course-level enrollments (prefetched on ChildViewSet queryset)
        for enrollment in obj.enrollments.all():
            course = enrollment.course
            if str(course.id) not in seen_courses:
                seen_courses.add(str(course.id))
                result.append({
                    'enrollment_id': str(enrollment.id),
                    'course_name': course.name,
                    'course_id': str(course.id),
                    'branch_name': course.branch.name if course.branch else None,
                    'instructor_name': None,  # Instructor is now lesson-specific
                    'day_of_week': None,
                    'start_time': None,
                    'end_time': None,
                    'lesson_id': None,
                    'status': 'active'
                })
        
        # Lesson-level enrollments (prefetched; filter active in Python)
        for enrollment in obj.lesson_enrollments.all():
            if enrollment.status != 'active':
                continue
            lesson = enrollment.lesson
            course_id = str(lesson.course.id)
            if course_id not in seen_courses:
                seen_courses.add(course_id)
                result.append({
                    'lesson_id': str(lesson.id),
                    'enrollment_id': str(enrollment.id),
                    'course_name': lesson.course.name,
                    'course_id': course_id,
                    'day_of_week': lesson.day_of_week,
                    'start_time': lesson.start_time.strftime('%H:%M') if lesson.start_time else None,
                    'end_time': lesson.end_time.strftime('%H:%M') if lesson.end_time else None,
                    'branch_name': lesson.course.branch.name if lesson.course and lesson.course.branch_id else None,
                    'instructor_name': lesson.instructor.full_name if lesson.instructor else None,
                    'status': enrollment.status
                })
        
        return result
    
    def get_attendance_rate(self, obj):
        """
        אחוז נוכחות (no DB queries here; relies on queryset annotations)
        
        USAGE: Used in ChildWithDetailsSerializer
        Calculates attendance percentage from annotated fields
        Falls back to DB query if annotations not present
        """
        total = getattr(obj, 'attendance_total', None)
        present = getattr(obj, 'attendance_present', None)

        # Fallback (shouldn't happen if queryset annotation is applied)
        if total is None or present is None:
            total = LessonAttendance.objects.filter(child=obj).count()
            if total == 0:
                return 0
            present = LessonAttendance.objects.filter(child=obj, status='present').count()

        if not total:
            return 0

        return round((present / total) * 100, 1)
    
    def get_is_ghost_visible(self, obj):
        """Check if ghost child should be visible (created within 30 days)"""
        if obj.status != 'ghost':
            return True  # Non-ghost children are always visible
        
        from datetime import timedelta
        from django.utils import timezone
        
        # Ghost children are visible for 30 days from creation
        threshold_date = timezone.now() - timedelta(days=30)
        return obj.created_at >= threshold_date


class ChildCreateSerializer(serializers.ModelSerializer):
    """יצירת ילד חדש"""
    class Meta:
        model = Child
        fields = [
            'family', 'first_name', 'last_name', 'birth_date', 'gender',
            'id_number', 'phone_number', 'status', 'subscription_start_date',
            'subscription_end_date', 'notes'
        ]


class ChildUpdateSerializer(serializers.ModelSerializer):
    """עדכון ילד"""
    class Meta:
        model = Child
        fields = [
            'first_name', 'last_name', 'birth_date', 'gender',
            'id_number', 'phone_number', 'status', 'subscription_start_date',
            'subscription_end_date', 'paid_until_date', 'trial_classes_attended', 
            'absent_irregularly', 'notes'
        ]


# Store serializers moved to apps.store.serializers


class DiscountSerializer(serializers.ModelSerializer):
    """הנחה - Full serializer for all discount operations"""
    
    class Meta:
        model = Discount
        fields = [
            'id', 'name', 'description', 'discount_type', 'value',
            'applies_to', 'promotion_type', 'start_date', 'end_date',
            'is_built_in', 'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'is_built_in', 'created_at', 'updated_at']
    
    def validate(self, data):
        """Validate discount data"""
        # If it's a temporary discount, ensure dates are provided
        if data.get('promotion_type') == 'temporary':
            if not data.get('start_date') or not data.get('end_date'):
                raise serializers.ValidationError(
                    "הנחות זמניות חייבות לכלול תאריך התחלה וסיום"
                )
            
            # Ensure end_date is after start_date
            if data.get('start_date') and data.get('end_date'):
                if data['end_date'] < data['start_date']:
                    raise serializers.ValidationError(
                        "תאריך הסיום חייב להיות אחרי תאריך ההתחלה"
                    )
        
        # Validate fixed_final_price discount type
        if data.get('discount_type') == 'fixed_final_price':
            value = data.get('value', 0)
            if value <= 0:
                raise serializers.ValidationError(
                    "מחיר סופי קבוע חייב להיות גדול מ-0"
                )
        
        return data


class EarlySignupDiscountSerializer(serializers.ModelSerializer):
    """הנחת רישום מוקדם - Simplified for early signup discounts"""
    
    name = serializers.CharField(required=False, allow_blank=True, max_length=200)
    
    class Meta:
        model = Discount
        fields = [
            'id', 'name', 'value', 'start_date', 'end_date', 'is_active'
        ]
        read_only_fields = ['id']
    
    def validate(self, data):
        """Validate early signup discount"""
        # Get instance for updates
        instance = self.instance
        
        # For updates, merge with existing data
        if instance:
            start_date = data.get('start_date', instance.start_date)
            end_date = data.get('end_date', instance.end_date)
        else:
            start_date = data.get('start_date')
            end_date = data.get('end_date')
            
            # For creation, dates are required
            if not start_date or not end_date:
                raise serializers.ValidationError(
                    "חובה להזין תאריך התחלה וסיום"
                )
        
        # Validate date order if both are present
        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError(
                "תאריך הסיום חייב להיות אחרי תאריך ההתחלה"
            )
        
        return data
    
    def create(self, validated_data):
        """Create early signup discount with proper defaults"""
        validated_data['discount_type'] = 'fixed'
        validated_data['applies_to'] = 'family'
        validated_data['promotion_type'] = 'temporary'
        validated_data['is_built_in'] = True
        
        # Auto-generate name if not provided
        if not validated_data.get('name'):
            start = validated_data['start_date'].strftime('%d/%m/%Y')
            end = validated_data['end_date'].strftime('%d/%m/%Y')
            validated_data['name'] = f"הנחת רישום מוקדם {start} - {end}"
        
        return super().create(validated_data)
    
    def update(self, instance, validated_data):
        """Update early signup discount"""
        # Update name if dates changed and name wasn't explicitly set
        if ('start_date' in validated_data or 'end_date' in validated_data) and 'name' not in validated_data:
            start = validated_data.get('start_date', instance.start_date)
            end = validated_data.get('end_date', instance.end_date)
            validated_data['name'] = f"הנחת רישום מוקדם {start.strftime('%d/%m/%Y')} - {end.strftime('%d/%m/%Y')}"
        
        return super().update(instance, validated_data)


class SecondChildDiscountSerializer(serializers.ModelSerializer):
    """הנחת ילד שני - Simplified for second child discount"""
    
    class Meta:
        model = Discount
        fields = ['id', 'discount_type', 'value', 'is_active']
        read_only_fields = ['id']
    
    def validate(self, data):
        """Validate second child discount"""
        # If discount_type is fixed_final_price, validate value
        discount_type = data.get('discount_type', getattr(self.instance, 'discount_type', 'fixed'))
        value = data.get('value', 0)
        
        if discount_type == 'fixed_final_price' and value <= 0:
            raise serializers.ValidationError(
                "מחיר סופי קבוע חייב להיות גדול מ-0"
            )
        
        return data


class AdditionalLessonDiscountSerializer(serializers.ModelSerializer):
    """הנחת שיעור נוסף - Simplified for additional lesson discount"""
    
    class Meta:
        model = Discount
        fields = ['id', 'value', 'is_active']
        read_only_fields = ['id']
    
    def validate(self, data):
        """Validate additional lesson discount"""
        value = data.get('value', 0)
        if value <= 0:
            raise serializers.ValidationError(
                "מחיר שיעור נוסף חייב להיות גדול מ-0"
            )
        return data


# ============================================================================
# Payment Serializers - Tranzila Integration
# ============================================================================

class PaymentDiscountSnapshotSerializer(serializers.ModelSerializer):
    """צילום הנחה בתשלום"""
    
    class Meta:
        model = PaymentDiscountSnapshot
        fields = [
            'id', 'discount_name', 'discount_type', 'discount_value',
            'amount_deducted', 'reason', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class TranzilaTransactionSerializer(serializers.ModelSerializer):
    """עסקת טרנזילה"""
    
    class Meta:
        model = TranzilaTransaction
        fields = [
            'id', 'transaction_id', 'confirmation_code', 'transaction_type',
            'response_code', 'response_message', 'is_successful',
            'request_timestamp', 'response_timestamp', 'created_at'
        ]
        read_only_fields = fields


class PaymentSerializer(serializers.ModelSerializer):
    """תשלום - Full payment details"""
    child_name = serializers.CharField(source='child.full_name', read_only=True)
    family_name = serializers.CharField(source='family.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    lesson_name = serializers.SerializerMethodField()
    discount_snapshots = PaymentDiscountSnapshotSerializer(many=True, read_only=True)
    tranzila_transaction = TranzilaTransactionSerializer(read_only=True)
    
    def get_lesson_name(self, obj):
        if obj.lesson and obj.lesson.course:
            return obj.lesson.course.name
        return None
    
    class Meta:
        model = Payment
        fields = [
            'id', 'child', 'child_name', 'parent', 'family', 'family_name',
            'branch', 'branch_name', 'lesson', 'lesson_name',
            'payment_type', 'status', 'base_amount', 'discount_amount',
            'final_amount', 'description', 'payment_date', 'failure_reason',
            'failure_code', 'discount_snapshots', 'tranzila_transaction',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'child_name', 'family_name', 'branch_name', 'lesson_name',
            'discount_snapshots', 'tranzila_transaction', 'created_at', 'updated_at'
        ]


class RecurringPaymentSerializer(serializers.ModelSerializer):
    """מנוי חוזר - Recurring subscription details"""
    child_name = serializers.CharField(source='child.full_name', read_only=True)
    initial_payment_details = PaymentSerializer(source='initial_payment', read_only=True)
    
    class Meta:
        model = RecurringPayment
        fields = [
            'id', 'child', 'child_name', 'initial_payment', 'initial_payment_details',
            'status', 'amount', 'billing_day', 'start_date', 'end_date',
            'next_billing_date', 'last_charge_date', 'cancelled_at',
            'cancellation_reason', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'child_name', 'initial_payment_details', 'tranzila_token',
            'tranzila_recurring_index', 'created_at', 'updated_at'
        ]


class PaymentInitiationRequestSerializer(serializers.Serializer):
    """בקשה ליצירת תשלום - Request to initiate payment"""
    child_id = serializers.UUIDField(required=True)
    lesson_id = serializers.UUIDField(required=False)
    amount = serializers.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        required=False,
        help_text="Required for one-time payments"
    )
    description = serializers.CharField(required=False, allow_blank=True)
    payment_date = serializers.DateField(required=False)
    success_url = serializers.URLField(required=False, allow_blank=True)
    error_url = serializers.URLField(required=False, allow_blank=True)
    callback_url = serializers.URLField(required=False, allow_blank=True)


class PaymentInitiationResponseSerializer(serializers.Serializer):
    """תגובה ליצירת תשלום - Payment initiation response"""
    payment_id = serializers.UUIDField()
    tranzila_url = serializers.URLField()
    base_amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    discount_amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    final_amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    discounts_applied = serializers.ListField(
        child=serializers.DictField(),
        required=False
    )
    lesson = serializers.DictField(required=False)


class WebhookCallbackSerializer(serializers.Serializer):
    """Tranzila Webhook Callback - Validates incoming webhook data"""
    Response = serializers.CharField(required=False)
    ConfirmationCode = serializers.CharField(required=False)
    index = serializers.CharField(required=False)
    TranzilaTK = serializers.CharField(required=False)
    sum = serializers.CharField(required=False)
    currency = serializers.CharField(required=False)
    ccno = serializers.CharField(required=False)
    cardtype = serializers.CharField(required=False)
    error = serializers.CharField(required=False, allow_blank=True)
    errormessage = serializers.CharField(required=False, allow_blank=True)
    pdesc = serializers.CharField(required=False)  # Our payment.id (sent as pdesc to Tranzila)
    
    # Allow additional fields
    def to_internal_value(self, data):
        """Allow any additional fields from Tranzila"""
        return data


class RecurringPaymentUpdateSerializer(serializers.Serializer):
    """עדכון מנוי חוזר - Update recurring payment"""
    recalculate_discounts = serializers.BooleanField(default=True)


class RecurringPaymentCancelSerializer(serializers.Serializer):
    """ביטול מנוי חוזר - Cancel recurring payment"""
    cancellation_reason = serializers.CharField(required=False, allow_blank=True)


class BusinessCustomerSerializer(serializers.ModelSerializer):
    full_name = serializers.ReadOnlyField()

    class Meta:
        model = BusinessCustomer
        fields = [
            'id', 'first_name', 'last_name', 'full_name',
            'email', 'phone', 'id_number', 'company_number', 'address',
            'business_type', 'category', 'notes',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'full_name', 'created_at', 'updated_at']

