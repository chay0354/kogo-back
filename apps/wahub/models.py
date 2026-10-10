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
SOURCE_DEMO = 'demo'
SOURCE_CHOICES = [
    (SOURCE_WHATSAPP, 'וואטסאפ'),
    ('ad', 'מודעה'),
    ('broadcast_reply', 'תשובה לתפוצה'),
    ('kogo_trial', 'נרשם לניסיון במערכת'),
    ('kogo_signup', 'נרשם במערכת'),
    ('import', 'ייבוא'),
    (SOURCE_MANUAL, 'הוסף ידנית'),
    (SOURCE_DEMO, 'דמו'),
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

    # --- the shadow bot (stage 2): set by inbound.py, cleared by shadow.py ---
    needs_shadow = models.BooleanField(default=False)
    last_shadow_at = models.DateTimeField(null=True, blank=True)
    # An invented contact the owner plays with in production (docs/WAHUB-CONTRACT-STAGE2.md, ה).
    # Nothing is ever sent to it, no office alert is raised for it, and the
    # manager may type its messages in (contacts/{id}/simulate-inbound/).
    is_demo = models.BooleanField(default=False)

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
            # What the shadow bot picks up: the few rows with a customer message it has not answered.
            models.Index(
                fields=['last_inbound_at'], name='wahub_contact_to_shadow',
                condition=Q(needs_shadow=True),
            ),
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


# =====================================================================================
# Stage 2 — the bot's knowledge, the shadow bot, and the review (docs/WAHUB-CONTRACT-STAGE2.md)
# =====================================================================================

KIND_PROFILE = 'profile'
KIND_STYLE_RULE = 'style_rule'
KIND_BEHAVIOR_RULE = 'behavior_rule'
KIND_PHRASING = 'phrasing'
KIND_TOPIC = 'topic'
KIND_FACT = 'fact'
KIND_CONTACT = 'contact'
KIND_LINK = 'link'
KIND_ALIAS = 'alias'
KIND_SPECIAL_DAY = 'special_day'
KIND_OFFICE_HOURS = 'office_hours'
KNOWLEDGE_KIND_CHOICES = [
    (KIND_PROFILE, 'פרופיל הבוט'),
    (KIND_STYLE_RULE, 'כלל עיצוב'),
    (KIND_BEHAVIOR_RULE, 'כלל התנהגות'),
    (KIND_PHRASING, 'נוסח'),
    (KIND_TOPIC, 'נושא'),
    (KIND_FACT, 'עובדה'),
    (KIND_CONTACT, 'איש קשר'),
    (KIND_LINK, 'קישור'),
    (KIND_ALIAS, 'כינוי'),
    (KIND_SPECIAL_DAY, 'יום מיוחד'),
    (KIND_OFFICE_HOURS, 'שעות משרד'),
]
# One live row each.
SINGLETON_KINDS = (KIND_PROFILE, KIND_OFFICE_HOURS)

SCOPE_BUSINESS = 'business'
SCOPE_CITY = 'city'
SCOPE_BRANCH = 'branch'
SCOPE_COURSE_TYPE = 'course_type'
SCOPE_COURSE = 'course'
SCOPE_LEVEL_CHOICES = [
    (SCOPE_BUSINESS, 'כל העסק'),
    (SCOPE_CITY, 'עיר'),
    (SCOPE_BRANCH, 'סניף'),
    (SCOPE_COURSE_TYPE, 'תחום'),
    (SCOPE_COURSE, 'חוג'),
]

WHEN_PROACTIVE = 'proactive'
WHEN_IF_ASKED = 'if_asked'
WHEN_INTERNAL = 'internal'
WHEN_TO_SAY_CHOICES = [
    (WHEN_PROACTIVE, 'מיוזמתו כשרלוונטי'),
    (WHEN_IF_ASKED, 'רק אם שואלים'),
    (WHEN_INTERNAL, 'הנחיה פנימית, לא נאמר ללקוח'),
]

SPECIAL_CLOSED = 'closed'
SPECIAL_OPEN = 'open'
SPECIAL_HOURS = 'hours'
SPECIAL_QUIET = 'quiet'
SPECIAL_STATE_CHOICES = [
    (SPECIAL_CLOSED, 'סגור'),
    (SPECIAL_OPEN, 'פתוח כרגיל'),
    (SPECIAL_HOURS, 'שעות שונות'),
    (SPECIAL_QUIET, 'פעילות שקטה'),
]

SEND_ON_AGENT_REQUEST = 'on_agent_request'
SEND_ALWAYS = 'always'
SEND_ON_TAKEOVER = 'on_takeover'
SEND_MODE_CHOICES = [
    (SEND_ON_AGENT_REQUEST, 'רק כשמבקשים נציג'),
    (SEND_ALWAYS, 'בכל פנייה מחוץ לשעות'),
    (SEND_ON_TAKEOVER, 'כשנציג לוקח שיחה'),
]


class KnowledgeItem(models.Model):
    """
    One thing the bot knows that has no home in Kogo: a rule, a phrasing, a fact,
    a contact, a link, a nickname, a special day, the office hours, the profile.
    Prices, addresses, schedules and capacities are NOT here — the bot reads
    them from Kogo (shadow_tools.py), by the owner's rule of 10.10.2026.

    The fields every kind shares are columns; what one kind alone needs lives in
    `data` and is flattened into the JSON the screen gets (knowledge.py).
    """
    kind = models.CharField(max_length=20, choices=KNOWLEDGE_KIND_CHOICES)
    # phrasing / link: how the code and the other items refer to it ({נוסח:greeting}).
    key = models.CharField(max_length=80, blank=True)
    title = models.CharField(max_length=200, blank=True)
    body = models.TextField(blank=True)
    scope_level = models.CharField(max_length=20, choices=SCOPE_LEVEL_CHOICES, default=SCOPE_BUSINESS)
    # The Kogo id (UUID as text) of the city / branch / course type / course; empty for the business.
    scope_id = models.CharField(max_length=64, blank=True)
    scope_label = models.CharField(max_length=200, blank=True)
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    when_to_say = models.CharField(max_length=20, choices=WHEN_TO_SAY_CHOICES, default=WHEN_PROACTIVE)
    example_good = models.TextField(blank=True)
    example_bad = models.TextField(blank=True)
    # Where it came from. The import of the old bot's texts keys on it, so running
    # the import twice makes nothing twice.
    source_note = models.CharField(max_length=300, blank=True, db_index=True)
    data = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    class Meta:
        db_table = 'wahub_knowledge_items'
        ordering = ['kind', 'id']
        verbose_name = 'רשומת ידע'
        verbose_name_plural = 'ידע הבוט'
        indexes = [models.Index(fields=['kind', 'is_active'], name='wahub_knowledge_kind')]

    def __str__(self):
        return f'{self.get_kind_display()} · {self.title or self.key}'


class KnowledgeHistory(models.Model):
    """Every version of every item: who, when, before, after, why. Restore reads `after`."""
    item = models.ForeignKey(KnowledgeItem, on_delete=models.CASCADE, related_name='history')
    version = models.PositiveIntegerField()
    changed_at = models.DateTimeField(auto_now_add=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(default=dict)
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        db_table = 'wahub_knowledge_history'
        ordering = ['-version', '-id']
        verbose_name = 'גרסה של רשומת ידע'
        verbose_name_plural = 'היסטוריית ידע'
        indexes = [models.Index(fields=['item', '-version'], name='wahub_knowledge_hist_item')]

    def __str__(self):
        return f'#{self.item_id} v{self.version}'


VERDICT_GOOD = 'good'
VERDICT_BAD = 'bad'
VERDICT_CHOICES = [(VERDICT_GOOD, 'טוב'), (VERDICT_BAD, 'לא טוב')]

SHADOW_MODEL_STUB = 'stub'


class ShadowReply(models.Model):
    """
    What the new bot WOULD have answered a customer's message. Never sent.

    One row per customer message (or per burst of messages within twenty
    seconds — `covers_message_ids`). The owner marks it good or bad; "bad" with
    a note becomes a proposal to fix the knowledge (reviewer.py).
    """
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='shadow_replies')
    after_message = models.ForeignKey(Message, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    covers_message_ids = models.JSONField(default=list, blank=True)
    text = models.TextField(blank=True)
    # Short, in Hebrew: why this answer. Shown on click.
    reasoning = models.TextField(blank=True)
    # [{name, input, summary}]
    tools_used = models.JSONField(default=list, blank=True)
    # [knowledge item ids]
    knowledge_used = models.JSONField(default=list, blank=True)
    took_ms = models.PositiveIntegerField(default=0)
    # "stub" when no Anthropic key was set and the answer was built from the tools alone.
    model = models.CharField(max_length=60, blank=True)
    request_human = models.BooleanField(default=False)
    request_human_reason = models.CharField(max_length=200, blank=True)
    verdict = models.CharField(max_length=10, choices=VERDICT_CHOICES, blank=True)
    verdict_note = models.TextField(blank=True)
    verdict_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    verdict_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'wahub_shadow_replies'
        ordering = ['-id']
        verbose_name = 'תשובת צל'
        verbose_name_plural = 'תשובות צל'
        indexes = [
            models.Index(fields=['contact', '-id'], name='wahub_shadow_contact'),
            models.Index(fields=['created_at'], name='wahub_shadow_created'),
        ]

    def __str__(self):
        return f'{self.contact_id} · {self.text[:40]}'


class TrialQuestion(models.Model):
    """"נסה שאלה" on the knowledge screen, kept when the manager asked to keep it."""
    question = models.TextField()
    contact = models.ForeignKey(Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    pretend_now = models.DateTimeField(null=True, blank=True)
    last_outbound = models.TextField(blank=True)
    answer = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'wahub_trial_questions'
        ordering = ['-id']
        verbose_name = 'שאלת ניסיון'
        verbose_name_plural = 'שאלות ניסיון'

    def __str__(self):
        return self.question[:60]


PROPOSAL_PENDING = 'pending'
PROPOSAL_APPROVED = 'approved'
PROPOSAL_REJECTED = 'rejected'
PROPOSAL_APPLIED = 'applied'
PROPOSAL_STATUS_CHOICES = [
    (PROPOSAL_PENDING, 'ממתין לאישור'),
    (PROPOSAL_APPROVED, 'אושר'),
    (PROPOSAL_REJECTED, 'נדחה'),
    (PROPOSAL_APPLIED, 'הוחל על הידע'),
]

SOURCE_HUMAN_OVERRIDE = 'human_override'
SOURCE_BAD_VERDICT = 'bad_verdict'
SOURCE_SERVICE_NOTE = 'service_note'
SOURCE_REVIEWER = 'reviewer'
PROPOSAL_SOURCE_CHOICES = [
    (SOURCE_HUMAN_OVERRIDE, 'נציג ענה מעל הבוט'),
    (SOURCE_BAD_VERDICT, 'סימון 👎 על תשובת צל'),
    (SOURCE_SERVICE_NOTE, 'הערת שירות'),
    (SOURCE_REVIEWER, 'סריקה יזומה של השיחות'),
]


class KnowledgeProposal(models.Model):
    """
    "האם אתה רוצה שאני אשנה את ההגדרות?" — a change to the knowledge that waits
    for a click. Nothing here touches a KnowledgeItem until approve() runs.
    """
    status = models.CharField(max_length=10, choices=PROPOSAL_STATUS_CHOICES, default=PROPOSAL_PENDING)
    source = models.CharField(max_length=20, choices=PROPOSAL_SOURCE_CHOICES)
    # For the reviewer: which pattern fired.
    pattern = models.CharField(max_length=40, blank=True)
    contact = models.ForeignKey(Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='proposals')
    message = models.ForeignKey(Message, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    shadow = models.ForeignKey(ShadowReply, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    title = models.CharField(max_length=200)
    explanation = models.TextField(blank=True)
    # {action: create|update, item_id, kind, before, after}
    change = models.JSONField(default=dict, blank=True)
    # [{message_id, who, text}]
    evidence = models.JSONField(default=list, blank=True)
    # source + contact + day: the same cause never makes two proposals in one day.
    dedup_key = models.CharField(max_length=200, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    decision_note = models.TextField(blank=True)
    applied_item = models.ForeignKey(
        KnowledgeItem, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    class Meta:
        db_table = 'wahub_knowledge_proposals'
        ordering = ['-id']
        verbose_name = 'הצעת עדכון לבוט'
        verbose_name_plural = 'הצעות עדכון לבוט'
        indexes = [models.Index(fields=['status', '-id'], name='wahub_proposal_status')]

    def __str__(self):
        return f'{self.get_status_display()} · {self.title}'


class ServiceNote(models.Model):
    """"היי, שימי לב, הבוט עשה ככה וככה" — a line from the office, turned into a proposal."""
    text = models.TextField()
    contact = models.ForeignKey(Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    message = models.ForeignKey(Message, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    proposal = models.ForeignKey(KnowledgeProposal, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'wahub_service_notes'
        ordering = ['-id']
        verbose_name = 'הערת שירות'
        verbose_name_plural = 'הערות שירות'

    def __str__(self):
        return self.text[:60]
