"""Shared test helpers: real PNG signatures and stored Signature rows."""
import base64
import hashlib
import io

from django.utils import timezone
from PIL import Image, ImageDraw

from apps.signatures.models import Signature

TERMS_HTML = (
    '<p><strong>1. מחיר שנתי:</strong> המחיר הינו מחיר שנתי &amp; חודשי.</p>'
    '<ul><li>סעיף ברשימה</li></ul>'
    '<p>מסמכים ממוחשבים: ההורה מסכים לקבל חשבוניות בדוא"ל.</p>'
)


def png_bytes(seed: int = 0) -> bytes:
    """A small transparent PNG with one stroke; a different seed draws a different stroke."""
    image = Image.new('RGBA', (300, 100), (255, 255, 255, 0))
    ImageDraw.Draw(image).line((10, 20 + seed, 290, 80 - seed), fill=(20, 20, 60, 255), width=3)
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


def png_data_url(seed: int = 0) -> str:
    return 'data:image/png;base64,' + base64.b64encode(png_bytes(seed)).decode('ascii')


def make_signature(*, family=None, branch=None, children=(), seed=0, signed_at=None, **overrides):
    png = png_bytes(seed)
    fields = {
        'kind': Signature.KIND_REGISTRATION_TERMS,
        'signed_at': signed_at or timezone.now(),
        'signer_name': 'רות ניסן',
        'signer_id_number': '123456782',
        'signer_phone': '0521234567',
        'signer_email': 'ruth@example.com',
        'family': family,
        'branch': branch,
        'document_title': 'תקנון הרשמה',
        'document_html': TERMS_HTML,
        'document_sha256': hashlib.sha256(TERMS_HTML.encode('utf-8')).hexdigest(),
        'consents': {'health': True, 'terms': True, 'computerized_documents': True},
        'signature_png': png,
        'signature_sha256': hashlib.sha256(png).hexdigest(),
        'ip_address': '203.0.113.9',
        'user_agent': 'Mozilla/5.0 (Test)',
        'refs': {'payment_ids': ['p-1'], 'trial': False},
    }
    fields.update(overrides)
    signature = Signature.objects.create(**fields)
    if children:
        signature.children.add(*children)
    return signature
