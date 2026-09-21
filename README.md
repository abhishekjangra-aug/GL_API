# Augmont Gold Loan — End-to-End API Test Harness

A single-file async Python harness (`src/maintest.py`) that drives the **Augmont Gold Loan (GL)
backend** through its real REST API, end to end — from customer creation and KYC all the way to
disbursement and packet submission. Use it to validate the full GL journey against a live backend.

## What it does

One run walks the complete loan lifecycle:

```
(create customer + KYC)  ->  appraiser request  ->  loan basics  ->  nominee  ->  ornaments
   ->  scheme / eligibility  ->  final loan details  ->  bank details  ->  assign packet
   ->  ratings (appraiser / BM if > 5L)  ->  loan documents
   ->  manual bank verification (ops)  ->  ops rating
   ->  partner approval  ->  disbursement  ->  submit packet  ->  load loan details
```

The run ends when the loan reaches stage **"packet submitted"**, and prints a **LOAN DETAILS** table
plus a pass/fail metrics summary (with any failed endpoints listed).

### Environments

The harness runs against either environment, selected with `--env` (default `test`):

| `--env` | Host                                           |
|---------|------------------------------------------------|
| `test`  | `https://gold-loan-backend-api.gfat.augmont.com` |
| `uat`   | `https://gold-loan-backend-api.gfau.augmont.com` |

The environment picks the base URL **and** every login mobile below — the two environments have
separate users. `GOLD_LOAN_ENV` does the same thing as `--env`; `GOLD_LOAN_BASE_URL` still overrides
the host outright.

The flow spans several roles; the harness logs into each one as needed (static OTP `1234`):

| Role      | Mobile (test)  | Mobile (uat)   | Used for                                 |
|-----------|----------------|----------------|-------------------------------------------|
| Appraiser | 8880008881     | 9990009991     | Main actor — the whole loan flow          |
| Admin     | 8880008880     | 9990009990     | Packet create + assign                    |
| BM        | 8880008883     | 9990009993     | Branch-Manager approval (loans over ₹5L)  |
| Ops       | 8880008882     | 9990009995     | Ops rating / final approval               |
| Partner (Roshan) | 8767002003 | 8767002002  | Partner approval + disbursement — depends on `--partner` |
| Partner (Arvog)  | 9375876473 | 8846651348  | Partner approval + disbursement — depends on `--partner` |
| Partner branch user (Roshan) | 8652849318 | 8652849318 | Receives the packet at submit-packet |
| Partner branch user (Arvog)  | 8899999999 | 8888888880 | Receives the packet at submit-packet |

`GOLD_LOAN_PARTNER_USER_MOBILE` overrides the branch user if one of these ever changes. Everything
else about the partner handover (packet location, partner branch id) is resolved from the API at
runtime, so it needs no per-environment config.

## Prerequisites

- **Python 3.9+**
- **Node.js** — the backend expects CryptoJS-AES encryption for identity-proof fields; the harness
  produces it by calling `node` with the `crypto-js` package. This is the one non-Python dependency.

## Setup

```bash
# Python dependencies
pip install -r requirements.txt

# Node dependency (crypto-js) — required for identity-proof encryption
npm install
```

## Usage

Two modes, selected by whether you pass `--customer`:

```bash
# Create a BRAND-NEW customer and run the whole flow (default, no flags)
python src/maintest.py

# Do a new loan against an EXISTING customer (by its customer unique id)
python src/maintest.py --customer MS35QNJP
```

Either mode runs on either environment:

```bash
python src/maintest.py --env uat                     # new customer, on UAT
python src/maintest.py --env uat --customer MS35QNJP # existing customer, on UAT
```

### Optional flags

| Flag                | Default     | Purpose                                                       |
|---------------------|-------------|---------------------------------------------------------------|
| `--env test\|uat`   | `test`      | Target environment — sets the host and all role logins.        |
| `--customer ID`     | *(none)*    | Do a new loan against an existing customer. Omit → create new. |
| `--partner NAME`    | `roshan`    | Lending partner (one per loan): `roshan` or `arvog`.          |
| `--scheme ID`       | *(auto)*    | Pin a specific scheme id (e.g. `853`).                        |
| `--co-lender [NAME\|ID]` | off    | Co-lending: pass a bank, or pass it bare to pick from a menu. |
| `--amount N`        | `400000`    | Requested loan amount in rupees (capped at eligibility).      |
| `--kyc MODE`        | `manual`    | Identity mode: `manual`, `auto` or `ovd` — see below.         |
| `--kyc-profile PATH`| `config/kyc_identity.json` | Real identity data for `auto`/`ovd`.           |
| `--login ROLE`      | `appraiser` | Login role for the main flow.                                 |

**`--customer <UNIQUE_ID>`** — run a loan against a customer that already exists and is KYC-approved
(KYC is skipped). The id is the customer's unique id, not the numeric database id.
```bash
python src/maintest.py --customer MS35QNJP
```
The lookup searches the logged-in user's customers first and then *all* customers, so a customer
created by another user (the harness creates them as admin) is still found. If it reports
**"was not found"**, the id either doesn't exist on that host or belongs to the other environment —
check `--env`.

**`--kyc <manual|auto|ovd>`** — how the customer's identity is established.

| Mode | Identity used | Verification calls | Needs real data |
|------|---------------|--------------------|-----------------|
| `manual` (**default**) | generated dummy PAN + Aadhaar | none — relies on the server's manual-validation fallback | no |
| `auto` | **real** PAN + **real** offline-Aadhaar XML | `POST /api/e-kyc/verify-pan`, `POST /api/e-kyc/offline-aadhaar-xml` | yes |
| `ovd`  | **real** Voter ID or Driving Licence, **no PAN**, plus Form 97 | `POST /api/e-kyc/v2/verify-voter-id`, `POST /api/e-kyc/v2/verify-dl` | yes |

```bash
python src/maintest.py --kyc auto
python src/maintest.py --kyc ovd
```

`auto` and `ovd` need genuine documents — the verification APIs check them against the issuing
authority, so numbers cannot be generated. Supply them in **`config/kyc_identity.json`** (copy
`config/kyc_identity.example.json`) or via `GOLD_LOAN_KYC_*` env vars. `config/` is gitignored
because it holds real PII; only the example file is tracked.

Two behaviours worth knowing:

- **Fallback.** The server counts consecutive verification failures and returns `switchToManual:true`
  on the third. The harness honours that signal, falls back to the manual data path so the run still
  reaches disbursement, and reports `requested: auto → effective: manual (DEGRADED)`. A degraded run
  can never be mistaken for a verified one.
- **Duplicates stop the run.** If any KYC document (PAN, Aadhaar, DL, Voter ID) is already registered
  against another customer, the run **aborts immediately** with `KycDocumentAlreadyExistsError`
  rather than degrading — continuing would report a pass for a document that can never be used.
  Use an unregistered document, or point the run at the customer that already owns it via
  `--customer`.

`ovd` mode is the CR-2 "PAN not available" path: `panType: "ovd"`, `panCardNumber: null`, an OVD scan
and a Form 97 PDF. Only **Voter ID** is fully capture-verified; the `ovdType` *string* for Driving
Licence is inferred and can be overridden with `GOLD_LOAN_KYC_OVD_TYPE_STRING`. Passport and NREGA
appear in CR-2 but have no captured endpoint, so they are rejected rather than guessed.

**`--partner <roshan|arvog>`** — which lending partner underwrites the loan. **A loan is underwritten
by exactly one partner** — you pick one, not both:
- `roshan` (**default**) — Roshan Partner (`ROS-152`). The proven partner; every verified end-to-end
  run uses it.
- `arvog` — Arvog (`ARV-10`).

The harness auto-picks an eligible scheme within the chosen partner (by karat/amount range).
```bash
python src/maintest.py --partner arvog
```

**`--scheme <SCHEME_ID>`** — pin one specific scheme instead of auto-picking. The scheme can be any
scheme under the selected partner (Roshan's are `853` / `861` / `882`). If the id isn't found under
that partner, the harness warns and falls back to the auto-picked scheme.
```bash
python src/maintest.py --partner roshan --scheme 853
```

**`--co-lender [NAME|ID]`** — apply **co-lending**: the loan is split between the partner and a
co-lender bank. Three ways to use it:
- **Omit the flag** → no co-lending (default).
- **`--co-lender` with no value** → the harness prints a **menu of the available co-lender banks**
  (fetched live) right after login and asks you to pick one — so you don't have to remember any
  names or ids.
- **`--co-lender <NAME|ID>`** → use that bank directly (name substring like `DCB`/`Godrej`, or its id).

```bash
python src/maintest.py --co-lender                     # choose from a menu
python src/maintest.py --co-lender DCB                 # by name
python src/maintest.py --co-lender 4                   # by id
```
Example of the menu shown for the bare `--co-lender`:
```
Select a co-lender bank (co-lending):
   0) No co-lending
   1) colender added by ops  (id 5, disbursement 50%, interest 2%)
   2) DCB bank               (id 4, disbursement 80%, interest 0.94%)
   3) Godrej Bank            (id 2, ...)
Enter choice number [0 = no co-lending]:
```

**`--amount <N>`** — the loan amount to request, in rupees. It's automatically capped at the
computed eligibility, and it decides whether BM approval is required (loans over ₹5,00,000 add a BM
rating step). Handy for exercising specific branches:
```bash
python src/maintest.py --amount 300000            # small loan, no BM rating
python src/maintest.py --amount 550000            # over ₹5L → BM approval step runs
python src/maintest.py --customer MS35QNJP --amount 250000
```

**`--login <ROLE>`** — which role logs in to drive the main flow (default `appraiser`). The harness
still switches to admin / BM / ops / partner automatically for the steps that require them (packet
assign, BM/ops ratings, partner approval + disbursement), so you rarely need to change this.
```bash
python src/maintest.py --login appraiser
```

Flags combine freely. A couple of complete examples:
```bash
# Fresh new customer, small loan
python src/maintest.py --amount 300000

# New loan for an existing customer, large enough to trigger BM approval
python src/maintest.py --customer MS35QNJP --amount 550000
```

Every run logs in fresh (there is no session cache to reuse or clear).

Run `python src/maintest.py --help` for the auto-generated usage.

### Configuration via environment variables

All optional; sensible defaults are baked in. Common ones:

| Variable                              | Purpose                                                    |
|---------------------------------------|------------------------------------------------------------|
| `GOLD_LOAN_ENV`                       | Target environment, `test` or `uat` (also settable via `--env`) |
| `GOLD_LOAN_BASE_URL`                  | API base URL — overrides the environment's host            |
| `GOLD_LOAN_ORIGIN`                    | `Origin` header — overrides the environment's web-app origin (`ap.gfat` / `ap.gfau`) |
| `GOLD_LOAN_PARTNER_USER_MOBILE`       | Partner-branch user for submit-packet (overrides the environment's) |
| `GOLD_LOAN_AUTH_TOKEN`                | Supply a JWT to skip login (must contain `internalBranchId`, `id`) |
| `GOLD_LOAN_AMOUNT`                    | Requested loan amount (also settable via `--amount`)       |
| `GOLD_LOAN_EXISTING_CUSTOMER_UNIQUE_ID` | Existing-customer unique id (also settable via `--customer`) |
| `GOLD_LOAN_KYC_MODE`                  | `manual` / `auto` / `ovd` (also settable via `--kyc`)       |
| `GOLD_LOAN_KYC_TYPE`                  | `kycType` on the v2 endpoints (default `RE_KYC`)           |
| `GOLD_LOAN_KYC_PROFILE`               | Path to the real-identity JSON (default `config/kyc_identity.json`) |

Every field of the identity profile also has an env override, so a run can be driven without the
file at all:

| Variable | Field |
|----------|-------|
| `GOLD_LOAN_KYC_FIRST_NAME` / `GOLD_LOAN_KYC_LAST_NAME` | customer name (must match the documents) |
| `GOLD_LOAN_KYC_DOB`                   | date of birth, `YYYY-MM-DD`                       |
| `GOLD_LOAN_KYC_GENDER`                | `m` / `f` / `o`                                    |
| `GOLD_LOAN_KYC_PAN`                   | real PAN (`auto`)                                  |
| `GOLD_LOAN_KYC_AADHAAR`               | real 12-digit Aadhaar (`auto`)                     |
| `GOLD_LOAN_KYC_AADHAAR_XML`           | UIDAI offline e-KYC XML file (`auto`)              |
| `GOLD_LOAN_KYC_OVD_TYPE`              | `voterId` or `drivingLicense` (`ovd`)              |
| `GOLD_LOAN_KYC_OVD_NUMBER`            | EPIC / DL number (`ovd`)                           |
| `GOLD_LOAN_KYC_OVD_IMAGE`             | OVD scan (`ovd`)                                   |
| `GOLD_LOAN_KYC_FORM97`                | Form 97 PDF (`ovd`)                                |
| `GOLD_LOAN_KYC_OVD_TYPE_STRING`       | Override the submitted `ovdType` string            |
| `GOLD_LOAN_KYC_IDENTITY_TYPE_ID`      | Override `identityTypeId` (default 5 = Aadhaar)    |

Paths may be relative to the **project root** or absolute; relative ones are anchored at the repo
root, so a run works from any working directory.

## KYC on its own

`src/test_kyc.py` runs the **complete KYC journey and nothing else** — useful for seeding customers,
exercising a verification mode, or re-KYC'ing an existing customer without the loan process.

```bash
python src/test_kyc.py                      # manual KYC, one new customer
python src/test_kyc.py --kyc auto           # real PAN + real Aadhaar XML
python src/test_kyc.py --kyc ovd            # real OVD + Form 97, no PAN
python src/test_kyc.py --count 5            # seed 5 KYC'd customers (manual only)
python src/test_kyc.py --customer MS35QNJP  # Re-KYC an existing customer
python src/test_kyc.py --skip-ops-approval  # submit but leave the KYC pending
```

It covers customer creation (admin) → basic info + PAN/OVD verification → consent OTP → master data
→ address/identity → personal details → `submit-all-kyc-info` → ops approval, and finishes by
printing the command to create a loan against each customer it produced:

```
■ NEXT: create a loan against these customers
  python src/maintest.py --customer MSCUST01 --env test
```

`--count > 1` is only allowed in `manual` mode: the identity profile holds one real person's
documents, and a document can be registered against only one customer.

## Packets on their own

`src/test_create_packets.py` creates empty packets as admin and assigns each to the appraiser —
the packet half of the flow, without a loan.

```bash
python src/test_create_packets.py                # 5 packets, assigned to the appraiser
python src/test_create_packets.py --count 20     # 20 packets
python src/test_create_packets.py --no-assign    # create only, leave unassigned
```

## Tests

`tests/test_harness.py` is an offline regression suite — **no network, no extra dependencies**
(there is no pytest in this project). Request bodies are byte-compared against the captured HARs in
`reference/`, and the control flow is checked with mocks.

```bash
python tests/test_harness.py            # everything
python tests/test_harness.py kyc        # just the KYC groups
python tests/test_harness.py duplicate  # one group
```

A pass means "the requests we build match the ones the browser sent, and the control flow behaves" —
**not** "the server accepted it". Node.js is required (the CryptoJS tests shell out to it).

## Running it from Postman instead

The same end-to-end flow is also available as a **Postman collection** (`postman/`) with one
environment per target:

```
Import postman/Augmont-GoldLoan-E2E.postman_collection.json
     + postman/Augmont-GL-TEST.postman_environment.json
     + postman/Augmont-GL-UAT.postman_environment.json
Set Postman's working directory to this repository root (uploads use assets/…)
Pick an environment -> Run the collection
```

It is generated from this harness (`python tools/build_postman_collection.py`), does the same HMAC
signing, CryptoJS encryption, role switching and eligibility maths in scripts, and ends at the same
loan stage. See [postman/README.md](postman/README.md) for the knobs (`flow_mode`, `loan_amount`,
`partner_key`, `scheme_id_pin`, `co_lender`) and the handful of deliberate differences.

## Project structure

```
.
├── README.md                     This file.
├── requirements.txt              Python dependencies.
├── package.json                  The single Node dependency (crypto-js).
├── .gitignore
├── src/
│   ├── maintest.py               The harness — one class, GoldLoanApiTest, with a CLI (main()).
│   ├── test_kyc.py               KYC-only runner (all three identity modes, no loan process).
│   └── test_create_packets.py    Bulk packet creation + appraiser assignment (admin).
├── tests/
│   └── test_harness.py           Offline regression suite (no network, no extra dependencies).
├── config/                       REAL identity data for --kyc auto/ovd. GIT-IGNORED except the
│   ├── kyc_identity.example.json   template — copy it to kyc_identity.json and fill it in.
│   ├── kyc_identity.json         Your real PAN / Aadhaar / OVD numbers (never committed).
│   ├── offline_aadhar.xml        UIDAI offline e-KYC XML (--kyc auto).
│   └── form-97.pdf               Form 97 upload (--kyc ovd).
├── postman/                      Generated Postman collection + TEST/UAT environments (see its README).
├── tools/
│   ├── build_postman_collection.py  Generates postman/ from this harness. Edit this, not the JSON.
│   └── postman_lib.js               Shared collection JavaScript (signing, encryption, helpers).
├── assets/                       Files uploaded during the flow (resolved relative to the project root).
│   ├── AADHAR.png                Real aadhaar image (required for the KYC identity-proof upload).
│   ├── PAN.png                   PAN card image.
│   ├── scale.jpg                 Ornament image (gold on a scale).
│   ├── cancelled-cheque-1.png    Cancelled cheque (bank-details proof).
│   ├── CPV.pdf                   PDF used for document uploads (loan docs, income, CPV).
│   └── dummy_image.png           Generic image placeholder for uploads.
├── reference/
│   ├── gold-loan-e2e-flow.har    Consolidated HAR of the complete flow (API-only) — the ground-truth
│   │                             reference for request/response shapes. Each entry's `comment` tags
│   │                             its stage: A = customer/KYC/loan/bank/packet/docs,
│   │                             B0 = partner approval, B = disbursement, C = submit-packet/load-details.
│   └── ovd-kyc-flow.har          KYC via an OVD instead of PAN (verify-dl / verify-voter-id,
│                                 panType "ovd", Form 97). Ground truth for --kyc ovd.
└── .claude/
    └── skills/gold-loan-api-test/
        └── SKILL.md              Deep architecture + domain notes (invariants, gotchas, formulas).
```

Asset paths are anchored to the project root (computed from the source file location), so the harness
runs correctly from any working directory.

Generated at runtime, in the project root (git-ignored): `loan_calc_debug.json`, `node_modules/`.

## Notes

- The harness signs requests (HMAC-SHA256) and handles the full multi-role login dance itself.
- **Partner / co-lending caveat:** the fully verified end-to-end path is **Roshan Partner, no
  co-lending** (that's the default). `--partner arvog`, `--scheme`, and `--co-lender` are wired
  correctly and validated in isolation, but the Arvog and co-lending combinations have not been
  confirmed against a captured accepted run — the backend may reject some scheme/co-lender
  combinations at `final-loan-details`. If a run fails there, the console prints the exact request
  and server message; try Roshan / a different scheme.
- Eligibility, processing charges, and "to-be-paid" amounts are computed from the scheme the server
  returns — not hardcoded — so runs adapt to whatever scheme/loan is selected.
- `gold-loan-e2e-flow.har` is a **step reference**, not a single live session: it's merged from
  captures with different customer/loan ids. Use it to check the shape of any call, not to replay.
- **KYC caveat:** `manual` and `ovd` (Voter ID) are byte-verified against captures. Two things are
  *not* capture-confirmed and only a live run will settle them: the `ovdType` string for **Driving
  Licence** (inferred — override with `GOLD_LOAN_KYC_OVD_TYPE_STRING`), and the loan steps
  **after** KYC on the OVD path, where `panCardNumber` is null (`ovd-kyc-flow.har` ends at the
  consent OTP). The Postman collection models the `manual` path only.
- The Postman collection is regenerated from the harness and currently matches it byte-for-byte;
  re-run `python tools/build_postman_collection.py` after any change to a request body or ordering.
- For the deep "why", the hard-won domain rules, and per-endpoint gotchas, read
  `.claude/skills/gold-loan-api-test/SKILL.md`.
