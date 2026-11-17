"""Customer CPV (Contact Point Verification) -- the portal's CPV form, completed through the API.

Source of truth: the TEST portal's lazy chunk 46.js (CpvService, CpvFormComponent at
/admin/cpv/form?customerId&customerKycId) and chunk 48.js (AppliedKycComponent.cpvStatus / doCpv:
Applied KYC -> "CPV"). Live-verified on TEST 17 Nov 2026 (customer MWK4XHP4 -> CPV 69, Approved).

The form
  personMetName, relationshipWithCustomer, isNameSameAsSystem, isAddressSameAsSystem,
  verifiedAddress, residenceOwnership, landmarkArea, yearsAtCurrentAddress, neighbourhoodFeedback,
  endUser, remarks  +  two geo-tagged photos {url, path, lat, lng, capturedAt, label} labelled
  'Selfie with person met' / 'Image of the residence' (uploaded with reason 'customer').
  POST /api/customer-cpv/create {customerId, customerKycId, ...form, photos, customerSignatureUrl}.

Signature -- PLACEHOLDER, TEST/UAT ONLY
  The portal gets customerSignatureUrl from a real Digio e-sign (POST customer-cpv/digi-sign ->
  referenceId, then GET loan-process/digi-sign-file?documentFrom=cpv answers 406 "Digi sign is
  requested" until the CUSTOMER signs with an OTP). That cannot run unattended, and the server only
  checks that a URL is present ("Customer Signature Url is required"). By the user's decision
  (17 Nov) the harness uploads a placeholder image and sends its URL instead -- so a harness CPV
  is NOT a signed CPV; never use this against production.

Status
  GET /api/customer-cpv/customer/<customerId>: 404 "No CPV record found" = none; else data
  {cpvStatus ("Approved"), isReKycInitiated, expiresAt}. The portal allows a new CPV only when
  there is none, or the current one is invalidated by a re-KYC (isReKycInitiated) -- and an
  expired one counts as missing (AppliedKycComponent.cpvStatus -> 'expired').
  Photos are tagged with the branch's coordinates (kyc-form-review internalBranch.latitude /
  longitude): the portal refuses photos further than cpvGeoDeviationMeters from it.
"""

import datetime

import httpx

PHOTO_LABELS = ("Selfie with person met", "Image of the residence")
UPLOAD_REASON = "customer"
DEFAULT_LOCATION = (18.9522622, 72.8314101)   # TEST branch 1 (Augmont, Mumbai), if none is listed


class CpvError(RuntimeError):
    """CPV cannot be completed (with the reason)."""


def _utc_iso(now=None) -> str:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def cpv_state(record, now=None):
    """'none' | 'valid' | 'expired' | 're-kyc' for a customer-cpv record (None = no record)."""
    if not record:
        return "none"
    if record.get("isReKycInitiated"):
        return "re-kyc"
    expires = record.get("expiresAt")
    if expires:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        try:
            when = datetime.datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if when <= now:
                return "expired"
        except ValueError:
            pass
    return "valid"


def build_cpv_form(customer_name, residence_address=""):
    """The CpvFormComponent form, as a field officer who met the customer in person fills it."""
    return {
        "personMetName": customer_name,
        "relationshipWithCustomer": "Self",
        "isNameSameAsSystem": True,
        "isAddressSameAsSystem": True,
        "verifiedAddress": residence_address or "Residence address verified in person",
        "residenceOwnership": "Self-owned",
        "landmarkArea": "",
        "yearsAtCurrentAddress": 5,
        "neighbourhoodFeedback": "urban",
        "endUser": "Self",
        "remarks": "CPV completed by API test (placeholder signature)",
    }


def build_photo(upload, label, location, now=None):
    file = upload["uploadFile"]
    return {"url": file.get("URL") or file["path"], "path": file.get("path") or file.get("URL"),
            "lat": location[0], "lng": location[1], "capturedAt": _utc_iso(now), "label": label}


class CustomerCpv:
    """CPV for the suite's current customer (customer_id set, logged in)."""

    def __init__(self, suite):
        self.suite = suite
        self.record = None
        self.created = False

    async def _get(self, path):
        response = await self.suite._make_authenticated_request("GET", path)
        return response.json() if response.content else {}

    async def current(self):
        """The customer's CPV record, or None."""
        try:
            data = await self._get(f"/api/customer-cpv/customer/{self.suite.customer_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        record = data.get("data") if isinstance(data, dict) else None
        return (record[0] if record else None) if isinstance(record, list) else record

    async def _review(self):
        suite = self.suite
        if not suite.customer_kyc_id:
            await suite.get_kyc_customer_detail()
        if not suite.customer_kyc_id:
            raise CpvError(f"Customer {suite.customer_unique_id} has no KYC record; CPV needs one "
                           "(complete the KYC first).")
        data = await self._get(f"/api/kyc/kyc-form-review?customerId={suite.customer_id}"
                               f"&customerKycId={suite.customer_kyc_id}")
        return (data.get("customerKycReview") if isinstance(data, dict) else None) or {}

    async def complete(self):
        """Create a CPV unless a valid one exists. Returns the CPV record."""
        suite = self.suite
        existing = await self.current()
        state = cpv_state(existing)
        if state == "valid":
            self.record = existing
            print(f"CPV already complete for {suite.customer_unique_id}: id {existing.get('id')}, "
                  f"{existing.get('cpvStatus')}, expires {str(existing.get('expiresAt'))[:10]} -- not redone.")
            return existing
        if state != "none":
            print(f"Existing CPV {existing.get('id')} is {state}; a new CPV is due.")

        review = await self._review()
        if str(review.get("kycStatus") or suite.kyc_status or "").lower() not in ("approved", ""):
            print(f"Note: KYC status is {review.get('kycStatus')!r}; the portal offers CPV from Applied KYC.")
        branch = review.get("internalBranch") or {}
        location = ((float(branch["latitude"]), float(branch["longitude"]))
                    if branch.get("latitude") and branch.get("longitude") else DEFAULT_LOCATION)
        addresses = review.get("customerKycAddress") or []
        residence = next((a for a in addresses if a.get("addressType") == "residential"), addresses[0] if addresses else {})
        name = " ".join(x for x in (review.get("firstName") or suite.first_name,
                                    review.get("lastName") or suite.last_name) if x).strip() or suite.full_name

        photos = [build_photo(await suite._upload_file(UPLOAD_REASON, "image"), label, location)
                  for label in PHOTO_LABELS]
        signature = await suite._upload_file(UPLOAD_REASON, "image")
        body = {"customerId": str(suite.customer_id), "customerKycId": str(suite.customer_kyc_id),
                **build_cpv_form(name, residence.get("address") or ""),
                "photos": photos,
                "customerSignatureUrl": signature["uploadFile"].get("URL") or signature["uploadFile"]["path"]}
        response = await suite._make_authenticated_request("POST", "/api/customer-cpv/create", json_data=body)
        answer = response.json() if response.content else {}
        self.record = (answer.get("data") if isinstance(answer, dict) else None) or await self.current()
        self.created = True
        invalid = [p.get("label") for p in (self.record or {}).get("photos") or [] if p.get("isValid") is False]
        print(f"CPV created for {suite.customer_unique_id}: id {self.record.get('id')}, "
              f"{self.record.get('cpvStatus')}, expires {str(self.record.get('expiresAt'))[:10]}"
              + (f"; photos flagged invalid: {', '.join(invalid)}" if invalid else "") + ".")
        return self.record
