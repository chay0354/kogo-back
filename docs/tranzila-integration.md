# Tranzila — how Kogo uses it

The API itself is described in `tranzila-api.md` and the three YAML files
beside it. This file is the other half: which of *our* code talks to
Tranzila, through which terminal, and what happens when Tranzila does not
answer. No credentials live here or anywhere in the repository; they are
environment variables on Vercel.

## Terminals

Two terminals exist on the Tranzila account, and they are not
interchangeable:

| Terminal            | Purpose                                             | Env var                          |
|---------------------|-----------------------------------------------------|----------------------------------|
| `fxpmichalweb`      | Charges — one-off payments, standing orders, refunds | `TRANZILA_TERMINAL` / `TRANZILA_PROD_TERMINAL` |
| `fxpmichalwebtok`   | Token terminal — stores a card once, charges it later without the card present | `TRANZILA_PROD_TOKEN_TERMINAL` |

Billing documents (the invoices Tranzila issues and hosts) go through
`TRANZILA_BILLING_TERMINAL`. When that variable is **empty**, every local
document creation skips Tranzila entirely — which is exactly the state of
`.env.local`, and why a document created on a developer's machine never
reaches the real account. Keep it empty locally.

`TRANZILA_ENVIRONMENT` is `development` locally and `production` on Vercel.

## Two ledgers

There are two sources of truth for "documents", and the invoices page
merges them:

1. **Tranzila-issued** — documents Tranzila created itself when a card was
   charged. Fetched live with `get_documents`; the PDF is theirs.
2. **Local `FormalDocument` rows** — created through
   `POST /documents/documents/create-document/`. Each one is *also* sent to
   Tranzila via `create_document`; on success the row stores
   `tranzila_doc_id`, `tranzila_retrieval_key`, `pdf_url` and
   `tranzila_issued=True`.

`apps/core/tranzila_ledger.list_ledger_documents()` merges the two,
deduplicating on Tranzila's document id and on our document number. Pass
`local_only=1` to skip the live call.

The official PDF is always Tranzila's, at
`https://my.tranzila.com/api/get_financial_document/{retrieval_key}`.
**There is no local invoice PDF generator.** The period report
(`period_report_pdf.py`) is a management report, not a legal document.

## What goes to Tranzila, and what does not

`apps/documents/models.TRANZILA_DOCUMENT_TYPE` maps our types to theirs:

| Ours                  | Tranzila | Sent? |
|-----------------------|----------|-------|
| `tax_invoice`         | `IN`     | yes   |
| `receipt`             | `RE`     | yes   |
| `combined`            | `IR`     | yes   |
| `transaction_invoice` | `DI`     | yes   |
| `credit_invoice`      | —        | **no** — exists only locally, no PDF |

## Fail silently — and what that costs

`service._attempt_tranzila(doc)` swallows every Tranzila error. The local
row is saved and numbered either way. A failure therefore leaves a document
with `tranzila_issued=False` and no `pdf_url`: a consumed serial number
with no legal document behind it, and nothing on the page that says so.
Filter on `tranzila_issued=False` to find these.

## Numbering

`DocumentCounter.next_number(year)` is **one sequence per year across all
document types**. Any future draft type must not draw from it — a draft
that consumes a tax-invoice serial creates a numbering gap the tax
authority will ask about.

## Rules for touching any of this

- `payment_service.py`, `tranzila_service.py`, charge/refund paths, amounts
  and issuance are the contract developer's active work. Do not edit them
  without coordinating.
- Never run anything against the real terminals from a local machine.
- Never store terminal passwords, tokens, card data or transaction exports
  in the repository.
