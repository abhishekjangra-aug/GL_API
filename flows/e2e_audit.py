#!/usr/bin/env python3
"""Audit a completed gold loan as the AUDITOR (LIVE, TEST only for now).

    python flows/e2e_audit.py                         # newest unaudited loan of the auditor's branch
    python flows/e2e_audit.py --loan AUGM-09203
    python flows/e2e_audit.py --master-loan-id 12325 --weight-change 0.1 --purity-change 1
    python flows/e2e_audit.py --loan AUGM-09203 --old-packet
    python flows/e2e_audit.py --audit-type Open-audit     # open audit, with the branch custodian

The admin assigns the loan to the auditor (TEST: 8880008886) and creates a NEW packet for them; the
auditor re-checks every ornament -- the net weight comes out a little lower and the purity
(ltvPercent) a step lower than the appraiser's -- re-seals them in the new packet and submits the
audit. The run ends with the appraiser's and the auditor's values side by side. See
gold_loan/audit.py for the portal screen behind every call.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # project root

from gold_loan.harness import GoldLoanApiTest  # noqa: E402
from gold_loan.audit import AuditFlow, AuditError, AUDIT_TYPES  # noqa: E402


def build_parser():
    parser = argparse.ArgumentParser(
        description="Audit a completed gold loan as the auditor (TEST only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n")[1])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--loan", metavar="AUGM-XXXXX", help="The completed loan to audit.")
    target.add_argument("--master-loan-id", metavar="ID", help="Its master loan id (instead of --loan).")
    parser.add_argument("--env", choices=["test"], default="test", help="Only TEST for now.")
    parser.add_argument("--weight-change", type=float, metavar="GRAMS",
                        help="Grams the auditor finds LESS in each ornament's net weight "
                             "(default: random 0.05-0.25 g per ornament).")
    parser.add_argument("--purity-change", type=float, metavar="POINTS",
                        help="Purity (ltvPercent) points LOWER than the appraiser's "
                             "(default: one step down the ornament's purity range).")
    parser.add_argument("--old-packet", action="store_true",
                        help="Re-use the loan's packet instead of a new one for the auditor.")
    parser.add_argument("--audit-type", choices=list(AUDIT_TYPES), default="Audit",
                        help="Assignment type (default: Audit).")
    parser.add_argument("--note", help="Audit note (default: a generated one).")
    parser.add_argument("--custodian", choices=["appraiser", "bm", "ops", "admin"], default="appraiser",
                        help="Open-audit only: the login that acts as the branch custodian (it must be a "
                             "user of the loan's branch; default: appraiser).")
    parser.add_argument("--fresh-login", action="store_true",
                        help="Log every role in again instead of reusing saved sessions "
                             "(the new tokens are still saved).")
    return parser


def main():
    args = build_parser().parse_args()
    os.environ["GOLD_LOAN_ENV"] = args.env
    if args.fresh_login:
        os.environ["GOLD_LOAN_FRESH_LOGIN"] = "true"
    suite = GoldLoanApiTest()
    flow = AuditFlow(suite, loan_unique_id=args.loan, master_loan_id=args.master_loan_id,
                     weight_change=args.weight_change, purity_change=args.purity_change,
                     old_packet=args.old_packet, audit_type=args.audit_type, note=args.note,
                     custodian_role=args.custodian)
    suite._banner("GOLD LOAN - AUDIT", f"env={args.env.upper()}   loan={args.loan or args.master_loan_id or 'auto'}   "
                                       f"packet={'old' if args.old_packet else 'new'}   base={suite.BASE_URL}")

    async def _run():
        try:
            await flow.run()
            suite._print_run_metrics(passed=True)
            return True
        except Exception as e:
            suite._banner("RESULT - FAILED", f"{type(e).__name__}: {e}")
            suite._print_run_metrics(passed=False)
            if not isinstance(e, AuditError):
                raise
            return False
        finally:
            await suite.client.aclose()

    sys.exit(0 if asyncio.run(_run()) else 1)


if __name__ == "__main__":
    main()
