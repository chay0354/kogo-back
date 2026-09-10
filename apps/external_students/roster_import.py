"""
Driving a municipality sheet from upload to applied.

Three things in here carry the weight.

**A unit is claimed before the model is asked anything.** The claim is its own
committed transaction, so two callers can never take the same group, and a call
that dies mid-flight is afterwards visible as ``running`` with an old timestamp
rather than as work that quietly never happened. That is what lets the browser
drive the normal case and a cron rescue the abandoned one, without the two
tripping over each other.

**The file's own totals are used as a check, not as decoration.** Both
municipalities print a count per group and a count for the report. When what we
read disagrees with what the sheet claims, the group is marked ``mismatch``: it
still shows its rows, it does not block the rest, and the manager sees the two
numbers side by side.

**Nothing is applied until a person says so, and then only where the file
spoke.** Lessons the sheet never covered are not touched at all.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.enrollments.person_match import normalise_name, normalise_phone
from apps.external_students import extraction, matching
from apps.external_students.models import (
    ExternalRosterGroup,
    ExternalRosterImport,
    ExternalRosterImportRow,
    ExternalRosterImportUnit,
    ExternalStudent,
)

logger = logging.getLogger(__name__)

# A unit still 'running' after this long is assumed to belong to a request that
# died, and is offered back to the next caller.
RECLAIM_AFTER = timedelta(minutes=10)
# An import nobody finished within this long is closed out and its provider
# files released.
ABANDON_AFTER = timedelta(hours=24)
# Removing most of a lesson's roster in one go is the one outcome worth making a
# manager say out loud. Both bars have to be cleared: a share, so a big class
# losing a handful is ordinary, and a floor, so a class of two losing one is not
# treated as a catastrophe just because one of two is 'most'.
BULK_REMOVAL_SHARE = 0.5
BULK_REMOVAL_FLOOR = 3


class ImportError_(Exception):
    """Something about the file itself is wrong, in a way worth telling a person."""


# --------------------------------------------------------------------------
# Upload: segment the file into groups. No participant is read here.
# --------------------------------------------------------------------------

def segment_spreadsheet(roster_import: ExternalRosterImport, file_bytes: bytes) -> None:
    """
    Read the grid, ask the model for the layout, drop the identifying columns.

    The one model call in the upload request is small — it answers "what shape is
    this", not "who is in it" — so it fits comfortably inside a short request.
    """
    grid = extraction.read_grid(file_bytes)
    if not grid:
        raise ImportError_('הגיליון ריק')

    layout, usage = extraction.read_sheet_layout(grid)
    if not layout.groups:
        raise ImportError_('לא זוהו קבוצות בגיליון')

    redacted = extraction.redact(grid, layout.columns.excluded_columns)

    roster_import.sheet_grid = redacted
    roster_import.period_label = layout.period_label[:120]
    roster_import.stated_report_total = layout.stated_report_total
    roster_import.units_total = len(layout.groups)
    roster_import.model_id = extraction.MODEL_ID
    roster_import.input_tokens = usage['input_tokens']
    roster_import.output_tokens = usage['output_tokens']
    roster_import.save()

    for ordinal, block in enumerate(layout.groups, start=1):
        ExternalRosterImportUnit.objects.create(
            roster_import=roster_import,
            ordinal=ordinal,
            municipality_code=(block.municipality_code or '')[:40],
            group_name=(block.group_name or '')[:200],
            slots_raw=(block.slots_raw or '')[:200],
            slots=extraction.parse_slots(block.slots_raw),
            stated_total=block.stated_total,
            source_ref={
                'first_row': block.first_person_row,
                'last_row': block.last_person_row,
                'name_column': layout.columns.name_column,
                'phone_column': layout.columns.phone_column,
                'second_phone_column': layout.columns.second_phone_column,
            },
        )


def segment_scan(roster_import: ExternalRosterImport, file_bytes: bytes) -> None:
    """
    One unit per page. The pages are held as bytes on the unit until read.

    A scanned page has no grid, so the model has to read the names off it — and
    the identity numbers printed beside them travel with the page. That is
    unavoidable and was agreed explicitly. What is avoidable, and is avoided, is
    keeping any of it: the page bytes go the moment the group is read.
    """
    pages = extraction.split_pdf_pages(file_bytes)
    if not pages:
        raise ImportError_('הקובץ אינו מכיל עמודים')

    roster_import.units_total = len(pages)
    roster_import.model_id = extraction.MODEL_ID
    roster_import.save()

    import base64

    for ordinal, page in enumerate(pages, start=1):
        ExternalRosterImportUnit.objects.create(
            roster_import=roster_import,
            ordinal=ordinal,
            source_ref={'page': base64.standard_b64encode(page).decode('ascii')},
        )


# --------------------------------------------------------------------------
# Parse: one group at a time, claimed before anything is asked
# --------------------------------------------------------------------------

def claim_next_unit(roster_import: ExternalRosterImport) -> ExternalRosterImportUnit | None:
    """
    Take the next group to read, in a transaction of its own.

    Committing the claim before the model is called is the whole reliability
    story: it is what makes a second caller pick a different group, and what
    makes a dead call visible instead of invisible.
    """
    stale_before = timezone.now() - RECLAIM_AFTER
    with transaction.atomic():
        unit = (
            ExternalRosterImportUnit.objects
            .select_for_update(skip_locked=True)
            .filter(roster_import=roster_import)
            .filter(
                models_q_pending() | models_q_stale(stale_before)
            )
            .order_by('ordinal')
            .first()
        )
        if unit is None:
            return None
        unit.status = ExternalRosterImportUnit.STATUS_RUNNING
        unit.started_at = timezone.now()
        unit.attempts += 1
        unit.save(update_fields=['status', 'started_at', 'attempts', 'updated_at'])
    return unit


def models_q_pending():
    from django.db.models import Q

    return Q(status=ExternalRosterImportUnit.STATUS_PENDING)


def models_q_stale(before):
    from django.db.models import Q

    return Q(status=ExternalRosterImportUnit.STATUS_RUNNING, started_at__lt=before)


def parse_unit(unit: ExternalRosterImportUnit) -> ExternalRosterImportUnit:
    """Read one group's people, match it to lessons, and write the result."""
    started = timezone.now()
    roster_import = unit.roster_import
    try:
        if roster_import.kind == ExternalRosterImport.KIND_XLSX:
            people = _people_from_sheet(roster_import, unit)
        else:
            people = _people_from_scan(unit)
    except extraction.IdentityNumberLeak as exc:
        return _fail(unit, str(exc), started)
    except Exception as exc:  # noqa: BLE001 — the manager needs the reason, not a 500
        logger.exception('Roster unit %s failed to parse', unit.id)
        return _fail(unit, str(exc)[:500], started)

    with transaction.atomic():
        unit.rows.all().delete()
        result = matching.match_unit(unit)
        unit.match_state = result['state']
        unit.candidates = result['candidates']
        unit.matched_lessons.set(result['lessons'])

        _write_rows(unit, people, result['lessons'])

        stated = unit.stated_total
        unit.status = (
            ExternalRosterImportUnit.STATUS_MISMATCH
            if stated is not None and stated != len(people)
            else ExternalRosterImportUnit.STATUS_PARSED
        )
        unit.error = ''
        unit.duration_ms = int((timezone.now() - started).total_seconds() * 1000)
        # The page has been read; there is no reason to keep holding it.
        if roster_import.kind == ExternalRosterImport.KIND_PDF:
            unit.source_ref = {'pages_released': True}
        unit.save()
        _refresh_progress(roster_import)
    return unit


def _fail(unit, message, started):
    unit.status = ExternalRosterImportUnit.STATUS_FAILED
    unit.error = message
    unit.duration_ms = int((timezone.now() - started).total_seconds() * 1000)
    unit.save(update_fields=['status', 'error', 'duration_ms', 'updated_at'])
    _refresh_progress(unit.roster_import)
    return unit


def _people_from_sheet(roster_import, unit) -> list[dict]:
    from apps.external_students.extraction import SheetColumns, SheetGroupBlock

    ref = unit.source_ref
    columns = SheetColumns(
        name_column=ref['name_column'],
        phone_column=ref.get('phone_column'),
        second_phone_column=ref.get('second_phone_column'),
    )
    block = SheetGroupBlock(
        first_person_row=ref['first_row'],
        last_person_row=ref['last_row'],
    )
    return extraction.people_from_block(roster_import.sheet_grid, columns, block)


def _people_from_scan(unit) -> list[dict]:
    import base64

    encoded = unit.source_ref.get('page')
    if not encoded:
        raise ImportError_('העמוד כבר שוחרר; יש להעלות את הקובץ מחדש')

    group, usage = extraction.read_scanned_page(base64.standard_b64decode(encoded))

    people = []
    for person in group.people:
        extraction.scrub_identity_numbers(person.first_name, person.last_name, person.phone)
        if not (person.first_name or person.last_name).strip():
            continue
        people.append({
            'first_name': person.first_name.strip(),
            'last_name': person.last_name.strip(),
            'phone': person.phone.strip(),
        })

    unit.municipality_code = (group.municipality_code or '')[:40]
    unit.group_name = (group.group_name or '')[:200]
    unit.slots_raw = (group.slots_raw or '')[:200]
    unit.slots = extraction.parse_slots(group.slots_raw)
    unit.stated_total = group.stated_total

    roster_import = unit.roster_import
    ExternalRosterImport.objects.filter(pk=roster_import.pk).update(
        input_tokens=roster_import.input_tokens + usage['input_tokens'],
        output_tokens=roster_import.output_tokens + usage['output_tokens'],
    )
    return people


def _person_key(first, last, phone):
    return normalise_name(f'{first} {last}'), normalise_phone(phone)


def _write_rows(unit, people, lessons) -> None:
    """
    Turn what the file said into what applying it would do.

    Existing students on the matched lessons that the file no longer lists
    become removals; the rest are kept or added. A lesson the file never covered
    is not consulted, which is what keeps a partial sheet from emptying a class
    it said nothing about.
    """
    existing = list(
        ExternalStudent.objects.filter(lesson__in=lessons, is_active=True)
    ) if lessons else []
    by_key = {}
    for student in existing:
        by_key.setdefault(_person_key(student.first_name, student.last_name, student.phone), []).append(student)

    seen = set()
    for person in people:
        key = _person_key(person['first_name'], person['last_name'], person['phone'])
        seen.add(key)
        match = by_key.get(key)
        ExternalRosterImportRow.objects.create(
            unit=unit,
            first_name=person['first_name'][:60],
            last_name=person['last_name'][:60],
            phone=person['phone'][:30],
            action=(
                ExternalRosterImportRow.ACTION_KEEP if match
                else ExternalRosterImportRow.ACTION_ADD
            ),
            existing_student=match[0] if match else None,
        )

    for key, students in by_key.items():
        if key in seen:
            continue
        student = students[0]
        ExternalRosterImportRow.objects.create(
            unit=unit,
            first_name=student.first_name,
            last_name=student.last_name,
            phone=student.phone,
            action=ExternalRosterImportRow.ACTION_REMOVE,
            existing_student=student,
        )


def _refresh_progress(roster_import: ExternalRosterImport) -> None:
    terminal = (
        ExternalRosterImportUnit.STATUS_PARSED,
        ExternalRosterImportUnit.STATUS_MISMATCH,
        ExternalRosterImportUnit.STATUS_FAILED,
        ExternalRosterImportUnit.STATUS_SKIPPED,
    )
    units = list(roster_import.units.all())
    done = sum(1 for unit in units if unit.status in terminal)
    roster_import.units_done = done
    if roster_import.status in (
        ExternalRosterImport.STATUS_UPLOADED, ExternalRosterImport.STATUS_PARSING,
    ):
        roster_import.status = (
            ExternalRosterImport.STATUS_PARSED if done == len(units) and units
            else ExternalRosterImport.STATUS_PARSING
        )
    roster_import.save(update_fields=['units_done', 'status', 'updated_at'])


# --------------------------------------------------------------------------
# Review and apply
# --------------------------------------------------------------------------

def diff_digest(roster_import: ExternalRosterImport) -> str:
    """
    A fingerprint of exactly what the manager is looking at.

    Applying carries it back. If a colleague added a child by hand in between,
    the picture on screen is no longer the picture in the database and the apply
    stops rather than acting on a stale reading — the same guard the priced
    course change uses.
    """
    parts = []
    for unit in roster_import.units.prefetch_related('rows', 'matched_lessons').order_by('ordinal'):
        lessons = ','.join(sorted(str(l.id) for l in unit.matched_lessons.all()))
        rows = ';'.join(
            f'{r.action}:{normalise_name(r.full_name)}:{normalise_phone(r.phone)}'
            for r in sorted(unit.rows.all(), key=lambda r: (r.last_name, r.first_name))
        )
        parts.append(f'{unit.ordinal}|{unit.status}|{unit.match_state}|{lessons}|{rows}')
    payload = json.dumps(parts, ensure_ascii=False)
    return f'sha256:{hashlib.sha256(payload.encode()).hexdigest()[:32]}'


def blocking_units(roster_import: ExternalRosterImport) -> list[ExternalRosterImportUnit]:
    """Groups that must be finished or skipped before anything can be applied."""
    return [
        unit for unit in roster_import.units.all()
        if unit.status in (
            ExternalRosterImportUnit.STATUS_PENDING,
            ExternalRosterImportUnit.STATUS_RUNNING,
            ExternalRosterImportUnit.STATUS_FAILED,
        )
    ]


def bulk_removal_lessons(roster_import: ExternalRosterImport) -> list[str]:
    """
    Lessons the sheet would strip most of, which a manager has to opt into.

    A partial file or a misread page looks exactly like a class that emptied.
    The difference matters enough to ask.
    """
    flagged = []
    for unit in roster_import.units.prefetch_related('rows', 'matched_lessons'):
        rows = list(unit.rows.all())
        removals = sum(1 for row in rows if row.action == ExternalRosterImportRow.ACTION_REMOVE)
        if not removals:
            continue
        for lesson in unit.matched_lessons.all():
            active = ExternalStudent.objects.filter(lesson=lesson, is_active=True).count()
            if (
                active
                and removals >= BULK_REMOVAL_FLOOR
                and removals / active >= BULK_REMOVAL_SHARE
            ):
                flagged.append(str(lesson.id))
    return flagged


def apply_import(roster_import: ExternalRosterImport, *, user, confirmed_bulk_lessons=()) -> dict:
    """
    Write the reviewed plan. One transaction, and only where the file spoke.

    The municipality's group codes are recorded here and nowhere else: a mapping
    a manager opened and walked away from must not shape next month's import.
    """
    pending = blocking_units(roster_import)
    if pending:
        raise ImportError_('יש קבוצות שטרם נקראו או שנכשלו. יש להשלים או לדלג עליהן לפני ההחלה.')

    needed = set(bulk_removal_lessons(roster_import))
    missing = needed - set(str(x) for x in confirmed_bulk_lessons)
    if missing:
        raise ImportError_('יש שיעורים שמאבדים את רוב הרשימה. נדרש אישור נפרד לכל אחד מהם.')

    # Counted as children rather than as rows. A child in a twice-weekly group
    # gets a row on each of its lessons, so counting rows would report seven
    # where the review promised five, and the two numbers have to agree.
    added_people: set = set()
    kept_people: set = set()
    removed = 0
    with transaction.atomic():
        for unit in roster_import.units.prefetch_related('rows', 'matched_lessons').order_by('ordinal'):
            if unit.status == ExternalRosterImportUnit.STATUS_SKIPPED:
                continue
            lessons = list(unit.matched_lessons.all())
            if not lessons:
                continue

            for row in unit.rows.all():
                if row.action == ExternalRosterImportRow.ACTION_REMOVE:
                    if row.existing_student and row.existing_student.is_active:
                        student = row.existing_student
                        student.is_active = False
                        student.end_date = student.end_date or timezone.localdate()
                        student.updated_by = user
                        student.save(update_fields=['is_active', 'end_date', 'updated_by', 'updated_at'])
                        removed += 1
                    continue

                for lesson in lessons:
                    key = _person_key(row.first_name, row.last_name, row.phone)
                    already = next(
                        (
                            s for s in ExternalStudent.objects.filter(lesson=lesson, is_active=True)
                            if _person_key(s.first_name, s.last_name, s.phone) == key
                        ),
                        None,
                    )
                    if already is not None:
                        kept_people.add(key)
                        continue
                    # Written directly rather than through the CRUD serializer:
                    # that one holds `source` read-only, and an imported child
                    # has to say it came from a file.
                    student = ExternalStudent(
                        lesson=lesson,
                        first_name=row.first_name,
                        last_name=row.last_name,
                        phone=row.phone,
                        source=ExternalStudent.SOURCE_IMPORT,
                        created_by=user,
                        updated_by=user,
                    )
                    student.save()
                    added_people.add(key)

            if unit.municipality_code:
                group, _ = ExternalRosterGroup.objects.update_or_create(
                    branch=roster_import.branch,
                    municipality_code=unit.municipality_code,
                    defaults={
                        'group_name': unit.group_name,
                        'confirmed_by': user,
                        'confirmed_at': timezone.now(),
                    },
                )
                group.lessons.set(lessons)

        roster_import.status = ExternalRosterImport.STATUS_APPLIED
        roster_import.applied_by = user
        roster_import.applied_at = timezone.now()
        # Nothing downstream needs the sheet once it has been applied.
        roster_import.sheet_grid = []
        roster_import.save()
        roster_import.units.update(source_ref={})

    return {
        'added': len(added_people),
        'removed': removed,
        'kept': len(kept_people - added_people),
    }


def release_files(roster_import: ExternalRosterImport) -> None:
    """Drop everything we were holding from the file itself."""
    roster_import.units.update(source_ref={})
    ExternalRosterImport.objects.filter(pk=roster_import.pk).update(sheet_grid=[])


def sweep(limit: int = 3) -> dict:
    """
    What the cron does: unstick abandoned imports so nobody has to babysit one.

    Reclaiming is implicit — a stale ``running`` unit is offered to the next
    caller by ``claim_next_unit`` — so this only has to advance the imports
    nobody is watching, and close out the ones nobody came back to.
    """
    summary = {'advanced': 0, 'abandoned': 0}

    cutoff = timezone.now() - ABANDON_AFTER
    stale = ExternalRosterImport.objects.filter(
        status__in=(ExternalRosterImport.STATUS_UPLOADED, ExternalRosterImport.STATUS_PARSING),
        created_at__lt=cutoff,
    )
    for roster_import in stale:
        release_files(roster_import)
        roster_import.status = ExternalRosterImport.STATUS_FAILED
        roster_import.error = 'הייבוא לא הושלם בתוך יממה ונסגר. יש להעלות את הקובץ מחדש.'
        roster_import.save(update_fields=['status', 'error', 'updated_at'])
        summary['abandoned'] += 1

    open_imports = ExternalRosterImport.objects.filter(
        status__in=(ExternalRosterImport.STATUS_UPLOADED, ExternalRosterImport.STATUS_PARSING),
    ).order_by('created_at')
    for roster_import in open_imports:
        while summary['advanced'] < limit:
            unit = claim_next_unit(roster_import)
            if unit is None:
                break
            parse_unit(unit)
            summary['advanced'] += 1
        if summary['advanced'] >= limit:
            break

    return summary
