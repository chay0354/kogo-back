#!/usr/bin/env python
"""Send one sample staff order notification to a given inbox."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

BACK_ROOT = Path(__file__).resolve().parents[1]
B2C_ENV = BACK_ROOT.parent / 'b2c-website' / '.env.local'
TO = (sys.argv[1] if len(sys.argv) > 1 else 'chay.moalem@gmail.com').strip()
FROM = os.environ.get('RESEND_FROM_EMAIL', 'Cogomelo <orders@cogopass.site>').strip()


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, val = line.split('=', 1)
        out[key.strip()] = val.strip()
    return out


def shekels(agorot: int) -> str:
    return f'{agorot / 100:.2f} ₪'


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


def main() -> None:
    back_env = load_env(BACK_ROOT / '.env')
    b2c_env = load_env(B2C_ENV)
    api_key = (back_env.get('RESEND_API_KEY') or b2c_env.get('RESEND_API_KEY') or '').strip()
    sb_url = b2c_env.get('NEXT_PUBLIC_SUPABASE_URL', '').rstrip('/')
    service_key = b2c_env.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not api_key or not sb_url or not service_key:
        raise SystemExit('Missing RESEND or B2C Supabase config')

    h = {'apikey': service_key, 'Authorization': f'Bearer {service_key}'}
    orders_r = requests.get(
        f'{sb_url}/rest/v1/orders',
        params={
            'select': 'id,order_number,brand,customer_name,customer_email,customer_phone,shipping_address,customer_notes,total_agorot,status,crm_invoice_number,provider_txn_id,created_at',
            'status': 'eq.paid',
            'order': 'created_at.desc',
            'limit': '1',
        },
        headers=h,
        timeout=30,
    )
    orders_r.raise_for_status()
    order = orders_r.json()[0]

    items_r = requests.get(
        f'{sb_url}/rest/v1/order_items',
        params={
            'order_id': f'eq.{order["id"]}',
            'select': 'name,variant,qty,unit_price_agorot,line_total_agorot',
        },
        headers=h,
        timeout=30,
    )
    items_r.raise_for_status()
    items = items_r.json()

    order_number = order['order_number']
    customer_name = order.get('customer_name') or ''
    notes = (order.get('customer_notes') or '').strip() or '—'
    heading = 'הזמנה חדשה מהאתר'
    products = format_product_summary(items)
    title = with_product_suffix(f'{heading} — {order_number}', products)
    text_lines = []
    html_rows = []
    for item in items:
        name = line_label(item)
        text_lines.append(f"• {name} × {item['qty']} — {shekels(item['line_total_agorot'])}")
        html_rows.append(
            f'<tr><td style="padding:8px;border-bottom:1px solid #eee">{name}</td>'
            f'<td style="padding:8px;text-align:center">{item["qty"]}</td>'
            f'<td style="padding:8px;text-align:left" dir="ltr">{shekels(item["line_total_agorot"])}</td></tr>'
        )

    subject = f'[דוגמה] {with_product_suffix(f"הזמנה חדשה {order_number} — {customer_name}", products)}'
    text = '\n'.join([
        title,
        'זהו מייל לדוגמה — כך נראה המייל שנשלח ל-order_emails (bom@cogo.co.il)',
        f'תאריך: {order.get("created_at", "")}',
        'סטטוס: שולם',
        f'חשבונית CRM: {order.get("crm_invoice_number") or ""}',
        f'אסמכתא תשלום: {order.get("provider_txn_id") or ""}',
        '',
        f'שם: {customer_name}',
        f'טלפון: {order.get("customer_phone") or ""}',
        f'אימייל: {order.get("customer_email") or ""}',
        f'כתובת למשלוח: {order.get("shipping_address") or ""}',
        f'הערות: {notes}',
        '',
        'הפריטים:',
        *text_lines,
        '',
        f'סה"כ לתשלום: {shekels(order.get("total_agorot") or 0)}',
    ])
    html = f'''
<div dir="rtl" style="font-family:Arial,sans-serif;color:#25326a;max-width:620px;margin:auto">
  <p style="background:#fffbe9;border:1px solid #fbcf1f;border-radius:8px;padding:10px 12px;color:#25326a">
    <b>דוגמה</b> — כך נראה המייל שנשלח לכתובת order_emails (bom@cogo.co.il)
  </p>
  <h2 style="color:#303094">{title}</h2>
  <p style="color:#888">{order.get("created_at", "")} · <b>שולם</b></p>
  <h3 style="color:#303094">פרטי הלקוח</h3>
  <p>
    <b>שם:</b> {customer_name}<br>
    <b>טלפון:</b> {order.get("customer_phone") or ""}<br>
    <b>אימייל:</b> {order.get("customer_email") or ""}<br>
    <b>כתובת למשלוח:</b> {order.get("shipping_address") or ""}<br>
    <b>הערות:</b> {notes}
  </p>
  <h3 style="color:#303094">הפריטים</h3>
  <table style="width:100%;border-collapse:collapse">
    <thead><tr style="background:#f7f6fc">
      <th style="padding:8px;text-align:right">מוצר</th>
      <th style="padding:8px;text-align:center">כמות</th>
      <th style="padding:8px;text-align:left">סה"כ</th>
    </tr></thead>
    <tbody>{"".join(html_rows)}</tbody>
  </table>
  <p style="font-size:18px;font-weight:bold;margin-top:16px">סה"כ לתשלום: {shekels(order.get("total_agorot") or 0)}</p>
</div>'''

    resp = requests.post(
        'https://api.resend.com/emails',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json={
            'from': FROM,
            'to': [TO],
            'subject': subject,
            'text': text,
            'html': html,
            'reply_to': order.get('customer_email') or None,
        },
        timeout=30,
    )
    if not resp.ok:
        raise SystemExit(f'Resend failed ({resp.status_code}): {resp.text[:300]}')
    print(f'Sent sample staff order email for {order_number} -> {TO}')


if __name__ == '__main__':
    main()
