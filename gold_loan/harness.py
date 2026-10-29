# gold_loan/harness.py -- the Gold Loan API harness (GoldLoanApiTest) and the full-loan CLI.

import httpx
import hmac
import hashlib
import json
import random
import datetime
import os
import sys
import urllib.parse
import asyncio
import re  # Import regex module
import jwt  # Import jwt library for decoding
import mimetypes
import subprocess
from decimal import Decimal, ROUND_HALF_UP


class KycDocumentAlreadyExistsError(Exception):
    """A KYC document (PAN / Aadhaar / OVD) is already registered against another customer.

    This ABORTS the run instead of degrading to the manual data path. Retrying cannot help -- the
    number is taken in the database -- and silently continuing would turn a real-identity test into
    a dummy-data one that reports success, hiding the fact that the document was never usable.
    Fix the data (use an unregistered document, or point the run at the customer that already owns
    it with --customer) and re-run.
    """


class GoldLoanApiTest:

    # --- Environment profiles -----------------------------------------------------------------
    # The harness runs against either TEST (gfat) or UAT (gfau). EVERYTHING that differs between
    # the two lives here -- the API host and the login mobiles of every role the flow hands the
    # loan to. Pick with `--env {test|uat}` or GOLD_LOAN_ENV (default: test).
    #   role_mobiles         -- internal staff logins (static OTP 1234).
    #   partner_mobiles      -- partner login used for approval + disbursement, keyed by partner id
    #                           (Roshan Partner = 152, Arvog = 10).
    #   partner_user_mobiles -- partner-BRANCH user who receives the packet at submit-packet, keyed
    #                           by the same partner id (TEST Arvog: 'newbranch user' 7777777788).
    # Anything else that is environment-specific (packet location id, partner branch id) is
    # resolved live from the API in submit_packet rather than pinned per environment.
    ENVIRONMENTS = {
        "test": {
            "base_url": "https://gold-loan-backend-api.gfat.augmont.com",
            # The web app's origin, sent as the Origin header on every call: without it the
            # server answers {"message": "This request is not permitted."}.
            "origin": "https://ap.gfat.augmont.com",
            "role_mobiles": {"admin": "8880008880", "appraiser": "8880008881",
                             "ops": "8880008882", "bm": "8880008883"},
            "partner_mobiles": {"152": "8767002003", "10": "9375876473"},  # ROSHAN / ARVOG ARV10
            "partner_user_mobiles": {"152": "8652849318",   # roshan ghadge (id 170)
                                     "10": "7777777788"},   # arvog: newbranch user
        },
        "uat": {
            "base_url": "https://gold-loan-backend-api.gfau.augmont.com",
            "origin": "https://ap.gfau.augmont.com",
            "role_mobiles": {"admin": "9990009990", "appraiser": "9990009991",
                             "ops": "9990009995", "bm": "9990009993"},
            "partner_mobiles": {"152": "8767002002", "10": "8846651348"},  # ROSHAN PARTNER / ARVOG ARV10
            "partner_user_mobiles": {"152": "8652849318",   # ROSHAN GHADGE
                                     "10": "8888888880"},   # ARVOG Qa Test
        },
    }
    DEFAULT_ENV = "test"

    # --- KYC verification modes ---------------------------------------------------------------
    # How the customer's identity is established. Chosen with --kyc / GOLD_LOAN_KYC_MODE.
    #   manual -- generated dummy PAN + Aadhaar, no verification calls. The long-standing default;
    #             relies on the server's manual-validation fallback, which accepts any number.
    #   auto   -- REAL PAN and REAL offline-Aadhaar XML, verified through /api/e-kyc/verify-pan
    #             and /api/e-kyc/offline-aadhaar-xml.
    #   ovd    -- REAL PAN plus a REAL Officially Valid Document (driving licence / voter id /
    #             passport / NREGA) instead of Aadhaar -- the CR-2 "PAN & Other OVD" path.
    # auto and ovd need genuine identity data, which cannot be invented: the verification APIs
    # check the numbers against the issuing authority. See _load_kyc_profile for how it is supplied.
    KYC_MODE_MANUAL = "manual"
    KYC_MODE_AUTO = "auto"
    KYC_MODE_OVD = "ovd"
    KYC_MODES = (KYC_MODE_MANUAL, KYC_MODE_AUTO, KYC_MODE_OVD)
    # The SERVER counts consecutive verification failures and flips `switchToManual` to true on the
    # third ("pan API failed 3 time(s) in a row -> switching to manual KYC"). We retry to the same
    # ceiling so the harness reproduces the real UI's behaviour rather than inventing its own.
    KYC_VERIFY_MAX_ATTEMPTS = 3
    # OVD catalogue, from ovd.har. Per OVD: the string submit-basic-info expects in `ovdType`, the
    # /api/identity-type id, the verification endpoint, the field the number goes in, and whether
    # that endpoint also wants a date of birth.
    #   Voter ID       -- fully capture-confirmed (verify-voter-id 200, submit-basic-info 200).
    #   Driving Licence-- endpoint + body confirmed (verify-dl); its `ovdType` STRING is inferred
    #                     from the identity-type name "Driving License" with the space removed,
    #                     the same rule that turns "Voter ID" into the confirmed "VoterID". The
    #                     captured DL call 400'd ("already registered with another customer"), so
    #                     submit-basic-info was never exercised with DL. Override with
    #                     GOLD_LOAN_KYC_OVD_TYPE_STRING if the server disagrees.
    # Passport / NREGA appear in CR-2 but have no captured endpoint, so they are not offered here
    # rather than being guessed.
    OVD_CATALOG = {
        "voterId": {"ovdType": "VoterID", "identityTypeId": 2,
                    "path": "/api/e-kyc/v2/verify-voter-id", "number_field": "epicNo",
                    "needs_dob": False, "confirmed": True},
        "drivingLicense": {"ovdType": "DL", "identityTypeId": 3,   # portal _ovdTypeMap: dl -> 'DL'
                           "path": "/api/e-kyc/v2/verify-dl", "number_field": "dlNo",
                           "needs_dob": True, "confirmed": False},
    }
    OVD_TYPES = tuple(OVD_CATALOG)
    # panType on submit-basic-info: 'pan' and 'form60' are the pre-existing values; 'ovd' is what
    # the OVD path sends (captured), alongside panCardNumber=null and a Form 97 upload.
    PAN_TYPES = ("pan", "form60", "ovd")
    # Real identity fields, and the env var that overrides each one from the profile file.
    KYC_PROFILE_ENV_KEYS = {
        "firstName": "GOLD_LOAN_KYC_FIRST_NAME",
        "lastName": "GOLD_LOAN_KYC_LAST_NAME",
        "dateOfBirth": "GOLD_LOAN_KYC_DOB",              # YYYY-MM-DD
        "gender": "GOLD_LOAN_KYC_GENDER",                # m | f | o
        "panCardNumber": "GOLD_LOAN_KYC_PAN",
        "aadhaarNumber": "GOLD_LOAN_KYC_AADHAAR",        # 12 digits or just the last 4: only the last 4 must match the XML
        "aadhaarXmlPath": "GOLD_LOAN_KYC_AADHAAR_XML",   # UIDAI offline e-KYC XML on disk
        "ovdType": "GOLD_LOAN_KYC_OVD_TYPE",
        "ovdNumber": "GOLD_LOAN_KYC_OVD_NUMBER",
        "ovdImagePath": "GOLD_LOAN_KYC_OVD_IMAGE",
        "form97Path": "GOLD_LOAN_KYC_FORM97",
    }
    # Fields each mode cannot run without. OVD is the CR-2 "PAN not available" path -- the captured
    # submit-basic-info sends panCardNumber=null / isPanVerified=false -- so no PAN is required.
    # DL additionally needs dateOfBirth; that is enforced per-OVD in _validate_kyc_profile.
    KYC_MODE_REQUIRED_FIELDS = {
        KYC_MODE_MANUAL: (),
        KYC_MODE_AUTO: ("panCardNumber", "dateOfBirth", "aadhaarNumber", "aadhaarXmlPath"),
        KYC_MODE_OVD: ("ovdType", "ovdNumber"),
    }

    def __init__(self):
        # --- Configuration ---
        # Environment selection first: it supplies the base URL and every role login mobile.
        # GOLD_LOAN_BASE_URL still wins outright if someone points the run at another host.
        self.env_name = (os.getenv("GOLD_LOAN_ENV") or self.DEFAULT_ENV).strip().lower()
        if self.env_name not in self.ENVIRONMENTS:
            raise ValueError(f"Unknown environment '{self.env_name}'. "
                             f"Choose one of {sorted(self.ENVIRONMENTS)} "
                             f"(--env / GOLD_LOAN_ENV).")
        self.env = self.ENVIRONMENTS[self.env_name]
        self.BASE_URL = os.getenv(
            "GOLD_LOAN_BASE_URL", self.env["base_url"]
        ).rstrip("/")
        self.ORIGIN = os.getenv("GOLD_LOAN_ORIGIN", self.env["origin"]).rstrip("/")
        self.HMAC_SECRET = os.getenv(
            "GOLD_LOAN_HMAC_SECRET",
            "3056301006072a8648ce3d020106052b8104000a0342000499c5f442c3264bcdfb093b0bc820e3f0f6546972856ebec2f8ccc03f49abdb47ffcfcaf4f37e0ec53050760e74014767e30a8a3e891f4db8c83fa27627898f15",
        )
        # Upload assets live in <project-root>/assets. Anchor to the project root (two levels up from
        # this file) so paths resolve no matter what directory the harness is launched from.
        _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _assets = os.path.join(_project_root, "assets")
        self.DUMMY_IMAGE_PATH = os.path.join(_assets, "dummy_image.png")
        self.DUMMY_PDF_PATH = os.path.join(_assets, "CPV.pdf")  # real document for all PDF uploads (loan docs, income, CPV)
        # Real document images used for the matching uploads (more realistic than the dummy image).
        self.AADHAR_IMAGE_PATH = os.path.join(_assets, "AADHAR.png")             # KYC identity proof (aadhaar)
        self.PAN_IMAGE_PATH = os.path.join(_assets, "PAN.png")                   # KYC PAN card
        self.ORNAMENT_IMAGE_PATH = os.path.join(_assets, "scale.jpg")           # loan ornament image (gold on scale)
        self.CHEQUE_IMAGE_PATH = os.path.join(_assets, "cancelled-cheque-1.png")  # bank details (cancelled cheque)
        # Candidate customer payout accounts for the penny-drop (see _load_bank_accounts).
        self.BANK_ACCOUNTS_PATH = os.path.join("reference", "bank_accounts.json")
        self.DEFAULT_BANK_ACCOUNT = {"label": "SBI - LIC MUTUAL FUND POOL A/C.",
                                    "ifscCode": "SBIN0011777",
                                    "accountNumber": "00000036150491589"}

        self.log_http = os.getenv("GOLD_LOAN_HTTP_LOG", "true").lower() not in {"0", "false", "no"}
        # Console verbosity: quiet | normal | verbose. Back-compat: HTTP_LOG=false => quiet.
        self.log_level = os.getenv("GOLD_LOAN_LOG_LEVEL", "normal" if self.log_http else "quiet").lower()
        if self.log_level not in {"quiet", "normal", "verbose"}:
            self.log_level = "normal"
        self.use_unicode = self._init_unicode()
        self.use_color = self._init_color(os.getenv("GOLD_LOAN_COLOR", "auto").lower())
        # Glyph set: pretty Unicode when the console supports it, ASCII fallback otherwise.
        if self.use_unicode:
            self._G = {"bar": "═", "rule": "─", "corner": "┌─", "ok": "✓", "warn": "→",
                       "fail": "✗", "square": "■", "dot": "·", "pipe": "│", "ell": "…"}
        else:
            self._G = {"bar": "=", "rule": "-", "corner": "+-", "ok": "[OK]", "warn": "[->]",
                       "fail": "[X]", "square": "#", "dot": "-", "pipe": "|", "ell": "..."}
        # API-testing metrics, reported in the run summary.
        self._api_calls = 0
        self._api_failures = 0
        self._failed_endpoints = []  # (call#, method, path, status, reason) for the end-of-run report
        self._step_no = 0
        # httpx's default is a 5 s limit on every call; the servers regularly take longer (a live
        # run lost a loan to a ReadTimeout on an 8.7 KB upload-file/base). Wait like a browser does:
        # GOLD_LOAN_HTTP_TIMEOUT seconds for a reply (default 60), 15 s to connect.
        http_timeout = float(os.getenv("GOLD_LOAN_HTTP_TIMEOUT", "60") or 60)
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Origin": self.ORIGIN},
            timeout=httpx.Timeout(http_timeout, connect=min(15.0, http_timeout)),
            event_hooks={"response": [self._log_http_exchange]},
        )
        # --- Global Variables to store dynamic data ---
        self.auth_token = ""
        self.customer_id = ''
        self.customer_unique_id = ''
        self.mobile_number = ''
        self.module_id = ''
        self.state_id = ''
        self.city_id = ''
        self.first_name = ''
        self.last_name = ''
        self.customer_kyc_id = ''
        self.file_path = ''  # For uploaded files
        self.file_originalname = ''
        self.random_pan = ''
        self.pan_image = ''
        self.form60_image = ''
        self.pan_type = ''
        self.special_category_id = ''
        self.occupation_id = ''
        self.industry_category_id = None  # set when the occupation isIndustryRequired (random pick)
        self.industry_id = None
        self.religion_id = ''
        self.physical_challenge_id = ''
        self.political_exposed_id = ''
        self.annual_income = ''
        self.qualification_id = ''
        self.cis_id = ''
        self.bsr_id = ''
        self.profile_image = ''
        self.signature_proof = ''
        self.age = 0
        self.dob = ''
        self.gender = ''
        self.martial_status = ''
        self.spouse_name = ''
        self.mother_name = ''
        self.identity_proof_number = ''
        self.encrypted_identity_proof_number = ''
        self.name_as_per_aadhaar = ''
        self.masked_identity_proof = ''
        self.unmasked_identity_proof = ''
        self.address_proof = ''
        self.address_proof_type_id = ''
        self.latitude = None
        self.longitude = None
        self.customer_details = {}  # To store the full customer KYC review object
        # Server payloads later request bodies are filled from (instead of hard-coded values), as
        # the web app does: customer-loan-details.customerData feeds basic-details; the
        # final-loan-details response feeds bank-details; check-loan-type securedScheme feeds the
        # interest table.
        self.customer_loan_data = {}
        self.final_loan_details_data = {}
        self.secured_scheme = {}
        self.global_setting = {}   # GET /api/global-setting (LIP thresholds, LIP eligibility check, ...)
        self.customer_user_type = ''   # the customer record's userType (e.g. 'Individual'), from /api/customer
        self._user_details_by_token = {}  # verify-login userDetails per bearer token (state/city/branch of that user)
        self.ornaments_total_nwap = None  # ornaments-details response netWtAfterPurity (total), for eligibility
        self.basic_details_response = {}  # basic-details response: createdBy / internalBranchId / loanNomineeDetail
        # check-loan-type outputs the portal patches into the interest calculator
        self.secured_loan_amount = None
        self.secured_top_up_amount = 0
        self.unsecured_top_up_amount = 0
        self.manual_charges = []
        self.auto_assigned_co_lender_id = None  # check-loan-type's default co-lender (portal autoAssignedCoLenderBank)
        self.validated_customer_name = ''  # validate-account response customerName (penny-drop)
        self._aadhaar_identity_type_id = None  # GET /api/identity-type -> 'Aadhaar Card' id
        self.customer_branch_id = ''           # the customer record's internalBranch.id
        # Disbursement transfer mode: the portal's OFFLINE payout options IMPS / NEFT / RTGS (no direct
        # ICICI / RazorpayX access). --transfer-mode / GOLD_LOAN_TRANSFER_MODE, default NEFT.
        self.transfer_mode = (os.getenv("GOLD_LOAN_TRANSFER_MODE") or "neft").strip().lower()
        # --release-kyc-docs / GOLD_LOAN_RELEASE_KYC_DOCS: free the identity profile's REAL PAN (and full
        # Aadhaar) on TEST/UAT before KYC, so --kyc auto can register them on a new customer again.
        self.release_kyc_docs = os.getenv("GOLD_LOAN_RELEASE_KYC_DOCS", "").lower() in ("1", "true", "yes")
        # --record-har PATH / GOLD_LOAN_HAR_PATH: write every request/response of the run as a HAR 1.2
        # file (entry comment = the run step), e.g. to refresh reference/gold-loan-e2e-flow.har.
        self.har_path = os.getenv("GOLD_LOAN_HAR_PATH", "").strip()
        self._har_entries = []
        self._current_step = ""
        if self.transfer_mode not in self.TRANSFER_MODES:
            raise ValueError(f"Unknown transfer mode {self.transfer_mode!r}; use one of {self.TRANSFER_MODES}.")
        self.submit_all_kyc_response = {}      # submit-all-kyc-info response (feeds the ops approval)
        self.loan_type = ''            # the loan's loanType (basic-details response / single-loan masterLoan)
        self.lip_form = None       # the LIP assessment sent with loan-documents, when the loan needs one
        self.appraiser_id = ''
        self.appraiser_request_id = ''
        # An already-existing appraiser request is RESUMED by default; --fresh-request /
        # GOLD_LOAN_FRESH_APPRAISER_REQUEST=true cancels it and creates a new one instead.
        self.reuse_existing_request = os.getenv("GOLD_LOAN_FRESH_APPRAISER_REQUEST", "").lower() not in ("1", "true", "yes")
        self._existing_request_status = ''
        self._existing_process_complete = False
        self._existing_loan_completed = False
        self._existing_request_appraiser_id = None
        self._branch_attempts = []  # per-attempt results of the alternate-branch create ladder
        self.bank_account_label = ''
        # Accounts the penny-drop rejected THIS RUN, so they are not retried per loan.
        self._bank_accounts_rejected = set()
        self._bank_penny_drop_unavailable = False  # a full walk found nothing usable
        self._bank_rejection_reasons = {}          # accountNumber -> why it was flagged
        self.bank_retry_all_accounts = os.getenv("GOLD_LOAN_BANK_RETRY_ALL", "").lower() in ("1", "true", "yes")
        self._bank_status_loaded = False  # deferred: needs env_name + the account list
        # nginx 502/503/504 and transport blips: how many times a SAFE request is repeated.
        self.gateway_retries = int(os.getenv("GOLD_LOAN_GATEWAY_RETRIES", "3") or 1)
        self.gateway_retry_delay = float(os.getenv("GOLD_LOAN_GATEWAY_RETRY_DELAY", "2") or 0)
        self.bm_rating_submitted = False  # set by _submit_bm_rating; stops a duplicate on recovery
        # How many candidate payout accounts the penny-drop may walk (each one hits a real bank).
        self.bank_account_attempts = int(os.getenv("GOLD_LOAN_BANK_ACCOUNT_ATTEMPTS", "12") or 0)
        self.master_loan_id = ''
        self.loan_id = ''
        self.reference_code = ''
        self.random_pincode = ''
        self.lead_source = 'Abhi testAppraiser'
        self.status_id = ''  # For customer creation status
        self.nominee_relation_id = ''
        self.ornament_type_id = ''
        self.karat_id = ''
        self.purpose_id = ''  # the purpose NAME (basic-details sends the name, not the id)
        # Loan purpose selection (--purpose / --purpose-type). GOLD_LOAN_PURPOSE pins one purpose by
        # name; otherwise GOLD_LOAN_PURPOSE_TYPE picks a random purpose of that type: cb (Consumption
        # Based, the default), igl (Income Generating) or any.
        self.purpose_choice = os.getenv("GOLD_LOAN_PURPOSE", "").strip()
        self.purpose_type_choice = (os.getenv("GOLD_LOAN_PURPOSE_TYPE") or "cb").strip().lower()
        self.purpose_type = ''      # the chosen purpose's purposeType, sent with basic-details
        self.purpose_record = {}    # the chosen purpose as the server listed it
        self.scheme_id = ''
        self.selected_scheme = {}
        self.gold_rate = 0.0
        self.secured_ltv = 0.0
        self._diagnostics = {}  # captured responses for the loan-eligibility debug dump
        self.partner_id = ''
        self.co_lender_bank_id = ''
        # Loan-composition choices (partner / scheme / co-lender), settable via CLI flags or env.
        # GOLD_LOAN_PARTNER_NAME: which partner(s) to consider (comma list, matched by name).
        #   Defaults to Roshan first, then Arvog. e.g. "ARVOG" or "ROSHAN PARTNER".
        # GOLD_LOAN_SCHEME_ID: pin one scheme id (e.g. 853); empty = auto-pick within the partner.
        # GOLD_LOAN_CO_LENDER: co-lender bank name or id to apply (co-lending); empty = no co-lending.
        self.forced_scheme_id = os.getenv("GOLD_LOAN_SCHEME_ID", "").strip()
        self.co_lender_choice = os.getenv("GOLD_LOAN_CO_LENDER", "").strip()
        self.co_lender_interactive = False  # True => prompt the user to pick a co-lender from a menu
        self._forced_co_lender_id = None  # resolved bank id when co-lending is requested
        self._role_token_cache = {}  # mobile -> token, so a role (e.g. admin) logs in ONCE per run
                                     # (the OTP send-endpoint rate-limits repeat requests within a minute)
        self.loan_stage_id = ''
        self.loan_unique_id = ''  # e.g. AUGM-67273, captured at disbursement; used to search loan-details
        self.min_loan_amount = 0
        self.max_loan_amount = 0
        self.final_loan_amount = 0.0 # New instance variable
        self.total_eligible_amount = 0.0 # New instance variable
        self.loan_ornaments_details = [] # New instance variable
        self.total_final_interest_amt = 0.0 # New instance variable
        self.secured_rpg = 0.0 # New instance variable
        self.interest_table_data = [] # New instance variable
        # Loan-calculator params. These are PLACEHOLDERS only — they are identified via scheme
        # selection: check_loan_type() overwrites tenure/interestRate/processingCharge/
        # upfrontInterest/exposure/dates from the check-loan-type response (the scheme the server
        # picked for this amount), and generate_interest_table() sets securedRebateInterest. The
        # server recomputes final-loan-details with the scheme's real values and 400s if they differ,
        # so nothing here may reach final-loan-details un-refreshed by scheme selection.
        self.tenure_months = 0
        self.interest_rate = 0
        self.secured_processing_charge = 0
        self.upfront_interest_amount = 0
        # Stamp duty deducted from bank-details' toBePaid. The SERVER's figure (final-loan-details,
        # else single-loan) always wins, 0 included; GOLD_LOAN_STAMP_DUTY (default 0, as the portal) is only the fallback
        # when the server states none.
        self.stamp_duty_charges = float(os.getenv("GOLD_LOAN_STAMP_DUTY", "0"))
        self.stamp_duty_definition_type = None
        self.stamp_duty_from_server = False  # True once a server payload stated it (ARVOG needs this)
        self.secured_exposure = "0"
        self.secured_rebate_interest = 0
        self.loan_start_date = ""
        self.loan_end_date = ""
        self.max_loan_limit = 0.0  # From loan-process/max-loan-limit
        self.available_packet = {}  # From packet/available-packet
        self.packet_prepared = False  # True once prepare_packet generated this run's packet
        self.lead_converter_id = ''  # From lead/lead-converter
        self.karza_account_details = {}  # From loan-process/account-details-karza
        self.bank_account_number = ''  # Disbursement bank account details
        self.bank_ifsc_code = ''
        self.bank_name = ''
        self.bank_branch_name = ''
        self.account_holder_name = ''
        self.passbook_proofs = []  # Uploaded passbook proof paths
        # The customer's own saved bank accounts (GET loan-process/bank-details). When present,
        # the penny-drop uses ONLY these, never the reference/bank_accounts.json list.
        self.saved_bank_accounts = []
        self._passbook_from_saved = False  # passbook_proofs came from a saved account
        # Bank verification state, captured from validate-account / bank-verification-manual.
        # bank_account_verified  -> penny-drop reached the bank (data.bankTxnStatus)
        # bank_system_verified   -> the server accepted it outright (isVerified)
        # bank_manually_verified -> an ops user approved it manually (isManuallyVerified)
        # bank_for_ops_approval  -> the record is PENDING an ops manual approval (forOpsApproval)
        self.bank_account_verified = False
        self.bank_system_verified = False
        self.bank_manually_verified = False
        self.bank_for_ops_approval = False
        # The downstream E2E request payloads are scoped to this internal branch.
        # Keep the token scope and payload scope in sync.
        self.internal_branch_id = os.getenv("GOLD_LOAN_INTERNAL_BRANCH_ID", "1")
        self.module_context = "request"
        # Existing-customer mode: set either the numeric customer ID or its customer unique ID
        # (via GOLD_LOAN_EXISTING_CUSTOMER_ID / GOLD_LOAN_EXISTING_CUSTOMER_UNIQUE_ID). When set,
        # the run resolves the customer, skips KYC if it is already approved, and goes straight to
        # the appraiser-request/loan process. The remaining fields are resolved automatically.
        self.existing_customer_id = os.getenv("GOLD_LOAN_EXISTING_CUSTOMER_ID", "")
        self.existing_customer_unique_id = os.getenv("GOLD_LOAN_EXISTING_CUSTOMER_UNIQUE_ID", "")
        self.kyc_status = ""  # captured from get_customer_by_id (approved/pending/etc.)
        # KYC v2 flow: kycType drives the v2 endpoints. RE_KYC re-runs KYC for an existing customer;
        # a fresh customer's KYC also flows through the same v2 endpoints.
        self.kyc_type = os.getenv("GOLD_LOAN_KYC_TYPE", "RE_KYC")
        self.kyc_reference_code = ""  # OTP consent reference (send-otp -> verify-otp-admin)
        # --- KYC verification mode + real identity data ----------------------------------------
        self.kyc_mode = (os.getenv("GOLD_LOAN_KYC_MODE", self.KYC_MODE_MANUAL) or "").strip().lower()
        if self.kyc_mode not in self.KYC_MODES:
            raise ValueError(f"Unknown KYC mode '{self.kyc_mode}'. Choose one of "
                             f"{list(self.KYC_MODES)} (--kyc / GOLD_LOAN_KYC_MODE).")
        # Requested vs effective: an auto/ovd run degrades to manual when the server signals
        # switchToManual, and the summary must not claim a verification that never happened.
        self.kyc_mode_requested = self.kyc_mode
        self.kyc_mode_effective = self.kyc_mode
        # Verification outcomes, sent on submit-basic-info / customer-kyc-address and reported.
        self.pan_verified = False
        self.aadhaar_verified = False
        self.ovd_verified = False
        self.kyc_verification_log = []  # (step, outcome, detail) trail shown in the run summary
        self.ovd_type = ""            # the string submit-basic-info expects, e.g. "VoterID"
        self.ovd_number = ""          # epic/DL number as issued
        self.ovd_image = ""           # uploaded OVD scan path
        self.ovd_name = ""            # name as returned by the OVD API (CR-2 name reconciliation)
        self.ovd_verification_data = {}  # full `data` block from the verify response
        self.form97_image = ""        # Form 97 PDF path (replaces Form 60 under IT Rules 2026)
        # Must come AFTER the ovd_* defaults above: _validate_kyc_profile resolves ovd_type /
        # ovd_number from the profile, and initialising them afterwards would wipe those values.
        self.kyc_profile = self._load_kyc_profile()
        self._validate_kyc_profile()
        self.supplied_auth_token = os.getenv("GOLD_LOAN_AUTH_TOKEN", "")
        self.logged_in_mobile_number = '' # New instance variable
        self.logged_in_appraiser_name = ''
        self.logged_in_appraiser_mobile_number = ''
        self.logged_in_user_id = None

        self.random = random.Random()  # Initialize random object

        # Ensure dummy image and PDF exist for file uploads
        if not os.path.exists(self.DUMMY_IMAGE_PATH):
            try:
                from PIL import Image
                img = Image.new('RGB', (60, 30), color = 'red')
                img.save(self.DUMMY_IMAGE_PATH)
                print(f"Created {self.DUMMY_IMAGE_PATH} for file uploads.")
            except ImportError:
                print("Pillow not installed. Cannot create dummy_image.png. Please install it (`pip install Pillow`) or create the file manually.")
                raise Exception(f"Failed to create {self.DUMMY_IMAGE_PATH}, which is required for file uploads.")
            except IOError as e:
                print(f"Could not create {self.DUMMY_IMAGE_PATH}: {e}")
                raise Exception(f"Failed to create {self.DUMMY_IMAGE_PATH}, which is required for file uploads.")

        if not os.path.exists(self.DUMMY_PDF_PATH):
            try:
                from reportlab.pdfgen import canvas
                c = canvas.Canvas(self.DUMMY_PDF_PATH)
                c.drawString(100, 750, "Dummy PDF Content")
                c.save()
                print(f"Created {self.DUMMY_PDF_PATH} for file uploads.")
            except ImportError:
                print("ReportLab not installed. Cannot create CPV.pdf. Please install it (`pip install reportlab`) or create the file manually.")
                raise Exception(f"Failed to create {self.DUMMY_PDF_PATH}, which is required for file uploads.")
            except IOError as e:
                print(f"Could not create {self.DUMMY_PDF_PATH}: {e}")
                raise Exception(f"Failed to create {self.DUMMY_PDF_PATH}, which is required for file uploads.")

    # ------------------------------------------------------------------ #
    #  Console formatting helpers (API-testing oriented output)          #
    # ------------------------------------------------------------------ #
    _ANSI = {
        "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
        "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
        "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
        "gray": "\033[90m",
    }
    _WIDTH = 78

    @staticmethod
    def _init_unicode() -> bool:
        """Force UTF-8 stdout/stderr where possible so glyphs don't crash on cp1252 consoles."""
        import sys
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream and hasattr(stream, "reconfigure"):
                    enc = (getattr(stream, "encoding", "") or "").lower()
                    if "utf" not in enc:
                        stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        enc = (getattr(sys.stdout, "encoding", "") or "").lower()
        return "utf" in enc

    @staticmethod
    def _init_color(mode: str) -> bool:
        """Decide whether to emit ANSI color, enabling VT processing on Windows."""
        import sys
        if mode in {"0", "false", "no", "off", "never"}:
            return False
        if mode in {"1", "true", "yes", "on", "always"}:
            enabled = True
        else:  # auto
            enabled = sys.stdout.isatty()
        if enabled and os.name == "nt":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            except Exception:
                pass
        return enabled

    def _c(self, text, *styles) -> str:
        if not self.use_color or not styles:
            return str(text)
        prefix = "".join(self._ANSI.get(s, "") for s in styles)
        return f"{prefix}{text}{self._ANSI['reset']}"

    @classmethod
    def _json_text(cls, value) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, indent=2, ensure_ascii=False)
        if isinstance(value, (bytes, bytearray)):
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError:
                return "<binary body omitted>"
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return ""
            try:
                return json.dumps(json.loads(stripped), indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                return stripped
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)

    def _print_json_block(self, title: str, value) -> None:
        print(f"\n{self._c(title, 'bold', 'cyan')}")
        print(self._json_text(value))

    def _sanitize(self, value):
        """Shorten long strings (base64/data URIs) and large lists for readable logs."""
        if self.log_level == "verbose":
            return value
        str_limit, list_limit = 160, 10

        def rec(v):
            if isinstance(v, dict):
                return {k: rec(x) for k, x in v.items()}
            if isinstance(v, list):
                if len(v) > list_limit:
                    return [rec(x) for x in v[:list_limit]] + [f"{self._G['ell']}(+{len(v) - list_limit} more items)"]
                return [rec(x) for x in v]
            if isinstance(v, str) and len(v) > str_limit:
                head = v[:60]
                if v.startswith("data:"):
                    head = v.split(",", 1)[0] + ",<base64>"
                return f"{head}{self._G['ell']}(+{len(v) - len(head)} chars)"
            return v

        return rec(value)

    def _debug(self, message: str) -> None:
        if self.log_level == "verbose":
            print(self._c(f"   {self._G['dot']} {message}", "gray"))

    def _banner(self, title: str, subtitle: str = "") -> None:
        line = self._G["bar"] * self._WIDTH
        print("\n" + self._c(line, "bold", "blue"))
        print(self._c(f" {title}", "bold", "blue"))
        if subtitle:
            print(self._c(f" {subtitle}", "blue"))
        print(self._c(line, "bold", "blue"))

    # --- HAR recording ------------------------------------------------------------------------
    HAR_REDACTED_HEADERS = ("authorization", "cookie", "set-cookie")

    def _record_har_entry(self, response: httpx.Response) -> None:
        """One HAR 1.2 entry for this exchange. Bearer tokens / cookies are redacted; multipart
        uploads are summarised (file name + size) instead of embedding the binary."""
        request = response.request
        elapsed_ms = round(response.elapsed.total_seconds() * 1000, 1) if response.elapsed is not None else 0
        started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(milliseconds=elapsed_ms)

        def headers(items):
            return [{"name": k, "value": "<redacted>" if k.lower() in self.HAR_REDACTED_HEADERS else v}
                    for k, v in items]

        entry = {
            "startedDateTime": started.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "time": elapsed_ms,
            "comment": self._current_step or "",
            "request": {
                "method": request.method,
                "url": str(request.url),
                "httpVersion": "HTTP/1.1",
                "headers": headers(request.headers.items()),
                "queryString": [{"name": k, "value": v} for k, v in request.url.params.multi_items()],
                "cookies": [],
                "headersSize": -1,
                "bodySize": len(request.content or b""),
            },
            "response": {
                "status": response.status_code,
                "statusText": response.reason_phrase,
                "httpVersion": "HTTP/1.1",
                "headers": headers(response.headers.items()),
                "cookies": [],
                "content": {
                    "size": len(response.content or b""),
                    "mimeType": response.headers.get("content-type", ""),
                    "text": response.content.decode("utf-8", "replace") if response.content else "",
                },
                "redirectURL": "",
                "headersSize": -1,
                "bodySize": len(response.content or b""),
            },
            "cache": {},
            "timings": {"send": 0, "wait": elapsed_ms, "receive": 0},
        }
        content_type = request.headers.get("content-type", "")
        if request.content:
            if content_type.startswith("multipart/"):
                names = re.findall(rb'filename="([^"]+)"', request.content)
                files = ", ".join(n.decode("utf-8", "replace") for n in names) or "file"
                entry["request"]["postData"] = {
                    "mimeType": content_type.split(";")[0],
                    "text": f"<multipart upload: {files} ({len(request.content)} bytes)>",
                }
            else:
                entry["request"]["postData"] = {
                    "mimeType": content_type or "application/json",
                    "text": request.content.decode("utf-8", "replace"),
                }
        self._har_entries.append(entry)

    def write_har(self) -> None:
        """Write the recorded entries to self.har_path (no-op when recording is off)."""
        if not self.har_path or not self._har_entries:
            return
        har = {"log": {
            "version": "1.2",
            "creator": {"name": "gold_loan harness (flows/e2e_full_loan.py --record-har)", "version": "1.0"},
            "comment": (f"Recorded from a live {self.env_name.upper()} run of the harness "
                        f"({self.BASE_URL}). Entry comments name the run step. Authorization / cookies "
                        "redacted; multipart uploads summarised."),
            "pages": [],
            "entries": self._har_entries,
        }}
        os.makedirs(os.path.dirname(os.path.abspath(self.har_path)), exist_ok=True)
        with open(self.har_path, "w", encoding="utf-8") as fh:
            json.dump(har, fh, indent=1, ensure_ascii=False)
        print(f"HAR written: {self.har_path} ({len(self._har_entries)} entries)")

    def _log_step(self, title: str) -> None:
        self._step_no += 1
        self._current_step = title
        label = f" STEP {self._step_no:>2} {self._G['dot']} {title} "
        pad = max(0, self._WIDTH - len(label))
        print("\n" + self._c(f"{self._G['corner']}{label}" + self._G["rule"] * pad, "bold", "magenta"))

    def _parse_body(self, raw):
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return "<binary body omitted>"
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return None
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return stripped
        return raw

    async def _log_http_exchange(self, response: httpx.Response) -> None:
        await response.aread()
        self._api_calls += 1
        if self.har_path:
            self._record_har_entry(response)
        status = response.status_code
        request = response.request
        method = request.method
        path = request.url.raw_path.decode("ascii", "ignore") if hasattr(request.url, "raw_path") else str(request.url)
        if status >= 400:
            self._api_failures += 1
            # Record the failing endpoint + server message for the end-of-run report.
            try:
                err_body = response.json()
                err_msg = err_body.get("message") if isinstance(err_body, dict) else str(err_body)
            except Exception:
                # An nginx/express failure is an HTML page; collapsed to one line so it does not
                # bury the other failures in the end-of-run report.
                err_msg = self._short_error_body(response.text, 200)
            self._failed_endpoints.append(
                (self._api_calls, method, path.split("?")[0], status, response.reason_phrase, err_msg or "")
            )
        if not self.log_http:
            return  # Metrics still counted, but no per-call output.

        # Status glyph + color by class.
        if status < 300:
            glyph, color = self._G["ok"], "green"
        elif status < 400:
            glyph, color = self._G["warn"], "yellow"
        else:
            glyph, color = self._G["fail"], "red"
        elapsed = f"{response.elapsed.total_seconds():.2f}s" if response.elapsed is not None else "-"

        head = (
            f"{self._c(glyph, color, 'bold')} "
            f"{self._c(f'{method:<4}', 'cyan')} "
            f"{path}"
        )
        dot = self._G["dot"]
        meta = self._c(f"{status} {response.reason_phrase} {dot} {elapsed} {dot} call #{self._api_calls}", color)
        print(f"  {head}")
        print(f"     {meta}")

        if self.log_level == "quiet":
            return

        # Request body (skip noisy multipart uploads).
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            print(self._c("     req  <multipart/form-data upload>", "gray"))
        else:
            body = self._parse_body(request.content) if request.content else None
            if body not in (None, ""):
                text = self._json_text(self._sanitize(body))
                print(self._c("     req  " + self._indent(text), "gray"))

        # Response body.
        resp_body = self._parse_body(response.text)
        if resp_body not in (None, ""):
            text = self._json_text(self._sanitize(resp_body))
            label_color = "gray" if status < 400 else "red"
            print(self._c("     resp " + self._indent(text), label_color))

        if self.log_level == "verbose":
            shown = {k: v for k, v in request.headers.items()
                     if k.lower() in {"content-type", "authorization", "signature", "ismask", "documenttype"}}
            if "authorization" in {k.lower() for k in shown}:
                for k in list(shown):
                    if k.lower() == "authorization":
                        shown[k] = shown[k][:24] + self._G["ell"]
            print(self._c("     hdrs " + self._indent(self._json_text(shown)), "gray"))

    @staticmethod
    def _indent(text: str, pad: str = "          ") -> str:
        lines = text.splitlines() or [text]
        return ("\n" + pad).join(lines)

    @classmethod
    def _js_number_normalize(cls, value):
        """Render numbers the way JavaScript's JSON.stringify does.

        The backend re-serializes the parsed request body (JS-style) to verify the HMAC
        signature. Python renders whole-number floats as ``4480.0``/``0.0`` whereas JS renders
        ``4480``/``0``; that mismatch makes the signature fail with 401 "auth failed". Converting
        integral floats to ints before signing *and* sending keeps both sides identical.
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {k: cls._js_number_normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._js_number_normalize(v) for v in value]
        return value

    def _generate_signature(self, api_path: str, request_body: dict = None) -> str:
        if request_body is None:
            request_body = {}

        # The browser signs the relative path and query string, not the backend URL.
        url_for_hmac = api_path.replace("%20", " ")

        final_payload = {}
        if request_body:
            final_payload.update(request_body)

        final_payload["url"] = url_for_hmac
        final_payload = self._js_number_normalize(final_payload)

        hmac_string = json.dumps(final_payload, separators=(',', ':'))
        if self.log_level == "verbose":
            self._print_json_block("HMAC PAYLOAD", final_payload)

        hashed = hmac.new(
            self.HMAC_SECRET.encode('utf-8'),
            hmac_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return hashed

    # --- Helper functions for random data generation ---
    def _generate_random_mobile_number(self) -> str:
        prefix = self.random.randint(7, 9)
        rest_of_number = self.random.randint(100000000, 999999999)
        return f"{prefix}{rest_of_number}"

    def _generate_random_pincode(self) -> str:
        return str(self.random.randint(100000, 999999))

    def _generate_random_name(self) -> dict:
        first_names = ["John", "Jane", "Peter", "Mary", "Robert", "Patricia", "Michael", "Linda"]
        last_names = ["Doe", "Smith", "Jones", "Williams", "Brown", "Davis", "Miller", "Wilson"]
        return {
            "firstName": self.random.choice(first_names),
            "lastName": self.random.choice(last_names),
        }

    def _generate_random_pan(self) -> str:
        letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        digits = '0123456789'
        first_three = ''.join(self.random.choice(letters) for _ in range(3))
        fourth_char = 'P'  # Type of holder, 'P' for individual
        fifth_char = self.random.choice(letters)
        number_part = ''.join(self.random.choice(digits) for _ in range(4))
        last_char = self.random.choice(letters)
        return f"{first_three}{fourth_char}{fifth_char}{number_part}{last_char}"

    def _generate_random_dob_and_age(self) -> dict:
        today = datetime.date.today()
        min_age = 24
        max_age = 58
        age_val = self.random.randint(min_age, max_age)

        # Approximate DOB
        dob_date = today - datetime.timedelta(days=age_val * 365 + self.random.randint(0, 364))

        return {"age": str(age_val), "dob": dob_date.isoformat()}

    @staticmethod
    def _age_from_dob(dob: str) -> int:
        """Whole years between a YYYY-MM-DD date of birth and today."""
        born = datetime.date.fromisoformat(str(dob)[:10])
        today = datetime.date.today()
        return today.year - born.year - ((today.month, today.day) < (born.month, born.day))

    # Standard Verhoeff tables (also used by _generate_random_aadhaar_number).
    _VERHOEFF_D = ((0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
                   (2, 3, 4, 0, 1, 7, 8, 9, 5, 6), (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
                   (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
                   (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
                   (8, 7, 6, 5, 9, 3, 2, 1, 0, 4), (9, 8, 7, 6, 5, 4, 3, 2, 1, 0))
    _VERHOEFF_P = ((0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
                   (5, 8, 0, 3, 7, 9, 6, 1, 4, 2), (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
                   (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
                   (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 1, 3, 5, 8, 2, 9))

    @classmethod
    def _verhoeff_valid(cls, number: str) -> bool:
        check = 0
        for index, digit in enumerate(reversed(number)):
            check = cls._VERHOEFF_D[check][cls._VERHOEFF_P[index % 8][int(digit)]]
        return check == 0

    def _aadhaar_with_last4(self, last4: str) -> str:
        """An Aadhaar for --kyc auto: 8 RANDOM digits + the person's REAL last 4.

        The offline e-KYC XML only carries the last 4 digits, so that (with the real name) is all
        the server can match; the random head makes every customer's number unique, so the same
        person's XML can KYC more than one customer without "Aadhaar already exists". The head is
        re-drawn until the full number passes the Verhoeff check (the last digit is the checksum)."""
        assert len(last4) == 4 and last4.isdigit(), f"need the 4 real last digits, got {last4!r}"
        for _ in range(10000):
            head = self.random.choice('23456789') + ''.join(self.random.choice('0123456789') for _ in range(7))
            if self._verhoeff_valid(head + last4):
                return head + last4
        raise RuntimeError(f"Could not build a Verhoeff-valid Aadhaar ending in {last4}.")

    def _generate_random_aadhaar_number(self) -> str:
        # Aadhaar uses the Verhoeff checksum; generate eleven random digits and
        # append a valid check digit so the KYC API accepts the format.
        multiplication = (
            (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
            (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
            (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
            (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
            (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
            (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
            (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
            (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
            (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
            (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
        )
        permutation = (
            (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
            (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
            (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
            (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
            (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
            (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
            (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
            (7, 0, 4, 6, 1, 3, 5, 8, 2, 9),
        )
        inverse = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)
        prefix = self.random.choice('23456789') + ''.join(
            self.random.choice('0123456789') for _ in range(10)
        )
        checksum = 0
        for index, digit in enumerate(reversed(prefix)):
            checksum = multiplication[checksum][permutation[(index + 1) % 8][int(digit)]]
        
        aadhaar_number = prefix + str(inverse[checksum])
        # Return unformatted 12-digit string
        return aadhaar_number

    def _encrypt_identity_proof_number(self, value: str) -> str:
        """Match the CryptoJS encryption used by the Augmont gold-loan web application."""
        script = """
        const CryptoJS = require('crypto-js');
        let input = '';
        process.stdin.setEncoding('utf8');
        process.stdin.on('data', chunk => input += chunk);
        process.stdin.on('end', () => {
          const key = CryptoJS.HmacSHA256('TSHZ2_AUGMONT_CY2RG', '').toString().substring(0, 32);
          const iv = CryptoJS.HmacSHA256('4WI3R_AUGMONT_OVEEC', '');
          process.stdout.write(CryptoJS.AES.encrypt(input, key, {
            keySize: 16, iv, mode: CryptoJS.mode.ECB, padding: CryptoJS.pad.Pkcs7,
            algorithm: 'AES-256-CBC'
          }).toString());
        });
        """
        result = subprocess.run(
            ["node", "-e", script], input=value, text=True, capture_output=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Identity-proof encryption failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def _generate_random_address(self) -> dict:
        streets = ["MG Road", "Park Street", "Church Street", "Link Road", "Main Street"]
        landmarks = ["GT Circle", "City Mall", "Bus Depot", "Central Park", "Old Temple"]
        cities = ["Mumbai", "Delhi", "Bangalore", "Chennai", "Hyderabad"]
        states = ["Maharashtra", "Karnataka", "Tamil Nadu", "Telangana", "West Bengal"]

        street_number = self.random.randint(1, 200)
        street = self.random.choice(streets)
        landmark = self.random.choice(landmarks)
        city = self.random.choice(cities)
        state = self.random.choice(states)
        postal_code = self.random.randint(100000, 999999)

        return {
            "address": f"{street_number}, {street}, near {landmark}, {city}, {state} - {postal_code}",
            "landmark": landmark
        }

    def _round_double(self, value, places):
        return round(value, places)

    @staticmethod
    def _round_half_up(value, places=2):
        """Round HALF-UP, matching the backend's rounding (JS-style), not Python's banker's rounding.

        The server recomputes netWtAfterPurity and validates eligibility against ITS value. Python's
        round() rounds half-to-even AND is at the mercy of float artifacts, so a value landing exactly
        on a half-cent tie (e.g. 31.65 * 0.90 = 28.485) rounds to 28.48 here but 28.49 on the server.
        That 0.01 difference, times the scheme rpg, makes final-loan-details 400 with an eligibility
        mismatch. Quantizing the decimal string half-up reproduces the server's result exactly.
        """
        try:
            quantum = Decimal(1).scaleb(-places)  # e.g. places=2 -> Decimal("0.01")
            return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))
        except (ValueError, ArithmeticError):
            return round(value, places)

    ROSHAN_PARTNER_ID = "152"
    ARVOG_PARTNER_ID = "10"

    def _partner_key(self) -> str:
        """Which partner this run is underwritten by, as a key into the environment's partner
        mobile maps. Keys on self.partner_id (resolved during scheme selection), falling back to
        GOLD_LOAN_PARTNER_NAME before the partner is known, and defaults to Roshan."""
        if str(self.partner_id) in (self.ROSHAN_PARTNER_ID, self.ARVOG_PARTNER_ID):
            return str(self.partner_id)
        if "ARVOG" in os.getenv("GOLD_LOAN_PARTNER_NAME", "").upper():
            return self.ARVOG_PARTNER_ID
        return self.ROSHAN_PARTNER_ID

    def _partner_login_mobile(self) -> str:
        """Partner login mobile for the currently selected partner (approval + disbursement).
        Each partner has its own login, and the numbers differ per environment."""
        mobiles = self.env["partner_mobiles"]
        key = self._partner_key()
        if key not in mobiles:
            raise ValueError(f"No partner login mobile configured for partner {key} in the "
                             f"'{self.env_name}' environment (known: {sorted(mobiles)}).")
        return mobiles[key]

    def _partner_user_mobile(self) -> str:
        """Partner-BRANCH user who receives the packet at submit-packet, for the selected partner
        and environment. GOLD_LOAN_PARTNER_USER_MOBILE overrides (e.g. a partner whose branch user
        isn't in the profile yet)."""
        override = os.getenv("GOLD_LOAN_PARTNER_USER_MOBILE", "").strip()
        if override:
            return override
        users = self.env["partner_user_mobiles"]
        key = self._partner_key()
        if key not in users:
            raise ValueError(f"No partner-branch user mobile configured for partner {key} in the "
                             f"'{self.env_name}' environment (known: {sorted(users)}). Set "
                             f"GOLD_LOAN_PARTNER_USER_MOBILE to supply one.")
        return users[key]

    def _get_mobile_number_for_login_type(self, login_type: str) -> str:
        # Role -> mobile, per environment. Static OTP 1234 for all. admin: packet create/assign;
        # bm: BM approval (loans > 5L); ops: ops rating (final approval); appraiser: the main
        # loan-flow actor. partner: approval + disbursement, resolved per selected partner.
        if login_type == "partner":
            return self._partner_login_mobile()
        mobiles = self.env["role_mobiles"]
        if login_type not in mobiles:
            raise ValueError(f"Invalid login_type '{login_type}'. Must be one of "
                             f"{sorted(mobiles) + ['partner']}.")
        return mobiles[login_type]

    # nginx in front of the API answers 502/503/504 while the backend is restarting or wedged.
    # That is infrastructure, not our request -- one blip should not lose a whole run, let alone
    # a whole --count batch.
    GATEWAY_STATUSES = (502, 503, 504)
    # Writes that are safe to repeat after a lost reply: re-sending a file upload at worst leaves
    # an unused file behind -- it cannot duplicate a customer, loan or rating.
    RETRY_SAFE_WRITES = ("/api/upload-file",)

    @staticmethod
    def _is_html_error_page(text: str) -> bool:
        return bool(text) and text.lstrip()[:200].lower().startswith(("<html", "<!doctype"))

    @classmethod
    def _short_error_body(cls, text: str, limit: int = 300) -> str:
        """Collapse an nginx/express HTML error page to one line; leave JSON bodies alone.

        A 502 page is eight lines of markup that buries the actual failure in the run summary.
        """
        if not text:
            return ""
        if not cls._is_html_error_page(text):
            return text[:limit]
        title = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
        heading = re.search(r"<h1>(.*?)</h1>", text, re.I | re.S)
        server = re.search(r"<center>([^<]*?/[^<]*?)</center>", text, re.I)
        parts = [p.group(1).strip() for p in (title, heading) if p and p.group(1).strip()]
        label = parts[0] if parts else "HTML error page"
        if server:
            label += f" ({server.group(1).strip()})"
        return label

    async def _send_with_gateway_retry(self, verb: str, api_path: str, send_once):
        """Send, retrying only failures that are safe to repeat.

        GET is idempotent, so a 502/503/504 or any transport error is retried. A write is NOT
        retried on a gateway status: nginx cannot tell us whether the backend processed the
        request before dying, and re-sending a create would duplicate a customer, a loan or a
        rating -- the exact mess "already exists" handling exists to clean up. A write IS retried
        when the connection never got established, because then nothing can have been processed.
        """
        attempts = max(1, self.gateway_retries)
        # File uploads (RETRY_SAFE_WRITES) may be repeated after a lost reply, like a GET.
        repeatable = verb == 'GET' or api_path.split('?')[0].startswith(self.RETRY_SAFE_WRITES)
        for attempt in range(1, attempts + 1):
            last = attempt == attempts
            try:
                response = await send_once()
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # No connection was made, so nothing was processed -- safe to repeat for any verb.
                if last:
                    raise
                await self._gateway_backoff(attempt, verb, api_path, f"{type(e).__name__}")
                continue
            except httpx.TransportError as e:
                # The request went out; the reply did not come back. Only a GET (or a file upload)
                # may be repeated.
                if last or not repeatable:
                    raise
                await self._gateway_backoff(attempt, verb, api_path, f"{type(e).__name__}")
                continue

            if response.status_code in self.GATEWAY_STATUSES and not last and verb == 'GET':
                await self._gateway_backoff(
                    attempt, verb, api_path,
                    f"{response.status_code} {response.reason_phrase}")
                continue
            if response.status_code in self.GATEWAY_STATUSES and verb != 'GET':
                print(self._c(
                    f"  {response.status_code} {response.reason_phrase} on {verb} {api_path} - "
                    "NOT retried: the gateway cannot say whether the backend applied it, and "
                    "re-sending a write would risk a duplicate.", "yellow"))
            return response
        return response

    async def _gateway_backoff(self, attempt: int, verb: str, api_path: str, reason: str) -> None:
        delay = self.gateway_retry_delay * attempt
        print(self._c(f"  {reason} on {verb} {api_path} - retrying in {delay:.0f}s "
                      f"(attempt {attempt + 1}/{max(1, self.gateway_retries)}).", "yellow"))
        await asyncio.sleep(delay)

    async def _make_authenticated_request(self, method: str, api_path: str, json_data: dict = None, params: dict = None, files: dict = None, headers: dict = None):
        if headers is None:
            headers = {}

        # Always set/update the Authorization header with the current token. The token comes from the
        # fresh login at the start of the run; there is no per-request re-login.
        if self.auth_token:
            headers['Authorization'] = f'Bearer {self.auth_token}'
        # Else, if no token, Authorization header will not be set. This is fine for initial login steps.

        # Normalize numbers to JS/JSON.stringify form so the body we send matches the body we sign
        # (integral floats like 4480.0 -> 4480); otherwise the server's signature check fails.
        if json_data is not None:
            json_data = self._js_number_normalize(json_data)

        # Generate signature
        if method.upper() == 'GET':
            signature = self._generate_signature(api_path, {})
        else: # POST, PUT, etc.
            signature = self._generate_signature(api_path, json_data)

        headers['signature'] = signature

        # Set Content-Type if not already set and it's a JSON request
        if json_data is not None and 'Content-Type' not in headers:
            headers['Content-Type'] = 'application/json'

        # Special handling for file uploads (multipart/form-data)
        if files:
            # httpx automatically sets Content-Type for multipart/form-data when 'files' is used
            # so we should not set 'application/json' here.
            if 'Content-Type' in headers:
                del headers['Content-Type'] # Let httpx handle it

        self._debug(f"Sending {method} {api_path} with headers: {list(headers)}")
        verb = method.upper()
        if verb not in ('GET', 'POST', 'PUT'):
            raise ValueError(f"Unsupported HTTP method: {method}")

        async def send_once():
            if verb == 'GET':
                return await self.client.get(api_path, headers=headers, params=params)
            if verb == 'POST':
                return await self.client.post(api_path, json=json_data, headers=headers, files=files)
            return await self.client.put(api_path, json=json_data, headers=headers)

        response = await self._send_with_gateway_retry(verb, api_path, send_once)

        if response.status_code == 401:
            raise httpx.HTTPStatusError(
                f"401 Unauthorized for {api_path}. The login token was rejected; re-run to log in again.",
                request=response.request,
                response=response,
            )
        # Any KYC document already on file for another customer ends the run HERE. Raised from the
        # chokepoint rather than per-endpoint so it cannot be missed, and as its own exception type
        # so the verify steps' `except httpx.HTTPStatusError` fallback handlers do not swallow it.
        if response.status_code >= 400 and self._is_duplicate_document_error(response.text):
            document = self._kyc_document_label(api_path)
            if document:
                self._abort_on_duplicate_document(document, self._duplicate_reason(response))
        response.raise_for_status()
        return response

    @staticmethod
    def _kyc_document_label(api_path: str) -> str:
        """Name the document a KYC endpoint deals with, or '' if the path is not KYC-related.

        Scoped deliberately: an "already exists" from an unrelated endpoint (a packet, say) must
        not abort the run as though a KYC document were duplicated.
        """
        path = (api_path or "").lower()
        for marker, label in (
            ("verify-pan", "PAN"),
            ("offline-aadhaar-xml", "Aadhaar"),
            ("verify-dl", "Driving Licence"),
            ("verify-voter-id", "Voter ID"),
            ("verify-passport", "Passport"),
            ("submit-basic-info", "identity document"),
            ("customer-kyc-address", "identity document"),
            ("submit-all-kyc-info", "identity document"),
            ("customer-kyc-personal", "identity document"),
        ):
            if marker in path:
                return label
        # Customer creation rejects a duplicate PAN with "PAN Card already exists!".
        if re.fullmatch(r"/api/customer/?", path.split("?")[0]):
            return "PAN"
        return ""

    @classmethod
    def _duplicate_reason(cls, response) -> str:
        """Pull the server's own wording out of a duplicate-document rejection."""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                for key in ("message", "reason", "error"):
                    if payload.get(key):
                        return str(payload[key])
        except (ValueError, json.JSONDecodeError):
            pass
        return (response.text or "")[:200]

    # --- API Call Methods ---

    async def login(self, mobile_number: str):
        # Step 1: Send OTP
        api_path_send_otp = "/api/user-otp/user-send-otp"
        request_body_send_otp = {
            "mobileNumber": mobile_number,
            "type": "login",
            "id": None
        }
        print(f"Attempting to send OTP to {mobile_number}...")
        response_send_otp = await self._make_authenticated_request(
            'POST',
            api_path_send_otp,
            json_data=request_body_send_otp,
            headers={'Content-Type': 'application/json'}, # Explicitly set for initial login
        )
        response_data_send_otp = response_send_otp.json()
        self.reference_code = response_data_send_otp.get('referenceCode')
        assert self.reference_code, f"Reference code not found in send-otp response: {response_data_send_otp}"
        print(f"OTP sent successfully. Reference Code: {self.reference_code}")
        self._print_json_block("Cookies", dict(self.client.cookies))

        # Step 2: Verify Login with OTP
        api_path_verify_login = "/api/auth/verify-login"
        otp_to_verify = 1234
        request_body_verify_login = {
            "referenceCode": self.reference_code,
            "otp": otp_to_verify,
            "type": "login",
            "isFromWeb": True
        }
        print(f"Attempting to verify login with OTP {otp_to_verify} and Reference Code {self.reference_code}...")
        response_verify_login = await self._make_authenticated_request(
            'POST',
            api_path_verify_login,
            json_data=request_body_verify_login,
            headers={'Content-Type': 'application/json'}, # Explicitly set for initial login
        )

        response_data_verify_login = response_verify_login.json()
        self.auth_token = response_data_verify_login.get('Token')
        self._user_details_by_token[self.auth_token] = response_data_verify_login.get('userDetails') or {}
        print(f"Login successful. New auth_token obtained: {self.auth_token[:10]}...") # Added debug print
        assert self.auth_token, f"Auth Token not found in verify-login response: {response_data_verify_login}"
        print("Login successful. Auth token obtained.")
        self.logged_in_mobile_number = mobile_number # Set the logged-in mobile number

        # Decode JWT and print relevant fields
        try:
            decoded_jwt = jwt.decode(self.auth_token, options={"verify_signature": False})
            self.logged_in_user_id = decoded_jwt.get('id')
            self._print_json_block("Decoded JWT Payload", decoded_jwt)
            if not self.internal_branch_id:
                self.internal_branch_id = str(decoded_jwt.get("internalBranchId") or "")
            assert self.internal_branch_id, "The login token did not include internalBranchId."
        except Exception as e:
            print(f"Error decoding JWT: {e}")

    async def _send_otp_verification(self, ref_code: str):
        # The portal's LeadService.verifyOtp: customer-otp/verify-otp-admin (the old
        # customer/verify-otp is commented out in the portal). Static test OTP.
        api_path = "/api/customer-otp/verify-otp-admin"
        request_body = {
            "otp": self.CUSTOMER_OTP,
            "referenceCode": ref_code,
            "type": "lead",
        }
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        assert response.status_code == 200, f"OTP Verification failed: {response.text}"
        self._print_json_block('OTP Verified Successfully', response.json())


    CUSTOMER_OTP = "1234"  # static OTP on TEST/UAT; the portal's lead OTP field takes 4 digits

    async def _send_register_otp(self):
        """Send the new-customer OTP the way the portal's AddLeadComponent does: customer-otp/send-otp
        with the customer's name and type 'lead'. The old customer/send-register-otp endpoint is
        commented out in the portal and answers 403 "This request is not permitted." on TEST."""
        self.mobile_number = self.mobile_number or self._generate_random_mobile_number()
        api_path = "/api/customer-otp/send-otp"
        request_body = {"mobileNumber": self.mobile_number, "firstName": self.first_name,
                        "lastName": self.last_name, "type": "lead"}
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        response_body = response.json()
        self.reference_code = response_body.get('referenceCode')
        assert self.reference_code, f"Reference code not found in customer-otp/send-otp response: {response_body}"
        print(f'OTP Sent. Reference Code: {self.reference_code}')
        await self._send_otp_verification(self.reference_code)


    async def _fetch_states(self):
        api_path = "/api/state"
        self._debug("Prepared signed headers for /api/state")
        response = await self._make_authenticated_request('GET', api_path)
        states = response.json().get('data')
        assert states, "No states found."
        self.state_id = str(self.random.choice(states).get('id'))
        print(f'Fetched random state_id: {self.state_id}')


    async def _fetch_cities(self, state_id: str):
        api_path = f"/api/city?stateId={state_id}"
        self._debug(f"Prepared signed headers for /api/city?stateId={state_id}")
        response = await self._make_authenticated_request('GET', api_path)
        cities = response.json().get('data')
        assert cities, "No cities found for the selected state."
        self.city_id = str(self.random.choice(cities).get('id'))
        print(f'Fetched random city_id: {self.city_id}')


    async def _fetch_modules(self):
        api_path = f"/api/modules/appraiser-request-module?isFor={self.module_context}"
        self._debug(f"Prepared signed headers for {api_path}")
        response = await self._make_authenticated_request('GET', api_path)
        modules = response.json()
        gold_loan_module = next((item for item in modules if item.get('moduleName') == 'gold loan'), None)
        assert gold_loan_module, "Gold loan module not found."
        self.module_id = str(gold_loan_module.get('id'))
        print(f'Fetched module_id for "gold loan": {self.module_id}')


    async def _fetch_status(self):
        api_path = "/api/status"
        self._debug("Prepared signed headers for /api/status")
        response = await self._make_authenticated_request('GET', api_path)
        statuses = response.json().get('data')
        assert statuses, "No statuses found."
        confirm_status = next((item for item in statuses if item.get('statusName') == 'confirm'), None)
        assert confirm_status, "Confirm status not found."
        self.status_id = str(confirm_status.get('id'))
        print(f'Fetched status_id for "confirm": {self.status_id}')


    async def _upload_file(self, reason: str, file_type: str = "image", document_type: str = None,
                           is_mask: bool = False, file_path_override: str = None):
        if file_path_override:
            file_path_on_disk = file_path_override
            assert os.path.exists(file_path_on_disk), (
                f"Upload file not found: {file_path_on_disk}"
            )
        else:
            file_path_on_disk = self.DUMMY_IMAGE_PATH if file_type == "image" else self.DUMMY_PDF_PATH

        if not file_path_override and not os.path.exists(file_path_on_disk):
            if file_type == "image":
                try:
                    from PIL import Image
                    img = Image.new('RGB', (60, 30), color = 'red')
                    img.save(file_path_on_disk)
                    print(f"Created {file_path_on_disk} for file uploads.")
                except ImportError:
                    print("Pillow not installed. Cannot create dummy_image.png. Please install it (`pip install Pillow`) or create the file manually.")
                    raise Exception(f"Failed to create {file_path_on_disk}, which is required for file uploads.")
            elif file_type == "pdf":
                try:
                    from reportlab.pdfgen import canvas
                    c = canvas.Canvas(file_path_on_disk)
                    c.drawString(100, 750, "Dummy PDF Content")
                    c.save()
                    print(f"Created {file_path_on_disk} for file uploads.")
                except ImportError:
                    print("ReportLab not installed. Cannot create CPV.pdf. Please install it (`pip install reportlab`) or create the file manually.")
                    raise Exception(f"Failed to create {file_path_on_disk}, which is required for file uploads.")
            else:
                raise ValueError(f"Unsupported file_type: {file_type}")

        api_path = f"/api/upload-file?reason={reason}&customerId={self.customer_id}"
        if document_type:
            api_path += f"&documentType={urllib.parse.quote(document_type)}"

        headers = {} # _make_authenticated_request will add Authorization and signature
        if is_mask:
            headers['isMask'] = 'true'
        if document_type:
            headers['documentType'] = document_type

        content_type = mimetypes.guess_type(file_path_on_disk)[0] or "application/octet-stream"
        with open(file_path_on_disk, "rb") as upload_file:
            files = {"avatar": (os.path.basename(file_path_on_disk), upload_file, content_type)}
            response = await self._make_authenticated_request(
                'POST',
                api_path,
                files=files,
                headers=headers
            )
        response_data = response.json()

        self.file_path = response_data['uploadFile']['path']
        self.file_originalname = response_data['uploadFile']['originalname']

        if is_mask and 'maskedData' in response_data:
            self.masked_identity_proof = response_data['maskedData']['path']
            self.unmasked_identity_proof = response_data['uploadFile']['path']

        print(f'File uploaded successfully. Path: {self.file_path}')
        return response_data

    @staticmethod
    def _find_customer_record(value, customer_id: str):
        if isinstance(value, dict):
            record_id = value.get("id") or value.get("customerId")
            unique_id = value.get("customerUniqueId") or value.get("uniqueId") or value.get("unique_id")
            if str(record_id) == str(customer_id) and unique_id:
                return value
            for nested in value.values():
                record = GoldLoanApiTest._find_customer_record(nested, customer_id)
                if record:
                    return record
        elif isinstance(value, list):
            for item in value:
                record = GoldLoanApiTest._find_customer_record(item, customer_id)
                if record:
                    return record
        return None

    async def _search_customers(self, filters: dict) -> dict:
        """GET /api/customer with `filters`, widening the scope when the user's own list is empty.

        `viewAllCustomer=false` only lists customers belonging to the logged-in user/branch. The
        harness creates customers under the ADMIN token, so a customer from an earlier run is often
        absent from the appraiser's own list even though the record exists — retry with
        `viewAllCustomer=true` before concluding it isn't there.
        """
        payload = {}
        for view_all in ("false", "true"):
            params = {"viewAllCustomer": view_all, "from": "1", "to": "100", **filters}
            api_path = "/api/customer?" + urllib.parse.urlencode(params)
            response = await self._make_authenticated_request('GET', api_path)
            payload = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else None
            if rows:
                if view_all == "true":
                    print("Customer is outside this user's own list — found with viewAllCustomer=true.")
                return payload
        return payload

    @property
    def full_name(self) -> str:
        """First + last name; last name is optional (single-name Aadhaar / AUTO KYC customers)."""
        return f"{self.first_name} {self.last_name}".strip()

    async def _fetch_customer_unique_id(self) -> None:
        if self.customer_unique_id:
            filters = {"customerUniqueId": self.customer_unique_id}
        else:
            filters = {"mobileNumber": self.mobile_number}

        customer = self._find_customer_record(await self._search_customers(filters), self.customer_id)

        assert customer, (
            f"Customer {self.customer_id} was not found in the customer lookup response."
        )

        self.customer_unique_id = str(
            customer.get("customerUniqueId")
            or customer.get("uniqueId")
            or customer.get("unique_id")
        )
        # The web app sends the customer record's userType with submit-all-kyc-info / ops approval.
        self.customer_user_type = customer.get("userType") or self.customer_user_type

        print(f"Resolved customerUniqueId for customer {self.customer_id}.")

    async def get_customer_by_id(self) -> dict:
        api_path = f"/api/customer/{self.customer_id}"
        response = await self._make_authenticated_request('GET', api_path)
        data = response.json()
        customer = data.get("singleCustomer") if isinstance(data, dict) else None
        if customer:
            self.customer_unique_id = str(
                customer.get("customerUniqueId") or self.customer_unique_id
            )
            self.mobile_number = str(customer.get("mobileNumber") or self.mobile_number)
            self.first_name = str(customer.get("firstName") or self.first_name)
            self.last_name = str(customer.get("lastName") or self.last_name)
            self.module_id = str(customer.get("moduleId") or self.module_id)
            self.state_id = str(customer.get("stateId") or self.state_id)
            self.city_id = str(customer.get("cityId") or self.city_id)
            self.random_pincode = str(customer.get("pinCode") or self.random_pincode)
            self.pan_type = customer.get("panType") or self.pan_type
            self.random_pan = customer.get("panCardNumber") or self.random_pan
            self.pan_image = customer.get("panImage") or self.pan_image
            self.form60_image = customer.get("form60Image") or self.form60_image
            self.kyc_status = str(customer.get("kycStatus") or self.kyc_status or "")
            # Already-filled personal info (used by the loan step for an existing customer) — take
            # the real stored values instead of regenerating them.
            if customer.get("gender"):
                self.gender = customer.get("gender")
            if customer.get("dateOfBirth"):
                self.dob = str(customer.get("dateOfBirth"))[:10]  # ISO -> YYYY-MM-DD
            if customer.get("age"):
                try:
                    self.age = int(customer.get("age"))
                except (TypeError, ValueError):
                    pass
            if customer.get("motherName"):
                self.mother_name = str(customer.get("motherName"))
            branch = customer.get("internalBranch") if isinstance(customer.get("internalBranch"), dict) else {}
            if branch.get("id"):
                self.customer_branch_id = str(branch["id"])
        print(f"Fetched customer {self.customer_id} (kycStatus={self.kyc_status or 'unknown'}).")

    async def appraiser_request_branch_id(self):
        """The portal posts the appraiser request with the CUSTOMER's branch
        (leadData.internalBranch.id), falling back to the login's branch."""
        if not self.customer_branch_id and self.customer_id:
            try:
                await self.get_customer_by_id()
            except httpx.HTTPStatusError as e:
                print(f"Customer lookup for the branch failed ({e.response.status_code}); using the login branch.")
        branch = self.customer_branch_id or self.internal_branch_id
        return int(branch) if str(branch).isdigit() else None
        return data

    async def get_kyc_customer_detail(self) -> dict:
        # KYC v2: fetch by customerId + kycType (the old /api/kyc/get-customer-detail is superseded).
        api_path = "/api/kyc/v2/get-customer-detail"
        request_body = {
            "customerId": str(self.customer_id),
            "moduleId": str(self.module_id),
            "kycType": self.kyc_type,
        }
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        data = response.json()
        customer_info = data.get("customerInfo") if isinstance(data, dict) else None
        if customer_info:
            # customerKycId lives on the nested customerKycPersonal for an existing KYC record.
            personal_id = customer_info.get("customerKycPersonal") or {}
            if isinstance(personal_id, dict) and personal_id.get("customerKycId"):
                self.customer_kyc_id = str(personal_id.get("customerKycId"))
            self.first_name = customer_info.get("firstName") or self.first_name
            self.last_name = customer_info.get("lastName") or self.last_name
            self.pan_type = customer_info.get("panType") or self.pan_type
            self.random_pan = customer_info.get("panCardNumber") or self.random_pan
            self.pan_image = customer_info.get("panImage") or self.pan_image
            self.form60_image = customer_info.get("form60Image") or self.form60_image
            # Already-filled personal info at the top level.
            if customer_info.get("gender"):
                self.gender = customer_info.get("gender")
            if customer_info.get("dateOfBirth"):
                self.dob = str(customer_info.get("dateOfBirth"))[:10]
            if customer_info.get("age"):
                try:
                    self.age = int(customer_info.get("age"))
                except (TypeError, ValueError):
                    pass
            # customerKycPersonal holds the real KYC-submitted values (profile/signature images,
            # marital/spouse, identity proof). Reuse them for an existing customer instead of
            # regenerating/re-uploading.
            personal = customer_info.get("customerKycPersonal") or {}
            if isinstance(personal, dict):
                self.profile_image = personal.get("profileImage") or self.profile_image
                self.signature_proof = personal.get("signatureProof") or self.signature_proof
                self.martial_status = personal.get("martialStatus") or self.martial_status
                self.spouse_name = personal.get("spouseName") or self.spouse_name
                self.identity_proof_number = personal.get("identityProofNumber") or self.identity_proof_number
                if personal.get("gender"):
                    self.gender = personal.get("gender")
                if personal.get("dateOfBirth"):
                    self.dob = str(personal.get("dateOfBirth"))[:10]
                if personal.get("age"):
                    try:
                        self.age = int(personal.get("age"))
                    except (TypeError, ValueError):
                        pass
            addresses = customer_info.get("customerKycAddress") or []
            if addresses:
                self.customer_details["existingCustomerKycAddress"] = addresses
        print(f"Fetched KYC customer detail for {self.customer_id} "
              f"(gender={self.gender or '?'}, dob={self.dob or '?'}, sign={'yes' if self.signature_proof else 'no'}).")
        return data

    async def fetch_existing_e_kyc(self) -> dict:
        api_path = f"/api/e-kyc/data?customerId={self.customer_id}"
        response = await self._make_authenticated_request('GET', api_path)
        print(f"Fetched existing e-KYC data for customer {self.customer_id}.")
        return response.json()

    async def _use_existing_customer(self) -> None:
        """Load the minimum state needed to start at appraiser request/loan flow."""
        self.customer_id = self.existing_customer_id
        self.mobile_number = ""
        self.first_name = ""
        self.last_name = ""
        self.module_id = "1"
        self.customer_unique_id = self.existing_customer_unique_id
        assert self.customer_id or self.customer_unique_id, (
            "Set GOLD_LOAN_EXISTING_CUSTOMER_ID or GOLD_LOAN_EXISTING_CUSTOMER_UNIQUE_ID."
        )
        if self.customer_id and (not self.mobile_number or not self.first_name):
            await self.get_customer_by_id()
        if self.customer_unique_id and (
            not self.customer_id or not self.mobile_number or not self.first_name
        ):
            payload = await self._search_customers({"customerUniqueId": self.customer_unique_id})

            def find_by_unique_id(value):
                if isinstance(value, dict):
                    unique_id = value.get("customerUniqueId") or value.get("uniqueId")
                    if unique_id == self.customer_unique_id and (value.get("id") or value.get("customerId")):
                        return value
                    for nested in value.values():
                        record = find_by_unique_id(nested)
                        if record:
                            return record
                elif isinstance(value, list):
                    for item in value:
                        record = find_by_unique_id(item)
                        if record:
                            return record
                return None

            customer = find_by_unique_id(payload)
            other_env = "uat" if self.env_name == "test" else "test"
            assert customer, (
                f"Customer {self.customer_unique_id} was not found on "
                f"{self.env_name.upper()} ({self.BASE_URL}) — searched this user's list and all "
                f"customers. Check the unique id, or try the other environment "
                f"(--env {other_env})."
            )
            self.customer_id = str(customer.get("id") or customer.get("customerId"))
            self.mobile_number = self.mobile_number or str(customer.get("mobileNumber") or "")
            self.first_name = self.first_name or str(customer.get("firstName") or "")
            self.last_name = self.last_name or str(customer.get("lastName") or "")
        # lastName is optional: AUTO KYC (Aadhaar) customers with a single name have none.
        assert self.mobile_number and self.first_name, (
            "Customer lookup did not return the mobile number and first name required for loan processing."
        )
        if not self.customer_unique_id:
            await self._fetch_customer_unique_id()
        print(f"Using existing customer {self.customer_id} for loan processing.")

    async def release_kyc_documents(self, pan: str = None, aadhaar: str = None) -> dict:
        """TEST/UAT only: free a PAN and/or Aadhaar from the KYC records (POST /api/e-kyc/remove),
        so a document already registered on some customer can be registered again. Only the Origin
        header is needed (the shared client always sends it). Returns the server's answer, which
        says what was actually freed (panCardUpdated / identityProofNumberUpdated)."""
        if self.env_name not in ("test", "uat"):
            raise RuntimeError(f"Refusing to release KYC documents on environment {self.env_name!r}.")
        body = {"identityProofNumber": aadhaar or None, "panCardNumber": (pan or "").upper() or None}
        response = await self.client.post("/api/e-kyc/remove", json=body)
        response.raise_for_status()
        result = response.json() if response.content else {}
        masked_pan = f"{body['panCardNumber'][:2]}******{body['panCardNumber'][-2:]}" if body['panCardNumber'] else "-"
        masked_aadhaar = f"XXXXXXXX{aadhaar[-4:]}" if aadhaar else "-"
        print(f"Released KYC documents on {self.env_name.upper()}: PAN {masked_pan} "
              f"(freed: {result.get('panCardUpdated')}), Aadhaar {masked_aadhaar} "
              f"(freed: {result.get('identityProofNumberUpdated')}).")
        return result

    async def release_kyc_documents_if_enabled(self) -> None:
        """With --release-kyc-docs, free the identity profile's real PAN (and the full Aadhaar when the
        profile has all 12 digits) before a real-identity KYC. The auto-mode Aadhaar is already unique
        per customer (random 8 + real last 4), so the PAN is what normally blocks a re-run."""
        if not self.release_kyc_docs or not self.kyc_uses_real_identity:
            return
        pan = self.kyc_profile.get("panCardNumber")
        digits = re.sub(r"\D", "", str(self.kyc_profile.get("aadhaarNumber", "")))
        await self.release_kyc_documents(pan=pan, aadhaar=digits if len(digits) == 12 else None)

    async def _run_full_kyc(self) -> None:
        """The complete KYC submission flow (KYC v2), used for new customers and not-yet-approved
        existing ones. Mirrors the captured v2 flow: get-customer-detail -> submit-basic-info ->
        consent OTP -> customer-kyc-address -> customer-kyc-personal -> submit-all-kyc-info ->
        ops-team approval. (No PUT /kyc/initiate and no PAN/Aadhaar auto-verification in v2.)"""
        self._print_kyc_mode_header()
        await self.release_kyc_documents_if_enabled()   # --release-kyc-docs (TEST/UAT)
        await self.get_customer_by_id()
        try:
            await self.get_kyc_customer_detail()  # v2: also captures an existing customerKycId
        except httpx.HTTPStatusError as e:
            # A brand-new customer has no KYC record yet; submit-basic-info creates it below.
            print(f"get-customer-detail (v2) not available yet (non-fatal): {e.response.status_code}")
        await self.submit_basic_info()            # v2: records PAN, returns/creates customerKycId
        await self._kyc_consent_otp()             # v2: send-otp + verify-otp-admin (kycConsent)
        await self.load_kyc_master_data()
        await self.fetch_existing_e_kyc()
        await self.save_customer_address()        # v2: prepares + POSTs customer-kyc-address
        await self.save_customer_personal_details()  # v2: customer-kyc-personal
        await self.submit_all_kyc_info()
        await self.kyc_ops_approval()
        await self.get_customer_by_id()           # Refresh customer data after KYC approval.
        self._print_kyc_verification_summary()

    def _print_kyc_mode_header(self) -> None:
        """State up front which identity path this run takes and what it is using."""
        described = {
            self.KYC_MODE_MANUAL: "generated dummy PAN + Aadhaar, no verification calls",
            self.KYC_MODE_AUTO: "REAL PAN + REAL offline-Aadhaar XML, verified via /api/e-kyc",
            self.KYC_MODE_OVD: "REAL OVD (no PAN) + Form 97, verified via /api/e-kyc/v2",
        }[self.kyc_mode]
        details = {"Mode": f"{self.kyc_mode}  -  {described}"}
        if self.kyc_mode == self.KYC_MODE_AUTO:
            pan = self.kyc_profile.get("panCardNumber", "")
            aadhaar = re.sub(r"\D", "", str(self.kyc_profile.get("aadhaarNumber", "")))
            details["PAN"] = f"{pan[:2]}{'*' * 6}{pan[-2:]}" if len(pan) >= 4 else "(set)"
            details["Aadhaar"] = f"XXXXXXXX{aadhaar[-4:]}"
            details["Aadhaar XML"] = self.kyc_profile.get("aadhaarXmlPath")
        elif self.kyc_mode == self.KYC_MODE_OVD:
            details["OVD type"] = f"{self.kyc_profile.get('ovdType')} -> ovdType={self.ovd_type!r}"
            details["OVD number"] = self.ovd_number
            details["Endpoint"] = self.OVD_CATALOG[self.kyc_profile["ovdType"]]["path"]
            details["Form 97"] = self.kyc_profile.get("form97Path") or "(dummy PDF)"
        self._print_summary_table("KYC IDENTITY MODE", details)

    def _print_kyc_verification_summary(self) -> None:
        """Close the KYC step with what actually verified, so a run that fell back to the manual
        data path can never be mistaken for a successful real-identity run."""
        degraded = self.kyc_mode_effective != self.kyc_mode_requested
        details = {
            "Requested mode": self.kyc_mode_requested,
            "Effective mode": self.kyc_mode_effective + ("  (DEGRADED)" if degraded else ""),
            "PAN verified": self.pan_verified,
            "Aadhaar verified": self.aadhaar_verified,
            "OVD verified": self.ovd_verified,
        }
        if self.ovd_verified and self.ovd_name:
            details["Name on OVD"] = f"{self.ovd_name}  (customer: {self.full_name})"
        self._print_summary_table("KYC VERIFICATION RESULT", details)
        for step, outcome, detail in self.kyc_verification_log:
            print(f"    {self._G['dot']} {step}: {outcome}" + (f" - {detail}" if detail else ""))

    async def _existing_customer_kyc_ready(self) -> bool:
        """Return True if the existing customer's KYC is already approved (loan can proceed)."""
        data = await self.get_customer_by_id()
        customer = data.get("singleCustomer", {}) if isinstance(data, dict) else {}
        status = str(customer.get("kycStatus") or "").lower()
        self.kyc_status = status
        # Pull KYC detail so PAN/name/address are populated for the loan step.
        try:
            await self.get_kyc_customer_detail()
        except httpx.HTTPStatusError as e:
            print(f"get-customer-detail unavailable (non-fatal): {e.response.status_code}")
        return status == "approved"

    async def _prepare_loan_fields_from_existing(self) -> None:
        """Populate the fields the loan step needs that normally come from the KYC personal step,
        for an existing customer whose KYC is already approved (so we skip KYC submission)."""
        # Master data (occupation/religion/income/etc.) is required by loan basic-details.
        if not all([
            self.occupation_id, self.religion_id, self.physical_challenge_id,
            self.political_exposed_id, self.special_category_id, self.cis_id,
            self.bsr_id, self.annual_income, self.qualification_id,
        ]):
            await self.load_kyc_master_data()
        if not self.gender:
            self.gender = self.random.choice(["m", "f", "o"])
        if not self.age or not self.dob:
            dob_and_age = self._generate_random_dob_and_age()
            self.age = int(dob_and_age["age"])
            self.dob = dob_and_age["dob"]
        if not self.mother_name:
            mother_names = self._generate_random_name()
            self.mother_name = f"{mother_names['firstName']} {mother_names['lastName']}"
        if not self.signature_proof:
            signature_upload = await self._upload_file("customer", file_type="image")
            self.signature_proof = signature_upload['uploadFile']['path']
        # loan-documents requires an identity proof. For an existing customer we skip KYC, so
        # masked/unmasked identity proof are never set -> loan-documents 400s "Identity proof is
        # required". Upload a fresh masked aadhaar (same call KYC uses) so the paths exist.
        if not self.masked_identity_proof or not self.unmasked_identity_proof:
            masked_resp = await self._upload_file(
                "customer", file_type="image", document_type="aadhar", is_mask=True,
                file_path_override=self.AADHAR_IMAGE_PATH,
            )
            masked_data = masked_resp.get('maskedData') or {}
            self.masked_identity_proof = (masked_data.get('path')
                                          or masked_resp['uploadFile']['path'])
            self.unmasked_identity_proof = masked_resp['uploadFile']['path']
            print(f"Uploaded identity proof for existing customer (masked={self.masked_identity_proof}).")
        # e-KYC data (non-fatal) mirrors what the Apply-Loan screen fetches.
        try:
            await self.fetch_existing_e_kyc()
        except httpx.HTTPStatusError as e:
            print(f"e-KYC fetch unavailable (non-fatal): {e.response.status_code}")
        print(f"Prepared loan fields for existing customer {self.customer_id} (KYC pre-approved).")


    async def add_customer(self):
        api_path = "/api/customer"
        # On a real-identity run the customer must BE the person the documents belong to: the
        # verification APIs compare the submitted name against the issuing authority's record
        # (verify-voter-id returns a nameComparison block), so a random name would not reconcile.
        random_names = self._generate_random_name()
        self.first_name = self.kyc_profile.get("firstName") or random_names['firstName']
        self.last_name = self.kyc_profile.get("lastName") or random_names['lastName']
        if self.kyc_uses_real_identity and self.kyc_profile.get("firstName"):
            print(f"Using the identity profile's name for the new customer: {self.full_name}")
        self.random_pincode = self._generate_random_pincode()
        # Always mint a FRESH PAN for a new customer. A loaded session may have restored a PAN from a
        # prior run that is already registered on the server -> "PAN Card already exists!". Generating
        # it here (before submit-basic-info's `if not self.random_pan` guard) guarantees uniqueness.
        self.random_pan = self._generate_random_pan()
        self.mobile_number = self._generate_random_mobile_number()  # Generate for new customer

        await self._fetch_states()
        await self._fetch_cities(self.state_id)
        await self._fetch_modules()
        await self._fetch_status()
        # The portal's form defaults state/city to the LOGGED-IN user's (verify-login userDetails)
        # and fixes the status to 1; this step runs under the admin token.
        user = self._user_details_by_token.get(self.auth_token) or {}
        if user.get('stateId') and user.get('cityId'):
            self.state_id, self.city_id = str(user['stateId']), str(user['cityId'])
        lead_source, lead_source_id = await self._pick_lead_source()

        await self._send_register_otp()

        request_body = {
            "firstName": self.first_name,
            "lastName": self.last_name,
            "mobileNumber": self.mobile_number,
            "otp": self.CUSTOMER_OTP,
            "referenceCode": self.reference_code,
            "panCardNumber": "",
            "stateId": int(self.state_id) if str(self.state_id).isdigit() else self.state_id,
            "cityId": int(self.city_id) if str(self.city_id).isdigit() else self.city_id,
            "pinCode": self.random_pincode,
            "dateTime": datetime.datetime.now(datetime.timezone.utc)
                .isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            "statusId": 1,              # the portal patches statusId to 1
            "dateOfBirth": None,
            "panType": None,            # '' is sent as null by the portal
            "form60": "",
            "panImage": None,
            "panImg": None,
            "comment": "",
            "leadSource": lead_source,
            "source": "",
            "moduleId": self.module_id,
            "form60Image": None,
            "form60Img": None,
            "sourceFrom": None,
            "createBtRequest": False,
            "leadSourceId": lead_source_id,
        }
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        self._print_json_block("Cookies after OTP", dict(self.client.cookies))
        response_body = response.json()
        data = response_body.get("data") if isinstance(response_body, dict) else None
        customer = (
            response_body.get("customer")
            or (data.get("customer") if isinstance(data, dict) else None)
            or data
            or response_body
        )
        assert isinstance(customer, dict), f"Unexpected customer-create response: {response_body}"
        customer_id = customer.get("id") or customer.get("customerId")
        assert customer_id, f"Customer-create response did not include an ID: {response_body}"
        self.customer_id = str(customer_id)
        self.customer_unique_id = str(
            customer.get("customerUniqueId") or customer.get("uniqueId") or customer.get("unique_id") or ""
        )
        if not self.customer_unique_id:
            await self._fetch_customer_unique_id()
        print(f'Customer created successfully. Customer ID: {self.customer_id}')


    async def _pick_lead_source(self):
        """The portal's lead-source dropdown: GET /api/lead/lead-user; the chosen entry's
        user.leadName is sent as leadSource and its userId as leadSourceId. Prefer the configured
        name (self.lead_source) when the server lists it, else the first entry."""
        response = await self._make_authenticated_request('GET', "/api/lead/lead-user")
        rows = [r for r in (response.json().get('data') or []) if isinstance(r, dict)]
        named = [r for r in rows if (r.get('user') or {}).get('leadName')]
        chosen = next((r for r in named if r['user']['leadName'] == self.lead_source), None) or (named[0] if named else None)
        if not chosen:
            return self.lead_source, ''
        print(f"Lead source: {chosen['user']['leadName']!r} (userId {chosen.get('userId')}).")
        return chosen['user']['leadName'], chosen.get('userId') or ''

    # --- KYC identity profile + auto-verification -------------------------------------------------

    def _load_kyc_profile(self) -> dict:
        """Real identity data for the auto/ovd KYC modes.

        Real PAN / Aadhaar / OVD numbers cannot be generated -- the verification APIs check them
        against the issuing authority, so they must be SUPPLIED. Two sources, env wins over file:
          1. a JSON file at GOLD_LOAN_KYC_PROFILE (default <project-root>/config/kyc_identity.json)
          2. per-field GOLD_LOAN_KYC_* overrides (see KYC_PROFILE_ENV_KEYS)
        The file holds real PII, so config/ is gitignored; keep it off the repo and out of shell
        history. Returns {} in manual mode -- nothing real is needed there.
        """
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default_path = os.path.join(project_root, "config", "kyc_identity.json")
        path = os.getenv("GOLD_LOAN_KYC_PROFILE", default_path)
        profile = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if not isinstance(loaded, dict):
                    raise ValueError("the KYC profile file must contain a JSON object")
                profile = {k: v for k, v in loaded.items() if v not in (None, "")}
                print(f"Loaded KYC identity profile from {path} ({len(profile)} field(s)).")
            except (OSError, ValueError, json.JSONDecodeError) as e:
                raise ValueError(f"Could not read the KYC identity profile at {path}: {e}") from e
        # Per-field env overrides win over the file.
        for field, env_key in self.KYC_PROFILE_ENV_KEYS.items():
            value = os.getenv(env_key, "").strip()
            if value:
                profile[field] = value
        # Accept friendly spellings of the OVD type and normalise to an OVD_CATALOG key.
        if profile.get("ovdType"):
            aliases = {t.lower(): t for t in self.OVD_TYPES}
            aliases.update({
                "dl": "drivingLicense", "drivinglicense": "drivingLicense",
                "drivinglicence": "drivingLicense", "driving_license": "drivingLicense",
                "voter": "voterId", "voterid": "voterId", "voter_id": "voterId",
                "epic": "voterId",
            })
            key = str(profile["ovdType"]).strip().lower().replace(" ", "").replace("-", "")
            profile["ovdType"] = aliases.get(key, profile["ovdType"])
        return profile

    @staticmethod
    def _resolve_profile_path(path: str) -> str:
        """Resolve a profile file path against the PROJECT ROOT, not the current directory.

        Every other asset in the harness is anchored to the project root (see __init__) so a run
        works from any cwd; profile paths must behave the same, otherwise a perfectly correct
        "assets/voter-card.jpg" resolves from the repo root and fails from anywhere else.
        Absolute paths are left alone.
        """
        path = str(path or "").strip()
        if not path or os.path.isabs(path):
            return path
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        anchored = os.path.join(project_root, path)
        # Prefer the project-root reading, but honour a file that really is relative to the cwd.
        return anchored if os.path.exists(anchored) or not os.path.exists(path) else os.path.abspath(path)

    def _validate_kyc_profile(self) -> None:
        """Fail fast at startup when the chosen mode is missing identity data, rather than after a
        login + customer creation. Also resolves every file path in the profile to an absolute one
        (anchored at the project root) so a typo surfaces now and the run is cwd-independent."""
        for key in ("aadhaarXmlPath", "ovdImagePath", "form97Path"):
            if self.kyc_profile.get(key):
                self.kyc_profile[key] = self._resolve_profile_path(self.kyc_profile[key])
        required = self.KYC_MODE_REQUIRED_FIELDS[self.kyc_mode]
        missing = [f for f in required if not self.kyc_profile.get(f)]
        if missing:
            hints = ", ".join(f"{f} ({self.KYC_PROFILE_ENV_KEYS[f]})" for f in missing)
            raise ValueError(
                f"KYC mode '{self.kyc_mode}' needs real identity data, but these fields are "
                f"missing: {hints}.\n"
                f"Supply them in a JSON profile (GOLD_LOAN_KYC_PROFILE, default "
                f"config/kyc_identity.json) or via the GOLD_LOAN_KYC_* env vars. "
                f"Use --kyc manual to run with generated dummy data instead."
            )
        if self.kyc_mode == self.KYC_MODE_AUTO:
            xml_path = self.kyc_profile.get("aadhaarXmlPath")
            if not os.path.exists(xml_path):
                raise ValueError(f"Aadhaar offline e-KYC XML not found: {xml_path} "
                                 f"(GOLD_LOAN_KYC_AADHAAR_XML).")
            digits = re.sub(r"\D", "", str(self.kyc_profile.get("aadhaarNumber", "")))
            if len(digits) not in (4, 12):
                raise ValueError(f"aadhaarNumber must be the 12-digit Aadhaar or its last 4 digits, "
                                 f"got {len(digits)} digits (GOLD_LOAN_KYC_AADHAAR).")
            self.kyc_profile["aadhaarNumber"] = digits
        if self.kyc_mode == self.KYC_MODE_OVD:
            ovd_type = self.kyc_profile.get("ovdType")
            if ovd_type not in self.OVD_CATALOG:
                raise ValueError(
                    f"ovdType '{ovd_type}' is not supported. Captured OVD endpoints exist for "
                    f"{list(self.OVD_CATALOG)} (GOLD_LOAN_KYC_OVD_TYPE). Passport and NREGA appear "
                    f"in CR-2 but have no captured verification call yet.")
            spec = self.OVD_CATALOG[ovd_type]
            if spec["needs_dob"] and not self.kyc_profile.get("dateOfBirth"):
                raise ValueError(f"OVD type '{ovd_type}' verifies against a date of birth; set "
                                 f"dateOfBirth (GOLD_LOAN_KYC_DOB, YYYY-MM-DD).")
            # The captured OVD ovdType string is only confirmed for Voter ID; allow an override
            # rather than making a wrong guess unfixable without a code change.
            override = os.getenv("GOLD_LOAN_KYC_OVD_TYPE_STRING", "").strip()
            self.ovd_type = override or spec["ovdType"]
            self.ovd_number = str(self.kyc_profile["ovdNumber"]).strip()
            if not spec["confirmed"] and not override:
                print(f"  NOTE: ovdType string {self.ovd_type!r} for '{ovd_type}' is inferred from "
                      f"the identity-type name, not captured. Override with "
                      f"GOLD_LOAN_KYC_OVD_TYPE_STRING if the server rejects it.")
            for key, env_key in (("ovdImagePath", "GOLD_LOAN_KYC_OVD_IMAGE"),
                                 ("form97Path", "GOLD_LOAN_KYC_FORM97")):
                path_value = self.kyc_profile.get(key)
                if path_value and not os.path.exists(path_value):
                    raise ValueError(f"{key} file not found: {path_value} ({env_key}).")

    @property
    def kyc_identity_type_id(self) -> int:
        """/api/identity-type id of the document standing as identity proof in the KYC address /
        submit-all section: Aadhaar Card = 5.

        This stays Aadhaar on the OVD path too, and deliberately so. That section validates
        identityProofNumber as an Aadhaar -- an OVD number there is rejected with "Invalid Adhaar
        card format!" -- so the number, the uploaded masked/unmasked images and this type id must
        all remain Aadhaar and agree with each other. The OVD is carried on submit-basic-info,
        which is the only place ovd.har actually shows it.

        GOLD_LOAN_KYC_IDENTITY_TYPE_ID overrides it if a future capture shows otherwise; the
        catalogue's per-OVD ids (Voter ID 2, Driving License 3) are kept in OVD_CATALOG for when
        that capture exists.
        """
        override = os.getenv("GOLD_LOAN_KYC_IDENTITY_TYPE_ID", "").strip()
        if override.isdigit():
            return int(override)
        return self._aadhaar_identity_type_id or 5  # looked up from /api/identity-type; 5 = Aadhaar Card

    async def _lookup_identity_types(self) -> None:
        """The portal fills identityTypeId from GET /api/identity-type (name 'Aadhaar Card')."""
        if self._aadhaar_identity_type_id:
            return
        try:
            response = await self._make_authenticated_request('GET', "/api/identity-type")
            rows = response.json().get('data') if isinstance(response.json(), dict) else response.json()
            match = next((r for r in (rows or []) if isinstance(r, dict)
                          and str(r.get('name', '')).strip().lower() == 'aadhaar card'), None)
            if match and str(match.get('id', '')).isdigit():
                self._aadhaar_identity_type_id = int(match['id'])
        except httpx.HTTPStatusError as e:
            print(f"identity-type lookup failed ({e.response.status_code}); using the default Aadhaar id 5.")

    @property
    def kyc_uses_real_identity(self) -> bool:
        """True while the run is still on a real-data path (i.e. has not degraded to manual)."""
        return self.kyc_mode_effective in (self.KYC_MODE_AUTO, self.KYC_MODE_OVD)

    def _kyc_note(self, step: str, ok: bool, detail: str = "") -> None:
        """Record + print one verification outcome. The trail is reprinted in the run summary so a
        run that quietly degraded is visible at a glance."""
        self.kyc_verification_log.append((step, "verified" if ok else "not verified", detail))
        glyph = self._G["ok"] if ok else self._G["warn"]
        colour = "green" if ok else "yellow"
        print(self._c(f"  {glyph} {step}: {'verified' if ok else 'NOT verified'}"
                      + (f" - {detail}" if detail else ""), colour))

    def _kyc_degrade_to_manual(self, reason: str) -> None:
        """The server told us to switch to manual (3 consecutive failures), so stop pretending the
        identity was verified: fall back to the dummy-data path but keep the requested mode on
        record. Loud on purpose -- a silently-degraded run looks like a passing auto run."""
        if self.kyc_mode_effective == self.KYC_MODE_MANUAL:
            return
        self.kyc_mode_effective = self.KYC_MODE_MANUAL
        print(self._c(f"  {self._G['warn']} KYC AUTO-VERIFICATION UNAVAILABLE -> falling back to "
                      f"MANUAL data path", "yellow", "bold"))
        print(self._c(f"     reason: {reason}", "yellow"))
        self.kyc_verification_log.append(("mode", "degraded to manual", reason))

    @staticmethod
    def _kyc_iso_dob(dob: str) -> str:
        """DOB in the form verify-pan expects: 1982-08-30 -> 1982-08-30T00:00:00.000Z."""
        dob = str(dob or "").strip()
        if not dob:
            return None
        return dob if "T" in dob else f"{dob[:10]}T00:00:00.000Z"

    async def _verify_pan_auto(self) -> bool:
        """POST /api/e-kyc/verify-pan -- verify a REAL PAN against the issuing authority.

        Captured body: {customerId, panCardNumber, dateOfBirth}. The response carries
        `switchToManual` / `switchToManualReason`; the server counts the attempts itself and flips
        the flag on the third consecutive failure. We retry to the same ceiling and honor that
        signal instead of imposing our own policy. Returns True only on a genuine verification.
        """
        pan = self.kyc_profile.get("panCardNumber") or self.random_pan
        dob = self._kyc_iso_dob(self.kyc_profile.get("dateOfBirth"))
        body = {
            "customerId": int(self.customer_id) if str(self.customer_id).isdigit() else self.customer_id,
            "panCardNumber": pan,
            "dateOfBirth": dob,
        }
        for attempt in range(1, self.KYC_VERIFY_MAX_ATTEMPTS + 1):
            try:
                response = await self._make_authenticated_request(
                    'POST', "/api/e-kyc/verify-pan", json_data=body)
                payload = response.json() if response.content else {}
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                if data.get("switchToManual") or payload.get("switchToManual"):
                    reason = data.get("switchToManualReason") or payload.get("switchToManualReason") or ""
                    self._kyc_note("PAN", False, reason)
                    self._kyc_degrade_to_manual(reason or "PAN verification switched to manual")
                    return False
                # The portal treats PAN as verified ONLY when data.isPanVerified is true.
                if "isPanVerified" in data and not data.get("isPanVerified"):
                    print(f"  verify-pan attempt {attempt}/{self.KYC_VERIFY_MAX_ATTEMPTS} -> 200 but "
                          "isPanVerified is false.")
                    continue
                self.pan_verified = True
                # The portal replaces the customer's name with the PAN record's (last word = last name).
                pan_name = str(data.get("name") or "").strip()
                if pan_name and pan_name.lower() != self.full_name.lower():
                    parts = pan_name.split()
                    self.first_name = " ".join(parts[:-1]) or parts[0]
                    self.last_name = parts[-1] if len(parts) > 1 else ""
                    print(f"  PAN name {pan_name!r} replaces the customer name, as the portal does.")
                self._kyc_note("PAN", True, f"{pan} (attempt {attempt})")
                return True
            except httpx.HTTPStatusError as e:
                payload = self._safe_json(e.response)
                reason = (payload.get("switchToManualReason") or payload.get("reason")
                          or payload.get("message") or e.response.text[:120])
                print(f"  verify-pan attempt {attempt}/{self.KYC_VERIFY_MAX_ATTEMPTS} "
                      f"-> {e.response.status_code}: {reason}")
                if payload.get("switchToManual"):
                    self._kyc_note("PAN", False, reason)
                    self._kyc_degrade_to_manual(reason)
                    return False
        # Ran out of attempts without the server ever setting switchToManual.
        self._kyc_note("PAN", False, f"failed {self.KYC_VERIFY_MAX_ATTEMPTS} attempt(s)")
        self._kyc_degrade_to_manual(
            f"verify-pan failed {self.KYC_VERIFY_MAX_ATTEMPTS} time(s) without a switchToManual signal")
        return False

    async def _verify_aadhaar_xml_auto(self) -> bool:
        """POST /api/e-kyc/offline-aadhaar-xml -- verify the UIDAI offline e-KYC XML.

        Captured body: {file: "data:application/xml;base64,<xml>", customerId,
        aadhaarNumber: <CryptoJS-encrypted>, maskedAadhaarNumber: "XXXXXXXX" + last 4}. The XML and
        the Aadhaar number must belong to the same person -- a mismatch is rejected with "Aadhaar
        number does not match with the XML data", which is exactly what the captured (deliberately
        mismatched) session shows. Same 3-attempt switchToManual contract as verify-pan.
        """
        import base64
        # The per-customer number built in save_customer_address (random 8 + real last 4).
        aadhaar = self.identity_proof_number or self._aadhaar_with_last4(
            re.sub(r"\D", "", str(self.kyc_profile.get("aadhaarNumber", "")))[-4:])
        xml_path = self.kyc_profile.get("aadhaarXmlPath")
        with open(xml_path, "rb") as fh:
            encoded = base64.b64encode(fh.read()).decode("ascii")
        body = {
            "file": f"data:application/xml;base64,{encoded}",
            "customerId": int(self.customer_id) if str(self.customer_id).isdigit() else self.customer_id,
            # Same CryptoJS envelope every other identity number uses.
            "aadhaarNumber": self._encrypt_identity_proof_number(aadhaar),
            "maskedAadhaarNumber": f"XXXXXXXX{aadhaar[-4:]}",
        }
        for attempt in range(1, self.KYC_VERIFY_MAX_ATTEMPTS + 1):
            try:
                response = await self._make_authenticated_request(
                    'POST', "/api/e-kyc/offline-aadhaar-xml", json_data=body)
                payload = response.json() if response.content else {}
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                if data.get("switchToManual") or payload.get("switchToManual"):
                    reason = data.get("reason") or payload.get("reason") or ""
                    self._kyc_note("Aadhaar XML", False, reason)
                    self._kyc_degrade_to_manual(reason or "Aadhaar switched to manual")
                    return False
                # The portal treats Aadhaar as verified ONLY when data.isAahaarVerified is true.
                if "isAahaarVerified" in data and not data.get("isAahaarVerified"):
                    print(f"  offline-aadhaar-xml attempt {attempt}/{self.KYC_VERIFY_MAX_ATTEMPTS} -> 200 "
                          "but isAahaarVerified is false.")
                    continue
                self.aadhaar_verified = True
                # A verified XML is authoritative for the name (CR-1: Aadhaar name overrides LOS).
                aadhaar_name = data.get("aahaarName") or data.get("name")
                if aadhaar_name:
                    self.name_as_per_aadhaar = str(aadhaar_name)
                self._kyc_note("Aadhaar XML", True, f"XXXXXXXX{aadhaar[-4:]} (attempt {attempt})")
                return True
            except httpx.HTTPStatusError as e:
                payload = self._safe_json(e.response)
                reason = (payload.get("reason") or payload.get("message") or e.response.text[:120])
                print(f"  offline-aadhaar-xml attempt {attempt}/{self.KYC_VERIFY_MAX_ATTEMPTS} "
                      f"-> {e.response.status_code}: {reason}")
                if payload.get("switchToManual"):
                    self._kyc_note("Aadhaar XML", False, reason)
                    self._kyc_degrade_to_manual(reason)
                    return False
        self._kyc_note("Aadhaar XML", False, f"failed {self.KYC_VERIFY_MAX_ATTEMPTS} attempt(s)")
        self._kyc_degrade_to_manual(
            f"offline-aadhaar-xml failed {self.KYC_VERIFY_MAX_ATTEMPTS} time(s) "
            f"without a switchToManual signal")
        return False

    @staticmethod
    def _kyc_ddmmyyyy_dob(dob: str) -> str:
        """DOB in the form verify-dl expects: 1996-11-25 -> 25-11-1996.

        Note this differs from verify-pan, which takes an ISO timestamp -- the two e-KYC endpoints
        genuinely disagree on the format, so both helpers exist.
        """
        dob = str(dob or "").strip()[:10]
        if not dob:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob):
            y, m, d = dob.split("-")
            return f"{d}-{m}-{y}"
        return dob  # already DD-MM-YYYY

    async def _verify_ovd_auto(self) -> bool:
        """Verify the supplied Officially Valid Document (the CR-2 'PAN not available' path).

        One endpoint per document type -- see OVD_CATALOG:
            Voter ID       POST /api/e-kyc/v2/verify-voter-id  {epicNo, customerId}
            Driving Licence POST /api/e-kyc/v2/verify-dl       {dlNo, dob (DD-MM-YYYY), customerId}

        A success carries isVerified/isDocumentValid plus a `nameComparison` block
        (customerName vs ovdName, nameMatchScore, isNameMatch) and the same
        switchToManual/reason/attemptCount contract as verify-pan, so the retry policy is shared.
        The verified document's own name is recorded for the CR-2 name-reconciliation trail.
        """
        spec = self.OVD_CATALOG[self.kyc_profile["ovdType"]]
        body = {
            spec["number_field"]: self.ovd_number,
            "customerId": int(self.customer_id) if str(self.customer_id).isdigit() else self.customer_id,
        }
        if spec["needs_dob"]:
            body["dob"] = self._kyc_ddmmyyyy_dob(self.kyc_profile.get("dateOfBirth"))
        label = f"OVD {self.ovd_type}"
        for attempt in range(1, self.KYC_VERIFY_MAX_ATTEMPTS + 1):
            try:
                response = await self._make_authenticated_request('POST', spec["path"], json_data=body)
                payload = response.json() if response.content else {}
                if payload.get("switchToManual"):
                    self._kyc_note(label, False, payload.get("reason") or "")
                    self._kyc_degrade_to_manual(payload.get("reason") or "OVD switched to manual")
                    return False
                # A 200 that is not actually a verification must not be read as one.
                if payload.get("isVerified") is False:
                    self._kyc_note(label, False, payload.get("message") or "isVerified=false")
                    self._kyc_degrade_to_manual(payload.get("message") or "OVD not verified")
                    return False
                self.ovd_verified = True
                self.ovd_verification_data = payload.get("data") or {}
                name_cmp = payload.get("nameComparison") or {}
                self.ovd_name = str(name_cmp.get("ovdName") or self.ovd_verification_data.get("name") or "")
                detail = f"{self.ovd_number} (attempt {attempt})"
                if name_cmp:
                    detail += (f"; name match {name_cmp.get('isNameMatch')} "
                               f"score={name_cmp.get('nameMatchScore')} "
                               f"({name_cmp.get('customerName')!r} vs {name_cmp.get('ovdName')!r})")
                self._kyc_note(label, True, detail)
                return True
            except httpx.HTTPStatusError as e:
                payload = self._safe_json(e.response)
                reason = (payload.get("reason") or payload.get("message") or e.response.text[:120])
                print(f"  {spec['path']} attempt {attempt}/{self.KYC_VERIFY_MAX_ATTEMPTS} "
                      f"-> {e.response.status_code}: {reason}")
                if payload.get("switchToManual"):
                    self._kyc_note(label, False, reason)
                    self._kyc_degrade_to_manual(reason)
                    return False
        self._kyc_note(label, False, f"failed {self.KYC_VERIFY_MAX_ATTEMPTS} attempt(s)")
        self._kyc_degrade_to_manual(f"OVD verification failed {self.KYC_VERIFY_MAX_ATTEMPTS} time(s)")
        return False

    # Server phrasings that mean "this document is already in the database against someone else".
    # Captured: verify-dl -> "This DL number is already registered with another customer.";
    # customer creation -> "PAN Card already exists!".
    DUPLICATE_DOCUMENT_MARKERS = (
        "already registered",
        "already exists",
        "already linked",
        "already associated",
        "already mapped",
        "already used",
        "already taken",
        "is already in use",
    )

    @classmethod
    def _is_duplicate_document_error(cls, text: str) -> bool:
        """True when a rejection means the document is already on file for another customer."""
        low = str(text or "").lower()
        return any(marker in low for marker in cls.DUPLICATE_DOCUMENT_MARKERS)

    def _abort_on_duplicate_document(self, document: str, reason: str) -> None:
        """Stop the run the instant a document turns out to be already registered.

        Requested behaviour: any KYC document already present in the DB ends the test immediately
        rather than falling back to manual, because a run that quietly degrades would report a pass
        for a document that can never be used.
        """
        self._kyc_note(document, False, reason)
        self.kyc_verification_log.append(("mode", "aborted", f"{document} already registered"))
        print(self._c(f"  {self._G['fail']} {document.upper()} ALREADY REGISTERED - STOPPING THE RUN",
                      "red", "bold"))
        raise KycDocumentAlreadyExistsError(
            f"{document} is already registered in the KYC database: {reason}\n"
            f"  The run stops here rather than falling back to dummy data.\n"
            f"  Use a document that is not yet registered, or run against the customer that "
            f"already owns it (--customer <uniqueId>)."
        )

    @staticmethod
    def _safe_json(response) -> dict:
        """Parse a response body as JSON, returning {} instead of raising on a non-JSON error page."""
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    async def submit_basic_info(self):
            # KYC v2: submit-basic-info replaces the old /api/kyc/customer-info. It records the PAN/
            # basic details and returns the customerKycId (creating the KYC record for a fresh
            # customer). PAN auto-verification (/api/e-kyc/verify-pan) is skipped -- it 503s in the
            # captured flow and the manual path continues with isPanVerified=false.
            api_path = "/api/kyc/v2/submit-basic-info"

            if self.kyc_mode == self.KYC_MODE_OVD:
                # CR-2 "PAN not available" path (ovd.har): panType 'ovd', panCardNumber null, an OVD
                # scan and a Form 97 PDF uploaded, and the OVD verified through its own endpoint.
                self.pan_type = "ovd"
                self.random_pan = None
                self.pan_image = None
                self.form60_image = None
                if not self.ovd_image:
                    ovd_src = self.kyc_profile.get("ovdImagePath")
                    upload_result = await self._upload_file(
                        "lead", file_type="image",
                        **({"file_path_override": ovd_src} if ovd_src else {}))
                    self.ovd_image = upload_result['uploadFile']['path']
                if not self.form97_image:
                    form97_src = self.kyc_profile.get("form97Path")
                    upload_result = await self._upload_file(
                        "lead", file_type="pdf",
                        **({"file_path_override": form97_src} if form97_src else {}))
                    self.form97_image = upload_result['uploadFile']['path']
                await self._verify_ovd_auto()
            else:
                # PAN path. In auto mode the PAN is the real one from the identity profile and is
                # verified against the issuing authority; in manual mode it stays generated and
                # unverified, relying on the server's manual-validation fallback.
                if self.kyc_mode == self.KYC_MODE_AUTO and self.kyc_profile.get("panCardNumber"):
                    self.random_pan = self.kyc_profile["panCardNumber"]
                if not self.random_pan:
                    self.random_pan = self._generate_random_pan()
                if not self.pan_type or self.pan_type == "ovd":
                    self.pan_type = os.getenv("GOLD_LOAN_PAN_TYPE", "pan").lower()
                if self.pan_type not in self.PAN_TYPES:
                    raise ValueError(f"GOLD_LOAN_PAN_TYPE must be one of {list(self.PAN_TYPES)}.")

                if self.pan_type == "pan" and not self.pan_image:
                    upload_result = await self._upload_file(
                        "lead", file_type="image", file_path_override=self.PAN_IMAGE_PATH)
                    self.pan_image = upload_result['uploadFile']['path']
                    self.form60_image = None
                elif self.pan_type != "pan" and not self.form60_image:
                    upload_result = await self._upload_file("lead", file_type="pdf")
                    self.form60_image = upload_result['uploadFile']['path']
                    self.pan_image = None

                if self.kyc_mode == self.KYC_MODE_AUTO:
                    await self._verify_pan_auto()

            # isAutoApproved tracks whether an identity document actually verified (captured: true
            # on the successful OVD submission, false on the manual PAN one).
            is_auto_approved = bool(self.pan_verified or self.ovd_verified)
            request_body = {
                "id": int(self.customer_id) if self.customer_id and str(self.customer_id).isdigit() else self.customer_id,
                "moduleId": str(self.module_id),
                "firstName": self.first_name,
                "lastName": self.last_name,
                "panType": self.pan_type,
                "panCardNumber": self.random_pan,
                "panImage": self.pan_image,
                "panImg": f"{self.BASE_URL}/{self.pan_image}" if self.pan_image else None,
                "ovdImage": self.ovd_image or None,
                "ovdImg": f"{self.BASE_URL}/{self.ovd_image}" if self.ovd_image else None,
                "ovdNumber": self.ovd_number or None,
                "ovdType": self.ovd_type or None,
                "dateOfBirth": None,
                "isAutoApproved": is_auto_approved,
                "isPanVerified": self.pan_verified,
                "isOvdVerified": self.ovd_verified,
                "kycType": self.kyc_type,
            }
            # Form 97 replaces Form 60 under IT Rules 2026 and rides along on the OVD path only.
            if self.form97_image:
                request_body["form97Image"] = self.form97_image
                request_body["form97Img"] = f"{self.BASE_URL}/{self.form97_image}"
            response = await self._make_authenticated_request(
                'POST',
                api_path,
                json_data=request_body
            )
            data = response.json().get('data', {}) if isinstance(response.json(), dict) else {}
            if data.get('customerKycId'):
                self.customer_kyc_id = str(data['customerKycId'])
            if data.get('customerId'):
                self.customer_id = str(data['customerId'])
            print(f'Submit basic info successful. Customer KYC ID: {self.customer_kyc_id}')

    async def _kyc_consent_otp(self):
        """New v2 step: capture KYC consent via OTP (send-otp -> verify-otp-admin). The consent
        is required before the KYC personal/address submission in the v2 flow. The admin OTP is a
        fixed test value (123456), matching the captured flow."""
        try:
            send = await self._make_authenticated_request(
                'POST', "/api/customer-otp/send-otp",
                json_data={"type": "kycConsent", "customerId": int(self.customer_id) if str(self.customer_id).isdigit() else self.customer_id},
            )
            self.kyc_reference_code = str(send.json().get("referenceCode") or "")
            if not self.kyc_reference_code:
                print("KYC consent OTP: no referenceCode returned; skipping verify.")
                return
            await self._make_authenticated_request(
                'POST', "/api/customer-otp/verify-otp-admin",
                json_data={"type": "kycConsent", "referenceCode": self.kyc_reference_code, "otp": "123456"},
            )
            print("KYC consent OTP verified.")
        except httpx.HTTPStatusError as e:
            print(f"KYC consent OTP step failed (continuing): {e.response.status_code} - {e.response.text[:120]}")


    async def _fetch_special_categories(self):
        api_path = "/api/special-category"
        response = await self._make_authenticated_request('GET', api_path)
        categories = response.json().get('data')
        assert categories, "No special categories found."
        self.special_category_id = str(self.random.choice(categories).get('id'))
        print(f'Fetched random special_category_id: {self.special_category_id}')


    async def _fetch_occupations(self):
        api_path = "/api/occupation/list"
        response = await self._make_authenticated_request('GET', api_path)
        occupations = response.json().get('data')
        low_risk_occupations = [
            occupation
            for occupation in occupations
            if occupation.get("riskCategory", "").lower() == "low"
        ]
        assert low_risk_occupations, "No low-risk occupations found."
        occupation = self.random.choice(low_risk_occupations)
        self.occupation_id = str(occupation["id"])
        # The portal makes industryCategoryId / industryId MANDATORY for an occupation flagged
        # isIndustryRequired: pick both at random from its lookups.
        self.industry_category_id = self.industry_id = None
        if occupation.get("isIndustryRequired"):
            await self._pick_random_industry()
        print(f"Fetched random low-risk occupation_id: {self.occupation_id}")


    @staticmethod
    def _rows(payload):
        data = payload.get('data') if isinstance(payload, dict) else payload
        if isinstance(data, dict):   # some list endpoints nest the rows one level down
            data = next((v for v in data.values() if isinstance(v, list)), [])
        return [r for r in (data or []) if isinstance(r, dict) and r.get('id') is not None]

    async def _pick_random_industry(self) -> None:
        """Random industry category, then a random industry inside it (the portal's
        getIndustriesCategory -> getIndustriesByCategory dropdowns)."""
        response = await self._make_authenticated_request('GET', "/api/industry/categories")
        categories = self._rows(response.json())
        self.random.shuffle(categories)
        for category in categories:
            response = await self._make_authenticated_request(
                'GET', f"/api/industry/by-category-list/{category['id']}")
            industries = self._rows(response.json())
            if industries:
                industry = self.random.choice(industries)
                self.industry_category_id, self.industry_id = category['id'], industry['id']
                print(f"Occupation {self.occupation_id} needs an industry: picked category "
                      f"{category['id']} / industry {industry['id']} at random.")
                return
        raise RuntimeError(f"Occupation {self.occupation_id} requires an industry, but no industry "
                           "category with industries was found.")

    async def _fetch_religions(self):
        api_path = "/api/religion"
        response = await self._make_authenticated_request('GET', api_path)
        religions = response.json().get('data')
        assert religions, "No religions found."
        self.religion_id = str(self.random.choice(religions).get('id'))
        print(f'Fetched random religion_id: {self.religion_id}')


    async def _fetch_physical_challenges(self):
        api_path = "/api/physical-challenge"
        response = await self._make_authenticated_request('GET', api_path)
        challenges = response.json().get('data')
        assert challenges, "No physical challenges found."
        self.physical_challenge_id = str(self.random.choice(challenges).get('id'))
        print(f'Fetched random physical_challenge_id: {self.physical_challenge_id}')


    async def _fetch_political_exposed(self):
        api_path = "/api/political-exposed"
        response = await self._make_authenticated_request('GET', api_path)
        exposed = response.json().get("data")
        assert exposed, "No political exposed categories found."
        selected = next(
            item for item in exposed
            if item.get("riskCategory", "").upper() == "NA"
        )
        self.political_exposed_id = str(selected["id"])
        print(f"Fetched political_exposed_id: {self.political_exposed_id}")


    async def _fetch_cis(self):
        api_path = "/api/cis"
        response = await self._make_authenticated_request('GET', api_path)
        cis_values = response.json().get('data')
        assert cis_values, "No CIS values found."
        self.cis_id = str(self.random.choice(cis_values).get('id'))
        print(f'Fetched random cis_id: {self.cis_id}')


    async def _fetch_bsr(self):
        api_path = "/api/bsr"
        response = await self._make_authenticated_request('GET', api_path)
        bsr_values = response.json().get('data')
        assert bsr_values, "No BSR values found."
        self.bsr_id = str(self.random.choice(bsr_values).get('id'))
        print(f'Fetched random bsr_id: {self.bsr_id}')


    async def _fetch_annual_incomes(self):
        api_path = "/api/annual-income"
        response = await self._make_authenticated_request('GET', api_path)
        incomes = response.json().get('data')
        assert incomes, "No annual incomes found."
        self.annual_income = self.random.choice(incomes).get('incomeRange')
        print(f'Fetched random annual_income: {self.annual_income}')


    async def _fetch_qualifications(self):
        api_path = "/api/qualification"
        response = await self._make_authenticated_request('GET', api_path)
        qualifications = response.json().get('data')
        assert qualifications, "No qualifications found."
        self.qualification_id = str(self.random.choice(qualifications).get('id'))
        print(f'Fetched random qualification_id: {self.qualification_id}')


    async def load_kyc_master_data(self):
        await asyncio.gather(
            self._fetch_occupations(),
            self._fetch_religions(),
            self._fetch_physical_challenges(),
            self._fetch_political_exposed(),
            self._fetch_special_categories(),
            self._fetch_cis(),
            self._fetch_bsr(),
            self._fetch_annual_incomes(),
            self._fetch_qualifications(),
        )
        print('KYC master data loaded.')


    async def save_customer_personal_details(self):
        api_path = "/api/kyc/v2/customer-kyc-personal"  # v2 endpoint
        # DOB/gender: a real identity profile is authoritative -- regenerating them here would
        # contradict the PAN/Aadhaar/OVD the run just verified against those very values.
        profile_dob = self.kyc_profile.get("dateOfBirth") if self.kyc_uses_real_identity else None
        if profile_dob:
            self.dob = str(profile_dob)[:10]
            self.age = self._age_from_dob(self.dob)
        else:
            dob_and_age = self._generate_random_dob_and_age()
            self.age = int(dob_and_age['age'])
            self.dob = dob_and_age['dob']
        profile_gender = self.kyc_profile.get("gender") if self.kyc_uses_real_identity else None
        # A verified OVD reports the holder's gender ("M"/"F"); prefer it over a random pick.
        ovd_gender = str(self.ovd_verification_data.get("gender") or "").strip().lower()[:1]
        self.gender = (str(profile_gender).strip().lower()[:1] if profile_gender
                       else (ovd_gender if ovd_gender in ("m", "f", "o")
                             else self.random.choice(["m", "f", "o"])))
        self.martial_status = self.random.choice(["single", "married", "divorced"])
        spouse_names = self._generate_random_name()
        self.spouse_name = f"{spouse_names['firstName']} {spouse_names['lastName']}"
        mother_names = self._generate_random_name()
        self.mother_name = f"{mother_names['firstName']} {mother_names['lastName']}"

        profile_upload = await self._upload_file("customer", file_type="image")
        signature_upload = await self._upload_file("customer", file_type="image")
        self.profile_image = profile_upload['uploadFile']['path']
        self.signature_proof = signature_upload['uploadFile']['path']
        signature_file_name = signature_upload['uploadFile'].get('originalname')

        if not all([
            self.occupation_id,
            self.religion_id,
            self.physical_challenge_id,
            self.political_exposed_id,
            self.special_category_id,
            self.cis_id,
            self.bsr_id,
            self.annual_income,
            self.qualification_id,
        ]):
            await self.load_kyc_master_data()

        request_body = {
            "customerId": self.customer_id,
            "customerKycId": self.customer_kyc_id,
            "profileImage": self.profile_image,
            "profileImg": f"{self.BASE_URL}/{self.profile_image}" if self.profile_image else None,
            "alternateMobileNumber": '',
            "gender": self.gender,
            "spouseName": self.spouse_name,
            "martialStatus": self.martial_status,
            "signatureProof": self.signature_proof,
            "signatureProofImg": f"{self.BASE_URL}/{self.signature_proof}" if self.signature_proof else None,
            "signatureProofFileName": signature_file_name,
            "occupationId": int(self.occupation_id) if self.occupation_id else None, # Convert to int
            "industryCategoryId": self.industry_category_id,   # required when the occupation isIndustryRequired
            "industryId": self.industry_id,
            "dateOfBirth": self.dob,
            "age": int(self.age) if str(self.age).isdigit() else self.age,   # the portal computes a number
            "moduleId": self.module_id,
            "userType": self.customer_user_type or None,   # customerDetails.userType
            "email": None,
            "alternateEmail": None,
            "landLineNumber": None,
            "gstinNumber": None,
            "cinNumber": None,
            "constitutionsDeed": [],
            "constitutionsDeedFileName": None,
            "gstCertificate": [],
            "gstCertificateFileName": None,
            "annualIncome": self.annual_income,
            "motherName": self.mother_name,
            "religionId": int(self.religion_id) if self.religion_id else None, # Convert to int
            "qualificationId": int(self.qualification_id) if self.qualification_id else None, # Convert to int
            "physicalChallengeId": int(self.physical_challenge_id) if self.physical_challenge_id else None, # Convert to int
            "politicalExposedId": int(self.political_exposed_id) if self.political_exposed_id else None, # Convert to int
            "specialCategoryId": int(self.special_category_id) if self.special_category_id else None, # Convert to int
            "kycType": self.kyc_type,  # v2 requires kycType
        }
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        response_body = response.json()
        review = response_body.get("customerKycReview") if isinstance(response_body, dict) else None
        personal_review = (
            review.get("customerKycPersonal")
            if isinstance(review, dict) and isinstance(review.get("customerKycPersonal"), dict)
            else None
        )
        if personal_review and personal_review.get("id"):
            self.customer_details["customerKycPersonalId"] = personal_review["id"]
        self.customer_details['customerKycPersonal'] = request_body
        print('Save customer personal details successful.')


    async def _fetch_address_proof_types(self):
        api_path = "/api/address-proof-type"
        response = await self._make_authenticated_request('GET', api_path)
        types = response.json().get('data')
        assert types, "No address proof types found."
        self.address_proof_type_id = str(self.random.choice(types).get('id'))
        print(f'Fetched random address_proof_type_id: {self.address_proof_type_id}')


    async def _fetch_geo_location(self, address: str) -> None:
        """Resolve an address to lat/long (as the UI does before submit-all-kyc-info)."""
        api_path = "/api/geo-location/lat-long-by-address"
        try:
            response = await self._make_authenticated_request(
                'POST', api_path, json_data={"address": address}
            )
            data = response.json().get("data") if isinstance(response.json(), dict) else None
            if isinstance(data, dict):
                self.latitude = data.get("lat")
                self.longitude = data.get("lng")
            print(f"Fetched geo-location: lat={self.latitude}, lng={self.longitude}")
        except httpx.HTTPStatusError as e:
            print(f"Geo-location lookup failed (non-fatal): {e.response.status_code}")

    async def save_customer_address(self):
        # NOTE: This intentionally does NOT call POST /api/kyc/customer-kyc-address.
        # That endpoint runs Aadhaar auto-verification and rejects unverifiable (test) Aadhaar
        # numbers with "Invalid Adhaar card format!". Mirroring the UI's manual-validation
        # fallback (after auto-verification fails, any Aadhaar/PAN is accepted), we only *prepare*
        # the address + identity data here and submit it via submit-all-kyc-info, exactly like the
        # captured working KYC flow (which never calls customer-kyc-address).
        await self._fetch_address_proof_types()
        await self._lookup_identity_types()   # identityTypeId for Aadhaar, as the portal looks it up

        # The identity proof depends on the KYC mode:
        #   auto   -- the REAL Aadhaar from the profile, verified against its offline e-KYC XML.
        #   ovd    -- the verified OVD number stands in for Aadhaar (CR-2 'PAN not available').
        #   manual -- a generated Aadhaar, accepted by the server's manual-validation fallback.
        if self.kyc_mode == self.KYC_MODE_AUTO:
            # 8 random digits + the REAL last 4 (the only part the XML can be matched on), so each
            # customer gets a unique Aadhaar while the real XML still verifies. PAN has no such
            # workaround -- it stays the real one.
            self.identity_proof_number = self._aadhaar_with_last4(self.kyc_profile["aadhaarNumber"][-4:])
            print(f"Auto KYC Aadhaar: XXXXXXXX{self.identity_proof_number[-4:]} "
                  "(random 8 digits + the real last 4).")
            await self._verify_aadhaar_xml_auto()
        else:
            # Both manual AND ovd generate an Aadhaar here. The identity/address section validates
            # identityProofNumber as an AADHAAR regardless of the OVD selected on the basic-info
            # step: sending the OVD number (e.g. the EPIC "IRU0628347") is rejected by BOTH
            # customer-kyc-address and submit-all-kyc-info with "Invalid Adhaar card format!".
            # ovd.har stops at the consent OTP, so it does not cover this step -- until a capture
            # of customer-kyc-address on the OVD path exists, the proven manual behaviour stands.
            self.identity_proof_number = self._generate_random_aadhaar_number()
        # Do not encrypt file paths, only sensitive numbers
        self.encrypted_identity_proof_number = self._encrypt_identity_proof_number(
            self.identity_proof_number
        )
        same_as_permanent = self.random.choice([True, False])
        # A verified document's own name is authoritative (CR-1: Aadhaar name overrides the LOS
        # name; CR-2: the OVD/PAN name drives reconciliation in the fallback paths).
        if not self.name_as_per_aadhaar:
            self.name_as_per_aadhaar = self.ovd_name or self.full_name

        # The Aadhaar masking service needs a real Aadhaar *image* (not the dummy PDF); masking a
        # dummy PDF yields an invalid masked file that customer-kyc-address rejects with
        # "Invalid identityProof file at index 0".
        upload_masked_id_proof_response = await self._upload_file(
            "customer", file_type="image", document_type="aadhar", is_mask=True,
            file_path_override=self.AADHAR_IMAGE_PATH,
        )
        masked_data = upload_masked_id_proof_response.get('maskedData') or {}
        self.masked_identity_proof = masked_data.get('path') or upload_masked_id_proof_response['uploadFile']['path']
        self.unmasked_identity_proof = upload_masked_id_proof_response['uploadFile']['path']

        self.address_proof = (await self._upload_file("customer", file_type="pdf"))['uploadFile']['path']

        addresses = []
        permanent_address_details = self._generate_random_address()
        permanent_address = {
            "addressType": "permanent",
            "addressProofTypeId": int(self.address_proof_type_id) if self.address_proof_type_id else None, # Convert to int
            "addressProofNumber": self.identity_proof_number,
            "address": permanent_address_details['address'],
            "stateId": int(self.state_id) if self.state_id else None, # Convert to int
            "cityId": int(self.city_id) if self.city_id else None, # Convert to int
            "pinCode": self.random_pincode,
            "landmark": permanent_address_details['landmark'],
            "addressProof": [self.address_proof],
            "unMaskedAddressProof": [self.unmasked_identity_proof],
        }
        addresses.append(permanent_address)

        if same_as_permanent:
            residential_address = permanent_address.copy()
            residential_address["addressType"] = "residential"
            addresses.append(residential_address)
        else:
            residential_address_details = self._generate_random_address()
            residential_address = {
                "addressType": "residential",
                "addressProofTypeId": int(self.address_proof_type_id) if self.address_proof_type_id else None, # Convert to int
                "addressProofNumber": self.identity_proof_number,
                "address": residential_address_details['address'],
                "stateId": int(self.state_id) if self.state_id else None, # Convert to int
                "cityId": int(self.city_id) if self.city_id else None, # Convert to int
                "pinCode": self._generate_random_pincode(),
                "landmark": residential_address_details['landmark'],
                "addressProof": [self.address_proof],
                "unMaskedAddressProof": [self.unmasked_identity_proof],
            }
            addresses.append(residential_address)

        # Resolve coordinates for the permanent address (submit-all-kyc-info includes lat/long).
        await self._fetch_geo_location(permanent_address["address"])

        # Store the prepared address + identity data for submit-all-kyc-info.
        self.customer_details['customerKycAddress'] = addresses

        # KYC v2: POST customer-kyc-address (manual path — isAutoApproved=false, encrypted identity
        # proofs). Non-fatal: submit-all-kyc-info is the authoritative submission, so a rejection
        # here (e.g. Aadhaar verification quirks) does not block the flow.
        try:
            enc = self._encrypt_identity_proof_number
            v2_addresses = []
            for a in addresses:
                v2_addresses.append({
                    "addressType": a.get("addressType"),
                    "addressProofTypeId": str(a.get("addressProofTypeId") or self.address_proof_type_id or ""),
                    "addressProofNumber": self.encrypted_identity_proof_number,
                    "address": a.get("address"),
                    "stateId": a.get("stateId"),
                    "cityId": a.get("cityId"),
                    "pinCode": a.get("pinCode"),
                    "landmark": a.get("landmark"),
                    "addressProof": [enc(p) for p in (a.get("addressProof") or [])],
                    "unMaskedAddressProof": [],
                    "addressProofImg": [f"{self.BASE_URL}/{p}" for p in (a.get("addressProof") or [])],
                    "addressProofFileName": [enc(self.address_proof)] if self.address_proof else [],
                })
            v2_body = {
                "customerId": int(self.customer_id) if str(self.customer_id).isdigit() else self.customer_id,
                "customerKycId": int(self.customer_kyc_id) if str(self.customer_kyc_id).isdigit() else self.customer_kyc_id,
                "identityTypeId": self.kyc_identity_type_id,
                "identityProof": [enc(self.masked_identity_proof)] if self.masked_identity_proof else [],
                "unMaskedIdentityProof": [enc(self.unmasked_identity_proof)] if self.unmasked_identity_proof else [],
                "identityProofImg": [enc(f"{self.BASE_URL}/{self.masked_identity_proof}")] if self.masked_identity_proof else [],
                "identityProofFileName": [enc(self.masked_identity_proof)] if self.masked_identity_proof else [],
                "identityProofNumber": self.encrypted_identity_proof_number,
                "isAutoApproved": bool(self.aadhaar_verified or self.ovd_verified),
                "nameAsPerAadhaar": self.name_as_per_aadhaar,
                "file": None,
                "xmlFileName": None,
                "address": v2_addresses,
                "latitude": self.latitude,
                "longitude": self.longitude,
                "isCityEdit": False,
                "isAahaarVerified": self.aadhaar_verified,  # server's spelling, not a typo here
                "kycType": self.kyc_type,
            }
            await self._make_authenticated_request('POST', "/api/kyc/v2/customer-kyc-address", json_data=v2_body)
            print('Submitted customer-kyc-address (v2).')
        except httpx.HTTPStatusError as e:
            # A duplicate document never reaches here: _make_authenticated_request raises
            # KycDocumentAlreadyExistsError, which this handler deliberately does not catch.
            print(f"customer-kyc-address (v2) rejected (continuing via submit-all-kyc-info): "
                  f"{e.response.status_code} - {e.response.text[:120]}")

        print('Prepared customer address & identity data.')


    async def submit_all_kyc_info(self):
        api_path = "/api/kyc/submit-all-kyc-info"

        kyc_personal = self.customer_details.get('customerKycPersonal', {})
        kyc_address = self.customer_details.get('customerKycAddress', [])
        if isinstance(kyc_address, dict):
            kyc_address = kyc_address.get("address", [])
        kyc_address = [
            self._address_proof_fields(address, {
                **({"id": address["id"]} if isinstance(address, dict) and address.get("id") else {}),
                "customerKycId": int(address.get("customerKycId") or self.customer_kyc_id) if (address.get("customerKycId") or self.customer_kyc_id) else None, # Convert to int
                "customerId": int(address.get("customerId") or self.customer_id) if (address.get("customerId") or self.customer_id) else None, # Convert to int
                "addressType": address.get("addressType"),
                "address": address.get("address"),
                "stateId": int(address.get("stateId") or self.state_id) if (address.get("stateId") or self.state_id) else None, # Convert to int
                "cityId": int(address.get("cityId") or self.city_id) if (address.get("cityId") or self.city_id) else None, # Convert to int
                "pinCode": address.get("pinCode"),
                "addressProof": address.get("addressProof") or [],
                "unMaskedAddressProof": address.get("unMaskedAddressProof") or [],
                "addressProofFileName": address.get("addressProofFileName"),
                "addressProofTypeId": int(address.get("addressProofTypeId") or self.address_proof_type_id) if (address.get("addressProofTypeId") or self.address_proof_type_id) else None, # Convert to int
                "addressProofNumber": self.identity_proof_number,
                "landmark": address.get("landmark"),
            })
            for address in kyc_address
            if isinstance(address, dict)
        ]

        kyc_personal = dict(kyc_personal)
        for key in ("customerId", "customerKycId", "moduleId", "userType", "profileImg", "signatureProofImg"):
            kyc_personal.pop(key, None)
        # In customerKycPersonal the identity-proof PATHS are encrypted (CryptoJS passphrase form),
        # just like identityProofNumber. Sending plaintext paths here makes the server reject them
        # with "Invalid (unMasked) Identity Proof file". customerKycBasicDetails keeps plaintext.
        kyc_personal.update({
            "identityTypeId": self.kyc_identity_type_id,
            "identityProof": [self._encrypt_identity_proof_number(self.masked_identity_proof)] if self.masked_identity_proof else [],
            "unMaskedIdentityProof": [self._encrypt_identity_proof_number(self.unmasked_identity_proof)] if self.unmasked_identity_proof else [],
            "identityProofNumber": self.encrypted_identity_proof_number,
            "panCardNumber": self.random_pan,
            "nameAsPerAadhaar": self.name_as_per_aadhaar,
            "riskCategory": self._kyc_risk_category(),
        })
        if self.cis_id:
            kyc_personal["cisId"] = int(self.cis_id) if str(self.cis_id).isdigit() else self.cis_id
        if self.bsr_id:
            kyc_personal["bsrId"] = int(self.bsr_id) if str(self.bsr_id).isdigit() else self.bsr_id
        if kyc_personal.get("dateOfBirth") and "T" not in str(kyc_personal["dateOfBirth"]):
            kyc_personal["dateOfBirth"] = f"{kyc_personal['dateOfBirth']}T00:00:00.000Z"

        kyc_basic_details = {
            "id": int(self.customer_details.get("customerKycPersonalId") or self.customer_kyc_id) if (self.customer_details.get("customerKycPersonalId") or self.customer_kyc_id) else None, # Convert to int
            "profileImage": self.profile_image,
            "firstName": self.first_name,
            "lastName": self.last_name,
            "mobileNumber": self.mobile_number,
            "panCardNumber": self.random_pan,
            "panType": self.pan_type,
            "form60": None,
            "panImage": self.pan_image,
            "panImg": f"{self.BASE_URL}/{self.pan_image}" if self.pan_image else None,
            "identityTypeId": self.kyc_identity_type_id,
            "identityProof": [self.masked_identity_proof],
            "unMaskedIdentityProof": [self.unmasked_identity_proof],
            "identityProofFileName": None,
            "identityProofNumber": self.identity_proof_number,
            "userType": None,
            "organizationTypeId": None,
            "dateOfIncorporation": None,
            "form60Image": self.form60_image,
            "form60Img": f"{self.BASE_URL}/{self.form60_image}" if self.form60_image else None,
            "isCityEdit": (self.customer_details or {}).get("isCityEdit"),
            # The portal: "Lat: <residential latitude>, Lng: <residential longitude>".
            "geoAddress": f"Lat: {self.latitude}, Lng: {self.longitude}" if self.latitude else "",
            "nameAsPerAadhaar": self.name_as_per_aadhaar,
            "riskCategory": self._kyc_risk_category(),
        }

        request_body = {
            "customerId": int(self.customer_id) if self.customer_id else None, # Convert to int
            "customerKycId": int(self.customer_kyc_id) if self.customer_kyc_id else None, # Convert to int
            "customerKycPersonal": kyc_personal,
            "customerKycAddress": kyc_address,
            "customerKycBasicDetails": kyc_basic_details,
            "moduleId": int(self.module_id) if self.module_id else None, # Convert to int
            "userType": self.customer_user_type or "Individual",  # the customer record's userType
            "customerOrganizationDetail": None,
            "isCityEdit": (self.customer_details or {}).get("isCityEdit"),
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        payload = response.json() if response.content else {}
        self.submit_all_kyc_response = payload if isinstance(payload, dict) else {}
        print('Submit all KYC info successful.')

    AADHAAR_ADDRESS_PROOF_TYPE_ID = 2
    TRANSFER_MODES = ("neft", "imps", "rtgs")   # the portal's OFFLINE options, sent lowercased

    def _address_proof_fields(self, address: dict, item: dict) -> dict:
        """The portal's address-proof rule: when the proof type is AADHAAR (2) the proof re-uses the
        Aadhaar identity images, and the proof paths AND number are sent CryptoJS-encrypted, like
        the identity proof. Sent in plain text the server answers "Invalid Adhaar card format!"
        (seen whenever the random proof type landed on 2). Other types keep the stored proof and
        the entered number as they are."""
        if item.get("addressProofTypeId") == self.AADHAAR_ADDRESS_PROOF_TYPE_ID:
            enc = self._encrypt_identity_proof_number
            item["addressProof"] = [enc(self.masked_identity_proof)] if self.masked_identity_proof else []
            item["unMaskedAddressProof"] = [enc(self.unmasked_identity_proof)] if self.unmasked_identity_proof else []
            item["addressProofNumber"] = self.encrypted_identity_proof_number
        return item

    def _kyc_risk_category(self) -> str:
        """riskCategory as the server's KYC record holds it (the portal sends data.riskCategory)."""
        details = self.customer_details or {}
        personal = details.get('customerKycPersonal') if isinstance(details.get('customerKycPersonal'), dict) else {}
        return details.get('riskCategory') or personal.get('riskCategory') or "low"


    async def kyc_ops_approval(self):
        api_path = "/api/classification/ops-team"
        # The portal builds this from the submit-all-kyc-info response (kycDetails): its
        # KycClassification ids, moduleId and userType, plus the customer's kycType.
        submitted = self.submit_all_kyc_response or {}
        classification = submitted.get("KycClassification") if isinstance(submitted.get("KycClassification"), dict) else {}
        # The portal posts the KYC rating to ops-team only at ratingStage 2; at stage 1 it is the CCE's
        # turn (POST /api/classification/cce, a separate behaviour-rating form this harness does not
        # model). Every observed run comes back at stage 2 (CCE auto-approved) -- stop clearly if not.
        stage = submitted.get("ratingStage")
        if stage is not None and str(stage) != "2":
            raise RuntimeError(
                f"submit-all-kyc-info returned ratingStage={stage}: the portal would now ask the CCE for a "
                "KYC rating (POST /api/classification/cce) before ops can approve. That step is not "
                "automated here; rate the KYC as CCE in the portal, then re-run with --customer.")
        request_body = {
            "customerId": classification.get("customerId") or (int(self.customer_id) if self.customer_id else None),
            "customerKycId": classification.get("customerKycId") or (int(self.customer_kyc_id) if self.customer_kyc_id else None),
            "kycRatingFromBM": False,
            "kycStatusFromOperationalTeam": "approved",
            "reasonFromOperationalTeam": "",
            "moduleId": submitted.get("moduleId") or (int(self.module_id) if self.module_id else None),
            "userType": submitted.get("userType") or self.customer_user_type or "Individual",
            "kycType": self.kyc_type,
        }
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        print('KYC ops approval successful.')


    def _login_user_type_id(self):
        """userTypeId of the session's login (7 = appraiser), from its verify-login userDetails."""
        details = self._user_details_by_token.get(self.auth_token) or {}
        return details.get('userTypeId')

    async def _fetch_appraisers(self):
        api_path = f"/api/user/appraiser-list?internalBranchId={self.internal_branch_id}"
        response = await self._make_authenticated_request('GET', api_path)
        appraisers = response.json().get('data')
        assert appraisers, "No appraisers found."
        
        # Prioritize appraiser matching logged-in user ID or mobile number
        found_appraiser = None
        if self.logged_in_user_id:
            for appraiser in appraisers:
                if appraiser.get('id') == self.logged_in_user_id:
                    found_appraiser = appraiser
                    break
        if not found_appraiser and self.logged_in_mobile_number:
            for appraiser in appraisers:
                mobile = appraiser.get('mobileNumber') or appraiser.get('mobile')
                if mobile == self.logged_in_mobile_number:
                    found_appraiser = appraiser
                    break
        
        if found_appraiser:
            self.appraiser_id = str(found_appraiser.get('id'))
            first_name = found_appraiser.get('firstName') or ''
            last_name = found_appraiser.get('lastName') or ''
            self.logged_in_appraiser_name = f"{first_name} {last_name}".strip() or found_appraiser.get('name')
            self.logged_in_appraiser_mobile_number = found_appraiser.get('mobileNumber') or found_appraiser.get('mobile') or self.logged_in_mobile_number
            print(f'Fetched appraiser_id: {self.appraiser_id}, name: {self.logged_in_appraiser_name}, mobile: {self.logged_in_appraiser_mobile_number} for logged-in mobile number {self.logged_in_mobile_number}')
        elif self._login_user_type_id() == 7 and self.logged_in_user_id:
            # An APPRAISER login: the portal locks appraiserId to the login's own id (disabled
            # control, still sent). Never hand the loan to a random appraiser.
            self.appraiser_id = str(self.logged_in_user_id)
            print(f"Appraiser {self.appraiser_id} (the login) is not in this branch's appraiser list; "
                  "using the login's own id, as the portal does.")
        else:
            # Another role picks from the list in the portal; the harness picks one at random.
            random_appraiser = self.random.choice(appraisers)
            self.appraiser_id = str(random_appraiser.get('id'))
            first_name = random_appraiser.get('firstName') or ''
            last_name = random_appraiser.get('lastName') or ''
            self.logged_in_appraiser_name = f"{first_name} {last_name}".strip() or random_appraiser.get('name')
            self.logged_in_appraiser_mobile_number = random_appraiser.get('mobileNumber') or random_appraiser.get('mobile')
            print(f'Fetched random appraiser_id: {self.appraiser_id}, name: {self.logged_in_appraiser_name}, mobile: {self.logged_in_appraiser_mobile_number}')

    def _extract_master_loan_ids(self, master_loan) -> bool:
        """Populate loan_id/master_loan_id from a view-all masterLoan object. Returns True on success."""
        if not isinstance(master_loan, dict) or not master_loan.get("id"):
            return False
        self.master_loan_id = str(master_loan.get("id"))
        # loanId (customerLoanId) is usually nested under a customerLoan array/object.
        loan_id = None
        candidate = master_loan.get("customerLoan") or master_loan.get("customerLoanData") or master_loan.get("loan")
        if isinstance(candidate, list) and candidate and isinstance(candidate[0], dict):
            loan_id = candidate[0].get("id") or candidate[0].get("customerLoanId")
        elif isinstance(candidate, dict):
            loan_id = candidate.get("id") or candidate.get("customerLoanId")
        loan_id = loan_id or master_loan.get("customerLoanId") or master_loan.get("loanId")
        self.loan_id = str(loan_id or master_loan.get("id"))
        return True

    async def _list_appraiser_requests(self, search: str = "", max_pages: int = 8, page_size: int = 25):
        """Page through /appraiser-request/view-all and return every item.

        `search` is optional: the server's search filter does not reliably cover
        customerUniqueId, so callers fall back to an unfiltered scan.
        """
        items = []
        for page in range(max_pages):
            frm = page * page_size + 1
            to = frm + page_size - 1
            api_path = f"/api/appraiser-request/view-all?from={frm}&to={to}"
            if search:
                api_path += f"&search={urllib.parse.quote(str(search))}"
            response = await self._make_authenticated_request('GET', api_path)
            body = response.json() if response.content else {}
            body = body if isinstance(body, dict) else {}
            page_items = body.get("data")
            page_items = page_items if isinstance(page_items, list) else []
            items.extend(x for x in page_items if isinstance(x, dict))
            pagination = body.get("pagination") if isinstance(body.get("pagination"), dict) else {}
            if not page_items or not pagination.get("hasMore"):
                break
        return items

    def _match_appraiser_request(self, items):
        """Pick the item belonging to the current customer.

        Match by the item's *own* customer, then use the item-level id (the appraiser
        request id). NOTE: item["customer"]["id"] is the CUSTOMER id, not the request
        id — do not use it here.
        """
        for item in items:
            customer = item.get("customer") if isinstance(item.get("customer"), dict) else {}
            uid = str(customer.get("customerUniqueId") or item.get("customerUniqueId") or "")
            cid = str(item.get("customerId") or customer.get("id") or "")
            if (self.customer_unique_id and uid == str(self.customer_unique_id)) or \
               (self.customer_id and cid == str(self.customer_id)):
                return item
        return None

    async def _lookup_appraiser_request(self):
        """Find this customer's appraiser-request item using the CURRENT token, or None.

        The `search` filter is not guaranteed to index customerUniqueId, so try the cheap
        filtered lookups first and fall back to an unfiltered scan before giving up.
        """
        attempts = []
        if self.customer_unique_id:
            attempts.append(str(self.customer_unique_id))
        if self.mobile_number and str(self.mobile_number) not in attempts:
            attempts.append(str(self.mobile_number))
        attempts.append("")  # unfiltered scan

        for search in attempts:
            items = await self._list_appraiser_requests(search=search)
            match = self._match_appraiser_request(items)
            if match and match.get("id"):
                if not search:
                    print("Found the appraiser request only via an unfiltered view-all scan "
                          "(the search filter did not return it).")
                return match
        return None

    async def _fetch_existing_appraiser_request(self, required: bool = True) -> bool:
        """Resolve this customer's existing appraiser request id (and any master loan).

        Returns True when a request was found. With required=False a miss returns
        False instead of raising, so callers can report a better diagnosis.
        """
        match = await self._lookup_appraiser_request()

        if not match:
            # view-all is scoped to the logged-in user, so a request raised by another
            # appraiser/branch is invisible to us. Admin sees every branch (the same reason
            # add_customer and packet create/assign run as admin), so retry under that token.
            print("Appraiser request not visible to this login — retrying the lookup as admin.")
            saved_token = self.auth_token
            try:
                self.auth_token = await self._role_token("admin")
                match = await self._lookup_appraiser_request()
            except Exception as e:  # a failed admin login must not mask the real diagnosis
                print(f"Admin-scoped appraiser-request lookup failed: {e}")
                match = None
            finally:
                self.auth_token = saved_token
            if match:
                print(f"Found appraiser request {match.get('id')} under the ADMIN scope "
                      f"(appraiserId={match.get('appraiserId')}, branch={match.get('internalBranchId')}).")

        if not match:
            if not required:
                return False
            raise AssertionError(
                f"No existing appraiser request found for {self.customer_unique_id} "
                f"(customerId={self.customer_id}) on env '{self.env_name}' — searched by unique id, "
                "by mobile number, and with an unfiltered view-all scan, under both the appraiser "
                "and the admin scope."
            )

        self.appraiser_request_id = str(match["id"])
        # Capture the request/loan state so the caller can decide resume-vs-recreate.
        self._existing_request_status = str(match.get("status") or "").lower()
        self._existing_process_complete = bool(match.get("isProcessComplete"))
        self._existing_request_appraiser_id = match.get("appraiserId")
        ml = match.get("masterLoan") if isinstance(match.get("masterLoan"), dict) else {}
        self._existing_loan_completed = bool(ml.get("isLoanCompleted"))
        # If a loan was already initiated for this request, capture its ids so we can skip re-init.
        captured = self._extract_master_loan_ids(match.get("masterLoan"))
        print(
            f"Using existing appraiser request {self.appraiser_request_id} "
            f"(status={self._existing_request_status or 'unknown'}, "
            f"processComplete={self._existing_process_complete}, loanCompleted={self._existing_loan_completed}, "
            f"appraiserId={self._existing_request_appraiser_id})"
            + (f" — existing loan {self.loan_id}/{self.master_loan_id}" if captured else "")
            + "."
        )
        return True

    def _existing_request_is_mine(self) -> bool:
        """True when the existing request is assigned to the appraiser we are logged in as.

        Every loan-process call on someone else's request 400s with
        "This customer is not assign to you", so a request owned by another appraiser
        cannot be resumed -- it has to be cancelled and recreated under our own id.
        """
        owner = self._existing_request_appraiser_id
        if owner in (None, ""):
            return False  # unassigned: nothing binds it to us, so recreate rather than resume
        mine = {str(x) for x in (self.logged_in_user_id, self.appraiser_id) if x not in (None, "")}
        return str(owner) in mine

    def _existing_request_is_reusable(self) -> bool:
        """True when the already-existing request is still mid-flight AND is ours, so we can resume.

        A request whose loan is finished cannot carry a new Fresh Loan, and one assigned to a
        different appraiser is unusable from this login; both have to be cancelled and recreated.
        """
        if self._existing_process_complete or self._existing_loan_completed:
            return False
        return self._existing_request_is_mine()


    async def _fetch_my_branches(self) -> list:
        """GET /api/user/get-my-branches -> the internal branches this login can act in.

        Shape (captured): {"data": [{"name": "Augmont", "id": 1, "internalBranchUniqueId": ...}, ...]}
        """
        try:
            response = await self._make_authenticated_request('GET', "/api/user/get-my-branches")
            data = response.json().get("data") if response.content else None
        except httpx.HTTPStatusError as e:
            print(f"Could not list this user's branches: {e.response.status_code} - {e.response.text[:120]}")
            return []
        if not isinstance(data, list):
            return []
        return [b for b in data if isinstance(b, dict) and b.get("id")]

    def _branch_attempt_summary(self) -> str:
        """Render what the alternate-branch ladder tried, for an error message."""
        if not self._branch_attempts:
            return " (no alternate branch was available to try)"
        return " (tried " + "; ".join(self._branch_attempts) + ")"

    async def _create_request_in_another_branch(self, request_body, max_branches: int = 3):
        """Retry the appraiser-request create in another branch, keeping OUR OWN appraiserId.

        The non-destructive answer to "This product Request already Exists" on a request owned
        by someone else: rather than cancelling their loan (which the server refuses with 403
        "You are not allowed to cencel this loan" even under the admin token), ask for a fresh
        request in a different branch. This only works if the server's uniqueness key includes
        the branch; every attempt's response is printed either way, so a failed ladder says
        exactly what the server objected to.

        Returns the successful response, or None. On success `self.internal_branch_id` is
        updated so every later branch-scoped call agrees with the request we just created.
        """
        self._branch_attempts = []
        branches = await self._fetch_my_branches()
        current = str(self.internal_branch_id or "")
        candidates = [b for b in branches if str(b.get("id")) != current]
        if not candidates:
            print("No alternate branch is available to this login, so the create cannot be retried elsewhere.")
            return None
        if len(candidates) > max_branches:
            print(f"Retrying the create in {max_branches} of {len(candidates)} alternate branches "
                  f"(the remaining {len(candidates) - max_branches} are not attempted).")
            candidates = candidates[:max_branches]

        for branch in candidates:
            branch_id = int(branch["id"])
            body = {**request_body, "internalBranchId": branch_id}
            print(f"Retrying the appraiser request in branch {branch_id} ({branch.get('name')!r}) "
                  f"as appraiser {request_body.get('appraiserId')}.")
            try:
                response = await self._make_authenticated_request(
                    'POST', "/api/appraiser-request", json_data=body)
            except httpx.HTTPStatusError as e:
                detail = (e.response.text or "").strip()[:120]
                self._branch_attempts.append(f"branch {branch_id} -> {e.response.status_code} {detail}")
                print(f"  branch {branch_id} rejected it: {e.response.status_code} - {detail}")
                continue
            self.internal_branch_id = str(branch_id)
            print(f"Created the appraiser request in branch {branch_id}; using that branch for the "
                  "rest of the run.")
            return response
        return None

    async def create_appraiser_request(self):
        api_path = "/api/appraiser-request"
        await self._fetch_appraisers()

        request_body = {
            "id": None,
            "customerId": int(self.customer_id) if self.customer_id else None, # Convert to int
            "customerName": self.full_name,
            "customerUniqueId": self.customer_unique_id,
            "mobileNumber": self.mobile_number,
            "moduleId": int(self.module_id) if self.module_id else None, # Convert to int
            "internalBranchId": await self.appraiser_request_branch_id(),  # the customer's branch
            "appraiserId": int(self.appraiser_id) if self.appraiser_id else None, # Convert to int
            "loanType": "Fresh Loan",  # From collection example
            "trackProccesingTime": True,
        }
        # Create the appraiser request. If one already exists for this customer, REUSE it when
        # it is still mid-flight (nothing to gain from destroying work the server already has);
        # only a finished request is cancelled and recreated. `--fresh-request` forces the
        # cancel-and-recreate path.
        response = None
        for attempt in range(2):
            try:
                response = await self._make_authenticated_request('POST', api_path, json_data=request_body)
                if attempt > 0:
                    print("Created a fresh appraiser request.")
                break
            except httpx.HTTPStatusError as e:
                text = e.response.text or ""
                if e.response.status_code == 400 and "already Exists" in text:
                    print(f"Appraiser request for customer {self.customer_unique_id} already exists.")
                    found = await self._fetch_existing_appraiser_request(required=False)
                    if not found:
                        # The server rejects the create but the request is invisible even to
                        # admin, so there is nothing to reuse and a plain retry would fail
                        # identically. Still worth trying another branch before giving up.
                        branch_response = await self._create_request_in_another_branch(request_body)
                        if branch_response is not None:
                            response = branch_response
                            break
                        raise RuntimeError(
                            f"Server rejected the appraiser request for {self.customer_unique_id} "
                            f"(customerId={self.customer_id}) with 'already Exists', but no matching "
                            f"request is visible on env '{self.env_name}' via view-all (searched by "
                            "unique id, mobile and unfiltered, under both the appraiser and the admin "
                            "scope), and no alternate branch accepted a new one"
                            + self._branch_attempt_summary() + "."
                        ) from e

                    mine = self._existing_request_is_mine()
                    if attempt == 0 and self.reuse_existing_request and self._existing_request_is_reusable():
                        # Resume: keep the request id and any master loan it already carries.
                        # store_loan_basic_details re-resolves the ids on "Your loan already
                        # initiated", so an in-progress loan continues from where it stopped.
                        print(f"Reusing appraiser request {self.appraiser_request_id} "
                              "(still in progress, assigned to this appraiser) instead of recreating it.")
                        return

                    if attempt == 0 and mine and self.reuse_existing_request:
                        # OUR OWN request whose loan is FINISHED (an in-progress one was reused
                        # above). Cancelling a finished loan is futile -- the server refuses it
                        # ("You are not allowed to cencel this loan") -- so keep the request and
                        # start a FRESH loan on it. This is what makes --customer with --count
                        # work: loan 2..N for the same customer land on the same request.
                        print(f"Appraiser request {self.appraiser_request_id} is ours but its loan "
                              "is finished; keeping the request and starting a fresh loan on it.")
                        self.loan_id = ''
                        self.master_loan_id = ''
                        return

                    if attempt == 0 and mine:
                        # --fresh-request: the caller explicitly wants the old loan gone.
                        reason = "--fresh-request was given"
                        print(f"Cancelling our existing loan and creating a fresh request ({reason}).")
                        if self.loan_id and self.master_loan_id:
                            try:
                                await self._cancel_existing_loan()  # clears loan ids on success
                                if self.loan_id:
                                    await self._cancel_existing_loan(as_admin=True)
                            except httpx.HTTPStatusError as ce:
                                print(f"Could not cancel existing loan: {ce.response.status_code} - {ce.response.text[:120]}")
                        self.loan_id = ''
                        self.master_loan_id = ''
                        continue  # retry the POST to create a fresh request

                    if mine:
                        # Still refused after the cancel, but the request IS ours, so resuming it
                        # with a fresh loan downstream is safe.
                        print("Appraiser request persists after cancelling the loan; reusing our own "
                              "request with a fresh loan downstream.")
                        self.loan_id = ''
                        self.master_loan_id = ''
                        return

                    # The request belongs to a DIFFERENT appraiser. Do NOT cancel it: the server
                    # refuses anyway (403 "You are not allowed to cencel this loan", even under the
                    # admin token) and destroying someone else's in-progress loan is not ours to do.
                    # Try to create OUR OWN request in another branch this user has access to
                    # instead -- non-destructive, and it succeeds if the server's uniqueness key
                    # includes the branch.
                    branch_response = await self._create_request_in_another_branch(request_body)
                    if branch_response is not None:
                        response = branch_response
                        break
                    raise RuntimeError(
                        f"Appraiser request {self.appraiser_request_id} for {self.customer_unique_id} "
                        f"is assigned to appraiser {self._existing_request_appraiser_id}, not to this "
                        f"login ({self.logged_in_user_id}), so it cannot be used -- every loan-process "
                        "call on it 400s 'This customer is not assign to you'. It was left untouched "
                        "(cancelling another appraiser's loan is refused with 403 'You are not allowed "
                        "to cencel this loan'), and no alternate branch accepted a new request"
                        + self._branch_attempt_summary() + ". Reassign or close the request from the "
                        "owning appraiser's login, or run with a different --customer."
                    ) from e
                if e.response.status_code == 400 and "Not Eligible For New Loan" in text:
                    customer_response = await self.get_customer_by_id()
                    customer = customer_response.get("singleCustomer", {}) if isinstance(customer_response, dict) else {}
                    raise RuntimeError(
                        "Customer is not eligible for Fresh Loan appraiser request. "
                        f"customerId={self.customer_id}, customerUniqueId={self.customer_unique_id}, "
                        f"panType={customer.get('panType') or self.pan_type}, "
                        f"kycStatus={customer.get('kycStatus')}, "
                        f"scrapKycStatus={customer.get('scrapKycStatus')}, "
                        f"customerKyc={customer.get('customerKyc')}"
                    )
                raise  # Re-raise other HTTP errors

        assert response is not None, "Appraiser request POST did not produce a response."
        resp_json = response.json()
        self._print_json_block("Create Appraiser Request Response", resp_json)
        data = resp_json.get('data') if isinstance(resp_json, dict) else None
        parsed_id = str((data.get('id') if isinstance(data, dict) else None) or resp_json.get('id') or '')

        # The create response shape is unreliable and can echo the customer id. Resolve the
        # appraiser request id authoritatively from view-all (which also captures any masterLoan).
        if not await self._fetch_existing_appraiser_request(required=False):
            self.appraiser_request_id = parsed_id  # view-all not indexed yet; fall back to parsed id
        if not self.appraiser_request_id:
            self.appraiser_request_id = parsed_id
        assert self.appraiser_request_id, f"Could not determine appraiser request id from: {resp_json}"
        print(f'Appraiser request created successfully. Appraiser Request ID: {self.appraiser_request_id}')


    async def track_loan_history(self, action: int):
        api_path = "/api/appraiser-request/track-loan-history"
        request_body = {
            "action": action,
            "appraiserRequestId": int(self.appraiser_request_id) if self.appraiser_request_id else None, # Convert to int
        }
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        print(f'Track loan history (action {action}) successful.')


    # Purpose used for IGL loans when the environment has it (--purpose overrides).
    IGL_PURPOSE = os.getenv("GOLD_LOAN_IGL_PURPOSE", "Agro")

    # --purpose-type keys -> the purposeType the server groups /api/purpose by.
    PURPOSE_TYPES = {"cb": "Consumption Based", "igl": "Income Generating"}

    async def _fetch_loan_purposes(self):
        """Pick the loan purpose and its purposeType.

        /api/purpose returns the purposes GROUPED by type -- {"Consumption Based": [...],
        "Income Generating": [...]} -- so every group is flattened, each entry keeping its type.
        GOLD_LOAN_PURPOSE pins one by name; otherwise a random active purpose of
        GOLD_LOAN_PURPOSE_TYPE (cb / igl / any) is chosen.
        """
        api_path = "/api/purpose?search=&from=1&to=-1"
        response = await self._make_authenticated_request('GET', api_path)
        data = response.json().get('data')
        groups = data if isinstance(data, dict) else {"": data or []}
        purposes = []
        for group, items in groups.items():
            if not isinstance(items, list):
                continue
            for p in items:
                if isinstance(p, dict) and p.get('name') and p.get('isActive', True) is not False:
                    purposes.append({**p, "purposeType": p.get('purposeType') or group or "Consumption Based"})
        assert purposes, "No loan purposes found."

        def describe(items):
            return "; ".join(f"{p['name']} ({p['purposeType']})" for p in items)

        if self.purpose_choice:
            wanted = self.purpose_choice.lower()
            matches = ([p for p in purposes if p['name'].strip().lower() == wanted]
                       or [p for p in purposes if wanted in p['name'].lower()])
            if not matches:
                raise RuntimeError(f"No active loan purpose named {self.purpose_choice!r}. "
                                   f"Available: {describe(purposes)}")
            chosen = matches[0]
            how = "pinned by --purpose"
        else:
            kind = self.purpose_type_choice
            if kind == "any":
                pool = purposes
            elif kind in self.PURPOSE_TYPES:
                pool = [p for p in purposes if p['purposeType'].lower() == self.PURPOSE_TYPES[kind].lower()]
            else:
                raise RuntimeError(f"Unknown purpose type {kind!r}; use one of "
                                   f"{sorted(self.PURPOSE_TYPES) + ['any']}.")
            if not pool:
                raise RuntimeError(f"No active loan purpose of type {kind!r}. Available: {describe(purposes)}")
            # IGL loans use the preferred IGL purpose ('Agro' by default, GOLD_LOAN_IGL_PURPOSE) when the
            # environment offers it; otherwise a random Income Generating purpose.
            preferred = next((p for p in pool if kind == "igl"
                              and p['name'].strip().lower() == self.IGL_PURPOSE.strip().lower()), None)
            chosen = preferred or self.random.choice(pool)
            how = f"preferred IGL purpose" if preferred else f"random, type={kind}"

        self.purpose_record = chosen
        self.purpose_id = chosen['name']
        self.purpose_type = chosen['purposeType']
        docs = chosen.get('incomeGeneratingDocumentTypes')
        print(f"Loan purpose: {self.purpose_id!r} [{self.purpose_type}] ({how})"
              + (f" -- income-generating document types: {docs}" if docs else ""))
        return self.purpose_id


    async def _find_and_set_existing_loan_ids(self):
        """
        Attempts to find existing loan_id and master_loan_id for the current customer
        if a loan has already been initiated.
        """
        if not self.customer_id:
            print("Cannot find existing loan IDs: customer_id is not set.")
            return

        # Attempt 1: Try to find loan details from the appraiser requests list. Go through the
        # same search-then-unfiltered-scan lookup as _fetch_existing_appraiser_request, because
        # the server's search filter does not reliably index customerUniqueId.
        try:
            for search in (str(self.customer_unique_id or ''), str(self.mobile_number or ''), ''):
                items = await self._list_appraiser_requests(search=search)
                item = self._match_appraiser_request(items)
                if item and self._extract_master_loan_ids(item.get("masterLoan")):
                    print(f"Successfully retrieved existing loan IDs from appraiser request list: Loan ID={self.loan_id}, Master Loan ID={self.master_loan_id}")
                    return
                if item:
                    break  # request found but it has no master loan yet
        except Exception as e:
            print(f"An unexpected error occurred while finding existing loan IDs via appraiser request list: {e}")

        # Attempt 2: Fallback to the original method using customerId if appraiser_request_id didn't yield results or was not set
        api_path_customer_loan = f"/api/loan-process/basic-details?customerId={self.customer_id}"
        try:
            response_customer_loan = await self._make_authenticated_request('GET', api_path_customer_loan)
            response_data_customer_loan = response_customer_loan.json()

            loans = []
            if isinstance(response_data_customer_loan, dict) and 'data' in response_data_customer_loan:
                if isinstance(response_data_customer_loan['data'], list):
                    loans = response_data_customer_loan['data']
                elif isinstance(response_data_customer_loan['data'], dict):
                    loans = [response_data_customer_loan['data']] # Wrap single object in a list
            elif isinstance(response_data_customer_loan, list):
                loans = response_data_customer_loan

            if loans:
                first_loan = loans[0]
                self.loan_id = str(first_loan.get('loanId') or first_loan.get('id'))
                self.master_loan_id = str(first_loan.get('masterLoanId'))
                print(f"Successfully retrieved existing loan IDs from customer basic details: Loan ID={self.loan_id}, Master Loan ID={self.master_loan_id}")
            else:
                print(f"No existing loan details found for customer {self.customer_id} via basic-details endpoint. Response: {response_data_customer_loan}")

        except httpx.HTTPStatusError as e:
            print(f"HTTP error while trying to find existing loan IDs for customer {self.customer_id} via basic-details: {e.response.status_code} - {e.response.text}")
            # Print response body for debugging 404s
            if e.response.status_code == 404:
                print(f"Response Body (404): {e.response.text}")
        except Exception as e:
            print(f"An unexpected error occurred while finding existing loan IDs via basic-details: {e}")

        if not self.loan_id or not self.master_loan_id:
            print(f"WARNING: Could not find loan IDs for customer {self.customer_id} after all attempts.")


    async def _cancel_existing_loan(self, as_admin: bool = False):
        """Cancel an existing loan, optionally under the ADMIN token.

        A loan raised by a different appraiser cannot be cancelled with our own token -- every
        loan-process call on it 400s "This customer is not assign to you" -- and admin sees
        every branch, so the caller passes as_admin=True for a request it does not own.
        """
        if not as_admin:
            return await self._cancel_existing_loan_call()
        saved_token = self.auth_token
        try:
            self.auth_token = await self._role_token("admin")
            return await self._cancel_existing_loan_call()
        finally:
            self.auth_token = saved_token

    async def _cancel_existing_loan_call(self):
        """
        Cancels an existing loan using the fetched loan_id and master_loan_id.
        """
        if not self.loan_id or not self.master_loan_id:
            print("Cannot cancel loan: loan_id or master_loan_id is not set.")
            return

        # Step 1: Fetch loan cancellation reasons
        api_path_reasons = "/api/loan-cancel-reason"
        try:
            response_reasons = await self._make_authenticated_request('GET', api_path_reasons)
            reasons_data = response_reasons.json().get('data')
            if not reasons_data:
                print("No loan cancellation reasons found. Cannot cancel loan.")
                return
            
            # Select a reason. For simplicity, pick the first one or a specific known reason.
            cancel_reason = reasons_data[0].get('reason') if reasons_data else "Other"
            print(f"Using loan cancellation reason: {cancel_reason}")

        except httpx.HTTPStatusError as e:
            print(f"Error fetching loan cancellation reasons: {e.response.status_code} - {e.response.text}")
            return
        except Exception as e:
            print(f"An unexpected error occurred while fetching cancellation reasons: {e}")
            return

        # Step 2: Cancel the loan
        api_path_cancel = "/api/loan-process/cancel"
        request_body_cancel = {
            "loanId": int(self.loan_id),
            "masterLoanId": int(self.master_loan_id),
            "loanCancelReason": cancel_reason,
        }
        try:
            response_cancel = await self._make_authenticated_request(
                'POST',
                api_path_cancel,
                json_data=request_body_cancel
            )
            print(f"Loan {self.loan_id} (Master Loan: {self.master_loan_id}) cancelled successfully.")
            self.loan_id = '' # Clear loan IDs after successful cancellation
            self.master_loan_id = ''
        except httpx.HTTPStatusError as e:
            print(f"Error cancelling loan {self.loan_id}: {e.response.status_code} - {e.response.text}")
            print(f"Response Body (Cancellation Error): {e.response.text}") # Added for debugging
        except Exception as e:
            print(f"An unexpected error occurred during loan cancellation: {e}")


    async def store_loan_basic_details(self):
        api_path = "/api/loan-process/basic-details"
        purpose = await self._fetch_loan_purposes()
        # Keep the real/KYC dob+age if already known (existing customer or the KYC step); only
        # generate when unset. Regenerating unconditionally overwrote the customer's actual values.
        # The web app fills basic-details from customer-loan-details.customerData; refresh it if the
        # earlier fetch did not run or failed.
        if not self.customer_loan_data:
            await self.fetch_customer_loan_details()
        cld = self.customer_loan_data or {}
        # The portal: basicForm.patchValue(customerData) then getRawValue(), so every matching key
        # comes from customer-loan-details; branchName is the appraiser request's branch.
        requests = cld.get('appraiserRequest') if isinstance(cld.get('appraiserRequest'), list) else []
        branch = (requests[0].get('internalBranch') or {}) if requests and isinstance(requests[0], dict) else {}
        appraiser = cld.get('appraiserDetail') if isinstance(cld.get('appraiserDetail'), dict) else {}
        kyc_personal = cld.get('customerKycPersonal') if isinstance(cld.get('customerKycPersonal'), dict) else {}

        def kyc_or(key, fallback):
            value = kyc_personal.get(key)
            return value if value not in (None, "") else fallback

        def as_int(value):
            return int(value) if str(value).isdigit() else (value or None)

        signature = kyc_personal.get('signatureProof')
        signature_img = kyc_personal.get('signatureProofImg') or ''
        if not self.age and cld.get('age'):
            self.age = cld.get('age')
        if not self.age or not self.dob:
            dob_and_age = self._generate_random_dob_and_age()
            self.age = self.age or int(dob_and_age['age'])
            self.dob = self.dob or dob_and_age['dob']

        request_body = {
            "customerUniqueId": self.customer_unique_id,
            "mobileNumber": cld.get('mobileNumber') or self.mobile_number,
            "panCardNumber": cld.get('panCardNumber') or self.random_pan,
            "startDate": datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z'),  # ISO 8601 with Z
            "customerId": int(self.customer_id) if self.customer_id else None, # Convert to int
            "kycStatus": cld.get('kycStatus') or "approved",  # customerData.kycStatus
            "purpose": purpose,
            "panType": cld.get('panType') or self.pan_type,
            "loanId": None,
            "scrapId": None,
            "masterLoanId": None,
            "panImg": cld.get('panImg') or self.pan_image,
            "partReleaseId": None,
            "requestId": int(self.appraiser_request_id) if self.appraiser_request_id else None, # Convert to int
            "customerName": cld.get('customerName') or self.full_name,
            "form60Img": cld.get('form60Img') or self.form60_image,
            "age": str(cld.get('age') or self.age),
            "branchName": branch.get('name') or '',  # customerData.appraiserRequest[0].internalBranch.name
            "branchDistance": None,  # the web app sends null too
            "gender": cld.get('gender') or self.gender,
            "appraiserName": appraiser.get('appraiserName') or self.logged_in_appraiser_name,
            "tillDateOutstanding": str(cld.get('tillDateOutstanding') if cld.get('tillDateOutstanding') is not None else "0.00"),
            "appraiserMobileNumber": appraiser.get('mobileNumber') or self.logged_in_appraiser_mobile_number,
            "currentActiveLoans": cld.get('currentActiveLoans') if cld.get('currentActiveLoans') is not None else 0,
            "cpvImageFullUrl": cld.get('cpvImageFullUrl'),   # customerData; null when the customer has none
            "annualIncome": kyc_or('annualIncome', self.annual_income),
            # KYC personal values (the portal disables each control that already has a value).
            "motherName": kyc_or('motherName', self.mother_name),
            "religionId": as_int(kyc_or('religionId', self.religion_id)),
            "physicalChallengeId": as_int(kyc_or('physicalChallengeId', self.physical_challenge_id)),
            "occupationId": as_int(kyc_or('occupationId', self.occupation_id)),
            "industryCategoryId": kyc_personal.get('industryCategoryId'),
            "industryId": kyc_personal.get('industryId'),
            # Signature as stored on the KYC record: null / '' / '' when the customer has none.
            "signatureProof": signature or None,
            "signatureProofImg": signature_img,
            "signatureProofFileName": signature_img.rsplit('/', 1)[-1] if signature_img else '',
            "referenceCustomerNumber": None,
            "qualificationId": as_int(kyc_or('qualificationId', self.qualification_id)),
            # politicalExposedId / specialCategoryId are no longer on the portal's basic-details form.
            "incomeGeneratingDocuments": None,
            "purposeType": self.purpose_type or "Consumption Based",  # from the chosen purpose's group
            "checkPointers": {},
        }
        try:
            response = await self._make_authenticated_request(
                'POST',
                api_path,
                json_data=request_body
            )
            resp_json = response.json()
            # Extract loanId and masterLoanId directly from the response JSON
            self.loan_id = str(resp_json['loanId'])
            self.master_loan_id = str(resp_json['masterLoanId'])
            self.loan_type = resp_json.get('loanType') or self.loan_type  # every later step echoes it
            # The portal passes this response on to the nominee step: createdBy / internalBranchId
            # are echoed from it and an existing loanNomineeDetail is prefilled.
            self.basic_details_response = resp_json if isinstance(resp_json, dict) else {}
            print(f'Loan basic details stored successfully. Loan ID: {self.loan_id}, Master Loan ID: {self.master_loan_id}')
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400 and "Your loan already initiated" in e.response.text:
                print(f"Loan for customer {self.customer_id} is already initiated. Attempting to retrieve existing loan IDs.")
                await self._find_and_set_existing_loan_ids()
                if not self.loan_id or not self.master_loan_id:
                    raise RuntimeError(f"Failed to retrieve existing loan IDs for customer {self.customer_id} after 'loan already initiated' error.")
            else:
                raise # Re-raise other HTTP errors


    async def _fetch_nominee_relations(self):
        api_path = "/api/nominee-relation/list"
        response = await self._make_authenticated_request('GET', api_path)
        relations = response.json().get('data')
        assert relations, "No nominee relations found."
        # The server matches `relationship` against relationshipName from THIS list, and those
        # values are lowercase (e.g. 'brother'). A hardcoded 'Brother' matches nothing, so the
        # nominee relationship silently ends up unset on the loan. Always pick a real value.
        names = [str(r.get('relationshipName') or r.get('name') or '').strip()
                 for r in relations if isinstance(r, dict)]
        names = [n for n in names if n]
        assert names, "Nominee relation list had no relationshipName values."
        adult = {'brother', 'sister', 'father', 'mother', 'spouse', 'son', 'daughter', 'wife', 'husband'}
        preferred = next((n for n in names if n.lower() in adult), None)
        self.nominee_relation_id = preferred or names[0]
        print(f'Selected nominee relationship: {self.nominee_relation_id!r}')
        return self.nominee_relation_id


    async def store_nominee_details(self):
        api_path = "/api/loan-process/nominee-details"
        nominee_names = self._generate_random_name()
        nominee_name = f"{nominee_names['firstName']} {nominee_names['lastName']}"
        nominee_age_val = self.random.randint(20, 60)
        nominee_relation = await self._fetch_nominee_relations()
        basic = self.basic_details_response or {}
        # A nominee the server already holds for this loan (basic-details loanNomineeDetail) is
        # prefilled by the portal; reuse it instead of inventing a new one.
        saved = basic.get('loanNomineeDetail') if isinstance(basic.get('loanNomineeDetail'), dict) else {}
        if saved.get('nomineeName'):
            nominee_name = saved['nomineeName']
            nominee_age_val = int(saved.get('nomineeAge') or nominee_age_val)
            print(f"Nominee: reusing the server's nominee {nominee_name!r}.")

        request_body = {
            "nomineeName": nominee_name,
            "nomineeAge": nominee_age_val,
            "relationship": nominee_relation,
            "mobileNumber": self._generate_random_mobile_number(),
            "referenceCode": None,
            "nomineeType": 'major' if nominee_age_val >= 18 else 'minor',
            # An adult nominee leaves the guardian controls untouched in the portal -> null.
            "guardianName": None,
            "guardianAge": None,
            "guardianRelationship": None,
            "checkPointers": {},
            "loanId": int(self.loan_id) if self.loan_id else None, # Convert to int if not empty
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None, # Convert to int if not empty
            "loanType": self.loan_type or "Fresh Loan",
            # Echoed from the basic-details response, as the portal does (masterAndLoanIds).
            "createdBy": basic.get('createdBy') or self.logged_in_user_id,
            "internalBranchId": basic.get('internalBranchId') or (int(self.internal_branch_id) if self.internal_branch_id else None),
        }
        for key in ("relationship", "mobileNumber", "nomineeType", "guardianName", "guardianAge", "guardianRelationship"):
            if saved.get(key) not in (None, ""):
                request_body[key] = saved[key]

        if nominee_age_val < 18:
            guardian_names = self._generate_random_name()
            request_body["guardianName"] = f"{guardian_names['firstName']} {guardian_names['lastName']}"
            request_body["guardianAge"] = self.random.randint(30, 70)
            request_body["guardianRelationship"] = 'father'  # valid lowercase relationshipName

        try:
            response = await self._make_authenticated_request(
                'POST',
                api_path,
                json_data=request_body
            )
            print('Nominee details stored successfully.')
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400 and "Nominee details already added" in e.response.text:
                print(f"Nominee details for loan {self.loan_id} are already added. Continuing to next step.")
            else:
                raise # Re-raise other HTTP errors


    def _is_integer(self, s):
        """True if the string carries a karat number (e.g. '22K', '18KT', '24 K HM', '916')."""
        if not s:
            return False
        return bool(re.search(r'\d+', str(s)))

    def _extract_karat_value(self, s):
        """Extract the integer karat value from a string like '22K', '18KT', '24 K HM', '916'.
        Uses the leading integer so hallmark/suffix tokens (e.g. 'HM') don't break parsing."""
        if not s:
            return 0
        m = re.search(r'\d+', str(s))
        return int(m.group()) if m else 0

    @staticmethod
    def _is_positive_number(value) -> bool:
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return False

    async def _fetch_karat_details(self):
        api_path = "/api/karat-details"  # From collection example, no query params
        response = await self._make_authenticated_request('GET', api_path)
        karats = response.json().get('data')
        assert karats, "No karat details found."
        
        # Filter karats to be greater than 20
        filtered_karats = []
        for k in karats:
            karat_value_str = k.get('karat')
            # The karat's LTV `range` drives netWtAfterPurity (netWeight * ltv% / 100), so a karat
            # without a positive LTV option (e.g. '0 K HM' -> [0]) would value the gold at nothing.
            usable_range = any(self._is_positive_number(x) for x in (k.get('range') or []))
            if karat_value_str and self._is_integer(karat_value_str) and usable_range:
                karat_value_int = self._extract_karat_value(karat_value_str)
                if karat_value_int > 18:
                    filtered_karats.append(k)

        if filtered_karats:
            # Prefer 22K: it's the most broadly accepted karat across secured schemes' karat ranges
            # (e.g. 18-24, 22-24), maximizing the odds a candidate scheme covers every ornament.
            preferred = [k for k in filtered_karats if self._extract_karat_value(k.get('karat')) == 22]
            karat_detail = self.random.choice(preferred or filtered_karats)
        else:
            print("No karats > 18 found, selecting a random available karat.")
            # Fallback to selecting any valid karat if no filtered ones are found
            valid_karats = [k for k in karats if k.get('karat') and self._is_integer(k['karat'])]
            if valid_karats:
                karat_detail = self.random.choice(valid_karats)
            else:
                raise ValueError("No valid karat details found after filtering.")

        self.karat_id = karat_detail.get('karat')
        print(f'Fetched random karat_id: {self.karat_id}')
        return karat_detail


    async def _fetch_ornament_types(self):
        api_path = "/api/ornament-type?from=1&to=-1&search="  # From collection example
        response = await self._make_authenticated_request('GET', api_path)
        ornament_types_list = response.json().get('data')
        assert ornament_types_list, "No ornament types found."
        ornament_type = self.random.choice(ornament_types_list)
        self.ornament_type_id = str(ornament_type.get('id'))
        print(f'Fetched random ornament_type_id: {self.ornament_type_id}')
        return ornament_type


    @staticmethod
    def _find_key_recursive(obj, key):
        """Return the first scalar value found for `key` anywhere in a nested structure."""
        if isinstance(obj, dict):
            if key in obj and not isinstance(obj[key], (dict, list)):
                return obj[key]
            for value in obj.values():
                found = GoldLoanApiTest._find_key_recursive(value, key)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = GoldLoanApiTest._find_key_recursive(item, key)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _extract_ornaments_from_response(obj):
        """Locate the server's stored-ornament list (with DB ids + calculated amounts) in a response."""
        marker_keys = ("netWtAfterPurity", "currentLtvAmount", "ornamentTypeId", "grossWeight")
        if isinstance(obj, list):
            if obj and isinstance(obj[0], dict) and any(k in obj[0] for k in marker_keys):
                return obj
            for item in obj:
                found = GoldLoanApiTest._extract_ornaments_from_response(item)
                if found:
                    return found
        elif isinstance(obj, dict):
            for value in obj.values():
                found = GoldLoanApiTest._extract_ornaments_from_response(value)
                if found:
                    return found
        return None

    def _maybe_set_rpg(self, resp_json, label: str) -> bool:
        """Capture the secured rate-per-gram from a response if present (scheme/loan-calc steps)."""
        for key in ("securedRpg", "rpg", "ratePerGram", "securedRatePerGram",
                    "goldLoanRate", "perGramRate", "loanRatePerGram"):
            val = self._find_key_recursive(resp_json, key)
            if val is None:
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            if fval > 0:
                self.secured_rpg = fval
                print(f"  -> captured secured rate/gram {fval} from {label}.{key}")
                return True
        return False

    async def _fetch_gold_rate(self) -> float:
        """Fetch the real current gold rate/gram (used for ornament valuation)."""
        try:
            response = await self._make_authenticated_request('GET', "/api/gold-rate")
            data = response.json()
            rate = self._find_key_recursive(data, 'goldRate')
            if rate:
                self.gold_rate = float(rate)
                print(f"Fetched current gold rate/gram: {self.gold_rate}")
        except httpx.HTTPStatusError as e:
            print(f"Fetch gold rate failed (non-fatal): {e.response.status_code}")
        return self.gold_rate

    async def store_ornament_details(self):
        api_path = "/api/loan-process/ornaments-details"

        # Use the REAL gold rate (not a random one) so the server's eligibility calc is sane.
        await self._fetch_gold_rate()
        if not self.gold_rate:
            raise RuntimeError("GET /api/gold-rate returned no goldRate; ornaments cannot be valued.")
        # currentLtvAmount is a PER-GRAM rate in the portal: goldRate * ltvGoldValue% (global setting),
        # e.g. 12340 * 75% = 9255 in the captured flow -- not a total.
        settings = await self.fetch_global_setting()
        ltv_gold_value = float(settings.get('ltvGoldValue') or 0)
        if ltv_gold_value <= 0:
            raise RuntimeError("Global setting ltvGoldValue is missing; the portal needs it to value ornaments.")
        num_ornaments = 4  # fixed count; total weight is sized to the target loan below
        loan_ornaments = []

        # Size the gold so eligibility (sum of netWtAfterPurity * schemeRpg) comfortably exceeds the
        # target loan -- final-loan-details 400s (and finalLoanAmount gets capped) when eligible < loan.
        # Use a conservative rate floor (below the lowest scheme rpg seen, ~4533) plus headroom, then
        # spread the required weight across the ornaments.
        target_loan = float(os.getenv("GOLD_LOAN_AMOUNT", "400000"))
        conservative_rpg = 4000.0
        avg_purity = 0.9  # netWtAfterPurity ~= grossWeight * purity
        required_total_gross = (target_loan * 1.3 / conservative_rpg) / avg_purity
        base_gross = max(20.0, required_total_gross / num_ornaments)

        # Use ONE karat for every ornament in the run. Schemes have a [minKarat, maxKarat] range and
        # the server only counts karat-eligible ornaments in its eligibility calc; mixing karats
        # (e.g. 19K + 24K) means no single scheme covers them all, so final-loan-details' totalEligibleAmt
        # (summed over all ornaments) won't match the server's (summed over the karat-eligible subset).
        # A single karat lets check_loan_type pick a scheme whose range covers every ornament.
        karat_detail = await self._fetch_karat_details()

        for i in range(num_ornaments):
            ornament_type = await self._fetch_ornament_types()

            # Vary each ornament +/-15% around the sized base so the total clears the target loan.
            gross_weight = self._round_double(base_gross * self.random.uniform(0.85, 1.15), 2)
            deduction_weight = self._round_double(self.random.uniform(0, 5), 2)  # 0-5 grams
            net_weight = self._round_double(gross_weight - deduction_weight, 2)
            current_gold_rate = self._round_double(self.gold_rate, 2)
            current_ltv_amount = self._round_double(current_gold_rate * ltv_gold_value / 100, 2)

            # The karat's LTV options are its `range` (karat-details); the portal's selectKarat uses it.
            ltv_range = karat_detail.get('range') or karat_detail.get('ltvRange') or [90]
            ltv_choices = [x for x in ltv_range if self._is_positive_number(x)] or [90]
            ltv_percent = str(self.random.choice(ltv_choices))

            # netWtAfterPurity MUST equal what the server RECOMPUTES for eligibility, or final-loan-details
            # 400s with "total eligible ... doesn't match". Confirmed against the server's own stored
            # ornaments (single-loan on a mismatch): the server uses
            #     nwap = netWeight * min(purityReading, ltvPercent) / 100
            # i.e. it CAPS the assessed purity at the ornament's LTV%. Two runs pinned this exactly:
            # run A purity 81.46 < ltv 90 -> server kept 81.46; run B purity 91.67(karat) > ltv 90 ->
            # server capped to 90. So we compute purity from the karat (realistic) but cap it at
            # ltvPercent, and store that same value as purityReading so no further server capping
            # applies -- our sent nwap then equals the server's recomputed nwap to the paisa.
            # The portal's getPurityWeight: netWtAfterPurity = netWeight * ltvPercent / 100 (2 dp,
            # half-up like the server), with purityReading left null on the loan path. (The old capped
            # formula only existed because the harness used to send a purityReading.)
            net_wt_after_purity = self._round_half_up(net_weight * float(ltv_percent) / 100, 2)

            ornament_image_response = await self._upload_file(
                "loan", file_type="image", file_path_override=self.ORNAMENT_IMAGE_PATH)
            ornament_image = ornament_image_response['uploadFile']['path']

            ornament = {
                "ornamentType": ornament_type,
                "ornamentTypeId": int(self.ornament_type_id) if self.ornament_type_id else None, # Convert to int
                "quantity": "1",
                "grossWeight": str(gross_weight),
                "netWeight": str(net_weight),
                "deductionWeight": str(deduction_weight),
                "ornamentImage": ornament_image,
                "weightMachineZeroWeight": None,
                "stoneTouch": None,
                "acidTest": None,
                "karat": self.karat_id,
                "ltvRange": ltv_range,
                "purityTest": [],
                "ltvPercent": ltv_percent,
                "loanAmount": None,
                "id": None,
                "currentLtvAmount": current_ltv_amount,   # per-gram: goldRate * ltvGoldValue%
                "ornamentImageData": ornament_image_response['uploadFile'].get('URL') or f'{self.BASE_URL}/{ornament_image}',
                "weightMachineZeroWeightData": None,
                "withOrnamentWeightData": None,
                "stoneTouchData": None,
                "acidTestData": None,
                "purityTestImage": [],
                "ornamentFullAmount": None,
                "currentGoldRate": current_gold_rate,
                "ornamentImageWithWeight": None,
                "ornamentImageWithWeightData": None,
                "ornamentImageWithXrfMachineReading": None,
                "ornamentImageWithXrfMachineReadingData": None,
                "approxPurityReading": None,   # scrap-only; null on the loan path
                "scrapAmount": None,
                "purityReading": None,         # scrap-only; null on the loan path
                "customerConfirmation": None,
                "finalScrapAmountAfterMelting": None,
                "processingCharges": None,
                "packetId": None,
                "netWtAfterPurity": str(net_wt_after_purity),
                "remark": None,
                "isReleased": None,
                "withOrnamentWeight": None,
                "ltvAmount": None,
            }
            loan_ornaments.append(ornament)

        total_eligible_amt = 0  # the portal sends this.totalAmount, which stays 0 on the loan path

        request_body = {
            "loanOrnaments": loan_ornaments,
            "totalEligibleAmt": total_eligible_amt,
            "loanId": int(self.loan_id) if self.loan_id else None,
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            "fullAmount": 0,
            "checkPointers": {},
            "latitude": self.latitude or None,
            "longitude": self.longitude or None,
        }
        try:
            response = await self._make_authenticated_request(
                'POST',
                api_path,
                json_data=request_body
            )
            resp_json = response.json()
            self._diagnostics['ornaments_details_response'] = resp_json
            # Capture the SERVER's stored ornaments (real DB ids + server-calculated eligibility)
            # and total. final-loan-details validates against these, so echoing our locally-built
            # ornaments (id=null, random amounts) fails with "total eligible ... doesn't match".
            server_ornaments = self._extract_ornaments_from_response(resp_json)
            server_total = self._find_key_recursive(resp_json, 'totalEligibleAmt')
            # The portal's eligibility for final-loan-details uses this TOTAL net weight.
            if isinstance(resp_json, dict) and resp_json.get('netWtAfterPurity') is not None:
                self.ornaments_total_nwap = float(resp_json['netWtAfterPurity'])
            self.loan_ornaments_details = server_ornaments if server_ornaments else loan_ornaments
            self.total_eligible_amount = float(server_total) if server_total is not None else total_eligible_amt
            captured_ids = [o.get('id') for o in self.loan_ornaments_details if isinstance(o, dict)]
            print(
                f"Ornament details stored successfully. "
                f"Captured {len(self.loan_ornaments_details)} ornament(s) "
                f"(ids={captured_ids}), server totalEligibleAmt={self.total_eligible_amount}."
            )
            if not any(captured_ids):
                print(
                    "WARNING: ornaments-details response did not include server ornament ids; "
                    "final-loan-details may fail. Response was:"
                )
                self._print_json_block("ornaments-details response", resp_json)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400 and "Ornament details already added" in e.response.text:
                print(f"Ornament details for loan {self.loan_id} are already added. Continuing to next step.")
                # If already added, try to fetch existing ornaments to populate self.loan_ornaments_details
                # This would require a GET endpoint for ornaments, which is not currently implemented.
                # For now, we'll assume the locally generated ornaments are sufficient if the API doesn't return them.
                self.total_eligible_amount = total_eligible_amt
                self.loan_ornaments_details = loan_ornaments
            else:
                raise # Re-raise other HTTP errors


    @staticmethod
    def _extract_scheme_list(obj):
        """Locate a flat list of scheme objects inside a response, whatever the wrapper. NOTE: the
        marker deliberately EXCLUDES 'rpg' -- the /api/scheme catalog nests schemes under PARTNER
        objects that also carry 'rpg', so keying on rpg would wrongly return the partners list. A
        real scheme is identified by schemeName / schemeAmountStart / minKarat / ltv."""
        marker = ("schemeName", "schemeAmountStart", "minKarat", "ltv")
        if isinstance(obj, list):
            if obj and isinstance(obj[0], dict) and any(k in obj[0] for k in marker):
                return obj
            for item in obj:
                found = GoldLoanApiTest._extract_scheme_list(item)
                if found:
                    return found
        elif isinstance(obj, dict):
            for value in obj.values():
                found = GoldLoanApiTest._extract_scheme_list(value)
                if found:
                    return found
        return []

    async def fetch_scheme_catalog(self):
        """GET the authoritative scheme catalog (/api/scheme?partnerType=partner) and index every
        scheme by its id. Scheme selection uses this to pre-check eligibility (active, secured,
        karat range, amount range) so ineligible schemes are skipped BEFORE check-loan-type /
        final-loan-details 400. Best-effort: on any failure we proceed with the partner-scheme-amount
        candidates unfiltered.

        The response is {data: [ {id, name, partnerId, rpg, schemes:[...]}, ... ]} -- a list of
        PARTNERS each with a nested schemes[]; we flatten every partner's schemes. IMPORTANT: the
        catalog's scheme rpg (~45000) is NOT the per-gram rate used for eligibility -- that comes
        from check-loan-type's securedScheme.rpg (~4533). We only read active/karat/amount/schemeType
        here, never rpg."""
        if getattr(self, "_scheme_catalog_by_id", None):
            return self._scheme_catalog_by_id
        self._scheme_catalog_by_id = {}
        try:
            response = await self._make_authenticated_request(
                'GET', "/api/scheme?partnerType=partner&search=")
            payload = response.json()
        except Exception as e:  # non-fatal: catalog is an optimisation, not a hard dependency
            print(f"  -> scheme catalog fetch failed ({e}); selecting without catalog pre-check.")
            return self._scheme_catalog_by_id

        data = payload.get('data') if isinstance(payload, dict) else payload
        if isinstance(data, list):
            for partner in data:
                if not isinstance(partner, dict):
                    continue
                sub = partner.get('schemes')
                scheme_list = sub if isinstance(sub, list) and sub else (
                    [partner] if partner.get('schemeName') else [])
                for s in scheme_list:
                    if isinstance(s, dict) and s.get('id') is not None:
                        self._scheme_catalog_by_id[str(s.get('id'))] = s
        if not self._scheme_catalog_by_id:  # unexpected shape -> fall back to a flat-list search
            for s in self._extract_scheme_list(payload):
                if isinstance(s, dict) and s.get('id') is not None:
                    self._scheme_catalog_by_id[str(s.get('id'))] = s
        self._diagnostics['scheme_catalog_ids'] = sorted(
            self._scheme_catalog_by_id.keys(), key=lambda x: int(x) if str(x).isdigit() else 0)
        print(f"Fetched scheme catalog: {len(self._scheme_catalog_by_id)} scheme(s) across partners.")
        return self._scheme_catalog_by_id

    def _scheme_eligibility(self, scheme, ornament_karats, target_amount):
        """Return (eligible: bool, reason: str) for a candidate, using the catalog config merged
        over the candidate's own fields. Checks: scheme active, requested amount within the scheme's
        amount range, and every ornament karat within the scheme's [minKarat, maxKarat]."""
        sid = str(scheme.get('id'))
        cat = getattr(self, "_scheme_catalog_by_id", {}).get(sid, {})

        def pick(key, default=None):
            val = scheme.get(key)
            if val is None:
                val = cat.get(key)
            return default if val is None else val

        if pick('isActive', True) in (False, 0, '0', 'false', 'False'):
            return False, "scheme inactive in catalog"

        stype = pick('schemeType')
        if stype and str(stype).lower() != 'secured':
            return False, f"schemeType '{stype}' is not secured"

        try:
            lo = float(pick('schemeAmountStart', 0) or 0)
            hi = float(pick('schemeAmountEnd', 0) or 0)
            if target_amount < lo or (hi and target_amount > hi):
                return False, f"loan {target_amount:.0f} outside amount range {lo:.0f}-{hi:.0f}"
        except (TypeError, ValueError):
            pass

        min_k, max_k = pick('minKarat'), pick('maxKarat')
        if ornament_karats and min_k is not None and max_k is not None:
            mn, mx = self._extract_karat_value(min_k), self._extract_karat_value(max_k)
            if mn and mx:
                outside = [k for k in ornament_karats if k < mn or k > mx]
                if outside:
                    return False, f"karat(s) {outside} outside scheme range {mn}-{mx}"
        return True, "ok"

    async def _fetch_partner_scheme_amount(self):
        api_path = f"/api/scheme/partner-scheme-amount/1?masterLoanId={self.master_loan_id}"
        response = await self._make_authenticated_request('GET', api_path)
        partners = response.json().get('data')
        assert partners, "No schemes found."

        # partner-scheme-amount returns a list of PARTNERS (e.g. Roshan Partner id 152), each with a
        # nested schemes[] array. The actual scheme ids/rpg/ltv/amount-range live in schemes[], NOT
        # at the partner level -- the partner's own id/rpg must NOT be used as the scheme (a bug that
        # sent partnerId 152 as securedSchemeId and picked up an unrelated scheme's rpg). We flatten
        # to (partner -> scheme) candidates and pick a scheme whose amount range fits the loan; the
        # secured rate/gram (rpg) is then read from the SELECTED scheme, which is what
        # final-loan-details' eligibility (sum of netWtAfterPurity * schemeRpg) must match.
        partner_names = [
            p.strip().upper()
            for p in os.getenv("GOLD_LOAN_PARTNER_NAME", "ROSHAN PARTNER,ARVOG").split(",")
            if p.strip()
        ]

        def _partner_text(p):
            return " ".join(str(p.get(k) or "") for k in (
                "partnerName", "partner", "partnerId", "name"
            )).upper()

        def _is_partner(p):
            text = _partner_text(p)
            return any(name in text for name in partner_names)

        matched_partners = [p for p in partners if _is_partner(p)]
        if not matched_partners:
            print(f"WARNING: no partners matched {partner_names}; using all partners. "
                  f"Available: {sorted({_partner_text(p) for p in partners})[:10]}")
            matched_partners = partners

        def _partner_num(p):
            raw = str(p.get('partnerId') or p.get('id') or '')
            digits = ''.join(c for c in raw.split('-')[-1] if c.isdigit()) or ''.join(c for c in raw if c.isdigit())
            return digits or str(p.get('id') or '')

        # Flatten each partner's schemes[] into candidates, tagging the owning partner. If an entry
        # has no schemes[] but already looks like a scheme (has rpg/amount range), treat it as one.
        candidates = []
        for p in matched_partners:
            sub_schemes = p.get('schemes') or []
            if not sub_schemes and (p.get('rpg') is not None and p.get('schemeAmountStart') is not None):
                sub_schemes = [p]
            for s in sub_schemes:
                cand = dict(s)
                cand['_partnerId'] = _partner_num(p)
                cand['_partnerName'] = p.get('name') or p.get('partnerId')
                candidates.append(cand)

        target_amount = float(os.getenv("GOLD_LOAN_AMOUNT", "400000"))

        def _fits(s):
            try:
                lo = float(s.get('schemeAmountStart') or 0)
                hi = float(s.get('schemeAmountEnd') or 0)
            except (TypeError, ValueError):
                return False
            return lo <= target_amount and (hi == 0 or target_amount <= hi)

        fitting = [c for c in candidates if _fits(c)]
        # Prefer schemes whose amount range covers the loan; fall back to all candidates and let
        # check_loan_type validate/retry on a "range amount is greater than" rejection.
        pool = fitting or candidates
        assert pool, f"No schemes available under partners {partner_names}."
        # Order by partner priority (the order they appear in GOLD_LOAN_PARTNER_NAME), then shuffle
        # WITHIN each partner for variety. Roshan is listed first and is the ONLY partner with a
        # confirmed-accepted final-loan-details capture; ARVOG schemes have consistently 400'd on
        # final-loan-details despite structurally-correct requests (a partner-level server issue we
        # can't diagnose without an accepted ARVOG capture). So we try the proven partner first and
        # fall back to the others only if no Roshan scheme fits.
        def _sort_key(cand):
            name = (cand.get('_partnerName') or '').upper()
            prio = next((i for i, pn in enumerate(partner_names) if pn in name), len(partner_names))
            try:
                sid = int(cand.get('id') or 0)
            except (TypeError, ValueError):
                sid = 0
            return (prio, sid)
        # Deterministic: preferred partner first, then LOWEST scheme id. For Roshan this picks
        # scheme 853 — the only scheme with a confirmed-accepted final-loan-details (200) capture.
        # Sibling schemes 861/882 (same rpg/ltv) return different server params and have 400'd.
        pool.sort(key=_sort_key)

        # Pre-check each candidate against the authoritative scheme catalog (active / karat range /
        # amount range) and drop the ineligible ones BEFORE we try them. The ornaments are already
        # stored at this point, so we know their karats. This is what lets us skip a scheme like 882
        # up front if the catalog marks it inactive or karat/amount-restricted, instead of eating a
        # check-loan-type or final-loan-details 400. If the catalog is unavailable, nothing is
        # filtered and we fall back to the full pool (check_loan_type still retries server-side).
        await self.fetch_scheme_catalog()
        ornament_karats = sorted({
            self._extract_karat_value(o.get('karat'))
            for o in (self.loan_ornaments_details or [])
            if isinstance(o, dict) and o.get('karat')
        } - {0})
        eligible_pool = []
        for cand in pool:
            ok, reason = self._scheme_eligibility(cand, ornament_karats, target_amount)
            if ok:
                eligible_pool.append(cand)
            else:
                print(f"  -> catalog pre-check drops scheme {cand.get('id')} "
                      f"(partner {cand.get('_partnerName')}): {reason}")
        if not eligible_pool:
            print("  -> no scheme passed the catalog pre-check; falling back to the full pool "
                  "and letting check-loan-type validate server-side.")
        self._candidate_schemes = eligible_pool or pool

        # If the user pinned a scheme (--scheme ID), honor it: move that scheme to the front (searching
        # the full candidate list, so a pinned scheme is used even if the catalog pre-check dropped it).
        if self.forced_scheme_id:
            forced = next((c for c in candidates if str(c.get('id')) == self.forced_scheme_id), None)
            if forced:
                self._candidate_schemes = [forced] + [c for c in self._candidate_schemes
                                                      if str(c.get('id')) != self.forced_scheme_id]
                print(f"  -> pinned scheme {self.forced_scheme_id} "
                      f"(partner {forced.get('_partnerName')}) per --scheme.")
            else:
                print(f"  -> WARNING: --scheme {self.forced_scheme_id} not found under the selected "
                      f"partner(s); using the auto-picked scheme instead.")

        self._diagnostics['all_scheme_partners'] = sorted({_partner_text(p) for p in partners})
        self._diagnostics['ornament_karats'] = ornament_karats
        print(f"Candidate schemes for partners {partner_names} (amount {target_amount}, "
              f"karats {ornament_karats}): "
              f"{[(str(s.get('id')), s.get('rpg')) for s in self._candidate_schemes]}")
        self._apply_scheme(self._candidate_schemes[0])

    def _apply_scheme(self, scheme: dict):
        """Set scheme/partner ids, co-lender, and capture the SELECTED scheme's rpg + ltv + range."""
        self.selected_scheme = scheme
        self.scheme_id = str(scheme.get('id'))
        self.partner_id = str(scheme.get('_partnerId') or '')
        # Co-lending ONLY when requested with --co-lender: neither the scheme's own co-lender mapping
        # nor the server's default co-lender is applied otherwise.
        self.co_lender_bank_id = self._forced_co_lender_id or None
        # Amount range comes from the scheme's schemeAmountStart/End (falls back to legacy fields).
        self.min_loan_amount = scheme.get('schemeAmountStart') or scheme.get('minLoanAmount', 0) or 0
        self.max_loan_amount = scheme.get('schemeAmountEnd') or scheme.get('maxLoanAmount', 0) or 0
        # rpg + ltv are properties of the SELECTED scheme, retrieved here after scheme selection.
        # This is the rate final-loan-details multiplies netWtAfterPurity by -- never a hardcode.
        self.secured_rpg = 0.0  # re-capture per candidate
        try:
            if scheme.get('rpg') is not None:
                self.secured_rpg = float(scheme['rpg'])
        except (TypeError, ValueError):
            pass
        if not self.secured_rpg:
            self._maybe_set_rpg(scheme, "scheme")  # last resort: hunt any rate-like key
        try:
            if scheme.get('ltv') is not None:
                self.secured_ltv = float(scheme['ltv'])
        except (TypeError, ValueError):
            pass
        self._diagnostics['selected_scheme'] = scheme
        print(f"Applied schemeId {self.scheme_id} (rpg {self.secured_rpg}, ltv {self.secured_ltv}, "
              f"range {self.min_loan_amount}-{self.max_loan_amount}), partnerId {self.partner_id} "
              f"(partner: {scheme.get('_partnerName')}).")


    async def check_loan_type(self):
        api_path = "/api/loan-process/check-loan-type"
        await self._fetch_partner_scheme_amount()  # Builds the candidate scheme pool.

        loan_amount_to_use = float(os.getenv("GOLD_LOAN_AMOUNT", "400000"))
        candidates = getattr(self, "_candidate_schemes", None) or [self.selected_scheme]
        last_error = None
        last_reason = ""

        # Karats of the stored ornaments; a chosen scheme must cover ALL of them or the server's
        # eligibility (over karat-eligible ornaments only) won't match ours (over all ornaments).
        ornament_karats = sorted({
            self._extract_karat_value(o.get('karat'))
            for o in (self.loan_ornaments_details or [])
            if isinstance(o, dict) and o.get('karat')
        } - {0})

        for idx, scheme in enumerate(candidates):
            self._apply_scheme(scheme)
            self.final_loan_amount = loan_amount_to_use
            request_body = {
                "loanAmount": loan_amount_to_use,
                "securedSchemeId": int(self.scheme_id) if self.scheme_id else None,
                "fullAmount": 0,
                "partnerId": int(self.partner_id) if self.partner_id and self.partner_id.isdigit() else (self.partner_id or None),
                "isLoanTransfer": False,
                "isNewLoanFromPartRelease": False,
                "unsecuredSchemeId": None,
                "isUnsecuredSchemeApplied": False,
                "loanTransferExtraAmount": None,
                "isNewLoanFromRenew": False,
                "loanId": int(self.loan_id) if self.loan_id else None,
                "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
                "loanType": self.loan_type or "Fresh Loan",
                "createdBy": str(self.logged_in_user_id) if self.logged_in_user_id else "3179",
                "internalBranchId": int(self.internal_branch_id) if self.internal_branch_id else None,
                # The portal sends only a co-lender the USER picked (null otherwise); the server's
                # default co-lender is adopted from the response below.
                "coLenderBankId": int(self._forced_co_lender_id) if str(self._forced_co_lender_id or '').isdigit() else None,
            }
            try:
                response = await self._make_authenticated_request('POST', api_path, json_data=request_body)
                resp_json = response.json()
                self._diagnostics['check_loan_type_request'] = request_body
                self._diagnostics['check_loan_type_response'] = resp_json
                self._print_json_block("check-loan-type response", resp_json)
                self._maybe_set_rpg(resp_json, "check-loan-type")
                # Capture the secured scheme's rate/gram (rpg) and LTV%. The scheme rpg is the loan
                # rate/gram the lender advances (e.g. 4440), NOT the raw gold market rate used to
                # VALUE the ornaments (currentLtvAmount = nwap * goldRate). final-loan-details'
                # eligibility is nwap * schemeRpg, so we must pin the scheme's own rpg here.
                data = resp_json.get('data') if isinstance(resp_json, dict) else None
                secured_scheme = data.get('securedScheme') if isinstance(data, dict) else None
                if isinstance(secured_scheme, dict):
                    self.secured_scheme = secured_scheme
                    if secured_scheme.get('ltv'):
                        try:
                            self.secured_ltv = float(secured_scheme['ltv'])
                            print(f"  -> captured scheme LTV% = {self.secured_ltv}")
                        except (TypeError, ValueError):
                            pass
                    if secured_scheme.get('rpg'):
                        try:
                            self.secured_rpg = float(secured_scheme['rpg'])
                            print(f"  -> captured scheme rate/gram (rpg) = {self.secured_rpg}")
                        except (TypeError, ValueError):
                            pass
                # Capture the loan-calculator params the server computed for this scheme/amount.
                # final-loan-details must echo these (charges, interest rate, exposure, tenure,
                # dates) or the server's recomputed "calculated amount" won't match ours -> 400.
                if isinstance(data, dict):
                    if data.get('securedprocessingCharge') is not None:
                        self.secured_processing_charge = data.get('securedprocessingCharge')
                    if data.get('upfrontInterestAmount') is not None:
                        self.upfront_interest_amount = data.get('upfrontInterestAmount')
                    if data.get('interestRate') is not None:
                        self.interest_rate = data.get('interestRate')
                    if data.get('posExposureAgainstScheme') is not None:
                        self.secured_exposure = str(data.get('posExposureAgainstScheme'))
                    if data.get('tenure'):
                        try:
                            self.tenure_months = int(data.get('tenure'))
                        except (TypeError, ValueError):
                            pass
                    if data.get('loanStartDate'):
                        self.loan_start_date = str(data.get('loanStartDate'))[:10]
                    if data.get('loanEndDate'):
                        self.loan_end_date = str(data.get('loanEndDate'))[:10]
                    # The portal patches the calculator from this response: the CONFIRMED loan amount,
                    # the secured/top-up split, manual charges and the default co-lender.
                    if data.get('loanAmount') is not None:
                        self.final_loan_amount = float(data['loanAmount'])
                    if data.get('securedLoanAmount') is not None:
                        self.secured_loan_amount = data['securedLoanAmount']
                    self.secured_top_up_amount = data.get('securedTopUpAmount') or 0
                    self.unsecured_top_up_amount = data.get('unsecuredTopUpAmount') or 0
                    self.manual_charges = [
                        {"chargeName": c.get('chargeName'), "charges": c.get('charges')} if isinstance(c, dict) else c
                        for c in (data.get('manualCharges') or [])]
                    default_bank = next((b for b in (data.get('coLenderBanks') or [])
                                         if isinstance(b, dict) and b.get('isDefault')), None)
                    self.auto_assigned_co_lender_id = default_bank['id'] if default_bank else None
                    # No co-lending unless --co-lender was given: the server's default is NOT adopted
                    # (the portal would pre-select it). Declining it counts as an override in the
                    # portal, so final-loan-details carries the override flag + reason.
                    if not self._forced_co_lender_id:
                        self.co_lender_bank_id = ''
                    print(f"  -> captured calc params: procCharge={self.secured_processing_charge} "
                          f"interestRate={self.interest_rate} exposure={self.secured_exposure} "
                          f"tenure={self.tenure_months}")
                # Reject schemes whose karat range doesn't cover every ornament: the server would
                # then compute eligibility over only the karat-eligible ornaments, so our
                # totalEligibleAmt (over all ornaments) would mismatch. Try the next candidate.
                s_min = self._extract_karat_value(secured_scheme.get('minKarat')) if isinstance(secured_scheme, dict) else 0
                s_max = self._extract_karat_value(secured_scheme.get('maxKarat')) if isinstance(secured_scheme, dict) else 0
                if ornament_karats and s_min and s_max and not all(s_min <= k <= s_max for k in ornament_karats):
                    last_reason = (f"scheme {self.scheme_id} karat range {s_min}-{s_max} does not cover "
                                   f"ornament karats {ornament_karats}")
                    print(f"  -> {last_reason}; trying next candidate ({idx + 1}/{len(candidates)}).")
                    continue
                print(f'Check loan type successful (scheme {self.scheme_id}, rpg {self.secured_rpg}, '
                      f'karat {s_min}-{s_max} covers {ornament_karats}).')
                return
            except httpx.HTTPStatusError as e:
                text = (e.response.text or "").lower()
                if e.response.status_code == 400 and "loan already exists" in text:
                    print(f"Loan type for loan {self.loan_id} already exists. Continuing to next step.")
                    return
                # Scheme's amount range doesn't fit this loan amount — try the next candidate.
                if "range amount is greater than" in text or "scheme range" in text or "range amount" in text:
                    print(f"Scheme {self.scheme_id} range doesn't fit loan amount {loan_amount_to_use}; "
                          f"trying next candidate ({idx + 1}/{len(candidates)}).")
                    last_error = e
                    last_reason = f"scheme {self.scheme_id} amount range doesn't fit {loan_amount_to_use}"
                    continue
                raise  # Any other error is fatal.

        # No candidate scheme fit (by amount range or karat coverage).
        print("No accepted-partner scheme covers both the loan amount and the ornament karats "
              f"({ornament_karats}). Last reason: {last_reason or 'unknown'}. "
              "Adjust GOLD_LOAN_AMOUNT, widen the partner list, or the ornament karats.")
        if last_error is not None:
            raise last_error
        raise ValueError(f"No eligible scheme found: {last_reason or 'no candidates matched'}.")


    async def get_interest_rate(self):
        api_path = "/api/loan-process/interest-rate"
        request_body = {
            "securedSchemeId": int(self.scheme_id) if self.scheme_id else None,
            "unsecuredSchemeId": None,
        }
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        resp_json = response.json()
        self._diagnostics['interest_rate_response'] = resp_json
        self._print_json_block("interest-rate response", resp_json)
        self._maybe_set_rpg(resp_json, "interest-rate")
        print('Get interest rate successful.')


    async def generate_interest_table(self):
        api_path = "/api/loan-process/generate-interest-table"
        # Use the tenure/interest-rate/dates the server returned at check-loan-type (fall back to
        # defaults). The server bases the schedule on the scheme's tenure; a mismatched tenure makes
        # the interest table (and thus final-loan-details' calculated amount) diverge.
        tenure_months = self.tenure_months or 4
        loan_start_date = self.loan_start_date or datetime.date.today().isoformat()
        loan_end_date = self.loan_end_date or (
            datetime.date.today() + datetime.timedelta(days=tenure_months * 30)).isoformat()

        request_body = {
            "partnerId": int(self.partner_id) if self.partner_id and self.partner_id.isdigit() else (self.partner_id or None),
            "coLenderBankId": int(self.co_lender_bank_id) if self.co_lender_bank_id else None,
            "schemeId": int(self.scheme_id) if self.scheme_id else None,
            "finalLoanAmount": self.final_loan_amount,  # Use dynamic value
            "tenure": tenure_months,
            "loanStartDate": loan_start_date,
            "loanEndDate": loan_end_date,
            "paymentFrequency": self._payment_frequency(),  # scheme interestBillingCycleType
            "totalFinalInterestAmt": None,
            "unsecuredInterestRate": None,
            "interestRate": self.interest_rate,  # scheme's real rate (from check-loan-type)
            "processingCharge": self.secured_processing_charge,
            "unsecuredSchemeId": None,
            "securedLoanAmount": self.secured_loan_amount if self.secured_loan_amount is not None else self.final_loan_amount,  # check-loan-type
            "unsecuredLoanAmount": None,
            "isUnsecuredSchemeApplied": False,
            "unsecuredRebateInterest": None,
            "securedRebateInterest": None,
            "otherAmount": None,
            "loanTransferExtraAmount": None,
            "upfrontInterestAmount": self.upfront_interest_amount or 0,  # from check-loan-type
            "topUpAmount": 0,
            "securedTopUpAmount": self.secured_top_up_amount,
            "unsecuredTopUpAmount": self.unsecured_top_up_amount,
            "manualCharges": self.manual_charges,   # check-loan-type manualCharges
            "unsecuredRpg": None,
            "securedRpg": self.secured_rpg if self.secured_rpg else 4480,  # scheme rate-per-gram
            "unsecuredPartnerId": None,
            "unsecuredprocessingCharge": 0,
            "coLenderOverrideReason": self._co_lender_override()[1],
            "securedprocessingCharge": self.secured_processing_charge,  # from check-loan-type
            "securedExposure": str(self.secured_exposure),  # posExposureAgainstScheme (check-loan-type)
        }
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        resp_json = response.json()
        self._diagnostics['generate_interest_table_response'] = resp_json
        self._print_json_block("generate-interest-table response", resp_json)
        # The payload is wrapped in a "data" object: {"data": {"interestTable": [...],
        # "totalInterestAmount": N}}. Reading the top level (as before) silently dropped the
        # schedule -> final-loan-details got interestTable=[] and totalFinalInterestAmt=0, which the
        # server rejects with "total eligible amount doesn't match the calculated amount" (its
        # calculation needs the interest schedule). Read from data and pass the table straight
        # through; its row shape already matches what final-loan-details expects.
        data = resp_json.get('data', resp_json) if isinstance(resp_json, dict) else {}
        self.interest_table_data = data.get('interestTable', []) or []
        self.total_final_interest_amt = (
            data.get('totalInterestAmount',
                     data.get('totalFinalInterestAmt', resp_json.get('totalFinalInterestAmt', 0)))
            or 0
        )
        # securedRebateInterest is server-computed here and echoed into final-loan-details.
        if data.get('securedRebateInterest') is not None:
            self.secured_rebate_interest = data.get('securedRebateInterest')
        # Prefer a server-provided securedRpg; otherwise keep the rate captured at scheme selection
        # / check-loan-type rather than clobbering it. (No rpg is normally present here.)
        self._maybe_set_rpg(resp_json, "generate-interest-table")
        print(f"Generate interest table successful "
              f"({len(self.interest_table_data)} rows, totalInterest {self.total_final_interest_amt}).")


    async def get_final_loan_details(self):
        api_path = "/api/loan-process/final-loan-details"
        # Use the tenure/dates the server assigned at check-loan-type (fall back to defaults).
        tenure_months = self.tenure_months or 4
        loan_start_date = self.loan_start_date or datetime.date.today().isoformat()
        loan_end_date = self.loan_end_date or (
            datetime.date.today() + datetime.timedelta(days=tenure_months * 30)).isoformat()

        # The backend validates totalEligibleAmt against the eligibility it CALCULATED for the
        # stored ornaments (netWtAfterPurity * secured rate-per-gram). We must echo the server's
        # own ornament ids and per-gram rate, or it 400s with "total eligible ... doesn't match".
        def _num(value, default=0.0):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        # Eligibility = netWtAfterPurity * the SELECTED scheme's rate/gram (rpg). CONFIRMED against a
        # real accepted (200) final-loan-details capture (fic flow.har): scheme 853 rpg 4533,
        # loanAmount = ornamentsCal * 4533 per ornament, totalEligibleAmt = sum(loanAmount). The
        # scheme rpg already encodes LTV (rpg/ltv is a per-partner constant), so we do NOT apply LTV
        # again and do NOT use the gold valuation (currentLtvAmount). loanFinalCalculator.securedRpg
        # equals this same scheme rpg. Prereq: eligible must be >= finalLoanAmount (the accepted
        # capture had eligible 653749 > loan 400000) and all ornaments karat-eligible (check_loan_type
        # enforces karat) -- store_ornament_details generates enough gold weight to clear the loan.
        scheme_rpg = _num(self.secured_rpg) or _num((self.selected_scheme or {}).get('rpg'))
        if scheme_rpg <= 0:
            raise ValueError(
                "Secured rate/gram (rpg) was not captured from the selected scheme. "
                "Scheme selection (partner-scheme-amount / check-loan-type) must run before "
                "final-loan-details so the scheme's rpg is known."
            )
        ornaments_for_request = []
        calculated_total = 0.0
        for ornament in self.loan_ornaments_details:
            net_wt_after_purity = _num(ornament.get('netWtAfterPurity') or ornament.get('ornamentsCal'))
            ornaments_for_request.append({
                "id": ornament.get('id'),
                "loanAmount": net_wt_after_purity * scheme_rpg,   # unrounded, as the portal sends it
                "ornamentsCal": net_wt_after_purity,
                "rpg": scheme_rpg,
            })
            calculated_total += net_wt_after_purity

        # The portal: totalAmt = +(totalNetWeightAfterPurity * rpg).toFixed(2), where the total weight
        # is the ornaments-details response's netWtAfterPurity (fallback: sum of the ornaments').
        total_nwap = self.ornaments_total_nwap if self.ornaments_total_nwap is not None else calculated_total
        self.total_eligible_amount = self._round_half_up(total_nwap * scheme_rpg, 2)
        self.secured_rpg = scheme_rpg  # scheme loan rate/gram, echoed in loanFinalCalculator.securedRpg
        print(f"  -> final-loan-details totalEligibleAmt = {self.total_eligible_amount} "
              f"(sum of netWtAfterPurity * scheme rpg {scheme_rpg}).")

        # A loan can't exceed its eligibility; cap the requested amount if the ornaments are small.
        if self.final_loan_amount and self.final_loan_amount > self.total_eligible_amount:
            self.final_loan_amount = self.total_eligible_amount
            print(f"  -> capped finalLoanAmount to eligible amount {self.final_loan_amount}")

        loan_final_calculator = {
            "partnerId": int(self.partner_id) if self.partner_id and self.partner_id.isdigit() else (self.partner_id or None),
            "coLenderBankId": int(self.co_lender_bank_id) if self.co_lender_bank_id else None,
            "schemeId": int(self.scheme_id) if self.scheme_id else None,
            "finalLoanAmount": self.final_loan_amount, # Use dynamic value
            "tenure": tenure_months,  # server-assigned scheme tenure (from check-loan-type)
            "loanStartDate": loan_start_date,
            "loanEndDate": loan_end_date,
            "paymentFrequency": self._payment_frequency(),  # scheme interestBillingCycleType
            "totalFinalInterestAmt": self.total_final_interest_amt, # from generate-interest-table
            "unsecuredInterestRate": None,
            "interestRate": self.interest_rate,  # scheme's real rate (from check-loan-type)
            "processingCharge": self.secured_processing_charge,  # scheme's real processing charge
            "unsecuredSchemeId": None,
            "securedLoanAmount": self.secured_loan_amount if self.secured_loan_amount is not None else self.final_loan_amount,  # check-loan-type
            "unsecuredLoanAmount": None,
            "isUnsecuredSchemeApplied": False,
            "unsecuredRebateInterest": 0,
            "securedRebateInterest": self.secured_rebate_interest,  # from generate-interest-table
            "otherAmount": None,
            "loanTransferExtraAmount": None,
            "upfrontInterestAmount": self.upfront_interest_amount,  # from check-loan-type
            "topUpAmount": 0,
            "securedTopUpAmount": self.secured_top_up_amount,
            "unsecuredTopUpAmount": self.unsecured_top_up_amount,
            "manualCharges": self.manual_charges,   # check-loan-type manualCharges
            "unsecuredRpg": None,
            "securedRpg": self.secured_rpg, # Use dynamic value
            "unsecuredPartnerId": None,
            "unsecuredprocessingCharge": 0,
            "coLenderOverrideFlag": self._co_lender_override()[0],
            "coLenderOverrideReason": self._co_lender_override()[1],
            "systemAssignedBankId": self.auto_assigned_co_lender_id,
            "securedprocessingCharge": self.secured_processing_charge,  # scheme's real processing charge
            "securedExposure": str(self.secured_exposure),  # posExposureAgainstScheme (from check-loan-type)
        }

        request_body = {
            "manualCharges": self.manual_charges,   # check-loan-type manualCharges
            "loanFinalCalculator": loan_final_calculator,
            "interestTable": self.interest_table_data, # Use dynamic value
            "loanId": int(self.loan_id) if self.loan_id else None,
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            "ornaments": ornaments_for_request, # Use dynamic value
            "totalEligibleAmt": self.total_eligible_amount, # Use dynamic value
            "checkPointers": {},
        }

        # Dump every input the server uses to calculate eligibility, so the exact rate/formula can
        # be pinned from real data instead of guessed. Written regardless of success/failure.
        self._diagnostics['final_loan_details_request'] = request_body
        self._diagnostics['gold_rate'] = self.gold_rate
        self._diagnostics['secured_rpg_used'] = scheme_rpg
        self._dump_loan_calc_diagnostics()

        try:
            response = await self._make_authenticated_request(
                'POST',
                api_path,
                json_data=request_body
            )
            print('Get final loan details successful.')
            self._capture_final_loan_details(response)
            return
        except httpx.HTTPStatusError as e:
            body = e.response.text if e.response is not None else ""
            is_eligibility_mismatch = (
                e.response is not None and e.response.status_code == 400
                and "eligible amount" in body.lower()
            )
            if not is_eligibility_mismatch:
                raise
            # DO NOT blindly re-submit single-loan's totalEligibleAmt: for an in-progress loan that
            # is the VALUATION (sum of currentLtvAmount = nwap * goldRate ~ 1.66M), NOT the rpg-based
            # eligible final-loan-details wants (~610k). Adopting it just fails again with the same
            # 400. Instead fetch it for DIAGNOSTICS -- compare the server's stored nwap/currentLtvAmount
            # against what we sent -- then surface the original error with the comparison logged.
            print("  -> eligibility mismatch; fetching single-loan for a server-vs-sent comparison.")

        server_view = await self.fetch_single_loan()
        server_total = self._find_key_recursive(server_view, 'totalEligibleAmt')
        server_ornaments = self._extract_ornaments_from_response(server_view) or []
        sent_by_id = {str(o.get('id')): o for o in ornaments_for_request}
        print(f"  -> sent totalEligibleAmt={self.total_eligible_amount} (rpg {scheme_rpg}); "
              f"single-loan totalEligibleAmt={server_total}")
        print("  -> per-ornament server(stored) vs sent:")
        for o in server_ornaments:
            oid = str(o.get('id'))
            snwap = o.get('netWtAfterPurity')
            clv = o.get('currentLtvAmount')
            sent = sent_by_id.get(oid, {})
            print(f"       id={oid} server nwap={snwap} currentLtvAmount={clv} | "
                  f"sent ornamentsCal={sent.get('ornamentsCal')} loanAmount={sent.get('loanAmount')}")
        self._diagnostics['single_loan_on_mismatch'] = {
            'totalEligibleAmt': server_total,
            'ornaments': server_ornaments,
        }
        self._dump_loan_calc_diagnostics()
        raise RuntimeError(
            "final-loan-details rejected our eligibility (structurally identical to the accepted "
            f"capture): sent {self.total_eligible_amount} = Sum(nwap*{scheme_rpg}). See "
            "'single_loan_on_mismatch' in loan_calc_debug.json for the server's stored ornaments."
        )

    def _is_arvog_loan(self) -> bool:
        return self._partner_key() == self.ARVOG_PARTNER_ID

    def _co_lender_override(self):
        """(flag, reason) as the portal derives them: overridden when the chosen co-lender differs
        from the server's auto-assigned default; the reason is then mandatory
        (GOLD_LOAN_CO_LENDER_OVERRIDE_REASON, default 'Customer preference')."""
        default = self.auto_assigned_co_lender_id
        if not default:
            return False, None
        chosen = int(self.co_lender_bank_id) if str(self.co_lender_bank_id or '').isdigit() else None
        if chosen == int(default):
            return False, None
        default_reason = "Customer preference" if chosen else "No co-lending requested"
        return True, os.getenv("GOLD_LOAN_CO_LENDER_OVERRIDE_REASON", default_reason)

    def _payment_frequency(self) -> str:
        """The scheme's billing cycle as the interest table names it ("30Days" -> "30 Days")."""
        cycle = str((self.secured_scheme or {}).get('interestBillingCycleType') or '').strip()
        digits = cycle.lower().replace('days', '').replace('day', '').strip()
        return f"{digits} Days" if digits.isdigit() else "30 Days"

    def _capture_final_loan_details(self, response):
        """Keep the final-loan-details response: bank-details is filled from it (balances, cash
        limits, charges, stamp duty), exactly as the web app does."""
        try:
            data = response.json()
        except ValueError:
            data = {}
        self.final_loan_details_data = data if isinstance(data, dict) else {}
        self._capture_stamp_duty(response)

    def _capture_stamp_duty(self, response):
        """Adopt final-loan-details' stampDutyCharges whenever the server states one (0 included);
        the configured default (GOLD_LOAN_STAMP_DUTY, default 0 like the portal) is only a fallback -- see
        _ensure_stamp_duty."""
        try:
            data = response.json()
        except ValueError:
            data = {}
        self._adopt_server_stamp_duty(data if isinstance(data, dict) else {}, "final-loan-details")

    def _adopt_server_stamp_duty(self, data: dict, source: str) -> bool:
        """Take stampDutyCharges (+ definition type) from a server payload. Returns True when the
        server stated a figure (0 included) and it was adopted. A live TEST run confirmed the server
        validates toBePaid against ITS figure: final-loan-details said 0 on an Augmont-branch loan and
        bank-details rejected the 1500 default with "To Be Paid amount is incorrect"."""
        raw = data.get('stampDutyCharges')
        try:
            server_duty = float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            server_duty = None
        arvog = self._is_arvog_loan()
        adopt = server_duty is not None
        if adopt:
            self.stamp_duty_charges = server_duty
            self.stamp_duty_definition_type = data.get('stampDutyDefinitionType')
            self.stamp_duty_from_server = True
        print(f"  -> stamp duty: {self.stamp_duty_charges} "
              f"({'from ' + source if adopt else 'configured default'}"
              f"{', ARVOG' if arvog else ''})")
        return adopt

    async def _ensure_stamp_duty(self):
        """bank-details must carry the SERVER's stamp duty. If final-loan-details did not state one,
        read it from single-loan (data.stampDutyCharges). If the server still gives none:
        GOLD_LOAN_STAMP_DUTY, when set, is a deliberate override; otherwise ARVOG loans stop here
        rather than send a guessed toBePaid, and other partners fall back to the default (0)."""
        if self.stamp_duty_from_server:
            return
        print("Stamp duty not in final-loan-details: fetching it from single-loan before bank details.")
        payload = await self.fetch_single_loan()
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and self._adopt_server_stamp_duty(data, "single-loan"):
            return
        if os.getenv("GOLD_LOAN_STAMP_DUTY"):
            print(f"  -> server gave no stamp duty; using GOLD_LOAN_STAMP_DUTY={self.stamp_duty_charges}.")
            return
        if not self._is_arvog_loan():
            print(self._c(f"  -> server gave no stamp duty; falling back to the default "
                          f"{self.stamp_duty_charges}.", "yellow"))
            return
        raise RuntimeError(
            f"ARVOG loan {self.loan_id}: neither final-loan-details nor single-loan returned "
            "stampDutyCharges, so bank-details' toBePaid cannot be computed. Set "
            "GOLD_LOAN_STAMP_DUTY to the branch's stamp duty to proceed."
        )

    def _dump_loan_calc_diagnostics(self):
        debug_path = os.path.join(os.getcwd(), "loan_calc_debug.json")
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump(self._diagnostics, f, indent=2, ensure_ascii=False, default=str)
            print("\n" + self._c(f"### LOAN-CALC DEBUG written to {debug_path}", "bold", "yellow"))
            print(self._c("### If final-loan-details fails, share this file so the eligibility "
                          "rate/formula can be pinned exactly.", "yellow"))
        except OSError as e:
            print(f"Could not write loan-calc debug file: {e}")


    async def store_bank_details(self):
        api_path = "/api/loan-process/bank-details"
        # ARVOG: the server's stamp duty must be known before toBePaid is computed below.
        await self._ensure_stamp_duty()
        # Use whatever account validate_account settled on (it walks reference/bank_accounts.json
        # until the penny-drop accepts one). The hard-coded SBI account is only the fallback for
        # when validate_account never ran or every candidate failed.
        account_number = self.bank_account_number or self.DEFAULT_BANK_ACCOUNT["accountNumber"]
        ifsc_code = self.bank_ifsc_code or self.DEFAULT_BANK_ACCOUNT["ifscCode"]
        bank_name = self.bank_name or "STATE BANK OF INDIA"          # from the IFSC lookup
        bank_branch_name = self.bank_branch_name or "SBI CAPITAL MARKET BRANCH, MUMBAI"
        account_holder_name = self.account_holder_name or "LIC MUTUAL FUND"

        if self._passbook_from_saved and self.passbook_proofs:
            # A saved account keeps the proof already on file.
            passbook_proofs = list(self.passbook_proofs)
        else:
            # Bank proof = cancelled cheque image (both proof slots use it).
            passbook_proofs = [
                (await self._upload_file(
                    "loan", file_type="image", file_path_override=self.CHEQUE_IMAGE_PATH))['uploadFile']['path']
                for _ in range(2)
            ]

        # Persist disbursement bank details so validate-account and loan-documents can reuse them.
        self.bank_name = bank_name
        self.bank_account_number = account_number
        self.bank_ifsc_code = ifsc_code
        self.account_holder_name = account_holder_name
        self.bank_branch_name = bank_branch_name
        self.passbook_proofs = passbook_proofs

        # "To Be Paid" = final loan amount MINUS the scheme's processing charge, upfront interest and
        # the branch's stamp duty. The server rejects bank-details with "To Be Paid amount is
        # incorrect" if this doesn't match its own computation. processingCharge/upfrontInterestAmount
        # are captured from check-loan-type (data.securedprocessingCharge / upfrontInterestAmount),
        # e.g. proc = max(fixed, percent%); stamp duty comes from _capture_stamp_duty.
        fld = self.final_loan_details_data or {}
        balance = fld.get('getBalance') if isinstance(fld.get('getBalance'), dict) else {}
        cash_params = (fld.get('getAdvanceCashParameters')
                       if isinstance(fld.get('getAdvanceCashParameters'), dict) else {})

        def num(value, fallback):
            try:
                return float(value) if value not in (None, "") else float(fallback or 0)
            except (TypeError, ValueError):
                return float(fallback or 0)

        # The server's own figures from final-loan-details, falling back to check-loan-type's.
        proc_charge = num(fld.get('actualProcessingCharge'), self.secured_processing_charge)
        upfront_interest = num(fld.get('upfrontInterestAmount'), 0)         # res.upfrontInterestAmount || 0
        appraiser_pct = fld.get('appraiserCharges') or 0                     # res.appraiserCharges, as sent
        bt_money = num(balance.get('customerTransferBalance'), 0)            # bTMoney = getBalance.customerTransferBalance
        try:
            stamp_duty = float(self.stamp_duty_charges or 0)
        except (TypeError, ValueError):
            stamp_duty = 0.0
        final_amt = num(fld.get('finalLoanAmount'), self.final_loan_amount)    # res.finalLoanAmount
        # The portal (bank details): available = final - (bTMoney + advanceBankTransfer + stampDuty +
        # processingCharge + upfrontInterest); toBePaid = available - (actualCashGiven + appraiserCharges),
        # where actualCashGiven = advanceCash + waiver - actualAppraiserCharges (all 0 here).
        available_limit = self._round_double(final_amt - (bt_money + 0 + max(stamp_duty, 0) + proc_charge
                                                          + max(upfront_interest, 0)), 2)
        to_be_paid = self._round_double(available_limit - (0 + 0), 2)

        request_body = {
            "paymentType": ["bank"],
            "finalScrapAmount": None,
            "bankName": bank_name,
            "accountNumber": account_number,
            "ifscCode": ifsc_code,
            "accountType": None,
            "customerName": self.validated_customer_name or self.full_name,   # validate-account customerName
            "paymentMultiSelect": {"multiSelect": ["bank"]},
            "accountHolderName": account_holder_name,
            "bankBranchName": bank_branch_name,
            "passbookProof": passbook_proofs,
            "passbookProofImage": [p if p.startswith("http") else f"{self.BASE_URL}/{p}"
                                   for p in passbook_proofs],
            "passbookProofImageName": [os.path.basename(p) for p in passbook_proofs],
            "account": None,  # the web app sends null
            "detailsFor": "customer",
            # Branch/customer balances as final-loan-details reported them (getBalance).
            "customerTransferBalance": balance.get('customerTransferBalance', 0),
            "internalBranchTransferBalance": balance.get('branchTotalHolding'),
            "internalBranchBlockedBalance": balance.get('branchTotalBlockedBalance'),
            "advanceBankTransfer": 0,
            "advanceBankTransactionId": None,
            "advanceCash": 0,
            "advanceCashTransactionId": [],
            "advanceCashTransactionImage": [],
            "advanceCashTransactionImageName": "",
            "advanceCashDeclarationId": [],
            "bTMoney": bt_money,
            "actualProcessingCharge": proc_charge,
            "toBePaid": to_be_paid,  # finalLoanAmount - processingCharge - upfrontInterest - stampDuty
            "remark": None,
            "upfrontInterestAmount": upfront_interest,
            "upfrontTransactionId": None,
            "upfrontReceipt": [],
            "upfrontReceiptImage": [],
            "upfrontReceiptImageName": "",
            "amountDisbursedByCash": 0,
            "amountDisbursedByBank": 0,
            "forOpsApproval": False,
            # bank-details 400s with "bank details is not verified" unless the account is verified.
            # If validate-account's penny-drop succeeded (bankTxnStatus True) send it as system-verified;
            # otherwise mark it MANUALLY verified (no penny-drop API for our dummy account) so the flow
            # proceeds -- manual entry is acceptable here per the test's ops-rating stage.
            # NOTE: the server OVERRIDES these flags with its own validate-account verdict -- the
            # stored record comes back isVerified false / forOpsApproval true / bankVerificationStatus
            # "pending" whatever we send here, because the account name does not match the customer.
            # ops_manual_bank_verification() clears that pending state in the Ops stage.
            "isManuallyVerified": not getattr(self, "bank_account_verified", False),
            "isVerified": True,
            "isDummyDetails": False,
            "manualVerifiedStatus": "" if getattr(self, "bank_account_verified", False) else "verified",
            "wavier": 0,
            "processingCharge": proc_charge,
            "receiptNumber": "",
            "signature": self.signature_proof,
            "pendingAmount": "",
            "totalPledgedGoldAmount": "",
            "extraAmountDisbursed": "",
            "advanceAmount": "",
            "modeOfPayment": "",
            "appraiserCharges": 0,
            "actualAppraiserCharges": 0,
            "appraiserChargesPercentage": appraiser_pct or 0,  # final-loan-details appraiserCharges
            "actualCashGiven": 0,
            "availableLimitForDisbuserment": available_limit,
            "wavierAppraiserCharges": 0,
            "stampDutyCharges": stamp_duty,
            "stampDutyDefinitionType": self.stamp_duty_definition_type,
            "stampDutyRemarks": "",
            "totalLimitForCashDisbursal": cash_params.get('totalLimitForCashDisbursal', 200000),
            "multiModeDisbursement": cash_params.get('multiModeDisbursement', True),
            "checkPointers": {},  # From collection example
            "loanId": int(self.loan_id) if self.loan_id else None,
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            "loanType": self.loan_type or "Fresh Loan",
            "createdBy": str(self.logged_in_user_id or balance.get('userId') or ""),  # the logged-in user
            "internalBranchId": int(self.internal_branch_id) if self.internal_branch_id else None, # Convert to int
        }

        try:
            response = await self._make_authenticated_request(
                'POST',
                api_path,
                json_data=request_body
            )
        except httpx.HTTPStatusError as e:
            body = e.response.text or ""
            if not (e.response.status_code == 400 and "not verified" in body.lower()):
                raise
            # The penny-drop did not verify the account -- on UAT validate-account itself 400s
            # "Something went wrong" -- and the server ignores the isManuallyVerified flags we
            # send here (it applies its own validate-account verdict). Only the OPS team's manual
            # verification clears that state, so run it NOW rather than at the Ops stage: the flow
            # cannot reach the Ops stage without bank-details succeeding first.
            print(self._c(f"bank-details rejected ({body.strip()[:120]}). Running the OPS manual "
                          "bank verification now, then retrying.", "yellow"))
            await self.ops_manual_bank_verification(force=True, strict=False)
            request_body["isManuallyVerified"] = True
            request_body["manualVerifiedStatus"] = "verified"
            response = await self._make_authenticated_request(
                'POST',
                api_path,
                json_data=request_body
            )
            print('Store bank details successful (after the ops manual bank verification).')
            return response
        print('Store bank details successful.')


    async def add_appraiser_rating(self):
        api_path = "/api/loan-process/appraiser-rating"
        request_body = {
            "applicationFormForAppraiser": True,
            "goldValuationForAppraiser": True,
            "loanStatusForAppraiser": "approved",
            "commentByAppraiser": None,
            "partRelease": None,
            "loanId": int(self.loan_id) if self.loan_id else None,
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
        }
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        print('Add appraiser rating successful.')


    async def add_bm_rating(self):
        api_path = "/api/loan-process/bm-rating"
        request_body = {
            "loanId": int(self.loan_id) if self.loan_id else None,
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            "applicationFormForBM": True,
            "goldValuationForBM": True,
            "loanStatusForBM": "approved",
            "commentByBM": None,
            "partRelease": None,
        }
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        print('Add BM rating successful.')


    async def add_ops_rating(self):
        api_path = "/api/loan-process/ops-rating"
        # The portal's approval form is patched from masterLoan and sent with getRawValue(): Ops only
        # CHOOSES its own three fields; everything the appraiser/BM decided is copied from the loan.
        loan = await self._loan_record()
        request_body = self._rating_base(loan, defaults={
            "applicationFormForAppraiser": True, "goldValuationForAppraiser": True,
            "loanStatusForAppraiser": "approved", "commentByAppraiser": None,
            "applicationFormForBM": False, "goldValuationForBM": False, "loanStatusForBM": "pending",
            "applicationFormForPartner": False, "goldValuationForPartner": False,
            "loanStatusForPartner": "pending", "scrapStatusForAppraiser": None, "scrapStatusForBM": None,
            "scrapStatusForOperatinalTeam": "pending", "packetApprovalByOps": None,
            "commentByOpsForPacketApproval": "", "statusForRh": "pending", "commentByRh": "",
            "applicationFormForRh": False, "goldValuationForRh": False,
        })
        request_body.update({
            "applicationFormForOperatinalTeam": True,
            "goldValuationForOperatinalTeam": True,
            "loanStatusForOperatinalTeam": "approved",
        })
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        print('Add Ops rating successful.')

    # 5-lakh threshold above which a Branch Manager approval (BM rating) is required.
    BM_RATING_THRESHOLD = 500000

    # The server refuses any loan-process call whose stage has moved on, with a 400 whose body
    # names the stage it is now waiting on:
    #   {"message":"Loan Stage has been changed to: bm rating, please re-visit the applied loan page."}
    # That is not really a failure -- it is the server telling us which step to do first.
    STAGE_CHANGED_MARKER = "Loan Stage has been changed to"

    @classmethod
    def _stage_changed_to(cls, response_text: str) -> str:
        """The stage name out of that message, lower-cased, or '' for any other error."""
        if not response_text or cls.STAGE_CHANGED_MARKER not in response_text:
            return ""
        tail = response_text.split(cls.STAGE_CHANGED_MARKER, 1)[1]
        return tail.lstrip(': "').split(",")[0].strip().strip('".').lower()

    async def _settle_pending_stage(self, response_text: str) -> bool:
        """Do whatever the loan is now waiting on. True when the caller should retry.

        Only stages this harness can actually settle are handled; anything else is reported and
        left alone, so an unexpected stage surfaces as the original error rather than a silent
        no-op retry loop.
        """
        stage = self._stage_changed_to(response_text)
        if not stage:
            return False
        if stage == "bm rating":
            if self.bm_rating_submitted:
                print(f"Loan is waiting on '{stage}' but the BM rating was already submitted; "
                      "not retrying.")
                return False
            print(self._c(f"Loan stage moved to '{stage}' -- submitting the BM rating "
                          "(the amount is at or over the 5L threshold), then retrying.", "yellow"))
            await self._submit_bm_rating()
            return True
        if stage == "disbursement pending":
            # The ops rating already advanced the loan to where disbursement wants it; the
            # caller's own call is what is out of date, not the loan.
            print(f"Loan stage moved to '{stage}'; nothing to settle before disbursement.")
            return False
        print(self._c(f"Loan stage moved to '{stage}' and this harness has no recovery for it.",
                      "yellow"))
        return False

    async def _with_stage_recovery(self, description: str, send):
        """Run `send()`; if the server says the stage moved, settle that stage and retry ONCE.

        One retry only: if the stage moves again the loan is being changed underneath us and
        looping would just hide it.
        """
        try:
            return await send()
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 400:
                raise
            if not await self._settle_pending_stage(e.response.text or ""):
                raise
            print(f"Retrying {description} now that the pending stage is settled.")
            return await send()

    async def submit_bm_rating_if_required(self):
        """BM approval is only needed when the loan amount exceeds 5L. BM rating must be posted
        under the BM's own login (per-environment mobile, OTP 1234), so swap to a BM role token for
        just this call and restore the appraiser session afterwards."""
        amount = float(self.final_loan_amount) if self.final_loan_amount else 0.0
        # The threshold is INCLUSIVE: a loan of exactly 500000 is moved to the "bm rating"
        # stage by the server (confirmed on UAT -- loanAmountDigi "500000.00" then answered
        # "Loan Stage has been changed to: bm rating" on loan-documents and ops-rating). A
        # strict > here skipped the rating and deadlocked the rest of the flow.
        if amount < self.BM_RATING_THRESHOLD:
            print(f"BM rating skipped: loan amount {amount:.2f} < {self.BM_RATING_THRESHOLD} threshold.")
            return
        print(f"BM rating required: loan amount {amount:.2f} >= {self.BM_RATING_THRESHOLD} threshold.")
        try:
            await self._submit_bm_rating()
        except httpx.HTTPStatusError as e:
            # The amount rule is only our guess at the server's: an exactly-5L ARVOG loan on UAT went
            # straight to "upload documents" and bm-rating then 400'd "Loan Stage has been changed to:
            # upload documents". A stage other than "bm rating" means the server is NOT waiting on a
            # BM rating, so skip it. If it does want one later, loan-documents / ops-rating get "bm
            # rating" back and _settle_pending_stage submits it then.
            stage = self._stage_changed_to(e.response.text or "") if e.response.status_code == 400 else ""
            if not stage or stage == "bm rating":
                raise
            print(self._c(f"BM rating not needed: the server already moved the loan to '{stage}' "
                          "(no BM approval stage for this loan). Continuing.", "yellow"))

    async def _submit_bm_rating(self) -> None:
        """Post the BM rating under the BM's own login, then restore the appraiser session."""
        saved_token = self.auth_token
        try:
            self.auth_token = await self._role_token("bm")
            await self.add_bm_rating()
            self.bm_rating_submitted = True
        finally:
            self.auth_token = saved_token

    async def submit_ops_rating(self):
        """Ops rating (final approval) must be posted under the Ops login (per env, OTP 1234).
        Swap to an ops role token for just this call, then restore the appraiser session."""
        async def send():
            saved_token = self.auth_token
            try:
                self.auth_token = await self._role_token("ops")
                return await self.add_ops_rating()
            finally:
                self.auth_token = saved_token
        await self._with_stage_recovery("ops rating", send)

    async def ops_manual_bank_verification(self, force: bool = False, strict: bool = True):
        """MANUAL bank verification by the OPS team -- clears the pending bank-verification state.

        Why this exists: validate-account's penny-drop succeeds (bankTxnStatus true) but the server
        still refuses to mark the account verified because the bank's account name
        ("LIC MUTUAL FUND") does not match the customer's name, so it answers
        `isVerified:false, isManuallyVerified:false, forOpsApproval:true` and stores the bank detail
        with `bankVerificationStatus:"pending"` (CONFIRMED in `ops complete.har` -> single-loan
        `data.loanBankDetail`). The loan then cannot be disbursed until an ops user approves the
        account manually.

        In the real ops session (`ops complete.har`, entries 35/41) the order is:
        validate-account -> POST /api/loan-process/bank-verification-manual (status "approved")
        -> update-processing-charge -> ops-rating. This mirrors that, and like every other role call
        it swaps to the OPS role token for the duration and restores the appraiser session after.

        Set `force=True` (or leave the state unknown) to send the approval regardless of the flags
        captured by validate_account. `strict=False` downgrades the post-conditions to warnings --
        used when this runs BEFORE bank-details (store_bank_details' fallback), where the bank
        detail record does not exist yet and the retried bank-details call is the real check.
        """
        if not force and self.bank_manually_verified and not self.bank_for_ops_approval:
            print("Manual bank verification skipped: account is already manually verified.")
            return
        if not force and self.bank_system_verified and not self.bank_for_ops_approval:
            print("Manual bank verification skipped: account is already system-verified.")
            return

        api_path = "/api/loan-process/bank-verification-manual"
        request_body = {
            "accountNumber": self.bank_account_number or "00000036150491589",
            "accountHolderName": self.account_holder_name or self.full_name,
            "ifscCode": self.bank_ifsc_code or "SBIN0011777",
            "detailsFor": "customer",
            "customerId": int(self.customer_id) if self.customer_id else None,
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            "loanId": int(self.loan_id) if self.loan_id else None,
            "status": "approved",
            "requestFrom": "loan",
            "incompleteReason": "",
            "approvalReason": "test",
        }
        saved_token = self.auth_token
        try:
            self.auth_token = await self._role_token("ops")
            response = await self._make_authenticated_request('POST', api_path, json_data=request_body)
            payload = response.json()
            # Response shape: {"message":"success","data":{... isVerified, isManuallyVerified,
            # forOpsApproval ...}} -- the verification verdict lives one level down, under "data".
            result = payload.get('data') if isinstance(payload.get('data'), dict) else payload
            self.bank_manually_verified = bool(result.get('isManuallyVerified'))
            self.bank_system_verified = bool(result.get('isVerified')) or self.bank_system_verified
            self.bank_for_ops_approval = bool(result.get('forOpsApproval'))
            print(f"Manual bank verification submitted by ops (isManuallyVerified="
                  f"{self.bank_manually_verified}, isVerified={self.bank_system_verified}, "
                  f"forOpsApproval={self.bank_for_ops_approval}).")
            problems = []
            if not (self.bank_manually_verified or self.bank_system_verified):
                problems.append("the account is still not verified")
            if self.bank_for_ops_approval:
                problems.append("the bank detail is still pending ops approval")
            if problems:
                detail = (f"Ops manual bank verification: {' and '.join(problems)} "
                          f"-- {json.dumps(payload)[:500]}")
                if strict:
                    raise AssertionError(detail)
                print(self._c(f"WARNING: {detail}", "yellow"))
        except httpx.HTTPStatusError as e:
            body = e.response.text or ""
            # Approving a bank detail that ops already approved comes back as a 400; that is the
            # state we wanted, so treat it as done rather than failing the run.
            if e.response.status_code == 400 and "already" in body.lower():
                self.bank_manually_verified = True
                self.bank_for_ops_approval = False
                print(f"Manual bank verification already done: {body}")
            else:
                print(self._c(f"Manual bank verification FAILED: {e.response.status_code} - {body}",
                              "red"))
                raise
        finally:
            self.auth_token = saved_token


    async def _upload_file_base(self, file_type: str = "image") -> str:
        """Upload a base64-encoded image via /api/upload-file/base (used for packet images)."""
        import base64
        file_path_on_disk = self.DUMMY_IMAGE_PATH if file_type == "image" else self.DUMMY_PDF_PATH
        mime = mimetypes.guess_type(file_path_on_disk)[0] or "image/jpeg"
        with open(file_path_on_disk, "rb") as fh:
            encoded = base64.b64encode(fh.read()).decode("ascii")
        api_path = "/api/upload-file/base"
        request_body = {"avatar": f"data:{mime};base64,{encoded}"}
        response = await self._make_authenticated_request('POST', api_path, json_data=request_body)
        data = response.json()
        upload = data.get("uploadFile") if isinstance(data, dict) else None
        path = (upload or {}).get("path") if isinstance(upload, dict) else None
        path = path or (data.get("path") if isinstance(data, dict) else None)
        assert path, f"upload-file/base did not return a path: {data}"
        print(f"Base64 file uploaded successfully. Path: {path}")
        return path

    async def _role_token(self, login_type: str) -> str:
        """Authenticate as another ROLE (admin/bm/ops/partner) and return that role's bearer token
        WITHOUT persisting it or mutating the current session's saved state. Callers swap
        self.auth_token for this around the role-only calls, then restore the appraiser token in a
        finally. Static OTP 1234 for all roles.

        Tokens are CACHED per mobile for the run: a role used more than once (admin does customer
        creation AND packet create/assign) logs in only ONCE. The OTP send endpoint rate-limits
        repeat requests to the same number ('Please try again after a minute'), and a fast run fires
        both within that window -- so re-requesting an OTP we already hold would 400."""
        mobile = self._get_mobile_number_for_login_type(login_type)
        cached = self._role_token_cache.get(mobile)
        if cached:
            print(f"{login_type.capitalize()} authenticated (reusing cached role token).")
            return cached
        r1 = await self._make_authenticated_request(
            'POST', "/api/user-otp/user-send-otp",
            json_data={"mobileNumber": mobile, "type": "login", "id": None},
            headers={'Content-Type': 'application/json'})
        ref = r1.json().get('referenceCode')
        assert ref, f"{login_type} send-otp returned no referenceCode: {r1.json()}"
        r2 = await self._make_authenticated_request(
            'POST', "/api/auth/verify-login",
            json_data={"referenceCode": ref, "otp": 1234, "type": "login", "isFromWeb": True},
            headers={'Content-Type': 'application/json'})
        token = r2.json().get('Token')
        assert token, f"{login_type} verify-login returned no Token: {r2.json()}"
        self._role_token_cache[mobile] = token
        self._user_details_by_token[token] = r2.json().get('userDetails') or {}
        print(f"{login_type.capitalize()} authenticated (role token, not persisted).")
        return token

    async def _admin_token(self) -> str:
        """Back-compat shim: admin token for packet create/assign. See _role_token."""
        return await self._role_token("admin")

    async def assign_packet(self, appraiser_id) -> None:
        """Assign the created packet to the appraiser (PUT /api/packet/{id}) -- ADMIN-only. Sets
        appraiserId + userType 'appraiser' so the appraiser can then seal ornaments into it. Mirrors
        the create body but with the packet's real id and the appraiser bound."""
        packet = self.available_packet if isinstance(self.available_packet, dict) else {}
        packet_id = packet.get('id')
        unique = (packet.get('packetUniqueId') or packet.get('barcodeNumber')
                  or getattr(self, '_created_packet_unique', None))
        if not packet_id:
            print("  -> WARNING: no packet id to assign; skipping packet assignment.")
            return
        api_path = f"/api/packet/{packet_id}"
        request_body = {
            "id": packet_id,
            "packetUniqueId": unique,
            "internalUserBranch": int(self.internal_branch_id) if self.internal_branch_id else 1,
            "appraiserId": int(appraiser_id) if appraiser_id else None,  # the loan's appraiser
            "auditorId": None,
            "barcodeNumber": unique,  # SAME value as packetUniqueId
            "userType": "appraiser",  # bind as appraiser (create used "")
            "isNewPacket": False,
        }
        await self._make_authenticated_request('PUT', api_path, json_data=request_body)
        print(f"Packet {packet_id} assigned to appraiser {appraiser_id}.")

    async def create_packet(self) -> dict:
        """Create a new empty packet (POST /api/packet) so one is available to seal ornaments into,
        then resolve its numeric id from the packet listing (POST returns only a message; add-packet-
        images needs the id for packetOrnamentArray.packetId). barcodeNumber and packetUniqueId are
        the SAME randomly-generated value. Sets and returns self.available_packet."""
        api_path = "/api/packet"
        unique = f"pac{self.random.randint(10000000, 99999999)}"  # e.g. pac12345678 (no dash)
        request_body = {
            "id": None,
            "packetUniqueId": unique,
            "internalUserBranch": int(self.internal_branch_id) if self.internal_branch_id else 1,
            "appraiserId": None,
            "auditorId": None,
            "barcodeNumber": unique,  # SAME value as packetUniqueId
            "userType": "",
            "isNewPacket": False,
        }
        await self._make_authenticated_request('POST', api_path, json_data=request_body)
        self._created_packet_unique = unique
        print(f"Packet created (packetUniqueId=barcodeNumber={unique}).")
        # Resolve the packet's numeric id from the listing (newest first, so it's on the first page).
        packet = await self._find_packet_by_unique_id(unique)
        if packet:
            self.available_packet = packet
            print(f"  -> resolved created packet id={packet.get('id')}")
        return self.available_packet

    async def _find_packet_by_unique_id(self, unique_id, page_size=50) -> dict:
        """Locate a packet in the paginated listing (GET /api/packet?from=1&to=N) by its unique id /
        barcode. Returns the full packet dict (with numeric id) or None."""
        api_path = f"/api/packet?from=1&to={page_size}"
        response = await self._make_authenticated_request('GET', api_path)
        data = response.json()
        packets = data.get("packetDetails") or data.get("data") or []
        for p in packets:
            if isinstance(p, dict) and unique_id in (p.get("packetUniqueId"), p.get("barcodeNumber")):
                return p
        print(f"  -> WARNING: created packet {unique_id} not found in first {page_size} listing rows.")
        return None

    async def fetch_available_packet(self) -> dict:
        api_path = f"/api/packet/available-packet?masterLoanId={self.master_loan_id}"
        response = await self._make_authenticated_request('GET', api_path)
        data = response.json()
        packets = data.get("data") if isinstance(data, dict) else data
        if isinstance(packets, list) and packets:
            # Prefer the packet we just created (match on its unique id) so add-packet-images seals
            # into the fresh one; otherwise take the first available.
            want = getattr(self, "_created_packet_unique", None)
            match = next(
                (p for p in packets if isinstance(p, dict) and want and want in (
                    p.get("packetUniqueId"), p.get("barcodeNumber"),
                    p.get("packetName"), p.get("packetsName"))),
                None)
            self.available_packet = match or packets[0]
        elif isinstance(packets, dict):
            self.available_packet = packets
        print(f"Fetched available packet: {self.available_packet.get('id') if isinstance(self.available_packet, dict) else self.available_packet}")
        return data

    async def fetch_single_loan(self) -> dict:
        api_path = f"/api/loan-process/single-loan?customerLoanId={self.loan_id}&from=undefined"
        response = await self._make_authenticated_request('GET', api_path)
        payload = response.json()
        # The human loan id (AUGM-…) is assigned during the assign-packet stage. single-loan is
        # fetched there (inside add_packet_images), so capture data.loanUniqueId now — it's needed
        # for the disbursement body and the final loan-details search.
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and data.get("loanUniqueId"):
            self.loan_unique_id = data["loanUniqueId"]
        if isinstance(data, dict) and isinstance(data.get("masterLoan"), dict) and data["masterLoan"].get("loanType"):
            self.loan_type = data["masterLoan"]["loanType"]
        print(f"Fetched single loan for customerLoanId {self.loan_id} "
              f"(loanUniqueId={self.loan_unique_id or 'n/a'}).")
        return payload

    async def prepare_packet(self) -> None:
        """Generate a FRESH packet before the loan flow starts: create it and assign it to the
        logged-in appraiser (both ADMIN-only), so every run seals its ornaments into its own new
        packet instead of depending on what happens to be available. add_packet_images then uses
        this packet rather than creating one mid-flow."""
        appraiser_id = self.logged_in_user_id
        saved_token = self.auth_token
        try:
            self.auth_token = await self._admin_token()
            await self.create_packet()             # POST /api/packet (admin) + resolve numeric id
            await self.assign_packet(appraiser_id)  # PUT /api/packet/{id} (admin) -> bind appraiser
        finally:
            self.auth_token = saved_token           # back to the appraiser session
        self.packet_prepared = bool(isinstance(self.available_packet, dict)
                                    and self.available_packet.get("id"))
        print(f"Packet ready for this run: {getattr(self, '_created_packet_unique', '?')} "
              f"(id={self.available_packet.get('id') if isinstance(self.available_packet, dict) else None}, "
              f"assigned to appraiser {appraiser_id}).")

    async def add_packet_images(self):
        # The packet is normally generated up front by prepare_packet (start of the run). Create +
        # assign it here only when that did not happen (e.g. a resumed loan). Packet CREATE + ASSIGN
        # require ADMIN rights; the sealing (add-packet-images) runs as the current appraiser.
        if not getattr(self, "packet_prepared", False):
            await self.prepare_packet()
        # Fall back to available-packet only if the created packet couldn't be resolved from the listing.
        if not (isinstance(self.available_packet, dict) and self.available_packet.get("id")):
            await self.fetch_available_packet()
        await self.fetch_single_loan()

        api_path = "/api/loan-process/add-packet-images"
        empty_packet_image = await self._upload_file_base(file_type="image")
        sealing_packet_with_weight_image = await self._upload_file_base(file_type="image")
        sealing_packet_with_customer_image = await self._upload_file_base(file_type="image")
        sealed_packet_image = await self._upload_file_base(file_type="image")

        # Build the packet-ornament mapping from the fetched packet and the stored ornaments.
        packet = self.available_packet if isinstance(self.available_packet, dict) else {}
        ornament_ids = [
            o.get("id") for o in self.loan_ornaments_details
            if isinstance(o, dict) and o.get("id")
        ]
        ornament_names = []
        for o in self.loan_ornaments_details:
            if isinstance(o, dict):
                ot = o.get("ornamentType")
                if isinstance(ot, dict):
                    ornament_names.append(ot.get("name") or ot.get("ornamentType") or "")
                elif isinstance(ot, str):
                    ornament_names.append(ot)
        packet_ornament = {
            "packetId": str(packet.get("id") or packet.get("packetId") or ""),
            "ornamentsId": ornament_ids,
            "packetsName": (packet.get("packetName") or packet.get("name") or packet.get("packetsName")
                            or packet.get("packetUniqueId") or packet.get("barcodeNumber") or ""),
            "ornamentsName": ", ".join(n for n in ornament_names if n),
        }

        request_body = {
            "emptyPacketWithNoOrnament": empty_packet_image,
            "emptyPacketWithNoOrnamentImage": f'{self.BASE_URL}/{empty_packet_image}',
            "sealingPacketWithWeight": sealing_packet_with_weight_image,
            "sealingPacketWithWeightImage": f'{self.BASE_URL}/{sealing_packet_with_weight_image}',
            "sealingPacketWithCustomer": sealing_packet_with_customer_image,
            "sealingPacketWithCustomerImage": f'{self.BASE_URL}/{sealing_packet_with_customer_image}',
            "sealedPacketWithWeight": "",
            "sealedPacketWithWeightImage": "",
            "ornamentImageWithWeight": "",
            "ornamentImageWithWeightImage": "",
            "ornamentImageWithXrfMachineReading": "",
            "ornamentImageWithXrfMachineReadingImage": "",
            "sealedPacket": sealed_packet_image,
            "sealedPacketImage": f'{self.BASE_URL}/{sealed_packet_image}',
            "packetOrnamentArray": [packet_ornament],
            "checkPointers": {},  # From collection example
            "loanType": self.loan_type or "Fresh Loan",  # Example
            "loanId": int(self.loan_id) if self.loan_id else None,
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            # HAR sends loanAmountDigi here (NOT createdBy/internalBranchId, which the server ignores/rejects).
            "loanAmountDigi": f"{float(self.final_loan_amount):.2f}" if self.final_loan_amount else "0.00",
        }
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        print('Add packet images successful.')


    # --- Additional fetch (GET) steps from the loan-flow collection ---

    async def fetch_max_loan_limit(self) -> dict:
        api_path = f"/api/loan-process/max-loan-limit/{self.customer_id}?loanType=Fresh Loan"
        try:
            response = await self._make_authenticated_request('GET', api_path)
            data = response.json()
            if isinstance(data, dict):
                self.max_loan_limit = data.get("data") or data.get("maxLoanLimit") or self.max_loan_limit
            print(f"Fetched max loan limit: {self.max_loan_limit}")
            return data
        except httpx.HTTPStatusError as e:
            print(f"Fetch max loan limit failed (non-fatal): {e.response.status_code}")
            return {}

    async def fetch_customer_loan_details(self) -> dict:
        api_path = f"/api/loan-process/customer-loan-details/{self.appraiser_request_id}"
        try:
            response = await self._make_authenticated_request('GET', api_path)
            print(f"Fetched customer loan details for appraiser request {self.appraiser_request_id}.")
            payload = response.json()
            data = payload.get('customerData') if isinstance(payload, dict) else None
            self.customer_loan_data = data if isinstance(data, dict) else {}
            return payload
        except httpx.HTTPStatusError as e:
            print(f"Fetch customer loan details failed (non-fatal): {e.response.status_code}")
            return {}

    async def fetch_rating_reasons(self) -> dict:
        api_path = "/api/rating-reason?from=1&to=-1"
        try:
            response = await self._make_authenticated_request('GET', api_path)
            print("Fetched rating reasons.")
            return response.json()
        except httpx.HTTPStatusError as e:
            print(f"Fetch rating reasons failed (non-fatal): {e.response.status_code}")
            return {}

    async def fetch_loan_balance(self) -> dict:
        api_path = f"/api/loan-process/get-balance?appraiserRequestId={self.appraiser_request_id}"
        try:
            response = await self._make_authenticated_request('GET', api_path)
            resp_json = response.json()
            self._print_json_block("get-balance response", resp_json)
            self._maybe_set_rpg(resp_json, "get-balance")
            print(f"Fetched loan balance for appraiser request {self.appraiser_request_id}.")
            return resp_json
        except httpx.HTTPStatusError as e:
            print(f"Fetch loan balance failed (non-fatal): {e.response.status_code}")
            return {}

    async def choose_co_lender_interactive(self) -> None:
        """Prompt the user to pick a co-lender bank from a menu (so they don't have to remember
        names/ids). Fetches the live list from /api/co-lender-bank, prints it numbered, reads a
        selection, and sets self.co_lender_choice to the chosen bank id (or '' for no co-lending).
        Runs after auth (inside run_e2e_test), so the session is already established."""
        try:
            response = await self._make_authenticated_request('GET', "/api/co-lender-bank")
            data = response.json()
        except httpx.HTTPStatusError as e:
            print(f"Could not fetch co-lender banks ({e.response.status_code}); continuing WITHOUT co-lending.")
            self.co_lender_choice = ""
            return
        banks = data.get("data") if isinstance(data, dict) else data
        banks = [b for b in (banks or []) if isinstance(b, dict) and b.get("id") is not None]
        if not banks:
            print("No co-lender banks available; continuing WITHOUT co-lending.")
            self.co_lender_choice = ""
            return
        print("\n" + self._c("Select a co-lender bank (co-lending):", "bold", "cyan"))
        print("   0) No co-lending")
        for i, b in enumerate(banks, 1):
            split = b.get("splitUpPercentage") or {}
            print(f"  {i:>2}) {b.get('bankName')}  "
                  f"(id {b.get('id')}, disbursement {split.get('disbursementPercent')}%, "
                  f"interest {split.get('interestRatePercent')}%)")
        try:
            raw = input("Enter choice number [0 = no co-lending]: ").strip()
        except EOFError:
            raw = ""  # non-interactive stdin -> default to no co-lending
        idx = int(raw) if raw.isdigit() else 0
        if 1 <= idx <= len(banks):
            chosen = banks[idx - 1]
            self.co_lender_choice = str(chosen.get("id"))
            print(f"  -> co-lending with '{chosen.get('bankName')}' (id {self.co_lender_choice}).")
        else:
            self.co_lender_choice = ""
            print("  -> no co-lending selected.")

    async def fetch_co_lender_banks(self) -> dict:
        api_path = "/api/co-lender-bank"
        try:
            response = await self._make_authenticated_request('GET', api_path)
            data = response.json()
            print("Fetched co-lender banks.")
        except httpx.HTTPStatusError as e:
            print(f"Fetch co-lender banks failed (non-fatal): {e.response.status_code}")
            return {}
        # If the user asked for co-lending (--co-lender NAME|ID), resolve it to a bank id now so it
        # flows into check-loan-type / final-loan-details (via self.co_lender_bank_id). Matching is by
        # exact id or case-insensitive name substring; nothing is applied when the choice is empty.
        if self.co_lender_choice:
            banks = data.get("data") if isinstance(data, dict) else data
            banks = banks if isinstance(banks, list) else []
            choice = self.co_lender_choice.strip().lower()
            match = None
            for b in banks:
                bid = str(b.get("id"))
                bname = str(b.get("bankName") or "").lower()
                if choice == bid or (choice and choice in bname):
                    match = b
                    break
            if match:
                self._forced_co_lender_id = str(match.get("id"))
                self.co_lender_bank_id = self._forced_co_lender_id
                split = match.get("splitUpPercentage") or {}
                print(f"Co-lending ON: bank '{match.get('bankName')}' (id {self._forced_co_lender_id}, "
                      f"disbursement {split.get('disbursementPercent')}%).")
            else:
                available = [f"{b.get('id')}:{b.get('bankName')}" for b in banks]
                print(f"WARNING: co-lender '{self.co_lender_choice}' not found; proceeding WITHOUT "
                      f"co-lending. Available: {available}")
        return data

    async def fetch_bank_details(self) -> dict:
        api_path = f"/api/loan-process/bank-details?masterLoanId={self.master_loan_id}"
        try:
            response = await self._make_authenticated_request('GET', api_path)
        except httpx.HTTPStatusError as e:
            print(f"Fetch bank details failed (non-fatal): {e.response.status_code}")
            return {}
        data = response.json()
        body = data.get("data") if isinstance(data, dict) else None
        details = body.get("bankDetails") if isinstance(body, dict) else None
        saved = [self._normalize_saved_bank_account(d) for d in details or []
                 if isinstance(d, dict) and d.get("accountNumber") and d.get("ifscCode")]
        # Verified accounts first -- they are the likeliest to clear the penny-drop.
        saved.sort(key=lambda a: not a["verified"])
        # One entry per account number (a re-used account can appear once per loan).
        unique = {}
        for account in saved:
            unique.setdefault(account["accountNumber"], account)
        self.saved_bank_accounts = list(unique.values())
        if self.saved_bank_accounts:
            listed = ", ".join(f"{a['label']} ({a['ifscCode']}/{a['accountNumber']}"
                               f"{', verified' if a['verified'] else ''})"
                               for a in self.saved_bank_accounts)
            print(self._c(f"Customer already has {len(self.saved_bank_accounts)} saved bank "
                          f"account(s); using those: {listed}.", "green"))
        else:
            print(f"Fetched bank details for master loan {self.master_loan_id}: no saved accounts; "
                  "a new account will be added from the reference list.")
        return data

    @staticmethod
    def _normalize_saved_bank_account(detail: dict) -> dict:
        """A saved bank-details row, in the shape _apply_bank_account expects."""
        proofs = detail.get("passbookProof") or []
        if isinstance(proofs, str):
            proofs = [proofs]
        status = str(detail.get("bankVerificationStatus") or "").lower()
        return {
            "label": f"saved {detail.get('bankName') or 'account'}",
            "accountNumber": str(detail.get("accountNumber")),
            "ifscCode": str(detail.get("ifscCode")),
            "bankName": detail.get("bankName") or "",
            "bankBranchName": detail.get("bankBranchName") or detail.get("branchName") or "",
            "accountHolderName": detail.get("accountHolderName") or "",
            "passbookProof": [p for p in proofs if p],
            "verified": bool(detail.get("isVerified") or detail.get("isManuallyVerified")
                             or status in ("verified", "approved")),
            "saved": True,
        }

    async def fetch_loan_stages(self) -> dict:
        api_path = "/api/loan-process/get-loan-stages"
        try:
            response = await self._make_authenticated_request('GET', api_path)
            data = response.json()
            stages = data.get("data") if isinstance(data, dict) else data
            if isinstance(stages, list) and stages:
                # Store the last stage id for the loan-lock update if not already known.
                last = stages[-1]
                if isinstance(last, dict) and last.get("id"):
                    self.loan_stage_id = str(last.get("id"))
            print(f"Fetched loan stages (loan_stage_id={self.loan_stage_id}).")
            return data
        except httpx.HTTPStatusError as e:
            print(f"Fetch loan stages failed (non-fatal): {e.response.status_code}")
            return {}

    async def fetch_applied_loan_details(self) -> dict:
        api_path = "/api/loan-process/applied-loan-details?from=1&to=25&isRejectedLoan=true"
        try:
            response = await self._make_authenticated_request('GET', api_path)
            print("Fetched applied loan details.")
            return response.json()
        except httpx.HTTPStatusError as e:
            print(f"Fetch applied loan details failed (non-fatal): {e.response.status_code}")
            return {}

    async def fetch_lead_converter(self) -> dict:
        api_path = "/api/lead/lead-converter?isForLeadConverter=true&isForProductivityReport=false"
        try:
            response = await self._make_authenticated_request('GET', api_path)
            data = response.json()
            converters = data.get("data") if isinstance(data, dict) else data
            if isinstance(converters, list) and converters:
                first = converters[0]
                if isinstance(first, dict):
                    self.lead_converter_id = str(first.get("id") or first.get("userId") or "")
            if not self.lead_converter_id and self.logged_in_user_id:
                self.lead_converter_id = str(self.logged_in_user_id)
            print(f"Fetched lead converter id: {self.lead_converter_id}")
            return data
        except httpx.HTTPStatusError as e:
            print(f"Fetch lead converter failed (non-fatal): {e.response.status_code}")
            if self.logged_in_user_id:
                self.lead_converter_id = str(self.logged_in_user_id)
            return {}

    async def fetch_account_details_karza(self) -> dict:
        ifsc = self.bank_ifsc_code or "SBIN0011777"
        api_path = f"/api/loan-process/account-details-karza?ifscCode={urllib.parse.quote(ifsc)}"
        try:
            response = await self._make_authenticated_request('GET', api_path)
            data = response.json()
            details = data.get("data") if isinstance(data, dict) else data
            if isinstance(details, dict):
                self.karza_account_details = details
                # The portal reads res.data.bank / res.data.branch from this lookup.
                self.bank_name = details.get("bank") or details.get("bankName") or self.bank_name
                self.bank_branch_name = details.get("branch") or details.get("bankBranchName") or self.bank_branch_name
            print(f"Fetched Karza account details for IFSC {ifsc}.")
            return data
        except httpx.HTTPStatusError as e:
            body = self._short_error_body(e.response.text, 120)
            print(f"Fetch Karza account details failed (non-fatal): {e.response.status_code} - {body}")
            # "Invalid IFSC Code" condemns the ACCOUNT, not just this lookup -- the penny-drop
            # that would follow answers "The ifsc code is not found". Say so, so the caller can
            # skip it and save that second call.
            return {"_error": {"status": e.response.status_code, "body": body,
                               "badIfsc": "ifsc" in body.lower()}}


    # --- Disbursement / documents phase (from loan-flow collection) ---

    async def _loan_record(self) -> dict:
        """single-loan `data`: the record every portal loan screen is built from (the parent
        LoanApplicationFormComponent loads it and passes it down)."""
        return ((await self.fetch_single_loan()) or {}).get('data') or {}

    @staticmethod
    def _check_pointers(loan: dict) -> dict:
        """Loan defects the portal sends back with every rating / packet / lock call."""
        logs = ((loan.get('masterLoan') or {}).get('loanIncompleteReasonsLogs')) or {}
        return (logs.get('checkPointers') if isinstance(logs, dict) else None) or {}

    # Rating fields an approver does NOT choose: the portal patches them from masterLoan, so every
    # later rating echoes what the earlier roles actually decided (e.g. BM 'pending' when the BM
    # stage was skipped) instead of a blanket 'approved'.
    RATING_COPIED_FIELDS = (
        "applicationFormForAppraiser", "goldValuationForAppraiser", "loanStatusForAppraiser",
        "commentByAppraiser", "applicationFormForBM", "goldValuationForBM", "loanStatusForBM",
        "applicationFormForPartner", "goldValuationForPartner", "loanStatusForPartner",
        "scrapStatusForAppraiser", "scrapStatusForBM", "scrapStatusForOperatinalTeam",
        "packetApprovalByOps", "commentByOpsForPacketApproval", "statusForRh", "commentByRh",
        "applicationFormForRh", "goldValuationForRh",
        "applicationFormForOperatinalTeam", "goldValuationForOperatinalTeam", "loanStatusForOperatinalTeam",
    )

    def _rating_base(self, loan: dict, defaults: dict) -> dict:
        master = loan.get('masterLoan') or {}
        body = {k: master.get(k, defaults.get(k)) for k in self.RATING_COPIED_FIELDS if k in defaults}
        comment = body.get("commentByAppraiser")
        body["reasons"] = "Other" if comment else ""   # portal: '' with no comment, else the matching reason / 'Other'
        body["cKycNumber"] = (loan.get('customer') or {}).get('cKycNumber')
        body["loanId"] = loan.get('id') or (int(self.loan_id) if self.loan_id else None)
        body["masterLoanId"] = loan.get('masterLoanId') or (int(self.master_loan_id) if self.master_loan_id else None)
        body["loanAmountDigi"] = loan.get('loanAmount') or (
            f"{float(self.final_loan_amount):.2f}" if self.final_loan_amount else "0.00")
        body["loanType"] = master.get('loanType') or self.loan_type or "Fresh Loan"
        body["checkPointers"] = self._check_pointers(loan)
        if body["checkPointers"]:
            # The portal refuses to APPROVE while the loan carries open defects (checkPointers;
            # BM may ignore the UPLOAD_DOCUMENT one). Surface them so a server rejection is explained.
            print(self._c(f"WARNING: the loan has open defects (checkPointers: "
                          f"{', '.join(map(str, body['checkPointers']))}); the portal would block approval.",
                          "yellow"))
        return body

    async def update_loan_lock(self):
        api_path = "/api/loan-process/update-loan-lock"
        # The portal locks the loan at its CURRENT stage (loanStage.id of the loan record), not the
        # last stage of the master list.
        loan = await self._loan_record()
        master = loan.get('masterLoan') or {}
        current = ((master.get('loanStage') or {}).get('id') or master.get('loanStageId')
                   or loan.get('loanStageId') or self.loan_stage_id)
        loan_stage_id = int(current) if current and str(current).isdigit() else 8
        request_body = {
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            "loanStageId": loan_stage_id,
        }
        try:
            await self._make_authenticated_request('POST', api_path, json_data=request_body)
            print(f'Update loan lock successful (loanStageId={loan_stage_id}).')
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                print(f"Update loan lock skipped (already locked/invalid stage): {e.response.text}")
            else:
                raise

    # --- persistent bank-account status ---------------------------------------------------
    #
    # The penny-drop hits a REAL bank, so a rejection is worth remembering ACROSS runs: the
    # eight "Something went wrong" calls, the closed beneficiary account and the two bad IFSC
    # codes cost two API calls each (the karza IFSC lookup plus validate-account) and answer the
    # same way every time. The file records them per ENVIRONMENT (test and uat sit in front of
    # different backends) together with the account that last worked.
    #
    # This is NOT the session persistence that was deliberately removed: no tokens, no customer,
    # no PAN -- nothing that can go stale into a "already exists" failure. It is a list of dead
    # bank accounts, which stay dead.
    BANK_STATUS_FILENAME = os.path.join("config", "bank_account_status.json")

    # A rejection naming the ACCOUNT or its IFSC will answer the same way forever. Anything else
    # ("Something went wrong", a 5xx) may just be the provider having a bad day, so it is
    # remembered but can be forgiven -- see _load_bank_status.
    DEFINITIVE_BANK_REJECTIONS = (
        "account is closed", "account closed", "invalid account",
        "account does not exist", "no such account", "invalid beneficiary",
        "ifsc code is not found", "invalid ifsc",
    )

    @classmethod
    def _is_definitive_bank_rejection(cls, text: str) -> bool:
        low = (text or "").lower()
        return any(marker in low for marker in cls.DEFINITIVE_BANK_REJECTIONS)

    def _bank_status_path(self) -> str:
        override = os.getenv("GOLD_LOAN_BANK_STATUS_FILE", "").strip()
        if override:
            return override
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(project_root, self.BANK_STATUS_FILENAME)

    def _read_bank_status(self) -> dict:
        try:
            with open(self._bank_status_path(), encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _load_bank_status(self) -> None:
        """Seed this run's rejections (and preferred account) from the status file.

        `GOLD_LOAN_BANK_RETRY_ALL=true` ignores the file entirely. Otherwise every recorded
        rejection is honoured -- UNLESS honouring them would leave nothing to try, in which case
        the non-definitive ones are forgiven for this run, so a provider outage recorded once can
        never permanently disable the flow.
        """
        if self.bank_retry_all_accounts:
            print("GOLD_LOAN_BANK_RETRY_ALL is set: ignoring the recorded bank-account rejections.")
            return
        env_block = (self._read_bank_status().get("environments") or {}).get(self.env_name) or {}
        rejected = env_block.get("rejected") or {}
        if not isinstance(rejected, dict):
            return

        definitive = {n for n, info in rejected.items()
                      if isinstance(info, dict) and info.get("definitive")}
        soft = set(rejected) - definitive
        total = len(self._load_bank_accounts())
        if len(definitive) + len(soft) >= total and soft:
            print(f"Every known account is flagged on '{self.env_name}'; forgiving "
                  f"{len(soft)} non-definitive rejection(s) so there is something to try.")
            self._bank_accounts_rejected |= set(definitive)
        else:
            self._bank_accounts_rejected |= set(rejected)
        # UNION, never assignment: anything already flagged in memory (an earlier loan of a
        # --count batch, or a caller that seeded it) must not be forgotten by the file load.
        self._bank_rejection_reasons.update(
            {n: (info or {}).get("reason", "") for n, info in rejected.items()
             if isinstance(info, dict)})

        preferred = env_block.get("preferred")
        if preferred and preferred not in self._bank_accounts_rejected:
            match = next((a for a in self._load_bank_accounts()
                          if str(a.get("accountNumber")) == str(preferred)), None)
            if match:
                self._apply_bank_account(match)
                print(f"Starting from the account that last worked on '{self.env_name}': "
                      f"{self.bank_account_label}.")
        if self._bank_accounts_rejected:
            print(f"Skipping {len(self._bank_accounts_rejected)} bank account(s) recorded as "
                  f"failing on '{self.env_name}' (GOLD_LOAN_BANK_RETRY_ALL=true to try them again).")

    def _write_bank_status(self, mutate) -> None:
        """Read-modify-write this environment's block. Never fatal -- it is a cache, not state."""
        path = self._bank_status_path()
        payload = self._read_bank_status()
        payload.setdefault("version", 1)
        environments = payload.setdefault("environments", {})
        env_block = environments.setdefault(self.env_name, {})
        env_block.setdefault("rejected", {})
        mutate(env_block)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
        except OSError as e:
            print(f"Could not update the bank-account status file at {path}: {e}")

    def _flag_bank_account(self, number: str, reason: str, status, definitive: bool) -> None:
        """Remember that this account failed, for this run AND the next one."""
        number = str(number)
        self._bank_accounts_rejected.add(number)
        self._bank_rejection_reasons[number] = reason
        if self._is_saved_bank_account(number):
            return  # the customer's own account: an in-run flag only, not the shared file
        def mutate(env_block):
            env_block["rejected"][number] = {
                "reason": reason[:200],
                "status": status,
                "definitive": bool(definitive),
                "label": self.bank_account_label,
                "lastSeen": datetime.datetime.now(datetime.timezone.utc)
                    .isoformat().replace("+00:00", "Z"),
            }
            if env_block.get("preferred") == number:
                env_block.pop("preferred", None)
        self._write_bank_status(mutate)

    def _remember_working_bank_account(self, number: str) -> None:
        """Record the account that worked so the next run starts with it (one call, not twelve)."""
        number = str(number)
        self._bank_accounts_rejected.discard(number)
        if self._is_saved_bank_account(number):
            return
        def mutate(env_block):
            env_block["preferred"] = number
            env_block["rejected"].pop(number, None)
        self._write_bank_status(mutate)

    def _is_saved_bank_account(self, number: str) -> bool:
        return any(a["accountNumber"] == str(number) for a in self.saved_bank_accounts)

    def _load_bank_accounts(self) -> list:
        """The candidate customer payout accounts for the penny-drop, in the order to try them.

        Source: `reference/bank_accounts.json` (copied from the augmont-test-suite fixture
        `fixtures/new-admin/bankAccounts.json` -- the LIC collection accounts from
        LIST-OF-ALL-BANK-DETAILS-UPDATED26112019.pdf). Override the path with
        GOLD_LOAN_BANK_ACCOUNTS. Falls back to the single hard-coded SBI account, which is what
        the harness used before the list existed.
        """
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.getenv("GOLD_LOAN_BANK_ACCOUNTS") or os.path.join(project_root, self.BANK_ACCOUNTS_PATH)
        if not os.path.isabs(path):
            path = os.path.join(project_root, path)
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as e:
            print(f"Could not read the bank-account list at {path} ({e}); "
                  "falling back to the built-in account.")
            return [dict(self.DEFAULT_BANK_ACCOUNT)]
        accounts = payload.get("accounts") if isinstance(payload, dict) else payload
        usable = [a for a in accounts or []
                  if isinstance(a, dict) and a.get("accountNumber") and a.get("ifscCode")]
        if not usable:
            print(f"The bank-account list at {path} has no usable entries; "
                  "falling back to the built-in account.")
            return [dict(self.DEFAULT_BANK_ACCOUNT)]
        return usable

    def _bank_account_candidates(self) -> list:
        """The accounts still worth penny-dropping, best first.

        Two savings, both of which matter across a --count batch and across the disbursement
        stage's second validate-account:
          * the account that already WORKED is tried first, so a re-run settles in one call;
          * accounts the penny-drop already rejected THIS RUN are skipped entirely -- they are
            flagged in `_bank_accounts_rejected` and cost two calls each (the IFSC lookup plus
            the penny-drop itself) every time they are retried.
        """
        if self.saved_bank_accounts:
            # The customer's own accounts, and nothing else: a new account is only added for a
            # customer who has none. The recorded LIC-account rejections do not apply to these.
            keep = [a for a in self.saved_bank_accounts
                    if a["accountNumber"] not in self._bank_accounts_rejected]
            return keep or list(self.saved_bank_accounts[:1])
        if not self._bank_status_loaded:
            self._bank_status_loaded = True
            self._load_bank_status()
        accounts = self._load_bank_accounts()
        chosen = str(self.bank_account_number or "")
        rejected = self._bank_accounts_rejected

        keep, skipped = [], 0
        for account in accounts:
            number = str(account.get("accountNumber"))
            if number in rejected and number != chosen:
                skipped += 1
                continue
            keep.append(account)
        if skipped:
            print(f"Skipping {skipped} account(s) the penny-drop already rejected in this run "
                  "(set GOLD_LOAN_BANK_RETRY_ALL=true to try them again).")
        accounts = keep

        if chosen:
            accounts = ([a for a in accounts if str(a.get("accountNumber")) == chosen]
                        + [a for a in accounts if str(a.get("accountNumber")) != chosen])
        limit = self.bank_account_attempts
        if limit and len(accounts) > limit:
            print(f"Penny-dropping {limit} of {len(accounts)} candidate accounts "
                  f"(the remaining {len(accounts) - limit} are not attempted; raise "
                  "GOLD_LOAN_BANK_ACCOUNT_ATTEMPTS to try more).")
            accounts = accounts[:limit]
        return accounts

    def _apply_bank_account(self, account: dict) -> None:
        """Point the run's bank fields at `account` (bank name/branch come from the IFSC lookup)."""
        self.bank_account_number = str(account.get("accountNumber"))
        self.bank_ifsc_code = str(account.get("ifscCode"))
        self.bank_account_label = account.get("label") or self.bank_ifsc_code
        if account.get("saved"):
            self.account_holder_name = account.get("accountHolderName") or self.account_holder_name
            self.bank_name = account.get("bankName") or self.bank_name
            self.bank_branch_name = account.get("bankBranchName") or self.bank_branch_name
            if account.get("passbookProof"):
                self.passbook_proofs = list(account["passbookProof"])
                self._passbook_from_saved = True
        self.account_holder_name = self.account_holder_name or "LIC MUTUAL FUND"

    async def validate_account(self):
        """Penny-drop the candidate payout accounts and keep the first usable one.

        The penny-drop reaches the REAL bank and rejects accounts unpredictably -- on UAT the
        single hard-coded SBI account answers 400 "Something went wrong", which then blocks
        bank-details with "bank details is not verified" -- so walk the list from
        reference/bank_accounts.json instead of relying on one account.

        Ranking, best first:
          1. verified outright (isVerified / isManuallyVerified) -- no ops approval needed;
          2. bankTxnStatus true -- the transaction reached the bank; the account is USABLE and
             ops_manual_bank_verification clears the pending state (this is the normal outcome:
             the bank's account name never matches our random customer name);
          3. the call merely succeeded (HTTP 200 with no bankTxnStatus) -- kept only as a
             last resort if nothing better turns up.
        The walk stops as soon as it reaches rank 1 or 2. If every account fails, the fields are
        left on the first candidate and the flags stay false, so store_bank_details' ops manual
        verification fallback still gets its chance.
        """
        api_path = "/api/loan-process/validate-account"
        # Passbook proof is a cheque IMAGE (PNG), matching the real call; penny-drop verifies by
        # account/IFSC, not the image. Upload it ONCE, not per candidate, and only when needed:
        # a saved account brings its own proof.
        uploaded = None

        async def passbook_for(account):
            nonlocal uploaded
            if account.get("saved") and account.get("passbookProof"):
                return list(account["passbookProof"])
            if self.passbook_proofs and not self._passbook_from_saved:
                return self.passbook_proofs
            if uploaded is None:
                uploaded = [(await self._upload_file(
                    "loan", file_type="image",
                    file_path_override=self.CHEQUE_IMAGE_PATH))['uploadFile']['path']]
            return uploaded

        if self._bank_penny_drop_unavailable and not self.bank_retry_all_accounts:
            # A full walk already rejected every account it tried. Re-walking costs two calls per
            # account per loan and has never once changed the answer, so go straight to the state
            # store_bank_details' ops-manual-verification fallback expects.
            print(self._c("Penny-drop already rejected every account tried in this run; skipping "
                          "it and relying on the ops manual verification "
                          "(GOLD_LOAN_BANK_RETRY_ALL=true to try again).", "yellow"))
            self.bank_account_verified = False
            self.bank_system_verified = False
            self.bank_manually_verified = False
            self.bank_for_ops_approval = True
            return

        candidates = self._bank_account_candidates()
        if not candidates:
            print(self._c("Every candidate account is flagged as rejected; skipping the penny-drop "
                          "and relying on the ops manual verification.", "yellow"))
            self._bank_penny_drop_unavailable = True
            self.bank_account_verified = False
            self.bank_for_ops_approval = True
            return

        attempts = []          # human-readable per-account outcome, for the summary
        fallback = None        # (account, payload) for a rank-3 result

        for position, account in enumerate(candidates, 1):
            self._apply_bank_account(account)
            label = f"{self.bank_account_label} ({self.bank_ifsc_code}/{self.bank_account_number})"
            karza = await self.fetch_account_details_karza()  # bank name + branch for this IFSC
            karza_error = karza.get("_error") if isinstance(karza, dict) else None
            if karza_error and karza_error.get("badIfsc"):
                reason = karza_error.get("body") or "Invalid IFSC Code"
                attempts.append(f"{self.bank_account_label} -> IFSC lookup: {reason}")
                print(f"  {reason}; skipping the penny-drop for {label}.")
                self._flag_bank_account(self.bank_account_number, reason,
                                        karza_error.get("status"), definitive=True)
                continue
            passbook = await passbook_for(account)
            request_body = {
                "ifscCode": self.bank_ifsc_code,
                "accountNumber": self.bank_account_number,
                "accountHolderName": self.account_holder_name or self.full_name,
                "bankName": self.bank_name or "STATE BANK OF INDIA",
                "bankBranchName": self.bank_branch_name or "SBI CAPITAL MARKET BRANCH, MUMBAI",
                "passbookProof": passbook,
                "passbookProofImage": [p if p.startswith("http") else f"{self.BASE_URL}/{p}"
                                       for p in passbook],
                "passbookProofImageName": [],
                "detailsFor": "customer",
                "detailsForId": int(self.customer_id) if self.customer_id else None,
            }
            print(f"Penny-drop {position}/{len(candidates)}: {label}")
            try:
                response = await self._make_authenticated_request('POST', api_path, json_data=request_body)
            except httpx.HTTPStatusError as e:
                detail = self._short_error_body(e.response.text, 100).strip()
                attempts.append(f"{self.bank_account_label} -> {e.response.status_code} {detail}")
                # Flag it so neither the disbursement re-run, nor a later loan in a --count
                # batch, nor the NEXT RUN spends two calls on it again.
                self._flag_bank_account(self.bank_account_number, detail, e.response.status_code,
                                        definitive=self._is_definitive_bank_rejection(detail))
                print(f"  rejected: {e.response.status_code} - {detail}")
                continue

            data = response.json()
            self._capture_validate_account_flags(data)
            if self.bank_system_verified or self.bank_manually_verified:
                print(self._c(f"  VERIFIED outright; using {label}.", "green"))
                self._remember_working_bank_account(self.bank_account_number)
                self._report_validate_account(data, request_body)
                return
            if self.bank_account_verified:
                print(f"  usable (bankTxnStatus true); using {label}.")
                self._remember_working_bank_account(self.bank_account_number)
                self._report_validate_account(data, request_body)
                return
            attempts.append(f"{self.bank_account_label} -> 200 but no bankTxnStatus")
            print("  reached the server but the bank transaction did not go through.")
            if fallback is None:
                fallback = (account, data)

        if fallback is not None:
            account, data = fallback
            self._apply_bank_account(account)
            await self.fetch_account_details_karza()
            self._capture_validate_account_flags(data)
            print(self._c(f"No account verified; falling back to {self.bank_account_label} "
                          "(the server accepted the call). The ops manual verification will have "
                          "to clear it.", "yellow"))
            self._report_validate_account(data, {"accountHolderName": self.account_holder_name})
            return

        # Nothing worked at all: leave the fields on the first candidate so the rest of the flow
        # has a consistent account, and let store_bank_details' ops fallback take over. For a
        # customer with saved accounts that is still their own account -- no new one is added.
        self._apply_bank_account(candidates[0])
        self._bank_penny_drop_unavailable = True  # do not walk the list again this run
        self.bank_account_verified = False
        self.bank_system_verified = False
        self.bank_manually_verified = False
        self.bank_for_ops_approval = True
        print(self._c("Validate account FAILED for every candidate account (" +
                      "; ".join(attempts) + "). Continuing with "
                      f"{self.bank_account_label}; the ops manual verification will have to "
                      "clear it.", "red"))

    def _capture_validate_account_flags(self, data) -> None:
        """Record the penny-drop verdict (and the names the portal copies into bank-details:
        accountHolderName = data.accountName, customerName = customerName).

        bankTxnStatus True does NOT mean the account is VERIFIED. The server also compares the
        bank's account name against the customer name; the LIC collection accounts are never a
        random test customer, so a good run answers
          bankTxnStatus: true, isVerified: false, isManuallyVerified: false, forOpsApproval: true
        i.e. the bank detail is parked as "bankVerificationStatus: pending" awaiting an OPS
        manual approval (see ops_manual_bank_verification). Reading only bankTxnStatus here used
        to make the run believe the account was verified and fail later in the flow.

        NOTE: the response's top-level "message" can read "Something went wrong" even on success
        -- rely on these flags, not the message.
        """
        if isinstance(data, dict):
            inner = data.get('data') if isinstance(data.get('data'), dict) else {}
            if inner.get('accountName'):
                self.account_holder_name = inner['accountName']
            if data.get('customerName'):
                self.validated_customer_name = data['customerName']
        self.bank_account_verified = bool(data.get('data', {}).get('bankTxnStatus')
                                          if isinstance(data.get('data'), dict)
                                          else self._find_key_recursive(data, 'bankTxnStatus'))
        self.bank_system_verified = bool(data.get('isVerified'))
        self.bank_manually_verified = bool(data.get('isManuallyVerified'))
        self.bank_for_ops_approval = bool(data.get('forOpsApproval'))

    def _report_validate_account(self, data, request_body) -> None:
        print(f"Validate account successful (bankTxnStatus={self.bank_account_verified}, "
              f"isVerified={self.bank_system_verified}, "
              f"isManuallyVerified={self.bank_manually_verified}, "
              f"forOpsApproval={self.bank_for_ops_approval}).")
        if self.bank_for_ops_approval or not (self.bank_system_verified or self.bank_manually_verified):
            name_as_per_system = data.get('nameAsPerSystem')
            print(self._c("  Bank account is NOT verified yet (nameAsPerSystem="
                          f"{name_as_per_system!r} vs accountHolderName="
                          f"{request_body.get('accountHolderName')!r}); it needs the OPS manual "
                          "verification step.", "yellow"))

    async def upload_income_document(self) -> dict:
        """Upload one income-generating document (a PDF -- the only type the upload page accepts)
        with the reason the web app uses. Returns the server's uploadFile record."""
        api_path = f"/api/upload-file?reason=customerIncomeGeneratingDocument&customerId={self.customer_id}"
        headers = {}
        content_type = mimetypes.guess_type(self.DUMMY_PDF_PATH)[0] or "application/octet-stream"
        with open(self.DUMMY_PDF_PATH, "rb") as upload_file:
            files = {"avatar": (os.path.basename(self.DUMMY_PDF_PATH), upload_file, content_type)}
            response = await self._make_authenticated_request('POST', api_path, files=files, headers=headers)
        upload = response.json()['uploadFile']
        print(f"Income-generating document uploaded. Path: {upload['path']}")
        return upload

    async def fetch_global_setting(self) -> dict:
        """GET /api/global-setting: the rules the web app reads at runtime (IGLLIPApplicability,
        consumptionLipApplicability, LIPEligibilityValidation, ...). Cached for the run."""
        if not self.global_setting:
            response = await self._make_authenticated_request('GET', "/api/global-setting")
            data = response.json()
            self.global_setting = data if isinstance(data, dict) else {}
        return self.global_setting

    # Occupation category (customerKycPersonal.occupation.category) -> the LIP income field the web
    # app makes MANDATORY for it (LipFormComponent.applyIncomeFieldState). 'Others' makes salary /
    # business / agricultural optional; any other category leaves only rental + other income.
    LIP_CATEGORY_INCOME_FIELD = {
        "Salary": "salary",
        "Self Employed": "primaryBusinessIncome",
        "Farmer / Agricultural Labourer": "agriculturalIncome",
        "Others": "salary",
    }
    LIP_INCOME_FIELDS = ("salary", "primaryBusinessIncome", "agriculturalIncome",
                         "rentalAssetIncome", "otherBusinessIncome")

    def _build_lip_form(self, loan: dict) -> dict:
        """The Liquid Income Program (LIP) assessment, as the web app's LipFormComponent submits it
        (lipForm.getRawValue(), sent as `lipForm` in loan-documents). Every input comes from the
        single-loan record, the login JWT and global settings -- nothing is invented except the
        self-declared income/expense figures, which are sized so the customer passes the eligibility
        rule: net monthly income > monthly interest obligation
        (currentLoanPresentInterest + presentInterest)."""
        customer = loan.get('customer') or {}
        kyc = customer.get('customerKycPersonal') or {}
        occupation = kyc.get('occupation') or {}
        category = occupation.get('category') or 'Others'
        personal = loan.get('loanPersonalDetail') or {}
        master = loan.get('masterLoan') or {}

        def num(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        obligation = round(num(loan.get('currentLoanPresentInterest')) + num(loan.get('presentInterest')), 2)
        household = 10000
        business = 5000 if category == "Self Employed" else ''   # mandatory only for Self Employed
        income = int(obligation + household + num(business)) + 25000

        # Income fields the category does not enable are reset to '' by the form; enabled ones
        # that are left empty stay null.
        enabled = {"rentalAssetIncome", "otherBusinessIncome"}
        if category == "Others":
            enabled |= {"salary", "primaryBusinessIncome", "agriculturalIncome"}
        elif category in self.LIP_CATEGORY_INCOME_FIELD:
            enabled.add(self.LIP_CATEGORY_INCOME_FIELD[category])
        incomes = {f: (None if f in enabled else '') for f in self.LIP_INCOME_FIELDS}
        incomes[self.LIP_CATEGORY_INCOME_FIELD.get(category, "otherBusinessIncome")] = income

        claims = jwt.decode(self.auth_token, options={"verify_signature": False}) if self.auth_token else {}
        role_name = claims.get('roleName')
        checker = {"checkerName": '', "checkerEmployeeId": '', "checkerDesignation": ''}
        if master.get('commentByBM') != "auto approved" and isinstance(master.get('bm'), dict):
            bm = master['bm']
            roles = bm.get('roles') or [{}]
            checker = {"checkerName": f"{bm.get('firstName')} {bm.get('lastName')}",
                       "checkerEmployeeId": bm.get('userUniqueId'),
                       "checkerDesignation": roles[0].get('roleName') if isinstance(roles[0], dict) else ''}

        form = {
            # Section 1 - Borrower Profile
            "loanAccountNumber": loan.get('loanUniqueId'),
            "customerName": f"{customer.get('firstName')} {customer.get('lastName')}",
            "occupationType": category,
            "occupationTypeOther": (occupation.get('name') or "Other") if category == "Others" else '',
            "yearsInCurrentOccupation": str(self.random.randint(2, 15)),
            "endUseOfLoan": personal.get('purpose'),
            "assessmentDate": datetime.datetime.now(datetime.timezone.utc)
                .isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            # Section 2 - Monthly Income Sources
            **incomes,
            # Section 3 - Monthly Expenses / Obligations
            "householdLivingExpenses": str(household),
            "businessOperatingExpenses": str(business) if business != '' else '',
            "otherEmis": '',
            "informalChitFundOutflow": '',
            "remarks": "Self-Declared",
            # Section 4 - Loan Eligibility
            "newLoanRequested": loan.get('loanAmount'),
            "presentInterest": f"{num(loan.get('presentInterest')):.2f}",
            "totalExistingOutstanding": customer.get('tillDateOutstanding'),
            "isOtherIncome": False,
            # Section 5 - Officer Sign-off (maker = the logged-in user; checker = the loan's BM)
            "assessedByName": f"{claims.get('firstName')} {claims.get('lastName')}",
            "assessedByEmployeeId": str(claims.get('userUniqueId') or ''),
            "assessedByDesignation": role_name[0] if isinstance(role_name, list) and role_name else (role_name or ''),
            **checker,
        }
        # A loan that already has a saved assessment reopens with it: the web app overlays the
        # saved lipFormInfo on the prefilled values. Only real form fields survive getRawValue(), so
        # the server-computed totals (grossMonthlyIncome, isEligible, pdfUrl, existingLoanEmis, ...)
        # are not sent back.
        saved = ((loan.get('customerLoanDocument') or {}).get('lipFormInfo')) or {}
        reused = [k for k in form if k in saved and saved[k] is not None]
        for key in reused:
            form[key] = saved[key]
        if reused:
            print(f"LIP assessment: reusing the saved form values ({len(reused)} fields).")

        # The web form's save() refuses an ineligible customer when LIPEligibilityValidation is on
        # (net monthly income must exceed the monthly interest obligation). If saved values fail it,
        # re-declare the income the way a user would before saving.
        net = self._lip_net_income(form)
        if self.global_setting.get('LIPEligibilityValidation') and not net > obligation:
            field = self.LIP_CATEGORY_INCOME_FIELD.get(category, "otherBusinessIncome")
            form[field] = int(num(form.get(field)) + (obligation - net)) + 25000
            print(f"LIP assessment: saved values were not eligible (net {net:.2f} <= obligation "
                  f"{obligation:.2f}); re-declared {field}={form[field]}.")
            net = self._lip_net_income(form)
        print(f"LIP assessment: category={category!r}, net monthly income {net:.2f} vs monthly "
              f"interest obligation {obligation:.2f} (existing loan EMIs {form['presentInterest']})")
        return form

    @staticmethod
    def _lip_net_income(form: dict) -> float:
        """Net monthly income exactly as LipFormComponent.calculate() derives it."""
        def num(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0
        gross = sum(num(form.get(f)) for f in ("salary", "primaryBusinessIncome", "agriculturalIncome",
                                               "rentalAssetIncome", "otherBusinessIncome"))
        expenses = sum(num(form.get(f)) for f in ("householdLivingExpenses", "businessOperatingExpenses",
                                                  "otherEmis", "informalChitFundOutflow"))
        return gross - expenses

    async def verify_lip_assessment(self) -> None:
        """Read back what the server stored for the LIP assessment (customerLoanDocument.lipFormInfo)
        and check it: the server recomputes the totals itself, so a mismatch shows up here rather
        than as a confusing failure at a later stage."""
        loan = ((await self.fetch_single_loan()) or {}).get('data') or {}
        saved = ((loan.get('customerLoanDocument') or {}).get('lipFormInfo')) or {}
        if not saved:
            raise RuntimeError("loan-documents was accepted but the server stored no LIP assessment "
                               "(customerLoanDocument.lipFormInfo is empty).")
        problems = []
        if saved.get('isEligible') is not True:
            problems.append(f"server isEligible={saved.get('isEligible')!r}")
        sent_emis = (self.lip_form or {}).get('presentInterest')
        try:
            if sent_emis is not None and abs(float(saved.get('existingLoanEmis')) - float(sent_emis)) > 0.01:
                problems.append(f"existingLoanEmis {saved.get('existingLoanEmis')} != sent presentInterest {sent_emis}")
        except (TypeError, ValueError):
            problems.append(f"existingLoanEmis unreadable: {saved.get('existingLoanEmis')!r}")
        self._print_summary_table("LIP ASSESSMENT (as stored by the server)", {
            "Existing loan EMIs": saved.get('existingLoanEmis'),
            "Gross monthly income": saved.get('grossMonthlyIncome'),
            "Total monthly expenses": saved.get('totalMonthlyExpenses'),
            "Net monthly income": saved.get('netMonthlyIncome'),
            "Monthly interest obligation": saved.get('totalMonthlyInterestObligation'),
            "Total customer obligation": saved.get('totalCustomerObligation'),
            "Eligible": saved.get('isEligible'),
            "PDF": saved.get('pdfUrl') or "-",
        })
        if problems:
            raise RuntimeError("LIP assessment stored by the server does not check out: " + "; ".join(problems))

    async def _igl_document_names(self, loan: dict) -> list:
        """Document names the upload page offers for the loan's Income Generating purpose: each
        head's documents minus its hiddenDocuments (the web app's getVisibleDocumentHeads). Older
        purpose records are a flat list of names."""
        purpose_name = (loan.get('loanPersonalDetail') or {}).get('purpose')
        record = self.purpose_record if (self.purpose_record or {}).get('name') == purpose_name else None
        if record is None:
            response = await self._make_authenticated_request('GET', "/api/purpose?search=&from=1&to=-1")
            groups = response.json().get('data') or {}
            for items in (groups.values() if isinstance(groups, dict) else [groups]):
                record = next((p for p in (items or []) if isinstance(p, dict) and p.get('name') == purpose_name), None)
                if record:
                    break
        names = []
        for head in (record or {}).get('incomeGeneratingDocumentTypes') or []:
            if isinstance(head, dict):
                hidden = set(head.get('hiddenDocuments') or [])
                names += [d for d in head.get('documents') or [] if d not in hidden]
            elif isinstance(head, str):
                names.append(head)
        return names

    async def _upload_loan_pdf(self) -> dict:
        upload = (await self._upload_file("loan", file_type="pdf"))['uploadFile']
        upload.setdefault('URL', f"{self.BASE_URL}/{upload['path']}")
        upload.setdefault('originalname', os.path.basename(upload['path']))
        return upload

    async def store_loan_documents(self):
        api_path = "/api/loan-process/loan-documents"

        # The upload page is driven by the loan record (single-loan): whether the LIP assessment is
        # required (isLIPFormRequired -- the server applies the IGL / consumption thresholds from
        # global settings), the purpose type, and the Arvog / stamp-paper document rules.
        loan = ((await self.fetch_single_loan()) or {}).get('data') or {}
        master = loan.get('masterLoan') or {}
        await self.fetch_global_setting()

        # Upload the required loan documents (agreement, pawn copy, scheme confirmation).
        loan_agreement = (await self._upload_file("loan", file_type="pdf"))['uploadFile']['path']
        pawn_copy = (await self._upload_file("loan", file_type="pdf"))['uploadFile']['path']
        scheme_confirmation = (await self._upload_file("loan", file_type="pdf"))['uploadFile']['path']

        # Arvog (unless DCB is the co-lender, bank id 4): loan application, gold receipt and KFS copies
        # are mandatory on the upload page.
        arvog_docs = {}
        if loan.get('isArvog') and str(master.get('bankId')) != "4":
            application, receipt, kfs = (await self._upload_loan_pdf(), await self._upload_loan_pdf(),
                                         await self._upload_loan_pdf())
            arvog_docs = {
                "loanApplicationCopy": [application['path']],
                "loanApplicationImageName": application['originalname'],
                "loanApplicationCopyImage": application['URL'],
                "goldReceipt": [receipt['path']],
                "goldReceiptName": receipt['originalname'],
                "goldReceiptCopyName": receipt['URL'],
                "kfsCopy": [kfs['path']],
                "kfsCopyFullUrl": kfs['URL'],
            }
            print("Arvog loan: uploaded loan application, gold receipt and KFS copies.")
        # A stamp-paper stamp duty needs the stamp paper copy.
        stamp_paper_docs = {}
        if loan.get('stampDutyDefinitionType') == 'stampPaper':
            paper = await self._upload_loan_pdf()
            stamp_paper_docs = {"stampPaperCopy": [paper['path']],
                                "stampPaperCopyImageName": paper['originalname'],
                                "stampPaperCopyFullUrl": paper['URL']}
            print("Stamp duty is stamp paper: uploaded the stamp paper copy.")

        # Income Generating loan: at least one income document, under one of the purpose's visible
        # document names -> {name: {name: [filename], path: [path]}} (the web app drops `url`).
        income_generating_documents = None
        purpose_type = (loan.get('loanPersonalDetail') or {}).get('purposeType') or self.purpose_type
        if purpose_type == self.PURPOSE_TYPES["igl"]:
            names = await self._igl_document_names(loan)
            if not names:
                raise RuntimeError(f"Income Generating purpose {self.purpose_id!r} has no visible "
                                   "income document types, but the upload page requires at least one.")
            doc_name = self.random.choice(names)
            upload = await self.upload_income_document()
            income_generating_documents = {doc_name: {
                "name": [upload.get('filename') or upload.get('originalname') or os.path.basename(upload['path'])],
                "path": [upload['path']]}}
            print(f"IGL income document submitted as {doc_name!r}.")

        # Consumption supporting documents: required for a Consumption Based loan whenever the SERVER
        # flags it (showConsumptionSupportingDocs). UAT's portal still enforces this; TEST's portal
        # replaced it with the LIP form (and its server leaves the flag false), so the flag decides.
        consumption_supporting_docs = None
        if loan.get('showConsumptionSupportingDocs') and purpose_type == self.PURPOSE_TYPES["cb"]:
            upload = await self.upload_income_document()
            consumption_supporting_docs = [{"path": upload['path']}]
            print("Consumption supporting document uploaded (server flag showConsumptionSupportingDocs).")

        # LIP assessment, when the server says this loan needs one.
        self.lip_form = self._build_lip_form(loan) if loan.get('isLIPFormRequired') else None
        if self.lip_form is None:
            print("LIP assessment not required for this loan (isLIPFormRequired is false).")

        await self.fetch_lead_converter()

        def full_url(path):
            return f"{self.BASE_URL}/{path}" if path else None

        c_kyc_number = ''.join(self.random.choice('0123456789') for _ in range(14))

        request_body = {
            "loanAgreementCopy": [loan_agreement],
            "pawnCopy": [pawn_copy],
            "schemeConfirmationCopy": [scheme_confirmation],
            "loanApplicationCopy": None,
            "goldReceipt": None,
            "stampPaperCopy": None,
            "signedCheque": None,
            "declaration": None,
            "loanAgreementImageName": os.path.basename(loan_agreement),
            "loanApplicationImageName": None,
            "pawnCopyImageName": os.path.basename(pawn_copy),
            "schemeConfirmationCopyImageName": os.path.basename(scheme_confirmation),
            "goldReceiptName": None,
            "stampPaperCopyImageName": None,
            "signedChequeImageName": None,
            "declarationCopyImageName": None,
            "signedChequeImage": None,
            "declarationCopyImage": None,
            "outstandingLoanAmount": None,
            "loanAgreementCopyImage": [full_url(loan_agreement)],
            "loanApplicationCopyImage": None,
            "pawnCopyImage": [full_url(pawn_copy)],
            "schemeConfirmationCopyImage": full_url(scheme_confirmation),
            "goldReceiptCopyName": None,
            "stampPaperCopyFullUrl": None,
            "kfsCopy": None,
            "kfsCopyFullUrl": None,
            "form97": None,
            "form97Name": None,
            "form97FullUrl": None,
            "processingCharges": None,
            "standardDeduction": None,
            "customerConfirmation": None,
            "customerConfirmationImage": None,
            "customerConfirmationImageName": None,
            "customerConfirmationStatus": None,
            "purchaseVoucher": None,
            "purchaseVoucherImage": None,
            "purchaseVoucherImageName": None,
            "purchaseInvoice": None,
            "purchaseInvoiceImage": None,
            "purchaseInvoiceImageName": None,
            "saleInvoice": None,
            "saleInvoiceImage": None,
            "saleInvoiceImageName": None,
            "signature": full_url(self.signature_proof),
            "panImage": self.pan_image,
            "panImageFullUrl": full_url(self.pan_image),
            "panImageName": None,
            "panCardNumber": self.random_pan,  # HAR includes this alongside panImage
            # HAR sends identityProof / unMaskedIdentityProof ENCRYPTED (CryptoJS AES, "U2FsdGVkX1..."),
            # same as submit-all-kyc-info. Raw paths -> "Invalid identity proof"; empty -> "Identity
            # proof is required". identityProofFullUrl stays a plaintext URL.
            "identityProof": [self._encrypt_identity_proof_number(self.masked_identity_proof)] if self.masked_identity_proof else [],
            "unMaskedIdentityProof": [self._encrypt_identity_proof_number(self.unmasked_identity_proof)] if self.unmasked_identity_proof else [],
            "identityProofFullUrl": [full_url(self.masked_identity_proof)] if self.masked_identity_proof else [],
            "identityProofName": None,
            "advanceCashTransactionId": [],
            "advanceCashTransactionImage": [],
            "advanceCashTransactionImageName": "",
            "advanceCashDeclarationId": [],
            "advanceCashDeclarationImage": [],
            "advanceCashDeclarationImageName": "",
            "receiptNumber": "",
            "advanceCash": 0,
            "appraiserCharges": 0,
            "actualCashGiven": 0,
            "advanceTransferId": "",
            "cpvImage": None,
            "cpvImageFullUrl": None,
            "leadConverterId": self.lead_converter_id or (str(self.logged_in_user_id) if self.logged_in_user_id else ""),
            "incomeGeneratingDocuments": income_generating_documents,
            # Only when the server asks for them (UAT); TEST replaced them with the LIP form.
            **({"consumptionSupportingDocs": consumption_supporting_docs} if consumption_supporting_docs else {}),
            "lipForm": self.lip_form,
            "cKycNumber": c_kyc_number,
            "checkPointers": {},
            "loanId": int(self.loan_id) if self.loan_id else None,
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            "loanAmountDigi": f"{float(self.final_loan_amount):.2f}" if self.final_loan_amount else "0.00",
            "loanType": self.loan_type or "Fresh Loan",
        }
        request_body.update(arvog_docs)
        request_body.update(stamp_paper_docs)
        async def send():
            return await self._make_authenticated_request('POST', api_path, json_data=request_body)
        # A rejected upload is FATAL: the loan cannot leave the upload-documents stage, so every
        # later step would only fail with a confusing "stage has been changed" error.
        await self._with_stage_recovery("loan documents", send)
        print('Store loan documents successful.' + (' (with LIP assessment)' if self.lip_form else ''))
        if self.lip_form:
            await self.verify_lip_assessment()

    # --- Disbursement (partner login) -------------------------------------------------------------
    async def fetch_disbursement_bank_detail(self) -> dict:
        """GET the disbursement bank/amount detail. Its `data` supplies almost every field the
        partner-wise-disbursement POST needs (amounts, scheme name, unique id, customer bank), so we
        source the disbursement body from here rather than reconstructing from tracked state."""
        api_path = (f"/api/loan-process/disbursement-loan-bank-detail"
                    f"?loanId={self.loan_id}&masterLoanId={self.master_loan_id}")
        response = await self._make_authenticated_request('GET', api_path)
        data = response.json().get("data", {}) if isinstance(response.json(), dict) else {}
        # loan_unique_id is normally captured at the assign-packet stage (fetch_single_loan); this is
        # just a fallback in case that didn't run for this path.
        if not self.loan_unique_id and data.get("securedLoanUniqueId"):
            self.loan_unique_id = data["securedLoanUniqueId"]
        print(f"Fetched disbursement bank detail (finalLoanAmount={data.get('finalLoanAmount')}, "
              f"loanUniqueId={self.loan_unique_id}).")
        return data

    async def disburse_amount(self):
        """Disburse the loan to the customer's bank account. Runs under the PARTNER login
        (per env + selected partner, OTP 1234) -- disbursement is the partner's action -- then restores the
        appraiser session. Body is sourced from disbursement-loan-bank-detail so amounts/scheme
        always match the server's view of this loan."""
        saved_token = self.auth_token
        try:
            print(f"Partner login for disbursement: {self._partner_login_mobile()} "
                  f"(partnerId {self.partner_id}).")
            self.auth_token = await self._role_token("partner")
            await self.update_loan_lock()          # HAR: partner locks the disbursement stage first
            detail = await self.fetch_disbursement_bank_detail()
            await self.validate_account()          # HAR re-runs penny-drop before disbursing
            await self._partner_disbursement_status(detail)  # partner APPROVES, then disburses
            await self._post_partner_disbursement(detail)
        finally:
            self.auth_token = saved_token

    async def _partner_disbursement_status(self, detail: dict):
        """Partner APPROVAL, immediately before disbursement (POST /api/loan-process/partner-
        disbursement-status). The partner signs off on the loan (loanStatusForPartner 'approved')
        under the partner login. Runs inside disburse_amount's partner-token block."""
        api_path = "/api/loan-process/partner-disbursement-status"
        # The portal sends approvalForm.value: the appraiser/BM/Ops STATUS controls are disabled for
        # the partner (left out); their application/gold flags and comments come from masterLoan.
        loan = await self._loan_record()
        request_body = self._rating_base(loan, defaults={
            "applicationFormForAppraiser": True, "goldValuationForAppraiser": True,
            "commentByAppraiser": None, "applicationFormForBM": False, "goldValuationForBM": False,
            "applicationFormForOperatinalTeam": True, "goldValuationForOperatinalTeam": True,
            "scrapStatusForAppraiser": None, "scrapStatusForBM": None,
            "scrapStatusForOperatinalTeam": "pending", "packetApprovalByOps": None,
            "commentByOpsForPacketApproval": "", "statusForRh": "pending", "commentByRh": "",
            "applicationFormForRh": False, "goldValuationForRh": False,
        })
        if not request_body.get("loanId"):
            request_body["loanId"] = detail.get("securedLoanId")
        request_body.update({
            "applicationFormForPartner": True,
            "goldValuationForPartner": True,
            "loanStatusForPartner": "approved",
            "partnerId": int(self.partner_id) if self.partner_id and str(self.partner_id).isdigit() else 152,
        })
        try:
            response = await self._make_authenticated_request('POST', api_path, json_data=request_body)
            msg = response.json().get("message") if isinstance(response.json(), dict) else ""
            print(f"Partner approval successful: {msg}")
        except httpx.HTTPStatusError as e:
            # The loan can already be at "disbursement pending" (the stage this approval moves it to)
            # -- the server then 400s with a "stage has been changed to: disbursement pending, please
            # re-visit" notice. That is exactly the state disbursement needs, so treat it as done.
            body = e.response.text if e.response is not None else ""
            if e.response is not None and e.response.status_code == 400 and "disbursement pending" in body.lower():
                print(f"Partner approval: loan already at disbursement-pending (continuing). Server said: {body}")
            else:
                raise

    @staticmethod
    def _apply_portal_disbursement_amounts(body: dict) -> None:
        """The portal's calcfinalLoanAmount for a fresh loan (not renew / top-up):
            finalAmount = +((fullSecuredAmount + fullUnsecuredAmount) - (processingCharge +
                           totalManualCharges + upfrontInterestAmount + advanceBankTransfer +
                           actualCashGiven + appraiserCharges + stampDutyCharges + bTMoney)).toFixed(2)
            fullAmount  = fullSecuredAmount + fullUnsecuredAmount + processingCharge +
                          appraiserCharges + stampDutyCharges + totalManualCharges + upfrontInterestAmount
        i.e. finalAmount is the NET amount paid out and fullAmount the gross with charges."""
        def n(key):
            try:
                return float(body.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0
        principal = n("fullSecuredAmount") + n("fullUnsecuredAmount")
        deductions = (n("processingCharge") + n("totalManualCharges") + n("upfrontInterestAmount")
                      + n("advanceBankTransfer") + n("actualCashGiven") + n("appraiserCharges")
                      + n("stampDutyCharges") + n("bTMoney"))
        body["finalAmount"] = round(principal - deductions + 1e-9, 2)
        body["fullAmount"] = principal + n("processingCharge") + n("appraiserCharges") + \
            n("stampDutyCharges") + n("totalManualCharges") + n("upfrontInterestAmount")

    async def _post_partner_disbursement(self, detail: dict):
        api_path = "/api/loan-process/partner-wise-disbursement"
        ubd = detail.get("userBankDetail", {}) if isinstance(detail, dict) else {}
        secured_amount = detail.get("securedLoanAmount") or f"{float(self.final_loan_amount):.2f}"
        final_amount = detail.get("finalLoanAmount") or float(self.final_loan_amount or 0)
        # Manually-entered bank UTR/reference (a random numeric string in the HAR).
        bank_txn_id = str(self.random.randint(10**14, 10**15 - 1))
        # penalInterest comes from single-loan (data.penalInterest), as in the web app -- NOT the
        # scheme's own penalInterest, which differs (27.18 vs 2 in the capture).
        loan = await self._loan_record()
        penal_interest = detail.get("penalInterest") if isinstance(detail, dict) else None
        if penal_interest is None:
            penal_interest = loan.get("penalInterest")
        # actualProcessingCharge = the loan's processing charge (the upper limit the portal checks
        # processingCharge against): bank-detail response first, else single-loan. NOT interest.
        actual_processing_charge = detail.get("actualProcessingCharge")
        if actual_processing_charge is None:
            actual_processing_charge = loan.get("actualProcessingCharge")
        bank_array = [{
            "disbursementStatus": "Disbursed to Customer",
            "ifscCode": ubd.get("ifscCode") or self.bank_ifsc_code,
            "bankName": ubd.get("bankName") or self.bank_name,
            "bankBranch": ubd.get("bankBranchName") or self.bank_branch_name,
            "accountHolderName": ubd.get("accountHolderName") or self.account_holder_name,
            "accountNumber": ubd.get("accountNumber") or self.bank_account_number,
        }]
        # The portal's disburse form is patched from the bank-detail response (patchValue(detail)),
        # so every key that response carries wins over a default.
        def d(key, default=None):
            return detail.get(key, default) if isinstance(detail, dict) else default
        # Payment gateway: the partner's ICICI payout config on the loan record (the portal's default
        # gateway when there is an amount to pay); the captured flow's 5 is exactly that config id.
        payout = next((c for c in ((loan.get('partner') or {}).get('merchantPayoutConfigs') or [])
                       if isinstance(c, dict) and str(c.get('paymentGateway', '')).lower() == 'icici'), None)
        # The portal pre-selects the ICICI config (id) on load when it is usable and there is an
        # amount to pay; picking IMPS/NEFT/RTGS under OFFLINE does not change that id (the change
        # handler finds no config named after the mode), so the id stays the ICICI one -- exactly
        # the captured "neft + gateway 5" pair. With no usable ICICI config the id stays null.
        to_be_paid = float(((loan.get('masterLoan') or {}).get('toBePaid')) or 0)
        payment_gateway_id = (payout or {}).get('id') if (payout and to_be_paid > 0) else None
        # partnerId comes from the PARTNER login's token in the portal.
        try:
            partner_claims = jwt.decode(self.auth_token, options={"verify_signature": False}) if self.auth_token else {}
        except Exception:
            partner_claims = {}
        partner_id = partner_claims.get('partnerId') or (
            int(self.partner_id) if self.partner_id and str(self.partner_id).isdigit() else 152)
        request_body = {
            "isNewLoanFromRenew": d("isNewLoanFromRenew", False), "isTopUpAdded": d("isTopUpAdded", False),
            "isNewLoanFromTopUp": d("isNewLoanFromTopUp", False),
            "loanId": int(self.loan_id) if self.loan_id else detail.get("securedLoanId"),
            "securedCashTransactionId": None,
            "securedBankTransactionId": bank_txn_id,
            "securedAmountDisbursedByCash": None,
            "securedAmountDisbursedByBank": secured_amount,
            "securedCashReceiptImage": None, "unsecuredCashReceiptImage": None,
            "securedCashReceiptPath": None, "unsecuredCashReceiptPath": None,
            "unsecuredCashTransactionId": None, "unsecuredBankTransactionId": None,
            "unsecuredAmountDisbursedByCash": None, "unsecuredAmountDisbursedByBank": None,
            "securedTransactionId": None, "unsecuredTransactionId": None,
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "paymentMode": ["bank"],
            "loanAmount": final_amount, "otp": None,
            "maxAmountToBeDisbursedByCash": detail.get("maxAmountToBeDisbursedByCash", 1000000000),
            "bankArray": bank_array,
            "masterLoanId": int(self.master_loan_id) if str(self.master_loan_id).isdigit() else detail.get("masterLoanId"),
            "securedSchemeName": detail.get("securedSchemeName"),
            "unsecuredLoanAmount": detail.get("unsecuredLoanAmount", 0),
            "unsecuredSchemeName": d("unsecuredSchemeName"),
            "securedLoanAmount": secured_amount,
            "securedLoanId": detail.get("securedLoanId") or (int(self.loan_id) if self.loan_id else None),
            "unsecuredLoanId": d("unsecuredLoanId"), "scrapId": None, "scrapAmount": None, "transactionId": None,
            "fullSecuredAmount": detail.get("fullSecuredAmount", final_amount),
            "fullUnsecuredAmount": detail.get("fullUnsecuredAmount", 0),
            "processingCharge": detail.get("processingCharge", 0),
            "securedProcessingCharge": detail.get("securedProcessingCharge", "0.00"),
            "unsecuredProcessingCharge": detail.get("unsecuredProcessingCharge", 0),
            "isUnsecuredSchemeApplied": detail.get("isUnsecuredSchemeApplied", False),
            "securedLoanUniqueId": detail.get("securedLoanUniqueId"),
            "unsecuredLoanUniqueId": d("unsecuredLoanUniqueId"),
            "finalAmount": None, "fullAmount": None,   # computed below, as calcfinalLoanAmount
            "bankTransferType": self.transfer_mode,   # IMPS / NEFT / RTGS (OFFLINE), lowercased like the portal
            "loanTransferExtraAmount": d("loanTransferExtraAmount"),
            "otherAmountTransactionId": d("otherAmountTransactionId"), "utrNumber": d("utrNumber"),
            "totalManualCharges": detail.get("totalManualCharges", 0),
            "upfrontInterestAmount": detail.get("upfrontInterestAmount", 0),
            "upfrontTransactionId": None, "upfrontReceiptImage": None, "upfrontReceipt": None,
            "fullUnsecuredTopUpAmount": d("fullUnsecuredTopUpAmount", 0), "fullSecuredTopUpAmount": d("fullSecuredTopUpAmount", 0),
            "finalTopUpAmount": d("finalTopUpAmount", 0),
            "securedTopUpAmount": d("securedTopUpAmount", 0), "unsecuredTopUpAmount": d("unsecuredTopUpAmount", 0),
            "advanceBankTransfer": d("advanceBankTransfer", 0), "advanceCash": d("advanceCash", 0), "bTMoney": d("bTMoney", 0),
            "penalInterest": penal_interest if penal_interest is not None else 2,
            "advancedCashReceiptImage": None, "advanceCashDeclarationImage": None,
            "receiptNumber": d("receiptNumber"), "appraiserCharges": d("appraiserCharges", loan.get("appraiserCharges") or 0),
            "actualProcessingCharge": actual_processing_charge,
            "actualCashGiven": d("actualCashGiven", 0), "stampDutyRemarks": "",
            "stampDutyCharges": detail.get("stampDutyCharges") or f"{float(self.stamp_duty_charges or 0):.2f}",
            "paymentGatewayId": payment_gateway_id,   # partner.merchantPayoutConfigs (icici)
            "partnerId": partner_id,                  # the partner login's token
            "loanType": self.loan_type or "Fresh Loan",
        }
        self._apply_portal_disbursement_amounts(request_body)
        response = await self._make_authenticated_request('POST', api_path, json_data=request_body)
        msg = response.json().get("message") if isinstance(response.json(), dict) else ""
        print(f"Disbursement successful: {msg}")

    # --- Submit packet (appraiser) --------------------------------------------------------------
    # The packet is handed over at the partner-branch collection point, to a partner user who signs
    # off with an OTP (static 1234). The location and branch ids differ per environment/partner, so
    # they are resolved live (location by name from /api/packet-location, branch from the partner
    # record) and these values are only fallbacks / overrides.
    PACKET_LOCATION_NAME = "partner branch in"
    PACKET_LOCATION_ID = "4"          # fallback: "partner branch in" on test
    PARTNER_BRANCH_ID = "146"         # fallback: Roshan / Nerul Navi Mumbai on test

    async def submit_packet(self):
        """Hand the sealed packet to the partner branch to complete the loan. Runs as the appraiser:
        view packets -> resolve partner location/user -> partner-user OTP (1234) -> submit-packet-
        location. The collection point is resolved from the API for the running environment: the
        "partner branch in" packet location, the selected partner's branch, and that partner's
        branch user (per-environment mobile)."""
        await self.update_loan_lock()

        # 1. The sealed packet(s) for this loan -> barcode + packetUniqueId for submit-packet-location.
        vp = await self._make_authenticated_request(
            'GET', f"/api/packet-tracking/view-packets?masterLoanId={self.master_loan_id}")
        barcodes = []
        for grp in (vp.json().get("data") or []):
            for p in (grp.get("packets") or []):
                bc = p.get("barcodeNumber") or p.get("packetUniqueId")
                if bc:
                    barcodes.append({"Barcode": bc.upper(), "packetId": bc})
        assert barcodes, f"No packets found for masterLoanId {self.master_loan_id}"

        # 2. Partner location details (partnerId + name) for the chosen collection location. The
        #    "partner branch in" location id is looked up by name so it holds on any environment.
        locs = await self._make_authenticated_request('GET', "/api/packet-location?search=&from=1&to=-1")
        packet_location_id = os.getenv("GOLD_LOAN_PACKET_LOCATION_ID", "").strip() or self.PACKET_LOCATION_ID
        for entry in (locs.json().get("data") or []):
            if str(entry.get("location", "")).strip().lower() == self.PACKET_LOCATION_NAME:
                packet_location_id = str(entry.get("id"))
                break
        loc = await self._make_authenticated_request(
            'GET', f"/api/packet-tracking/get-particular-location"
                   f"?packetLocationId={packet_location_id}&masterLoanId={self.master_loan_id}")
        loc_data = loc.json().get("data", {}) if isinstance(loc.json(), dict) else {}
        default_partner_id = int(self.partner_id) if str(self.partner_id).isdigit() else int(self.ROSHAN_PARTNER_ID)
        partner_id = loc_data.get("id") or default_partner_id
        partner_name = loc_data.get("name") or "Roshan Partner"

        # 3. Resolve the partner user who receives the packet: the mobile comes from the environment
        #    profile, the branch from the partner record just fetched (branch ids are env-specific).
        #    The user only resolves under the branch they belong to, so try the partner's branches in
        #    order and keep the first that returns a user.
        partner_user_mobile = self._partner_user_mobile()
        forced_branch = os.getenv("GOLD_LOAN_PARTNER_BRANCH_ID", "").strip()
        if forced_branch:
            branch_candidates = [forced_branch]
        else:
            branch_candidates = [str(b.get("id")) for b in (loc_data.get("partnerBranch") or [])
                                 if b.get("id")] or [self.PARTNER_BRANCH_ID]
        partner_branch_id, un_data = branch_candidates[0], {}
        receiver_type = "PartnerUser"
        for candidate in branch_candidates:
            un = await self._make_authenticated_request(
                'GET', f"/api/packet-tracking/user-name?mobileNumber={partner_user_mobile}"
                       f"&receiverType=PartnerUser&partnerBranchId={candidate}"
                       f"&masterLoanId={self.master_loan_id}&allUsers=1")
            un_json = un.json() if isinstance(un.json(), dict) else {}
            data = un_json.get("data")
            if isinstance(data, dict) and data.get("id"):
                partner_branch_id, un_data = candidate, data
                receiver_type = un_json.get("receiverType") or "PartnerUser"
                break
        partner_receiver_id = un_data.get("id")
        user_full = f"{un_data.get('firstName','')} {un_data.get('lastName','')}".strip()
        print(f"Packet handover -> location {packet_location_id} ({self.PACKET_LOCATION_NAME}), "
              f"partner {partner_name}, branch {partner_branch_id}, "
              f"user {user_full or partner_user_mobile}.")
        if not partner_receiver_id:
            print(f"WARNING: no PartnerUser resolved for {partner_user_mobile} in branches "
                  f"{branch_candidates} — submit-packet will likely be rejected.")

        # 4. Partner-user OTP handshake (static OTP 1234).
        send = await self._make_authenticated_request(
            'POST', "/api/partner-user-otp/send-otp",
            json_data={"mobileNumber": partner_user_mobile,
                       "id": int(self.logged_in_user_id) if self.logged_in_user_id else None,
                       "type": "updateLocationCollect", "masterLoanId": int(self.master_loan_id)})
        ref = send.json().get("referenceCode")
        await self._make_authenticated_request(
            'POST', "/api/partner-user-otp/verify-otp",
            json_data={"otp": "1234", "referenceCode": ref, "type": "updateLocationCollect"})

        # 5. Submit the packet to the partner location.
        request_body = {
            "packetLocationId": packet_location_id,
            "barcodeNumber": barcodes,
            "mobileNumber": partner_user_mobile,
            "user": user_full,
            "receiverType": receiver_type,  # as the user-name lookup answered
            "otp": "1234", "referenceCode": ref,
            "userReceiverId": None, "customerReceiverId": None,
            "partnerReceiverId": partner_receiver_id,
            "loanId": int(self.loan_id) if self.loan_id else None,
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            "partnerId": partner_id, "partnerName": partner_name,
            "partnerBranchId": partner_branch_id,
            "internalBranchId": None, "deliveryPacketLocationId": None,
            "deliveryInternalBranchId": None, "deliveryPartnerBranchId": None,
            "deliveryPartnerName": None, "id": None, "releaseId": None, "role": None,
            "packetTransferId": None, "partnerBranch": None, "partnerBranchUserName": None,
            "customerHandOver": None, "customerAcknowledgement": None,
            "isAuction": None, "auctionDocuments": None,
        }
        response = await self._make_authenticated_request(
            'POST', "/api/packet-tracking/submit-packet-location", json_data=request_body)
        msg = response.json().get("message") if isinstance(response.json(), dict) else ""
        print(f"Submit packet successful: {msg}")

    # Where a loan can be looked up by its AUGM id. Each entry is
    # (path, extra query params to try, filter keys to try); a filter key of "" means an
    # unfiltered paged scan matched client-side.
    #
    # BOTH listings are needed and BOTH are scoped to the logged-in user:
    #   * applied-loan-details backs the portal's "applied loan" screen and returns its rows
    #     under `appliedLoanDetails`, NOT `data`;
    #   * loan-details is what the UI loads after submit-packet -- but a just-completed loan is
    #     NOT reliably in it (a run that finished at stage 13 still got `{"data": []}` for its
    #     own AUGM id), so it cannot be the only place we look.
    #
    # ONLY `loanUniqueId` is sent as a filter. These endpoints map an unrecognised query
    # parameter straight onto a database column, so a guessed one is not an ignored filter or a
    # 400 -- it is a 500 with the raw error in an HTML page:
    #     GET applied-loan-details?...&search=AUGM-79787
    #     -> 500 "column customerLoanMaster.search does not exist"
    # Do not add speculative filter names without a capture showing the server accepts them.
    LOAN_SEARCH_ENDPOINTS = (
        ("/api/loan-process/loan-details", ("",), ("loanUniqueId", "")),
        ("/api/loan-process/applied-loan-details",
         ("isRejectedLoan=false", "isRejectedLoan=true"), ("loanUniqueId", "")),
    )
    LOAN_SEARCH_MAX_PAGES = 8

    @staticmethod
    def _loan_rows(payload):
        """Rows out of a loan listing, whichever key that endpoint happens to use."""
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ("data", "appliedLoanDetails", "loanDetails"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return rows
        return []

    @staticmethod
    def _loan_row_matches(row, loan_unique_id):
        if not isinstance(row, dict):
            return False
        if str(row.get("loanUniqueId") or "") == loan_unique_id:
            return True
        customer_loans = row.get("customerLoan")
        if isinstance(customer_loans, dict):
            customer_loans = [customer_loans]
        for loan in customer_loans or []:
            if isinstance(loan, dict) and str(loan.get("loanUniqueId") or "") == loan_unique_id:
                return True
        return False

    async def _scan_for_loan(self, loan_unique_id, attempts):
        """Walk every listing/filter combination under the CURRENT token. Returns a row or None."""
        quoted = urllib.parse.quote(str(loan_unique_id))
        for endpoint, extras, filters in self.LOAN_SEARCH_ENDPOINTS:
            for extra in extras:
                for key in filters:
                    pages = self.LOAN_SEARCH_MAX_PAGES if not key else 1
                    for page in range(pages):
                        frm = page * 25 + 1
                        api_path = f"{endpoint}?from={frm}&to={frm + 24}"
                        if extra:
                            api_path += f"&{extra}"
                        if key:
                            api_path += f"&{key}={quoted}"
                        try:
                            response = await self._make_authenticated_request('GET', api_path)
                        except httpx.HTTPStatusError as e:
                            attempts.append(f"{endpoint} {extra or ''} {key or 'scan'} -> "
                                            f"{e.response.status_code}")
                            break
                        payload = response.json() if response.content else {}
                        for row in self._loan_rows(payload):
                            if self._loan_row_matches(row, loan_unique_id):
                                print(f"Found {loan_unique_id} via {endpoint} "
                                      f"({key or 'unfiltered scan'}).")
                                return row
                        pagination = payload.get("pagination") if isinstance(payload, dict) else {}
                        if not self._loan_rows(payload) or not (pagination or {}).get("hasMore"):
                            break
        return None

    async def find_loan_row(self, loan_unique_id, attempts=None):
        """Find a loan's listing row by its AUGM id, or None.

        Tries loan-details then applied-loan-details, each filtered by loanUniqueId and then
        scanned unfiltered, and repeats the whole ladder under an ADMIN token -- these listings
        are scoped to the logged-in user, so a loan handed between roles (the appraiser creates
        it, the partner disburses it) is frequently invisible to the session that made it.
        `attempts` collects a per-listing note for the caller's error message.
        """
        attempts = attempts if attempts is not None else []
        row = await self._scan_for_loan(loan_unique_id, attempts)
        if row is not None:
            return row
        print(f"{loan_unique_id} is not visible to this login - retrying the lookup as admin.")
        saved_token = self.auth_token
        try:
            self.auth_token = await self._role_token("admin")
            row = await self._scan_for_loan(loan_unique_id, attempts)
        except Exception as e:  # a failed admin login must not mask the real diagnosis
            print(f"Admin-scoped loan lookup failed: {e}")
            row = None
        finally:
            self.auth_token = saved_token
        return row

    async def fetch_loan_details(self) -> dict:
        """Final step: search the loan-details list by the loan's unique id (AUGM-…) and display the
        completed loan's key attributes. Mirrors the UI loading the loan after submit-packet. Returns
        the matched loan record (or {})."""
        if not self.loan_unique_id:
            raise AssertionError(
                "Cannot load the finished loan: no loanUniqueId was captured. It is assigned at "
                "the assign-packet stage and read from single-loan; without it there is nothing "
                "to search for, and taking the first row of the list would show a DIFFERENT loan.")
        attempts = []
        loan = await self.find_loan_row(self.loan_unique_id, attempts)
        if not loan:
            # The whole point of this step is to read the finished loan back. Not finding it is a
            # FAILED verification, not a cosmetic gap -- returning {} here let a run print
            # "RESULT - PASSED" while its closing check silently found nothing.
            raise AssertionError(
                f"Loan {self.loan_unique_id} could not be loaded back after the flow completed - "
                "searched loan-details and applied-loan-details, filtered and unfiltered, under "
                f"both the {self.logged_in_mobile_number} session and the admin scope "
                f"({'; '.join(attempts) or 'every listing came back empty'}). The loan may exist "
                "but be invisible to these logins; check it on the portal.")

        # Pull display fields (some live under customerLoan[0]).
        cl = (loan.get("customerLoan") or [{}])[0] if isinstance(loan.get("customerLoan"), list) else {}
        cust = loan.get("customer") or {}
        stage = loan.get("loanStage") or {}
        appr = loan.get("appraiser") or {}
        details = {
            "Loan Unique ID": cl.get("loanUniqueId") or self.loan_unique_id,
            "Master Loan ID": loan.get("id"),
            "Loan Stage": f"{stage.get('name')} (id {stage.get('id')})",
            "Final Loan Amount": loan.get("finalLoanAmount"),
            "Loan Type": loan.get("loanType"),
            "Tenure (months)": loan.get("tenure"),
            "Loan Start": loan.get("loanStartDate"),
            "Loan End": loan.get("loanEndDate"),
            "Audit Status": loan.get("auditStatus"),
            "DPD Days": loan.get("dpdDays"),
            "Customer": f"{cust.get('firstName','')} {cust.get('lastName','')} "
                        f"({cust.get('customerUniqueId','')}, {cust.get('mobileNumber','')})".strip(),
            "PAN": cust.get("panCardNumber"),
            "Appraiser": f"{appr.get('firstName','')} {appr.get('lastName','')}".strip(),
        }
        self._print_summary_table(f"LOAN DETAILS ({details['Loan Unique ID']})", details)
        print(f"Loan process complete — stage: {stage.get('name')}.")
        return loan


    # Cleared between loans in a --count run: everything that identifies ONE customer or ONE
    # loan. Deliberately KEPT, because keeping it is the whole point of a batch:
    #   * auth_token and _role_token_cache -- one login per role for the WHOLE batch, so the
    #     OTP send endpoint's "Please try again after a minute" rate limit is never hit;
    #   * the appraiser identity (appraiser_id, logged_in_*) and every CLI/env choice;
    #   * the bank ACCOUNT the penny-drop settled on -- re-walking the candidate list per loan
    #     would fire the same doomed penny-drops again. Its VERIFICATION verdicts are cleared
    #     below, because every loan needs its own ops approval.
    PER_LOAN_STATE = (
        # customer identity
        "customer_id", "customer_unique_id", "customer_kyc_id", "mobile_number",
        "first_name", "last_name", "random_pan", "pan_image", "form60_image",
        "identity_proof_number", "encrypted_identity_proof_number",
        "masked_identity_proof", "unmasked_identity_proof", "address_proof",
        "name_as_per_aadhaar", "profile_image", "signature_proof",
        "mother_name", "spouse_name", "martial_status", "gender", "dob", "age",
        "ovd_image", "form97_image", "ovd_name", "kyc_status", "kyc_reference_code",
        # loan identity and financials
        "appraiser_request_id", "master_loan_id", "loan_id", "loan_unique_id",
        "loan_stage_id", "scheme_id", "final_loan_amount", "total_eligible_amount",
        "total_final_interest_amt", "secured_rpg", "secured_ltv", "tenure_months",
        "interest_rate", "secured_processing_charge", "upfront_interest_amount",
        "secured_exposure", "secured_rebate_interest", "loan_start_date", "loan_end_date",
        "max_loan_limit", "min_loan_amount", "max_loan_amount", "purpose_id", "purpose_type",
        "nominee_relation_id", "ornament_type_id", "karat_id", "reference_code",
        "lead_converter_id",
    )

    def _reset_for_next_loan(self) -> None:
        """Clear the previous loan's customer and loan state so the next one starts clean."""
        for field in self.PER_LOAN_STATE:
            if not hasattr(self, field):
                continue
            current = getattr(self, field)
            if isinstance(current, bool):
                setattr(self, field, False)
            elif isinstance(current, (int, float)):
                setattr(self, field, type(current)())
            else:
                setattr(self, field, "")
        # Stamp duty goes back to the configured default, not 0 -- the next loan still pays it.
        self.stamp_duty_charges = float(os.getenv("GOLD_LOAN_STAMP_DUTY", "0"))
        self.stamp_duty_definition_type = None
        self.stamp_duty_from_server = False
        # Containers
        self.customer_details = {}
        self.customer_loan_data = {}
        self.final_loan_details_data = {}
        self.secured_scheme = {}
        self.purpose_record = {}
        self.lip_form = None
        self.loan_type = ''
        self.customer_user_type = ''
        self.ornaments_total_nwap = None
        self.basic_details_response = {}
        self.selected_scheme = {}
        self.loan_ornaments_details = []
        self.interest_table_data = []
        self.available_packet = {}
        self.packet_prepared = False
        self._created_packet_unique = None
        self.passbook_proofs = []
        self.karza_account_details = {}
        self._diagnostics = {}
        self._branch_attempts = []
        # Call metrics are cumulative, so reset them: each loan reports its OWN calls and
        # failures rather than the batch running total.
        self._api_calls = 0
        self._api_failures = 0
        self._failed_endpoints = []
        # Per-loan decisions
        self._existing_request_status = ''
        self._existing_process_complete = False
        self._existing_loan_completed = False
        self._existing_request_appraiser_id = None
        self.bm_rating_submitted = False
        self.co_lender_bank_id = self._forced_co_lender_id or ''
        # The account stays; its verdicts do not -- each loan gets its own ops approval.
        self.bank_account_verified = False
        self.bank_system_verified = False
        self.bank_manually_verified = False
        self.bank_for_ops_approval = False
        # KYC verdicts are per-customer, and so is a degrade back to manual.
        self.pan_verified = False
        self.aadhaar_verified = False
        self.ovd_verified = False
        self.kyc_verification_log = []
        self.kyc_mode_effective = self.kyc_mode_requested
        self.kyc_mode = self.kyc_mode_requested

    async def run_many(self, login_type: str, count: int) -> bool:
        """Run the full journey `count` times inside ONE logged-in session.

        Only the FIRST loan logs in; every later one reuses `auth_token` and the role-token
        cache, so admin/bm/ops/partner each authenticate once for the batch instead of once per
        loan. That is not just faster: the OTP send endpoint rate-limits repeat requests to the
        same number ("Please try again after a minute"), which a loan-per-login batch would hit
        immediately.

        A failure stops that loan, not the batch -- the remaining loans still run, and the
        summary at the end says which ones failed.
        """
        results = []
        seen_master_loans = {}
        for index in range(1, count + 1):
            if index > 1:
                self._reset_for_next_loan()
            self._banner(f"LOAN {index} OF {count}",
                         "reusing the logged-in session" if index > 1 else "logging in")
            error = ""
            try:
                passed = bool(await self.run_e2e_test(login_type=login_type, skip_login=index > 1))
            except Exception as e:
                # run_e2e_test re-raises after printing its own diagnosis; in a batch that must
                # end THIS loan, not the remaining ones.
                passed, error = False, f"{type(e).__name__}: {e}"
            # With --customer, loans 2..N land on the SAME appraiser request. If the server
            # answers "Your loan already initiated" instead of opening a new one, the flow would
            # re-run every step against the PREVIOUS loan and could still report success. Catch
            # that here rather than reporting a loan that was never created.
            master = str(self.master_loan_id or "")
            if passed and master and master in seen_master_loans:
                passed = False
                error = (f"masterLoanId {master} is the loan from run "
                         f"{seen_master_loans[master]} - the server did not open a new loan for "
                         "this customer, it handed back the previous one")
                print(self._c(f"  {error}.", "red"))
            elif master:
                seen_master_loans[master] = index
            results.append({
                "n": index,
                "passed": passed,
                "customer": self.customer_unique_id or "-",
                "loan": self.loan_unique_id or "-",
                "amount": self.final_loan_amount or 0,
                "error": error,
            })

        self._banner("BATCH SUMMARY", f"{sum(r['passed'] for r in results)}/{count} loans completed")
        for r in results:
            mark = self._c(self._G["ok"], "green") if r["passed"] else self._c(self._G["fail"], "red")
            print(f"  {mark} loan {r['n']}/{count}  customer={r['customer']:<12} "
                  f"loan={r['loan']:<14} amount={r['amount']}")
            if r["error"]:
                print(self._c(f"      {r['error'][:160]}", "red"))
        return all(r["passed"] for r in results)

    async def run_e2e_test(self, login_type: str, skip_login: bool = False):
        """Run the whole journey once. Returns True when it completed.

        `skip_login=True` reuses the session already on this instance -- used by run_many for
        loans 2..N so a batch logs in once instead of once per loan.
        """
        self._banner(
            "GOLD LOAN - END-TO-END API TEST",
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
                # Always a fresh login (the backend invalidates old sessions, so there is nothing to
                # reuse). Static OTP 1234.
                print("Performing fresh login.")
                await self.login(self._get_mobile_number_for_login_type(login_type))
                assert self.auth_token, "Login failed: auth_token is empty after login attempt."

            # Interactive co-lender pick (from --co-lender with no value): now that we're authenticated,
            # show the live co-lender menu and let the user choose before the loan flow starts.
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
                # Customer creation runs under ADMIN — creating the customer as the appraiser hits a
                # "request already exists" error, which the admin credential avoids. KYC and the
                # appraiser-request (assigning the appraiser to the loan) then run as the appraiser.
                # The admin swap only changes self.auth_token; logged_in_user_id stays the appraiser's
                # (set at login, untouched by _role_token), so the appraiser-request binds correctly.
                self._log_step("Create Customer / Lead (admin)")
                saved_token = self.auth_token
                try:
                    self.auth_token = await self._role_token("admin")
                    await self.add_customer()
                finally:
                    self.auth_token = saved_token  # back to the appraiser for KYC + appraiser-request
                self._log_step("KYC Process (appraiser)")
                await self._run_full_kyc()

            self._log_step("Appraiser Request & Loan Basics")
            await self.create_appraiser_request()
            await self.track_loan_history(3)  # Assuming action 3 is a relevant tracking step
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
            # validate-account (penny-drop) must run BEFORE bank-details: the server rejects
            # bank-details with "bank details is not verified" otherwise. It sets bank_account_verified,
            # which store_bank_details uses to send system- vs manual-verified flags.
            await self.validate_account()
            await self.store_bank_details()
            # Appraiser's own rating (submitted by the current appraiser session).
            await self.add_appraiser_rating()

            # Flow: bank details -> assign packet -> (BM rating if > 5L) -> upload docs -> ops rating.
            self._log_step("Assign Packet")
            await self.add_packet_images()   # admin create/assign + appraiser seal
            await self.update_loan_lock()
            await self.fetch_loan_stages()
            await self.fetch_applied_loan_details()

            self._log_step("BM Rating (only if loan amount > 5L)")
            await self.submit_bm_rating_if_required()  # BM login (per env), conditional on > 5L

            self._log_step("Upload Documents")
            await self.store_loan_documents()

            self._log_step("Manual Bank Verification (ops approval)")
            # The penny-drop leaves the bank detail "pending" because the test account's name does
            # not match the customer, so ops must approve it manually before the loan can disburse
            # (order taken from the real ops session: validate-account -> bank-verification-manual
            # -> ops-rating). The penny-drop result is already stored against the loan (the manual
            # approval echoes the same bankRRN), so validate-account is not re-run here.
            await self.ops_manual_bank_verification()

            self._log_step("Ops Rating (final approval)")
            await self.submit_ops_rating()   # Ops login (per env)
            await self.fetch_loan_stages()
            await self.fetch_applied_loan_details()

            self._log_step("Disbursement (partner login)")
            await self.disburse_amount()     # Partner login (per env + selected partner)
            await self.fetch_loan_stages()
            await self.fetch_applied_loan_details()

            self._log_step("Submit Packet (appraiser) — completes the loan")
            await self.submit_packet()       # runs as the appraiser
            await self.fetch_loan_stages()
            await self.fetch_applied_loan_details()

            self._log_step("Load Loan Details (search by loanUniqueId)")
            await self.fetch_loan_details()  # search + display the completed loan

            self._banner(
                "RESULT - PASSED",
                f"full GL process completed for login={login_type}",
            )
            # The closing "LOAN DETAILS" table (from fetch_loan_details) already shows the completed
            # loan's key attributes, so no separate identifiers dump is printed here.
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

    def _print_summary_table(self, title: str, details: dict) -> None:
        print("\n" + self._c(f"{self._G['square']} {title}", "bold", "cyan"))
        width = max((len(str(k)) for k in details), default=0)
        for key, value in details.items():
            val = "" if value in (None, "") else str(value)
            print(f"  {self._c(str(key).ljust(width), 'gray')}  {val}")

    def _print_run_metrics(self, passed: bool) -> None:
        total = self._api_calls
        failed = self._api_failures
        ok = total - failed
        verdict = self._c("PASSED", "green", "bold") if passed else self._c("FAILED", "red", "bold")
        rule = self._G["rule"] * self._WIDTH
        ok_txt = self._c(f"{self._G['ok']} {ok}", "green")
        fail_txt = self._c(f"{self._G['fail']} {failed}", "red" if failed else "gray")
        print("\n" + self._c(rule, "gray"))
        print(
            f"  API calls: {self._c(total, 'bold')}   {ok_txt}   {fail_txt}   "
            f"{self._G['pipe']}  verdict: {verdict}"
        )
        print(self._c(rule, "gray"))

        # List every failed endpoint so failures are visible at a glance at the end of the run.
        if self._failed_endpoints:
            print("\n" + self._c(f"{self._G['fail']} FAILED ENDPOINTS ({len(self._failed_endpoints)})", "red", "bold"))
            for call_no, method, path, status, reason, msg in self._failed_endpoints:
                print(self._c(
                    f"  {self._G['fail']} call #{call_no}  {method:<4} {status} {reason}  {path}",
                    "red",
                ))
                if msg:
                    print(self._c(f"       -> {msg}", "gray"))
            print(self._c(rule, "gray"))


def validate_cli(args, fail):
    """Reject argument combinations that cannot work. `fail` is argparse's error() (it exits).

    Kept out of main() so the offline tests can check these rules without running a flow.
    """
    if args.count < 1:
        fail("--count must be at least 1")
    releases_pan = args.kyc == "auto" and getattr(args, "release_kyc_docs", False)
    if args.count > 1 and args.kyc in ("auto", "ovd") and not args.customer and not releases_pan:
        # One real person's documents can only be registered against ONE customer, so a batch of
        # NEW customers cannot use a real-identity KYC mode (same rule as kyc_only.py --count).
        # With --customer no new customer is created, so the combination is fine there.
        # (auto: the Aadhaar is made unique per customer -- random 8 + real last 4 -- but the real
        # PAN has no such workaround; ovd: the real OVD number.)
        blocker = "PAN" if args.kyc == "auto" else "OVD"
        fail(f"--count {args.count} cannot be combined with --kyc {args.kyc}: the real "
             f"{blocker} in the identity profile can only be registered against one "
             "customer. Use --kyc manual for a batch, add --customer"
             + (", or add --release-kyc-docs (TEST/UAT) to free the PAN before each customer."
                if args.kyc == "auto" else "."))


def main():
    """Command-line entry point. Two ways to run the full Gold Loan journey:

      Brand-new customer (DEFAULT, no flags) -- create the customer, run KYC, then the loan flow:
          python flows/e2e_full_loan.py

      Existing customer -- reuse an already-KYC'd customer and just do a new loan against it:
          python flows/e2e_full_loan.py --customer MS35QNJP

    Either way the run goes end-to-end: (customer/KYC ->) appraiser request -> ornaments ->
    scheme/eligibility -> bank details -> assign packet -> ratings -> disbursement -> submit packet
    -> load loan details. Optional flags below choose the environment, customer, partner/scheme,
    co-lending, and loan amount.

    Both modes run on either environment -- `--env test` (gfat, default) or `--env uat` (gfau);
    the environment selects the base URL and every role/partner login mobile.
    """
    import argparse
    parser = argparse.ArgumentParser(
        description="Augmont Gold Loan end-to-end API test harness "
                    "(customer/KYC -> loan -> assign packet -> disburse -> submit packet).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python flows/e2e_full_loan.py                          # NEW customer -> full create + KYC -> loan\n"
               "  python flows/e2e_full_loan.py --customer MS35QNJP      # existing customer -> new loan\n"
               "  python flows/e2e_full_loan.py --partner arvog          # loan under the Arvog (ARV10) partner\n"
               "  python flows/e2e_full_loan.py --co-lender              # pick a co-lender from a menu\n"
               "  python flows/e2e_full_loan.py --co-lender DCB          # co-lending with a named bank\n"
               "  python flows/e2e_full_loan.py --scheme 853 --amount 550000\n"
               "  python flows/e2e_full_loan.py --env uat                # run the whole flow on UAT (gfau)\n",
    )
    parser.add_argument("--env", choices=sorted(GoldLoanApiTest.ENVIRONMENTS),
                        default=os.getenv("GOLD_LOAN_ENV", GoldLoanApiTest.DEFAULT_ENV).strip().lower(),
                        help="Target environment (default: %(default)s). 'test' = gfat, 'uat' = gfau. "
                             "Selects the base URL and every role/partner login mobile.")
    parser.add_argument("--customer", metavar="UNIQUE_ID",
                        help="Do a new loan against an EXISTING customer (by its customer unique id). "
                             "Omit this to CREATE A NEW customer and run the whole flow.")
    # A loan is underwritten by exactly ONE partner.
    parser.add_argument("--partner", choices=["roshan", "arvog"], default="roshan",
                        help="Lending partner for this loan (a loan has exactly one): 'roshan' "
                             "(Roshan Partner, ROS-152 - the proven one, default) or 'arvog' (Arvog, ARV-10).")
    parser.add_argument("--scheme", metavar="SCHEME_ID",
                        help="Pin a specific scheme id (e.g. 853). Omit to auto-pick within the partner.")
    parser.add_argument("--co-lender", metavar="NAME|ID", dest="co_lender",
                        nargs="?", const="__ASK__", default=None,
                        help="Apply co-lending. Give a bank name/id (e.g. 'DCB' or 4), OR pass "
                             "--co-lender with NO value to choose from a menu of available co-lenders. "
                             "Omit the flag entirely for NO co-lending.")
    parser.add_argument("--kyc", choices=list(GoldLoanApiTest.KYC_MODES),
                        default=os.getenv("GOLD_LOAN_KYC_MODE", GoldLoanApiTest.KYC_MODE_MANUAL),
                        help="How the customer's identity is established (default: %(default)s). "
                             "'manual' = generated dummy PAN/Aadhaar, no verification calls. "
                             "'auto' = REAL PAN + REAL offline-Aadhaar XML, verified via "
                             "/api/e-kyc/verify-pan and /api/e-kyc/offline-aadhaar-xml. "
                             "'ovd' = REAL Officially Valid Document instead of PAN (Voter ID or "
                             "Driving Licence) + Form 97, verified via /api/e-kyc/v2. "
                             "auto and ovd need real identity data -- see --kyc-profile.")
    parser.add_argument("--kyc-profile", metavar="PATH",
                        help="JSON file holding the REAL identity data for --kyc auto/ovd "
                             "(default: config/kyc_identity.json). Real numbers cannot be "
                             "generated, so they are supplied here or via GOLD_LOAN_KYC_* env vars.")
    parser.add_argument("--login", default="appraiser",
                        help="Login role for the main flow (default: appraiser).")
    parser.add_argument("--amount", type=float,
                        help="Requested loan amount (default 400000; capped at eligibility). "
                             "Over 500000 adds a BM-approval step.")
    parser.add_argument("--purpose-type", choices=sorted(GoldLoanApiTest.PURPOSE_TYPES) + ["any"],
                        default=os.getenv("GOLD_LOAN_PURPOSE_TYPE", "cb").strip().lower(),
                        help="Loan purpose type (default: %(default)s): 'cb' = Consumption Based, "
                             "'igl' = Income Generating (e.g. Agricultural Purposes), 'any' = either. "
                             "A random active purpose of that type is chosen.")
    parser.add_argument("--purpose", metavar="NAME",
                        help="Pin one loan purpose by name, e.g. \"Agricultural Purposes\" "
                             "(overrides --purpose-type; its own type is used).")
    parser.add_argument("--transfer-mode", choices=list(GoldLoanApiTest.TRANSFER_MODES),
                        default=(os.getenv("GOLD_LOAN_TRANSFER_MODE") or "neft").strip().lower(),
                        help="Disbursement bank transfer mode (default: %(default)s): the portal's "
                             "manual OFFLINE options NEFT / IMPS / RTGS, with a UTR. ICICI / RazorpayX "
                             "payouts are not used.")
    parser.add_argument("--release-kyc-docs", action="store_true",
                        help="TEST/UAT only: before KYC, free the identity profile's REAL PAN (and full "
                             "Aadhaar) from the KYC records (POST /api/e-kyc/remove), so --kyc auto can "
                             "register them on a new customer again. Allows --kyc auto with --count > 1.")
    parser.add_argument("--record-har", metavar="PATH",
                        help="Record every request/response of the run to a HAR 1.2 file at PATH "
                             "(tokens redacted), e.g. reference/gold-loan-e2e-flow.har.")
    parser.add_argument("--fresh-request", action="store_true",
                        help="If an appraiser request already exists for the customer, cancel its "
                             "loan and create a new request. Default is to RESUME the existing "
                             "request when it is still in progress.")
    parser.add_argument("--count", type=int, default=1, metavar="N",
                        help="Create N loans back to back in ONE logged-in session (default 1). "
                             "Every role logs in once for the whole batch, so the OTP endpoint's "
                             "one-a-minute rate limit is never hit. A loan that fails does not "
                             "stop the rest; the batch summary says which ones did.")
    args = parser.parse_args()

    validate_cli(args, parser.error)

    # Environment must be set before the suite is constructed -- it picks the base URL and the
    # login mobiles for every role.
    os.environ["GOLD_LOAN_ENV"] = args.env
    # KYC mode + identity profile are read by the constructor, so set them before it runs.
    os.environ["GOLD_LOAN_KYC_MODE"] = args.kyc
    if args.kyc_profile:
        os.environ["GOLD_LOAN_KYC_PROFILE"] = args.kyc_profile

    # Map the (single) partner choice to the name filter _fetch_partner_scheme_amount matches against.
    partner_env = {"roshan": "ROSHAN PARTNER", "arvog": "ARVOG"}[args.partner]
    os.environ["GOLD_LOAN_PARTNER_NAME"] = partner_env
    if args.amount:
        os.environ["GOLD_LOAN_AMOUNT"] = str(args.amount)
    if args.scheme:
        os.environ["GOLD_LOAN_SCHEME_ID"] = args.scheme
    if args.fresh_request:
        os.environ["GOLD_LOAN_FRESH_APPRAISER_REQUEST"] = "true"
    os.environ["GOLD_LOAN_PURPOSE_TYPE"] = args.purpose_type
    os.environ["GOLD_LOAN_TRANSFER_MODE"] = args.transfer_mode
    if args.record_har:
        os.environ["GOLD_LOAN_HAR_PATH"] = args.record_har
    if args.release_kyc_docs:
        os.environ["GOLD_LOAN_RELEASE_KYC_DOCS"] = "true"
    if args.purpose:
        os.environ["GOLD_LOAN_PURPOSE"] = args.purpose

    suite = GoldLoanApiTest()
    suite.existing_customer_id = ""
    if args.customer:
        # Existing-customer loan: resolve the customer, reuse approved KYC, do a new loan.
        suite.existing_customer_unique_id = args.customer
        print(f">> Mode: EXISTING customer {args.customer} — reuse KYC, do a new loan.")
    else:
        # Default: clear existing-customer config so run_e2e_test creates a new customer + runs KYC.
        suite.existing_customer_unique_id = ""
        print(">> Mode: NEW customer — full create -> KYC -> loan -> submit-packet.")

    # Co-lending: a concrete name/id is used directly; bare --co-lender defers to an in-flow menu.
    if args.co_lender == "__ASK__":
        suite.co_lender_interactive = True
        co_lender_label = "menu (choose during run)"
    elif args.co_lender:
        suite.co_lender_choice = args.co_lender
        co_lender_label = args.co_lender
    else:
        co_lender_label = "off"
    print(f">> Environment: {args.env.upper()} ({suite.BASE_URL})")
    print(f">> KYC mode: {args.kyc}"
          + ("  (real identity data)" if args.kyc != GoldLoanApiTest.KYC_MODE_MANUAL
             else "  (generated dummy data)"))
    print(f">> Partner: {args.partner} ({partner_env})"
          + (f"  |  scheme: {args.scheme}" if args.scheme else "")
          + f"  |  co-lending: {co_lender_label}")
    print(f">> Purpose: {args.purpose!r} (pinned)" if args.purpose
          else f">> Purpose: random, type={args.purpose_type}")

    if args.count > 1:
        who = (f"the SAME customer {args.customer}" if args.customer else "a NEW customer each")
        print(f">> Batch: {args.count} loans for {who}, in ONE session "
              "(one login per role for the batch).")
        try:
            passed = asyncio.run(suite.run_many(login_type=args.login, count=args.count))
        finally:
            suite.write_har()   # --record-har: written even when the run fails
        sys.exit(0 if passed else 1)
    try:
        asyncio.run(suite.run_e2e_test(login_type=args.login))
    finally:
        suite.write_har()   # --record-har: written even when the run fails


if __name__ == "__main__":
    main()