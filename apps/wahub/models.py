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

A fourth group is read from those three and never the other way round:

    lead_*      the one stage of the lead and who makes the next move, by rules
                (lead_stage.py). Three of them are kept by somebody else:
                `lead_ai` — what the conversation says about the lead — by the
                summary (analysis.py); `lead_manual` — what a person decided on
                the leads screen — by its endpoints (leads.py); `lead_bot`
                — where the bot's own follow-ups stand — by lead_followups.py.

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

# --- the lead stage (docs/WAHUB-CONTRACT-LEAD-STAGES.md, docs/WAHUB-CONTRACT-LEADS-V2.md) -----
# One stage per contact. The rules that pick it, and the sentence each stage
# shows, are in apps/wahub/lead_stage.py. The three groups are what the first
# board shows (leads/board/); the leads screen that follows it reads who makes
# the next move instead (`lead_owner`).

LEAD_GROUP_ACT = 'act'
LEAD_GROUP_WAIT = 'wait'
LEAD_GROUP_CLOSED = 'closed'
LEAD_GROUP_CHOICES = [
    (LEAD_GROUP_ACT, 'דורש טיפול'),
    (LEAD_GROUP_WAIT, 'בתהליך'),
    (LEAD_GROUP_CLOSED, 'סגור'),
]

LEAD_NEEDS_REPLY = 'needs_reply'
LEAD_SIGNUP_STUCK = 'signup_stuck'
LEAD_SIGNUP_FAILED = 'signup_failed'
LEAD_SIGNUP_STARTED = 'signup_started'
LEAD_LINK_SENT = 'link_sent'
LEAD_TRIAL_NO_SHOW = 'trial_no_show'
LEAD_TRIAL_DONE = 'trial_done'
LEAD_CALLBACK_DUE = 'callback_due'
LEAD_ANSWERED_SILENT = 'answered_silent'
LEAD_WILL_CHECK = 'will_check'
LEAD_TRIAL_BOOKED = 'trial_booked'
LEAD_WAITING_DATE = 'waiting_date'
# On the waiting list of one lesson that is full (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ד).
LEAD_WAITING_LIST = 'waiting_list'
LEAD_NOTHING_TO_OFFER = 'nothing_to_offer'
LEAD_IN_CONVERSATION = 'in_conversation'
LEAD_REGISTERED = 'registered'
LEAD_CUSTOMER = 'customer'
LEAD_NOT_INTERESTED = 'not_interested'
LEAD_DELETED = 'deleted'
LEAD_COLD = 'cold'
# In the order the board shows them, group by group.
LEAD_STAGE_CHOICES = [
    (LEAD_SIGNUP_STUCK, 'נתקע בהרשמה'),
    (LEAD_NEEDS_REPLY, 'שאל ולא קיבל מענה'),
    (LEAD_SIGNUP_FAILED, 'ניסה להירשם והחיוב נכשל'),
    (LEAD_SIGNUP_STARTED, 'התחיל הרשמה ולא סיים'),
    (LEAD_LINK_SENT, 'קיבל קישור ועוד לא נרשם'),
    (LEAD_TRIAL_NO_SHOW, 'נרשם לניסיון ולא הגיע'),
    (LEAD_TRIAL_DONE, 'עשה ניסיון ולא נרשם'),
    (LEAD_CALLBACK_DUE, 'הגיע הזמן לחזור אליו'),
    (LEAD_ANSWERED_SILENT, 'קיבל תשובה ולא נרשם'),
    (LEAD_WILL_CHECK, 'אמר שיבדוק ויחזור'),
    (LEAD_TRIAL_BOOKED, 'רשום לשיעור ניסיון'),
    (LEAD_WAITING_DATE, 'מחכה למועד'),
    (LEAD_WAITING_LIST, 'ברשימת המתנה'),
    (LEAD_NOTHING_TO_OFFER, 'אין כרגע מה להציע'),
    (LEAD_IN_CONVERSATION, 'בשיחה'),
    (LEAD_REGISTERED, 'נרשם'),
    (LEAD_CUSTOMER, 'לקוח קיים'),
    (LEAD_NOT_INTERESTED, 'לא מעוניין'),
    (LEAD_DELETED, 'נמחק'),
    (LEAD_COLD, 'רדום'),
]
LEAD_STAGE_GROUPS = {
    LEAD_SIGNUP_STUCK: LEAD_GROUP_ACT,
    LEAD_NEEDS_REPLY: LEAD_GROUP_ACT,
    LEAD_SIGNUP_FAILED: LEAD_GROUP_ACT,
    LEAD_SIGNUP_STARTED: LEAD_GROUP_ACT,
    LEAD_LINK_SENT: LEAD_GROUP_ACT,
    LEAD_TRIAL_NO_SHOW: LEAD_GROUP_ACT,
    LEAD_TRIAL_DONE: LEAD_GROUP_ACT,
    LEAD_CALLBACK_DUE: LEAD_GROUP_ACT,
    LEAD_ANSWERED_SILENT: LEAD_GROUP_ACT,
    LEAD_WILL_CHECK: LEAD_GROUP_WAIT,
    LEAD_TRIAL_BOOKED: LEAD_GROUP_WAIT,
    LEAD_WAITING_DATE: LEAD_GROUP_WAIT,
    LEAD_WAITING_LIST: LEAD_GROUP_WAIT,
    LEAD_NOTHING_TO_OFFER: LEAD_GROUP_WAIT,
    LEAD_IN_CONVERSATION: LEAD_GROUP_WAIT,
    LEAD_REGISTERED: LEAD_GROUP_CLOSED,
    LEAD_CUSTOMER: LEAD_GROUP_CLOSED,
    LEAD_NOT_INTERESTED: LEAD_GROUP_CLOSED,
    LEAD_DELETED: LEAD_GROUP_CLOSED,
    LEAD_COLD: LEAD_GROUP_CLOSED,
}
# What a stage was called before 12.10.2026, on a row the cron has not looked at since.
LEGACY_LEAD_STAGES = {'not_relevant': LEAD_NOT_INTERESTED}

# Who makes the next move on a lead (docs/WAHUB-CONTRACT-LEADS-V2.md, ב). '' — nobody
# has a step now, or the lead is closed.
LEAD_OWNER_BOT = 'bot'
LEAD_OWNER_OFFICE = 'office'
LEAD_OWNER_CHOICES = [(LEAD_OWNER_BOT, 'הבוט מטפל'), (LEAD_OWNER_OFFICE, 'מחכה למשרד')]

# The two ways a person closes a lead on the leads screen (`lead_manual`).
LEAD_CLOSE_DELETED = 'deleted'
LEAD_CLOSE_NOT_INTERESTED = 'not_interested'
LEAD_CLOSE_CHOICES = [(LEAD_CLOSE_DELETED, 'נמחק'), (LEAD_CLOSE_NOT_INTERESTED, 'לא מעוניין')]

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
TYPE_VOICE = 'voice'
TYPE_IMAGE = 'image'
TYPE_TEMPLATE = 'template'
# A file ManyChat passed that is neither a recording nor a picture we know, or
# media it did not pass at all (docs/WAHUB-CONTRACT-LIVE-TEST.md, ב).
TYPE_OTHER = 'other'
MESSAGE_TYPE_CHOICES = [
    (TYPE_TEXT, 'טקסט'),
    (TYPE_VOICE, 'הודעה קולית'),
    (TYPE_IMAGE, 'תמונה'),
    (TYPE_TEMPLATE, 'תבנית'),
    (TYPE_OTHER, 'אחר'),
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
EVENT_LIVE_TEST = 'live_test'
EVENT_LEAD_STAGE = 'lead_stage_changed'
EVENT_LEAD_FOLLOWUP = 'lead_followup'
EVENT_LEAD_ACTION = 'lead_action'
# A person closed the conversation (✓), and what happened to a place on a waiting list
# (docs/WAHUB-CONTRACT-FUTURE-LEADS.md).
EVENT_CLOSED = 'conversation_closed'
EVENT_WAITING_LIST = 'waiting_list'
# A note about the customer was rewritten or deleted (the same contract, ז.5). The line says who and
# when, never the words: a note that was deleted is gone.
EVENT_NOTE_CHANGED = 'note_changed'
EVENT_KIND_CHOICES = [
    (EVENT_CREATED, 'נוצר'),
    (EVENT_FOLLOWUP, 'סימון מעקב'),
    (EVENT_TAGS, 'תגיות'),
    (EVENT_OUTCOME, 'מצב במערכת'),
    (EVENT_ANALYZED, 'סיכום אוטומטי'),
    (EVENT_HANDLED_BY, 'מי עונה'),
    (EVENT_NEEDS_HUMAN, 'מבקש נציג'),
    (EVENT_NOTE, 'הערה'),
    (EVENT_LIVE_TEST, 'בדיקה חיה'),
    (EVENT_LEAD_STAGE, 'מצב הליד'),
    (EVENT_LEAD_FOLLOWUP, 'חזרה של הבוט לליד'),
    (EVENT_LEAD_ACTION, 'פעולה על הליד'),
    (EVENT_CLOSED, 'השיחה נסגרה'),
    (EVENT_WAITING_LIST, 'רשימת המתנה'),
    (EVENT_NOTE_CHANGED, 'שינוי בפתק'),
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
    # The trial behind a trial_upcoming / trial_only answer, as facts and not as the
    # sentence above: {on: 'YYYY-MM-DD', outcome: 'attended'|'no_show'|'', course: its name}.
    # Empty for every other answer, and for a trial known only from the child's status.
    kogo_trial = models.JSONField(default=dict, blank=True)
    kogo_checked_at = models.DateTimeField(null=True, blank=True)

    # --- the shadow bot (stage 2): set by inbound.py, cleared by shadow.py ---
    needs_shadow = models.BooleanField(default=False)
    last_shadow_at = models.DateTimeField(null=True, blank=True)
    # An invented contact the owner plays with in production (docs/WAHUB-CONTRACT-STAGE2.md, ה).
    # Nothing is ever sent to it, no office alert is raised for it, and the
    # manager may type its messages in (contacts/{id}/simulate-inbound/).
    is_demo = models.BooleanField(default=False)

    # --- the live test (docs/WAHUB-CONTRACT-LIVE-TEST.md): the owner's own number ---
    # The old bot is muted for this contact in ManyChat and the NEW bot answers it
    # for real, through ManyChat, even while the general sending switch is off
    # (sending.sending_enabled). Turned on and off by a manager (handoff.py).
    live_test = models.BooleanField(default=False)
    # A live answer in progress claims the contact until this moment, so a second
    # call for the same burst answers "busy" instead of answering twice (live_test.py).
    live_test_busy_until = models.DateTimeField(null=True, blank=True)

    # --- what a person marked. Never written by automatic code. ---
    followup_status = models.CharField(max_length=20, choices=FOLLOWUP_CHOICES, blank=True)
    followup_due = models.DateField(null=True, blank=True)
    followup_note = models.TextField(blank=True)
    followup_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    followup_at = models.DateTimeField(null=True, blank=True)

    # --- the lead stage, written by lead_stage.py only (docs/WAHUB-CONTRACT-LEAD-STAGES.md) ---
    # One stage, picked by rules from the three groups above; '' until the first look.
    lead_stage = models.CharField(max_length=20, choices=LEAD_STAGE_CHOICES, blank=True)
    # Since when: the moment of the fact behind the stage where there is one
    # (the unanswered message, the last message, the trial), else when the stage was first seen.
    lead_stage_since = models.DateTimeField(null=True, blank=True)
    lead_reason = models.CharField(max_length=300, blank=True)
    lead_next_step = models.CharField(max_length=200, blank=True)
    # Somebody from the office has ever written to this contact from the CRM.
    lead_human_touched = models.BooleanField(default=False)
    lead_checked_at = models.DateTimeField(null=True, blank=True)
    # When the clock alone may change the answer (30 minutes with no reply, 48 hours of
    # quiet, the day to come back, midnight for "yesterday"): the cron looks again then.
    lead_recheck_at = models.DateTimeField(null=True, blank=True)

    # --- leads, version 2 (docs/WAHUB-CONTRACT-LEADS-V2.md) ---
    # Who makes the next move: the bot, the office, or '' (no step now, or closed). lead_stage.py.
    lead_owner = models.CharField(max_length=10, choices=LEAD_OWNER_CHOICES, blank=True)
    # What the conversation says about the lead, worked out in the summary pass (analysis.py —
    # the only writer; apps/wahub/lead_ai.py describes the keys): interest, what holds them
    # back, the child, when to come back, one sentence of "why is this a lead". {} until the
    # pass has reached the contact.
    lead_ai = models.JSONField(default=dict, blank=True)
    # The branch, the kind of class and the day to come back, out of lead_ai, as columns the
    # lists filter by. lead_stage.py.
    lead_branch_id = models.UUIDField(null=True, blank=True)
    lead_course_type = models.CharField(max_length=100, blank=True)
    lead_come_back_on = models.DateField(null=True, blank=True)
    # A registration link went out to this contact, in any outgoing message Kogo saw: the
    # newest of them (inbound.py, sending.py; the summary fills it for older conversations).
    lead_link_sent_at = models.DateTimeField(null=True, blank=True)
    # What a person decided on the leads screen — {closed: {reason, note, at, by, by_name},
    # owner: {value: 'office', at, by, by_name}, reopened_at}. Written by its endpoints only
    # (leads.py); the marks of the older screen stay in followup_*.
    lead_manual = models.JSONField(default=dict, blank=True)
    # Where the bot's own follow-ups stand in the lead's present round — {episode, started_at,
    # step, attempts, last_at, done, note}. Written by lead_followups.py only.
    lead_bot = models.JSONField(default=dict, blank=True)
    # Which version of the rules last worked the stage out; an older one is looked at again.
    lead_version = models.PositiveSmallIntegerField(default=0)
    # The contact's place on a waiting list, as the stage and the lists read it without another
    # query — {status: waiting | freed, entry_id, lesson_id, label, course, day, start, branch,
    # child, since, freed_at, more}; {} when the contact waits for no lesson. A copy of the rows
    # of WaitingListEntry, written by waiting_list.py only.
    lead_waiting = models.JSONField(default=dict, blank=True)

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
            # The board's counts and the list of one stage, longest in the stage first.
            models.Index(fields=['lead_stage', 'lead_stage_since'], name='wahub_contact_lead_stage'),
            # What the cron looks at again because time passed.
            models.Index(
                fields=['lead_recheck_at'], name='wahub_contact_lead_recheck',
                condition=Q(lead_recheck_at__isnull=False),
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
    # A demo contact's recording or picture: the object's path in the private
    # bucket (apps/wahub/media.py), never a public address. The screen asks
    # messages/{id}/media/ for a short-lived signed URL.
    media_url = models.TextField(blank=True)
    # What a recording said, when a transcription key was set (apps/wahub/transcribe.py).
    transcript = models.TextField(blank=True)
    # The NEW bot's answer: written into a demo conversation (docs/WAHUB-CONTRACT-REAL-DEMO.md, ב;
    # sender "bot", status "simulated", never sent), or sent for real to a live-test
    # contact (docs/WAHUB-CONTRACT-LIVE-TEST.md; the status is the send's). The old
    # bot's real replies carry False.
    new_bot = models.BooleanField(default=False)
    # ManyChat's body as it came, kept for a live-test contact's incoming messages
    # only (docs/WAHUB-CONTRACT-LIVE-TEST.md, ב) — to learn how a recording or a
    # picture arrives. Null for everybody else.
    raw = models.JSONField(null=True, blank=True)
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
    # A note (`kind` "note") a person rewrote: when, and who. Empty for every other line and for a
    # note as it was first written — a line of the journal is otherwise never changed.
    # No index: nothing looks a line up by who rewrote it, and building one would hold the whole
    # journal locked while the migration runs.
    edited_at = models.DateTimeField(null=True, blank=True)
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', db_index=False,
    )

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
# Taken back: an applied change undone, or a waiting one withdrawn (docs/WAHUB-CONTRACT-REFINE.md).
PROPOSAL_UNDONE = 'undone'
PROPOSAL_STATUS_CHOICES = [
    (PROPOSAL_PENDING, 'ממתין לאישור'),
    (PROPOSAL_APPROVED, 'אושר'),
    (PROPOSAL_REJECTED, 'נדחה'),
    (PROPOSAL_APPLIED, 'הוחל על הידע'),
    (PROPOSAL_UNDONE, 'בוטל'),
]

SOURCE_HUMAN_OVERRIDE = 'human_override'
SOURCE_BAD_VERDICT = 'bad_verdict'
SOURCE_SERVICE_NOTE = 'service_note'
SOURCE_REVIEWER = 'reviewer'
SOURCE_REFINE = 'refine'
PROPOSAL_SOURCE_CHOICES = [
    (SOURCE_HUMAN_OVERRIDE, 'נציג ענה מעל הבוט'),
    (SOURCE_BAD_VERDICT, 'סימון 👎 על תשובת צל'),
    (SOURCE_SERVICE_NOTE, 'הערת שירות'),
    (SOURCE_REVIEWER, 'סריקה יזומה של השיחות'),
    (SOURCE_REFINE, 'דיוק'),
]


class KnowledgeProposal(models.Model):
    """
    "האם אתה רוצה שאני אשנה את ההגדרות?" — a change to the knowledge. It waits
    for a click, unless the owner's standing approval of 11.10.2026 covers it
    (reviewer.py, "automatic approval"): then it is applied as it is made, marked
    `auto_applied`, and can be taken back (`undone`).
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
    # {action: create|update|deactivate, item_id, kind, before, after}
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
    # Applied with no click, under the owner's standing approval.
    auto_applied = models.BooleanField(default=False)
    # The version this proposal gave the item — what undo reads. 0: applying it
    # changed nothing; null: applied before versions were kept here.
    applied_version = models.PositiveIntegerField(null=True, blank=True)
    undone_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
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


# =====================================================================================
# "דיוק" — fixing the bot in free text, and the automatic approval (docs/WAHUB-CONTRACT-REFINE.md)
# =====================================================================================

class WahubSetting(models.Model):
    """
    A switch or a marker of this app that is neither knowledge (KnowledgeItem:
    it would be read to the bot) nor a secret (IntegrationCredential: write-only,
    and the environment overrides it): `review_auto_apply`, and the place the
    one-time sweep over the old conversations has reached. Read and written
    through settings_store.py only.
    """
    key = models.CharField(max_length=64, unique=True)
    value = models.JSONField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    class Meta:
        db_table = 'wahub_settings'
        verbose_name = 'הגדרה של וואטסאפ ולידים'
        verbose_name_plural = 'הגדרות של וואטסאפ ולידים'

    def __str__(self):
        return self.key


REFINE_OPEN = 'open'
REFINE_DONE = 'done'
REFINE_STATUS_CHOICES = [(REFINE_OPEN, 'פתוח'), (REFINE_DONE, 'הסתיים')]

TURN_OPERATOR = 'operator'
TURN_SYSTEM = 'system'
TURN_ROLE_CHOICES = [(TURN_OPERATOR, 'מפעילה'), (TURN_SYSTEM, 'המערכת')]


class RefineThread(models.Model):
    """
    One "דיוק": the operator picked messages of a conversation and wrote, in her
    own words, what was wrong — or pressed "דיוק" on a proposal that already
    exists (`proposal`) and wrote what to change in it. The small chat that
    follows is the turns; what it changed in the knowledge are KnowledgeProposal
    rows whose ids the system turns carry.
    """
    contact = models.ForeignKey(Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='refine_threads')
    # A "דיוק" on a proposal: the one she pressed it on. Waiting — it is rewritten
    # in place; already applied — its item in the knowledge is what gets edited.
    proposal = models.ForeignKey(
        KnowledgeProposal, on_delete=models.SET_NULL, null=True, blank=True, related_name='refine_threads',
    )
    # The messages she picked, and (when the screen pointed at one) the shadow replies.
    message_ids = models.JSONField(default=list, blank=True)
    shadow_ids = models.JSONField(default=list, blank=True)
    # open: the system asked, or nothing of the thread stands; done: there is an outcome.
    status = models.CharField(max_length=10, choices=REFINE_STATUS_CHOICES, default=REFINE_OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wahub_refine_threads'
        ordering = ['-id']
        verbose_name = 'דיוק'
        verbose_name_plural = 'דיוקים'
        indexes = [models.Index(fields=['contact', '-id'], name='wahub_refine_contact')]

    def __str__(self):
        return f'דיוק #{self.pk} · {self.get_status_display()}'


class RefineTurn(models.Model):
    thread = models.ForeignKey(RefineThread, on_delete=models.CASCADE, related_name='turns')
    role = models.CharField(max_length=10, choices=TURN_ROLE_CHOICES)
    text = models.TextField(blank=True)
    # A system turn: what it proposed — [{proposal_id, action, kind, kind_label, title, before, after}].
    # Whether each still stands is read from the proposal, never from here.
    changes = models.JSONField(default=list, blank=True)
    # A system turn that ends with a question to the operator.
    needs_answer = models.BooleanField(default=False)
    # A system turn: {outcome, understood, question, confidence, earlier_changes, model, took_ms}.
    # The turn the system opens a thread with (no text came with the request): {outcome: "summary",
    # summary: {what_happened, what_was_answered, suspected_issue}, model, took_ms}.
    meta = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'wahub_refine_turns'
        ordering = ['id']
        verbose_name = 'הודעה בדיוק'
        verbose_name_plural = 'הודעות בדיוק'
        indexes = [models.Index(fields=['thread', 'id'], name='wahub_refine_turn_thread')]

    def __str__(self):
        return f'{self.get_role_display()} · {self.text[:40]}'


# =====================================================================================
# "בדיקות הבוט" — the bot's self-checks (docs/WAHUB-CONTRACT-BOT-EVALS.md)
# =====================================================================================

EVAL_RUNNING = 'running'
EVAL_PASSED = 'passed'
EVAL_FAILED = 'failed'
EVAL_SKIPPED = 'skipped'
EVAL_ERROR = 'error'
EVAL_STATUS_CHOICES = [
    (EVAL_RUNNING, 'רץ'),
    (EVAL_PASSED, 'עבר'),
    (EVAL_FAILED, 'נכשל'),
    (EVAL_SKIPPED, 'דולג'),
    (EVAL_ERROR, 'לא רץ'),
]
EVAL_FINAL_STATUSES = (EVAL_PASSED, EVAL_FAILED, EVAL_SKIPPED, EVAL_ERROR)


class BotEvalRun(models.Model):
    """
    One run of one written conversation against the bot (apps/wahub/evals/).
    The conversation is played in memory: no contact, no message and no shadow
    reply is made by it, and nothing is sent. What is kept is this row alone —
    what the customer of the script wrote, what the bot answered, the tools it
    called with what they returned, and every check with its verdict.

    A run may take more than one request (a server function is cut after a
    minute): `turns` holds what was played so far, and the next request
    continues from there with the same `world`.
    """
    scenario_id = models.CharField(max_length=80)
    status = models.CharField(max_length=10, choices=EVAL_STATUS_CHOICES, default=EVAL_RUNNING)
    # Who answered: a Claude model id, or "stub" on a machine with no key.
    engine = models.CharField(max_length=80, blank=True)
    # The words and the facts the script was filled with, read from the system when the run began.
    world = models.JSONField(default=dict, blank=True)
    # [{customer: [{text, kind}], at, reply, reasoning, tools: [{name, input, summary, result}], …}]
    turns = models.JSONField(default=list, blank=True)
    # [{id, turn, kind: hard | rubric, title, status: passed | failed | not_judged, explain}]
    checks = models.JSONField(default=list, blank=True)
    # Why it was skipped or did not run, in plain Hebrew.
    detail = models.TextField(blank=True)
    # How many times a turn was tried again after the model did not answer in time.
    attempts = models.PositiveSmallIntegerField(default=0)
    took_ms = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'wahub_bot_eval_runs'
        ordering = ['-id']
        verbose_name = 'בדיקת בוט'
        verbose_name_plural = 'בדיקות הבוט'
        indexes = [models.Index(fields=['scenario_id', '-id'], name='wahub_eval_scenario')]

    def __str__(self):
        return f'{self.scenario_id} · {self.get_status_display()}'


# =====================================================================================
# Leads, version 2 — the bot's own follow-ups (docs/WAHUB-CONTRACT-LEADS-V2.md, ג)
# =====================================================================================

NUDGE_LINK_REMINDER = 'link_reminder'
NUDGE_WILL_CHECK = 'will_check'
NUDGE_TRIAL_FEEDBACK = 'trial_feedback'
NUDGE_TRIAL_NO_SHOW = 'trial_no_show'
NUDGE_DATE_ARRIVED = 'date_arrived'
NUDGE_OPENING = 'opening'
NUDGE_DORMANT = 'dormant'
NUDGE_KIND_CHOICES = [
    (NUDGE_LINK_REMINDER, 'קיבל קישור ועוד לא נרשם'),
    (NUDGE_WILL_CHECK, 'אמר שיבדוק ויחזור'),
    (NUDGE_TRIAL_FEEDBACK, 'אחרי שיעור ניסיון'),
    (NUDGE_TRIAL_NO_SHOW, 'לא הגיע לשיעור ניסיון'),
    (NUDGE_DATE_ARRIVED, 'הגיע המועד שדובר עליו'),
    (NUDGE_OPENING, 'פנה ולא כתב מה הוא מחפש'),
    (NUDGE_DORMANT, 'ליד רדום — נפתחה הרשמה'),
]

NUDGE_PLANNED = 'planned'
NUDGE_DUE = 'due'
NUDGE_SENT = 'sent'
NUDGE_SKIPPED = 'skipped'
NUDGE_FAILED = 'failed'
NUDGE_STATUS_CHOICES = [
    (NUDGE_PLANNED, 'מתוכנן'),
    (NUDGE_DUE, 'הגיע הזמן — לא נשלח'),
    (NUDGE_SENT, 'נשלח'),
    (NUDGE_SKIPPED, 'בוטל'),
    (NUDGE_FAILED, 'נכשל'),
]
# Still ahead of the bot: at most one of these per contact (the constraint below).
NUDGE_OPEN_STATUSES = (NUDGE_PLANNED, NUDGE_DUE)


class LeadFollowUp(models.Model):
    """
    One time the bot comes back to a lead by itself: planned for a moment inside
    the office hours, with the text already written. When the moment comes it
    goes out only if a message may leave for this contact (sending.sending_enabled)
    and WhatsApp allows it (free text within 24 hours of the customer's last
    message, otherwise the approved template mapped to the kind); if not, it
    stays `due` — "the bot would have sent this now" — and nothing leaves.
    apps/wahub/lead_followups.py is the only writer.
    """
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='lead_followups')
    kind = models.CharField(max_length=20, choices=NUDGE_KIND_CHOICES)
    # 1–3: which of the round's attempts this is.
    step = models.PositiveSmallIntegerField(default=1)
    # The round of the lead this belongs to (lead_stage.compute names it). A new message from
    # the customer, a registration, a closure start another round or none — and this one is skipped.
    episode = models.CharField(max_length=160, blank=True)
    due_at = models.DateTimeField()
    # When the cron looks at the row next: `due_at` while planned; a `due` row is tried again later.
    next_look_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=NUDGE_STATUS_CHOICES, default=NUDGE_PLANNED)
    # What would be said, written when it was planned — no model is asked at sending time.
    text = models.TextField(blank=True)
    # The name of the kind's WhatsApp template in the mapping (settings_store.lead_templates).
    template_key = models.CharField(max_length=40, blank=True)
    # One sentence: why the bot comes back.
    why = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    acted_at = models.DateTimeField(null=True, blank=True)
    # What happened: why it was not sent, why it was skipped, the send's own error.
    outcome_note = models.CharField(max_length=300, blank=True)
    # A cron call that took the row to act on it. Two calls never act on one row; a claim
    # nobody released is a send whose outcome is unknown, and it is never tried again.
    claimed_at = models.DateTimeField(null=True, blank=True)
    # The message that went out (or was written into a demo conversation).
    message = models.ForeignKey(Message, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'wahub_lead_followups'
        ordering = ['-id']
        verbose_name = 'חזרה לליד'
        verbose_name_plural = 'חזרות ללידים'
        indexes = [
            models.Index(fields=['contact', '-id'], name='wahub_nudge_contact'),
            # What the cron picks up: the few rows whose moment has come.
            models.Index(
                fields=['next_look_at'], name='wahub_nudge_open',
                condition=Q(status__in=NUDGE_OPEN_STATUSES),
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['contact'], condition=Q(status__in=NUDGE_OPEN_STATUSES), name='wahub_nudge_one_open',
            ),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} · {self.get_status_display()}'


# =====================================================================================
# The waiting list of a lesson that is full (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ד)
# =====================================================================================

WAITING_WAITING = 'waiting'
WAITING_FREED = 'freed'
WAITING_HANDLED = 'handled'
WAITING_REMOVED = 'removed'
WAITING_STATUS_CHOICES = [
    (WAITING_WAITING, 'ממתין'),
    (WAITING_FREED, 'התפנה מקום'),
    (WAITING_HANDLED, 'טופל'),
    (WAITING_REMOVED, 'הוסר'),
]
# Still on the list: at most one of these per contact and lesson (the constraint below).
WAITING_ACTIVE_STATUSES = (WAITING_WAITING, WAITING_FREED)

WAITING_BY_BOT = 'bot'
WAITING_BY_PERSON = 'person'
WAITING_BY_CHOICES = [(WAITING_BY_BOT, 'הבוט'), (WAITING_BY_PERSON, 'נציג')]

# How the customer was told that a seat came free: free text inside WhatsApp's 24 hours, an
# approved template after them.
WAITING_TOLD_TEXT = 'text'
WAITING_TOLD_TEMPLATE = 'template'
WAITING_TOLD_CHOICES = [(WAITING_TOLD_TEXT, 'הודעה רגילה'), (WAITING_TOLD_TEMPLATE, 'תבנית')]


class WaitingListEntry(models.Model):
    """
    A contact who wants one lesson that is full — the class, the day, the hour
    and the branch — put on its waiting list by the bot (the tool
    `join_waiting_list`) or by the office. The cron looks at every waiting row
    with the very count the registration form uses; when a seat is free the row
    becomes `freed` and the lead goes to the office's list. Nothing is ever
    sent by itself, to the customer or to the office: the customer is told only
    when a person presses for this very row (apps/wahub/waiting_notify.py), and
    `notified_*` keeps when, by whom and how.

    Like the rest of the app, the lesson is kept as a plain id with a copy of
    what the screen shows, not as a foreign key: deleting or moving a lesson
    never touches this table, and a row whose lesson is gone still says what
    was waited for. apps/wahub/waiting_list.py is the only writer — but for
    `notified_*`, which waiting_notify.py writes.
    """
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='waiting_list')
    lesson_id = models.UUIDField()
    course_id = models.UUIDField(null=True, blank=True)
    branch_id = models.UUIDField(null=True, blank=True)
    # What the screen shows, copied when the row was made.
    course_name = models.CharField(max_length=200, blank=True)
    branch_name = models.CharField(max_length=200, blank=True)
    day_of_week = models.SmallIntegerField(null=True, blank=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    # What is known about the child, in a few words: an age, a grade, a name.
    child = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=WAITING_STATUS_CHOICES, default=WAITING_WAITING)
    added_by_kind = models.CharField(max_length=10, choices=WAITING_BY_CHOICES, default=WAITING_BY_BOT)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    freed_at = models.DateTimeField(null=True, blank=True)
    # When the cron last counted the seats of this row's lesson: the rows longest unlooked-at go first.
    checked_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    # Why it left the list when no person took it off: the lesson is gone, the child registered.
    closed_reason = models.CharField(max_length=200, blank=True)
    # The last time the customer was told a seat came free — a message that really left. A second
    # telling is allowed and writes over the first; the journal keeps every one.
    notified_at = models.DateTimeField(null=True, blank=True)
    notified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    notified_how = models.CharField(max_length=10, choices=WAITING_TOLD_CHOICES, blank=True, default='')
    # A press that is being sent this moment: taken before ManyChat is asked and given back after,
    # so two presses on one row never send two messages. A claim nobody gave back goes stale by itself.
    notify_claimed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'wahub_waiting_list'
        ordering = ['id']
        verbose_name = 'רשומה ברשימת המתנה'
        verbose_name_plural = 'רשימת המתנה'
        indexes = [
            models.Index(fields=['contact', '-id'], name='wahub_waiting_contact'),
            # What the cron walks, and the list of one lesson.
            models.Index(
                fields=['lesson_id', 'id'], name='wahub_waiting_active',
                condition=Q(status__in=WAITING_ACTIVE_STATUSES),
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['contact', 'lesson_id'], condition=Q(status__in=WAITING_ACTIVE_STATUSES),
                name='wahub_waiting_one_active',
            ),
        ]

    def __str__(self):
        return f'{self.course_name} · {self.get_status_display()}'
