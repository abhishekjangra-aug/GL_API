# tests/test_harness.py
"""Offline regression tests for the KYC modes, the packet batch runner and the KYC runner.

Runs with NO network and NO extra dependencies (there is no pytest in this project):

    python tests/test_harness.py            # all groups
    python tests/test_harness.py kyc-body   # one group, by name prefix

Every assertion is either a byte-comparison against captured ground truth in reference/*.har or
a mock-driven behaviour check. Nothing here talks to the API, so a pass means "the requests we
build match the ones the browser sent and the control flow behaves", NOT "the server accepted it".

Node.js is required for the CryptoJS tests (the harness shells out to it for AES).
"""

import asyncio
import io
import json
import os
import re
import sys
import traceback
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ["GOLD_LOAN_COLOR"] = "off"
os.environ.setdefault("GOLD_LOAN_LOG_LEVEL", "quiet")

import httpx  # noqa: E402

import maintest  # noqa: E402
from maintest import GoldLoanApiTest as G, KycDocumentAlreadyExistsError  # noqa: E402
from test_create_packets import PacketBatchTest  # noqa: E402
from test_kyc import KycOnlyTest  # noqa: E402

# --------------------------------------------------------------------------------------------
# tiny test framework
# --------------------------------------------------------------------------------------------

TESTS = []
def test(name):
    def deco(fn):
        TESTS.append((name, fn)); return fn
    return deco

class Fail(AssertionError):
    pass

def eq(actual, expected, what=""):
    if actual != expected:
        raise Fail(f"{what}: expected {expected!r}, got {actual!r}")

def ok(cond, what=""):
    if not cond:
        raise Fail(what or "condition was false")

def quiet(fn, *a, **kw):
    """Run something noisy, swallowing its stdout but keeping it for failure messages."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()

# --------------------------------------------------------------------------------------------
# ground truth
# --------------------------------------------------------------------------------------------

def har(name):
    with open(os.path.join(ROOT, "reference", name), encoding="utf-8") as fh:
        return json.load(fh)["log"]["entries"]

E2E = har("gold-loan-e2e-flow.har")
OVD = har("ovd-kyc-flow.har")

def find(entries, marker, method=None, nth=0):
    hits = [e for e in entries
            if marker in e["request"]["url"] and (method is None or e["request"]["method"] == method)]
    if not hits:
        raise Fail(f"no captured call matching {marker!r}")
    return hits[nth]

def req_body(entry):
    return json.loads(entry["request"]["postData"]["text"])

def resp_body(entry):
    return json.loads(entry["response"]["content"]["text"])

def clean_env():
    for k in list(os.environ):
        if k.startswith("GOLD_LOAN_KYC") or k == "GOLD_LOAN_PAN_TYPE":
            del os.environ[k]
    os.environ["GOLD_LOAN_KYC_PROFILE"] = os.path.join(ROOT, "__no_such_profile__.json")

def suite(mode="manual", **env):
    """A GoldLoanApiTest with a clean KYC environment, configured via GOLD_LOAN_KYC_* vars."""
    clean_env()
    os.environ["GOLD_LOAN_KYC_MODE"] = mode
    os.environ.update(env)
    with redirect_stdout(io.StringIO()):  # the constructor prints profile/OVD notes
        s = G()
    s.auth_token = "tok"
    s.internal_branch_id = "1"
    return s

def close(s):
    asyncio.run(s.client.aclose())

def record_requests(s, responder=None):
    """Replace the auth chokepoint with a recorder. Returns the list it appends (path, body) to."""
    calls = []
    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        calls.append((method, path, json_data))
        payload = responder(path) if responder else {}
        class R:
            content = b"{}"
            def json(self_inner): return payload
        return R()
    s._make_authenticated_request = fake
    return calls

def fake_uploads(s, image="uploads/lead/img.png", pdf="uploads/lead/doc.pdf"):
    async def up(reason, file_type="image", document_type=None, is_mask=False, file_path_override=None):
        path = image if file_type == "image" else pdf
        return {"uploadFile": {"path": path, "originalname": os.path.basename(path)},
                "maskedData": {"path": path}}
    s._upload_file = up

def diff_bodies(mine, expected):
    return [(k, expected.get(k, "<absent>"), mine.get(k, "<absent>"))
            for k in sorted(set(expected) | set(mine))
            if expected.get(k, "<absent>") != mine.get(k, "<absent>")]

def http_error(status, payload, path="/x"):
    request = httpx.Request("POST", "http://host" + path)
    response = httpx.Response(status, json=payload, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


# --------------------------------------------------------------------------------------------
# GROUP: kyc-body -- generated request bodies vs the captured ones
# --------------------------------------------------------------------------------------------

@test("kyc-body: manual submit-basic-info is byte-identical to gold-loan-e2e-flow.har")
def _():
    expected = req_body(find(E2E, "submit-basic-info"))
    s = suite("manual")
    s.customer_id, s.module_id, s.kyc_type = "8463", "1", "RE_KYC"
    s.first_name, s.last_name, s.random_pan = "Peter", "Jones", "EOMPS1364U"
    calls = record_requests(s, lambda p: {"data": {"customerKycId": 2458, "customerId": 8463}})
    fake_uploads(s, image="uploads/lead/1785150237926.png")
    quiet(lambda: asyncio.run(s.submit_basic_info()))
    close(s)
    mine = [b for _m, p, b in calls if "submit-basic-info" in p][0]
    d = diff_bodies(mine, expected)
    ok(not d, f"differs from capture: {d}")
    eq(len(mine), 17, "field count")

@test("kyc-body: ovd submit-basic-info is byte-identical to ovd-kyc-flow.har")
def _():
    expected = req_body(find(OVD, "submit-basic-info"))
    s = suite("ovd", GOLD_LOAN_KYC_FIRST_NAME="Abhishek", GOLD_LOAN_KYC_LAST_NAME="Jangra",
              GOLD_LOAN_KYC_DOB="1996-11-25", GOLD_LOAN_KYC_OVD_TYPE="voterId",
              GOLD_LOAN_KYC_OVD_NUMBER="IRU0628347")
    s.customer_id, s.module_id, s.kyc_type = "8693", "1", "NEW_KYC"
    s.first_name, s.last_name = "Abhishek", "Jangra"
    voter = resp_body(find(OVD, "verify-voter-id"))
    calls = record_requests(
        s, lambda p: voter if "verify-voter-id" in p else {"data": {"customerKycId": 2603, "customerId": 8693}})
    fake_uploads(s, image="uploads/lead/1786692209728.JPG", pdf="uploads/lead/1786692212457.pdf")
    quiet(lambda: asyncio.run(s.submit_basic_info()))
    close(s)
    mine = [b for _m, p, b in calls if "submit-basic-info" in p][0]
    d = diff_bodies(mine, expected)
    ok(not d, f"differs from capture: {d}")
    eq(len(mine), 19, "field count")
    eq(mine["panType"], "ovd", "panType")
    eq(mine["panCardNumber"], None, "panCardNumber must be null on the OVD path")
    eq(mine["isAutoApproved"], True, "isAutoApproved after a verified OVD")

@test("kyc-body: verify-voter-id body matches the capture")
def _():
    entry = find(OVD, "verify-voter-id")
    s = suite("ovd", GOLD_LOAN_KYC_OVD_TYPE="voterId", GOLD_LOAN_KYC_OVD_NUMBER="IRU0628347")
    s.customer_id = "8693"
    calls = record_requests(s, lambda p: resp_body(entry))
    quiet(lambda: asyncio.run(s._verify_ovd_auto()))
    close(s)
    path, body = [(p, b) for _m, p, b in calls][0]
    eq(path, entry["request"]["url"].split("augmont.com")[-1], "endpoint path")
    eq(body, req_body(entry), "request body")

@test("kyc-body: verify-dl body matches the capture, incl. DD-MM-YYYY dob")
def _():
    entry = find(OVD, "verify-dl")
    expected = req_body(entry)
    s = suite("ovd", GOLD_LOAN_KYC_OVD_TYPE="dl", GOLD_LOAN_KYC_OVD_NUMBER=expected["dlNo"],
              GOLD_LOAN_KYC_DOB="1996-11-25")
    s.customer_id = "8693"
    sent = {}
    async def fake(method, path, json_data=None, **kw):
        sent["path"], sent["body"] = path, json_data
        raise http_error(400, resp_body(entry), path)
    s._make_authenticated_request = fake
    try:
        quiet(lambda: asyncio.run(s._verify_ovd_auto()))
    except KycDocumentAlreadyExistsError:
        pass  # expected: the captured DL is already registered
    close(s)
    eq(sent["path"], entry["request"]["url"].split("augmont.com")[-1], "endpoint path")
    eq(sent["body"], expected, "request body")
    eq(G._kyc_ddmmyyyy_dob("1996-11-25"), expected["dob"], "dob format")

@test("kyc-body: verify-pan body matches the capture, incl. ISO dob")
def _():
    entry = find(E2E, "verify-pan")
    expected = req_body(entry)
    s = suite("auto", GOLD_LOAN_KYC_PAN=expected["panCardNumber"],
              GOLD_LOAN_KYC_DOB=expected["dateOfBirth"][:10],
              GOLD_LOAN_KYC_AADHAAR="773626564863",
              GOLD_LOAN_KYC_AADHAAR_XML=os.path.join(ROOT, "config", "offline_aadhar.xml"))
    s.customer_id = str(expected["customerId"])
    sent = {}
    async def fake(method, path, json_data=None, **kw):
        sent["path"], sent["body"] = path, json_data
        raise http_error(503, resp_body(entry), path)
    s._make_authenticated_request = fake
    quiet(lambda: asyncio.run(s._verify_pan_auto()))
    close(s)
    eq(sent["path"], "/api/e-kyc/verify-pan", "endpoint path")
    eq(sent["body"], expected, "request body")
    eq(G._kyc_iso_dob("1982-08-30"), "1982-08-30T00:00:00.000Z", "dob format")

@test("kyc-body: offline-aadhaar-xml body has the captured shape (keys, mask, CryptoJS envelope)")
def _():
    entry = find(E2E, "offline-aadhaar-xml")
    expected = req_body(entry)
    xml = os.path.join(ROOT, "config", "offline_aadhar.xml")
    s = suite("auto", GOLD_LOAN_KYC_PAN="BXDPA4371D", GOLD_LOAN_KYC_DOB="1996-11-25",
              GOLD_LOAN_KYC_AADHAAR="773626564863", GOLD_LOAN_KYC_AADHAAR_XML=xml)
    s.customer_id = "8463"
    sent = {}
    async def fake(method, path, json_data=None, **kw):
        sent["path"], sent["body"] = path, json_data
        raise http_error(400, resp_body(entry), path)
    s._make_authenticated_request = fake
    quiet(lambda: asyncio.run(s._verify_aadhaar_xml_auto()))
    close(s)
    eq(sent["path"], "/api/e-kyc/offline-aadhaar-xml", "endpoint path")
    eq(sorted(sent["body"]), sorted(expected), "body keys")
    ok(sent["body"]["file"].startswith("data:application/xml;base64,"), "file data-URI prefix")
    eq(sent["body"]["maskedAadhaarNumber"], "XXXXXXXX4863", "masked aadhaar")
    ok(sent["body"]["aadhaarNumber"].startswith("U2FsdGVkX1"),
       "aadhaarNumber must be the CryptoJS 'Salted__' envelope")
    ok(expected["maskedAadhaarNumber"].startswith("XXXXXXXX"), "captured mask format")

@test("kyc-body: HMAC signature is unchanged by the KYC work")
def _():
    s = suite("manual")
    body = {"customerId": 8463, "amount": 4480.0, "zero": 0.0}
    sig = s._generate_signature("/api/kyc/v2/submit-basic-info", body)
    eq(len(sig), 64, "sha256 hex length")
    # integral floats must still normalise to ints, or the server's JSON.stringify disagrees
    eq(G._js_number_normalize({"a": 4480.0, "b": 0.0, "c": 1.5}), {"a": 4480, "b": 0, "c": 1.5},
       "float normalisation")
    close(s)


# --------------------------------------------------------------------------------------------
# GROUP: kyc-verify -- verification control flow
# --------------------------------------------------------------------------------------------

@test("kyc-verify: 3 failures with switchToManual degrade to manual and record it")
def _():
    s = suite("auto", GOLD_LOAN_KYC_PAN="EOMPS1364U", GOLD_LOAN_KYC_DOB="1982-08-30",
              GOLD_LOAN_KYC_AADHAAR="773626564863",
              GOLD_LOAN_KYC_AADHAAR_XML=os.path.join(ROOT, "config", "offline_aadhar.xml"))
    s.customer_id = "8463"
    n = {"i": 0}
    async def fake(method, path, json_data=None, **kw):
        n["i"] += 1
        raise http_error(503, {"message": "Please Try Again After Sometime",
                               "switchToManual": n["i"] >= 3,
                               "switchToManualReason": f"pan API call failed (attempt {n['i']}/3)"}, path)
    s._make_authenticated_request = fake
    (verified, _), _out = quiet(lambda: (asyncio.run(s._verify_pan_auto()), None))
    close(s)
    eq(verified, False, "must not report verified")
    eq(n["i"], 3, "attempt ceiling")
    eq(s.kyc_mode_requested, "auto", "requested mode preserved")
    eq(s.kyc_mode_effective, "manual", "degraded to manual")
    ok(any(step == "mode" and "degraded" in outcome for step, outcome, _d in s.kyc_verification_log),
       "degrade recorded in the verification log")

@test("kyc-verify: switchToManual on attempt 1 stops immediately (signal honoured, not exhausted)")
def _():
    # Distinguishes "honoured the server's signal" from "ran out of retries and degraded anyway":
    # both end in manual mode, so only the attempt COUNT proves the signal was read.
    for label, run in (
        ("PAN", lambda s: s._verify_pan_auto()),
        ("Aadhaar", lambda s: s._verify_aadhaar_xml_auto()),
    ):
        s = suite("auto", GOLD_LOAN_KYC_PAN="BXDPA4371D", GOLD_LOAN_KYC_DOB="1996-11-25",
                  GOLD_LOAN_KYC_AADHAAR="773626564863",
                  GOLD_LOAN_KYC_AADHAAR_XML=os.path.join(ROOT, "config", "offline_aadhar.xml"))
        s.customer_id = "8463"
        n = {"i": 0}
        async def fake(method, path, json_data=None, **kw):
            n["i"] += 1
            raise http_error(400, {"message": "rejected", "switchToManual": True,
                                   "reason": "switching to manual KYC",
                                   "switchToManualReason": "switching to manual KYC"}, path)
        s._make_authenticated_request = fake
        (verified, _), _o = quiet(lambda: (asyncio.run(run(s)), None))
        close(s)
        eq(verified, False, f"{label} must not report verified")
        eq(n["i"], 1, f"{label}: must stop on the first switchToManual, not retry to the ceiling")
        eq(s.kyc_mode_effective, "manual", f"{label} degraded")

@test("kyc-verify: switchToManual on an OVD 200 response stops immediately too")
def _():
    s = suite("ovd", GOLD_LOAN_KYC_OVD_TYPE="voterId", GOLD_LOAN_KYC_OVD_NUMBER="X1")
    s.customer_id = "1"
    n = {"i": 0}
    def responder(path):
        n["i"] += 1
        return {"message": "no", "switchToManual": True, "reason": "switching to manual KYC"}
    record_requests(s, responder)
    (verified, _), _o = quiet(lambda: (asyncio.run(s._verify_ovd_auto()), None))
    close(s)
    eq(verified, False, "must not report verified")
    eq(n["i"], 1, "must stop on the first switchToManual")
    eq(s.kyc_mode_effective, "manual", "degraded")

@test("kyc-verify: a successful OVD sets verified state and captures the name comparison")
def _():
    voter = resp_body(find(OVD, "verify-voter-id"))
    s = suite("ovd", GOLD_LOAN_KYC_OVD_TYPE="voterId", GOLD_LOAN_KYC_OVD_NUMBER="IRU0628347")
    s.customer_id = "8693"
    record_requests(s, lambda p: voter)
    (verified, _), _o = quiet(lambda: (asyncio.run(s._verify_ovd_auto()), None))
    close(s)
    eq(verified, True, "verified")
    eq(s.ovd_verified, True, "ovd_verified flag")
    eq(s.ovd_name, "abhishek", "name from the OVD API")
    eq(s.ovd_verification_data.get("gender"), "M", "gender from the OVD API")
    eq(s.kyc_mode_effective, "ovd", "must not degrade on success")

@test("kyc-verify: a 200 carrying isVerified=false is NOT treated as verified")
def _():
    s = suite("ovd", GOLD_LOAN_KYC_OVD_TYPE="voterId", GOLD_LOAN_KYC_OVD_NUMBER="X1")
    s.customer_id = "1"
    record_requests(s, lambda p: {"message": "could not verify", "isVerified": False,
                                  "switchToManual": False})
    (verified, _), _o = quiet(lambda: (asyncio.run(s._verify_ovd_auto()), None))
    close(s)
    eq(verified, False, "must not claim verification")
    eq(s.ovd_verified, False, "ovd_verified flag")
    eq(s.kyc_mode_effective, "manual", "degraded")

@test("kyc-verify: identityTypeId stays 5 (Aadhaar) in every mode")
def _():
    for mode, env in (("manual", {}),
                      ("ovd", {"GOLD_LOAN_KYC_OVD_TYPE": "voterId", "GOLD_LOAN_KYC_OVD_NUMBER": "X"}),
                      ("auto", {"GOLD_LOAN_KYC_PAN": "BXDPA4371D", "GOLD_LOAN_KYC_DOB": "1996-11-25",
                                "GOLD_LOAN_KYC_AADHAAR": "773626564863",
                                "GOLD_LOAN_KYC_AADHAAR_XML": os.path.join(ROOT, "config", "offline_aadhar.xml")})):
        s = suite(mode, **env)
        eq(s.kyc_identity_type_id, 5, f"identityTypeId in {mode} mode")
        close(s)

@test("kyc-verify: the OVD path never puts the OVD number in identityProofNumber")
def _():
    # Regression for the live 400 "Invalid Adhaar card format!" on customer-kyc-address /
    # submit-all-kyc-info: that section validates identityProofNumber as an Aadhaar.
    s = suite("ovd", GOLD_LOAN_KYC_OVD_TYPE="voterId", GOLD_LOAN_KYC_OVD_NUMBER="IRU0628347")
    s.customer_id, s.module_id, s.state_id, s.city_id = "8693", "1", "22", "2707"
    s.first_name, s.last_name, s.random_pincode = "Abhishek", "Jangra", 136234
    s.address_proof_type_id = "1"
    record_requests(s, lambda p: {"data": []})
    fake_uploads(s)
    async def noop(*a, **kw): return None
    s._fetch_address_proof_types = noop
    s._fetch_geo_location = noop
    quiet(lambda: asyncio.run(s.save_customer_address()))
    number = s.identity_proof_number
    close(s)
    ok(number != "IRU0628347", "identityProofNumber must not be the OVD number")
    ok(re.fullmatch(r"\d{12}", str(number)), f"must be a 12-digit Aadhaar, got {number!r}")


# --------------------------------------------------------------------------------------------
# GROUP: duplicate -- "already registered" aborts the run
# --------------------------------------------------------------------------------------------

@test("duplicate: KYC paths are labelled, non-KYC paths are not")
def _():
    for path, expected in (("/api/e-kyc/v2/verify-dl", "Driving Licence"),
                           ("/api/e-kyc/v2/verify-voter-id", "Voter ID"),
                           ("/api/e-kyc/verify-pan", "PAN"),
                           ("/api/e-kyc/offline-aadhaar-xml", "Aadhaar"),
                           ("/api/kyc/v2/submit-basic-info", "identity document"),
                           ("/api/kyc/submit-all-kyc-info", "identity document"),
                           ("/api/kyc/v2/customer-kyc-address", "identity document"),
                           ("/api/customer", "PAN"),
                           ("/api/packet", ""),
                           ("/api/loan-process/add-packet-images", "")):
        eq(G._kyc_document_label(path), expected, f"label for {path}")

@test("duplicate: server phrasings are classified correctly")
def _():
    duplicates = ["This DL number is already registered with another customer.",
                  "PAN Card already exists!",
                  "Aadhaar already linked to another customer",
                  "This number is already in use"]
    others = ["Please Try Again After Sometime",
              "Invalid Adhaar card format!",
              "Aadhaar number does not match with the XML data",
              "PAN not found in records."]
    for text in duplicates:
        ok(G._is_duplicate_document_error(text), f"should be duplicate: {text!r}")
    for text in others:
        ok(not G._is_duplicate_document_error(text), f"should NOT be duplicate: {text!r}")

@test("duplicate: the captured DL rejection aborts instead of degrading")
def _():
    entry = find(OVD, "verify-dl")
    s = suite("ovd", GOLD_LOAN_KYC_OVD_TYPE="dl", GOLD_LOAN_KYC_OVD_NUMBER="HR2020150166904",
              GOLD_LOAN_KYC_DOB="1996-11-25")
    s.customer_id = "8693"
    n = {"i": 0}
    class FakeClient:
        async def post(self, url, **kw):
            n["i"] += 1
            return httpx.Response(400, json=resp_body(entry), request=httpx.Request("POST", url))
        async def aclose(self): pass
    s.client = FakeClient()
    raised = None
    try:
        quiet(lambda: asyncio.run(s._verify_ovd_auto()))
    except KycDocumentAlreadyExistsError as e:
        raised = e
    ok(raised is not None, "must raise KycDocumentAlreadyExistsError")
    eq(n["i"], 1, "must stop after the first attempt, not retry a taken document")
    eq(s.kyc_mode_effective, "ovd", "must NOT degrade to manual")
    ok("already registered" in str(raised), "message carries the server's reason")

@test("duplicate: raised from the chokepoint, so HTTPStatusError handlers cannot swallow it")
def _():
    s = suite("manual")
    class FakeClient:
        async def post(self, url, **kw):
            return httpx.Response(400, json={"message": "PAN Card already exists!"},
                                  request=httpx.Request("POST", url))
        async def aclose(self): pass
    s.client = FakeClient()
    raised = None
    try:
        quiet(lambda: asyncio.run(
            s._make_authenticated_request("POST", "/api/kyc/v2/submit-basic-info", json_data={})))
    except KycDocumentAlreadyExistsError as e:
        raised = e
    except httpx.HTTPStatusError:
        raise Fail("raised HTTPStatusError -- a fallback handler would swallow it")
    ok(raised is not None, "must raise KycDocumentAlreadyExistsError")
    ok(not issubclass(KycDocumentAlreadyExistsError, httpx.HTTPStatusError),
       "must not be a subclass of HTTPStatusError")

@test("duplicate: an unrelated endpoint's 'already exists' does not abort")
def _():
    s = suite("manual")
    class FakeClient:
        async def post(self, url, **kw):
            return httpx.Response(400, json={"message": "packet already exists"},
                                  request=httpx.Request("POST", url))
        async def aclose(self): pass
    s.client = FakeClient()
    try:
        quiet(lambda: asyncio.run(s._make_authenticated_request("POST", "/api/packet", json_data={})))
        raise Fail("expected an HTTP error")
    except KycDocumentAlreadyExistsError:
        raise Fail("a packet duplicate must not abort the run as a KYC document")
    except httpx.HTTPStatusError:
        pass  # correct: ordinary HTTP failure


# --------------------------------------------------------------------------------------------
# GROUP: profile -- identity profile loading, validation, path resolution
# --------------------------------------------------------------------------------------------

@test("profile: missing data fails fast with an actionable message")
def _():
    cases = [
        ({"GOLD_LOAN_KYC_MODE": "auto"}, "panCardNumber"),
        ({"GOLD_LOAN_KYC_MODE": "ovd", "GOLD_LOAN_KYC_OVD_TYPE": "passport",
          "GOLD_LOAN_KYC_OVD_NUMBER": "X1"}, "not supported"),
        ({"GOLD_LOAN_KYC_MODE": "ovd", "GOLD_LOAN_KYC_OVD_TYPE": "dl",
          "GOLD_LOAN_KYC_OVD_NUMBER": "X1"}, "date of birth"),
        ({"GOLD_LOAN_KYC_MODE": "bogus"}, "Unknown KYC mode"),
    ]
    for env, marker in cases:
        clean_env(); os.environ.update(env)
        try:
            G(); raise Fail(f"expected a ValueError for {env}")
        except ValueError as e:
            ok(marker in str(e), f"message for {env} should mention {marker!r}, got: {e}")

@test("profile: aliases normalise to catalogue keys")
def _():
    for alias in ("dl", "DL", "driving_license", "Driving License", "drivingLicence"):
        s = suite("ovd", GOLD_LOAN_KYC_OVD_TYPE=alias, GOLD_LOAN_KYC_OVD_NUMBER="X1",
                  GOLD_LOAN_KYC_DOB="1996-11-25")
        eq(s.kyc_profile["ovdType"], "drivingLicense", f"alias {alias!r}")
        eq(s.ovd_type, "DrivingLicense", "submitted ovdType string")
        close(s)
    for alias in ("voter", "Voter ID", "voterid", "epic"):
        s = suite("ovd", GOLD_LOAN_KYC_OVD_TYPE=alias, GOLD_LOAN_KYC_OVD_NUMBER="X1")
        eq(s.kyc_profile["ovdType"], "voterId", f"alias {alias!r}")
        eq(s.ovd_type, "VoterID", "submitted ovdType string")
        close(s)

@test("profile: relative paths resolve against the project root, not the cwd")
def _():
    original = os.getcwd()
    try:
        os.chdir(os.path.expanduser("~"))
        s = suite("ovd", GOLD_LOAN_KYC_OVD_TYPE="voterId", GOLD_LOAN_KYC_OVD_NUMBER="X1",
                  GOLD_LOAN_KYC_OVD_IMAGE="assets/voter-card.jpg")
        resolved = s.kyc_profile["ovdImagePath"]
        close(s)
        ok(os.path.isabs(resolved), f"must be absolute, got {resolved!r}")
        ok(os.path.exists(resolved), f"must exist, got {resolved!r}")
        eq(os.path.normcase(resolved),
           os.path.normcase(os.path.join(ROOT, "assets", "voter-card.jpg")), "resolved path")
    finally:
        os.chdir(original)

@test("profile: ovd_type/ovd_number survive construction (constructor ordering regression)")
def _():
    # These were once initialised AFTER _validate_kyc_profile populated them, wiping both to "".
    s = suite("ovd", GOLD_LOAN_KYC_OVD_TYPE="voterId", GOLD_LOAN_KYC_OVD_NUMBER="IRU0628347")
    eq(s.ovd_type, "VoterID", "ovd_type")
    eq(s.ovd_number, "IRU0628347", "ovd_number")
    close(s)

@test("profile: the shipped config matches its Aadhaar XML (UIDAI referenceId rule)")
def _():
    path = os.path.join(ROOT, "config", "kyc_identity.json")
    if not os.path.exists(path):
        return "skipped (no config/kyc_identity.json)"
    with open(path, encoding="utf-8") as fh:
        profile = json.load(fh)
    xml_path = G._resolve_profile_path(profile["aadhaarXmlPath"])
    ok(os.path.exists(xml_path), f"aadhaarXmlPath does not exist: {xml_path}")
    for key in ("ovdImagePath", "form97Path"):
        if profile.get(key):
            resolved = G._resolve_profile_path(profile[key])
            ok(os.path.exists(resolved), f"{key} does not exist: {resolved}")
    with open(xml_path, encoding="utf-8", errors="replace") as fh:
        head = fh.read(3000)
    ref = re.search(r'referenceId="(\d+)"', head)
    ok(ref is not None, "no referenceId in the Aadhaar XML")
    aadhaar = re.sub(r"\D", "", profile["aadhaarNumber"])
    eq(ref.group(1)[:4], aadhaar[-4:],
       "UIDAI referenceId must start with the last 4 digits of the Aadhaar number")


# --------------------------------------------------------------------------------------------
# GROUP: packets -- the bulk packet runner
# --------------------------------------------------------------------------------------------

def packet_batch(count, assign=True, script=None):
    """Run PacketBatchTest against fakes. `script` maps packet index -> 'ok'|'raise'|'unresolved'."""
    s = suite("manual")
    s.logged_in_user_id = 1473
    batch = PacketBatchTest(s, count=count, assign=assign)
    batch.appraiser_id = 1473
    state = {"i": 0, "tokens": [], "assigns": []}
    async def admin_token():
        state["tokens"].append("admin"); return "admin-tok"
    async def create():
        state["i"] += 1; i = state["i"]
        outcome = (script or {}).get(i, "ok")
        if outcome == "raise":
            raise Exception(f"server rejected packet {i}")
        if s.auth_token != "admin-tok":
            raise Fail("create_packet ran without the admin token")
        s._created_packet_unique = f"pac-1000000{i}"
        if outcome != "unresolved":
            s.available_packet = {"id": 10390 + i, "packetUniqueId": f"pac-1000000{i}"}
        # On "unresolved" we deliberately LEAVE available_packet untouched, exactly as the real
        # create_packet does when its listing lookup fails -- so the previous packet is still
        # sitting there. Only create_one's own reset stops it being re-assigned.
        return s.available_packet
    async def assign(appraiser_id):
        if s.auth_token != "admin-tok":
            raise Fail("assign_packet ran without the admin token")
        state["assigns"].append((s.available_packet.get("id"), appraiser_id))
    s._admin_token = admin_token; s.create_packet = create; s.assign_packet = assign
    async def go():
        saved = s.auth_token
        s.auth_token = await s._admin_token()
        try:
            for i in range(1, count + 1):
                batch.results.append(await batch.create_one(i))
        finally:
            s.auth_token = saved
    quiet(lambda: asyncio.run(go()))
    close(s)
    return batch, state, s

@test("packets: a clean batch creates and assigns every packet, logging admin in once")
def _():
    batch, state, s = packet_batch(5)
    eq(len([r for r in batch.results if r["assigned"]]), 5, "assigned count")
    eq(len(state["tokens"]), 1, "admin must log in once (OTP rate limit)")
    eq(state["assigns"], [(10391, 1473), (10392, 1473), (10393, 1473), (10394, 1473), (10395, 1473)],
       "assignments")
    eq(s.auth_token, "tok", "appraiser token restored")

@test("packets: one failure does not abort the batch")
def _():
    batch, state, _s = packet_batch(5, script={3: "raise"})
    eq(len(batch.results), 5, "all packets attempted")
    ok(batch.results[2]["error"], "packet 3 recorded an error")
    eq([r["assigned"] for r in batch.results], [True, True, False, True, True], "assignment pattern")

@test("packets: an unresolved id is skipped, never assigned as the previous packet")
def _():
    batch, state, _s = packet_batch(4, script={3: "unresolved"})
    third = batch.results[2]
    eq(third["created"], True, "packet 3 was created")
    eq(third["assigned"], False, "packet 3 must not be assigned")
    eq(third["id"], None, "packet 3 has no id")
    ok(10393 not in [a[0] for a in state["assigns"]], "must not assign a stale packet id")
    eq(len(state["assigns"]), 3, "only the resolved packets were assigned")

@test("packets: --no-assign creates without assigning and still passes")
def _():
    batch, state, _s = packet_batch(3, assign=False)
    eq(state["assigns"], [], "no assignments")
    ok(all(r["created"] and not r["assigned"] for r in batch.results), "created but unassigned")
    passed, _out = quiet(batch.report)
    eq(passed, True, "a create-only batch should pass")


# --------------------------------------------------------------------------------------------
# GROUP: kyc-runner -- the standalone KYC script
# --------------------------------------------------------------------------------------------

@test("kyc-runner: covers exactly the same steps as the harness's _run_full_kyc")
def _():
    import inspect
    calls = lambda text: set(re.findall(r"await s(?:elf)?\.(\w+)\(", text))
    mine = calls(inspect.getsource(KycOnlyTest._run_kyc))
    theirs = calls(inspect.getsource(G._run_full_kyc))
    eq(sorted(theirs - mine), [], "steps in _run_full_kyc missing from test_kyc")
    eq(sorted(mine - theirs), [], "extra steps in test_kyc not in _run_full_kyc")

@test("kyc-runner: per-customer state does not leak across a batch")
def _():
    s = suite("manual")
    runner = KycOnlyTest(s, count=3)
    seen = []
    n = {"i": 0}
    async def admin(t): return "admin-tok"
    async def add_customer():
        n["i"] += 1
        if s.auth_token != "admin-tok":
            raise Fail("add_customer must run under the admin token")
        s.customer_id = str(8000 + n["i"]); s.customer_unique_id = f"MSC{n['i']}"
        s.random_pan = f"ABCDE{1000 + n['i']}F"
    s._role_token = admin; s.add_customer = add_customer
    async def fake_kyc():
        seen.append({"customer": s.customer_id, "pan": s.random_pan,
                     "leftover_kyc_id": s.customer_kyc_id, "leftover_verified": s.pan_verified})
        s.customer_kyc_id = "KYC" + s.customer_id
        s.kyc_status = "approved"
        s.pan_verified = True
    runner._run_kyc = fake_kyc
    async def go():
        for i in range(1, 4):
            if i > 1:
                runner._reset_for_next_customer()
            runner.results.append(await runner.run_one(i))
    quiet(lambda: asyncio.run(go()))
    close(s)
    eq([r["unique_id"] for r in runner.results], ["MSC1", "MSC2", "MSC3"], "unique ids")
    for row in seen:
        ok(row["leftover_kyc_id"] in (None, ""), f"customerKycId leaked: {row}")
        eq(row["leftover_verified"], False, f"verification flag leaked: {row}")
    eq(len({row["pan"] for row in seen}), 3, "each customer got a distinct PAN")

@test("kyc-runner: a duplicate document stops the whole batch")
def _():
    s = suite("manual")
    runner = KycOnlyTest(s, count=4)
    n = {"i": 0}
    async def admin(t): return "admin-tok"
    async def add_customer():
        n["i"] += 1; s.customer_id = str(8000 + n["i"]); s.customer_unique_id = f"MSC{n['i']}"
    async def login(mobile): s.auth_token = "tok"
    s._role_token = admin; s.add_customer = add_customer; s.login = login
    s._get_mobile_number_for_login_type = lambda t: "8880008881"
    async def fake_kyc():
        if n["i"] == 2:
            raise KycDocumentAlreadyExistsError("PAN is already registered: PAN Card already exists!")
        s.kyc_status = "approved"
    runner._run_kyc = fake_kyc
    passed, _out = quiet(lambda: asyncio.run(runner.run()))
    close(s)
    eq(n["i"], 2, "must stop at the duplicate, not attempt customers 3 and 4")
    eq(passed, False, "verdict")
    eq(len(runner.results), 2, "only two customers recorded")
    ok(runner.results[1]["aborted"], "second result marked aborted")

@test("kyc-runner: CLI refuses --count > 1 for auto/ovd and other bad combinations")
def _():
    import subprocess
    script = os.path.join(ROOT, "src", "test_kyc.py")
    cases = [(["--count", "3", "--kyc", "ovd"], "is not possible with --kyc ovd"),
             (["--count", "2", "--kyc", "auto"], "is not possible with --kyc auto"),
             (["--customer", "MS35QNJP", "--count", "2"], "drop --count"),
             (["--count", "0"], "at least 1")]
    for args, marker in cases:
        proc = subprocess.run([sys.executable, script] + args, capture_output=True, text=True)
        combined = proc.stdout + proc.stderr
        ok(proc.returncode != 0, f"{args} should exit non-zero")
        ok(marker in combined, f"{args} should mention {marker!r}, got: {combined[-200:]}")

@test("kyc-runner: every harness member the runners rely on exists")
def _():
    needed = ["login", "_get_mobile_number_for_login_type", "_role_token", "_admin_token",
              "add_customer", "_use_existing_customer", "get_customer_by_id",
              "get_kyc_customer_detail", "submit_basic_info", "_kyc_consent_otp",
              "load_kyc_master_data", "fetch_existing_e_kyc", "save_customer_address",
              "save_customer_personal_details", "submit_all_kyc_info", "kyc_ops_approval",
              "create_packet", "assign_packet", "_find_packet_by_unique_id",
              "_print_kyc_mode_header", "_print_kyc_verification_summary",
              "_banner", "_log_step", "_c", "_print_summary_table", "_print_run_metrics"]
    missing = [m for m in needed if not hasattr(G, m)]
    eq(missing, [], "missing harness members")
    s = suite("manual")
    missing_attrs = [a for a in KycOnlyTest.PER_CUSTOMER_STATE if not hasattr(s, a)]
    close(s)
    eq(missing_attrs, [], "PER_CUSTOMER_STATE names that are not real attributes")


# --------------------------------------------------------------------------------------------

def main():
    wanted = sys.argv[1] if len(sys.argv) > 1 else ""
    selected = [(n, f) for n, f in TESTS if n.startswith(wanted)]
    if not selected:
        print(f"No tests match {wanted!r}. Groups: "
              f"{sorted({n.split(':')[0] for n, _ in TESTS})}")
        return 1
    passed = failed = skipped = 0
    failures = []
    group = None
    for name, fn in selected:
        this_group = name.split(":")[0]
        if this_group != group:
            group = this_group
            print(f"\n--- {group} ---")
        try:
            result = fn()
            if isinstance(result, str) and result.startswith("skipped"):
                skipped += 1
                print(f"  ~ {name}  [{result}]")
            else:
                passed += 1
                print(f"  PASS  {name}")
        except Fail as e:
            failed += 1
            failures.append((name, str(e)))
            print(f"  FAIL  {name}\n          {e}")
        except Exception:
            failed += 1
            detail = traceback.format_exc().strip().splitlines()[-1]
            failures.append((name, detail))
            print(f"  ERROR {name}\n          {detail}")
    print("\n" + "=" * 78)
    print(f"  {passed} passed, {failed} failed, {skipped} skipped   (of {len(selected)})")
    if failures:
        print("\n  Failures:")
        for name, detail in failures:
            print(f"    - {name}: {detail}")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
