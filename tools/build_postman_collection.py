"""Generate the Augmont Gold Loan end-to-end Postman collection + TEST/UAT environments.

The collection is a faithful port of src/maintest.py's `run_e2e_test` flow: it drives the same
endpoints, in the same order, with the same bodies, the same HMAC signing and the same CryptoJS
encryption -- but runs entirely inside the Postman Collection Runner (or newman).

Run:
    python tools/build_postman_collection.py

Outputs (postman/):
    Augmont-GoldLoan-E2E.postman_collection.json
    Augmont-GL-TEST.postman_environment.json
    Augmont-GL-UAT.postman_environment.json

The environments are generated FROM GoldLoanApiTest.ENVIRONMENTS, so the hosts and every role /
partner / partner-branch-user mobile stay in sync with the harness automatically. The shared
JavaScript lives in tools/postman_lib.js and is embedded as the GL_LIB collection variable.
"""

import base64
import json
import mimetypes
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from maintest import GoldLoanApiTest  # noqa: E402  (path set above)

OUT_DIR = os.path.join(ROOT, "postman")
LIB_PATH = os.path.join(ROOT, "tools", "postman_lib.js")

LIB = "eval(pm.collectionVariables.get(\"GL_LIB\"));\n"


# --------------------------------------------------------------------------------------------
# request builder
# --------------------------------------------------------------------------------------------
def req(name, method, path, pre="", test="", json_body=False, form=None,
        extra_headers=None, description=""):
    """One Postman request.

    path         relative API path (may embed {{vars}} and a query string)
    pre/test     JS appended after the GL_LIB eval
    json_body    True  -> raw body is {{__body}}, which the pre-request script fills via GL.body()
    form         list of (key, type, value) -> multipart/form-data ('file' type uses src)
    """
    headers = [{"key": "Authorization", "value": "Bearer {{token}}"}]
    if json_body:
        headers.append({"key": "Content-Type", "value": "application/json"})
    for key, value in (extra_headers or []):
        headers.append({"key": key, "value": value})

    item = {
        "name": name,
        "request": {
            "method": method,
            "header": headers,
            "url": "{{base_url}}" + path,
        },
        "event": [],
    }
    if description:
        item["request"]["description"] = description
    if json_body:
        item["request"]["body"] = {"mode": "raw", "raw": "{{__body}}",
                                   "options": {"raw": {"language": "json"}}}
    elif form:
        item["request"]["body"] = {
            "mode": "formdata",
            "formdata": [
                ({"key": k, "type": "file", "src": v} if t == "file"
                 else {"key": k, "type": "text", "value": v})
                for k, t, v in form
            ],
        }

    item["event"].append({"listen": "prerequest",
                          "script": {"type": "text/javascript",
                                     "exec": (LIB + pre).strip("\n").split("\n")}})
    if test:
        item["event"].append({"listen": "test",
                              "script": {"type": "text/javascript",
                                         "exec": test.strip("\n").split("\n")}})
    return item


def folder(name, items, description=""):
    f = {"name": name, "item": items}
    if description:
        f["description"] = description
    return f


def login_pair(role, mobile_var, extra_capture=""):
    """send-otp + verify-login for one role; stores token_<role>."""
    send = req(
        "%s: send OTP" % role,
        "POST", "/api/user-otp/user-send-otp",
        pre=("GL.set('token', '');\n"
             "GL.body({ mobileNumber: String(GL.get('%s')), type: 'login', id: null });" % mobile_var),
        test=("GL.ok('%s send-otp');\n"
              "GL.set('ref_%s', GL.json().referenceCode);" % (role, role)),
        json_body=True,
        description="Static test OTP is 1234; the send-otp endpoint rate-limits repeat requests "
                    "to the same number, so each role logs in exactly once per run.",
    )
    verify = req(
        "%s: verify login" % role,
        "POST", "/api/auth/verify-login",
        pre=("GL.set('token', '');\n"
             "GL.body({ referenceCode: GL.get('ref_%s'), otp: 1234, type: 'login', isFromWeb: true });" % role),
        test=("GL.ok('%s verify-login');\n"
              "var t = GL.json().Token;\n"
              "pm.test('%s token captured', function () { pm.expect(!!t).to.be.true; });\n"
              "GL.set('token_%s', t);\n"
              "GL.set('token', t);\n" % (role, role, role)) + extra_capture,
        json_body=True,
    )
    return [send, verify]


# --------------------------------------------------------------------------------------------
# 00 — logins
# --------------------------------------------------------------------------------------------
def folder_logins():
    items = []
    items += login_pair(
        "appraiser", "mobile_appraiser",
        extra_capture=(
            "var p = GL.jwtPayload(t);\n"
            "GL.set('internal_branch_id', p.internalBranchId || GL.get('internal_branch_id', '1'));\n"
            "GL.set('logged_in_user_id', p.id);\n"
            "GL.set('logged_in_mobile_number', GL.get('mobile_appraiser'));\n"
            "GL.log('appraiser user', p.id, 'branch', p.internalBranchId);"
        ),
    )
    items += login_pair("admin", "mobile_admin")
    items += login_pair("ops", "mobile_ops")
    items += login_pair("bm", "mobile_bm")

    # The partner login depends on WHICH partner underwrites this loan (152 Roshan / 10 Arvog)
    # and on the environment, so resolve the number before sending the OTP.
    resolve = req(
        "partner: resolve mobile + send OTP",
        "POST", "/api/user-otp/user-send-otp",
        pre=("GL.set('token', '');\n"
             "var key = String(GL.get('partner_key', '152'));\n"
             "var m = GL.get('partner_mobile_' + key);\n"
             "if (!m) { throw new Error('No partner mobile for partner ' + key + ' in this environment.'); }\n"
             "GL.set('mobile_partner', m);\n"
             "GL.set('partner_user_mobile', GL.get('partner_user_mobile_' + key));\n"
             "GL.log('partner login', m, 'partner', key);\n"
             "GL.body({ mobileNumber: String(m), type: 'login', id: null });"),
        test=("GL.ok('partner send-otp');\nGL.set('ref_partner', GL.json().referenceCode);"),
        json_body=True,
    )
    verify = req(
        "partner: verify login",
        "POST", "/api/auth/verify-login",
        pre=("GL.set('token', '');\n"
             "GL.body({ referenceCode: GL.get('ref_partner'), otp: 1234, type: 'login', isFromWeb: true });"),
        test=("GL.ok('partner verify-login');\n"
              "GL.set('token_partner', GL.json().Token);\n"
              "GL.role('appraiser');"),
        json_body=True,
    )
    items += [resolve, verify]
    return folder("00 - Logins (all roles)", items,
                  "Logs in every role the flow hands the loan to (appraiser, admin, ops, bm, "
                  "partner) and stores one token each. Mobiles come from the selected environment.")


# --------------------------------------------------------------------------------------------
# 01 — master data
# --------------------------------------------------------------------------------------------
def folder_master():
    items = [
        req("States", "GET", "/api/state",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('states');\n"
                  "var d = GL.json().data;\n"
                  "GL.set('state_id', GL.pick(d).id);")),
        req("Cities", "GET", "/api/city?stateId={{state_id}}",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('cities');\n"
                  "var d = GL.json().data;\n"
                  "GL.set('city_id', GL.pick(d).id);")),
        req("Modules (gold loan)", "GET", "/api/modules/appraiser-request-module?isFor=request",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('modules');\n"
                  "var list = GL.json();\n"
                  "var m = list.filter(function (x) { return x.moduleName === 'gold loan'; })[0];\n"
                  "pm.test('gold loan module found', function () { pm.expect(!!m).to.be.true; });\n"
                  "GL.set('module_id', m.id);")),
        req("Status (confirm)", "GET", "/api/status",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('status');\n"
                  "var s = GL.json().data.filter(function (x) { return x.statusName === 'confirm'; })[0];\n"
                  "GL.set('status_id', s.id);\n"
                  "// Branch: existing-customer runs skip customer creation entirely.\n"
                  "if (String(GL.get('flow_mode', 'new')).toLowerCase() === 'existing') {\n"
                  "  GL.log('flow_mode=existing -> jumping to the existing-customer lookup');\n"
                  "  postman.setNextRequest('Find customer by unique id');\n"
                  "}")),
    ]
    return folder("01 - Master data", items,
                  "Reference data the customer/KYC bodies need. The last request branches to the "
                  "existing-customer folder when flow_mode=existing.")


# --------------------------------------------------------------------------------------------
# 02 — new customer
# --------------------------------------------------------------------------------------------
def folder_new_customer():
    items = [
        req("Send register OTP", "POST", "/api/customer/send-register-otp",
            pre=("GL.role('appraiser');\n"
                 "GL.set('customer_mobile', GL.mobile());\n"
                 "GL.set('first_name', GL.firstName());\n"
                 "GL.set('last_name', GL.lastName());\n"
                 "GL.set('pan_card_number', GL.pan());\n"
                 "GL.set('pin_code', GL.pincode());\n"
                 "GL.body({ mobileNumber: GL.get('customer_mobile') });"),
            test=("GL.ok('send-register-otp');\n"
                  "GL.set('reference_code', GL.json().referenceCode);"),
            json_body=True),
        req("Verify register OTP", "POST", "/api/customer/verify-otp",
            pre=("GL.role('appraiser');\n"
                 "GL.body({ otp: '1234', referenceCode: GL.get('reference_code'), type: 'lead' });"),
            test="GL.ok('customer verify-otp');",
            json_body=True),
        req("Create customer (ADMIN)", "POST", "/api/customer",
            pre=("// Creating the customer as the appraiser hits 'request already exists'; the\n"
                 "// harness creates it under ADMIN and then continues as the appraiser.\n"
                 "GL.role('admin');\n"
                 "GL.body({\n"
                 "  firstName: GL.get('first_name'), lastName: GL.get('last_name'),\n"
                 "  mobileNumber: GL.get('customer_mobile'), referenceCode: GL.get('reference_code'),\n"
                 "  panCardNumber: null, stateId: GL.get('state_id'), cityId: GL.get('city_id'),\n"
                 "  statusId: GL.get('status_id'), comment: null, pinCode: GL.get('pin_code'),\n"
                 "  source: null, panType: null, panImage: null,\n"
                 "  leadSource: GL.get('lead_source', 'Abhi testAppraiser'),\n"
                 "  moduleId: GL.get('module_id'), form60Image: null,\n"
                 "  email: (GL.get('first_name') + '.' + GL.get('last_name')).toLowerCase() + '@example.com'\n"
                 "});"),
            test=("GL.ok('create customer');\n"
                  "var b = GL.json();\n"
                  "var c = b.customer || (b.data && b.data.customer) || b.data || b;\n"
                  "GL.set('customer_id', c.id || c.customerId);\n"
                  "GL.set('customer_unique_id', c.customerUniqueId || c.uniqueId || '');\n"
                  "GL.role('appraiser');\n"
                  "GL.log('customer', GL.get('customer_id'), GL.get('customer_unique_id'));"),
            json_body=True),
        req("Resolve customer unique id",
            # viewAllCustomer=true: the customer was created under the ADMIN login, so it is not in
            # the appraiser's own list. The walk below still matches on the exact customer id.
            "GET", "/api/customer?viewAllCustomer=true&from=1&to=100&mobileNumber={{customer_mobile}}",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('customer lookup');\n"
                  "if (!GL.get('customer_unique_id')) {\n"
                  "  var want = String(GL.get('customer_id'));\n"
                  "  var found = null;\n"
                  "  (function walk(o) {\n"
                  "    if (found || !o || typeof o !== 'object') { return; }\n"
                  "    if (!Array.isArray(o) && String(o.id || o.customerId) === want &&\n"
                  "        (o.customerUniqueId || o.uniqueId)) { found = o; return; }\n"
                  "    var vals = Array.isArray(o) ? o : Object.keys(o).map(function (k) { return o[k]; });\n"
                  "    vals.forEach(walk);\n"
                  "  }(GL.json()));\n"
                  "  pm.test('unique id resolved', function () { pm.expect(!!found).to.be.true; });\n"
                  "  GL.set('customer_unique_id', found.customerUniqueId || found.uniqueId);\n"
                  "}\n"
                  "postman.setNextRequest('KYC: get customer');")),
    ]
    return folder("02 - New customer", items,
                  "Only runs when flow_mode=new. Registers a fresh customer (OTP-verified) under "
                  "the admin login.")


# --------------------------------------------------------------------------------------------
# 02b — existing customer
# --------------------------------------------------------------------------------------------
def folder_existing_customer():
    items = [
        req("Find customer by unique id",
            # viewAllCustomer=true — a customer from an earlier run belongs to whoever created it
            # (usually admin), so the appraiser's own list would not contain it.
            "GET", "/api/customer?viewAllCustomer=true&from=1&to=100&customerUniqueId={{customer_unique_id}}",
            pre=("GL.role('appraiser');\n"
                 "if (!GL.get('customer_unique_id')) {\n"
                 "  throw new Error('flow_mode=existing requires the customer_unique_id variable.');\n"
                 "}\n"
                 "GL.sign();"),
            test=("GL.ok('existing customer lookup');\n"
                  "var want = String(GL.get('customer_unique_id'));\n"
                  "var found = null;\n"
                  "(function walk(o) {\n"
                  "  if (found || !o || typeof o !== 'object') { return; }\n"
                  "  if (!Array.isArray(o) && String(o.customerUniqueId || o.uniqueId) === want && o.id) {\n"
                  "    found = o; return;\n"
                  "  }\n"
                  "  var vals = Array.isArray(o) ? o : Object.keys(o).map(function (k) { return o[k]; });\n"
                  "  vals.forEach(walk);\n"
                  "}(GL.json()));\n"
                  "pm.test('customer found', function () { pm.expect(!!found).to.be.true; });\n"
                  "GL.set('customer_id', found.id);")),
    ]
    return folder("02b - Existing customer", items,
                  "Only runs when flow_mode=existing. Resolves the numeric customer id from the "
                  "customer unique id; KYC is then skipped if the record is already approved.")


# --------------------------------------------------------------------------------------------
# 03 — KYC
# --------------------------------------------------------------------------------------------
def folder_kyc():
    upload = lambda name, path, asset, extra=None, capture="": req(  # noqa: E731
        name, "POST", path,
        pre="GL.role('appraiser'); GL.sign();",
        test=("GL.ok('%s');\n" % name) + capture,
        form=[("avatar", "file", asset)],
        extra_headers=extra)

    items = [
        req("KYC: get customer", "GET", "/api/customer/{{customer_id}}",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('get customer');\n"
                  "var c = GL.json().singleCustomer || {};\n"
                  "GL.set('kyc_status', String(c.kycStatus || ''));\n"
                  "if (c.customerUniqueId) { GL.set('customer_unique_id', c.customerUniqueId); }\n"
                  "if (c.mobileNumber) { GL.set('customer_mobile', c.mobileNumber); }\n"
                  "if (c.firstName) { GL.set('first_name', c.firstName); }\n"
                  "if (c.lastName) { GL.set('last_name', c.lastName); }\n"
                  "if (c.moduleId) { GL.set('module_id', c.moduleId); }\n"
                  "if (c.stateId) { GL.set('state_id', c.stateId); }\n"
                  "if (c.cityId) { GL.set('city_id', c.cityId); }\n"
                  "if (c.pinCode) { GL.set('pin_code', c.pinCode); }\n"
                  "if (c.panType) { GL.set('pan_type', c.panType); }\n"
                  "if (c.panCardNumber) { GL.set('pan_card_number', c.panCardNumber); }\n"
                  "if (c.panImage) { GL.set('pan_image', c.panImage); }\n"
                  "if (c.gender) { GL.set('gender', c.gender); }\n"
                  "if (c.dateOfBirth) { GL.set('dob', String(c.dateOfBirth).substring(0, 10)); }\n"
                  "if (c.age) { GL.set('age', c.age); }\n"
                  "if (c.motherName) { GL.set('mother_name', c.motherName); }\n"
                  "GL.log('kycStatus =', GL.get('kyc_status'));")),
        req("KYC: get customer detail (v2)", "POST", "/api/kyc/v2/get-customer-detail",
            pre=("GL.role('appraiser');\n"
                 "GL.body({ customerId: String(GL.get('customer_id')), moduleId: String(GL.get('module_id')),\n"
                 "          kycType: GL.get('kyc_type', 'RE_KYC') });"),
            test=("GL.soft('kyc get-customer-detail');\n"
                  "var info = (GL.json() || {}).customerInfo || {};\n"
                  "var p = info.customerKycPersonal || {};\n"
                  "if (p.id) { GL.set('customer_kyc_id', p.id); }\n"
                  "if (p.profileImage) { GL.set('profile_image', p.profileImage); }\n"
                  "if (p.signatureProof) { GL.set('signature_proof', p.signatureProof); }\n"
                  "if (p.martialStatus) { GL.set('martial_status', p.martialStatus); }\n"
                  "if (p.spouseName) { GL.set('spouse_name', p.spouseName); }\n"
                  "// Approved KYC on an existing customer -> skip the whole KYC folder.\n"
                  "if (String(GL.get('flow_mode', 'new')).toLowerCase() === 'existing' &&\n"
                  "    String(GL.get('kyc_status')).toLowerCase() === 'approved') {\n"
                  "  GL.log('KYC already approved -> skipping to the loan process');\n"
                  "  postman.setNextRequest('EX: upload signature');\n"
                  "}"),
            json_body=True),
        upload("KYC: upload PAN image", "/api/upload-file?reason=lead&customerId={{customer_id}}",
               "assets/PAN.png",
               capture="GL.set('pan_image', GL.json().uploadFile.path);"),
        req("KYC: submit basic info (v2)", "POST", "/api/kyc/v2/submit-basic-info",
            pre=("GL.role('appraiser');\n"
                 "if (!GL.get('pan_card_number')) { GL.set('pan_card_number', GL.pan()); }\n"
                 "if (!GL.get('pan_type')) { GL.set('pan_type', 'pan'); }\n"
                 "var img = GL.get('pan_image') || null;\n"
                 "GL.body({\n"
                 "  id: GL.int(GL.get('customer_id')), moduleId: String(GL.get('module_id')),\n"
                 "  firstName: GL.get('first_name'), lastName: GL.get('last_name'),\n"
                 "  panType: GL.get('pan_type'), panCardNumber: GL.get('pan_card_number'),\n"
                 "  panImage: img, panImg: img ? GL.fullUrl(img) : null,\n"
                 "  ovdImage: null, ovdImg: null, ovdNumber: null, ovdType: null, dateOfBirth: null,\n"
                 "  isAutoApproved: false, isPanVerified: false, isOvdVerified: false,\n"
                 "  kycType: GL.get('kyc_type', 'RE_KYC')\n"
                 "});"),
            test=("GL.ok('submit-basic-info');\n"
                  "var d = GL.json().data || {};\n"
                  "if (d.customerKycId) { GL.set('customer_kyc_id', d.customerKycId); }\n"
                  "if (d.customerId) { GL.set('customer_id', d.customerId); }"),
            json_body=True),
        req("KYC: consent OTP send", "POST", "/api/customer-otp/send-otp",
            pre=("GL.role('appraiser');\n"
                 "GL.body({ type: 'kycConsent', customerId: GL.int(GL.get('customer_id')) });"),
            test=("GL.soft('kyc consent send-otp');\n"
                  "GL.set('kyc_reference_code', (GL.json() || {}).referenceCode || '');"),
            json_body=True),
        req("KYC: consent OTP verify", "POST", "/api/customer-otp/verify-otp-admin",
            pre=("GL.role('appraiser');\n"
                 "GL.body({ type: 'kycConsent', referenceCode: GL.get('kyc_reference_code'), otp: '123456' });"),
            test="GL.soft('kyc consent verify-otp');",
            json_body=True),
    ]

    # KYC master data
    masters = [
        ("Occupations", "/api/occupation/list", "occupation_id",
         "var low = d.filter(function (x) { return String(x.riskCategory || '').toLowerCase() === 'low'; });\n"
         "GL.set('occupation_id', GL.pick(low.length ? low : d).id);"),
        ("Religions", "/api/religion", "religion_id", "GL.set('religion_id', GL.pick(d).id);"),
        ("Physical challenges", "/api/physical-challenge", "physical_challenge_id",
         "GL.set('physical_challenge_id', GL.pick(d).id);"),
        ("Political exposed", "/api/political-exposed", "political_exposed_id",
         "var na = d.filter(function (x) { return String(x.riskCategory || '').toUpperCase() === 'NA'; });\n"
         "GL.set('political_exposed_id', (na[0] || d[0]).id);"),
        ("Special categories", "/api/special-category", "special_category_id",
         "GL.set('special_category_id', GL.pick(d).id);"),
        ("CIS", "/api/cis", "cis_id", "GL.set('cis_id', GL.pick(d).id);"),
        ("BSR", "/api/bsr", "bsr_id", "GL.set('bsr_id', GL.pick(d).id);"),
        ("Annual incomes", "/api/annual-income", "annual_income",
         "GL.set('annual_income', GL.pick(d).incomeRange);"),
        ("Qualifications", "/api/qualification", "qualification_id",
         "GL.set('qualification_id', GL.pick(d).id);"),
    ]
    for label, path, _var, capture in masters:
        items.append(req("KYC master: " + label, "GET", path,
                         pre="GL.role('appraiser'); GL.sign();",
                         test="GL.ok('%s');\nvar d = GL.json().data;\n%s" % (label, capture)))

    items += [
        upload("KYC: upload profile image", "/api/upload-file?reason=customer&customerId={{customer_id}}",
               "assets/dummy_image.png",
               capture="GL.set('profile_image', GL.json().uploadFile.path);"),
        upload("KYC: upload signature", "/api/upload-file?reason=customer&customerId={{customer_id}}",
               "assets/dummy_image.png",
               capture=("GL.set('signature_proof', GL.json().uploadFile.path);\n"
                        "GL.set('signature_file_name', GL.json().uploadFile.originalname);")),
        req("KYC: personal details (v2)", "POST", "/api/kyc/v2/customer-kyc-personal",
            pre=("GL.role('appraiser');\n"
                 "var da = GL.dobAge();\n"
                 "GL.set('dob', da.dob); GL.set('age', da.age);\n"
                 "GL.set('gender', GL.pick(['m', 'f', 'o']));\n"
                 "GL.set('martial_status', GL.pick(['single', 'married', 'divorced']));\n"
                 "GL.set('spouse_name', GL.fullName());\n"
                 "GL.set('mother_name', GL.fullName());\n"
                 "var prof = GL.get('profile_image'), sig = GL.get('signature_proof');\n"
                 "GL.body({\n"
                 "  customerId: GL.get('customer_id'), customerKycId: GL.get('customer_kyc_id'),\n"
                 "  profileImage: prof, profileImg: prof ? GL.fullUrl(prof) : null,\n"
                 "  alternateMobileNumber: '', gender: GL.get('gender'),\n"
                 "  spouseName: GL.get('spouse_name'), martialStatus: GL.get('martial_status'),\n"
                 "  signatureProof: sig, signatureProofImg: sig ? GL.fullUrl(sig) : null,\n"
                 "  signatureProofFileName: GL.get('signature_file_name') || null,\n"
                 "  occupationId: GL.int(GL.get('occupation_id')), dateOfBirth: GL.get('dob'),\n"
                 "  age: String(GL.get('age')), moduleId: GL.get('module_id'), userType: null,\n"
                 "  email: null, alternateEmail: null, landLineNumber: null, gstinNumber: null,\n"
                 "  cinNumber: null, constitutionsDeed: [], constitutionsDeedFileName: null,\n"
                 "  gstCertificate: [], gstCertificateFileName: null,\n"
                 "  annualIncome: GL.get('annual_income'), motherName: GL.get('mother_name'),\n"
                 "  religionId: GL.int(GL.get('religion_id')),\n"
                 "  qualificationId: GL.int(GL.get('qualification_id')),\n"
                 "  physicalChallengeId: GL.int(GL.get('physical_challenge_id')),\n"
                 "  politicalExposedId: GL.int(GL.get('political_exposed_id')),\n"
                 "  specialCategoryId: GL.int(GL.get('special_category_id')),\n"
                 "  kycType: GL.get('kyc_type', 'RE_KYC')\n"
                 "});"),
            test=("GL.ok('customer-kyc-personal');\n"
                  "var r = (GL.json() || {}).customerKycReview || {};\n"
                  "var p = r.customerKycPersonal || {};\n"
                  "if (p.id) { GL.set('customer_kyc_personal_id', p.id); }\n"
                  "GL.setJson('kyc_personal', JSON.parse(pm.collectionVariables.get('__body')));"),
            json_body=True),
        req("KYC: address proof types", "GET", "/api/address-proof-type",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('address-proof-type');\n"
                  "GL.set('address_proof_type_id', GL.pick(GL.json().data).id);")),
        upload("KYC: upload masked aadhaar",
               "/api/upload-file?reason=customer&customerId={{customer_id}}&documentType=aadhar",
               "assets/AADHAR.png",
               extra=[("isMask", "true"), ("documentType", "aadhar")],
               capture=("var r = GL.json();\n"
                        "var masked = (r.maskedData || {}).path || r.uploadFile.path;\n"
                        "GL.set('masked_identity_proof', masked);\n"
                        "GL.set('unmasked_identity_proof', r.uploadFile.path);")),
        upload("KYC: upload address proof", "/api/upload-file?reason=customer&customerId={{customer_id}}",
               "assets/CPV.pdf",
               capture="GL.set('address_proof', GL.json().uploadFile.path);"),
        req("KYC: geo-location", "POST", "/api/geo-location/lat-long-by-address",
            pre=("GL.role('appraiser');\n"
                 "GL.set('identity_proof_number', GL.aadhaar());\n"
                 "GL.set('name_as_per_aadhaar', GL.get('first_name') + ' ' + GL.get('last_name'));\n"
                 "var perm = GL.address();\n"
                 "var same = Math.random() < 0.5;\n"
                 "var res = same ? perm : GL.address();\n"
                 "var mk = function (type, a, pin) {\n"
                 "  return { addressType: type,\n"
                 "           addressProofTypeId: GL.int(GL.get('address_proof_type_id')),\n"
                 "           addressProofNumber: GL.get('identity_proof_number'),\n"
                 "           address: a.address, stateId: GL.int(GL.get('state_id')),\n"
                 "           cityId: GL.int(GL.get('city_id')), pinCode: pin, landmark: a.landmark,\n"
                 "           addressProof: [GL.get('address_proof')],\n"
                 "           unMaskedAddressProof: [GL.get('unmasked_identity_proof')] };\n"
                 "};\n"
                 "var addrs = [mk('permanent', perm, GL.get('pin_code'))];\n"
                 "addrs.push(mk('residential', res, same ? GL.get('pin_code') : GL.pincode()));\n"
                 "GL.setJson('kyc_addresses', addrs);\n"
                 "GL.body({ address: perm.address });"),
            test=("GL.soft('geo-location');\n"
                  "var d = (GL.json() || {}).data || {};\n"
                  "GL.set('latitude', d.lat === undefined ? '' : d.lat);\n"
                  "GL.set('longitude', d.lng === undefined ? '' : d.lng);"),
            json_body=True),
        req("KYC: customer-kyc-address (v2)", "POST", "/api/kyc/v2/customer-kyc-address",
            pre=("GL.role('appraiser');\n"
                 "var addrs = GL.getJson('kyc_addresses', []);\n"
                 "var ap = GL.get('address_proof');\n"
                 "var masked = GL.get('masked_identity_proof'), unmasked = GL.get('unmasked_identity_proof');\n"
                 "var lat = GL.get('latitude'), lng = GL.get('longitude');\n"
                 "var v2 = addrs.map(function (a) {\n"
                 "  return {\n"
                 "    addressType: a.addressType,\n"
                 "    addressProofTypeId: String(a.addressProofTypeId || ''),\n"
                 "    addressProofNumber: GL.enc(GL.get('identity_proof_number')),\n"
                 "    address: a.address, stateId: a.stateId, cityId: a.cityId,\n"
                 "    pinCode: a.pinCode, landmark: a.landmark,\n"
                 "    addressProof: (a.addressProof || []).map(function (p) { return GL.enc(p); }),\n"
                 "    unMaskedAddressProof: [],\n"
                 "    addressProofImg: (a.addressProof || []).map(function (p) { return GL.fullUrl(p); }),\n"
                 "    addressProofFileName: ap ? [GL.enc(ap)] : []\n"
                 "  };\n"
                 "});\n"
                 "GL.body({\n"
                 "  customerId: GL.int(GL.get('customer_id')), customerKycId: GL.int(GL.get('customer_kyc_id')),\n"
                 "  identityTypeId: 5,\n"
                 "  identityProof: masked ? [GL.enc(masked)] : [],\n"
                 "  unMaskedIdentityProof: unmasked ? [GL.enc(unmasked)] : [],\n"
                 "  identityProofImg: masked ? [GL.enc(GL.fullUrl(masked))] : [],\n"
                 "  identityProofFileName: masked ? [GL.enc(masked)] : [],\n"
                 "  identityProofNumber: GL.enc(GL.get('identity_proof_number')),\n"
                 "  isAutoApproved: false, nameAsPerAadhaar: GL.get('name_as_per_aadhaar'),\n"
                 "  file: null, xmlFileName: null, address: v2,\n"
                 "  latitude: lat === '' ? null : lat, longitude: lng === '' ? null : lng,\n"
                 "  isCityEdit: false, isAahaarVerified: false, kycType: GL.get('kyc_type', 'RE_KYC')\n"
                 "});"),
            test=("GL.soft('customer-kyc-address v2');\n"
                  "// Non-fatal by design: submit-all-kyc-info is the authoritative submission."),
            json_body=True,
            description="Non-fatal. This endpoint auto-verifies aadhaar and can reject test "
                        "numbers; submit-all-kyc-info is the manual path the flow relies on."),
        req("KYC: submit all KYC info", "POST", "/api/kyc/submit-all-kyc-info",
            pre=("GL.role('appraiser');\n"
                 "var personal = GL.getJson('kyc_personal', {});\n"
                 "['customerId', 'customerKycId', 'moduleId', 'userType', 'profileImg',\n"
                 " 'signatureProofImg'].forEach(function (k) { delete personal[k]; });\n"
                 "var masked = GL.get('masked_identity_proof'), unmasked = GL.get('unmasked_identity_proof');\n"
                 "// In customerKycPersonal the identity-proof PATHS are encrypted too (plaintext\n"
                 "// paths -> 'Invalid unMasked Identity Proof file'); the basic-details block stays plain.\n"
                 "personal.identityTypeId = 5;\n"
                 "personal.identityProof = masked ? [GL.enc(masked)] : [];\n"
                 "personal.unMaskedIdentityProof = unmasked ? [GL.enc(unmasked)] : [];\n"
                 "personal.identityProofNumber = GL.enc(GL.get('identity_proof_number'));\n"
                 "personal.panCardNumber = GL.get('pan_card_number');\n"
                 "personal.nameAsPerAadhaar = GL.get('name_as_per_aadhaar');\n"
                 "personal.riskCategory = 'low';\n"
                 "if (GL.get('cis_id')) { personal.cisId = GL.int(GL.get('cis_id')); }\n"
                 "if (GL.get('bsr_id')) { personal.bsrId = GL.int(GL.get('bsr_id')); }\n"
                 "if (personal.dateOfBirth && String(personal.dateOfBirth).indexOf('T') < 0) {\n"
                 "  personal.dateOfBirth = personal.dateOfBirth + 'T00:00:00.000Z';\n"
                 "}\n"
                 "var addrs = GL.getJson('kyc_addresses', []).map(function (a) {\n"
                 "  return {\n"
                 "    customerKycId: GL.int(GL.get('customer_kyc_id')),\n"
                 "    customerId: GL.int(GL.get('customer_id')),\n"
                 "    addressType: a.addressType, address: a.address,\n"
                 "    stateId: a.stateId, cityId: a.cityId, pinCode: a.pinCode,\n"
                 "    addressProof: a.addressProof || [],\n"
                 "    unMaskedAddressProof: a.unMaskedAddressProof || [],\n"
                 "    addressProofFileName: a.addressProofFileName === undefined ? null : a.addressProofFileName,\n"
                 "    addressProofTypeId: a.addressProofTypeId,\n"
                 "    addressProofNumber: GL.get('identity_proof_number'),\n"
                 "    landmark: a.landmark\n"
                 "  };\n"
                 "});\n"
                 "var panImg = GL.get('pan_image') || null;\n"
                 "var lat = GL.get('latitude'), lng = GL.get('longitude');\n"
                 "GL.body({\n"
                 "  customerId: GL.int(GL.get('customer_id')),\n"
                 "  customerKycId: GL.int(GL.get('customer_kyc_id')),\n"
                 "  customerKycPersonal: personal,\n"
                 "  customerKycAddress: addrs,\n"
                 "  customerKycBasicDetails: {\n"
                 "    id: GL.int(GL.get('customer_kyc_personal_id') || GL.get('customer_kyc_id')),\n"
                 "    profileImage: GL.get('profile_image') || null,\n"
                 "    firstName: GL.get('first_name'), lastName: GL.get('last_name'),\n"
                 "    mobileNumber: GL.get('customer_mobile'), panCardNumber: GL.get('pan_card_number'),\n"
                 "    panType: GL.get('pan_type'), form60: null, panImage: panImg,\n"
                 "    panImg: panImg ? GL.fullUrl(panImg) : null, identityTypeId: 5,\n"
                 "    identityProof: [masked], unMaskedIdentityProof: [unmasked],\n"
                 "    identityProofFileName: null,\n"
                 "    identityProofNumber: GL.get('identity_proof_number'),\n"
                 "    userType: null, organizationTypeId: null, dateOfIncorporation: null,\n"
                 "    form60Image: null, form60Img: null, isCityEdit: null, geoAddress: '',\n"
                 "    nameAsPerAadhaar: GL.get('name_as_per_aadhaar'), riskCategory: 'low'\n"
                 "  },\n"
                 "  moduleId: GL.int(GL.get('module_id')), userType: null,\n"
                 "  customerOrganizationDetail: null, isCityEdit: null,\n"
                 "  latitude: lat === '' ? null : lat, longitude: lng === '' ? null : lng\n"
                 "});"),
            test="GL.ok('submit-all-kyc-info');",
            json_body=True),
        req("KYC: ops approval", "POST", "/api/classification/ops-team",
            pre=("GL.role('appraiser');\n"
                 "GL.body({\n"
                 "  customerId: GL.int(GL.get('customer_id')),\n"
                 "  customerKycId: GL.int(GL.get('customer_kyc_id')),\n"
                 "  kycRatingFromBM: false, kycStatusFromOperationalTeam: 'approved',\n"
                 "  reasonFromOperationalTeam: '', moduleId: GL.int(GL.get('module_id')),\n"
                 "  userType: 'Individual'\n"
                 "});"),
            test=("GL.ok('kyc ops approval');\n"
                  "postman.setNextRequest('LOAN: appraiser list');"),
            json_body=True),
    ]
    return folder("03 - KYC (v2)", items,
                  "Full KYC: basic info -> consent OTP -> master data -> personal -> address/identity "
                  "-> submit-all-kyc-info -> ops approval. Skipped for an already-approved customer.")


# --------------------------------------------------------------------------------------------
# 03b — existing-customer loan prep (uploads KYC artefacts the loan step needs)
# --------------------------------------------------------------------------------------------
def folder_existing_prep():
    items = [
        req("EX: upload signature", "POST",
            "/api/upload-file?reason=customer&customerId={{customer_id}}",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('signature upload');\n"
                  "GL.set('signature_proof', GL.json().uploadFile.path);"),
            form=[("avatar", "file", "assets/dummy_image.png")]),
        req("EX: upload masked aadhaar", "POST",
            "/api/upload-file?reason=customer&customerId={{customer_id}}&documentType=aadhar",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('aadhaar upload');\n"
                  "var r = GL.json();\n"
                  "GL.set('masked_identity_proof', (r.maskedData || {}).path || r.uploadFile.path);\n"
                  "GL.set('unmasked_identity_proof', r.uploadFile.path);"),
            form=[("avatar", "file", "assets/AADHAR.png")],
            extra_headers=[("isMask", "true"), ("documentType", "aadhar")],
            description="loan-documents needs an identity proof; an existing customer skips KYC, "
                        "so upload one here (same call KYC uses)."),
    ]
    # The existing-customer path still needs the KYC master-data ids for basic-details.
    masters = [
        ("Occupations", "/api/occupation/list",
         "var low = d.filter(function (x) { return String(x.riskCategory || '').toLowerCase() === 'low'; });\n"
         "GL.set('occupation_id', GL.pick(low.length ? low : d).id);"),
        ("Religions", "/api/religion", "GL.set('religion_id', GL.pick(d).id);"),
        ("Physical challenges", "/api/physical-challenge", "GL.set('physical_challenge_id', GL.pick(d).id);"),
        ("Political exposed", "/api/political-exposed",
         "var na = d.filter(function (x) { return String(x.riskCategory || '').toUpperCase() === 'NA'; });\n"
         "GL.set('political_exposed_id', (na[0] || d[0]).id);"),
        ("Special categories", "/api/special-category", "GL.set('special_category_id', GL.pick(d).id);"),
        ("Annual incomes", "/api/annual-income", "GL.set('annual_income', GL.pick(d).incomeRange);"),
        ("Qualifications", "/api/qualification", "GL.set('qualification_id', GL.pick(d).id);"),
    ]
    for label, path, capture in masters:
        items.append(req("EX master: " + label, "GET", path,
                         pre="GL.role('appraiser'); GL.sign();",
                         test="GL.ok('%s');\nvar d = GL.json().data;\n%s" % (label, capture)))
    items[-1]["event"][-1]["script"]["exec"] += [
        "// Fill anything the stored record did not supply.",
        "if (!GL.get('gender')) { GL.set('gender', GL.pick(['m', 'f', 'o'])); }",
        "if (!GL.get('mother_name')) { GL.set('mother_name', GL.fullName()); }",
        "if (!GL.get('dob') || !GL.get('age')) {",
        "  var da = GL.dobAge(); GL.set('dob', da.dob); GL.set('age', da.age);",
        "}",
    ]
    return folder("03b - Existing customer prep", items,
                  "Only runs for an already-approved customer: uploads the signature + identity "
                  "proof the loan step needs and loads the KYC master-data ids.")


# --------------------------------------------------------------------------------------------
# 04 — appraiser request & loan basics
# --------------------------------------------------------------------------------------------
def folder_loan_basics():
    items = [
        req("LOAN: appraiser list", "GET",
            "/api/user/appraiser-list?internalBranchId={{internal_branch_id}}",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('appraiser-list');\n"
                  "var d = GL.json().data || [];\n"
                  "var me = String(GL.get('logged_in_user_id'));\n"
                  "var a = d.filter(function (x) { return String(x.id) === me; })[0];\n"
                  "if (!a) {\n"
                  "  a = d.filter(function (x) {\n"
                  "    return String(x.mobileNumber || x.mobile) === String(GL.get('mobile_appraiser'));\n"
                  "  })[0];\n"
                  "}\n"
                  "a = a || GL.pick(d);\n"
                  "GL.set('appraiser_id', a.id);\n"
                  "GL.set('appraiser_name', ((a.firstName || '') + ' ' + (a.lastName || '')).trim());\n"
                  "GL.set('appraiser_mobile', a.mobileNumber || a.mobile || GL.get('mobile_appraiser'));")),
        req("LOAN: create appraiser request", "POST", "/api/appraiser-request",
            pre=("GL.role('appraiser');\n"
                 "GL.body({\n"
                 "  id: null, customerId: GL.int(GL.get('customer_id')),\n"
                 "  customerName: GL.get('first_name') + ' ' + GL.get('last_name'),\n"
                 "  customerUniqueId: GL.get('customer_unique_id'),\n"
                 "  mobileNumber: GL.get('customer_mobile'),\n"
                 "  moduleId: GL.int(GL.get('module_id')),\n"
                 "  internalBranchId: GL.int(GL.get('internal_branch_id')),\n"
                 "  appraiserId: GL.int(GL.get('appraiser_id')),\n"
                 "  loanType: 'Fresh Loan', trackProccesingTime: true\n"
                 "});"),
            test=("// 400 'already Exists' is tolerated: the id is resolved from view-all next.\n"
                  "GL.soft('appraiser-request');\n"
                  "var d = (GL.json() || {}).data || {};\n"
                  "if (d.id) { GL.set('appraiser_request_id', d.id); }"),
            json_body=True),
        req("LOAN: resolve appraiser request", "GET",
            "/api/appraiser-request/view-all?from=1&to=25&search={{customer_unique_id}}",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('appraiser-request view-all');\n"
                  "var items = GL.json().data || [];\n"
                  "var uid = String(GL.get('customer_unique_id'));\n"
                  "var cid = String(GL.get('customer_id'));\n"
                  "// Match the item's OWN customer, then take the ITEM-level id (not customer.id).\n"
                  "var m = items.filter(function (it) {\n"
                  "  var c = it.customer || {};\n"
                  "  return String(c.customerUniqueId || it.customerUniqueId || '') === uid ||\n"
                  "         String(it.customerId || c.id || '') === cid;\n"
                  "})[0];\n"
                  "pm.test('appraiser request found', function () { pm.expect(!!(m && m.id)).to.be.true; });\n"
                  "GL.set('appraiser_request_id', m.id);\n"
                  "GL.log('appraiserRequestId', m.id);")),
        req("LOAN: track loan history", "POST", "/api/appraiser-request/track-loan-history",
            pre=("GL.role('appraiser');\n"
                 "GL.body({ action: 3, appraiserRequestId: GL.int(GL.get('appraiser_request_id')) });"),
            test="GL.soft('track-loan-history');",
            json_body=True),
        req("LOAN: max loan limit", "GET",
            "/api/loan-process/max-loan-limit/{{customer_id}}?loanType=Fresh%20Loan",
            pre="GL.role('appraiser'); GL.sign();",
            test="GL.soft('max-loan-limit');"),
        req("LOAN: customer loan details", "GET",
            "/api/loan-process/customer-loan-details/{{appraiser_request_id}}",
            pre="GL.role('appraiser'); GL.sign();",
            test="GL.soft('customer-loan-details');"),
        req("LOAN: purposes", "GET", "/api/purpose?search=&from=1&to=-1",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('purposes');\n"
                  "var d = GL.json().data;\n"
                  "if (!Array.isArray(d)) {\n"
                  "  var lists = Object.keys(d).map(function (k) { return d[k]; })\n"
                  "                  .filter(function (v) { return Array.isArray(v); });\n"
                  "  d = lists[0] || [];\n"
                  "}\n"
                  "GL.set('purpose', GL.pick(d).name);")),
        req("LOAN: basic details", "POST", "/api/loan-process/basic-details",
            pre=("GL.role('appraiser');\n"
                 "if (!GL.get('age') || !GL.get('dob')) {\n"
                 "  var da = GL.dobAge(); GL.set('dob', da.dob); GL.set('age', da.age);\n"
                 "}\n"
                 "var sig = GL.get('signature_proof');\n"
                 "GL.body({\n"
                 "  customerUniqueId: GL.get('customer_unique_id'),\n"
                 "  mobileNumber: GL.get('customer_mobile'),\n"
                 "  panCardNumber: GL.get('pan_card_number'),\n"
                 "  startDate: GL.nowIsoZ(), customerId: GL.int(GL.get('customer_id')),\n"
                 "  kycStatus: 'approved', purpose: GL.get('purpose'), panType: GL.get('pan_type'),\n"
                 "  loanId: null, scrapId: null, masterLoanId: null,\n"
                 "  panImg: GL.get('pan_image') || null, partReleaseId: null,\n"
                 "  requestId: GL.int(GL.get('appraiser_request_id')),\n"
                 "  customerName: GL.get('first_name') + ' ' + GL.get('last_name'),\n"
                 "  form60Img: null, age: String(GL.get('age')), branchName: 'Augmont',\n"
                 "  branchDistance: null, gender: GL.get('gender'),\n"
                 "  appraiserName: GL.get('appraiser_name'), tillDateOutstanding: '3122296.67',\n"
                 "  appraiserMobileNumber: GL.get('appraiser_mobile'), currentActiveLoans: 2,\n"
                 "  cpvImageFullUrl: GL.fullUrl('assets/CPV.pdf'),\n"
                 "  annualIncome: GL.get('annual_income'), motherName: GL.get('mother_name'),\n"
                 "  religionId: GL.int(GL.get('religion_id')),\n"
                 "  physicalChallengeId: GL.int(GL.get('physical_challenge_id')),\n"
                 "  occupationId: GL.int(GL.get('occupation_id')),\n"
                 "  signatureProof: sig, signatureProofImg: GL.fullUrl(sig),\n"
                 "  signatureProofFileName: GL.baseName(sig),\n"
                 "  referenceCustomerNumber: null,\n"
                 "  qualificationId: GL.int(GL.get('qualification_id')),\n"
                 "  politicalExposedId: GL.int(GL.get('political_exposed_id')),\n"
                 "  specialCategoryId: GL.int(GL.get('special_category_id')),\n"
                 "  incomeGeneratingDocuments: null, purposeType: 'Consumption Based',\n"
                 "  checkPointers: {}\n"
                 "});"),
            test=("GL.ok('loan basic-details');\n"
                  "var r = GL.json();\n"
                  "GL.set('loan_id', r.loanId);\n"
                  "GL.set('master_loan_id', r.masterLoanId);\n"
                  "GL.log('loanId', r.loanId, 'masterLoanId', r.masterLoanId);"),
            json_body=True),
        req("LOAN: rating reasons", "GET", "/api/rating-reason?from=1&to=-1",
            pre="GL.role('appraiser'); GL.sign();", test="GL.soft('rating-reason');"),
        req("LOAN: nominee relations", "GET", "/api/nominee-relation/list",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('nominee-relation');\n"
                  "// The server matches relationship against these (lowercase) values.\n"
                  "var names = (GL.json().data || []).map(function (r) {\n"
                  "  return String(r.relationshipName || r.name || '').trim();\n"
                  "}).filter(Boolean);\n"
                  "var adult = ['brother', 'sister', 'father', 'mother', 'spouse', 'son', 'daughter',\n"
                  "             'wife', 'husband'];\n"
                  "var pref = names.filter(function (n) { return adult.indexOf(n.toLowerCase()) >= 0; })[0];\n"
                  "GL.set('nominee_relation', pref || names[0]);")),
        req("LOAN: nominee details", "POST", "/api/loan-process/nominee-details",
            pre=("GL.role('appraiser');\n"
                 "var age = GL.ri(20, 60);\n"
                 "var rel = GL.get('nominee_relation');\n"
                 "var b = {\n"
                 "  nomineeName: GL.fullName(), nomineeAge: age, relationship: rel,\n"
                 "  mobileNumber: GL.mobile(), referenceCode: null,\n"
                 "  nomineeType: age >= 18 ? 'major' : 'minor',\n"
                 "  guardianName: '', guardianAge: 30, guardianRelationship: rel,\n"
                 "  checkPointers: {},\n"
                 "  loanId: GL.int(GL.get('loan_id')), masterLoanId: GL.int(GL.get('master_loan_id')),\n"
                 "  loanType: 'Fresh Loan', createdBy: String(GL.get('logged_in_user_id')),\n"
                 "  internalBranchId: GL.int(GL.get('internal_branch_id'))\n"
                 "};\n"
                 "GL.body(b);"),
            test="GL.soft('nominee-details');",
            json_body=True),
    ]
    return folder("04 - Appraiser request & loan basics", items)


# --------------------------------------------------------------------------------------------
# 05 — ornaments
# --------------------------------------------------------------------------------------------
def folder_ornaments():
    items = [
        req("ORN: gold rate", "GET", "/api/gold-rate",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.soft('gold-rate');\n"
                  "var r = GL.findKey(GL.json(), 'goldRate');\n"
                  "GL.set('gold_rate', r ? GL.num(r) : 6000);")),
        req("ORN: karat details", "GET", "/api/karat-details",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('karat-details');\n"
                  "var d = GL.json().data || [];\n"
                  "var usable = d.filter(function (k) { return GL.karatValue(k.karat) > 18; });\n"
                  "// ONE karat for every ornament in the run: schemes have a [minKarat,maxKarat]\n"
                  "// range and the server only counts karat-eligible ornaments in its eligibility\n"
                  "// calc, so mixing karats guarantees a totalEligibleAmt mismatch. 22K is the most\n"
                  "// broadly covered.\n"
                  "var pref = usable.filter(function (k) { return GL.karatValue(k.karat) === 22; });\n"
                  "var chosen = GL.pick(pref.length ? pref : (usable.length ? usable : d));\n"
                  "GL.setJson('karat_detail', chosen);\n"
                  "GL.set('karat', chosen.karat);")),
        req("ORN: ornament types", "GET", "/api/ornament-type?from=1&to=-1&search=",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('ornament-type');\n"
                  "GL.setJson('ornament_types', (GL.json().data || []).slice(0, 20));")),
        req("ORN: upload ornament image", "POST",
            "/api/upload-file?reason=loan&customerId={{customer_id}}",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('ornament image');\n"
                  "GL.set('ornament_image', GL.json().uploadFile.path);"),
            form=[("avatar", "file", "assets/scale.jpg")],
            description="The harness uploads one image per ornament; the collection uploads once "
                        "and reuses the path for all four (same file, same result)."),
        req("ORN: store ornament details", "POST", "/api/loan-process/ornaments-details",
            pre=("GL.role('appraiser');\n"
                 "var target = GL.num(GL.get('loan_amount'), 400000);\n"
                 "var rate = GL.num(GL.get('gold_rate'), 6000);\n"
                 "var kd = GL.getJson('karat_detail', {});\n"
                 "var types = GL.getJson('ornament_types', []);\n"
                 "var img = GL.get('ornament_image');\n"
                 "var N = 4;\n"
                 "// Size the gold so eligibility (sum nwap*schemeRpg) comfortably exceeds the loan --\n"
                 "// final-loan-details 400s when eligible < finalLoanAmount. Conservative rpg floor.\n"
                 "var requiredGross = (target * 1.3 / 4000) / 0.9;\n"
                 "var baseGross = Math.max(20, requiredGross / N);\n"
                 "var karatValue = GL.karatValue(GL.get('karat')) || 22;\n"
                 "var karatPurity = karatValue / 24 * 100;\n"
                 "var ltvRange = kd.ltvRange && kd.ltvRange.length ? kd.ltvRange : [90];\n"
                 "var orn = [], total = 0;\n"
                 "for (var i = 0; i < N; i++) {\n"
                 "  var type = GL.pick(types);\n"
                 "  var gross = GL.round2(baseGross * GL.uniform(0.85, 1.15));\n"
                 "  var deduction = GL.round2(GL.uniform(0, 5));\n"
                 "  var net = GL.round2(gross - deduction);\n"
                 "  var ltvPercent = String(GL.pick(ltvRange));\n"
                 "  // netWtAfterPurity MUST mirror the server's recompute:\n"
                 "  //   nwap = netWeight * min(purityReading, ltvPercent) / 100\n"
                 "  // i.e. the assessed purity is CAPPED at the ornament's LTV%. Storing the capped\n"
                 "  // value as purityReading too means no further server capping applies.\n"
                 "  var purity = GL.round2(Math.min(karatPurity, GL.num(ltvPercent, karatPurity)));\n"
                 "  var nwap = GL.round2(net * (purity / 100));\n"
                 "  var ltvAmount = GL.round2(rate, 2) * nwap;\n"
                 "  total += ltvAmount;\n"
                 "  orn.push({\n"
                 "    ornamentType: type, ornamentTypeId: GL.int(type.id), quantity: '1',\n"
                 "    grossWeight: String(gross), netWeight: String(net),\n"
                 "    deductionWeight: String(deduction), ornamentImage: img,\n"
                 "    weightMachineZeroWeight: null, stoneTouch: null, acidTest: null,\n"
                 "    karat: GL.get('karat'), ltvRange: ltvRange, purityTest: [],\n"
                 "    ltvPercent: ltvPercent, loanAmount: null, id: null,\n"
                 "    currentLtvAmount: ltvAmount, ornamentImageData: GL.fullUrl(img),\n"
                 "    weightMachineZeroWeightData: null, withOrnamentWeightData: null,\n"
                 "    stoneTouchData: null, acidTestData: null, purityTestImage: [],\n"
                 "    ornamentFullAmount: null, currentGoldRate: GL.round2(rate),\n"
                 "    ornamentImageWithWeight: null, ornamentImageWithWeightData: null,\n"
                 "    ornamentImageWithXrfMachineReading: null,\n"
                 "    ornamentImageWithXrfMachineReadingData: null,\n"
                 "    approxPurityReading: String(purity), scrapAmount: null,\n"
                 "    purityReading: String(purity), customerConfirmation: null,\n"
                 "    finalScrapAmountAfterMelting: null, processingCharges: null, packetId: null,\n"
                 "    netWtAfterPurity: String(nwap), remark: null, isReleased: null\n"
                 "  });\n"
                 "}\n"
                 "GL.body({\n"
                 "  loanOrnaments: orn, totalEligibleAmt: total,\n"
                 "  loanId: GL.int(GL.get('loan_id')), masterLoanId: GL.int(GL.get('master_loan_id')),\n"
                 "  fullAmount: 0, checkPointers: {}\n"
                 "});"),
            test=("GL.soft('ornaments-details');\n"
                  "// Capture the SERVER's stored ornaments (real DB ids); final-loan-details validates\n"
                  "// against these, so echoing our locally-built ones (id=null) fails.\n"
                  "var server = GL.findOrnaments(GL.json());\n"
                  "var sent = JSON.parse(pm.collectionVariables.get('__body')).loanOrnaments;\n"
                  "var use = (server && server.length) ? server : sent;\n"
                  "GL.setJson('ornaments', use.map(function (o) {\n"
                  "  return { id: o.id === undefined ? null : o.id,\n"
                  "           netWtAfterPurity: o.netWtAfterPurity,\n"
                  "           karat: o.karat,\n"
                  "           ornamentName: (o.ornamentType && (o.ornamentType.name ||\n"
                  "                          o.ornamentType.ornamentType)) ||\n"
                  "                         (typeof o.ornamentType === 'string' ? o.ornamentType : '') };\n"
                  "}));\n"
                  "GL.log('stored ornaments:', use.length, 'ids',\n"
                  "       JSON.stringify(use.map(function (o) { return o.id; })));"),
            json_body=True),
    ]
    return folder("05 - Ornaments", items,
                  "Four single-karat ornaments, weight-sized so eligibility clears the target loan.")


# --------------------------------------------------------------------------------------------
# 06 — scheme, eligibility, final loan details
# --------------------------------------------------------------------------------------------
def folder_scheme():
    items = [
        req("SCH: loan balance", "GET",
            "/api/loan-process/get-balance?appraiserRequestId={{appraiser_request_id}}",
            pre="GL.role('appraiser'); GL.sign();", test="GL.soft('get-balance');"),
        req("SCH: co-lender banks", "GET", "/api/co-lender-bank",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.soft('co-lender-bank');\n"
                  "var want = String(GL.get('co_lender', '')).trim();\n"
                  "GL.set('forced_co_lender_id', '');\n"
                  "if (want) {\n"
                  "  var banks = GL.json().data || GL.json() || [];\n"
                  "  var hit = banks.filter(function (b) {\n"
                  "    return String(b.id) === want ||\n"
                  "           String(b.bankName || b.name || '').toLowerCase()\n"
                  "                 .indexOf(want.toLowerCase()) >= 0;\n"
                  "  })[0];\n"
                  "  if (hit) { GL.set('forced_co_lender_id', hit.id); GL.log('co-lender', hit.id); }\n"
                  "  else { GL.log('co-lender', want, 'not found; continuing without co-lending'); }\n"
                  "}")),
        req("SCH: scheme catalog", "GET", "/api/scheme?partnerType=partner&search=",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.soft('scheme catalog');\n"
                  "// {data:[{id,name,partnerId,rpg,schemes:[...]}]} -- a list of PARTNERS, each with\n"
                  "// nested schemes[]. Flatten and index by SCHEME id. NOTE: the catalog's scheme rpg\n"
                  "// (~45000) is NOT the per-gram eligibility rate; only active/secured/karat/amount\n"
                  "// are read here.\n"
                  "var cat = {};\n"
                  "(GL.json().data || []).forEach(function (p) {\n"
                  "  var list = (p.schemes && p.schemes.length) ? p.schemes : (p.schemeName ? [p] : []);\n"
                  "  list.forEach(function (s) { if (s && s.id !== undefined) { cat[String(s.id)] = s; } });\n"
                  "});\n"
                  "GL.setJson('scheme_catalog', cat);\n"
                  "GL.log('catalog schemes:', Object.keys(cat).length);")),
        req("SCH: partner scheme amounts", "GET",
            "/api/scheme/partner-scheme-amount/1?masterLoanId={{master_loan_id}}",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('partner-scheme-amount');\n"
                  "var partners = GL.json().data || [];\n"
                  "var wantName = String(GL.get('partner_name', 'ROSHAN PARTNER')).toUpperCase();\n"
                  "var target = GL.num(GL.get('loan_amount'), 400000);\n"
                  "var cat = GL.getJson('scheme_catalog', {});\n"
                  "var karats = GL.getJson('ornaments', []).map(function (o) {\n"
                  "  return GL.karatValue(o.karat);\n"
                  "}).filter(function (k) { return k > 0; });\n"
                  "function ptext(p) {\n"
                  "  return [p.partnerName, p.partner, p.partnerId, p.name]\n"
                  "         .map(function (v) { return String(v || ''); }).join(' ').toUpperCase();\n"
                  "}\n"
                  "function pnum(p) {\n"
                  "  var raw = String(p.partnerId || p.id || '');\n"
                  "  var tail = raw.split('-').pop().replace(/\\D/g, '');\n"
                  "  return tail || raw.replace(/\\D/g, '') || String(p.id || '');\n"
                  "}\n"
                  "var matched = partners.filter(function (p) { return ptext(p).indexOf(wantName) >= 0; });\n"
                  "if (!matched.length) { matched = partners; GL.log('WARNING: no partner matched', wantName); }\n"
                  "// The real scheme id/rpg/ltv/range live INSIDE schemes[] -- never use the partner's\n"
                  "// own id/rpg as the scheme.\n"
                  "var cands = [];\n"
                  "matched.forEach(function (p) {\n"
                  "  var subs = p.schemes || [];\n"
                  "  if (!subs.length && p.rpg !== undefined && p.schemeAmountStart !== undefined) { subs = [p]; }\n"
                  "  subs.forEach(function (s) {\n"
                  "    var c = JSON.parse(JSON.stringify(s));\n"
                  "    c._partnerId = pnum(p);\n"
                  "    c._partnerName = p.name || p.partnerId;\n"
                  "    cands.push(c);\n"
                  "  });\n"
                  "});\n"
                  "function fits(s) {\n"
                  "  var lo = GL.num(s.schemeAmountStart, 0), hi = GL.num(s.schemeAmountEnd, 0);\n"
                  "  return lo <= target && (hi === 0 || target <= hi);\n"
                  "}\n"
                  "var pool = cands.filter(fits);\n"
                  "if (!pool.length) { pool = cands; }\n"
                  "// Deterministic: preferred partner first, then LOWEST scheme id.\n"
                  "pool.sort(function (a, b) { return GL.num(a.id) - GL.num(b.id); });\n"
                  "// Catalog pre-check: drop inactive / non-secured / amount- or karat-ineligible.\n"
                  "function eligible(s) {\n"
                  "  var c = cat[String(s.id)] || {};\n"
                  "  var p = function (k, d) {\n"
                  "    var v = s[k]; if (v === undefined || v === null) { v = c[k]; }\n"
                  "    return (v === undefined || v === null) ? d : v;\n"
                  "  };\n"
                  "  if (p('isActive', true) === false) { return 'inactive'; }\n"
                  "  var st = p('schemeType', null);\n"
                  "  if (st && String(st).toLowerCase() !== 'secured') { return 'not secured (' + st + ')'; }\n"
                  "  var lo = GL.num(p('schemeAmountStart', 0)), hi = GL.num(p('schemeAmountEnd', 0));\n"
                  "  if (target < lo || (hi && target > hi)) { return 'amount outside ' + lo + '-' + hi; }\n"
                  "  var mn = GL.karatValue(p('minKarat', null)), mx = GL.karatValue(p('maxKarat', null));\n"
                  "  if (karats.length && mn && mx) {\n"
                  "    var bad = karats.filter(function (k) { return k < mn || k > mx; });\n"
                  "    if (bad.length) { return 'karat ' + bad.join(',') + ' outside ' + mn + '-' + mx; }\n"
                  "  }\n"
                  "  return null;\n"
                  "}\n"
                  "var ok = [];\n"
                  "pool.forEach(function (s) {\n"
                  "  var why = eligible(s);\n"
                  "  if (why) { GL.log('pre-check drops scheme', s.id, '-', why); } else { ok.push(s); }\n"
                  "});\n"
                  "var finalPool = ok.length ? ok : pool;\n"
                  "// A pinned scheme wins even over the pre-check.\n"
                  "var pin = String(GL.get('scheme_id_pin', '')).trim();\n"
                  "if (pin) {\n"
                  "  var forced = cands.filter(function (s) { return String(s.id) === pin; })[0];\n"
                  "  if (forced) {\n"
                  "    finalPool = [forced].concat(finalPool.filter(function (s) { return String(s.id) !== pin; }));\n"
                  "    GL.log('pinned scheme', pin);\n"
                  "  } else { GL.log('WARNING: pinned scheme', pin, 'not under this partner'); }\n"
                  "}\n"
                  "pm.test('candidate schemes found', function () { pm.expect(finalPool.length).to.be.above(0); });\n"
                  "GL.setJson('scheme_candidates', finalPool);\n"
                  "GL.set('scheme_idx', 0);\n"
                  "GL.log('candidates:', JSON.stringify(finalPool.map(function (s) {\n"
                  "  return [s.id, s.rpg];\n"
                  "})));")),
        req("SCH: check loan type", "POST", "/api/loan-process/check-loan-type",
            pre=("GL.role('appraiser');\n"
                 "var cands = GL.getJson('scheme_candidates', []);\n"
                 "var idx = GL.int(GL.get('scheme_idx', '0'), 0);\n"
                 "if (idx >= cands.length) { throw new Error('No candidate scheme left to try.'); }\n"
                 "var s = cands[idx];\n"
                 "GL.set('scheme_id', s.id);\n"
                 "GL.set('partner_id', s._partnerId);\n"
                 "// A user-requested co-lender wins over the scheme's own mapping.\n"
                 "var co = GL.get('forced_co_lender_id') || (s.coLenderBankId || '');\n"
                 "GL.set('co_lender_bank_id', co);\n"
                 "GL.set('secured_rpg', GL.num(s.rpg, 0));\n"
                 "GL.set('secured_ltv', GL.num(s.ltv, 0));\n"
                 "GL.set('final_loan_amount', GL.num(GL.get('loan_amount'), 400000));\n"
                 "GL.log('trying scheme', s.id, 'partner', s._partnerId, '(' + (idx + 1) + '/' + cands.length + ')');\n"
                 "GL.body({\n"
                 "  loanAmount: GL.num(GL.get('loan_amount'), 400000),\n"
                 "  securedSchemeId: GL.int(s.id), fullAmount: 0,\n"
                 "  partnerId: GL.int(s._partnerId), isLoanTransfer: false,\n"
                 "  isNewLoanFromPartRelease: false, unsecuredSchemeId: null,\n"
                 "  isUnsecuredSchemeApplied: false, loanTransferExtraAmount: null,\n"
                 "  isNewLoanFromRenew: false,\n"
                 "  loanId: GL.int(GL.get('loan_id')), masterLoanId: GL.int(GL.get('master_loan_id')),\n"
                 "  loanType: 'Fresh Loan', createdBy: String(GL.get('logged_in_user_id')),\n"
                 "  internalBranchId: GL.int(GL.get('internal_branch_id')),\n"
                 "  coLenderBankId: co ? GL.int(co) : null\n"
                 "});"),
            test=("var cands = GL.getJson('scheme_candidates', []);\n"
                  "var idx = GL.int(GL.get('scheme_idx', '0'), 0);\n"
                  "var body = pm.response.text();\n"
                  "function tryNext(why) {\n"
                  "  GL.log('scheme', GL.get('scheme_id'), 'rejected:', why);\n"
                  "  if (idx + 1 < cands.length) {\n"
                  "    GL.set('scheme_idx', idx + 1);\n"
                  "    postman.setNextRequest('SCH: check loan type');\n"
                  "  } else {\n"
                  "    pm.test('a scheme was accepted', function () {\n"
                  "      pm.expect.fail('No candidate scheme fit. Last: ' + why);\n"
                  "    });\n"
                  "  }\n"
                  "}\n"
                  "if (pm.response.code >= 400) {\n"
                  "  var low = body.toLowerCase();\n"
                  "  if (low.indexOf('loan already exists') >= 0) {\n"
                  "    pm.test('check-loan-type (loan already exists)', function () { pm.expect(true).to.be.true; });\n"
                  "  } else if (low.indexOf('range amount') >= 0 || low.indexOf('scheme range') >= 0) {\n"
                  "    tryNext('amount range');\n"
                  "  } else {\n"
                  "    pm.test('check-loan-type [' + pm.response.code + ']', function () {\n"
                  "      pm.expect.fail(body.substring(0, 400));\n"
                  "    });\n"
                  "  }\n"
                  "} else {\n"
                  "  var d = GL.json().data || {};\n"
                  "  var ss = d.securedScheme || {};\n"
                  "  // Capture the scheme's real rate/gram + LTV, and the loan-calculator params the\n"
                  "  // server computed. final-loan-details must echo these or it 400s.\n"
                  "  if (ss.rpg) { GL.set('secured_rpg', GL.num(ss.rpg)); }\n"
                  "  if (ss.ltv) { GL.set('secured_ltv', GL.num(ss.ltv)); }\n"
                  "  if (d.securedprocessingCharge !== undefined && d.securedprocessingCharge !== null) {\n"
                  "    GL.set('secured_processing_charge', d.securedprocessingCharge);\n"
                  "  }\n"
                  "  if (d.upfrontInterestAmount !== undefined && d.upfrontInterestAmount !== null) {\n"
                  "    GL.set('upfront_interest_amount', d.upfrontInterestAmount);\n"
                  "  }\n"
                  "  if (d.interestRate !== undefined && d.interestRate !== null) {\n"
                  "    GL.set('interest_rate', d.interestRate);\n"
                  "  }\n"
                  "  if (d.posExposureAgainstScheme !== undefined && d.posExposureAgainstScheme !== null) {\n"
                  "    GL.set('secured_exposure', d.posExposureAgainstScheme);\n"
                  "  }\n"
                  "  if (d.tenure) { GL.set('tenure', GL.int(d.tenure)); }\n"
                  "  if (d.loanStartDate) { GL.set('loan_start_date', String(d.loanStartDate).substring(0, 10)); }\n"
                  "  if (d.loanEndDate) { GL.set('loan_end_date', String(d.loanEndDate).substring(0, 10)); }\n"
                  "  // The scheme must cover EVERY ornament karat, else the server computes\n"
                  "  // eligibility over a subset and totalEligibleAmt won't match.\n"
                  "  var karats = GL.getJson('ornaments', []).map(function (o) { return GL.karatValue(o.karat); })\n"
                  "                 .filter(function (k) { return k > 0; });\n"
                  "  var mn = GL.karatValue(ss.minKarat), mx = GL.karatValue(ss.maxKarat);\n"
                  "  var bad = (karats.length && mn && mx)\n"
                  "    ? karats.filter(function (k) { return k < mn || k > mx; }) : [];\n"
                  "  if (bad.length) {\n"
                  "    tryNext('karat ' + bad.join(',') + ' outside ' + mn + '-' + mx);\n"
                  "  } else {\n"
                  "    pm.test('check-loan-type accepted scheme ' + GL.get('scheme_id'), function () {\n"
                  "      pm.expect(pm.response.code).to.equal(200);\n"
                  "    });\n"
                  "    GL.log('scheme', GL.get('scheme_id'), 'rpg', GL.get('secured_rpg'),\n"
                  "           'tenure', GL.get('tenure'), 'procCharge', GL.get('secured_processing_charge'));\n"
                  "  }\n"
                  "}"),
            json_body=True,
            description="Loops over the candidate schemes (setNextRequest onto itself) until one is "
                        "accepted and its karat range covers every ornament."),
        req("SCH: interest rate", "POST", "/api/loan-process/interest-rate",
            pre=("GL.role('appraiser');\n"
                 "GL.body({ securedSchemeId: GL.int(GL.get('scheme_id')), unsecuredSchemeId: null });"),
            test="GL.soft('interest-rate');",
            json_body=True),
        req("SCH: generate interest table", "POST", "/api/loan-process/generate-interest-table",
            pre=("GL.role('appraiser');\n"
                 "var tenure = GL.int(GL.get('tenure', '4'), 4);\n"
                 "var start = GL.get('loan_start_date') || GL.todayIso();\n"
                 "var end = GL.get('loan_end_date');\n"
                 "if (!end) {\n"
                 "  var d = new Date(); d.setDate(d.getDate() + tenure * 30);\n"
                 "  end = d.toISOString().substring(0, 10);\n"
                 "}\n"
                 "var co = GL.get('co_lender_bank_id');\n"
                 "GL.body({\n"
                 "  partnerId: GL.int(GL.get('partner_id')), coLenderBankId: co ? GL.int(co) : null,\n"
                 "  schemeId: GL.int(GL.get('scheme_id')),\n"
                 "  finalLoanAmount: GL.num(GL.get('final_loan_amount')),\n"
                 "  tenure: tenure, loanStartDate: start, loanEndDate: end,\n"
                 "  paymentFrequency: '30 Days', totalFinalInterestAmt: null,\n"
                 "  unsecuredInterestRate: null, interestRate: GL.num(GL.get('interest_rate')),\n"
                 "  processingCharge: GL.num(GL.get('secured_processing_charge')),\n"
                 "  unsecuredSchemeId: null,\n"
                 "  securedLoanAmount: GL.num(GL.get('final_loan_amount')),\n"
                 "  unsecuredLoanAmount: null, isUnsecuredSchemeApplied: false,\n"
                 "  unsecuredRebateInterest: null, securedRebateInterest: null,\n"
                 "  otherAmount: null, loanTransferExtraAmount: null,\n"
                 "  upfrontInterestAmount: 0, topUpAmount: 0, securedTopUpAmount: 0,\n"
                 "  unsecuredTopUpAmount: 0, manualCharges: [], unsecuredRpg: null,\n"
                 "  securedRpg: GL.num(GL.get('secured_rpg'), 4480), unsecuredPartnerId: null,\n"
                 "  unsecuredprocessingCharge: 0, securedprocessingCharge: 0,\n"
                 "  securedExposure: '0.13'\n"
                 "});"),
            test=("GL.ok('generate-interest-table');\n"
                  "// The payload is wrapped in data: reading the top level silently yields an empty\n"
                  "// schedule, which makes final-loan-details fail with the eligibility message.\n"
                  "var d = GL.json().data || GL.json() || {};\n"
                  "var table = d.interestTable || [];\n"
                  "pm.test('interest table is populated', function () { pm.expect(table.length).to.be.above(0); });\n"
                  "GL.setJson('interest_table', table);\n"
                  "GL.set('total_final_interest_amt', d.totalInterestAmount || d.totalFinalInterestAmt || 0);\n"
                  "if (d.securedRebateInterest !== undefined && d.securedRebateInterest !== null) {\n"
                  "  GL.set('secured_rebate_interest', d.securedRebateInterest);\n"
                  "}\n"
                  "GL.log('interest rows', table.length, 'total', GL.get('total_final_interest_amt'));"),
            json_body=True),
        req("SCH: final loan details", "POST", "/api/loan-process/final-loan-details",
            pre=("GL.role('appraiser');\n"
                 "var rpg = GL.num(GL.get('secured_rpg'));\n"
                 "if (rpg <= 0) { throw new Error('Scheme rate/gram (rpg) was never captured.'); }\n"
                 "// Eligibility = sum(netWtAfterPurity * SCHEME rpg). Do NOT apply LTV again (the rpg\n"
                 "// already encodes it) and do NOT use the gold valuation (currentLtvAmount).\n"
                 "var orn = GL.getJson('ornaments', []);\n"
                 "var rows = [], total = 0;\n"
                 "orn.forEach(function (o) {\n"
                 "  var nwap = GL.num(o.netWtAfterPurity);\n"
                 "  var amt = GL.round2(nwap * rpg);\n"
                 "  rows.push({ id: o.id, loanAmount: amt, ornamentsCal: nwap, rpg: rpg });\n"
                 "  total += amt;\n"
                 "});\n"
                 "total = GL.round2(total);\n"
                 "GL.set('total_eligible_amount', total);\n"
                 "// A loan can't exceed its eligibility.\n"
                 "var finalAmt = GL.num(GL.get('final_loan_amount'));\n"
                 "if (finalAmt > total) {\n"
                 "  finalAmt = total; GL.set('final_loan_amount', finalAmt);\n"
                 "  GL.log('capped finalLoanAmount to eligible', total);\n"
                 "}\n"
                 "var tenure = GL.int(GL.get('tenure', '4'), 4);\n"
                 "var start = GL.get('loan_start_date') || GL.todayIso();\n"
                 "var end = GL.get('loan_end_date');\n"
                 "if (!end) {\n"
                 "  var dt = new Date(); dt.setDate(dt.getDate() + tenure * 30);\n"
                 "  end = dt.toISOString().substring(0, 10);\n"
                 "}\n"
                 "var co = GL.get('co_lender_bank_id');\n"
                 "GL.log('totalEligibleAmt', total, '= sum(nwap * rpg', rpg + ')');\n"
                 "GL.body({\n"
                 "  manualCharges: [],\n"
                 "  loanFinalCalculator: {\n"
                 "    partnerId: GL.int(GL.get('partner_id')), coLenderBankId: co ? GL.int(co) : null,\n"
                 "    schemeId: GL.int(GL.get('scheme_id')), finalLoanAmount: finalAmt,\n"
                 "    tenure: tenure, loanStartDate: start, loanEndDate: end,\n"
                 "    paymentFrequency: '30 Days',\n"
                 "    totalFinalInterestAmt: GL.num(GL.get('total_final_interest_amt')),\n"
                 "    unsecuredInterestRate: null, interestRate: GL.num(GL.get('interest_rate')),\n"
                 "    processingCharge: GL.num(GL.get('secured_processing_charge')),\n"
                 "    unsecuredSchemeId: null, securedLoanAmount: finalAmt,\n"
                 "    unsecuredLoanAmount: null, isUnsecuredSchemeApplied: false,\n"
                 "    unsecuredRebateInterest: 0,\n"
                 "    securedRebateInterest: GL.num(GL.get('secured_rebate_interest')),\n"
                 "    otherAmount: null, loanTransferExtraAmount: null,\n"
                 "    upfrontInterestAmount: GL.num(GL.get('upfront_interest_amount')),\n"
                 "    topUpAmount: 0, securedTopUpAmount: 0, unsecuredTopUpAmount: 0,\n"
                 "    manualCharges: [], unsecuredRpg: null, securedRpg: rpg,\n"
                 "    unsecuredPartnerId: null, unsecuredprocessingCharge: 0,\n"
                 "    securedprocessingCharge: GL.num(GL.get('secured_processing_charge')),\n"
                 "    securedExposure: String(GL.get('secured_exposure', '0'))\n"
                 "  },\n"
                 "  interestTable: GL.getJson('interest_table', []),\n"
                 "  loanId: GL.int(GL.get('loan_id')), masterLoanId: GL.int(GL.get('master_loan_id')),\n"
                 "  ornaments: rows, totalEligibleAmt: total, checkPointers: {}\n"
                 "});"),
            test=("GL.ok('final-loan-details');\n"
                  "if (pm.response.code >= 400 &&\n"
                  "    pm.response.text().toLowerCase().indexOf('eligible amount') >= 0) {\n"
                  "  GL.log('ELIGIBILITY MISMATCH. Sent', GL.get('total_eligible_amount'),\n"
                  "         '= sum(nwap * rpg', GL.get('secured_rpg') + ').',\n"
                  "         'Check: interest table populated? all ornaments one karat?',\n"
                  "         'eligible >= loan amount?');\n"
                  "}"),
            json_body=True),
    ]
    return folder("06 - Scheme & eligibility", items,
                  "Scheme selection (partner -> schemes[] -> catalog pre-check -> karat-aware retry), "
                  "interest schedule, then the eligibility-validated final-loan-details.")


# --------------------------------------------------------------------------------------------
# 07 — bank details
# --------------------------------------------------------------------------------------------
def folder_bank():
    items = [
        req("BANK: fetch bank details", "GET",
            "/api/loan-process/bank-details?masterLoanId={{master_loan_id}}",
            pre="GL.role('appraiser'); GL.sign();", test="GL.soft('bank-details GET');"),
        req("BANK: IFSC lookup (karza)", "GET",
            "/api/loan-process/account-details-karza?ifscCode={{bank_ifsc_code}}",
            pre="GL.role('appraiser'); GL.sign();",
            test="GL.soft('account-details-karza');",
            description="IFSC lookup only — this does NOT verify the account."),
        req("BANK: upload cheque", "POST",
            "/api/upload-file?reason=loan&customerId={{customer_id}}",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('cheque upload');\n"
                  "GL.set('passbook_proof', GL.json().uploadFile.path);"),
            form=[("avatar", "file", "assets/cancelled-cheque-1.png")]),
        req("BANK: validate account (penny drop)", "POST", "/api/loan-process/validate-account",
            pre=("GL.role('appraiser');\n"
                 "var p = GL.get('passbook_proof');\n"
                 "GL.body({\n"
                 "  ifscCode: GL.get('bank_ifsc_code'), accountNumber: GL.get('bank_account_number'),\n"
                 "  accountHolderName: GL.get('account_holder_name'), bankName: GL.get('bank_name'),\n"
                 "  bankBranchName: GL.get('bank_branch_name'),\n"
                 "  passbookProof: [p, p], passbookProofImage: [GL.fullUrl(p), GL.fullUrl(p)],\n"
                 "  passbookProofImageName: [], detailsFor: 'customer',\n"
                 "  detailsForId: GL.int(GL.get('customer_id'))\n"
                 "});"),
            test=("GL.soft('validate-account');\n"
                  "// Rely on bankTxnStatus, NOT the message -- it can read 'Something went wrong'\n"
                  "// even on a fully successful verification.\n"
                  "var txn = GL.findKey(GL.json(), 'bankTxnStatus');\n"
                  "GL.set('bank_account_verified', txn ? 'true' : 'false');\n"
                  "GL.log('bankTxnStatus =', txn);"),
            json_body=True,
            description="MUST run before POST bank-details, else that call 400s with 'bank details "
                        "is not verified'."),
        req("BANK: store bank details", "POST", "/api/loan-process/bank-details",
            pre=("GL.role('appraiser');\n"
                 "var proc = GL.num(GL.get('secured_processing_charge'));\n"
                 "var upfront = GL.num(GL.get('upfront_interest_amount'));\n"
                 "var finalAmt = GL.num(GL.get('final_loan_amount'));\n"
                 "// toBePaid MUST equal finalLoanAmount - processingCharge - upfrontInterest, or the\n"
                 "// server rejects with 'To Be Paid amount is incorrect'.\n"
                 "var toBePaid = GL.round2(finalAmt - proc - upfront);\n"
                 "var verified = String(GL.get('bank_account_verified')) === 'true';\n"
                 "var p = GL.get('passbook_proof');\n"
                 "GL.body({\n"
                 "  paymentType: ['bank'], finalScrapAmount: null,\n"
                 "  bankName: GL.get('bank_name'), accountNumber: GL.get('bank_account_number'),\n"
                 "  ifscCode: GL.get('bank_ifsc_code'), accountType: null,\n"
                 "  customerName: GL.get('first_name') + ' ' + GL.get('last_name'),\n"
                 "  paymentMultiSelect: { multiSelect: ['bank'] },\n"
                 "  accountHolderName: GL.get('account_holder_name'),\n"
                 "  bankBranchName: GL.get('bank_branch_name'),\n"
                 "  passbookProof: [p, p],\n"
                 "  passbookProofImage: [GL.fullUrl(p), GL.fullUrl(p)],\n"
                 "  passbookProofImageName: [GL.baseName(p), GL.baseName(p)],\n"
                 "  account: 1508, detailsFor: 'customer', customerTransferBalance: 0,\n"
                 "  internalBranchTransferBalance: 17882793.96,\n"
                 "  internalBranchBlockedBalance: 11916017.72,\n"
                 "  advanceBankTransfer: 0, advanceBankTransactionId: null, advanceCash: 0,\n"
                 "  advanceCashTransactionId: [], advanceCashTransactionImage: [],\n"
                 "  advanceCashTransactionImageName: '', advanceCashDeclarationId: [],\n"
                 "  bTMoney: 0, actualProcessingCharge: proc, toBePaid: toBePaid, remark: null,\n"
                 "  upfrontInterestAmount: upfront, upfrontTransactionId: null,\n"
                 "  upfrontReceipt: [], upfrontReceiptImage: [], upfrontReceiptImageName: '',\n"
                 "  amountDisbursedByCash: 0, amountDisbursedByBank: 0, forOpsApproval: false,\n"
                 "  isManuallyVerified: !verified, isVerified: true, isDummyDetails: false,\n"
                 "  manualVerifiedStatus: verified ? '' : 'verified',\n"
                 "  wavier: 0, processingCharge: proc, receiptNumber: '',\n"
                 "  signature: GL.get('signature_proof'), pendingAmount: '',\n"
                 "  totalPledgedGoldAmount: '', extraAmountDisbursed: '', advanceAmount: '',\n"
                 "  modeOfPayment: '', appraiserCharges: 0, actualAppraiserCharges: 0,\n"
                 "  appraiserChargesPercentage: 0, actualCashGiven: 0,\n"
                 "  availableLimitForDisbuserment: toBePaid, wavierAppraiserCharges: 0,\n"
                 "  stampDutyCharges: 0, stampDutyDefinitionType: null, stampDutyRemarks: '',\n"
                 "  totalLimitForCashDisbursal: 200000, multiModeDisbursement: true,\n"
                 "  checkPointers: {}, loanId: GL.int(GL.get('loan_id')),\n"
                 "  masterLoanId: GL.int(GL.get('master_loan_id')), loanType: 'Fresh Loan',\n"
                 "  createdBy: String(GL.get('logged_in_user_id')),\n"
                 "  internalBranchId: GL.int(GL.get('internal_branch_id'))\n"
                 "});"),
            test="GL.ok('store bank-details');",
            json_body=True),
        req("BANK: appraiser rating", "POST", "/api/loan-process/appraiser-rating",
            pre=("GL.role('appraiser');\n"
                 "GL.body({\n"
                 "  applicationFormForAppraiser: true, goldValuationForAppraiser: true,\n"
                 "  loanStatusForAppraiser: 'approved', commentByAppraiser: null, partRelease: null,\n"
                 "  loanId: GL.int(GL.get('loan_id')), masterLoanId: GL.int(GL.get('master_loan_id'))\n"
                 "});"),
            test="GL.ok('appraiser-rating');",
            json_body=True),
    ]
    return folder("07 - Bank details", items)


# --------------------------------------------------------------------------------------------
# 08 — packet
# --------------------------------------------------------------------------------------------
def folder_packet():
    b64 = ("PKT: upload packet image %d", )  # placeholder for clarity
    del b64

    def packet_image(n, var):
        return req("PKT: upload packet image %d" % n, "POST", "/api/upload-file/base",
                   pre=("GL.role('appraiser');\n"
                        "GL.body({ avatar: GL.get('dummy_image_b64') });"),
                   test=("GL.ok('packet image %d');\n"
                         "var d = GL.json();\n"
                         "GL.set('%s', (d.uploadFile || {}).path || d.path);" % (n, var)),
                   json_body=True)

    items = [
        req("PKT: create packet (ADMIN)", "POST", "/api/packet",
            pre=("// Packet create + assign require ADMIN; sealing runs as the appraiser.\n"
                 "GL.role('admin');\n"
                 "var unique = 'pac-' + GL.ri(10000000, 99999999);\n"
                 "GL.set('packet_unique_id', unique);\n"
                 "GL.body({\n"
                 "  id: null, packetUniqueId: unique,\n"
                 "  internalUserBranch: GL.int(GL.get('internal_branch_id'), 1),\n"
                 "  appraiserId: null, auditorId: null,\n"
                 "  barcodeNumber: unique, userType: '', isNewPacket: false\n"
                 "});"),
            test=("GL.ok('create packet');\nGL.log('packet', GL.get('packet_unique_id'));"),
            json_body=True,
            description="barcodeNumber == packetUniqueId. The response carries no id, so the next "
                        "request resolves it from the listing."),
        req("PKT: resolve packet id (ADMIN)", "GET", "/api/packet?from=1&to=50",
            pre="GL.role('admin'); GL.sign();",
            test=("GL.ok('packet listing');\n"
                  "var want = GL.get('packet_unique_id');\n"
                  "var list = GL.json().packetDetails || GL.json().data || [];\n"
                  "var p = list.filter(function (x) {\n"
                  "  return x.packetUniqueId === want || x.barcodeNumber === want;\n"
                  "})[0];\n"
                  "pm.test('created packet found in listing', function () { pm.expect(!!p).to.be.true; });\n"
                  "GL.set('packet_id', p.id);")),
        req("PKT: assign packet to appraiser (ADMIN)", "PUT", "/api/packet/{{packet_id}}",
            pre=("GL.role('admin');\n"
                 "GL.body({\n"
                 "  id: GL.int(GL.get('packet_id')), packetUniqueId: GL.get('packet_unique_id'),\n"
                 "  internalUserBranch: GL.int(GL.get('internal_branch_id'), 1),\n"
                 "  appraiserId: GL.int(GL.get('logged_in_user_id')), auditorId: null,\n"
                 "  barcodeNumber: GL.get('packet_unique_id'), userType: 'appraiser',\n"
                 "  isNewPacket: false\n"
                 "});"),
            test=("GL.ok('assign packet');\nGL.role('appraiser');"),
            json_body=True),
        req("PKT: single loan", "GET",
            "/api/loan-process/single-loan?customerLoanId={{loan_id}}&from=undefined",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('single-loan');\n"
                  "// loanUniqueId (AUGM-...) is assigned at the assign-packet stage.\n"
                  "var d = GL.json().data || {};\n"
                  "if (d.loanUniqueId) { GL.set('loan_unique_id', d.loanUniqueId); }\n"
                  "GL.log('loanUniqueId', GL.get('loan_unique_id'));")),
        packet_image(1, "packet_img_empty"),
        packet_image(2, "packet_img_weight"),
        packet_image(3, "packet_img_customer"),
        packet_image(4, "packet_img_sealed"),
        req("PKT: add packet images (seal)", "POST", "/api/loan-process/add-packet-images",
            pre=("GL.role('appraiser');\n"
                 "var orn = GL.getJson('ornaments', []);\n"
                 "// There is no 'assign-packet' endpoint -- assignment is embedded here.\n"
                 "var packetOrnament = {\n"
                 "  packetId: String(GL.get('packet_id')),\n"
                 "  ornamentsId: orn.map(function (o) { return o.id; }).filter(function (x) { return !!x; }),\n"
                 "  packetsName: GL.get('packet_unique_id'),\n"
                 "  ornamentsName: orn.map(function (o) { return o.ornamentName; })\n"
                 "                    .filter(Boolean).join(', ')\n"
                 "};\n"
                 "var e = GL.get('packet_img_empty'), w = GL.get('packet_img_weight');\n"
                 "var c = GL.get('packet_img_customer'), s = GL.get('packet_img_sealed');\n"
                 "GL.body({\n"
                 "  emptyPacketWithNoOrnament: e, emptyPacketWithNoOrnamentImage: GL.fullUrl(e),\n"
                 "  sealingPacketWithWeight: w, sealingPacketWithWeightImage: GL.fullUrl(w),\n"
                 "  sealingPacketWithCustomer: c, sealingPacketWithCustomerImage: GL.fullUrl(c),\n"
                 "  sealedPacketWithWeight: '', sealedPacketWithWeightImage: '',\n"
                 "  ornamentImageWithWeight: '', ornamentImageWithWeightImage: '',\n"
                 "  ornamentImageWithXrfMachineReading: '',\n"
                 "  ornamentImageWithXrfMachineReadingImage: '',\n"
                 "  sealedPacket: s, sealedPacketImage: GL.fullUrl(s),\n"
                 "  packetOrnamentArray: [packetOrnament], checkPointers: {},\n"
                 "  loanType: 'Fresh Loan', loanId: GL.int(GL.get('loan_id')),\n"
                 "  masterLoanId: GL.int(GL.get('master_loan_id')),\n"
                 "  loanAmountDigi: GL.money(GL.get('final_loan_amount'))\n"
                 "});"),
            test="GL.ok('add-packet-images');",
            json_body=True),
        req("PKT: update loan lock", "POST", "/api/loan-process/update-loan-lock",
            pre=("GL.role('appraiser');\n"
                 "GL.body({ masterLoanId: GL.int(GL.get('master_loan_id')), loanStageId: 8 });"),
            test=("GL.soft('update-loan-lock');\n"
                  "// BM approval is only required above 5 lakh.\n"
                  "if (GL.num(GL.get('final_loan_amount')) <= 500000) {\n"
                  "  GL.log('loan <= 5L -> skipping BM rating');\n"
                  "  postman.setNextRequest('DOC: upload loan agreement');\n"
                  "}"),
            json_body=True),
    ]
    return folder("08 - Packet (create/assign/seal)", items,
                  "Admin creates + assigns the packet; the appraiser seals the ornaments into it.")


# --------------------------------------------------------------------------------------------
# 09..13
# --------------------------------------------------------------------------------------------
def folder_bm():
    return folder("09 - BM rating (only above 5L)", [
        req("BM: rating", "POST", "/api/loan-process/bm-rating",
            pre=("GL.role('bm');\n"
                 "GL.body({\n"
                 "  loanId: GL.int(GL.get('loan_id')), masterLoanId: GL.int(GL.get('master_loan_id')),\n"
                 "  applicationFormForBM: true, goldValuationForBM: true,\n"
                 "  loanStatusForBM: 'approved', commentByBM: null, partRelease: null\n"
                 "});"),
            test="GL.ok('bm-rating');\nGL.role('appraiser');",
            json_body=True),
    ], "Skipped automatically when finalLoanAmount <= 500000.")


def folder_documents():
    up = lambda name, var, reason, asset: req(  # noqa: E731
        name, "POST", "/api/upload-file?reason=%s&customerId={{customer_id}}" % reason,
        pre="GL.role('appraiser'); GL.sign();",
        test="GL.ok('%s');\nGL.set('%s', GL.json().uploadFile.path);" % (name, var),
        form=[("avatar", "file", asset)])

    items = [
        up("DOC: upload loan agreement", "doc_loan_agreement", "loan", "assets/CPV.pdf"),
        up("DOC: upload pawn copy", "doc_pawn_copy", "loan", "assets/CPV.pdf"),
        up("DOC: upload scheme confirmation", "doc_scheme_confirmation", "loan", "assets/CPV.pdf"),
        req("DOC: upload income document", "POST",
            "/api/upload-file?reason=customerIncomeGeneratingDocument&customerId={{customer_id}}",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('income document');\n"
                  "GL.set('doc_income', GL.json().uploadFile.path);"),
            form=[("avatar", "file", "assets/CPV.pdf")]),
        req("DOC: lead converter", "GET",
            "/api/lead/lead-converter?isForLeadConverter=true&isForProductivityReport=false",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.soft('lead-converter');\n"
                  "var id = GL.findKey(GL.json(), 'id');\n"
                  "GL.set('lead_converter_id', id || GL.get('logged_in_user_id'));")),
        req("DOC: loan documents", "POST", "/api/loan-process/loan-documents",
            pre=("GL.role('appraiser');\n"
                 "var la = GL.get('doc_loan_agreement'), pc = GL.get('doc_pawn_copy');\n"
                 "var sc = GL.get('doc_scheme_confirmation'), inc = GL.get('doc_income');\n"
                 "var masked = GL.get('masked_identity_proof'), unmasked = GL.get('unmasked_identity_proof');\n"
                 "var panImg = GL.get('pan_image') || null;\n"
                 "GL.body({\n"
                 "  loanAgreementCopy: [la], pawnCopy: [pc], schemeConfirmationCopy: [sc],\n"
                 "  loanApplicationCopy: null, goldReceipt: null, stampPaperCopy: null,\n"
                 "  signedCheque: null, declaration: null,\n"
                 "  loanAgreementImageName: GL.baseName(la), loanApplicationImageName: null,\n"
                 "  pawnCopyImageName: GL.baseName(pc),\n"
                 "  schemeConfirmationCopyImageName: GL.baseName(sc),\n"
                 "  goldReceiptName: null, stampPaperCopyImageName: null,\n"
                 "  signedChequeImageName: null, declarationCopyImageName: null,\n"
                 "  signedChequeImage: null, declarationCopyImage: null, outstandingLoanAmount: null,\n"
                 "  loanAgreementCopyImage: [GL.fullUrl(la)], loanApplicationCopyImage: null,\n"
                 "  pawnCopyImage: [GL.fullUrl(pc)], schemeConfirmationCopyImage: GL.fullUrl(sc),\n"
                 "  goldReceiptCopyName: null, stampPaperCopyFullUrl: null,\n"
                 "  kfsCopy: null, kfsCopyFullUrl: null, form97: null, form97Name: null,\n"
                 "  form97FullUrl: null, processingCharges: null, standardDeduction: null,\n"
                 "  customerConfirmation: null, customerConfirmationImage: null,\n"
                 "  customerConfirmationImageName: null, customerConfirmationStatus: null,\n"
                 "  purchaseVoucher: null, purchaseVoucherImage: null, purchaseVoucherImageName: null,\n"
                 "  purchaseInvoice: null, purchaseInvoiceImage: null, purchaseInvoiceImageName: null,\n"
                 "  saleInvoice: null, saleInvoiceImage: null, saleInvoiceImageName: null,\n"
                 "  signature: GL.fullUrl(GL.get('signature_proof')),\n"
                 "  panImage: panImg, panImageFullUrl: panImg ? GL.fullUrl(panImg) : null,\n"
                 "  panImageName: null, panCardNumber: GL.get('pan_card_number'),\n"
                 "  // identityProof / unMaskedIdentityProof go ENCRYPTED here (raw paths are rejected).\n"
                 "  identityProof: masked ? [GL.enc(masked)] : [],\n"
                 "  unMaskedIdentityProof: unmasked ? [GL.enc(unmasked)] : [],\n"
                 "  identityProofFullUrl: masked ? [GL.fullUrl(masked)] : [],\n"
                 "  identityProofName: null,\n"
                 "  advanceCashTransactionId: [], advanceCashTransactionImage: [],\n"
                 "  advanceCashTransactionImageName: '', advanceCashDeclarationId: [],\n"
                 "  advanceCashDeclarationImage: [], advanceCashDeclarationImageName: '',\n"
                 "  receiptNumber: '', advanceCash: 0, appraiserCharges: 0, actualCashGiven: 0,\n"
                 "  advanceTransferId: '', cpvImage: null, cpvImageFullUrl: null,\n"
                 "  leadConverterId: GL.get('lead_converter_id'),\n"
                 "  incomeGeneratingDocuments: null,\n"
                 "  consumptionSupportingDocs: [{ path: inc }],\n"
                 "  cKycNumber: GL.digits(14), checkPointers: {},\n"
                 "  loanId: GL.int(GL.get('loan_id')), masterLoanId: GL.int(GL.get('master_loan_id')),\n"
                 "  loanAmountDigi: GL.money(GL.get('final_loan_amount')), loanType: 'Fresh Loan'\n"
                 "});"),
            test="GL.soft('loan-documents');",
            json_body=True),
    ]
    return folder("10 - Loan documents", items)


def folder_ops():
    return folder("11 - Ops rating (final approval)", [
        req("OPS: rating", "POST", "/api/loan-process/ops-rating",
            pre=("GL.role('ops');\n"
                 "GL.body({\n"
                 "  applicationFormForAppraiser: true, goldValuationForAppraiser: true,\n"
                 "  loanStatusForAppraiser: 'approved', commentByAppraiser: 'auto approved',\n"
                 "  applicationFormForBM: true, goldValuationForBM: true,\n"
                 "  loanStatusForBM: 'approved', commentByBM: null, reasons: 'Other',\n"
                 "  applicationFormForOperatinalTeam: true, goldValuationForOperatinalTeam: true,\n"
                 "  applicationFormForPartner: false, goldValuationForPartner: false,\n"
                 "  loanStatusForOperatinalTeam: 'approved', loanStatusForPartner: 'pending',\n"
                 "  commentByPartner: '', scrapStatusForAppraiser: null, scrapStatusForBM: null,\n"
                 "  scrapStatusForOperatinalTeam: 'pending', packetApprovalByOps: null,\n"
                 "  commentByOpsForPacketApproval: '', statusForRh: 'pending', commentByRh: '',\n"
                 "  applicationFormForRh: false, goldValuationForRh: false, cKycNumber: null,\n"
                 "  loanId: GL.int(GL.get('loan_id')), masterLoanId: GL.int(GL.get('master_loan_id')),\n"
                 "  loanAmountDigi: GL.money(GL.get('final_loan_amount'))\n"
                 "});"),
            test="GL.ok('ops-rating');\nGL.role('appraiser');",
            json_body=True),
    ])


def folder_disbursement():
    items = [
        req("DISB: update loan lock (PARTNER)", "POST", "/api/loan-process/update-loan-lock",
            pre=("GL.role('partner');\n"
                 "GL.body({ masterLoanId: GL.int(GL.get('master_loan_id')), loanStageId: 8 });"),
            test="GL.soft('update-loan-lock (partner)');",
            json_body=True),
        req("DISB: disbursement bank detail", "GET",
            "/api/loan-process/disbursement-loan-bank-detail?loanId={{loan_id}}&masterLoanId={{master_loan_id}}",
            pre="GL.role('partner'); GL.sign();",
            test=("GL.ok('disbursement-loan-bank-detail');\n"
                  "// This response supplies almost every field partner-wise-disbursement needs.\n"
                  "var d = GL.json().data || {};\n"
                  "GL.setJson('disb_detail', d);\n"
                  "if (!GL.get('loan_unique_id') && d.securedLoanUniqueId) {\n"
                  "  GL.set('loan_unique_id', d.securedLoanUniqueId);\n"
                  "}\n"
                  "GL.log('disb finalLoanAmount', d.finalLoanAmount, 'unique', GL.get('loan_unique_id'));")),
        req("DISB: validate account (PARTNER)", "POST", "/api/loan-process/validate-account",
            pre=("GL.role('partner');\n"
                 "var p = GL.get('passbook_proof');\n"
                 "GL.body({\n"
                 "  ifscCode: GL.get('bank_ifsc_code'), accountNumber: GL.get('bank_account_number'),\n"
                 "  accountHolderName: GL.get('account_holder_name'), bankName: GL.get('bank_name'),\n"
                 "  bankBranchName: GL.get('bank_branch_name'),\n"
                 "  passbookProof: [p], passbookProofImage: [GL.fullUrl(p)],\n"
                 "  passbookProofImageName: [], detailsFor: 'customer',\n"
                 "  detailsForId: GL.int(GL.get('customer_id'))\n"
                 "});"),
            test="GL.soft('validate-account (partner)');",
            json_body=True),
        req("DISB: partner approval", "POST", "/api/loan-process/partner-disbursement-status",
            pre=("GL.role('partner');\n"
                 "var d = GL.getJson('disb_detail', {});\n"
                 "var amt = d.securedLoanAmount || GL.money(GL.get('final_loan_amount'));\n"
                 "GL.body({\n"
                 "  applicationFormForAppraiser: true, goldValuationForAppraiser: true,\n"
                 "  commentByAppraiser: null, applicationFormForBM: true, goldValuationForBM: true,\n"
                 "  reasons: '', applicationFormForOperatinalTeam: true,\n"
                 "  goldValuationForOperatinalTeam: true, applicationFormForPartner: true,\n"
                 "  goldValuationForPartner: true, loanStatusForPartner: 'approved',\n"
                 "  scrapStatusForAppraiser: null, scrapStatusForBM: null,\n"
                 "  scrapStatusForOperatinalTeam: 'pending', packetApprovalByOps: null,\n"
                 "  commentByOpsForPacketApproval: '', statusForRh: 'pending', commentByRh: '',\n"
                 "  applicationFormForRh: false, goldValuationForRh: false,\n"
                 "  loanId: GL.int(GL.get('loan_id')), masterLoanId: GL.int(GL.get('master_loan_id')),\n"
                 "  loanAmountDigi: amt, loanType: 'Fresh Loan',\n"
                 "  partnerId: GL.int(GL.get('partner_id'), 152), checkPointers: {}\n"
                 "});"),
            test=("// Tolerated: the ops rating usually already moved the loan to 'disbursement\n"
                  "// pending' -- which is exactly the state disbursement needs.\n"
                  "if (pm.response.code >= 400 &&\n"
                  "    pm.response.text().toLowerCase().indexOf('disbursement pending') >= 0) {\n"
                  "  pm.test('partner approval (already at disbursement pending)', function () {\n"
                  "    pm.expect(true).to.be.true;\n"
                  "  });\n"
                  "} else {\n"
                  "  GL.ok('partner approval');\n"
                  "}"),
            json_body=True),
        req("DISB: partner-wise disbursement", "POST", "/api/loan-process/partner-wise-disbursement",
            pre=("GL.role('partner');\n"
                 "var d = GL.getJson('disb_detail', {});\n"
                 "var ubd = d.userBankDetail || {};\n"
                 "var securedAmount = d.securedLoanAmount || GL.money(GL.get('final_loan_amount'));\n"
                 "var finalAmount = (d.finalLoanAmount !== undefined && d.finalLoanAmount !== null)\n"
                 "  ? d.finalLoanAmount : GL.num(GL.get('final_loan_amount'));\n"
                 "var table = GL.getJson('interest_table', []);\n"
                 "var firstInterest = (table.length && table[0].securedInterestAmount !== undefined)\n"
                 "  ? String(table[0].securedInterestAmount) : '0.00';\n"
                 "var pick = function (k, dflt) {\n"
                 "  return (d[k] === undefined || d[k] === null) ? dflt : d[k];\n"
                 "};\n"
                 "GL.body({\n"
                 "  isNewLoanFromRenew: false, isTopUpAdded: false,\n"
                 "  loanId: GL.int(GL.get('loan_id')) || d.securedLoanId,\n"
                 "  securedCashTransactionId: null, securedBankTransactionId: GL.utrNumber(),\n"
                 "  securedAmountDisbursedByCash: null, securedAmountDisbursedByBank: securedAmount,\n"
                 "  securedCashReceiptImage: null, unsecuredCashReceiptImage: null,\n"
                 "  securedCashReceiptPath: null, unsecuredCashReceiptPath: null,\n"
                 "  unsecuredCashTransactionId: null, unsecuredBankTransactionId: null,\n"
                 "  unsecuredAmountDisbursedByCash: null, unsecuredAmountDisbursedByBank: null,\n"
                 "  securedTransactionId: null, unsecuredTransactionId: null,\n"
                 "  date: GL.nowIsoZ(), paymentMode: ['bank'],\n"
                 "  loanAmount: finalAmount, otp: null,\n"
                 "  maxAmountToBeDisbursedByCash: pick('maxAmountToBeDisbursedByCash', 1000000000),\n"
                 "  bankArray: [{\n"
                 "    disbursementStatus: 'Disbursed to Customer',\n"
                 "    ifscCode: ubd.ifscCode || GL.get('bank_ifsc_code'),\n"
                 "    bankName: ubd.bankName || GL.get('bank_name'),\n"
                 "    bankBranch: ubd.bankBranchName || GL.get('bank_branch_name'),\n"
                 "    accountHolderName: ubd.accountHolderName || GL.get('account_holder_name'),\n"
                 "    accountNumber: ubd.accountNumber || GL.get('bank_account_number')\n"
                 "  }],\n"
                 "  masterLoanId: String(GL.get('master_loan_id')),\n"
                 "  securedSchemeName: pick('securedSchemeName', null),\n"
                 "  unsecuredLoanAmount: pick('unsecuredLoanAmount', 0), unsecuredSchemeName: null,\n"
                 "  securedLoanAmount: securedAmount,\n"
                 "  securedLoanId: pick('securedLoanId', GL.int(GL.get('loan_id'))),\n"
                 "  unsecuredLoanId: null, scrapId: null, scrapAmount: null, transactionId: null,\n"
                 "  fullSecuredAmount: pick('fullSecuredAmount', finalAmount),\n"
                 "  fullUnsecuredAmount: pick('fullUnsecuredAmount', 0),\n"
                 "  processingCharge: pick('processingCharge', 0),\n"
                 "  securedProcessingCharge: pick('securedProcessingCharge', '0.00'),\n"
                 "  unsecuredProcessingCharge: pick('unsecuredProcessingCharge', 0),\n"
                 "  isUnsecuredSchemeApplied: pick('isUnsecuredSchemeApplied', false),\n"
                 "  securedLoanUniqueId: pick('securedLoanUniqueId', GL.get('loan_unique_id')),\n"
                 "  unsecuredLoanUniqueId: null,\n"
                 "  finalAmount: finalAmount, fullAmount: finalAmount, bankTransferType: 'neft',\n"
                 "  loanTransferExtraAmount: null, otherAmountTransactionId: null, utrNumber: null,\n"
                 "  totalManualCharges: pick('totalManualCharges', 0),\n"
                 "  upfrontInterestAmount: pick('upfrontInterestAmount', 0),\n"
                 "  upfrontTransactionId: null, upfrontReceiptImage: null, upfrontReceipt: null,\n"
                 "  fullUnsecuredTopUpAmount: 0, fullSecuredTopUpAmount: 0, finalTopUpAmount: 0,\n"
                 "  securedTopUpAmount: 0, unsecuredTopUpAmount: 0,\n"
                 "  advanceBankTransfer: 0, advanceCash: 0, bTMoney: 0, penalInterest: 2,\n"
                 "  advancedCashReceiptImage: null, advanceCashDeclarationImage: null,\n"
                 "  receiptNumber: null, appraiserCharges: 0,\n"
                 "  actualProcessingCharge: firstInterest, actualCashGiven: 0,\n"
                 "  stampDutyCharges: '0.00', stampDutyRemarks: '', paymentGatewayId: 5,\n"
                 "  partnerId: GL.int(GL.get('partner_id'), 152), loanType: 'Fresh Loan'\n"
                 "});"),
            test=("GL.ok('partner-wise-disbursement');\n"
                  "GL.log('disbursement:', (GL.json() || {}).message);\n"
                  "GL.role('appraiser');"),
            json_body=True),
    ]
    return folder("12 - Disbursement (partner login)", items)


def folder_submit_packet():
    items = [
        req("SUB: update loan lock", "POST", "/api/loan-process/update-loan-lock",
            pre=("GL.role('appraiser');\n"
                 "GL.body({ masterLoanId: GL.int(GL.get('master_loan_id')), loanStageId: 8 });"),
            test="GL.soft('update-loan-lock (submit)');",
            json_body=True),
        req("SUB: view packets", "GET",
            "/api/packet-tracking/view-packets?masterLoanId={{master_loan_id}}",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('view-packets');\n"
                  "var out = [];\n"
                  "(GL.json().data || []).forEach(function (g) {\n"
                  "  (g.packets || []).forEach(function (p) {\n"
                  "    var bc = p.barcodeNumber || p.packetUniqueId;\n"
                  "    if (bc) { out.push({ Barcode: String(bc).toUpperCase(), packetId: bc }); }\n"
                  "  });\n"
                  "});\n"
                  "pm.test('packets found for the loan', function () { pm.expect(out.length).to.be.above(0); });\n"
                  "GL.setJson('submit_barcodes', out);")),
        req("SUB: packet locations", "GET", "/api/packet-location?search=&from=1&to=-1",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('packet-location');\n"
                  "// Resolve 'partner branch in' BY NAME so the id holds on any environment.\n"
                  "var want = String(GL.get('packet_location_name', 'partner branch in')).toLowerCase();\n"
                  "var hit = (GL.json().data || []).filter(function (l) {\n"
                  "  return String(l.location || '').trim().toLowerCase() === want;\n"
                  "})[0];\n"
                  "GL.set('packet_location_id', hit ? hit.id : GL.get('packet_location_id', '4'));\n"
                  "GL.log('packet location', GL.get('packet_location_id'));")),
        req("SUB: partner location detail", "GET",
            "/api/packet-tracking/get-particular-location"
            "?packetLocationId={{packet_location_id}}&masterLoanId={{master_loan_id}}",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('get-particular-location');\n"
                  "var d = GL.json().data || {};\n"
                  "GL.set('submit_partner_id', d.id || GL.get('partner_id'));\n"
                  "GL.set('submit_partner_name', d.name || 'Roshan Partner');\n"
                  "// The branch id is env-specific -- take it from the partner record.\n"
                  "var branches = d.partnerBranch || [];\n"
                  "GL.set('partner_branch_id',\n"
                  "       GL.get('partner_branch_id_override') ||\n"
                  "       (branches.length ? branches[0].id : GL.get('partner_branch_id', '146')));\n"
                  "GL.log('partner branch', GL.get('partner_branch_id'));")),
        req("SUB: resolve partner user", "GET",
            "/api/packet-tracking/user-name?mobileNumber={{partner_user_mobile}}"
            "&receiverType=PartnerUser&partnerBranchId={{partner_branch_id}}"
            "&masterLoanId={{master_loan_id}}&allUsers=1",
            pre=("GL.role('appraiser');\n"
                 "if (!GL.get('partner_user_mobile')) {\n"
                 "  throw new Error('No partner-branch user mobile for partner ' +\n"
                 "                  GL.get('partner_key') + ' in this environment.');\n"
                 "}\n"
                 "GL.sign();"),
            test=("GL.ok('partner user-name');\n"
                  "var d = GL.json().data || {};\n"
                  "GL.set('partner_receiver_id', d.id || '');\n"
                  "GL.set('partner_user_name',\n"
                  "       ((d.firstName || '') + ' ' + (d.lastName || '')).trim());\n"
                  "pm.test('partner user resolved', function () { pm.expect(!!d.id).to.be.true; });")),
        req("SUB: partner-user OTP send", "POST", "/api/partner-user-otp/send-otp",
            pre=("GL.role('appraiser');\n"
                 "GL.body({\n"
                 "  mobileNumber: GL.get('partner_user_mobile'),\n"
                 "  id: GL.int(GL.get('logged_in_user_id')),\n"
                 "  type: 'updateLocationCollect',\n"
                 "  masterLoanId: GL.int(GL.get('master_loan_id'))\n"
                 "});"),
            test=("GL.ok('partner-user send-otp');\n"
                  "GL.set('partner_otp_ref', GL.json().referenceCode);"),
            json_body=True),
        req("SUB: partner-user OTP verify", "POST", "/api/partner-user-otp/verify-otp",
            pre=("GL.role('appraiser');\n"
                 "GL.body({ otp: '1234', referenceCode: GL.get('partner_otp_ref'),\n"
                 "          type: 'updateLocationCollect' });"),
            test="GL.ok('partner-user verify-otp');",
            json_body=True),
        req("SUB: submit packet location", "POST", "/api/packet-tracking/submit-packet-location",
            pre=("GL.role('appraiser');\n"
                 "GL.body({\n"
                 "  packetLocationId: String(GL.get('packet_location_id')),\n"
                 "  barcodeNumber: GL.getJson('submit_barcodes', []),\n"
                 "  mobileNumber: GL.get('partner_user_mobile'),\n"
                 "  user: GL.get('partner_user_name'), receiverType: 'PartnerUser',\n"
                 "  otp: '1234', referenceCode: GL.get('partner_otp_ref'),\n"
                 "  userReceiverId: null, customerReceiverId: null,\n"
                 "  partnerReceiverId: GL.int(GL.get('partner_receiver_id')),\n"
                 "  loanId: GL.int(GL.get('loan_id')), masterLoanId: GL.int(GL.get('master_loan_id')),\n"
                 "  partnerId: GL.int(GL.get('submit_partner_id')),\n"
                 "  partnerName: GL.get('submit_partner_name'),\n"
                 "  partnerBranchId: String(GL.get('partner_branch_id')),\n"
                 "  internalBranchId: null, deliveryPacketLocationId: null,\n"
                 "  deliveryInternalBranchId: null, deliveryPartnerBranchId: null,\n"
                 "  deliveryPartnerName: null, id: null, releaseId: null, role: null,\n"
                 "  packetTransferId: null, partnerBranch: null, partnerBranchUserName: null,\n"
                 "  customerHandOver: null, customerAcknowledgement: null,\n"
                 "  isAuction: null, auctionDocuments: null\n"
                 "});"),
            test=("GL.ok('submit-packet-location');\n"
                  "GL.log('submit packet:', (GL.json() || {}).message);"),
            json_body=True),
    ]
    return folder("13 - Submit packet (appraiser)", items,
                  "Hands the sealed packet to the partner branch. Location and branch are resolved "
                  "live so this works on both environments.")


def folder_loan_details():
    items = [
        req("END: loan details list", "GET", "/api/loan-process/loan-details?from=1&to=25",
            pre="GL.role('appraiser'); GL.sign();", test="GL.soft('loan-details list');"),
        req("END: loan details by unique id", "GET",
            "/api/loan-process/loan-details?from=1&to=25&loanUniqueId={{loan_unique_id}}",
            pre="GL.role('appraiser'); GL.sign();",
            test=("GL.ok('loan-details search');\n"
                  "var loan = (GL.json().data || [])[0] || {};\n"
                  "var stage = loan.loanStage || {};\n"
                  "var c = loan.customer || {};\n"
                  "GL.log('=== LOAN COMPLETE ===');\n"
                  "GL.log('loanUniqueId   :', GL.get('loan_unique_id'));\n"
                  "GL.log('stage          :', stage.name, '(id ' + stage.id + ')');\n"
                  "GL.log('finalLoanAmount:', loan.finalLoanAmount);\n"
                  "GL.log('tenure         :', loan.tenure, loan.loanStartDate, '->', loan.loanEndDate);\n"
                  "GL.log('customer       :', (c.firstName || '') + ' ' + (c.lastName || ''),\n"
                  "       c.customerUniqueId, c.mobileNumber);\n"
                  "pm.test('loan reached \"packet submitted\"', function () {\n"
                  "  pm.expect(String(stage.name || '').toLowerCase()).to.equal('packet submitted');\n"
                  "});")),
    ]
    return folder("14 - Load loan details", items,
                  "Final check: the loan must end at stage 13 'packet submitted'.")


# --------------------------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------------------------
def build_collection(lib_source, dummy_data_uri):
    # Embed the library as a JSON string literal, NOT a template literal: a template literal
    # would consume the backslashes and silently destroy every regex in the library.
    collection_pre = [
        "// Publishes the shared GL helper library (tools/postman_lib.js) to a collection variable;",
        "// every request script starts with: eval(pm.collectionVariables.get(\"GL_LIB\"));",
        "pm.collectionVariables.set(\"GL_LIB\", " + json.dumps(lib_source) + ");",
    ]

    variables = [
        # --- knobs the user sets ---
        ("flow_mode", "new", "new = create a customer + run KYC; existing = reuse customer_unique_id"),
        ("customer_unique_id", "", "Required when flow_mode=existing (e.g. MS35QNJP)"),
        ("loan_amount", "400000", "Requested loan amount. Above 500000 adds the BM-approval step."),
        ("partner_key", "152", "Lending partner: 152 = Roshan Partner, 10 = Arvog"),
        ("partner_name", "ROSHAN PARTNER", "Partner name matched in partner-scheme-amount"),
        ("scheme_id_pin", "", "Pin one scheme id (e.g. 853); empty = auto-pick within the partner"),
        ("co_lender", "", "Co-lender bank name or id; empty = no co-lending"),
        # --- fixed config ---
        ("hmac_secret", GoldLoanApiTest().HMAC_SECRET, "Request-signing secret"),
        ("kyc_type", "RE_KYC", ""),
        ("lead_source", "Abhi testAppraiser", ""),
        ("internal_branch_id", "1", "Overwritten from the appraiser JWT at login"),
        ("packet_location_name", "partner branch in", "Resolved to an id at runtime"),
        ("packet_location_id", "4", "Fallback only"),
        ("partner_branch_id", "146", "Fallback only; resolved from the partner record"),
        ("partner_branch_id_override", "", "Force a partner branch id"),
        # --- test bank account (penny-drop verified) ---
        ("bank_name", "STATE BANK OF INDIA", ""),
        ("bank_account_number", "00000036150491589", ""),
        ("bank_ifsc_code", "SBIN0011777", ""),
        ("account_holder_name", "LIC MUTUAL FUND", ""),
        ("bank_branch_name", "SBI CAPITAL MARKET BRANCH, MUMBAI", ""),
        # --- embedded asset ---
        ("dummy_image_b64", dummy_data_uri, "Base64 packet image for /api/upload-file/base"),
        # --- runtime ---
        ("GL_LIB", "", "Set by the collection pre-request script"),
        ("__body", "", "Set by each request's pre-request script"),
        ("token", "", ""),
    ]
    for name in ("token_appraiser", "token_admin", "token_ops", "token_bm", "token_partner",
                 "customer_id", "customer_mobile", "first_name", "last_name", "pan_card_number",
                 "pan_type", "pan_image", "pin_code", "state_id", "city_id", "module_id",
                 "status_id", "reference_code", "customer_kyc_id", "customer_kyc_personal_id",
                 "kyc_status", "profile_image", "signature_proof", "signature_file_name",
                 "masked_identity_proof", "unmasked_identity_proof", "address_proof",
                 "address_proof_type_id", "identity_proof_number", "name_as_per_aadhaar",
                 "latitude", "longitude", "gender", "dob", "age", "mother_name", "spouse_name",
                 "martial_status", "occupation_id", "religion_id", "physical_challenge_id",
                 "political_exposed_id", "special_category_id", "cis_id", "bsr_id",
                 "annual_income", "qualification_id", "appraiser_id", "appraiser_name",
                 "appraiser_mobile", "appraiser_request_id", "loan_id", "master_loan_id",
                 "purpose", "nominee_relation", "gold_rate", "karat", "ornament_image",
                 "scheme_id", "partner_id", "co_lender_bank_id", "forced_co_lender_id",
                 "secured_rpg", "secured_ltv", "secured_processing_charge",
                 "upfront_interest_amount", "interest_rate", "secured_exposure",
                 "secured_rebate_interest", "tenure", "loan_start_date", "loan_end_date",
                 "final_loan_amount", "total_eligible_amount", "total_final_interest_amt",
                 "bank_account_verified", "passbook_proof", "packet_id", "packet_unique_id",
                 "loan_unique_id", "lead_converter_id", "partner_user_mobile",
                 "partner_receiver_id", "partner_user_name", "partner_otp_ref",
                 "submit_partner_id", "submit_partner_name", "scheme_idx",
                 "logged_in_user_id", "logged_in_mobile_number", "mobile_partner",
                 # OTP reference codes, per role
                 "ref_appraiser", "ref_admin", "ref_ops", "ref_bm", "ref_partner",
                 "kyc_reference_code", "partner_branch_id_resolved",
                 # JSON blobs carried between requests
                 "kyc_personal", "kyc_addresses", "ornaments", "ornament_types", "karat_detail",
                 "scheme_catalog", "scheme_candidates", "interest_table", "disb_detail",
                 "submit_barcodes",
                 # uploaded artefact paths
                 "packet_img_empty", "packet_img_weight", "packet_img_customer",
                 "packet_img_sealed", "doc_loan_agreement", "doc_pawn_copy",
                 "doc_scheme_confirmation", "doc_income"):
        variables.append((name, "", ""))

    return {
        "info": {
            "_postman_id": "aug-gl-e2e-0001",
            "name": "Augmont Gold Loan - E2E (TEST/UAT)",
            "description": (
                "End-to-end Augmont Gold Loan journey, ported from src/maintest.py.\n\n"
                "Pick the **Augmont GL - TEST** or **Augmont GL - UAT** environment and run the "
                "whole collection with the Collection Runner (or newman). It logs in every role, "
                "creates a customer, runs KYC, books the loan, seals the packet, disburses and "
                "submits the packet, ending at loan stage 13 'packet submitted'.\n\n"
                "IMPORTANT: file uploads use relative paths under assets/, so set the Postman "
                "**working directory to the repository root** (Settings -> General -> Working "
                "directory), or run newman with --working-dir. Requests must run IN ORDER: the "
                "flow chains ids through collection variables and uses setNextRequest for the "
                "new/existing-customer branch, the scheme retry loop and the >5L BM step.\n\n"
                "Generated by tools/build_postman_collection.py - edit that (and "
                "tools/postman_lib.js), not this file."
            ),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": [
            folder_logins(),
            folder_master(),
            folder_new_customer(),
            folder_existing_customer(),
            folder_kyc(),
            folder_existing_prep(),
            folder_loan_basics(),
            folder_ornaments(),
            folder_scheme(),
            folder_bank(),
            folder_packet(),
            folder_bm(),
            folder_documents(),
            folder_ops(),
            folder_disbursement(),
            folder_submit_packet(),
            folder_loan_details(),
        ],
        "event": [{"listen": "prerequest",
                   "script": {"type": "text/javascript", "exec": collection_pre}}],
        "variable": [
            {"key": k, "value": v, "type": "string",
             **({"description": d} if d else {})}
            for k, v, d in variables
        ],
    }


def build_environment(env_key, env_cfg):
    """One Postman environment per harness environment profile."""
    values = [
        ("env_name", env_key.upper()),
        ("base_url", env_cfg["base_url"]),
        ("mobile_appraiser", env_cfg["role_mobiles"]["appraiser"]),
        ("mobile_admin", env_cfg["role_mobiles"]["admin"]),
        ("mobile_ops", env_cfg["role_mobiles"]["ops"]),
        ("mobile_bm", env_cfg["role_mobiles"]["bm"]),
    ]
    for pid, mob in sorted(env_cfg["partner_mobiles"].items()):
        values.append(("partner_mobile_%s" % pid, mob))
    for pid, mob in sorted(env_cfg["partner_user_mobiles"].items()):
        values.append(("partner_user_mobile_%s" % pid, mob))
    return {
        "id": "aug-gl-env-%s" % env_key,
        "name": "Augmont GL - %s" % env_key.upper(),
        "values": [{"key": k, "value": v, "type": "default", "enabled": True} for k, v in values],
        "_postman_variable_scope": "environment",
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    with open(LIB_PATH, "r", encoding="utf-8") as fh:
        lib_source = fh.read()

    dummy = os.path.join(ROOT, "assets", "dummy_image.png")
    mime = mimetypes.guess_type(dummy)[0] or "image/png"
    with open(dummy, "rb") as fh:
        data_uri = "data:%s;base64,%s" % (mime, base64.b64encode(fh.read()).decode("ascii"))

    collection = build_collection(lib_source, data_uri)
    col_path = os.path.join(OUT_DIR, "Augmont-GoldLoan-E2E.postman_collection.json")
    with open(col_path, "w", encoding="utf-8") as fh:
        json.dump(collection, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    written = [col_path]
    for env_key, env_cfg in GoldLoanApiTest.ENVIRONMENTS.items():
        path = os.path.join(OUT_DIR, "Augmont-GL-%s.postman_environment.json" % env_key.upper())
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(build_environment(env_key, env_cfg), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        written.append(path)

    count = sum(len(f["item"]) for f in collection["item"])
    print("Wrote %d requests across %d folders." % (count, len(collection["item"])))
    for p in written:
        print("  " + os.path.relpath(p, ROOT).replace("\\", "/"))


if __name__ == "__main__":
    main()
