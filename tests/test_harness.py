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
from maintest import validate_cli  # noqa: E402
from test_create_packets import PacketBatchTest  # noqa: E402
from test_kyc import KycOnlyTest  # noqa: E402
from test_resume_loan import (  # noqa: E402
    ResumeLoanTest, STAGE_NAMES, STEPS as RESUME_STEPS, STEP_KEYS as RESUME_KEYS)

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
# GROUP: appraiser -- resolving / reusing / recreating an existing appraiser request
# --------------------------------------------------------------------------------------------

ADMIN_TOKEN = "admin-tok"

def _request_item(uid="MSYIITOI", customer_id=20308, request_id=13500,
                  master_loan=None, process_complete=False, appraiser_id=1473):
    return {
        "id": request_id,
        "customerId": customer_id,
        "appraiserId": appraiser_id,
        "internalBranchId": 1,
        "status": "processing",
        "isProcessComplete": process_complete,
        "customer": {"id": customer_id, "customerUniqueId": uid},
        "masterLoan": master_loan,
    }

def _appraiser_suite(uid="MSYIITOI", customer_id="20308", **env):
    # suite() only scrubs the KYC vars, so clear this one explicitly -- otherwise the
    # --fresh-request test leaks GOLD_LOAN_FRESH_APPRAISER_REQUEST into every later test.
    if "GOLD_LOAN_FRESH_APPRAISER_REQUEST" not in env:
        os.environ.pop("GOLD_LOAN_FRESH_APPRAISER_REQUEST", None)
    s = suite(**env)
    s.customer_unique_id = uid
    s.customer_id = customer_id
    s.mobile_number = "9876543210"
    s.first_name, s.last_name = "A", "B"
    s.module_id, s.internal_branch_id, s.appraiser_id = "1", "1", "1473"
    s.logged_in_user_id = 1473
    s._fetch_appraisers = lambda: asyncio.sleep(0)
    return s

def _json_response(payload):
    class R:
        content = b"{}"
        def json(self_inner): return payload
    return R()

def _stub_api(s, visible, search_works=False, pages=None, create_ok=False,
              branches=None, accept_branch=None, cancel_status=None):
    """Fake the auth chokepoint with a miniature appraiser-request server.

    `visible` maps scope ("appraiser"/"admin") to the items that scope can see -- the real
    view-all is scoped to the logged-in user. `search_works=False` reproduces the live
    behaviour where the `search` filter does not index customerUniqueId. `pages` overrides
    the unfiltered listing so pagination can be exercised. `create_ok` lets the create
    succeed once its loan has been cancelled. `branches` is what get-my-branches returns and
    `accept_branch` is the one branch id whose create the server accepts. `cancel_status`
    makes /loan-process/cancel fail with that status (the live UAT 403).
    Returns a state dict recording what was called.
    """
    state = {"calls": [], "posts": 0, "cancels": 0, "created_in_branch": None, "created_body": None}

    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        scope = "admin" if s.auth_token == ADMIN_TOKEN else "appraiser"
        state["calls"].append((scope, method, path))

        if path == "/api/user-otp/user-send-otp":
            return _json_response({"referenceCode": "rc"})
        if path == "/api/auth/verify-login":
            return _json_response({"Token": ADMIN_TOKEN})
        if path == "/api/user/get-my-branches":
            return _json_response({"data": branches or []})
        if method == "POST" and path == "/api/appraiser-request":
            state["posts"] += 1
            branch = (json_data or {}).get("internalBranchId")
            accepted = (accept_branch is not None and branch == accept_branch) or \
                       (create_ok and state["cancels"])
            if accepted:
                state["created_in_branch"] = branch
                state["created_body"] = json_data
                return _json_response({"data": {"id": 99999}})
            request = httpx.Request("POST", "https://x" + path)
            response = httpx.Response(400, text="This product Request already Exists", request=request)
            raise httpx.HTTPStatusError("400", request=request, response=response)
        if path == "/api/loan-cancel-reason":
            return _json_response({"data": [{"reason": "Test"}]})
        if path == "/api/loan-process/cancel":
            state["cancels"] += 1
            if cancel_status:
                request = httpx.Request("POST", "https://x" + path)
                response = httpx.Response(cancel_status, text="You are not allowed to cencel this loan.",
                                          request=request)
                raise httpx.HTTPStatusError(str(cancel_status), request=request, response=response)
            return _json_response({"message": "cancelled"})
        if path.startswith("/api/appraiser-request/view-all"):
            searched = "&search=" in path
            frm = int(path.split("from=", 1)[1].split("&", 1)[0])
            page_index = (frm - 1) // 25
            if searched and not search_works:
                chunks = []
            elif pages is not None and not searched:
                chunks = pages
            else:
                chunks = [visible.get(scope, [])]
            items = chunks[page_index] if page_index < len(chunks) else []
            return _json_response({
                "message": "ok" if items else "Data not found!",
                "data": items,
                "pagination": {"hasMore": page_index + 1 < len(chunks),
                               "per_page": 25, "current_page": page_index + 1},
            })
        return _json_response({})

    s._make_authenticated_request = fake
    return state

BRANCHES = [{"id": 1, "name": "Augmont"}, {"id": 3, "name": "Augmont Amritsar"},
            {"id": 40, "name": "aaa"}, {"id": 76, "name": "Akhil"}, {"id": 125, "name": "Augmontr"}]

@test("appraiser: an unfiltered scan finds the request when the search filter misses it")
def _():
    # Reproduces the live failure: POST said "already Exists" but search=<uniqueId> returned
    # an empty data list, so the old search-only lookup asserted "No existing appraiser request".
    s = _appraiser_suite()
    state = _stub_api(s, {"appraiser": [_request_item()]})
    found, _out = quiet(lambda: asyncio.run(s._fetch_existing_appraiser_request()))
    eq(found, True, "the fallback scan should find the request")
    eq(s.appraiser_request_id, "13500", "resolved the item-level request id, not the customer id")
    paths = [p for _scope, _m, p in state["calls"]]
    ok(any("search=MSYIITOI" in p for p in paths), "tried the unique-id search first")
    ok(any("view-all" in p and "search=" not in p for p in paths), "fell back to an unfiltered listing")
    close(s)

@test("appraiser: the unfiltered scan follows pagination past page 1")
def _():
    s = _appraiser_suite()
    state = _stub_api(s, {}, pages=[[_request_item(uid="OTHER1", customer_id=1)],
                                    [_request_item(uid="OTHER2", customer_id=2)],
                                    [_request_item()]])
    found, _out = quiet(lambda: asyncio.run(s._fetch_existing_appraiser_request()))
    eq(found, True, "found on page 3")
    ok(any("from=51&to=75" in p for _s, _m, p in state["calls"]), "requested the third page")
    close(s)

@test("appraiser: a request owned by another branch is found under the admin scope")
def _():
    # view-all is scoped to the logged-in user, so the appraiser sees nothing even though the
    # server refuses to create a second request.
    s = _appraiser_suite()
    state = _stub_api(s, {"appraiser": [], "admin": [_request_item()]})
    found, _out = quiet(lambda: asyncio.run(s._fetch_existing_appraiser_request()))
    eq(found, True, "admin sees every branch")
    eq(s.appraiser_request_id, "13500", "captured the request id from the admin listing")
    ok(any(scope == "admin" for scope, _m, _p in state["calls"]), "retried under the admin token")
    eq(s.auth_token, "tok", "the appraiser token is restored afterwards")
    close(s)

@test("appraiser: a genuine miss returns False with required=False and names both scopes")
def _():
    s = _appraiser_suite()
    _stub_api(s, {})  # nothing visible to anyone
    found, _out = quiet(lambda: asyncio.run(s._fetch_existing_appraiser_request(required=False)))
    eq(found, False, "required=False reports the miss instead of raising")
    try:
        quiet(lambda: asyncio.run(s._fetch_existing_appraiser_request()))
        raise Fail("required=True should raise")
    except AssertionError as e:
        if isinstance(e, Fail):
            raise
        ok(s.env_name in str(e), "the error names the environment")
        ok("admin" in str(e), "the error says the admin scope was tried too")
    close(s)

@test("appraiser: our own in-progress request is REUSED, not recreated")
def _():
    s = _appraiser_suite()
    state = _stub_api(s, {"appraiser": [_request_item(master_loan={"id": 10930, "isLoanCompleted": False})]})
    _r, out = quiet(lambda: asyncio.run(s.create_appraiser_request()))
    eq(s.appraiser_request_id, "13500", "kept the existing request id")
    eq(state["cancels"], 0, "the in-progress loan must NOT be cancelled")
    eq(state["posts"], 1, "no second create attempt")
    eq(s.master_loan_id, "10930", "carried the existing master loan forward for the resume")
    ok("Reusing appraiser request" in out, "said it was reusing")
    close(s)

@test("appraiser: our own FINISHED request is kept and a fresh loan started on it")
def _():
    # Cancelling a finished loan is futile (the server answers "You are not allowed to cencel
    # this loan"), and this is the path --customer with --count takes for loans 2..N.
    s = _appraiser_suite()
    item = _request_item(master_loan={"id": 10930, "isLoanCompleted": True}, process_complete=True)
    state = _stub_api(s, {"appraiser": [item]}, create_ok=True)
    _r, out = quiet(lambda: asyncio.run(s.create_appraiser_request()))
    eq(state["cancels"], 0, "no futile cancel of a finished loan")
    eq(state["posts"], 1, "and no second create attempt")
    eq(s.appraiser_request_id, "13500", "the request is kept")
    eq((s.loan_id, s.master_loan_id), ("", ""), "but the loan ids are cleared for a fresh loan")
    ok("fresh loan" in out, "and it said so")
    close(s)

@test("appraiser: --fresh-request forces cancel-and-recreate of our own request")
def _():
    s = _appraiser_suite(GOLD_LOAN_FRESH_APPRAISER_REQUEST="true")
    eq(s.reuse_existing_request, False, "the env var disables reuse")
    state = _stub_api(s, {"appraiser": [_request_item(master_loan={"id": 10930, "isLoanCompleted": False})]},
                      create_ok=True)
    _r, out = quiet(lambda: asyncio.run(s.create_appraiser_request()))
    eq(state["cancels"], 1, "cancelled despite the loan being in progress")
    eq(state["posts"], 2, "created a fresh request")
    ok("--fresh-request" in out, "explained why it did not reuse")
    close(s)

@test("appraiser: a request owned by ANOTHER appraiser is recreated in another branch")
def _():
    # The live UAT sequence: reusing it 400s "This customer is not assign to you" on every
    # loan-process call, and cancelling it 403s "You are not allowed to cencel this loan" --
    # so create our own request elsewhere instead.
    s = _appraiser_suite()
    item = _request_item(master_loan={"id": 10930, "isLoanCompleted": False}, appraiser_id=9999)
    state = _stub_api(s, {"appraiser": [item]}, branches=BRANCHES, accept_branch=3)
    _r, out = quiet(lambda: asyncio.run(s.create_appraiser_request()))
    eq(state["created_in_branch"], 3, "created in the first alternate branch that accepted it")
    eq(state["created_body"]["appraiserId"], 1473, "kept OUR appraiser id, not the previous owner's")
    eq(s.internal_branch_id, "3", "the rest of the run uses the branch we actually created in")
    ok("Created the appraiser request in branch 3" in out, "reported the branch it landed in")
    close(s)

@test("appraiser: a foreign request is NEVER cancelled")
def _():
    s = _appraiser_suite()
    item = _request_item(master_loan={"id": 10930, "isLoanCompleted": False}, appraiser_id=9999)
    state = _stub_api(s, {"appraiser": [item]}, branches=BRANCHES, accept_branch=3,
                      cancel_status=403)
    quiet(lambda: asyncio.run(s.create_appraiser_request()))
    eq(state["cancels"], 0, "another appraiser's loan is left untouched")
    close(s)

@test("appraiser: an UNASSIGNED request is not treated as ours")
def _():
    s = _appraiser_suite()
    item = _request_item(master_loan={"id": 10930, "isLoanCompleted": False}, appraiser_id=None)
    state = _stub_api(s, {"appraiser": [item]}, branches=BRANCHES, accept_branch=3)
    quiet(lambda: asyncio.run(s.create_appraiser_request()))
    eq(s._existing_request_is_mine(), False, "an unassigned request binds to nobody")
    eq(state["created_in_branch"], 3, "recreated so the request is bound to this appraiser")
    eq(state["cancels"], 0, "still nothing to cancel")
    close(s)

@test("appraiser: the branch ladder is capped and reports what it skipped")
def _():
    s = _appraiser_suite()
    item = _request_item(master_loan={"id": 10930, "isLoanCompleted": False}, appraiser_id=9999)
    # accept_branch=125 is the LAST candidate, past the cap, so it is never reached.
    state = _stub_api(s, {"appraiser": [item]}, branches=BRANCHES, accept_branch=125)
    try:
        quiet(lambda: asyncio.run(s.create_appraiser_request()))
        raise Fail("no reachable branch accepted the create, so it must fail")
    except RuntimeError as e:
        ok("branch 3 ->" in str(e) and "branch 40 ->" in str(e),
           "the error lists what each attempted branch answered")
    eq(state["posts"], 4, "the original create plus exactly 3 branch attempts")
    eq(s.internal_branch_id, "1", "a failed ladder must not move the run to another branch")
    close(s)

@test("appraiser: a foreign request with no branch left fails with the owner named")
def _():
    s = _appraiser_suite()
    item = _request_item(master_loan={"id": 10930, "isLoanCompleted": False}, appraiser_id=9999)
    state = _stub_api(s, {"appraiser": [item]}, branches=[{"id": 1, "name": "Augmont"}])
    try:
        quiet(lambda: asyncio.run(s.create_appraiser_request()))
        raise Fail("should not silently reuse a request owned by another appraiser")
    except RuntimeError as e:
        ok("9999" in str(e), "the error names the owning appraiser")
        ok("not assign to you" in str(e), "the error explains what would fail downstream")
        ok("no alternate branch" in str(e), "the error says the branch retry found nothing to try")
    eq(state["cancels"], 0, "and it still did not cancel anything")
    close(s)

@test("appraiser: an invisible existing request fails with a diagnosis, not a raw AssertionError")
def _():
    s = _appraiser_suite()
    _stub_api(s, {})  # the server refuses the create but shows the request to nobody
    try:
        quiet(lambda: asyncio.run(s.create_appraiser_request()))
        raise Fail("create_appraiser_request should fail when the request is invisible")
    except RuntimeError as e:
        ok("already Exists" in str(e), "the error quotes the server's rejection")
        ok("admin" in str(e), "the error says the admin scope was tried")
    close(s)

# --------------------------------------------------------------------------------------------
# GROUP: bank -- the ops manual-verification fallback for bank-details
# --------------------------------------------------------------------------------------------

def _bank_suite(**env):
    # suite() only scrubs the KYC vars; clear the bank ones so they cannot leak between tests.
    for var in ("GOLD_LOAN_BANK_ACCOUNTS", "GOLD_LOAN_BANK_ACCOUNT_ATTEMPTS",
                "GOLD_LOAN_BANK_STATUS_FILE", "GOLD_LOAN_BANK_RETRY_ALL", "GOLD_LOAN_ENV"):
        if var not in env:
            os.environ.pop(var, None)
    if "GOLD_LOAN_BANK_STATUS_FILE" not in env:
        # Every suite that does not ask for a specific status file gets a CLEAN one: the file
        # is written on each rejection, so a shared path would leak one test's flags into the
        # next and quietly shrink its candidate list.
        default_status = os.path.join(ROOT, "tests", "_bank_status_default.json")
        if os.path.exists(default_status):
            os.remove(default_status)
        env["GOLD_LOAN_BANK_STATUS_FILE"] = default_status
    s = suite(**env)
    s.customer_id = "20308"
    s.loan_id, s.master_loan_id = "12345", "10930"
    s.first_name, s.last_name = "A", "B"
    s.final_loan_amount = 400000
    s.secured_processing_charge = 4000
    s.upfront_interest_amount = 0
    s.logged_in_user_id = 1473
    # store_bank_details uploads two cheque images; keep the test offline.
    async def fake_upload(*a, **kw):
        return {"uploadFile": {"path": "uploads/cheque.png"}}
    s._upload_file = fake_upload
    return s

def _bank_stub(s, verified_after_ops=True, ops_status=None):
    """Serve bank-details + bank-verification-manual.

    bank-details 400s "bank details is not verified" until the ops manual verification has
    run -- the live UAT behaviour once validate-account fails. `ops_status` makes the ops
    call fail with that status instead.
    """
    state = {"bank_details_posts": 0, "ops_calls": 0, "ops_scope": None, "bodies": []}

    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        if path == "/api/user-otp/user-send-otp":
            return _json_response({"referenceCode": "rc"})
        if path == "/api/auth/verify-login":
            return _json_response({"Token": "ops-tok"})
        if path == "/api/loan-process/bank-verification-manual":
            state["ops_calls"] += 1
            state["ops_scope"] = s.auth_token
            if ops_status:
                request = httpx.Request("POST", "https://x" + path)
                response = httpx.Response(ops_status, text="ops rejected it", request=request)
                raise httpx.HTTPStatusError(str(ops_status), request=request, response=response)
            return _json_response({"message": "success", "data": {
                "isVerified": False,
                "isManuallyVerified": verified_after_ops,
                "forOpsApproval": not verified_after_ops,
            }})
        if path == "/api/loan-process/bank-details":
            state["bank_details_posts"] += 1
            state["bodies"].append(dict(json_data))  # copy: the retry mutates the same dict
            if state["ops_calls"]:
                return _json_response({"message": "success"})
            request = httpx.Request("POST", "https://x" + path)
            response = httpx.Response(400, text="bank details is not verified", request=request)
            raise httpx.HTTPStatusError("400", request=request, response=response)
        return _json_response({})

    s._make_authenticated_request = fake
    return state

@test("bank: an unverified account triggers the ops manual verification and a retry")
def _():
    # Live UAT: validate-account 400s "Something went wrong", so bank-details 400s
    # "bank details is not verified" and the run never reaches the Ops stage that would fix it.
    s = _bank_suite()
    # bankTxnStatus was true (the penny-drop reached the bank) but the account is still not
    # verified -- the documented name-mismatch case -- so the first body says system-verified.
    s.bank_account_verified = True
    state = _bank_stub(s)
    _r, out = quiet(lambda: asyncio.run(s.store_bank_details()))
    eq(state["ops_calls"], 1, "the ops manual verification ran")
    eq(state["bank_details_posts"], 2, "bank-details was retried after it")
    eq(state["bodies"][0]["isManuallyVerified"], False, "the first try went in as system-verified")
    eq(state["bodies"][1]["isManuallyVerified"], True, "the retry declares the manual verification")
    eq(state["bodies"][1]["manualVerifiedStatus"], "verified", "and its status")
    ok("ops manual bank verification" in out.lower(), "said what it did")
    close(s)

@test("bank: the ops manual verification runs under the OPS token and restores ours")
def _():
    s = _bank_suite()
    state = _bank_stub(s)
    quiet(lambda: asyncio.run(s.store_bank_details()))
    eq(state["ops_scope"], "ops-tok", "bank-verification-manual needs the ops role token")
    eq(s.auth_token, "tok", "the appraiser token is restored afterwards")
    close(s)

@test("bank: a verified account does not call the ops manual verification at all")
def _():
    s = _bank_suite()
    state = _bank_stub(s)
    state["ops_calls"] = 1  # pretend it is already verified: bank-details succeeds first time
    quiet(lambda: asyncio.run(s.store_bank_details()))
    eq(state["bank_details_posts"], 1, "no retry when the server accepts it")
    eq(state["ops_calls"], 1, "the ops call was not made again")
    close(s)

@test("bank: a bank-details error that is NOT about verification is re-raised untouched")
def _():
    s = _bank_suite()
    state = {"ops_calls": 0}
    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        if path == "/api/loan-process/bank-verification-manual":
            state["ops_calls"] += 1
            return _json_response({"message": "success", "data": {}})
        if path == "/api/loan-process/bank-details":
            request = httpx.Request("POST", "https://x" + path)
            response = httpx.Response(400, text="To Be Paid amount is incorrect", request=request)
            raise httpx.HTTPStatusError("400", request=request, response=response)
        return _json_response({})
    s._make_authenticated_request = fake
    try:
        quiet(lambda: asyncio.run(s.store_bank_details()))
        raise Fail("a toBePaid error must not be swallowed")
    except httpx.HTTPStatusError as e:
        ok("To Be Paid" in e.response.text, "the original error survives")
    eq(state["ops_calls"], 0, "and ops is not dragged into an unrelated failure")
    close(s)

@test("bank: strict=False downgrades an unverified ops verdict to a warning, strict=True raises")
def _():
    # Before bank-details the record does not exist yet, so the retried bank-details call is the
    # real check -- a lukewarm ops verdict must not abort the run there.
    s = _bank_suite()
    state = _bank_stub(s, verified_after_ops=False)
    _r, out = quiet(lambda: asyncio.run(s.store_bank_details()))
    eq(state["bank_details_posts"], 2, "it still retried bank-details")
    ok("WARNING" in out, "but it warned about the verdict")
    close(s)

    s2 = _bank_suite()
    _bank_stub(s2, verified_after_ops=False)
    try:
        quiet(lambda: asyncio.run(s2.ops_manual_bank_verification(force=True)))
        raise Fail("strict=True must reject an unverified verdict")
    except AssertionError as e:
        if isinstance(e, Fail):
            raise
        ok("still" in str(e), "the error says what was still wrong")
    close(s2)

def _accounts_stub(s, verdicts, bad_ifsc=()):
    """Serve validate-account from `verdicts`: {accountNumber: payload-or-status-int}.

    An int means the server answers with that HTTP status (the live UAT 400). A dict is the
    JSON body. An account not listed answers 400, like an account the bank rejects.
    Returns a state dict recording the accounts tried, in order.
    """
    state = {"tried": [], "karza": []}

    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        if path.startswith("/api/loan-process/account-details-karza"):
            ifsc = path.split("ifscCode=")[1]
            state["karza"].append(ifsc)
            if ifsc in bad_ifsc:
                request = httpx.Request("GET", "https://x" + path)
                response = httpx.Response(400, text="Invalid IFSC Code", request=request)
                raise httpx.HTTPStatusError("400", request=request, response=response)
            return _json_response({"data": {"bankName": "TEST BANK", "branch": "TEST BRANCH"}})
        if path == "/api/loan-process/validate-account":
            number = json_data["accountNumber"]
            state["tried"].append(number)
            verdict = verdicts.get(number, 400)
            if isinstance(verdict, int):
                request = httpx.Request("POST", "https://x" + path)
                response = httpx.Response(verdict, text="Something went wrong", request=request)
                raise httpx.HTTPStatusError(str(verdict), request=request, response=response)
            return _json_response(verdict)
        return _json_response({})

    s._make_authenticated_request = fake
    return state

REAL_ACCOUNTS = None  # loaded lazily from the shipped fixture

def _fixture_accounts():
    global REAL_ACCOUNTS
    if REAL_ACCOUNTS is None:
        with open(os.path.join(ROOT, "reference", "bank_accounts.json"), encoding="utf-8") as fh:
            REAL_ACCOUNTS = json.load(fh)["accounts"]
    return REAL_ACCOUNTS

@test("bank: the shipped account list is readable and every entry is usable")
def _():
    s = _bank_suite()
    accounts = s._load_bank_accounts()
    ok(len(accounts) > 1, "the fixture carries a real list, not just the fallback")
    eq(len(accounts), len(_fixture_accounts()), "every fixture entry survived validation")
    for a in accounts:
        # One real HSBC entry is printed with separators (006-089866-001); keep the fixture's
        # value verbatim rather than "fixing" the source data.
        ok(re.fullmatch(r"[0-9-]+", a["accountNumber"]), f"{a['label']}: account number is plausible")
        ok(re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", a["ifscCode"]), f"{a['label']}: IFSC is well formed")
    close(s)

@test("bank: an unreadable account list falls back to the built-in account")
def _():
    s = _bank_suite(GOLD_LOAN_BANK_ACCOUNTS=os.path.join(ROOT, "__no_such_bank_file__.json"))
    accounts, _out = quiet(s._load_bank_accounts)
    eq(len(accounts), 1, "one fallback account")
    eq(accounts[0]["accountNumber"], s.DEFAULT_BANK_ACCOUNT["accountNumber"], "the built-in SBI account")
    close(s)

@test("bank: the penny-drop walks the list and keeps the first usable account")
def _():
    # Live UAT: the first account (SBI, previously hard-coded) 400s "Something went wrong".
    s = _bank_suite()
    accounts = s._load_bank_accounts()
    third = accounts[2]["accountNumber"]
    state = _accounts_stub(s, {third: {"data": {"bankTxnStatus": True}, "forOpsApproval": True}})
    _r, out = quiet(lambda: asyncio.run(s.validate_account()))
    eq(state["tried"], [a["accountNumber"] for a in accounts[:3]], "tried in order, stopped at the third")
    eq(s.bank_account_number, third, "settled on the account that worked")
    eq(s.bank_account_verified, True, "recorded bankTxnStatus")
    ok("usable" in out, "said the account is usable")
    close(s)

@test("bank: an outright-verified account stops the walk immediately")
def _():
    path = _status_file()
    s = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path)
    first = s._load_bank_accounts()[0]["accountNumber"]
    state = _accounts_stub(s, {first: {"data": {"bankTxnStatus": True}, "isVerified": True}})
    _r, out = quiet(lambda: asyncio.run(s.validate_account()))
    eq(state["tried"], [first], "no further penny-drops once one verifies")
    eq(s.bank_system_verified, True, "recorded the verified flag")
    ok("VERIFIED outright" in out, "reported it")
    eq(_read_status(path)["environments"][s.env_name]["preferred"], first,
       "and it is remembered for the next run, like the bankTxnStatus path")
    close(s)
    os.remove(path)

@test("bank: the bank name and branch come from each account's IFSC lookup")
def _():
    s = _bank_suite()
    accounts = s._load_bank_accounts()
    second = accounts[1]
    _accounts_stub(s, {second["accountNumber"]: {"data": {"bankTxnStatus": True}}})
    quiet(lambda: asyncio.run(s.validate_account()))
    eq(s.bank_ifsc_code, second["ifscCode"], "IFSC follows the chosen account")
    eq(s.bank_name, "TEST BANK", "bank name came from the karza lookup, not a hard-coded string")
    eq(s.bank_branch_name, "TEST BRANCH", "and so did the branch")
    close(s)

@test("bank: when every account fails the flags stay false for the ops fallback")
def _():
    s = _bank_suite()
    state = _accounts_stub(s, {})  # every account 400s
    _r, out = quiet(lambda: asyncio.run(s.validate_account()))
    eq(len(state["tried"]), s.bank_account_attempts, "walked up to the attempt cap")
    eq(s.bank_account_verified, False, "nothing verified")
    eq(s.bank_for_ops_approval, True, "so the ops manual verification must run")
    ok(s.bank_account_number, "the run still has a concrete account to send")
    ok("FAILED for every candidate" in out, "said so plainly")
    close(s)

@test("bank: the attempt cap is honoured and reports what it skipped")
def _():
    s = _bank_suite(GOLD_LOAN_BANK_ACCOUNT_ATTEMPTS="3")
    state = _accounts_stub(s, {})
    _r, out = quiet(lambda: asyncio.run(s.validate_account()))
    eq(len(state["tried"]), 3, "stopped at the cap")
    ok("of 54 candidate accounts" in out, "said how many it skipped")
    close(s)

@test("bank: the disbursement re-run validates the SAME account first")
def _():
    s = _bank_suite()
    accounts = s._load_bank_accounts()
    chosen = accounts[4]["accountNumber"]
    s.bank_account_number = chosen  # as left by the Bank Details stage
    state = _accounts_stub(s, {chosen: {"data": {"bankTxnStatus": True}}})
    quiet(lambda: asyncio.run(s.validate_account()))
    eq(state["tried"], [chosen], "no re-walk: the chosen account is tried first and settles")
    close(s)

@test("bank: store_bank_details sends the account the penny-drop settled on")
def _():
    s = _bank_suite()
    accounts = s._load_bank_accounts()
    third = accounts[2]
    _accounts_stub(s, {third["accountNumber"]: {"data": {"bankTxnStatus": True}}})
    quiet(lambda: asyncio.run(s.validate_account()))
    state = _bank_stub(s)
    state["ops_calls"] = 1  # already verified: no fallback needed, first POST is accepted
    quiet(lambda: asyncio.run(s.store_bank_details()))
    body = state["bodies"][0]
    eq(body["accountNumber"], third["accountNumber"], "bank-details uses the chosen account")
    eq(body["ifscCode"], third["ifscCode"], "and its IFSC")
    eq(body["bankName"], "TEST BANK", "and the bank from the IFSC lookup, not hard-coded SBI")
    eq(body["bankBranchName"], "TEST BRANCH", "and its branch")
    close(s)

@test("bank: a rejected account is flagged and never penny-dropped again")
def _():
    # The live waste: validate-account 400'd for account after account, and the SAME accounts
    # were walked again on the next loan -- two calls each (IFSC lookup + penny-drop).
    s = _bank_suite()
    accounts = s._load_bank_accounts()
    good = accounts[3]["accountNumber"]
    first = _accounts_stub(s, {good: {"data": {"bankTxnStatus": True}}})
    quiet(lambda: asyncio.run(s.validate_account()))
    eq(len(first["tried"]), 4, "walked until one worked")
    eq(sorted(s._bank_accounts_rejected), sorted(a["accountNumber"] for a in accounts[:3]),
       "the three that failed are flagged")

    # Next loan: the good account is tried first and nothing else is touched.
    second = _accounts_stub(s, {good: {"data": {"bankTxnStatus": True}}})
    quiet(lambda: asyncio.run(s.validate_account()))
    eq(second["tried"], [good], "one call, straight to the account that works")
    close(s)

@test("bank: flagged accounts are dropped from the candidate list")
def _():
    # Asserted on the candidate list directly: in a live walk the settled account is tried
    # first and succeeds, which would hide whether the skip filter does anything at all.
    s = _bank_suite()
    accounts = s._load_bank_accounts()
    s._bank_accounts_rejected = {a["accountNumber"] for a in accounts[:5]}
    candidates, out = quiet(s._bank_account_candidates)
    numbers = {a["accountNumber"] for a in candidates}
    eq(numbers & s._bank_accounts_rejected, set(), "no flagged account survives")
    eq(len(candidates), min(s.bank_account_attempts, len(accounts) - 5), "five fewer to try")
    ok("Skipping 5 account" in out, "and it says how many it skipped")
    close(s)

@test("bank: the rejection flags survive the per-loan reset")
def _():
    s = _bank_suite()
    s._bank_accounts_rejected = {"111", "222"}
    s._bank_penny_drop_unavailable = True
    quiet(s._reset_for_next_loan)
    eq(sorted(s._bank_accounts_rejected), ["111", "222"],
       "a batch must not re-learn the same rejections per loan")
    eq(s._bank_penny_drop_unavailable, True, "and must not re-walk a dead penny-drop")
    close(s)

@test("bank: once a full walk finds nothing, later loans skip the penny-drop entirely")
def _():
    s = _bank_suite()
    first = _accounts_stub(s, {})  # every account 400s
    quiet(lambda: asyncio.run(s.validate_account()))
    eq(len(first["tried"]), s.bank_account_attempts, "the first walk went to the cap")
    eq(s._bank_penny_drop_unavailable, True, "and recorded that it found nothing")

    second = _accounts_stub(s, {})
    _r, out = quiet(lambda: asyncio.run(s.validate_account()))
    eq(second["tried"], [], "no penny-drop at all the second time")
    eq(second["karza"], [], "and no IFSC lookups either")
    eq(s.bank_for_ops_approval, True, "the ops manual verification still has to run")
    ok("skipping it" in out, "and it said what it was doing")
    close(s)

SAVED_ROWS = [
    {"accountNumber": "111122223333", "ifscCode": "HDFC0000001", "bankName": "HDFC BANK",
     "bankBranchName": "FORT", "accountHolderName": "RAMESH", "isVerified": False,
     "passbookProof": ["public/uploads/loan/old-cheque.png"]},
    {"accountNumber": "444455556666", "ifscCode": "ICIC0000002", "bankName": "ICICI BANK",
     "bankBranchName": "ANDHERI", "accountHolderName": "RAMESH", "isManuallyVerified": True,
     "passbookProof": "public/uploads/loan/verified-cheque.png"},
]

def _load_saved(s, rows):
    """Run fetch_bank_details against a GET that answers with `rows`, then restore the stub."""
    inner = s._make_authenticated_request

    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        if method == "GET" and path.startswith("/api/loan-process/bank-details"):
            return _json_response({"data": {"bankDetails": rows, "cashLimit": 1}})
        return await inner(method, path, json_data=json_data, params=params,
                           files=files, headers=headers)

    s._make_authenticated_request = fake
    _r, out = quiet(lambda: asyncio.run(s.fetch_bank_details()))
    s._make_authenticated_request = inner
    return out

@test("bank: an existing customer's saved accounts are penny-dropped first, verified first")
def _():
    s = _bank_suite()
    state = _accounts_stub(s, {"444455556666": {"data": {"bankTxnStatus": True}}})
    out = _load_saved(s, SAVED_ROWS + [dict(SAVED_ROWS[0])])  # duplicate row collapses
    eq([a["accountNumber"] for a in s.saved_bank_accounts], ["444455556666", "111122223333"],
       "verified saved account ranked first, duplicates dropped")
    ok("saved bank account" in out, "said it found saved accounts")
    quiet(lambda: asyncio.run(s.validate_account()))
    eq(state["tried"], ["444455556666"], "only the customer's own account was tried")
    eq(s.account_holder_name, "RAMESH", "holder name comes from the saved account")
    eq(s.passbook_proofs, ["public/uploads/loan/verified-cheque.png"], "and its passbook proof")
    close(s)

@test("bank: saved accounts that all fail are still used -- no new account is added")
def _():
    path = _status_file()
    s = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path)
    state = _accounts_stub(s, {})  # every penny-drop 400s
    _load_saved(s, SAVED_ROWS)
    quiet(lambda: asyncio.run(s.validate_account()))
    eq(sorted(state["tried"]), ["111122223333", "444455556666"], "never walked the LIC list")
    eq(s.bank_account_number, "444455556666", "stayed on the customer's first saved account")
    eq(s.bank_for_ops_approval, True, "left for the ops manual verification")
    ok(not os.path.exists(path) or not _read_status(path)["environments"][s.env_name]["rejected"],
       "the customer's own accounts are not written to the shared rejection file")
    close(s)

@test("bank: store_bank_details reuses a saved account's passbook proof")
def _():
    s = _bank_suite()
    uploads = []
    async def counting_upload(*a, **kw):
        uploads.append(1)
        return {"uploadFile": {"path": "uploads/cheque.png"}}
    s._upload_file = counting_upload
    _accounts_stub(s, {"444455556666": {"data": {"bankTxnStatus": True}}})
    _load_saved(s, SAVED_ROWS)
    quiet(lambda: asyncio.run(s.validate_account()))
    state = _bank_stub(s)
    state["ops_calls"] = 1
    quiet(lambda: asyncio.run(s.store_bank_details()))
    body = state["bodies"][0]
    eq(uploads, [], "no new cheque upload")
    eq(body["accountNumber"], "444455556666", "the saved account is sent")
    eq(body["accountHolderName"], "RAMESH", "with its holder name")
    eq(body["passbookProof"], ["public/uploads/loan/verified-cheque.png"], "and its proof")
    eq(body["passbookProofImage"], [f"{s.BASE_URL}/public/uploads/loan/verified-cheque.png"],
       "as a full URL")
    close(s)

@test("bank: a customer with no saved accounts gets a new one from the reference list")
def _():
    s = _bank_suite()
    first = s._load_bank_accounts()[0]["accountNumber"]
    state = _accounts_stub(s, {first: {"data": {"bankTxnStatus": True}}})
    out = _load_saved(s, [])
    eq(s.saved_bank_accounts, [], "nothing saved")
    ok("new account will be added" in out, "said a new account is being added")
    quiet(lambda: asyncio.run(s.validate_account()))
    eq(state["tried"], [first], "walked the reference list")
    close(s)

@test("bank: GOLD_LOAN_BANK_RETRY_ALL re-tries the flagged accounts")
def _():
    s = _bank_suite(GOLD_LOAN_BANK_RETRY_ALL="true")
    eq(s.bank_retry_all_accounts, True, "the switch is read")
    s._bank_penny_drop_unavailable = True
    accounts = s._load_bank_accounts()
    state = _accounts_stub(s, {accounts[0]["accountNumber"]: {"data": {"bankTxnStatus": True}}})
    quiet(lambda: asyncio.run(s.validate_account()))
    ok(state["tried"], "the walk happened despite the earlier verdict")
    close(s)

@test("bank: the account that worked is never skipped, even if once flagged")
def _():
    s = _bank_suite()
    accounts = s._load_bank_accounts()
    chosen = accounts[0]["accountNumber"]
    s.bank_account_number = chosen
    s._bank_accounts_rejected = {chosen}       # e.g. one flaky rejection earlier
    candidates, _out = quiet(s._bank_account_candidates)
    eq(candidates[0]["accountNumber"], chosen,
       "the settled account stays first rather than being filtered out")
    close(s)

def _status_file():
    """A scratch path for the persistent bank-account status, cleared before each use."""
    path = os.path.join(ROOT, "tests", "_bank_status_scratch.json")
    if os.path.exists(path):
        os.remove(path)
    return path

def _read_status(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)

@test("bank: a rejection is written to the status file, marked definitive or not")
def _():
    path = _status_file()
    s = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path)
    accounts = s._load_bank_accounts()
    first, second, good = accounts[0], accounts[1], accounts[2]
    # The stub answers "Something went wrong" -- the ambiguous reason the live run kept getting.
    _accounts_stub(s, {good["accountNumber"]: {"data": {"bankTxnStatus": True}}})
    quiet(lambda: asyncio.run(s.validate_account()))
    recorded = _read_status(path)["environments"][s.env_name]["rejected"]
    ok(first["accountNumber"] in recorded, "the first failure was written down")
    ok(second["accountNumber"] in recorded, "and so was the second")
    eq(recorded[first["accountNumber"]]["definitive"], False,
       '"Something went wrong" is not definitive -- the provider may just be down')
    eq(_read_status(path)["environments"][s.env_name]["preferred"], good["accountNumber"],
       "and the account that worked is remembered")
    close(s)
    os.remove(path)

@test("bank: a definitive reason is recorded as definitive")
def _():
    path = _status_file()
    s = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path)
    s.bank_account_label = "RBL - LIC MF POOL A/C"
    quiet(lambda: s._flag_bank_account("409000559756", "BENEFICIARY ACCOUNT IS CLOSED", 400,
                                       definitive=s._is_definitive_bank_rejection(
                                           "BENEFICIARY ACCOUNT IS CLOSED")))
    entry = _read_status(path)["environments"][s.env_name]["rejected"]["409000559756"]
    eq(entry["definitive"], True, "a closed account will never work")
    ok("CLOSED" in entry["reason"], "the reason is kept for the next reader")
    ok(entry["lastSeen"].endswith("Z"), "and when it was seen")
    for text in ("The ifsc code is not found", "Invalid IFSC Code", "Invalid account number"):
        ok(G._is_definitive_bank_rejection(text), f"{text!r} is definitive")
    for text in ("Something went wrong", "", "timeout"):
        ok(not G._is_definitive_bank_rejection(text), f"{text!r} is not definitive")
    close(s)
    os.remove(path)

@test("bank: the NEXT run skips the accounts recorded as failing")
def _():
    path = _status_file()
    first = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path)
    accounts = first._load_bank_accounts()
    good = accounts[3]["accountNumber"]
    state = _accounts_stub(first, {good: {"data": {"bankTxnStatus": True}}})
    quiet(lambda: asyncio.run(first.validate_account()))
    eq(len(state["tried"]), 4, "the first run walked four accounts")
    close(first)

    # A brand new process, same status file.
    second = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path)
    state2 = _accounts_stub(second, {good: {"data": {"bankTxnStatus": True}}})
    _r, out = quiet(lambda: asyncio.run(second.validate_account()))
    eq(state2["tried"], [good], "the second run goes straight to the one that worked")
    ok("last worked" in out, "and says it is starting from it")
    close(second)
    os.remove(path)

@test("bank: a bad IFSC skips the penny-drop instead of spending a second call on it")
def _():
    # Live: account-details-karza answered "Invalid IFSC Code" and validate-account was still
    # sent, answering "The ifsc code is not found" -- two calls to learn one thing.
    path = _status_file()
    s = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path)
    accounts = s._load_bank_accounts()
    good = accounts[1]["accountNumber"]
    bad_ifsc = accounts[0]["ifscCode"]
    state = _accounts_stub(s, {good: {"data": {"bankTxnStatus": True}}}, bad_ifsc=(bad_ifsc,))
    quiet(lambda: asyncio.run(s.validate_account()))
    eq(state["tried"], [good], "no penny-drop for the account whose IFSC does not resolve")
    entry = _read_status(path)["environments"][s.env_name]["rejected"][accounts[0]["accountNumber"]]
    eq(entry["definitive"], True, "a bad IFSC is permanent")
    close(s)
    os.remove(path)

@test("bank: the status file is per environment")
def _():
    path = _status_file()
    s = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path)
    quiet(lambda: s._flag_bank_account("111", "Something went wrong", 400, definitive=False))
    other = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path, GOLD_LOAN_ENV="uat")
    quiet(lambda: other._bank_account_candidates())
    eq(other._bank_accounts_rejected, set(), "uat is unaffected by a rejection recorded on test")
    envs = _read_status(path)["environments"]
    ok(s.env_name in envs, "the environment that recorded it is there")
    close(s)
    close(other)
    os.remove(path)

@test("bank: a recorded outage can never permanently exhaust the list")
def _():
    # If every account were flagged with a non-definitive reason, honouring the file would leave
    # nothing to try, forever. The soft ones are forgiven instead.
    path = _status_file()
    s = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path)
    accounts = s._load_bank_accounts()
    for a in accounts:
        quiet(lambda a=a: s._flag_bank_account(a["accountNumber"], "Something went wrong", 400,
                                               definitive=False))
    fresh = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path)
    candidates, out = quiet(fresh._bank_account_candidates)
    ok(candidates, "there is still something to try")
    ok("forgiving" in out, "and it says why")
    close(s)
    close(fresh)
    os.remove(path)

@test("bank: GOLD_LOAN_BANK_RETRY_ALL ignores the status file")
def _():
    path = _status_file()
    s = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path)
    accounts = s._load_bank_accounts()
    quiet(lambda: s._flag_bank_account(accounts[0]["accountNumber"], "closed", 400, definitive=True))
    fresh = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path, GOLD_LOAN_BANK_RETRY_ALL="true")
    candidates, _out = quiet(fresh._bank_account_candidates)
    eq(candidates[0]["accountNumber"], accounts[0]["accountNumber"],
       "even a definitively-dead account is retried when asked")
    close(s)
    close(fresh)
    os.remove(path)

@test("bank: an unreadable or missing status file is not fatal")
def _():
    path = os.path.join(ROOT, "tests", "_bank_status_broken.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    s = _bank_suite(GOLD_LOAN_BANK_STATUS_FILE=path)
    candidates, _out = quiet(s._bank_account_candidates)
    ok(candidates, "a corrupt cache falls back to trying everything")
    eq(s._bank_accounts_rejected, set(), "with nothing blocked")
    close(s)
    os.remove(path)

# --------------------------------------------------------------------------------------------
# GROUP: resume -- the resume-any-loan runner
# --------------------------------------------------------------------------------------------

def _resume_args(**over):
    class A:
        loan = customer = loan_id = master_loan_id = start_from = None
        env, login, partner = "test", "appraiser", "roshan"
        dry_run = list_steps = False
    a = A()
    for k, v in over.items():
        setattr(a, k, v)
    return a

def _resume_runner(**over):
    clean_env()  # the constructor loads the KYC profile and prints about it
    with redirect_stdout(io.StringIO()):
        r = ResumeLoanTest(_resume_args(**over))
    r.suite.auth_token = "tok"
    return r

def _loan_record(stage_id=6, ornaments=True, final_amount=400000, bank=True, packet=True,
                 documents=True, disbursed=True, completed=False):
    """A single-loan payload's `data`, trimmed to the fields the runner reads."""
    return {
        "id": 12952,
        "masterLoanId": 10947,
        "customerId": 8463,
        "loanUniqueId": "AUGM-67273",
        "partnerId": 152,
        "schemeId": 853,
        "rpg": 4533,
        "ltv": 85,
        "interestRate": 1,
        "posExposureAgainstScheme": 0.18,
        "loanOrnamentsDetail": [{"id": 1}] if ornaments else [],
        "loanBankDetail": {"accountNumber": "00000036150491589", "ifscCode": "SBIN0011777",
                           "bankName": "SBI", "isVerified": False,
                           "forOpsApproval": True} if bank else None,
        "loanPacketDetails": [{"id": 7}] if packet else [],
        "customerLoanDocument": [{"id": 3}] if documents else [],
        "isDisbursed": disbursed,
        "masterLoan": {
            "id": 10947,
            "loanStageId": stage_id,
            "loanStage": {"id": stage_id, "name": STAGE_NAMES.get(stage_id, "unknown")},
            "finalLoanAmount": final_amount,
            "tenure": 6,
            "processingCharge": 4000,
            "upfrontInterestAmount": 0,
            "loanStartDate": "2026-07-27",
            "loanEndDate": "2027-01-22",
            "appraiserRequestId": 13501,
            "internalBranchId": 1,
            "isLoanDisbursed": disbursed,
            "isLoanCompleted": completed,
        },
    }

def _planned(record, **over):
    """Run the runner's planning logic against a record, with no network at all."""
    r = _resume_runner(**over)
    r.loan_record = record
    master = record["masterLoan"]
    r.stage_id = master["loanStageId"]
    r.stage_name = master["loanStage"]["name"]
    steps = r.plan()
    return r, [s.key for s in steps]

@test("resume: every step maps to methods that exist on the harness")
def _():
    # The steps are named as strings, so a rename in maintest.py would otherwise only surface
    # mid-run, after the loan has already been mutated.
    for step in RESUME_STEPS:
        for name in step.methods:
            ok(callable(getattr(G, name, None)), f"{step.key}: GoldLoanApiTest.{name} exists")

@test("resume: the step list covers the post-creation half of run_e2e_test")
def _():
    import inspect
    called = set(re.findall(r"await self\.(\w+)\(", inspect.getsource(G.run_e2e_test)))
    covered = {m for step in RESUME_STEPS for m in step.methods}
    # Everything from ornaments onwards must be reachable by a resume; the earlier steps
    # (customer, KYC, appraiser request, basic details, nominee) are what make it resumable.
    tail = {"store_ornament_details", "check_loan_type", "generate_interest_table",
            "get_final_loan_details", "validate_account", "store_bank_details",
            "add_appraiser_rating", "add_packet_images", "submit_bm_rating_if_required",
            "store_loan_documents", "ops_manual_bank_verification", "submit_ops_rating",
            "disburse_amount", "submit_packet"}
    eq(sorted(tail - covered), [], "steps in run_e2e_test's tail that no resume step runs")
    eq(sorted(tail - called), [], "the expected tail no longer matches run_e2e_test")

@test("resume: each stage id resumes at the right step")
def _():
    cases = [
        (1, "appraiser-rating"), (3, "packet"), (2, "bm-rating"), (8, "documents"),
        (7, "ops-bank"), (4, "disburse"), (18, "disburse"), (16, "disburse"),
        (5, "submit-packet"), (11, "submit-packet"),
    ]
    for stage, expected in cases:
        # everything present, so only the stage decides
        _r, keys = _planned(_loan_record(stage_id=stage, disbursed=(stage in (5, 11))))
        eq(keys[0], expected, f"stage {stage} ({STAGE_NAMES[stage]})")
        eq(keys, RESUME_KEYS[RESUME_KEYS.index(expected):], "and runs every later step")

@test("resume: a completed loan runs nothing")
def _():
    r, keys = _planned(_loan_record(stage_id=13))
    eq(keys, [], "nothing left to do")
    eq(r.resume_key, None, "no resume point")
    ok("already" in r.reason, "and it says why")

    r2, keys2 = _planned(_loan_record(stage_id=5, completed=True))
    eq(keys2, [], "isLoanCompleted also ends it, whatever the stage says")

@test("resume: a missing prerequisite wins over a later stage id")
def _():
    # The live shape of a half-failed run: the stage says "upload documents" but bank-details
    # never stored. Starting at documents would fail on the missing prerequisite.
    r, keys = _planned(_loan_record(stage_id=8, bank=False))
    eq(keys[0], "bank", "backed up to the missing step")
    ok("starting earlier" in r.reason, "and explained itself")

    r2, keys2 = _planned(_loan_record(stage_id=4, packet=False))
    eq(keys2[0], "packet", "same for a loan waiting to disburse with no packet")

@test("resume: an early resume point backs up to ornaments")
def _():
    # The scheme step recomputes eligibility from ornaments held in memory, which a fresh
    # process does not have -- so any resume at or before 'scheme' re-sends them first.
    # Stage 1 says "appraiser rating" but the loan has no final amount, so the record pulls
    # the start back to 'scheme', and that in turn pulls it back to 'ornaments'.
    r, keys = _planned(_loan_record(stage_id=1, final_amount=None))
    eq(keys[0], "ornaments", "re-sends the ornaments before recomputing the scheme")
    ok("backed up" in r.reason, "and says so")

    # Stage 6 "applying" already points at ornaments, so there is nothing to back up.
    r2, keys2 = _planned(_loan_record(stage_id=6, final_amount=None))
    eq(keys2[0], "ornaments", "same start, reached straight from the stage")
    ok("backed up" not in r2.reason, "without a redundant back-up note")

@test("resume: --from overrides both signals")
def _():
    r, keys = _planned(_loan_record(stage_id=8, bank=False), start_from="disburse")
    eq(keys, ["disburse", "submit-packet"], "starts exactly where told")
    eq(r.reason, "--from disburse", "and records why")

@test("resume: an unmapped stage falls back to what the record holds")
def _():
    r, keys = _planned(_loan_record(stage_id=9, bank=False))  # 9 = loan transfer
    eq(keys[0], "bank", "the record decides")
    ok("not part of the new-loan flow" in r.reason, "and it says the stage was unmapped")

@test("resume: rehydration fills the fields the later steps send back")
def _():
    record = _loan_record(stage_id=8)
    r = _resume_runner()
    r.suite.loan_id = "12952"
    calls = []
    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        calls.append(path)
        if "single-loan" in path:
            return _json_response({"data": record})
        return _json_response({})
    r.suite._make_authenticated_request = fake
    async def noop(*a, **kw): return {}
    r.suite.get_customer_by_id = noop
    r.suite._prepare_loan_fields_from_existing = noop

    quiet(lambda: asyncio.run(r.rehydrate()))
    s = r.suite
    eq(s.master_loan_id, "10947", "master loan id")
    eq(s.customer_id, "8463", "customer id")
    eq(s.loan_unique_id, "AUGM-67273", "loan unique id for the final search")
    eq(s.final_loan_amount, 400000.0, "final amount (float, for toBePaid)")
    eq(s.secured_processing_charge, 4000.0, "processing charge")
    eq(s.upfront_interest_amount, 0.0, "upfront interest")
    eq(s.tenure_months, 6, "tenure")
    eq(s.appraiser_request_id, "13501", "appraiser request id, as a string")
    eq(s.internal_branch_id, "1", "branch id, as a string")
    eq(s.bank_account_number, "00000036150491589", "bank account from the stored detail")
    eq(s.bank_for_ops_approval, True, "and its pending-verification flag")
    eq(r.stage_id, 8, "captured the stage")
    close(s)

@test("resume: a loan with no single-loan data fails loudly")
def _():
    r = _resume_runner()
    r.suite.loan_id = "12952"
    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        return _json_response({"data": None})
    r.suite._make_authenticated_request = fake
    try:
        quiet(lambda: asyncio.run(r.rehydrate()))
        raise Fail("an empty single-loan must not be treated as a resumable loan")
    except RuntimeError as e:
        ok("single-loan" in str(e), "the error names what came back empty")
    close(r.suite)

RESUME_ADMIN_TOKEN = "resume-admin-tok"

def _applied_row(uid="AUGM-79787", master_id=10947, loan_id=12952):
    """An applied-loan-details row, shaped like the loan-details rows in the HAR."""
    return {
        "id": master_id,
        "loanStage": {"id": 7, "name": "OPS team rating"},
        "customerLoan": [{"id": loan_id, "loanUniqueId": uid, "masterLoanId": master_id}],
    }

def _loan_search_stub(r, where=None, row=None, scope="appraiser"):
    """Serve the loan listings.

    `where` is (endpoint_fragment, filter_key) naming the ONE combination that returns the row
    -- everything else answers an empty list, like the live portal loan that loan-details does
    not carry. `scope` is which token has to be in use for it to be found.
    """
    s = r.suite
    state = {"paths": [], "scopes": []}

    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        current = "admin" if s.auth_token == RESUME_ADMIN_TOKEN else "appraiser"
        state["paths"].append(path)
        state["scopes"].append(current)
        if path == "/api/user-otp/user-send-otp":
            return _json_response({"referenceCode": "rc"})
        if path == "/api/auth/verify-login":
            return _json_response({"Token": RESUME_ADMIN_TOKEN})

        hit = False
        if where and current == scope:
            endpoint, key = where
            # Match the endpoint EXACTLY: "loan-details" is a substring of
            # "applied-loan-details", and a loose match let the wrong listing answer.
            if path.split("?", 1)[0] == f"/api/loan-process/{endpoint}":
                hit = (f"&{key}=" in path) if key else ("loanUniqueId=" not in path
                                                        and "search=" not in path)
        rows = [row or _applied_row()] if hit else []
        # applied-loan-details answers under `appliedLoanDetails`; loan-details under `data`.
        key = "appliedLoanDetails" if "applied-loan-details" in path else "data"
        return _json_response({key: rows,
                               "pagination": {"hasMore": False, "per_page": 25, "current_page": 1}})

    s._make_authenticated_request = fake
    return state

@test("resume: an in-flight loan is found via applied-loan-details, not loan-details")
def _():
    # The live case: the portal shows AUGM-79787 at ops rating, but
    # loan-details?loanUniqueId=… returns {"data": []} because it only lists finished loans.
    r = _resume_runner(loan="AUGM-79787")
    state = _loan_search_stub(r, where=("applied-loan-details", "loanUniqueId"))
    (master, loan), _out = quiet(lambda: asyncio.run(r._resolve_by_unique_id("AUGM-79787")))
    eq((master, loan), ("10947", "12952"), "ids out of the customerLoan entry")
    queried = {p.split("?", 1)[0] for p in state["paths"] if "loan-process" in p}
    ok("/api/loan-process/loan-details" in queried, "loan-details was tried")
    ok("/api/loan-process/applied-loan-details" in queried,
       "and applied-loan-details too, which is where an in-flight loan actually is")
    close(r.suite)

@test("resume: applied-loan-details rows are read from appliedLoanDetails, not data")
def _():
    # The endpoint does not use the `data` key the rest of the API uses; reading `data` here
    # silently finds nothing.
    payload = {"appliedLoanDetails": [_applied_row()], "pagination": {}}
    rows = G._loan_rows(payload)
    eq(len(rows), 1, "the rows were found under appliedLoanDetails")
    eq(G._loan_rows({"data": [_applied_row()]}), [_applied_row()], "data still works")
    eq(G._loan_rows({"pagination": {}}), [], "and an empty payload is empty")

@test("resume: the search falls back to an unfiltered scan when the filter finds nothing")
def _():
    r = _resume_runner(loan="AUGM-79787")
    state = _loan_search_stub(r, where=("applied-loan-details", None))  # only the bare listing
    (master, loan), _out = quiet(lambda: asyncio.run(r._resolve_by_unique_id("AUGM-79787")))
    eq(loan, "12952", "found by matching the rows client-side")
    ok(any("loanUniqueId=" in p for p in state["paths"]), "the filtered query was tried first")
    close(r.suite)

@test("resume: a loan owned by another login is found under the admin scope")
def _():
    r = _resume_runner(loan="AUGM-79787")
    state = _loan_search_stub(r, where=("applied-loan-details", "loanUniqueId"), scope="admin")
    (_master, loan), _out = quiet(lambda: asyncio.run(r._resolve_by_unique_id("AUGM-79787")))
    eq(loan, "12952", "admin sees it")
    ok("admin" in state["scopes"], "retried under the admin token")
    eq(r.suite.auth_token, "tok", "and restored our own token")
    close(r.suite)

@test("resume: a loan nowhere to be found reports every listing it tried")
def _():
    r = _resume_runner(loan="AUGM-00000")
    _loan_search_stub(r, where=None)
    try:
        quiet(lambda: asyncio.run(r._resolve_by_unique_id("AUGM-00000")))
        raise Fail("an unfindable loan must fail, not resume something else")
    except RuntimeError as e:
        ok("applied-loan-details" in str(e), "names the in-flight listing")
        ok("loan-details" in str(e), "and the completed one")
        ok("admin scope" in str(e), "and says the admin scope was tried")
    close(r.suite)

@test("resume: the right customer loan is picked when a master loan holds several")
def _():
    row = {"id": 10947, "customerLoan": [
        {"id": 11111, "loanUniqueId": "AUGM-OTHER", "masterLoanId": 10947},
        {"id": 12952, "loanUniqueId": "AUGM-79787", "masterLoanId": 10947},
    ]}
    master, loan = ResumeLoanTest._row_ids(row, "AUGM-79787")
    eq((master, loan), ("10947", "12952"), "matched on the unique id, not just the first entry")

@test("resume: a row with no customer loan id fails with the row's keys")
def _():
    r = _resume_runner(loan="AUGM-79787")
    _loan_search_stub(r, where=("applied-loan-details", "loanUniqueId"),
                      row={"loanUniqueId": "AUGM-79787", "someOtherShape": True})
    try:
        quiet(lambda: asyncio.run(r._resolve_by_unique_id("AUGM-79787")))
        raise Fail("a row we cannot read ids from must not resume a mystery loan")
    except RuntimeError as e:
        ok("--loan-id" in str(e), "tells the user how to get past it")
    close(r.suite)

@test("resume: only loanUniqueId is ever sent as a filter")
def _():
    # These endpoints map an unrecognised query parameter onto a database column, so a guessed
    # filter is a 500, not an ignored one:
    #   applied-loan-details?...&search=AUGM-79787
    #   -> 500 "column customerLoanMaster.search does not exist"
    for _endpoint, _extras, filters in G.LOAN_SEARCH_ENDPOINTS:
        eq(sorted(set(filters) - {""}), ["loanUniqueId"],
           "no speculative filter names in the search ladder")

@test("resume: the live search sends no filter other than loanUniqueId")
def _():
    r = _resume_runner(loan="AUGM-79787")
    state = _loan_search_stub(r, where=("loan-details", "loanUniqueId"))
    quiet(lambda: asyncio.run(r._resolve_by_unique_id("AUGM-79787")))
    for path in state["paths"]:
        if "loan-process" not in path:
            continue
        params = {p.split("=")[0] for p in path.split("?", 1)[-1].split("&")}
        eq(sorted(params - {"from", "to", "isRejectedLoan", "loanUniqueId"}), [],
           f"unexpected query parameter in {path}")
    close(r.suite)

@test("resume: a 500 from one listing does not stop the search")
def _():
    # Belt and braces: even if some future parameter blows up server-side, the ladder has to
    # carry on to the listing that does work.
    r = _resume_runner(loan="AUGM-79787")
    s = r.suite
    state = {"paths": []}

    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        state["paths"].append(path)
        if path.startswith("/api/user-otp") or path.startswith("/api/auth"):
            return _json_response({"referenceCode": "rc", "Token": RESUME_ADMIN_TOKEN})
        if "applied-loan-details" in path:
            request = httpx.Request("GET", "https://x" + path)
            response = httpx.Response(
                500, text="<h1>column customerLoanMaster.search does not exist</h1>",
                request=request)
            raise httpx.HTTPStatusError("500", request=request, response=response)
        rows = [_applied_row()] if "loanUniqueId=" in path else []
        return _json_response({"data": rows, "pagination": {"hasMore": False}})

    s._make_authenticated_request = fake
    (_master, loan), _out = quiet(lambda: asyncio.run(r._resolve_by_unique_id("AUGM-79787")))
    eq(loan, "12952", "fell through to loan-details and found it there")
    close(s)

@test("resume: the supplied loan unique id survives a loan that has none yet")
def _():
    # single-loan reports loanUniqueId as null until the assign-packet stage, but the closing
    # fetch_loan_details searches by it -- so the id the user passed must not be lost.
    r = _resume_runner(loan="AUGM-79787")
    _loan_search_stub(r, where=("applied-loan-details", "loanUniqueId"))
    quiet(lambda: asyncio.run(r.resolve_loan()))
    eq(r.suite.loan_unique_id, "AUGM-79787", "kept from the command line")

    record = _loan_record(stage_id=7)
    record["loanUniqueId"] = None  # not assigned yet
    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        return _json_response({"data": record} if "single-loan" in path else {})
    r.suite._make_authenticated_request = fake
    async def noop(*a, **kw): return {}
    r.suite.get_customer_by_id = noop
    r.suite._prepare_loan_fields_from_existing = noop
    quiet(lambda: asyncio.run(r.rehydrate()))
    eq(r.suite.loan_unique_id, "AUGM-79787", "and not clobbered by a null from single-loan")
    close(r.suite)

# --------------------------------------------------------------------------------------------
# GROUP: stage -- "Loan Stage has been changed to: …" recovery
# --------------------------------------------------------------------------------------------

STAGE_CHANGED_BODY = ('{"message":"Loan Stage has been changed to: bm rating, '
                      'please re-visit the applied loan page."}')

def _stage_suite(amount=500000):
    s = suite()
    s.loan_id, s.master_loan_id = "11312", "9939"
    s.customer_id = "20308"
    s.final_loan_amount = amount
    s.logged_in_user_id = 1473
    return s

def _stage_error(body=STAGE_CHANGED_BODY, status=400):
    request = httpx.Request("POST", "https://x/api/loan-process/ops-rating")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)

def _bm_recorder(s):
    """Count BM ratings without touching the network."""
    state = {"bm": 0, "tokens": []}
    async def role_token(login_type):
        state["tokens"].append(login_type)
        return f"{login_type}-tok"
    async def add_bm_rating():
        state["bm"] += 1
    s._role_token = role_token
    s.add_bm_rating = add_bm_rating
    return state

@test("stage: the pending stage is parsed out of the server's message")
def _():
    s = _stage_suite()
    eq(s._stage_changed_to(STAGE_CHANGED_BODY), "bm rating", "the stage name, lower-cased")
    eq(s._stage_changed_to('{"message":"Loan Stage has been changed to: disbursement pending, '
                           'please re-visit the applied loan page."}'),
       "disbursement pending", "a multi-word stage")
    eq(s._stage_changed_to('{"message":"To Be Paid amount is incorrect"}'), "",
       "an unrelated error is not a stage change")
    eq(s._stage_changed_to(""), "", "and neither is an empty body")
    close(s)

@test("stage: a loan of exactly 5L DOES need the BM rating")
def _():
    # The live failure: loanAmountDigi "500000.00" was skipped by a strict > threshold, and the
    # server then refused loan-documents and ops-rating with "changed to: bm rating".
    s = _stage_suite(amount=500000)
    state = _bm_recorder(s)
    _r, out = quiet(lambda: asyncio.run(s.submit_bm_rating_if_required()))
    eq(state["bm"], 1, "the BM rating was submitted at the threshold")
    eq(state["tokens"], ["bm"], "under the BM login")
    eq(s.bm_rating_submitted, True, "and recorded")
    ok("required" in out, "and said so")
    close(s)

@test("stage: a loan under 5L still skips the BM rating")
def _():
    s = _stage_suite(amount=499999.99)
    state = _bm_recorder(s)
    _r, out = quiet(lambda: asyncio.run(s.submit_bm_rating_if_required()))
    eq(state["bm"], 0, "no BM rating below the threshold")
    ok("skipped" in out, "and said why")
    close(s)

@test("stage: a stage-change 400 submits the BM rating and retries the call")
def _():
    s = _stage_suite()
    state = _bm_recorder(s)
    calls = {"n": 0}
    async def send():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _stage_error()
        return "ok"
    result, out = quiet(lambda: asyncio.run(s._with_stage_recovery("ops rating", send)))
    eq(result, "ok", "the retry succeeded")
    eq(calls["n"], 2, "called exactly twice")
    eq(state["bm"], 1, "the pending BM rating was settled first")
    ok("Retrying ops rating" in out, "and it said what it retried")
    close(s)

@test("stage: recovery is attempted ONCE, not looped")
def _():
    # Force the settle step to keep claiming it fixed something: without a single-retry
    # contract this recurses forever. (The duplicate-BM guard also stops the real path, but
    # that is a different safeguard -- this pins the retry contract on its own.)
    s = _stage_suite()
    _bm_recorder(s)
    async def always_settled(_text):
        return True
    s._settle_pending_stage = always_settled
    calls = {"n": 0}
    async def send():
        calls["n"] += 1
        raise _stage_error()
    try:
        quiet(lambda: asyncio.run(s._with_stage_recovery("ops rating", send)))
        raise Fail("a stage that keeps moving must surface, not loop")
    except httpx.HTTPStatusError:
        pass
    except RecursionError:
        raise Fail("the recovery recursed instead of retrying once")
    eq(calls["n"], 2, "one retry only")
    close(s)

@test("stage: an already-submitted BM rating is not submitted again")
def _():
    s = _stage_suite()
    state = _bm_recorder(s)
    s.bm_rating_submitted = True
    calls = {"n": 0}
    async def send():
        calls["n"] += 1
        raise _stage_error()
    try:
        quiet(lambda: asyncio.run(s._with_stage_recovery("ops rating", send)))
        raise Fail("nothing left to settle, so the error must surface")
    except httpx.HTTPStatusError:
        pass
    eq(state["bm"], 0, "no duplicate BM rating")
    eq(calls["n"], 1, "and no pointless retry")
    close(s)

@test("stage: an unrelated 400 and any non-400 are re-raised untouched")
def _():
    s = _stage_suite()
    state = _bm_recorder(s)
    for err in (_stage_error(body='{"message":"To Be Paid amount is incorrect"}'),
                _stage_error(body=STAGE_CHANGED_BODY, status=500)):
        calls = {"n": 0}
        async def send(err=err):
            calls["n"] += 1
            raise err
        try:
            quiet(lambda: asyncio.run(s._with_stage_recovery("bank details", send)))
            raise Fail("the original error must survive")
        except httpx.HTTPStatusError as raised:
            eq(raised.response.status_code, err.response.status_code, "same error")
        eq(calls["n"], 1, "no retry")
    eq(state["bm"], 0, "and ops/bm were not dragged into an unrelated failure")
    close(s)

@test("stage: ops rating and loan documents both go through the recovery")
def _():
    import inspect
    for method in (G.submit_ops_rating, G.store_loan_documents):
        src = inspect.getsource(method)
        ok("_with_stage_recovery" in src,
           f"{method.__name__} must route through the stage recovery")

@test("stage: the closing loan read finds the loan via the shared ladder")
def _():
    # The live symptom: loan-details?loanUniqueId=AUGM-59084 came back empty for the run's OWN
    # just-completed loan, so the final step printed nothing -- and the run still said PASSED.
    s = _stage_suite()
    s.loan_unique_id = "AUGM-59084"
    s.logged_in_mobile_number = "8880008881"
    row = {"id": 10947, "loanStage": {"id": 13, "name": "packet submitted"},
           "finalLoanAmount": 500000.00, "loanType": "Fresh Loan", "tenure": 6,
           "customer": {"firstName": "A", "lastName": "B", "customerUniqueId": "MS1",
                        "mobileNumber": "9", "panCardNumber": "P"},
           "appraiser": {"firstName": "Ap", "lastName": "Pr"},
           "customerLoan": [{"id": 12952, "loanUniqueId": "AUGM-59084"}]}
    state = {"paths": []}

    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        state["paths"].append(path)
        if path.startswith("/api/user-otp") or path.startswith("/api/auth"):
            return _json_response({"referenceCode": "rc", "Token": "admin-tok"})
        # loan-details never carries it; applied-loan-details does.
        hit = "applied-loan-details" in path and "loanUniqueId=" in path
        key = "appliedLoanDetails" if "applied-loan-details" in path else "data"
        return _json_response({key: [row] if hit else [],
                               "pagination": {"hasMore": False}})

    s._make_authenticated_request = fake
    loan, out = quiet(lambda: asyncio.run(s.fetch_loan_details()))
    eq(loan.get("id"), 10947, "the finished loan was found and returned")
    ok("AUGM-59084" in out, "and its details printed")
    ok("packet submitted" in out, "including the stage it ended at")
    close(s)

@test("stage: a loan that cannot be read back FAILS instead of passing quietly")
def _():
    s = _stage_suite()
    s.loan_unique_id = "AUGM-59084"
    s.logged_in_mobile_number = "8880008881"

    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        if path.startswith("/api/user-otp") or path.startswith("/api/auth"):
            return _json_response({"referenceCode": "rc", "Token": "admin-tok"})
        return _json_response({"data": [], "pagination": {"hasMore": False}})

    s._make_authenticated_request = fake
    try:
        quiet(lambda: asyncio.run(s.fetch_loan_details()))
        raise Fail("a closing check that found nothing must not report success")
    except AssertionError as e:
        if isinstance(e, Fail):
            raise
        ok("AUGM-59084" in str(e), "the error names the loan")
        ok("admin scope" in str(e), "and says the admin scope was tried too")
    close(s)

@test("stage: no loanUniqueId fails rather than printing some other loan")
def _():
    # Without an id the old code fell back to the unfiltered list and took row[0] -- a
    # DIFFERENT loan, printed as if it were this run's.
    s = _stage_suite()
    s.loan_unique_id = ""
    called = {"n": 0}
    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        called["n"] += 1
        return _json_response({"data": [{"id": 1, "customerLoan": [{"loanUniqueId": "AUGM-OTHER"}]}]})
    s._make_authenticated_request = fake
    try:
        quiet(lambda: asyncio.run(s.fetch_loan_details()))
        raise Fail("with no id there is nothing to verify against")
    except AssertionError as e:
        if isinstance(e, Fail):
            raise
        ok("assign-packet" in str(e), "the error says where the id comes from")
    eq(called["n"], 0, "and it did not go fishing in the list")
    close(s)

@test("stage: the loan search is shared, not duplicated in the resume runner")
def _():
    import inspect
    ok(not hasattr(ResumeLoanTest, "LOAN_SEARCH_ENDPOINTS"),
       "the resume runner must not keep its own copy of the endpoint table")
    ok("find_loan_row" in inspect.getsource(ResumeLoanTest._resolve_by_unique_id),
       "it delegates to the harness's search")

# --------------------------------------------------------------------------------------------
# GROUP: batch -- maintest.py --count N, many loans in one session
# --------------------------------------------------------------------------------------------

def _batch_suite():
    s = suite()
    s.auth_token = "appraiser-tok"
    s.logged_in_user_id = 1473
    s.appraiser_id = "1473"
    s._role_token_cache = {"8880008880": "admin-tok"}
    return s

def _batch_recorder(s, fail_on=()):
    """Replace run_e2e_test with a recorder. `fail_on` is the 1-based loans that blow up."""
    state = {"runs": [], "skips": [], "tokens": [], "caches": []}
    async def fake_run(login_type, skip_login=False):
        n = len(state["runs"]) + 1
        state["runs"].append(n)
        state["skips"].append(skip_login)
        state["tokens"].append(s.auth_token)
        state["caches"].append(dict(s._role_token_cache))
        # Whatever the previous loan left behind must already be gone by now.
        state.setdefault("seen_customer", []).append(s.customer_unique_id)
        state.setdefault("seen_loan", []).append(s.loan_unique_id)
        s.customer_unique_id = f"MSCUST{n}"
        s.loan_unique_id = f"AUGM-{n}"
        s.final_loan_amount = 400000.0
        s.master_loan_id, s.loan_id = str(1000 + n), str(2000 + n)
        s.bm_rating_submitted = True
        s.bank_for_ops_approval = True
        s._api_calls, s._api_failures = 40, 0
        if n in fail_on:
            raise httpx.HTTPStatusError(
                "400", request=httpx.Request("POST", "https://x/api/loan-process/bank-details"),
                response=httpx.Response(400, text="bank details is not verified"))
        return True
    s.run_e2e_test = fake_run
    return state

@test("batch: only the FIRST loan logs in; the rest reuse the session")
def _():
    # The whole point: the OTP send endpoint rate-limits repeat requests to a number
    # ("Please try again after a minute"), which a login-per-loan batch would hit at once.
    s = _batch_suite()
    state = _batch_recorder(s)
    passed, _out = quiet(lambda: asyncio.run(s.run_many("appraiser", 3)))
    eq(passed, True, "all three loans passed")
    eq(state["runs"], [1, 2, 3], "ran three times")
    eq(state["skips"], [False, True, True], "loan 1 logs in, loans 2-3 do not")
    eq(set(state["tokens"]), {"appraiser-tok"}, "the same session throughout")
    close(s)

@test("batch: the role-token cache survives, so each role logs in once for the batch")
def _():
    s = _batch_suite()
    state = _batch_recorder(s)
    quiet(lambda: asyncio.run(s.run_many("appraiser", 3)))
    for cache in state["caches"]:
        eq(cache.get("8880008880"), "admin-tok", "the admin token is reused, not re-fetched")
    close(s)

@test("batch: each loan starts with the previous loan's identity cleared")
def _():
    s = _batch_suite()
    state = _batch_recorder(s)
    quiet(lambda: asyncio.run(s.run_many("appraiser", 3)))
    eq(state["seen_customer"], ["", "", ""], "no customer carried over")
    eq(state["seen_loan"], ["", "", ""], "and no loan id either")
    close(s)

@test("batch: per-loan flags that would corrupt the next loan are reset")
def _():
    s = _batch_suite()
    s.bm_rating_submitted = True
    s.bank_manually_verified = True
    s.bank_for_ops_approval = False
    s.loan_ornaments_details = [{"id": 1}]
    s.available_packet = {"id": 7}
    s._api_calls, s._api_failures = 88, 5
    quiet(s._reset_for_next_loan)
    eq(s.bm_rating_submitted, False, "a stale BM flag would skip a needed BM rating")
    eq(s.bank_manually_verified, False, "and a stale bank verdict would skip the ops approval")
    eq(s.loan_ornaments_details, [], "ornaments are per loan")
    eq(s.available_packet, {}, "so is the packet")
    eq((s._api_calls, s._api_failures), (0, 0), "metrics are per loan, not cumulative")
    close(s)

@test("batch: the bank ACCOUNT is kept but its verification is not")
def _():
    # Re-walking the candidate accounts each loan would re-fire the same doomed penny-drops.
    s = _batch_suite()
    s.bank_account_number, s.bank_ifsc_code = "00600350006733", "HDFC0000060"
    s.bank_name, s.bank_account_label = "HDFC", "HDFC - LIC MF LIQUID FUND"
    s.bank_account_verified = True
    quiet(s._reset_for_next_loan)
    eq(s.bank_account_number, "00600350006733", "the account that worked is kept")
    eq(s.bank_ifsc_code, "HDFC0000060", "with its IFSC")
    eq(s.bank_account_verified, False, "but it must be re-validated for this loan")
    close(s)

@test("batch: pinned CLI choices survive the reset")
def _():
    s = _batch_suite()
    s.forced_scheme_id = "853"
    s._forced_co_lender_id = "4"
    s.partner_id = "152"
    s.scheme_id = "853"
    quiet(s._reset_for_next_loan)
    eq(s.forced_scheme_id, "853", "--scheme is a run-wide choice")
    eq(s._forced_co_lender_id, "4", "so is --co-lender")
    eq(s.co_lender_bank_id, "4", "which is re-applied to the next loan")
    eq(s.partner_id, "152", "and the partner")
    eq(s.scheme_id, "", "but the resolved scheme is re-picked per loan")
    close(s)

@test("batch: one failed loan does not stop the others")
def _():
    s = _batch_suite()
    state = _batch_recorder(s, fail_on=(2,))
    passed, out = quiet(lambda: asyncio.run(s.run_many("appraiser", 3)))
    eq(state["runs"], [1, 2, 3], "loan 3 still ran after loan 2 failed")
    eq(passed, False, "but the batch verdict is a failure")
    ok("2/3 loans completed" in out, "the summary counts them")
    ok("HTTPStatusError" in out, "and names what went wrong")
    close(s)

@test("batch: the reset covers everything the KYC runner resets")
def _():
    # test_kyc.py resets per-customer state for its own batches; a field it clears but the loan
    # batch does not would leak one customer's identity into the next loan.
    missing = sorted(set(KycOnlyTest.PER_CUSTOMER_STATE) - set(G.PER_LOAN_STATE))
    eq(missing, [], "fields the KYC runner clears that PER_LOAN_STATE does not")

@test("batch: every name in PER_LOAN_STATE is a real attribute")
def _():
    s = _batch_suite()
    missing = [f for f in G.PER_LOAN_STATE if not hasattr(s, f)]
    eq(missing, [], "PER_LOAN_STATE must not name attributes that no longer exist")
    close(s)

def _cli(**over):
    class A:
        count, kyc, customer = 1, "manual", None
    a = A()
    for k, v in over.items():
        setattr(a, k, v)
    errors = []
    def fail(message):
        errors.append(message)
        raise SystemExit(2)
    try:
        validate_cli(a, fail)
    except SystemExit:
        pass
    return errors

@test("batch: --customer is allowed together with --count")
def _():
    eq(_cli(count=3, customer="MS35QNJP"), [], "many loans for one existing customer is valid")
    eq(_cli(count=3, kyc="auto", customer="MS35QNJP"), [],
       "and a real-identity mode is fine there -- no new customer is created")

@test("batch: a NEW-customer batch still refuses a real-identity KYC mode")
def _():
    errors = _cli(count=3, kyc="auto")
    eq(len(errors), 1, "rejected")
    ok("one customer" in errors[0], "and says why")
    ok("--customer" in errors[0], "and points at the way to make it work")
    eq(_cli(count=3, kyc="manual"), [], "manual is fine for a new-customer batch")

@test("batch: --count below 1 is rejected")
def _():
    ok(_cli(count=0), "zero is not a batch")
    eq(_cli(count=1), [], "one is the normal single run")

@test("batch: the existing-customer selection survives the per-loan reset")
def _():
    # --customer is a run-wide choice: run_e2e_test re-reads it each loan and re-resolves the
    # customer. Clearing it would turn loans 2..N into new-customer runs.
    s = _batch_suite()
    s.existing_customer_unique_id = "MS35QNJP"
    s.existing_customer_id = ""
    s.customer_unique_id, s.customer_id = "MS35QNJP", "8500"
    quiet(s._reset_for_next_loan)
    eq(s.existing_customer_unique_id, "MS35QNJP", "the selection is kept")
    eq(s.customer_unique_id, "", "but the resolved customer is re-fetched per loan")
    close(s)

@test("batch: a loan that reuses the PREVIOUS master loan is reported as failed")
def _():
    # With --customer, loans 2..N land on the same appraiser request. If the server answers
    # "Your loan already initiated" instead of opening a new loan, every step would re-run
    # against the finished loan and could still look like a pass.
    s = _batch_suite()
    state = {"n": 0}
    async def fake_run(login_type, skip_login=False):
        state["n"] += 1
        s.customer_unique_id = "MS35QNJP"
        s.master_loan_id = "9939"          # the SAME master loan every time
        s.loan_unique_id = "AUGM-1"
        return True
    s.run_e2e_test = fake_run
    passed, out = quiet(lambda: asyncio.run(s.run_many("appraiser", 3)))
    eq(passed, False, "a repeated loan is not a pass")
    ok("handed back the previous one" in out, "and the reason is spelled out")
    ok("1/3 loans completed" in out, "only the first one counts")
    close(s)

@test("batch: distinct loans for the same customer all pass")
def _():
    s = _batch_suite()
    state = {"n": 0}
    async def fake_run(login_type, skip_login=False):
        state["n"] += 1
        s.customer_unique_id = "MS35QNJP"
        s.master_loan_id = str(9939 + state["n"])   # a new master loan each time
        s.loan_unique_id = f"AUGM-{state['n']}"
        return True
    s.run_e2e_test = fake_run
    passed, out = quiet(lambda: asyncio.run(s.run_many("appraiser", 3)))
    eq(passed, True, "three real loans for one customer")
    ok("3/3 loans completed" in out, "all counted")
    close(s)

# --------------------------------------------------------------------------------------------
# GROUP: gateway -- 502/503/504 and transport blips
# --------------------------------------------------------------------------------------------

NGINX_502 = ("<html>\n<head><title>502 Bad Gateway</title></head>\n<body>\n"
             "<center><h1>502 Bad Gateway</h1></center>\n<hr><center>nginx/1.30.4</center>\n"
             "</body>\n</html>")

def _gateway_suite(**env):
    for var in ("GOLD_LOAN_GATEWAY_RETRIES", "GOLD_LOAN_GATEWAY_RETRY_DELAY"):
        if var not in env:
            os.environ.pop(var, None)
    env.setdefault("GOLD_LOAN_GATEWAY_RETRY_DELAY", "0")  # no real sleeping in tests
    s = suite(**env)
    s.auth_token = "tok"
    return s

def _responses(s, script):
    """Drive _send_with_gateway_retry from `script`: ints are statuses, exceptions are raised."""
    state = {"sent": 0}
    async def send_once():
        item = script[min(state["sent"], len(script) - 1)]
        state["sent"] += 1
        if isinstance(item, BaseException):
            raise item
        request = httpx.Request("GET", "https://x/api/customer")
        return httpx.Response(item, text=NGINX_502 if item >= 500 else "{}", request=request)
    return state, send_once

@test("gateway: a 502 on a GET is retried and the good response is used")
def _():
    # The live failure: GET /api/customer answered 502 while the backend was restarting, and
    # the whole batch went down with it.
    s = _gateway_suite()
    state, send = _responses(s, [502, 502, 200])
    response, out = quiet(lambda: asyncio.run(s._send_with_gateway_retry("GET", "/api/customer", send)))
    eq(response.status_code, 200, "the retry succeeded")
    eq(state["sent"], 3, "two retries after the first 502")
    ok("retrying in" in out, "and it said it was retrying")
    close(s)

@test("gateway: a GET that never recovers returns the last 502 rather than looping")
def _():
    s = _gateway_suite()
    state, send = _responses(s, [502])
    response, _out = quiet(lambda: asyncio.run(s._send_with_gateway_retry("GET", "/api/customer", send)))
    eq(response.status_code, 502, "the caller still sees the failure")
    eq(state["sent"], 3, "exactly the configured number of attempts")
    close(s)

@test("gateway: a WRITE is NOT retried on a 502")
def _():
    # nginx cannot say whether the backend applied the write before dying; re-sending a create
    # would duplicate a customer, a loan or a rating.
    s = _gateway_suite()
    state, send = _responses(s, [502, 200])
    response, out = quiet(lambda: asyncio.run(s._send_with_gateway_retry("POST", "/api/customer", send)))
    eq(response.status_code, 502, "the 502 is handed straight back")
    eq(state["sent"], 1, "sent exactly once")
    ok("NOT retried" in out, "and it explained why")
    close(s)

@test("gateway: a write IS retried when the connection never got made")
def _():
    # Nothing can have been processed if no connection was established.
    s = _gateway_suite()
    state, send = _responses(s, [httpx.ConnectError("refused"), 200])
    response, _out = quiet(lambda: asyncio.run(s._send_with_gateway_retry("POST", "/api/customer", send)))
    eq(response.status_code, 200, "the retry went through")
    eq(state["sent"], 2, "one retry")
    close(s)

@test("gateway: a write is NOT retried on a read timeout")
def _():
    # The request went out; the server may well have applied it.
    s = _gateway_suite()
    state, send = _responses(s, [httpx.ReadTimeout("slow"), 200])
    try:
        quiet(lambda: asyncio.run(s._send_with_gateway_retry("POST", "/api/customer", send)))
        raise Fail("a half-completed write must surface, not be repeated")
    except httpx.ReadTimeout:
        pass
    eq(state["sent"], 1, "sent exactly once")
    close(s)

@test("gateway: a 4xx is never retried")
def _():
    s = _gateway_suite()
    state, send = _responses(s, [400, 200])
    response, _out = quiet(lambda: asyncio.run(s._send_with_gateway_retry("GET", "/api/customer", send)))
    eq(response.status_code, 400, "a client error is the caller's to handle")
    eq(state["sent"], 1, "no retry")
    close(s)

@test("gateway: retries can be turned off")
def _():
    s = _gateway_suite(GOLD_LOAN_GATEWAY_RETRIES="1")
    state, send = _responses(s, [502, 200])
    response, _out = quiet(lambda: asyncio.run(s._send_with_gateway_retry("GET", "/api/customer", send)))
    eq(response.status_code, 502, "one attempt only")
    eq(state["sent"], 1, "and it really was one")
    close(s)

@test("gateway: an HTML error page is collapsed to one line in the report")
def _():
    eq(G._short_error_body(NGINX_502), "502 Bad Gateway (nginx/1.30.4)",
       "eight lines of markup become one")
    eq(G._short_error_body('{"message":"To Be Paid amount is incorrect"}'),
       '{"message":"To Be Paid amount is incorrect"}', "a JSON body is left alone")
    eq(G._short_error_body(""), "", "and an empty body stays empty")
    ok(len(G._short_error_body("<!DOCTYPE html><html><h1>column x does not exist</h1></html>")) < 60,
       "the 500 HTML page is collapsed too")

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
