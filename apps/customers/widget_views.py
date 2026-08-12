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
from django.db.models import Prefetch, Q

from apps.customers.models import Family, Parent, Child
from apps.courses.models import Lesson, Course, LessonBundle
from apps.core.models import City, Branch
from apps.core.payment_service import PaymentService


def _widget_bundles_queryset():
    return LessonBundle.objects.prefetch_related('lessons').order_by('-is_active', 'name')


def _widget_bundles_for_course(course):
    """
    Bundles exposed in the public widget catalog.

    For must_attend_all_lessons courses, include an inactive bundle when it is
    the only bundle with lessons (common after accidental soft-delete).
    """
    candidates = [b for b in course.lesson_bundles.all() if b.lessons.exists()]
    active = [b for b in candidates if b.is_active]
    if active:
        return active
    if course.must_attend_all_lessons and candidates:
        return candidates
    return []


def _resolve_widget_bundle(*, course, bundle_id: str):
    """Resolve a bundle for widget registration (allows inactive must-attend fallback)."""
    try:
        bundle = LessonBundle.objects.prefetch_related('lessons').get(id=bundle_id, course=course)
    except LessonBundle.DoesNotExist:
        return None
    if bundle.is_active and bundle.lessons.exists():
        return bundle
    if course.must_attend_all_lessons and bundle.lessons.exists():
        has_active = LessonBundle.objects.filter(
            course=course,
            is_active=True,
            lessons__isnull=False,
        ).exists()
        if not has_active:
            return bundle
    return None


def _serialize_widget_bundle(bundle):
    bundle_lessons = list(bundle.lessons.all())
    return {
        'id': str(bundle.id),
        'name': bundle.name,
        'combined_price': str(bundle.combined_price),
        'lessons': [
            {
                'id': str(bl.id),
                'day_of_week': bl.day_of_week,
                'start_time': str(bl.start_time)[:5],
                'end_time': str(bl.end_time)[:5],
            }
            for bl in bundle_lessons
        ],
    }


def _resolve_family_and_child(data, branch):
    """
    Find-or-create the Family/Parent for `data['parent_id_number']`, then resolve
    the Child (existing active child on discount confirmation, or a new record).

    Shared by WidgetRegisterView (paid flow) and WidgetTrialRegisterView (trial flow).
    """
    parent_id_number = data['parent_id_number'].strip()
    parent_email = (data.get('parent_email') or '').strip()
    child_first = data['child_first_name'].strip()
    child_last = data['child_last_name'].strip()
    discount_confirmed = bool(data.get('discount_confirmed', False))
    existing_child_id = (data.get('existing_child_id') or '').strip()

    # ── 1. Find or create Family ──────────────────────────────
    family = Family.objects.filter(parent_id_number=parent_id_number).first()
    if not family:
        family = Family.objects.create(
            name=data['parent_last_name'].strip(),
            phone=data['parent_phone'].strip(),
            email=parent_email,
            parent_id_number=parent_id_number,
            branch=branch,
        )
        Parent.objects.create(
            family=family,
            first_name=data['parent_first_name'].strip(),
            last_name=data['parent_last_name'].strip(),
            phone=data['parent_phone'].strip(),
            email=parent_email,
            is_primary=True,
        )
    else:
        updates = {}
        phone = data['parent_phone'].strip()
        if phone and family.phone != phone:
            family.phone = phone
            updates['phone'] = phone
        if parent_email and family.email != parent_email:
            family.email = parent_email
            updates['email'] = parent_email
        if updates:
            family.save(update_fields=list(updates.keys()) + ['updated_at'])
        primary = family.parents.filter(is_primary=True).first()
        if primary:
            parent_updates = {}
            if phone and primary.phone != phone:
                primary.phone = phone
                parent_updates['phone'] = phone
            if parent_email and primary.email != parent_email:
                primary.email = parent_email
                parent_updates['email'] = parent_email
            if parent_updates:
                primary.save(update_fields=list(parent_updates.keys()) + ['updated_at'])

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

    return family, child


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
      bundle_id (str) — register for a combined "twice a week" LessonBundle of this
        course instead of the course's first lesson. Creates one Payment per member
        lesson (each billed at combined_price / lesson_count); response has
        `is_bundle: True` and a `payments` list instead of a single payment_id.
      lesson_id (str) — register for this specific Lesson of the course instead of
        the course's first lesson. Ignored if bundle_id is also provided.
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

        lesson_id = (data.get('lesson_id') or '').strip()
        if lesson_id:
            lesson = next((l for l in lessons if str(l.id) == lesson_id), None)
            if lesson is None:
                return Response({'error': 'שיעור לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        else:
            lesson = lessons[0]

        bundle = None
        bundle_id = (data.get('bundle_id') or '').strip()
        if bundle_id:
            bundle = _resolve_widget_bundle(course=course, bundle_id=bundle_id)
            if bundle is None:
                return Response({'error': 'מסלול משולב לא נמצא'}, status=status.HTTP_404_NOT_FOUND)

        try:
            with transaction.atomic():
                family, child = _resolve_family_and_child(data, lesson.course.branch)
        except Exception as exc:
            return Response(
                {'error': f'שגיאה ביצירת הרשומה: {exc}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # ── 3. Initiate payment (pricing + pending Payment record) ───────
        try:
            if bundle:
                payments = [
                    PaymentService().initiate_subscription_payment(
                        child_id=str(child.id),
                        lesson_id=str(member_lesson.id),
                        success_url=data.get('success_url', ''),
                        error_url=data.get('error_url', ''),
                        callback_url=data.get('callback_url', ''),
                        bundle_id=str(bundle.id),
                    )
                    for member_lesson in bundle.lessons.all()
                ]
                return Response({
                    'is_bundle': True,
                    'bundle_id': str(bundle.id),
                    'payments': payments,
                    'base_amount': sum(p['base_amount'] for p in payments),
                    'discount_amount': sum(p['discount_amount'] for p in payments),
                    'prorated_amount': sum(p['prorated_amount'] for p in payments),
                    'registration_fee': sum(p['registration_fee'] for p in payments),
                    'final_amount': sum(p['final_amount'] for p in payments),
                }, status=status.HTTP_201_CREATED)

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


class WidgetTrialRegisterView(APIView):
    """
    Register a child for a single free trial lesson — no payment involved.

    Required fields:
      parent_id_number, parent_first_name, parent_last_name, parent_phone
      child_first_name, child_last_name, child_birth_date, child_gender
      course_id

    Optional:
      lesson_id (str) — specific Lesson of the course; defaults to the course's first lesson.
      child_id_number, child_phone
      discount_confirmed  (bool) — parent confirmed the discount question
      existing_child_id   (str)  — returned by lookup when child is already active
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

        lesson_id = (data.get('lesson_id') or '').strip()
        if lesson_id:
            lesson = next((l for l in lessons if str(l.id) == lesson_id), None)
            if lesson is None:
                return Response({'error': 'שיעור לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        else:
            lesson = lessons[0]

        from apps.enrollments.models import LessonEnrollment
        from apps.enrollments.trial_reminders import (
            stamp_and_notify_trial_enrollment,
            validate_trial_lesson_date,
        )
        from datetime import date
        import logging

        logger = logging.getLogger(__name__)

        trial_date_str = (data.get('trial_lesson_date') or '').strip()
        if not trial_date_str:
            return Response(
                {'error': 'יש לבחור תאריך לשיעור הניסיון'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            trial_date = date.fromisoformat(trial_date_str)
        except ValueError:
            return Response({'error': 'תאריך לא תקין'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            validate_trial_lesson_date(lesson, trial_date)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        from apps.enrollments.enrollment_counts import count_capacity_enrollments

        capacity = course.capacity or 20
        if count_capacity_enrollments(lesson=lesson, occurrence_date=trial_date) >= capacity:
            return Response(
                {'error': f'השיעור מלא בתאריך הנבחר (קיבולת: {capacity})'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        trial_is_paid = bool(course.trial_lesson_is_paid)
        trial_price = course.trial_lesson_price
        if trial_is_paid and (trial_price is None or trial_price <= 0):
            return Response(
                {'error': 'שיעור הניסיון מוגדר כבתשלום אך לא הוגדר מחיר'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if trial_is_paid:
            from apps.customers.models import Payment
            from decimal import Decimal

            try:
                with transaction.atomic():
                    family, child = _resolve_family_and_child(data, lesson.course.branch)
                    payment = Payment.objects.create(
                        child=child,
                        family=child.family,
                        parent=child.family.parents.filter(is_primary=True).first(),
                        branch=lesson.course.branch,
                        lesson=lesson,
                        payment_type='one_time',
                        status='pending',
                        base_amount=Decimal(trial_price),
                        discount_amount=Decimal('0'),
                        final_amount=Decimal(trial_price),
                        trial_lesson_date=trial_date,
                        description=f"שיעור ניסיון - {course.name} - {child.full_name}",
                    )
            except Exception as exc:
                return Response(
                    {'error': f'שגיאה ביצירת הרשומה: {exc}'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            return Response({
                'requires_payment': True,
                'payment_id': str(payment.id),
                'child_id': str(child.id),
                'base_amount': float(trial_price),
                'discount_amount': 0,
                'final_amount': float(trial_price),
                'trial_applied': True,
                'trial_lesson_date': trial_date.isoformat(),
            }, status=status.HTTP_201_CREATED)

        try:
            with transaction.atomic():
                family, child = _resolve_family_and_child(data, lesson.course.branch)
                enrollment, _created = LessonEnrollment.objects.get_or_create(
                    child=child,
                    lesson=lesson,
                    defaults={
                        'start_date': trial_date,
                        'status': 'active',
                        'trial_lesson_date': trial_date,
                    },
                )
                if not _created:
                    enrollment.start_date = trial_date
                    enrollment.trial_lesson_date = trial_date
                    enrollment.status = 'active'
                    enrollment.end_date = None
                    enrollment.save(update_fields=[
                        'start_date', 'trial_lesson_date', 'status', 'end_date', 'updated_at',
                    ])
        except Exception as exc:
            return Response(
                {'error': f'שגיאה ביצירת הרשומה: {exc}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        try:
            whatsapp_result = stamp_and_notify_trial_enrollment(str(enrollment.id))
        except Exception:
            logger.exception("Trial WhatsApp notification failed (non-fatal)")
            whatsapp_result = {'sent': False, 'reason': 'exception'}

        Child.objects.filter(pk=child.pk).update(status='trial_signed')

        return Response({
            'enrollment_id': str(enrollment.id),
            'child_id': str(child.id),
            'trial_lesson_date': trial_date.isoformat(),
            'trial_applied': True,
            'whatsapp': whatsapp_result,
        }, status=status.HTTP_201_CREATED)


class WidgetChargeView(APIView):
    """
    Charge an existing pending Payment directly with card details (no iframe/webhook).

    POST /api/v1/customers/widget/charge/
    Body: {
        "payment_id": "uuid",              # single-lesson registration
        # or, for a bundle registration (see WidgetRegisterView `payments` list):
        "payment_ids": ["uuid", "uuid"],   # one per bundle member lesson, charged with the same card
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
        card_details = request.data.get('card_details') or {}
        if not card_details.get('card_number'):
            return Response({'error': 'פרטי כרטיס נדרשים'}, status=status.HTTP_400_BAD_REQUEST)

        payment_ids = request.data.get('payment_ids')
        if payment_ids:
            results = [self._charge_one(str(pid), card_details) for pid in payment_ids]
            all_success = all(r.get('success') for r in results)
            status_code = status.HTTP_200_OK if all_success else status.HTTP_400_BAD_REQUEST
            return Response({'success': all_success, 'results': results}, status=status_code)

        payment_id = (request.data.get('payment_id') or '').strip()
        if not payment_id:
            return Response({'error': 'payment_id נדרש'}, status=status.HTTP_400_BAD_REQUEST)

        result = self._charge_one(payment_id, card_details)
        status_code = status.HTTP_200_OK if result.get('success') else status.HTTP_400_BAD_REQUEST
        return Response(result, status=status_code)

    def _charge_one(self, payment_id, card_details):
        from apps.core.payment_service import (
            _compute_prorate,
            payment_full_monthly_amount,
            payment_prorated_lesson_amount,
            subscription_tranzila_items,
        )
        from apps.customers.models import Payment, TranzilaTransaction, RecurringPayment
        from apps.customers.financial_models import Invoice, InvoiceChild
        from apps.core.tranzila_service import TranzilaService
        from apps.enrollments.models import LessonEnrollment
        from django.utils import timezone
        from datetime import date, timedelta
        from decimal import Decimal
        import logging

        logger = logging.getLogger(__name__)

        try:
            payment = Payment.objects.select_related('child', 'family', 'lesson__course__branch', 'bundle').get(
                id=payment_id, status='pending'
            )
        except Payment.DoesNotExist:
            return {'success': False, 'payment_id': payment_id, 'error': 'תשלום לא נמצא או כבר עובד'}

        child = payment.child
        lesson = payment.lesson

        try:
            card_number = str(card_details['card_number']).replace(' ', '')
            expiry_month = int(card_details['expiry_month'])
            expiry_year = int(card_details['expiry_year'])
            cvv = str(card_details['cvv'])
            card_holder_id = str(card_details.get('card_holder_id', ''))
        except (KeyError, ValueError, TypeError) as e:
            return {'success': False, 'payment_id': payment_id, 'error': f'פרטי כרטיס שגויים: {e}'}

        tranzila = TranzilaService()
        is_trial_payment = payment.trial_lesson_date is not None
        item_label = (
            f"שיעור ניסיון - {lesson.course.name} - {child.full_name}"
            if is_trial_payment and lesson
            else (f"{lesson.course.name} - {child.full_name}" if lesson else child.full_name)
        )
        if is_trial_payment:
            items = [{
                'name': item_label,
                'type': 'I',
                'unit_price': float(payment.final_amount),
                'units_number': 1,
                'unit_type': 1,
                'price_type': 'G',
                'currency_code': 'ILS',
            }]
        else:
            registration_fee = payment.registration_fee or Decimal('0')
            prorated_lesson = payment_prorated_lesson_amount(payment)
            items = subscription_tranzila_items(
                label=item_label,
                prorated_lesson=prorated_lesson,
                registration_fee=registration_fee,
            )

        result = tranzila.charge_with_card(
            card_number=card_number,
            expiry_month=expiry_month,
            expiry_year=expiry_year,
            cvv=cvv,
            card_holder_id=card_holder_id,
            amount=payment.final_amount,
            description=payment.description or (
                f"שיעור ניסיון - {child.full_name}" if is_trial_payment else f"מנוי - {child.full_name}"
            ),
            items=items,
        )

        if result['success']:
            enrollment_id_for_whatsapp = None
            with transaction.atomic():
                payment.status = 'completed'
                payment.payment_date = timezone.now()
                payment.save()

                tranzila_txn = TranzilaTransaction.objects.create(
                    transaction_id=result.get('transaction_id', ''),
                    confirmation_code=result.get('confirmation_code', ''),
                    transaction_type='charge' if is_trial_payment else 'recurring_setup',
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

                if is_trial_payment:
                    trial_date = payment.trial_lesson_date
                    enrollment, created = LessonEnrollment.objects.get_or_create(
                        child=child,
                        lesson=lesson,
                        defaults={
                            'start_date': trial_date,
                            'status': 'active',
                            'trial_lesson_date': trial_date,
                        },
                    )
                    if not created:
                        enrollment.start_date = trial_date
                        enrollment.trial_lesson_date = trial_date
                        enrollment.status = 'active'
                        enrollment.end_date = None
                        enrollment.save(update_fields=[
                            'start_date', 'trial_lesson_date', 'status', 'end_date', 'updated_at',
                        ])
                    enrollment_id_for_whatsapp = str(enrollment.id)

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
                        payment_type='one_time',
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
                    try:
                        from apps.customers.subscription_invoice_email import send_subscription_invoice_email
                        send_subscription_invoice_email(invoice)
                    except Exception:
                        logger.exception('Trial invoice email failed (non-fatal)')

                    Child.objects.filter(pk=child.pk).update(status='trial_signed')
                else:
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
                        enrollment_date = date.today()
                        lesson_dow = lesson.day_of_week if lesson else 1
                        _, _, _, next_billing_date = _compute_prorate(enrollment_date, lesson_dow)
                        RecurringPayment.objects.create(
                            child=child,
                            initial_payment=payment,
                            tranzila_token=token,
                            card_expire_month=expiry_month,
                            card_expire_year=expiry_year,
                            status='active',
                            base_amount=payment.base_amount,
                            discount_amount=payment.discount_amount,
                            amount=payment_full_monthly_amount(payment),
                            discount_details=discount_details,
                            billing_day=1,
                            start_date=enrollment_date,
                            next_billing_date=next_billing_date,
                        )

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
                    try:
                        from apps.customers.subscription_invoice_email import send_subscription_invoice_email
                        send_subscription_invoice_email(invoice)
                    except Exception:
                        logger.exception('Subscription invoice email failed (non-fatal)')

                    child.status = 'active'
                    child.subscription_start_date = date.today()
                    _, _, _, next_bill = _compute_prorate(date.today(), lesson.day_of_week)
                    child.paid_until_date = next_bill - timedelta(days=1)
                    child.save()

                    if lesson:
                        enrollment, created = LessonEnrollment.objects.get_or_create(
                            child=child,
                            lesson=lesson,
                            defaults={'start_date': date.today(), 'status': 'active', 'bundle': payment.bundle},
                        )
                        if not created:
                            enrollment.status = 'active'
                            if not enrollment.start_date:
                                enrollment.start_date = date.today()
                            if payment.bundle and not enrollment.bundle:
                                enrollment.bundle = payment.bundle
                            enrollment.save()

            if enrollment_id_for_whatsapp:
                try:
                    from apps.enrollments.trial_reminders import stamp_and_notify_trial_enrollment
                    stamp_and_notify_trial_enrollment(enrollment_id_for_whatsapp)
                except Exception:
                    logger.exception("Trial WhatsApp notification failed after paid trial charge (non-fatal)")

            return {'success': True, 'payment_id': str(payment.id)}

        else:
            payment.status = 'failed'
            payment.failure_reason = result.get('error', 'Unknown')
            payment.save()
            child.status = 'payment_problem'
            child.save()
            return {'success': False, 'payment_id': str(payment.id), 'error': result.get('error', 'התשלום נכשל')}


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
            .filter(
                branch_id=branch_id,
                is_active=True,
            )
            .filter(Q(course_type__is_active=True) | Q(course_type__isnull=True))
            .select_related('course_type', 'branch')
            .prefetch_related(
                Prefetch('lessons', queryset=Lesson.objects.select_related('instructor').order_by('day_of_week', 'start_time')),
                Prefetch(
                    'lesson_bundles',
                    queryset=_widget_bundles_queryset(),
                ),
            )
            .order_by('course_type__name', 'name')
        )

        result = []
        for course in courses:
            lessons = []
            if not course.must_attend_all_lessons:
                for lesson in course.lessons.all():
                    lessons.append({
                        'id': str(lesson.id),
                        'day_of_week': lesson.day_of_week,
                        'start_time': str(lesson.start_time)[:5],
                        'end_time': str(lesson.end_time)[:5],
                        'price': str(lesson.lesson_price_override or course.price),
                        'instructor_name': lesson.instructor.full_name if lesson.instructor else None,
                        'lesson_date': lesson.lesson_date.isoformat() if lesson.lesson_date else None,
                        'is_recurring': lesson.is_recurring,
                    })

            bundles = [_serialize_widget_bundle(bundle) for bundle in _widget_bundles_for_course(course)]

            result.append({
                'id': str(course.id),
                'name': course.name,
                'course_type': str(course.course_type_id) if course.course_type_id else None,
                'course_type_name': course.course_type.name if course.course_type else None,
                'course_type_description': course.course_type.description if course.course_type else None,
                'branch_name': course.branch.name,
                'price': str(course.price),
                'min_age': course.min_age,
                'max_age': course.max_age,
                'is_adult': course.is_adult,
                'must_attend_all_lessons': course.must_attend_all_lessons,
                'trial_lesson_is_paid': course.trial_lesson_is_paid,
                'trial_lesson_price': str(course.trial_lesson_price) if course.trial_lesson_price is not None else None,
                'external_link': course.external_link,
                'lessons_count': len(lessons),
                'lessons': lessons,
                'bundles': bundles,
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
            .values('id', 'name', 'city_id', 'is_external', 'external_link')
            .order_by('name')
        )
        return Response([
            {'id': b['id'], 'name': b['name'], 'city': b['city_id'], 'is_external': b['is_external'], 'external_link': b['external_link']}
            for b in branches
        ])


class WidgetLessonOccurrencesView(APIView):
    """Public endpoint — upcoming calendar dates for a lesson (trial date picker)."""
    authentication_classes = []
    permission_classes = [AllowAny]

    DAY_NAMES = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']

    def get(self, request):
        lesson_id = (request.query_params.get('lesson_id') or '').strip()
        if not lesson_id:
            return Response({'error': 'lesson_id נדרש'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            count = int(request.query_params.get('count') or 8)
        except (TypeError, ValueError):
            count = 8
        count = max(1, min(count, 16))

        try:
            lesson = Lesson.objects.get(id=lesson_id)
        except Lesson.DoesNotExist:
            return Response({'error': 'שיעור לא נמצא'}, status=status.HTTP_404_NOT_FOUND)

        from apps.enrollments.trial_reminders import iter_upcoming_lesson_occurrences

        dates = iter_upcoming_lesson_occurrences(lesson, count=count)
        return Response([
            {
                'date': d.isoformat(),
                'label': d.strftime('%d/%m/%Y'),
                'day_name': self.DAY_NAMES[(d.weekday() + 1) % 7],
                'start_time': str(lesson.start_time)[:5],
                'end_time': str(lesson.end_time)[:5],
            }
            for d in dates
        ])
