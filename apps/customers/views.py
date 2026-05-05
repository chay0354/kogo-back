import logging
from decimal import Decimal
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.db.models import Q, Prefetch, Count, Value, CharField
from django.db.models.functions import Concat
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from datetime import datetime, date
from apps.customers.models import Family, Parent, Child, Payment, RecurringPayment
# Store models moved to apps.store
from apps.customers.financial_models import Discount
from apps.customers.serializers import (
    FamilySerializer, ParentSerializer, ChildSerializer,
    ChildWithDetailsSerializer, ChildCreateSerializer, ChildUpdateSerializer,
    # Store serializers moved to apps.store.serializers
    DiscountSerializer, EarlySignupDiscountSerializer, SecondChildDiscountSerializer,
    AdditionalLessonDiscountSerializer,
    PaymentSerializer, RecurringPaymentSerializer,
    PaymentInitiationRequestSerializer, PaymentInitiationResponseSerializer,
    WebhookCallbackSerializer, RecurringPaymentUpdateSerializer,
    RecurringPaymentCancelSerializer
)
from apps.customers.discount_service import DiscountService
from apps.core.payment_service import PaymentService
from apps.enrollments.models import LessonEnrollment
from apps.core.permissions import IsManager

logger = logging.getLogger(__name__)


class FamilyViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Family
    
    USAGE: Used by frontend/src/app/customers/page.tsx
    - GET /api/v1/customers/families/ - Load families for dropdown
    - POST /api/v1/customers/families/ - Create new family
    """
    queryset = Family.objects.all().prefetch_related('parents', 'children')
    serializer_class = FamilySerializer
    permission_classes = [IsAuthenticated, IsManager]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'phone', 'email']
    ordering_fields = ['name', 'created_at']
    ordering = ['-created_at']


class ParentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Parent
    
    USAGE: Used by frontend/src/app/customers/page.tsx
    - POST /api/v1/customers/parents/ - Create parent for new family
    """
    queryset = Parent.objects.all().select_related('family')
    serializer_class = ParentSerializer
    permission_classes = [IsAuthenticated, IsManager]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['first_name', 'last_name', 'phone', 'email']
    ordering_fields = ['last_name', 'created_at']


class ChildViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Children with advanced filtering
    
    USAGE: Main endpoint used heavily by frontend/src/app/customers/page.tsx
    - GET /api/v1/customers/children/ - List children with filters
    - POST /api/v1/customers/children/ - Create new child
    - PUT /api/v1/customers/children/{id}/ - Update child
    - DELETE /api/v1/customers/children/{id}/ - Delete child
    - Custom actions: by_course, students_for_course, soft_delete, update_status
    """
    queryset = Child.objects.all().select_related(
        'family', 'family__branch'
    ).prefetch_related(
        'family__parents',
        Prefetch('lesson_enrollments', queryset=LessonEnrollment.objects.select_related(
            'lesson', 'lesson__course', 'lesson__branch', 'lesson__instructor'
        ))
    )
    permission_classes = [IsAuthenticated, IsManager]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['first_name', 'last_name', 'family__name', 'family__phone', 'id_number']
    ordering_fields = ['first_name', 'last_name', 'created_at', 'birth_date']
    ordering = ['-created_at']
    
    def get_serializer_class(self):
        """
        USAGE: Returns appropriate serializer based on action
        - list: ChildWithDetailsSerializer
        - create: ChildCreateSerializer
        - update/partial_update: ChildUpdateSerializer
        - default: ChildSerializer
        """
        if self.action == 'list':
            return ChildWithDetailsSerializer
        elif self.action == 'create':
            return ChildCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return ChildUpdateSerializer
        return ChildSerializer
    
    def get_queryset(self):
        """
        USAGE: Applies filters and annotations for child listing
        Filters: branch, course, instructor, age range, status
        Adds annotations for attendance_total and attendance_present
        """
        queryset = super().get_queryset()

        # Attendance aggregation (avoid N+1 in serializer)
        # NOTE: distinct=True protects counts from join-multiplication when other joins are added by filters.
        queryset = queryset.annotate(
            attendance_total=Count('attendance_records', distinct=True),
            attendance_present=Count(
                'attendance_records',
                filter=Q(attendance_records__status='present'),
                distinct=True,
            ),
        )
        
        # Filter by branch (only children enrolled in lessons in this branch)
        branch_id = self.request.query_params.get('branch')
        if branch_id and branch_id != 'all':
            queryset = queryset.filter(
                lesson_enrollments__lesson__branch_id=branch_id,
                lesson_enrollments__status__in=['active', 'payments_problem']
            ).distinct()
        
        # Filter by course (through lesson enrollments)
        course_id = self.request.query_params.get('course')
        if course_id and course_id != 'all':
            queryset = queryset.filter(
                lesson_enrollments__lesson__course_id=course_id,
                lesson_enrollments__status='active'
            ).distinct()
        
        # Filter by instructor
        instructor_id = self.request.query_params.get('instructor')
        if instructor_id and instructor_id != 'all':
            queryset = queryset.filter(
                lesson_enrollments__lesson__instructor_id=instructor_id,
                lesson_enrollments__status='active'
            ).distinct()
        
        # Filter by age range
        age_range = self.request.query_params.get('age')
        if age_range and age_range != 'all':
            today = date.today()
            if age_range == '0-3':
                min_date = today.replace(year=today.year - 3)
                max_date = today
            elif age_range == '4-6':
                min_date = today.replace(year=today.year - 6)
                max_date = today.replace(year=today.year - 4)
            elif age_range == '7-9':
                min_date = today.replace(year=today.year - 9)
                max_date = today.replace(year=today.year - 7)
            elif age_range == '10-12':
                min_date = today.replace(year=today.year - 12)
                max_date = today.replace(year=today.year - 10)
            elif age_range == '13-18':
                min_date = today.replace(year=today.year - 18)
                max_date = today.replace(year=today.year - 13)
            else:
                min_date = None
                max_date = None
            
            if min_date and max_date:
                queryset = queryset.filter(birth_date__gte=min_date, birth_date__lte=max_date)
        
        # Filter by status (using new explicit status field)
        status_filter = self.request.query_params.get('status')
        if status_filter and status_filter != 'all':
            queryset = queryset.filter(status=status_filter)
        
        # Filter by absent_irregularly
        absent_irregularly = self.request.query_params.get('absent_irregularly')
        if absent_irregularly and absent_irregularly.lower() == 'true':
            queryset = queryset.filter(absent_irregularly=True)
        
        return queryset
    
    @action(detail=False, methods=['get'])
    def by_course(self, request):
        """
        Group children by course (optimized).
        
        USAGE: Used by frontend/src/app/customers/page.tsx
        - GET /api/v1/customers/children/by_course/
        Returns children grouped by course with student preview

        Query params:
        - include_students: "true"/"false" (default: true) -> whether to include students_preview
        - students_limit: int (default: 30, max: 200) -> max unique students to include per course in preview
        """

        def parse_bool(value, default=True):
            """Helper function - only used within this view"""
            if value is None:
                return default
            return str(value).strip().lower() in ('1', 'true', 't', 'yes', 'y')

        child_qs = self.filter_queryset(self.get_queryset())

        include_students = parse_bool(request.query_params.get('include_students'), default=True)
        try:
            students_limit = int(request.query_params.get('students_limit', 30))
        except (TypeError, ValueError):
            students_limit = 30
        students_limit = max(0, min(students_limit, 200))

        enrollments_qs = (
            LessonEnrollment.objects.filter(
                status='active',
                child__in=child_qs,
            )
            .select_related(
                'lesson__course',
                'lesson__course__branch',
                'lesson__instructor',
                'child',
            )
        )

        # 1) Aggregate counts per course (single query)
        groups_rows = (
            enrollments_qs.values(
                'lesson__course_id',
                'lesson__course__name',
                'lesson__course__branch__name',
            )
            .annotate(students_count=Count('child_id', distinct=True))
            .order_by('lesson__course__name')
        )

        # Build base response objects
        result_by_course_id = {}
        for row in groups_rows:
            course_id = str(row['lesson__course_id'])

            result_by_course_id[course_id] = {
                'course_id': course_id,
                'course_name': row.get('lesson__course__name'),
                'branch_name': row.get('lesson__course__branch__name'),
                'instructor_name': None,  # Instructors are now lesson-specific, not course-specific
                'students_count': row.get('students_count', 0),
                'students_preview': [],
                'students_preview_truncated': False,
            }

        # 2) Students preview (single query, streamed in Python; dedupe per course)
        if include_students and students_limit > 0 and result_by_course_id:
            seen_by_course = {cid: set() for cid in result_by_course_id.keys()}
            preview_counts = {cid: 0 for cid in result_by_course_id.keys()}

            preview_rows = (
                enrollments_qs.values(
                    'lesson__course_id',
                    'child_id',
                    'child__first_name',
                    'child__last_name',
                )
                .order_by('lesson__course__name', 'child__last_name', 'child__first_name')
            )

            for row in preview_rows:
                course_id = str(row['lesson__course_id'])
                if course_id not in result_by_course_id:
                    continue

                child_id = str(row['child_id'])
                if child_id in seen_by_course[course_id]:
                    continue
                seen_by_course[course_id].add(child_id)

                if preview_counts[course_id] >= students_limit:
                    result_by_course_id[course_id]['students_preview_truncated'] = True
                    continue

                first = (row.get('child__first_name') or '').strip()
                last = (row.get('child__last_name') or '').strip()
                full_name = (f"{first} {last}".strip()) or None

                result_by_course_id[course_id]['students_preview'].append({
                    'id': child_id,
                    'full_name': full_name,
                })
                preview_counts[course_id] += 1

            # If preview is full but total > limit, mark truncated (even if we didn't see >limit due to ordering/dedup)
            for course_id, group in result_by_course_id.items():
                if group['students_count'] > students_limit:
                    group['students_preview_truncated'] = True

        return Response(list(result_by_course_id.values()))

    @action(detail=False, methods=['get'], url_path=r'by_course/(?P<course_id>[^/.]+)/students')
    def students_for_course(self, request, course_id=None):
        """
        Full student roster for a single course (names only).
        
        USAGE: Used by frontend when user clicks "+ עוד" to see full student list
        - GET /api/v1/customers/children/by_course/{course_id}/students/
        """
        students_qs = (
            Child.objects.filter(
                lesson_enrollments__status='active',
                lesson_enrollments__lesson__course_id=course_id,
            )
            .annotate(
                full_name=Concat(
                    'first_name',
                    Value(' '),
                    'last_name',
                    output_field=CharField(),
                )
            )
            .values('id', 'full_name')
            .order_by('last_name', 'first_name')
            .distinct()
        )

        return Response({
            'course_id': str(course_id),
            'students': list(students_qs),
        })
    
    @action(detail=True, methods=['post'])
    def update_status(self, request, pk=None):
        """
        Update child status based on payment dates and subscriptions
        
        USAGE: Used by frontend/src/components/dialogs/EnrollToLessonDialog.tsx
        - POST /api/v1/customers/children/{id}/update_status/
        Also used by Django admin action
        """
        child = self.get_object()
        child.update_status()
        return Response({
            'status': child.status,
            'message': f'Status updated to: {child.status}'
        }, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['get'])
    def absence_history(self, request, pk=None):
        """
        Get absence history for a child
        
        USAGE: Used by frontend child profile dialog to display absence history
        - GET /api/v1/customers/children/{id}/absence_history/
        """
        from apps.enrollments.models import ChildAbsence
        from apps.enrollments.serializers import AbsenceHistorySerializer
        
        child = self.get_object()
        absences = ChildAbsence.objects.filter(child=child).select_related(
            'lesson', 'course'
        ).order_by('-occurrence_date')
        
        serializer = AbsenceHistorySerializer(absences, many=True)
        return Response(serializer.data)
    
    def destroy(self, request, *args, **kwargs):
        """
        Override destroy to delete family and parents if no children remain.
        
        USAGE: Called when deleting a child
        - DELETE /api/v1/customers/children/{id}/
        
        After deleting the child, checks if the family has any remaining children.
        If not, deletes the family (which cascades to parents).
        """
        child = self.get_object()
        family = child.family
        
        # Delete the child first
        response = super().destroy(request, *args, **kwargs)
        
        # Check if the family has any remaining children
        if family.children.count() == 0:
            # No more children, delete the family (cascades to parents)
            family.delete()
        
        return response
    
    @action(detail=False, methods=['post'])
    def create_ghost(self, request):
        """
        Create a ghost child with minimal data.
        
        Ghost children are temporary trial students that:
        - Require only first_name
        - Auto-generate family (system "Ghost Family")
        - Use default values for required fields
        - Have status='ghost'
        - Are visible for 30 days from creation
        
        USAGE: Used by frontend lesson details dialog
        - POST /api/v1/customers/children/create_ghost/
        
        Request body:
        {
            "first_name": "שם",
            "lesson_id": "uuid"
        }
        
        Returns the created child and enrollment.
        """
        from apps.courses.models import Lesson
        from datetime import date
        
        first_name = request.data.get('first_name', '').strip()
        family_name = request.data.get('family_name', '').strip()
        phone_number = request.data.get('phone_number', '').strip()
        lesson_id = request.data.get('lesson_id')
        
        if not first_name:
            return Response(
                {'error': 'שם פרטי נדרש'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not lesson_id:
            return Response(
                {'error': 'מזהה שיעור נדרש'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            lesson = Lesson.objects.get(id=lesson_id)
        except Lesson.DoesNotExist:
            return Response(
                {'error': 'שיעור לא נמצא'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Determine family details
        if family_name or phone_number:
            # Create a specific ghost family for this child
            ghost_family = Family.objects.create(
                name=family_name or f'משפחת {first_name}',
                phone=phone_number or '0000000000',
                email='',
                address='',
                branch=lesson.branch,
                notes='משפחת רפאים'
            )
        else:
            # Get or create a system "Ghost Family"
            ghost_family, _ = Family.objects.get_or_create(
                name='רפאים (מערכת)',
                defaults={
                    'phone': '0000000000',
                    'email': '',
                    'address': '',
                    'branch': lesson.branch,
                    'notes': 'משפחה מערכתית לתלמידי רפאים'
                }
            )
        
        # Create ghost child with minimal data
        ghost_child = Child.objects.create(
            family=ghost_family,
            first_name=first_name,
            last_name=family_name or '',
            id_number='',
            phone_number=phone_number or '',
            birth_date=date(2010, 1, 1),  # Default birth date
            gender='male',  # Default gender
            status='ghost',
            notes=f'תלמיד רפאים - נוצר בתאריך {date.today().strftime("%d/%m/%Y")}'
        )
        
        # Enroll ghost child in the lesson
        enrollment = LessonEnrollment.objects.create(
            lesson=lesson,
            child=ghost_child,
            status='active',
            start_date=date.today()
        )
        
        return Response({
            'child': ChildSerializer(ghost_child).data,
            'enrollment': {
                'id': str(enrollment.id),
                'lesson_id': str(lesson.id),
                'status': enrollment.status
            },
            'message': 'תלמיד רפאים נוצר בהצלחה'
        }, status=status.HTTP_201_CREATED)


# Store ViewSets moved to apps.store.views


class DiscountViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Discounts Management
    
    USAGE: Manage all discount types (Early Sign-Up and Second Child)
    - GET /api/v1/customers/discounts/ - List all discounts
    - GET /api/v1/customers/discounts/early-signup/ - List early sign-up discounts
    - POST /api/v1/customers/discounts/early-signup/ - Create early sign-up discount
    - PATCH /api/v1/customers/discounts/{id}/ - Update early sign-up discount
    - GET /api/v1/customers/discounts/second-child/ - Get second child discount
    - PUT /api/v1/customers/discounts/second-child/ - Update second child discount value
    - DELETE /api/v1/customers/discounts/{id}/ - Delete discount (except built-in second child)
    - POST /api/v1/customers/discounts/evaluate/ - Evaluate discounts for a payment
    """
    queryset = Discount.objects.all().order_by('-created_at')
    serializer_class = DiscountSerializer
    permission_classes = [IsAuthenticated, IsManager]
    
    def get_serializer_class(self):
        """Return appropriate serializer based on the discount type"""
        if self.action in ['update', 'partial_update']:
            # For updates, check if it's an early signup discount
            if hasattr(self, 'get_object'):
                try:
                    discount = self.get_object()
                    if discount.is_built_in and 'רישום מוקדם' in discount.name:
                        return EarlySignupDiscountSerializer
                except:
                    pass
        return self.serializer_class
    
    @action(detail=False, methods=['get', 'post'], url_path='early-signup')
    def early_signup(self, request):
        """
        List or create early sign-up discounts
        
        GET: Returns all early sign-up date ranges
        POST: Creates a new early sign-up date range
        """
        if request.method == 'GET':
            # Get all early sign-up discounts
            discounts = Discount.objects.filter(
                is_built_in=True,
                name__contains='רישום מוקדם'
            ).order_by('start_date')
            
            serializer = EarlySignupDiscountSerializer(discounts, many=True)
            return Response(serializer.data)
        
        elif request.method == 'POST':
            # Create new early sign-up discount
            serializer = EarlySignupDiscountSerializer(data=request.data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['get', 'put'], url_path='second-child')
    def second_child(self, request):
        """
        Get or update second child discount
        
        GET: Returns the second child discount configuration
        PUT: Updates the second child discount value
        """
        # Get or create the second child discount
        discount, created = Discount.objects.get_or_create(
            is_built_in=True,
            name__contains='ילד שני',
            defaults={
                'name': 'הנחת ילד שני',
                'description': 'הנחה אוטומטית לילד שני ומעלה במשפחה',
                'discount_type': 'fixed',
                'value': 0,
                'applies_to': 'child',
                'promotion_type': 'permanent',
                'is_built_in': True,
                'is_active': True
            }
        )
        
        if request.method == 'GET':
            serializer = SecondChildDiscountSerializer(discount)
            return Response(serializer.data)
        
        elif request.method == 'PUT':
            serializer = SecondChildDiscountSerializer(discount, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['get', 'put'], url_path='additional-lesson')
    def additional_lesson(self, request):
        """
        Get or update additional lesson discount
        
        GET: Returns the additional lesson discount configuration
        PUT: Updates the additional lesson discount value
        
        This discount applies to active children enrolled in multiple lessons.
        The first lesson pays full price, additional lessons get this fixed price.
        """
        # Get or create the additional lesson discount
        discount, created = Discount.objects.get_or_create(
            is_built_in=True,
            name__contains='שיעור נוסף',
            defaults={
                'name': 'הנחת שיעור נוסף לילד פעיל',
                'description': 'מחיר קבוע לשיעורים נוספים לילד פעיל (שיעור ראשון במחיר מלא)',
                'discount_type': 'fixed_final_price',
                'value': 0,
                'applies_to': 'child',
                'promotion_type': 'permanent',
                'is_built_in': True,
                'is_active': True
            }
        )
        
        if request.method == 'GET':
            serializer = AdditionalLessonDiscountSerializer(discount)
            return Response(serializer.data)
        
        elif request.method == 'PUT':
            serializer = AdditionalLessonDiscountSerializer(discount, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['post'])
    def evaluate(self, request):
        """
        Evaluate applicable discounts for a payment
        
        Request body:
        {
            "family_id": "uuid",
            "child_id": "uuid",
            "payment_date": "2024-01-15",
            "base_price": 500.00
        }
        
        Returns:
        {
            "base_price": 500.00,
            "discounts": [...],
            "total_discount": 100.00,
            "final_price": 400.00,
            "discount_count": 2
        }
        """
        from decimal import Decimal
        
        family_id = request.data.get('family_id')
        child_id = request.data.get('child_id')
        payment_date_str = request.data.get('payment_date')
        base_price = request.data.get('base_price')
        
        # Validate required fields
        if not all([family_id, child_id, payment_date_str, base_price]):
            return Response({
                'error': 'חסרים פרטים: family_id, child_id, payment_date, base_price'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # Parse payment date
            payment_date = datetime.strptime(payment_date_str, '%Y-%m-%d').date()
            base_price = Decimal(str(base_price))
            
            # Evaluate discounts
            service = DiscountService()
            result = service.evaluate_discounts_for_payment(
                family_id=family_id,
                child_id=child_id,
                payment_date=payment_date,
                base_price=base_price
            )
            
            # Return summary
            return Response(service.get_discount_summary(result))
            
        except ValueError as e:
            return Response({
                'error': f'שגיאה בפורמט הנתונים: {str(e)}'
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'error': f'שגיאה בעיבוד ההנחות: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def destroy(self, request, *args, **kwargs):
        """
        Delete discount - prevent deletion of second child discount
        """
        discount = self.get_object()
        
        # Prevent deletion of second child discount
        if discount.is_built_in and 'ילד שני' in discount.name:
            return Response({
                'error': 'לא ניתן למחוק את הנחת הילד השני. ניתן לכבות אותה על ידי שינוי הערך ל-0 או שינוי הסטטוס ללא פעיל'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        return super().destroy(request, *args, **kwargs)
    
    @action(detail=False, methods=['get'])
    def metrics(self, request):
        """
        Get discount metrics per branch
        
        Query params:
        - branch_id (optional): Filter by branch
        - start_date (optional): Start date (YYYY-MM-DD)
        - end_date (optional): End date (YYYY-MM-DD)
        - month (optional): Specific month (YYYY-MM)
        
        Returns:
        {
            "total_discount_amount": 1500.00,
            "discount_count": 25,
            "breakdown": {
                "early_signup": 500.00,
                "second_child": 800.00,
                "fixed_price": 200.00
            },
            "by_branch": [...]
        }
        """
        from apps.core.revenue_service import RevenueService
        from datetime import date, datetime
        from dateutil.relativedelta import relativedelta
        
        revenue_service = RevenueService()
        
        # Parse query parameters
        branch_id = request.query_params.get('branch_id')
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')
        month_str = request.query_params.get('month')
        
        # Determine date range
        if month_str:
            # Specific month
            try:
                month_date = datetime.strptime(month_str, '%Y-%m').date()
                start_date = month_date.replace(day=1)
                end_date = (start_date + relativedelta(months=1)) - relativedelta(days=1)
            except ValueError:
                return Response({
                    'error': 'פורמט חודש לא תקין. השתמש ב-YYYY-MM'
                }, status=status.HTTP_400_BAD_REQUEST)
        elif start_date_str and end_date_str:
            # Date range
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({
                    'error': 'פורמט תאריך לא תקין. השתמש ב-YYYY-MM-DD'
                }, status=status.HTTP_400_BAD_REQUEST)
        else:
            # Default to current month
            today = date.today()
            start_date = today.replace(day=1)
            end_date = (start_date + relativedelta(months=1)) - relativedelta(days=1)
        
        if branch_id and branch_id != 'all':
            # Single branch metrics
            metrics = revenue_service.get_discount_metrics(branch_id, start_date, end_date)
            return Response(metrics)
        else:
            # All branches
            from apps.core.models import Branch
            from apps.customers.financial_models import BranchDiscountMetrics
            
            branches = Branch.objects.all()
            by_branch = []
            total_discount_amount = Decimal('0.00')
            total_discount_count = 0
            total_early_signup = Decimal('0.00')
            total_second_child = Decimal('0.00')
            total_fixed_price = Decimal('0.00')
            
            for branch in branches:
                metrics = revenue_service.get_discount_metrics(str(branch.id), start_date, end_date)
                by_branch.append({
                    'branch_id': str(branch.id),
                    'branch_name': branch.name,
                    'total_discount_amount': metrics['total_discount_amount'],
                    'discount_count': metrics['discount_count'],
                    'breakdown': metrics['breakdown']
                })
                
                total_discount_amount += Decimal(str(metrics['total_discount_amount']))
                total_discount_count += metrics['discount_count']
                total_early_signup += Decimal(str(metrics['breakdown']['early_signup']))
                total_second_child += Decimal(str(metrics['breakdown']['second_child']))
                total_fixed_price += Decimal(str(metrics['breakdown']['fixed_price']))
            
            return Response({
                'total_discount_amount': float(total_discount_amount),
                'discount_count': total_discount_count,
                'breakdown': {
                    'early_signup': float(total_early_signup),
                    'second_child': float(total_second_child),
                    'fixed_price': float(total_fixed_price)
                },
                'by_branch': by_branch,
                'date_range': {
                    'start_date': start_date.isoformat(),
                    'end_date': end_date.isoformat()
                }
            })


# ============================================================================
# Payment ViewSets - Tranzila Integration
# ============================================================================

class PaymentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Payment management
    
    Endpoints:
    - POST /api/v1/payments/initiate-subscription/ - Initiate subscription payment
    - POST /api/v1/payments/initiate-one-time/ - Initiate one-time payment
    - GET /api/v1/payments/{id}/ - Get payment status
    - POST /api/v1/payments/webhook/ - Tranzila webhook callback (public)
    """
    queryset = Payment.objects.all().select_related(
        'child', 'family', 'parent', 'tranzila_transaction', 'branch', 'lesson', 'lesson__course'
    ).prefetch_related('discount_snapshots')
    serializer_class = PaymentSerializer
    permission_classes = [IsAuthenticated, IsManager]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['child__first_name', 'child__last_name', 'family__name']
    ordering_fields = ['created_at', 'payment_date', 'final_amount']
    ordering = ['-created_at']
    
    def get_queryset(self):
        """Filter payments by child"""
        queryset = super().get_queryset()
        
        # Filter by child if provided
        child_id = self.request.query_params.get('child_id')
        if child_id:
            queryset = queryset.filter(child_id=child_id)
        
        return queryset
    
    @action(detail=False, methods=['post'])
    def initiate_subscription(self, request):
        """
        Initiate a recurring subscription payment.
        
        POST /api/v1/payments/initiate-subscription/
        Body: {
            "child_id": "uuid",
            "lesson_id": "uuid",
            "payment_date": "2024-01-01",  // optional
            "success_url": "https://...",  // optional
            "error_url": "https://...",    // optional
            "callback_url": "https://..."  // optional
        }
        
        Returns: {
            "payment_id": "uuid",
            "tranzila_url": "https://...",
            "base_amount": 500.00,
            "discount_amount": 50.00,
            "final_amount": 450.00,
            "discounts_applied": [...]
        }
        """
        serializer = PaymentInitiationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            lesson_id = serializer.validated_data.get('lesson_id')
            if not lesson_id:
                return Response(
                    {'error': 'lesson_id is required for subscription payments'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            payment_service = PaymentService()
            result = payment_service.initiate_subscription_payment(
                child_id=str(serializer.validated_data['child_id']),
                lesson_id=str(lesson_id),
                payment_date=serializer.validated_data.get('payment_date'),
                success_url=serializer.validated_data.get('success_url', ''),
                error_url=serializer.validated_data.get('error_url', ''),
                callback_url=serializer.validated_data.get('callback_url', '')
            )
            # Don't re-validate response with a serializer (Decimals/floats can trip it and cause 500).
            return Response(result, status=status.HTTP_201_CREATED)
            
        except ValueError as e:
            return Response({
                'error': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
        except DRFValidationError as e:
            return Response({
                'error': e.detail
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            # Ensure traceback is visible in server logs
            import logging
            logging.getLogger(__name__).exception("Failed to initiate subscription payment")
            return Response({
                'error': f'שגיאה ביצירת תשלום: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @method_decorator(csrf_exempt, name='dispatch')
    @action(detail=False, methods=['post'], permission_classes=[AllowAny])
    def webhook(self, request):
        """
        Tranzila webhook callback endpoint.
        
        POST /api/v1/payments/webhook/
        
        This endpoint receives callbacks from Tranzila after payment processing.
        It's public (no authentication) but validates webhook signature.
        """
        # === ENHANCED LOGGING FOR MONITORING ===
        logger.info("=" * 80)
        logger.info("🔔 WEBHOOK RECEIVED FROM TRANZILA")
        logger.info("=" * 80)
        logger.info(f"Method: {request.method}")
        logger.info(f"Path: {request.path}")
        logger.info(f"Content-Type: {request.content_type}")
        logger.info(f"Headers: {dict(request.headers)}")
        logger.info(f"GET params: {dict(request.GET)}")
        logger.info(f"POST data: {request.POST.dict()}")  # Use request.POST instead of request.body
        logger.info("=" * 80)
        
        serializer = WebhookCallbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # Get signature from headers if provided
        signature = request.headers.get('X-Tranzila-Signature', '')
        
        try:
            payment_service = PaymentService()
            result = payment_service.process_webhook_callback(
                webhook_payload=serializer.validated_data,
                signature=signature
            )
            
            if result['success']:
                logger.info(f"✅ Webhook processed successfully: {result}")
                return Response(result, status=status.HTTP_200_OK)
            else:
                logger.warning(f"⚠️  Webhook processing failed: {result}")
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
                
        except Exception as e:
            logger.exception(f"❌ Webhook processing error: {str(e)}")
            return Response({
                'error': f'שגיאה בעיבוד webhook: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['post'])
    def refund(self, request, pk=None):
        """
        Refund/credit a payment.
        
        POST /api/v1/payments/{id}/refund/
        Body: {
            "reason": "Refund reason",
            "amount": 100.00  // optional, for partial refund
        }
        """
        payment = self.get_object()
        
        reason = request.data.get('reason', 'זיכוי')
        amount = request.data.get('amount')  # Optional partial refund
        
        # Call payment service to handle refund
        from apps.core.payment_service import PaymentService
        payment_service = PaymentService()
        
        result = payment_service.refund_payment(
            payment_id=str(payment.id),
            reason=reason,
            amount=Decimal(str(amount)) if amount else None
        )
        
        if result['success']:
            return Response({
                'success': True,
                'message': result.get('message', 'התשלום זוכה בהצלחה'),
                'transaction_id': result.get('transaction_id')
            })
        else:
            return Response({
                'error': result.get('error', 'שגיאה בזיכוי התשלום')
            }, status=status.HTTP_400_BAD_REQUEST)


class RecurringPaymentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing recurring payments/subscriptions
    
    Endpoints:
    - GET /api/v1/payments/recurring/ - List recurring payments
    - GET /api/v1/payments/recurring/{id}/ - Get recurring payment details
    - POST /api/v1/payments/recurring/{id}/update/ - Update recurring payment
    - POST /api/v1/payments/recurring/{id}/cancel/ - Cancel subscription
    """
    queryset = RecurringPayment.objects.all().select_related(
        'child', 
        'initial_payment',
        'initial_payment__lesson',
        'initial_payment__lesson__course',
        'initial_payment__branch'
    )
    serializer_class = RecurringPaymentSerializer
    permission_classes = [IsAuthenticated, IsManager]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['child__first_name', 'child__last_name']
    ordering_fields = ['created_at', 'next_billing_date', 'amount']
    ordering = ['-created_at']
    
    def get_queryset(self):
        """Filter recurring payments by status and child"""
        queryset = super().get_queryset()
        
        # Filter by child if provided
        child_id = self.request.query_params.get('child_id')
        if child_id:
            queryset = queryset.filter(child_id=child_id)
        
        # Filter by status if provided
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Filter by child if provided
        child_id = self.request.query_params.get('child_id')
        if child_id:
            queryset = queryset.filter(child_id=child_id)
        
        return queryset
    
    @action(detail=True, methods=['post'])
    def update_subscription(self, request, pk=None):
        """
        Update recurring subscription (recalculate discounts and update amount).
        
        POST /api/v1/payments/recurring/{id}/update/
        Body: {
            "recalculate_discounts": true
        }
        """
        serializer = RecurringPaymentUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            payment_service = PaymentService()
            result = payment_service.update_recurring_subscription(
                recurring_payment_id=str(pk),
                recalculate_discounts=serializer.validated_data['recalculate_discounts']
            )
            
            if result['success']:
                return Response(result, status=status.HTTP_200_OK)
            else:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
                
        except ValueError as e:
            return Response({
                'error': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'error': f'שגיאה בעדכון מנוי: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """
        Cancel recurring subscription.
        
        POST /api/v1/payments/recurring/{id}/cancel/
        Body: {
            "cancellation_reason": "סיבה"
        }
        """
        serializer = RecurringPaymentCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            payment_service = PaymentService()
            result = payment_service.cancel_subscription(
                recurring_payment_id=str(pk),
                cancellation_reason=serializer.validated_data.get('cancellation_reason', '')
            )
            
            if result['success']:
                return Response(result, status=status.HTTP_200_OK)
            else:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
                
        except ValueError as e:
            return Response({
                'error': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'error': f'שגיאה בביטול מנוי: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

