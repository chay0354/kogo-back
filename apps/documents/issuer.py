"""Issuer details and the statutory markings printed on every tax document."""
from apps.scheduling.rental_agreement.content import STUDIO_COMPANY_NUMBER, STUDIO_NAME

ISSUER_NAME = STUDIO_NAME
ISSUER_COMPANY_NUMBER = STUDIO_COMPANY_NUMBER
ISSUER_ADDRESS = 'רפאל איתן 5, קניון ספיר, קומה 1, פתח תקווה'
ISSUER_PHONE = '050-9424755'
ISSUER_EMAIL = 'Invoice@cogo.co.il'

# תקנה 9א(א)(1): the words "עוסק מורשה" and the VAT registration number belong on
# the face of the document, printed — not only in a letterhead graphic.
VAT_REGISTRATION_LINE = f'עוסק מורשה {ISSUER_COMPANY_NUMBER}'
ISSUER_LINE = f'{ISSUER_NAME} · {VAT_REGISTRATION_LINE}'

# תקנה 9א(א)(2): "מקור" goes on the original copy only.
ORIGINAL_MARK = 'מקור'

# סעיף 18ב(א): a document sent by computer carries these words "בצורה בולטת לעין".
COMPUTERIZED_MARK = 'מסמך ממוחשב'
