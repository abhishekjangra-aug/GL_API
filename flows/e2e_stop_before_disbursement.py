#!/usr/bin/env python3
"""Run the gold loan journey up to and including the ops rating, then STOP before disbursement.

Same flow and same flags as `maintest.py` -- customer/KYC -> appraiser request -> ornaments ->
scheme -> bank details -> assign packet -> BM rating (> 5L) -> documents -> ops bank
verification -> ops rating -- but the partner approval, disbursement and submit-packet steps are
never run. The loan is left parked at disbursement so it can be inspected or disbursed by hand.

    python src/test_stop_before_disbursement.py
    python src/test_stop_before_disbursement.py --env uat
    python src/test_stop_before_disbursement.py --customer MS35QNJP --amount 550000
    python src/test_stop_before_disbursement.py --count 5

The run ends by printing the customer / loan ids and the `test_resume_loan.py` command that
finishes the loan later.

`maintest.py` is NOT modified. This file subclasses GoldLoanApiTest and overrides only
run_e2e_test (a copy of maintest's flow, cut off after the ops rating); every request body,
signature and step method is still the one defined in maintest.py. maintest's own CLI is reused
by pointing its GoldLoanApiTest name at the subclass for this process only.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402
import jwt  # noqa: E402

import maintest  # noqa: E402


class StopBeforeDisbursementTest(maintest.GoldLoanApiTest):

    async def run_e2e_test(self, login_type: str, skip_login: bool = False):
        """maintest's run_e2e_test, ending right after the ops rating. Returns True when it got there.

        `skip_login=True` reuses the session already on this instance -- used by run_many for
        loans 2..N so a batch logs in once instead of once per loan.
        """
        self._banner(
            "GOLD LOAN - API TEST (STOP BEFORE DISBURSEMENT)",
            f"env={self.env_name.upper()}   login={login_type} "
            f"({self._get_mobile_number_for_login_type(login_type)})   "
            f"base={self.BASE_URL}   log={self.log_level}",
        )
        try:
            self._log_step("Authentication")
            if skip_login and self.auth_token:
                print("Reusing the session from the previous loan (no re-login, no new OTP).")
            elif self.supplied_auth_token:
                # Escape hatch: run against a caller-supplied JWT (GOLD_LOAN_AUTH_TOKEN) instead of
                # logging in. Must carry internalBranchId + id.
                self.auth_token = self.supplied_auth_token
                claims = jwt.decode(self.auth_token, options={"verify_signature": False})
                self.internal_branch_id = str(claims.get("internalBranchId") or "")
                self.logged_in_user_id = claims.get("id")
                assert self.internal_branch_id, "Supplied token did not include internalBranchId."
                print("Using supplied authentication token.")
            else:
                print("Performing fresh login.")
                await self.login(self._get_mobile_number_for_login_type(login_type))
                assert self.auth_token, "Login failed: auth_token is empty after login attempt."

            if self.co_lender_interactive:
                await self.choose_co_lender_interactive()

            # Every run starts by generating its own packet (admin create + assign to the appraiser).
            self._log_step("Generate Packet (admin)")
            await self.prepare_packet()

            if self.existing_customer_id or self.existing_customer_unique_id:
                self._log_step("Load Existing Customer")
                await self._use_existing_customer()
                if await self._existing_customer_kyc_ready():
                    print("Existing customer KYC is already approved — skipping KYC, going to the loan process.")
                    await self._prepare_loan_fields_from_existing()
                else:
                    self._log_step(f"KYC Process (existing customer, status={self.kyc_status or 'unknown'})")
                    await self._run_full_kyc()
            else:
                # Customer creation runs under ADMIN (see maintest.run_e2e_test); KYC and the
                # appraiser-request then run as the appraiser.
                self._log_step("Create Customer / Lead (admin)")
                saved_token = self.auth_token
                try:
                    self.auth_token = await self._role_token("admin")
                    await self.add_customer()
                finally:
                    self.auth_token = saved_token
                self._log_step("KYC Process (appraiser)")
                await self._run_full_kyc()

            self._log_step("Appraiser Request & Loan Basics")
            await self.create_appraiser_request()
            await self.track_loan_history(3)
            await self.fetch_max_loan_limit()
            await self.fetch_customer_loan_details()
            await self.store_loan_basic_details()
            await self.fetch_rating_reasons()
            await self.store_nominee_details()
            await self.store_ornament_details()

            self._log_step("Scheme, Balance & Loan-Type Checks")
            await self.fetch_loan_balance()
            await self.fetch_co_lender_banks()
            await self.check_loan_type()
            await self.get_interest_rate()
            await self.generate_interest_table()
            await self.get_final_loan_details()

            self._log_step("Bank Details")
            await self.fetch_bank_details()
            await self.validate_account()   # penny-drop must precede bank-details
            await self.store_bank_details()
            await self.add_appraiser_rating()

            self._log_step("Assign Packet")
            await self.add_packet_images()   # admin create/assign + appraiser seal
            await self.update_loan_lock()
            await self.fetch_loan_stages()
            await self.fetch_applied_loan_details()

            self._log_step("BM Rating (only if loan amount > 5L)")
            await self.submit_bm_rating_if_required()

            self._log_step("Upload Documents")
            await self.store_loan_documents()

            self._log_step("Manual Bank Verification (ops approval)")
            await self.ops_manual_bank_verification()

            self._log_step("Ops Rating (final approval)")
            await self.submit_ops_rating()   # Ops login (per env)
            await self.fetch_loan_stages()
            await self.fetch_applied_loan_details()

            # STOP: no partner approval, no disbursement, no submit-packet.
            self._print_summary_table("LOAN STOPPED BEFORE DISBURSEMENT", {
                "Environment": f"{self.env_name.upper()} ({self.BASE_URL})",
                "Customer Unique ID": self.customer_unique_id,
                "Master Loan ID": self.master_loan_id,
                "Loan ID": self.loan_id,
                "Final Loan Amount": self.final_loan_amount,
                "Resume with": (f"python src/test_resume_loan.py --env {self.env_name} "
                                f"--master-loan-id {self.master_loan_id} --loan-id {self.loan_id}"),
            })
            self._banner(
                "RESULT - PASSED (STOPPED BEFORE DISBURSEMENT)",
                f"GL process run through ops rating for login={login_type}",
            )
            self._print_run_metrics(passed=True)
            return True

        except httpx.HTTPStatusError as e:
            self._banner("RESULT - FAILED (HTTP ERROR)", f"login={login_type}")
            print(self._c(f"  {e.request.method} {e.request.url}", "red", "bold"))
            print(self._c(f"  status : {e.response.status_code} {e.response.reason_phrase}", "red"))
            print(self._c(f"  body   : {self._short_error_body(e.response.text, 600)}", "red"))
            self._print_run_metrics(passed=False)
            raise
        except Exception as e:
            self._banner("RESULT - FAILED", f"login={login_type}")
            print(self._c(f"  {type(e).__name__}: {e}", "red", "bold"))
            self._print_run_metrics(passed=False)
            raise


if __name__ == "__main__":
    # maintest.main() builds its suite from the module-level GoldLoanApiTest name; point that at
    # the subclass so the full CLI (--env, --customer, --count, ...) is reused unchanged.
    maintest.GoldLoanApiTest = StopBeforeDisbursementTest
    print(">> Stop point: after ops rating (no partner approval / disbursement / submit-packet).")
    maintest.main()
