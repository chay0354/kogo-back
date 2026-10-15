"""
A contact's recording or picture (docs/WAHUB-CONTRACT-REAL-DEMO.md, א; docs/WAHUB-CONTRACT-LIVE-TEST.md, ב).

The file lives in Supabase Storage, in the PRIVATE bucket `wahub-media`, under
`demo/<contact_id>/<uuid>.<ext>` (a demo contact's upload from the screen) or
`manychat/<contact_id>/<hash of the address>.<ext>` (a real customer's file,
fetched from the address ManyChat passed — `archive`; until it is fetched, and
if the fetch fails, `media_url` is that address), or `office/<contact_id>/<uuid>.<ext>`
(a picture, a video or a document the office attached, uploaded by the browser
itself — `office_upload`): the same storage and the same service-role
credentials as the instructors' photos (apps/instructors/views.py), but nothing
here is public. `Message.media_url` holds the object's path; the screen gets a
short-lived signed URL from messages/{id}/media/, and the shadow bot reads the
bytes back through `download`.

The upload is validated by extension and declared type and bounded in size; it
is not decoded (a recording is not an image, and HEIC has no decoder in this
deployment).
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

BUCKET = 'wahub-media'
SIGNED_URL_SECONDS = 3600
TIMEOUT = 20
# Fetching a customer's file from ManyChat's address: a short wait, a bounded size.
FETCH_TIMEOUT = 10
FETCH_MAX_BYTES = 10 * 1024 * 1024

MB = 1024 * 1024
VOICE_MAX_BYTES = 10 * MB
IMAGE_MAX_BYTES = 5 * MB

# extension → media type, per kind. MediaRecorder gives webm/ogg (Chrome,
# Firefox) or mp4 (Safari); phones give m4a/aac/mp3 and jpg/heic/webp/png.
VOICE_TYPES = {
    'webm': 'audio/webm', 'ogg': 'audio/ogg', 'oga': 'audio/ogg', 'm4a': 'audio/mp4', 'mp4': 'audio/mp4',
    'mp3': 'audio/mpeg', 'aac': 'audio/aac', 'opus': 'audio/ogg',
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


class MediaRefused(MediaError):
    """A file the office may not attach (its kind or its size): the person's to fix, not the storage's."""


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


def archived_stem(contact_id: int, url: str) -> str:
    """
    Where the file at this ManyChat address is kept in the bucket, without its
    extension. One address is one file (ManyChat names each `original_<id>`),
    so the same address always lands on the same object: ManyChat's repeat of
    a message is known by it even after the file was fetched (inbound.py), and
    fetching it twice does not keep two copies.
    """
    return f'manychat/{contact_id}/{hashlib.sha256((url or "").strip().encode("utf-8")).hexdigest()[:32]}'


# ManyChat keeps the files customers send on its own bucket (seen 11.10.2026:
# https://manybot-files.s3.eu-central-1.amazonaws.com/<page>/wa/2026/10/11/original_<id>.ogg).
# Only an address there is a customer's file: this server fetches it and the
# office's browser opens it, and neither may be pointed at an address a customer
# typed. Another host ManyChat turns out to use is added by name in
# WAHUB_MEDIA_HOSTS (comma-separated).
_MANYCHAT_FILES_HOST = re.compile(r'^manybot-files\.s3([.-][a-z0-9-]+)?\.amazonaws\.com$')
# What the address's extension says. A WhatsApp video is an .mp4 and so is some
# audio, so an extension that may be either says nothing here: the type the file
# is served with decides when it is fetched (`archive`).
URL_VOICE_EXTENSIONS = ('ogg', 'oga', 'opus', 'm4a', 'mp3', 'aac')
URL_IMAGE_EXTENSIONS = ('jpg', 'jpeg', 'png', 'webp', 'heic')


def _url_parts(url: str) -> tuple[str, str]:
    """
    (host, extension) of a bare https address; ('', '') for anything else — a
    sentence, a link inside one, and any address that is not written plainly:
    the text is the customer's, and what is kept as `media_url` is later read
    as either an address or a path in our bucket (`is_external`), so it must
    be unmistakably an address — `https://` as ManyChat writes it, and a path
    with no `..`, no backslash and nothing percent-encoded to hide one.
    """
    text = (url or '').strip()
    if not text.startswith('https://') or any(char.isspace() for char in text):
        return '', ''
    rest = text[len('https://'):].split('?', 1)[0].split('#', 1)[0]
    host, _, path = rest.partition('/')
    host = host.lower()
    if '@' in host or not host:
        return '', ''
    if '\\' in path or '%' in path or any(part in ('.', '..') for part in path.split('/')):
        return '', ''
    name = path.rstrip('/').rsplit('/', 1)[-1]
    return host, (name.rsplit('.', 1)[-1].lower() if '.' in name else '')


def is_manychat_file(url: str) -> bool:
    """An address on the bucket ManyChat keeps customers' files in (or on a host named in WAHUB_MEDIA_HOSTS)."""
    host, _ = _url_parts(url)
    if not host:
        return False
    extra = {name.strip().lower() for name in str(getattr(settings, 'WAHUB_MEDIA_HOSTS', '') or '').split(',') if name.strip()}
    return bool(_MANYCHAT_FILES_HOST.match(host)) or host in extra


def kind_of_url(url: str) -> str:
    """
    What a message whose whole text is one address of a ManyChat file is, by
    its extension: 'voice', 'image', or 'other' for a file we do not know. ''
    for anything else — a sentence, a link inside a sentence, a link to a site
    (even to a picture on it): that stays the text the customer wrote.
    """
    if not is_manychat_file(url):
        return ''
    _, ext = _url_parts(url)
    if ext in URL_VOICE_EXTENSIONS:
        return 'voice'
    if ext in URL_IMAGE_EXTENSIONS:
        return 'image'
    return 'other'


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


def _storage_error(response) -> str:
    """The storage's own word for a refusal ("Bucket not found", "InvalidKey"…): its `error` field only, never the body."""
    try:
        body = response.json()
    except ValueError:
        return ''
    if not isinstance(body, dict):
        return ''
    return ' '.join(str(body.get('error') or '').split())[:60]


def _bucket_missing(response) -> bool:
    """
    The bucket is not there yet. Supabase Storage says so with HTTP 400 and
    `"statusCode": "404"` in the body (production, 11.10.2026: every upload was
    refused with 400 and the bucket was never made, because only a 404 status
    was read as "missing"); a plain 404 is read the same way.
    """
    if response.status_code not in (400, 404):
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    words = f"{body.get('message', '')} {body.get('error', '')}".lower()
    return 'bucket' in words and 'not found' in words


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
    return _store(contact_id, upload.read(), media_type, path=object_path(contact_id, ext))


def _store(contact_id: int, data: bytes, media_type: str, *, path: str, replace: bool = False) -> str:
    """Write the object at `path`; `replace` lets the same file be written over itself (a ManyChat file fetched again)."""
    url = f'{_base()}/object/{BUCKET}/{path}'
    headers = {'Content-Type': media_type, 'x-upsert': 'true' if replace else 'false'}
    response = _call('POST', url, headers=headers, data=data, failure='שמירת הקובץ נכשלה. נסו שוב.')
    if _bucket_missing(response):
        ensure_bucket()
        response = _call('POST', url, headers=headers, data=data, failure='שמירת הקובץ נכשלה. נסו שוב.')
    if response.status_code >= 300:
        logger.error(
            'wahub media: storage rejected upload for contact %s: %s %s', contact_id, response.status_code, _storage_error(response),
        )
        raise MediaError('אחסון המדיה דחה את הקובץ.')
    return path


def fetch(url: str, *, timeout: float = FETCH_TIMEOUT) -> tuple[bytes, str, str, str]:
    """
    The file ManyChat passed by address: (bytes, ext, kind, media_type) with
    kind 'voice' or 'image'. The kind comes from the address's extension, else
    from the Content-Type the file is served with. Only an address on
    ManyChat's own bucket is fetched, and a redirect is not followed. Raises
    MediaError (another host, too big, unknown type, no connection).
    """
    if not is_manychat_file(url):
        raise MediaError('הכתובת אינה של קובץ ב-ManyChat')
    try:
        response = requests.get(url, timeout=(min(5, timeout), timeout), stream=True, allow_redirects=False)
    except requests.RequestException as exc:
        raise MediaError(f'לא ניתן להוריד את הקובץ מ-ManyChat ({type(exc).__name__})') from exc
    try:
        if response.status_code >= 300:
            raise MediaError(f'ManyChat לא נתן את הקובץ ({response.status_code})')
        chunks, size = [], 0
        try:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                size += len(chunk)
                if size > FETCH_MAX_BYTES:
                    raise MediaError(f'הקובץ גדול מדי (מעל {FETCH_MAX_BYTES // MB}MB)')
                chunks.append(chunk)
        except requests.RequestException as exc:
            raise MediaError(f'הורדת הקובץ מ-ManyChat נקטעה ({type(exc).__name__})') from exc
    finally:
        response.close()
    data = b''.join(chunks)
    if not data:
        raise MediaError('הקובץ ריק')
    declared = (response.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
    _, ext = _url_parts(url)
    if ext not in URL_VOICE_EXTENSIONS and ext not in URL_IMAGE_EXTENSIONS:
        ext = _EXTENSION_BY_TYPE.get(declared) or ''
    if ext in VOICE_TYPES:
        kind, media_type = 'voice', VOICE_TYPES[ext]
    elif ext in IMAGE_TYPES:
        kind, media_type = 'image', IMAGE_TYPES[ext]
    else:
        raise MediaError(f'סוג קובץ לא נתמך ({declared or ext or "לא ידוע"})')
    # An Opus recording is an Ogg container; the transcription knows that name.
    if ext == 'opus':
        ext = 'ogg'
    return data, ext, kind, media_type


def archive(contact_id: int, url: str, *, timeout: float = FETCH_TIMEOUT) -> tuple[str, str]:
    """
    Fetch the file ManyChat passed by address (`fetch`) and keep it in the
    bucket under manychat/<contact_id>/…; returns (path, kind). Raises
    MediaError (see `fetch`, or the storage refused).
    """
    _require()
    data, ext, kind, media_type = fetch(url, timeout=timeout)
    return _store(contact_id, data, media_type, path=f'{archived_stem(contact_id, url)}.{ext}', replace=True), kind


def read(location: str, *, timeout: float = FETCH_TIMEOUT) -> tuple[bytes, str, str]:
    """
    A message's file wherever it is: kept in the bucket (a path), or still at
    ManyChat's address because the copy failed — the customer's words must not
    wait for the storage. Returns (bytes, media_type, filename). Raises MediaError.
    """
    if is_external(location):
        data, ext, _kind, media_type = fetch(location, timeout=timeout)
        return data, media_type, f'file.{ext}'
    return download(location), media_type_of(location), location.rsplit('/', 1)[-1]


def is_external(path: str) -> bool:
    """An address kept as it came from ManyChat, not yet (or never) archived in the bucket."""
    return (path or '').strip().lower().startswith(('http://', 'https://'))


# The objects this module writes: demo/<contact>/<uuid>.<ext>, manychat/<contact>/<hash>.<ext>, and
# office/<contact>/<uuid>.<ext> (a file the office attached, uploaded by the browser itself — `office_upload`).
_KEPT_PATH = re.compile(r'^(demo|manychat|office)/\d+/[A-Za-z0-9_-]{1,64}\.[a-z0-9]{2,5}$')


def _kept(path: str) -> str:
    """
    The path of an object this module wrote, and nothing else. The service-role
    key reads whatever it is pointed at, in any bucket, so a `media_url` that is
    not plainly one of ours is never handed to the storage.
    """
    if not _KEPT_PATH.match(path or ''):
        logger.error('wahub media: refused to read a path that is not one of ours')
        raise MediaError('הקובץ לא נמצא באחסון.')
    return path


def signed_url(path: str, *, expires_in: int = SIGNED_URL_SECONDS) -> str:
    """A short-lived address the browser can open. Raises MediaError."""
    _require()
    response = _call(
        'POST', f'{_base()}/object/sign/{BUCKET}/{_kept(path)}', json={'expiresIn': int(expires_in)},
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
    response = _call('GET', f'{_base()}/object/authenticated/{BUCKET}/{_kept(path)}', failure='אין חיבור לאחסון המדיה.')
    if response.status_code >= 300:
        logger.error('wahub media: storage refused to read %s: %s', path, response.status_code)
        raise MediaError('הקובץ לא נמצא באחסון.' if response.status_code == 404 else 'לא ניתן לקרוא את הקובץ מהאחסון.')
    return response.content


# --- a file the office attaches (docs/WAHUB-CONTRACT.md, "צירוף קבצים") -----------------------------
#
# The browser uploads it straight to the bucket: Vercel takes no request body over 4.5MB, and a video
# is more. In three steps — `office_upload` checks the file's name, type and size and hands out a
# signed address to upload to; the browser PUTs the file there; `office_kind` checks the path the
# screen sends back before anything is sent. Nothing here sends anything.

# kind → (extension → media type, the largest file, the word for it). WhatsApp's own ceilings are
# 5MB for a picture and 16MB for a video; a document is held to 25MB.
OFFICE_KINDS = {
    'image': ({'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp'}, 5 * MB, 'תמונה'),
    'video': ({'mp4': 'video/mp4', '3gp': 'video/3gpp'}, 16 * MB, 'סרטון'),
    'file': ({
        'pdf': 'application/pdf',
        'doc': 'application/msword',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'xls': 'application/vnd.ms-excel',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }, 25 * MB, 'מסמך'),
}
_OFFICE_EXTENSIONS = {ext: kind for kind, (types, _limit, _word) in OFFICE_KINDS.items() for ext in types}
_OFFICE_BY_TYPE = {
    media_type: ext for kind, (types, _limit, _word) in OFFICE_KINDS.items() for ext, media_type in reversed(list(types.items()))
}
# The address ManyChat and the customer open: long enough for WhatsApp to fetch the file, and for a
# link (a video, a document) to still open a few days later.
OFFICE_READ_SECONDS = 7 * 24 * 3600
OFFICE_NOT_SUPPORTED = 'סוג הקובץ לא נתמך. אפשר לשלוח תמונה (jpg, png, webp), סרטון (mp4, 3gp) או מסמך (pdf, doc, docx, xls, xlsx).'


def check_office_file(name, content_type, size) -> tuple[str, str, str]:
    """(kind, extension, media type) of a file the office may attach; raises MediaRefused with the reason otherwise."""
    parts = str(name or '').strip().rsplit('.', 1)
    ext = parts[1].lower() if len(parts) == 2 else ''
    if ext == 'jpeg':
        ext = 'jpg'
    if ext not in _OFFICE_EXTENSIONS:
        declared = str(content_type or '').split(';', 1)[0].strip().lower()
        ext = _OFFICE_BY_TYPE.get(declared, '')
    if ext not in _OFFICE_EXTENSIONS:
        raise MediaRefused(OFFICE_NOT_SUPPORTED)
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise MediaRefused('נדרש גודל הקובץ (size, בבתים).')
    kind = _OFFICE_EXTENSIONS[ext]
    types, limit, word = OFFICE_KINDS[kind]
    if size <= 0:
        raise MediaRefused('הקובץ ריק.')
    if size > limit:
        raise MediaRefused(f'הקובץ גדול מדי. {word} עד {limit // MB}MB.')
    return kind, ext, types[ext]


def office_path(contact_id: int, ext: str) -> str:
    return f'office/{contact_id}/{uuid.uuid4()}.{ext}'


def office_kind(contact_id: int, path: str) -> str:
    """
    The kind of a file the office uploaded for this contact, by its path. A
    path that is not plainly one this server handed out for this very contact
    is refused (MediaError): the service-role key signs whatever it is pointed at.
    """
    match = re.fullmatch(rf'office/{int(contact_id)}/[A-Za-z0-9_-]{{1,64}}\.([a-z0-9]{{2,5}})', str(path or ''))
    kind = _OFFICE_EXTENSIONS.get(match.group(1)) if match else None
    if kind is None:
        raise MediaError('הקובץ לא נמצא. צרפו אותו שוב.')
    return kind


def office_upload(contact_id: int, name, content_type, size) -> dict:
    """
    Step one: {path, kind, content_type, upload_url} for a file the office may
    attach. `upload_url` is the storage's signed address for uploading this one
    object (`object/upload/sign/…?token=…`): the browser PUTs the file there,
    with no key of ours. The bucket is made when it is missing. Raises MediaError.
    """
    kind, ext, media_type = check_office_file(name, content_type, size)
    _require()
    path = office_path(contact_id, ext)
    url = f'{_base()}/object/upload/sign/{BUCKET}/{_kept(path)}'
    response = _call('POST', url, json={}, failure='אין חיבור לאחסון המדיה.')
    if _bucket_missing(response):
        ensure_bucket()
        response = _call('POST', url, json={}, failure='אין חיבור לאחסון המדיה.')
    if response.status_code >= 300:
        logger.error('wahub media: storage refused an upload address for contact %s: %s %s', contact_id, response.status_code, _storage_error(response))
        raise MediaError('אחסון המדיה לא נתן כתובת להעלאה.')
    try:
        body = response.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    signed = str(body.get('url') or body.get('signedURL') or '').strip()
    if not signed:
        raise MediaError('אחסון המדיה לא נתן כתובת להעלאה.')
    upload_url = signed if signed.startswith('http') else f'{_base()}/{signed.lstrip("/")}'
    token = str(body.get('token') or '').strip()
    if token and 'token=' not in upload_url:
        upload_url += ('&' if '?' in upload_url else '?') + f'token={token}'
    return {'path': path, 'kind': kind, 'content_type': media_type, 'upload_url': upload_url}
