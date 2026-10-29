#!/usr/bin/env python3
"""Run the gold loan journey up to the upload-documents page, then STOP.

Same flow and same flags as `gold_loan/harness.py` -- customer/KYC -> appraiser request -> ornaments ->
scheme -> bank details -> appraiser rating -> assign packet -> BM rating (>= 5L) -- but the
loan documents are never uploaded, and nothing after that runs (ops bank verification, ops
rating, disbursement, submit-packet). The loan is left parked at "upload documents".

    python flows/e2e_stop_before_documents.py
    python flows/e2e_stop_before_documents.py --env uat
    python flows/e2e_stop_before_documents.py --customer MS35QNJP --amount 550000
    python flows/e2e_stop_before_documents.py --count 5

Every run creates a NEW appraiser request. An existing request is never cancelled and never
resumed: with --customer, if that customer already has one, a new request is tried in another
branch, and if none accepts it the run stops with a clear error. The run ends by printing the
customer / loan ids.

`gold_loan/harness.py` is NOT modified. This file subclasses GoldLoanApiTest and overrides only
create_appraiser_request (create-only, see above) and run_e2e_test (a copy of the harness's flow,
cut off before Upload Documents); every request body,
signature and step method is still the one defined in gold_loan/harness.py. the harness's own CLI is reused
by pointing its GoldLoanApiTest name at the subclass for this process only.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # project root

import httpx  # noqa: E402
import jwt  # noqa: E402

from gold_loan import harness  # noqa: E402


class StopBeforeDocumentsTest(harness.GoldLoanApiTest):

    async def create_appraiser_request(self):
        """Always CREATE a new appraiser request -- never cancel, never resume an existing one.

        Replaces the harness's version, which on "already Exists" resumes the existing request or
        cancels its loan. Here: POST a new request; if the server refuses because the customer
        already has one, try a new request in another branch this login can use (non-destructive);
        if none accepts it, stop with a clear error. Nothing existing is ever cancelled or reused.
        """
        api_path = "/api/appraiser-request"
        await self._fetch_appraisers()
        request_body = {
            "id": None,
            "customerId": int(self.customer_id) if self.customer_id else None,
            "customerName": self.full_name,
            "customerUniqueId": self.customer_unique_id,
            "mobileNumber": self.mobile_number,
            "moduleId": int(self.module_id) if self.module_id else None,
            "internalBranchId": await self.appraiser_request_branch_id(),  # the customer's branch
            "appraiserId": int(self.appraiser_id) if self.appraiser_id else None,
            "loanType": "Fresh Loan",
            "trackProccesingTime": True,
        }
        try:
            response = await self._make_authenticated_request('POST', api_path, json_data=request_body)
        except httpx.HTTPStatusError as e:
            if not (e.response.status_code == 400 and "already Exists" in (e.response.text or "")):
                raise
            print(f"Customer {self.customer_unique_id} already has an appraiser request; NOT cancelling "
                  "or resuming it -- trying a new request in another branch.")
            response = await self._create_request_in_another_branch(request_body)
            if response is None:
                raise RuntimeError(
                    f"Customer {self.customer_unique_id} already has an open appraiser request and no "
                    "other branch accepted a new one" + self._branch_attempt_summary() + ". This "
                    "runner never cancels or resumes a request: close that request in the app, or run "
                    "with a different --customer (or none, to create a new customer)."
                ) from e

        resp_json = response.json()
        self._print_json_block("Create Appraiser Request Response", resp_json)
        data = resp_json.get('data') if isinstance(resp_json, dict) else None
        parsed_id = str((data.get('id') if isinstance(data, dict) else None) or resp_json.get('id') or '')
        # The new request has no loan yet; never carry over ids from anything looked up before.
        self.loan_id = ''
        self.master_loan_id = ''
        # The create response can echo the CUSTOMER id instead of the request id (see harness).
        if parsed_id == str(self.customer_id):
            parsed_id = ''
        self.appraiser_request_id = parsed_id
        if not self.appraiser_request_id:
            # Create response did not carry the id: resolve it from view-all, as harness does.
            await self._fetch_existing_appraiser_request(required=False)
            self.loan_id = ''
            self.master_loan_id = ''
        assert self.appraiser_request_id, f"Could not determine appraiser request id from: {resp_json}"
        print(f'New appraiser request created. Appraiser Request ID: {self.appraiser_request_id}')

    async def run_e2e_test(self, login_type: str, skip_login: bool = False):
        """the harness's run_e2e_test, ending right before Upload Documents. Returns True when it got there.

        `skip_login=True` reuses the session already on this instance -- used by run_many for
        loans 2..N so a batch logs in once instead of once per loan.
        """
        self._banner(
            "GOLD LOAN - API TEST (STOP BEFORE UPLOAD DOCUMENTS)",
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
                # Customer creation runs under ADMIN (see harness.run_e2e_test); KYC and the
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

            self._log_step("BM Rating (only if loan amount >= 5L)")
            await self.submit_bm_rating_if_required()  # BM login (per env)

            # STOP: no loan documents, ops verification/rating, disbursement or submit-packet.
            self._print_summary_table("LOAN STOPPED BEFORE UPLOAD DOCUMENTS", {
                "Environment": f"{self.env_name.upper()} ({self.BASE_URL})",
                "Customer Unique ID": self.customer_unique_id,
                "Master Loan ID": self.master_loan_id,
                "Loan ID": self.loan_id,
                "Loan Unique ID": self.loan_unique_id,
                "Final Loan Amount": self.final_loan_amount,
            })
            self._banner(
                "RESULT - PASSED (STOPPED BEFORE UPLOAD DOCUMENTS)",
                f"GL process run through BM rating for login={login_type}",
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
    # harness.main() builds its suite from the module-level GoldLoanApiTest name; point that at
    # the subclass so the full CLI (--env, --customer, --count, ...) is reused unchanged.
    harness.GoldLoanApiTest = StopBeforeDocumentsTest
    print(">> Stop point: before upload documents (no documents / ops / disbursement / submit-packet).")
    print(">> Appraiser request: always NEW (an existing request is never cancelled or resumed).")
    harness.main()
