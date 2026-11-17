#!/usr/bin/env python3
"""Complete the customer CPV (Contact Point Verification) through the API (LIVE -- hits the real backend).

    python flows/complete_cpv.py --customer MWK4XHP4                 # one existing customer
    python flows/complete_cpv.py --customer MWK4XHP4 --customer MWK5D1YA
    python flows/complete_cpv.py                                      # new customer + KYC, then CPV
    python flows/complete_cpv.py --count 3                            # three new customers
    python flows/complete_cpv.py --customer MWK4XHP4 --status         # only show the CPV status

What the portal's CPV form sends (Applied KYC -> CPV): the visit details, two geo-tagged photos
(tagged at the customer's branch), and the customer's signature. The signature is a PLACEHOLDER
image -- the portal takes it from a real Digio e-sign the customer completes with an OTP, which
cannot run unattended. TEST/UAT only. A customer whose CPV is already valid is left alone; an
expired one, or one invalidated by a re-KYC, gets a new CPV. Saved login sessions are reused.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # project root

import httpx  # noqa: E402

from gold_loan.harness import GoldLoanApiTest  # noqa: E402
from gold_loan.cpv import CustomerCpv, CpvError, cpv_state  # noqa: E402


def build_parser():
    parser = argparse.ArgumentParser(
        description="Complete the customer CPV (placeholder signature; TEST/UAT).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n")[1])
    parser.add_argument("--customer", action="append", metavar="UNIQUE_ID",
                        help="Existing customer (repeatable). Default: create new customer(s) with KYC.")
    parser.add_argument("--count", type=int, default=1,
                        help="How many NEW customers to create and CPV (without --customer; default 1).")
    parser.add_argument("--status", action="store_true", help="Only print the CPV status (needs --customer).")
    parser.add_argument("--env", choices=sorted(GoldLoanApiTest.ENVIRONMENTS), default="test",
                        help="Target environment (default: test).")
    parser.add_argument("--login", default="appraiser", help="Role that does the CPV (default: appraiser).")
    parser.add_argument("--fresh-login", action="store_true",
                        help="Log every role in again instead of reusing saved sessions.")
    return parser


def validate(args, fail):
    if args.customer and args.count != 1:
        fail("--count is for new customers; with --customer list each customer instead")
    if args.count < 1:
        fail("--count must be at least 1")
    if args.status and not args.customer:
        fail("--status needs --customer")


async def load_customer(suite, unique_id):
    suite._reset_for_next_loan()
    suite.existing_customer_unique_id = unique_id
    suite.existing_customer_id = ""
    suite._log_step(f"Load Customer {unique_id}")
    await suite._use_existing_customer()
    if not await suite._existing_customer_kyc_ready():
        suite._log_step(f"KYC Process (status={suite.kyc_status or 'unknown'})")
        await suite._run_full_kyc()


async def new_customer(suite):
    suite._reset_for_next_loan()
    suite.existing_customer_unique_id = ""
    suite.existing_customer_id = ""
    suite._log_step("Create Customer / Lead (admin)")
    saved = suite.auth_token
    try:
        suite.auth_token = await suite._role_token("admin")
        await suite.add_customer()
    finally:
        suite.auth_token = saved
    suite._log_step("KYC Process (appraiser)")
    await suite._run_full_kyc()


async def run(args):
    suite = GoldLoanApiTest()
    targets = args.customer or [None] * args.count
    results = []
    suite._banner("CUSTOMER CPV" + (" - STATUS" if args.status else ""),
                  f"env={suite.env_name.upper()}   login={args.login}   customers={len(targets)}")
    try:
        suite._log_step("Authentication")
        await suite.login(suite._get_mobile_number_for_login_type(args.login))
        for target in targets:
            label = target or "(new customer)"
            try:
                if target:
                    await load_customer(suite, target) if not args.status else await _load_only(suite, target)
                else:
                    await new_customer(suite)
                cpv = CustomerCpv(suite)
                if args.status:
                    record = await cpv.current()
                    results.append((suite.customer_unique_id, cpv_state(record), record or {}, ""))
                    continue
                suite._log_step(f"CPV {suite.customer_unique_id}")
                record = await cpv.complete()
                results.append((suite.customer_unique_id, "created" if cpv.created else "already valid", record, ""))
            except (httpx.HTTPStatusError, CpvError, AssertionError, RuntimeError) as e:
                detail = (f"{e.response.status_code} {suite._short_error_body(e.response.text, 200)}"
                          if isinstance(e, httpx.HTTPStatusError) else str(e))
                print(f"CPV failed for {suite.customer_unique_id or label}: {detail}")
                results.append((suite.customer_unique_id or label, "FAILED", {}, detail))
    finally:
        await suite.client.aclose()

    print("\n■ CPV RESULTS")
    print(f"  {'customer':<12} {'result':<14} {'cpv id':<7} {'status':<9} expires")
    for uid, outcome, record, detail in results:
        print(f"  {uid:<12} {outcome:<14} {str(record.get('id') or '-'):<7} "
              f"{str(record.get('cpvStatus') or '-'):<9} {str(record.get('expiresAt') or '-')[:10]}"
              + (f"   {detail}" if detail else ""))
    failed = [r for r in results if r[1] == "FAILED"]
    suite._banner("RESULT - " + ("FAILED" if failed else "PASSED"),
                  f"{len(results) - len(failed)}/{len(results)} customer(s)")
    suite._print_run_metrics(passed=not failed)
    return not failed


async def _load_only(suite, unique_id):
    suite._reset_for_next_loan()
    suite.existing_customer_unique_id = unique_id
    suite.existing_customer_id = ""
    await suite._use_existing_customer()


def main():
    parser = build_parser()
    args = parser.parse_args()
    validate(args, parser.error)
    os.environ["GOLD_LOAN_ENV"] = args.env
    if args.fresh_login:
        os.environ["GOLD_LOAN_FRESH_LOGIN"] = "true"
    os.environ["GOLD_LOAN_EXISTING_CUSTOMER_UNIQUE_ID"] = ""
    os.environ.pop("GOLD_LOAN_EXISTING_CUSTOMER_ID", None)
    sys.exit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()
