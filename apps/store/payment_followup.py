"""
A store payment on Tranzila's hosted page that did not end cleanly — the CRM
half of stage 3 of the cogolive plan (the website store opens on cogolive).

The website's checkout, and the till's walk-in "secure page", pay on
Tranzila's page. Tranzila's notify completes the sale
(PaymentService.complete_store_purchase_from_webhook): the notify is public
and unsigned, so the sale happens only when the terminal's own report shows
the same transaction. Three things can go wrong after the customer paid, and
none of them may cost the customer money or the office a sale:

  1. The notify came, but the report could not be asked ('unavailable') or
     did not agree with it ('unverified'). The invoice stays pending with the
     number the notify quoted. `recheck_pending_payment` asks the report again
     about that same number and, when the report now confirms it, completes
     the sale through the very same path — the same row lock, the same
     checks, the same "never sold twice" guard. Nothing here ever charges or
     refunds; the only call to Tranzila is the report read the notify makes.
  2. The sale is complete, but the website never heard: its "paid" call was a
     single POST with no retry, so the order stayed "awaiting payment" on the
     site and the staff's order email never went out. `tell_website_paid`
     keeps the site's answer on the invoice (website_paid_notified_at), and
     the call is repeated until it lands.
  3. Either stays that way. The office is told at once (apps/core/office_alerts.py)
     — once per invoice, with who the customer is, the sum, the transaction
     number and what to check in Tranzila — after STUCK_AFTER, or at once
     when the report disagrees with the notify.

Who drives the follow-up:
  * the website's status poll, GET /api/v1/store/widget/payment/status/
    (`website_order_status`), at most once per RECHECK_INTERVAL per invoice;
  * the morning brief's sweep, "תשלומים שנתקעו בחנות"
    (`sweep_stuck_store_payments`), over the last SWEEP_WINDOW;
  * the site asking to pay an order again (widget/payment/initiate/);
  * another notify for the same invoice (the ordinary path).
There is no cron of its own: a new Vercel cron is a Level-2 decision (plan
item 2.7), so between the site's polls and the morning an invoice waits.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.store.models import StoreInvoice

logger = logging.getLogger(__name__)

# The site polls every few seconds while the customer waits; Tranzila's report
# is asked about the same invoice at most this often, whoever asks.
RECHECK_INTERVAL = timedelta(seconds=15)
# A payment that has a transaction number and is still not settled after this
# long — or a paid order the site still has not acknowledged — goes to the office.
STUCK_AFTER = timedelta(minutes=10)
# How far back the morning sweep looks.
SWEEP_WINDOW = timedelta(days=3)
# The sweep runs inside the morning brief's request (the platform cuts a request
# at 300 seconds, and every report read may take up to 30). What it does not
# reach in time it lists as not checked, and the next morning carries on.
SWEEP_BUDGET_SECONDS = 60
# From the status poll the site is waiting on our answer, so the repeated
# "paid" call to it gets a shorter leash than the first one.
POLL_SITE_TIMEOUT_SECONDS = 8

STATUS_PENDING = 'pending'
STATUS_COMPLETED = 'completed'
STATUS_FAILED = 'failed'

# Statuses in which the money for the order came in (a refund comes later, by the office).
PAID_STATUSES = ('completed', 'refunded', 'refund_failed')

# What a recheck ended in.
RECHECK_COMPLETED = 'completed'
RECHECK_PENDING = 'pending'          # asked; the report still does not confirm it
RECHECK_PACED = 'paced'              # asked less than RECHECK_INTERVAL ago
RECHECK_NOT_PENDING = 'not_pending'  # settled (or failed) before we got to it
RECHECK_NOT_ELIGIBLE = 'not_eligible'

WHERE_WEBSITE = 'חנות האתר'
WHERE_TILL = 'קופה — עמוד התשלום של טרנזילה'


# ---------------------------------------------------------------------------
# Which invoices this is about
# ---------------------------------------------------------------------------

def _pending_with_transaction():
    """
    Pending store invoices a notify gave a transaction number: hosted-page
    payments (website or till) waiting on the report. A till charge of a
    saved card, or a typed card whose answer did not come, is another matter
    — it never had a hosted page, carries no number, and the till's own
    "uncertain" handling covers it.
    """
    from apps.core.payment_service import TILL_CHARGE_UNCERTAIN_MARK

    return (
        StoreInvoice.objects
        .filter(payment_status='pending', charged_with_token=False)
        .exclude(tranzila_transaction_id='')
        .exclude(tranzila_confirmation_code=TILL_CHARGE_UNCERTAIN_MARK)
    )


def _owed_a_paid_call():
    """
    Paid website orders the site has not acknowledged. Only orders paid through
    Tranzila's page (they carry a transaction number): the retired
    widget/order/ endpoint wrote "completed" orders with no payment behind
    them, and the site is never told those are paid.
    """
    return (
        StoreInvoice.objects
        .filter(payment_status='completed', website_order_number__isnull=False,
                website_paid_notified_at__isnull=True)
        .exclude(website_order_number='')
        .exclude(tranzila_transaction_id='')
    )


def not_rechecked_because(invoice: StoreInvoice) -> str:
    """Why the report cannot settle this pending invoice by itself ('' when it can)."""
    from apps.core.payment_service import TILL_CHARGE_UNCERTAIN_MARK, parse_store_cart_notes
    from apps.core.tranzila_service import TranzilaService

    if invoice.payment_status != 'pending':
        return 'החשבונית כבר אינה ממתינה'
    if not (invoice.tranzila_transaction_id or '').strip():
        return 'לא התקבל מספר עסקה מטרנזילה'
    if invoice.charged_with_token or invoice.tranzila_confirmation_code == TILL_CHARGE_UNCERTAIN_MARK:
        return 'חיוב בקופה שאינו מעמוד התשלום'
    # A transaction number means something only on its own terminal. The
    # report asked is the hosted page's current one; if the page has moved
    # since this payment, that report cannot speak for it.
    current = (TranzilaService.iframe().terminal or '').strip()
    if (invoice.tranzila_terminal or '').strip() != current:
        return (f'התשלום נעשה במסוף {invoice.tranzila_terminal or "(לא ידוע)"}, '
                f'ועמוד התשלום עבר מאז למסוף {current or "(לא מוגדר)"}')
    # Completing sells what the cart holds; without it a sale would be a
    # "completed" invoice with nothing sold and no stock taken.
    if parse_store_cart_notes(invoice.notes) is None:
        return 'בחשבונית לא נשמרה העגלה, ואין מה למכור'
    return ''


def _claim_followup(queryset, invoice_id, min_interval: timedelta) -> bool:
    """
    Take this invoice's follow-up turn, or learn that someone had it less than
    `min_interval` ago. One conditional UPDATE, so two server instances (or
    the poll and the sweep) cannot both take the same turn.
    """
    now = timezone.now()
    return bool(
        queryset.filter(pk=invoice_id)
        .filter(Q(payment_followup_at__isnull=True) | Q(payment_followup_at__lte=now - min_interval))
        .update(payment_followup_at=now)
    )


# ---------------------------------------------------------------------------
# 1. Asking the report again
# ---------------------------------------------------------------------------

def recheck_pending_payment(invoice_id, *, min_interval: timedelta = RECHECK_INTERVAL) -> str:
    """
    Ask Tranzila's report again about ONE pending hosted-page invoice that a
    notify gave a transaction number, and complete it if the report now agrees.

    The completion is the notify's own (complete_store_purchase_from_webhook),
    handed the number and approval code the notify reported and kept on the
    invoice: the invoice row is locked, a completed invoice is never sold
    again, and nothing becomes a sale unless the report shows an approved
    charge of this sum, with this approval number, made after the order and
    not already paying for another. Nothing is charged; the report is read.
    """
    from apps.core.payment_service import PaymentService

    invoice = StoreInvoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        return RECHECK_NOT_ELIGIBLE
    if invoice.payment_status != 'pending':
        return RECHECK_NOT_PENDING
    reason = not_rechecked_because(invoice)
    if reason:
        logger.warning('Store invoice %s not rechecked: %s', invoice.invoice_number, reason)
        alert_if_stuck(invoice, why=reason)
        return RECHECK_NOT_ELIGIBLE
    if not _claim_followup(_pending_with_transaction(), invoice.pk, min_interval):
        return RECHECK_PACED

    result = PaymentService().complete_store_purchase_from_webhook(
        invoice_id=str(invoice.pk),
        tranzila_response={
            # The notify said "approved" — a declined notify fails the invoice
            # and keeps no number — and the report is the judge of it.
            'is_successful': True,
            'transaction_id': invoice.tranzila_transaction_id,
            'confirmation_code': invoice.tranzila_confirmation_code,
        },
    )
    invoice.refresh_from_db(fields=['payment_status'])
    if invoice.payment_status == 'completed':
        logger.info('Store invoice %s completed on recheck', invoice.invoice_number)
        return RECHECK_COMPLETED
    logger.info('Store invoice %s still pending on recheck: %s', invoice.invoice_number, result.get('verdict'))
    return RECHECK_PENDING


# ---------------------------------------------------------------------------
# 2. Telling the website
# ---------------------------------------------------------------------------

def tell_website_paid(invoice: StoreInvoice, *, timeout: Optional[float] = None) -> bool:
    """
    Tell the site this order is paid and keep its answer on the invoice.

    Called right after a sale completes, and again (retry_website_paid) until
    the site answers 2xx. The site's side is idempotent on the order's status;
    each "paid" it accepts also sends the staff's order email, so a repeat is
    made only when the previous call was not acknowledged.
    """
    from apps.store.website_integration import ORDER_STATUS_TIMEOUT_SECONDS, post_website_order_status

    if not invoice.website_order_number:
        return True
    ok, why = post_website_order_status(
        website_order_number=invoice.website_order_number,
        invoice_number=invoice.invoice_number,
        invoice_id=str(invoice.pk),
        status='paid',
        provider_txn_id=invoice.tranzila_transaction_id or '',
        timeout=timeout or ORDER_STATUS_TIMEOUT_SECONDS,
    )
    if ok:
        now = timezone.now()
        StoreInvoice.objects.filter(pk=invoice.pk, website_paid_notified_at__isnull=True).update(
            website_paid_notified_at=now,
        )
        invoice.website_paid_notified_at = invoice.website_paid_notified_at or now
        return True
    logger.error('Website not told that order %s is paid: %s', invoice.website_order_number, why)
    if timezone.now() - invoice.created_at >= STUCK_AFTER:
        alert_website_not_told(invoice, why)
    return False


def retry_website_paid(invoice_id, *, min_interval: timedelta = RECHECK_INTERVAL,
                       timeout: Optional[float] = None) -> bool:
    """Repeat the "paid" call for a paid order the site has not acknowledged. True when it now has."""
    invoice = _owed_a_paid_call().filter(pk=invoice_id).first()
    if invoice is None:
        return StoreInvoice.objects.filter(pk=invoice_id, website_paid_notified_at__isnull=False).exists()
    if not _claim_followup(_owed_a_paid_call(), invoice.pk, min_interval):
        return False
    return tell_website_paid(invoice, timeout=timeout)


# ---------------------------------------------------------------------------
# What the website sees
# ---------------------------------------------------------------------------

def site_status(invoice: StoreInvoice) -> dict:
    """The order as the website reads it: pending | completed | failed, and whether it is paid."""
    paid = invoice.payment_status in PAID_STATUSES
    if paid:
        state = STATUS_COMPLETED
    elif invoice.payment_status == 'failed':
        state = STATUS_FAILED
    else:
        state = STATUS_PENDING
    return {'status': state, 'invoice_number': invoice.invoice_number, 'paid': paid}


def website_order_status(website_order_number: str) -> Optional[dict]:
    """
    The site's poll for one order. A pending payment with a transaction number
    is asked about again (paced); a paid order the site never acknowledged is
    told again. None when there is no such order.
    """
    invoice = StoreInvoice.objects.filter(website_order_number=website_order_number).first()
    if invoice is None:
        return None
    try:
        if invoice.payment_status == 'pending' and (invoice.tranzila_transaction_id or '').strip():
            recheck_pending_payment(invoice.pk)
            invoice.refresh_from_db()
        elif invoice.payment_status == 'completed' and invoice.website_paid_notified_at is None:
            retry_website_paid(invoice.pk, timeout=POLL_SITE_TIMEOUT_SECONDS)
    except Exception:
        # The poll always gets the stored answer; the sweep tries again.
        logger.exception('Store order %s: follow-up from the status poll failed', website_order_number)
        invoice.refresh_from_db()
    return site_status(invoice)


# ---------------------------------------------------------------------------
# 3. The morning sweep
# ---------------------------------------------------------------------------

def sweep_stuck_store_payments(*, budget_seconds: float = SWEEP_BUDGET_SECONDS) -> dict:
    """
    Every hosted-page payment of the last SWEEP_WINDOW that is still pending
    with a transaction number, and every paid website order the site has not
    acknowledged: settle what the report (or the site) now allows, and return
    what is left for a person. Never charges; never raises for one invoice.

    Returns lists of invoices: 'settled' (completed now), 'still_pending'
    (with the reason), 'site_told', 'site_not_told', 'not_reached' (the
    budget ran out before them).
    """
    started = time.monotonic()
    since = timezone.now() - SWEEP_WINDOW
    result = {'settled': [], 'still_pending': [], 'site_told': [], 'site_not_told': [], 'not_reached': []}

    def out_of_time() -> bool:
        return time.monotonic() - started > budget_seconds

    for invoice in _pending_with_transaction().filter(created_at__gte=since).order_by('created_at'):
        if out_of_time():
            result['not_reached'].append(invoice)
            continue
        try:
            outcome = recheck_pending_payment(invoice.pk)
        except Exception as exc:  # noqa: BLE001 — one invoice never stops the rest
            logger.exception('Store sweep: recheck of %s failed', invoice.invoice_number)
            outcome = f'error: {exc}'
        invoice.refresh_from_db()
        if invoice.payment_status == 'completed':
            result['settled'].append(invoice)
            continue
        if invoice.payment_status != 'pending':
            continue
        reason = not_rechecked_because(invoice) or {
            RECHECK_PACED: 'נבדק ממש עכשיו מול טרנזילה, ועדיין לא אושר',
            RECHECK_PENDING: 'הדוח של טרנזילה עדיין לא מאשר את העסקה',
        }.get(outcome, str(outcome))
        alert_if_stuck(invoice, why=reason)
        result['still_pending'].append((invoice, reason))

    for invoice in _owed_a_paid_call().filter(created_at__gte=since).order_by('created_at'):
        if out_of_time():
            result['not_reached'].append(invoice)
            continue
        try:
            told = retry_website_paid(invoice.pk)
        except Exception:  # noqa: BLE001
            logger.exception('Store sweep: telling the site about %s failed', invoice.invoice_number)
            told = False
        (result['site_told'] if told else result['site_not_told']).append(invoice)
    return result


# ---------------------------------------------------------------------------
# The office
# ---------------------------------------------------------------------------

def _where(invoice: StoreInvoice, step: str) -> str:
    return f'{WHERE_WEBSITE if invoice.website_order_number else WHERE_TILL} — {step}'


def describe_store_customer(invoice: StoreInvoice) -> str:
    """One line: who bought, how to reach them, the sum, the order and the invoice."""
    from apps.core.office_alerts import describe_family

    parts = []
    child = invoice.child if invoice.child_id else None
    if child is not None:
        parts.append(describe_family(child.family, children=[child]))
    else:
        parts.append(f'לקוח: {invoice.customer_name or "לקוח מזדמן"}'
                     + (f' {invoice.customer_phone}' if invoice.customer_phone else ''))
    if invoice.customer_email:
        parts.append(f'מייל: {invoice.customer_email}')
    parts.append(f'סכום: ₪{invoice.total_amount}')
    if invoice.website_order_number:
        parts.append(f'הזמנה באתר: {invoice.website_order_number}')
    parts.append(f'חשבונית: {invoice.invoice_number}')
    return ' · '.join(part for part in parts if part)


def _link(invoice: StoreInvoice) -> str:
    from apps.core.office_alerts import crm_child_link

    if invoice.child_id:
        return crm_child_link(invoice.child_id)
    base = (getattr(settings, 'CRM_FRONTEND_URL', '') or '').strip().rstrip('/')
    return f'{base}/invoices' if base else ''


def _alert(invoice: StoreInvoice, *, kind: str, key: str, title: str, step: str, what: str,
           why: str = '', action: str = '') -> None:
    from apps.core.office_alerts import raise_office_alert

    try:
        customer = describe_store_customer(invoice)
    except Exception:  # noqa: BLE001 — an alert goes out even without its customer line
        logger.exception('Store alert %s: customer not described', key)
        customer = f'חשבונית {invoice.invoice_number}'
    raise_office_alert(
        kind=kind, dedup_key=key, title=title, where=_where(invoice, step), what=what, why=why,
        customer=customer, action=action, link=_link(invoice),
        details={'invoice_id': str(invoice.pk), 'invoice_number': invoice.invoice_number,
                 'website_order_number': invoice.website_order_number or '',
                 'transaction': invoice.tranzila_transaction_id or '',
                 'terminal': invoice.tranzila_terminal or ''},
    )


def _check_in_tranzila(invoice: StoreInvoice) -> str:
    # The template's "action" line holds 300 characters; this stays well inside.
    return (
        f'לבדוק בטרנזילה (מסוף {invoice.tranzila_terminal or "עמוד התשלום"}) את עסקה '
        f'{invoice.tranzila_transaction_id or "(ללא מספר)"} על ₪{invoice.total_amount}'
        + (f', אישור {invoice.tranzila_confirmation_code}' if invoice.tranzila_confirmation_code else '')
        + '. אושרה — הלקוח שילם: לא לבקש תשלום שוב; המערכת תשלים לבד כשהדוח יאשר, ואם לא — '
          'להעביר לבדיקה טכנית. לא אושרה — לחזור ללקוח.'
    )


def alert_payment_unverified(invoice: StoreInvoice, report_row: Optional[dict] = None) -> None:
    """The report disagrees with the notify: nothing was sold. At once, once per invoice."""
    from apps.core.tranzila_service import report_transaction_amount

    if report_row:
        try:
            seen = f'בדוח העסקה מופיעה על סך ₪{report_transaction_amount(report_row)}, tranmode {report_row.get("tranmode") or "?"}.'
        except Exception:  # noqa: BLE001
            seen = 'העסקה נמצאה בדוח אבל לא תאמה.'
    else:
        seen = 'העסקה לא נמצאה בדוח של המסוף.'
    _alert(
        invoice, kind='store_payment_unverified', key=f'store_payment_review:{invoice.pk}',
        title='תשלום בחנות שלא תאם לטרנזילה — ההזמנה לא הושלמה',
        step='אישור התשלום מול הדוח של טרנזילה',
        what=(f'הגיעה הודעה שהתשלום אושר (עסקה {invoice.tranzila_transaction_id or "?"}), '
              f'אבל הדוח של טרנזילה לא מאשר אותה. {seen} '
              'לא נמכר דבר, המלאי לא ירד ולא הופק מסמך. החשבונית ממתינה.'),
        why=('הסכום, מספר האישור, סוג העסקה או מועד העסקה בדוח שונים ממה שנשלח, '
             'או שהמספר כבר שייך להזמנה אחרת. ייתכן גם שהדוח עוד לא התעדכן — המערכת תבדוק שוב.'),
        action=_check_in_tranzila(invoice),
    )


def alert_if_stuck(invoice: StoreInvoice, *, why: str = '') -> None:
    """A pending payment with a transaction number, older than STUCK_AFTER. Once per invoice."""
    if invoice.payment_status != 'pending' or not (invoice.tranzila_transaction_id or '').strip():
        return
    if timezone.now() - invoice.created_at < STUCK_AFTER:
        return
    minutes = int((timezone.now() - invoice.created_at).total_seconds() // 60)
    _alert(
        invoice, kind='store_payment_stuck', key=f'store_payment_review:{invoice.pk}',
        title='תשלום בחנות תקוע — לא ידוע אם הלקוח שילם',
        step='אישור התשלום מול הדוח של טרנזילה',
        what=(f'ההזמנה נפתחה לפני {minutes} דקות וטרנזילה דיווחה על עסקה {invoice.tranzila_transaction_id}, '
              'אבל התשלום עדיין לא אומת מול הדוח ולכן ההזמנה לא הושלמה: לא נמכר דבר, המלאי לא ירד '
              'ולא הופק מסמך.'),
        why=why or 'הדוח של טרנזילה לא ענה או עדיין לא מאשר את העסקה.',
        action=_check_in_tranzila(invoice),
    )


def alert_website_not_told(invoice: StoreInvoice, why: str) -> None:
    """A paid order the site has not acknowledged after STUCK_AFTER. Once per invoice."""
    _alert(
        invoice, kind='store_website_not_told', key=f'store_website_not_told:{invoice.pk}',
        title='האתר לא יודע שהזמנה שולמה',
        step='עדכון האתר שההזמנה שולמה',
        what=(f'הזמנה {invoice.website_order_number} שולמה ונרשמה ב-CRM (חשבונית {invoice.invoice_number}, '
              f'₪{invoice.total_amount}), אבל האתר לא אישר שקיבל את העדכון. באתר היא עדיין "ממתינה לתשלום", '
              'וייתכן שמייל ההזמנה לצוות לא יצא.'),
        why=why,
        action=('לטפל בהזמנה מתוך ה-CRM (המכירה, המלאי והמסמך כבר נרשמו). לא לבקש מהלקוח לשלם שוב. '
                'המערכת תנסה לעדכן את האתר שוב בבדיקת הבוקר.'),
    )


def alert_oversold(invoice: StoreInvoice, lines: list[dict]) -> None:
    """A paid sale that took more units than the shelf had. The sale is kept."""
    described = '; '.join(
        f"{line['name']}{(' מידה ' + line['size']) if line.get('size') else ''} — הוזמנו {line['quantity']}, "
        f"היו במלאי {line['available']}"
        for line in lines
    )
    _alert(
        invoice, kind='store_oversold', key=f'store_oversold:{invoice.pk}',
        title='נמכר מעבר למלאי בחנות',
        step='רישום המכירה אחרי התשלום',
        what=f'ההזמנה שולמה והמכירה נרשמה, אבל במלאי לא היו מספיק יחידות: {described}.',
        why='המלאי השתנה בין פתיחת עמוד התשלום לבין התשלום (קנייה אחרת, או עדכון מלאי ידני).',
        action=('לבדוק את המלאי בפועל. אם המוצר חסר — לחזור ללקוח ולהציע החלפה או זיכוי. '
                'המכירה נשמרה כמו שהיא; לא לחייב שוב.'),
    )


def alert_payment_page_failed(invoice: StoreInvoice, error: str, terminal: str) -> None:
    """Tranzila's page could not be opened for a website order. Once a day."""
    _alert(
        invoice, kind='store_page_failed', key=f'store_page_failed:{timezone.localdate().isoformat()}',
        title='עמוד התשלום של החנות באתר לא נפתח',
        step='פתיחת עמוד התשלום (handshake מול טרנזילה)',
        what=('לקוח הגיע לתשלום באתר ועמוד טרנזילה לא נפתח. הלקוח קיבל הודעה שהתשלום אינו זמין כרגע; '
              'לא ירד כסף וההזמנה סומנה כנכשלה, כך שאפשר לנסות שוב. הלקוח המופיע כאן הוא הראשון היום.'),
        why=str(error)[:300],
        action=(f'לבדוק את מסוף {terminal or "(לא מוגדר)"} ואת המפתחות שלו. אם זה חוזר — לפנות לטרנזילה. '
                'ההתראה הזאת נשלחת פעם אחת ביום.'),
    )
