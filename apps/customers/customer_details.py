"""
The office edits a customer from the child's card: the child, the primary
parent, the family, and the family's extra phones — one save, all or nothing.

Why one route and not the three model routes (children/, parents/, families/):

- Family.phone/email and the primary Parent's phone/email are two copies of one
  fact. WhatsApp goes to the parent's (core/enrollment_whatsapp.py); duplicate
  detection, walk-in matching and the nightly merge read the family's
  (child_merge.py, enrollments/person_match.py); receipts are emailed to the
  family's. Written apart they drift, so a change here writes both.
- A phone or a parent ID that already belongs to another family changes which
  family the registration widget finds (it looks up by parent ID) and which
  children the merge folds together. The office is told and has to confirm.
- An extra phone is a non-primary Parent row, the same shape the "add customer"
  form has always created. It gets the group messages (customers/broadcast.py);
  payment and card links stay with the primary parent.

Nothing here touches money or history: no status, standing order, card token,
issued document or signature — and not the family's branch, which picks the
terminal a card link charges through and the branch a document is reported under.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Replace

from apps.core.card_validation import israeli_id_valid
from apps.customers.child_identity import normalize_id_number
from apps.customers.models import Child, Family, Parent
from apps.enrollments.person_match import normalise_phone
from apps.enrollments.repeat_trial import id_number_variants

logger = logging.getLogger(__name__)

# The one family every nameless walk-in shares (ChildViewSet.create_ghost).
# Its parent and phone belong to nobody, so they are not edited from a card.
SHARED_GHOST_FAMILY_NAME = 'רפאים (מערכת)'

# What the ghost flows store when no phone was given.
PLACEHOLDER_PHONE = '0000000000'

# The customers form's rule: a landline (0X-XXXXXXX) or a 10-digit number.
_PHONE = re.compile(r'^0\d{8,9}$')
# Extra phones exist for WhatsApp, which only a mobile number receives.
_MOBILE = re.compile(r'^05\d{8}$')

MAX_EXTRA_PHONES = 5

CHILD_FIELDS = ('first_name', 'last_name', 'birth_date', 'gender', 'id_number', 'phone_number', 'notes')
PARENT_FIELDS = ('first_name', 'last_name', 'phone', 'email', 'id_number')
FAMILY_FIELDS = ('name', 'address', 'notes')

LABELS = {
    'child.first_name': 'שם פרטי (ילד)',
    'child.last_name': 'שם משפחה (ילד)',
    'child.birth_date': 'תאריך לידה',
    'child.gender': 'מגדר',
    'child.id_number': 'ת.ז. ילד',
    'child.phone_number': 'טלפון ילד',
    'child.notes': 'הערות (ילד)',
    'parent.first_name': 'שם פרטי (הורה)',
    'parent.last_name': 'שם משפחה (הורה)',
    'parent.phone': 'טלפון הורה',
    'parent.email': 'אימייל',
    'parent.id_number': 'ת.ז. הורה',
    'family.name': 'שם המשפחה',
    'family.address': 'כתובת',
    'family.notes': 'הערות (משפחה)',
    'extra_phones': 'טלפונים נוספים',
}


class CustomerDetailsError(Exception):
    """The request cannot be saved as sent; ``errors`` maps a field path to the reason."""

    def __init__(self, errors: dict[str, str]):
        super().__init__('invalid customer details')
        self.errors = errors


class CustomerDetailsDuplicate(Exception):
    """A phone or ID the office typed belongs to another family; saved only once confirmed."""

    def __init__(self, duplicates: list[dict]):
        super().__init__('duplicate customer details')
        self.duplicates = duplicates


# --------------------------------------------------------------------------
# Reading the family the way the card shows it
# --------------------------------------------------------------------------

def primary_parent_of(parents) -> Parent | None:
    """The parent the card shows: the first marked primary, else the first — as ChildWithDetailsSerializer."""
    parents = list(parents)
    for parent in parents:
        if parent.is_primary:
            return parent
    return parents[0] if parents else None


def parent_display_name(parent: Parent) -> str:
    return f'{parent.first_name or ""} {parent.last_name or ""}'.strip()


def extra_phones_of(parents) -> list[dict]:
    """Every parent of the family other than the one the card shows as the parent."""
    parents = list(parents)
    primary = primary_parent_of(parents)
    return [
        {'id': str(parent.id), 'name': parent_display_name(parent), 'phone': parent.phone or ''}
        for parent in parents
        if primary is None or parent.pk != primary.pk
    ]


def family_is_shared(family: Family | None) -> bool:
    return bool(family) and (family.name or '').strip() == SHARED_GHOST_FAMILY_NAME


# --------------------------------------------------------------------------
# Normalising what was typed
# --------------------------------------------------------------------------

def _text(value) -> str:
    return '' if value is None else str(value).strip()


def _same_phone(a: str, b: str) -> bool:
    return normalise_phone(a) == normalise_phone(b)


def _phone_error(digits: str, *, mobile_only: bool = False) -> str | None:
    if mobile_only:
        return None if _MOBILE.match(digits) else 'מספר נייד לא תקין (05X-XXXXXXX)'
    return None if _PHONE.match(digits) else 'מספר טלפון לא תקין'


def _digits_expr(field: str):
    expr = F(field)
    for char in ('-', ' ', '+', '(', ')', '.'):
        expr = Replace(expr, Value(char), Value(''))
    return expr


def _other_families_with_phone(family: Family, digits: str):
    """Families other than this one holding the phone on the family or on any parent, however stored."""
    # 050-123-4567, 0501234567 and +972 50 123 4567 are one phone.
    same = Q(_d=digits) | Q(_d='972' + digits[1:])
    by_family = (
        Family.objects.exclude(pk=family.pk)
        .annotate(_d=_digits_expr('phone'))
        .filter(same)
        .values_list('pk', flat=True)
    )
    by_parent = (
        Parent.objects.exclude(family_id=family.pk)
        .annotate(_d=_digits_expr('phone'))
        .filter(same)
        .values_list('family_id', flat=True)
    )
    ids = set(by_family) | set(by_parent)
    return Family.objects.filter(pk__in=ids).exclude(name=SHARED_GHOST_FAMILY_NAME).order_by('name')


def _other_families_with_id(family: Family, digits: str):
    variants = id_number_variants(digits)
    if not variants:
        return Family.objects.none()
    return Family.objects.exclude(pk=family.pk).filter(parent_id_number__in=variants).order_by('name')


def _duplicate_message(what: str, families, reveal_names: bool) -> str:
    names = [f.name for f in families[:3]]
    if reveal_names and names:
        more = ' ועוד' if families.count() > 3 else ''
        return f'{what} כבר רשום אצל: {", ".join(names)}{more}'
    return f'{what} כבר רשום אצל משפחה אחרת'


# --------------------------------------------------------------------------
# The save
# --------------------------------------------------------------------------

def _section(payload, key) -> dict:
    value = payload.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CustomerDetailsError({key: 'מבנה לא תקין'})
    return value


def update_customer_details(child: Child, payload, *, user=None, reveal_names: bool = True) -> list[dict]:
    """
    Apply the office's edit and return what changed, as
    ``[{'field', 'label', 'old', 'new'}]`` — empty when nothing did.

    Each section is optional and only the keys it carries are read; a value
    that normalises to what is stored changes nothing, so an untouched field
    keeps its old formatting. Raises CustomerDetailsError (nothing saved) or
    CustomerDetailsDuplicate (nothing saved; resend with confirm_duplicates).
    """
    if not isinstance(payload, dict):
        raise CustomerDetailsError({'': 'מבנה לא תקין'})
    child_in = _section(payload, 'child')
    parent_in = _section(payload, 'parent')
    family_in = _section(payload, 'family')
    extras_in = payload.get('extra_phones')
    if extras_in is not None and not isinstance(extras_in, list):
        raise CustomerDetailsError({'extra_phones': 'מבנה לא תקין'})
    confirm = bool(payload.get('confirm_duplicates'))

    with transaction.atomic():
        child = Child.objects.select_for_update().get(pk=child.pk)
        family = Family.objects.select_for_update().get(pk=child.family_id)
        parents = list(
            Parent.objects.select_for_update()
            .filter(family=family)
            .order_by('-is_primary', 'first_name')
        )
        primary = primary_parent_of(parents)

        if (parent_in or family_in or extras_in is not None) and (
            family_is_shared(family) or child.status == 'ghost'
        ):
            raise CustomerDetailsError({
                'family': 'לילד שנוסף בשיעור אין עדיין משפחה משלו — פרטי ההורה יתעדכנו כשיירשם',
            })

        errors: dict[str, str] = {}
        changes: list[dict] = []
        duplicates: list[dict] = []

        def note(path, old, new):
            changes.append({'field': path, 'label': LABELS.get(path, path), 'old': old, 'new': new})

        # ---- child -------------------------------------------------------
        child_updates: dict = {}
        for key in CHILD_FIELDS:
            if key not in child_in:
                continue
            raw = child_in[key]
            path = f'child.{key}'
            if key in ('first_name', 'last_name'):
                value = _text(raw)
                if not value:
                    errors[path] = 'שדה חובה'
                elif len(value) > 100:
                    errors[path] = 'ארוך מדי'
                elif value != (getattr(child, key) or ''):
                    child_updates[key] = value
            elif key == 'birth_date':
                value = _text(raw)
                try:
                    parsed = date.fromisoformat(value)
                except ValueError:
                    errors[path] = 'תאריך לא תקין'
                    continue
                if parsed > date.today() or parsed.year < 1900:
                    errors[path] = 'תאריך לא תקין'
                elif parsed != child.birth_date:
                    child_updates[key] = parsed
            elif key == 'gender':
                value = _text(raw)
                if value not in ('male', 'female'):
                    errors[path] = 'ערך לא תקין'
                elif value != child.gender:
                    child_updates[key] = value
            elif key == 'id_number':
                value = normalize_id_number(_text(raw))
                if value == normalize_id_number(child.id_number or ''):
                    continue
                if value and not israeli_id_valid(value):
                    errors[path] = 'מספר ת.ז. לא תקין'
                else:
                    child_updates[key] = value
            elif key == 'phone_number':
                value = normalise_phone(_text(raw))
                if _same_phone(value, child.phone_number or ''):
                    continue
                if value and (error := _phone_error(value)):
                    errors[path] = error
                else:
                    child_updates[key] = value
            elif key == 'notes':
                value = _text(raw)
                if value != (child.notes or '').strip():
                    child_updates[key] = value

        # ---- primary parent ----------------------------------------------
        current_phone = (primary.phone if primary else '') or family.phone or ''
        current_email = (primary.email if primary else '') or family.email or ''
        parent_updates: dict = {}
        new_phone = None
        new_email = None
        for key in PARENT_FIELDS:
            if key not in parent_in:
                continue
            raw = parent_in[key]
            path = f'parent.{key}'
            if key in ('first_name', 'last_name'):
                value = _text(raw)
                if not value:
                    errors[path] = 'שדה חובה'
                elif len(value) > 100:
                    errors[path] = 'ארוך מדי'
                elif primary is None or value != (getattr(primary, key) or ''):
                    parent_updates[key] = value
            elif key == 'phone':
                value = normalise_phone(_text(raw))
                if _same_phone(value, current_phone):
                    continue
                if not value:
                    errors[path] = 'טלפון ההורה הוא שדה חובה'
                elif error := _phone_error(value):
                    errors[path] = error
                else:
                    new_phone = value
                    others = _other_families_with_phone(family, value)
                    if others.exists():
                        duplicates.append({
                            'field': path,
                            'message': _duplicate_message(f'הטלפון {value}', others, reveal_names),
                        })
            elif key == 'email':
                value = _text(raw)
                if value == current_email.strip():
                    continue
                if value:
                    try:
                        validate_email(value)
                    except DjangoValidationError:
                        errors[path] = 'כתובת אימייל לא תקינה'
                        continue
                    if len(value) > 254:
                        errors[path] = 'ארוך מדי'
                        continue
                new_email = value
            elif key == 'id_number':
                value = normalize_id_number(_text(raw))
                if value == normalize_id_number(family.parent_id_number or ''):
                    continue
                if not value:
                    # The widget finds a returning parent by this number; without
                    # it the next registration would open a second family.
                    errors[path] = 'ת.ז. ההורה משמשת לזיהוי בהרשמה ואינה יכולה להימחק'
                elif not israeli_id_valid(value):
                    errors[path] = 'מספר ת.ז. לא תקין'
                else:
                    parent_updates['id_number'] = value
                    others = _other_families_with_id(family, value)
                    if others.exists():
                        duplicates.append({
                            'field': path,
                            'message': _duplicate_message(f'ת.ז. {value}', others, reveal_names),
                        })

        creating_primary = primary is None and (parent_updates.keys() - {'id_number'} or new_phone or new_email)
        if creating_primary:
            if not parent_updates.get('first_name'):
                errors.setdefault('parent.first_name', 'שדה חובה')
            if not (new_phone or normalise_phone(family.phone or '')):
                errors.setdefault('parent.phone', 'טלפון ההורה הוא שדה חובה')

        # ---- family --------------------------------------------------------
        family_updates: dict = {}
        for key in FAMILY_FIELDS:
            if key not in family_in:
                continue
            value = _text(family_in[key])
            path = f'family.{key}'
            if key == 'name' and not value:
                errors[path] = 'שדה חובה'
            elif key == 'name' and len(value) > 200:
                errors[path] = 'ארוך מדי'
            elif value != (getattr(family, key) or '').strip():
                family_updates[key] = value

        # ---- extra phones --------------------------------------------------
        extras_plan = None
        if extras_in is not None:
            extras_plan = _plan_extras(
                extras_in,
                parents=parents,
                primary=primary,
                primary_phone=new_phone if new_phone is not None else current_phone,
                errors=errors,
            )

        if errors:
            raise CustomerDetailsError(errors)
        if duplicates and not confirm:
            raise CustomerDetailsDuplicate(duplicates)

        # ---- write -----------------------------------------------------------
        if child_updates:
            for key, value in child_updates.items():
                note(f'child.{key}', _display(getattr(child, key)), _display(value))
                setattr(child, key, value)
            # Saved alone: the post_save merge (signals.py) runs on commit and
            # reads the new name and phone, the same as any other edit.
            child.save(update_fields=[*child_updates.keys(), 'updated_at'])

        family_fields: set[str] = set()
        if 'id_number' in parent_updates:
            note('parent.id_number', family.parent_id_number or '', parent_updates['id_number'])
            family.parent_id_number = parent_updates.pop('id_number')
            family_fields.add('parent_id_number')

        if parent_updates or new_phone is not None or new_email is not None:
            if primary is None:
                primary = Parent(
                    family=family,
                    first_name='',
                    last_name=family.name or '',
                    phone=family.phone or '',
                    email=family.email or '',
                    is_primary=True,
                )
            for key, value in parent_updates.items():
                note(f'parent.{key}', getattr(primary, key) or '', value)
                setattr(primary, key, value)
            if new_phone is not None:
                note('parent.phone', current_phone, new_phone)
                primary.phone = new_phone
                family.phone = new_phone
                family_fields.add('phone')
            if new_email is not None:
                note('parent.email', current_email, new_email)
                primary.email = new_email
                family.email = new_email
                family_fields.add('email')
            primary.is_primary = True
            primary.save()

        for key, value in family_updates.items():
            note(f'family.{key}', getattr(family, key) or '', value)
            setattr(family, key, value)
            family_fields.add(key)

        if extras_plan is not None:
            before = [e['phone'] for e in extra_phones_of(parents)]
            _apply_extras(extras_plan, family=family, primary=primary)
            after = [e['phone'] for e in extra_phones_of(
                Parent.objects.filter(family=family).order_by('-is_primary', 'first_name')
            )]
            if extras_plan['changed']:
                note('extra_phones', ', '.join(before), ', '.join(after))

        if family_fields:
            family.save(update_fields=[*family_fields, 'updated_at'])

    if changes:
        # Field names only — the values are personal details and do not belong in logs.
        logger.info(
            'customer details edited: child=%s family=%s user=%s fields=%s confirmed_duplicates=%s',
            child.pk, family.pk, getattr(user, 'pk', None),
            ','.join(c['field'] for c in changes), bool(duplicates),
        )
    return changes


def _display(value) -> str:
    if value is None:
        return ''
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _plan_extras(rows, *, parents, primary, primary_phone, errors) -> dict:
    """
    Check the full list of extra phones the card sent and work out the writes.

    The list replaces what is there: a row with an id updates that parent, a
    row without one adds a parent, and an existing extra that is missing is
    removed — unless a payment or an invoice names it.
    """
    existing = {str(p.pk): p for p in parents if primary is None or p.pk != primary.pk}
    if len(rows) > MAX_EXTRA_PHONES:
        errors['extra_phones'] = f'עד {MAX_EXTRA_PHONES} טלפונים נוספים'
        return {'updates': [], 'creates': [], 'deletes': [], 'changed': False}

    seen = {normalise_phone(primary_phone)} if primary_phone else set()
    updates, creates, kept = [], [], set()
    for index, row in enumerate(rows):
        path = f'extra_phones.{index}'
        if not isinstance(row, dict):
            errors[path] = 'מבנה לא תקין'
            continue
        row_id = _text(row.get('id'))
        name = _text(row.get('name'))
        phone = normalise_phone(_text(row.get('phone')))
        if row_id and row_id not in existing:
            errors[path] = 'הטלפון הזה כבר לא קיים — רעננו את הכרטיס'
            continue
        if not phone:
            errors[f'{path}.phone'] = 'חסר מספר'
            continue
        target = existing.get(row_id)
        unchanged_phone = target is not None and _same_phone(phone, target.phone or '')
        if not unchanged_phone and (error := _phone_error(phone, mobile_only=True)):
            errors[f'{path}.phone'] = error
            continue
        if phone in seen:
            errors[f'{path}.phone'] = 'המספר כבר מופיע בכרטיס'
            continue
        seen.add(phone)
        if len(name) > 200:
            errors[f'{path}.name'] = 'ארוך מדי'
            continue
        if target is None:
            creates.append({'name': name, 'phone': phone})
            continue
        kept.add(row_id)
        name_changed = bool(name) and name != parent_display_name(target)
        if not unchanged_phone or name_changed or target.is_primary:
            updates.append({
                'parent': target,
                'name': name if name_changed else None,
                'phone': None if unchanged_phone else phone,
            })

    deletes = [p for key, p in existing.items() if key not in kept]
    for parent in deletes:
        if parent.payments.exists() or parent.invoices.exists():
            errors['extra_phones'] = (
                f'לא ניתן להסיר את {parent.phone or parent_display_name(parent)}: '
                'יש תשלום או חשבונית על שמו'
            )
    changed = bool(creates or deletes or any(u['name'] is not None or u['phone'] for u in updates))
    return {'updates': updates, 'creates': creates, 'deletes': deletes, 'changed': changed}


def _split_name(name: str, *, fallback_first: str, fallback_last: str) -> tuple[str, str]:
    if not name:
        return fallback_first, fallback_last
    parts = name.split(None, 1)
    return parts[0][:100], (parts[1] if len(parts) > 1 else fallback_last)[:100]


def _apply_extras(plan, *, family, primary) -> None:
    fallback_first = (primary.first_name if primary else '') or family.name or ''
    fallback_last = (primary.last_name if primary else '') or ''
    if primary is not None and primary.pk and not primary.is_primary:
        # The parent the card shows is the primary from now on, so every sender
        # (alerts, card links, broadcasts) agrees on who that is.
        primary.is_primary = True
        primary.save(update_fields=['is_primary', 'updated_at'])
    for parent in plan['deletes']:
        parent.delete()
    for item in plan['updates']:
        parent = item['parent']
        if item['name'] is not None:
            parent.first_name, parent.last_name = _split_name(
                item['name'], fallback_first=fallback_first, fallback_last=fallback_last,
            )
        if item['phone']:
            parent.phone = item['phone']
        # An extra is never the primary, whatever an old record said.
        parent.is_primary = False
        parent.save()
    for item in plan['creates']:
        first, last = _split_name(item['name'], fallback_first=fallback_first, fallback_last=fallback_last)
        Parent.objects.create(
            family=family,
            first_name=first,
            last_name=last,
            phone=item['phone'],
            email='',
            is_primary=False,
        )
