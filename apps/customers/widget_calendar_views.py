"""
The registration form's "add to calendar" for a trial lesson.

GET  /customers/widget/trial-event/?lesson_id=…&date=YYYY-MM-DD
     → the event's title, the link that opens Google Calendar with it filled
       in, and the path of the .ics file below.
GET  /customers/widget/trial-event.ics?lesson_id=…&date=YYYY-MM-DD
     → the same event as a file, for Apple Calendar and every other calendar.
       Served as text/calendar: a phone hands that straight to its calendar.

Both are open to anyone, like the catalogue they are built from: an event holds
the lesson's own details and nothing of whoever booked it.
"""
import uuid
from datetime import date
from urllib.parse import urlencode

from django.http import HttpResponse
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.courses.models import Lesson
from apps.customers import trial_calendar
from apps.customers.widget_limits import too_many

CALENDAR_HOURLY_LIMIT = 300
NOT_FOUND = {'error': 'השיעור לא נמצא'}


def _event_for(request):
    """The event the request asks for, or None when there is no such lesson on such a day."""
    raw_lesson = (request.query_params.get('lesson_id') or '').strip()
    raw_date = (request.query_params.get('date') or '').strip()
    try:
        lesson_id = uuid.UUID(raw_lesson)
        on = date.fromisoformat(raw_date)
    except (ValueError, AttributeError, TypeError):
        return None
    lesson = (
        Lesson.objects
        .select_related('course__branch__city', 'course__course_type', 'instructor')
        .filter(id=lesson_id, course__is_active=True)
        .first()
    )
    if lesson is None:
        return None
    try:
        return trial_calendar.build_trial_event(lesson, on)
    except trial_calendar.NoSuchEvent:
        return None


class WidgetTrialEventView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        refused = too_many('calendar', request, CALENDAR_HOURLY_LIMIT)
        if refused is not None:
            return refused
        event = _event_for(request)
        if event is None:
            return Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)
        query = urlencode({
            'lesson_id': request.query_params['lesson_id'].strip(),
            'date': request.query_params['date'].strip(),
        })
        return Response({
            'title': event.title,
            'google_url': trial_calendar.google_url(event),
            # Relative to the API: the form knows where the API is, and a link
            # built here would carry whatever host a proxy told us we are.
            'ics_path': f'customers/widget/trial-event.ics?{query}',
        })


class WidgetTrialEventFileView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        refused = too_many('calendar', request, CALENDAR_HOURLY_LIMIT)
        if refused is not None:
            return refused
        event = _event_for(request)
        if event is None:
            return Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)
        response = HttpResponse(
            trial_calendar.ics_file(event), content_type='text/calendar; charset=utf-8',
        )
        # Inline: a phone opens its "add to calendar" sheet instead of saving a file.
        response['Content-Disposition'] = 'inline; filename="kogo-trial-lesson.ics"'
        response['Cache-Control'] = 'private, max-age=300'
        return response
