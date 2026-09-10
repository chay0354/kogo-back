"""What the review screen is given. No identifying field exists to expose."""
from __future__ import annotations

from rest_framework import serializers

from apps.external_students.models import (
    ExternalRosterImport,
    ExternalRosterImportRow,
    ExternalRosterImportUnit,
)


class ExternalRosterImportRowSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = ExternalRosterImportRow
        fields = [
            'id', 'first_name', 'last_name', 'full_name', 'phone',
            'action', 'existing_student', 'edited',
        ]


class ExternalRosterImportUnitSerializer(serializers.ModelSerializer):
    rows = ExternalRosterImportRowSerializer(many=True, read_only=True)
    matched_lesson_ids = serializers.SerializerMethodField()
    read_total = serializers.SerializerMethodField()
    counts = serializers.SerializerMethodField()

    class Meta:
        model = ExternalRosterImportUnit
        fields = [
            'id', 'ordinal', 'municipality_code', 'group_name', 'slots_raw', 'slots',
            'status', 'error', 'stated_total', 'read_total', 'match_state',
            'candidates', 'matched_lesson_ids', 'counts', 'rows', 'duration_ms',
        ]

    def get_matched_lesson_ids(self, obj):
        return [str(lesson.id) for lesson in obj.matched_lessons.all()]

    def get_read_total(self, obj):
        """People actually read, as against what the file claims for itself."""
        return sum(
            1 for row in obj.rows.all()
            if row.action != ExternalRosterImportRow.ACTION_REMOVE
        )

    def get_counts(self, obj):
        rows = list(obj.rows.all())
        return {
            'add': sum(1 for r in rows if r.action == ExternalRosterImportRow.ACTION_ADD),
            'keep': sum(1 for r in rows if r.action == ExternalRosterImportRow.ACTION_KEEP),
            'remove': sum(1 for r in rows if r.action == ExternalRosterImportRow.ACTION_REMOVE),
        }


class ExternalRosterImportListSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = ExternalRosterImport
        fields = [
            'id', 'branch', 'branch_name', 'kind', 'original_filename', 'period_label',
            'status', 'units_total', 'units_done', 'error', 'created_at', 'applied_at',
        ]


class ExternalRosterImportDetailSerializer(ExternalRosterImportListSerializer):
    units = ExternalRosterImportUnitSerializer(many=True, read_only=True)
    summary = serializers.SerializerMethodField()

    class Meta(ExternalRosterImportListSerializer.Meta):
        fields = ExternalRosterImportListSerializer.Meta.fields + [
            'stated_report_total', 'summary', 'units',
        ]

    def get_summary(self, obj):
        add = keep = remove = read = 0
        for unit in obj.units.all():
            if unit.status == ExternalRosterImportUnit.STATUS_SKIPPED:
                continue
            for row in unit.rows.all():
                if row.action == ExternalRosterImportRow.ACTION_ADD:
                    add += 1
                elif row.action == ExternalRosterImportRow.ACTION_KEEP:
                    keep += 1
                else:
                    remove += 1
            read += sum(
                1 for row in unit.rows.all()
                if row.action != ExternalRosterImportRow.ACTION_REMOVE
            )
        return {
            'add': add, 'keep': keep, 'remove': remove, 'read_total': read,
            # Both municipalities print a total for the whole report. When it
            # disagrees with what we read, that is worth saying before anything
            # is written.
            'matches_stated_total': (
                obj.stated_report_total is None or obj.stated_report_total == read
            ),
        }
