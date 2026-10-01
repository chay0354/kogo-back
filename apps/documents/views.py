import logging
from rest_framework import viewsets, filters, serializers, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from django.db.models import Q
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone

from apps.core.permissions import IsManager, IsManagerOrPartner
from apps.customers.models import Child
from apps.documents.models import FormalDocument, CheckPlan, CashPlan
from apps.documents.serializers import (
    FormalDocumentSerializer,
    FormalDocumentListSerializer,
    CreateDocumentSerializer,
    CashPlanSerializer,
    CheckPlanSerializer,
    CreateCashPlanSerializer,
    CreateCheckPlanSerializer,
)
from apps.documents import service
from apps.documents.check_plans import register_check_plan
from apps.documents.settlement import settle_on_issue
from apps.documents.partner_scope import (
    document_create_refusal,
    partner_branches,
    plan_create_refusal,
    scope_documents,
    scope_plans,
)

logger = logging.getLogger(__name__)


class FormalDocumentViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['document_number', 'description']
    ordering_fields = ['document_date', 'created_at', 'total_amount']
    ordering = ['-created_at']

    @action(detail=True, methods=['post'], url_path='allocation-number')
    def set_allocation_number(self, request, pk=None):
        """
        Type in the number fetched by hand from the Tax Authority portal.

        There is no API integration yet, so the number arrives through a person.
        It is still checked: nine digits, and only on a document type that can
        carry one. Clearing it is allowed — a number entered on the wrong row
        has to be removable — until the original is signed.

        The number belongs on the original (the Tax Authority's "חשבוניות
        ישראל" FAQ, question 10), so a document that needs one is held unsigned
        until it arrives (signing/sources.FormalDocumentSource.awaiting_allocation).
        Entering it signs the original — with the number on it — and mails it
        after the commit when it goes by mail. Once the original is signed its
        number is fixed: changing or clearing it is 409. An original signed
        before any number was entered (issued before this gate) may still take
        one: it goes on the copies only, and the answer says so.

        200 {id, allocation_number, allocation_entered_at, copy_only, delivery,
        delivery_reason, signed}; 400 on a bad number or a document type that
        carries none; 409 when the signed original already carries another.
        """
        from django.db import transaction
        from django.utils import timezone

        from apps.documents.document_pdf import TAX_DOCUMENT_TYPES
        from apps.documents.signing.service import (
            ALLOCATION_COPY_ONLY, ALLOCATION_ON_ORIGINAL, release_allocation_hold, signed_original_of,
        )

        doc = self.get_object()
        if doc.document_type not in TAX_DOCUMENT_TYPES:
            return Response(
                {'error': 'מספר הקצאה נרשם על חשבונית מס בלבד'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw = str(request.data.get('allocation_number') or '').strip()
        digits = ''.join(ch for ch in raw if ch.isdigit())
        if raw and len(digits) != 9:
            return Response(
                {'error': 'מספר הקצאה הוא 9 ספרות'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        copy_only = False
        with transaction.atomic():
            # The original's row first, then the document — the order signing
            # takes them in (sign_original locks the row, then reads the
            # document): an original is never signed between this check and
            # the write, and never signed without a number written here.
            row = signed_original_of(doc.document_number, lock=True)
            doc = FormalDocument.objects.select_for_update().get(pk=doc.pk)
            current = (doc.allocation_number or '').strip()
            if row is not None and row.is_signed:
                if digits == current:
                    return Response(self._allocation_answer(doc, row))
                if current:
                    return Response(
                        {'error': ALLOCATION_ON_ORIGINAL.format(number=current)},
                        status=status.HTTP_409_CONFLICT,
                    )
                copy_only = True
            doc.allocation_number = digits
            doc.allocation_entered_at = timezone.now() if digits else None
            doc.allocation_entered_by = request.user if digits else None
            doc.save(update_fields=[
                'allocation_number', 'allocation_entered_at', 'allocation_entered_by', 'updated_at',
            ])
        if digits and row is not None and not row.is_signed:
            # Its original waited unsigned: signed now, with the number on it,
            # and mailed after the commit. A document with no original row
            # (issued before signing, or with signing off) is left as it was —
            # signing it now would draw a second "מקור".
            release_allocation_hold(doc.pk)
        logger.info(
            'Allocation number %s on %s by %s%s',
            'set' if digits else 'cleared', doc.document_number,
            getattr(request.user, 'email', request.user),
            ' (copies only — the original was signed before it)' if copy_only else '',
        )
        answer = self._allocation_answer(doc, signed_original_of(doc.document_number))
        if copy_only:
            answer['message'] = ALLOCATION_COPY_ONLY
        answer['copy_only'] = copy_only
        return Response(answer)

    @staticmethod
    def _allocation_answer(doc, row) -> dict:
        return {
            'id': str(doc.id),
            'allocation_number': doc.allocation_number,
            'allocation_entered_at': doc.allocation_entered_at,
            'copy_only': False,
            # The original's state after the entry: signed, and where it went.
            'signed': bool(row is not None and row.is_signed),
            'delivery': row.delivery if row is not None else None,
            'delivery_reason': row.delivery_reason if row is not None else '',
        }

    @action(detail=True, methods=['post'], url_path='customer-ack',
            permission_classes=[IsAuthenticated, IsManager])
    def customer_ack(self, request, pk=None):
        """
        POST /api/v1/documents/documents/{id}/customer-ack/  {note, date?}

        הוראה 23א(3): a credit note reduces the VAT once the customer confirms
        receiving it. Records when, and how (`note`: a signature on the copy,
        registered mail, a signed reply). `date` (YYYY-MM-DD, optional) is the
        day the confirmation arrived when it is recorded later — not in the
        future, not before the credit note; without it, now. Once — a second
        answer is 409.
        """
        from datetime import date as date_cls

        doc = self.get_object()
        raw_date = str(request.data.get('date') or '').strip()
        on = None
        if raw_date:
            try:
                on = date_cls.fromisoformat(raw_date)
            except ValueError:
                return Response({'error': 'תאריך האישור אינו תקין'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            doc = service.record_customer_ack(doc, request.data.get('note') or '', on=on)
        except service.AlreadyAcknowledged as exc:
            return Response(
                {'error': f'אישור הלקוח כבר נרשם ({timezone.localtime(exc.at):%d/%m/%Y %H:%M})'},
                status=status.HTTP_409_CONFLICT,
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        logger.info('Credit note %s: customer acknowledgement recorded by %s',
                    doc.document_number, getattr(request.user, 'email', request.user))
        return Response({
            'id': str(doc.id),
            'customer_ack_at': doc.customer_ack_at,
            'customer_ack_note': doc.customer_ack_note,
        })

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

        # The "open invoices" pickers (a receipt's link, a credit note's original)
        # ask for exclude_credits: neither a credit note nor a draft is a
        # document anything can be paid or credited against — a draft has no
        # number yet, and is not a tax document until it is approved.
        exclude_credits = self.request.query_params.get('exclude_credits')
        if exclude_credits:
            qs = qs.exclude(document_type__in=('credit_invoice', 'draft'))

        # A partner reaches their own branches' documents only — in the list and
        # in every action that finds a document by id (partner_scope.py).
        return scope_documents(qs, self.request.user)

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
        # A partner gets their branches' local rows; Tranzila's list is the whole
        # terminal's and carries no branch, so it is never fetched for them.
        branch_ids = partner_branches(request.user)
        result = list_ledger_documents(
            start_date=parse_day(request.query_params.get('start_date')),
            end_date=parse_day(request.query_params.get('end_date')),
            local_only=local_only or branch_ids is not None,
            branch_ids=branch_ids,
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
        document; below them, the period's income without a document.
        Read-only; managers only, like the report.
        """
        from apps.documents.period_report import ReportInputError, build_report, parse_period
        from apps.documents.register import register_csv
        from apps.documents.undocumented_income import collect_undocumented

        try:
            start, end, label = parse_period(request.query_params)
            report = build_report(request.user, start, end, label)
        except ReportInputError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # The income with no document behind it goes in a second section, as on
        # the period report. A failure there is said in the file, never hidden.
        failed = False
        try:
            report.undocumented = collect_undocumented(request.user, start, end)
        except Exception:
            logger.exception('Register export: undocumented income failed')
            failed = True

        response = HttpResponse(register_csv(report, undocumented_failed=failed),
                                content_type='text/csv; charset=utf-8')
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

        refusal = document_create_refusal(request.user, data)
        if refusal:
            return Response({'error': refusal[1]}, status=refusal[0])

        user = request.user
        check_plan = None
        if doc_type not in ('tax_invoice', 'transaction_invoice', 'combined', 'receipt', 'credit_invoice', 'draft'):
            return Response({'error': f'סוג מסמך לא נתמך: {doc_type}'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            # One transaction for the document and what it settles: a refused
            # settlement rolls the document back, number and all.
            with transaction.atomic():
                if doc_type in ('tax_invoice', 'transaction_invoice'):
                    doc = service.create_invoice(data, doc_type, issued_by=user)
                elif doc_type == 'combined':
                    doc = service.create_combined(data, issued_by=user)
                    settle_on_issue(doc, data, user=user)
                elif doc_type == 'receipt':
                    doc = service.create_receipt(data, issued_by=user)
                    settle_on_issue(doc, data, user=user)
                    if (data.get('receipt_details') or {}).get('invoice_per_check'):
                        # "חשבונית מס לכל צ'ק": the checks become a plan (D2).
                        from apps.documents.check_plans import plan_for_receipt

                        check_plan = plan_for_receipt(doc)
                elif doc_type == 'credit_invoice':
                    doc = service.create_credit_invoice(data, issued_by=user)
                else:
                    doc = service.create_draft(data)

            out = dict(FormalDocumentSerializer(doc, context={'request': request}).data)
            if check_plan is not None:
                out['check_plan_id'] = str(check_plan.pk)
            return Response(out, status=status.HTTP_201_CREATED)

        except ValueError as exc:
            # The service's refusals (a date its run refuses, a payment that
            # does not add up, a credit above what is left to credit) say why
            # in Hebrew; nothing was issued and no number was used.
            logger.info('Document creation refused: %s', exc)
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Document creation failed: {e}", exc_info=True)
            return Response({'error': f'שגיאה ביצירת המסמך: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='finalize',
            permission_classes=[IsAuthenticated, IsManager])
    def finalize(self, request, pk=None):
        """
        POST /api/v1/documents/documents/{id}/finalize/
        Approve a draft: it becomes its target type and takes the next fiscal number.

        A receipt's or an invoice-receipt's draft is checked again first — its
        payments add up, and each invoice it settles still owes what it pays.
        A refusal is 400 with the reason, and no number is used.
        """
        doc = self.get_object()
        try:
            doc = service.finalize_draft(doc, issued_by=request.user)
        except ValueError as exc:
            logger.info('Draft approval refused: %s', exc)
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(FormalDocumentSerializer(doc, context={'request': request}).data)

    @action(detail=True, methods=['post'], url_path='discard',
            permission_classes=[IsAuthenticated, IsManager])
    def discard(self, request, pk=None):
        """
        POST /api/v1/documents/documents/{id}/discard/

        Delete a draft — it has no number, no original and settles nothing, so
        nothing is left behind. Managers only, like the approval. 200
        {id, document_number}; 400 on anything that is no longer a draft.
        """
        doc = self.get_object()
        try:
            number = service.discard_draft(doc)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        logger.info('Draft %s discarded by %s', number, getattr(request.user, 'email', request.user))
        return Response({'id': str(pk), 'document_number': number})

    @action(detail=True, methods=['get'], url_path='pdf')
    def pdf(self, request, pk=None):
        """
        GET /api/v1/documents/documents/{id}/pdf/
        Locally rendered PDF — for drafts, credit invoices, and documents Tranzila did not issue.
        """
        from apps.documents.document_pdf import generate_document_pdf
        from apps.documents.signing.service import office_copy
        doc = self.get_object()
        # Once originals are signed and stored at issue, every print the office
        # makes is a copy (נספח ה'(א)(4)); the original is the stored file.
        pdf_bytes = generate_document_pdf(doc, copy=office_copy())
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{doc.document_number}.pdf"'
        return response

    @action(detail=False, methods=['get'], url_path='open-invoices')
    def open_invoices(self, request):
        """
        GET /api/v1/documents/documents/open-invoices/?child_id=… | business_customer_id=…
            [&payer_type=receipt|combined]

        The picker of the receipt form: the customer's invoices a document of
        `payer_type` can close that still owe something, oldest first. A
        receipt closes tax invoices, an invoice-receipt transaction invoices
        (settlement.PAYER_FOR). A partner sees their branches' invoices only.
        """
        from apps.documents.settlement import PAYER_FOR, money, open_invoices

        payer_type = (request.query_params.get('payer_type') or 'receipt').strip()
        if payer_type not in PAYER_FOR:
            return Response({'error': 'payer_type הוא receipt או combined'}, status=status.HTTP_400_BAD_REQUEST)
        child_id = request.query_params.get('child_id')
        customer_id = request.query_params.get('business_customer_id')
        if not child_id and not customer_id:
            return Response({'error': 'יש לבחור לקוח'}, status=status.HTTP_400_BAD_REQUEST)
        qs = FormalDocument.objects.all()
        try:
            qs = qs.filter(child_id=child_id) if child_id else qs.filter(business_customer_id=customer_id)
            rows = open_invoices(scope_documents(qs, request.user), payer_type=payer_type)
        except (ValueError, DjangoValidationError):
            return Response({'error': 'מזהה לקוח לא תקין'}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'payer_type': payer_type,
            'results': rows,
            'open_total': str(sum((money(row['open']) for row in rows), money(0))),
        })

    @action(detail=False, methods=['get'], url_path='credit-room')
    def credit_room(self, request):
        """
        GET /api/v1/documents/documents/credit-room/?number=TI-2026-000012

        "נותר לזכות" for the new-credit-note form: how much of the document
        `number` is left to credit, before VAT — its amount less the credit
        notes already issued against it (service.credit_room, the rule the
        credit note is checked by). Read-only.

        200 {number, known, kind, document_type, document_type_label,
        document_date, creditable, refusal, net, credited, left, child_id,
        business_customer_id}. `known` false: a number kogo never issued (the
        previous software's) — its amount is not known here. 400 without a
        number; 403 for a partner when the document is another branch's.
        """
        from apps.documents.partner_scope import LINKED_NOT_YOURS, _linked_elsewhere

        number = (request.query_params.get('number') or '').strip()
        if not number:
            return Response({'error': 'יש לציין מספר מסמך'}, status=status.HTTP_400_BAD_REQUEST)
        branch_ids = partner_branches(request.user)
        if branch_ids is not None and (not branch_ids or _linked_elsewhere(number, request.user, branch_ids)):
            return Response({'error': LINKED_NOT_YOURS}, status=status.HTTP_403_FORBIDDEN)
        return Response(service.credit_room(number).as_dict())

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
            .select_related('child', 'branch', 'branch__city', 'receipt', 'cancelled_by', *lesson_paths('lesson'))
            .prefetch_related('items', 'items__tax_invoice', 'items__credit_note', 'items__replaced_by')
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
        return scope_plans(qs, self.request.user)

    def create(self, request):
        serializer = CreateCheckPlanSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        refusal = plan_create_refusal(request.user, data['child_id'], data.get('lesson_id'))
        if refusal:
            return Response({'error': refusal[1]}, status=refusal[0])
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
        """
        POST /api/v1/documents/check-plans/{id}/cancel/  {reason?}

        Pending checks are cancelled (no invoice); an issued invoice nothing
        paid is credited (check_plans.cancel_check_plan). Repeating it does
        nothing again. 200 {...plan, credit_notes: [numbers]}.
        """
        from apps.documents.check_plans import CheckPlanError, cancel_check_plan

        plan = self.get_object()
        try:
            result = cancel_check_plan(plan.pk, user=request.user, reason=str(request.data.get('reason') or ''))
        except CheckPlanError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if not result['already_cancelled']:
            # The owner, 30.9.2026: the money stopped, so the status is worked out now, not next morning.
            from apps.customers.child_status import recheck_after_money_stopped

            recheck_after_money_stopped(plan.child, reason='תוכנית הצ׳קים בוטלה', changed_by=request.user)
        plan = self.get_queryset().get(pk=plan.pk)
        return Response({
            **CheckPlanSerializer(plan).data,
            'credit_notes': [doc.document_number for doc in result['credit_notes']],
        })

    @action(detail=True, methods=['post'], url_path='bounce')
    def bounce(self, request, pk=None):
        """
        POST /api/v1/documents/check-plans/{id}/bounce/
             {item_id, reason?, replacement?: {date, amount, bank, branch, account_number, check_number, check_crossed}}

        A check that came back unpaid (check_plans.bounce_check): marked, its
        invoice credited if one was issued, and a replacement check registered
        as a plan of its own. 200 {plan, item, credit_note_number,
        replacement_plan}; 404 for another plan's check; 409 when it is marked
        bounced already; 400 with the reason otherwise.
        """
        from apps.documents.check_plans import CheckAlreadyBounced, CheckPlanError, bounce_check
        from apps.documents.models import CheckItem

        plan = self.get_object()
        item_id = request.data.get('item_id')
        if not item_id:
            return Response({'error': "יש לבחור את הצ'ק שחזר"}, status=status.HTTP_400_BAD_REQUEST)
        replacement = request.data.get('replacement') or None
        if replacement is not None and not isinstance(replacement, dict):
            return Response({'error': "פרטי הצ'ק החלופי אינם תקינים"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = bounce_check(
                plan.pk, item_id, user=request.user,
                reason=str(request.data.get('reason') or ''), replacement=replacement,
            )
        except (CheckItem.DoesNotExist, DjangoValidationError):
            return Response({'error': "הצ'ק לא נמצא בתוכנית הזאת"}, status=status.HTTP_404_NOT_FOUND)
        except CheckAlreadyBounced as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
        except CheckPlanError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        replacement_plan = result['replacement_plan']
        return Response({
            'plan': CheckPlanSerializer(self.get_queryset().get(pk=plan.pk)).data,
            'item_id': str(result['item'].pk),
            'credit_note_number': result['credit_note'].document_number if result['credit_note'] else None,
            'replacement_plan': (
                CheckPlanSerializer(self.get_queryset().get(pk=replacement_plan.pk)).data
                if replacement_plan is not None else None
            ),
        })


class MissingReceiptsViewSet(viewsets.ViewSet):
    """
    Completed charges that never got their חשבונית מס / קבלה — `check_invoices`,
    for the office (apps/documents/missing_receipts.py holds both).

    GET  /api/v1/documents/missing-receipts/?year=YYYY
    GET  /api/v1/documents/missing-receipts/export/?year=YYYY   (CSV for the accountant)
    GET  /api/v1/documents/missing-receipts/next-number/        (the IR number issuing starts from, now)
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

    @action(detail=False, methods=['get'], url_path='next-number')
    def next_number(self, request):
        """The number the next late receipt takes — read again as the confirmation opens, so it is current."""
        from apps.documents.missing_receipts import next_receipt_number

        return Response({'next_number': next_receipt_number()})

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


class DocumentSeriesViewSet(viewsets.ViewSet):
    """
    The number runs of this tax year and the next, and continuing the previous
    software's runs in them (apps/documents/series_opening.py).

    GET  /api/v1/documents/series/
    POST /api/v1/documents/series/open/
         {series, year, start, previous_last_number, previous_type_label, note, reserve}
         `reserve`: start above last + 1, the numbers between left to the previous software.

    Managers only: an opening fixes where a run's numbers start, for good.
    """

    permission_classes = [IsAuthenticated, IsManager]

    def list(self, request):
        from apps.documents.series_opening import series_overview

        return Response(series_overview())

    @action(detail=False, methods=['post'], url_path='open')
    def open_run(self, request):
        from apps.documents.series_opening import OpeningRefused, open_series, series_overview

        data = request.data
        try:
            opening = open_series(
                series=data.get('series'),
                year=data.get('year'),
                start=data.get('start'),
                reserve=data.get('reserve') in (True, 'true', 'True', '1', 1),
                previous_last_number=data.get('previous_last_number'),
                previous_type_label=data.get('previous_type_label'),
                note=data.get('note') or '',
                user=request.user,
            )
        except OpeningRefused as exc:
            return Response(
                {'error': exc.message},
                status=status.HTTP_409_CONFLICT if exc.conflict else status.HTTP_400_BAD_REQUEST,
            )
        logger.info(
            'Series %s-%s opened at %s (previous %s %s) by %s',
            opening.series, opening.year, opening.start,
            opening.previous_type_label, opening.previous_last_number,
            getattr(request.user, 'email', request.user),
        )
        overview = series_overview()
        run = next(
            row for row in overview['runs']
            if row['series'] == opening.series and row['year'] == opening.year
        )
        return Response({'run': run, 'overview': overview}, status=status.HTTP_201_CREATED)


class CashPlanViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Cash paid up front: list the plans and register a new one.

    Registering issues the receipt for the whole sum immediately and lays out
    the months; any month already due gets its document in the same breath, and
    the rest are picked up by the monthly run.
    """

    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    serializer_class = CashPlanSerializer
    pagination_class = None
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['description', 'child__first_name', 'child__last_name']
    ordering_fields = ['created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        qs = (
            CashPlan.objects
            .select_related('child', 'branch', 'receipt', 'lesson', 'lesson__course', 'cancelled_by')
            .prefetch_related('months', 'months__document')
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
        return scope_plans(qs, self.request.user)

    @action(detail=False, methods=['post'], url_path='preview')
    def preview_plan(self, request):
        """The months and amounts, before anything is issued."""
        from apps.documents.cash_plans import preview

        try:
            return Response(preview(
                total_amount=request.data.get('total_amount'),
                monthly_amount=request.data.get('monthly_amount'),
                start_month=serializers.DateField().to_internal_value(request.data['start_month'])
                if request.data.get('start_month') else None,
            ))
        except (ValueError, ArithmeticError) as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def create(self, request):
        from apps.documents.cash_plans import register_cash_plan

        serializer = CreateCashPlanSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data
        refusal = plan_create_refusal(request.user, data['child_id'], data.get('lesson_id'))
        if refusal:
            return Response({'error': refusal[1]}, status=refusal[0])
        try:
            plan = register_cash_plan(
                child_id=str(data['child_id']),
                total_amount=data['total_amount'],
                monthly_amount=data['monthly_amount'],
                lesson_id=str(data['lesson_id']) if data.get('lesson_id') else None,
                start_month=data.get('start_month'),
                description=data.get('description') or '',
                monthly_document_type=data.get('monthly_document_type') or 'combined',
                actor=request.user,
            )
        except Child.DoesNotExist:
            return Response({'error': 'הילד לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.error('Cash plan registration failed: %s', exc, exc_info=True)
            return Response(
                {'error': f'שגיאה ברישום המזומן: {exc}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        plan.refresh_from_db()
        return Response(CashPlanSerializer(plan).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        """
        POST /api/v1/documents/cash-plans/{id}/cancel/  {reason?, refund_amount?}

        The months not yet begun stop (cash_plans.cancel_cash_plan). A plan
        registered with one חשבונית מס/קבלה ('upfront') is credited for them,
        or for refund_amount (0: nothing); an older plan has nothing invoiced
        for them to credit, and `message` says so. Repeating it does nothing.
        200 {...plan, credit_note_number, unused_amount, message}.
        """
        from apps.documents.cash_plans import CashPlanError, cancel_cash_plan
        from apps.documents.check_plans import CheckPlanError

        plan = self.get_object()
        refund = request.data.get('refund_amount')
        try:
            result = cancel_cash_plan(
                plan.pk, user=request.user, reason=str(request.data.get('reason') or ''),
                refund_amount=refund if refund not in (None, '') else None,
            )
        except (CashPlanError, CheckPlanError, ArithmeticError) as exc:
            return Response({'error': str(exc) or 'סכום ההחזר אינו תקין'}, status=status.HTTP_400_BAD_REQUEST)
        if not result.get('already_cancelled'):
            # The owner, 30.9.2026: the money stopped, so the status is worked out now, not next morning.
            from apps.customers.child_status import recheck_after_money_stopped

            recheck_after_money_stopped(plan.child, reason='תוכנית המזומן בוטלה', changed_by=request.user)
        plan = self.get_queryset().get(pk=plan.pk)
        return Response({
            **CashPlanSerializer(plan).data,
            'credit_note_number': result['credit_note'].document_number if result['credit_note'] else None,
            'unused_amount': str(result['unused_amount']),
            'message': result['message'],
        })


class SettlementViewSet(viewsets.GenericViewSet):
    """
    Receipts against invoices (apps/documents/settlement.py).

    POST /api/v1/documents/settlements/{id}/void/  {reason}

    Managers only. A settlement is never deleted: voiding keeps the row, with
    who voided it and when, and opens the invoice again by its amount.
    """

    permission_classes = [IsAuthenticated, IsManager]

    def get_queryset(self):
        from apps.documents.models import DocumentSettlement

        return DocumentSettlement.objects.select_related('payer', 'invoice')

    @action(detail=True, methods=['post'], url_path='void')
    def void(self, request, pk=None):
        from apps.documents.settlement import SettlementVoided, balance_of, void_settlement

        row = self.get_object()
        reason = str(request.data.get('reason') or '').strip()
        if not reason:
            return Response({'error': 'יש לציין למה הסגירה מבוטלת'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            row = void_settlement(row.pk, user=request.user, reason=reason)
        except SettlementVoided as exc:
            return Response(
                {'error': f'הסגירה כבר בוטלה ({timezone.localtime(exc.at):%d/%m/%Y %H:%M})'},
                status=status.HTTP_409_CONFLICT,
            )
        balance = balance_of(row.invoice) if row.invoice_id else None
        return Response({
            'id': str(row.pk),
            'payer_number': row.payer.document_number,
            'invoice_number': row.invoice_number,
            'amount': str(row.amount),
            'voided_at': row.voided_at,
            'invoice_balance': balance.as_dict() if balance is not None else None,
        })
