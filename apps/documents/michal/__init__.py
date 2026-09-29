"""
Documents for Michal Kagan's site: a business line of the company, with a site
and a payment page of its own, whose tax documents kogo issues.

Her site takes the payment (Tranzila, its own hosted page) and then asks kogo,
with a key of its own, for the חשבונית מס/קבלה of that payment, or for a
credit note when it refunds one. kogo numbers the document in a run of its own
(MK, numbering.py), files it under her Business, signs it and mails it to her
client like every other document it issues.

Her clients are not the company's class families. Each one is kept as a
business customer tagged to her Business, never as a Family, Parent or Child,
so none of them reaches the families list, a class roster, a broadcast or the
lessons' revenue.
"""
