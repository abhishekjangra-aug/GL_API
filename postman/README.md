# Postman collection — Augmont Gold Loan E2E (TEST / UAT)

A runnable port of [`src/maintest.py`](../src/maintest.py). One pass of the Collection Runner walks
the **entire** loan journey and ends with the loan at stage 13, *"packet submitted"*:

```
logins (5 roles) -> master data -> customer + KYC -> appraiser request -> loan basics -> nominee
  -> ornaments -> scheme/eligibility -> final loan details -> bank details -> packet create/assign/seal
  -> BM rating (only above 5L) -> loan documents -> ops rating -> partner approval -> disbursement
  -> submit packet -> load loan details
```

116 requests, 17 folders. Everything dynamic — ids, tokens, amounts, HMAC signatures, encrypted
identity proofs, the scheme retry loop — is computed in scripts. **You only pick an environment and
press Run.**

## Files

| File | What it is |
|------|------------|
| `Augmont-GoldLoan-E2E.postman_collection.json` | The collection |
| `Augmont-GL-TEST.postman_environment.json` | TEST — `gfat` host + its logins |
| `Augmont-GL-UAT.postman_environment.json` | UAT — `gfau` host + its logins |

All three are **generated** by [`tools/build_postman_collection.py`](../tools/build_postman_collection.py)
from the harness itself. Do not hand-edit them — edit the generator (and
[`tools/postman_lib.js`](../tools/postman_lib.js) for the shared JavaScript) and re-run:

```bash
python tools/build_postman_collection.py
```

The environments are built from `GoldLoanApiTest.ENVIRONMENTS`, so hosts and every role / partner /
partner-branch-user mobile stay in sync with the Python harness automatically.

## Setup (once)

1. **Import** the collection and both environment files into Postman.
2. **Set the working directory to the repository root** — *Settings -> General -> Working
   directory* -> select the folder containing `assets/`. File uploads use relative paths like
   `assets/AADHAR.png`; without this every upload fails.
3. Select **Augmont GL - TEST** or **Augmont GL - UAT** in the environment dropdown.

Nothing else to configure — the environment supplies the base URL and all five role logins.

## Running

Use the **Collection Runner** (or newman) on the whole collection, in order. Sending requests
one-by-one will not work: the flow chains ids through collection variables and uses
`setNextRequest` for the new/existing-customer branch, the scheme retry loop, and the >5L BM step —
and `setNextRequest` is a runner-only feature.

```bash
newman run postman/Augmont-GoldLoan-E2E.postman_collection.json \
       -e postman/Augmont-GL-UAT.postman_environment.json \
       --working-dir . --timeout-request 60000
```

Watch the Postman console: every step logs a `[GL]` line with the ids it captured (customer, loan,
scheme, rpg, eligibility, packet, loanUniqueId).

## Knobs (collection variables — edit under the collection's *Variables* tab)

| Variable | Default | Meaning |
|----------|---------|---------|
| `flow_mode` | `new` | `new` = create a customer and run full KYC. `existing` = reuse `customer_unique_id`, skip KYC if it is already approved. |
| `customer_unique_id` | *(empty)* | Required when `flow_mode=existing` (e.g. `MS35QNJP`). |
| `loan_amount` | `400000` | Requested amount. Above `500000` the BM-approval step runs automatically. |
| `partner_key` | `152` | `152` = Roshan Partner, `10` = Arvog. Selects the partner login **and** the partner-branch user. |
| `partner_name` | `ROSHAN PARTNER` | Name matched against `partner-scheme-amount`. Change with `partner_key`. |
| `scheme_id_pin` | *(empty)* | Pin one scheme id (e.g. `853`); wins even over the catalog pre-check. |
| `co_lender` | *(empty)* | Co-lender bank name or id (e.g. `DCB` or `4`). Empty = no co-lending. |
| `partner_branch_id_override` | *(empty)* | Force a partner branch id at submit-packet. |

To run Arvog, set `partner_key=10` **and** `partner_name=ARVOG`.

## How the tricky parts are handled

- **HMAC signing** — every request's pre-request script ends in `GL.body(...)` (JSON) or `GL.sign()`
  (GET / multipart), which signs `JSON.stringify({...body, url: path})` with HMAC-SHA256. The body
  is built as a JS object, stringified into `{{__body}}` and signed from that same object, so the
  bytes sent are the bytes signed. JS renders `4533.0` as `4533` natively — the Python harness has
  to emulate this with `_js_number_normalize`. GETs and file uploads sign an empty body, and `%20`
  in the path is signed as a literal space, exactly like the browser.
- **CryptoJS encryption** — `GL.enc()` reproduces the harness's Node call (HMAC-derived passphrase,
  ECB, PKCS7) and yields OpenSSL `U2FsdGVkX1...` ciphertext. Used for the identity-proof number and
  for the identity-proof **file paths** in `customerKycPersonal` and `loan-documents`.
- **Role switching** — all five roles log in up front (each number is rate-limited, so once per
  run) and every request selects its token with `GL.role('admin' | 'ops' | 'bm' | 'partner' | ...)`.
- **Scheme selection** — the catalog and `partner-scheme-amount` are flattened partner -> `schemes[]`,
  filtered (active / secured / amount / karat) and ordered; `SCH: check loan type` then loops onto
  itself over the candidates until one is accepted **and** its karat range covers every ornament.
- **Eligibility** — ornaments use one karat, are weight-sized to clear the loan, and their
  `netWtAfterPurity` mirrors the server's own recompute (`netWeight × min(purity, ltv%) / 100`).
  `final-loan-details` then sends `Σ(nwap × scheme rpg)`.
- **Environment-specific ids** — the submit-packet collection point is resolved live: the packet
  location by matching the name *"partner branch in"*, the partner branch from the partner record.
  Nothing environment-specific is hardcoded beyond the environment file.

## Known differences from `src/maintest.py`

Deliberate, and none of them change what the backend receives in a passing run:

- **One ornament image upload, reused for all four ornaments** (the harness uploads the same file
  four times). Likewise one cheque upload is reused for both passbook-proof slots.
- **Packet images** are uploaded via `/api/upload-file/base` using a base64 copy of
  `assets/dummy_image.png` embedded in the collection, so that step needs no working directory.
- **Rounding** uses JS half-up (`Math.round`). The Python harness now matches this for
  `netWtAfterPurity` (via a half-up `Decimal` round), so the two agree even on exact `.005` ties —
  which matters because the server rounds `nwap` half-up and validates eligibility against it.
- **No retry/recovery branches.** The harness recovers from "appraiser request already exists" by
  cancelling the prior loan; the collection tolerates the 400 and reuses the existing request, but
  does not cancel. If a customer already has an in-flight loan, finish or cancel it first.
- **Customer lookups use `viewAllCustomer=true`** where the harness tries the user-scoped list
  first and widens only if it comes back empty. Both match on an exact id, so the result is the
  same — the collection just skips the retry request.
- **No `loan_calc_debug.json` dump.** On an eligibility 400 the test script logs the sent total,
  the rpg and the three things to check, instead of writing a file.
