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
from apps.courses.models import Lesson
from apps.core.payment_service import PaymentService


class WidgetLookupView(APIView):
    """
    Check parent ID + child name against existing records to determine discount eligibility.

    Returns one of:
      family_status='new'      → no family found, no discount
      family_status='existing', child_status='active'  → same child → additional-lesson discount
      family_status='existing', child_status='new'     → sibling    → second-child discount
    """
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
      lesson_id

    Optional:
      child_id_number, child_phone
      discount_confirmed  (bool) — parent confirmed the discount question
      existing_child_id   (str)  — returned by lookup when child is already active
      success_url, error_url, callback_url
    """
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data

        required = [
            'parent_id_number', 'parent_first_name', 'parent_last_name', 'parent_phone',
            'child_first_name', 'child_last_name', 'child_birth_date', 'child_gender',
            'lesson_id',
        ]
        missing = [f for f in required if not data.get(f)]
        if missing:
            return Response(
                {'error': f'שדות חובה חסרים: {", ".join(missing)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        lesson_id = data['lesson_id']
        try:
            lesson = Lesson.objects.select_related('course', 'branch').get(id=lesson_id)
        except Lesson.DoesNotExist:
            return Response({'error': 'שיעור לא נמצא'}, status=status.HTTP_404_NOT_FOUND)

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
                        branch=lesson.branch,
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

        # ── 3. Initiate payment ───────────────────────────────────────────
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
