# Augmont Gold Loan — End-to-End API Test Harness

A single-file async Python harness (`src/maintest.py`) that drives the **Augmont Gold Loan (GL)
backend** through its real REST API, end to end — from customer creation and KYC all the way to
disbursement and packet submission. Use it to validate the full GL journey against a live backend.

## What it does

One run walks the complete loan lifecycle:

```
(create customer + KYC)  ->  appraiser request  ->  loan basics  ->  nominee  ->  ornaments
   ->  scheme / eligibility  ->  final loan details  ->  bank details  ->  assign packet
   ->  ratings (appraiser / BM if > 5L / ops)  ->  loan documents
   ->  partner approval  ->  disbursement  ->  submit packet  ->  load loan details
```

The run ends when the loan reaches stage **"packet submitted"**, and prints a **LOAN DETAILS** table
plus a pass/fail metrics summary (with any failed endpoints listed).

The flow spans several roles; the harness logs into each one as needed (static OTP `1234`):

| Role      | Mobile                    | Used for                                   |
|-----------|---------------------------|--------------------------------------------|
| Appraiser | 8880008881                | Main actor — the whole loan flow           |
| Admin     | 8880008880                | Packet create + assign                     |
| BM        | 8880008883                | Branch-Manager approval (loans over ₹5L)   |
| Ops       | 8880008882                | Ops rating / final approval                |
| Partner   | 8767002003 (Roshan) / 9375876473 (Arvog) | Partner approval + disbursement — the number depends on `--partner` |

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

### Optional flags

| Flag                | Default     | Purpose                                                       |
|---------------------|-------------|---------------------------------------------------------------|
| `--customer ID`     | *(none)*    | Do a new loan against an existing customer. Omit → create new. |
| `--partner NAME`    | `roshan`    | Lending partner (one per loan): `roshan` or `arvog`.          |
| `--scheme ID`       | *(auto)*    | Pin a specific scheme id (e.g. `853`).                        |
| `--co-lender [NAME\|ID]` | off    | Co-lending: pass a bank, or pass it bare to pick from a menu. |
| `--amount N`        | `400000`    | Requested loan amount in rupees (capped at eligibility).      |
| `--login ROLE`      | `appraiser` | Login role for the main flow.                                 |

**`--customer <UNIQUE_ID>`** — run a loan against a customer that already exists and is KYC-approved
(KYC is skipped). The id is the customer's unique id, not the numeric database id.
```bash
python src/maintest.py --customer MS35QNJP
```

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
| `GOLD_LOAN_BASE_URL`                  | API base URL (default: the gfat staging host)              |
| `GOLD_LOAN_AUTH_TOKEN`                | Supply a JWT to skip login (must contain `internalBranchId`, `id`) |
| `GOLD_LOAN_AMOUNT`                    | Requested loan amount (also settable via `--amount`)       |
| `GOLD_LOAN_EXISTING_CUSTOMER_UNIQUE_ID` | Existing-customer unique id (also settable via `--customer`) |

## Project structure

```
.
├── README.md                     This file.
├── requirements.txt              Python dependencies.
├── package.json                  The single Node dependency (crypto-js).
├── .gitignore
├── src/
│   └── maintest.py               The harness — one class, GoldLoanApiTest, with a CLI (main()).
├── assets/                       Files uploaded during the flow (resolved relative to the project root).
│   ├── AADHAR.png                Real aadhaar image (required for the KYC identity-proof upload).
│   ├── PAN.png                   PAN card image.
│   ├── scale.jpg                 Ornament image (gold on a scale).
│   ├── cancelled-cheque-1.png    Cancelled cheque (bank-details proof).
│   ├── CPV.pdf                   PDF used for document uploads (loan docs, income, CPV).
│   └── dummy_image.png           Generic image placeholder for uploads.
├── reference/
│   └── gold-loan-e2e-flow.har    Consolidated HAR of the complete flow (API-only) — the ground-truth
│                                 reference for request/response shapes. Each entry's `comment` tags
│                                 its stage: A = customer/KYC/loan/bank/packet/docs,
│                                 B0 = partner approval, B = disbursement, C = submit-packet/load-details.
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
- For the deep "why", the hard-won domain rules, and per-endpoint gotchas, read
  `.claude/skills/gold-loan-api-test/SKILL.md`.
