"""
The registration form recognises a returning parent.

A parent types an identity number and a mobile phone. When both are the ones
on a family's card, the form is told the children's first names and is given
the rest of the stored details hidden — one character of each — so it can show
them as filled without the browser ever holding them. Registration then sends
back the token it was handed, and the server completes the hidden details from
the card itself (`fill_from_identification`).

An identity number and a phone are not a password, so what is shown is kept
small and the ways to abuse it are closed one by one:

  * off unless WIDGET_IDENTIFICATION_ENABLED;
  * only a family whose parent accepted terms that say so, that was with us in
    the last twelve months, and that the office did not switch off;
  * five wrong phones for one identity number lock it for a day;
  * a third family from one device stops identification there for a day, and a
    cap per network, per hour and per day stops a sweep;
  * a silent check that the form was really opened, a moment before asking;
  * the same answer — "not known" — for an unknown parent, a wrong phone and
    every block, so nothing is learned from being refused;
  * the office is told of every block, and the parent can be told of every
    identification (WIDGET_IDENTIFICATION_NOTICE_ENABLED).

Every answer is kept (WidgetIdentifyAttempt): it is the log and it is what the
limits count from, since the server keeps no memory between requests.
"""
import hashlib
import hmac
import logging
import re
import time
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.db.models import Q
from django.utils import timezone

from apps.customers.identification_models import WidgetIdentifyAttempt as Attempt
from apps.customers.models import Family, Payment
from apps.enrollments.person_match import normalise_phone

logger = logging.getLogger(__name__)

# The title of the identification paragraph in the terms (core migration 0028).
# A family is recognised only if its parent accepted terms that carry it.
TERMS_MARKER = 'זיהוי בהרשמה הבאה'

TOKEN_SALT = 'widget-identification'
NEAR_SALT = 'widget-identification-near'
FORM_SALT = 'widget-identification-form'
# A registration is filled and paid within one sitting.
TOKEN_MAX_AGE = 2 * 60 * 60
NEAR_MAX_AGE = 10 * 60
FORM_MAX_AGE = 6 * 60 * 60
# Nobody opens a form and has typed two numbers into it in under this.
FORM_MIN_AGE = 1.2

WINDOW = timedelta(hours=24)
WRONG_PHONE_LIMIT = 5
# Two different parents from one device are a shared computer; a third is not.
DEVICE_FAMILY_LIMIT = 2
# One address can be a whole mobile network, so the bar is higher and it stops
# only families not yet seen from there.
NETWORK_FAMILY_LIMIT = 10
# Families identified by the whole form, from everywhere: above this it is a
# sweep and not a registration day, and identification pauses for everyone.
HOURLY_CAP = 60
DAILY_CAP = 300
ACTIVE_WINDOW = timedelta(days=365)
NOTICE_QUIET = timedelta(minutes=30)
# Every answer takes at least this long, so a refusal cannot be told from a
# "not known" by how fast it came back.
MIN_ANSWER_SECONDS = 0.4

ACTIVE_CHILD_STATUSES = ('active', 'payment_problem')
UNKNOWN = {'status': 'unknown'}
EXPIRED_MESSAGE = 'הזיהוי פג. מלאו את הפרטים והמשיכו כרגיל.'

_DEVICE_ID = re.compile(r'^[A-Za-z0-9-]{16,64}$')
_MOBILE = re.compile(r'^05\d{8}$')


class IdentificationExpired(Exception):
    """The token a registration carried is no longer good."""


def is_enabled() -> bool:
    return bool(getattr(settings, 'WIDGET_IDENTIFICATION_ENABLED', False))


def form_ticket() -> str:
    """Handed to the form when it opens; an identification must bring it back."""
    return signing.dumps({'t': time.time()}, salt=FORM_SALT)


def _keyed(value: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def _valid_identity_number(value: str) -> bool:
    if not re.fullmatch(r'\d{9}', value or ''):
        return False
    total = 0
    for index, char in enumerate(value):
        step = int(char) * (2 if index % 2 else 1)
        total += step - 9 if step > 9 else step
    return total % 10 == 0


# ── what is shown of a stored detail: one character ──────────────────────────

def mask_text(value) -> str:
    """The first character, then dots. Nothing for a detail the card does not hold."""
    value = (value or '').strip()
    return f'{value[0]}••••' if value else ''


def mask_email(value) -> str:
    value = (value or '').strip()
    return f'{value[0]}•••••••••' if value else ''


def mask_number(value, dots: int = 8) -> str:
    """Dots, then the last digit."""
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return f'{"•" * dots}{digits[-1]}' if digits else ''


def mask_date(value) -> str:
    return f'••/••/•••{value.year % 10}' if value else ''


# ── the family card ───────────────────────────────────────────────────────────

def stored_parent(family) -> dict:
    """The parent's details as registration will complete them: one source for the masks and the fill."""
    primary = family.parents.filter(is_primary=True).first()
    return {
        'first_name': (primary.first_name if primary else '') or '',
        'last_name': (primary.last_name if primary else '') or family.name or '',
        'email': family.email or (primary.email if primary else '') or '',
        'phone': family.phone or (primary.phone if primary else '') or '',
    }


def family_phones(family) -> set:
    """Every phone on the card that proves the parent: the family's and the primary parent's."""
    primary = family.parents.filter(is_primary=True).first()
    phones = {normalise_phone(family.phone), normalise_phone(primary.phone if primary else '')}
    phones.discard('')
    return phones


def identifiable_children(family):
    return family.children.exclude(status='ghost').order_by('created_at')


def _recently_active(family, now) -> bool:
    """A paying child today, or a payment or a trial lesson in the last twelve months."""
    since = now - ACTIVE_WINDOW
    if family.children.filter(status__in=ACTIVE_CHILD_STATUSES).exists():
        return True
    if Payment.objects.filter(
        Q(family=family) | Q(child__family=family), status='completed', payment_date__gte=since,
    ).exists():
        return True
    from apps.enrollments.models import LessonEnrollment

    return LessonEnrollment.objects.filter(
        child__family=family, trial_lesson_date__gte=since.date(),
    ).exists()


def terms_carry_the_paragraph() -> bool:
    from apps.core.registration_terms_service import get_registration_terms

    return TERMS_MARKER in (get_registration_terms().content or '')


def record_identification_consent(family) -> None:
    """The parent accepted the terms: if they carry the paragraph, that is the consent. Kept once."""
    if family.widget_identification_consent_at or not terms_carry_the_paragraph():
        return
    family.widget_identification_consent_at = timezone.now()
    family.save(update_fields=['widget_identification_consent_at', 'updated_at'])


def _near(typed: str, stored: str) -> bool:
    """A digit or two off: a slip of the finger, not another number."""
    if len(typed) != len(stored):
        return False
    return 0 < sum(1 for a, b in zip(typed, stored) if a != b) <= 2


# ── telling people ────────────────────────────────────────────────────────────

def _alert_office(outcome: str, *, key: str, what: str, family=None) -> None:
    try:
        from apps.core.office_alerts import describe_family, raise_office_alert

        raise_office_alert(
            kind='widget_identification_block',
            dedup_key=f'widget-identify:{outcome}:{key}:{timezone.localdate():%Y%m%d}',
            title='זיהוי הורים בטופס ההרשמה נחסם',
            where='טופס ההרשמה באתר',
            what=what,
            why='ההגנה על זיהוי הורים עצרה ניסיון שנראה כמו איסוף מידע. ההרשמה עצמה לא נחסמה.',
            customer=describe_family(family) if family is not None else '',
            action='אם הורה מתקשר ואומר שהטופס לא זיהה אותו, הוא יכול למלא את הפרטים ולהירשם כרגיל.',
        )
    except Exception:
        logger.exception('Identification block alert failed (non-fatal)')


def _send_notice(family, phone: str, now) -> bool:
    """The short WhatsApp to the parent. True when the parent was told, now or minutes ago."""
    if not getattr(settings, 'WIDGET_IDENTIFICATION_NOTICE_ENABLED', False):
        return False
    flow_ns = (getattr(settings, 'MANYCHAT_IDENTIFICATION_NOTICE_FLOW_NS', '') or '').strip()
    if not flow_ns:
        return False
    if Attempt.objects.filter(family=family, notice_sent=True, created_at__gte=now - NOTICE_QUIET).exists():
        return True
    try:
        from apps.core.manychat_service import ManyChatService

        service = ManyChatService()
        if not service.is_configured:
            return False
        # No contact is created for this: a parent we never wrote to is not written to now.
        subscriber = service.find_existing(phone)
        if not subscriber or not subscriber.get('id'):
            return False
        service.send_flow(subscriber['id'], flow_ns)
        return True
    except Exception:
        logger.exception('Identification notice to family %s failed (non-fatal)', family.pk)
        return False


# ── the answer ────────────────────────────────────────────────────────────────

def _keep(outcome, *, id_hash, family=None, device='', ip_hash='', notice=False) -> Attempt:
    return Attempt.objects.create(
        outcome=outcome, id_hash=id_hash, family=family, device_id=device, ip_hash=ip_hash, notice_sent=notice,
    )


def _opened_the_form(data) -> bool:
    """The silent check: a real form, opened a moment ago, with its hidden field left empty."""
    if str(data.get('website') or '').strip():
        return False
    try:
        opened = float(signing.loads(str(data.get('ticket') or ''), salt=FORM_SALT, max_age=FORM_MAX_AGE)['t'])
    except Exception:
        return False
    return time.time() - opened >= FORM_MIN_AGE


def _device_mark(device: str) -> str:
    return _keyed(device)[:20]


def _known_answer(family, attempt, *, device: str, notice_sent: bool) -> dict:
    parent = stored_parent(family)
    return {
        'status': 'known',
        # Good for this family, on the device that was identified, for one sitting.
        'token': signing.dumps(
            {'f': str(family.id), 'a': str(attempt.id), 'd': _device_mark(device)}, salt=TOKEN_SALT,
        ),
        'notice_sent': notice_sent,
        'parent': {
            'first_name': mask_text(parent['first_name']),
            'last_name': mask_text(parent['last_name']),
            'email': mask_email(parent['email']),
            'phone': mask_number(parent['phone'], dots=9),
        },
        'children': [
            {
                'id': str(child.id),
                'first_name': child.first_name,
                'last_name': mask_text(child.last_name),
                'id_number': mask_number(child.id_number),
                'birth_date': mask_date(child.birth_date),
                'gender': child.gender or '',
            }
            for child in identifiable_children(family)
        ],
    }


def identify(data, *, ip: str = '') -> dict:
    """Answer the form. Never raises; anything unexpected is "not known"."""
    started = time.monotonic()
    try:
        answer = _identify(data, ip=ip)
    except Exception:
        logger.exception('Identification failed — answered "not known"')
        answer = dict(UNKNOWN)
    wait = MIN_ANSWER_SECONDS - (time.monotonic() - started)
    if wait > 0:
        time.sleep(wait)
    return answer


def _identify(data, *, ip: str) -> dict:
    if not is_enabled():
        return dict(UNKNOWN)

    now = timezone.now()
    since = now - WINDOW
    device = str(data.get('device_id') or '').strip()
    ip_hash = _keyed(ip) if ip else ''
    near_token = str(data.get('near_token') or '').strip()
    parent_id = phone = ''

    if near_token:
        # The parent pressed "update" on the similar number offered a moment ago.
        try:
            offered = signing.loads(near_token, salt=NEAR_SALT, max_age=NEAR_MAX_AGE)
            family = Family.objects.filter(id=offered['f']).first()
            id_hash = offered['h']
        except Exception:
            return dict(UNKNOWN)
        # An offer is taken up only where it was made.
        if family is None or offered.get('d') != _device_mark(device):
            return dict(UNKNOWN)
        accepted_near = True
    else:
        parent_id = ''.join(ch for ch in str(data.get('parent_id_number') or '') if ch.isdigit())
        phone = normalise_phone(data.get('parent_phone'))
        if not _valid_identity_number(parent_id) or not _MOBILE.match(phone):
            return dict(UNKNOWN)
        id_hash = _keyed(parent_id)
        family = None
        accepted_near = False

    keep = dict(id_hash=id_hash, device=device, ip_hash=ip_hash)

    if not _DEVICE_ID.match(device) or not _opened_the_form(data):
        _keep(Attempt.OUTCOME_BOT, **keep)
        return dict(UNKNOWN)

    # Families, not requests: one parent typing again and again is one family.
    identified = Attempt.objects.filter(outcome__in=(Attempt.OUTCOME_KNOWN, Attempt.OUTCOME_KNOWN_NEAR))
    for window, cap, key, words in (
        (timedelta(hours=1), HOURLY_CAP, f'h{now:%H}', 'בשעה אחת'),
        (WINDOW, DAILY_CAP, 'day', 'ביממה'),
    ):
        if identified.filter(created_at__gte=now - window).values('family').distinct().count() >= cap:
            _keep(Attempt.OUTCOME_CAP, **keep)
            _alert_office(
                Attempt.OUTCOME_CAP, key=key,
                what=f'יותר מ-{cap} משפחות זוהו {words}. הזיהוי נעצר לכולם עד שהקצב יורד, והטופס נפתח ריק.',
            )
            return dict(UNKNOWN)

    if Attempt.objects.filter(
        id_hash=id_hash, outcome__in=Attempt.WRONG_PHONE, created_at__gte=since,
    ).count() >= WRONG_PHONE_LIMIT:
        locked_family = family or Family.objects.filter(parent_id_number=parent_id).first()
        _keep(Attempt.OUTCOME_LOCKED, family=locked_family, **keep)
        _alert_office(
            Attempt.OUTCOME_LOCKED, key=id_hash[:16], family=locked_family,
            what=f'{WRONG_PHONE_LIMIT} ניסיונות עם טלפון שגוי על אותה תעודת זהות. הזיהוי שלה ננעל ליממה.',
        )
        return dict(UNKNOWN)

    if family is None:
        families = list(Family.objects.filter(parent_id_number=parent_id)[:2])
        if not families:
            _keep(Attempt.OUTCOME_UNKNOWN, **keep)
            return dict(UNKNOWN)
        if len(families) > 1:
            # Two cards with one identity number: registration would pick one, we show neither.
            _keep(Attempt.OUTCOME_DUPLICATE, **keep)
            return dict(UNKNOWN)
        family = families[0]

    if family.widget_identification_blocked_at:
        _keep(Attempt.OUTCOME_HIDDEN, family=family, **keep)
        return dict(UNKNOWN)
    if not family.widget_identification_consent_at:
        _keep(Attempt.OUTCOME_NO_CONSENT, family=family, **keep)
        return dict(UNKNOWN)
    if not _recently_active(family, now) or not identifiable_children(family).exists():
        _keep(Attempt.OUTCOME_OLD, family=family, **keep)
        return dict(UNKNOWN)

    phones = family_phones(family)
    near_phone = ''
    if not accepted_near and phone not in phones:
        near_phone = next((stored for stored in sorted(phones) if _near(phone, stored)), '')
        if not near_phone:
            _keep(Attempt.OUTCOME_MISMATCH, family=family, **keep)
            return dict(UNKNOWN)

    # From here something of the family is about to be shown.
    if Attempt.objects.filter(device_id=device, outcome=Attempt.OUTCOME_DEVICE, created_at__gte=since).exists():
        _keep(Attempt.OUTCOME_DEVICE, family=family, **keep)
        return dict(UNKNOWN)
    shown = Attempt.objects.filter(outcome__in=Attempt.REVEALING, created_at__gte=since).exclude(family=family)
    if shown.filter(device_id=device).values('family').distinct().count() >= DEVICE_FAMILY_LIMIT:
        _keep(Attempt.OUTCOME_DEVICE, family=family, **keep)
        _alert_office(
            Attempt.OUTCOME_DEVICE, key=device[:24], family=family,
            what='ממכשיר אחד נבדקו פרטים של הורה שלישי ביממה. הזיהוי נחסם במכשיר הזה ליממה, והטופס נפתח בו ריק.',
        )
        return dict(UNKNOWN)
    if ip_hash and shown.filter(ip_hash=ip_hash).values('family').distinct().count() >= NETWORK_FAMILY_LIMIT:
        _keep(Attempt.OUTCOME_NETWORK, family=family, **keep)
        _alert_office(
            Attempt.OUTCOME_NETWORK, key=ip_hash[:16], family=family,
            what=f'מרשת אחת נבדקו פרטים של יותר מ-{NETWORK_FAMILY_LIMIT} הורים ביממה. הורים נוספים מהרשת הזאת לא מזוהים היום.',
        )
        return dict(UNKNOWN)

    if near_phone:
        _keep(Attempt.OUTCOME_NEAR, family=family, **keep)
        return {
            'status': 'near',
            'last_digit': near_phone[-1],
            'near_token': signing.dumps(
                {'f': str(family.id), 'h': id_hash, 'd': _device_mark(device)}, salt=NEAR_SALT,
            ),
        }

    notice_sent = _send_notice(family, stored_parent(family)['phone'], now)
    attempt = _keep(
        Attempt.OUTCOME_KNOWN_NEAR if accepted_near else Attempt.OUTCOME_KNOWN,
        family=family, notice=notice_sent, **keep,
    )
    return _known_answer(family, attempt, device=device, notice_sent=notice_sent)


# ── registration with the token ───────────────────────────────────────────────

def family_of_token(token: str, device: str = ''):
    """The family a token was given for — while it is still good, and on the device it was given to; else None."""
    if not is_enabled():
        return None
    try:
        payload = signing.loads(str(token or ''), salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except Exception:
        return None
    if payload.get('d') != _device_mark(str(device or '').strip()):
        return None
    family = Family.objects.filter(id=payload.get('f')).first()
    if family is None or family.widget_identification_blocked_at:
        return None
    return family


def fill_from_identification(data) -> dict:
    """
    Complete a registration that carries an identification token.

    The form holds only hidden versions of the stored details, so it sends
    those fields empty; they are completed here from the card. What the parent
    retyped is used as typed. A child chosen from the list (`identified_child_id`)
    is that child: every detail the card holds for the child comes from the
    card, so the form can never overwrite an existing child.

    Raises IdentificationExpired when the token is not good.
    """
    family = family_of_token(data.get('identify_token'), data.get('device_id'))
    if family is None:
        raise IdentificationExpired()
    filled = {
        key: value for key, value in data.items()
        if key not in ('identify_token', 'identified_child_id', 'device_id')
    }

    def blank(key) -> bool:
        return not str(filled.get(key) or '').strip()

    parent = stored_parent(family)
    filled['parent_id_number'] = family.parent_id_number
    # The phone is half of what proved the parent: it stays the stored one.
    filled['parent_phone'] = parent['phone']
    for key, stored in (
        ('parent_first_name', parent['first_name']),
        ('parent_last_name', parent['last_name']),
        ('parent_email', parent['email']),
    ):
        if blank(key):
            filled[key] = stored

    child_id = str(data.get('identified_child_id') or '').strip()
    if child_id:
        child = identifiable_children(family).filter(id=child_id).first()
        if child is None:
            raise IdentificationExpired()
        for key, stored in (
            ('child_first_name', child.first_name),
            ('child_last_name', child.last_name),
            ('child_id_number', child.id_number),
            ('child_birth_date', child.birth_date.isoformat() if child.birth_date else ''),
            ('child_gender', child.gender),
        ):
            # A detail the card lacks (an old card with no identity number) is taken as typed.
            if stored:
                filled[key] = stored
        filled['existing_child_id'] = str(child.id)
        filled['discount_confirmed'] = True
    return filled
