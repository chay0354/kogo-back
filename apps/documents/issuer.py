"""
Who issues the document.

A tax document must carry the issuer's registered name, company number and
address, so these live in one place that both the PDF and any future settings
screen read from. Name and company number are the same values the rental
agreement prints; they are imported rather than repeated so the two can never
drift apart.
"""
from apps.scheduling.rental_agreement.content import STUDIO_COMPANY_NUMBER, STUDIO_NAME

ISSUER_NAME = STUDIO_NAME
ISSUER_COMPANY_NUMBER = STUDIO_COMPANY_NUMBER
ISSUER_ADDRESS = 'רפאל איתן 5, קניון ספיר, קומה 1, פתח תקווה'
ISSUER_PHONE = '050-9424755'
# The only contact printed on invoices, by the owner's instruction.
ISSUER_EMAIL = 'Invoice@cogo.co.il'
