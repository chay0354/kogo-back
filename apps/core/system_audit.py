"""
The weekly system audit: a different part of the system every day, all of it
every week.

Every button on the office's screens calls a route on this server. Each day
the audit takes one area and calls every read route in it, the way a manager
clicking through would — inside a transaction that is always rolled back, so
nothing any of them does is kept. A route that falls over (a 5xx, an exception)
or answers slowly is a finding the office would otherwise have met at the worst
moment. Each area also has deeper checks of its own: the gateway's terminals,
ManyChat's automations, the document series, the price of every lesson.

Rules:
  * Nothing here charges, sends or keeps a change. Routes that talk to another
    company (Tranzila, ManyChat), that start a job (cron, backup, sync) or that
    produce a file are never called by the sweep; the area's own checks cover
    those, read-only.
  * Every API route belongs to exactly one day — a test holds that line, so a
    route added next month is covered without anyone remembering to add it.
  * The sweep is resumable. It works in slices sized to fit one request and
    picks up where it stopped, so the hosting platform's limit on how long a
    request may take never costs a whole day's audit.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date

from django.apps import apps as django_apps
from django.db import transaction
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone

logger = logging.getLogger(__name__)

# A read route slower than this is worth knowing about: it is a screen that
# makes someone wait.
SLOW_MS = 3000

# Words that mark a route the sweep must never call: it talks to another
# company, starts a job, or produces a file. Their areas check them read-only.
NEVER_CALL = (
    'cron', 'webhook', 'callback', 'backup', 'devops', 'export', 'download',
    'pdf', 'print', 'zip', 'csv', 'xlsx', 'send', 'email', 'sync', 'whatsapp',
    'tranzila', 'daily-brief', 'system-audit', 'env-info', 'handshake',
)


@dataclass(frozen=True)
class Area:
    key: str
    title: str
    # Route prefixes (after api/v1/) that belong to this area. The first area
    # whose prefix matches wins, so specific prefixes are listed before the
    # general ones they sit under.
    prefixes: tuple
    # Israel weekday: Sunday = 0 … Saturday = 6.
    day: int


AREAS = (
    Area('billing', 'גבייה וסליקה', (
        'customers/payments', 'customers/recurring', 'customers/card', 'customers/cron',
        'payment-links/', 'core/credit-cards', 'core/tranzila',
    ), 0),
    Area('registration', 'הרשמה, ווידג׳ט וחוגים', (
        'customers/widget', 'enrollments/', 'courses/',
    ), 1),
    Area('documents', 'מסמכים וחשבוניות', (
        'documents/', 'customers/invoices', 'customers/credit', 'customers/business-customers',
    ), 2),
    Area('messages', 'הודעות, חתימות ואוטומציות', (
        'core/whatsapp', 'signatures/', 'core/registration-terms',
    ), 3),
    Area('customers', 'לקוחות ונתונים', (
        'customers/', 'external-students/', 'legacy-import/',
    ), 4),
    Area('staff', 'מדריכים, שיעורים ומשתמשים', (
        'instructors/', 'scheduling/', 'core/',
    ), 5),
    Area('rentals_store', 'השכרות וחנות', (
        'rentals/', 'rental-billing/', 'store/',
    ), 6),
)

AREA_BY_KEY = {area.key: area for area in AREAS}


def israel_weekday(day: date) -> int:
    """Sunday = 0 … Saturday = 6 — the week as the office counts it."""
    return (day.weekday() + 1) % 7


def area_for_day(day: date) -> Area:
    weekday = israel_weekday(day)
    return next(area for area in AREAS if area.day == weekday)


# --- the routes ----------------------------------------------------------------

@dataclass
class Route:
    template: str           # api/v1/customers/children/{pk}/
    params: tuple           # ('pk',)
    area: str
    callable_get: bool
    never_call: bool


_PARAM = re.compile(r'\(\?P<(\w+)>[^)]*\)|<(?:\w+:)?(\w+)>')


def _normalise(raw: str) -> str:
    """'api/v1/customers/^children/(?P<pk>[^/.]+)/$' -> 'api/v1/customers/children/{pk}/'."""
    text = raw.replace('^', '').replace('$', '')
    return _PARAM.sub(lambda m: '{' + (m.group(1) or m.group(2)) + '}', text)


def _has_get(callback) -> bool:
    actions = getattr(callback, 'actions', None)
    if actions:
        return 'get' in actions
    cls = getattr(callback, 'view_class', None) or getattr(callback, 'cls', None)
    if cls is not None:
        allowed = [m.lower() for m in getattr(cls, 'http_method_names', [])]
        return hasattr(cls, 'get') and ('get' in allowed if allowed else True)
    return False


def area_of(template: str) -> str:
    tail = template[len('api/v1/'):]
    for area in AREAS:
        if any(tail.startswith(prefix) for prefix in area.prefixes):
            return area.key
    return ''


def all_routes() -> list[Route]:
    """Every API route, in a stable order, with the area it belongs to."""
    found: list[tuple[str, object]] = []

    def walk(patterns, prefix=''):
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns, prefix + str(pattern.pattern))
            elif isinstance(pattern, URLPattern):
                found.append((prefix + str(pattern.pattern), pattern.callback))

    walk(get_resolver().url_patterns)
    routes: dict[str, Route] = {}
    for raw, callback in found:
        if not raw.startswith('api/v1/') or '(?P<format>' in raw:
            continue
        template = _normalise(raw)
        if template in routes:
            # The same route registered twice (a trailing-slash twin): one entry.
            routes[template].callable_get = routes[template].callable_get or _has_get(callback)
            continue
        params = tuple(m.group(1) or m.group(2) for m in _PARAM.finditer(raw))
        routes[template] = Route(
            template=template,
            params=params,
            area=area_of(template),
            callable_get=_has_get(callback),
            never_call=any(word in template for word in NEVER_CALL),
        )
    return sorted(routes.values(), key=lambda r: r.template)


# --- filling in the ids a route needs ------------------------------------------

def _model_for_param(name: str):
    """'child_id' -> Child, 'family_pk' -> Family. None when nothing fits."""
    base = re.sub(r'_(id|pk|uuid)$', '', name).replace('_', '').lower()
    if not base or base in ('pk', 'id'):
        return None
    for model in django_apps.get_models():
        if model.__name__.lower() == base:
            return model
    return None


def _first_id(model) -> str | None:
    try:
        value = model.objects.order_by('-pk').values_list('pk', flat=True).first()
    except Exception:  # noqa: BLE001 — a model we cannot read is simply not sampled
        return None
    return str(value) if value is not None else None


# --- calling a route -------------------------------------------------------------

@dataclass
class Outcome:
    path: str
    status: int | None = None
    ms: int = 0
    error: str = ''
    skipped: str = ''


def _manager_user():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return (
        User.objects.filter(is_active=True, profile__role='manager').order_by('pk').first()
        or User.objects.filter(is_active=True, is_superuser=True).order_by('pk').first()
    )


def _host() -> str:
    """A host this server accepts, so paginated routes can build their links."""
    from django.conf import settings

    for host in getattr(settings, 'ALLOWED_HOSTS', []) or []:
        host = str(host).strip()
        if host and host != '*' and not host.startswith('.'):
            return host
    return 'localhost'


def _request(path: str, user):
    """A GET the way the browser sends it: to a real host, as a signed-in manager."""
    from django.urls import resolve
    from rest_framework.test import APIRequestFactory, force_authenticate

    match = resolve('/' + path)
    request = APIRequestFactory().get('/' + path, HTTP_HOST=_host())
    # Router index pages reverse their own names and need to know where they are.
    request.resolver_match = match
    force_authenticate(request, user=user)
    return match, request


def _call(path: str, user) -> Outcome:
    """GET a route as a manager, inside a transaction that is always rolled back."""
    outcome = Outcome(path=path)
    started = time.monotonic()
    try:
        match, request = _request(path, user)
        with transaction.atomic():
            response = match.func(request, *match.args, **match.kwargs)
            if hasattr(response, 'render') and not getattr(response, 'is_rendered', True):
                response.render()
            outcome.status = response.status_code
            # Whatever the route did, it is not kept.
            transaction.set_rollback(True)
    except Exception as exc:  # noqa: BLE001 — exactly what the sweep is looking for
        outcome.error = f'{type(exc).__name__}: {exc}'[:300]
    outcome.ms = int((time.monotonic() - started) * 1000)
    return outcome


def _pk_from_list(list_path: str, user) -> str | None:
    """The id of the first row a list route returns — what "open a row" would click."""
    try:
        match, request = _request(list_path, user)
        with transaction.atomic():
            response = match.func(request, *match.args, **match.kwargs)
            data = getattr(response, 'data', None)
            transaction.set_rollback(True)
    except Exception:  # noqa: BLE001
        return None
    rows = data.get('results') if isinstance(data, dict) else data
    if isinstance(rows, list) and rows and isinstance(rows[0], dict) and rows[0].get('id') is not None:
        return str(rows[0]['id'])
    return None


def concrete_path(route: Route, user) -> tuple[str | None, str]:
    """Fill in a route's ids from real rows. (None, reason) when it cannot be done."""
    path = route.template
    for name in route.params:
        if name in ('pk', 'id'):
            list_path = route.template.split('{' + name + '}')[0]
            value = _pk_from_list(list_path, user)
        else:
            model = _model_for_param(name)
            value = _first_id(model) if model else None
        if value is None:
            return None, f'אין ערך לדוגמה ל-{name}'
        path = path.replace('{' + name + '}', value)
    return path, ''


@dataclass
class SliceResult:
    outcomes: list = field(default_factory=list)
    next_index: int = 0
    finished: bool = False


def sweep_slice(area_key: str, start_index: int, *, budget_seconds: float) -> SliceResult:
    """
    Call the area's read routes from `start_index` until the time runs out.

    Returns where to start next time; `finished` once the last route is done.
    """
    routes = [r for r in all_routes() if r.area == area_key and r.callable_get]
    result = SliceResult(next_index=start_index)
    user = _manager_user()
    if user is None:
        result.outcomes.append(Outcome(path='—', skipped='אין משתמש מנהל פעיל שאפשר לבדוק בשמו'))
        result.finished = True
        return result

    deadline = time.monotonic() + budget_seconds
    index = start_index
    while index < len(routes) and time.monotonic() < deadline:
        route = routes[index]
        index += 1
        if route.never_call:
            result.outcomes.append(Outcome(path=route.template, skipped='פונה לשירות חיצוני או מפעיל פעולה — נבדק בבדיקות האזור'))
            continue
        path, reason = concrete_path(route, user)
        if path is None:
            result.outcomes.append(Outcome(path=route.template, skipped=reason))
            continue
        result.outcomes.append(_call(path, user))
    result.next_index = index
    result.finished = index >= len(routes)
    return result


def routes_in_area(area_key: str) -> int:
    return sum(1 for r in all_routes() if r.area == area_key and r.callable_get)


# --- each area's own checks -------------------------------------------------------
#
# The sweep proves the screens answer. These go behind them: the gateway, the
# automations, the numbering, the data the screens are built from. All of them
# read; none of them charges, sends or writes.

@dataclass
class ProbeResult:
    name: str
    title: str
    severity: str           # 'red' | 'yellow' | 'green'
    summary: str
    rows: list = field(default_factory=list)


def _rows(items, label, detail, limit=15):
    return [{'label': label(item), 'detail': detail(item)} for item in list(items)[:limit]]


def probe_tranzila_terminals() -> ProbeResult:
    """Every terminal the system charges through answers a handshake."""
    from apps.core.tranzila_service import TranzilaService

    problems = []
    for name, service in (('תשלום באתר', TranzilaService.iframe()), ('גבייה בטוקן', TranzilaService.production())):
        report = service.live_readiness()
        for check in report.get('checks') or []:
            if check.get('blocking') and not check.get('ok'):
                problems.append({'label': f"{name} · {check.get('name')}", 'detail': str(check.get('detail') or '')})
    if problems:
        return ProbeResult('tranzila_terminals', 'מסופי טרנזילה', 'red',
                           f'{len(problems)} בדיקות חוסמות נכשלו במסופים — חיוב עלול להיכשל.', problems)
    return ProbeResult('tranzila_terminals', 'מסופי טרנזילה', 'green', 'שני המסופים ענו ללחיצת יד תקינה.')


def probe_recurring_integrity() -> ProbeResult:
    """Standing orders whose own fields make them uncollectable."""
    from django.db.models import Q

    from apps.customers.models import RecurringPayment

    bad = (
        RecurringPayment.objects.filter(status='active')
        .filter(Q(next_billing_date__isnull=True) | Q(amount__lte=0))
        .select_related('child')
    )
    count = bad.count()
    if not count:
        return ProbeResult('recurring_integrity', 'שלמות הוראות הקבע', 'green',
                           'לכל הוראת קבע פעילה יש סכום ותאריך חיוב הבא.')
    return ProbeResult(
        'recurring_integrity', 'שלמות הוראות הקבע', 'red',
        f'{count} הוראות קבע פעילות בלי תאריך חיוב הבא או עם סכום אפס — הגבייה לא תיגע בהן.',
        _rows(bad, lambda r: r.child.full_name if r.child else str(r.id),
              lambda r: f'סכום {r.amount} · חיוב הבא {r.next_billing_date or "—"}'),
    )


def probe_payment_links() -> ProbeResult:
    """An active payment link has to land money on a live business and category."""
    from django.db.models import Q

    from apps.payment_links.models import PaymentLink

    broken = (
        PaymentLink.objects.filter(is_active=True)
        .filter(Q(business__isnull=True) | Q(business__is_active=False)
                | Q(business_category__isnull=True) | Q(business_category__is_active=False))
    )
    count = broken.count()
    if not count:
        return ProbeResult('payment_links', 'קישורי תשלום', 'green', 'כל קישור פעיל מוביל לעסק ולקטגוריה פעילים.')
    return ProbeResult('payment_links', 'קישורי תשלום', 'red',
                       f'{count} קישורי תשלום פעילים מפנים לעסק או לקטגוריה שאינם פעילים.',
                       _rows(broken, lambda l: l.title or l.slug, lambda l: f'/{l.slug}'))


def probe_lessons_priced() -> ProbeResult:
    """Every lesson the widget offers has a price to charge."""
    from django.db.models import Q

    from apps.courses.models import Lesson

    unpriced = (
        Lesson.objects.filter(status='scheduled', course__is_active=True, course__show_in_widget=True)
        .filter(Q(course__price__isnull=True) | Q(course__price__lte=0))
        .filter(Q(lesson_price_override__isnull=True) | Q(lesson_price_override__lte=0))
        .select_related('course', 'course__branch')
    )
    count = unpriced.count()
    if not count:
        return ProbeResult('lessons_priced', 'מחיר לכל שיעור בווידג׳ט', 'green', 'לכל שיעור שמוצג בווידג׳ט יש מחיר.')
    return ProbeResult('lessons_priced', 'מחיר לכל שיעור בווידג׳ט', 'red',
                       f'{count} שיעורים מוצגים בווידג׳ט בלי מחיר — הרשמה אליהם תיכשל או תיגבה אפס.',
                       _rows(unpriced, lambda l: l.course.name,
                             lambda l: getattr(l.course.branch, 'name', '') or ''))


def probe_lessons_staffed() -> ProbeResult:
    """A scheduled lesson with no instructor is a class with nobody to teach it."""
    from apps.courses.models import Lesson

    rows = (
        Lesson.objects.filter(status='scheduled', instructor__isnull=True, course__is_active=True)
        .select_related('course', 'course__branch')
    )
    count = rows.count()
    if not count:
        return ProbeResult('lessons_staffed', 'מדריך לכל שיעור', 'green', 'לכל שיעור פעיל משויך מדריך.')
    return ProbeResult('lessons_staffed', 'מדריך לכל שיעור', 'yellow',
                       f'{count} שיעורים פעילים בלי מדריך משויך.',
                       _rows(rows, lambda l: l.course.name, lambda l: getattr(l.course.branch, 'name', '') or ''))


def probe_document_series() -> ProbeResult:
    """This year's document runs exist and can hand out their next number."""
    from apps.documents.missing_receipts import next_receipt_number

    try:
        number = next_receipt_number()
    except Exception as exc:  # noqa: BLE001
        return ProbeResult('document_series', 'מספור מסמכים', 'red',
                           'לא ניתן לחשב את מספר הקבלה הבא — הפקת מסמכים תיכשל.',
                           [{'label': 'מספור', 'detail': str(exc)[:200]}])
    return ProbeResult('document_series', 'מספור מסמכים', 'green', f'הקבלה הבאה תקבל מספר {number}.')


# The User Fields the templates are filled from. Missing in ManyChat, a message
# goes out with a blank where the child's name or the lesson time should be.
REQUIRED_MANYCHAT_FIELDS = (
    'kogo_parent_name', 'kogo_child_name', 'kogo_course_name', 'kogo_branch_name',
    'kogo_lesson_day', 'kogo_lesson_time', 'kogo_location', 'kogo_card_update_url',
)


def probe_manychat_automations() -> ProbeResult:
    """Every automation the system sends through exists, and so do its fields."""
    from apps.core.manychat_service import ManyChatError, ManyChatService

    service = ManyChatService()
    if not service.is_configured:
        return ProbeResult('manychat', 'אוטומציות ManyChat', 'red', 'ManyChat אינו מוגדר בשרת — שום הודעה לא תצא.')
    problems = []
    try:
        flows = {str(f.get('ns') or '') for f in service.get_flows() or []}
        for kind, entry in service._REGISTRATION_KINDS.items():
            ns = service.resolve_flow_for(entry)
            if not ns:
                problems.append({'label': f'אוטומציה · {kind}', 'detail': 'לא הוגדרה — ההודעה תצא כטקסט חופשי'})
            elif ns not in flows:
                problems.append({'label': f'אוטומציה · {kind}', 'detail': f'{ns} לא קיימת ב-ManyChat'})
        names = {str(f.get('name') or '') for f in service.list_custom_fields()}
        for field_name in REQUIRED_MANYCHAT_FIELDS:
            if field_name not in names:
                problems.append({'label': f'שדה · {field_name}', 'detail': 'חסר ב-ManyChat — יופיע ריק בהודעה'})
    except ManyChatError as exc:
        return ProbeResult('manychat', 'אוטומציות ManyChat', 'red', 'ManyChat לא ענה.', [{'label': 'ManyChat', 'detail': str(exc)[:200]}])
    if problems:
        return ProbeResult('manychat', 'אוטומציות ManyChat', 'yellow',
                           f'{len(problems)} אוטומציות או שדות חסרים.', problems)
    return ProbeResult('manychat', 'אוטומציות ManyChat', 'green', 'כל האוטומציות והשדות שהמערכת משתמשת בהם קיימים.')


def probe_email_backend() -> ProbeResult:
    """The mail server accepts a connection. Nothing is sent."""
    from django.conf import settings
    from django.core.mail import get_connection

    backend = getattr(settings, 'EMAIL_BACKEND', '')
    if 'console' in backend or 'locmem' in backend or 'dummy' in backend:
        return ProbeResult('email', 'שליחת מיילים', 'red',
                           'השרת מוגדר לא לשלוח מיילים באמת — חשבוניות לא יגיעו ללקוחות.',
                           [{'label': 'EMAIL_BACKEND', 'detail': backend}])
    try:
        connection = get_connection(fail_silently=False)
        connection.open()
        connection.close()
    except Exception as exc:  # noqa: BLE001
        return ProbeResult('email', 'שליחת מיילים', 'red', 'שרת המייל לא מקבל חיבור.',
                           [{'label': 'מייל', 'detail': str(exc)[:200]}])
    return ProbeResult('email', 'שליחת מיילים', 'green', 'שרת המייל מקבל חיבור.')


def probe_family_contacts() -> ProbeResult:
    """A family nobody can reach: no phone on the family and none on a parent."""
    from django.db.models import Q

    from apps.customers.models import Family

    unreachable = (
        Family.objects.filter(Q(phone='') | Q(phone__isnull=True))
        .exclude(parents__phone__gt='')
        .filter(children__status__in=('active', 'trial_signed', 'payment_problem'))
        .distinct()
    )
    count = unreachable.count()
    if not count:
        return ProbeResult('family_contacts', 'דרך ליצור קשר', 'green', 'לכל משפחה פעילה יש טלפון.')
    return ProbeResult('family_contacts', 'דרך ליצור קשר', 'yellow',
                       f'{count} משפחות פעילות בלי אף טלפון — לא יקבלו WhatsApp.',
                       _rows(unreachable, lambda f: f.name, lambda f: f.email or 'ללא מייל'))


def probe_active_managers() -> ProbeResult:
    """Someone can still sign in and run the office."""
    from django.contrib.auth import get_user_model

    count = get_user_model().objects.filter(is_active=True, profile__role='manager').count()
    if count:
        return ProbeResult('managers', 'מנהלים פעילים', 'green', f'{count} מנהלים פעילים יכולים להתחבר.')
    return ProbeResult('managers', 'מנהלים פעילים', 'red', 'אין אף מנהל פעיל — אף אחד לא יכול לנהל את המערכת.')


def probe_rental_orders() -> ProbeResult:
    """An active rental standing order with no card cannot be charged."""
    from apps.rental_billing.models import TenantStandingOrder

    rows = TenantStandingOrder.objects.filter(status='active', tranzila_token='').select_related('tenant')
    count = rows.count()
    if not count:
        return ProbeResult('rental_orders', 'הוראות קבע של השכרות', 'green', 'לכל הוראת קבע פעילה של השכרה יש כרטיס.')
    return ProbeResult('rental_orders', 'הוראות קבע של השכרות', 'red',
                       f'{count} הוראות קבע פעילות של השכרה בלי כרטיס שמור.',
                       _rows(rows, lambda o: str(getattr(o, 'tenant', '') or o.id), lambda o: f'{o.amount_before_vat} ₪ לפני מע״מ'))


PROBES = {
    'billing': (probe_tranzila_terminals, probe_recurring_integrity, probe_payment_links),
    'registration': (probe_lessons_priced,),
    'documents': (probe_document_series,),
    'messages': (probe_manychat_automations, probe_email_backend),
    'customers': (probe_family_contacts,),
    'staff': (probe_lessons_staffed, probe_active_managers),
    'rentals_store': (probe_rental_orders,),
}


def run_probes(area_key: str) -> list[dict]:
    """The area's checks. A probe that breaks is itself a red finding."""
    results = []
    for probe in PROBES.get(area_key, ()):
        try:
            result = probe()
        except Exception as exc:  # noqa: BLE001
            logger.exception('audit probe failed: %s', probe.__name__)
            result = ProbeResult(probe.__name__, probe.__name__, 'red', f'הבדיקה עצמה נכשלה: {exc}'[:300])
        results.append({
            'name': result.name, 'title': result.title, 'severity': result.severity,
            'summary': result.summary, 'rows': result.rows,
        })
    return results


# --- the day's run, a slice at a time -----------------------------------------------

def _today() -> date:
    return timezone.localtime(timezone.now()).date()


def advance_audit(*, budget_seconds: float = 25, today: date | None = None):
    """
    Move today's audit forward by as much as fits in the budget, and save.

    Safe to call as often as the cron likes: a finished day returns at once,
    and two calls at the same moment do not both work — the second sees the
    first one's lease and steps aside.
    """
    from datetime import timedelta

    from django.db.models import Q

    from apps.core.models import SystemAuditRun

    day = today or _today()
    area = area_for_day(day)
    run, _ = SystemAuditRun.objects.get_or_create(
        day=day,
        defaults={'area': area.key, 'total_routes': routes_in_area(area.key)},
    )
    if run.finished_at:
        return run

    now = timezone.now()
    taken = (
        SystemAuditRun.objects
        .filter(pk=run.pk)
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now))
        .update(lease_until=now + timedelta(seconds=budget_seconds + 30))
    )
    if not taken:
        return run

    started = time.monotonic()
    try:
        if run.next_index < run.total_routes:
            result = sweep_slice(run.area, run.next_index, budget_seconds=budget_seconds)
            for outcome in result.outcomes:
                if outcome.skipped:
                    run.skipped = [*run.skipped, {'path': outcome.path, 'reason': outcome.skipped}]
                    continue
                run.called += 1
                if outcome.error or (outcome.status and outcome.status >= 500):
                    run.failures = [*run.failures, {
                        'path': outcome.path, 'status': outcome.status, 'error': outcome.error,
                    }]
                elif outcome.ms > SLOW_MS:
                    run.slow = [*run.slow, {'path': outcome.path, 'ms': outcome.ms}]
            run.next_index = result.next_index
            run.save()

        remaining = budget_seconds - (time.monotonic() - started)
        if run.next_index >= run.total_routes and not run.probes_done and remaining > 5:
            run.probes = run_probes(run.area)
            run.probes_done = True
            run.finished_at = timezone.now()
            run.save()
    finally:
        SystemAuditRun.objects.filter(pk=run.pk).update(lease_until=None)
    run.refresh_from_db()
    return run


def run_summary(run) -> dict:
    """How a day reads on the screen."""
    area = AREA_BY_KEY.get(run.area)
    red_probes = [p for p in run.probes if p.get('severity') == 'red']
    yellow_probes = [p for p in run.probes if p.get('severity') == 'yellow']
    if run.failures or red_probes:
        verdict = 'red'
    elif run.slow or yellow_probes:
        verdict = 'yellow'
    elif run.finished_at:
        verdict = 'green'
    else:
        verdict = 'running'
    return {
        'day': run.day.isoformat(),
        'area': run.area,
        'title': area.title if area else run.area,
        'verdict': verdict,
        'total_routes': run.total_routes,
        'checked_routes': run.next_index,
        'called': run.called,
        'failures': run.failures,
        'slow': run.slow,
        'skipped_count': len(run.skipped),
        'probes': run.probes,
        'finished_at': run.finished_at.isoformat() if run.finished_at else None,
    }
