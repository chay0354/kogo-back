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
  * only a family that was with us in the last twelve months — on a team,
    paying, or at a trial lesson — and that the office did not switch off;
  * never by a phone that an identity number alone put on the card;
  * five wrong phones for one identity number lock it for a day, counted one
    request at a time so sending them together changes nothing;
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
import uuid
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.customers.identification_models import WidgetIdentifyAttempt as Attempt
from apps.customers.models import Family, Payment
from apps.enrollments.person_match import normalise_phone

logger = logging.getLogger(__name__)

# The title of the identification paragraph in the terms (core migration 0028).
# Accepting terms that carry it is recorded on the family; it is asked for
# before recognising one only under WIDGET_IDENTIFICATION_REQUIRES_CONSENT.
TERMS_MARKER = 'זיהוי בהרשמה הבאה'

TOKEN_SALT = 'widget-identification'
NEAR_SALT = 'widget-identification-near'
FORM_SALT = 'widget-identification-form'
# A registration is filled and paid within one sitting.
TOKEN_MAX_AGE = 2 * 60 * 60
NEAR_MAX_AGE = 10 * 60
FORM_MAX_AGE = 2 * 60 * 60
# Nobody opens a form and has typed two numbers into it in under this.
FORM_MIN_AGE = 1.2

WINDOW = timedelta(hours=24)
WRONG_PHONE_LIMIT = 5
# Two different parents from one device are a shared computer; a third is not.
DEVICE_FAMILY_LIMIT = 2
# One address can be a whole mobile network, so the bar is higher and it stops
# only families not yet seen from there.
NETWORK_FAMILY_LIMIT = 25
# Families identified by the whole form, from everywhere: above this it is a
# sweep and not a registration day, and identification pauses for everyone.
HOURLY_CAP = 120
DAILY_CAP = 500
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
    """On by the setting — and never with the repository's own secret key, which anyone can sign with."""
    return bool(getattr(settings, 'WIDGET_IDENTIFICATION_ENABLED', False)) and not getattr(
        settings, 'SECRET_KEY_IS_DEFAULT', False,
    )


def _read_signed(value, *, salt: str, max_age):
    """
    What we signed — with the server's own key and no other.

    Links sent before a real key was set stay readable elsewhere
    (settings.SECRET_KEY_FALLBACKS); an identification must not be: the earlier
    key is in the repository, and whoever has it could sign himself into any family.
    """
    return signing.loads(str(value or ''), salt=salt, max_age=max_age, fallback_keys=[])


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
    """
    The phones on the card that prove the parent: the family's and the primary
    parent's — when they are a real mobile number. A placeholder on a card
    (zeros, a landline) proves nobody: anybody can guess it.
    """
    primary = family.parents.filter(is_primary=True).first()
    phones = {normalise_phone(family.phone), normalise_phone(primary.phone if primary else '')}
    return {phone for phone in phones if _MOBILE.match(phone) and len(set(phone[2:])) > 1}


def identifiable_children(family):
    return family.children.exclude(status='ghost').order_by('created_at')


def _paid(family):
    return Payment.objects.filter(Q(family=family) | Q(child__family=family), status='completed')


def is_established(family) -> bool:
    """
    The family has paid us, or has a child on a team: its card is its own.

    Until then a card is only what somebody typed — a registration never paid,
    a free trial booked — and whoever typed it may not have been the parent.
    So an unpaid card is corrected by the next registration, as it always was
    (and is marked when that happens: see `phone_is_the_parents`).
    """
    return family.children.filter(status__in=ACTIVE_CHILD_STATUSES).exists() or _paid(family).exists()


def phone_is_the_parents(family) -> bool:
    """
    The phone on the card can be taken for the parent's own.

    A family back from a trial lesson is whom the form is there to recognise,
    and its card was never paid on — so the next registration may still replace
    its phone with an identity number alone. Were that phone then good for
    identification, anybody with a parent's identity number could put his own
    phone on the card and be shown the children. So a card whose phone was
    replaced that way recognises nobody until the family has paid.
    """
    return not family.widget_contact_unproven_at or is_established(family)


def _recently_active(family, now) -> bool:
    """A paying child today, or a payment or a trial lesson in the last twelve months."""
    if family.children.filter(status__in=ACTIVE_CHILD_STATUSES).exists():
        return True
    since = now - ACTIVE_WINDOW
    if _paid(family).filter(payment_date__gte=since).exists():
        return True
    from apps.enrollments.models import LessonEnrollment

    # The date is cleared when a trial child subscribes; the day it was held is kept.
    return LessonEnrollment.objects.filter(child__family=family).filter(
        Q(trial_lesson_date__gte=since.date()) | Q(trial_held_on__gte=since.date()),
    ).exists()


def requires_consent() -> bool:
    return bool(getattr(settings, 'WIDGET_IDENTIFICATION_REQUIRES_CONSENT', False))


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
    """
    A slip of the finger: one wrong digit, or two neighbours swapped.

    Kept this narrow on purpose. A similar number is offered the stored one
    with a press, so every number counted as "similar" is a number that opens
    the family. Any two wrong digits would make some 2,300 numbers open each
    family; these two slips make about 80.
    """
    if len(typed) != len(stored):
        return False
    wrong = [index for index, (a, b) in enumerate(zip(typed, stored)) if a != b]
    if len(wrong) == 1:
        return True
    return (
        len(wrong) == 2
        and wrong[1] == wrong[0] + 1
        and typed[wrong[0]] == stored[wrong[1]]
        and typed[wrong[1]] == stored[wrong[0]]
    )


# ── telling people ────────────────────────────────────────────────────────────

def _alert_office(outcome: str, now, *, what: str) -> None:
    """
    Tell the office of a block — once an hour for each kind of block, however
    many there were. Whoever sets blocks off on purpose must not be able to
    fill the office's WhatsApp with them; the log (WidgetIdentifyAttempt) has
    every one.
    """
    try:
        from apps.core.office_alerts import raise_office_alert

        raise_office_alert(
            kind='widget_identification_block',
            dedup_key=f'widget-identify:{outcome}:{timezone.localtime(now):%Y%m%d%H}',
            title='זיהוי הורים בטופס ההרשמה נחסם',
            where='טופס ההרשמה באתר',
            what=what,
            why='ההגנה על זיהוי הורים עצרה ניסיון שנראה כמו איסוף מידע. ההרשמה עצמה לא נחסמה.',
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
        opened = float(_read_signed(data.get('ticket'), salt=FORM_SALT, max_age=FORM_MAX_AGE)['t'])
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


def _one_at_a_time(key: str) -> None:
    """
    Inside a transaction: wait for anyone else deciding about the same thing.

    The limits are "count, then write". Without this, requests sent together
    all count before any of them has written, and all pass. The lock is held
    until the transaction ends and never outlives it.
    """
    if connection.vendor != 'postgresql':
        return
    number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big', signed=True)
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s)', [number])


def _never_wait_long() -> None:
    """Inside a transaction: give up on a lock after a moment rather than queue behind it."""
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '3s'")


def guessing_locked(parent_id: str) -> bool:
    """Five wrong phones were tried for this identity number today — anywhere the form takes a phone."""
    if not parent_id:
        return False
    return Attempt.objects.filter(
        id_hash=_keyed(parent_id), outcome__in=Attempt.WRONG_PHONE, created_at__gte=timezone.now() - WINDOW,
    ).count() >= WRONG_PHONE_LIMIT


def note_wrong_phone(parent_id: str, family) -> None:
    """
    A phone that is not the card's was typed with a family's identity number,
    somewhere other than the identification itself (the look-up, the quote).

    Counted with the identification's own wrong tries. Otherwise those other
    doors answer "right" or "wrong" to as many guesses as one cares to send,
    and the five-tries lock on identification locks nothing.
    """
    try:
        with transaction.atomic():
            _never_wait_long()
            _one_at_a_time(f'identify:{_keyed(parent_id)}')
            _keep(Attempt.OUTCOME_MISMATCH, id_hash=_keyed(parent_id), family=family)
    except Exception:
        logger.exception('A wrong phone could not be counted (non-fatal)')


def _identify(data, *, ip: str) -> dict:
    if not is_enabled():
        return dict(UNKNOWN)

    device = str(data.get('device_id') or '').strip()
    near_token = str(data.get('near_token') or '').strip()
    parent_id = phone = ''
    family = None

    if near_token:
        # The parent pressed "update" on the similar number offered a moment ago.
        try:
            offered = _read_signed(near_token, salt=NEAR_SALT, max_age=NEAR_MAX_AGE)
            family = Family.objects.filter(id=offered['f']).first()
            id_hash = offered['h']
        except Exception:
            return dict(UNKNOWN)
        # An offer is taken up only where it was made.
        if family is None or offered.get('d') != _device_mark(device):
            return dict(UNKNOWN)
    else:
        parent_id = ''.join(ch for ch in str(data.get('parent_id_number') or '') if ch.isdigit())
        phone = normalise_phone(data.get('parent_phone'))
        if not _valid_identity_number(parent_id) or not _MOBILE.match(phone):
            return dict(UNKNOWN)
        id_hash = _keyed(parent_id)

    with transaction.atomic():
        _never_wait_long()
        # One decision at a time for one identity number: five wrong phones are five, however they are sent.
        _one_at_a_time(f'identify:{id_hash}')
        decided = _decide(
            family=family, parent_id=parent_id, phone=phone, accepted_near=bool(near_token),
            keep=dict(id_hash=id_hash, device=device, ip_hash=_keyed(ip) if ip else ''), data=data,
        )
    if isinstance(decided, dict):
        return decided

    # A known parent. The message to them is sent with every lock let go: it
    # is two calls to another company, and nobody else should wait on those.
    family, attempt = decided
    notice_sent = _send_notice(family, stored_parent(family)['phone'], timezone.now())
    if notice_sent:
        Attempt.objects.filter(pk=attempt.pk).update(notice_sent=True)
    return _known_answer(family, attempt, device=device, notice_sent=notice_sent)


def _decide(*, family, parent_id, phone, accepted_near, keep, data):
    """The answer — or, for a known parent, the family and the kept attempt, for the caller to finish."""
    now = timezone.now()
    since = now - WINDOW
    id_hash, device, ip_hash = keep['id_hash'], keep['device'], keep['ip_hash']

    if not _DEVICE_ID.match(device) or not _opened_the_form(data):
        _keep(Attempt.OUTCOME_BOT, **keep)
        return dict(UNKNOWN)

    if Attempt.objects.filter(
        id_hash=id_hash, outcome__in=Attempt.WRONG_PHONE, created_at__gte=since,
    ).count() >= WRONG_PHONE_LIMIT:
        locked_family = family or Family.objects.filter(parent_id_number=parent_id).first()
        _keep(Attempt.OUTCOME_LOCKED, family=locked_family, **keep)
        _alert_office(
            Attempt.OUTCOME_LOCKED, now,
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
    if requires_consent() and not family.widget_identification_consent_at:
        _keep(Attempt.OUTCOME_NO_CONSENT, family=family, **keep)
        return dict(UNKNOWN)
    if not _recently_active(family, now) or not identifiable_children(family).exists():
        _keep(Attempt.OUTCOME_OLD, family=family, **keep)
        return dict(UNKNOWN)
    if not phone_is_the_parents(family):
        _keep(Attempt.OUTCOME_UNPROVEN_CARD, family=family, **keep)
        return dict(UNKNOWN)

    phones = family_phones(family)
    near_phone = ''
    if not accepted_near and phone not in phones:
        near_phone = next((stored for stored in sorted(phones) if _near(phone, stored)), '')
        if not near_phone:
            _keep(Attempt.OUTCOME_MISMATCH, family=family, **keep)
            return dict(UNKNOWN)

    # From here something of the family is about to be shown. The counts below
    # are across families, so they are taken one request at a time.
    _one_at_a_time('identify:reveal')

    # Families, not requests: one parent typing again and again is one family.
    identified = Attempt.objects.filter(
        outcome__in=(Attempt.OUTCOME_KNOWN, Attempt.OUTCOME_KNOWN_NEAR),
    ).exclude(family=family)
    for window, cap, words in ((timedelta(hours=1), HOURLY_CAP, 'בשעה אחת'), (WINDOW, DAILY_CAP, 'ביממה')):
        if identified.filter(created_at__gte=now - window).values('family').distinct().count() >= cap:
            _keep(Attempt.OUTCOME_CAP, family=family, **keep)
            _alert_office(
                Attempt.OUTCOME_CAP, now,
                what=f'יותר מ-{cap} משפחות זוהו {words}. הזיהוי נעצר לכולם עד שהקצב יורד, והטופס נפתח ריק.',
            )
            return dict(UNKNOWN)

    if Attempt.objects.filter(device_id=device, outcome=Attempt.OUTCOME_DEVICE, created_at__gte=since).exists():
        # Still inside the day the device was stopped for. Kept under its own
        # name, so being refused again does not start the day over.
        _keep(Attempt.OUTCOME_DEVICE_HELD, family=family, **keep)
        return dict(UNKNOWN)
    shown = Attempt.objects.filter(outcome__in=Attempt.REVEALING, created_at__gte=since).exclude(family=family)
    if shown.filter(device_id=device).values('family').distinct().count() >= DEVICE_FAMILY_LIMIT:
        _keep(Attempt.OUTCOME_DEVICE, family=family, **keep)
        _alert_office(
            Attempt.OUTCOME_DEVICE, now,
            what='ממכשיר אחד נבדקו פרטים של הורה שלישי ביממה. הזיהוי נחסם במכשיר הזה ליממה, והטופס נפתח בו ריק.',
        )
        return dict(UNKNOWN)
    if ip_hash and shown.filter(ip_hash=ip_hash).values('family').distinct().count() >= NETWORK_FAMILY_LIMIT:
        _keep(Attempt.OUTCOME_NETWORK, family=family, **keep)
        _alert_office(
            Attempt.OUTCOME_NETWORK, now,
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

    return family, _keep(Attempt.OUTCOME_KNOWN_NEAR if accepted_near else Attempt.OUTCOME_KNOWN, family=family, **keep)


def proves_parent(family, typed_phone) -> bool:
    """
    The phone typed is one the family's card holds.

    That, with the identity number, is what the form's identification rests
    on — and it is the least that is asked before anything about an existing
    family is said or changed for whoever typed its identity number.
    """
    phones = family_phones(family)
    return bool(phones) and normalise_phone(typed_phone) in phones


# ── registration with the token ───────────────────────────────────────────────

def family_of_token(token: str, device: str = ''):
    """The family a token was given for — while it is still good, and on the device it was given to; else None."""
    if not is_enabled():
        return None
    try:
        payload = _read_signed(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
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
    # A token speaks for its own family only: with another identity number
    # typed beside it, it is not this registration's token.
    typed_id = str(data.get('parent_id_number') or '').strip()
    if typed_id and typed_id != family.parent_id_number:
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
        try:
            child = identifiable_children(family).filter(id=uuid.UUID(child_id)).first()
        except ValueError:
            child = None
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
