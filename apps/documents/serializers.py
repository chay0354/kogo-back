from decimal import Decimal

from rest_framework import serializers
from apps.documents.models import CashPlan, CashPlanMonth, FormalDocument, DocumentLineItem, DocumentPayment, CheckPlan, CheckItem


class DocumentLineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentLineItem
        fields = ['id', 'sku', 'description', 'quantity', 'unit_price', 'line_total']
        read_only_fields = ['id', 'line_total']


class DocumentPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentPayment
        fields = [
            'id', 'payment_method', 'amount', 'reference', 'notes',
            'check_date', 'check_bank', 'check_branch', 'check_account', 'check_crossed',
            'card_last_four', 'card_expiry', 'card_installments', 'card_brand', 'paid_on',
        ]
        read_only_fields = ['id']


def _allocation_required(obj) -> bool:
    """A tax invoice above the threshold to a business customer — never a credit note or a family."""
    from apps.documents.invoice_document import document_needs_allocation

    return document_needs_allocation(
        obj.document_type, obj.subtotal - obj.discount_amount, to_business=obj.client_type == 'business',
    )


class FormalDocumentSerializer(serializers.ModelSerializer):
    line_items = DocumentLineItemSerializer(many=True, read_only=True)
    payments = DocumentPaymentSerializer(many=True, read_only=True)
    document_type_display = serializers.CharField(source='get_document_type_display', read_only=True)

    business_name = serializers.CharField(source='business.name', read_only=True, default='')
    business_category_name = serializers.CharField(source='business_category.name', read_only=True, default='')
    allocation_required = serializers.SerializerMethodField()
    # Receipts against invoices (settlement.py): an invoice's balance, what
    # paid it and what a receipt paid. Detail only — never on the list.
    balance = serializers.SerializerMethodField()
    settled_by = serializers.SerializerMethodField()
    settles = serializers.SerializerMethodField()

    class Meta:
        model = FormalDocument
        fields = [
            'id', 'document_number', 'document_type', 'document_type_display', 'draft_target_type',
            'client_type', 'child', 'business_customer',
            'business', 'business_name', 'business_category', 'business_category_name',
            'document_date', 'due_date', 'description', 'currency',
            'prices_include_vat', 'payment_terms',
            'vat_exempt', 'vat_percent',
            'subtotal', 'discount_amount', 'discount_percent', 'vat_amount', 'total_amount',
            'customer_notes', 'internal_notes',
            'linked_document', 'linked_document_number', 'linked_document_date', 'credit_reason',
            'customer_ack_at', 'customer_ack_note', 'withholding_amount',
            'tranzila_doc_id', 'pdf_url', 'tranzila_issued',
            'allocation_number', 'allocation_required', 'allocation_entered_at',
            'branch', 'created_at', 'updated_at', 'issued_at',
            'line_items', 'payments',
            'balance', 'settled_by', 'settles',
        ]
        read_only_fields = [
            'id', 'document_number', 'created_at', 'updated_at', 'issued_at', 'customer_ack_at', 'customer_ack_note',
        ]

    def get_allocation_required(self, obj):
        return _allocation_required(obj)

    def _settlements(self, obj) -> dict:
        cache = getattr(self, '_settlement_cache', None)
        if cache is None:
            cache = self._settlement_cache = {}
        if obj.pk not in cache:
            from apps.documents.settlement import document_settlements

            request = self.context.get('request')
            cache[obj.pk] = document_settlements(obj, user=getattr(request, 'user', None))
        return cache[obj.pk]

    def get_balance(self, obj):
        return self._settlements(obj)['balance']

    def get_settled_by(self, obj):
        return self._settlements(obj)['settled_by']

    def get_settles(self, obj):
        return self._settlements(obj)['settles']



class FormalDocumentListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list/dropdown views."""
    document_type_display = serializers.CharField(source='get_document_type_display', read_only=True)
    customer_name = serializers.SerializerMethodField()

    business_name = serializers.CharField(source='business.name', read_only=True, default='')
    business_category_name = serializers.CharField(source='business_category.name', read_only=True, default='')
    allocation_required = serializers.SerializerMethodField()

    class Meta:
        model = FormalDocument
        fields = [
            'id', 'document_number', 'document_type', 'document_type_display',
            'document_date', 'total_amount', 'currency', 'tranzila_issued', 'pdf_url',
            'customer_name', 'tranzila_doc_id',
            'business_name', 'business_category_name',
            'allocation_number', 'allocation_required',
        ]

    def get_allocation_required(self, obj):
        return _allocation_required(obj)

    def get_customer_name(self, obj):
        if obj.child_id:
            return obj.child.full_name
        if obj.business_customer_id:
            return obj.business_customer.full_name
        return ''


# ── Write serializers ────────────────────────────────────────────────────────

class LineItemInputSerializer(serializers.Serializer):
    sku = serializers.CharField(required=False, allow_blank=True, default='')
    description = serializers.CharField(required=False, allow_blank=True, default='')
    quantity = serializers.DecimalField(
        max_digits=10, decimal_places=2, default=1, min_value=Decimal('0.01'),
        error_messages={'min_value': 'כמות חייבת להיות גדולה מאפס'},
    )
    price = serializers.DecimalField(
        max_digits=12, decimal_places=2, default=0, min_value=Decimal('0'),
        error_messages={'min_value': 'מחיר לא יכול להיות שלילי — הנחה נרשמת בשדה ההנחה, החזר בחשבונית זיכוי'},
    )


# How a payment is named: the dialog's Hebrew labels, or the stored keys.
PAYMENT_METHOD_INPUTS = ['מזומן', "צ'ק", 'אשראי', 'העברה בנקאית', 'cash', 'check', 'credit_card', 'bank_transfer']


class InvoicePaymentInputSerializer(serializers.Serializer):
    """
    One way a חשבונית מס/קבלה was paid, for the amount paid that way (G).

    The rows of a document add up to its total exactly — the service checks
    that, since the total is worked out there. A check names itself (הוראה
    5(ב)): number, bank, branch, account and due date; a card its last four
    digits, brand and installments; a transfer its reference and value date.
    """
    method = serializers.ChoiceField(choices=PAYMENT_METHOD_INPUTS)
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal('0.01'),
        error_messages={'min_value': 'סכום של אמצעי תשלום חייב להיות גדול מאפס'},
    )
    check_number = serializers.CharField(required=False, allow_blank=True, default='', max_length=50)
    check_bank = serializers.CharField(required=False, allow_blank=True, default='', max_length=100)
    check_branch = serializers.CharField(required=False, allow_blank=True, default='', max_length=50)
    check_account = serializers.CharField(required=False, allow_blank=True, default='', max_length=50)
    check_date = serializers.DateField(required=False, allow_null=True, default=None)
    check_crossed = serializers.BooleanField(required=False, default=False)
    card_last_four = serializers.RegexField(
        r'^[0-9]{0,4}$', required=False, allow_blank=True, default='',
        error_messages={'invalid': '4 הספרות האחרונות של הכרטיס — ספרות בלבד'},
    )
    card_brand = serializers.CharField(required=False, allow_blank=True, default='', max_length=30)
    installments = serializers.IntegerField(required=False, default=1, min_value=1, max_value=99)
    reference = serializers.CharField(required=False, allow_blank=True, default='', max_length=200)
    paid_on = serializers.DateField(required=False, allow_null=True, default=None)
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class InvoiceDetailsInputSerializer(serializers.Serializer):
    document_date = serializers.DateField()
    due_date = serializers.DateField(required=False, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    # Shekels only (owner decision D6): no rate is kept for a foreign-currency
    # document, and the uniform file records shekels.
    currency = serializers.ChoiceField(
        choices=['ILS'], default='ILS',
        error_messages={'invalid_choice': 'מסמכים מופקים בשקלים בלבד'},
    )
    prices_include_vat = serializers.BooleanField(default=False)
    line_items = LineItemInputSerializer(many=True)
    discount_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, default=0, min_value=Decimal('0'),
        error_messages={'min_value': 'הנחה לא יכולה להיות שלילית'},
    )
    discount_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, default=0, min_value=Decimal('0'), max_value=Decimal('100'),
        error_messages={'min_value': 'אחוז הנחה בין 0 ל־100', 'max_value': 'אחוז הנחה בין 0 ל־100'},
    )
    vat_exempt = serializers.BooleanField(default=False)
    # "עגל סכום" is gone (D6): a total rounded to the shekel no longer matched
    # its net and VAT. Still accepted from an older screen, and ignored.
    round_total = serializers.BooleanField(default=False)
    payment_terms = serializers.CharField(required=False, allow_blank=True, default='')
    customer_notes = serializers.CharField(required=False, allow_blank=True, default='')
    internal_notes = serializers.CharField(required=False, allow_blank=True, default='')
    # A חשבונית מס/קבלה's payments, one row per way it was paid, adding up to
    # its total (G). The older payment_methods — names only, no amounts — is
    # still read when it names a single method: that one method paid it all.
    payments = InvoicePaymentInputSerializer(many=True, required=False)
    # ניכוי במקור the customer withheld: the payments and it come to the total.
    withholding_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=0, min_value=Decimal('0'),
    )
    payment_methods = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    # מספר הקצאה, when the office already has it from the Tax Authority's
    # portal: printed on the original from the start (B). Nine digits.
    allocation_number = serializers.CharField(required=False, allow_blank=True, default='', max_length=20)
    # A combined document paid by check: the check is crossed "לא סחיר", in the
    # customer's name (הוראה 18ב(ד)(2)). A receipt says it per check, in
    # receipt_details.checks[].check_crossed.
    check_crossed = serializers.BooleanField(required=False, default=False)

    def validate_allocation_number(self, value):
        digits = ''.join(ch for ch in (value or '') if ch.isdigit())
        if (value or '').strip() and len(digits) != 9:
            raise serializers.ValidationError('מספר הקצאה הוא 9 ספרות')
        return digits


class ReceiptDetailsInputSerializer(serializers.Serializer):
    payment_method = serializers.CharField()
    linked_invoice_id = serializers.CharField(required=False, allow_blank=True, default='')
    cash_amount = serializers.DecimalField(max_digits=12, decimal_places=2, default=0, min_value=Decimal('0'))
    cash_notes = serializers.CharField(required=False, allow_blank=True, default='')
    checks = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    withholding = serializers.DecimalField(max_digits=12, decimal_places=2, default=0, min_value=Decimal('0'))
    check_notes = serializers.CharField(required=False, allow_blank=True, default='')
    card_last_four = serializers.CharField(required=False, allow_blank=True, default='')
    card_expiry = serializers.CharField(required=False, allow_blank=True, default='')
    card_amount = serializers.DecimalField(max_digits=12, decimal_places=2, default=0, min_value=Decimal('0'))
    card_brand = serializers.CharField(required=False, allow_blank=True, default='', max_length=30)
    card_installments = serializers.IntegerField(default=1)
    card_notes = serializers.CharField(required=False, allow_blank=True, default='')
    bank_date = serializers.DateField(required=False, allow_null=True)
    bank_reference = serializers.CharField(required=False, allow_blank=True, default='')
    bank_amount = serializers.DecimalField(max_digits=12, decimal_places=2, default=0, min_value=Decimal('0'))
    bank_notes = serializers.CharField(required=False, allow_blank=True, default='')
    # "חשבונית מס לכל צ'ק": the receipt's checks become a check plan, and each
    # check's tax invoice is issued on (or after) its date (check_plans.py, D2).
    invoice_per_check = serializers.BooleanField(required=False, default=False)


class SettlementInputSerializer(serializers.Serializer):
    """One invoice a receipt (or an invoice-receipt) pays, and how much of it (settlement.py)."""
    invoice_id = serializers.UUIDField(error_messages={'invalid': 'מזהה חשבונית לא תקין'})
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal('0.01'),
        error_messages={'min_value': 'הסכום שנסגר בכל חשבונית חייב להיות גדול מאפס'},
    )


class CreditInvoiceInputSerializer(serializers.Serializer):
    document_date = serializers.DateField()
    # A credit note names the document it credits (its number and date): the
    # dialog always asked for it, and now the API does too. The number may be
    # one kogo never issued (the previous software's), so it is not looked up
    # here; its date is found when kogo has the document, or given with it.
    linked_invoice_id = serializers.CharField(
        max_length=30,
        error_messages={
            'required': 'חשבונית זיכוי חייבת לציין את מספר המסמך המקורי',
            'blank': 'חשבונית זיכוי חייבת לציין את מספר המסמך המקורי',
        },
    )
    linked_document_date = serializers.DateField(required=False, allow_null=True)
    credit_reason = serializers.CharField()
    credit_amount_before_vat = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal('0.01'),
        error_messages={'min_value': 'סכום הזיכוי חייב להיות גדול מאפס'},
    )
    vat_exempt = serializers.BooleanField(default=False)
    customer_notes = serializers.CharField(required=False, allow_blank=True, default='')
    internal_notes = serializers.CharField(required=False, allow_blank=True, default='')


class CreateDocumentSerializer(serializers.Serializer):
    """Top-level create payload for all document types."""
    document_type = serializers.ChoiceField(choices=[
        'tax_invoice', 'receipt', 'combined', 'transaction_invoice', 'credit_invoice', 'draft'
    ])
    # Income tagging; defaults to the business customer's own when omitted.
    business_id = serializers.UUIDField(required=False, allow_null=True)
    business_category_id = serializers.UUIDField(required=False, allow_null=True)
    # Only for drafts: what the document becomes when approved.
    draft_target_type = serializers.ChoiceField(
        choices=['tax_invoice', 'transaction_invoice'], required=False, allow_blank=True,
    )
    client_type = serializers.ChoiceField(choices=['business', 'existing'])
    child_id = serializers.UUIDField(required=False, allow_null=True)
    business_customer_id = serializers.UUIDField(required=False, allow_null=True)
    branch_id = serializers.UUIDField(required=False, allow_null=True)
    document_date = serializers.DateField(required=False)
    # A combined document (חשבונית מס/קבלה) sends only its payment methods'
    # names, no check lines: this says the check it was paid with is crossed
    # "לא סחיר" in the customer's name (הוראה 18ב(ד)(2)), for every check row
    # it creates. Absent or false → the signed original goes on paper.
    check_crossed = serializers.BooleanField(required=False, default=False)

    invoice_details = InvoiceDetailsInputSerializer(required=False)
    receipt_details = ReceiptDetailsInputSerializer(required=False)
    credit_invoice_details = CreditInvoiceInputSerializer(required=False)
    # The invoices a receipt pays (tax invoices) or an invoice-receipt closes
    # (transaction invoices), and how much of each (settlement.py, C).
    settlements = SettlementInputSerializer(many=True, required=False, default=list)

    # Which section each type is built from. Optional above because a receipt
    # carries no invoice section and an invoice carries no receipt one.
    _SECTIONS_BY_TYPE = {
        'tax_invoice': ('invoice_details',),
        'transaction_invoice': ('invoice_details',),
        'draft': ('invoice_details',),
        # A combined document is built from the invoice section alone; how it
        # was paid comes from invoice_details.payment_methods.
        'combined': ('invoice_details',),
        'receipt': ('receipt_details',),
        'credit_invoice': ('credit_invoice_details',),
    }
    _SECTION_LABELS = {
        'invoice_details': 'פרטי החשבונית',
        'receipt_details': 'פרטי הקבלה',
        'credit_invoice_details': 'פרטי הזיכוי',
    }

    def validate(self, attrs):
        """Say which section is missing, instead of failing deep inside the service."""
        missing = {
            section: [f'{self._SECTION_LABELS[section]} חסרים למסמך מסוג זה']
            for section in self._SECTIONS_BY_TYPE.get(attrs.get('document_type'), ())
            if section not in attrs
        }
        if missing:
            raise serializers.ValidationError(missing)
        # Every document names its customer — a tax invoice the buyer (תקנה
        # 9א), a receipt the payer (הוראה 5(א)(4)), a credit note whom it credits.
        if attrs.get('client_type') == 'existing' and not attrs.get('child_id'):
            raise serializers.ValidationError({'child_id': ['יש לבחור את הלקוח שהמסמך מופק לו']})
        if attrs.get('client_type') == 'business' and not attrs.get('business_customer_id'):
            raise serializers.ValidationError({'business_customer_id': ['יש לבחור את הלקוח העסקי שהמסמך מופק לו']})
        details = attrs.get('invoice_details') or {}
        if details.get('allocation_number') and attrs.get('document_type') not in ('tax_invoice', 'combined'):
            raise serializers.ValidationError({'invoice_details': {'allocation_number': [
                'מספר הקצאה נרשם על חשבונית מס או חשבונית מס/קבלה בלבד',
            ]}})
        if attrs.get('document_type') == 'credit_invoice':
            # סעיף 9(ה)(4): the original's number AND its date. kogo finds the
            # date of a document it issued; a number it never issued (the
            # previous software's) has to come with its date.
            from apps.documents.service import original_document_date

            credit = attrs['credit_invoice_details']
            number = (credit.get('linked_invoice_id') or '').strip()
            if number and not credit.get('linked_document_date') and original_document_date(number) is None:
                raise serializers.ValidationError({'credit_invoice_details': {'linked_document_date': [
                    f'{number} אינו מסמך שהופק בקוגו — יש לציין את תאריך המסמך המקורי',
                ]}})
        if attrs.get('settlements') and attrs.get('document_type') not in ('receipt', 'combined'):
            raise serializers.ValidationError({'settlements': [
                'חשבונית נסגרת בקבלה או בחשבונית מס/קבלה בלבד',
            ]})
        receipt = attrs.get('receipt_details') or {}
        if attrs.get('document_type') == 'receipt' and receipt.get('invoice_per_check'):
            if attrs.get('client_type') != 'existing':
                raise serializers.ValidationError({'receipt_details': {'invoice_per_check': [
                    "חשבונית לכל צ'ק מופקת ללקוח פרטי (ילד) בלבד. ללקוח עסקי מפיקים חשבונית מס לכל צ'ק מהטופס.",
                ]}})
            if receipt.get('payment_method') != "צ'ק":
                raise serializers.ValidationError({'receipt_details': {'invoice_per_check': [
                    "חשבונית לכל צ'ק — רק לקבלה על צ'קים",
                ]}})
            if attrs.get('settlements') or (receipt.get('linked_invoice_id') or '').strip():
                raise serializers.ValidationError({'receipt_details': {'invoice_per_check': [
                    "קבלה שסוגרת חשבונית קיימת לא מפיקה חשבונית לכל צ'ק — החשבונית כבר הונפקה.",
                ]}})
        if attrs.get('document_type') == 'combined':
            details = attrs['invoice_details']
            if not details.get('payments'):
                methods = details.get('payment_methods') or []
                if not methods:
                    raise serializers.ValidationError({'invoice_details': [
                        'חשבונית מס/קבלה צריכה לפחות אמצעי תשלום אחד וסכומו',
                    ]})
                if len(methods) > 1:
                    raise serializers.ValidationError({'invoice_details': [
                        'חשבונית מס/קבלה בכמה אמצעי תשלום צריכה את הסכום של כל אחד מהם',
                    ]})
        return attrs


class CheckItemSerializer(serializers.ModelSerializer):
    tax_invoice_number = serializers.CharField(source='tax_invoice.document_number', read_only=True, allow_null=True)
    tax_invoice_date = serializers.DateField(source='tax_invoice.document_date', read_only=True, allow_null=True)
    # A check that came back (D2): when, the credit note of its invoice, the check that replaced it.
    credit_note_number = serializers.CharField(source='credit_note.document_number', read_only=True, allow_null=True)
    replaced_by_plan = serializers.UUIDField(source='replaced_by.plan_id', read_only=True, allow_null=True)

    class Meta:
        model = CheckItem
        fields = [
            'id', 'due_date', 'amount', 'bank', 'bank_branch', 'account_number',
            'check_number', 'status', 'tax_invoice', 'tax_invoice_number', 'tax_invoice_date', 'invoiced_at',
            'bounced_at', 'credit_note', 'credit_note_number', 'replaced_by', 'replaced_by_plan',
        ]
        read_only_fields = fields


class CheckPlanSerializer(serializers.ModelSerializer):
    child_name = serializers.CharField(source='child.full_name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    lesson_name = serializers.SerializerMethodField()
    receipt_number = serializers.CharField(source='receipt.document_number', read_only=True, allow_null=True)
    items = CheckItemSerializer(many=True, read_only=True)
    total_amount = serializers.SerializerMethodField()
    next_due_date = serializers.SerializerMethodField()

    class Meta:
        model = CheckPlan
        fields = [
            'id', 'child', 'child_name', 'lesson', 'lesson_name', 'description',
            'status', 'receipt', 'receipt_number', 'branch', 'branch_name',
            'items', 'total_amount', 'next_due_date', 'created_at',
            'cancelled_at', 'cancelled_by_name',
        ]
        read_only_fields = fields

    cancelled_by_name = serializers.SerializerMethodField()

    def get_cancelled_by_name(self, obj):
        user = obj.cancelled_by
        return (user.get_full_name() or user.email or user.username) if user is not None else ''

    def get_lesson_name(self, obj):
        if obj.lesson_id and obj.lesson:
            course = getattr(obj.lesson, 'course', None)
            return course.name if course else str(obj.lesson)
        return None

    def get_total_amount(self, obj):
        return sum((item.amount for item in obj.items.all()), start=0)

    def get_next_due_date(self, obj):
        pending = [item.due_date for item in obj.items.all() if item.status == 'pending']
        return min(pending) if pending else None

    def to_representation(self, obj):
        # Business → city → course type → age → instructor, as on every ledger
        # row, so the invoices page's filter bar narrows plans like charges.
        # branch_id is the plan's own branch: its lesson's, or the family's when
        # it has no lesson (register_check_plan).
        from apps.core.ledger_dimensions import row_dimensions
        return {
            **super().to_representation(obj),
            **row_dimensions(lesson=obj.lesson, branch=obj.branch),
            'branch_id': str(obj.branch_id) if obj.branch_id else None,
        }


class CreateCheckPlanSerializer(serializers.Serializer):
    child_id = serializers.UUIDField()
    lesson_id = serializers.UUIDField(required=False, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    checks = serializers.ListField(child=serializers.DictField(), allow_empty=False)


# ── Cash plans ───────────────────────────────────────────────────────────────

class CashPlanMonthSerializer(serializers.ModelSerializer):
    document_number = serializers.CharField(source='document.document_number', read_only=True, default='')
    document_type = serializers.CharField(source='document.document_type', read_only=True, default='')

    class Meta:
        model = CashPlanMonth
        fields = ['id', 'due_date', 'amount', 'status', 'invoiced_at', 'document_number', 'document_type']


class CashPlanSerializer(serializers.ModelSerializer):
    child_name = serializers.CharField(source='child.full_name', read_only=True, default='')
    course_name = serializers.CharField(source='lesson.course.name', read_only=True, default='')
    branch_name = serializers.CharField(source='branch.name', read_only=True, default='')
    receipt_number = serializers.CharField(source='receipt.document_number', read_only=True, default='')
    # 'upfront' (D1, from 30.9.2026): `receipt` is the one חשבונית מס/קבלה for
    # the whole sum; null: the older design, a receipt and a document a month.
    receipt_document_type = serializers.CharField(source='receipt.document_type', read_only=True, default='')
    months = CashPlanMonthSerializer(many=True, read_only=True)
    months_paid = serializers.SerializerMethodField()
    months_total = serializers.SerializerMethodField()
    unused_amount = serializers.SerializerMethodField()
    cancelled_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CashPlan
        fields = [
            'id', 'child', 'child_name', 'lesson', 'course_name', 'branch', 'branch_name',
            'description', 'status', 'total_amount', 'monthly_amount',
            'monthly_document_type', 'receipt', 'receipt_number', 'receipt_document_type', 'mode',
            'months', 'months_paid', 'months_total', 'unused_amount', 'created_at',
            'cancelled_at', 'cancelled_by_name',
        ]

    def get_unused_amount(self, obj):
        from apps.documents.cash_plans import unused_amount

        return str(unused_amount(obj))

    def get_cancelled_by_name(self, obj):
        user = obj.cancelled_by
        return (user.get_full_name() or user.email or user.username) if user is not None else ''

    def get_months_paid(self, obj):
        return sum(1 for m in obj.months.all() if m.status == 'invoiced')

    def get_months_total(self, obj):
        return obj.months.count()


class CreateCashPlanSerializer(serializers.Serializer):
    child_id = serializers.UUIDField()
    lesson_id = serializers.UUIDField(required=False, allow_null=True)
    total_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    monthly_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    start_month = serializers.DateField(required=False, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    monthly_document_type = serializers.ChoiceField(
        choices=[c[0] for c in CashPlan.MONTHLY_DOCUMENT_CHOICES],
        required=False,
        default='combined',
    )
