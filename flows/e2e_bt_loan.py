#!/usr/bin/env python3
"""BT (Balance Transfer) loan: take over a gold loan from another lender (LIVE -- hits the real backend).

    python flows/e2e_bt_loan.py                                   # FULL: new customer + KYC -> BT approved
    python flows/e2e_bt_loan.py --stop-before-documents           # stop at "Upload Documents" (stage 5)
    python flows/e2e_bt_loan.py --customer MWK4X6YU --amount 600000
    python flows/e2e_bt_loan.py --bt AUGM-BT-24551                # RESUME a BT from wherever it is parked
    python flows/e2e_bt_loan.py --customer MWK4X6YU --resume      # resume that customer's open BT
    python flows/e2e_bt_loan.py --rch-decision incomplete         # negative path at RCH

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
    APPROVAL_DECISIONS, BtLoanError, BtLoanFlow, DEFAULT_TENURE)

TENURES = ("30", "60", "90", "120", "150", "180")   # SharedService.loanTenure


def build_parser():
    parser = argparse.ArgumentParser(
        description="Create (or resume) a BT loan application.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n")[1])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--bt", metavar="AUGM-BT-XXXXX|ID", help="Resume this BT application.")
    target.add_argument("--customer", metavar="UNIQUE_ID",
                        help="Existing customer (default: create one with full KYC).")
    parser.add_argument("--resume", action="store_true",
                        help="With --customer: continue that customer's newest open BT instead of "
                             "starting a new one.")
    parser.add_argument("--stop-before-documents", action="store_true",
                        help="Stop when the BT reaches 'Upload Documents' (stage 5).")
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
    parser.add_argument("--fresh-login", action="store_true",
                        help="Log every role in again instead of reusing saved sessions.")
    return parser


def validate(args, fail):
    if args.resume and not args.customer:
        fail("--resume needs --customer (or use --bt)")
    if not 1 <= args.ornaments <= 20:
        fail("--ornaments must be 1-20 (the portal allows 20)")
    if args.amount < 1:
        fail("--amount must be positive")


class BtLoanRun:
    def __init__(self, args):
        self.args = args
        self.suite = GoldLoanApiTest()
        self.flow = BtLoanFlow(
            self.suite, amount=args.amount, tenure=args.tenure, approver=args.approver,
            rch_decision=args.rch_decision, lender=args.lender or "", company_type=args.company_type,
            scheme_id=args.scheme or "", ornament_count=args.ornaments, reupload_cpv=args.reupload_cpv)

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
        mode = "STOP BEFORE DOCUMENTS" if args.stop_before_documents else "FULL"
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
                suite._log_step("BT Request (Start New BT Process)")
                await flow.create_request()

            if args.complete_cpv:
                suite._log_step(f"Customer CPV ({suite.customer_unique_id})")
                await CustomerCpv(suite).complete()

            await flow.load()
            if flow.request_id:
                await flow.apply()
            print(f"BT {flow.bt_unique_id} (id {flow.bt_id}) is at stage {flow.stage}: {flow.stage_name}.")
            outcome = await flow.drive(stop_before_documents=args.stop_before_documents,
                                       log_step=suite._log_step)

            suite._print_summary_table("BT LOAN DETAILS", {
                "Environment": f"{suite.env_name.upper()} ({suite.BASE_URL})",
                "Customer": f"{suite.customer_unique_id} (id {suite.customer_id})",
                **flow.summary(),
                "Steps run": ", ".join(flow.steps_done) or "none",
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
    os.environ["GOLD_LOAN_EXISTING_CUSTOMER_UNIQUE_ID"] = args.customer or ""
    os.environ.pop("GOLD_LOAN_EXISTING_CUSTOMER_ID", None)
    ok = asyncio.run(BtLoanRun(args).run())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
