# flows/kyc_v1.py
"""Customer creation + KYC on the VERSION 1 KYC endpoints -- and nothing else.

The original (v1) KYC journey, before the portal moved to /api/kyc/v2/* and added the consent OTP:

    customer creation (admin)            POST /api/customer            (today's lead OTP)
    KYC record + basic details / PAN     POST /api/kyc/customer-info
      - manual : generated dummy PAN / Aadhaar, no verification calls
      - auto   : REAL PAN + REAL offline-Aadhaar XML, verified
                 POST /api/e-kyc/verify-pan, POST /api/e-kyc/offline-aadhaar-xml
    KYC master data                      occupation / religion / qualification / ...
    personal details                     POST /api/kyc/customer-kyc-personal
    address + identity proof             POST /api/kyc/customer-kyc-address
    authoritative submission             POST /api/kyc/submit-all-kyc-info   (same as v2)
    ops approval                         POST /api/classification/ops-team   (same as v2)

No consent OTP and no v2 get-customer-detail (neither existed in v1). The request bodies, the
encryption and the manual / auto identity rules are the harness's own (gold_loan/harness.py with
GOLD_LOAN_KYC_API_VERSION=v1), so this file only changes the order and the endpoints; the batch,
Re-KYC and reporting come from flows/kyc_only.py.

Usage:
    python flows/kyc_v1.py                         # manual v1 KYC, one new customer
    python flows/kyc_v1.py --kyc auto              # real PAN + real Aadhaar XML
    python flows/kyc_v1.py --count 3               # three KYC'd customers (manual)
    python flows/kyc_v1.py --customer MS35QNJP     # v1 KYC on an existing customer
    python flows/kyc_v1.py --env uat --skip-ops-approval
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # project root

from gold_loan.harness import GoldLoanApiTest  # noqa: E402
from flows.kyc_only import KycOnlyTest  # noqa: E402

# v1 had only the PAN identity path: manual (dummy numbers) or auto (real, verified).
V1_KYC_MODES = (GoldLoanApiTest.KYC_MODE_MANUAL, GoldLoanApiTest.KYC_MODE_AUTO)


class KycV1Test(KycOnlyTest):
    """kyc_only.py's runner (customer creation, batch, Re-KYC, report) with the v1 KYC sequence."""

    async def _run_kyc(self):
        s = self.suite
        if s.kyc_api_version != "v1":
            raise RuntimeError("KycV1Test needs GOLD_LOAN_KYC_API_VERSION=v1 (set by flows/kyc_v1.py).")
        s._print_kyc_mode_header()
        await s.release_kyc_documents_if_enabled()   # --release-kyc-docs (TEST/UAT)
        await s.get_customer_by_id()
        await s.submit_basic_info()             # v1 customer-info (+ PAN verification for auto)
        await s.load_kyc_master_data()
        await s.fetch_existing_e_kyc()
        await s.save_customer_personal_details()  # v1 customer-kyc-personal
        await s.save_customer_address()         # v1 customer-kyc-address (+ Aadhaar XML for auto)
        await s.submit_all_kyc_info()
        if self.ops_approval:
            await s.kyc_ops_approval()
        else:
            print("Skipping ops approval (--skip-ops-approval): KYC left pending.")
        await s.get_customer_by_id()            # refresh so kycStatus reflects the approval
        s._print_kyc_verification_summary()


def main():
    parser = argparse.ArgumentParser(
        description="Create customer(s) and complete KYC on the version 1 KYC endpoints.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1])
    parser.add_argument("--env", choices=sorted(GoldLoanApiTest.ENVIRONMENTS),
                        default=os.getenv("GOLD_LOAN_ENV", GoldLoanApiTest.DEFAULT_ENV).strip().lower(),
                        help="Target environment (default: %(default)s).")
    parser.add_argument("--kyc", choices=list(V1_KYC_MODES), default=GoldLoanApiTest.KYC_MODE_MANUAL,
                        help="'manual' (default) = generated dummy PAN / Aadhaar, no verification; "
                             "'auto' = REAL PAN + REAL offline-Aadhaar XML, verified.")
    parser.add_argument("--kyc-profile", metavar="PATH",
                        help="Real identity data for --kyc auto (default: config/kyc_identity.json).")
    parser.add_argument("--pan-type", choices=["pan", "form60"],
                        help="PAN document type (default: pan). v1 also accepted Form 60 instead of a PAN.")
    parser.add_argument("--customer", metavar="UNIQUE_ID",
                        help="Run v1 KYC on an EXISTING customer instead of creating one.")
    parser.add_argument("--count", "-n", type=int, default=1,
                        help="New customers to create and KYC (default 1; manual mode only).")
    parser.add_argument("--skip-ops-approval", dest="ops_approval", action="store_false",
                        help="Submit the KYC but leave it pending (no ops approval).")
    parser.add_argument("--release-kyc-docs", action="store_true",
                        help="TEST/UAT: free the profile's real PAN (+ Aadhaar) before KYC (--kyc auto).")
    parser.add_argument("--login", default="appraiser", help="Role that runs the KYC (default: appraiser).")
    parser.add_argument("--fresh-login", action="store_true",
                        help="Ignore the saved login sessions and log every role in with OTP.")
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be at least 1.")
    if args.customer and args.count > 1:
        parser.error("--customer works on one existing customer; drop --count.")
    if args.count > 1 and args.kyc == GoldLoanApiTest.KYC_MODE_AUTO and not args.release_kyc_docs:
        parser.error("--count > 1 with --kyc auto needs --release-kyc-docs: the real PAN can only be "
                     "registered against one customer at a time.")

    os.environ["GOLD_LOAN_ENV"] = args.env
    os.environ["GOLD_LOAN_KYC_API_VERSION"] = "v1"
    os.environ["GOLD_LOAN_KYC_MODE"] = args.kyc
    if args.kyc_profile:
        os.environ["GOLD_LOAN_KYC_PROFILE"] = args.kyc_profile
    if args.pan_type:
        os.environ["GOLD_LOAN_PAN_TYPE"] = args.pan_type
    if args.release_kyc_docs:
        os.environ["GOLD_LOAN_RELEASE_KYC_DOCS"] = "true"
    if args.fresh_login:
        os.environ["GOLD_LOAN_SESSION_CACHE"] = "false"

    suite = GoldLoanApiTest()
    print(f">> Environment: {args.env.upper()} ({suite.BASE_URL})")
    print(f">> KYC version 1 | mode: {args.kyc} | ops approval: {'yes' if args.ops_approval else 'skipped'}")
    print(f">> {'Re-KYC of existing customer ' + args.customer if args.customer else str(args.count) + ' new customer(s)'}")

    runner = KycV1Test(suite, count=1 if args.customer else args.count,
                       existing_customer=args.customer or "", ops_approval=args.ops_approval,
                       login_type=args.login)

    async def _run():
        try:
            return await runner.run()
        finally:
            await suite.client.aclose()

    try:
        passed = asyncio.run(_run())
    except Exception:
        sys.exit(1)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
