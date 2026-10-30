#!/usr/bin/env python3
"""Repayment scenarios on a disbursed gold loan: renewal, renewal + top-up, top-up, part release,
full release (LIVE -- hits the real backend).

    python flows/e2e_repayment.py --scenario renewal                 # new loan, then renew it
    python flows/e2e_repayment.py --scenario renewal-topup --loan AUGM-71513
    python flows/e2e_repayment.py --scenario topup --master-loan-id 12284
    python flows/e2e_repayment.py --scenario part-release --customer MSSU4GTG
    python flows/e2e_repayment.py --scenario full-release --transfer-mode imps

Without --loan / --master-loan-id a fresh loan is created first (the full e2e journey, every new-loan
flag applies), in the SAME session, and the scenario runs on it -- one login per role for the whole
run. Renewal, top-up and part release open a NEW loan; it is driven to completion with the resume
machinery (flows/resume_loan.py), so it ends at "packet submitted" like any other loan. A full
release closes the loan.

Every request mirrors the web portal; see gold_loan/repayment.py for the screen-by-screen mapping.
Payments are the portal's OFFLINE NEFT / IMPS / RTGS entries -- no payment gateway is used.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # project root

import httpx  # noqa: E402

from gold_loan.harness import GoldLoanApiTest  # noqa: E402
from gold_loan.repayment import RepaymentScenarios, RepaymentScenarioError  # noqa: E402
from flows.resume_loan import ResumeLoanTest  # noqa: E402


def build_parser():
    parser = argparse.ArgumentParser(
        description="Renewal / top-up / part release / full release on a disbursed gold loan.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n")[1])
    parser.add_argument("--scenario", required=True, choices=RepaymentScenarios.SCENARIOS,
                        help="What to do with the loan.")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--loan", metavar="AUGM-XXXXX",
                        help="An existing DISBURSED loan. Omit to create a new loan first.")
    target.add_argument("--master-loan-id", metavar="ID",
                        help="The existing loan's master loan id (instead of --loan).")
    parser.add_argument("--env", choices=sorted(GoldLoanApiTest.ENVIRONMENTS), default="test",
                        help="Target environment (default: test).")
    parser.add_argument("--login", default="appraiser",
                        help="Login role for the main flow (default: appraiser).")
    parser.add_argument("--partner", choices=["roshan", "arvog"], default="roshan",
                        help="Partner of the loan; its branch user hands the packet back on a release.")
    parser.add_argument("--transfer-mode", choices=list(GoldLoanApiTest.TRANSFER_MODES), default="neft",
                        help="OFFLINE payment mode for the repayment (and the new loan's disbursement).")
    parser.add_argument("--igl-docs", metavar="N|all",
                        help="IGL loans: income document types to upload (default 1; 'all').")
    parser.add_argument("--igl-doc-files", type=int, metavar="M",
                        help="IGL loans: PDFs per income document type, 1-10 (default 1).")
    parser.add_argument("--cash-amount", metavar="N|full",
                        help="Pay part (or 'full') of the NEW loan in cash at bank details; "
                             "default: everything by bank transfer.")
    parser.add_argument("--purpose-type", choices=sorted(GoldLoanApiTest.PURPOSE_TYPES) + ["any"],
                        default="cb", help="Purpose type for the new loan (default: cb).")
    parser.add_argument("--purpose", metavar="NAME", help="Pin the new loan's purpose by name.")
    parser.add_argument("--new-scheme", metavar="SCHEME_ID",
                        help="Pin the scheme the renewal / top-up / part-release loan moves to.")
    parser.add_argument("--fresh-login", action="store_true",
                        help="Ignore the saved login sessions and log every role in with OTP.")
    parser.add_argument("--approver", choices=["ops", "admin"], default="ops",
                        help="Role that approves the paid amount and assigns the appraiser "
                             "(default: ops; falls back to admin when refused).")
    fresh = parser.add_argument_group("creating the loan first (no --loan / --master-loan-id)")
    fresh.add_argument("--customer", metavar="UNIQUE_ID",
                       help="Existing customer for the new loan (default: create one).")
    fresh.add_argument("--amount", type=float, help="Amount of the new loan (default 400000).")
    fresh.add_argument("--scheme", metavar="SCHEME_ID", help="Scheme of the new loan.")
    return parser


def resume_point(scenario):
    """Where the new loan is picked up. A top-up loan arrives with its final-loan-details already
    posted (the body create-new-loan built) and the portal opens it at bank details
    (loanCurrentStage 5); re-running ornaments + scheme would recompute it as a fresh loan and
    break toBePaid (live TEST, loan 14300). Renewal / part-release loans start from the record."""
    return "bank" if scenario == "topup" else None


class RepaymentFlow:
    def __init__(self, args):
        self.args = args
        self.suite = GoldLoanApiTest()
        self.parent = {}     # the loan the scenario acted on
        self.child = {}      # the loan it opened (renewal / top-up / part release)

    async def _resolve_master_loan_id(self):
        args, suite = self.args, self.suite
        if args.master_loan_id:
            return str(args.master_loan_id)
        row = await suite.find_loan_row(args.loan, [])
        if row is None:
            raise RepaymentScenarioError(f"Loan {args.loan} was not found on env '{suite.env_name}'.")
        master_loan_id, _ = ResumeLoanTest._row_ids(row, args.loan)
        if not master_loan_id:
            raise RepaymentScenarioError(f"Found {args.loan} but its row carries no master loan id.")
        return master_loan_id

    async def run(self):
        args, suite = self.args, self.suite
        suite._banner(f"REPAYMENT - {args.scenario.upper()}",
                      f"env={suite.env_name}   transfer={args.transfer_mode}   approver={args.approver}")
        try:
            if args.loan or args.master_loan_id:
                await suite.login(suite._get_mobile_number_for_login_type(args.login))
                master_loan_id = await self._resolve_master_loan_id()
                self.parent = {"masterLoanId": master_loan_id, "loanUniqueId": args.loan or ""}
            else:
                # The full journey first, in this session; it raises if the loan does not complete.
                await suite.run_e2e_test(login_type=args.login)
                master_loan_id = str(suite.master_loan_id)
                self.parent = {"masterLoanId": master_loan_id, "loanUniqueId": suite.loan_unique_id}

            # The new loan's scheme is pinned only now, so it never steers the parent loan.
            suite.forced_scheme_id = args.new_scheme or ""
            scenario = RepaymentScenarios(suite, args.scenario, args.approver)
            new_loan = await scenario.run(master_loan_id)

            if new_loan:
                final_data = suite.final_loan_details_data   # top-up: the body we just submitted
                suite._reset_for_next_loan()                 # the new loan starts clean (tokens kept)
                resumer = ResumeLoanTest(argparse.Namespace(start_from=resume_point(args.scenario),
                                                            dry_run=False, login=args.login), suite)

                def restore(s):
                    if args.scenario == "topup" and final_data:
                        s.final_loan_details_data = final_data
                resumed_at = await resumer.complete(new_loan["loanId"], new_loan.get("masterLoanId"), restore)
                if resumed_at is None and scenario.child_only:
                    raise RepaymentScenarioError(
                        f"Master loan {master_loan_id} already completed its {args.scenario} (request "
                        f"{scenario.request_id}) and the loan it opened is complete too. A parent loan "
                        "can be used for one scenario only - use the new loan or another loan.")
                self.child = {"masterLoanId": suite.master_loan_id, "loanUniqueId": suite.loan_unique_id}

            self._summary(scenario)
            suite._banner("RESULT - PASSED", f"{args.scenario} completed")
            suite._print_run_metrics(passed=True)
            return True
        except httpx.HTTPStatusError as e:
            suite._banner("RESULT - FAILED (HTTP ERROR)", args.scenario)
            print(f"  {e.request.method} {e.request.url}\n  status : {e.response.status_code}\n"
                  f"  body   : {suite._short_error_body(e.response.text, 600)}")
            suite._print_run_metrics(passed=False)
            return False
        except Exception as e:
            suite._banner("RESULT - FAILED", args.scenario)
            print(f"  {type(e).__name__}: {e}")
            suite._print_run_metrics(passed=False)
            return False
        finally:
            await suite.client.aclose()

    def _summary(self, scenario):
        details = {
            "Scenario": self.args.scenario,
            "Parent loan": f"{self.parent.get('loanUniqueId') or '-'} (master {self.parent.get('masterLoanId')})",
            "Request id": scenario.request_id or "-",
            "Scheme": (f"{scenario.scheme.get('id')} {scenario.scheme.get('schemeName') or ''}".strip()
                       if scenario.scheme else "-"),
            **scenario.result,
        }
        if self.child:
            details["New loan"] = (f"{self.child.get('loanUniqueId') or '-'} "
                                   f"(master {self.child.get('masterLoanId')})")
        elif self.args.scenario == "full-release":
            details["Parent loan status"] = "closed (ornaments released)"
        self.suite._print_summary_table("REPAYMENT DETAILS", details)


def main():
    args = build_parser().parse_args()
    # Read by the constructor: environment, partner, purpose, transfer mode, new-loan choices.
    os.environ["GOLD_LOAN_ENV"] = args.env
    os.environ["GOLD_LOAN_PARTNER_NAME"] = {"roshan": "ROSHAN PARTNER", "arvog": "ARVOG"}[args.partner]
    os.environ["GOLD_LOAN_TRANSFER_MODE"] = args.transfer_mode
    if args.fresh_login:
        os.environ["GOLD_LOAN_SESSION_CACHE"] = "false"
    os.environ["GOLD_LOAN_PURPOSE_TYPE"] = args.purpose_type
    if args.cash_amount:
        os.environ["GOLD_LOAN_CASH_AMOUNT"] = args.cash_amount
    if args.igl_docs:
        os.environ["GOLD_LOAN_IGL_DOCS"] = args.igl_docs
    if args.igl_doc_files:
        os.environ["GOLD_LOAN_IGL_DOC_FILES"] = str(args.igl_doc_files)
    if args.purpose:
        os.environ["GOLD_LOAN_PURPOSE"] = args.purpose
    if args.amount:
        os.environ["GOLD_LOAN_AMOUNT"] = str(args.amount)
    if args.scheme:
        os.environ["GOLD_LOAN_SCHEME_ID"] = args.scheme
    flow = RepaymentFlow(args)
    flow.suite.existing_customer_id = ""
    flow.suite.existing_customer_unique_id = args.customer or ""
    return 0 if asyncio.run(flow.run()) else 1


if __name__ == "__main__":
    sys.exit(main())
