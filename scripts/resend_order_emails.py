#!/usr/bin/env python
"""Resend failed store customer invoices + B2C staff order notifications."""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path

import requests

BACK_ROOT = Path(__file__).resolve().parents[1]
B2C_ENV = BACK_ROOT.parent / 'b2c-website' / '.env.local'
FROM_EMAIL = os.environ.get('RESEND_FROM_EMAIL', 'Cogomelo <orders@cogopass.site>').strip()


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, val = line.split('=', 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def shekels(agorot: int) -> str:
    return f'{(Decimal(agorot) / 100):.2f} ₪'


def line_label(item: dict) -> str:
    name = item['name']
    if item.get('variant'):
        name = f"{name} ({item['variant']})"
    return name


def format_product_summary(items: list[dict], max_len: int = 80) -> str:
    if not items:
        return ''
    labels = [line_label(item) for item in items]
    if len(labels) == 1:
        summary = labels[0]
    else:
        summary = ', '.join(labels[:2])
        if len(labels) > 2:
            summary += f' +{len(labels) - 2}'
    if len(summary) > max_len:
        return summary[: max_len - 1] + '…'
    return summary


def with_product_suffix(base: str, products: str) -> str:
    return f'{base} — {products}' if products else base


def resend_send(*, api_key: str, to: list[str], subject: str, text: str, html: str,
                reply_to: str | None = None, attachments: list[dict] | None = None) -> None:
    body: dict = {
        'from': FROM_EMAIL,
        'to': to,
        'subject': subject,
        'html': html,
        'text': text,
    }
    if reply_to:
        body['reply_to'] = reply_to
    if attachments:
        body['attachments'] = attachments
    r = requests.post(
        'https://api.resend.com/emails',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json=body,
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f'Resend failed ({r.status_code}): {r.text[:300]}')


def retry_crm_customer_invoices(api_key: str) -> None:
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    os.environ['RESEND_FROM_EMAIL'] = FROM_EMAIL
    sys.path.insert(0, str(BACK_ROOT))
    import django
    django.setup()
    from apps.store.invoice_email import send_store_invoice_email
    from apps.store.models import StoreInvoice

    qs = StoreInvoice.objects.filter(
        payment_status='completed',
        website_order_number__gt='',
        invoice_email_sent_at__isnull=True,
    ).order_by('created_at')
    print(f'\n=== CRM customer invoice emails ({qs.count()}) ===')
    for inv in qs:
        email = (inv.customer_email or '').strip()
        if not email:
            print('SKIP no email', inv.invoice_number, inv.website_order_number)
            continue
        try:
            send_store_invoice_email(inv)
            inv.refresh_from_db()
            status = 'OK' if inv.invoice_email_sent_at else 'FAIL'
            print(status, inv.invoice_number, inv.website_order_number, '->', email)
        except Exception as exc:
            print('ERROR', inv.invoice_number, str(exc)[:200])


def sb_headers(service_key: str) -> dict[str, str]:
    return {
        'apikey': service_key,
        'Authorization': f'Bearer {service_key}',
        'Content-Type': 'application/json',
    }


def retry_b2c_staff_emails(api_key: str, b2c_env: dict[str, str]) -> None:
    sb_url = b2c_env.get('NEXT_PUBLIC_SUPABASE_URL', '').rstrip('/')
    service_key = b2c_env.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not sb_url or not service_key:
        print('\n=== B2C staff emails: skipped (missing Supabase config) ===')
        return

    h = sb_headers(service_key)
    settings_r = requests.get(
        f'{sb_url}/rest/v1/app_settings?key=eq.order_emails&select=value',
        headers=h,
        timeout=30,
    )
    settings_r.raise_for_status()
    rows = settings_r.json()
    raw = (rows[0]['value'] if rows else '') or ''
    recipients = [p for p in re.split(r'[,;\s]+', raw) if re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', p)]
    if not recipients:
        print('\n=== B2C staff emails: skipped (order_emails empty) ===')
        return

    orders_r = requests.get(
        f'{sb_url}/rest/v1/orders?select=id,order_number,brand,customer_name,customer_email,customer_phone,shipping_address,customer_notes,shipping_method,total_agorot,status,crm_invoice_number,provider_txn_id,created_at&status=in.(paid,pending)&order=created_at.asc',
        headers=h,
        timeout=30,
    )
    orders_r.raise_for_status()
    orders = orders_r.json()
    print(f'\n=== B2C staff order emails ({len(orders)}) -> {", ".join(recipients)} ===')

    for order in orders:
        order_number = order['order_number']
        items_r = requests.get(
            f'{sb_url}/rest/v1/order_items?order_id=eq.{order["id"]}&select=name,variant,qty,unit_price_agorot,line_total_agorot',
            headers=h,
            timeout=30,
        )
        items_r.raise_for_status()
        items = items_r.json()
        status = order.get('status') or 'pending'
        status_label = {'paid': 'שולם', 'failed': 'תשלום נכשל', 'pending': 'ממתין לתשלום'}.get(status, '')
        brand = order.get('brand') or 'cogo'
        heading = 'הזמנה חדשה מהחנות של געגע' if brand == 'gaga' else 'הזמנה חדשה מהאתר'
        customer_name = order.get('customer_name') or ''
        customer_email = order.get('customer_email') or ''
        customer_phone = order.get('customer_phone') or ''
        shipping = order.get('shipping_address') or ''
        notes = (order.get('customer_notes') or '').strip() or '—'
        products = format_product_summary(items)
        title = with_product_suffix(f'{heading} — {order_number}', products)

        text_lines = []
        html_rows = []
        for item in items:
            name = line_label(item)
            text_lines.append(f"• {name} × {item['qty']} — {shekels(item['line_total_agorot'])}")
            html_rows.append(
                f'<tr><td style="padding:8px;border-bottom:1px solid #eee">{name}</td>'
                f'<td style="padding:8px;border-bottom:1px solid #eee;text-align:center">{item["qty"]}</td>'
                f'<td style="padding:8px;border-bottom:1px solid #eee;text-align:left" dir="ltr">{shekels(item["line_total_agorot"])}</td></tr>'
            )

        crm_inv = order.get('crm_invoice_number') or ''
        txn = order.get('provider_txn_id') or ''
        subject = with_product_suffix(
            f'הזמנה מהחנות של געגע {order_number} — {customer_name}'
            if brand == 'gaga'
            else f'הזמנה חדשה {order_number} — {customer_name}',
            products,
        )
        text = '\n'.join([
            title,
            f'תאריך: {order.get("created_at", "")}',
            f'סטטוס: {status_label}' if status_label else '',
            f'חשבונית CRM: {crm_inv}' if crm_inv else '',
            f'אסמכתא תשלום: {txn}' if txn else '',
            '',
            'פרטי הלקוח:',
            f'שם: {customer_name}',
            f'טלפון: {customer_phone}',
            f'אימייל: {customer_email}',
            f'כתובת למשלוח: {shipping}',
            f'הערות: {notes}',
            '',
            'הפריטים:',
            *text_lines,
            '',
            f'סה"כ לתשלום: {shekels(order.get("total_agorot") or 0)}',
        ])
        html = f'''
<div dir="rtl" style="font-family:Arial,sans-serif;color:#25326a;max-width:620px;margin:auto">
  <h2 style="color:#303094">{title}</h2>
  <p style="color:#888">{order.get("created_at", "")}{f" · <b>{status_label}</b>" if status_label else ""}</p>
  {"<p><b>חשבונית CRM:</b> " + crm_inv + "</p>" if crm_inv else ""}
  {"<p><b>אסמכתא תשלום:</b> " + txn + "</p>" if txn else ""}
  <p><b>שם:</b> {customer_name}<br><b>טלפון:</b> {customer_phone}<br><b>אימייל:</b> {customer_email}<br><b>כתובת:</b> {shipping}<br><b>הערות:</b> {notes}</p>
  <table style="width:100%;border-collapse:collapse"><tbody>{"".join(html_rows)}</tbody></table>
  <p style="font-weight:bold;margin-top:16px">סה"כ: {shekels(order.get("total_agorot") or 0)}</p>
</div>'''
        try:
            resend_send(
                api_key=api_key,
                to=recipients,
                subject=subject,
                text=text,
                html=html,
                reply_to=customer_email or None,
            )
            print('OK staff', order_number, status)
        except Exception as exc:
            print('ERROR staff', order_number, str(exc)[:200])


def main() -> None:
    b2c_env = load_env_file(B2C_ENV)
    back_env = load_env_file(BACK_ROOT / '.env')
    api_key = (
        os.environ.get('RESEND_API_KEY')
        or back_env.get('RESEND_API_KEY')
        or b2c_env.get('RESEND_API_KEY')
        or ''
    ).strip()
    if not api_key:
        raise SystemExit('RESEND_API_KEY not configured')
    print('From:', FROM_EMAIL)
    retry_crm_customer_invoices(api_key)
    retry_b2c_staff_emails(api_key, b2c_env)


if __name__ == '__main__':
    main()
