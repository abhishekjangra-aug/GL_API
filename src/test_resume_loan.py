#!/usr/bin/env python3
"""Resume an in-flight gold loan from whatever stage it is at and drive it to completion.

The end-to-end harness (`maintest.py`) always starts a loan from scratch. When a run dies
half-way -- a flaky penny-drop, a 400 at final-loan-details, an interrupted session -- the
loan is left parked at some stage and the only way forward was to start a brand new one.
This runner picks such a loan up and finishes it.

    python src/test_resume_loan.py --loan AUGM-67273
    python src/test_resume_loan.py --master-loan-id 10947 --loan-id 12952
    python src/test_resume_loan.py --customer MS35QNJP
    python src/test_resume_loan.py --loan AUGM-67273 --from documents
    python src/test_resume_loan.py --loan AUGM-67273 --dry-run     # print the plan, change nothing
    python src/test_resume_loan.py --list-steps

Like `test_kyc.py` and `test_create_packets.py`, this WRAPS `GoldLoanApiTest` rather than
subclassing it: every request body, signature and domain rule stays defined once in
`maintest.py` and cannot drift.

How the resume point is chosen
------------------------------
`GET /api/loan-process/single-loan?customerLoanId=<id>` returns the whole loan, including
`masterLoan.loanStageId`. Two signals are combined and the EARLIER of the two wins, so a
prerequisite the stage id claims is done but the record says is missing never gets skipped:

  * the stage id, mapped through STAGE_RESUME;
  * the record's own contents -- no ornaments, no finalLoanAmount, no loanBankDetail, no
    packet, no documents, not disbursed.

`--from KEY` overrides both.
"""

import argparse
import asyncio
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

from maintest import GoldLoanApiTest  # noqa: E402


class Step:
    """One resumable stage of the loan, as an ordered list of GoldLoanApiTest coroutines."""

    def __init__(self, key, label, methods, note=""):
        self.key = key
        self.label = label
        self.methods = methods
        self.note = note

    async def run(self, suite):
        for name in self.methods:
            await getattr(suite, name)()


# The post-creation half of run_e2e_test, in the same order. Everything before this
# (customer, KYC, appraiser request, basic details, nominee) has to exist already -- that is
# what makes a loan resumable at all.
STEPS = [
    Step("ornaments", "Ornaments",
         ["store_ornament_details"],
         "re-sends the ornaments so the in-memory list matches the server's"),
    Step("scheme", "Scheme, interest table & final loan amount",
         ["fetch_loan_balance", "fetch_co_lender_banks", "check_loan_type",
          "get_interest_rate", "generate_interest_table", "get_final_loan_details"]),
    Step("bank", "Bank details (penny-drop, then store)",
         ["fetch_bank_details", "validate_account", "store_bank_details"]),
    Step("appraiser-rating", "Appraiser rating",
         ["add_appraiser_rating"]),
    Step("packet", "Assign & seal packet",
         ["add_packet_images", "update_loan_lock", "fetch_loan_stages"]),
    Step("bm-rating", "BM rating (only if the loan is over 5L)",
         ["submit_bm_rating_if_required"]),
    Step("documents", "Upload loan documents",
         ["store_loan_documents"]),
    Step("ops-bank", "Manual bank verification (ops)",
         ["ops_manual_bank_verification"]),
    Step("ops-rating", "Ops rating (final approval)",
         ["submit_ops_rating", "fetch_loan_stages"]),
    Step("disburse", "Disbursement (partner login)",
         ["disburse_amount", "fetch_loan_stages"]),
    Step("submit-packet", "Submit packet (appraiser) - completes the loan",
         ["submit_packet", "fetch_loan_stages"]),
]

STEP_KEYS = [s.key for s in STEPS]
STEP_INDEX = {s.key: i for i, s in enumerate(STEPS)}

# Loan stages (GET /api/loan-process/get-loan-stages). The stage names the step the loan is
# WAITING on, not the last one it finished -- right after basic-details it reads "applying".
STAGE_NAMES = {
    1: "appraiser rating", 2: "bm rating", 3: "assign packet", 4: "disbursement pending",
    5: "disbursed", 6: "applying", 7: "OPS team rating", 8: "upload documents",
    9: "loan transfer", 10: "check out", 11: "submit packet", 12: "packet in branch",
    13: "packet submitted", 14: "packet branch out", 15: "UTR",
    16: "disbursement initiated", 17: "partner disbursement", 18: "partner approval",
    19: "co-lender approval", 20: "collect packet",
}

STAGE_RESUME = {
    6: "ornaments",          # applying -- refined below by what the record actually holds
    1: "appraiser-rating",
    3: "packet",
    2: "bm-rating",
    8: "documents",
    7: "ops-bank",
    18: "disburse",          # partner approval
    19: "disburse",          # co-lender approval
    4: "disburse",           # disbursement pending
    16: "disburse",          # disbursement initiated
    17: "disburse",          # partner disbursement
    5: "submit-packet",      # disbursed
    10: "submit-packet",     # check out
    11: "submit-packet",
}

# Stages at or past which the loan is finished; nothing is left to run.
DONE_STAGES = {12, 13, 14, 20}

# The scheme step recomputes eligibility from the ornaments held in memory, which a fresh
# process does not have -- so resuming at or before it always re-sends the ornaments first.
SCHEME_INDEX = STEP_INDEX["scheme"]


class ResumeLoanTest:
    def __init__(self, args):
        self.args = args
        self.suite = GoldLoanApiTest()
        self.loan_record = {}
        self.stage_id = None
        self.stage_name = ""
        self.resume_key = None
        self.reason = ""

    # ---------------------------------------------------------------- resolving the loan

    # The listing search itself lives on GoldLoanApiTest (find_loan_row): the main
    # harness needs the same ladder for its closing loan-details read, and one copy
    # means one place to fix when a listing changes.

    @staticmethod
    def _row_ids(row, loan_unique_id):
        """(masterLoanId, customerLoanId) out of a listing row.

        loan-details rows put the MASTER loan id at the top level and the customer loan under
        customerLoan[]; applied-loan-details may name them outright. Prefer the customerLoan
        entry that actually carries our unique id -- a master loan can hold more than one.
        """
        customer_loans = row.get("customerLoan")
        if isinstance(customer_loans, dict):
            customer_loans = [customer_loans]
        customer_loans = [x for x in (customer_loans or []) if isinstance(x, dict)]
        chosen = next((x for x in customer_loans
                       if str(x.get("loanUniqueId") or "") == loan_unique_id), None)
        if chosen is None and customer_loans:
            chosen = customer_loans[0]
        chosen = chosen or {}
        master_loan_id = (row.get("masterLoanId") or chosen.get("masterLoanId")
                          or row.get("id") or "")
        loan_id = (chosen.get("id") or row.get("customerLoanId") or row.get("loanId") or "")
        if not loan_id and not customer_loans and row.get("masterLoanId"):
            # A row that IS the customer loan: its own id is the loan id.
            loan_id = row.get("id") or ""
        return str(master_loan_id or ""), str(loan_id or "")

    async def _resolve_by_unique_id(self, loan_unique_id):
        """AUGM-… -> (masterLoanId, customerLoanId), via the harness's shared listing search."""
        attempts = []
        row = await self.suite.find_loan_row(loan_unique_id, attempts)
        if row is None:
            raise RuntimeError(
                f"No loan found for loanUniqueId={loan_unique_id} on env "
                f"'{self.suite.env_name}' - searched loan-details and applied-loan-details, "
                "filtered and unfiltered, under both the appraiser and the admin scope "
                f"({'; '.join(attempts) or 'every listing came back empty'}). Check the id, or "
                "try --env with the other environment.")

        master_loan_id, loan_id = self._row_ids(row, loan_unique_id)
        if not loan_id:
            raise RuntimeError(
                f"Found {loan_unique_id} but the listing row carries no customer loan id "
                f"(keys: {sorted(row)[:15]}). Pass --loan-id explicitly.")
        return master_loan_id, loan_id

    async def _resolve_by_customer(self, customer_unique_id):
        """Customer unique id -> the ids of the loan hanging off their appraiser request."""
        self.suite.existing_customer_unique_id = customer_unique_id
        await self.suite._use_existing_customer()
        found = await self.suite._fetch_existing_appraiser_request(required=False)
        if not found or not self.suite.master_loan_id:
            raise RuntimeError(
                f"Customer {customer_unique_id} has no appraiser request with a loan on env "
                f"'{self.suite.env_name}'. Start a new loan with maintest.py instead.")
        return str(self.suite.master_loan_id), str(self.suite.loan_id)

    async def resolve_loan(self):
        args = self.args
        if args.loan_id:
            master_loan_id = args.master_loan_id or ""
            loan_id = args.loan_id
        elif args.loan:
            master_loan_id, loan_id = await self._resolve_by_unique_id(args.loan)
            # Keep the id the user gave us: single-loan reports loanUniqueId as null until the
            # assign-packet stage, and the closing fetch_loan_details searches by it.
            self.suite.loan_unique_id = args.loan
        elif args.customer:
            master_loan_id, loan_id = await self._resolve_by_customer(args.customer)
        else:  # argparse enforces this, but fail clearly if it is ever bypassed
            raise RuntimeError("Give one of --loan, --loan-id or --customer.")
        if not loan_id:
            raise RuntimeError("Could not determine the customer loan id (loanId) for that loan.")
        self.suite.loan_id = str(loan_id)
        self.suite.master_loan_id = str(master_loan_id or "")
        print(f"Resolved loan: loanId={self.suite.loan_id}, masterLoanId={self.suite.master_loan_id}")

    # ---------------------------------------------------------------- rehydrating state

    async def rehydrate(self):
        """Fill the suite's in-memory state from the loan the server already holds.

        Only fields the remaining steps actually read are set; anything a step recomputes is
        left alone. Values the record does not carry (KYC artifacts, master data) come from
        the customer record via the harness's own existing-customer helpers.
        """
        payload = await self.suite.fetch_single_loan()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError(f"single-loan returned no data for loanId={self.suite.loan_id}.")
        self.loan_record = data
        master = data.get("masterLoan") if isinstance(data.get("masterLoan"), dict) else {}

        suite = self.suite
        suite.master_loan_id = str(data.get("masterLoanId") or suite.master_loan_id or "")
        suite.customer_id = str(data.get("customerId") or "")
        suite.loan_unique_id = data.get("loanUniqueId") or suite.loan_unique_id
        if data.get("partnerId"):
            suite.partner_id = str(data["partnerId"])
        if data.get("schemeId"):
            suite.scheme_id = str(data["schemeId"])
        # Financials the later steps send back verbatim (toBePaid, loanAmountDigi, ratings).
        for attr, value in (
            ("secured_rpg", data.get("rpg")),
            ("secured_ltv", data.get("ltv")),
            ("interest_rate", data.get("interestRate")),
            ("secured_exposure", data.get("posExposureAgainstScheme")),
            ("final_loan_amount", master.get("finalLoanAmount")),
            ("tenure_months", master.get("tenure")),
            ("secured_processing_charge", master.get("processingCharge")),
            ("upfront_interest_amount", master.get("upfrontInterestAmount")),
            ("loan_start_date", master.get("loanStartDate")),
            ("loan_end_date", master.get("loanEndDate")),
            ("appraiser_request_id", master.get("appraiserRequestId")),
            ("internal_branch_id", master.get("internalBranchId")),
        ):
            if value not in (None, ""):
                setattr(suite, attr, value if not isinstance(value, str) else value)
        for attr in ("final_loan_amount", "secured_processing_charge", "upfront_interest_amount"):
            raw = getattr(suite, attr)
            if raw not in (None, ""):
                setattr(suite, attr, float(raw))
        for attr in ("appraiser_request_id", "internal_branch_id"):
            if getattr(suite, attr):
                setattr(suite, attr, str(getattr(suite, attr)))

        bank = data.get("loanBankDetail") if isinstance(data.get("loanBankDetail"), dict) else {}
        if bank:
            suite.bank_account_number = bank.get("accountNumber") or suite.bank_account_number
            suite.bank_ifsc_code = bank.get("ifscCode") or suite.bank_ifsc_code
            suite.bank_name = bank.get("bankName") or suite.bank_name
            suite.bank_branch_name = bank.get("bankBranchName") or suite.bank_branch_name
            suite.account_holder_name = bank.get("accountHolderName") or suite.account_holder_name
            suite.bank_system_verified = bool(bank.get("isVerified"))
            suite.bank_manually_verified = bool(bank.get("isManuallyVerified"))
            suite.bank_for_ops_approval = bool(bank.get("forOpsApproval"))

        # Customer identity + the KYC artifacts loan-documents reuses (signature, identity
        # proof, PAN image). These come from the customer record, exactly as the
        # existing-customer path in maintest.py does it.
        if suite.customer_id:
            await self.suite.get_customer_by_id()
        await self.suite._prepare_loan_fields_from_existing()

        self.stage_id = master.get("loanStageId")
        stage = master.get("loanStage") if isinstance(master.get("loanStage"), dict) else {}
        self.stage_name = stage.get("name") or STAGE_NAMES.get(self.stage_id, "unknown")
        print(f"Loan is at stage {self.stage_id} ({self.stage_name}); "
              f"customer {suite.customer_unique_id or suite.customer_id}, "
              f"amount {suite.final_loan_amount or 'n/a'}, "
              f"loanUniqueId {suite.loan_unique_id or 'not assigned yet'}.")

    # ---------------------------------------------------------------- picking the step

    def _content_resume_key(self):
        """The earliest step whose output is MISSING from the loan record.

        This is the safety net: the stage id can say "upload documents" while the record has
        no bank detail, and starting at documents would then fail on a missing prerequisite.
        """
        data = self.loan_record
        master = data.get("masterLoan") if isinstance(data.get("masterLoan"), dict) else {}
        if not data.get("loanOrnamentsDetail"):
            return "ornaments", "the loan has no ornaments"
        if not master.get("finalLoanAmount"):
            return "scheme", "the loan has no final amount"
        if not data.get("loanBankDetail"):
            return "bank", "the loan has no bank detail"
        if not (data.get("loanPacketDetails") or master.get("loanPacketDetails")):
            return "packet", "the loan has no packet"
        if not data.get("customerLoanDocument"):
            return "documents", "the loan has no uploaded documents"
        if not (master.get("isLoanDisbursed") or data.get("isDisbursed")):
            return "disburse", "the loan is not disbursed"
        return "submit-packet", "the loan is disbursed but the packet is not submitted"

    def plan(self):
        """Decide where to resume. Returns the list of steps to run (possibly empty)."""
        master = self.loan_record.get("masterLoan") or {}
        if master.get("isLoanCompleted") or self.stage_id in DONE_STAGES:
            self.resume_key = None
            self.reason = (f"stage {self.stage_id} ({self.stage_name}) - the loan is already "
                           "complete")
            return []

        if self.args.start_from:
            self.resume_key = self.args.start_from
            self.reason = f"--from {self.args.start_from}"
            return STEPS[STEP_INDEX[self.resume_key]:]

        content_key, content_why = self._content_resume_key()
        stage_key = STAGE_RESUME.get(self.stage_id)
        if stage_key is None:
            # An unmapped stage (loan transfer, UTR, …) is not part of this flow; trust the record.
            self.resume_key = content_key
            self.reason = (f"stage {self.stage_id} ({self.stage_name}) is not part of the new-loan "
                           f"flow, so the record decides: {content_why}")
        elif STEP_INDEX[content_key] < STEP_INDEX[stage_key]:
            self.resume_key = content_key
            self.reason = (f"stage {self.stage_id} ({self.stage_name}) points at '{stage_key}', but "
                           f"{content_why}, so starting earlier")

        else:
            self.resume_key = stage_key
            self.reason = f"stage {self.stage_id} ({self.stage_name})"

        # The scheme step recomputes eligibility from ornaments held in memory, which this
        # process does not have; re-send them first so the two agree.
        if STEP_INDEX[self.resume_key] <= SCHEME_INDEX and self.resume_key != "ornaments":
            self.resume_key = "ornaments"
            self.reason += " (backed up to 'ornaments': the scheme step needs them in memory)"
        return STEPS[STEP_INDEX[self.resume_key]:]

    def print_plan(self, steps):
        self.suite._log_step("Resume plan")
        print(f"  Loan        : {self.suite.loan_unique_id or '(no AUGM id yet)'} "
              f"(loanId={self.suite.loan_id}, masterLoanId={self.suite.master_loan_id})")
        print(f"  Stage       : {self.stage_id} ({self.stage_name})")
        print(f"  Resuming at : {self.resume_key or '(nothing to do)'}")
        print(f"  Because     : {self.reason}")
        if not steps:
            return
        print("  Steps:")
        for step in steps:
            suffix = f"  # {step.note}" if step.note else ""
            print(f"    - {step.key:<16} {step.label}{suffix}")

    # ---------------------------------------------------------------- driving it

    async def run(self):
        suite = self.suite
        suite._banner("RESUME LOAN", f"env={suite.env_name}, login={self.args.login}")
        try:
            await suite.login(suite._get_mobile_number_for_login_type(self.args.login))
            await self.resolve_loan()
            await self.rehydrate()
            steps = self.plan()
            self.print_plan(steps)

            if self.args.dry_run:
                suite._banner("DRY RUN - nothing was changed",
                              f"would resume at {self.resume_key or 'nothing'}")
                return True
            if not steps:
                suite._banner("RESULT - NOTHING TO DO", self.reason)
                await suite.fetch_loan_details()
                return True

            for step in steps:
                suite._log_step(f"{step.key} - {step.label}")
                await step.run(suite)

            suite._log_step("Load Loan Details")
            await suite.fetch_loan_details()
            suite._banner("RESULT - PASSED",
                          f"loan resumed from '{self.resume_key}' and completed")
            suite._print_run_metrics(passed=True)
            return True

        except httpx.HTTPStatusError as e:
            suite._banner("RESULT - FAILED (HTTP ERROR)",
                          f"resumed at {self.resume_key or 'n/a'}")
            print(f"{e.response.status_code} {e.request.url}\n{e.response.text[:600]}")
            suite._print_run_metrics(passed=False)
            return False
        except Exception as e:
            suite._banner("RESULT - FAILED", f"resumed at {self.resume_key or 'n/a'}")
            print(f"{type(e).__name__}: {e}")
            suite._print_run_metrics(passed=False)
            return False
        finally:
            await suite.client.aclose()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Resume an in-flight gold loan from its current stage and finish it.")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--loan", metavar="AUGM-XXXXX",
                        help="Loan unique id, as shown on the loan-details screen.")
    target.add_argument("--customer", metavar="UNIQUE_ID",
                        help="Customer unique id; resumes the loan on their appraiser request.")
    target.add_argument("--loan-id", metavar="ID",
                        help="Customer loan id (the numeric loanId). Pair with --master-loan-id.")
    parser.add_argument("--master-loan-id", metavar="ID",
                        help="Master loan id; only needed alongside --loan-id.")
    parser.add_argument("--from", dest="start_from", choices=STEP_KEYS,
                        help="Force the resume point instead of deriving it from the loan.")
    parser.add_argument("--env", choices=sorted(GoldLoanApiTest.ENVIRONMENTS), default="test",
                        help="Target environment (default: test).")
    parser.add_argument("--login", default="appraiser",
                        help="Login role for the main flow (default: appraiser).")
    parser.add_argument("--partner", choices=["roshan", "arvog"], default="roshan",
                        help="Partner whose login disburses the loan (default: roshan).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve the loan and print the plan without changing anything.")
    parser.add_argument("--list-steps", action="store_true",
                        help="List the resumable step keys and exit.")
    return parser


def main():
    args = build_parser().parse_args()

    if args.list_steps:
        print("Resumable steps, in order:")
        for step in STEPS:
            print(f"  {step.key:<16} {step.label}")
        return 0

    if not (args.loan or args.customer or args.loan_id):
        build_parser().error("give one of --loan, --customer or --loan-id")

    # The environment picks the base URL and every role's login mobile, so it has to be set
    # before the suite is constructed.
    os.environ["GOLD_LOAN_ENV"] = args.env
    os.environ["GOLD_LOAN_PARTNER_NAME"] = {"roshan": "ROSHAN PARTNER", "arvog": "ARVOG"}[args.partner]

    runner = ResumeLoanTest(args)
    return 0 if asyncio.run(runner.run()) else 1


if __name__ == "__main__":
    sys.exit(main())
