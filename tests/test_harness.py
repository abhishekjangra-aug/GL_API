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

@test("appraiser: our own COMPLETED request is cancelled and recreated")
def _():
    s = _appraiser_suite()
    item = _request_item(master_loan={"id": 10930, "isLoanCompleted": True}, process_complete=True)
    state = _stub_api(s, {"appraiser": [item]}, create_ok=True)
    _r, _out = quiet(lambda: asyncio.run(s.create_appraiser_request()))
    eq(state["cancels"], 1, "our finished loan is cancelled")
    eq(state["posts"], 2, "a fresh request is created after the cancel")
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
    for var in ("GOLD_LOAN_BANK_ACCOUNTS", "GOLD_LOAN_BANK_ACCOUNT_ATTEMPTS"):
        if var not in env:
            os.environ.pop(var, None)
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

def _accounts_stub(s, verdicts):
    """Serve validate-account from `verdicts`: {accountNumber: payload-or-status-int}.

    An int means the server answers with that HTTP status (the live UAT 400). A dict is the
    JSON body. An account not listed answers 400, like an account the bank rejects.
    Returns a state dict recording the accounts tried, in order.
    """
    state = {"tried": [], "karza": []}

    async def fake(method, path, json_data=None, params=None, files=None, headers=None):
        if path.startswith("/api/loan-process/account-details-karza"):
            state["karza"].append(path.split("ifscCode=")[1])
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
    s = _bank_suite()
    first = s._load_bank_accounts()[0]["accountNumber"]
    state = _accounts_stub(s, {first: {"data": {"bankTxnStatus": True}, "isVerified": True}})
    _r, out = quiet(lambda: asyncio.run(s.validate_account()))
    eq(state["tried"], [first], "no further penny-drops once one verifies")
    eq(s.bank_system_verified, True, "recorded the verified flag")
    ok("VERIFIED outright" in out, "reported it")
    close(s)

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
