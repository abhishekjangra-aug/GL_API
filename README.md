# Augmont Gold Loan — End-to-End API Test Harness

An async Python harness (library: `gold_loan/harness.py`; runnable scenarios: `flows/`) that drives the **Augmont Gold Loan (GL)
backend** through its real REST API, end to end — from customer creation and KYC all the way to
disbursement and packet submission. Use it to validate the full GL journey against a live backend.

## What it does

One run walks the complete loan lifecycle:

```
generate packet (admin)  ->  (create customer + KYC)  ->  appraiser request  ->  loan basics
   ->  nominee  ->  ornaments  ->  scheme / eligibility  ->  final loan details  ->  bank details
   ->  appraiser rating  ->  seal packet  ->  BM rating (when the server asks)
   ->  loan documents (+ LIP assessment / income documents when required)
   ->  manual bank verification (ops)  ->  ops rating
   ->  partner approval  ->  disbursement (NEFT / IMPS / RTGS)  ->  submit packet  ->  load loan details
```

The run ends when the loan reaches stage **"packet submitted"**, and prints a **LOAN DETAILS** table
plus a pass/fail metrics summary (with any failed endpoints listed).

Every request mirrors what the **current web portal** sends: values the portal pre-fills, looks up or
computes from server data are taken from the server responses, not hard-coded — see
[How the harness mirrors the portal](#how-the-harness-mirrors-the-portal).

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
| Partner branch user (Arvog)  | 7777777788 | 8888888880 | Receives the packet at submit-packet |

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
python flows/e2e_full_loan.py

# Do a new loan against an EXISTING customer (by its customer unique id)
python flows/e2e_full_loan.py --customer MS35QNJP
```

Either mode runs on either environment:

```bash
python flows/e2e_full_loan.py --env uat                     # new customer, on UAT
python flows/e2e_full_loan.py --env uat --customer MS35QNJP # existing customer, on UAT
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
| `--purpose-type T`  | `cb`        | Loan purpose type: `cb` (Consumption Based), `igl` (Income Generating) or `any`. |
| `--purpose NAME`    | *(random)*  | Pin one purpose by name, e.g. `"Agricultural Purposes"` (uses its own type). |
| `--kyc MODE`        | `manual`    | Identity mode: `manual`, `auto` or `ovd` — see below.         |
| `--kyc-profile PATH`| `config/kyc_identity.json` | Real identity data for `auto`/`ovd`.           |
| `--login ROLE`      | `appraiser` | Login role for the main flow.                                 |
| `--transfer-mode M` | `neft`      | Disbursement transfer mode: `neft`, `imps` or `rtgs` (manual, with a UTR). |
| `--release-kyc-docs`| off         | TEST/UAT: free the profile's real PAN (+ Aadhaar) before KYC — see below. |
| `--fresh-request`   | off         | Cancel an existing appraiser request's loan and start a new request. |
| `--count N`         | `1`         | Create N loans back to back in one logged-in session.          |

**`--customer <UNIQUE_ID>`** — run a loan against a customer that already exists and is KYC-approved
(KYC is skipped). The id is the customer's unique id, not the numeric database id.
```bash
python flows/e2e_full_loan.py --customer MS35QNJP
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
python flows/e2e_full_loan.py --kyc auto
python flows/e2e_full_loan.py --kyc ovd
```

**Aadhaar in `auto` mode:** the offline e-KYC XML only carries the Aadhaar's **last 4 digits**, so each
new customer gets a fresh Aadhaar made of **8 random digits + the real last 4** (re-drawn until the
Verhoeff check digit is valid), with the person's real name. The same XML can therefore KYC several
customers. The profile's `aadhaarNumber` may be the full number or just its last 4 digits. **PAN has no
such workaround** — it stays the real one, so once it is registered on a customer a new `auto` run stops
with "PAN Card already exists!". On TEST/UAT add **`--release-kyc-docs`** to free it first (see
[Releasing real KYC documents](#releasing-real-kyc-documents-testuat)); that also allows
`--kyc auto --count N`.

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
and a Form 97 PDF. The `ovdType` strings come from the live portal's map (`dl -> "DL"`,
`voter -> "VoterID"`); `GOLD_LOAN_KYC_OVD_TYPE_STRING` still overrides them. Only **Voter ID** is
capture-verified end to end. Passport and NREGA have no captured endpoint, so they are rejected
rather than guessed.

**`--partner <roshan|arvog>`** — which lending partner underwrites the loan. **A loan is underwritten
by exactly one partner** — you pick one, not both:
- `roshan` (**default**) — Roshan Partner (`ROS-152`). The proven partner; every verified end-to-end
  run uses it.
- `arvog` — Arvog (`ARV-10`).

The harness auto-picks an eligible scheme within the chosen partner (by karat/amount range).
```bash
python flows/e2e_full_loan.py --partner arvog
```

**`--scheme <SCHEME_ID>`** — pin one specific scheme instead of auto-picking. The scheme can be any
scheme under the selected partner (Roshan's are `853` / `861` / `882`). If the id isn't found under
that partner, the harness warns and falls back to the auto-picked scheme.
```bash
python flows/e2e_full_loan.py --partner roshan --scheme 853
```

**`--co-lender [NAME|ID]`** — apply **co-lending**: the loan is split between the partner and a
co-lender bank. Co-lending happens **only** with this flag — neither the scheme's own co-lender nor the
server's default co-lender is applied otherwise. (The portal pre-selects a server default; declining it
counts as an override there, so the harness then sends `coLenderOverrideFlag: true`, a reason —
`GOLD_LOAN_CO_LENDER_OVERRIDE_REASON` — and the default as `systemAssignedBankId`, just like the portal.)
Three ways to use it:
- **Omit the flag** → no co-lending (default).
- **`--co-lender` with no value** → the harness prints a **menu of the available co-lender banks**
  (fetched live) right after login and asks you to pick one — so you don't have to remember any
  names or ids.
- **`--co-lender <NAME|ID>`** → use that bank directly (name substring like `DCB`/`Godrej`, or its id).

```bash
python flows/e2e_full_loan.py --co-lender                     # choose from a menu
python flows/e2e_full_loan.py --co-lender DCB                 # by name
python flows/e2e_full_loan.py --co-lender 4                   # by id
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

**`--amount <N>`** — the loan amount to request, in rupees. The loan-type check returns the confirmed
amount, which is what the rest of the flow uses. Large loans (around ₹5,00,000 and above) usually need
a BM rating; the **server** decides — the harness submits one when the loan reaches the BM stage and
skips it when the server has already moved on. Handy for exercising specific branches:
```bash
python flows/e2e_full_loan.py --amount 300000            # small loan, no BM rating
python flows/e2e_full_loan.py --amount 550000            # over ₹5L → BM approval step usually runs
python flows/e2e_full_loan.py --customer MS35QNJP --amount 250000
```

**`--purpose-type cb|igl|any` / `--purpose <NAME>`** — the loan purpose on the basic-details page.
`/api/purpose` lists purposes grouped by type (**CB** = Consumption Based, **IGL** = Income
Generating, e.g. Agricultural Purposes); the harness picks a random active purpose of the chosen type
(default `cb`) and sends that purpose's own `purposeType`. For `igl` it uses **Agro** whenever the
environment offers it (`GOLD_LOAN_IGL_PURPOSE` changes the preferred name). `--purpose` pins one by name (case-insensitive)
and fails with the available list if the environment has no such purpose — the lists differ between
TEST and UAT.
```bash
python flows/e2e_full_loan.py --purpose-type igl                    # random Income Generating purpose
python flows/e2e_full_loan.py --purpose "Agricultural Purposes"     # that exact purpose (IGL on TEST)
```

**`--transfer-mode neft|imps|rtgs`** — how the disbursement is paid out (default `neft`). These are the
portal's manual **OFFLINE** options: the harness sends the mode lowercased with a generated UTR, as the
portal does when a user picks NEFT / IMPS / RTGS. ICICI / RazorpayX payouts are never used.
```bash
python flows/e2e_full_loan.py --transfer-mode imps
python flows/e2e_full_loan.py --transfer-mode rtgs
```

**`--login <ROLE>`** — which role logs in to drive the main flow (default `appraiser`). The harness
still switches to admin / BM / ops / partner automatically for the steps that require them (packet
assign, BM/ops ratings, partner approval + disbursement), so you rarely need to change this.
```bash
python flows/e2e_full_loan.py --login appraiser
```

Flags combine freely. A couple of complete examples:
```bash
# Fresh new customer, small loan
python flows/e2e_full_loan.py --amount 300000

# New loan for an existing customer, large enough to trigger BM approval
python flows/e2e_full_loan.py --customer MS35QNJP --amount 550000
```

Every run logs in fresh (there is no session cache to reuse or clear).

Run `python flows/e2e_full_loan.py --help` for the auto-generated usage.

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
| `GOLD_LOAN_HTTP_TIMEOUT`              | Seconds to wait for a server reply (default `60`; httpx's own default of 5 s was too short) |
| `GOLD_LOAN_TRANSFER_MODE`             | Disbursement transfer mode `neft` / `imps` / `rtgs` (also settable via `--transfer-mode`) |
| `GOLD_LOAN_RELEASE_KYC_DOCS`          | `true` = free the profile's real PAN (+ Aadhaar) before KYC, TEST/UAT only (also `--release-kyc-docs`) |
| `GOLD_LOAN_STAMP_DUTY`                | Stamp duty used **only** when the server states none (default `0`, as the portal) |
| `GOLD_LOAN_CO_LENDER_OVERRIDE_REASON` | Reason sent when a server-default co-lender is overridden / declined |
| `GOLD_LOAN_GATEWAY_RETRIES`           | Retries for 502/503/504 and lost replies on GETs and file uploads (default `3`) |
| `GOLD_LOAN_PURPOSE_TYPE`              | `cb` / `igl` / `any` (also settable via `--purpose-type`)  |
| `GOLD_LOAN_IGL_PURPOSE`               | Purpose used for IGL loans when available (default `Agro`) |
| `GOLD_LOAN_PURPOSE`                   | Pin one loan purpose by name (also settable via `--purpose`) |
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
| `GOLD_LOAN_KYC_AADHAAR`               | real Aadhaar, 12 digits or just the last 4 (`auto`; only the last 4 are used) |
| `GOLD_LOAN_KYC_AADHAAR_XML`           | UIDAI offline e-KYC XML file (`auto`)              |
| `GOLD_LOAN_KYC_OVD_TYPE`              | `voterId` or `drivingLicense` (`ovd`)              |
| `GOLD_LOAN_KYC_OVD_NUMBER`            | EPIC / DL number (`ovd`)                           |
| `GOLD_LOAN_KYC_OVD_IMAGE`             | OVD scan (`ovd`)                                   |
| `GOLD_LOAN_KYC_FORM97`                | Form 97 PDF (`ovd`)                                |
| `GOLD_LOAN_KYC_OVD_TYPE_STRING`       | Override the submitted `ovdType` string            |
| `GOLD_LOAN_KYC_IDENTITY_TYPE_ID`      | Override `identityTypeId` (default: looked up from `/api/identity-type`, "Aadhaar Card" = 5) |

Paths may be relative to the **project root** or absolute; relative ones are anchored at the repo
root, so a run works from any working directory.

## KYC on its own

`flows/kyc_only.py` runs the **complete KYC journey and nothing else** — useful for seeding customers,
exercising a verification mode, or re-KYC'ing an existing customer without the loan process.

```bash
python flows/kyc_only.py                      # manual KYC, one new customer
python flows/kyc_only.py --kyc auto           # real PAN + real Aadhaar XML
python flows/kyc_only.py --kyc ovd            # real OVD + Form 97, no PAN
python flows/kyc_only.py --count 5            # seed 5 KYC'd customers (manual only)
python flows/kyc_only.py --customer MS35QNJP  # Re-KYC an existing customer
python flows/kyc_only.py --skip-ops-approval  # submit but leave the KYC pending
```

It covers customer creation (admin) → basic info + PAN/OVD verification → consent OTP → master data
→ address/identity → personal details → `submit-all-kyc-info` → ops approval, and finishes by
printing the command to create a loan against each customer it produced:

```
■ NEXT: create a loan against these customers
  python flows/e2e_full_loan.py --customer MSCUST01 --env test
```

`--count > 1` is allowed in `manual` mode, and in `auto` mode together with `--release-kyc-docs`
(TEST/UAT; the real PAN is freed before each customer). It is refused for `ovd`: the identity profile
holds one real person's OVD, and a document can be registered against only one customer.

## Packets on their own

`flows/create_packets.py` creates empty packets as admin and assigns each to the appraiser —
the packet half of the flow, without a loan.

```bash
python flows/create_packets.py                # 5 packets, assigned to the appraiser
python flows/create_packets.py --count 20     # 20 packets
python flows/create_packets.py --no-assign    # create only, leave unassigned
```

## Upload documents: LIP assessment and income documents

The upload-documents step mirrors the web portal, driven by the loan record (`single-loan`):

| Rule | Source of truth | What the harness sends |
|---|---|---|
| **LIP (Liquid Income Program) assessment** required | `isLIPFormRequired` on the loan — the server applies `IGLLIPApplicability` (IGL, TEST: any amount ≥ ₹1) and `consumptionLipApplicability` (CB, TEST: ₹20,00,000) from global settings | `lipForm`, field-for-field as the portal's form |
| Prefilled LIP fields (read-only on screen) | loan record + login | loan account no., customer name, occupation category, end use (purpose), assessment date, **Existing Loan EMIs** (`presentInterest`), new loan requested, total existing outstanding, maker (logged-in user), checker (BM, when BM approval was manual) |
| Self-declared LIP fields | harness | years in occupation, the income field the occupation category makes mandatory (Salary / Self Employed / Farmer / Others), household expenses (+ business expenses for Self Employed) |
| LIP eligibility (`LIPEligibilityValidation`) | portal rule | net monthly income > monthly interest obligation (`currentLoanPresentInterest + presentInterest`); income is declared to clear it |
| Saved assessment on a resumed loan | `customerLoanDocument.lipFormInfo` | saved values are reused (re-declared if no longer eligible) |
| After upload | server-stored `lipFormInfo` | read back; the step fails unless the server marks it eligible and stores the same Existing Loan EMIs |
| **IGL income documents** | purpose's `incomeGeneratingDocumentTypes` minus `hiddenDocuments` | one PDF under a randomly chosen visible document (at least one is mandatory) |
| Consumption (CB) loans | `showConsumptionSupportingDocs` on the loan | none on TEST (the LIP form replaced them); one consumption supporting document only when the server flags it (UAT's older portal) |
| **Arvog** (co-lender not DCB) | `isArvog`, `masterLoan.bankId` | loan application, gold receipt and KFS copies |
| Stamp paper | `stampDutyDefinitionType == "stampPaper"` | stamp paper copy |

A rejected upload now stops the run (it used to be logged and ignored).

## Releasing real KYC documents (TEST/UAT)

A real PAN / Aadhaar can be registered on only one customer. On TEST and UAT it can be freed again with
`POST /api/e-kyc/remove` (only the `Origin` header is needed). The server's answer says what was
actually freed (`panCardUpdated` / `identityProofNumberUpdated`; `false` = that number was not
registered). Any other environment is refused.

```bash
python flows/kyc_release_documents.py --env test --from-profile      # the identity profile's PAN (+ full Aadhaar)
python flows/kyc_release_documents.py --env uat --pan ABCDE1234F
python flows/e2e_full_loan.py --kyc auto --release-kyc-docs          # free the profile's PAN, then KYC + loan
python flows/kyc_only.py --kyc auto --count 3 --release-kyc-docs     # 3 auto-KYC customers from one profile
```

This detaches the document from whichever customer holds it — use it on test data only.

## How the harness mirrors the portal

The request bodies were audited against the **live portal code** (screen by screen) and the captured
traffic. Values the portal pre-fills, looks up or computes are taken from the server, as the portal does:

| Area | What the harness does |
|---|---|
| Customer creation | Portal's lead OTP: `customer-otp/send-otp` `{mobileNumber, firstName, lastName, type:'lead'}` + `verify-otp-admin` (the old `send-register-otp` answers 403 on TEST). State/city = the logged-in user's (login `userDetails`), status 1, lead source from `GET /api/lead/lead-user`. |
| Packets | Every run first **generates its own packet** (admin create + assign to the appraiser); ids look like `pac12345678` (no dash). |
| KYC | Identity type looked up from `/api/identity-type`; PAN / Aadhaar verdicts read from `data.isPanVerified` / `data.isAahaarVerified`, and a verified PAN name replaces the customer name; an Aadhaar-type address proof is sent encrypted; risk category, geo-address and city-edit flag from the server; ops approval built from the `submit-all-kyc-info` response. The CCE rating is automatic on TEST (rating stage 2 → ops); the harness stops clearly if the server ever asks for a CCE rating. Occupations that need industry details get a random industry category + industry. |
| Appraiser request | The customer's branch (`internalBranch.id`); for an appraiser login the appraiser id is the login itself. |
| Basic details / nominee | Branch, appraiser, outstanding, active loans, KYC personal values and signature from `customer-loan-details`; nominee `createdBy` / branch from the basic-details response, an existing nominee reused, guardian fields `null` for an adult nominee. |
| Ornaments | `currentLtvAmount` = gold rate × global `ltvGoldValue` % (per gram); `netWtAfterPurity` = net weight × LTV %; LTV options from the karat's `range`; `totalEligibleAmt` 0; latitude/longitude sent. |
| Scheme / eligibility | Loan amount, secured / top-up split, manual charges and billing cycle from `check-loan-type`; eligibility = server's total net weight × scheme rate per gram. |
| Bank details | Balances, cash limits, charges and **stamp duty from `final-loan-details`** (the server's figure always wins; `0` when none is stated, like the portal); to-be-paid = final amount − (balance transfer + stamp duty + processing charge + upfront interest); bank name from the IFSC lookup; account-holder / customer name from the penny-drop result. |
| Ratings | Ops and partner ratings copy the appraiser / BM decisions from the loan record (`single-loan → masterLoan`). Open loan defects (`checkPointers` — problems the server recorded on the loan, which block approval in the portal) are printed as a warning. |
| Loan lock | Locked at the loan's **current** stage. |
| Disbursement | `actualProcessingCharge` = processing charge; `finalAmount` = net amount paid out, `fullAmount` = gross with charges; payment gateway id from the partner's payout config; partner id from the partner login; penal interest from `single-loan`; NEFT / IMPS / RTGS with a UTR. |
| Upload documents | See [Upload documents](#upload-documents-lip-assessment-and-income-documents). |
| Network | 60-second reply timeout (`GOLD_LOAN_HTTP_TIMEOUT`); file uploads are retried after a lost reply, other writes are not (they might already have been applied). |

**Rate limits:** the backend limits logins — one OTP per number per minute, and an hourly cap
(HTTP 429 "Come back after an hour"). A run logs in once per role and reuses the tokens, but many runs
back to back can still hit the cap; wait it out (the portal itself keeps working).

## Tests

`tests/test_offline_harness.py` is an offline regression suite — **no network, no extra dependencies**
(there is no pytest in this project). Request bodies are byte-compared against the captured HARs in
`reference/`, and the control flow is checked with mocks.

```bash
python tests/test_offline_harness.py            # everything
python tests/test_offline_harness.py kyc        # just the KYC groups
python tests/test_offline_harness.py duplicate  # one group
```

A pass means "the requests we build match the ones the browser sent, and the control flow behaves" —
**not** "the server accepted it". Node.js is required (the CryptoJS tests shell out to it).

## Project structure

```
.
├── README.md                     This file.
├── requirements.txt              Python dependencies.
├── package.json                  The single Node dependency (crypto-js).
├── .gitignore
├── gold_loan/                    The library (import it; it is not a test).
│   ├── __init__.py
│   └── harness.py                GoldLoanApiTest — every API step, signing, encryption — plus the
│                                 full-loan CLI (main()) that the flows reuse.
├── flows/                        Runnable live scenarios, one per file. Naming: e2e_<scenario>.py
│   │                             for loan journeys, <area>_<action>.py for focused runs.
│   ├── e2e_full_loan.py          Complete loan: customer/KYC -> ... -> disbursement -> submit packet.
│   ├── e2e_stop_before_documents.py     Loan up to (not including) Upload Documents.
│   ├── e2e_stop_before_disbursement.py  Loan up to and including Ops Rating.
│   ├── resume_loan.py            Finish an in-flight loan from the stage it is at.
│   ├── kyc_only.py               KYC only (all three identity modes, no loan process).
│   ├── kyc_release_documents.py  TEST/UAT: free a PAN / Aadhaar so it can be registered again.
│   └── create_packets.py         Bulk packet creation + appraiser assignment (admin).
├── tests/                        OFFLINE regression tests only (no network). Naming: test_<what>.py
│   └── test_offline_harness.py   Request bodies vs captured traffic + control-flow checks.
├── config/                       REAL identity data for --kyc auto/ovd. GIT-IGNORED except the
│   ├── kyc_identity.example.json   template — copy it to kyc_identity.json and fill it in.
│   ├── kyc_identity.json         Your real PAN / Aadhaar / OVD numbers (never committed).
│   ├── offline_aadhar.xml        UIDAI offline e-KYC XML (--kyc auto).
│   └── form-97.pdf               Form 97 upload (--kyc ovd).
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
- **What is live-verified:** complete loans on TEST with **new customers** (Roshan Partner, no
  co-lending), with CB and IGL (Agricultural Purposes) purposes, the LIP assessment, and NEFT / IMPS /
  RTGS disbursement. `--partner arvog` and `--co-lender` are wired to the portal's rules but have not
  been run end to end recently — the backend may reject some scheme / co-lender combinations at
  `final-loan-details`; the console then prints the exact request and server message.
- Eligibility, processing charges, and "to-be-paid" amounts are computed from the scheme the server
  returns — not hardcoded — so runs adapt to whatever scheme/loan is selected.
- `gold-loan-e2e-flow.har` is a **step reference**, not a single live session: it's merged from
  captures with different customer/loan ids. Use it to check the shape of any call, not to replay.
- **KYC caveat:** `manual` is live-verified; `ovd` (Voter ID) is verified against captures. Not yet
  confirmed live: the loan steps **after** KYC on the OVD path (where `panCardNumber` is null), and the
  `auto` Aadhaar rule (random 8 + real last 4) — the real PAN was already registered; run it with
  `--release-kyc-docs`.
- **Deliberately not modelled** (renewal / top-up or webcam-only paths): the "nothing to pay" dummy bank
  details, geo-tagged webcam uploads, the auto-mode address taken from the Aadhaar XML, and the
  date-of-birth / gender prefills from `e-kyc/data`.
- For the deep "why", the hard-won domain rules, and per-endpoint gotchas, read
  `.claude/skills/gold-loan-api-test/SKILL.md`.

## Flag reference (all runners)

Every command-line flag in the project, in one place. Run any script with `--help` for the same list.

### `flows/e2e_full_loan.py` — full end-to-end loan

The same flags work unchanged on `flows/e2e_stop_before_documents.py` (stops before upload documents)
and `flows/e2e_stop_before_disbursement.py` (stops after ops rating), which reuse this CLI.

| Flag | Values | Default | What it does | Env var equivalent |
|---|---|---|---|---|
| `--env` | `test` \| `uat` | `test` | Target environment: `test` = gfat, `uat` = gfau. Picks the API host, `Origin`, and every role/partner login. | `GOLD_LOAN_ENV` |
| `--customer` | customer unique id | *(none — create new)* | New loan for an EXISTING customer (KYC skipped if already approved). Omit to create a new customer and run KYC. | `GOLD_LOAN_EXISTING_CUSTOMER_UNIQUE_ID` |
| `--partner` | `roshan` \| `arvog` | `roshan` | Lending partner (exactly one per loan): Roshan Partner (ROS-152) or Arvog (ARV-10). | `GOLD_LOAN_PARTNER_NAME` |
| `--scheme` | scheme id | *(auto-pick)* | Pin one scheme id (e.g. `853`) instead of auto-picking within the partner. | `GOLD_LOAN_SCHEME_ID` |
| `--co-lender` | bank name/id, or no value | off | Co-lending with a named bank (`DCB`, `4`); bare `--co-lender` shows a menu to pick from. | `GOLD_LOAN_CO_LENDER` |
| `--kyc` | `manual` \| `auto` \| `ovd` | `manual` | Identity mode: generated dummy PAN/Aadhaar, real PAN + offline-Aadhaar XML, or real OVD (Voter ID / DL) + Form 97. | `GOLD_LOAN_KYC_MODE` |
| `--kyc-profile` | path | `config/kyc_identity.json` | Real identity data for `--kyc auto` / `ovd`. | `GOLD_LOAN_KYC_PROFILE` |
| `--login` | role | `appraiser` | Role that logs in to drive the main flow. | — |
| `--amount` | rupees | `400000` | Requested loan amount, capped at eligibility. At or above ₹5,00,000 adds the BM-rating step (the server decides). | `GOLD_LOAN_AMOUNT` |
| `--purpose-type` | `cb` \| `igl` \| `any` | `cb` | Loan purpose type: Consumption Based, Income Generating, or either. A random active purpose of that type is chosen. | `GOLD_LOAN_PURPOSE_TYPE` |
| `--purpose` | purpose name | *(random)* | Pin one purpose by name (e.g. `"Agricultural Purposes"`); its own type is used. Fails with the available list if the environment has no such purpose. | `GOLD_LOAN_PURPOSE` |
| `--transfer-mode` | `neft` \| `imps` \| `rtgs` | `neft` | Disbursement bank transfer mode: the portal's manual OFFLINE options, with a UTR (ICICI / RazorpayX payouts are not used). | `GOLD_LOAN_TRANSFER_MODE` |
| `--release-kyc-docs` | *(switch)* | off | TEST/UAT only: before KYC, free the identity profile's real PAN (and full Aadhaar) via `POST /api/e-kyc/remove`, so `--kyc auto` can register them again; allows `--kyc auto --count N`. | `GOLD_LOAN_RELEASE_KYC_DOCS=true` |
| `--fresh-request` | *(switch)* | off | If the customer already has an appraiser request, cancel its loan and create a new request (default: resume it). | `GOLD_LOAN_FRESH_APPRAISER_REQUEST=true` |
| `--count` | N | `1` | Create N loans back to back in one logged-in session. A failed loan does not stop the batch. | — |
| `-h`, `--help` | | | Show the help text and exit. | |

**Runner-specific behaviour (no extra flags):**

| Script | Stops | Appraiser request |
|---|---|---|
| `flows/e2e_stop_before_documents.py` | before Upload Documents (after BM rating) | Always creates a NEW one; never cancels or resumes. If the customer already has one, tries another branch, else stops with a clear error. |
| `flows/e2e_stop_before_disbursement.py` | after Ops Rating (before partner approval / disbursement) | Same as `flows/e2e_full_loan.py` (resume by default; `--fresh-request` to recreate). |

### `flows/kyc_only.py` — KYC only

| Flag | Values | Default | What it does |
|---|---|---|---|
| `--env` | `test` \| `uat` | `test` | Target environment. |
| `--kyc` | `manual` \| `auto` \| `ovd` | `manual` | Identity mode (as in `flows/e2e_full_loan.py`). |
| `--kyc-profile` | path | `config/kyc_identity.json` | Real identity data for `auto` / `ovd`. |
| `--kyc-type` | string | `RE_KYC` | `kycType` sent on the v2 endpoints (the captured OVD flow used `NEW_KYC`). |
| `--customer` | customer unique id | *(none — create new)* | Run KYC on an EXISTING customer (Re-KYC). |
| `--count`, `-n` | N | `1` | How many new customers to create and KYC (`manual`; `auto` only with `--release-kyc-docs`). |
| `--skip-ops-approval` | *(switch)* | off | Submit the KYC but leave it pending (no ops approval). |
| `--pan-type` | `pan` \| `form60` | `pan` | PAN document type for manual/auto mode. |
| `--release-kyc-docs` | *(switch)* | off | TEST/UAT only: free the profile's real PAN (and full Aadhaar) before each KYC. |
| `--login` | role | `appraiser` | Role that runs the KYC. |

### `flows/create_packets.py` — packets only

| Flag | Values | Default | What it does |
|---|---|---|---|
| `--env` | `test` \| `uat` | `test` | Target environment. |
| `--count`, `-n` | N | `5` | How many packets to create. |
| `--no-assign` | *(switch)* | off | Create the packets but leave them unassigned. |
| `--appraiser-id` | user id | *(logged-in appraiser)* | Assign to this appraiser user id instead. |
| `--login` | role | `appraiser` | Role logged in to source the user id and branch. |
| `--branch` | branch id | *(from the login JWT)* | Override `internalUserBranch`. |
| `--delay` | seconds | `0.0` | Wait between packets (useful for large batches). |
| `--listing-size` | N | `50` | Rows fetched from `/api/packet` when resolving a new packet's id. |

### `flows/resume_loan.py` — finish an in-flight loan

| Flag | Values | Default | What it does |
|---|---|---|---|
| `--loan` | `AUGM-XXXXX` | — | Loan unique id (as on the loan-details screen). One of `--loan` / `--customer` / `--loan-id` is required. |
| `--customer` | customer unique id | — | Resume the loan on this customer's appraiser request. |
| `--loan-id` | id | — | Numeric customer loan id; pair with `--master-loan-id`. |
| `--master-loan-id` | id | — | Master loan id (only alongside `--loan-id`). |
| `--from` | `ornaments`, `scheme`, `bank`, `appraiser-rating`, `packet`, `bm-rating`, `documents`, `ops-bank`, `ops-rating`, `disburse`, `submit-packet` | *(derived from the loan)* | Force the resume point. |
| `--env` | `test` \| `uat` | `test` | Target environment. |
| `--login` | role | `appraiser` | Login role for the main flow. |
| `--partner` | `roshan` \| `arvog` | `roshan` | Partner whose login disburses the loan. |
| `--dry-run` | *(switch)* | off | Resolve the loan and print the plan without changing anything. |
| `--list-steps` | *(switch)* | off | List the resumable step keys and exit. |

### Other scripts

| Script | Arguments | What it does |
|---|---|---|
| `flows/kyc_release_documents.py` | `--env`, `--pan`, `--aadhaar`, `--from-profile`, `--kyc-profile` | TEST/UAT only: free a PAN / Aadhaar from the KYC records (`POST /api/e-kyc/remove`; only the `Origin` header is needed). The answer says what was actually freed. |
| `tests/test_offline_harness.py` | optional group name prefix (e.g. `purpose`, `kyc-body`, `server-values`) | Offline regression tests; no argument runs every group. |
