"""Which Tranzila terminal each money flow actually runs through.

Five terminal settings accumulated over time, and nothing in the system ever
said which one does what — so the only way to answer "what is this terminal
for" was to read the code. This module is that answer, written once and read
by the settings screen.

Terminal *names* only. Public and secret keys are never returned: a terminal
name already travels in the iframe URL a customer sees, a key does not.
"""
from __future__ import annotations

from django.conf import settings

# A setting whose value is one of these was never filled in with a real one.
PLACEHOLDERS = {'', 'mock-terminal', 'mock-token-terminal', 'mock-supplier'}


def _value(name: str) -> str:
    return (getattr(settings, name, '') or '').strip()


def _configured(name: str) -> bool:
    return _value(name) not in PLACEHOLDERS


# Every flow that moves money, and the setting that decides where it lands.
# `method` names the TranzilaService call, so a reader can follow it in code.
FLOWS = [
    {
        'id': 'widget_signup',
        'title': 'הרשמה לחוג דרך האתר',
        'detail': 'ההורה מקליד כרטיס בווידג\'ט; החיוב הראשון נגבה מיד ונשמר טוקן להוראת קבע.',
        'setting': 'TRANZILA_PROD_TERMINAL',
        'method': 'charge_with_card',
        'code': 'apps/customers/widget_views.py',
    },
    {
        'id': 'recurring',
        'title': 'הוראת קבע חודשית',
        'detail': 'הקרון מחייב את הטוקן השמור בכל חודש. זה רוב הכסף.',
        'setting': 'TRANZILA_PROD_TOKEN_TERMINAL',
        'method': 'charge_with_token',
        'code': 'apps/customers/recurring_billing.py',
    },
    {
        'id': 'crm_charge',
        'title': 'חיוב ידני מהמערכת',
        'detail': 'הקלדת כרטיס בעמוד כרטיסי אשראי, ושינויי מחיר שנגבים מיד.',
        'setting': 'TRANZILA_PROD_TERMINAL',
        'method': 'charge_with_card',
        'code': 'apps/core/credit_card_charge_views.py',
    },
    {
        'id': 'card_update',
        'title': 'קישור עדכון כרטיס',
        'detail': 'הורה מעדכן כרטיס פג תוקף דרך קישור שנשלח אליו.',
        'setting': 'TRANZILA_PROD_TERMINAL',
        'method': 'charge_with_card / verify_card',
        'code': 'apps/customers/card_link.py',
    },
    {
        'id': 'store_b2c',
        'title': 'חנות האתר (B2C)',
        'detail': 'הזמנה מהאתר נסלקת בעמוד המתארח של טרנזילה — כאן עובדים Bit ו-Apple Pay.',
        'setting': 'TRANZILA_TERMINAL',
        'method': 'create_payment_request (iframe)',
        'code': 'apps/store/widget_views.py',
    },
    {
        'id': 'payment_links',
        'title': 'קישורי תשלום',
        'detail': 'קישור לתשלום חד-פעמי לכל מטרה — גם הוא בעמוד המתארח.',
        'setting': 'TRANZILA_TERMINAL',
        'method': 'create_payment_request (iframe)',
        'code': 'apps/payment_links/public_views.py',
    },
    {
        'id': 'documents',
        'title': 'הפקת חשבוניות וקבלות',
        'detail': 'מסמכי מס. כל עוד המסוף ריק — לא מופק שום מסמך, לאף חיוב.',
        'setting': 'TRANZILA_BILLING_TERMINAL',
        'method': 'create_formal_document',
        'code': 'apps/documents/service.py',
    },
]

SETTING_NOTES = {
    'TRANZILA_TERMINAL': 'מסוף העמוד המתארח (iframe). היחיד שתומך ב-Bit וב-Apple Pay.',
    'TRANZILA_TOKEN_TERMINAL': 'לא בשימוש בפרודקשן — נקרא רק כשיוצרים שירות בלי production().',
    'TRANZILA_PROD_TERMINAL': 'מסוף ה-REST להקלדת כרטיס.',
    'TRANZILA_PROD_TOKEN_TERMINAL': 'מסוף ה-REST לחיוב טוקן שמור (הוראות קבע).',
    'TRANZILA_BILLING_TERMINAL': 'מסוף הפקת מסמכים. ריק = אין חשבוניות.',
}

ALL_SETTINGS = [
    'TRANZILA_TERMINAL',
    'TRANZILA_TOKEN_TERMINAL',
    'TRANZILA_PROD_TERMINAL',
    'TRANZILA_PROD_TOKEN_TERMINAL',
    'TRANZILA_BILLING_TERMINAL',
]


def terminal_map() -> dict:
    """
    The flows, the settings behind them, and the distinct terminals in play.

    The last part is the one that untangles the knot: two settings holding the
    same value are one terminal wearing two names, and a setting no flow reads
    is a terminal the system does not use at all.
    """
    flows = []
    for flow in FLOWS:
        terminal = _value(flow['setting'])
        flows.append({
            **flow,
            'terminal': terminal,
            'configured': _configured(flow['setting']),
        })

    settings_rows = []
    for name in ALL_SETTINGS:
        used_by = [f['title'] for f in flows if f['setting'] == name]
        settings_rows.append({
            'setting': name,
            'terminal': _value(name),
            'configured': _configured(name),
            'note': SETTING_NOTES.get(name, ''),
            'used_by': used_by,
            'in_use': bool(used_by),
        })

    # Group the flows by the terminal that actually clears them, so two
    # settings pointing at one terminal collapse into a single row.
    by_terminal: dict[str, dict] = {}
    for flow in flows:
        if not flow['terminal']:
            continue
        row = by_terminal.setdefault(flow['terminal'], {
            'terminal': flow['terminal'],
            'settings': [],
            'flows': [],
        })
        if flow['setting'] not in row['settings']:
            row['settings'].append(flow['setting'])
        row['flows'].append(flow['title'])

    unused = [r['setting'] for r in settings_rows if not r['in_use']]
    missing = [f['title'] for f in flows if not f['configured']]

    return {
        'environment': getattr(settings, 'TRANZILA_ENVIRONMENT', ''),
        'flows': flows,
        'settings': settings_rows,
        'terminals': sorted(by_terminal.values(), key=lambda r: r['terminal']),
        'unused_settings': unused,
        'flows_without_a_terminal': missing,
    }
