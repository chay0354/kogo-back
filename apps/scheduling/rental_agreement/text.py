"""The rental contract as plain paragraphs: what the tenant reads on the signing page, and what they sign.

contract_paragraphs(terms) says what generate_tenancy_contract_pdf(terms)
draws, in the same order and from the same helpers and content.py, one string
per paragraph. The payment table becomes one line per row and one per total.
The signing page shows these paragraphs, and the Signature row keeps them as
the document that was signed (contract_html), so the office's signature record
reads exactly what the tenant read.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from . import content
from .generator import (
    PAYMENT_TABLE_HEADING,
    STUDIO_PARTY_ALIAS,
    STUDIO_PARTY_HEADING,
    TENANT_PARTY_ALIAS,
    TENANT_PARTY_HEADING,
    activity_line,
    pay_label,
    row_hours,
    row_place,
    row_when,
    section_2_items,
    shekels,
    studio_contact_line,
    studio_party_line,
    tenant_contact_line,
    tenant_line,
    vat_label,
)
from .terms import KIND_ONE_TIME


def _row_line(row: dict, terms: dict) -> str:
    total = 'סה"כ (לפני מע"מ)' if row['kind'] == KIND_ONE_TIME else 'סה"כ לחודש (לפני מע"מ)'
    return (
        f'{row_when(row)} · {row_hours(row)} · {row_place(row, terms)} · '
        f'תעריף שעתי (לפני מע"מ): {shekels(row["rate"])} · {total}: {shekels(row["sum"])}'
    )


def contract_paragraphs(terms: dict) -> list[str]:
    """The contract the terms describe, as the paragraphs it reads in."""
    studio = terms['studio']
    tenant = terms['tenant']
    paragraphs = [
        content.AGREEMENT_TITLE,
        studio_contact_line(studio),
        STUDIO_PARTY_HEADING,
        studio_party_line(studio),
        STUDIO_PARTY_ALIAS,
        TENANT_PARTY_HEADING,
        tenant_line(tenant),
    ]
    contact = tenant_contact_line(tenant)
    if contact:
        paragraphs.append(contact)
    paragraphs += [TENANT_PARTY_ALIAS, activity_line(terms), PAYMENT_TABLE_HEADING]
    paragraphs += [_row_line(row, terms) for row in terms['rows']]
    paragraphs += [
        f'סה"כ לפני מע"מ: {shekels(terms["monthly_amount"])}',
        f'{vat_label(terms)} {shekels(terms["vat_amount"])}',
        f'{pay_label(terms)} {shekels(terms["monthly_total"])}',
        content.SECTION_2_TITLE,
    ]
    for text, emphasized, bullets in section_2_items(terms):
        paragraphs.append(text)
        if emphasized:
            paragraphs.append(emphasized)
        paragraphs += [f'• {bullet}' for bullet, _bold in bullets]
    paragraphs += [content.SECTION_3_TITLE, content.SECTION_3_INTRO]
    for i, item in enumerate(content.SECTION_3_ITEMS, start=1):
        paragraphs.append(f'{i}. {item[0]}')
        paragraphs.append(item[1])
        if len(item) > 2 and item[2]:
            paragraphs.append(item[2])
    paragraphs.append(content.SIGNATURE_INTRO)
    # One line each, single spaces: the form html_to_paragraphs gives back.
    return [' '.join(paragraph.split()) for paragraph in paragraphs]


def contract_html(terms: dict) -> str:
    """The paragraphs as the HTML a Signature keeps (document_html): one escaped <p> each."""
    return ''.join(f'<p>{escape(paragraph)}</p>' for paragraph in contract_paragraphs(terms))
