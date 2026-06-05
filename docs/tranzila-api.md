# Tranzila API Reference

---

## Error Codes

| Error Code | Meaning |
|------------|---------|
| 10000 | Invalid characters in terminal name or terminal name not found in system |
| 10001 | Unknown document action |
| 10002 | Unknown document type |
| 10003 | Invalid document date format or document date not allowed by regulation |
| 10004 | VAT percent not numeric |
| 10005 | Unsupported document language |
| 10006 | Unsupported response language |
| 10007 | Document base number for document type was not found in terminal settings |
| 10008 | Items total ILS amount differs from Payments total ILS amount – unsupported partial document coverage |
| 10009 | Invalid related document number |
| 10010 | Related document sent but missing relation type |
| 10011 | Invalid related document relation type |
| 10100 | Client id number not numeric |
| 10101 | Country code not ISO 2 letter code |
| 10102 | Invalid email format |
| 10200 | Unsupported or unknown item currency code |
| 10201 | Item name empty or contains illegal characters |
| 10202 | Unknown item type |
| 10203 | Item units number must be numeric and greater than 0 |
| 10204 | Unknown item unit type |
| 10206 | Item unit price not sent, or not numeric or empty |
| 10208 | Unknown item price type |
| 10210 | Item currency code not ILS but no exchange rate provided |
| 10300 | `<FAILED_TO_CREATE_DOCUMENT>` |
| 10301 | Terminal settings not found |
| 10302 | `<FAILED_TO_CREATE_PDF_FILE>` |
| 10400 | Unknown payment method |
| 10401 | Unsupported or unknown payment currency code |
| 10402 | Items not present although mandatory according to document type |
| 10403 | Payments not present although mandatory according to document type |
| 10404 | Payment currency code not ILS but no exchange rate provided |
| 10405 | Payment amount not sent, or not numeric or empty |
| 10406 | Invalid payment date format or payment date not allowed by regulation |
| 10407 | Unknown payment CC credit term |
| 10408 | Unknown payment CC brand |
| 10500 | Document financial status not found for document type |
| 10501 | Document update failed |
| 10600 | Unknown document id |
| 10601 | `<DOCUMENT_ID_NOT_FOUND_IN_TABLE>` |
| 10602 | `<NO_FILE_FOUND_FOR_DOCUMENT>` |

---

## Payment Fields Requirement by Payment Method

| Payment Method | Relevant Payment Fields |
|----------------|------------------------|
| Credit Card | cc_last_4_digits, credit_term, installments_number, credit_card_brand |
| Bank Transfer | bank, bank_branch, bank_account |
| Cheque | bank, bank_branch, bank_account, cheque_number |
| Cash | |
| PayPal | paypal_account, paypal_transaction_number |
| Other | other_payment_type |

---

## Currencies

| Code | Meaning |
|------|---------|
| ILS | Israeli Shekel |
| USD | US Dollar |
| GBP | British Pounds |
| EUR | Euro |
| CAD | Canadian Dollar |
| CHF | Swiss Franks |
| AUD | Australian Dollar |
| DKK | Danish Krone |
| SEK | Swedish Krona |
| NOK | Norwegian Korne |
| JPY | Japanese Yen |
| JOD | Jordanian Dinar |
| HKD | Hong Kong Dollar |

---

## Payment Methods

| Code | Meaning |
|------|---------|
| 1 | Credit Card |
| 3 | Cheque |
| 4 | Bank Transfer |
| 5 | Cash |
| 6 | PayPal |
| 10 | Other |

---

## Country Codes

Valid country ISO 3166 two-letter codes (e.g. `IL` for Israel). Full list: https://www.iban.com/country-codes

---

## Document Actions

| Code | Meaning |
|------|---------|
| 1 | Debit |
| 3 | Credit |

---

## Document Types

| Code | Meaning |
|------|---------|
| IR | Tax invoice / receipt |
| RE | Receipt |
| DI | Deal Invoice |
| IN | Tax invoice |

---

## Amount Types

| Code | Meaning |
|------|---------|
| N | Net – VAT added to amounts |
| G | Gross – VAT extracted from amounts |

---

## Unit Types

| Code | Meaning |
|------|---------|
| 1 | Unit |
| 2 | Gram |
| 3 | Kilogram |
| 4 | Metric Ton |
| 5 | Day |
| 6 | Week |
| 7 | Month |
| 8 | Year |
| 9 | Centimeter |
| 10 | Meter |
| 11 | Kilometer |
| 12 | MB |
| 13 | GB |
| 14 | TB |

---

## Item Types

| Code | Meaning |
|------|---------|
| I | Item |
| S | Shipping and handling |
| C | Coupon |

---

## Languages

| Code | Meaning |
|------|---------|
| heb | Hebrew |
| eng | English |

---

## Credit Card Brands

| Code | Meaning |
|------|---------|
| 1 | Mastercard |
| 2 | Visa |
| 3 | Diners |
| 4 | American Express |
| 5 | Isracard |
| 6 | JCB |
| 7 | Discover |
| 8 | Maestro |

---

## Credit Card Credit Terms

| Code | Meaning |
|------|---------|
| 1 | Regular |
| 6 | Credit plan (isracredit/visa adif) |
| 8 | Payments |

---

## Document Relation Types

| Code | Meaning |
|------|---------|
| 1 | Cancelling document number – for whole document cancellation |
| 2 | Cancelling document id – for whole document cancellation |

---

## Document Transaction Index

| Parameter | Type | Description |
|-----------|------|-------------|
| txnindex | Integer | Allows adding a transaction index to the document; used to correlate with transactions in the Reports API |
| canceldoc | String | Indicates whether the document is a credit document for a cancelled transaction. If `Y`, the created document's number is reflected in the report response under the `txnfdnumber` parameter |

---

## Invoices API

> Full OpenAPI spec: `docs/tranzila-invoices-api.yaml`
>
> Base URL: `https://billing5.tranzila.com`
> PDF retrieval URL: `https://my.tranzila.com`

### Authentication Headers

All requests to the Invoices API require these headers:

| Header | Description |
|--------|-------------|
| `X-tranzila-api-app-key` | App key |
| `X-tranzila-api-request-time` | Current timestamp |
| `X-tranzila-api-nonce` | Unique nonce per request |
| `X-tranzila-api-access-token` | Access token |
| `Content-Type` | `application/json` |

> **Note:** All responses return HTTP 200. Check `status_code` field — `0` = success, anything else = error.

---

### POST `/api/documents_db/create_document`

Creates a financial document (invoice/receipt). Returns JSON with document details on success, JSON with error code on failure.

#### Document-level params

| Field | Type | Mandatory | Default |
|-------|------|-----------|---------|
| `terminal_name` | String | **Yes** | — |
| `document_date` | Date `yyyy-mm-dd` | No | Current date |
| `document_type` | String | No | `IR` |
| `document_currency_code` | String | No | `ILS` |
| `vat_percent` | Decimal | No | Terminal setting or BOI rate |
| `action` | Integer | No | `1` |
| `client_company` | String | No | — |
| `client_name` | String | No | — |
| `client_id` | String (numeric) | No | — |
| `client_email` | String | No | — |
| `client_address_line_1` | String | No | — |
| `client_address_line_2` | String | No | — |
| `client_city` | String | No | — |
| `client_country_code` | String | No | `IL` |
| `client_zip` | String (numeric) | No | — |
| `client_receipt_paid_for` | String | No | RE documents only |
| `document_language` | String | No | `heb` |
| `response_language` | String | No | `eng` |
| `related_document_number` | Integer | No | — |
| `relation_type` | Integer | If `related_document_number` set | — |
| `created_by_user` | String | No | Not shown on invoice |
| `created_by_system` | String | No | — |
| `items` | Array | See notes | Required for IR, DI, IN |
| `payments` | Array | See notes | Required for IR, RE |

#### Item params

| Field | Type | Mandatory | Default |
|-------|------|-----------|---------|
| `name` | String | **Yes** | — |
| `unit_price` | Decimal | **Yes** | — |
| `code` | String | No | — |
| `type` | String | No | `I` |
| `units_number` | Decimal | No | `1` |
| `unit_type` | Integer | No | `1` |
| `price_type` | String | No | `G` |
| `currency_code` | String | No | `ILS` |
| `to_doc_currency_exchange_rate` | Decimal | If non-ILS currency | — |

#### Payment params

| Field | Type | Mandatory | Default |
|-------|------|-----------|---------|
| `payment_method` | Integer | **Yes** | — |
| `amount` | Decimal | **Yes** | — |
| `payment_date` | Date `yyyy-mm-dd` | No | Current date |
| `currency_code` | String | No | `ILS` |
| `to_doc_currency_exchange_rate` | Decimal | If non-ILS currency | — |
| `cc_last_4_digits` | String (max 4) | method=1 | — |
| `cc_credit_term` | Integer | method=1 | `1` |
| `cc_installments_number` | Integer (min 2) | method=1, term=8 | — |
| `cc_brand` | Integer | method=1 | — |
| `bank` | String | method=3,4 | — |
| `bank_branch` | String | method=3,4 | — |
| `bank_account` | String | method=3,4 | — |
| `cheque_number` | String | method=3 | — |
| `paypal_account` | String | method=6 | — |
| `paypal_transaction_number` | String | method=6 | — |
| `other_description` | String | method=10 | — |
| `txnindex` | Integer | No | Links to Reports API transaction |
| `canceldoc` | String (`Y`/`N`) | No | — |

#### Success response

```json
{
  "status_code": 0,
  "status_msg": "הצלחה",
  "enquiry_key": "14451a5f",
  "document": {
    "id": "6995",
    "number": "1000010",
    "assignment_number": null,
    "total_charge_amount": 11.9,
    "currency": "ILS",
    "created_at": "2025-04-06 14:12:09",
    "retrieval_key": "<base64_key>"
  }
}
```

#### Minimal example (cash payment)

```json
{
  "terminal_name": "terminalname",
  "document_date": "2021-01-01",
  "document_type": "IR",
  "document_language": "heb",
  "document_currency_code": "ILS",
  "items": [
    { "name": "Lesson", "unit_price": 100.0, "price_type": "G" }
  ],
  "payments": [
    { "payment_method": 5, "amount": 100.0 }
  ]
}
```

---

### POST `/api/documents_db/get_document`

Retrieves a document by ID. Returns PDF binary on success, JSON error on failure.

| Field | Type | Mandatory |
|-------|------|-----------|
| `terminal_name` | String | **Yes** |
| `document_id` | String | **Yes** |
| `response_language` | String | No (default `eng`) |

Common errors: `10600` (unknown id), `10601` (not in DB), `10602` (no PDF file).

---

### GET `/api/get_financial_document/{retrieval_key}`

**Web-only** — displays document PDF in browser for end customers.

- URL: `https://my.tranzila.com/api/get_financial_document/{retrieval_key}`
- Success: `application/pdf`
- Error: HTML page (404)

The `retrieval_key` comes from `create_document` or `get_documents` responses.

---

### POST `/api/documents_db/get_documents`

Lists documents by terminal(s) with optional filters. At least one of `transaction_ids`, `start_date`+`end_date`, or `client_email` is required alongside `terminal_names`.

| Field | Type | Mandatory |
|-------|------|-----------|
| `terminal_names` | Array of strings | **Yes** |
| `transaction_ids` | Array of integers | See note |
| `start_date` | Date `yyyy-mm-dd` | See note |
| `end_date` | Date `yyyy-mm-dd` | See note |
| `client_email` | String | See note |

Response includes array of documents each with: `id`, `number`, `assignment_number`, `type`, `action`, `total_charge_amount`, `currency`, `created_at`, `retrieval_key`.

---

## Payment Request API

> Full OpenAPI spec: `docs/tranzila-payment-request-api.yaml`
>
> Base URL: `https://api.tranzila.com`

Creates a payment request link that can be sent to a customer via email, SMS, or both. Uses the same authentication headers as the Invoices API (see above).

### POST `/v1/pr/create`

#### Top-level params

| Field | Type | Mandatory | Default / Notes |
|-------|------|-----------|-----------------|
| `terminal_name` | String | **Yes** | Pattern: `^[a-z][a-z0-9]{2,15}$` |
| `created_by_user` | String | **Yes** | |
| `created_by_system` | String | **Yes** | |
| `created_via` | String | **Yes** | e.g. `"TRAPI"` |
| `request_date` | Date `yyyy-mm-dd` | **Yes** | `null` = current date |
| `request_language` | String | **Yes** | `hebrew` / `english` (default `hebrew`) |
| `response_language` | String | **Yes** | `hebrew` / `english` (default `hebrew`) |
| `client` | Object | **Yes** | See below |
| `items` | Array | **Yes** | See below |
| `payment_plans` | Array of integers | **Yes** | `[1]` = regular, `[8]` = installments |
| `payment_methods` | Array of integers | **Yes** | See below |
| `payments_number` | Number | **Yes** | |
| `action_type` | Number | No | `1` = charge, `2` = J5 |
| `request_currency` | String | No | `ILS` (default). Supported: ILS, USD, EUR, GBP, CAD, AUD, SEK, NOK, CHF, DKK, JPY |
| `request_vat` | Number (0–100) | No | Terminal setting if omitted |
| `pr_token` | String (80 chars) | No | |
| `send_email` | Object | No | Required to deliver via email |
| `send_sms` | Object | No | Required to deliver via SMS |

#### `payment_methods` values

| Code | Method |
|------|--------|
| `1` | Credit Card |
| `2` | Bank Transfer via Masav (legacy page, paid module) |
| `14` | Bit (paid module) |
| `15` | Apple Pay (paid module) |

Multiple methods can be combined: `"payment_methods": [1, 14, 15]`. Using a method not enabled for the terminal returns error `20403`.

#### `client` object

| Field | Type | Mandatory | Notes |
|-------|------|-----------|-------|
| `name` | String | **Yes** | |
| `contact_person` | String | **Yes** | |
| `email` | String | **Yes** | If provided, phone fields become optional |
| `external_id` | String | No | |
| `id` | String | No | Pattern: `^[0-9A-Za-z]{5,9}$` |
| `address_line_1` | String | No | |
| `address_line_2` | String | No | |
| `city` | String | No | |
| `country_code` | String | No | ISO 2-letter, uppercase |
| `zip` | String | No | |
| `phone_country_code` | String | For SMS | e.g. `"972"` |
| `phone_area_code` | String | For SMS | e.g. `"054"` |
| `phone_number` | String | For SMS | |

#### `items` array

| Field | Type | Mandatory | Default |
|-------|------|-----------|---------|
| `name` | String | **Yes** | |
| `unit_price` | Number | **Yes** | |
| `units_number` | Number | **Yes** | |
| `id` | Number | No | |
| `code` | String | No | |
| `type` | String | No | `I` (I/S/C) |
| `unit_type` | Number | No | `1` |
| `price_type` | String | No | `G` (G/N) |
| `currency_code` | String | No | `ILS` |
| `to_txn_currency_exchange_rate` | Number | No | `1` |
| `vat_percent` | Number (0–100) | No | `null` |

#### `send_email` object

| Field | Mandatory | Notes |
|-------|-----------|-------|
| `sender_name` | **Yes** | Business name shown in email |
| `sender_email` | **Yes** | From address (use `donotreply@tranzila.com`) |

#### `send_sms` object

| Field | Mandatory |
|-------|-----------|
| `sender_name` | **Yes** |

#### Success response

```json
{
  "error_code": 0,
  "message": "The payment request created successfully.",
  "pr_id": "634",
  "pr_link": "https://pay.tranzila.com/pr/<token>"
}
```

Use `pr_id` to match NOTIFY callbacks after payment.

#### Error response

```json
{
  "error_code": 20002,
  "message": "Authorization failed"
}
```

Common error codes: `20002` = auth failed, `20403` = unsupported payment method.

---

## Main Tranzila API (Transactions & Standing Orders)

> Full OpenAPI spec: `docs/tranzila-main-api.yaml`
>
> Base URL: `https://api.tranzila.com/v1`

Same authentication headers as all other Tranzila APIs (see above).

---

### POST `/transaction/credit_card/create`

Charge a credit card directly. Can also be used with an **express terminal**.

#### Required fields

| Field | Type | Notes |
|-------|------|-------|
| `terminal_name` | String | |
| `card_number` | String (8–19 chars) | |
| `expire_month` | Integer (1–12) | |
| `expire_year` | Integer (4 digits) | |

#### Key optional fields

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `txn_type` | String | `debit` | See transaction types below |
| `txn_currency_code` | String | `ILS` | ILS, USD, EUR, GBP, CAD, AUD, SEK, NOK, CHF, DKK, JPY |
| `cvv` | String (3–4) | null | |
| `card_holder_id` | String (9 chars) | null | Israeli ID, required by some acquirer contracts |
| `payment_plan` | Integer | `1` | `1`=regular, `6`=credit installments, `8`=regular installments |
| `installments_number` | Integer (1–36) | null | For payment_plan 6 or 8 |
| `first_installment_amount` | Number | null | |
| `other_installments_amount` | Number | null | |
| `reference_txn_id` | Integer | null | Required for credit/cancel/reversal/force/sto |
| `authorization_number` | String | null | Required for credit/cancel/reversal |
| `items` | Array | — | See items schema below |
| `client` | Object | — | See client fields below |
| `user_defined_fields` | Array `[{name, value}]` | null | Used for DCdisable and custom fields |
| `remarks` | String | null | Transaction-level remark |
| `response_language` | String | `english` | `english` / `hebrew` |
| `created_by_user` | String | null | |
| `created_by_system` | String | null | |

#### Transaction types (`txn_type`)

| Value | Description |
|-------|-------------|
| `debit` | Charge card (default) |
| `credit` | Refund/credit. Requires `reference_txn_id` + `authorization_number` |
| `verify` | Card check. Pairs with `verfiy_mode` (2=j2 check, 5=j5 hold, 6=init STO) |
| `force` | Actual charge after j5 verify. Requires `reference_txn_id` |
| `cancel` | Cancel a debit or credit. Requires `reference_txn_id` + `authorization_number` |
| `reversal` | Release j5 credit limit hold. Requires `reference_txn_id` + `authorization_number` |
| `sto` | Standing order charge. Requires `reference_txn_id` from a verify/mode-6 txn |

#### `items` array fields

| Field | Type | Mandatory | Default |
|-------|------|-----------|---------|
| `name` | String | **Yes** | |
| `unit_price` | Number | **Yes** | |
| `units_number` | Number | **Yes** | |
| `code` | String | No | null |
| `type` | String | No | `I` (I=Item, S=Shipping, C=Coupon) |
| `unit_type` | Integer (1–16) | No | `1` (1=Unit…14=TB, 15=Hour, 16=Liter) |
| `price_type` | String | No | `G` (G=Gross/incl. VAT, N=Net/excl. VAT) |
| `currency_code` | String | No | `ILS` |
| `to_txn_currency_exchange_rate` | Number | No | null |
| `discount_type` | String | No | `fixed` (fixed / percent) |
| `discount` | Number | No | null |
| `vat_percent` | Number | No | null (uses BOI rate) |
| `attributes` | Array `[{language, name, value}]` | No | |

#### `client` object fields

| Field | Type | Notes |
|-------|------|-------|
| `name` | String | |
| `contact_person` | String | |
| `id` | String | Israeli ID |
| `email` | String | |
| `external_id` | String | |
| `phone_country_code` | String | e.g. `"972"` |
| `phone_area_code` | String | e.g. `"050"` |
| `phone_number` | String | |
| `address_line_1` | String | |
| `address_line_2` | String | |
| `city` | String | |
| `country_code` | String | ISO 2-letter (e.g. `IL`) |
| `zip` | String | |

#### Success response

```json
{
  "error_code": 0,
  "message": "success",
  "transaction_result": {
    "processor_response_code": "000",
    "transaction_id": 12345,
    "auth_number": "0587923",
    "card_type": 1,
    "card_type_name": "Mastercard",
    "currency_code": "ILS",
    "expiry_month": 12,
    "expiry_year": 25,
    "payment_plan": 1,
    "token": "f1057849hg8495838",
    "last_4": "1234",
    "card_mask": "458021xxxxxx4245",
    "card_locality": "foreign",
    "amount": 10,
    "txn_type": "debit",
    "tranmode": "A",
    "Responsecvv": "1",
    "Responseid": "1"
  }
}
```

`Responsecvv` / `Responseid`: `0`=not entered, `1`=valid, `2`=invalid, `3`=not checked.

#### Application error codes (HTTP 200)

| Code | Description |
|------|-------------|
| 20111 | Provided token check failure |
| 20112 | Original transaction not found / uncreditable origin |
| 21100 | Transaction index mismatch |
| 21101 | Provided index was empty |
| 22100 | Authorization number mismatch |
| 22101 | Empty authorization number |
| 22103 | Invalid DCdisable |
| 23001 | Original auth number invalid / trying to credit cancelled transaction |
| 23002 | Already credited / credit sum exceeds original debit |

#### HTTP error codes

| Code | Error codes | Description |
|------|-------------|-------------|
| 400 | 20001, 20003, 20004 | Invalid/malformed JSON |
| 401 | 20000, 20002 | Authentication/authorization failed |
| 404 | — | Resource not found |

#### DCdisable (duplicate charge prevention)

Pass a unique value per transaction via `user_defined_fields`. Terminal must have field 20 configured as DCdisable in my.tranzila settings. The system checks 24h back at parent terminal level.

```json
"user_defined_fields": [
  { "name": "DCdisable", "value": "your_unique_value_here" }
]
```

---

### POST `/sto/create` *(Deprecated — use V2)*

Create a Standing Order. Requires a prior `verify` (txn_type=verify, verfiy_mode=6) transaction.

Required: `terminal_name`, `sto_payments_number` (2–9999), `charge_frequency` (weekly/monthly/quarterly/half-yearly/yearly), `charge_dom` (1–28), `item`.

Optional: `client`, `card` (token + expiry), `msv` (MASAV bank details), `first_charge_date`, `response_language`, `created_by_user`.

Response: `{ error_code, message, sto_id }`

---

### POST `/sto/update` *(Deprecated — use V2, paid module)*

Update STO status. Required: `terminal_name`, `sto_id`. Optional: `sto_status` (active/inactive), `updated_by_user`.

---

### POST `/stos/get` *(Paid module)*

Retrieve STOs. Required: `terminal_name`. Filter by: `sto_id`, `sto_status`, `client_name`, `client_id`, `client_email`, `token`, `last_4_digits`, `card_holder_id`, `bank_account_number`.

Response: `{ error_code, message, stos: [...] }`
