"""BT (Balance Transfer) loan journey -- a gold loan taken over from another lender.

Source of truth: the TEST portal's lazy chunk 13.js (loan-management), BTLoanService plus the
six tabs of BtLoanComponent (`/admin/loan-management/edit-bt-loan/<btId>`), and chunk 16.js
(NewRequestListComponent.applyBTLoan) / main.js (NewRequestAddComponent). Live-verified on TEST
17 Nov 2026: BT 167 / AUGM-BT-24551 went stage 1 -> 11 through exactly these calls.

How a BT starts
  Customer Management -> "Start New BT Process" opens the ordinary new-request dialog with
  `request: "bt application"`; the dialog POSTs /api/appraiser-request with that extra key. The
  server answers {"message": "Request Created"} and opens a business-transfer application
  (`customerLoanBusinessTransfer` {id, btUniqueId "AUGM-BT-…", customerBtStage}) on the request.
  The request is visible in /api/appraiser-request/view-all only under the ADMIN token (the
  appraiser's own list does not show it). "Apply BT Loan" = POST
  /api/appraiser-request/track-bt-history {appraiserRequestId, action: 3}, then the BT screen.

Stages (`businessTransfer.btCurrentStageId`, GET /api/bt-process/applied-bt-application/<id>)
  1 Existing BT Info  -> POST bt-process/existing-bt-info
  2 Ornament Details  -> POST bt-process/ornament-details
  3 Scheme Details    -> POST bt-process/scheme-details
  4 Bank Details      -> POST bt-process/bank-details   (after the usual karza + validate-account)
  5 Upload Documents  -> POST bt-process/upload-document
  6 RCH / 7 RH / 8 ZH / 9 ZAH / 10 CEO approval -> POST bt-process/update-rating-<rch|rh|zh|zah|ceo>
  11 Advance Transfer Initiated -- the application is approved; an advance request
     (`advanceRequestId`) now waits in the finance "advance transfer" module. This flow STOPS
     there on purpose: approving that advance pays the old lender through the ICICI payout
     gateway, and the harness never drives a real payout.
  Which of RH/ZH/ZAH/CEO run is decided by the server (is<Role>ApprovalRequired); a 3L BT went
  RCH -> 11 directly. Every step re-reads the application first, so a run can be resumed at any
  stage and the server's own values (netWeightAfterPurity, collateral, requiredLoanAmount) are
  always the ones sent on.
"""

import datetime
import math
import os
import random
import urllib.parse
from decimal import Decimal, ROUND_HALF_UP

import httpx


class BtLoanError(RuntimeError):
    """The BT journey cannot continue (with the reason)."""


BT_STAGE_NAMES = {
    1: "Existing BT Info", 2: "Ornament Details", 3: "Scheme Details", 4: "Bank Details",
    5: "Upload Documents", 6: "RCH Approval", 7: "RH Approval", 8: "ZH Approval",
    9: "ZAH Approval", 10: "CEO Approval", 11: "Advance Transfer Initiated",
}
DOCUMENTS_STAGE = 5
# ApprovalsComponent.approvalStages + the BTLoanService method each stage calls.
APPROVAL_STAGES = {6: ("rch", "RCH Approval"), 7: ("rh", "RH Approval"), 8: ("zh", "ZH Approval"),
                   9: ("zah", "ZAH Approval"), 10: ("ceo", "CEO Approval")}
APPLICATION_APPROVED_STAGE = 11
APPROVAL_DECISIONS = ("approved", "incomplete", "rejected")
# ApprovalsComponent.rejectionReasons (a rejection with any of these sends the reason as comment).
REJECTION_REASON = "Documents Not Clear / Legible"
BT_REQUEST_MARKER = "bt application"
BT_UPLOAD_REASON = "btApplication"
CHEQUE_LIMIT = 500000            # one signed cheque per 5L of the BT amount (uploadFile)
LIVE_IMAGE_LOCATION = (19.0760, 72.8777)   # the portal sends the browser's geolocation
DEFAULT_TENURE = "180"
DEFAULT_KARAT = "22 K HM"        # OrnamentDetailsComponent's default karat
ELIGIBILITY_RATE_FLOOR = 4000    # rupees per gram of netWeightAfterPurity (live schemes: 4533-5133)


def half_up(value, places=2) -> float:
    """SharedService.getPurityWeight: Decimal(net) * (ltv / 100), toDecimalPlaces(2) -- HALF_UP."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def purity_weight(net_weight, purity) -> float:
    return half_up(Decimal(str(net_weight)) * Decimal(str(purity)) / Decimal(100))


def iso_midnight(day: datetime.date) -> str:
    """A mat-datepicker date as Angular serialises it."""
    return datetime.datetime(day.year, day.month, day.day).isoformat() + ".000Z"


def build_existing_bt_info(amount, lender, company_type, today=None, rng=random):
    """The existing-bt-info form (`existingLoanInfo.getRawValue()`, bankNbfcName unwrapped).

    The datepicker only allows past start dates; end must be after start. The old loan is
    released in full, so releaseLoanAmount = loanAmount = the amount being transferred."""
    today = today or datetime.date.today()
    start = today - datetime.timedelta(days=rng.randint(90, 240))
    end = start + datetime.timedelta(days=365)
    return {
        "bankNbfcName": lender,
        "companyType": company_type,
        "existingLoanId": "LN%d" % rng.randint(10 ** 9, 10 ** 10 - 1),
        "loanAmount": int(amount),
        "releaseLoanAmount": int(amount),
        "loanStartDate": iso_midnight(start),
        "loanEndDate": iso_midnight(end),
    }


def build_ornaments(amount, ornament_types, karat, count=3, rng=random):
    """OrnamentDetailsComponent rows + the totals its nextAction() adds.

    `karat` is a /api/karat-details row; purity = the top of its ltvRange (the default the
    karat dropdown patches in). Weights are sized so Σ netWeightAfterPurity × a conservative
    rate clears the BT amount with headroom (the scheme calculator's eligibility is ~5000/g)."""
    ltv_range = list(karat.get("range") or [])
    if not ltv_range:
        raise BtLoanError(f"Karat {karat.get('karat')!r} has no LTV range.")
    purity = ltv_range[-1]
    target_nwap = float(amount) * 1.3 / ELIGIBILITY_RATE_FLOOR
    names = [t.get("name") for t in ornament_types if t.get("name")] or ["Chain"]
    rows = []
    for i in range(count):
        nwap = target_nwap / count * rng.uniform(0.9, 1.15)
        net = half_up(nwap * 100 / float(purity))
        gross = half_up(net + rng.uniform(0.3, 1.5))
        rows.append({
            "ornamentType": names[i % len(names)] if count <= len(names) else rng.choice(names),
            "numberOfPieces": 1,
            "grossWeight": gross,
            "netWeight": net,
            "karat": karat.get("karat"),
            "ltvRange": ltv_range,
            "purity": purity,
            "netweightAfterPurity": purity_weight(net, purity),
            "remark": None,
            "ornamentName": None,
        })
    totals = {
        "totalNetWeightAfterPurity": half_up(sum(r["netweightAfterPurity"] for r in rows)),
        "totalGrossWeight": half_up(sum(r["grossWeight"] for r in rows)),
        "totalNetWeight": half_up(sum(r["netWeight"] for r in rows)),
    }
    return {"ornamentDetails": rows, **totals}


def build_approval(customer_id, bt_id, stage, decision="approved", comment=None):
    """ApprovalsComponent.submit(): (endpoint key, body) for the approval the stage is waiting for."""
    if stage not in APPROVAL_STAGES:
        raise BtLoanError(f"Stage {stage} ({BT_STAGE_NAMES.get(stage, '?')}) is not an approval stage.")
    if decision not in APPROVAL_DECISIONS:
        raise BtLoanError(f"Unknown approval decision {decision!r}.")
    key, label = APPROVAL_STAGES[stage]
    body = {"customerId": int(customer_id), "businessTransferId": int(bt_id), "requestFor": label,
            "approvalStatus": decision}
    if decision == "rejected":
        body["comment"] = REJECTION_REASON          # a listed reason is sent as the comment
        body["rejectionReasonType"] = REJECTION_REASON
    else:
        body["comment"] = comment or f"{decision} by API test"   # required for 'incomplete'
    return key, body


def cheques_required(required_loan_amount) -> int:
    """RegulatoryDocumentsComponent: one signed cheque per 5L, at least one."""
    return max(1, math.ceil(float(required_loan_amount or 0) / CHEQUE_LIMIT))


def pick_scheme(schemes, amount, forced_scheme_id=""):
    """The calculator row to use: the pinned scheme, else the first whose eligibility covers the
    amount. Returns (scheme_row or None, amount_to_request)."""
    rows = [s for s in schemes or [] if isinstance(s, dict) and s.get("schemeId") is not None]
    if forced_scheme_id:
        rows = [s for s in rows if str(s.get("schemeId")) == str(forced_scheme_id)]
    fitting = [s for s in rows if float(s.get("eligibilityAmount") or 0) >= float(amount)]
    if fitting:
        return fitting[0], int(amount)
    return None, int(amount)


def select_partners(partners, wanted_names):
    """Partners (allowing BT) the scheme step may use. `wanted_names` is GOLD_LOAN_PARTNER_NAME
    (comma list, matched by name, e.g. "ARVOG" -> "Arvog ARV10"): when set, ONLY those partners --
    a BT for Arvog must not quietly land on another partner; empty = every partner (--partner any)."""
    usable = [p for p in partners or [] if isinstance(p, dict) and p.get("allowBtTransfer", True)]
    names = [n.strip().upper() for n in str(wanted_names or "").split(",") if n.strip()]
    if not names:
        if not usable:
            raise BtLoanError("partner-scheme-amount listed no partner that allows BT.")
        return usable
    chosen = [p for p in usable if str(p.get("name") or "").strip()
              and any(n in str(p["name"]).upper() or str(p["name"]).upper() in n for n in names)]
    if not chosen:
        raise BtLoanError(f"No partner matching {', '.join(names)} allows BT on this branch "
                          f"(offered: {', '.join(str(p.get('name')) for p in usable) or 'none'}).")
    return chosen


class BtLoanFlow:
    """Drives one BT application with a logged-in GoldLoanApiTest suite."""

    def __init__(self, suite, *, amount=300000, tenure=DEFAULT_TENURE, approver="admin",
                 rch_decision="approved", lender="", company_type="NBFC", scheme_id="",
                 ornament_count=3, reupload_cpv=False, rng=None):
        self.suite = suite
        self.amount = int(amount)
        self.tenure = str(tenure)
        self.approver = approver
        self.rch_decision = rch_decision
        self.lender = lender
        self.company_type = company_type
        self.scheme_id = str(scheme_id or "")
        self.ornament_count = int(ornament_count)
        self.reupload_cpv = reupload_cpv
        self.rng = rng or random.Random()
        self.bt_id = ""
        self.bt_unique_id = ""
        self.request_id = ""
        self.app = {}           # businessTransfer
        self.flags = {}         # isCpvRequired / isStatementItrSsRequired / customerTotalOutstanding
        self.steps_done = []
        self.decisions = []     # (stage label, decision)
        self.chosen_scheme = {}

    # --- plumbing -------------------------------------------------------------------------

    async def _get(self, path):
        response = await self.suite._make_authenticated_request("GET", path)
        return response.json() if response.content else {}

    async def _post(self, path, body):
        response = await self.suite._make_authenticated_request("POST", path, json_data=body)
        return response.json() if response.content else {}

    async def _as_role(self, role, fn):
        """Run fn() under another role's token (cached/saved), restoring the session after."""
        if role == "appraiser":
            return await fn()
        saved = self.suite.auth_token
        self.suite.auth_token = await self.suite._role_token(role)
        try:
            return await fn()
        finally:
            self.suite.auth_token = saved

    @property
    def stage(self) -> int:
        return int(self.app.get("btCurrentStageId") or 0)

    @property
    def stage_name(self) -> str:
        return (self.app.get("customerBtStage") or {}).get("name") or BT_STAGE_NAMES.get(self.stage, "?")

    async def load(self) -> int:
        data = await self._get(f"/api/bt-process/applied-bt-application/{self.bt_id}")
        bt = data.get("businessTransfer") if isinstance(data, dict) else None
        if not isinstance(bt, dict):
            raise BtLoanError(f"BT application {self.bt_id} could not be read: {data}")
        self.app = bt
        self.flags = {k: data.get(k) for k in ("isCpvRequired", "isStatementItrSsRequired",
                                                "customerTotalOutstanding")}
        self.bt_unique_id = bt.get("btUniqueId") or self.bt_unique_id
        self.request_id = str(bt.get("appraiserRequestId") or self.request_id)
        return self.stage

    def _customer_id(self) -> int:
        cid = self.app.get("customerId") or self.suite.customer_id
        if not cid:
            raise BtLoanError("No customer id for the BT application.")
        return int(cid)

    # --- finding / creating the application -----------------------------------------------

    async def _requests_for_customer(self, customer_unique_id):
        """Appraiser requests of the customer that carry a BT application, newest first. ADMIN
        only: the appraiser's view-all does not list BT requests."""
        async def fetch():
            q = urllib.parse.quote(str(customer_unique_id))
            return await self._get(f"/api/appraiser-request/view-all?from=1&to=25&search={q}")
        data = await self._as_role("admin", fetch)
        rows = [r for r in (data.get("data") or []) if isinstance(r, dict)
                and isinstance(r.get("customerLoanBusinessTransfer"), dict)
                and str((r.get("customer") or {}).get("customerUniqueId") or customer_unique_id)
                == str(customer_unique_id)]
        return sorted(rows, key=lambda r: int(r.get("id") or 0), reverse=True)

    async def find_open_for_customer(self, customer_unique_id):
        """The customer's newest BT that has not reached the approved stage, or None."""
        for row in await self._requests_for_customer(customer_unique_id):
            bt = row["customerLoanBusinessTransfer"]
            stage = int((bt.get("customerBtStage") or {}).get("id") or 0)
            if stage and stage < APPLICATION_APPROVED_STAGE:
                self.bt_id, self.bt_unique_id, self.request_id = str(bt["id"]), bt.get("btUniqueId", ""), str(row["id"])
                return row
        return None

    async def find_by_reference(self, reference):
        """--bt: a numeric BT id or an AUGM-BT-… number (applied-closed-list `search`, admin)."""
        reference = str(reference).strip()
        if reference.isdigit():
            self.bt_id = reference
            return
        for completed in ("false", "true"):
            async def fetch():
                q = urllib.parse.quote(reference)
                return await self._get("/api/bt-process/applied-closed-list?from=1&to=25"
                                       f"&search={q}&showCompletedApplications={completed}")
            data = await self._as_role("admin", fetch)
            for row in data.get("appliedBtList") or []:
                if str(row.get("btUniqueId")) == reference:
                    self.bt_id, self.bt_unique_id = str(row["id"]), reference
                    return
        raise BtLoanError(f"BT {reference} was not found on env '{self.suite.env_name}'.")

    async def create_request(self):
        """'Start New BT Process': the new-request body + request='bt application'."""
        suite = self.suite
        await suite._fetch_appraisers()
        body = await suite._appraiser_request_body()
        body["request"] = BT_REQUEST_MARKER
        try:
            answer = await self._post("/api/appraiser-request", body)
        except httpx.HTTPStatusError as e:
            text = e.response.text or ""
            existing = ""
            if "transfer is in process" in text.lower():
                # One BT per customer at a time -- also after approval (stage 11), while the
                # advance transfer is pending. Name the BT(s) so the user can resume one.
                rows = await self._requests_for_customer(suite.customer_unique_id)
                listed = [f"{r['customerLoanBusinessTransfer'].get('btUniqueId')} (stage "
                          f"{(r['customerLoanBusinessTransfer'].get('customerBtStage') or {}).get('id')}: "
                          f"{(r['customerLoanBusinessTransfer'].get('customerBtStage') or {}).get('name')})"
                          for r in rows]
                existing = (" The customer's BT: " + ", ".join(listed) + ".") if listed else ""
            raise BtLoanError(
                f"The server refused a new BT request for {suite.customer_unique_id}: "
                f"{e.response.status_code} {suite._short_error_body(text, 200)}.{existing} Resume an "
                "open BT with --bt, or use another --customer (or none, to create one).") from None
        print(f"BT request: {answer.get('message') or answer}")
        rows = await self._requests_for_customer(suite.customer_unique_id)
        if not rows:
            raise BtLoanError(f"The BT request for {suite.customer_unique_id} was created but no "
                              "request with a BT application is listed for the customer.")
        row = rows[0]
        bt = row["customerLoanBusinessTransfer"]
        self.request_id, self.bt_id, self.bt_unique_id = str(row["id"]), str(bt["id"]), bt.get("btUniqueId", "")
        suite.appraiser_request_id = self.request_id
        print(f"Appraiser request {self.request_id} -> BT application {self.bt_id} ({self.bt_unique_id}).")

    async def apply(self):
        """'Apply BT Loan': logged before the BT screen opens."""
        await self._post("/api/appraiser-request/track-bt-history",
                         {"appraiserRequestId": int(self.request_id), "action": 3})

    # --- the tabs ---------------------------------------------------------------------------

    async def _pick_lender(self):
        if self.lender:
            return self.lender
        data = await self._get("/api/nbfc?search=&from=1&to=25")
        names = [r.get("name") for r in data.get("data") or [] if r.get("name") and r.get("isActive", True)]
        if not names:
            raise BtLoanError("/api/nbfc listed no lender to transfer from.")
        return self.rng.choice(names)

    async def _company_type(self):
        data = await self._get("/api/company-type?search=&from=&to=")
        types = [r.get("companyType") for r in data.get("data") or [] if r.get("isActive", True)]
        if self.company_type in types or not types:
            return self.company_type
        raise BtLoanError(f"Company type {self.company_type!r} is not one of {types}.")

    async def existing_info(self):
        info = build_existing_bt_info(self.amount, await self._pick_lender(), await self._company_type(),
                                      rng=self.rng)
        body = {"customerId": self._customer_id(), "existingBtInfo": info,
                "requestId": int(self.request_id), "businessTransferId": int(self.bt_id)}
        await self._post("/api/bt-process/existing-bt-info", body)
        print(f"Existing loan: {info['bankNbfcName']} ({info['companyType']}) {info['existingLoanId']}, "
              f"amount {info['loanAmount']}.")

    async def ornaments(self):
        types = (await self._get("/api/ornament-type?from=1&to=-1&search=")).get("data") or []
        karats = (await self._get("/api/karat-details")).get("data") or []
        karat = next((k for k in karats if k.get("karat") == DEFAULT_KARAT), None)
        if karat is None:
            raise BtLoanError(f"/api/karat-details has no {DEFAULT_KARAT!r}.")
        self.rng.shuffle(types)
        payload = build_ornaments(self.amount, types, karat, self.ornament_count, self.rng)
        await self._post("/api/bt-process/ornament-details",
                         {"customerId": self._customer_id(), "businessTransferId": int(self.bt_id), **payload})
        print(f"Ornaments: {len(payload['ornamentDetails'])} x {DEFAULT_KARAT}, net weight after purity "
              f"{payload['totalNetWeightAfterPurity']} g.")

    async def scheme(self):
        """SchemesComponent: partner list -> billing cycles for the tenure -> scheme calculator."""
        branch = self.app.get("internalBranchId") or self.suite.login_branch_id or self.suite.internal_branch_id or 1
        partners = (await self._get(f"/api/scheme/partner-scheme-amount/{branch}?isFromLoanTransfer=true")).get("data") or []
        partners = select_partners(partners, os.getenv("GOLD_LOAN_PARTNER_NAME", ""))
        cycles = (await self._get(f"/api/scheme/billing-cycle?tenure={self.tenure}&isFromBt=true")).get("billingCycles") or []
        cycles = [c.get("value") if isinstance(c, dict) else c for c in cycles]
        if not cycles:
            raise BtLoanError(f"No repayment cycle is offered for a {self.tenure}-day BT.")
        nwap = self.app.get("netWeightAfterPurity")
        best = None   # the largest eligibility seen, for the error message
        tried = 0
        for partner in partners[:3]:
            for cycle in cycles:
                params = {"interestBillingCycleType": cycle, "partnerId": partner["id"], "loanTenure": self.tenure,
                          "netWeightAfterPurity": nwap, "internalBranchId": self.app.get("internalBranchId"),
                          "loanType": "BT", "customerId": self._customer_id()}
                query = urllib.parse.urlencode({k: v for k, v in params.items() if v})   # the portal drops falsy params
                schemes = (await self._get(f"/api/scheme/loan-scheme-calculator?{query}")).get("data") or []
                tried += 1
                for s in schemes:
                    if best is None or float(s.get("eligibilityAmount") or 0) > float(best[2].get("eligibilityAmount") or 0):
                        best = (partner, cycle, s)
                chosen, amount = pick_scheme(schemes, self.amount, self.scheme_id)
                if chosen:
                    body = {"customerId": self._customer_id(), "businessTransferId": int(self.bt_id),
                            "collateralValueOfOrnaments": self.app.get("collateralValueOfOrnaments"),
                            "schemeId": chosen["schemeId"], "partnerId": partner["id"],
                            "loanTenuar": self.tenure, "repaymentCycle": cycle,
                            "requiredLoanAmount": amount, "finalBtAmount": amount,
                            "eligibility": chosen["eligibilityAmount"]}
                    await self._post("/api/bt-process/scheme-details", body)
                    self.chosen_scheme = {"partner": partner.get("name"), "partnerId": partner["id"],
                                          "schemeId": chosen["schemeId"], "schemeName": chosen.get("schemeName"),
                                          "cycle": cycle, "eligibility": chosen["eligibilityAmount"]}
                    print(f"Scheme: {chosen.get('schemeName')} (id {chosen['schemeId']}) of {partner.get('name')}, "
                          f"{self.tenure} days / {cycle}; eligibility {chosen['eligibilityAmount']:.2f} "
                          f">= BT amount {amount}.")
                    return
        hint = (f" The best eligibility seen was {float(best[2]['eligibilityAmount']):.2f} "
                f"({best[2].get('schemeName')} / {best[0].get('name')})." if best else "")
        raise BtLoanError(f"No scheme{' ' + self.scheme_id if self.scheme_id else ''} covers a BT of "
                          f"{self.amount} for {self.tenure} days (tried {tried} partner/cycle "
                          f"combinations).{hint} Lower --amount or pick another --tenure / --scheme.")

    @staticmethod
    def bank_needs_ops(suite) -> bool:
        """The penny-drop verdict leaves the account pending ops: not verified by the system nor
        manually, or explicitly forOpsApproval (name mismatch). bankTxnStatus is NOT a verdict --
        it only says the bank answered."""
        return bool(suite.bank_for_ops_approval
                    or not (suite.bank_system_verified or suite.bank_manually_verified))

    def ops_bank_verification_body(self, decision="approved"):
        """bank-verification-manual for a BT account, before any loan or advance exists.

        Shape = the advance-approval screen's bank verdict (AdvanceApprovalComponent, chunk 4.js),
        with requestFrom 'bt application' + businessTransferId. The portal itself only sends
        'loan' (needs the loan's bank detail: 400 "Bank details does not exist for this loan") and
        'advance request' (needs the advance: "...for this request"); 'bt application' was found
        by a live probe on TEST 17 Nov (BT 175) -- it marks the customer's account manually
        verified (isManuallyVerified true, forOpsApproval false), which validate-account then
        reports too."""
        suite = self.suite
        return {
            "status": decision,
            "accountHolderName": suite.account_holder_name or suite.full_name,
            "accountNumber": suite.bank_account_number,
            "ifscCode": suite.bank_ifsc_code,
            "detailsFor": "customer",
            "customerId": self._customer_id(),
            "internalBranchId": None,
            "userId": None,
            "incompleteReason": False,
            "approvalReason": "Verified by ops (API test)",
            "reasonOfRejection": False,
            "requestFrom": BT_REQUEST_MARKER,
            "businessTransferId": int(self.bt_id),
        }

    async def ops_verify_bank(self):
        suite = self.suite
        answer = await self._as_role("ops", lambda: self._post("/api/loan-process/bank-verification-manual",
                                                              self.ops_bank_verification_body()))
        verdict = answer.get("data") if isinstance(answer.get("data"), dict) else answer
        suite.bank_manually_verified = bool(verdict.get("isManuallyVerified"))
        suite.bank_system_verified = bool(verdict.get("isVerified")) or suite.bank_system_verified
        suite.bank_for_ops_approval = bool(verdict.get("forOpsApproval"))
        print(f"Ops bank verification: isManuallyVerified={suite.bank_manually_verified}, "
              f"isVerified={suite.bank_system_verified}, forOpsApproval={suite.bank_for_ops_approval}.")

    async def bank(self):
        """BankInfoComponent: IFSC lookup + penny-drop (the harness's validate_account walks the
        candidate accounts), then bank-details with the verification flags the account really has.

        A penny-drop whose account name does not match the customer answers isVerified false /
        forOpsApproval true. Left like that, the BT is approved with an unverified account and
        the advance request waits on ops (live: advances 2199-2206). So the account is verified
        HERE, at the bank stage: OPS approves it manually (loan-process/bank-verification-manual,
        the same call the gold-loan flow uses) and bank-details carries isManuallyVerified true /
        forOpsApproval false. A failed ops verification stops the run."""
        suite = self.suite
        await suite.validate_account()
        if self.bank_needs_ops(suite):
            print("Bank account is pending ops verification -- ops verifies it now, at the bank stage.")
            await self.ops_verify_bank()
        if self.bank_needs_ops(suite):
            raise BtLoanError(f"Bank account {suite.bank_account_number} is still not verified after the "
                              "ops manual verification.")
        body = {
            "customerId": self._customer_id(), "businessTransferId": int(self.bt_id),
            "bankDetails": {"accountNumber": suite.bank_account_number, "ifscCode": suite.bank_ifsc_code,
                            "bankName": suite.bank_name, "bankBranchName": suite.bank_branch_name,
                            "accountHolderName": suite.account_holder_name or suite.full_name},
            "passbookProof": [],   # mandatory only for an unverified account, which never gets here
            "forOpsApproval": bool(suite.bank_for_ops_approval),
            "isVerified": bool(suite.bank_system_verified),
            "isManuallyVerified": bool(suite.bank_manually_verified),
        }
        answer = await self._post("/api/bt-process/bank-details", body)
        for key in ("isCpvRequired", "isStatementItrSsRequired", "isCpvEditable"):
            if key in answer:
                self.flags[key] = answer[key]
        print(f"Bank: {suite.bank_name} {suite.bank_account_number} ({suite.bank_ifsc_code}); "
              f"verified={body['isVerified']} manuallyVerified={body['isManuallyVerified']}.")

    async def _upload(self, kind, path=None, reason=BT_UPLOAD_REASON):
        upload = await self.suite._upload_file(reason, kind, file_path_override=path)
        return upload["uploadFile"]["path"]

    async def _cpv_image(self):
        """RegulatoryDocumentsComponent.setValidations: the application's own cpvImage (the
        customer's CPV on file) is pre-filled; only a required, missing one is uploaded."""
        on_file = self.app.get("cpvImage")
        if isinstance(on_file, list) and on_file:
            return on_file[0]
        if isinstance(on_file, str) and on_file:
            return on_file
        return (await self._upload("image")) if self.flags.get("isCpvRequired") else None

    async def documents(self):
        """RegulatoryDocumentsComponent, 'sign manually' path: the undertaking is generated, then a
        signed copy is uploaded (no digital signing). CPV / statement only when the server
        requires them; `--reupload-cpv` adds the APNT-350 re-upload (reason 'customer')."""
        required = self.app.get("requiredLoanAmount") or self.amount
        await self._post("/api/bt-process/generate-undertaking",
                         {"businessTransferId": int(self.bt_id), "customerId": self._customer_id()})
        body = {
            "customerId": self._customer_id(), "businessTransferId": int(self.bt_id),
            "isCpvEditable": self.app.get("isCpvEditable"),
            "pawnTicket": [await self._upload("pdf")],
            "signedCheque": [await self._upload("pdf") for _ in range(cheques_required(required))],
            "neftRtgsForm": [],
            "oldLoanLedgerOrSoa": [],
            "statementItrSalarySlip": [await self._upload("pdf")] if self.flags.get("isStatementItrSsRequired") else [],
            "creditBureauReport": [],
            "undertakingDocument": [await self._upload("pdf")],
            "liveImage": await self._upload("image"),
            "latitude": LIVE_IMAGE_LOCATION[0],
            "longitude": LIVE_IMAGE_LOCATION[1],
            "referenceId": None,
            "cpvImage": await self._cpv_image(),
        }
        if self.reupload_cpv:
            body["reUploadCpv"] = await self._upload("image", reason="customer")
        await self._post("/api/bt-process/upload-document", body)
        print(f"Documents: pawn ticket, {len(body['signedCheque'])} cheque(s), undertaking, live image"
              + (", statement" if body["statementItrSalarySlip"] else "")
              + (", CPV" if body["cpvImage"] else "") + (", CPV re-upload" if self.reupload_cpv else "") + ".")

    async def approve(self):
        """One approval: RCH takes --rch-decision, every later approver approves."""
        stage = self.stage
        decision = self.rch_decision if stage == 6 else "approved"
        key, body = build_approval(self._customer_id(), self.bt_id, stage, decision)
        await self._as_role(self.approver, lambda: self._post(f"/api/bt-process/update-rating-{key}", body))
        self.decisions.append((body["requestFor"], decision))
        print(f"{body['requestFor']}: {decision} (as {self.approver}).")
        return decision

    # --- driver -------------------------------------------------------------------------------

    STEP_BY_STAGE = {1: "existing_info", 2: "ornaments", 3: "scheme", 4: "bank", 5: "documents"}

    def step_for(self, stage):
        if stage in self.STEP_BY_STAGE:
            return self.STEP_BY_STAGE[stage]
        if stage in APPROVAL_STAGES:
            return "approve"
        return None

    async def drive(self, stop_before_documents=False, log_step=print):
        """Run the application from its current stage. Returns the reason it stopped."""
        await self.load()
        for _ in range(16):   # 10 stages at most; a stage that never moves must surface
            stage = self.stage
            if stop_before_documents and stage >= DOCUMENTS_STAGE:
                return "stopped before documents" if stage == DOCUMENTS_STAGE else f"already past documents ({self.stage_name})"
            step = self.step_for(stage)
            if step is None:
                return "approved" if stage >= APPLICATION_APPROVED_STAGE else f"stage {stage} ({self.stage_name})"
            log_step(f"BT {self.bt_unique_id} - stage {stage}: {BT_STAGE_NAMES.get(stage, self.stage_name)}")
            result = await getattr(self, step)()
            self.steps_done.append(step)
            await self.load()
            if step == "approve" and result != "approved":
                return f"RCH decision '{result}' (stage now {self.stage}: {self.stage_name})"
            if self.stage == stage:
                raise BtLoanError(f"BT {self.bt_unique_id} did not leave stage {stage} ({self.stage_name}) "
                                  f"after {step}.")
        raise BtLoanError(f"BT {self.bt_unique_id} kept moving without reaching an end stage.")

    def summary(self) -> dict:
        app = self.app
        return {
            "BT application": f"{self.bt_unique_id} (id {self.bt_id})",
            "Appraiser request": self.request_id or "-",
            "Stage": f"{self.stage} - {self.stage_name}",
            "Existing lender": (app.get("existingBtInfo") or {}).get("bankNbfcName") or "-",
            "BT amount": app.get("requiredLoanAmount") or "-",
            "Partner / scheme": (f"{(app.get('partner') or {}).get('name')} / {(app.get('scheme') or {}).get('schemeName')}"
                                 if app.get("scheme") else "-"),
            "Collateral value": app.get("collateralValueOfOrnaments") or "-",
            "Approvals": ", ".join(f"{label} {d}" for label, d in self.decisions) or "-",
            "Advance request": app.get("advanceRequestId") or "-",
        }
