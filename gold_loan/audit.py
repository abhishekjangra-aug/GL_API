"""Gold-loan AUDIT on a completed loan (TEST only for now) -- the portal's auditor journey.

    admin    POST /api/audit/assign {auditorId, masterLoanId, auditType: "Audit"}
             (Unaudited loans -> Assign Auditor; RequestAuditDialogComponent)
    admin    POST /api/packet + PUT /api/packet/{id} {auditorId, userType: "auditor"}
             -- a NEW packet for the auditor to re-seal the ornaments into
    auditor  GET  /api/audit/auditor-loan                        (Auditor Loan list -> START AUDIT)
    auditor  GET  /api/loan-process/single-loan-customer?masterLoanId=   (AuditCalculationComponent)
    auditor  re-checks every ornament: the net weight and the purity (ltvPercent) come out a little
             different from the appraiser's, netWtAfterPurity recomputed (SharedService.getPurityWeight)
    auditor  GET  /api/packet/available-packet?masterLoanId=  + 4 packet photos (upload-file/base)
    auditor  POST /api/audit/submit {auditorOrnaments, oldOrnaments, auditorPackets, oldPacket,
                                     masterLoanId, auditNote, isOldPacketInUse}
    admin    GET  single-loan-customer again: appraiser vs auditor values side by side.

The auditor login is role "auditor" in ENVIRONMENTS (TEST: 8880008886). Like every role it uses the
saved sessions and the password recovery in gold_loan/harness.py.

OPEN AUDIT (--audit-type Open-audit) -- the same re-check and submit, assigned and closed differently
(portal chunk 13: OpenAuditAssignmentComponent, AuditorsLoanComponent.startAudit,
AssignCustodianComponent, CustodianRequestApprovalComponent, CustodianLoanListComponent):
    admin     POST /api/audit/assign-multiple {internalBranchId, auditorId, startDate, endDate,
              auditType: "Open-audit"}  -- a whole branch, optionally limited to a date range
              (YYYY-MM-DD); the run uses the loan's own start date for both, so only that day's loans
    auditor   START AUDIT is blocked until the branch's custodian has Accepted
              (loan.internalBranch.custodianStatus: Not Assigned / Pending / Accepted / Rejected):
              POST /api/audit/custodian-assign {internalBranchId, custodianId}
              (custodianId from GET /api/user/internal-branch-user-list?internalBranchId=)
    custodian GET  /api/audit/custodian-status-requests?internalBranchId=  -> data.custodianStatus
              POST /api/audit/custodian-status {internalBranchId, status: "Accepted"}
              The custodian works in the branch of its TOKEN, so a login whose default branch is
              another one switches first: GET /api/auth/change-user-branch?branchId= (new Token;
              the old one stays valid) -- and is switched back at the end.
    auditor   the audit itself, as above (POST /api/audit/submit -- same body for every type)
    custodian GET  /api/audit/custodian-requests-list?from&to&internalBranchId  (rows id, masterLoanId,
              masterLoan.auditStatus "Custodian Approval")
              POST /api/audit/custodian-requests-status {status: "accepted", internalBranchId,
              ids: [row id], masterLoanIds: [masterLoanId]}
"""

import random
from decimal import Decimal, ROUND_HALF_UP


class AuditError(RuntimeError):
    """The audit could not be done (wrong loan, not assignable, nothing changed...)."""


# The ornament fields the portal's ornaments form carries (OrnamentsComponent.addmore) -- what
# auditorOrnaments is made of. Server-side extras (ids of the loan, timestamps) are left out.
ORNAMENT_FORM_FIELDS = (
    "ornamentType", "ornamentTypeId", "quantity", "grossWeight", "netWeight", "deductionWeight",
    "ornamentImage", "weightMachineZeroWeight", "withOrnamentWeight", "stoneTouch", "acidTest", "karat",
    "ltvRange", "purityTest", "ltvPercent", "ltvAmount", "loanAmount", "id", "currentLtvAmount",
    "ornamentImageData", "weightMachineZeroWeightData", "withOrnamentWeightData", "stoneTouchData",
    "acidTestData", "purityTestImage", "ornamentFullAmount",
)
PACKET_IMAGE_KEYS = ("emptyPacketWithNoOrnament", "sealingPacketWithCustomer", "sealingPacketWithWeight",
                     "sealedPacket")
OPEN_AUDIT = "Open-audit"
AUDIT_TYPES = ("Audit", "Re-audit", OPEN_AUDIT)
CUSTODIAN_ACCEPTED = "Accepted"           # CustodianStatus (the branch's custodian)
CUSTODIAN_LOAN_ACCEPTED = "accepted"      # CustodianRequestLoanStatus (one audited loan)
# masterLoan.auditStatus values (admin Unaudited Loans rows)
UNAUDITED = "Un-audited"
OPEN_AUDIT_INITIATED = "Open-audit Initiated"   # open-audit assigned, not submitted yet
CUSTODIAN_APPROVAL = "Custodian Approval"       # open audit submitted, awaiting the custodian


def open_audit_assignment_body(branch_id, auditor_id, start_date=None, end_date=None) -> dict:
    """OpenAuditAssignmentComponent.assignOpenAudit: the form plus auditType, dates as YYYY-MM-DD
    (or null for no bound)."""
    return {"internalBranchId": int(branch_id), "auditorId": int(auditor_id),
            "startDate": str(start_date)[:10] if start_date else None,
            "endDate": str(end_date)[:10] if end_date else None,
            "auditType": OPEN_AUDIT}


def custodian_loan_approval_body(branch_id, rows, status=CUSTODIAN_LOAN_ACCEPTED) -> dict:
    """CustodianLoanListComponent.statusApproval for the selected custodian-request rows."""
    return {"status": status, "internalBranchId": int(branch_id),
            "ids": [r.get("id") for r in rows], "masterLoanIds": [r.get("masterLoanId") for r in rows]}


def purity_weight(net_weight, ltv_percent) -> str:
    """SharedService.getPurityWeight: net * ltv% / 100, 2 decimals (decimal.js default rounding =
    ROUND_HALF_UP)."""
    value = Decimal(str(net_weight)) * Decimal(str(ltv_percent)) / Decimal(100)
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class AuditFlow:
    def __init__(self, suite, loan_unique_id=None, master_loan_id=None, weight_change=None,
                 purity_change=None, old_packet=False, audit_type="Audit", note=None, seed=None,
                 custodian_role="appraiser"):
        self.s = suite
        self.loan_unique_id = loan_unique_id
        self.master_loan_id = str(master_loan_id) if master_loan_id else None
        self.weight_change = weight_change      # grams off the net weight; None = random 0.05-0.25
        self.purity_change = purity_change      # ltvPercent points; None = one step down the range
        self.old_packet = old_packet
        if audit_type not in AUDIT_TYPES:
            raise AuditError(f"audit type must be one of {AUDIT_TYPES}, not {audit_type!r}")
        self.audit_type = audit_type
        self.custodian_role = custodian_role
        self.branch_id = None            # the loan's branch (open audit works per branch)
        self.custodian_id = None
        self._custodian_token = None
        self._custodian_home_branch = None   # switched back to this at the end
        self.custodian_steps = []        # what the custodian did, for the summary
        self.note = note
        self.rand = random.Random(seed)
        self.auditor_id = self.auditor_branch = None
        self.loan = {}
        self.auditor_packet = {}
        self.changes = []        # (ornament, field, appraiser value, auditor value)
        self.result = {}

    # --- sessions ------------------------------------------------------------------------------

    async def _as(self, role: str):
        self.s.auth_token = await self.s._role_token(role)

    async def _get(self, path):
        return (await self.s._make_authenticated_request('GET', path)).json()

    async def _post(self, path, body):
        return (await self.s._make_authenticated_request('POST', path, json_data=body)).json()

    async def auditor_identity(self):
        import jwt
        token = await self.s._role_token("auditor")
        claims = jwt.decode(token, options={"verify_signature": False})
        self.auditor_id, self.auditor_branch = claims.get("id"), claims.get("internalBranchId")
        if "Auditor" not in (claims.get("roleName") or []):
            raise AuditError(f"The auditor login ({self.s._get_mobile_number_for_login_type('auditor')}) "
                             f"is not an Auditor: roles {claims.get('roleName')}")
        print(f"Auditor: {claims.get('firstName')} {claims.get('lastName')} (user {self.auditor_id}, "
              f"branch {self.auditor_branch}).")

    # --- which loan ----------------------------------------------------------------------------

    async def resolve_loan(self):
        """--loan AUGM-x / --master-loan-id, else the newest completed, unaudited loan of the
        auditor's branch (admin's Unaudited Loans list)."""
        await self._as("admin")
        if self.master_loan_id:
            return
        if self.loan_unique_id:
            row = await self.s.find_loan_row(self.loan_unique_id)
            if not row:
                raise AuditError(f"No loan {self.loan_unique_id} on {self.s.env_name.upper()}.")
            self.master_loan_id = str(row.get("masterLoanId") or row.get("id"))
            return
        rows = (await self._get("/api/audit/un-audited-loan?from=1&to=50")).get("data") or []
        for row in rows:
            # Only "Un-audited" loans: any other auditStatus ("Open-audit Initiated", "Custodian
            # Approval") is an audit already under way, often another tester's.
            if (str(row.get("internalBranchId")) == str(self.auditor_branch) and row.get("isLoanCompleted")
                    and not row.get("isOrnamentsReleased")
                    and row.get("auditStatus") in (None, UNAUDITED)):
                self.master_loan_id = str(row["id"])
                self.branch_id = str(row.get("internalBranchId"))
                print(f"Picked the newest unaudited loan of branch {self.auditor_branch}: master loan "
                      f"{self.master_loan_id}.")
                return
        raise AuditError(f"No completed, unaudited loan in branch {self.auditor_branch}; pass --loan.")

    async def load_loan(self):
        """AuditCalculationComponent.getLoanDetails: single-loan-customer, with the unaudited
        ornaments / packets taking the place of the current ones when the server holds them."""
        data = (await self._get(f"/api/loan-process/single-loan-customer?masterLoanId={self.master_loan_id}")
                ).get("data") or {}
        if not data:
            raise AuditError(f"single-loan-customer returned nothing for master loan {self.master_loan_id}.")
        if data.get("loanUnAuditedOrnamentsDetail"):
            data["loanOrnamentsDetail"] = [dict(o, id=o.get("oldOrnamentId"))
                                           for o in data["loanUnAuditedOrnamentsDetail"]]
        if data.get("loanUnAuditedPacketDetails"):
            first = dict(data["loanUnAuditedPacketDetails"][0])
            first["packets"] = [dict(p, customerLoanOrnamentsDetails=[
                dict(e, id=e.get("oldOrnamentId")) for e in p.get("customerLoanUnAuditedOrnamentsDetails") or []])
                for p in first.get("packets") or []]
            data["loanPacketDetails"] = [first]
        stage = (data.get("loanStage") or {}).get("name")
        if not data.get("loanOrnamentsDetail") or not data.get("loanPacketDetails"):
            raise AuditError(f"Master loan {self.master_loan_id} (stage {stage!r}) has no ornaments / packet "
                             "to audit; it must be a completed loan.")
        self.loan = data
        unique = ((data.get("customerLoan") or [{}])[0] or {}).get("loanUniqueId")
        self.loan_unique_id = self.loan_unique_id or unique
        print(f"Loan {unique} (master {self.master_loan_id}): stage {stage!r}, "
              f"{len(data['loanOrnamentsDetail'])} ornament(s).")

    # --- assignment + the auditor's packet -------------------------------------------------------

    async def assign_auditor(self):
        await self._as("admin")
        try:
            answer = await self._post("/api/audit/assign", {"auditorId": self.auditor_id,
                                                             "masterLoanId": int(self.master_loan_id),
                                                             "auditType": self.audit_type})
            print(f"Assigned to auditor {self.auditor_id} ({self.audit_type}): {answer.get('message')}")
        except Exception as e:
            response = getattr(e, "response", None)
            text = response.text if response is not None else str(e)
            if response is None or response.status_code != 400:
                raise
            print(f"Assign answered 400 ({text[:160]}); checking the auditor's list.")
        await self._auditor_row()

    async def _auditor_row(self) -> dict:
        """The loan's row in the auditor's list (Auditor Loan screen), or AuditError."""
        await self._as("auditor")
        rows = (await self._get("/api/audit/auditor-loan?from=1&to=100")).get("data") or []
        mine = next((r for r in rows if str(r.get("id")) == str(self.master_loan_id)
                     or str(r.get("masterLoanId")) == str(self.master_loan_id)), None)
        if not mine:
            raise AuditError(f"Master loan {self.master_loan_id} is not in auditor {self.auditor_id}'s list "
                             f"after the assignment ({len(rows)} loan(s) listed).")
        print(f"In the auditor's list: auditType={mine.get('auditType')!r}, "
              f"audited count={mine.get('auditCount')}, branch custodian="
              f"{(mine.get('internalBranch') or {}).get('custodianStatus')!r}.")
        return mine

    # --- open audit: branch assignment + custodian -----------------------------------------------

    def _loan_branch(self):
        first = (self.loan.get("customerLoan") or [{}])[0] or {}
        branch = self.branch_id or self.loan.get("internalBranchId") or first.get("internalBranchId")
        if not branch:
            raise AuditError(f"Cannot tell which branch master loan {self.master_loan_id} belongs to.")
        self.branch_id = str(branch)
        return self.branch_id

    def _loan_start_date(self):
        first = (self.loan.get("customerLoan") or [{}])[0] or {}
        return str(self.loan.get("loanStartDate") or first.get("loanStartDate") or "")[:10] or None

    async def assign_open_audit(self):
        """Admin: Open Audit Assignment for the loan's branch, limited to the loan's start date so
        only that day's loans of the branch are handed to the auditor."""
        branch = self._loan_branch()
        await self._as("admin")
        auditors = (await self._get(f"/api/user/auditor-list?internalBranchId={branch}")).get("data") or []
        if not any(str(a.get("id")) == str(self.auditor_id) for a in auditors):
            raise AuditError(f"Auditor {self.auditor_id} is not an auditor of branch {branch} "
                             f"(auditor-list: {[a.get('id') for a in auditors]}).")
        day = self._loan_start_date()
        body = open_audit_assignment_body(branch, self.auditor_id, day, day)
        try:
            answer = await self._post("/api/audit/assign-multiple", body)
            print(f"Open audit assigned: branch {branch}, loans started {day or 'any day'} -> auditor "
                  f"{self.auditor_id}: {answer.get('message')}")
        except Exception as e:
            response = getattr(e, "response", None)
            if response is None or response.status_code != 400:
                raise
            print(f"assign-multiple answered 400 ({response.text[:200]}); checking the auditor's list.")
        return await self._auditor_row()

    async def _custodian_session(self):
        """The custodian's token, working in the loan's branch (switching branch if its default is
        another one, as the portal's branch selector does)."""
        import jwt
        if self._custodian_token:
            self.s.auth_token = self._custodian_token
            return
        token = await self.s._role_token(self.custodian_role)
        claims = jwt.decode(token, options={"verify_signature": False})
        self.custodian_id = claims.get("id")
        if str(claims.get("internalBranchId")) != str(self.branch_id):
            self.s.auth_token = token
            switched = await self._get(f"/api/auth/change-user-branch?branchId={self.branch_id}")
            if not switched.get("Token"):
                raise AuditError(f"change-user-branch to {self.branch_id} gave no token: {switched}")
            self._custodian_home_branch = claims.get("internalBranchId")
            token = switched["Token"]
            print(f"Custodian ({self.custodian_role}, user {self.custodian_id}) switched from branch "
                  f"{self._custodian_home_branch} to {self.branch_id}.")
        self._custodian_token = token
        self.s.auth_token = token

    async def restore_custodian_branch(self):
        """Put the custodian login back on its own branch (best effort)."""
        if not (self._custodian_token and self._custodian_home_branch):
            return
        self.s.auth_token = self._custodian_token
        try:
            answer = await self._get(f"/api/auth/change-user-branch?branchId={self._custodian_home_branch}")
            token = answer.get("Token")
            if token:
                # The switch retires the earlier sessions of this login: keep the new one as THE
                # saved session, so the next run does not meet a 401 and log in again.
                mobile = self.s._get_mobile_number_for_login_type(self.custodian_role)
                self.s._role_token_cache[mobile] = token
                self.s._token_mobile[token] = mobile
                self.s._session_store(mobile, token, answer.get("userDetails") or {})
            print(f"Custodian switched back to branch {self._custodian_home_branch}.")
        except Exception as e:
            print(f"Could not switch the custodian back to branch {self._custodian_home_branch}: {e}")

    async def ensure_custodian(self):
        """The run's custodian login must be the branch's Accepted custodian: START AUDIT on an open
        audit is blocked until the branch custodian has Accepted, and only that custodian sees the
        audits awaiting approval. custodian-status-requests answers only the user the branch's
        request is addressed to (data null for anyone else) -- so: ours and Accepted -> nothing to
        do; ours and Pending -> accept; otherwise the auditor requests ours (Assign Custodian) and
        it accepts (Custodian Request Approval). Re-requesting replaces another tester's custodian
        of that branch."""
        await self._custodian_session()
        mine = (await self._get(f"/api/audit/custodian-status-requests?internalBranchId={self.branch_id}"
                                )).get("data") or {}
        status = mine.get("custodianStatus")
        if status == CUSTODIAN_ACCEPTED:
            print(f"Branch {self.branch_id} custodian is already {self.custodian_role} "
                  f"(user {self.custodian_id}), Accepted -- nothing to do.")
            self.custodian_steps.append("custodian already Accepted")
            return
        if status != "Pending":
            await self._as("auditor")
            users = (await self._get(f"/api/user/internal-branch-user-list?internalBranchId={self.branch_id}")
                     ).get("data") or []
            if not any(str(u.get("id")) == str(self.custodian_id) for u in users):
                raise AuditError(f"The custodian login ({self.custodian_role}, user {self.custodian_id}) is not a "
                                 f"user of branch {self.branch_id}; pick another with --custodian.")
            answer = await self._post("/api/audit/custodian-assign", {"internalBranchId": int(self.branch_id),
                                                                       "custodianId": int(self.custodian_id)})
            print(f"Auditor requested {self.custodian_role} (user {self.custodian_id}) as custodian of branch "
                  f"{self.branch_id}: {answer.get('message')}")
            self.custodian_steps.append(f"custodian {self.custodian_id} requested")
            await self._custodian_session()
            mine = (await self._get(f"/api/audit/custodian-status-requests?internalBranchId={self.branch_id}"
                                    )).get("data") or {}
            print(f"Custodian sees the request: custodianStatus={mine.get('custodianStatus')!r}.")
        answer = await self._post("/api/audit/custodian-status", {"internalBranchId": int(self.branch_id),
                                                                   "status": CUSTODIAN_ACCEPTED})
        print(f"Custodian accepted: {answer.get('message')}")
        self.custodian_steps.append("custodian Accepted")
        mine = (await self._get(f"/api/audit/custodian-status-requests?internalBranchId={self.branch_id}"
                                )).get("data") or {}
        if mine.get("custodianStatus") != CUSTODIAN_ACCEPTED:
            raise AuditError(f"Branch {self.branch_id} custodian is still {mine.get('custodianStatus')!r} "
                             "after accepting.")

    async def audit_status(self):
        """(auditStatus, auditType) of the loan from the admin's Unaudited Loans list -- e.g.
        ('Custodian Approval', 'Open-audit') once an open audit is submitted. single-loan-customer
        does not carry it. Scans the list in pages (no search filter is sent: these listings 500 on
        an unknown query parameter)."""
        await self._as("admin")
        start, size = 1, 500
        while True:
            page = await self._get(f"/api/audit/un-audited-loan?from={start}&to={start + size - 1}")
            rows = page.get("data") or []
            row = next((r for r in rows if str(r.get("id")) == str(self.master_loan_id)), None)
            if row:
                if not self.branch_id and row.get("internalBranchId"):
                    self.branch_id = str(row["internalBranchId"])
                return row.get("auditStatus"), row.get("auditType")
            # Pages can come back a little short (498 of 500 rows), so stop on the reported count.
            start += size
            if not rows or start > int(page.get("count") or 0):
                return None, None

    async def _custodian_rows(self):
        """The loan's rows in the custodian's request list: filtered by loanUniqueId (the screen's
        own Loan ID filter), else every page scanned (the list holds the branch's whole history --
        1441 rows on TEST branch 1). Returns (rows, totalCount)."""
        base = f"/api/audit/custodian-requests-list?internalBranchId={self.branch_id}"

        def ours(listing):
            return [r for r in listing.get("data") or [] if str(r.get("masterLoanId")) == str(self.master_loan_id)]
        total = 0
        if self.loan_unique_id:
            listing = await self._get(f"{base}&from=1&to=100&loanUniqueId={self.loan_unique_id}")
            total = listing.get("totalCount")
            if ours(listing):
                return ours(listing), total
        start, size = 1, 500
        while True:
            listing = await self._get(f"{base}&from={start}&to={start + size - 1}")
            total = listing.get("totalCount") or 0
            if ours(listing):
                return ours(listing), total
            start += size
            if not listing.get("data") or start > int(total):
                return [], total

    async def _accept_custodian_rows(self, stage):
        """Custodian: Accept the loan's pending request row(s). The row is created when the loan is
        open-audit assigned ("Open-audit Initiated", auditorReqData null) and only becomes
        acceptable once the auditor's submit is attached to it (auditorReqData; the loan is then
        "Custodian Approval") -- accepting it earlier 500s "Cannot read properties of null
        (reading 'body')". So `stage` 'approval' accepts only rows with the audit attached."""
        await self._custodian_session()
        rows, total = await self._custodian_rows()
        pending = [r for r in rows if r.get("status") == "pending"]
        wanted = [r for r in pending if bool(r.get("auditorReqData")) == (stage == "approval")]
        print(f"Custodian request(s) for the loan: {[(r.get('id'), r.get('status'), (r.get('masterLoan') or {}).get('auditStatus'), 'audit attached' if r.get('auditorReqData') else 'no audit yet') for r in rows]}")
        if not wanted:
            raise AuditError(f"No pending custodian {stage} request for master loan {self.master_loan_id} "
                             f"({len(rows)} row(s) for it among {total}).")
        answer = await self._post("/api/audit/custodian-requests-status",
                                  custodian_loan_approval_body(self.branch_id, wanted))
        print(f"Custodian accepted the {stage} request {[r.get('id') for r in wanted]}: {answer.get('message')}")
        self.custodian_steps.append(f"{stage} accepted (request {', '.join(str(r.get('id')) for r in wanted)})")

    async def custodian_approve_audit(self):
        """After the submit: the custodian approves the audited loan (Custodian Loan list -> Accept)."""
        await self._accept_custodian_rows("approval")

    async def create_auditor_packet(self):
        """A fresh packet bound to the auditor (admin): the same create + assign the appraiser's
        packet gets, with auditorId and userType 'auditor'."""
        await self._as("admin")
        unique = f"pac{self.rand.randint(10000000, 99999999)}"
        base = {"packetUniqueId": unique, "barcodeNumber": unique, "isNewPacket": False,
                "internalUserBranch": int(self.auditor_branch or 1)}
        await self._post("/api/packet", dict(base, id=None, appraiserId=None, auditorId=None, userType=""))
        packet = await self.s._find_packet_by_unique_id(unique)
        if not packet:
            raise AuditError(f"Created packet {unique} not found in the packet list.")
        await self.s._make_authenticated_request('PUT', f"/api/packet/{packet['id']}", json_data=dict(
            base, id=packet["id"], appraiserId=None, auditorId=self.auditor_id, userType="auditor"))
        self.auditor_packet = packet
        print(f"New packet for the auditor: {unique} (id {packet['id']}).")

    # --- the auditor's re-check ----------------------------------------------------------------

    def _new_purity(self, ornament) -> str:
        current = Decimal(str(ornament.get("ltvPercent") or 0))
        if self.purity_change is not None:
            return self._plain(current - Decimal(str(self.purity_change)))
        steps = sorted({Decimal(str(v)) for v in (ornament.get("ltvRange") or []) if str(v).strip()})
        lower = [v for v in steps if v < current]
        return self._plain(lower[-1] if lower else current - 1)

    @staticmethod
    def _plain(value: Decimal) -> str:
        """A purity as the portal types it: 90, 91.6 -- never '9E+1' (str(Decimal('90').normalize())
        is scientific notation, and the server stores that string as-is)."""
        text = format(value.normalize(), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    def audited_ornaments(self) -> list:
        """The appraiser's ornaments as the auditor finds them: a little lighter (net weight down,
        deduction up by the same amount, gross unchanged) and a step lower in purity."""
        out = []
        for orn in self.loan["loanOrnamentsDetail"]:
            row = {k: orn.get(k) for k in ORNAMENT_FORM_FIELDS}
            name = (orn.get("ornamentType") or {}).get("name") or f"ornament {orn.get('id')}"
            net = Decimal(str(orn.get("netWeight") or 0))
            delta = Decimal(str(self.weight_change if self.weight_change is not None
                                else self.rand.choice(["0.05", "0.08", "0.10", "0.12", "0.15", "0.20", "0.25"])))
            if delta <= 0 or delta >= net:
                raise AuditError(f"Weight change {delta} g is not usable on {name} (net {net} g).")
            new_net = (net - delta).quantize(Decimal("0.01"))
            new_deduction = (Decimal(str(orn.get("deductionWeight") or 0)) + delta).quantize(Decimal("0.01"))
            new_ltv = self._new_purity(orn)
            row.update(netWeight=str(new_net), deductionWeight=str(new_deduction), ltvPercent=new_ltv,
                       netWtAfterPurity=purity_weight(new_net, new_ltv))
            for field, before, after in (("netWeight", orn.get("netWeight"), row["netWeight"]),
                                         ("deductionWeight", orn.get("deductionWeight"), row["deductionWeight"]),
                                         ("ltvPercent", orn.get("ltvPercent"), row["ltvPercent"]),
                                         ("netWtAfterPurity", orn.get("netWtAfterPurity"), row["netWtAfterPurity"])):
                self.changes.append((name, field, before, after))
            out.append(row)
        return out

    async def auditor_packets(self) -> dict:
        """The packet step in audit mode: photos of the (new) packet and which ornaments it holds."""
        await self._as("auditor")
        ornaments = self.loan["loanOrnamentsDetail"]
        ids = [o.get("id") for o in ornaments]
        names = ", ".join(((o.get("ornamentType") or {}).get("name") or "") for o in ornaments)
        if self.old_packet:     # AuditCalculationComponent.submit(hasSamePacket=true)
            old = self.loan["loanPacketDetails"][0]
            body = {k: old.get(k) for k in PACKET_IMAGE_KEYS}
            body.update({f"{k}Image": old.get(f"{k}Image") for k in PACKET_IMAGE_KEYS})
            body["packetOrnamentArray"] = [{
                "packetId": p.get("id"), "ornamentsId": [e.get("id") for e in p.get("customerLoanOrnamentsDetails") or []],
                "packetsName": p.get("packetUniqueId"),
                "ornamentsName": ", ".join((e.get("ornamentType") or {}).get("name") or ""
                                           for e in p.get("customerLoanOrnamentsDetails") or [])}
                for p in old.get("packets") or []]
            return body
        available = await self._get(f"/api/packet/available-packet?masterLoanId={self.master_loan_id}")
        rows = available.get("data") if isinstance(available, dict) else available
        rows = rows if isinstance(rows, list) else []
        want = self.auditor_packet.get("packetUniqueId")
        packet = next((p for p in rows if want in (p.get("packetUniqueId"), p.get("barcodeNumber"))), None)
        if not packet:
            raise AuditError(f"The auditor's new packet {want} is not in available-packet "
                             f"({[p.get('packetUniqueId') for p in rows][:10]}).")
        body = {}
        for key in PACKET_IMAGE_KEYS:
            path = await self.s._upload_file_base(file_type="packet")
            body[key], body[f"{key}Image"] = path, f"{self.s.BASE_URL}/{path}"
        body["packetOrnamentArray"] = [{"packetId": str(packet["id"]), "ornamentsId": ids,
                                        "packetsName": packet.get("packetUniqueId"), "ornamentsName": names}]
        return body

    async def submit(self):
        ornaments = self.audited_ornaments()
        packets = await self.auditor_packets()
        old_ids = sorted(int(p["id"]) for p in self.loan["loanPacketDetails"][0].get("packets") or [])
        new_ids = sorted(int(p["packetId"]) for p in packets["packetOrnamentArray"])
        body = {
            "auditorOrnaments": ornaments,
            "oldOrnaments": self.loan["loanOrnamentsDetail"],
            "auditorPackets": packets,
            "oldPacket": self.loan["loanPacketDetails"],
            "masterLoanId": self.master_loan_id,
            "auditNote": self.note or (f"API audit: net weight and purity re-checked "
                                       f"({len(ornaments)} ornament(s)).")[:250],
            "isOldPacketInUse": old_ids == new_ids,
        }
        await self._as("auditor")
        answer = await self._post("/api/audit/submit", body)
        print(f"Audit submitted: {answer.get('message')}")

    # --- check -----------------------------------------------------------------------------------

    async def verify(self):
        await self._as("admin")
        data = (await self._get(f"/api/loan-process/single-loan-customer?masterLoanId={self.master_loan_id}")
                ).get("data") or {}
        self.result = {k: data.get(k) for k in ("auditNote", "loanAmountAfterAudit", "auditExcessMarginAmount",
                                                "auditValues")}
        unaudited = data.get("loanUnAuditedOrnamentsDetail") or []
        current = data.get("loanOrnamentsDetail") or []
        print(f"After the audit: loanOrnamentsDetail={len(current)}, loanUnAuditedOrnamentsDetail={len(unaudited)}, "
              f"auditNote={data.get('auditNote')!r}")
        if not data.get("auditNote") and not unaudited:
            raise AuditError("The loan shows no trace of the audit (no auditNote, no unaudited ornaments).")
        # The server's copy of the auditor's values (current ornaments after the audit).
        server_after = {str(o.get("id")): o for o in current}
        self.server_after = server_after
        return data

    def report(self):
        s = self.s
        rows = [(n, f, str(b), str(a)) for n, f, b, a in self.changes]
        print("\n" + s._c(f"{s._G['square']} APPRAISER vs AUDITOR", "bold", "cyan"))
        print(s._c(f"  {'ornament':<14} {'field':<18} {'appraiser':>10}   {'auditor':>10}", "gray"))
        for name, field, before, after in rows:
            colour = "yellow" if before != after else None
            line = f"  {name[:14]:<14} {field:<18} {before:>10}   {after:>10}"
            print(s._c(line, colour) if colour else line)
        s._print_summary_table("AUDIT SUMMARY", {
            "Environment": f"{s.env_name.upper()} ({s.BASE_URL})",
            "Loan": f"{self.loan_unique_id} (master {self.master_loan_id})",
            "Auditor": f"{s._get_mobile_number_for_login_type('auditor')} (user {self.auditor_id})",
            "Audit type": self.audit_type,
            **({"Branch / custodian": f"{self.branch_id} / {self.custodian_role} (user {self.custodian_id}): "
                                      + "; ".join(self.custodian_steps)} if self.audit_type == OPEN_AUDIT else {}),
            "Packet": "old packet reused" if self.old_packet else
                      f"new packet {self.auditor_packet.get('packetUniqueId')} (id {self.auditor_packet.get('id')})",
            "Loan amount after audit": self.result.get("loanAmountAfterAudit"),
            "Excess margin after audit": self.result.get("auditExcessMarginAmount"),
            "Audit note": self.result.get("auditNote"),
        })

    async def run(self):
        if self.s.env_name != "test":
            raise AuditError("The audit flow is TEST-only for now (no auditor login configured for "
                             f"{self.s.env_name.upper()}).")
        s = self.s
        s._log_step("Auditor login")
        await self.auditor_identity()
        s._log_step("Loan to audit")
        await self.resolve_loan()
        await self.load_loan()
        open_audit = self.audit_type == OPEN_AUDIT
        try:
            if open_audit:
                status, kind = await self.audit_status()   # also the loan's branch, from its row
                self._loan_branch()
                if status == CUSTODIAN_APPROVAL:
                    # Submitted by an earlier run; only the custodian's approval is left.
                    print(f"Master loan {self.master_loan_id}: {kind} already submitted, awaiting the "
                          "custodian -- resuming at the custodian approval.")
                    s._log_step("Branch custodian: auditor requests, custodian accepts")
                    await self.ensure_custodian()
                    s._log_step("Custodian approves the audited loan")
                    await self.custodian_approve_audit()
                    s._log_step("Check the audited loan")
                    await self.verify()
                    self.report()
                    return
                if status == OPEN_AUDIT_INITIATED:
                    # Already assigned for open audit (by an earlier run, or a branch-wide
                    # assignment): carry on, if it is assigned to OUR auditor.
                    row = await self._auditor_row()
                    print(f"Master loan {self.master_loan_id} is already open-audit assigned to auditor "
                          f"{self.auditor_id} -- no new assignment.")
                else:
                    s._log_step("Open audit: assign the branch's loans to the auditor (admin)")
                    await self.assign_open_audit()
                s._log_step("Branch custodian: auditor requests, custodian accepts")
                await self.ensure_custodian()
            else:
                s._log_step("Assign the loan to the auditor (admin)")
                await self.assign_auditor()
            if not self.old_packet:
                s._log_step("New packet for the auditor (admin)")
                await self.create_auditor_packet()
            s._log_step("Audit: re-check ornaments, re-seal, submit (auditor)")
            await self._as("auditor")
            await self.load_loan()           # as the auditor, like the audit screen
            await self.submit()
            if open_audit:
                s._log_step("Custodian approves the audited loan")
                await self.custodian_approve_audit()
            s._log_step("Check the audited loan")
            await self.verify()
            self.report()
        finally:
            await self.restore_custodian_branch()
