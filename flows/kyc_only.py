# src/test_kyc.py
"""Standalone KYC harness -- the complete KYC journey, and nothing else.

Runs the customer + KYC half of the Gold Loan flow on its own, in any of the three identity
modes, and stops before the loan process. When a loan is wanted afterwards, hand the customer
this script prints to `maintest.py --customer <uniqueId>`; that path reuses an approved KYC and
goes straight to the loan.

Covered here (the full KYC feature set):
    customer creation (admin)            POST /api/customer
    KYC record + basic details           POST /api/kyc/v2/submit-basic-info
      - manual : generated dummy PAN / Aadhaar, no verification calls
      - auto   : REAL PAN + REAL offline-Aadhaar XML
                 POST /api/e-kyc/verify-pan, POST /api/e-kyc/offline-aadhaar-xml
      - ovd    : REAL Voter ID / Driving Licence instead of PAN, plus Form 97
                 POST /api/e-kyc/v2/verify-voter-id, POST /api/e-kyc/v2/verify-dl
    consent OTP                          POST /api/customer-otp/send-otp + verify-otp-admin
    KYC master data                      occupation / religion / qualification / ...
    address + identity proof             POST /api/kyc/v2/customer-kyc-address
    personal details                     POST /api/kyc/v2/customer-kyc-personal
    authoritative submission             POST /api/kyc/submit-all-kyc-info
    ops approval                         POST /api/classification/ops-team

Usage:
    python src/test_kyc.py                              # manual KYC, one new customer
    python src/test_kyc.py --kyc auto                   # real PAN + real Aadhaar XML
    python src/test_kyc.py --kyc ovd                    # real OVD + Form 97, no PAN
    python src/test_kyc.py --count 5                    # seed 5 KYC'd customers (manual only)
    python src/test_kyc.py --customer MS35QNJP          # Re-KYC an existing customer
    python src/test_kyc.py --kyc-type NEW_KYC           # NEW_KYC instead of the RE_KYC default
    python src/test_kyc.py --skip-ops-approval          # leave the KYC pending for a manual review
    python src/test_kyc.py --env uat --kyc ovd

Real identity data for --kyc auto/ovd comes from config/kyc_identity.json (or GOLD_LOAN_KYC_*
env vars) -- see config/kyc_identity.example.json. Real numbers cannot be generated.

Exit code is 0 only when every requested customer reached an approved KYC (or a completed
submission when --skip-ops-approval is used).
"""

import argparse
import asyncio
import os
import sys

# Allow `python src/test_kyc.py` from any cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

from maintest import GoldLoanApiTest, KycDocumentAlreadyExistsError  # noqa: E402


class KycOnlyTest:
    """Drives GoldLoanApiTest through customer creation + KYC, then stops.

    Wraps the harness rather than subclassing it, so every request body, HMAC signature and
    verification rule stays defined in exactly one place (maintest.py) and this file cannot
    drift away from the flow maintest actually runs.
    """

    # Per-customer state that must NOT leak from one customer to the next in a --count batch.
    # Anything left set here would be silently reused (e.g. a previous customer's PAN, which the
    # server then rejects as already registered).
    PER_CUSTOMER_STATE = (
        "customer_id", "customer_unique_id", "customer_kyc_id", "mobile_number",
        "first_name", "last_name", "random_pan", "pan_image", "form60_image",
        "identity_proof_number", "encrypted_identity_proof_number",
        "masked_identity_proof", "unmasked_identity_proof", "address_proof",
        "name_as_per_aadhaar", "profile_image", "signature_proof",
        "mother_name", "spouse_name", "martial_status", "gender", "dob",
        "ovd_image", "form97_image", "ovd_name", "kyc_status", "kyc_reference_code",
    )

    def __init__(self, suite: GoldLoanApiTest, count: int = 1, existing_customer: str = "",
                 ops_approval: bool = True, login_type: str = "appraiser"):
        self.suite = suite
        self.count = count
        self.existing_customer = existing_customer
        self.ops_approval = ops_approval
        self.login_type = login_type
        self.results = []

    # --- per-customer state hygiene ----------------------------------------------------------

    def _reset_for_next_customer(self):
        """Clear the previous customer's identity so the next one starts clean."""
        s = self.suite
        for field in self.PER_CUSTOMER_STATE:
            if hasattr(s, field):
                setattr(s, field, "" if isinstance(getattr(s, field), str) else None)
        s.age = None
        s.customer_details = {}
        s.ovd_verification_data = {}
        # Verification verdicts are per-customer, and so is a degrade back to manual.
        s.pan_verified = False
        s.aadhaar_verified = False
        s.ovd_verified = False
        s.kyc_verification_log = []
        s.kyc_mode_effective = s.kyc_mode_requested
        s.kyc_mode = s.kyc_mode_requested

    # --- one customer ------------------------------------------------------------------------

    async def run_one(self, index: int) -> dict:
        """Create (or load) a customer and take it all the way through KYC."""
        s = self.suite
        record = {"n": index, "customer_id": None, "unique_id": None, "kyc_status": None,
                  "mode_requested": s.kyc_mode_requested, "mode_effective": None,
                  "pan": False, "aadhaar": False, "ovd": False, "error": None, "aborted": False}
        try:
            if self.existing_customer:
                s._log_step(f"Load existing customer {self.existing_customer}")
                s.existing_customer_unique_id = self.existing_customer
                await s._use_existing_customer()
            else:
                # Customer creation runs under ADMIN: creating it as the appraiser hits a
                # "request already exists" error the admin credential avoids. KYC itself then
                # runs as the appraiser, exactly as run_e2e_test does it.
                s._log_step(f"Create customer {index}/{self.count} (admin)")
                saved_token = s.auth_token
                try:
                    s.auth_token = await s._role_token("admin")
                    await s.add_customer()
                finally:
                    s.auth_token = saved_token

            record["customer_id"] = s.customer_id
            s._log_step(f"KYC ({s.kyc_mode_requested}) for customer {s.customer_id}")
            await self._run_kyc()

            record.update({
                "unique_id": s.customer_unique_id,
                "customer_id": s.customer_id,
                "kyc_status": s.kyc_status or ("submitted" if not self.ops_approval else "unknown"),
                "mode_effective": s.kyc_mode_effective,
                "pan": s.pan_verified,
                "aadhaar": s.aadhaar_verified,
                "ovd": s.ovd_verified,
            })
        except KycDocumentAlreadyExistsError as e:
            # A document already on file is terminal by design -- do not continue the batch,
            # because every remaining customer would hit the same registered document.
            record["error"] = str(e).splitlines()[0]
            record["aborted"] = True
            record["mode_effective"] = s.kyc_mode_effective
            raise
        except httpx.HTTPStatusError as e:
            record["error"] = f"HTTP {e.response.status_code} {e.request.url.path}: {e.response.text[:160]}"
            record["mode_effective"] = s.kyc_mode_effective
        except Exception as e:  # noqa: BLE001 - one customer must not kill the batch
            record["error"] = f"{type(e).__name__}: {e}"
            record["mode_effective"] = s.kyc_mode_effective
        return record

    async def _run_kyc(self):
        """The KYC sequence itself.

        Mirrors GoldLoanApiTest._run_full_kyc step for step, but with ops approval optional so a
        run can stop at 'submitted' and leave the record for a human reviewer.
        """
        s = self.suite
        s._print_kyc_mode_header()
        await s.get_customer_by_id()
        try:
            await s.get_kyc_customer_detail()
        except httpx.HTTPStatusError as e:
            # A brand-new customer has no KYC record yet; submit-basic-info creates it.
            print(f"get-customer-detail (v2) not available yet (non-fatal): {e.response.status_code}")
        await s.submit_basic_info()             # + PAN / OVD verification for auto / ovd
        await s._kyc_consent_otp()
        await s.load_kyc_master_data()
        await s.fetch_existing_e_kyc()
        await s.save_customer_address()         # + Aadhaar XML verification for auto
        await s.save_customer_personal_details()
        await s.submit_all_kyc_info()
        if self.ops_approval:
            await s.kyc_ops_approval()
        else:
            print("Skipping ops approval (--skip-ops-approval): KYC left pending.")
        await s.get_customer_by_id()            # refresh so kycStatus reflects the approval
        s._print_kyc_verification_summary()

    # --- batch -------------------------------------------------------------------------------

    async def run(self):
        s = self.suite
        s._banner(
            "GOLD LOAN - KYC ONLY",
            f"env={s.env_name.upper()}   kyc={s.kyc_mode_requested}   "
            f"kycType={s.kyc_type}   customers={self.count}   base={s.BASE_URL}",
        )
        try:
            s._log_step("Authentication")
            await s.login(s._get_mobile_number_for_login_type(self.login_type))
            assert s.auth_token, "Login failed: auth_token empty."

            aborted = False
            for i in range(1, self.count + 1):
                if i > 1:
                    self._reset_for_next_customer()
                try:
                    self.results.append(await self.run_one(i))
                except KycDocumentAlreadyExistsError as e:
                    self.results.append({
                        "n": i, "customer_id": s.customer_id, "unique_id": s.customer_unique_id,
                        "kyc_status": None, "mode_requested": s.kyc_mode_requested,
                        "mode_effective": s.kyc_mode_effective, "pan": s.pan_verified,
                        "aadhaar": s.aadhaar_verified, "ovd": s.ovd_verified,
                        "error": str(e).splitlines()[0], "aborted": True,
                    })
                    print(s._c(f"\n{s._G['fail']} Batch stopped: a KYC document is already "
                               f"registered, so the remaining customers would fail the same way.",
                               "red", "bold"))
                    aborted = True
                    break

            passed = self.report(aborted)
            s._print_run_metrics(passed=passed)
            return passed
        except Exception as e:
            s._banner("RESULT - FAILED", f"{type(e).__name__}: {e}")
            if self.results:
                self.report(aborted=True)
            s._print_run_metrics(passed=False)
            raise

    # --- reporting ---------------------------------------------------------------------------

    def report(self, aborted: bool = False) -> bool:
        s = self.suite
        approved = [r for r in self.results if not r["error"]]
        failed = [r for r in self.results if r["error"]]

        print("\n" + s._c(f"{s._G['square']} KYC RESULTS", "bold", "cyan"))
        print(s._c(f"  {'#':<3} {'customer':<12} {'uniqueId':<12} {'status':<12} "
                   f"{'mode':<16} verified", "gray"))
        for r in self.results:
            if r["error"]:
                line = f"  {r['n']:<3} {str(r['customer_id'] or '-'):<12} " \
                       f"{str(r['unique_id'] or '-'):<12} {s._G['fail']} {r['error'][:80]}"
                print(s._c(line, "red"))
                continue
            degraded = r["mode_effective"] != r["mode_requested"]
            mode = r["mode_effective"] + ("(DEGRADED)" if degraded else "")
            verified = ", ".join(
                n for n, v in (("PAN", r["pan"]), ("Aadhaar", r["aadhaar"]), ("OVD", r["ovd"])) if v
            ) or "none (manual data)"
            colour = "yellow" if degraded else "green"
            print(s._c(f"  {r['n']:<3} {str(r['customer_id']):<12} {str(r['unique_id']):<12} "
                       f"{str(r['kyc_status']):<12} {mode:<16} {verified}", colour))

        s._print_summary_table("KYC SUMMARY", {
            "Environment": f"{s.env_name.upper()} ({s.BASE_URL})",
            "KYC mode": s.kyc_mode_requested,
            "KYC type": s.kyc_type,
            "Requested": self.count,
            "Completed": len(approved),
            "Failed": len(failed),
            "Ops approval": "yes" if self.ops_approval else "skipped (left pending)",
        })

        # The whole point of a KYC-only run: hand the customer straight to the loan harness.
        ready = [r for r in approved if r["unique_id"]]
        if ready:
            print("\n" + s._c(f"{s._G['square']} NEXT: create a loan against these customers",
                              "bold", "cyan"))
            for r in ready:
                print(f"  python src/maintest.py --customer {r['unique_id']} --env {s.env_name}")

        passed = bool(self.results) and not failed and not aborted
        s._banner("RESULT - PASSED" if passed else "RESULT - FAILED",
                  f"{len(approved)}/{self.count} customer(s) through KYC "
                  f"({s.kyc_mode_requested} mode)")
        return passed


def main():
    parser = argparse.ArgumentParser(
        prog="test_kyc.py",
        description="Run the complete Gold Loan KYC flow on its own (no loan process).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python src/test_kyc.py                        # manual KYC, one new customer\n"
               "  python src/test_kyc.py --kyc auto             # real PAN + real Aadhaar XML\n"
               "  python src/test_kyc.py --kyc ovd              # real OVD + Form 97, no PAN\n"
               "  python src/test_kyc.py --count 5              # seed 5 KYC'd customers\n"
               "  python src/test_kyc.py --customer MS35QNJP    # Re-KYC an existing customer\n"
               "  python src/test_kyc.py --skip-ops-approval    # leave KYC pending\n"
               "\nA loan against the resulting customer is created with:\n"
               "  python src/maintest.py --customer <uniqueId>\n",
    )
    parser.add_argument("--env", choices=sorted(GoldLoanApiTest.ENVIRONMENTS),
                        default=os.getenv("GOLD_LOAN_ENV", GoldLoanApiTest.DEFAULT_ENV).strip().lower(),
                        help="Target environment (default: %(default)s).")
    parser.add_argument("--kyc", choices=list(GoldLoanApiTest.KYC_MODES),
                        default=os.getenv("GOLD_LOAN_KYC_MODE", GoldLoanApiTest.KYC_MODE_MANUAL),
                        help="Identity mode (default: %(default)s). 'manual' = generated dummy "
                             "PAN/Aadhaar. 'auto' = REAL PAN + REAL offline-Aadhaar XML, verified. "
                             "'ovd' = REAL Voter ID / Driving Licence + Form 97, verified.")
    parser.add_argument("--kyc-profile", metavar="PATH",
                        help="JSON file with the REAL identity data for --kyc auto/ovd "
                             "(default: config/kyc_identity.json).")
    parser.add_argument("--kyc-type", default=os.getenv("GOLD_LOAN_KYC_TYPE", "RE_KYC"),
                        help="kycType sent on the v2 endpoints (default: %(default)s). "
                             "The captured OVD flow used NEW_KYC.")
    parser.add_argument("--customer", metavar="UNIQUE_ID",
                        help="Run KYC against an EXISTING customer (Re-KYC) instead of creating one.")
    parser.add_argument("--count", "-n", type=int, default=1,
                        help="How many new customers to create and KYC (default: %(default)s). "
                             "Only valid in manual mode -- see below.")
    parser.add_argument("--skip-ops-approval", dest="ops_approval", action="store_false",
                        help="Submit the KYC but do not approve it, leaving the record pending.")
    parser.add_argument("--pan-type", choices=["pan", "form60"],
                        help="PAN document type for manual/auto mode (default: pan).")
    parser.add_argument("--login", default="appraiser",
                        help="Role that runs the KYC (default: %(default)s).")
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be at least 1.")
    if args.customer and args.count > 1:
        parser.error("--customer works on a single existing customer; drop --count.")
    # A real document can only ever be registered to ONE customer. Creating a second customer with
    # the same profile would hit "already registered" and abort by design, so refuse it up front
    # rather than burning a customer record to discover that.
    if args.count > 1 and args.kyc != GoldLoanApiTest.KYC_MODE_MANUAL:
        parser.error(
            f"--count {args.count} is not possible with --kyc {args.kyc}: the identity profile "
            f"holds ONE real person's documents, and a document can only be registered against "
            f"one customer. Use --kyc manual to seed several customers, or --count 1.")

    # These are read by the constructor, so set them before it runs.
    os.environ["GOLD_LOAN_ENV"] = args.env
    os.environ["GOLD_LOAN_KYC_MODE"] = args.kyc
    os.environ["GOLD_LOAN_KYC_TYPE"] = args.kyc_type
    if args.kyc_profile:
        os.environ["GOLD_LOAN_KYC_PROFILE"] = args.kyc_profile
    if args.pan_type:
        os.environ["GOLD_LOAN_PAN_TYPE"] = args.pan_type

    suite = GoldLoanApiTest()
    print(f">> Environment: {args.env.upper()} ({suite.BASE_URL})")
    print(f">> KYC mode: {args.kyc}   |  kycType: {args.kyc_type}   |  "
          f"ops approval: {'yes' if args.ops_approval else 'skipped'}")
    if args.customer:
        print(f">> Mode: Re-KYC of existing customer {args.customer}")
    else:
        print(f">> Mode: {args.count} new customer(s)")

    runner = KycOnlyTest(
        suite,
        count=1 if args.customer else args.count,
        existing_customer=args.customer or "",
        ops_approval=args.ops_approval,
        login_type=args.login,
    )

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
