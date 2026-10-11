"""
A demo contact's recording or picture (docs/WAHUB-CONTRACT-REAL-DEMO.md, א).

The file lives in Supabase Storage, in the PRIVATE bucket `wahub-media`, under
`demo/<contact_id>/<uuid>.<ext>`: the same storage and the same service-role
credentials as the instructors' photos (apps/instructors/views.py), but nothing
here is public. `Message.media_url` holds the object's path; the screen gets a
short-lived signed URL from messages/{id}/media/, and the shadow bot reads the
bytes back through `download`.

Only demo contacts ever have media here: ManyChat's copy of a real message
carries text alone. The upload is validated by extension and declared type and
bounded in size; it is not decoded (a recording is not an image, and HEIC has
no decoder in this deployment).
"""
from __future__ import annotations

import logging
import uuid

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

BUCKET = 'wahub-media'
SIGNED_URL_SECONDS = 3600
TIMEOUT = 20

MB = 1024 * 1024
VOICE_MAX_BYTES = 10 * MB
IMAGE_MAX_BYTES = 5 * MB

# extension → media type, per kind. MediaRecorder gives webm/ogg (Chrome,
# Firefox) or mp4 (Safari); phones give m4a/aac/mp3 and jpg/heic/webp/png.
VOICE_TYPES = {
    'webm': 'audio/webm', 'ogg': 'audio/ogg', 'oga': 'audio/ogg', 'm4a': 'audio/mp4', 'mp4': 'audio/mp4',
    'mp3': 'audio/mpeg', 'aac': 'audio/aac',
}
IMAGE_TYPES = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'heic': 'image/heic'}
# A declared type with no usable filename: the first extension of each type.
_EXTENSION_BY_TYPE = {
    **{media_type: ext for ext, media_type in reversed(list(VOICE_TYPES.items()))},
    **{media_type: ext for ext, media_type in reversed(list(IMAGE_TYPES.items()))},
    'audio/x-m4a': 'm4a', 'audio/mp3': 'mp3', 'audio/wave': None, 'image/jpg': 'jpg', 'image/heif': 'heic',
}

NOT_CONFIGURED = 'אחסון המדיה אינו מוגדר בשרת (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY).'


class MediaError(Exception):
    """A refused file or a storage call that did not go through. The message is for the screen, in Hebrew."""


class MediaNotConfigured(MediaError):
    pass


# --- what is accepted -------------------------------------------------------------------------------

def _extension(upload, message_type: str) -> str | None:
    """The stored extension, from the filename first and the declared type second; None when neither fits."""
    allowed = VOICE_TYPES if message_type == 'voice' else IMAGE_TYPES
    name = (getattr(upload, 'name', '') or '').rsplit('.', 1)
    if len(name) == 2 and name[1].lower() in allowed:
        return name[1].lower()
    declared = (getattr(upload, 'content_type', '') or '').split(';', 1)[0].strip().lower()
    ext = _EXTENSION_BY_TYPE.get(declared)
    return ext if ext in allowed else None


def check_upload(upload, message_type: str) -> tuple[str, str]:
    """(extension, media_type) for a file the contract accepts; raises MediaError otherwise."""
    if message_type not in ('voice', 'image'):
        raise MediaError('message_type: text, voice או image.')
    if upload is None:
        raise MediaError('נדרש קובץ (file) להודעה קולית או לתמונה.')
    ext = _extension(upload, message_type)
    if ext is None:
        if message_type == 'voice':
            raise MediaError('סוג ההקלטה לא נתמך. מקבלים webm, ogg, m4a, mp3 או aac.')
        raise MediaError('סוג התמונה לא נתמך. מקבלים jpg, png, webp או heic.')
    limit = VOICE_MAX_BYTES if message_type == 'voice' else IMAGE_MAX_BYTES
    if (getattr(upload, 'size', 0) or 0) > limit:
        raise MediaError(f'הקובץ גדול מדי. {"הקלטה" if message_type == "voice" else "תמונה"} עד {limit // MB}MB.')
    if not (getattr(upload, 'size', 0) or 0):
        raise MediaError('הקובץ ריק.')
    return ext, (VOICE_TYPES if message_type == 'voice' else IMAGE_TYPES)[ext]


def media_type_of(path: str) -> str:
    """The media type a stored path implies, from its extension ('' when unknown)."""
    ext = (path or '').rsplit('.', 1)[-1].lower() if '.' in (path or '') else ''
    return VOICE_TYPES.get(ext) or IMAGE_TYPES.get(ext) or ''


def object_path(contact_id: int, ext: str) -> str:
    return f'demo/{contact_id}/{uuid.uuid4()}.{ext}'


# --- the storage ------------------------------------------------------------------------------------

def _key() -> str:
    from apps.core.scoping import integration_credential

    return integration_credential('SUPABASE_SERVICE_ROLE_KEY')


def configured() -> bool:
    return bool((getattr(settings, 'SUPABASE_URL', '') or '').strip() and _key())


def _base() -> str:
    return settings.SUPABASE_URL.rstrip('/') + '/storage/v1'


def _headers() -> dict:
    # The service-role key bypasses RLS; it stays here and is never logged or returned.
    key = _key()
    return {'apikey': key, 'Authorization': f'Bearer {key}'}


def _require():
    if not configured():
        raise MediaNotConfigured(NOT_CONFIGURED)


def _call(method: str, url: str, *, failure: str, **kwargs):
    try:
        return requests.request(method, url, headers={**_headers(), **kwargs.pop('headers', {})}, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        logger.warning('wahub media: %s %s failed: %s', method, url.split('/storage/v1', 1)[-1], type(exc).__name__)
        raise MediaError(failure) from exc


def _bucket_missing(response) -> bool:
    if response.status_code != 404:
        return False
    try:
        return 'bucket' in str(response.json().get('message', '')).lower()
    except ValueError:
        return False


def ensure_bucket() -> bool:
    """Create the private bucket. True when it was made, False when it was already there."""
    response = _call(
        'POST', f'{_base()}/bucket', json={'id': BUCKET, 'name': BUCKET, 'public': False},
        failure='אין חיבור לאחסון המדיה.',
    )
    if response.status_code < 300:
        logger.info('wahub media: bucket %s created', BUCKET)
        return True
    if response.status_code == 409:
        return False
    # The body may echo the key back in an error; only the status is kept.
    logger.error('wahub media: storage refused to create bucket %s: %s', BUCKET, response.status_code)
    raise MediaError('אחסון המדיה סירב ליצור את ה-bucket. בדקו את ההרשאות של המפתח.')


def upload(contact_id: int, upload, message_type: str) -> str:
    """
    Keep the file under demo/<contact_id>/<uuid>.<ext>; returns the path. The
    bucket is made on the first upload when it is missing. Raises MediaError.
    """
    _require()
    ext, media_type = check_upload(upload, message_type)
    upload.seek(0)
    data = upload.read()
    path = object_path(contact_id, ext)
    url = f'{_base()}/object/{BUCKET}/{path}'
    headers = {'Content-Type': media_type, 'x-upsert': 'false'}
    response = _call('POST', url, headers=headers, data=data, failure='שמירת הקובץ נכשלה. נסו שוב.')
    if _bucket_missing(response):
        ensure_bucket()
        response = _call('POST', url, headers=headers, data=data, failure='שמירת הקובץ נכשלה. נסו שוב.')
    if response.status_code >= 300:
        logger.error('wahub media: storage rejected upload for contact %s: %s', contact_id, response.status_code)
        raise MediaError('אחסון המדיה דחה את הקובץ.')
    return path


def signed_url(path: str, *, expires_in: int = SIGNED_URL_SECONDS) -> str:
    """A short-lived address the browser can open. Raises MediaError."""
    _require()
    response = _call(
        'POST', f'{_base()}/object/sign/{BUCKET}/{path}', json={'expiresIn': int(expires_in)},
        failure='אין חיבור לאחסון המדיה.',
    )
    if response.status_code >= 300:
        logger.error('wahub media: storage refused to sign %s: %s', path, response.status_code)
        raise MediaError('הקובץ לא נמצא באחסון.' if response.status_code == 404 else 'אחסון המדיה לא נתן קישור לקובץ.')
    try:
        signed = response.json().get('signedURL') or ''
    except ValueError:
        signed = ''
    if not signed:
        raise MediaError('אחסון המדיה לא נתן קישור לקובץ.')
    if signed.startswith('http'):
        return signed
    return f'{_base()}/{signed.lstrip("/")}'


def download(path: str) -> bytes:
    """The object's bytes, for the shadow bot. Raises MediaError."""
    _require()
    response = _call('GET', f'{_base()}/object/authenticated/{BUCKET}/{path}', failure='אין חיבור לאחסון המדיה.')
    if response.status_code >= 300:
        logger.error('wahub media: storage refused to read %s: %s', path, response.status_code)
        raise MediaError('הקובץ לא נמצא באחסון.' if response.status_code == 404 else 'לא ניתן לקרוא את הקובץ מהאחסון.')
    return response.content
