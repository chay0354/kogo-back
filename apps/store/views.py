"""
Store Views - API Endpoints for Store Management
"""
import logging
from decimal import Decimal
from datetime import date, timedelta
from collections import defaultdict

from django.db import transaction as db_transaction
from django.db.models import Sum, F, Q, Count
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from apps.store.models import StoreProduct, StoreProductSize, StoreInvoice, StoreSale, InventoryAdjustment
from apps.store.serializers import (
    StoreProductSerializer, StoreInvoiceSerializer,
    StoreSaleSerializer, StoreAnalyticsSerializer,
    PaymentInitiationResponseSerializer, InventoryAdjustmentSerializer
)
from apps.core.payment_service import PaymentService
from apps.core.scoping import scope_store_products, is_scoped_partner, partner_branch_ids

logger = logging.getLogger(__name__)


class StoreProductViewSet(viewsets.ModelViewSet):
    """
    API endpoints for managing store products.
    
    Supports:
    - List products with filters
    - Create/update products
    - Stock updates
    - Soft delete (set is_active=False)
    """
    queryset = StoreProduct.objects.filter(is_active=True).prefetch_related('size_stocks__branch')
    serializer_class = StoreProductSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filter products by query parameters."""
        queryset = super().get_queryset()
        queryset = scope_store_products(queryset, self.request.user)

        # Search by name/category
        search = self.request.query_params.get('search', '')
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(category__icontains=search)
            )
        
        # Filter by branch
        branch_id = self.request.query_params.get('branch')
        if branch_id and branch_id != 'all':
            if branch_id == 'delivery':
                queryset = queryset.filter(branch__isnull=True)
            else:
                queryset = queryset.filter(branch_id=branch_id)
        
        # Filter by stock status
        stock_filter = self.request.query_params.get('stock_filter')
        if stock_filter == 'low':
            queryset = queryset.filter(stock_quantity__lte=F('min_stock_alert'))
        elif stock_filter == 'normal':
            queryset = queryset.filter(stock_quantity__gt=F('min_stock_alert'))
        
        # Sorting
        sort_by = self.request.query_params.get('sort_by', 'name')
        sort_order = self.request.query_params.get('sort_order', 'asc')
        
        order_prefix = '' if sort_order == 'asc' else '-'
        queryset = queryset.order_by(f'{order_prefix}{sort_by}')
        
        return queryset
    
    @action(detail=True, methods=['patch'])
    def update_stock(self, request, pk=None):
        """
        Update product stock quantity.

        Body: {
            "mode": "add" | "subtract" | "set",
            "quantity": number,
            "size": string | null,           # legacy: target row by size label
            "size_stock_id": uuid | null,    # preferred: exact StoreProductSize row (size + location)
        }

        When `size_stock_id` is provided and the product has per-size stock rows,
        that exact row is updated (disambiguates when UI shows size + branch).
        Else when `size` is provided, the first matching row for that size is used.
        Otherwise we fall back to mutating `stock_quantity` on the product.
        """
        product = self.get_object()
        mode = request.data.get('mode')
        try:
            quantity = int(request.data.get('quantity', 0))
        except (TypeError, ValueError):
            return Response({'error': 'quantity must be an integer'}, status=status.HTTP_400_BAD_REQUEST)

        if mode not in {'add', 'subtract', 'set'}:
            return Response(
                {'error': 'Invalid mode. Must be add, subtract, or set'},
                status=status.HTTP_400_BAD_REQUEST
            )

        size = (request.data.get('size') or '').strip()
        size_stock_id = (request.data.get('size_stock_id') or '').strip()

        if size_stock_id and not product.has_per_size_stock():
            return Response(
                {'error': 'size_stock_id is only valid for products with per-size stock rows'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with db_transaction.atomic():
            size_row = None
            if size_stock_id and product.has_per_size_stock():
                size_row = (
                    StoreProductSize.objects
                    .select_for_update()
                    .filter(product=product, pk=size_stock_id)
                    .first()
                )
                if size_row is None:
                    return Response(
                        {'error': 'Size stock row not found for this product'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            elif size and product.has_per_size_stock():
                size_row = (
                    StoreProductSize.objects
                    .select_for_update()
                    .filter(product=product, size=size)
                    .first()
                )
                if size_row is None:
                    return Response(
                        {'error': f'Size "{size}" not found for this product'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            if size_row is not None:
                if mode == 'add':
                    size_row.stock_quantity += quantity
                elif mode == 'subtract':
                    size_row.stock_quantity = max(0, size_row.stock_quantity - quantity)
                else:
                    size_row.stock_quantity = max(0, quantity)
                size_row.save(update_fields=['stock_quantity', 'updated_at'])
                product.recalculate_total_stock()
            else:
                if mode == 'add':
                    product.stock_quantity += quantity
                elif mode == 'subtract':
                    product.stock_quantity = max(0, product.stock_quantity - quantity)
                else:
                    product.stock_quantity = max(0, quantity)
                product.save(update_fields=['stock_quantity', 'updated_at'])

        logger.info(
            "Updated stock for %s: mode=%s, quantity=%s, size=%s, size_stock_id=%s, new_stock=%s",
            product.name, mode, quantity, size or '-', size_stock_id or '-', product.stock_quantity,
        )
        product.refresh_from_db()
        serializer = self.get_serializer(product)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def adjust_stock(self, request, pk=None):
        """
        Adjust stock with a documented reason (add or subtract).

        Body: {
            "quantity_delta": int,           # positive = add, negative = subtract
            "reason": "receipt"|"theft"|"damage"|"recount"|"other",
            "note": string,                  # optional
            "size_stock_id": uuid | null,    # preferred for per-size products
        }

        Creates an InventoryAdjustment audit record and updates the stock atomically.
        """
        product = self.get_object()
        try:
            quantity_delta = int(request.data.get('quantity_delta', 0))
        except (TypeError, ValueError):
            return Response({'error': 'quantity_delta must be an integer'}, status=status.HTTP_400_BAD_REQUEST)

        reason = request.data.get('reason', '')
        valid_reasons = {r[0] for r in InventoryAdjustment.REASON_CHOICES}
        if reason not in valid_reasons:
            return Response(
                {'error': f'Invalid reason. Must be one of: {", ".join(valid_reasons)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        note = request.data.get('note', '')
        size_stock_id = (request.data.get('size_stock_id') or '').strip()

        with db_transaction.atomic():
            size_row = None
            if size_stock_id and product.has_per_size_stock():
                size_row = (
                    StoreProductSize.objects
                    .select_for_update()
                    .filter(product=product, pk=size_stock_id)
                    .first()
                )
                if size_row is None:
                    return Response(
                        {'error': 'Size stock row not found for this product'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            if size_row is not None:
                new_qty = max(0, size_row.stock_quantity + quantity_delta)
                actual_delta = new_qty - size_row.stock_quantity
                size_row.stock_quantity = new_qty
                size_row.save(update_fields=['stock_quantity', 'updated_at'])
                product.recalculate_total_stock()
            else:
                new_qty = max(0, product.stock_quantity + quantity_delta)
                actual_delta = new_qty - product.stock_quantity
                product.stock_quantity = new_qty
                product.save(update_fields=['stock_quantity', 'updated_at'])

            InventoryAdjustment.objects.create(
                product=product,
                size_stock=size_row,
                quantity_delta=actual_delta,
                reason=reason,
                note=note,
                adjusted_by=request.user if request.user.is_authenticated else None,
            )

        logger.info(
            "Inventory adjustment for %s: delta=%s, reason=%s, size_stock=%s",
            product.name, actual_delta, reason, size_stock_id or '-',
        )
        product.refresh_from_db()
        serializer = self.get_serializer(product)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def transfer_stock(self, request, pk=None):
        """
        Transfer stock between two StoreProductSize rows (different locations).

        Body: {
            "quantity": int,                 # units to transfer (positive)
            "from_size_stock_id": uuid,      # source row
            "to_size_stock_id": uuid,        # destination row
        }

        Atomically decrements the source and increments the destination.
        """
        product = self.get_object()
        try:
            quantity = int(request.data.get('quantity', 0))
        except (TypeError, ValueError):
            return Response({'error': 'quantity must be a positive integer'}, status=status.HTTP_400_BAD_REQUEST)

        if quantity <= 0:
            return Response({'error': 'quantity must be greater than 0'}, status=status.HTTP_400_BAD_REQUEST)

        from_id = (request.data.get('from_size_stock_id') or '').strip()
        to_id = (request.data.get('to_size_stock_id') or '').strip()

        if not from_id or not to_id:
            return Response(
                {'error': 'from_size_stock_id and to_size_stock_id are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if from_id == to_id:
            return Response({'error': 'Source and destination cannot be the same row'}, status=status.HTTP_400_BAD_REQUEST)

        with db_transaction.atomic():
            rows = (
                StoreProductSize.objects
                .select_for_update()
                .filter(product=product, pk__in=[from_id, to_id])
            )
            row_map = {str(r.pk): r for r in rows}

            if from_id not in row_map:
                return Response({'error': 'Source size stock row not found'}, status=status.HTTP_400_BAD_REQUEST)
            if to_id not in row_map:
                return Response({'error': 'Destination size stock row not found'}, status=status.HTTP_400_BAD_REQUEST)

            from_row = row_map[from_id]
            to_row = row_map[to_id]

            if from_row.stock_quantity < quantity:
                return Response(
                    {'error': f'מלאי לא מספיק במקור (נוכחי: {from_row.stock_quantity}, מבוקש: {quantity})'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            from_row.stock_quantity -= quantity
            to_row.stock_quantity += quantity
            from_row.save(update_fields=['stock_quantity', 'updated_at'])
            to_row.save(update_fields=['stock_quantity', 'updated_at'])
            product.recalculate_total_stock()

        logger.info(
            "Stock transfer for %s: quantity=%s, from=%s, to=%s",
            product.name, quantity, from_id, to_id,
        )
        product.refresh_from_db()
        serializer = self.get_serializer(product)
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        """Soft delete: set is_active=False instead of deleting."""
        instance = self.get_object()
        instance.is_active = False
        instance.save(update_fields=['is_active'])

        logger.info(f"Soft deleted product: {instance.name}")

        return Response(status=status.HTTP_204_NO_CONTENT)


class StoreInvoiceViewSet(viewsets.ModelViewSet):
    """
    API endpoints for store invoices.
    
    Provides:
    - List invoices with filters
    - View invoice details
    - Create cash/monthly invoices
    """
    queryset = StoreInvoice.objects.all()
    serializer_class = StoreInvoiceSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filter invoices by query parameters."""
        queryset = super().get_queryset()
        
        # Filter by child
        child_id = self.request.query_params.get('child_id')
        if child_id:
            queryset = queryset.filter(child_id=child_id)
        
        # Filter by status
        payment_status = self.request.query_params.get('status')
        if payment_status:
            queryset = queryset.filter(payment_status=payment_status)
        
        # Filter by date range
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date:
            queryset = queryset.filter(issue_date__gte=start_date)
        if end_date:
            queryset = queryset.filter(issue_date__lte=end_date)
        
        return queryset.order_by('-issue_date')
    
    def create(self, request, *args, **kwargs):
        """
        Create invoice for cash or monthly billing payment.
        
        Body: {
            "items": [{product_id, quantity, size}],
            "child_id": uuid,
            "payment_method": "cash" | "monthly_billing"
        }
        """
        payment_service = PaymentService()
        
        try:
            invoice_data = payment_service.create_cash_invoice(
                product_items=request.data['items'],
                child_id=request.data['child_id'],
                payment_method=request.data['payment_method']
            )
            
            return Response(invoice_data, status=status.HTTP_201_CREATED)
        
        except Exception as e:
            logger.error(f"Error creating cash invoice: {str(e)}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @action(detail=True, methods=['post'])
    def refund(self, request, pk=None):
        """
        Refund a store invoice.
        
        POST /api/v1/store/invoices/{id}/refund/
        Body: {
            "reason": "Refund reason"
        }
        """
        invoice = self.get_object()
        
        reason = request.data.get('reason', 'זיכוי רכישה')
        amount = request.data.get('amount')  # Optional partial refund
        
        # Call payment service to handle refund
        from apps.core.payment_service import PaymentService
        payment_service = PaymentService()
        
        result = payment_service.refund_store_invoice(
            invoice_id=str(invoice.id),
            reason=reason,
            amount=Decimal(str(amount)) if amount else None
        )
        
        if result['success']:
            return Response({
                'success': True,
                'message': result.get('message', 'החשבונית זוכתה בהצלחה'),
                'invoice_number': result.get('invoice_number')
            })
        else:
            return Response({
                'error': result.get('error', 'שגיאה בזיכוי החשבונית')
            }, status=status.HTTP_400_BAD_REQUEST)


class StoreSaleViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only API endpoints for store sales.
    
    Used primarily for analytics and reporting.
    """
    queryset = StoreSale.objects.all()
    serializer_class = StoreSaleSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filter sales by query parameters."""
        queryset = super().get_queryset()
        
        # Filter by date range
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date:
            queryset = queryset.filter(sale_date__gte=start_date)
        if end_date:
            queryset = queryset.filter(sale_date__lte=end_date)
        
        return queryset.order_by('-sale_date')
    
    @action(detail=False, methods=['get'])
    def analytics(self, request):
        """
        Get store analytics dashboard data.

        Query params:
        - days: int (default 30) — lookback window for sales
        - branch: uuid | 'delivery' — filter all data to one branch
        - city: uuid — filter all data to branches in one city

        Returns KPIs, chart data, low-stock list, recent sales, inventory value,
        shrinkage by reason, and top product.
        """
        days = int(request.query_params.get('days', 30))
        start_date = date.today() - timedelta(days=days)
        branch_param = request.query_params.get('branch', '')
        city_param = request.query_params.get('city', '')

        # Base sales queryset — completed only
        completed_sales = StoreSale.objects.filter(
            sale_date__gte=start_date,
            invoice__payment_status='completed',
        ).select_related('product', 'child', 'branch', 'invoice')

        # Base products queryset
        products_qs = StoreProduct.objects.filter(is_active=True)

        # Base adjustments queryset (all time for shrinkage is scoped to same date window)
        adjustments_qs = InventoryAdjustment.objects.filter(created_at__date__gte=start_date)

        if branch_param and branch_param != 'all':
            if branch_param == 'delivery':
                completed_sales = completed_sales.filter(branch__isnull=True)
                products_qs = products_qs.filter(branch__isnull=True)
                adjustments_qs = adjustments_qs.filter(size_stock__branch__isnull=True)
            else:
                completed_sales = completed_sales.filter(branch_id=branch_param)
                products_qs = products_qs.filter(branch_id=branch_param)
                adjustments_qs = adjustments_qs.filter(size_stock__branch_id=branch_param)
        elif city_param and city_param != 'all':
            completed_sales = completed_sales.filter(branch__city_id=city_param)
            products_qs = products_qs.filter(
                Q(branch__city_id=city_param) | Q(size_stocks__branch__city_id=city_param)
            ).distinct()
            adjustments_qs = adjustments_qs.filter(size_stock__branch__city_id=city_param)

        if is_scoped_partner(request.user):
            partner_ids = partner_branch_ids(request.user)
            completed_sales = completed_sales.filter(branch_id__in=partner_ids)
            products_qs = scope_store_products(products_qs, request.user)
            adjustments_qs = adjustments_qs.filter(size_stock__branch_id__in=partner_ids)

        logger.info(
            "[ANALYTICS] last %s days, branch=%s, city=%s; completed sales: %s",
            days, branch_param or 'all', city_param or 'all', completed_sales.count(),
        )

        # KPI 1: Total Revenue
        total_revenue = completed_sales.aggregate(total=Sum('total_price'))['total'] or Decimal('0.00')

        # KPI 2: Net Profit
        net_profit = Decimal('0.00')
        for sale in completed_sales:
            net_profit += sale.total_price - sale.product.cost_price * sale.quantity

        # KPI 3: Total Sales Count
        total_sales_count = completed_sales.count()

        # KPI 4: Low Stock
        low_stock_products = products_qs.filter(stock_quantity__lte=F('min_stock_alert'))
        low_stock_count = low_stock_products.count()

        # KPI 5: Inventory value (current stock × sale_price)
        inventory_value = float(
            sum(p.stock_quantity * p.sale_price for p in products_qs.prefetch_related('size_stocks'))
        )

        # Top product (most units sold in period)
        top_product_row = (
            completed_sales.values('product__name')
            .annotate(total_qty=Sum('quantity'))
            .order_by('-total_qty')
            .first()
        )
        top_product = {
            'name': top_product_row['product__name'],
            'quantity': top_product_row['total_qty'],
        } if top_product_row else None

        # Shrinkage by reason (subtractions only, i.e. quantity_delta < 0)
        shrinkage_rows = (
            adjustments_qs.filter(quantity_delta__lt=0)
            .values('reason')
            .annotate(total_units=Sum('quantity_delta'))
        )
        reason_labels = dict(InventoryAdjustment.REASON_CHOICES)
        shrinkage_by_reason = [
            {
                'reason': item['reason'],
                'reason_label': reason_labels.get(item['reason'], item['reason']),
                'total_units': abs(item['total_units']),
            }
            for item in shrinkage_rows
        ]

        # Chart 1: Monthly Revenue
        monthly_revenue = defaultdict(Decimal)
        for sale in completed_sales:
            monthly_revenue[sale.sale_date.strftime('%Y-%m')] += sale.total_price
        monthly_revenue_data = [
            {'month': m, 'revenue': float(r)}
            for m, r in sorted(monthly_revenue.items())
        ]

        # Chart 2: Sales by Product (top 6)
        sales_by_product_data = [
            {'product': item['product__name'], 'quantity': item['quantity'], 'revenue': float(item['revenue'])}
            for item in completed_sales.values('product__name')
            .annotate(quantity=Sum('quantity'), revenue=Sum('total_price'))
            .order_by('-quantity')[:6]
        ]

        # Chart 3: Sales by Category
        sales_by_category_data = [
            {'category': item['product__category'], 'total': float(item['total'])}
            for item in completed_sales.values('product__category')
            .annotate(total=Sum('total_price'))
            .order_by('-total')
        ]

        # Chart 4: Sales by Branch
        sales_by_branch_data = [
            {'branch': item['branch__name'] or 'לא משויך', 'total': float(item['total'])}
            for item in completed_sales.values('branch__name')
            .annotate(total=Sum('total_price'))
            .order_by('-total')
        ]

        # Chart 5: Sales by Payment Method
        payment_method_names = {'credit_card': 'אשראי', 'cash': 'מזומן', 'monthly_billing': 'הוראת קבע'}
        sales_by_payment_data = [
            {'method': payment_method_names.get(item['payment_method'], item['payment_method']), 'total': float(item['total'])}
            for item in completed_sales.values('payment_method')
            .annotate(total=Sum('total_price'))
            .order_by('-total')
        ]

        # Recent sales (last 10)
        recent_sales = completed_sales.order_by('-sale_date')[:10]

        return Response({
            'total_revenue': float(total_revenue),
            'net_profit': float(net_profit),
            'total_sales_count': total_sales_count,
            'low_stock_count': low_stock_count,
            'inventory_value': inventory_value,
            'top_product': top_product,
            'shrinkage_by_reason': shrinkage_by_reason,
            'monthly_revenue': monthly_revenue_data,
            'sales_by_product': sales_by_product_data,
            'sales_by_category': sales_by_category_data,
            'sales_by_branch': sales_by_branch_data,
            'sales_by_payment_method': sales_by_payment_data,
            'low_stock_products': StoreProductSerializer(low_stock_products, many=True).data,
            'recent_sales': StoreSaleSerializer(recent_sales, many=True).data,
        })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def initiate_payment(request):
    """
    Initiate payment for store purchase.
    
    Uses smart routing:
    - Child with token → Direct charge (synchronous)
    - Child without token → Iframe
    - Walk-in → Iframe
    
    Body: {
        "items": [{product_id, quantity, size}],
        "child_id": uuid | null,
        "customer_info": {name, phone} | null,
        "callback_url": string
    }
    
    Returns:
    - Token charge: {requires_iframe: false, invoice: {...}, success: bool}
    - Iframe: {requires_iframe: true, iframe_url: str, invoice_id: str}
    """
    payment_service = PaymentService()
    
    try:
        result = payment_service.initiate_store_purchase(
            product_items=request.data['items'],
            child_id=request.data.get('child_id'),
            customer_info=request.data.get('customer_info'),
            callback_url=request.data.get('callback_url', '')
        )
        
        return Response(result)
    
    except Exception as e:
        logger.error(f"Error initiating payment: {str(e)}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def charge_card(request):
    """
    Charge a credit card directly using card details.
    
    POST /store/payment/charge-card/
    Body: {
        "items": [{product_id, quantity, size}],
        "child_id": uuid | null,
        "customer_info": {name, phone} | null,
        "card_details": {
            "card_number": "4580458045804580",
            "expiry_month": 12,
            "expiry_year": 2025,
            "cvv": "111",
            "card_holder_id": "123456789"
        },
        "installments": 1
    }
    
    Returns: {success: bool, invoice: {...}, token?: string}
    """
    from apps.store.models import StoreProduct, StoreInvoice, StoreSale
    from apps.core.tranzila_service import TranzilaService
    from apps.store.stock_utils import decrement_product_stock as _decrement_product_stock
    from apps.store.stock_utils import store_line_item_branch_id as _store_line_item_branch_id
    from apps.customers.models import Child, RecurringPayment
    from django.db import transaction as db_transaction
    
    try:
        product_items = request.data['items']
        child_id = request.data.get('child_id')
        installments = request.data.get('installments', 1)
        use_token = request.data.get('charged_with_token', False)
        card_details = request.data.get('card_details') if not use_token else None

        if not use_token and not card_details:
            return Response({'error': 'card_details required'}, status=status.HTTP_400_BAD_REQUEST)

        if not use_token:
            missing = [f for f in ('card_number', 'expiry_month', 'expiry_year', 'cvv') if not card_details.get(f)]
            if missing:
                return Response({'error': f"Missing card fields: {', '.join(missing)}"}, status=status.HTTP_400_BAD_REQUEST)

        # Calculate total
        total_amount = Decimal('0.00')
        tranzila_items = []

        for item in product_items:
            product = StoreProduct.objects.get(id=item['product_id'])
            unit_price = Decimal(str(item['price_override'])) if item.get('price_override') else product.sale_price
            total_amount += unit_price * item['quantity']

            tranzila_items.append({
                'name': f"{product.name} {item.get('size', '')}".strip(),
                'type': 'I',
                'unit_price': float(unit_price),
                'units_number': item['quantity'],
                'unit_type': 1,
                'price_type': 'G',
                'currency_code': 'ILS'
            })

        # Create invoice
        invoice = StoreInvoice.objects.create(
            child_id=child_id,
            customer_name=request.data.get('customer_info', {}).get('name', ''),
            customer_phone=request.data.get('customer_info', {}).get('phone', ''),
            total_amount=total_amount,
            payment_method='credit_card',
            payment_status='pending',
            charged_with_token=use_token,
        )

        tranzila = TranzilaService()

        if use_token:
            # Charge using stored Tranzila token
            child = Child.objects.get(id=child_id)
            recurring = RecurringPayment.objects.filter(child=child, status='active').first()
            if not recurring or not recurring.tranzila_token:
                invoice.payment_status = 'failed'
                invoice.save()
                return Response({'error': 'No stored token for this child'}, status=status.HTTP_400_BAD_REQUEST)
            result = tranzila.charge_with_token(
                token=recurring.tranzila_token,
                amount=total_amount,
                description=f"Store purchase - Invoice {invoice.invoice_number}",
                items=tranzila_items,
            )
        else:
            result = tranzila.charge_with_card(
                card_number=card_details['card_number'],
                expiry_month=int(card_details['expiry_month']),
                expiry_year=int(card_details['expiry_year']),
                cvv=card_details['cvv'],
                card_holder_id=card_details['card_holder_id'],
                amount=total_amount,
                description=f"Store purchase - Invoice {invoice.invoice_number}",
                items=tranzila_items,
                installments=installments,
            )
        
        if result['success']:
            # Update invoice
            invoice.payment_status = 'completed'
            invoice.tranzila_transaction_id = result.get('transaction_id', '')
            invoice.tranzila_confirmation_code = result.get('confirmation_code', '')
            invoice.save()
            
            # Create sales and update stock
            with db_transaction.atomic():
                for item in product_items:
                    product = StoreProduct.objects.select_for_update().get(id=item['product_id'])

                    StoreSale.objects.create(
                        invoice=invoice,
                        product=product,
                        child=invoice.child,
                        quantity=item['quantity'],
                        unit_price=product.sale_price,
                        total_price=product.sale_price * item['quantity'],
                        size=item.get('size', ''),
                        payment_method='credit_card',
                        branch_id=_store_line_item_branch_id(item, product),
                    )

                    _decrement_product_stock(product, item)
            
            # Save token for future use if child exists
            token_created = result.get('token')
            if token_created and child_id:
                try:
                    child = Child.objects.get(id=child_id)
                    # Check if recurring payment exists
                    recurring = RecurringPayment.objects.filter(
                        child=child,
                        status='active'
                    ).first()
                    
                    if recurring:
                        # Update existing token
                        recurring.tranzila_token = token_created
                        recurring.save()
                    else:
                        # Create new recurring payment record (for token storage)
                        RecurringPayment.objects.create(
                            child=child,
                            tranzila_token=token_created,
                            status='active',
                            amount=Decimal('0.00'),  # Will be updated when used
                            billing_day=1,
                            start_date=date.today(),
                            next_billing_date=date.today()
                        )
                    logger.info(f"Saved token for future use: child={child_id}")
                except Exception as e:
                    logger.warning(f"Could not save token: {e}")
            
            from apps.store.serializers import StoreInvoiceSerializer
            return Response({
                'success': True,
                'invoice': StoreInvoiceSerializer(invoice).data,
                'token_saved': bool(token_created and child_id)
            })
        else:
            invoice.payment_status = 'failed'
            invoice.notes = f"Payment failed: {result.get('error')}"
            invoice.save()
            
            return Response({
                'success': False,
                'error': result.get('error', 'Payment failed')
            }, status=status.HTTP_400_BAD_REQUEST)
    
    except Exception as e:
        logger.error(f"Error charging card: {str(e)}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def payment_callback(request):
    """
    Tranzila webhook callback for iframe payments.
    
    Called by Tranzila after successful/failed payment.
    Completes the purchase by creating sales records and updating stock.
    
    This endpoint is public (no authentication required) since it's called
    by Tranzila's servers.
    """
    logger.info("[STORE WEBHOOK] received: method=%s content_type=%s", request.method, request.content_type)
    logger.debug("[STORE WEBHOOK] data=%s GET=%s POST=%s",
                 getattr(request, 'data', None), dict(request.GET), dict(request.POST))

    payment_service = PaymentService()

    try:
        signature = request.headers.get('X-Tranzila-Signature', '')
        if signature:
            logger.info("[STORE WEBHOOK] signature present: %s...", signature[:20])
        else:
            logger.warning("[STORE WEBHOOK] no signature in headers (development mode)")

        tranzila_service = payment_service.tranzila_service
        parsed_response = tranzila_service.parse_webhook_response(request.data)

        invoice_id = request.data.get('pdesc', '')

        logger.info("[STORE WEBHOOK] parsed invoice_id=%s response=%s", invoice_id, parsed_response)

        result = payment_service.complete_store_purchase_from_webhook(
            invoice_id=invoice_id,
            tranzila_response=parsed_response,
            signature=signature
        )

        logger.info("[STORE WEBHOOK] processed for invoice %s: %s", invoice_id, result)

        return Response(result)

    except Exception as e:
        logger.error("[STORE WEBHOOK] error processing webhook: %s", str(e), exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

