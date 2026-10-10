"""
The few things every part of the app does to a contact: move its `touched_at`
(so the live screen picks the change up), write a line in its journal, and cut
a message down to the preview the list shows.
"""
import re
from datetime import timedelta

from django.utils import timezone

from apps.wahub.models import LAST_MESSAGE_PREVIEW_CHARS, Contact, ContactEvent


def now_israel_date():
    return timezone.localtime(timezone.now()).date()


def touch(contact_id, **fields) -> int:
    """
    A query-level update of one contact that the screen will see.

    Only the fields named are written, which is what keeps the automatic code
    (summary, matching) physically unable to write a follow-up mark.
    """
    moment = timezone.now()
    return Contact.objects.filter(pk=contact_id).update(touched_at=moment, updated_at=moment, **fields)


def touch_many(contact_ids) -> None:
    """
    Many contacts the screen has to see again, each given its own instant.

    The live update walks the contacts by `touched_at`, a hundred a call. A
    thousand rows stamped with one and the same time would have to go out in a
    single call; a microsecond apart, they go out in order.
    """
    moment = timezone.now()
    rows = [
        Contact(pk=contact_id, touched_at=moment + timedelta(microseconds=index))
        for index, contact_id in enumerate(contact_ids)
    ]
    if rows:
        Contact.objects.bulk_update(rows, ['touched_at'], batch_size=500)


def log_event(contact_id, kind: str, text: str = '', actor=None) -> ContactEvent:
    return ContactEvent.objects.create(
        contact_id=contact_id,
        kind=kind,
        text=(text or '')[:ContactEvent._meta.get_field('text').max_length],
        actor=actor if getattr(actor, 'is_authenticated', False) else None,
    )


def preview(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()[:LAST_MESSAGE_PREVIEW_CHARS]


def user_display_name(user) -> str:
    if user is None:
        return ''
    full = f'{user.first_name} {user.last_name}'.strip()
    return full or (user.email or user.username or '')
