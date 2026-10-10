"""
"וואטסאפ ולידים" — every WhatsApp conversation the business has, in one place.

ManyChat stays the only pipe to the number. It sends Kogo a copy of each
incoming message and of each bot reply (apps/wahub/inbound.py); the office
reads and answers from the CRM (apps/wahub/sending.py), again through ManyChat.

One row per phone (`Contact`), the messages under it, and a journal of what
happened to the contact. Three groups of fields on a contact must never be
confused (docs/WAHUB-CONTRACT.md):

    known_*     what the system worked out from the conversation (analysis.py)
    kogo_*      what the registrations say about this phone (matching.py)
    followup_*  what a person marked. Automatic code never writes these.

The tables hold phones and the text of conversations, so the migration turns
ROW LEVEL SECURITY on for all of them (docs/11-SECURITY-FINDING-ANON-EXPOSURE.md).

Nothing here points at the customers' tables with a foreign key: the family and
the children a phone belongs to are kept as plain ids, a cache refreshed by the
matching. Deleting or merging a family therefore never touches this app.
"""
from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


# --- allowed values, with the Hebrew the screens show ---------------------------

SOURCE_WHATSAPP = 'whatsapp'
SOURCE_MANUAL = 'manual'
SOURCE_CHOICES = [
    (SOURCE_WHATSAPP, 'וואטסאפ'),
    ('ad', 'מודעה'),
    ('broadcast_reply', 'תשובה לתפוצה'),
    ('kogo_trial', 'נרשם לניסיון במערכת'),
    ('kogo_signup', 'נרשם במערכת'),
    ('import', 'ייבוא'),
    (SOURCE_MANUAL, 'הוסף ידנית'),
]

HANDLED_BOT = 'bot'
HANDLED_HUMAN = 'human'
HANDLED_BY_CHOICES = [
    (HANDLED_BOT, 'הבוט עונה'),
    (HANDLED_HUMAN, 'נציג עונה'),
]

TOPIC_CHOICES = [
    ('trial', 'שיעור ניסיון'),
    ('registration', 'רישום לחוג'),
    ('info', 'בקשת פרטים'),
    ('other', 'אחר'),
]

INTEREST_CHOICES = [
    ('hot', 'רוצה להירשם'),
    ('warm', 'מתעניין'),
    ('cold', 'לא מעוניין כרגע'),
    ('none', 'לא ידוע'),
]

FLAG_LABELS = {
    'price': 'המחיר עצר אותו',
    'class_full': 'החוג מלא',
    'lives_far': 'גר רחוק',
    'no_branch_nearby': 'אין סניף באזור',
    'complaint': 'תלונה',
    'says_registered': 'אומר שכבר נרשם',
    'child_too_young': 'הילד צעיר מדי',
    'difficult': 'שיחה מורכבת',
}

ANALYSIS_AI = 'ai'
ANALYSIS_RULES = 'rules'
ANALYSIS_SOURCE_CHOICES = [(ANALYSIS_AI, 'AI'), (ANALYSIS_RULES, 'כללים')]

OUTCOME_NOT_FOUND = 'not_found'
OUTCOME_IN_SYSTEM = 'in_system'
OUTCOME_PENDING = 'pending'
OUTCOME_SIGNUP_DECLINED = 'signup_declined'
OUTCOME_TRIAL_UPCOMING = 'trial_upcoming'
OUTCOME_TRIAL_ONLY = 'trial_only'
OUTCOME_REGISTERED_AFTER = 'registered_after'
OUTCOME_CUSTOMER_BEFORE = 'customer_before'
OUTCOME_CHOICES = [
    (OUTCOME_NOT_FOUND, 'לא נמצא במערכת'),
    (OUTCOME_IN_SYSTEM, 'קיים במערכת בלי רישום'),
    (OUTCOME_PENDING, 'התחיל רישום ולא סיים'),
    (OUTCOME_SIGNUP_DECLINED, 'ניסה להירשם והחיוב נכשל'),
    (OUTCOME_TRIAL_UPCOMING, 'רשום לשיעור ניסיון'),
    (OUTCOME_TRIAL_ONLY, 'עשה ניסיון ולא נרשם'),
    (OUTCOME_REGISTERED_AFTER, 'נרשם אחרי הפנייה'),
    (OUTCOME_CUSTOMER_BEFORE, 'לקוח רשום'),
]
# A registered customer, or somebody whose trial is still ahead, is not a lead.
CUSTOMER_OUTCOMES = (OUTCOME_REGISTERED_AFTER, OUTCOME_CUSTOMER_BEFORE)
HIDDEN_OUTCOMES = CUSTOMER_OUTCOMES + (OUTCOME_TRIAL_UPCOMING,)

FOLLOWUP_WAITING_US = 'waiting_us'
FOLLOWUP_NO_ANSWER = 'no_answer'
FOLLOWUP_ANSWERED = 'answered'
FOLLOWUP_LATER = 'later'
FOLLOWUP_REGISTERED = 'registered'
FOLLOWUP_NOT_RELEVANT = 'not_relevant'
FOLLOWUP_CHOICES = [
    (FOLLOWUP_WAITING_US, 'מחכה לנו'),
    (FOLLOWUP_NO_ANSWER, 'לא ענה'),
    (FOLLOWUP_ANSWERED, 'ענה'),
    (FOLLOWUP_LATER, 'בזמן אחר'),
    (FOLLOWUP_REGISTERED, 'נרשם'),
    (FOLLOWUP_NOT_RELEVANT, 'לא רלוונטי'),
]
# The marks under which "the customer said when to come back" still counts.
CALLBACK_COUNTS_UNDER = ('', FOLLOWUP_LATER, FOLLOWUP_ANSWERED, FOLLOWUP_NO_ANSWER)

DIRECTION_IN = 'in'
DIRECTION_OUT = 'out'
DIRECTION_CHOICES = [(DIRECTION_IN, 'נכנסת'), (DIRECTION_OUT, 'יוצאת')]

SENDER_CUSTOMER = 'customer'
SENDER_BOT = 'bot'
SENDER_OFFICE = 'office'
SENDER_SYSTEM = 'system'
SENDER_CHOICES = [
    (SENDER_CUSTOMER, 'לקוח'),
    (SENDER_BOT, 'בוט'),
    (SENDER_OFFICE, 'משרד'),
    (SENDER_SYSTEM, 'מערכת'),
]

TYPE_TEXT = 'text'
TYPE_TEMPLATE = 'template'
MESSAGE_TYPE_CHOICES = [
    (TYPE_TEXT, 'טקסט'),
    ('voice', 'הודעה קולית'),
    ('image', 'תמונה'),
    (TYPE_TEMPLATE, 'תבנית'),
    ('other', 'אחר'),
]

STATUS_RECEIVED = 'received'
STATUS_SENT = 'sent'
STATUS_FAILED = 'failed'
STATUS_SIMULATED = 'simulated'
MESSAGE_STATUS_CHOICES = [
    (STATUS_RECEIVED, 'התקבלה'),
    (STATUS_SENT, 'נשלחה'),
    (STATUS_FAILED, 'נכשלה'),
    (STATUS_SIMULATED, 'הדמיה'),
]
# A message that went out, as far as the conversation is concerned.
DELIVERED_STATUSES = (STATUS_SENT, STATUS_SIMULATED)

MESSAGE_SOURCE_MANYCHAT = 'manychat'
MESSAGE_SOURCE_KOGO = 'kogo'
MESSAGE_SOURCE_CHOICES = [
    (MESSAGE_SOURCE_MANYCHAT, 'ManyChat'),
    (MESSAGE_SOURCE_KOGO, 'Kogo'),
    ('bot_import', 'ייבוא מהבוט'),
    ('manual', 'ידני'),
]

EVENT_CREATED = 'created'
EVENT_FOLLOWUP = 'followup_changed'
EVENT_TAGS = 'tags_changed'
EVENT_OUTCOME = 'kogo_outcome_changed'
EVENT_ANALYZED = 'analyzed'
EVENT_HANDLED_BY = 'handled_by_changed'
EVENT_NEEDS_HUMAN = 'needs_human_changed'
EVENT_NOTE = 'note'
EVENT_KIND_CHOICES = [
    (EVENT_CREATED, 'נוצר'),
    (EVENT_FOLLOWUP, 'סימון מעקב'),
    (EVENT_TAGS, 'תגיות'),
    (EVENT_OUTCOME, 'מצב במערכת'),
    (EVENT_ANALYZED, 'סיכום אוטומטי'),
    (EVENT_HANDLED_BY, 'מי עונה'),
    (EVENT_NEEDS_HUMAN, 'מבקש נציג'),
    (EVENT_NOTE, 'הערה'),
]

LAST_MESSAGE_PREVIEW_CHARS = 300


class Tag(models.Model):
    name = models.CharField(max_length=60, unique=True, verbose_name='שם')
    color = models.CharField(max_length=9, default='#64748b', verbose_name='צבע')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'wahub_tags'
        ordering = ['name', 'id']
        verbose_name = 'תגית'
        verbose_name_plural = 'תגיות'

    def __str__(self):
        return self.name


class QuickReply(models.Model):
    title = models.CharField(max_length=80, verbose_name='כותרת')
    text = models.TextField(verbose_name='נוסח')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wahub_quick_replies'
        # In the order they were written: the office's own order, and the same
        # on every database (a sort by Hebrew text depends on the collation).
        ordering = ['id']
        verbose_name = 'תשובה מוכנה'
        verbose_name_plural = 'תשובות מוכנות'

    def __str__(self):
        return self.title


class Contact(models.Model):
    # 9725XXXXXXXX — ManyChatService.normalize_phone_e164, Israeli mobiles only.
    phone = models.CharField(max_length=20, unique=True, verbose_name='טלפון')
    name = models.CharField(max_length=200, blank=True, verbose_name='שם')
    manychat_subscriber_id = models.CharField(max_length=40, blank=True, db_index=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_WHATSAPP)

    first_inbound_at = models.DateTimeField(null=True, blank=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    # The last message, as the list shows it — so the list never reads messages.
    last_message_text = models.CharField(max_length=LAST_MESSAGE_PREVIEW_CHARS, blank=True)
    last_message_direction = models.CharField(max_length=3, choices=DIRECTION_CHOICES, blank=True)
    last_message_sender = models.CharField(max_length=10, choices=SENDER_CHOICES, blank=True)
    messages_count = models.PositiveIntegerField(default=0)

    # --- the state of the conversation ---
    unread_count = models.PositiveIntegerField(default=0)
    # The oldest incoming message nobody (bot or office) has answered yet.
    waiting_since = models.DateTimeField(null=True, blank=True)
    handled_by = models.CharField(max_length=10, choices=HANDLED_BY_CHOICES, default=HANDLED_BOT)
    needs_human = models.BooleanField(default=False)
    needs_human_reason = models.CharField(max_length=200, blank=True)
    needs_human_at = models.DateTimeField(null=True, blank=True)

    # --- what is known, written by analysis.py only ---
    known_topic = models.CharField(max_length=20, choices=TOPIC_CHOICES, blank=True)
    known_course_type = models.CharField(max_length=100, blank=True)
    known_city = models.CharField(max_length=100, blank=True)
    known_branch_id = models.UUIDField(null=True, blank=True)
    known_branch_name = models.CharField(max_length=200, blank=True)
    known_child_age = models.CharField(max_length=40, blank=True)
    known_interest = models.CharField(max_length=10, choices=INTEREST_CHOICES, blank=True)
    known_callback_on = models.DateField(null=True, blank=True)
    known_flags = models.JSONField(default=list, blank=True)
    known_summary = models.TextField(blank=True)
    needs_analysis = models.BooleanField(default=False)
    analyzed_at = models.DateTimeField(null=True, blank=True)
    analysis_source = models.CharField(max_length=10, choices=ANALYSIS_SOURCE_CHOICES, blank=True)

    # --- what the registrations say, written by matching.py only ---
    kogo_outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES, blank=True)
    kogo_family_id = models.UUIDField(null=True, blank=True)
    kogo_child_ids = models.JSONField(default=list, blank=True)
    kogo_detail = models.CharField(max_length=300, blank=True)
    kogo_checked_at = models.DateTimeField(null=True, blank=True)

    # --- what a person marked. Never written by automatic code. ---
    followup_status = models.CharField(max_length=20, choices=FOLLOWUP_CHOICES, blank=True)
    followup_due = models.DateField(null=True, blank=True)
    followup_note = models.TextField(blank=True)
    followup_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    followup_at = models.DateTimeField(null=True, blank=True)

    tags = models.ManyToManyField(Tag, blank=True, related_name='contacts', db_table='wahub_contact_tags')

    # Moved forward by every change the screen has to see; the live update
    # (contacts/updates/) reads the rows touched since its cursor.
    touched_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wahub_contacts'
        verbose_name = 'איש קשר'
        verbose_name_plural = 'אנשי קשר'
        indexes = [
            # The two lists, in the order each is read (a contact with no message goes last).
            models.Index(F('last_message_at').desc(nulls_last=True), F('id').desc(), name='wahub_contact_last_msg'),
            models.Index(F('last_inbound_at').desc(nulls_last=True), F('id').desc(), name='wahub_contact_last_in'),
            # What the cron picks up: the few rows waiting for a summary.
            models.Index(
                fields=['last_message_at'], name='wahub_contact_to_analyze',
                condition=Q(needs_analysis=True),
            ),
            models.Index(fields=['kogo_checked_at'], name='wahub_contact_checked'),
        ]

    def __str__(self):
        return self.name or self.phone

    def save(self, *args, **kwargs):
        # A change saved through the model is a change the screen has to see.
        # Query-level updates set touched_at themselves (state.touch).
        self.touched_at = timezone.now()
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            kwargs['update_fields'] = list({*update_fields, 'touched_at', 'updated_at'})
        super().save(*args, **kwargs)


class Message(models.Model):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='messages')
    direction = models.CharField(max_length=3, choices=DIRECTION_CHOICES)
    sender = models.CharField(max_length=10, choices=SENDER_CHOICES)
    # For a message from the office: who sent it.
    sender_name = models.CharField(max_length=150, blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    text = models.TextField(blank=True)
    message_type = models.CharField(max_length=10, choices=MESSAGE_TYPE_CHOICES, default=TYPE_TEXT)
    media_url = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=MESSAGE_STATUS_CHOICES, default=STATUS_RECEIVED)
    error = models.CharField(max_length=300, blank=True)
    sent_at = models.DateTimeField(default=timezone.now)
    external_id = models.CharField(max_length=120, blank=True)
    source = models.CharField(max_length=12, choices=MESSAGE_SOURCE_CHOICES, default=MESSAGE_SOURCE_MANYCHAT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'wahub_messages'
        ordering = ['id']
        verbose_name = 'הודעה'
        verbose_name_plural = 'הודעות'
        indexes = [
            models.Index(fields=['contact', 'id'], name='wahub_message_contact'),
            # The day counts of the "today" tab.
            models.Index(fields=['sent_at'], name='wahub_message_sent_at'),
        ]

    def __str__(self):
        return f'{self.get_direction_display()} · {self.text[:40]}'


class ContactEvent(models.Model):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='events')
    kind = models.CharField(max_length=30, choices=EVENT_KIND_CHOICES)
    text = models.CharField(max_length=500, blank=True)
    # Empty for what happened by itself: a message arriving, a summary, a match.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'wahub_contact_events'
        ordering = ['-id']
        verbose_name = 'אירוע ביומן'
        verbose_name_plural = 'יומן'
        indexes = [
            models.Index(fields=['contact', '-id'], name='wahub_event_contact'),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} · {self.text[:40]}'
