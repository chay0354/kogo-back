from datetime import time as time_cls
from types import SimpleNamespace

from rest_framework import serializers

from apps.scheduling.models import ScheduleEvent
from apps.scheduling.studio_conflict import (
    event_conflicts_lessons,
    event_conflicts_other_events,
)
from apps.scheduling.weekdays import lesson_style_dow_from_date


def _parse_time_str(value, field_name='weekly_day_times'):
    if isinstance(value, time_cls):
        return value
    if not value:
        raise serializers.ValidationError({field_name: 'שעה חסרה'})
    try:
        parts = [int(p) for p in str(value).split(':')]
        while len(parts) < 3:
            parts.append(0)
        h, m, s = parts[:3]
        return time_cls(hour=h, minute=m, second=s)
    except (TypeError, ValueError):
        raise serializers.ValidationError({field_name: 'פורמט שעה לא תקין'})


class ScheduleEventSerializer(serializers.ModelSerializer):
    """Serializer for schedule events"""
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    studio_name = serializers.CharField(source='studio.name', read_only=True, allow_null=True)
    city_name = serializers.CharField(source='city.name', read_only=True, allow_null=True)
    assigned_instructor_names = serializers.SerializerMethodField()

    class Meta:
        model = ScheduleEvent
        fields = [
            'id', 'name', 'event_date', 'start_time', 'end_time',
            'event_type', 'is_daily_event', 'branch', 'branch_name',
            'studio', 'studio_name', 'city', 'city_name', 'location',
            'assigned_instructors', 'assigned_instructor_names',
            'color', 'notes', 'files', 'is_active', 'is_studio_rental', 'renter_name',
            'renter_id_number', 'price_per_session', 'contract_start_date', 'contract_end_date',
            'weekly_repeat_days', 'weekly_day_times',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_assigned_instructor_names(self, obj):
        """Get list of assigned instructor names"""
        return [f"{instructor.first_name} {instructor.last_name}"
                for instructor in obj.assigned_instructors.all()]

    def _candidate_namespace(self, attrs):
        inst = self.instance
        branch = attrs.get('branch', inst.branch if inst else None)
        studio = attrs.get('studio', inst.studio if inst else None)
        return SimpleNamespace(
            is_daily_event=attrs.get('is_daily_event', inst.is_daily_event if inst else False),
            branch_id=branch.pk if branch else None,
            studio_id=studio.pk if studio else None,
            start_time=attrs.get('start_time', inst.start_time if inst else None),
            end_time=attrs.get('end_time', inst.end_time if inst else None),
            event_date=attrs.get('event_date', inst.event_date if inst else None),
            event_type=attrs.get('event_type', inst.event_type if inst else 'one_time'),
            weekly_repeat_days=attrs.get('weekly_repeat_days', inst.weekly_repeat_days if inst else []),
            weekly_day_times=attrs.get('weekly_day_times', inst.weekly_day_times if inst else {}),
        )

    def validate(self, attrs):
        """Validate times, city, studio rentals, weekly per-day times, and studio slot conflicts."""
        inst = self.instance
        is_daily = attrs.get('is_daily_event', inst.is_daily_event if inst else False)
        city = attrs.get('city', inst.city if inst else None)
        is_studio_rental = attrs.get('is_studio_rental', inst.is_studio_rental if inst else False)
        price_per_session = attrs.get('price_per_session', inst.price_per_session if inst else None)
        branch = attrs.get('branch', inst.branch if inst else None)
        studio = attrs.get('studio', inst.studio if inst else None)
        start_time = attrs.get('start_time', inst.start_time if inst else None)
        end_time = attrs.get('end_time', inst.end_time if inst else None)

        if not city:
            raise serializers.ValidationError({
                'city': 'עיר נדרשת'
            })

        # --- normalize weekly_repeat_days ---
        et = attrs.get('event_type', inst.event_type if inst else 'one_time')
        ed = attrs.get('event_date', inst.event_date if inst else None)
        wrd_raw = attrs.get('weekly_repeat_days', inst.weekly_repeat_days if inst else None)

        if et == 'weekly' and ed:
            if wrd_raw is None or (isinstance(wrd_raw, list) and len(wrd_raw) == 0):
                weekly_repeat_days = [lesson_style_dow_from_date(ed)]
            else:
                if not isinstance(wrd_raw, list):
                    raise serializers.ValidationError({
                        'weekly_repeat_days': 'חייבת להיות רשימת מספרים (0–6)'
                    })
                norm = []
                for x in wrd_raw:
                    try:
                        i = int(x)
                    except (TypeError, ValueError):
                        raise serializers.ValidationError({
                            'weekly_repeat_days': 'ערכים לא תקינים (צפוי 0–6)'
                        })
                    if not 0 <= i <= 6:
                        raise serializers.ValidationError({
                            'weekly_repeat_days': 'כל יום חייב להיות בין 0 ל־6'
                        })
                    norm.append(i)
                weekly_repeat_days = sorted(set(norm))
            attrs['weekly_repeat_days'] = weekly_repeat_days
        elif et == 'one_time':
            weekly_repeat_days = []
            attrs['weekly_repeat_days'] = []
            attrs['weekly_day_times'] = {}
        else:
            weekly_repeat_days = inst.weekly_repeat_days if inst else []

        # --- normalize weekly_day_times (studio rentals only; regular weekly events keep
        # sharing a single start_time/end_time across all their repeat days) ---
        if et == 'weekly' and is_studio_rental:
            wdt_raw = attrs.get('weekly_day_times', inst.weekly_day_times if inst else {})
            if not isinstance(wdt_raw, dict):
                raise serializers.ValidationError({
                    'weekly_day_times': 'חייב להיות מיפוי יום לשעות'
                })
            normalized_wdt = {}
            for dow in weekly_repeat_days:
                entry = wdt_raw.get(str(dow))
                if entry:
                    s = _parse_time_str(entry.get('start_time'))
                    e = _parse_time_str(entry.get('end_time'))
                elif start_time and end_time:
                    s, e = start_time, end_time
                else:
                    raise serializers.ValidationError({
                        'weekly_day_times': f'חסרות שעות ליום {dow}'
                    })
                if s >= e:
                    raise serializers.ValidationError({
                        'weekly_day_times': 'שעת התחלה חייבת להיות לפני שעת הסיום'
                    })
                normalized_wdt[str(dow)] = {
                    'start_time': s.isoformat(),
                    'end_time': e.isoformat(),
                }
            attrs['weekly_day_times'] = normalized_wdt

            # Envelope start_time/end_time (earliest start, latest end) kept in sync for
            # legacy consumers (ordering, "not is_daily" guard below, quick display).
            all_starts = [_parse_time_str(v['start_time']) for v in normalized_wdt.values()]
            all_ends = [_parse_time_str(v['end_time']) for v in normalized_wdt.values()]
            start_time = min(all_starts)
            end_time = max(all_ends)
            attrs['start_time'] = start_time
            attrs['end_time'] = end_time

        if not is_daily:
            if not start_time:
                raise serializers.ValidationError({
                    'start_time': 'שעת התחלה נדרשת לאירועים שאינם יומיים'
                })
            if not end_time:
                raise serializers.ValidationError({
                    'end_time': 'שעת סיום נדרשת לאירועים שאינם יומיים'
                })

        if is_studio_rental:
            if is_daily:
                raise serializers.ValidationError({
                    'is_daily_event': 'שכירות סטודיו דורשת שעת התחלה וסיום (לא אירוע יומי)'
                })
            if not branch:
                raise serializers.ValidationError({
                    'branch': 'נדרש לבחור סניף לשכירות סטודיו'
                })
            if not studio:
                raise serializers.ValidationError({
                    'studio': 'נדרש לבחור סטודיו לשכירות'
                })
            if price_per_session is None or price_per_session < 0:
                raise serializers.ValidationError({
                    'price_per_session': 'מחיר למופע חייב להיות 0 או חיובי'
                })
            renter_id_number = attrs.get('renter_id_number', inst.renter_id_number if inst else '')
            if not renter_id_number:
                raise serializers.ValidationError({
                    'renter_id_number': 'ת.ז / ח.פ של השוכר נדרש לשכירות סטודיו'
                })
            contract_start_date = attrs.get('contract_start_date', inst.contract_start_date if inst else None)
            contract_end_date = attrs.get('contract_end_date', inst.contract_end_date if inst else None)
            if not contract_start_date or not contract_end_date:
                raise serializers.ValidationError({
                    'contract_start_date': 'יש להזין תאריך תחילת וסיום תוקף ההסכם'
                })
            if contract_start_date >= contract_end_date:
                raise serializers.ValidationError({
                    'contract_end_date': 'תאריך סיום ההסכם חייב להיות אחרי תאריך ההתחלה'
                })
            attrs.pop('assigned_instructors', None)

        cand = self._candidate_namespace(attrs)
        if not cand.is_daily_event and cand.branch_id and cand.studio_id and cand.start_time and cand.end_time:
            ex = inst.pk if inst else None
            if event_conflicts_other_events(cand, exclude_pk=ex):
                raise serializers.ValidationError({
                    'studio': 'הסטודיו תפוס באותה שעה (אירוע אחר)'
                })
            if event_conflicts_lessons(cand):
                raise serializers.ValidationError({
                    'studio': 'הסטודיו תפוס באותה שעה (שיעור קיים)'
                })

        return attrs

    def create(self, validated_data):
        instructors = validated_data.pop('assigned_instructors', None)
        is_rental = validated_data.get('is_studio_rental', False)
        event = ScheduleEvent.objects.create(**validated_data)
        if instructors is not None and not is_rental:
            event.assigned_instructors.set(instructors)
        return event

    def update(self, instance, validated_data):
        instructors = validated_data.pop('assigned_instructors', None)
        is_rental = validated_data.get('is_studio_rental', instance.is_studio_rental)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if instructors is not None:
            if is_rental:
                instance.assigned_instructors.clear()
            else:
                instance.assigned_instructors.set(instructors)
        return instance


class ScheduleEventListSerializer(serializers.ModelSerializer):
    """Simplified serializer for event list views"""
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    studio_name = serializers.CharField(source='studio.name', read_only=True, allow_null=True)
    city_name = serializers.CharField(source='city.name', read_only=True, allow_null=True)

    class Meta:
        model = ScheduleEvent
        fields = [
            'id', 'name', 'event_date', 'start_time', 'end_time',
            'event_type', 'is_daily_event', 'branch_name', 'studio_name',
            'city_name', 'location', 'color', 'notes', 'is_active',
            'is_studio_rental', 'renter_name', 'renter_id_number', 'price_per_session',
            'contract_start_date', 'contract_end_date',
            'weekly_repeat_days', 'weekly_day_times',
        ]
