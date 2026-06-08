"""ManyChat API client for WhatsApp test / outbound messaging."""
from __future__ import annotations

import logging
import re
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

MANYCHAT_API_BASE = 'https://api.manychat.com'


class ManyChatError(Exception):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _manychat_error_text(exc: ManyChatError) -> str:
    """Full error text including nested validation messages."""
    parts = [str(exc)]
    payload = exc.payload
    if isinstance(payload, dict):
        parts.append(str(payload.get('message') or ''))
        details = payload.get('details')
        if details is not None:
            parts.append(str(details))
    return ' '.join(p for p in parts if p).lower()


class ManyChatService:
    def __init__(self, api_key: str | None = None):
        self.api_key = (api_key or getattr(settings, 'MANYCHAT_KEY', '') or '').strip()
        field_id = getattr(settings, 'MANYCHAT_PHONE_FIELD_ID', '') or ''
        self.phone_field_id = str(field_id).strip() or None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        timeout: int = 30,
    ) -> dict:
        if not self.is_configured:
            raise ManyChatError('ManyChat API key לא מוגדר (MANYCHAT_KEY)')

        url = f'{MANYCHAT_API_BASE}{path}'
        try:
            resp = requests.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=json_body,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            logger.exception('ManyChat request failed: %s %s', method, path)
            raise ManyChatError(f'שגיאת רשת ב-ManyChat: {exc}') from exc

        try:
            data = resp.json()
        except ValueError:
            data = {'raw': resp.text}

        if resp.status_code >= 400:
            msg = data.get('message') or data.get('error') or resp.text or 'ManyChat API error'
            raise ManyChatError(str(msg), status_code=resp.status_code, payload=data)

        if isinstance(data, dict) and data.get('status') == 'error':
            raise ManyChatError(
                data.get('message') or data.get('details') or 'ManyChat API error',
                status_code=resp.status_code,
                payload=data,
            )

        return data if isinstance(data, dict) else {'data': data}

    @staticmethod
    def normalize_phone_e164(phone: str) -> str:
        """Israeli-friendly E.164 without + (ManyChat often expects digits with country code)."""
        cleaned = re.sub(r'\D', '', phone or '')
        if not cleaned:
            return ''
        if cleaned.startswith('0'):
            return '972' + cleaned[1:]
        if cleaned.startswith('972'):
            return cleaned
        return '972' + cleaned

    @staticmethod
    def phone_lookup_variants(phone: str) -> list[str]:
        """All common formats used across ManyChat lookup endpoints."""
        normalized = ManyChatService.normalize_phone_e164(phone)
        if not normalized:
            return []
        local = f'0{normalized[3:]}' if normalized.startswith('972') else phone
        variants = [
            local,
            normalized,
            f'+{normalized}',
            f'0{normalized[3:]}' if normalized.startswith('972') else normalized,
        ]
        return list(dict.fromkeys(v for v in variants if v))

    def get_page_info(self) -> dict:
        return self._request('GET', '/fb/page/getInfo')

    def find_by_phone(self, phone: str) -> list[dict]:
        """Lookup subscriber(s) by phone (system field — works when phone is synced in ManyChat)."""
        variants = self.phone_lookup_variants(phone)
        if not variants:
            return []
        found: list[dict] = []
        seen: set[str | int] = set()
        for variant in variants:
            try:
                result = self._request('GET', '/fb/subscriber/findBySystemField', params={'phone': variant})
            except ManyChatError:
                continue
            rows = result.get('data') or []
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                sid = row.get('id')
                if sid is not None and sid not in seen:
                    seen.add(sid)
                    found.append(row)
        matched = self._matches_for_phone(found, phone)
        return matched or found

    def find_by_name(self, name: str) -> list[dict]:
        result = self._request('GET', '/fb/subscriber/findByName', params={'name': name.strip()})
        rows = result.get('data') or []
        if isinstance(rows, dict):
            return [rows]
        return rows if isinstance(rows, list) else []

    def find_by_custom_phone_field(self, phone: str) -> list[dict]:
        """Lookup via mirrored custom field (ManyChat cannot search by WhatsApp ID directly)."""
        if not self.phone_field_id:
            return []
        variants = self.phone_lookup_variants(phone)
        found: list[dict] = []
        seen: set[str | int] = set()
        for value in variants:
            try:
                result = self._request(
                    'GET',
                    '/fb/subscriber/findByCustomField',
                    params={'field_id': self.phone_field_id, 'field_value': value},
                )
            except ManyChatError:
                continue
            rows = result.get('data') or []
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                sid = row.get('id')
                if sid is not None and sid not in seen:
                    seen.add(sid)
                    found.append(row)
        matched = self._matches_for_phone(found, phone)
        return matched or found

    def get_subscriber(self, subscriber_id: int | str) -> dict:
        result = self._request('GET', '/fb/subscriber/getInfo', params={'subscriber_id': subscriber_id})
        data = result.get('data')
        return data if isinstance(data, dict) else result

    def create_whatsapp_subscriber(self, phone: str, first_name: str = '', last_name: str = '') -> dict:
        normalized = self.normalize_phone_e164(phone)
        body: dict[str, Any] = {
            'whatsapp_phone': normalized,
            'has_opt_in_whatsapp': True,
        }
        if first_name:
            body['first_name'] = first_name
        if last_name:
            body['last_name'] = last_name
        result = self._request('POST', '/fb/subscriber/createSubscriber', json_body=body)
        data = result.get('data')
        return data if isinstance(data, dict) else result

    def send_whatsapp_text(self, subscriber_id: int | str, text: str) -> dict:
        # message_tag is for Messenger only; including it breaks WhatsApp sendContent.
        payload = {
            'subscriber_id': int(subscriber_id),
            'data': {
                'version': 'v2',
                'content': {
                    'type': 'whatsapp',
                    'messages': [{'type': 'text', 'text': text}],
                },
            },
        }
        return self._request('POST', '/fb/sending/sendContent', json_body=payload)

    def set_custom_fields(self, subscriber_id: int | str, fields: dict[str, Any]) -> dict:
        """Set multiple ManyChat User Fields by name. Skips empty values."""
        clean = [
            {'field_name': str(k), 'field_value': '' if v is None else str(v)}
            for k, v in fields.items()
            if v is not None and str(v) != ''
        ]
        if not clean:
            return {'status': 'success', 'skipped': True}
        payload = {'subscriber_id': int(subscriber_id), 'fields': clean}
        return self._request('POST', '/fb/subscriber/setCustomFields', json_body=payload)

    def send_flow(self, subscriber_id: int | str, flow_ns: str) -> dict:
        """Trigger a published Automation/Flow (use this for WhatsApp Templates)."""
        payload = {'subscriber_id': int(subscriber_id), 'flow_ns': flow_ns}
        return self._request('POST', '/fb/sending/sendFlow', json_body=payload)

    def get_flows(self) -> list[dict]:
        """List automations from ManyChat (name + ns)."""
        result = self._request('GET', '/fb/page/getFlows')
        data = result.get('data')

        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]

        if isinstance(data, dict):
            flows = data.get('flows')
            if isinstance(flows, list):
                return [row for row in flows if isinstance(row, dict)]

        top_flows = result.get('flows')
        if isinstance(top_flows, list):
            return [row for row in top_flows if isinstance(row, dict)]

        return []

    def _kind_by_flow_ns_from_flows(self, flows: list[dict]) -> dict[str, str]:
        """Map ManyChat flow ns → Kogo kind using settings + one getFlows payload."""
        mapping: dict[str, str] = {}
        for kind, entry in self._REGISTRATION_KINDS.items():
            setting_name = entry['flow_setting']
            configured = (getattr(settings, setting_name, '') or '').strip()
            if configured:
                mapping[configured] = kind
                continue
            aliases = self._FLOW_NAME_ALIASES.get(setting_name, ())
            alias_set = {a.strip().lower() for a in aliases if a.strip()}
            if not alias_set:
                continue
            for flow in flows:
                if not isinstance(flow, dict):
                    continue
                name = (flow.get('name') or '').strip().lower()
                if name not in alias_set:
                    continue
                ns = (flow.get('ns') or flow.get('flow_ns') or '').strip()
                if ns:
                    mapping[ns] = kind
                    break
        return mapping

    # Fallback names when flow ns is not set in Django settings.
    _FLOW_NAME_ALIASES: dict[str, tuple[str, ...]] = {
        'MANYCHAT_REGISTRATION_FLOW_NS': ('lesson-register',),
        'MANYCHAT_TRIAL_FLOW_NS': (
            'test-lesson-register',
            'test lesson register',
            'test-lesson-registser',
        ),
        'MANYCHAT_PAYMENT_FAILED_FLOW_NS': ('payment-failed',),
        'MANYCHAT_TRIAL_10AM_FLOW_NS': ('test-lesson-10am', 'test lesson 10am'),
        'MANYCHAT_TRIAL_AFTER_TEST_FLOW_NS': ('after-test', 'after test'),
        'MANYCHAT_DIDNT_ARRIVE_FLOW_NS': ('didnt_arrive', 'didnt arrive', "didn't arrive"),
    }

    def resolve_flow_ns(self, setting_name: str) -> str:
        """Return flow ns from settings, or match automation name via getFlows."""
        configured = (getattr(settings, setting_name, '') or '').strip()
        if configured:
            return configured

        aliases = self._FLOW_NAME_ALIASES.get(setting_name)
        if not aliases:
            return ''

        try:
            flows = self.get_flows()
        except ManyChatError:
            logger.warning('ManyChat getFlows failed while resolving %s', setting_name)
            return ''

        alias_set = {a.strip().lower() for a in aliases if a.strip()}
        for flow in flows:
            if not isinstance(flow, dict):
                continue
            name = (flow.get('name') or '').strip().lower()
            if name in alias_set:
                ns = (flow.get('ns') or '').strip()
                if ns:
                    return ns
        return ''

    REGISTRATION_KIND_SUBSCRIPTION = 'subscription'
    REGISTRATION_KIND_TRIAL = 'trial'
    REGISTRATION_KIND_TRIAL_10AM = 'trial_10am'
    REGISTRATION_KIND_TRIAL_AFTER_TEST = 'trial_after_test'
    REGISTRATION_KIND_PAYMENT_FAILED = 'payment_failed'
    REGISTRATION_KIND_DIDNT_ARRIVE = 'didnt_arrive'

    _REGISTRATION_KINDS = {
        REGISTRATION_KIND_SUBSCRIPTION: {
            'flow_setting': 'MANYCHAT_REGISTRATION_FLOW_NS',
            'fallback_template': (
                'שלום {parent_name}!\n'
                'הילד {child_name} נרשם בהצלחה לחוג {course_name} בסניף {branch_name}.\n'
                'השיעור מתקיים בימי {day_name} בשעה {time_range}.\n'
                'תודה שבחרת ב-Kogo!'
            ),
        },
        REGISTRATION_KIND_TRIAL: {
            'flow_setting': 'MANYCHAT_TRIAL_FLOW_NS',
            'fallback_template': (
                'שלום {parent_name}!\n'
                'הילד {child_name} רשום לשיעור ניסיון בחוג {course_name} בסניף {branch_name}.\n'
                'שיעור הניסיון יתקיים בימי {day_name} בשעה {time_range}.\n'
                'מצפים לראותכם!'
            ),
        },
        # 10:00 Israel time on the trial lesson date (test-lesson-10am).
        REGISTRATION_KIND_TRIAL_10AM: {
            'flow_setting': 'MANYCHAT_TRIAL_10AM_FLOW_NS',
            'fallback_template': (
                'שלום {parent_name}!\n'
                'תזכורת לשיעור הניסיון של {child_name} בחוג {course_name}.\n'
                'יום {day_name} בשעה {time_range} בסניף {branch_name}.\n'
                'האם אתם מגיעים?'
            ),
        },
        # 2 hours after the trial lesson ends (after-test automation).
        REGISTRATION_KIND_TRIAL_AFTER_TEST: {
            'flow_setting': 'MANYCHAT_TRIAL_AFTER_TEST_FLOW_NS',
            'fallback_template': (
                'שלום {parent_name}!\n'
                'מקווים שהשיעור של {child_name} בחוג {course_name} היה מוצלח.\n'
                'המקום בשיעור בימי {day_name} בשעה {time_range} בסניף {branch_name} שמור עבור {child_name}.\n'
                'להרשמה כמנוי ולהבטחת המקום, ניתן להיכנס לאזור האישי במערכת Kogo.'
            ),
        },
        # Fired when Tranzila notify callback reports Response != 000 (subscription payment).
        REGISTRATION_KIND_PAYMENT_FAILED: {
            'flow_setting': 'MANYCHAT_PAYMENT_FAILED_FLOW_NS',
            'fallback_template': (
                'שלום {parent_name}!\n'
                'התשלום עבור רישום {child_name} לחוג {course_name} בסניף {branch_name} לא הושלם.\n'
                'השיעור מתוכנן בימי {day_name} בשעה {time_range}.\n'
                'ניתן לנסות שנית דרך מערכת Kogo או לפנות אלינו.'
            ),
        },
        # 3 consecutive times not marked present (didnt_arrive automation).
        REGISTRATION_KIND_DIDNT_ARRIVE: {
            'flow_setting': 'MANYCHAT_DIDNT_ARRIVE_FLOW_NS',
            'fallback_template': (
                'שלום {parent_name}!\n'
                'שמנו לב ש-{child_name} לא הגיע/ה לשלושה שיעורים ברצף בחוג {course_name} בסניף {branch_name}.\n'
                'השיעור מתקיים בימי {day_name} בשעה {time_range}.\n'
                'נשמח לשמוע מכם אם הכל בסדר ואם אתם ממשיכים להגיע.\n'
                'צוות Kogo'
            ),
        },
    }

    AUTOMATION_LABELS: dict[str, str] = {
        REGISTRATION_KIND_SUBSCRIPTION: 'הרשמה למנוי',
        REGISTRATION_KIND_TRIAL: 'רישום לשיעור ניסיון',
        REGISTRATION_KIND_TRIAL_10AM: 'תזכורת שיעור ניסיון (10:00)',
        REGISTRATION_KIND_TRIAL_AFTER_TEST: 'אחרי שיעור ניסיון',
        REGISTRATION_KIND_PAYMENT_FAILED: 'תשלום נכשל',
        REGISTRATION_KIND_DIDNT_ARRIVE: 'לא הגיע (3 פעמים)',
    }

    def list_available_automations(self) -> list[dict]:
        """
        Every automation returned by ManyChat getFlows (source of truth).
        Kogo kinds are matched by flow_ns for richer field handling when sending.
        """
        items: list[dict] = []
        seen_ns: set[str] = set()

        try:
            flows = self.get_flows()
        except ManyChatError:
            logger.warning('ManyChat getFlows failed while listing automations')
            flows = []

        kind_by_ns = self._kind_by_flow_ns_from_flows(flows)

        for flow in flows:
            ns = (flow.get('ns') or flow.get('flow_ns') or '').strip()
            if not ns or ns in seen_ns:
                continue
            seen_ns.add(ns)
            name = (flow.get('name') or flow.get('title') or '').strip() or ns
            kind = kind_by_ns.get(ns)
            item: dict[str, Any] = {
                'flow_ns': ns,
                'label': name,
                'manychat_name': name,
            }
            if kind:
                item['automation_type'] = 'kind'
                item['automation_id'] = kind
                item['kogo_label'] = self.AUTOMATION_LABELS.get(kind, kind)
                item['needs_enrollment_context'] = True
            else:
                item['automation_type'] = 'flow'
                item['automation_id'] = ns
                item['needs_enrollment_context'] = False
            items.append(item)

        # Flows configured in Django but missing from getFlows (rare).
        for kind, entry in self._REGISTRATION_KINDS.items():
            ns = self.resolve_flow_ns(entry['flow_setting'])
            if not ns or ns in seen_ns:
                continue
            seen_ns.add(ns)
            items.append({
                'automation_type': 'kind',
                'automation_id': kind,
                'flow_ns': ns,
                'label': self.AUTOMATION_LABELS.get(kind, kind),
                'manychat_name': None,
                'kogo_label': self.AUTOMATION_LABELS.get(kind, kind),
                'needs_enrollment_context': True,
            })

        items.sort(key=lambda row: (row.get('label') or '').casefold())
        return items

    def send_automation_to_contact(
        self,
        *,
        automation_type: str,
        automation_id: str,
        phone: str,
        name: str = '',
        branch_name: str | None = None,
    ) -> dict:
        """Trigger one ManyChat automation for a phone (broadcast row)."""
        if automation_type == 'kind':
            if automation_id not in self._REGISTRATION_KINDS:
                return {'sent': False, 'reason': 'unknown_kind'}
            display_name = (name or '').strip() or 'הורה'
            branch = (branch_name or '').strip() or '—'
            return self.notify_registration(
                kind=automation_id,
                phone=phone,
                parent_name=display_name,
                child_name='—',
                course_name='—',
                branch_name=branch,
                day_name='—',
                start_time='',
                end_time='',
                lookup_names=[display_name] if display_name else None,
            )

        if automation_type == 'flow':
            flow_ns = (automation_id or '').strip()
            if not flow_ns:
                return {'sent': False, 'reason': 'missing_flow_ns'}
            resolved = self.lookup_or_create(phone, name)
            sid = resolved.get('subscriber_id')
            if not sid:
                return {'sent': False, 'reason': 'no_subscriber_id'}
            if (name or '').strip():
                try:
                    self.set_custom_fields(sid, {'kogo_parent_name': name.strip()})
                except ManyChatError as exc:
                    logger.warning('ManyChat setCustomFields (broadcast) failed for %s: %s', sid, exc)
            self.send_flow(sid, flow_ns)
            return {
                'sent': True,
                'method': 'flow',
                'subscriber_id': sid,
                'flow_ns': flow_ns,
                'phone': phone,
            }

        return {'sent': False, 'reason': 'invalid_automation_type'}

    def notify_registration(
        self,
        *,
        phone: str,
        parent_name: str,
        child_name: str,
        course_name: str,
        day_name: str,
        start_time: str,
        end_time: str,
        branch_name: str,
        kind: str = REGISTRATION_KIND_SUBSCRIPTION,
        lookup_names: list[str] | None = None,
        trial_date: str = '',
    ) -> dict:
        """
        Send a course-registration / trial confirmation to the parent on WhatsApp.

        Strategy:
          1. Resolve or create a ManyChat subscriber for the phone.
          2. Set the 6 custom fields used by the WhatsApp Template flow.
          3. If the matching flow ns is configured → trigger the approved template.
             Otherwise fall back to free-text (only delivers within 24h window).
        """
        if not self.is_configured:
            return {'sent': False, 'reason': 'manychat_not_configured'}

        config_entry = self._REGISTRATION_KINDS.get(kind, self._REGISTRATION_KINDS[self.REGISTRATION_KIND_SUBSCRIPTION])

        try:
            resolved = self.lookup_or_create(phone, parent_name, lookup_names=lookup_names)
        except ManyChatError as exc:
            return {'sent': False, 'reason': 'lookup_failed', 'error': str(exc)}

        sid = resolved.get('subscriber_id')
        if not sid:
            return {'sent': False, 'reason': 'no_subscriber_id'}

        whatsapp_phone = phone
        try:
            sub_info = self.get_subscriber(sid)
            whatsapp_phone = str(sub_info.get('whatsapp_phone') or sub_info.get('phone') or phone)
        except ManyChatError:
            pass

        time_range = f'{start_time}-{end_time}' if end_time else start_time
        custom_fields = {
            'kogo_parent_name': parent_name,
            'kogo_child_name': child_name,
            'kogo_course_name': course_name,
            'kogo_branch_name': branch_name,
            'kogo_lesson_day': day_name,
            'kogo_lesson_time': time_range,
        }
        if trial_date:
            custom_fields['kogo_trial_date'] = trial_date
        try:
            self.set_custom_fields(sid, custom_fields)
        except ManyChatError as exc:
            logger.warning('ManyChat setCustomFields failed for %s: %s', sid, exc)

        flow_ns = self.resolve_flow_ns(config_entry['flow_setting'])
        if flow_ns:
            try:
                self.send_flow(sid, flow_ns)
                return {
                    'sent': True,
                    'method': 'flow',
                    'kind': kind,
                    'subscriber_id': sid,
                    'flow_ns': flow_ns,
                    'phone': phone,
                    'whatsapp_phone': whatsapp_phone,
                    'parent_name': parent_name,
                    'child_name': child_name,
                }
            except ManyChatError as exc:
                logger.exception('ManyChat sendFlow (%s) failed for %s', kind, sid)
                return {
                    'sent': False,
                    'reason': 'send_flow_failed',
                    'error': str(exc),
                    'subscriber_id': sid,
                }

        # Fallback (only delivers if user is within 24h customer-service window).
        text = config_entry['fallback_template'].format(
            parent_name=parent_name,
            child_name=child_name,
            course_name=course_name,
            branch_name=branch_name,
            day_name=day_name,
            time_range=time_range,
        )
        try:
            self.send_whatsapp_text(sid, text)
            return {'sent': True, 'method': 'text', 'kind': kind, 'subscriber_id': sid, 'phone': phone, 'whatsapp_phone': whatsapp_phone, 'parent_name': parent_name, 'child_name': child_name}
        except ManyChatError as exc:
            logger.exception('ManyChat send_whatsapp_text (%s) failed for %s', kind, sid)
            return {
                'sent': False,
                'reason': 'send_text_failed',
                'error': str(exc),
                'subscriber_id': sid,
            }

    def _matches_for_phone(self, rows: list[dict], phone: str) -> list[dict]:
        """Keep subscribers whose WhatsApp/system phone matches the requested number."""
        target = self.normalize_phone_e164(phone)
        if not target:
            return rows
        matched: list[dict] = []
        for row in rows:
            wa = self.normalize_phone_e164(str(row.get('whatsapp_phone') or row.get('phone') or ''))
            if wa == target:
                matched.append(row)
        return matched

    def _collect_name_queries(self, name: str, extra_names: list[str] | None = None) -> list[str]:
        queries: list[str] = []
        for raw in [name, *(extra_names or [])]:
            n = (raw or '').strip()
            if not n:
                continue
            queries.append(n)
            for word in n.split():
                if len(word) >= 2:
                    queries.append(word)
            first = n.split(maxsplit=1)[0]
            if first and first not in queries:
                queries.append(first)
        return list(dict.fromkeys(queries))

    def _ensure_phone_indexed(self, subscriber_id: int | str, phone: str) -> None:
        """
        Mirror the phone into ManyChat so future lookups work for any number.

        ManyChat WhatsApp contacts are not searchable by whatsapp_phone via API;
        syncing to Client_Phone (custom field) and system phone fixes that.
        """
        variants = self.phone_lookup_variants(phone)
        local = variants[0] if variants else phone
        normalized = self.normalize_phone_e164(phone)
        intl_phone = f'+{normalized}' if normalized else ''

        if self.phone_field_id and local:
            try:
                self._request(
                    'POST',
                    '/fb/subscriber/setCustomField',
                    json_body={
                        'subscriber_id': int(subscriber_id),
                        'field_id': int(self.phone_field_id),
                        'field_value': local,
                    },
                )
            except ManyChatError as exc:
                logger.debug('ManyChat setCustomField phone mirror failed for %s: %s', subscriber_id, exc)

        if intl_phone:
            try:
                sub = self.get_subscriber(subscriber_id)
                existing = self.normalize_phone_e164(str(sub.get('phone') or ''))
                if existing != normalized:
                    self._request(
                        'POST',
                        '/fb/subscriber/updateSubscriber',
                        json_body={
                            'subscriber_id': int(subscriber_id),
                            'phone': intl_phone,
                            'has_opt_in_sms': True,
                            'consent_phrase': 'Kogo registration',
                        },
                    )
            except ManyChatError as exc:
                logger.debug('ManyChat updateSubscriber phone mirror failed for %s: %s', subscriber_id, exc)

    def _pick_best_subscriber(self, rows: list[dict], phone: str) -> dict | None:
        if not rows:
            return None
        target = self.normalize_phone_e164(phone)
        matched = self._matches_for_phone(rows, phone)
        if matched:
            return matched[0]
        for row in rows:
            sid = row.get('id')
            if sid is None:
                continue
            wa = self.normalize_phone_e164(str(row.get('whatsapp_phone') or row.get('phone') or ''))
            if wa == target:
                return row
            try:
                info = self.get_subscriber(sid)
            except ManyChatError:
                continue
            wa = self.normalize_phone_e164(str(info.get('whatsapp_phone') or info.get('phone') or ''))
            if wa == target:
                return info
        return None

    def _find_by_whatsapp_phone(self, phone: str) -> list[dict]:
        """Find subscriber by WhatsApp number (ManyChat stores it separately from SMS phone)."""
        target = self.normalize_phone_e164(phone)
        if not target:
            return []
        matched: list[dict] = []
        seen: set[str | int] = set()
        for variant in self.phone_lookup_variants(phone):
            try:
                result = self._request('GET', '/fb/subscriber/findBySystemField', params={'phone': variant})
            except ManyChatError:
                continue
            rows = result.get('data') or []
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                sid = row.get('id')
                if sid is None or sid in seen:
                    continue
                wa = self.normalize_phone_e164(str(row.get('whatsapp_phone') or row.get('phone') or ''))
                if wa != target:
                    try:
                        info = self.get_subscriber(sid)
                        wa = self.normalize_phone_e164(str(info.get('whatsapp_phone') or info.get('phone') or ''))
                    except ManyChatError:
                        continue
                if wa == target:
                    seen.add(sid)
                    matched.append(row)
        return matched

    def _resolve_subscriber(self, phone: str, name: str = '', lookup_names: list[str] | None = None) -> dict | None:
        """Try every lookup strategy before creating a new WhatsApp subscriber."""
        extra = [n.strip() for n in (lookup_names or []) if (n or '').strip()]
        finders = [
            self._find_by_whatsapp_phone,
            self.find_by_custom_phone_field,
            self.find_by_phone,
            lambda p: self.find_by_name_for_phone(p, name, extra),
        ]
        for finder in finders:
            matches = finder(phone)
            sub = self._pick_best_subscriber(matches, phone)
            if sub:
                return sub
        return None

    def find_by_name_for_phone(self, phone: str, name: str, extra_names: list[str] | None = None) -> list[dict]:
        """ManyChat WhatsApp contacts are often only findable by name, not by phone API."""
        found: list[dict] = []
        seen: set[str | int] = set()
        for query in self._collect_name_queries(name, extra_names):
            for row in self.find_by_name(query):
                sid = row.get('id')
                if sid is not None and sid not in seen:
                    seen.add(sid)
                    found.append(row)
        matched = self._matches_for_phone(found, phone)
        if matched:
            return matched
        wa_matched = []
        for row in found:
            try:
                info = self.get_subscriber(row.get('id'))
            except ManyChatError:
                continue
            wa = self.normalize_phone_e164(str(info.get('whatsapp_phone') or info.get('phone') or ''))
            if wa == self.normalize_phone_e164(phone):
                wa_matched.append(info)
        return wa_matched

    def lookup_or_create(self, phone: str, name: str = '', lookup_names: list[str] | None = None) -> dict:
        """Find by phone or create a WhatsApp subscriber for any valid number."""
        sub = self._resolve_subscriber(phone, name, lookup_names)
        if sub:
            sid = sub.get('id')
            if sid:
                self._ensure_phone_indexed(sid, phone)
            return {'subscriber': sub, 'created': False, 'subscriber_id': sid}

        parts = (name or 'Kogo').strip().split(maxsplit=1)
        first = parts[0] if parts else 'Kogo'
        last = parts[1] if len(parts) > 1 else ''
        try:
            created = self.create_whatsapp_subscriber(phone, first, last)
            sid = created.get('id')
            if sid:
                self._ensure_phone_indexed(sid, phone)
            return {'subscriber': created, 'created': True, 'subscriber_id': sid}
        except ManyChatError as exc:
            if 'already exists' in _manychat_error_text(exc) or 'whatsapp id' in _manychat_error_text(exc):
                sub = self._resolve_subscriber(phone, name, lookup_names)
                if not sub:
                    wa_match = self._find_by_whatsapp_phone(phone)
                    sub = self._pick_best_subscriber(wa_match, phone)
                if sub and sub.get('id'):
                    self._ensure_phone_indexed(sub.get('id'), phone)
                    return {'subscriber': sub, 'created': False, 'subscriber_id': sub.get('id')}
                raise ManyChatError(
                    'לא ניתן למצוא את איש הקשר ב-ManyChat לפי מספר הטלפון. '
                    'ודא שהמספר רשום ב-WhatsApp.',
                    status_code=exc.status_code,
                    payload=exc.payload,
                ) from exc
            if 'not a valid whatsapp id' in _manychat_error_text(exc):
                raise ManyChatError(
                    'מספר הטלפון אינו רשום ב-WhatsApp — לא ניתן לשלוח הודעה.',
                    status_code=exc.status_code,
                    payload=exc.payload,
                ) from exc
            raise
