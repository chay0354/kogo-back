import logging
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q
from django.core.mail import send_mail
from django.conf import settings
from django.http import HttpResponse

from apps.core.permissions import IsManager, IsManagerOrPartner
from apps.customers.models import Child
from apps.documents.models import FormalDocument, CheckPlan
from apps.documents.serializers import (
    FormalDocumentSerializer,
    FormalDocumentListSerializer,
    CreateDocumentSerializer,
    CheckPlanSerializer,
    CreateCheckPlanSerializer,
)
from apps.documents import service
from apps.documents.check_plans import register_check_plan

logger = logging.getLogger(__name__)


class FormalDocumentViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['document_number', 'description']
    ordering_fields = ['document_date', 'created_at', 'total_amount']
    ordering = ['-created_at']

    def get_queryset(self):
        qs = FormalDocument.objects.select_related('child', 'business_customer', 'branch')

        doc_type = self.request.query_params.get('document_type')
        if doc_type:
            qs = qs.filter(document_type=doc_type)

        child_id = self.request.query_params.get('child_id')
        if child_id:
            qs = qs.filter(child_id=child_id)

        business_customer_id = self.request.query_params.get('business_customer_id')
        if business_customer_id:
            qs = qs.filter(business_customer_id=business_customer_id)

        # Exclude credit invoices from the "open invoices" list when requested
        exclude_credits = self.request.query_params.get('exclude_credits')
        if exclude_credits:
            qs = qs.exclude(document_type='credit_invoice')

        return qs

    def get_serializer_class(self):
        if self.action == 'list':
            return FormalDocumentListSerializer
        return FormalDocumentSerializer

    @action(detail=False, methods=['get'], url_path='tranzila')
    def tranzila(self, request):
        """
        All Tranzila tax documents (plus local invoices issued from those charges).

        GET /api/v1/documents/documents/tranzila/?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
        """
        from apps.core.tranzila_ledger import list_ledger_documents
        from datetime import date as date_cls

        def parse_day(raw):
            try:
                return date_cls.fromisoformat(raw) if raw else None
            except ValueError:
                return None

        local_only = str(request.query_params.get('local_only') or '').lower() in ('1', 'true', 'yes')
        result = list_ledger_documents(
            start_date=parse_day(request.query_params.get('start_date')),
            end_date=parse_day(request.query_params.get('end_date')),
            local_only=local_only,
        )
        return Response(result)

    @action(detail=False, methods=['get'], url_path='period-report',
            permission_classes=[IsAuthenticated, IsManager])
    def period_report(self, request):
        """
        Every document in a period, grouped, totalled, as a PDF.

        GET /api/v1/documents/documents/period-report/?month=YYYY-MM&group_by=branch|business
        (or start_date/end_date for a custom range; document_type narrows it)

        Read-only: it selects rows that already exist and renders them. It
        creates, changes and charges nothing. Managers only — this is the whole
        business's revenue on one page, and scoped_documents() applies the
        caller's scope before anything is counted.
        """
        from apps.documents.period_report import (
            GROUP_BY_BRANCH, GROUP_BY_CHOICES, ReportInputError, build_report, parse_period,
        )
        from apps.documents.period_report_pdf import generate_period_report_pdf

        group_by = (request.query_params.get('group_by') or GROUP_BY_BRANCH).strip()
        if group_by not in GROUP_BY_CHOICES:
            return Response({'error': 'קיבוץ לא נתמך'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            start, end, label = parse_period(request.query_params)
            report = build_report(
                request.user, start, end, label,
                group_by=group_by,
                document_type=(request.query_params.get('document_type') or '').strip(),
            )
        except ReportInputError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # תקבולים שאין מאחוריהם מסמך — חיובי חוגים ומכירות חנות שהמסמך שלהן
        # נכשל. נאסף תמיד, אלא אם ביקשו דוח של סוג מסמך יחיד: שם הקורא מבקש
        # חתך של מסמכים, וסעיף בלי מסמך אינו שייך אליו.
        if not (request.query_params.get('document_type') or '').strip():
            from apps.documents.undocumented_income import collect_undocumented

            try:
                report.undocumented = collect_undocumented(request.user, start, end)
            except Exception:
                logger.exception('Period report undocumented income failed')

        # אימות מול טרנזילה, אלא אם ביקשו במפורש לוותר עליו (verify=0). נכשל
        # בשקט אם הקריאה נופלת: דוח שלא מצליח לאמת עדיף על דוח שלא מופק, והעמוד
        # עצמו אומר שלא בוצע אימות.
        if (request.query_params.get('verify') or '1').strip() not in ('0', 'false', 'no'):
            from apps.core.tranzila_ledger import reconcile_period

            try:
                report.reconciliation = reconcile_period(start, end)
            except Exception:
                logger.exception('Period report reconciliation failed')
                report.reconciliation = {'reachable': False, 'error': 'שגיאה בעת האימות'}

        pdf_bytes = generate_period_report_pdf(report)
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = (
            f'attachment; filename="invoices-{start.isoformat()}-{end.isoformat()}-{group_by}.pdf"'
        )
        return response

    @action(detail=False, methods=['get'], url_path='register-export',
            permission_classes=[IsAuthenticated, IsManager])
    def register_export(self, request):
        """
        Every document in a period, one row each, as a CSV the accountant opens in Excel.

        GET /api/v1/documents/documents/register-export/?month=YYYY-MM
        (or start_date/end_date). The same rows as the period report
        (apps/documents/register.py), and the numbers that never became a
        document. Read-only; managers only, like the report.
        """
        from apps.documents.period_report import ReportInputError, build_report, parse_period
        from apps.documents.register import register_csv

        try:
            start, end, label = parse_period(request.query_params)
            report = build_report(request.user, start, end, label)
        except ReportInputError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        response = HttpResponse(register_csv(report), content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = (
            f'attachment; filename="documents-{start.isoformat()}-{end.isoformat()}.csv"'
        )
        return response

    @action(detail=False, methods=['get'], url_path='uniform-export',
            permission_classes=[IsAuthenticated, IsManager])
    def uniform_export(self, request):
        """
        The period's documents in the uniform structure (מבנה אחיד): INI.TXT and
        BKMVDATA.TXT in their OPENFRMT folder, zipped.

        GET /api/v1/documents/documents/uniform-export/?month=YYYY-MM
        (or start_date/end_date inside one tax year). The same register as the
        period report (apps/documents/uniform_export.py). Read-only; managers only.
        """
        from apps.documents.period_report import ReportInputError, parse_period
        from apps.documents.uniform_export import build_uniform_export
        from apps.documents.uniform_format import UniformFormatError

        try:
            start, end, label = parse_period(request.query_params)
            archive, filename = build_uniform_export(request.user, start, end, label)
        except ReportInputError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except UniformFormatError as exc:
            # A stored document the format cannot carry, named so it can be found —
            # never a file with that document quietly missing.
            logger.warning('Uniform export refused: %s', exc)
            return Response(
                {'error': f'מסמך בטווח אינו עומד בדרישות המבנה האחיד: {exc}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        response = HttpResponse(archive, content_type='application/zip')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    @action(detail=False, methods=['post'], url_path='create-document')
    def create_document(self, request):
        """
        Unified endpoint for all document types.
        POST /api/v1/documents/documents/create-document/
        """
        serializer = CreateDocumentSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        doc_type = data['document_type']

        try:
            if doc_type in ('tax_invoice', 'transaction_invoice'):
                doc = service.create_invoice(data, doc_type)
            elif doc_type == 'combined':
                doc = service.create_combined(data)
            elif doc_type == 'receipt':
                doc = service.create_receipt(data)
            elif doc_type == 'credit_invoice':
                doc = service.create_credit_invoice(data)
            elif doc_type == 'draft':
                doc = service.create_draft(data)
            else:
                return Response({'error': f'סוג מסמך לא נתמך: {doc_type}'}, status=status.HTTP_400_BAD_REQUEST)

            out = FormalDocumentSerializer(doc)
            return Response(out.data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Document creation failed: {e}", exc_info=True)
            return Response({'error': f'שגיאה ביצירת המסמך: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='finalize',
            permission_classes=[IsAuthenticated, IsManager])
    def finalize(self, request, pk=None):
        """
        POST /api/v1/documents/documents/{id}/finalize/
        Approve a draft: it becomes its target type and takes the next fiscal number.
        """
        doc = self.get_object()
        try:
            doc = service.finalize_draft(doc)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(FormalDocumentSerializer(doc).data)

    @action(detail=True, methods=['get'], url_path='pdf')
    def pdf(self, request, pk=None):
        """
        GET /api/v1/documents/documents/{id}/pdf/
        Locally rendered PDF — for drafts, credit invoices, and documents Tranzila did not issue.
        """
        from apps.documents.document_pdf import generate_document_pdf
        doc = self.get_object()
        pdf_bytes = generate_document_pdf(doc)
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{doc.document_number}.pdf"'
        return response

    @action(detail=True, methods=['post'], url_path='send-reminder')
    def send_reminder(self, request, pk=None):
        """
        POST /api/v1/documents/documents/{id}/send-reminder/
        Sends a payment reminder email to the customer.
        """
        doc = self.get_object()

        # Resolve recipient email
        email = None
        customer_name = ''
        if doc.client_type == 'business' and doc.business_customer:
            email = doc.business_customer.email or None
            customer_name = doc.business_customer.full_name
        elif doc.client_type == 'existing' and doc.child:
            family = getattr(doc.child, 'family', None)
            if family:
                email = family.email or None
                customer_name = doc.child.full_name

        if not email:
            return Response({'error': 'no_email'}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        due_date_str = doc.due_date.strftime('%d/%m/%Y') if doc.due_date else 'לא הוגדר'
        subject = f'תזכורת תשלום — מסמך {doc.document_number}'
        body = (
            f'שלום {customer_name},\n\n'
            f'זוהי תזכורת לתשלום עבור {doc.document_type_display if hasattr(doc, "document_type_display") else "מסמך"} '
            f'מספר {doc.document_number}.\n\n'
            f'סכום לתשלום: ₪{doc.total_amount}\n'
            f'תאריך פירעון: {due_date_str}\n\n'
            f'בברכה,\nצוות קוגומלו'
        )

        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@kogomalo.com')
        try:
            send_mail(subject, body, from_email, [email], fail_silently=False)
        except Exception as e:
            logger.error(f"Reminder email failed for doc {pk}: {e}", exc_info=True)
            return Response({'error': 'send_failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({'sent': True})


class CheckPlanViewSet(viewsets.ReadOnlyModelViewSet):
    """Office check series: list existing plans and register a new one with a receipt."""

    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    serializer_class = CheckPlanSerializer
    pagination_class = None
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        'description',
        'child__first_name',
        'child__last_name',
        'items__check_number',
    ]
    ordering_fields = ['created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        from apps.core.ledger_dimensions import lesson_paths

        qs = (
            CheckPlan.objects
            # Everything the ledger dimensions read (CheckPlanSerializer): the
            # lesson's course, type, business, city and instructor, and the
            # plan branch's city.
            .select_related('child', 'branch', 'branch__city', 'receipt', *lesson_paths('lesson'))
            .prefetch_related('items', 'items__tax_invoice')
        )
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        child_id = self.request.query_params.get('child_id')
        if child_id:
            qs = qs.filter(child_id=child_id)
        branch_id = self.request.query_params.get('branch')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return qs

    def create(self, request):
        serializer = CreateCheckPlanSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        try:
            plan = register_check_plan(
                child_id=str(data['child_id']),
                checks=data['checks'],
                description=data.get('description') or '',
                lesson_id=str(data['lesson_id']) if data.get('lesson_id') else None,
            )
        except Child.DoesNotExist:
            return Response({'error': 'הילד לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.error('Check plan registration failed: %s', exc, exc_info=True)
            return Response(
                {'error': f'שגיאה ברישום הצ׳קים: {exc}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        plan = self.get_queryset().get(pk=plan.pk)
        return Response(CheckPlanSerializer(plan).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        plan = self.get_object()
        if plan.status == 'cancelled':
            return Response(CheckPlanSerializer(plan).data)
        plan.status = 'cancelled'
        plan.save(update_fields=['status', 'updated_at'])
        plan.items.filter(status='pending').update(status='cancelled')
        plan = self.get_queryset().get(pk=plan.pk)
        return Response(CheckPlanSerializer(plan).data)


class MissingReceiptsViewSet(viewsets.ViewSet):
    """
    Completed charges that never got their חשבונית מס / קבלה — `check_invoices`,
    for the office (apps/documents/missing_receipts.py holds both).

    GET  /api/v1/documents/missing-receipts/?year=YYYY
    GET  /api/v1/documents/missing-receipts/export/?year=YYYY   (CSV for the accountant)
    POST /api/v1/documents/missing-receipts/issue/  {payment_ids: [...], confirm: 'הפק'}

    Managers only: the list is the whole business's money, and issuing takes
    numbers in the IR run that can never be given back.
    """

    permission_classes = [IsAuthenticated, IsManager]

    @staticmethod
    def _year(request):
        """The year asked for (this one by default), or None when it is not a year."""
        from django.utils import timezone

        this_year = timezone.localdate().year
        raw = (request.query_params.get('year') or '').strip()
        if not raw:
            return this_year
        try:
            year = int(raw)
        except ValueError:
            return None
        return year if 2000 <= year <= this_year + 1 else None

    def list(self, request):
        from apps.documents.missing_receipts import missing_receipts_report

        year = self._year(request)
        if year is None:
            return Response({'error': 'שנה לא תקינה'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(missing_receipts_report(year))

    @action(detail=False, methods=['get'], url_path='export')
    def export(self, request):
        from apps.documents.missing_receipts import missing_receipts_csv, missing_receipts_report

        year = self._year(request)
        if year is None:
            return Response({'error': 'שנה לא תקינה'}, status=status.HTTP_400_BAD_REQUEST)
        response = HttpResponse(
            missing_receipts_csv(missing_receipts_report(year)), content_type='text/csv; charset=utf-8',
        )
        response['Content-Disposition'] = f'attachment; filename="missing-receipts-{year}.csv"'
        return response

    @action(detail=False, methods=['post'], url_path='issue')
    def issue(self, request):
        """
        Issue the chosen charges' receipts, exactly as `check_invoices --fix` does:
        dated today, in payment order, marked "הופק באיחור", never mailed, never
        backdated. Safe to repeat — a charge that has its receipt is skipped.
        """
        import uuid

        from apps.documents.missing_receipts import CONFIRM_WORD, MAX_ISSUE_BATCH, issue_missing_receipts

        if request.data.get('confirm') != CONFIRM_WORD:
            return Response(
                {'error': f'לא הופקו קבלות: כדי להפיק יש להקליד "{CONFIRM_WORD}" לאישור.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        raw_ids = request.data.get('payment_ids')
        if not isinstance(raw_ids, list) or not raw_ids:
            return Response({'error': 'לא נבחרו תשלומים להפקה.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(raw_ids) > MAX_ISSUE_BATCH:
            return Response(
                {'error': f'אפשר להפיק עד {MAX_ISSUE_BATCH} קבלות בפעם אחת.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            payment_ids = [str(uuid.UUID(str(raw))) for raw in raw_ids]
        except ValueError:
            return Response({'error': 'מזהה תשלום לא תקין.'}, status=status.HTTP_400_BAD_REQUEST)

        return Response(issue_missing_receipts(payment_ids, user=request.user))
