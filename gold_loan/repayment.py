"""Repayment scenarios on a DISBURSED gold loan: renewal, renewal + top-up, top-up, part release and
full release.

Every request mirrors the web portal (Angular bundle, read as text only):

  * PartReleaseComponent      -- the repayment screen for all five scenarios
                                 (/admin/repayment/{renew,top-up,part-release,full-release}/<masterLoanId>)
  * JewelleryReleaseService   -- its endpoints (jewellery-release, renewal, top-up)
  * PaymentDialogComponent    -- the OFFLINE payment (NEFT / IMPS / RTGS with a UTR)
  * LoanConsentDialogComponent-- the customer's consent OTP for renewal / top-up
  * PaymentPreviewComponent   -- top-up: loan-preview -> create-new-loan -> final-loan-details
  * UpdateStatusComponent     -- ops: amount approval, appraiser / releaser release status
  * AssignAppraiserComponent  -- assign the appraiser (renewal, part release) / releaser (full release)
  * UpdateLocationComponent   -- packet release: collect from the partner branch, hand over to the customer
  * UploadDocumentComponent   -- the release documents (customer acknowledgement)

What each scenario does, end to end:

  renewal / renewal-topup
      GET get-top-up-scheme -> POST renewal/amount -> consent OTP -> POST renewal/submit (interest paid
      offline) -> ops: amount-status 'completed' -> assign appraiser -> GET renewal/apply-loan (the
      server opens the NEW loan) -> the new loan is driven to completion by the resume machinery.
  topup
      quick-pay the accrued interest when the server says it is due -> GET get-top-up-scheme ->
      GET re-calculate -> consent OTP -> GET top-up/loan-preview -> POST top-up/create-new-loan ->
      POST final-loan-details (the body the server hands back) -> the new loan is driven to completion.
  part-release
      GET partner-scheme-amount (new-loan scheme) -> POST jewellery-release (summary) ->
      POST jewellery-release/part-release (paid offline) -> ops: amount-status 'completed' ->
      assign appraiser -> packet collect + customer hand-over -> appraiser-status 'released' ->
      document -> GET jewellery-release/apply-loan (NEW loan on the remaining ornaments) -> completion.
  full-release
      POST jewellery-release (summary) -> POST jewellery-release/full-release (paid offline) ->
      ops: full-release/amount-status 'completed' -> assign releaser -> packet collect + customer
      hand-over -> releaser-status 'released' -> full-release/document. The loan is closed.

Money never moves through a payment gateway: the portal's OFFLINE payment is used (the same
NEFT / IMPS / RTGS choice as disbursement, `--transfer-mode`). ICICI / RazorpayX are not touched.
"""

import datetime
import os
import urllib.parse

import httpx


class RepaymentScenarioError(RuntimeError):
    """A scenario cannot continue -- the message says why, in the portal's terms."""


class RepaymentScenarios:
    """Drives one repayment scenario on one disbursed loan, through a logged-in GoldLoanApiTest.

    Wraps the suite rather than subclassing it, like the other runners: signing, encryption,
    role logins and every new-loan step stay defined once in gold_loan/harness.py.
    """

    SCENARIOS = ("renewal", "renewal-topup", "topup", "part-release", "full-release")

    # get-top-up-scheme's schemeFor, per scenario (the portal's renewalType radio / 'topup').
    SCHEME_FOR = {"renewal": "renewal", "renewal-topup": "renewalPlusTopUp", "topup": "topup"}
    # LoanConsentDialogComponent transactionType.
    CONSENT_TYPE = {"renewal": "Renewal", "renewal-topup": "Renewal + Top Up", "topup": "Top Up"}
    # Loan lock stage the approval lists take before opening UpdateStatusComponent.
    LOCK_STAGE = {"renewal": "renewLoan", "renewal-topup": "renewLoan",
                  "part-release": "partRelease", "full-release": "fullRelease"}
    CONSENT_OTP = os.getenv("GOLD_LOAN_CONSENT_OTP", "123456")   # 6-digit admin OTP, as kycConsent
    CUSTOMER_HOME_LOCATION_ID = 7    # UpdateLocationComponent: isCustomerHomeIn keeps location 7 only

    def __init__(self, suite, scenario, approver_role=None):
        if scenario not in self.SCENARIOS:
            raise ValueError(f"Unknown scenario {scenario!r}; use one of {self.SCENARIOS}.")
        self.suite = suite
        self.scenario = scenario
        # Who approves the amount and assigns the appraiser (the funds-approvals screens).
        self.approver_role = (approver_role or os.getenv("GOLD_LOAN_REPAYMENT_APPROVER") or "ops").lower()
        self.master_loan_id = ""
        self.loan = {}             # jewellery-release/<id> -> customerLoan (the parent loan)
        self.release_info = {}     # the whole jewellery-release/<id> answer (isPayableAmount, ...)
        self.purposes = {}         # /api/purpose, grouped by purpose type
        self.wanted_purpose_type = None   # the new loan's purpose type (None = any)
        self.scheme = {}           # the chosen scheme (top-up list entry or partner scheme)
        self.summary = {}          # renewal/amount or jewellery-release answer (loanInfo, calculationData, ...)
        self.loan_consent = {}
        self.request_id = None     # loanRenewalId / partReleaseId / fullReleaseId
        self.request_row = {}      # the request as the approval list shows it
        self.new_loan = {}         # {loanId, masterLoanId, ...} of the loan a renewal/top-up/part release opens
        self._selected_ornaments = []   # the ornaments this scenario acts on
        # A finished request of THIS scenario whose new loan may still be in progress: the re-run
        # only drives that loan to completion (and is refused if it is complete too).
        self.child_only = False
        self.result = {}           # the closing summary

    # ------------------------------------------------------------------ plumbing

    @staticmethod
    def _q(path, params):
        """Path with its query string. The HMAC signs the path INCLUDING the query, so a GET's
        parameters must be in the path, never in httpx params= (a signature mismatch is a 401
        'auth failed' that also ends the server-side session)."""
        params = {k: v for k, v in params.items() if v is not None}
        return f"{path}?{urllib.parse.urlencode(params)}" if params else path

    async def _get(self, path, **params):
        response = await self.suite._make_authenticated_request("GET", self._q(path, params))
        return response.json()

    async def _post(self, path, body):
        response = await self.suite._make_authenticated_request("POST", path, json_data=body)
        return response.json()

    async def _put(self, path, body):
        response = await self.suite._make_authenticated_request("PUT", path, json_data=body)
        return response.json()

    async def _as_role(self, role, call):
        """Run `call()` under another role's token, restoring the current one afterwards."""
        suite = self.suite
        saved = suite.auth_token
        suite.auth_token = await suite._role_token(role)
        try:
            return await call()
        finally:
            suite.auth_token = saved

    async def _as_approver(self, what, call):
        """The funds-approvals screens (amount approval, appraiser assignment). Runs as
        `approver_role` (default ops) and falls back to admin when that role is refused."""
        try:
            return await self._as_role(self.approver_role, call)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (401, 403) or self.approver_role == "admin":
                raise
            print(f"{what}: {self.approver_role} was refused ({e.response.status_code}); retrying as admin.")
            return await self._as_role("admin", call)

    @staticmethod
    def _num(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _id_from(answer, key):
        """The request id a submit answer carries, if any (else find_request looks it up).
        renewal/submit answers {message, renewealLoan: {id, masterLoanId, ...}} (sic)."""
        if not isinstance(answer, dict):
            return None
        if answer.get(key):
            return answer[key]
        for value in answer.values():
            if isinstance(value, dict) and value.get("id") and "masterLoanId" in value:
                return value["id"]
        return None

    @staticmethod
    def _js_number(value):
        """A number as JavaScript prints it in a template string (296 not 296.0)."""
        value = float(value)
        return str(int(value)) if value.is_integer() else repr(value)

    @staticmethod
    def _js_date_now():
        """PaymentDialogComponent.getOffsetDateTime(new Date()) as JSON.stringify writes it:
        local wall-clock time labelled UTC."""
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def _log(self, title):
        self.suite._log_step(title)

    # ------------------------------------------------------------------ the parent loan

    async def load_loan(self, master_loan_id):
        """GET jewellery-release/<masterLoanId>: the repayment screen's loan (ornaments, outstanding,
        co-lender permissions, maturity, DPD, isPayableAmount)."""
        self.master_loan_id = str(master_loan_id)
        info = await self._get(f"/api/jewellery-release/{self.master_loan_id}")
        loan = info.get("customerLoan") if isinstance(info, dict) else None
        if not isinstance(loan, dict) or not loan.get("loanOrnamentsDetail"):
            raise RepaymentScenarioError(
                f"jewellery-release/{self.master_loan_id} returned no loan with ornaments - is it disbursed?")
        self.release_info, self.loan = info, loan
        customer = loan.get("customer") or {}
        suite = self.suite
        suite.master_loan_id = self.master_loan_id
        suite.customer_id = str(loan.get("customerId") or customer.get("id") or suite.customer_id or "")
        # The screen masks the customer (customerUniqueId null, mobile XXXXXX…); the request row and
        # the customer record carry the real values.
        suite.customer_unique_id = customer.get("customerUniqueId") or suite.customer_unique_id
        print(f"Parent loan {self.master_loan_id}: outstanding {loan.get('outstandingAmount')}, "
              f"{len(loan['loanOrnamentsDetail'])} ornament(s), ends {loan.get('loanEndDate')}, "
              f"DPD {loan.get('dpdDays')}, payable now: {info.get('isPayableAmount')}, "
              f"co-lender: {(loan.get('loanCoLenderBank') or {}).get('bankName') or 'none'}.")
        self._check_allowed()
        return loan

    async def _customer_unique_id(self):
        """The customer's unique id (apply-loan's path): from the request row, else the record."""
        if not self.suite.customer_unique_id:
            await self.suite.get_customer_by_id()
        if not self.suite.customer_unique_id:
            raise RepaymentScenarioError(f"Could not read customer {self.suite.customer_id}'s unique id.")
        return self.suite.customer_unique_id

    def _post_maturity(self):
        end = self.loan.get("loanEndDate")
        return bool(end) and datetime.date.today() > datetime.date.fromisoformat(str(end)[:10])

    def _check_allowed(self):
        """The portal's own guards (patchValuePartRelease / scheme / show), so a run fails with the
        portal's reason instead of a server 400 further on."""
        loan, scenario = self.loan, self.scenario
        perms = (loan.get("loanCoLenderBank") or {}).get("releasePermissions") or {}
        blocked = {
            "renewal": perms and not perms.get("isRenewalAllowed", True),
            "renewal-topup": perms and not perms.get("isRenewalAllowed", True),
            "topup": perms and not perms.get("isTopUpAllowed", True),
            "part-release": perms and not perms.get("isPartReleaseAllowed", True),
        }
        if blocked.get(scenario):
            raise RepaymentScenarioError(f"The loan's co-lender does not allow {scenario}.")
        if scenario == "topup" and (self._post_maturity() or self._num(loan.get("dpdDays")) > 1):
            raise RepaymentScenarioError("Top-up is blocked after maturity or with DPD over 1 day.")
        if scenario.startswith("renewal") and not loan.get("isRenewalBeforeMaturity") \
                and not self._post_maturity():
            raise RepaymentScenarioError("The server does not allow renewing this loan yet "
                                         "(isRenewalBeforeMaturity is false).")
        if scenario == "part-release":
            if len(loan["loanOrnamentsDetail"]) < 2:
                raise RepaymentScenarioError("Part release needs at least 2 ornaments (one is released, "
                                             "the rest stay on the new loan).")
            if self._post_maturity() or len(loan.get("customerLoan") or []) == 2:
                raise RepaymentScenarioError("Part release is not offered after maturity or on a "
                                             "secured + unsecured loan.")

    # ------------------------------------------------------------------ shared building blocks

    async def fetch_purposes(self):
        """GET /api/purpose?from=1&to=-1&search= (getPurposeInfo): purposes grouped by type."""
        if not self.purposes:
            body = await self._get("/api/purpose", **{"from": 1, "to": -1, "search": ""})
            self.purposes = body.get("data") or {}
        return self.purposes

    async def resolve_purpose_type(self):
        """The purpose type the new loan must have, from --purpose (its own type) or
        --purpose-type (cb / igl); None for 'any'. Resolved BEFORE the scheme is chosen, so only
        schemes allowing that type are considered (a scheme lists them in loanPurposeType)."""
        await self.fetch_purposes()
        suite = self.suite
        types = {"cb": "Consumption Based", "igl": "Income Generating"}
        pinned = (suite.purpose_choice or "").strip()
        if pinned.lower() in tuple(types) + ("any",):     # `--purpose cb` is a type, as in the harness
            suite.purpose_type_choice, suite.purpose_choice, pinned = pinned.lower(), "", ""
        if pinned:
            for purpose_type, items in self.purposes.items():
                if any(str(i.get("name", "")).strip().lower() == pinned.lower() for i in items or []):
                    self.wanted_purpose_type = purpose_type
                    break
            else:
                raise RepaymentScenarioError(f"No loan purpose named {pinned!r}. Available: "
                                             + "; ".join(f"{i.get('name')} ({t})" for t, items in
                                                         self.purposes.items() for i in items or []))
        else:
            choice = (suite.purpose_type_choice or "cb").lower()
            self.wanted_purpose_type = types.get(choice) if choice != "any" else None
        print(f"New loan purpose type: {self.wanted_purpose_type or 'any'}"
              + (f" (purpose {pinned!r})" if pinned else ""))
        return self.wanted_purpose_type

    def scheme_fits_purpose(self, scheme):
        return self.suite.scheme_allows_purpose(scheme, self.wanted_purpose_type)

    def pick_purpose(self, scheme):
        """setPurposeList(scheme): only the scheme's loanPurposeType groups are offered, narrowed
        to the requested type; the purpose is the pinned one, else (IGL) the preferred IGL purpose,
        else a random one -- as the new-loan basic details choose it. Never switches type."""
        allowed = list((scheme or {}).get("loanPurposeType") or self.purposes.keys())
        if self.wanted_purpose_type:
            allowed = [t for t in allowed if t.lower() == self.wanted_purpose_type.lower()]
        if not allowed:
            raise RepaymentScenarioError(f"Scheme {scheme.get('id')} does not allow purpose type "
                                         f"{self.wanted_purpose_type!r} (it takes {scheme.get('loanPurposeType')}).")
        pool = [(i, t) for t in allowed for i in self.purposes.get(t) or []
                if isinstance(i, dict) and i.get("name") and i.get("isActive", True) is not False]
        pinned = (self.suite.purpose_choice or "").strip().lower()
        if pinned:
            pool = [(i, t) for i, t in pool if str(i["name"]).strip().lower() == pinned]
        if not pool:
            raise RepaymentScenarioError(f"No loan purpose available for {allowed}"
                                         + (f" named {self.suite.purpose_choice!r}" if pinned else "") + ".")
        preferred = [(i, t) for i, t in pool if t == "Income Generating"
                     and str(i["name"]).strip().lower() == self.suite.IGL_PURPOSE.strip().lower()]
        purpose, purpose_type = preferred[0] if preferred else self.suite.random.choice(pool)
        return purpose["name"], purpose_type

    def offline_payment(self, amount):
        """PaymentDialogComponent.getRawValue() for an OFFLINE NEFT / IMPS / RTGS payment: the
        bank fields and a UTR are required, every cash / cheque field is null."""
        suite = self.suite
        return {
            "paymentType": suite.transfer_mode.upper(),
            "bankName": suite.bank_name or "State Bank of India",
            "branchName": suite.bank_branch_name or "Main Branch",
            "transactionId": f"UTR{suite.random.randint(10**11, 10**12 - 1)}",
            "depositDate": self._js_date_now(),
            "paidAmount": round(self._num(amount), 2),
            "chequeNumber": None, "depositTransactionId": None, "paymentReceivedDate": None,
            "depositStatus": None, "cashReceipt": None, "cashDeclrationReceipt": None,
            "cashReceiptUrl": None, "cashDeclrationReceiptUrl": None, "cashReceiptNumber": None,
            "receivedAmountForBranchId": None, "splitOtherAmount": None, "cashSplitAmount": None,
            "isSigned": False, "referenceId": None,
        }

    async def loan_consent_otp(self, consent):
        """LoanConsentDialogComponent: customer-otp/send-otp {type:'loanConsent', ...} then
        verify-otp-admin with the 6-digit OTP and the consentInfo the confirm step audits."""
        self._log(f"Loan consent OTP ({consent['transactionType']})")
        sent = await self._post("/api/customer-otp/send-otp", {
            "type": "loanConsent", "masterLoanId": consent["masterLoanId"],
            "transactionType": consent["transactionType"], "newLoanAmount": consent["newLoanAmount"],
            "interestRate": consent["interestRate"], "tenure": consent["tenure"],
        })
        reference = sent.get("referenceCode")
        if not reference:
            raise RepaymentScenarioError(f"Consent send-otp returned no referenceCode: {sent}")
        verified = await self._post("/api/customer-otp/verify-otp-admin", {
            "type": "loanConsent", "referenceCode": reference, "otp": self.CONSENT_OTP,
            "masterLoanId": consent["masterLoanId"],
            "consentInfo": {k: consent[k] for k in ("transactionType", "newLoanAmount",
                                                    "interestRate", "tenure", "schemeId")},
        })
        self.loan_consent = verified.get("loanConsent") or {}
        print(f"Consent recorded (active: {self.loan_consent.get('isActive', 'n/a')}).")
        return self.loan_consent

    @staticmethod
    def _highest_rate(scheme):
        """getInteresRate: the LAST schemeInterest entry's rate."""
        rates = (scheme or {}).get("schemeInterest") or []
        return rates[-1].get("interestRate") if rates else None

    def _co_lender_id(self, scheme=None):
        """The portal only carries a co-lender forward when it is bank 2 or 4 (Godrej / DCB)."""
        bank = ((scheme or {}).get("loanCoLenderBank") or self.loan.get("loanCoLenderBank") or {})
        return bank.get("id") if bank.get("id") in (2, 4) else None

    def _pay_object(self, scheme_id, top_up, processing_charge, actual_processing_charge, rpg,
                    payment, purpose=None, purpose_type=None, co_lender_id=None):
        """PartReleaseComponent.apiCall's payObject: the summary's dues, the new scheme's charges,
        the payment, then the summary's ornamentWeight block on top (Object.assign order)."""
        info = self.summary.get("loanInfo") or {}
        stamp = self.summary.get("stampDuty") or {}
        body = {
            "ornamentId": self._ornament_ids, "masterLoanId": int(self.master_loan_id),
            "interestAmount": info.get("interestAmount"), "penalInterest": info.get("penalInterest"),
            "payableAmount": info.get("totalPayableAmount"), "transactionDetails": None,
            "isFromPacketTransfer": False, "schemeId": scheme_id, "topupAmount": top_up,
            "processingCharges": processing_charge, "upfrontInterest":
                (self.summary.get("calculationData") or {}).get("upfrontInterestAmount", 0),
            "rpg": rpg, "actualProcessingCharge": actual_processing_charge,
            "stampDutyCharges": stamp.get("stampDutyCharges"),
            "stampDutyDefinitionType": stamp.get("stampDutyDefinitionType"),
            "stampDutyPercent": stamp.get("stampDutyPercent"),
            "isSigned": payment.get("isSigned", False), "coLenderBankId": co_lender_id,
        }
        if purpose:
            body["loanPurpose"] = purpose
        if purpose_type:
            body["loanPurposeType"] = purpose_type
        body.update(payment)
        body.update(self.summary.get("ornamentWeight") or {})
        return body

    def _calc_charges(self, calculation):
        """patchDatas(calculationData): the processing charge the form shows (2dp number) and the
        actual one (a 2dp STRING -- toFixed), plus rpg from the scheme."""
        scheme = calculation.get("securedScheme") or {}
        return (round(self._num(calculation.get("securedprocessingCharge")), 2),
                f"{self._num(calculation.get('actualProcessingCharge')):.2f}",
                scheme.get("rpg"))

    # ------------------------------------------------------------------ the approval half

    REQUEST_LISTS = {"renewal": "/api/renewal", "renewal-topup": "/api/renewal",
                     "part-release": "/api/jewellery-release/part-release",
                     "full-release": "/api/jewellery-release/full-release"}
    OPEN_STATUSES = ("pending", "completed")   # amountStatus of a request still in progress

    async def _scan_requests(self):
        path = self.REQUEST_LISTS[self.scenario]
        for page in range(1, 9):
            body = await self._get(path, **{"from": (page - 1) * 25 + 1, "to": page * 25})
            rows = body.get("data") or []
            for row in rows:
                if str(row.get("masterLoanId")) != self.master_loan_id:
                    continue
                if self.request_id is not None and str(row.get("id")) != str(self.request_id):
                    continue
                return row
            if len(rows) < 25:
                return None
        return None

    async def find_request(self, required=True):
        """The request as the funds-approvals list shows it (the id the approval / assignment
        bodies need, plus its transaction). The lists are BRANCH-scoped (TEST: ops and the
        appraiser sit in branch 33 and do not see a branch-1 loan's request), so a request the
        approver cannot see is looked up as admin, and admin then approves it."""
        row = await self._as_approver("Request list", self._scan_requests)
        if row is None and self.approver_role != "admin":
            row = await self._as_role("admin", self._scan_requests)
            if row is not None:
                print(f"The {self.approver_role} login cannot see this branch's request; "
                      "approving as admin.")
                self.approver_role = "admin"
        if row is None:
            if not required:
                return None
            raise RepaymentScenarioError(f"The {self.scenario} request for master loan "
                                         f"{self.master_loan_id} is not in {self.REQUEST_LISTS[self.scenario]}.")
        self.request_row, self.request_id = row, row.get("id")
        customer = ((row.get("masterLoan") or {}).get("customer") or {})
        if customer.get("customerUniqueId"):
            # jewellery-release/<id> masks it (null); apply-loan needs it in the path.
            self.suite.customer_unique_id = customer["customerUniqueId"]
        return row

    # Which approval list records each scenario's request (a top-up has none: it opens the new
    # loan directly).
    SCENARIO_KIND = {"renewal": "renewal", "renewal-topup": "renewal", "part-release": "part release",
                     "full-release": "full release", "topup": None}
    HISTORY_LISTS = (("renewal", "/api/renewal"),
                     ("part release", "/api/jewellery-release/part-release"),
                     ("full release", "/api/jewellery-release/full-release"))

    @staticmethod
    def _request_state(row, kind):
        """'finished' (the scenario went through), 'open' (placed, not done yet) or 'void'
        (rejected: it never happened, so it does not use the loan up).

        A renewal is through once the renewed loan exists. A release is through only when the
        ornaments are 'released' AND the release document is on file: the server opens a part
        release's new loan (newLoanId) already at the customer hand-over, so newLoanId alone
        does not mean done (live TEST, part release 1823)."""
        if str(row.get("amountStatus") or "").lower() == "rejected":
            return "void"
        if kind == "renewal":
            done = row.get("isRenewalLoanCreated") or str(row.get("renewalStatus")).lower() == "renewed"
        else:
            status = str(row.get("partReleaseStatus") or row.get("fullReleaseStatus") or "").lower()
            done = status == "released" and bool(row.get("documents"))
        return "finished" if done else "open"

    async def _parent_history(self):
        """Every renewal / part-release / full-release request ever placed on this parent loan.
        Read as admin: the lists are branch-scoped and admin sees every branch."""
        async def scan():
            found = []
            for kind, path in self.HISTORY_LISTS:
                for page in range(1, 9):
                    rows = (await self._get(path, **{"from": (page - 1) * 25 + 1, "to": page * 25})
                            ).get("data") or []
                    found += [(kind, r) for r in rows if str(r.get("masterLoanId")) == self.master_loan_id]
                    if len(rows) < 25:
                        break
            return found
        return await self._as_role("admin", scan)

    async def claim_parent(self):
        """A parent loan carries ONE scenario. Returns the open request of THIS scenario to finish
        (a re-run after a failure), None when the loan is untouched, and refuses everything else:
        a scenario that already went through, an open request of a different scenario, or a loan
        with nothing outstanding (closed, or carried into the loan a top-up / renewal opened)."""
        mine = self.SCENARIO_KIND[self.scenario]
        resume = None
        for kind, row in await self._parent_history():
            state = self._request_state(row, kind)
            if state == "void":
                continue
            if state == "open" and kind == mine and resume is None:
                resume = row
                continue
            if state == "finished" and kind == mine and row.get("newLoanId") and resume is None \
                    and self.scenario != "full-release":
                # The parent's part is done, but the scenario also covers the loan it opened.
                resume, self.child_only = row, True
                continue
            produced = row.get("newLoanId")
            raise RepaymentScenarioError(
                f"Master loan {self.master_loan_id} already has a {kind} request {row.get('id')} "
                f"({'completed' if state == 'finished' else 'in progress'}). A parent loan can be used "
                "for one scenario only - finish that one, or use the loan it produced / a new loan."
                + (f" The loan it produced is master loan {produced} (finish it with "
                   f"flows/resume_loan.py if it is still in progress)." if produced else ""))
        if resume is None and self._num(self.loan.get("outstandingAmount"), 1) <= 0:
            raise RepaymentScenarioError(
                f"Master loan {self.master_loan_id} has nothing outstanding: it is closed or was carried "
                "into a renewal / top-up loan. Use that loan, or a new one.")
        if resume is not None:
            self.request_row, self.request_id = resume, resume.get("id")
            customer = ((resume.get("masterLoan") or {}).get("customer") or {})
            if customer.get("customerUniqueId"):
                self.suite.customer_unique_id = customer["customerUniqueId"]
            if self.child_only:
                print(f"The {mine} request {resume.get('id')} went through; finishing the loan it "
                      f"opened (master loan {resume.get('newLoanId')}).")
            else:
                print(f"Resuming the open {mine} request {resume.get('id')} "
                      f"(amount {resume.get('amountStatus')}) placed earlier on this loan.")
        return resume

    async def resume_open_request(self):
        """The open request of this scenario to carry on with, or None to place a new one."""
        return await self.claim_parent()

    async def approve_amount(self):
        """UpdateStatusComponent ('renewLoan' / 'jewelleryReleaseApproval' / 'fullReleaseApproval'):
        lock the loan, then amountStatus 'completed'. form.value carries only the enabled
        controls: the request id, amountStatus and utrNumber."""
        self._log("Approve the paid amount (funds approval)")
        row = await self.find_request()
        id_key, path = {
            "renewal": ("loanRenewalId", "/api/renewal/amount-status"),
            "renewal-topup": ("loanRenewalId", "/api/renewal/amount-status"),
            "part-release": ("partReleaseId", "/api/jewellery-release/amount-status"),
            "full-release": ("fullReleaseId", "/api/jewellery-release/full-release/amount-status"),
        }[self.scenario]
        if str(row.get("amountStatus")) == "completed":
            print(f"Amount already approved for {id_key}={self.request_id}.")
            return
        utr = (row.get("transaction") or {}).get("utrNumber")

        async def approve():
            lock = await self._post("/api/loan-process/update-loan-lock", {
                "masterLoanId": int(self.master_loan_id), "loanStageId": self.LOCK_STAGE[self.scenario]})
            if lock.get("message") != "Success":
                raise RepaymentScenarioError(f"Loan lock refused: {lock}")
            return await self._put(path, {id_key: self.request_id, "amountStatus": "completed",
                                          "utrNumber": utr})
        answer = await self._as_approver("Amount approval", approve)
        print(f"Amount approved ({id_key}={self.request_id}): {answer.get('message', answer)}")

    async def assign_appraiser(self):
        """AssignAppraiserComponent 'add': renewal / part release assign an APPRAISER; full release
        a RELEASER (an appraiser may stand in: changeAppraiserToReleaser moves the id into
        releaserId and disables appraiserId, so form.value drops it)."""
        self._log("Assign " + ("releaser" if self.scenario == "full-release" else "appraiser"))
        row = await self.find_request()
        if row.get("isAppraiserAssigned") or row.get("isReleaserAssigned") or row.get("releaserId"):
            print(f"Already assigned on request {self.request_id}.")
            return
        customer = self.loan.get("customer") or {}
        appraiser_id = self.suite.logged_in_user_id
        body = {
            "id": None, "customerUniqueId": customer.get("customerUniqueId"),
            "customerId": self.loan.get("customerId"),
            "customerName": f"{customer.get('firstName', '')} {customer.get('lastName', '')}".strip(),
            "appoinmentDate": None, "loanUniqueId": None, "startTime": None, "endTime": None,
            "partReleaseId": None, "fullReleaseId": None, "loanAuctionId": None,
            "internalBranchId": None, "requestId": None, "requestFor": None, "loanTransferId": None,
            "packetTransferId": None, "packetUniqueId": None, "partnerName": None,
            "loanRenewalId": None, "releaseId": None,
        }
        if self.scenario == "full-release":
            body.update({"userType": "appraiser", "releaserId": appraiser_id,
                         "fullReleaseId": self.request_id})
            path = "/api/jewellery-release/assign-releaser"
        elif self.scenario == "part-release":
            body.update({"appraiserId": appraiser_id, "partReleaseId": self.request_id})
            path = "/api/jewellery-release/assign-appraiser"
        else:
            body.update({"appraiserId": appraiser_id, "loanRenewalId": self.request_id})
            path = "/api/renewal/assign-appraiser"
        answer = await self._as_approver("Assignment", lambda: self._post(path, body))
        print(f"Assigned user {appraiser_id}: {answer.get('message', answer)}")

    # ------------------------------------------------------------------ renewal / renewal + top-up

    async def top_up_schemes(self):
        """GET scheme/get-top-up-scheme?masterLoanId&from=1&to=10&schemeFor=<renewal|renewalPlusTopUp|topup>.
        Each entry is a scheme already priced for this loan (totalOutstanding, netTopUp,
        processingCharge, stampDuty, loanCoLenderBank)."""
        body = await self._get("/api/scheme/get-top-up-scheme", masterLoanId=self.master_loan_id,
                               **{"from": 1, "to": 10}, schemeFor=self.SCHEME_FOR[self.scenario])
        schemes = body.get("data") or []
        if not schemes:
            raise RepaymentScenarioError(f"No {self.SCHEME_FOR[self.scenario]} scheme is offered for "
                                         f"master loan {self.master_loan_id}.")
        return schemes

    def _pick_top_up_scheme(self, schemes, minimum_top_up):
        """The first scheme whose new loan amount lands inside its own range (selectSchemeForToup's
        check), with a usable net top-up when the scenario adds one."""
        forced = str(self.suite.forced_scheme_id or "")
        for scheme in schemes:
            if forced and str(scheme.get("id")) != forced:
                continue
            if not self.scheme_fits_purpose(scheme):
                continue
            net = self._num(scheme.get("netTopUp"))
            adds_top_up = self.scenario in ("topup", "renewal-topup")
            # selectSchemeForToup: 'You Are Not Eligible For Top-up' at netTopUp <= 0, and
            # 'Minimum top up amount should be …' when minimumTopUpAmount >= netTopUp.
            if adds_top_up and (net <= 0 or net <= minimum_top_up):
                continue
            top_up = net if adds_top_up else 0
            new_amount = self._num(scheme.get("totalOutstanding")) + top_up
            if self._num(scheme.get("schemeAmountStart")) <= new_amount <= \
                    self._num(scheme.get("schemeAmountEnd"), float("inf")):
                return scheme, round(top_up, 2)
        raise RepaymentScenarioError(
            f"None of the {len(schemes)} {self.SCHEME_FOR[self.scenario]} schemes fits"
            + (f" (scheme {forced} pinned)" if forced else "")
            + (f" for purpose type {self.wanted_purpose_type!r}" if self.wanted_purpose_type else "")
            + " - the scheme does not take that purpose type, the new loan amount is outside its range,"
              " or the net top-up is too small.")

    @property
    def _ornament_ids(self):
        return [o["id"] for o in self._selected_ornaments]

    async def renewal(self):
        self._selected_ornaments = self.loan["loanOrnamentsDetail"]   # renew / top-up: every ornament
        if not await self.resume_open_request():
            await self._place_renewal()
        if not self.child_only:
            await self.approve_amount()
            await self.assign_appraiser()

        self._log("Open the renewed loan (GET renewal/apply-loan)")
        opened = await self._get(f"/api/renewal/apply-loan/{await self._customer_unique_id()}",
                                 loanRenewalId=self.request_id)
        return self._take_new_loan(opened, "renewal/apply-loan")

    async def _place_renewal(self):
        loan = self.loan
        await self.suite.fetch_global_setting()
        minimum = self._num(self.suite.global_setting.get("minimumTopUpAmount"))

        await self.resolve_purpose_type()
        self._log(f"Renewal scheme ({self.SCHEME_FOR[self.scenario]})")
        scheme, top_up = self._pick_top_up_scheme(await self.top_up_schemes(), minimum)
        self.scheme = scheme
        print(f"Scheme {scheme['id']} {scheme.get('schemeName')!r}: outstanding "
              f"{scheme.get('totalOutstanding')}, top-up {top_up}.")

        self._log("Renewal amount (POST renewal/amount)")
        co_lender = self._co_lender_id()
        self.summary = await self._post("/api/renewal/amount", {
            "masterLoanId": loan["loanOrnamentsDetail"][0]["masterLoanId"],
            "ornamentId": self._ornament_ids, "newSchemeId": scheme["id"], "wavier": 0,
            "coLenderBankId": (loan.get("loanCoLenderBank") or {}).get("id"), "topUpAmount": top_up,
        })
        if self.summary.get("isForCoLenderReset"):
            co_lender = None
            print(f"Co-lender reset by the server: {self.summary.get('errorMessage')}")
        calculation = self.summary.get("calculationData") or {}
        info = self.summary.get("loanInfo") or {}
        processing, actual_processing, rpg = self._calc_charges(calculation)
        secured_scheme = calculation.get("securedScheme") or {}
        new_amount = self._num(calculation.get("securedLoanAmount"))
        if not (self._num(secured_scheme.get("schemeAmountStart")) <= new_amount
                <= self._num(secured_scheme.get("schemeAmountEnd"), float("inf"))):
            raise RepaymentScenarioError(f"New loan amount {new_amount} is outside scheme "
                                         f"{secured_scheme.get('id')}'s range (portal: schemeRange).")
        print(f"Payable now {info.get('totalPayableAmount')} (interest {info.get('interestAmount')}, "
              f"penal {info.get('penalInterest')}); new loan {new_amount}, processing {processing}.")

        await self.fetch_purposes()
        purpose, purpose_type = self.pick_purpose(scheme)
        base = self._num(calculation.get("loanAmount") or new_amount)
        await self.loan_consent_otp({
            "transactionType": self.CONSENT_TYPE[self.scenario], "masterLoanId": self.master_loan_id,
            "schemeId": secured_scheme.get("id"), "schemeName": secured_scheme.get("schemeName"),
            "interestRate": self._highest_rate(secured_scheme),
            "newLoanAmount": round(base + (top_up if self.scenario == "renewal-topup" else 0), 2),
            "tenure": secured_scheme.get("loanTenure"),
        })

        self._log(f"Renewal payment ({self.suite.transfer_mode.upper()}, offline) - POST renewal/submit")
        payment = self.offline_payment(info.get("totalPayableAmount"))
        answer = await self._post("/api/renewal/submit", self._pay_object(
            scheme["id"], top_up, processing, actual_processing, rpg, payment,
            purpose, purpose_type, co_lender))
        self.request_id = self._id_from(answer, "loanRenewalId")
        print(f"Renewal placed: {answer.get('message', answer)}")
        self.result.update({"Purpose": f"{purpose} ({purpose_type})", "Top-up": top_up,
                            "Paid": f"{payment['paidAmount']} via {payment['paymentType']} "
                                    f"(UTR {payment['transactionId']})"})

    # ------------------------------------------------------------------ top-up

    async def pay_due_interest(self):
        """A top-up is refused while interest is due (isPayableAmount -> the portal sends the user
        to interest-emi). Quick-pay it OFFLINE: payable-amount -> confirm-payment-info -> payment."""
        if not self.release_info.get("isPayableAmount"):
            return
        self._log("Quick pay the due interest (offline)")
        due = (await self._get("/api/quick-pay/payable-amount", masterLoanId=self.master_loan_id)).get("data") or {}
        # amountPaidCal, 'tilldate' (the default): penal (secured + unsecured) + GST on penal +
        # interest accrued till today + manual charges.
        amount = round(sum(self._num(due.get(k)) for k in (
            "securedPenalInterest", "unsecuredPenalInterest", "gstOnPenalInterest", "interest",
            "totalManualCharges")), 2)
        if amount <= 0:
            return
        await self._get("/api/quick-pay/confirm-payment-info", masterLoanId=self.master_loan_id, amount=amount)
        payment = self.offline_payment(amount)
        # QuickPayComponent.submit (offline): the amount travels twice, once in clear and once as
        # deCryptedAmount = encryptionAndDecryption(`${amount}_AUGQ`, 'e') -- the same CryptoJS
        # AES the harness uses for identity numbers. Without it the server answers 500.
        answer = await self._post("/api/quick-pay/payment", {
            "masterLoanId": self.master_loan_id, "payableAmount": amount,
            "deCryptedAmount": self.suite._encrypt_identity_proof_number(f"{self._js_number(amount)}_AUGQ"),
            "paymentDetails": payment, "interestAmountPaidTill": "tilldate",
        })
        print(f"Interest {amount} paid: {answer.get('message', answer)}")
        await self.approve_deposit(payment["transactionId"])
        await self.load_loan(self.master_loan_id)
        if self.release_info.get("isPayableAmount"):
            raise RepaymentScenarioError("Interest was paid but the server still reports an amount "
                                         "due (isPayableAmount) - the payment may need deposit approval.")

    async def _pending_deposit(self, utr):
        for page in range(1, 5):
            rows = (await self._get("/api/deposit", **{"from": (page - 1) * 25 + 1, "to": page * 25})
                    ).get("data") or []
            for row in rows:
                if str(row.get("masterLoanId")) == self.master_loan_id and                         str(row.get("depositStatus")).lower() == "pending" and                         str(row.get("bankTransactionUniqueId") or utr).upper() == utr.upper():
                    return row
            if len(rows) < 25:
                return None
        return None

    async def approve_deposit(self, utr):
        """Funds approvals -> Deposit: an offline payment stays 'Pending' (and the interest stays
        due) until it is confirmed. DepositListComponent.updateStatus -> POST quick-pay/confirm-payment
        {transactionId, status 'Completed', paymentReceivedDate, masterLoanId, depositAmount,
        receivedAmountForBranchId}."""
        self._log("Confirm the deposit (funds approval)")
        row = await self._as_approver("Deposit list", lambda: self._pending_deposit(utr))
        if row is None and self.approver_role != "admin":
            row = await self._as_role("admin", lambda: self._pending_deposit(utr))
            if row is not None:
                self.approver_role = "admin"
        if row is None:
            raise RepaymentScenarioError(f"No pending deposit with UTR {utr} for master loan "
                                         f"{self.master_loan_id} in /api/deposit.")
        body = {"transactionId": row.get("id"), "status": "Completed",
                "paymentReceivedDate": self._js_date_now(), "masterLoanId": row.get("masterLoanId"),
                "depositAmount": row.get("transactionAmont"),
                "receivedAmountForBranchId": row.get("receivedAmountForBranchId")}
        answer = await self._as_approver("Deposit confirmation",
                                         lambda: self._post("/api/quick-pay/confirm-payment", body))
        print(f"Deposit {row.get('id')} confirmed: {answer.get('message', answer)}")

    async def topup(self):
        loan = self.loan
        self._selected_ornaments = loan["loanOrnamentsDetail"]
        await self.claim_parent()     # a top-up opens its loan at once: nothing to resume, only refuse
        await self.pay_due_interest()
        await self.suite.fetch_global_setting()
        minimum = self._num(self.suite.global_setting.get("minimumTopUpAmount"))

        await self.resolve_purpose_type()
        self._log("Top-up scheme (get-top-up-scheme schemeFor=topup)")
        scheme, top_up = self._pick_top_up_scheme(await self.top_up_schemes(), minimum)
        self.scheme = scheme
        print(f"Scheme {scheme['id']} {scheme.get('schemeName')!r}: outstanding "
              f"{scheme.get('totalOutstanding')}, net top-up {top_up}.")

        self._log("Processing fee for the top-up (GET loan-process/re-calculate)")
        fee = (await self._get("/api/loan-process/re-calculate", schemeId=scheme["id"],
                               amount=top_up, masterLoanId=self.master_loan_id)).get("data") or {}
        print(f"Processing {fee.get('securedprocessingCharge')}, upfront {fee.get('upfrontInterestAmount')}, "
              f"stamp duty {fee.get('stampDutyCharges')}.")

        await self.fetch_purposes()
        purpose, purpose_type = self.pick_purpose(scheme)
        co_lender = self._co_lender_id(scheme)
        await self.loan_consent_otp({
            "transactionType": "Top Up", "masterLoanId": self.master_loan_id,
            "schemeId": scheme["id"], "schemeName": scheme.get("schemeName"),
            "interestRate": self._highest_rate(scheme),
            "newLoanAmount": round(self._num(scheme.get("totalOutstanding")) + top_up, 2),
            "tenure": scheme.get("loanTenure"),
        })

        self._log("Top-up preview (GET top-up/loan-preview)")
        preview = await self._get("/api/top-up/loan-preview", masterLoanId=self.master_loan_id,
                                  schemeId=scheme["id"], topUpAmount=top_up, coLenderBankId=co_lender)
        if preview.get("isForCoLenderReset"):
            raise RepaymentScenarioError(f"Top-up preview: {preview.get('errorMessage')}")

        self._log("Create the top-up loan (POST top-up/create-new-loan + final-loan-details)")
        body = {"masterLoanId": self.master_loan_id, "schemeId": scheme["id"], "topUpAmount": top_up,
                "coLenderBankId": co_lender, "loanPurpose": purpose, "loanPurposeType": purpose_type}
        created = await self._post("/api/top-up/create-new-loan", body)
        final_request = created.get("req")
        if not isinstance(final_request, dict):
            raise RepaymentScenarioError(f"create-new-loan returned no final-loan-details body: {created}")
        final = await self._post("/api/loan-process/final-loan-details", final_request)
        self.suite.final_loan_details_data = final.get("data") if isinstance(final.get("data"), dict) else final
        self.result.update({"Purpose": f"{purpose} ({purpose_type})", "Top-up": top_up})
        return self._take_new_loan(final_request, "top-up/create-new-loan")

    # ------------------------------------------------------------------ part / full release

    async def release_summary(self, scheme_id=None):
        """POST jewellery-release {masterLoanId, ornamentId, newSchemeId, wavier, coLenderBankId}:
        what releasing the selected ornaments costs (loanInfo), and for a part release the new
        loan on the rest (calculationData)."""
        self.summary = await self._post("/api/jewellery-release", {
            "masterLoanId": self.loan["loanOrnamentsDetail"][0]["masterLoanId"],
            "ornamentId": self._ornament_ids, "newSchemeId": scheme_id, "wavier": 0,
            "coLenderBankId": (self.loan.get("loanCoLenderBank") or {}).get("id"),
        })
        return self.summary

    async def part_release_schemes(self):
        """getPartner(): GET scheme/partner-scheme-amount/<internalBranchId> with the part-release
        flags (internalBranchId is the path, not a query parameter)."""
        branch = self.suite.internal_branch_id or 1
        body = await self._get(f"/api/scheme/partner-scheme-amount/{branch}", parentLoanId=self.master_loan_id,
                               isFromRelease="true", masterLoanId=self.master_loan_id,
                               isNewLoanFromPartRelease="true")
        partners = body.get("data") or []
        schemes = (partners[0].get("schemes") if partners else None) or []
        if not schemes:
            raise RepaymentScenarioError("No scheme is offered for the new loan after part release.")
        return schemes

    async def part_release(self):
        self._selected_ornaments = self.loan["loanOrnamentsDetail"][:1]   # release one, keep the rest
        if not await self.resume_open_request():
            await self._place_part_release()
        if not self.child_only:
            await self.approve_amount()
            await self.assign_appraiser()
            await self.release_packet(is_part_release=True)
            await self.set_release_status()
            await self.upload_release_document()

        self._log("Open the new loan on the remaining ornaments (GET jewellery-release/apply-loan)")
        opened = await self._get(f"/api/jewellery-release/apply-loan/{await self._customer_unique_id()}",
                                 partReleaseId=self.request_id)
        return self._take_new_loan(opened, "jewellery-release/apply-loan")

    async def _place_part_release(self):
        ornaments = self.loan["loanOrnamentsDetail"]
        self._log(f"Part release: ornament {ornaments[0].get('id')} of {len(ornaments)}")
        await self.resolve_purpose_type()
        schemes = await self.part_release_schemes()
        forced = str(self.suite.forced_scheme_id or "")
        last_error = ""
        for scheme in schemes:
            if forced and str(scheme.get("id")) != forced:
                continue
            if not self.scheme_fits_purpose(scheme):
                last_error = f"scheme {scheme['id']} does not take purpose type {self.wanted_purpose_type!r}"
                continue
            summary = await self.release_summary(scheme["id"])
            calculation = summary.get("calculationData") or {}
            secured = calculation.get("securedScheme") or scheme
            amount = self._num(calculation.get("securedLoanAmount"))
            if self._num(secured.get("schemeAmountStart")) <= amount <= \
                    self._num(secured.get("schemeAmountEnd"), float("inf")):
                self.scheme = scheme
                break
            last_error = f"scheme {scheme['id']}: new loan {amount} outside its range"
        else:
            raise RepaymentScenarioError("No part-release scheme fits the new loan amount"
                                         + (f" ({last_error})" if last_error else "") + ".")
        calculation = self.summary.get("calculationData") or {}
        info = self.summary.get("loanInfo") or {}
        processing, actual_processing, rpg = self._calc_charges(calculation)
        await self.fetch_purposes()
        purpose, purpose_type = self.pick_purpose(self.scheme)
        print(f"Scheme {self.scheme['id']}: payable now {info.get('totalPayableAmount')}, new loan "
              f"{calculation.get('securedLoanAmount')} on {len(ornaments) - 1} ornament(s).")

        self._log(f"Part release payment ({self.suite.transfer_mode.upper()}, offline)")
        payment = self.offline_payment(info.get("totalPayableAmount"))
        answer = await self._post("/api/jewellery-release/part-release", self._pay_object(
            self.scheme["id"], 0, processing, actual_processing, rpg, payment,
            purpose, purpose_type, self._co_lender_id()))
        self.request_id = self._id_from(answer, "partReleaseId")
        print(f"Part release placed: {answer.get('message', answer)}")
        self.result.update({"Released ornaments": len(self._selected_ornaments),
                            "Purpose": f"{purpose} ({purpose_type})",
                            "Paid": f"{payment['paidAmount']} via {payment['paymentType']} "
                                    f"(UTR {payment['transactionId']})"})

    async def full_release(self):
        self._selected_ornaments = self.loan["loanOrnamentsDetail"]
        if not await self.resume_open_request():
            await self._place_full_release()

        await self.approve_amount()
        await self.assign_appraiser()
        await self.release_packet(is_part_release=False)
        await self.set_release_status()
        await self.upload_release_document()
        return None

    async def _place_full_release(self):
        self._log("Full release summary (POST jewellery-release, every ornament)")
        info = (await self.release_summary(None)).get("loanInfo") or {}
        payable = self._num(info.get("totalPayableAmount"))
        if payable < 0:
            raise RepaymentScenarioError("Amount should be greater than zero (portal check).")
        print(f"Payable to close the loan: {payable} (interest {info.get('interestAmount')}, "
              f"penal {info.get('penalInterest')}).")

        self._log(f"Full release payment ({self.suite.transfer_mode.upper()}, offline)")
        payment = self.offline_payment(payable)
        answer = await self._post("/api/jewellery-release/full-release", self._pay_object(
            None, 0, 0, None, None, payment, co_lender_id=self._co_lender_id()))
        self.request_id = self._id_from(answer, "fullReleaseId")
        print(f"Full release placed: {answer.get('message', answer)}")
        self.result["Paid"] = (f"{payment['paidAmount']} via {payment['paymentType']} "
                               f"(UTR {payment['transactionId']})")

    # ------------------------------------------------------------------ release: packet + documents

    async def _packets(self):
        """packet-tracking/view-packets?masterLoanId -> the loan's packets (barcode + unique id)."""
        body = await self._get("/api/packet-tracking/view-packets", masterLoanId=self.master_loan_id)
        data = body.get("data") or []
        packets = (data[0].get("packets") if data and isinstance(data[0], dict) else None) or data
        packets = [p for p in packets if isinstance(p, dict) and p.get("barcodeNumber")]
        if not packets:
            raise RepaymentScenarioError(f"No packet found for master loan {self.master_loan_id}.")
        return packets

    def _barcodes(self, packets):
        """verifyBarcode: the scanned barcode must equal {Barcode, packetId} of each packet."""
        return [{"Barcode": p.get("barcodeNumber"), "packetId": p.get("packetUniqueId")} for p in packets]

    async def release_packet(self, is_part_release):
        """Packet out of the partner branch to the appraiser (packet-release-collect, partner-user
        OTP), then into the customer's hands (packet-release-home-in, customer OTP + hand-over
        photo + the NOC acknowledgement)."""
        suite = self.suite
        if (await self.find_request()).get("isCustomerReceivedPacket"):
            print("Packet already handed over to the customer.")
            return
        packets = await self._packets()
        loan_id = packets[0].get("loanId")

        self._log("Collect the packet from the partner branch (packet-release-collect)")
        nxt = (await self._get("/api/packet-tracking/next-packet-location",
                               masterLoanId=self.master_loan_id)).get("data") or []
        if not nxt:
            raise RepaymentScenarioError("next-packet-location offered no location to collect into.")
        location = nxt[0]
        tracking = await self._get("/api/packet-tracking/get-particular-location",
                                   packetLocationId=4, masterLoanId=self.master_loan_id)
        branches = [str(b.get("id")) for b in ((tracking.get("data") or {}).get("partnerBranch") or [])
                    if b.get("id")] or [suite.PARTNER_BRANCH_ID]
        forced = os.getenv("GOLD_LOAN_PARTNER_BRANCH_ID", "").strip()
        partner_mobile = suite._partner_user_mobile()
        partner_id = (tracking.get("data") or {}).get("id") or suite.partner_id
        partner_branch_id, user, _, partner_mobile = await suite.resolve_packet_receiver(
            partner_id, [forced] if forced else branches, partner_mobile)
        if not user:
            raise RepaymentScenarioError(f"Partner user {partner_mobile} is not in any of the partner's "
                                         f"branches {branches}; set GOLD_LOAN_PARTNER_USER_MOBILE / "
                                         "GOLD_LOAN_PARTNER_BRANCH_ID.")
        sent = await self._post("/api/partner-user-otp/send-otp", {
            "mobileNumber": partner_mobile, "id": suite.logged_in_user_id,
            "type": "updateLocationHandover", "masterLoanId": self.master_loan_id})
        reference = sent.get("referenceCode")
        await self._post("/api/partner-user-otp/verify-otp", {
            "otp": suite.CUSTOMER_OTP, "referenceCode": reference, "type": "updateLocationHandover"})
        collect = self._location_form(location.get("id"), packets, loan_id)
        collect.update({"mobileNumber": partner_mobile, "receiverType": "PartnerUser",
                        "user": f"{user.get('firstName', '')} {user.get('lastName', '')}".strip(),
                        "otp": suite.CUSTOMER_OTP, "referenceCode": reference,
                        "partnerReceiverId": user.get("id"), "partnerBranchId": partner_branch_id,
                        "role": ((user.get("roles") or [{}])[0]).get("roleName")})
        answer = await self._post("/api/packet-tracking/packet-release-collect", collect)
        print(f"Packet collected into location {location.get('id')} "
              f"({location.get('location')}): {answer.get('message', answer)}")

        self._log("Hand the packet over to the customer (packet-release-home-in)")
        customer = (await self._get("/api/packet-tracking/customer-info",
                                    masterLoanId=self.master_loan_id)).get("data") or {}
        mobile = customer.get("mobileNumber")
        receiver = (await self._get("/api/packet-tracking/user-name", mobileNumber=mobile,
                                    receiverType="Customer", masterLoanId=self.master_loan_id,
                                    allUsers=1)).get("data") or {}
        sent = await self._post("/api/customer-otp/send-otp", {
            "mobileNumber": mobile, "id": suite.logged_in_user_id, "type": "updateLocationCollect"})
        reference = sent.get("referenceCode")
        await self._post("/api/customer-otp/verify-otp-admin", {
            "otp": suite.CUSTOMER_OTP, "referenceCode": reference, "type": "updateLocationCollect"})
        noc = await self._get("/api/jewellery-release/noc", masterLoanId=self.master_loan_id,
                              type="", documentFrom="NOC")
        hand_over = await self._upload({"reason": "user"}, suite.DUMMY_IMAGE_PATH)
        home_in = self._location_form(self.CUSTOMER_HOME_LOCATION_ID, packets, loan_id)
        home_in.update({
            "id": customer.get("id"), "receiverType": customer.get("receiverType") or "Customer",
            "mobileNumber": mobile, "releaseId": self.request_id,
            "user": f"{customer.get('firstName', '')}{customer.get('lastName', '')}",
            "otp": suite.CUSTOMER_OTP, "referenceCode": reference,
            "customerReceiverId": receiver.get("id"),
            "customerHandOver": [hand_over], "customerAcknowledgement": [noc.get("path")],
            "latitude": suite.latitude, "longitude": suite.longitude,
            "isFullRelease": not is_part_release, "isPartRelease": is_part_release,
        })
        answer = await self._post("/api/packet-tracking/packet-release-home-in", home_in)
        print(f"Packet handed over to the customer: {answer.get('message', answer)}")
        if isinstance(answer, dict) and answer.get("appraiserRequestId"):
            self.new_loan["appraiserRequestId"] = answer["appraiserRequestId"]

    def _location_form(self, location_id, packets, loan_id):
        """UpdateLocationComponent's locationForm.getRawValue(), every control present."""
        return {
            "packetLocationId": location_id, "barcodeNumber": self._barcodes(packets),
            "mobileNumber": None, "user": None, "receiverType": None, "otp": None,
            "referenceCode": None, "userReceiverId": None, "customerReceiverId": None,
            "partnerReceiverId": None, "loanId": loan_id, "masterLoanId": int(self.master_loan_id),
            "partnerId": None, "partnerName": None, "partnerBranchId": None, "internalBranchId": None,
            "deliveryPacketLocationId": None, "deliveryInternalBranchId": None,
            "deliveryPartnerBranchId": None, "deliveryPartnerName": None, "id": None,
            "releaseId": None, "role": None, "packetTransferId": None, "partnerBranch": None,
            "partnerBranchUserName": None, "customerHandOver": None, "customerAcknowledgement": None,
            "isAuction": None, "auctionDocuments": None,
        }

    async def _upload(self, params, file_path, full=False):
        """sharedService.uploadFile(file, params): multipart 'avatar' to upload-file?<params>.
        Returns the stored path, or with full=True the whole uploadFile (path, URL, originalname)."""
        import mimetypes
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        with open(file_path, "rb") as fh:
            response = await self.suite._make_authenticated_request(
                "POST", self._q("/api/upload-file", params),
                files={"avatar": (os.path.basename(file_path), fh, content_type)})
        upload = response.json().get("uploadFile") or {}
        if full:
            return {"path": upload.get("path"), "URL": upload.get("URL"),
                    "originalname": upload.get("originalname") or os.path.basename(file_path)}
        return upload.get("path")

    async def set_release_status(self):
        """UpdateStatusComponent 'jewelleryReleaseFinal' / 'fullReleaseFinal': the appraiser /
        releaser marks the ornaments released (a reason is only required for other statuses)."""
        self._log("Mark the ornaments released")
        row = await self.find_request()
        if str(row.get("partReleaseStatus") or row.get("fullReleaseStatus") or "").lower() == "released":
            print("Ornaments already marked released.")
            return
        utr = (row.get("transaction") or {}).get("utrNumber")
        if self.scenario == "part-release":
            path, body = "/api/jewellery-release/appraiser-status", {
                "partReleaseId": self.request_id, "partReleaseStatus": "released",
                "appraiserReason": None, "utrNumber": utr}
        else:
            path, body = "/api/jewellery-release/releaser-status", {
                "fullReleaseId": self.request_id, "fullReleaseStatus": "released",
                "releaserReason": None, "utrNumber": utr}
        answer = await self._put(path, body)
        print(f"Release status: {answer.get('message', answer)}")

    async def upload_release_document(self):
        """UploadDocumentComponent.save: the signed customer acknowledgement (required), uploaded
        with reason partRelease / fullRelease and the request id."""
        self._log("Upload the release document")
        if (await self.find_request()).get("documents"):
            # packet-release-home-in already stored the NOC acknowledgement, pawn copy and hand-over
            # photo on the request (live TEST, part release 1823).
            print("Release documents already on file (stored at the customer hand-over).")
            return
        part = self.scenario == "part-release"
        id_key = "partReleaseId" if part else "fullReleaseId"
        reason = "partRelease" if part else "fullRelease"
        params = {"reason": reason, id_key: self.request_id}
        acknowledgement = await self._upload(params, self.suite.DUMMY_PDF_PATH, full=True)
        # The form marks only the acknowledgement required, but the server also demands the
        # customer hand-over photo (400 "customerHandOver is required", live TEST).
        hand_over = await self._upload(params, self.suite.DUMMY_IMAGE_PATH, full=True)
        body = {"partReleaseId": self.request_id if part else None,
                "fullReleaseId": None if part else self.request_id,
                "documents": [acknowledgement["path"]], "documentsName": acknowledgement["originalname"],
                "documentsImage": acknowledgement["URL"],
                "pawnCopy": None, "pawnCopyName": "", "pawnCopyImage": None,
                "customerHandOver": [hand_over["path"]], "customerHandOverName": hand_over["originalname"],
                "customerHandOverImage": hand_over["URL"]}
        endpoint = "/api/jewellery-release/document" if part else "/api/jewellery-release/full-release/document"
        answer = await self._post(endpoint, body)
        print(f"Release document: {answer.get('message', answer)}")

    # ------------------------------------------------------------------ the new loan

    def _take_new_loan(self, answer, source):
        """The renewal / top-up / part-release loan the server opened: its ids (and, from
        apply-loan, the stage the portal resumes the loan form at)."""
        answer = answer if isinstance(answer, dict) else {}
        loan_id = answer.get("loanId")
        master_loan_id = answer.get("masterLoanId")
        if not loan_id:
            raise RepaymentScenarioError(f"{source} did not return the new loan's ids: "
                                         f"{str(answer)[:400]}")
        self.new_loan.update({"loanId": str(loan_id), "masterLoanId": str(master_loan_id or ""),
                              "stage": answer.get("loanCurrentStage")})
        print(f"New loan opened: loanId={loan_id}, masterLoanId={master_loan_id}"
              + (f", form stage {answer.get('loanCurrentStage')}" if answer.get("loanCurrentStage") else ""))
        return self.new_loan

    # ------------------------------------------------------------------ entry point

    async def run(self, master_loan_id):
        """Run the scenario on `master_loan_id`. Returns the new loan's ids for renewal, top-up and
        part release (the caller drives it to completion), or None for a full release."""
        await self.load_loan(master_loan_id)
        handler = {"renewal": self.renewal, "renewal-topup": self.renewal, "topup": self.topup,
                   "part-release": self.part_release, "full-release": self.full_release}[self.scenario]
        return await handler()
