#!/usr/bin/env python3
"""BT (Balance Transfer) loan: take over a gold loan from another lender (LIVE -- hits the real backend).

    python flows/e2e_bt_loan.py                                   # FULL: new customer + KYC -> BT approved
    python flows/e2e_bt_loan.py --stop-before-documents           # stop at "Upload Documents" (stage 5)
    python flows/e2e_bt_loan.py --customer MWK4X6YU --amount 600000
    python flows/e2e_bt_loan.py --bt AUGM-BT-24551                # RESUME a BT from wherever it is parked
    python flows/e2e_bt_loan.py --customer MWK4X6YU --resume      # resume that customer's open BT
    python flows/e2e_bt_loan.py --rch-decision incomplete         # negative path at RCH
    python flows/e2e_bt_loan.py --gold-loan                       # COMPLETE: BT + manual advance + gold loan
    python flows/e2e_bt_loan.py --bt AUGM-BT-77380 --complete-advance   # stage 11 -> 12 only (manual NEFT + UTR)
    python flows/e2e_bt_loan.py --customer 9352 --until documents       # numeric customer id, park at stage 5
    python flows/e2e_bt_loan.py --customer MWK4X6YU --until complete    # same as --gold-loan
    python flows/e2e_bt_loan.py --customer MWK4X6YU --all-steps         # every optional step, to the gold loan

How far to go (--until; the older flags still work):
  documents   stop at stage 5 "Upload Documents"           (= --stop-before-documents)
  approvals   stop at stage 11 "Advance Transfer Initiated" (the default)
  advance     + manual advance transfer -> stage 12         (= --complete-advance)
  complete    + the gold loan on the BT's request           (= --gold-loan)
--all-steps = --until complete + --complete-cpv + --reupload-cpv.

Modes, mirroring the gold-loan flows:
  full (default)            customer -> KYC -> BT request -> existing loan -> ornaments -> scheme ->
                            bank -> documents -> RCH (+ RH / ZH / ZAH / CEO when the server asks for
                            them). Ends at stage 11 "Advance Transfer Initiated".
  --stop-before-documents   same, but parks the BT at stage 5 "Upload Documents".
  --bt / --resume           pick an existing BT up at its current stage and continue (with
                            --stop-before-documents it stops at stage 5 again).

The run deliberately ends at stage 11: what follows is the finance advance-transfer approval that
pays the old lender through the ICICI payout gateway, which this harness never triggers.
Saved login sessions (config/session_tokens.json) are reused; --fresh-login forces new logins.
Request shapes and stage handling: gold_loan/bt_loan.py.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # project root

import httpx  # noqa: E402

from gold_loan.harness import GoldLoanApiTest  # noqa: E402
from gold_loan.cpv import CustomerCpv  # noqa: E402
from gold_loan.bt_loan import (  # noqa: E402
    ADVANCE_COMPLETED_STAGE, APPROVAL_DECISIONS, BtLoanError, BtLoanFlow, DEFAULT_TENURE,
    OFFLINE_TRANSFER_MODES)

# BT partner id -> the GOLD_LOAN_PARTNER_NAME the gold-loan flow matches (GoldLoanApiTest
# ROSHAN_PARTNER_ID / ARVOG_PARTNER_ID).
BT_PARTNER_NAMES = {GoldLoanApiTest.ROSHAN_PARTNER_ID: "ROSHAN PARTNER", GoldLoanApiTest.ARVOG_PARTNER_ID: "ARVOG"}

TENURES = ("30", "60", "90", "120", "150", "180")   # SharedService.loanTenure
UNTIL_CHOICES = ("documents", "approvals", "advance", "complete")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Create (or resume) a BT loan application.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n")[1])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--bt", metavar="AUGM-BT-XXXXX|ID", help="Resume this BT application.")
    target.add_argument("--customer", metavar="UNIQUE_ID|ID",
                        help="Existing customer, by unique id (MWK4X6YU) or numeric customer id (9352) "
                             "(default: create one with full KYC).")
    parser.add_argument("--resume", action="store_true",
                        help="With --customer: continue that customer's newest open BT instead of "
                             "starting a new one (now the default for --customer; kept for old commands).")
    request = parser.add_mutually_exclusive_group()
    request.add_argument("--new-bt", action="store_true",
                         help="With --customer: a NEW BT request only; never continue the customer's existing "
                              "one (the run stops if the server refuses the new request).")
    request.add_argument("--reuse-bt", action="store_true",
                         help="With --customer: continue the customer's existing BT without asking for a new "
                              "request first. (Default: ask for a new request, and continue the existing BT only "
                              "when the server refuses it.)")
    parser.add_argument("--stop-before-documents", action="store_true",
                        help="Stop when the BT reaches 'Upload Documents' (stage 5). Same as --until documents.")
    parser.add_argument("--until", choices=UNTIL_CHOICES,
                        help="How far to take the BT: documents (stage 5), approvals (stage 11, default), "
                             "advance (stage 12, manual transfer), complete (+ gold loan).")
    parser.add_argument("--all-steps", action="store_true",
                        help="Every optional step: --until complete with --complete-cpv and --reupload-cpv.")
    parser.add_argument("--env", choices=sorted(GoldLoanApiTest.ENVIRONMENTS), default="test",
                        help="Target environment (default: test).")
    parser.add_argument("--login", default="appraiser", help="Role that runs the BT (default: appraiser).")
    parser.add_argument("--amount", type=int, default=300000,
                        help="BT amount = the old loan being taken over (default 300000).")
    parser.add_argument("--tenure", choices=TENURES, default=DEFAULT_TENURE,
                        help=f"Loan tenure in days (default {DEFAULT_TENURE}).")
    parser.add_argument("--partner", choices=["roshan", "arvog", "any"], default="roshan",
                        help="Partner for the scheme (default roshan): only that partner is used; any = every partner allowing BT.")
    parser.add_argument("--scheme", metavar="SCHEME_ID", help="Pin the scheme (scheme calculator id).")
    parser.add_argument("--lender", metavar="NAME",
                        help="Existing lender (bankNbfcName); default: one from /api/nbfc.")
    parser.add_argument("--company-type", default="NBFC",
                        help="Existing lender's company type (default NBFC; e.g. Bank, PSU).")
    parser.add_argument("--ornaments", type=int, default=3, help="Number of ornaments (1-20, default 3).")
    parser.add_argument("--approver", choices=["admin", "ops", "bm", "appraiser"], default="admin",
                        help="Login that gives the RCH/RH/ZH/ZAH/CEO approvals (default admin).")
    parser.add_argument("--rch-decision", choices=APPROVAL_DECISIONS, default="approved",
                        help="RCH's decision (default approved); incomplete / rejected stop the run there.")
    parser.add_argument("--complete-cpv", action="store_true",
                        help="Complete the customer's CPV first (placeholder signature, TEST/UAT; "
                             "skipped when a valid CPV exists) -- flows/complete_cpv.py.")
    parser.add_argument("--reupload-cpv", action="store_true",
                        help="Also send a CPV re-upload with the documents (APNT-350).")
    parser.add_argument("--complete-advance", action="store_true",
                        help="After the approvals (stage 11), approve the advance to the existing lender as a "
                             "MANUAL transfer (OFFLINE gateway: NEFT/IMPS/RTGS + UTR, no payout API) -> stage 12 "
                             "'Advance Transfer Completed'.")
    parser.add_argument("--transfer-mode", choices=list(OFFLINE_TRANSFER_MODES), default="NEFT",
                        help="Manual transfer mode for --complete-advance (default NEFT).")
    parser.add_argument("--utr", help="UTR to record for --complete-advance (default: a random 12-digit test UTR).")
    parser.add_argument("--gold-loan", action="store_true",
                        help="The COMPLETE BT: --complete-advance, then the gold loan on the BT's own appraiser "
                             "request (Appraiser Requests -> Apply Loan): ornaments, scheme, bank, packet, "
                             "documents, ops, disbursement, submit packet -- the flows/e2e_full_loan.py journey "
                             "with the BT's partner. The loan repays the BT advance.")
    parser.add_argument("--gl-amount", type=int, metavar="RUPEES",
                        help="Gold-loan amount for --gold-loan (default: the BT amount). The disbursement nets off "
                             "stamp duty + processing, which the customer then owes on the advance (toBePaid).")
    parser.add_argument("--fresh-login", action="store_true",
                        help="Log every role in again instead of reusing saved sessions.")
    return parser


def validate(args, fail):
    if args.all_steps:
        if args.until not in (None, "complete"):
            fail("--all-steps runs to the end; drop --until " + args.until)
        args.until = "complete"
        args.complete_cpv = args.reupload_cpv = True
    if args.until:
        if (args.stop_before_documents and args.until != "documents") or \
                (args.gold_loan and args.until != "complete") or \
                (args.complete_advance and args.until not in ("advance", "complete")):
            fail(f"--until {args.until} conflicts with the other stop flags given")
        args.stop_before_documents = args.until == "documents"
        args.complete_advance = args.until in ("advance", "complete")
        args.gold_loan = args.until == "complete"
    if (args.new_bt or args.reuse_bt) and (args.resume or args.bt):
        fail("--new-bt / --reuse-bt choose how a --customer BT starts; drop --resume / --bt")
    if args.resume and not args.customer:
        fail("--resume needs --customer (or use --bt)")
    if not 1 <= args.ornaments <= 20:
        fail("--ornaments must be 1-20 (the portal allows 20)")
    if args.amount < 1:
        fail("--amount must be positive")
    if args.gold_loan:
        args.complete_advance = True          # the loan needs the advance transfer completed first
    if args.complete_advance and args.stop_before_documents:
        fail("--complete-advance / --gold-loan cannot be combined with --stop-before-documents")
    if args.gl_amount is not None and not args.gold_loan:
        fail("--gl-amount only applies with --gold-loan")
    if args.gl_amount is not None and args.gl_amount < 1:
        fail("--gl-amount must be positive")
    if args.utr is not None and not args.complete_advance:
        fail("--utr only applies with --complete-advance (or --gold-loan)")
    if args.complete_advance and args.env not in ("test", "uat"):
        fail("--complete-advance is for TEST/UAT only")


class BtLoanRun:
    def __init__(self, args):
        self.args = args
        self.suite = GoldLoanApiTest()
        self.flow = BtLoanFlow(
            self.suite, amount=args.amount, tenure=args.tenure, approver=args.approver,
            rch_decision=args.rch_decision, lender=args.lender or "", company_type=args.company_type,
            scheme_id=args.scheme or "", ornament_count=args.ornaments, reupload_cpv=args.reupload_cpv,
            complete_advance=args.complete_advance, transfer_mode=args.transfer_mode, utr=args.utr)

    async def _customer(self):
        """The customer for a NEW BT: existing (KYC run if not approved) or a fresh one."""
        suite = self.suite
        if self.args.customer:
            suite._log_step("Load Existing Customer")
            await suite._use_existing_customer()
            if not await suite._existing_customer_kyc_ready():
                suite._log_step(f"KYC Process (existing customer, status={suite.kyc_status or 'unknown'})")
                await suite._run_full_kyc()
            return
        suite._log_step("Create Customer / Lead (admin)")
        saved = suite.auth_token
        try:
            suite.auth_token = await suite._role_token("admin")
            await suite.add_customer()
        finally:
            suite.auth_token = saved
        suite._log_step("KYC Process (appraiser)")
        await suite._run_full_kyc()

    async def _attach_customer_of_bt(self):
        """Resume: load the BT's own customer (uploads and the penny-drop need its id / name)."""
        suite, flow = self.suite, self.flow
        await flow.load()
        uid = (flow.app.get("customer") or {}).get("customerUniqueId")
        suite.existing_customer_unique_id = uid or ""
        suite.existing_customer_id = "" if uid else str(flow.app.get("customerId") or "")
        await suite._use_existing_customer()

    async def run(self):
        args, suite, flow = self.args, self.suite, self.flow
        mode = ("STOP BEFORE DOCUMENTS" if args.stop_before_documents else
                "COMPLETE (WITH GOLD LOAN)" if args.gold_loan else
                "TO ADVANCE COMPLETED" if args.complete_advance else "FULL")
        if args.bt or args.resume:
            mode = "RESUME" + (" (STOP BEFORE DOCUMENTS)" if args.stop_before_documents else "")
        suite._banner(f"BT LOAN - {mode}",
                      f"env={suite.env_name.upper()}   login={args.login}   amount={args.amount}   "
                      f"tenure={args.tenure}   approver={args.approver}")
        try:
            suite._log_step("Authentication")
            await suite.login(suite._get_mobile_number_for_login_type(args.login))

            if args.bt:
                suite._log_step(f"Resume BT {args.bt}")
                await flow.find_by_reference(args.bt)
                await self._attach_customer_of_bt()
            elif args.resume:
                suite._log_step(f"Resume open BT of {args.customer}")
                await suite._use_existing_customer()
                if not await flow.find_open_for_customer(suite.customer_unique_id):
                    raise BtLoanError(f"Customer {suite.customer_unique_id} has no open BT application "
                                      "(stage < 11). Drop --resume to start a new one.")
            else:
                await self._customer()
                suite._log_step("BT Request (Start New BT Process; the customer's existing BT if refused)")
                await flow.create_request("new-only" if args.new_bt else
                                          "reuse-first" if args.reuse_bt else "new-first")

            if args.complete_cpv:
                suite._log_step(f"Customer CPV ({suite.customer_unique_id})")
                await CustomerCpv(suite).complete()

            await flow.load()
            if flow.request_id:
                await flow.apply()
            print(f"BT {flow.bt_unique_id} (id {flow.bt_id}) is at stage {flow.stage}: {flow.stage_name}.")
            outcome = await flow.drive(stop_before_documents=args.stop_before_documents,
                                       log_step=suite._log_step)

            gold_loan = {}
            if args.gold_loan:
                if flow.stage < ADVANCE_COMPLETED_STAGE:
                    raise BtLoanError(f"BT {flow.bt_unique_id} is at stage {flow.stage} ({flow.stage_name}); the gold "
                                      "loan needs 'Advance Transfer Completed'.")
                gold_loan = await self._gold_loan()
                outcome = "complete: advance transferred + gold loan " + str(gold_loan.get("Gold loan", "")).split(" ")[0]

            suite._print_summary_table("BT LOAN DETAILS", {
                "Environment": f"{suite.env_name.upper()} ({suite.BASE_URL})",
                "Customer": f"{suite.customer_unique_id} (id {suite.customer_id})",
                **flow.summary(),
                **gold_loan,
                "Steps run": ", ".join(flow.steps_done + (["gold loan"] if gold_loan else [])) or "none",
                "Outcome": outcome,
            })
            print(f"\nResume later with:  python flows/e2e_bt_loan.py --bt {flow.bt_unique_id} --env {args.env}")
            suite._banner(f"RESULT - PASSED ({outcome.upper()})", f"BT {flow.bt_unique_id}")
            suite._print_run_metrics(passed=True)
            return True
        except httpx.HTTPStatusError as e:
            suite._banner("RESULT - FAILED (HTTP ERROR)", f"BT {flow.bt_unique_id or '-'}")
            print(f"  {e.request.method} {e.request.url}\n  status : {e.response.status_code}\n"
                  f"  body   : {suite._short_error_body(e.response.text, 600)}")
            self._resume_hint()
            suite._print_run_metrics(passed=False)
            return False
        except Exception as e:
            suite._banner("RESULT - FAILED", f"BT {flow.bt_unique_id or '-'}")
            print(f"  {type(e).__name__}: {e}")
            self._resume_hint()
            suite._print_run_metrics(passed=False)
            return False
        finally:
            await suite.client.aclose()

    async def _gold_loan(self) -> dict:
        """The gold loan on the BT's appraiser request, run as the full-loan journey in its own
        GoldLoanApiTest (fresh state; the saved sessions are reused). The request is pinned with
        GOLD_LOAN_APPRAISER_REQUEST_ID and the partner is the BT's. Returns summary rows."""
        flow, suite, args = self.flow, self.suite, self.args
        app = flow.app
        partner_id = str((app.get("partner") or {}).get("id") or app.get("partnerId") or "")
        partner_name = BT_PARTNER_NAMES.get(partner_id)
        if not partner_name:
            raise BtLoanError(f"BT partner {app.get('partner')} is not one the gold-loan flow knows "
                              f"({sorted(BT_PARTNER_NAMES.values())}).")
        amount = args.gl_amount or int(float(app.get("requiredLoanAmount") or args.amount))
        if not flow.request_id:
            raise BtLoanError(f"BT {flow.bt_unique_id} has no appraiser request id.")
        suite._log_step(f"Gold loan on BT request {flow.request_id} ({partner_name}, {amount})")
        pinned = {"GOLD_LOAN_APPRAISER_REQUEST_ID": str(flow.request_id), "GOLD_LOAN_AMOUNT": str(amount),
                  "GOLD_LOAN_PARTNER_NAME": partner_name,
                  "GOLD_LOAN_EXISTING_CUSTOMER_UNIQUE_ID": suite.customer_unique_id}
        before = {k: os.environ.get(k) for k in pinned}
        os.environ.update(pinned)
        try:
            gl = GoldLoanApiTest()
            gl.existing_customer_id = ""
            gl.existing_customer_unique_id = suite.customer_unique_id
            try:
                await gl.run_e2e_test(login_type=args.login)
            finally:
                await gl.client.aclose()
        finally:
            for key, value in before.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        # How the loan settles the BT: the server's advanceTransfer on the master loan.
        settle = {}
        if gl.master_loan_id:
            saved = suite.auth_token
            try:
                suite.auth_token = await suite._role_token("admin")
                response = await suite._make_authenticated_request(
                    "GET", f"/api/loan-process/single-loan-customer?masterLoanId={gl.master_loan_id}")
                settle = ((response.json() or {}).get("data") or {}).get("advanceTransfer") or {}
            finally:
                suite.auth_token = saved
        rows = {"Gold loan": f"{gl.loan_unique_id or '-'} (master {gl.master_loan_id or '-'}), {amount}, "
                             f"{partner_name}"}
        if settle:
            rows["BT settlement"] = (f"BT money {settle.get('bTMoney')}, to be paid {settle.get('toBePaid')}"
                                     + (" (the customer still owes this)" if float(settle.get("toBePaid") or 0) < 0
                                        else ""))
        return rows

    def _resume_hint(self):
        if self.flow.bt_unique_id or self.flow.bt_id:
            print(f"  Resume with: python flows/e2e_bt_loan.py --bt {self.flow.bt_unique_id or self.flow.bt_id} "
                  f"--env {self.args.env}")


def main():
    parser = build_parser()
    args = parser.parse_args()
    validate(args, parser.error)
    os.environ["GOLD_LOAN_ENV"] = args.env
    if args.partner != "any":
        os.environ["GOLD_LOAN_PARTNER_NAME"] = {"roshan": "ROSHAN PARTNER", "arvog": "ARVOG"}[args.partner]
    else:
        os.environ["GOLD_LOAN_PARTNER_NAME"] = ""
    if args.fresh_login:
        os.environ["GOLD_LOAN_FRESH_LOGIN"] = "true"
    # A new customer must not be confused with a stale env choice; --customer sets it explicitly.
    # A purely numeric --customer is the customer id, anything else the unique id.
    numeric = bool(args.customer and args.customer.strip().isdigit())
    os.environ["GOLD_LOAN_EXISTING_CUSTOMER_UNIQUE_ID"] = "" if numeric else (args.customer or "")
    if numeric:
        os.environ["GOLD_LOAN_EXISTING_CUSTOMER_ID"] = args.customer.strip()
    else:
        os.environ.pop("GOLD_LOAN_EXISTING_CUSTOMER_ID", None)
    ok = asyncio.run(BtLoanRun(args).run())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
