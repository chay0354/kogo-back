"""
Widget Registration Views — public (no-auth) endpoints for self-service registration.

Flow:
  1. POST /api/v1/customers/widget/lookup/   — identify family & discount eligibility
  2. POST /api/v1/customers/widget/register/ — create records + initiate Tranzila payment
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework import status
from django.db import transaction

from apps.customers.models import Family, Parent, Child
from apps.courses.models import Lesson, Course
from apps.core.models import City, Branch
from apps.core.payment_service import PaymentService


class WidgetLookupView(APIView):
    """
    Check parent ID + child name against existing records to determine discount eligibility.

    Returns one of:
      family_status='new'      → no family found, no discount
      family_status='existing', child_status='active'  → same child → additional-lesson discount
      family_status='existing', child_status='new'     → sibling    → second-child discount
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        parent_id_number = (request.data.get('parent_id_number') or '').strip()
        child_first_name = (request.data.get('child_first_name') or '').strip()
        child_last_name  = (request.data.get('child_last_name')  or '').strip()

        if not parent_id_number or not child_first_name or not child_last_name:
            return Response(
                {'error': 'נא למלא ת.ז. הורה ושם הילד'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        family = Family.objects.filter(parent_id_number=parent_id_number).first()

        if not family:
            return Response({
                'family_status': 'new',
                'child_status': 'new',
                'discount_type': None,
                'discount_question': None,
            })

        # Family exists — check for an active child with matching name
        active_child = family.children.filter(
            first_name__iexact=child_first_name,
            last_name__iexact=child_last_name,
            status='active',
        ).first()

        if active_child:
            return Response({
                'family_status': 'existing',
                'child_status': 'active',
                'child_id': str(active_child.id),
                'discount_type': 'additional_lesson',
                'discount_question': (
                    'זיהינו שהילד כבר מתאמן אצלנו. '
                    'האם מדובר בהרשמה לשיעור נוסף עבור אותו ילד?'
                ),
            })

        # Family exists but child not active → sibling
        return Response({
            'family_status': 'existing',
            'child_status': 'new',
            'discount_type': 'sibling',
            'discount_question': (
                'זיהינו שמשפחתכם כבר רשומה אצלנו. '
                'האם מדובר באח/אחות של ילד אחר שמתאמן אצלנו?'
            ),
        })


class WidgetRegisterView(APIView):
    """
    Complete registration: create family/child records then initiate Tranzila payment.

    Required fields:
      parent_id_number, parent_first_name, parent_last_name, parent_phone
      child_first_name, child_last_name, child_birth_date, child_gender
      course_id

    Optional:
      child_id_number, child_phone
      discount_confirmed  (bool) — parent confirmed the discount question
      existing_child_id   (str)  — returned by lookup when child is already active
      success_url, error_url, callback_url
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data

        required = [
            'parent_id_number', 'parent_first_name', 'parent_last_name', 'parent_phone',
            'child_first_name', 'child_last_name', 'child_birth_date', 'child_gender',
            'course_id',
        ]
        missing = [f for f in required if not data.get(f)]
        if missing:
            return Response(
                {'error': f'שדות חובה חסרים: {", ".join(missing)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        course_id = data['course_id']
        try:
            course = Course.objects.prefetch_related('lessons').get(id=course_id)
        except Course.DoesNotExist:
            return Response({'error': 'חוג לא נמצא'}, status=status.HTTP_404_NOT_FOUND)

        lessons = list(course.lessons.select_related('course__branch').all())
        if not lessons:
            return Response({'error': 'לא נמצאו שיעורים לחוג זה'}, status=status.HTTP_400_BAD_REQUEST)
        lesson = lessons[0]

        parent_id_number  = data['parent_id_number'].strip()
        child_first       = data['child_first_name'].strip()
        child_last        = data['child_last_name'].strip()
        discount_confirmed = bool(data.get('discount_confirmed', False))
        existing_child_id  = data.get('existing_child_id', '').strip()

        try:
            with transaction.atomic():
                # ── 1. Find or create Family ──────────────────────────────
                family = Family.objects.filter(parent_id_number=parent_id_number).first()
                if not family:
                    family = Family.objects.create(
                        name=data['parent_last_name'].strip(),
                        phone=data['parent_phone'].strip(),
                        parent_id_number=parent_id_number,
                        branch=lesson.course.branch,
                    )
                    Parent.objects.create(
                        family=family,
                        first_name=data['parent_first_name'].strip(),
                        last_name=data['parent_last_name'].strip(),
                        phone=data['parent_phone'].strip(),
                        is_primary=True,
                    )

                # ── 2. Resolve child ──────────────────────────────────────
                child = None

                # If lookup returned an existing active child and parent confirmed
                if existing_child_id and discount_confirmed:
                    try:
                        child = Child.objects.get(id=existing_child_id, family=family)
                    except Child.DoesNotExist:
                        pass

                # Fallback: look for active name match (handles re-submission edge cases)
                if child is None and discount_confirmed:
                    child = family.children.filter(
                        first_name__iexact=child_first,
                        last_name__iexact=child_last,
                        status='active',
                    ).first()

                # Otherwise create a new child
                if child is None:
                    child = Child.objects.create(
                        family=family,
                        first_name=child_first,
                        last_name=child_last,
                        birth_date=data['child_birth_date'],
                        gender=data['child_gender'],
                        phone_number=(data.get('child_phone') or '').strip(),
                        id_number=(data.get('child_id_number') or '').strip(),
                        status='pending',
                    )

        except Exception as exc:
            return Response(
                {'error': f'שגיאה ביצירת הרשומה: {exc}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # ── 3. Initiate payment (pricing + pending Payment record) ───────
        try:
            result = PaymentService().initiate_subscription_payment(
                child_id=str(child.id),
                lesson_id=str(lesson.id),
                success_url=data.get('success_url', ''),
                error_url=data.get('error_url', ''),
                callback_url=data.get('callback_url', ''),
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response(
                {'error': f'שגיאה בתהליך התשלום: {exc}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class WidgetChargeView(APIView):
    """
    Charge an existing pending Payment directly with card details (no iframe/webhook).

    POST /api/v1/customers/widget/charge/
    Body: {
        "payment_id": "uuid",
        "card_details": {
            "card_number": "...",
            "expiry_month": 12,
            "expiry_year": 2026,
            "cvv": "123",
            "card_holder_id": "012345678"
        }
    }
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        from apps.customers.models import Payment
        from apps.core.tranzila_service import TranzilaService
        from apps.customers.models import TranzilaTransaction, RecurringPayment
        from apps.customers.financial_models import Invoice, InvoiceChild
        from apps.enrollments.models import LessonEnrollment
        from django.utils import timezone
        from datetime import date, timedelta
        import logging

        logger = logging.getLogger(__name__)

        payment_id = (request.data.get('payment_id') or '').strip()
        card_details = request.data.get('card_details') or {}

        if not payment_id:
            return Response({'error': 'payment_id נדרש'}, status=status.HTTP_400_BAD_REQUEST)
        if not card_details.get('card_number'):
            return Response({'error': 'פרטי כרטיס נדרשים'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            payment = Payment.objects.select_related('child', 'family', 'lesson__course__branch').get(
                id=payment_id, status='pending'
            )
        except Payment.DoesNotExist:
            return Response({'error': 'תשלום לא נמצא או כבר עובד'}, status=status.HTTP_404_NOT_FOUND)

        child = payment.child
        lesson = payment.lesson

        try:
            card_number = str(card_details['card_number']).replace(' ', '')
            expiry_month = int(card_details['expiry_month'])
            expiry_year = int(card_details['expiry_year'])
            cvv = str(card_details['cvv'])
            card_holder_id = str(card_details.get('card_holder_id', ''))
        except (KeyError, ValueError, TypeError) as e:
            return Response({'error': f'פרטי כרטיס שגויים: {e}'}, status=status.HTTP_400_BAD_REQUEST)

        tranzila = TranzilaService()
        items = [{
            'name': f"{lesson.course.name} - {child.full_name}" if lesson else child.full_name,
            'type': 'I',
            'unit_price': float(payment.final_amount),
            'units_number': 1,
            'unit_type': 1,
            'price_type': 'G',
            'currency_code': 'ILS',
        }]

        result = tranzila.charge_with_card(
            card_number=card_number,
            expiry_month=expiry_month,
            expiry_year=expiry_year,
            cvv=cvv,
            card_holder_id=card_holder_id,
            amount=payment.final_amount,
            description=payment.description or f"מנוי - {child.full_name}",
            items=items,
        )

        if result['success']:
            with transaction.atomic():
                payment.status = 'completed'
                payment.payment_date = timezone.now()
                payment.save()

                tranzila_txn = TranzilaTransaction.objects.create(
                    transaction_id=result.get('transaction_id', ''),
                    confirmation_code=result.get('confirmation_code', ''),
                    transaction_type='recurring_setup',
                    response_code=result.get('response_code', '000'),
                    response_message='',
                    request_data={},
                    response_data=result.get('raw_response', {}),
                    idempotency_key=f"widget_{result.get('transaction_id', payment_id)}",
                    is_successful=True,
                    response_timestamp=timezone.now(),
                )
                payment.tranzila_transaction = tranzila_txn
                payment.save(update_fields=['tranzila_transaction'])

                token = result.get('token', '')
                if token:
                    discount_details = [
                        {
                            'name': s.discount_name,
                            'type': s.discount_type,
                            'value': str(s.discount_value),
                            'amount_deducted': str(s.amount_deducted),
                            'reason': s.reason,
                        }
                        for s in payment.discount_snapshots.all()
                    ]
                    RecurringPayment.objects.create(
                        child=child,
                        initial_payment=payment,
                        tranzila_token=token,
                        card_expire_month=expiry_month,
                        card_expire_year=expiry_year,
                        status='active',
                        base_amount=payment.base_amount,
                        discount_amount=payment.discount_amount,
                        amount=payment.final_amount,
                        discount_details=discount_details,
                        billing_day=date.today().day,
                        start_date=date.today(),
                        next_billing_date=date.today() + timedelta(days=30),
                    )

                # Invoice
                invoice_number = f"INV-{timezone.now().strftime('%Y%m%d')}-{payment.id.hex[:8].upper()}"
                invoice = Invoice.objects.create(
                    invoice_number=invoice_number,
                    family=payment.family,
                    parent=payment.parent,
                    branch=payment.branch,
                    payment=payment,
                    amount=payment.final_amount,
                    status='paid',
                    payment_method='credit_card',
                    payment_type='recurring',
                    payer_name=payment.family.name,
                    payer_email=payment.family.email if payment.family.email else '',
                    payer_phone=payment.family.phone,
                    tranzila_transaction_id=tranzila_txn.transaction_id,
                    invoice_date=timezone.now(),
                )
                if child and lesson:
                    InvoiceChild.objects.create(
                        invoice=invoice,
                        child=child,
                        course=lesson.course,
                        lesson=lesson,
                    )

                child.status = 'active'
                child.subscription_start_date = date.today()
                child.paid_until_date = date.today() + timedelta(days=30)
                child.save()

                if lesson:
                    enrollment, created = LessonEnrollment.objects.get_or_create(
                        child=child,
                        lesson=lesson,
                        defaults={'start_date': date.today(), 'status': 'active'},
                    )
                    if not created:
                        enrollment.status = 'active'
                        if not enrollment.start_date:
                            enrollment.start_date = date.today()
                        enrollment.save()

            return Response({'success': True, 'payment_id': str(payment.id)})

        else:
            payment.status = 'failed'
            payment.failure_reason = result.get('error', 'Unknown')
            payment.save()
            child.status = 'payment_problem'
            child.save()
            return Response(
                {'success': False, 'error': result.get('error', 'התשלום נכשל')},
                status=status.HTTP_400_BAD_REQUEST,
            )


class WidgetCoursesView(APIView):
    """Public endpoint — returns active courses with lessons for a given branch."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        branch_id = request.query_params.get('branch_id')
        if not branch_id:
            return Response({'error': 'branch_id נדרש'}, status=status.HTTP_400_BAD_REQUEST)

        courses = (
            Course.objects
            .filter(branch_id=branch_id, is_active=True)
            .select_related('course_type', 'branch')
            .prefetch_related('lessons__instructor')
            .order_by('course_type__name', 'name')
        )

        result = []
        for course in courses:
            lessons = []
            for lesson in course.lessons.all():
                lessons.append({
                    'id': str(lesson.id),
                    'day_of_week': lesson.day_of_week,
                    'start_time': str(lesson.start_time)[:5],
                    'end_time': str(lesson.end_time)[:5],
                    'price': str(lesson.lesson_price_override or course.price),
                    'instructor_name': lesson.instructor.full_name if lesson.instructor else None,
                })
            result.append({
                'id': str(course.id),
                'name': course.name,
                'course_type': str(course.course_type_id) if course.course_type_id else None,
                'course_type_name': course.course_type.name if course.course_type else None,
                'branch_name': course.branch.name,
                'price': str(course.price),
                'min_age': course.min_age,
                'max_age': course.max_age,
                'lessons_count': len(lessons),
                'lessons': lessons,
            })

        return Response(result)


class WidgetCitiesView(APIView):
    """Public endpoint — returns all cities for the widget city selector."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        cities = City.objects.all().values('id', 'name').order_by('name')
        return Response(list(cities))


class WidgetBranchesView(APIView):
    """Public endpoint — returns active branches (id, name, city) for the widget branch selector."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        branches = (
            Branch.objects.filter(is_active=True)
            .values('id', 'name', 'city_id')
            .order_by('name')
        )
        return Response([{'id': b['id'], 'name': b['name'], 'city': b['city_id']} for b in branches])
