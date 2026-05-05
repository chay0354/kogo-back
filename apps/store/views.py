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

from apps.store.models import StoreProduct, StoreInvoice, StoreSale
from apps.store.serializers import (
    StoreProductSerializer, StoreInvoiceSerializer,
    StoreSaleSerializer, StoreAnalyticsSerializer,
    PaymentInitiationResponseSerializer
)
from apps.core.payment_service import PaymentService

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
    queryset = StoreProduct.objects.filter(is_active=True)
    serializer_class = StoreProductSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filter products by query parameters."""
        queryset = super().get_queryset()
        
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
            "quantity": number
        }
        """
        product = self.get_object()
        mode = request.data.get('mode')
        quantity = int(request.data.get('quantity', 0))
        
        if mode == 'add':
            product.stock_quantity += quantity
        elif mode == 'subtract':
            product.stock_quantity = max(0, product.stock_quantity - quantity)
        elif mode == 'set':
            product.stock_quantity = max(0, quantity)
        else:
            return Response(
                {'error': 'Invalid mode. Must be add, subtract, or set'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        product.save(update_fields=['stock_quantity'])
        
        logger.info(f"Updated stock for {product.name}: mode={mode}, quantity={quantity}, new_stock={product.stock_quantity}")
        
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
        
        Returns:
        - KPIs (revenue, profit, sales count, low stock)
        - Charts data (monthly revenue, by product, by category, etc.)
        - Low stock products list
        - Recent sales list
        """
        # Date range (default: last 30 days)
        days = int(request.query_params.get('days', 30))
        start_date = date.today() - timedelta(days=days)
        
        # Get ONLY completed sales - refunded invoices are excluded automatically
        completed_sales = StoreSale.objects.filter(
            sale_date__gte=start_date,
            invoice__payment_status='completed'
        ).select_related('product', 'child', 'branch', 'invoice')
        
        print(f"\n📊 [ANALYTICS] Calculating KPIs for last {days} days (from {start_date})")
        print(f"   Completed sales found: {completed_sales.count()}")
        
        # KPI 1: Total Revenue (sum all completed sales)
        total_revenue = completed_sales.aggregate(
            total=Sum('total_price')
        )['total'] or Decimal('0.00')
        
        print(f"   📈 Total Revenue (completed sales only): ₪{total_revenue}")
        
        # KPI 2: Net Profit (revenue - cost for completed sales)
        net_profit = Decimal('0.00')
        for sale in completed_sales:
            cost = sale.product.cost_price * sale.quantity
            profit = sale.total_price - cost
            net_profit += profit
        
        print(f"   💰 Net Profit (completed sales only): ₪{net_profit}")
        
        # KPI 3: Total Sales Count
        total_sales_count = completed_sales.count()
        
        # KPI 4: Low Stock Products
        low_stock_products = StoreProduct.objects.filter(
            is_active=True,
            stock_quantity__lte=F('min_stock_alert')
        )
        low_stock_count = low_stock_products.count()
        
        # Chart 1: Monthly Revenue (completed sales only)
        monthly_revenue = defaultdict(Decimal)
        for sale in completed_sales:
            month_key = sale.sale_date.strftime('%Y-%m')
            monthly_revenue[month_key] += sale.total_price
        
        monthly_revenue_data = [
            {'month': month, 'revenue': float(revenue)}
            for month, revenue in sorted(monthly_revenue.items())
        ]
        
        print(f"   📅 Monthly revenue data: {monthly_revenue_data}")
        
        # Chart 2: Sales by Product (top 6) - only completed sales
        sales_by_product = completed_sales.values('product__name').annotate(
            quantity=Sum('quantity'),
            revenue=Sum('total_price')
        ).order_by('-quantity')[:6]
        
        sales_by_product_data = [
            {
                'product': item['product__name'],
                'quantity': item['quantity'],
                'revenue': float(item['revenue'])
            }
            for item in sales_by_product
        ]
        
        # Chart 3: Sales by Category - only completed sales
        sales_by_category = completed_sales.values('product__category').annotate(
            total=Sum('total_price')
        ).order_by('-total')
        
        sales_by_category_data = [
            {'category': item['product__category'], 'total': float(item['total'])}
            for item in sales_by_category
        ]
        
        # Chart 4: Sales by Branch - only completed sales
        sales_by_branch = completed_sales.values('branch__name').annotate(
            total=Sum('total_price')
        ).order_by('-total')
        
        sales_by_branch_data = [
            {
                'branch': item['branch__name'] or 'לא משויך',
                'total': float(item['total'])
            }
            for item in sales_by_branch
        ]
        
        # Chart 5: Sales by Payment Method - only completed sales
        sales_by_payment = completed_sales.values('payment_method').annotate(
            total=Sum('total_price')
        ).order_by('-total')
        
        payment_method_names = {
            'credit_card': 'אשראי',
            'cash': 'מזומן',
            'monthly_billing': 'הוראת קבע'
        }
        
        sales_by_payment_data = [
            {
                'method': payment_method_names.get(item['payment_method'], item['payment_method']),
                'total': float(item['total'])
            }
            for item in sales_by_payment
        ]
        
        # Recent sales (last 10) - only completed
        recent_sales = completed_sales.order_by('-sale_date')[:10]
        
        # Assemble response
        analytics_data = {
            'total_revenue': float(total_revenue),
            'net_profit': float(net_profit),
            'total_sales_count': total_sales_count,
            'low_stock_count': low_stock_count,
            'monthly_revenue': monthly_revenue_data,
            'sales_by_product': sales_by_product_data,
            'sales_by_category': sales_by_category_data,
            'sales_by_branch': sales_by_branch_data,
            'sales_by_payment_method': sales_by_payment_data,
            'low_stock_products': StoreProductSerializer(low_stock_products, many=True).data,
            'recent_sales': StoreSaleSerializer(recent_sales, many=True).data
        }
        
        return Response(analytics_data)


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
    from apps.customers.models import Child, RecurringPayment
    from django.db import transaction as db_transaction
    
    try:
        product_items = request.data['items']
        card_details = request.data['card_details']
        child_id = request.data.get('child_id')
        installments = request.data.get('installments', 1)
        
        # Calculate total
        total_amount = Decimal('0.00')
        tranzila_items = []
        
        for item in product_items:
            product = StoreProduct.objects.get(id=item['product_id'])
            total_amount += product.sale_price * item['quantity']
            
            tranzila_items.append({
                'name': f"{product.name} {item.get('size', '')}".strip(),
                'type': 'I',
                'unit_price': float(product.sale_price),
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
            charged_with_token=False
        )
        
        # Charge card
        tranzila = TranzilaService()
        result = tranzila.charge_with_card(
            card_number=card_details['card_number'],
            expiry_month=int(card_details['expiry_month']),
            expiry_year=int(card_details['expiry_year']),
            cvv=card_details['cvv'],
            card_holder_id=card_details['card_holder_id'],
            amount=total_amount,
            description=f"Store purchase - Invoice {invoice.invoice_number}",
            transaction_id=str(invoice.id),
            items=tranzila_items,
            installments=installments
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
                    
                    if product.stock_quantity < item['quantity']:
                        raise ValueError(f'אין מספיק מלאי עבור {product.name}')
                    
                    StoreSale.objects.create(
                        invoice=invoice,
                        product=product,
                        child=invoice.child,
                        quantity=item['quantity'],
                        unit_price=product.sale_price,
                        total_price=product.sale_price * item['quantity'],
                        size=item.get('size', ''),
                        payment_method='credit_card',
                        branch=product.branch
                    )
                    
                    product.stock_quantity -= item['quantity']
                    product.save(update_fields=['stock_quantity'])
            
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
    print("\n" + "=" * 100)
    print("🔔 TRANZILA WEBHOOK RECEIVED - STORE PAYMENT")
    print("=" * 100)
    print(f"Request method: {request.method}")
    print(f"Content type: {request.content_type}")
    print(f"Request data keys: {list(request.data.keys()) if hasattr(request, 'data') else 'N/A'}")
    print(f"Request data: {request.data if hasattr(request, 'data') else 'N/A'}")
    print(f"GET params: {dict(request.GET)}")
    print(f"POST params: {dict(request.POST)}")
    print("=" * 100 + "\n")
    
    payment_service = PaymentService()
    
    try:
        # Get signature from headers for security verification
        signature = request.headers.get('X-Tranzila-Signature', '')
        if signature:
            print(f"🔐 Webhook signature present: {signature[:20]}...")
        else:
            print(f"⚠️  No webhook signature in headers (development mode)")
        
        # Parse Tranzila webhook
        tranzila_service = payment_service.tranzila_service
        parsed_response = tranzila_service.parse_webhook_response(request.data)
        
        # Get invoice ID from transaction metadata
        invoice_id = request.data.get('pdesc', '')
        
        print(f"📋 Parsed webhook:")
        print(f"   Invoice ID (pdesc): {invoice_id}")
        print(f"   Response: {parsed_response}\n")
        
        # Complete purchase with signature verification
        result = payment_service.complete_store_purchase_from_webhook(
            invoice_id=invoice_id,
            tranzila_response=parsed_response,
            signature=signature
        )
        
        logger.info(f"✅ Webhook processed for invoice {invoice_id}: {result}")
        print(f"✅ Webhook processing completed successfully\n")
        
        return Response(result)
    
    except Exception as e:
        print(f"❌ ERROR processing webhook: {str(e)}\n")
        logger.error(f"Error processing webhook: {str(e)}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

