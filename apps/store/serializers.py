"""
Store Serializers - API Serialization for Store Models
"""
import uuid

from django.db import transaction, IntegrityError
from rest_framework import serializers, status
from rest_framework.exceptions import APIException
from apps.store.models import StoreProduct, StoreProductSize, StoreInvoice, StoreSale, InventoryAdjustment
from apps.core.models import Branch
from apps.customers.models import Child


class StockChangedSinceOpened(APIException):
    """The stock the office changed moved under it (a sale, an update) since the form was opened."""

    status_code = status.HTTP_409_CONFLICT
    default_code = 'stock_changed'


class StoreProductSizeSerializer(serializers.ModelSerializer):
    """Serializer for per-size stock rows."""

    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    # An empty size is a real row: a product without sizes keeping its stock
    # per location (see _normalize_size_stocks). The model field has no
    # blank=True, so the nested serializer refused "" before the normalizer
    # that accepts it ever ran.
    size = serializers.CharField(max_length=20, allow_blank=True, required=False, default='')

    class Meta:
        model = StoreProductSize
        fields = ['id', 'size', 'stock_quantity', 'sort_order', 'branch', 'branch_name']
        read_only_fields = ['id', 'branch_name']

    def to_internal_value(self, data):
        # Nested list validation runs before StoreProductSerializer.validate_size_stocks.
        # Empty select sends ""; FK/UUID fields reject that — coerce before field validation.
        # Only touch `branch` when the client sent the key (omit = leave default handling).
        # HTML/JSON sometimes sends "" for integers (e.g. sort_order) → DRF 400 otherwise.
        if isinstance(data, dict):
            data = {**data}
            if 'branch' in data and data.get('branch') in ('', 'delivery', None):
                data['branch'] = None
            if 'sort_order' in data:
                so = data.get('sort_order')
                if so in ('', None):
                    data['sort_order'] = 0
                elif isinstance(so, str):
                    try:
                        data['sort_order'] = max(0, int(so.strip() or '0'))
                    except (TypeError, ValueError):
                        data['sort_order'] = 0
            if 'stock_quantity' in data:
                sq = data.get('stock_quantity')
                if sq in ('', None):
                    data['stock_quantity'] = 0
                elif isinstance(sq, str):
                    try:
                        data['stock_quantity'] = max(0, int(sq.strip() or '0'))
                    except (TypeError, ValueError):
                        data['stock_quantity'] = 0
        return super().to_internal_value(data)


def _coerce_branch_pk_string(branch_raw):
    """
    Nested `StoreProductSizeSerializer` turns `branch` into a `Branch` model instance.
    `str(Branch)` is the display name, not the UUID — never use bare str() for FK ids.
    """
    if branch_raw in (None, '', serializers.empty, 'delivery'):
        return None
    if isinstance(branch_raw, Branch):
        pk = branch_raw.pk
        return str(pk) if pk is not None else None
    if isinstance(branch_raw, uuid.UUID):
        return str(branch_raw)
    sid = str(branch_raw).strip()
    return sid if sid else None


def _normalize_size_stocks(value):
    """
    Coerce incoming size_stocks into a clean, deduplicated list.

    Rules:
    - Each entry must be {size: str, stock_quantity: int >= 0, branch?: uuid|null}.
    - Sizes are stripped. An empty size is kept and means "the product itself at
      this location" — that is how a product without sizes holds stock per
      branch instead of one number for everywhere.
    - Same (size, branch) repeated → last one wins (branch None = משלוח).
    - sort_order is filled in from the input order if not provided.
    - branch: omit, null, '', or 'delivery' → None; otherwise must be a valid Branch id.
    """
    if value in (None, ''):
        return []
    if not isinstance(value, list):
        raise serializers.ValidationError("size_stocks חייב להיות רשימה")

    cleaned: dict[tuple, dict] = {}
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise serializers.ValidationError(
                "כל פריט מידה חייב להיות אובייקט עם size ו-stock_quantity"
            )
        size_label = str(entry.get('size', '')).strip()
        try:
            qty = int(entry.get('stock_quantity', 0) or 0)
        except (TypeError, ValueError):
            raise serializers.ValidationError("stock_quantity לכל מידה חייב להיות מספר שלם")
        if qty < 0:
            raise serializers.ValidationError("stock_quantity לכל מידה לא יכול להיות שלילי")

        try:
            sort_order = int(entry.get('sort_order', index))
        except (TypeError, ValueError):
            sort_order = index

        branch_raw = entry.get('branch', serializers.empty)
        branch_id = None
        sid = _coerce_branch_pk_string(branch_raw)
        if sid:
            if not Branch.objects.filter(id=sid).exists():
                raise serializers.ValidationError(
                    f'סניף לא תקף למידה "{size_label}"' if size_label
                    else 'סניף לא תקף בשורת המלאי'
                )
            branch_id = sid

        row_key = (size_label, branch_id)
        cleaned[row_key] = {
            'size': size_label,
            'stock_quantity': qty,
            'sort_order': sort_order,
            'branch': branch_id,
        }

    return sorted(cleaned.values(), key=lambda e: (e['sort_order'], e['size'], e['branch'] or ''))


def _row_key(size, branch) -> tuple:
    branch_id = _coerce_branch_pk_string(branch)
    return ((size or '').strip(), branch_id)


def _row_label(key: tuple) -> str:
    size, branch_id = key
    place = 'משלוח'
    if branch_id:
        place = Branch.objects.filter(pk=branch_id).values_list('name', flat=True).first() or 'סניף'
    return f'{size} · {place}' if size else place


def _record_recount(product, row, delta: int, label: str, user) -> None:
    if not delta:
        return
    InventoryAdjustment.objects.create(
        product=product,
        size_stock=row if row is not None and row.pk else None,
        quantity_delta=int(delta),
        reason='recount',
        note=f'עדכון בחלון עריכת מוצר — {label}',
        adjusted_by=user,
    )


def _read_expected(expected):
    """({(size, branch): qty} or None, flat qty or None) from the form's stock_expected."""
    if not isinstance(expected, dict):
        return None, None
    rows = None
    if isinstance(expected.get('rows'), list):
        rows = {}
        for entry in expected['rows']:
            if not isinstance(entry, dict):
                continue
            try:
                qty = int(entry.get('stock_quantity') or 0)
            except (TypeError, ValueError):
                continue
            key = _row_key(entry.get('size'), entry.get('branch'))
            rows[key] = rows.get(key, 0) + qty
    flat = expected.get('stock_quantity')
    try:
        flat = int(flat) if flat is not None else None
    except (TypeError, ValueError):
        flat = None
    return rows, flat


class StoreProductSerializer(serializers.ModelSerializer):
    """Serializer for StoreProduct model."""

    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    is_low_stock = serializers.BooleanField(read_only=True)
    profit_margin = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    size_stocks = StoreProductSizeSerializer(many=True, required=False)
    # What the edit form loaded, so the save changes only what the office
    # changed: {"rows": [{size, branch, stock_quantity}], "stock_quantity": n}.
    stock_expected = serializers.JSONField(write_only=True, required=False)
    # B2C sync often stores site-relative paths (/images/...) — not strict URLs.
    image_url = serializers.CharField(required=False, allow_blank=True, max_length=500)
    # The model has a default but no blank=True, so DRF rejected ''. Both product
    # dialogs always send the field, and the add dialog's placeholder is the
    # default itself — clearing the box read as "use the default" and got
    # "This field may not be blank" instead. Empty now means the default.
    category = serializers.CharField(required=False, allow_blank=True, max_length=50)

    class Meta:
        model = StoreProduct
        fields = [
            'id', 'name', 'category', 'size',
            'cost_price', 'sale_price', 'delivery_price',
            'branch', 'branch_name',
            'stock_quantity', 'min_stock_alert', 'is_low_stock',
            'image_url', 'notes', 'is_active',
            'website_legacy_id', 'branch_only',
            'profit_margin',
            'size_stocks', 'stock_expected',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_size_stocks(self, value):
        return _normalize_size_stocks(value)

    def validate(self, data):
        """
        Sale price must beat cost price — but only for prices being set.

        Falling back to the stored values unconditionally meant a product that
        already had sale <= cost could never be saved again: renaming it, or
        fixing a note, re-ran the check against its own stored prices and was
        refused. The rule now applies to an actual price change, so such a
        product can still be edited (and repaired), while no save is allowed to
        put the pair into that state.
        """
        stored_sale = getattr(self.instance, 'sale_price', None)
        stored_cost = getattr(self.instance, 'cost_price', None)
        sale_price = data.get('sale_price', stored_sale)
        cost_price = data.get('cost_price', stored_cost)

        prices_changed = (
            self.instance is None
            or ('sale_price' in data and data['sale_price'] != stored_sale)
            or ('cost_price' in data and data['cost_price'] != stored_cost)
        )

        if prices_changed and sale_price and cost_price and sale_price <= cost_price:
            raise serializers.ValidationError(
                "מחיר מכירה חייב להיות גבוה ממחיר עלות (Sale price must be higher than cost price)"
            )

        return data

    def validate_category(self, value):
        """An empty category means the model default, not a rejected save."""
        cleaned = (value or '').strip()
        return cleaned or StoreProduct._meta.get_field('category').default

    def validate_image_url(self, value):
        if value is None:
            return ''
        return str(value).strip()

    def _sync_size_stocks(self, product, size_stocks, *, expected_rows=None, record=True):
        """
        Bring the product's stock rows to the list the office saved, keyed by
        (size, location): rows that are there are updated in place, new ones
        added, missing ones removed. Keeps the derived `size` (CSV),
        `stock_quantity` (total) and `branch` (first row's) in step.

        The rows keep their ids. The till's cart, a pending website order and
        the write-off history all point at a row by id; deleting and
        re-creating every row on each save broke all three.

        With `expected_rows` — {(size, branch): qty} as the form loaded them —
        a row the office did not change keeps what it holds now (a sale since
        the form opened is not undone), and a row the office did change must
        still hold what the form loaded, or the save is refused. Every change
        in quantity is written to the stock history as a recount.
        """
        rows = list(StoreProductSize.objects.select_for_update().filter(product=product).order_by('sort_order', 'size'))
        current: dict[tuple, list] = {}
        for row in rows:
            current.setdefault(_row_key(row.size, row.branch_id), []).append(row)

        def held(key) -> int:
            return sum(int(r.stock_quantity or 0) for r in current.get(key, []))

        wanted = {_row_key(e['size'], e.get('branch')): e for e in size_stocks}
        conflicts: list[str] = []

        # Rows the office removed.
        removed = []
        for key, found in current.items():
            if key in wanted:
                continue
            if expected_rows is not None and key not in expected_rows:
                # Added by someone else after the form was opened: not the office's to remove.
                continue
            if expected_rows is not None and held(key) != expected_rows[key]:
                conflicts.append(_row_label(key))
                continue
            removed.append((key, found))

        plan: list[tuple] = []  # (key, entry, qty)
        for key, entry in wanted.items():
            qty = int(entry['stock_quantity'])
            if expected_rows is not None and key in expected_rows:
                if key not in current:
                    conflicts.append(_row_label(key))
                    continue
                if qty == expected_rows[key]:
                    qty = held(key)                      # untouched: keep what is there now
                elif held(key) != expected_rows[key]:
                    conflicts.append(_row_label(key))
                    continue
            plan.append((key, entry, qty))

        if conflicts:
            raise StockChangedSinceOpened(
                'המלאי השתנה מאז שנפתח החלון (מכירה או עדכון מלאי) בשורות: '
                + ', '.join(conflicts)
                + '. סגרו את החלון, פתחו אותו מחדש ועדכנו שוב.'
            )

        try:
            for key, found in removed:
                for row in found:
                    # The history row outlives the stock row (size_stock → NULL); its note keeps the place.
                    if row.stock_quantity:
                        if record:
                            _record_recount(product, row, -int(row.stock_quantity), _row_label(key), self._user())
                    row.delete()

            for key, entry, qty in plan:
                found = current.get(key, [])
                if found:
                    row, extra = found[0], found[1:]
                    before = held(key)
                    for dup in extra:
                        # Two rows for one size and place (NULL locations are not unique in
                        # the database): the saved quantity is the one row's now.
                        dup.delete()
                    fields = []
                    if row.stock_quantity != qty:
                        row.stock_quantity = qty
                        fields.append('stock_quantity')
                    if row.sort_order != entry['sort_order']:
                        row.sort_order = entry['sort_order']
                        fields.append('sort_order')
                    if fields:
                        row.save(update_fields=[*fields, 'updated_at'])
                    if record and qty != before:
                        _record_recount(product, row, qty - before, _row_label(key), self._user())
                else:
                    row = StoreProductSize.objects.create(
                        product=product,
                        size=entry['size'],
                        stock_quantity=qty,
                        sort_order=entry['sort_order'],
                        branch_id=entry.get('branch'),
                    )
                    if record and qty:
                        _record_recount(product, row, qty, _row_label(key), self._user())

            remaining = list(StoreProductSize.objects.filter(product=product).order_by('sort_order', 'size'))
            if remaining:
                product.size = ','.join(dict.fromkeys(r.size for r in remaining if r.size))
                product.stock_quantity = sum(int(r.stock_quantity or 0) for r in remaining)
                product.branch_id = next((r.branch_id for r in remaining if r.branch_id), None)
                product.save(update_fields=['size', 'stock_quantity', 'branch', 'updated_at'])
        except IntegrityError as exc:
            raise serializers.ValidationError(
                {
                    'size_stocks': [
                        'שמירת שורות המלאי נכשלה: אותה מידה באותו מיקום מופיעה פעמיים.'
                    ]
                }
            ) from exc

    def _user(self):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        return user if getattr(user, 'is_authenticated', False) else None

    def _apply_flat_stock(self, instance, validated_data, expected):
        """
        A product without stock rows keeps one number. When the form says what
        it loaded, an unchanged number keeps what is there now, and a changed
        one needs nothing to have moved it meanwhile. The change is recorded.
        """
        if 'stock_quantity' not in validated_data or instance.size_stocks.exists():
            return None
        new = int(validated_data['stock_quantity'])
        locked = StoreProduct.objects.select_for_update().get(pk=instance.pk)
        now = int(locked.stock_quantity or 0)
        if expected is not None:
            loaded = int(expected)
            if new == loaded:
                validated_data.pop('stock_quantity')
                return None
            if now != loaded:
                raise StockChangedSinceOpened(
                    f'המלאי השתנה מאז שנפתח החלון (עכשיו {now}, כשנפתח {loaded}). '
                    'סגרו את החלון, פתחו אותו מחדש ועדכנו שוב.'
                )
        return new - now

    def create(self, validated_data):
        validated_data.pop('stock_expected', None)
        size_stocks = validated_data.pop('size_stocks', None)
        with transaction.atomic():
            product = super().create(validated_data)
            if size_stocks is not None:
                self._sync_size_stocks(product, size_stocks)
        # No explicit push: saving the product schedules the website push, and
        # the whole save sends exactly one call after COMMIT.
        return product

    def update(self, instance, validated_data):
        size_stocks = validated_data.pop('size_stocks', None)
        expected = validated_data.pop('stock_expected', None)
        expected_rows, expected_flat = _read_expected(expected)
        with transaction.atomic():
            if size_stocks and not StoreProductSize.objects.filter(product=instance).exists():
                return self._convert_to_rows(instance, validated_data, size_stocks, expected_rows, expected_flat)
            flat_delta = self._apply_flat_stock(instance, validated_data, expected_flat)
            product = super().update(instance, validated_data)
            if flat_delta:
                _record_recount(product, None, flat_delta, 'מלאי כללי', self._user())
            if size_stocks is not None:
                self._sync_size_stocks(product, size_stocks, expected_rows=expected_rows)
        return product


    def _convert_to_rows(self, instance, validated_data, size_stocks, expected_rows, expected_flat):
        """
        A product that kept one number gets its first rows (a size, or a place).

        Moving that number into rows is not a change in stock, so the history
        gets only the difference between the rows and what the number held —
        not every row as if it had just arrived. The edit form carries the old
        number over as a row of its own, so adding a size adds to the stock;
        an older form that did not, replaced it, and the difference says so.
        """
        locked = StoreProduct.objects.select_for_update().get(pk=instance.pk)
        before = int(locked.stock_quantity or 0)
        if expected_flat is not None and before != int(expected_flat):
            raise StockChangedSinceOpened(
                f'המלאי השתנה מאז שנפתח החלון (עכשיו {before}, כשנפתח {int(expected_flat)}). '
                'סגרו את החלון, פתחו אותו מחדש ועדכנו שוב.'
            )
        validated_data.pop('stock_quantity', None)
        instance.stock_quantity = before
        product = super().update(instance, validated_data)
        self._sync_size_stocks(product, size_stocks, expected_rows=expected_rows, record=False)
        product.refresh_from_db(fields=['stock_quantity', 'size', 'branch'])
        _record_recount(product, None, int(product.stock_quantity or 0) - before, 'מעבר למלאי לפי מידות ומיקומים', self._user())
        return product


class StoreSaleSerializer(serializers.ModelSerializer):
    """Serializer for StoreSale model (line items)."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    child_name = serializers.CharField(source='child.full_name', read_only=True, allow_null=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    invoice_status = serializers.CharField(source='invoice.payment_status', read_only=True)
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)
    
    class Meta:
        model = StoreSale
        fields = [
            'id', 'invoice', 'invoice_number', 'invoice_status',
            'product', 'product_name',
            'child', 'child_name',
            'quantity', 'unit_price', 'total_price', 'size',
            'payment_method', 'branch', 'branch_name',
            'sale_date', 'created_at'
        ]
        read_only_fields = ['id', 'sale_date', 'created_at']


class StoreInvoiceSerializer(serializers.ModelSerializer):
    """Serializer for StoreInvoice model."""
    
    line_items = StoreSaleSerializer(many=True, read_only=True)
    child_name = serializers.CharField(source='child.full_name', read_only=True, allow_null=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    # A payment in review (apps/store/payment_followup.py), and the numbers
    # the managers' "payment-review" action decides about. `payment_in_review`
    # is the unpaid order a number holds (no second page for the customer);
    # `payment_review_numbers` lists every undecided number of the invoice —
    # also a released one on a failed order (it may still be completed) and a
    # further one on a paid order (a second charge, or "not ours").
    payment_in_review = serializers.SerializerMethodField()
    payment_review_numbers = serializers.SerializerMethodField()

    def get_payment_in_review(self, obj) -> bool:
        from apps.store.payment_followup import holds_reported_payment

        return holds_reported_payment(obj)

    def get_payment_review_numbers(self, obj) -> list:
        from apps.store.payment_followup import open_numbers

        return [
            {'index': n.index, 'suspected': n.suspected, 'released': n.released,
             'reported_at': n.reported_at.isoformat()}
            for n in open_numbers(obj, include_suspected=True, include_released=True)
        ]

    class Meta:
        model = StoreInvoice
        fields = [
            'id', 'invoice_number',
            'child', 'child_name', 'customer_name', 'customer_phone', 'customer_email',
            'shipping_address', 'customer_notes', 'website_order_number',
            'total_amount', 'refunded_amount', 'amount_paid', 'payment_method', 'payment_status',
            'tranzila_transaction_id', 'tranzila_confirmation_code',
            'charged_with_token',
            'branch', 'branch_name',
            'issue_date', 'notes',
            'line_items',
            'created_at',
            'payment_in_review', 'payment_review_numbers',
        ]
        read_only_fields = ['id', 'invoice_number', 'issue_date', 'created_at', 'refunded_amount', 'amount_paid']


class StoreAnalyticsSerializer(serializers.Serializer):
    """Serializer for store analytics dashboard data."""
    
    # KPIs
    total_revenue = serializers.DecimalField(max_digits=10, decimal_places=2)
    net_profit = serializers.DecimalField(max_digits=10, decimal_places=2)
    total_sales_count = serializers.IntegerField()
    low_stock_count = serializers.IntegerField()
    
    # Charts data
    monthly_revenue = serializers.ListField(child=serializers.DictField())
    sales_by_product = serializers.ListField(child=serializers.DictField())
    sales_by_category = serializers.ListField(child=serializers.DictField())
    sales_by_branch = serializers.ListField(child=serializers.DictField())
    sales_by_payment_method = serializers.ListField(child=serializers.DictField())
    
    # Lists
    low_stock_products = StoreProductSerializer(many=True)
    recent_sales = StoreSaleSerializer(many=True)


class InventoryAdjustmentSerializer(serializers.ModelSerializer):
    adjusted_by_name = serializers.CharField(source='adjusted_by.get_full_name', read_only=True, allow_null=True)
    reason_display = serializers.CharField(source='get_reason_display', read_only=True)
    size_stock_label = serializers.SerializerMethodField()

    class Meta:
        model = InventoryAdjustment
        fields = [
            'id', 'product', 'size_stock', 'size_stock_label',
            'quantity_delta', 'reason', 'reason_display', 'note',
            'adjusted_by', 'adjusted_by_name', 'created_at',
        ]
        read_only_fields = ['id', 'created_at', 'adjusted_by_name', 'reason_display', 'size_stock_label']

    def get_size_stock_label(self, obj):
        if not obj.size_stock:
            return None
        branch_name = obj.size_stock.branch.name if obj.size_stock.branch else 'משלוח'
        return f"{obj.size_stock.size} · {branch_name}"


class PaymentInitiationResponseSerializer(serializers.Serializer):
    """Response serializer for payment initiation."""
    
    requires_iframe = serializers.BooleanField()
    iframe_url = serializers.URLField(required=False, allow_null=True)
    invoice_id = serializers.UUIDField(required=False, allow_null=True)
    invoice = StoreInvoiceSerializer(required=False, allow_null=True)
    success = serializers.BooleanField(required=False, allow_null=True)
    error = serializers.CharField(required=False, allow_null=True)

