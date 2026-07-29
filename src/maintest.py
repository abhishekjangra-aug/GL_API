# src/main.py

import httpx
import hmac
import hashlib
import json
import random
import datetime
import os
import urllib.parse
import asyncio
import re  # Import regex module
import jwt  # Import jwt library for decoding
import mimetypes
import subprocess
from decimal import Decimal, ROUND_HALF_UP


class GoldLoanApiTest:

    # --- Environment profiles -----------------------------------------------------------------
    # The harness runs against either TEST (gfat) or UAT (gfau). EVERYTHING that differs between
    # the two lives here -- the API host and the login mobiles of every role the flow hands the
    # loan to. Pick with `--env {test|uat}` or GOLD_LOAN_ENV (default: test).
    #   role_mobiles         -- internal staff logins (static OTP 1234).
    #   partner_mobiles      -- partner login used for approval + disbursement, keyed by partner id
    #                           (Roshan Partner = 152, Arvog = 10).
    #   partner_user_mobiles -- partner-BRANCH user who receives the packet at submit-packet, keyed
    #                           by the same partner id. Test only has Roshan's captured.
    # Anything else that is environment-specific (packet location id, partner branch id) is
    # resolved live from the API in submit_packet rather than pinned per environment.
    ENVIRONMENTS = {
        "test": {
            "base_url": "https://gold-loan-backend-api.gfat.augmont.com",
            "role_mobiles": {"admin": "8880008880", "appraiser": "8880008881",
                             "ops": "8880008882", "bm": "8880008883"},
            "partner_mobiles": {"152": "8767002003", "10": "9375876473"},
            "partner_user_mobiles": {"152": "8652849318",   # roshan ghadge (id 170)
                                     "10": "8899999999"},   # arvog branch user
        },
        "uat": {
            "base_url": "https://gold-loan-backend-api.gfau.augmont.com",
            "role_mobiles": {"admin": "9990009990", "appraiser": "9990009991",
                             "ops": "9990009995", "bm": "9990009993"},
            "partner_mobiles": {"152": "8767002002", "10": "8846651348"},  # ROSHAN PARTNER / ARVOG ARV10
            "partner_user_mobiles": {"152": "8652849318",   # ROSHAN GHADGE
                                     "10": "8888888880"},   # ARVOG Qa Test
        },
    }
    DEFAULT_ENV = "test"

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
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
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
        self.appraiser_id = ''
        self.appraiser_request_id = ''
        self.master_loan_id = ''
        self.loan_id = ''
        self.reference_code = ''
        self.random_pincode = ''
        self.lead_source = 'Abhi testAppraiser'
        self.status_id = ''  # For customer creation status
        self.nominee_relation_id = ''
        self.ornament_type_id = ''
        self.karat_id = ''
        self.purpose_id = ''
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
        self.secured_exposure = "0"
        self.secured_rebate_interest = 0
        self.loan_start_date = ""
        self.loan_end_date = ""
        self.max_loan_limit = 0.0  # From loan-process/max-loan-limit
        self.available_packet = {}  # From packet/available-packet
        self.lead_converter_id = ''  # From lead/lead-converter
        self.karza_account_details = {}  # From loan-process/account-details-karza
        self.bank_account_number = ''  # Disbursement bank account details
        self.bank_ifsc_code = ''
        self.bank_name = ''
        self.bank_branch_name = ''
        self.account_holder_name = ''
        self.passbook_proofs = []  # Uploaded passbook proof paths
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

    def _log_step(self, title: str) -> None:
        self._step_no += 1
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
                err_msg = (response.text or "")[:200]
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
            (7, 0, 4, 6, 1, 3, 5, 8, 2, 9),
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
        if method.upper() == 'GET':
            response = await self.client.get(api_path, headers=headers, params=params)
        elif method.upper() == 'POST':
            response = await self.client.post(api_path, json=json_data, headers=headers, files=files)
        elif method.upper() == 'PUT':
            response = await self.client.put(api_path, json=json_data, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        if response.status_code == 401:
            raise httpx.HTTPStatusError(
                f"401 Unauthorized for {api_path}. The login token was rejected; re-run to log in again.",
                request=response.request,
                response=response,
            )
        response.raise_for_status()
        return response

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
        api_path = "/api/customer/verify-otp"
        request_body = {
            "otp": "1234",  # Assuming a static OTP for testing
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


    async def _send_register_otp(self):
        self.mobile_number = self._generate_random_mobile_number()
        api_path = "/api/customer/send-register-otp"
        request_body = {"mobileNumber": self.mobile_number}
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        response_body = response.json()
        self.reference_code = response_body.get('referenceCode')
        assert self.reference_code, f"Reference code not found in send-register-otp response: {response_body}"
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

        print(f"Resolved customerUniqueId for customer {self.customer_id}.")

    async def initiate_kyc(self) -> dict:
        api_path = f"/api/kyc/initiate/{self.customer_id}"
        response = await self._make_authenticated_request(
            'PUT',
            api_path,
            json_data={}
        )
        print(f"KYC initiated for customer {self.customer_id}.")
        return response.json()

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
        print(f"Fetched customer {self.customer_id} (kycStatus={self.kyc_status or 'unknown'}).")
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
        if self.customer_id and (
            not self.mobile_number or not self.first_name or not self.last_name
        ):
            await self.get_customer_by_id()
        if self.customer_unique_id and (
            not self.customer_id or not self.mobile_number or not self.first_name or not self.last_name
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
        assert self.mobile_number and self.first_name and self.last_name, (
            "Customer lookup did not return the mobile number and full name required for loan processing."
        )
        if not self.customer_unique_id:
            await self._fetch_customer_unique_id()
        print(f"Using existing customer {self.customer_id} for loan processing.")

    async def _safe_initiate_kyc(self) -> None:
        """initiate_kyc, tolerating an already-submitted/initiated KYC for existing customers."""
        try:
            await self.initiate_kyc()
        except httpx.HTTPStatusError as e:
            text = (e.response.text or "").lower()
            if e.response.status_code in (400, 404) and (
                "already submitted" in text or "already initiated" in text
            ):
                print(f"KYC for customer {self.customer_id} is already submitted/initiated. Continuing.")
            else:
                raise

    async def _run_full_kyc(self) -> None:
        """The complete KYC submission flow (KYC v2), used for new customers and not-yet-approved
        existing ones. Mirrors the captured v2 flow: get-customer-detail -> submit-basic-info ->
        consent OTP -> customer-kyc-address -> customer-kyc-personal -> submit-all-kyc-info ->
        ops-team approval. (No PUT /kyc/initiate and no PAN/Aadhaar auto-verification in v2.)"""
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
        random_names = self._generate_random_name()
        self.first_name = random_names['firstName']
        self.last_name = random_names['lastName']
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

        # The _send_register_otp method already handles sending OTP and verifying it
        # for customer registration.
        await self._send_register_otp()
        await asyncio.sleep(3)  # Wait for OTP verification to complete

        request_body = {
            "firstName": self.first_name,
            "lastName": self.last_name,
            "mobileNumber": self.mobile_number, # Added mobileNumber to request body
            "referenceCode": self.reference_code,
            "panCardNumber": None,
            "stateId": self.state_id,
            "cityId": self.city_id,
            "statusId": self.status_id,
            "comment": None,
            "pinCode": self.random_pincode,
            "source": None,
            "panType": None,
            "panImage": None,
            "leadSource": self.lead_source,
            "moduleId": self.module_id,
            "form60Image": None,
            "email": f'{self.first_name.lower()}.{self.last_name.lower()}@example.com',
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


    async def submit_basic_info(self):
            # KYC v2: submit-basic-info replaces the old /api/kyc/customer-info. It records the PAN/
            # basic details and returns the customerKycId (creating the KYC record for a fresh
            # customer). PAN auto-verification (/api/e-kyc/verify-pan) is skipped -- it 503s in the
            # captured flow and the manual path continues with isPanVerified=false.
            api_path = "/api/kyc/v2/submit-basic-info"
            if not self.random_pan:
                self.random_pan = self._generate_random_pan()
            if not self.pan_type:
                self.pan_type = os.getenv("GOLD_LOAN_PAN_TYPE", "pan").lower()
            if self.pan_type not in {"pan", "form60"}:
                raise ValueError("GOLD_LOAN_PAN_TYPE must be either 'pan' or 'form60'.")

            if self.pan_type == "pan" and not self.pan_image:
                upload_result = await self._upload_file(
                    "lead", file_type="image", file_path_override=self.PAN_IMAGE_PATH)
                self.pan_image = upload_result['uploadFile']['path']
                self.form60_image = None
            elif self.pan_type != "pan" and not self.form60_image:
                upload_result = await self._upload_file("lead", file_type="pdf")
                self.form60_image = upload_result['uploadFile']['path']
                self.pan_image = None

            request_body = {
                "id": int(self.customer_id) if self.customer_id and str(self.customer_id).isdigit() else self.customer_id,
                "moduleId": str(self.module_id),
                "firstName": self.first_name,
                "lastName": self.last_name,
                "panType": self.pan_type,
                "panCardNumber": self.random_pan,
                "panImage": self.pan_image,
                "panImg": f"{self.BASE_URL}/{self.pan_image}" if self.pan_image else None,
                "ovdImage": None,
                "ovdImg": None,
                "ovdNumber": None,
                "ovdType": None,
                "dateOfBirth": None,
                "isAutoApproved": False,
                "isPanVerified": False,
                "isOvdVerified": False,
                "kycType": self.kyc_type,
            }
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
        self.occupation_id = str(
            self.random.choice(low_risk_occupations)["id"]
        )
        print(f"Fetched random low-risk occupation_id: {self.occupation_id}")


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
        dob_and_age = self._generate_random_dob_and_age()
        self.age = int(dob_and_age['age'])
        self.dob = dob_and_age['dob']
        self.gender = self.random.choice(["m", "f", "o"])
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
            "dateOfBirth": self.dob,
            "age": str(self.age),
            "moduleId": self.module_id,
            "userType": None,
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

        self.identity_proof_number = self._generate_random_aadhaar_number()
        # Do not encrypt file paths, only sensitive numbers
        self.encrypted_identity_proof_number = self._encrypt_identity_proof_number(
            self.identity_proof_number
        )
        same_as_permanent = self.random.choice([True, False])
        self.name_as_per_aadhaar = f"{self.first_name} {self.last_name}"

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
                "identityTypeId": 5,
                "identityProof": [enc(self.masked_identity_proof)] if self.masked_identity_proof else [],
                "unMaskedIdentityProof": [enc(self.unmasked_identity_proof)] if self.unmasked_identity_proof else [],
                "identityProofImg": [enc(f"{self.BASE_URL}/{self.masked_identity_proof}")] if self.masked_identity_proof else [],
                "identityProofFileName": [enc(self.masked_identity_proof)] if self.masked_identity_proof else [],
                "identityProofNumber": self.encrypted_identity_proof_number,
                "isAutoApproved": False,
                "nameAsPerAadhaar": self.name_as_per_aadhaar,
                "file": None,
                "xmlFileName": None,
                "address": v2_addresses,
                "latitude": self.latitude,
                "longitude": self.longitude,
                "isCityEdit": False,
                "isAahaarVerified": False,
                "kycType": self.kyc_type,
            }
            await self._make_authenticated_request('POST', "/api/kyc/v2/customer-kyc-address", json_data=v2_body)
            print('Submitted customer-kyc-address (v2).')
        except httpx.HTTPStatusError as e:
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
            {
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
            }
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
            "identityTypeId": 5,
            "identityProof": [self._encrypt_identity_proof_number(self.masked_identity_proof)] if self.masked_identity_proof else [],
            "unMaskedIdentityProof": [self._encrypt_identity_proof_number(self.unmasked_identity_proof)] if self.unmasked_identity_proof else [],
            "identityProofNumber": self.encrypted_identity_proof_number,
            "panCardNumber": self.random_pan,
            "nameAsPerAadhaar": self.name_as_per_aadhaar,
            "riskCategory": "low",
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
            "identityTypeId": 5,
            "identityProof": [self.masked_identity_proof],
            "unMaskedIdentityProof": [self.unmasked_identity_proof],
            "identityProofFileName": None,
            "identityProofNumber": self.identity_proof_number,
            "userType": None,
            "organizationTypeId": None,
            "dateOfIncorporation": None,
            "form60Image": self.form60_image,
            "form60Img": f"{self.BASE_URL}/{self.form60_image}" if self.form60_image else None,
            "isCityEdit": None,
            "geoAddress": "",
            "nameAsPerAadhaar": self.name_as_per_aadhaar,
            "riskCategory": "low",
        }

        request_body = {
            "customerId": int(self.customer_id) if self.customer_id else None, # Convert to int
            "customerKycId": int(self.customer_kyc_id) if self.customer_kyc_id else None, # Convert to int
            "customerKycPersonal": kyc_personal,
            "customerKycAddress": kyc_address,
            "customerKycBasicDetails": kyc_basic_details,
            "moduleId": int(self.module_id) if self.module_id else None, # Convert to int
            "userType": None,
            "customerOrganizationDetail": None,
            "isCityEdit": None,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        print('Submit all KYC info successful.')


    async def kyc_ops_approval(self):
        api_path = "/api/classification/ops-team"
        request_body = {
            "customerId": int(self.customer_id) if self.customer_id else None, # Convert to int
            "customerKycId": int(self.customer_kyc_id) if self.customer_kyc_id else None, # Convert to int
            "kycRatingFromBM": False,
            "kycStatusFromOperationalTeam": "approved",
            "reasonFromOperationalTeam": "",
            "moduleId": int(self.module_id) if self.module_id else None, # Convert to int
            "userType": "Individual",  # per captured ops-team approval
        }
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        print('KYC ops approval successful.')


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
        else:
            # Fallback to random if not found or not logged in as specific appraiser
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

    async def _fetch_existing_appraiser_request(self) -> None:
        api_path = (
            "/api/appraiser-request/view-all?from=1&to=25&search="
            f"{urllib.parse.quote(self.customer_unique_id)}"
        )
        response = await self._make_authenticated_request('GET', api_path)
        data = response.json().get("data") if isinstance(response.json(), dict) else None
        items = data if isinstance(data, list) else []

        # Match by the item's *own* customer, then use the item-level id (the appraiser request id).
        # NOTE: item["customer"]["id"] is the CUSTOMER id, not the request id — do not use it here.
        match = None
        for item in items:
            if not isinstance(item, dict):
                continue
            customer = item.get("customer") if isinstance(item.get("customer"), dict) else {}
            uid = str(customer.get("customerUniqueId") or item.get("customerUniqueId") or "")
            cid = str(item.get("customerId") or customer.get("id") or "")
            if (self.customer_unique_id and uid == str(self.customer_unique_id)) or \
               (self.customer_id and cid == str(self.customer_id)):
                match = item
                break

        assert match and match.get("id"), (
            f"No existing appraiser request found for {self.customer_unique_id}."
        )
        self.appraiser_request_id = str(match["id"])
        # Capture the request/loan state so the caller can decide resume-vs-recreate.
        self._existing_request_status = str(match.get("status") or "").lower()
        self._existing_process_complete = bool(match.get("isProcessComplete"))
        ml = match.get("masterLoan") if isinstance(match.get("masterLoan"), dict) else {}
        self._existing_loan_completed = bool(ml.get("isLoanCompleted"))
        # If a loan was already initiated for this request, capture its ids so we can skip re-init.
        captured = self._extract_master_loan_ids(match.get("masterLoan"))
        print(
            f"Using existing appraiser request {self.appraiser_request_id} "
            f"(status={self._existing_request_status or 'unknown'}, "
            f"processComplete={self._existing_process_complete}, loanCompleted={self._existing_loan_completed})"
            + (f" — existing loan {self.loan_id}/{self.master_loan_id}" if captured else "")
            + "."
        )



    async def create_appraiser_request(self):
        api_path = "/api/appraiser-request"
        await self._fetch_appraisers()

        request_body = {
            "id": None,
            "customerId": int(self.customer_id) if self.customer_id else None, # Convert to int
            "customerName": f'{self.first_name} {self.last_name}',
            "customerUniqueId": self.customer_unique_id,
            "mobileNumber": self.mobile_number,
            "moduleId": int(self.module_id) if self.module_id else None, # Convert to int
            "internalBranchId": int(self.internal_branch_id) if self.internal_branch_id else None, # Convert to int
            "appraiserId": int(self.appraiser_id) if self.appraiser_id else None, # Convert to int
            "loanType": "Fresh Loan",  # From collection example
            "trackProccesingTime": True,
        }
        # Create the appraiser request. If one already exists for the customer: a finished loan
        # (past the upload-documents stage) is left untouched; a loan still WITHIN the
        # upload-documents stage is deleted (cancelled) and a FRESH request is created (attempt 1).
        response = None
        for attempt in range(2):
            try:
                response = await self._make_authenticated_request('POST', api_path, json_data=request_body)
                if attempt > 0:
                    print("Created a fresh appraiser request after deleting the prior loan.")
                break
            except httpx.HTTPStatusError as e:
                text = e.response.text or ""
                if e.response.status_code == 400 and "already Exists" in text:
                    print(f"Appraiser request for customer {self.customer_unique_id} already exists — "
                          "deleting it and creating a new one.")
                    if attempt == 0:
                        # Delete the existing loan (frees the customer), then retry to create fresh.
                        await self._fetch_existing_appraiser_request()  # get loan ids to cancel
                        if self.loan_id and self.master_loan_id:
                            try:
                                await self._cancel_existing_loan()  # clears loan ids on success
                            except httpx.HTTPStatusError as ce:
                                print(f"Could not cancel existing loan: {ce.response.status_code} - {ce.response.text[:120]}")
                        self.loan_id = ''
                        self.master_loan_id = ''
                        continue  # retry the POST to create a fresh request
                    # Still rejected after deleting: server keeps the request; reuse it with a fresh loan.
                    print("Appraiser request persists after deleting the loan; reusing it with a fresh loan downstream.")
                    await self._fetch_existing_appraiser_request()
                    self.loan_id = ''
                    self.master_loan_id = ''
                    return
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
        try:
            await self._fetch_existing_appraiser_request()
        except AssertionError:
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


    async def _fetch_loan_purposes(self):
        api_path = "/api/purpose?search=&from=1&to=-1"
        response = await self._make_authenticated_request('GET', api_path)
        purposes = response.json().get('data')
        if isinstance(purposes, dict):
            # Test API may return a keyed object or nest the list one level down.
            purposes = next(
                (value for value in purposes.values() if isinstance(value, list)),
                list(purposes.values()),
            )
        assert purposes, "No loan purposes found."
        self.purpose_id = self.random.choice(purposes).get('name')
        print(f'Fetched random purpose: {self.purpose_id}')
        return self.purpose_id


    async def _find_and_set_existing_loan_ids(self):
        """
        Attempts to find existing loan_id and master_loan_id for the current customer
        if a loan has already been initiated.
        """
        if not self.customer_id:
            print("Cannot find existing loan IDs: customer_id is not set.")
            return

        # Attempt 1: Try to find loan details from the appraiser requests list
        api_path = (
            "/api/appraiser-request/view-all?from=1&to=25&search="
            f"{urllib.parse.quote(self.customer_unique_id)}"
        )
        try:
            response = await self._make_authenticated_request('GET', api_path)
            data = response.json()
            items = data.get('data', [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                customer_dict = item.get("customer") if isinstance(item.get("customer"), dict) else {}
                uid = str(customer_dict.get("customerUniqueId") or "")
                cid = str(item.get("customerId") or customer_dict.get("id") or "")
                if uid == str(self.customer_unique_id) or (self.customer_id and cid == str(self.customer_id)):
                    if self._extract_master_loan_ids(item.get("masterLoan")):
                        print(f"Successfully retrieved existing loan IDs from appraiser request list: Loan ID={self.loan_id}, Master Loan ID={self.master_loan_id}")
                        return
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


    async def _cancel_existing_loan(self):
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
        if not self.age or not self.dob:
            dob_and_age = self._generate_random_dob_and_age()
            self.age = self.age or int(dob_and_age['age'])
            self.dob = self.dob or dob_and_age['dob']

        request_body = {
            "customerUniqueId": self.customer_unique_id,
            "mobileNumber": self.mobile_number,
            "panCardNumber": self.random_pan,
            "startDate": datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z'),  # ISO 8601 with Z
            "customerId": int(self.customer_id) if self.customer_id else None, # Convert to int
            "kycStatus": "approved",
            "purpose": purpose,
            "panType": self.pan_type,
            "loanId": None,
            "scrapId": None,
            "masterLoanId": None,
            "panImg": self.pan_image,
            "partReleaseId": None,
            "requestId": int(self.appraiser_request_id) if self.appraiser_request_id else None, # Convert to int
            "customerName": f'{self.first_name} {self.last_name}',
            "form60Img": self.form60_image,
            "age": str(self.age),
            "branchName": "Augmont",
            "branchDistance": None,  # Example from collection
            "gender": self.gender,
            "appraiserName": self.logged_in_appraiser_name,  # Use fetched appraiser name
            "tillDateOutstanding": "3122296.67",  # Example from collection
            "appraiserMobileNumber": self.logged_in_appraiser_mobile_number,  # Use fetched appraiser mobile
            "currentActiveLoans": 2,  # Example from collection
            "cpvImageFullUrl": f"{self.BASE_URL}/{self.DUMMY_PDF_PATH}",  # Placeholder
            "annualIncome": self.annual_income,
            "motherName": self.mother_name,
            "religionId": int(self.religion_id) if self.religion_id else None, # Convert to int
            "physicalChallengeId": int(self.physical_challenge_id) if self.physical_challenge_id else None, # Convert to int
            "occupationId": int(self.occupation_id) if self.occupation_id else None, # Convert to int
            "signatureProof": self.signature_proof,
            "signatureProofImg": f"{self.BASE_URL}/{self.signature_proof}",
            "signatureProofFileName": os.path.basename(self.signature_proof),
            "referenceCustomerNumber": None,
            "qualificationId": int(self.qualification_id) if self.qualification_id else None, # Convert to int
            "politicalExposedId": int(self.political_exposed_id) if self.political_exposed_id else None, # Convert to int
            "specialCategoryId": int(self.special_category_id) if self.special_category_id else None, # Convert to int
            "incomeGeneratingDocuments": None,
            "purposeType": "Consumption Based",  # Example from collection
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

        request_body = {
            "nomineeName": nominee_name,
            "nomineeAge": nominee_age_val,
            "relationship": nominee_relation,
            "mobileNumber": self._generate_random_mobile_number(),
            "referenceCode": None,
            "nomineeType": 'major' if nominee_age_val >= 18 else 'minor',
            "guardianName": "",
            "guardianAge": 30,
            "guardianRelationship": nominee_relation,  # valid lowercase relationshipName
            "checkPointers": {},
            "loanId": int(self.loan_id) if self.loan_id else None, # Convert to int if not empty
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None, # Convert to int if not empty
            "loanType": "Fresh Loan",
            "createdBy": str(self.logged_in_user_id) if self.logged_in_user_id else "3179",  # Use logged_in_user_id
            "internalBranchId": int(self.internal_branch_id) if self.internal_branch_id else None, # Convert to int
        }

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

    async def _fetch_karat_details(self):
        api_path = "/api/karat-details"  # From collection example, no query params
        response = await self._make_authenticated_request('GET', api_path)
        karats = response.json().get('data')
        assert karats, "No karat details found."
        
        # Filter karats to be greater than 20
        filtered_karats = []
        for k in karats:
            karat_value_str = k.get('karat')
            if karat_value_str and self._is_integer(karat_value_str):
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
            current_gold_rate = self._round_double(self.gold_rate, 2) if self.gold_rate else 6000.0

            ltv_range = karat_detail.get('ltvRange', [90])
            ltv_percent = str(self.random.choice(ltv_range))

            # netWtAfterPurity MUST equal what the server RECOMPUTES for eligibility, or final-loan-details
            # 400s with "total eligible ... doesn't match". Confirmed against the server's own stored
            # ornaments (single-loan on a mismatch): the server uses
            #     nwap = netWeight * min(purityReading, ltvPercent) / 100
            # i.e. it CAPS the assessed purity at the ornament's LTV%. Two runs pinned this exactly:
            # run A purity 81.46 < ltv 90 -> server kept 81.46; run B purity 91.67(karat) > ltv 90 ->
            # server capped to 90. So we compute purity from the karat (realistic) but cap it at
            # ltvPercent, and store that same value as purityReading so no further server capping
            # applies -- our sent nwap then equals the server's recomputed nwap to the paisa.
            karat_value = self._extract_karat_value(self.karat_id) or 22
            karat_purity = karat_value / 24 * 100  # e.g. 22K -> 91.67%
            try:
                ltv_pct_num = float(ltv_percent)
            except (TypeError, ValueError):
                ltv_pct_num = karat_purity
            purity_percentage = self._round_half_up(min(karat_purity, ltv_pct_num), 2)  # capped at LTV%
            # Round HALF-UP like the server: on a half-cent tie (e.g. 31.65*0.90=28.485) Python's
            # round() gives 28.48 but the server recomputes 28.49, and it validates eligibility against
            # ITS value -> final-loan-details 400. See _round_half_up.
            net_wt_after_purity = self._round_half_up(net_weight * (purity_percentage / 100), 2)

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
                "currentLtvAmount": current_gold_rate * net_wt_after_purity,
                "ornamentImageData": f'{self.BASE_URL}/{ornament_image}',
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
                "approxPurityReading": str(purity_percentage), # Store purity
                "scrapAmount": None,
                "purityReading": str(purity_percentage), # Store purity
                "customerConfirmation": None,
                "finalScrapAmountAfterMelting": None,
                "processingCharges": None,
                "packetId": None,
                "netWtAfterPurity": str(net_wt_after_purity),
                "remark": None,
                "isReleased": None,
            }
            loan_ornaments.append(ornament)

        total_eligible_amt = sum(o['currentLtvAmount'] for o in loan_ornaments)

        request_body = {
            "loanOrnaments": loan_ornaments,
            "totalEligibleAmt": total_eligible_amt,
            "loanId": int(self.loan_id) if self.loan_id else None,
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            "fullAmount": 0,
            "checkPointers": {},  # From collection example
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
        # A user-requested co-lender (via --co-lender) wins; otherwise use the scheme's own mapping.
        self.co_lender_bank_id = (self._forced_co_lender_id
                                  or (str(scheme.get('coLenderBankId')) if scheme.get('coLenderBankId') else None))
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
                "loanType": "Fresh Loan",
                "createdBy": str(self.logged_in_user_id) if self.logged_in_user_id else "3179",
                "internalBranchId": int(self.internal_branch_id) if self.internal_branch_id else None,
                "coLenderBankId": int(self.co_lender_bank_id) if self.co_lender_bank_id else None,
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
            "paymentFrequency": "30 Days",  # Example from collection
            "totalFinalInterestAmt": None,
            "unsecuredInterestRate": None,
            "interestRate": self.interest_rate,  # scheme's real rate (from check-loan-type)
            "processingCharge": self.secured_processing_charge,
            "unsecuredSchemeId": None,
            "securedLoanAmount": self.final_loan_amount, # Use dynamic value
            "unsecuredLoanAmount": None,
            "isUnsecuredSchemeApplied": False,
            "unsecuredRebateInterest": None,
            "securedRebateInterest": None,
            "otherAmount": None,
            "loanTransferExtraAmount": None,
            "upfrontInterestAmount": 0,
            "topUpAmount": 0,
            "securedTopUpAmount": 0,
            "unsecuredTopUpAmount": 0,
            "manualCharges": [],
            "unsecuredRpg": None,
            "securedRpg": self.secured_rpg if self.secured_rpg else 4480,  # scheme rate-per-gram
            "unsecuredPartnerId": None,
            "unsecuredprocessingCharge": 0,
            "securedprocessingCharge": 0,
            "securedExposure": "0.13",  # Example from collection
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
            ornament_loan_amount = self._round_double(net_wt_after_purity * scheme_rpg, 2)
            ornaments_for_request.append({
                "id": ornament.get('id'),
                "loanAmount": ornament_loan_amount,
                "ornamentsCal": net_wt_after_purity,
                "rpg": scheme_rpg,
            })
            calculated_total += ornament_loan_amount

        self.total_eligible_amount = self._round_double(calculated_total, 2)
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
            "paymentFrequency": "30 Days",
            "totalFinalInterestAmt": self.total_final_interest_amt, # from generate-interest-table
            "unsecuredInterestRate": None,
            "interestRate": self.interest_rate,  # scheme's real rate (from check-loan-type)
            "processingCharge": self.secured_processing_charge,  # scheme's real processing charge
            "unsecuredSchemeId": None,
            "securedLoanAmount": self.final_loan_amount, # Use dynamic value
            "unsecuredLoanAmount": None,
            "isUnsecuredSchemeApplied": False,
            "unsecuredRebateInterest": 0,
            "securedRebateInterest": self.secured_rebate_interest,  # from generate-interest-table
            "otherAmount": None,
            "loanTransferExtraAmount": None,
            "upfrontInterestAmount": self.upfront_interest_amount,  # from check-loan-type
            "topUpAmount": 0,
            "securedTopUpAmount": 0,
            "unsecuredTopUpAmount": 0,
            "manualCharges": [],
            "unsecuredRpg": None,
            "securedRpg": self.secured_rpg, # Use dynamic value
            "unsecuredPartnerId": None,
            "unsecuredprocessingCharge": 0,
            "securedprocessingCharge": self.secured_processing_charge,  # scheme's real processing charge
            "securedExposure": str(self.secured_exposure),  # posExposureAgainstScheme (from check-loan-type)
        }

        request_body = {
            "manualCharges": [],
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
        bank_name = "STATE BANK OF INDIA"  # Example from collection
        account_number = "00000036150491589"  # Example from collection
        ifsc_code = "SBIN0011777"  # Example from collection
        account_holder_name = "LIC MUTUAL FUND"  # Example from collection
        bank_branch_name = "SBI CAPITAL MARKET BRANCH, MUMBAI"

        # Bank proof = cancelled cheque image (both proof slots use it).
        passbook_proof_1 = (await self._upload_file(
            "loan", file_type="image", file_path_override=self.CHEQUE_IMAGE_PATH))['uploadFile']['path']
        passbook_proof_2 = (await self._upload_file(
            "loan", file_type="image", file_path_override=self.CHEQUE_IMAGE_PATH))['uploadFile']['path']

        # Persist disbursement bank details so validate-account and loan-documents can reuse them.
        self.bank_name = bank_name
        self.bank_account_number = account_number
        self.bank_ifsc_code = ifsc_code
        self.account_holder_name = account_holder_name
        self.bank_branch_name = bank_branch_name
        self.passbook_proofs = [passbook_proof_1, passbook_proof_2]

        # "To Be Paid" = final loan amount MINUS the scheme's processing charge and upfront interest.
        # The server rejects bank-details with "To Be Paid amount is incorrect" if this doesn't match
        # its own computation. processingCharge/upfrontInterestAmount are captured from check-loan-type
        # (data.securedprocessingCharge / upfrontInterestAmount), e.g. proc = max(fixed, percent%).
        try:
            proc_charge = float(self.secured_processing_charge or 0)
        except (TypeError, ValueError):
            proc_charge = 0.0
        try:
            upfront_interest = float(self.upfront_interest_amount or 0)
        except (TypeError, ValueError):
            upfront_interest = 0.0
        final_amt = float(self.final_loan_amount or 0)
        to_be_paid = self._round_double(final_amt - proc_charge - upfront_interest, 2)

        request_body = {
            "paymentType": ["bank"],
            "finalScrapAmount": None,
            "bankName": bank_name,
            "accountNumber": account_number,
            "ifscCode": ifsc_code,
            "accountType": None,
            "customerName": f"{self.first_name} {self.last_name}",
            "paymentMultiSelect": {"multiSelect": ["bank"]},
            "accountHolderName": account_holder_name,
            "bankBranchName": "SBI CAPITAL MARKET BRANCH, MUMBAI",  # Example
            "passbookProof": [passbook_proof_1, passbook_proof_2],
            "passbookProofImage": [f"{self.BASE_URL}/{passbook_proof_1}", f"{self.BASE_URL}/{passbook_proof_2}"],
            "passbookProofImageName": [os.path.basename(passbook_proof_1), os.path.basename(passbook_proof_2)],
            "account": 1508,  # Example
            "detailsFor": "customer",
            "customerTransferBalance": 0,
            "internalBranchTransferBalance": 17882793.96,  # Example
            "internalBranchBlockedBalance": 11916017.72,  # Example
            "advanceBankTransfer": 0,
            "advanceBankTransactionId": None,
            "advanceCash": 0,
            "advanceCashTransactionId": [],
            "advanceCashTransactionImage": [],
            "advanceCashTransactionImageName": "",
            "advanceCashDeclarationId": [],
            "bTMoney": 0,
            "actualProcessingCharge": proc_charge,
            "toBePaid": to_be_paid,  # finalLoanAmount - processingCharge - upfrontInterest
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
            "appraiserChargesPercentage": 0,
            "actualCashGiven": 0,
            "availableLimitForDisbuserment": to_be_paid,
            "wavierAppraiserCharges": 0,
            "stampDutyCharges": 0,
            "stampDutyDefinitionType": None,
            "stampDutyRemarks": "",
            "totalLimitForCashDisbursal": 200000,  # Example
            "multiModeDisbursement": True,
            "checkPointers": {},  # From collection example
            "loanId": int(self.loan_id) if self.loan_id else None,
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            "loanType": "Fresh Loan",
            "createdBy": "3179",  # Placeholder
            "internalBranchId": int(self.internal_branch_id) if self.internal_branch_id else None, # Convert to int
        }

        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
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
        request_body = {
            "applicationFormForAppraiser": True,
            "goldValuationForAppraiser": True,
            "loanStatusForAppraiser": "approved",
            "commentByAppraiser": "auto approved",
            "applicationFormForBM": True,
            "goldValuationForBM": True,
            "loanStatusForBM": "approved",
            "commentByBM": None,
            "reasons": "Other",
            "applicationFormForOperatinalTeam": True,
            "goldValuationForOperatinalTeam": True,
            "applicationFormForPartner": False,
            "goldValuationForPartner": False,
            "loanStatusForOperatinalTeam": "approved",
            "loanStatusForPartner": "pending",
            "commentByPartner": "",
            "scrapStatusForAppraiser": None,
            "scrapStatusForBM": None,
            "scrapStatusForOperatinalTeam": "pending",
            "packetApprovalByOps": None,
            "commentByOpsForPacketApproval": "",
            "statusForRh": "pending",
            "commentByRh": "",
            "applicationFormForRh": False,
            "goldValuationForRh": False,
            "cKycNumber": None,
            "loanId": int(self.loan_id) if self.loan_id else None,
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            "loanAmountDigi": f"{float(self.final_loan_amount):.2f}" if self.final_loan_amount else "0.00",
        }
        response = await self._make_authenticated_request(
            'POST',
            api_path,
            json_data=request_body
        )
        print('Add Ops rating successful.')

    # 5-lakh threshold above which a Branch Manager approval (BM rating) is required.
    BM_RATING_THRESHOLD = 500000

    async def submit_bm_rating_if_required(self):
        """BM approval is only needed when the loan amount exceeds 5L. BM rating must be posted
        under the BM's own login (per-environment mobile, OTP 1234), so swap to a BM role token for
        just this call and restore the appraiser session afterwards."""
        amount = float(self.final_loan_amount) if self.final_loan_amount else 0.0
        if amount <= self.BM_RATING_THRESHOLD:
            print(f"BM rating skipped: loan amount {amount:.2f} <= {self.BM_RATING_THRESHOLD} threshold.")
            return
        print(f"BM rating required: loan amount {amount:.2f} > {self.BM_RATING_THRESHOLD} threshold.")
        saved_token = self.auth_token
        try:
            self.auth_token = await self._role_token("bm")
            await self.add_bm_rating()
        finally:
            self.auth_token = saved_token

    async def submit_ops_rating(self):
        """Ops rating (final approval) must be posted under the Ops login (per env, OTP 1234).
        Swap to an ops role token for just this call, then restore the appraiser session."""
        saved_token = self.auth_token
        try:
            self.auth_token = await self._role_token("ops")
            await self.add_ops_rating()
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
        unique = f"pac-{self.random.randint(10000000, 99999999)}"
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
        print(f"Fetched single loan for customerLoanId {self.loan_id} "
              f"(loanUniqueId={self.loan_unique_id or 'n/a'}).")
        return payload

    async def add_packet_images(self):
        # Packet CREATE + ASSIGN require ADMIN rights; the sealing (add-packet-images) runs as the
        # current appraiser. Swap to an admin token for just those two calls, then restore the
        # appraiser session. appraiserId for the assignment is the loan's current (appraiser) user.
        appraiser_id = self.logged_in_user_id
        saved_token = self.auth_token
        try:
            self.auth_token = await self._admin_token()
            await self.create_packet()             # POST /api/packet (admin) + resolve numeric id
            await self.assign_packet(appraiser_id)  # PUT /api/packet/{id} (admin) -> bind appraiser
        finally:
            self.auth_token = saved_token           # restore appraiser session for sealing
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
            "loanType": "Fresh Loan",  # Example
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
            return response.json()
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
            print(f"Fetched bank details for master loan {self.master_loan_id}.")
            return response.json()
        except httpx.HTTPStatusError as e:
            print(f"Fetch bank details failed (non-fatal): {e.response.status_code}")
            return {}

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
                self.bank_name = details.get("bankName") or self.bank_name
                self.bank_branch_name = details.get("branch") or details.get("bankBranchName") or self.bank_branch_name
            print(f"Fetched Karza account details for IFSC {ifsc}.")
            return data
        except httpx.HTTPStatusError as e:
            print(f"Fetch Karza account details failed (non-fatal): {e.response.status_code}")
            return {}


    # --- Disbursement / documents phase (from loan-flow collection) ---

    async def update_loan_lock(self):
        api_path = "/api/loan-process/update-loan-lock"
        loan_stage_id = int(self.loan_stage_id) if self.loan_stage_id and str(self.loan_stage_id).isdigit() else 8
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

    async def validate_account(self):
        api_path = "/api/loan-process/validate-account"
        await self.fetch_account_details_karza()
        # Passbook proof is a cheque IMAGE (PNG), matching the real call; penny-drop verifies by
        # account/IFSC, not the image. When run before store_bank_details, passbook_proofs is empty.
        passbook = self.passbook_proofs or [
            (await self._upload_file(
                "loan", file_type="image", file_path_override=self.CHEQUE_IMAGE_PATH))['uploadFile']['path']
        ]
        request_body = {
            "ifscCode": self.bank_ifsc_code or "SBIN0011777",
            "accountNumber": self.bank_account_number or "00000036150491589",
            "accountHolderName": self.account_holder_name or f"{self.first_name} {self.last_name}",
            "bankName": self.bank_name or "STATE BANK OF INDIA",
            "bankBranchName": self.bank_branch_name or "SBI CAPITAL MARKET BRANCH, MUMBAI",
            "passbookProof": passbook,
            "passbookProofImage": [f"{self.BASE_URL}/{p}" for p in passbook],
            "passbookProofImageName": [],
            "detailsFor": "customer",
            "detailsForId": int(self.customer_id) if self.customer_id else None,
        }
        try:
            response = await self._make_authenticated_request('POST', api_path, json_data=request_body)
            # Penny-drop result: data.bankTxnStatus True means the account really verified (CONFIRMED
            # for the SBI test account 00000036150491589), so bank-details goes in system-verified.
            # NOTE: the response's top-level "message" can read "Something went wrong" even on success
            # -- rely on bankTxnStatus, not the message. Falls back to manual only if this is False.
            txn = self._find_key_recursive(response.json(), 'bankTxnStatus')
            self.bank_account_verified = bool(txn)
            print(f"Validate account successful (bankTxnStatus={txn}).")
        except httpx.HTTPStatusError as e:
            self.bank_account_verified = False
            print(f"Validate account failed (non-fatal): {e.response.status_code} - {e.response.text}")

    async def upload_income_document(self) -> str:
        api_path = f"/api/upload-file?reason=customerIncomeGeneratingDocument&customerId={self.customer_id}"
        headers = {}
        content_type = mimetypes.guess_type(self.DUMMY_PDF_PATH)[0] or "application/octet-stream"
        with open(self.DUMMY_PDF_PATH, "rb") as upload_file:
            files = {"avatar": (os.path.basename(self.DUMMY_PDF_PATH), upload_file, content_type)}
            response = await self._make_authenticated_request('POST', api_path, files=files, headers=headers)
        data = response.json()
        path = data['uploadFile']['path']
        print(f"Income-generating document uploaded. Path: {path}")
        return path

    async def store_loan_documents(self):
        api_path = "/api/loan-process/loan-documents"

        # Upload the required loan documents (agreement, pawn copy, scheme confirmation).
        loan_agreement = (await self._upload_file("loan", file_type="pdf"))['uploadFile']['path']
        pawn_copy = (await self._upload_file("loan", file_type="pdf"))['uploadFile']['path']
        scheme_confirmation = (await self._upload_file("loan", file_type="pdf"))['uploadFile']['path']
        income_doc = await self.upload_income_document()

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
            "incomeGeneratingDocuments": None,
            "consumptionSupportingDocs": [{"path": income_doc}],
            "cKycNumber": c_kyc_number,
            "checkPointers": {},
            "loanId": int(self.loan_id) if self.loan_id else None,
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            "loanAmountDigi": f"{float(self.final_loan_amount):.2f}" if self.final_loan_amount else "0.00",
            "loanType": "Fresh Loan",
        }
        try:
            await self._make_authenticated_request('POST', api_path, json_data=request_body)
            print('Store loan documents successful.')
        except httpx.HTTPStatusError as e:
            print(f"Store loan documents failed (non-fatal): {e.response.status_code} - {e.response.text}")

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
        secured_amount = detail.get("securedLoanAmount") or f"{float(self.final_loan_amount):.2f}"
        request_body = {
            "applicationFormForAppraiser": True, "goldValuationForAppraiser": True,
            "commentByAppraiser": None,
            "applicationFormForBM": True, "goldValuationForBM": True, "reasons": "",
            "applicationFormForOperatinalTeam": True, "goldValuationForOperatinalTeam": True,
            "applicationFormForPartner": True, "goldValuationForPartner": True,
            "loanStatusForPartner": "approved",
            "scrapStatusForAppraiser": None, "scrapStatusForBM": None,
            "scrapStatusForOperatinalTeam": "pending",
            "packetApprovalByOps": None, "commentByOpsForPacketApproval": "",
            "statusForRh": "pending", "commentByRh": "",
            "applicationFormForRh": False, "goldValuationForRh": False,
            "loanId": int(self.loan_id) if self.loan_id else detail.get("securedLoanId"),
            "masterLoanId": int(self.master_loan_id) if self.master_loan_id else None,
            "loanAmountDigi": secured_amount,
            "loanType": "Fresh Loan",
            "partnerId": int(self.partner_id) if self.partner_id and str(self.partner_id).isdigit() else 152,
            "checkPointers": {},
        }
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

    async def _post_partner_disbursement(self, detail: dict):
        api_path = "/api/loan-process/partner-wise-disbursement"
        ubd = detail.get("userBankDetail", {}) if isinstance(detail, dict) else {}
        secured_amount = detail.get("securedLoanAmount") or f"{float(self.final_loan_amount):.2f}"
        final_amount = detail.get("finalLoanAmount") or float(self.final_loan_amount or 0)
        # Manually-entered bank UTR/reference (a random numeric string in the HAR).
        bank_txn_id = str(self.random.randint(10**14, 10**15 - 1))
        # First-month secured interest ~ actualProcessingCharge in the HAR (falls back to 0.00).
        first_interest = "0.00"
        if isinstance(self.interest_table_data, list) and self.interest_table_data:
            first_interest = str(self.interest_table_data[0].get("securedInterestAmount") or "0.00")
        bank_array = [{
            "disbursementStatus": "Disbursed to Customer",
            "ifscCode": ubd.get("ifscCode") or self.bank_ifsc_code,
            "bankName": ubd.get("bankName") or self.bank_name,
            "bankBranch": ubd.get("bankBranchName") or self.bank_branch_name,
            "accountHolderName": ubd.get("accountHolderName") or self.account_holder_name,
            "accountNumber": ubd.get("accountNumber") or self.bank_account_number,
        }]
        request_body = {
            "isNewLoanFromRenew": False, "isTopUpAdded": False,
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
            "masterLoanId": str(self.master_loan_id) if self.master_loan_id else detail.get("masterLoanId"),
            "securedSchemeName": detail.get("securedSchemeName"),
            "unsecuredLoanAmount": detail.get("unsecuredLoanAmount", 0),
            "unsecuredSchemeName": None,
            "securedLoanAmount": secured_amount,
            "securedLoanId": detail.get("securedLoanId") or (int(self.loan_id) if self.loan_id else None),
            "unsecuredLoanId": None, "scrapId": None, "scrapAmount": None, "transactionId": None,
            "fullSecuredAmount": detail.get("fullSecuredAmount", final_amount),
            "fullUnsecuredAmount": detail.get("fullUnsecuredAmount", 0),
            "processingCharge": detail.get("processingCharge", 0),
            "securedProcessingCharge": detail.get("securedProcessingCharge", "0.00"),
            "unsecuredProcessingCharge": detail.get("unsecuredProcessingCharge", 0),
            "isUnsecuredSchemeApplied": detail.get("isUnsecuredSchemeApplied", False),
            "securedLoanUniqueId": detail.get("securedLoanUniqueId"),
            "unsecuredLoanUniqueId": None,
            "finalAmount": final_amount, "fullAmount": final_amount,
            "bankTransferType": "neft",
            "loanTransferExtraAmount": None, "otherAmountTransactionId": None, "utrNumber": None,
            "totalManualCharges": detail.get("totalManualCharges", 0),
            "upfrontInterestAmount": detail.get("upfrontInterestAmount", 0),
            "upfrontTransactionId": None, "upfrontReceiptImage": None, "upfrontReceipt": None,
            "fullUnsecuredTopUpAmount": 0, "fullSecuredTopUpAmount": 0, "finalTopUpAmount": 0,
            "securedTopUpAmount": 0, "unsecuredTopUpAmount": 0,
            "advanceBankTransfer": 0, "advanceCash": 0, "bTMoney": 0,
            "penalInterest": 2,
            "advancedCashReceiptImage": None, "advanceCashDeclarationImage": None,
            "receiptNumber": None, "appraiserCharges": 0,
            "actualProcessingCharge": first_interest,
            "actualCashGiven": 0, "stampDutyCharges": "0.00", "stampDutyRemarks": "",
            "paymentGatewayId": 5,  # env constant from HAR (Roshan partner gateway)
            "partnerId": int(self.partner_id) if self.partner_id and str(self.partner_id).isdigit() else 152,
            "loanType": "Fresh Loan",
        }
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
        for candidate in branch_candidates:
            un = await self._make_authenticated_request(
                'GET', f"/api/packet-tracking/user-name?mobileNumber={partner_user_mobile}"
                       f"&receiverType=PartnerUser&partnerBranchId={candidate}"
                       f"&masterLoanId={self.master_loan_id}&allUsers=1")
            data = un.json().get("data") if isinstance(un.json(), dict) else None
            if isinstance(data, dict) and data.get("id"):
                partner_branch_id, un_data = candidate, data
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
            "receiverType": "PartnerUser",
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

    async def fetch_loan_details(self) -> dict:
        """Final step: search the loan-details list by the loan's unique id (AUGM-…) and display the
        completed loan's key attributes. Mirrors the UI loading the loan after submit-packet. Returns
        the matched loan record (or {})."""
        base = "/api/loan-process/loan-details?from=1&to=25"
        # First the plain list (as the UI does), then the targeted search by loanUniqueId.
        await self._make_authenticated_request('GET', base)
        api_path = f"{base}&loanUniqueId={self.loan_unique_id}" if self.loan_unique_id else base
        response = await self._make_authenticated_request('GET', api_path)
        rows = response.json().get("data", []) if isinstance(response.json(), dict) else []
        loan = rows[0] if rows else {}
        if not loan:
            print(f"Loan details: no loan found for loanUniqueId={self.loan_unique_id}.")
            return {}

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


    async def run_e2e_test(self, login_type: str):
        self._banner(
            "GOLD LOAN - END-TO-END API TEST",
            f"env={self.env_name.upper()}   login={login_type} "
            f"({self._get_mobile_number_for_login_type(login_type)})   "
            f"base={self.BASE_URL}   log={self.log_level}",
        )
        try:
            self._log_step("Authentication")
            if self.supplied_auth_token:
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


        except httpx.HTTPStatusError as e:
            self._banner("RESULT - FAILED (HTTP ERROR)", f"login={login_type}")
            print(self._c(f"  {e.request.method} {e.request.url}", "red", "bold"))
            print(self._c(f"  status : {e.response.status_code} {e.response.reason_phrase}", "red"))
            print(self._c(f"  body   : {e.response.text}", "red"))
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


def main():
    """Command-line entry point. Two ways to run the full Gold Loan journey:

      Brand-new customer (DEFAULT, no flags) -- create the customer, run KYC, then the loan flow:
          python src/maintest.py

      Existing customer -- reuse an already-KYC'd customer and just do a new loan against it:
          python src/maintest.py --customer MS35QNJP

    Either way the run goes end-to-end: (customer/KYC ->) appraiser request -> ornaments ->
    scheme/eligibility -> bank details -> assign packet -> ratings -> disbursement -> submit packet
    -> load loan details. Optional flags below choose the environment, customer, partner/scheme,
    co-lending, and loan amount.

    Both modes run on either environment -- `--env test` (gfat, default) or `--env uat` (gfau);
    the environment selects the base URL and every role/partner login mobile.
    """
    import argparse
    parser = argparse.ArgumentParser(
        prog="maintest.py",
        description="Augmont Gold Loan end-to-end API test harness "
                    "(customer/KYC -> loan -> assign packet -> disburse -> submit packet).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python src/maintest.py                          # NEW customer -> full create + KYC -> loan\n"
               "  python src/maintest.py --customer MS35QNJP      # existing customer -> new loan\n"
               "  python src/maintest.py --partner arvog          # loan under the Arvog (ARV10) partner\n"
               "  python src/maintest.py --co-lender              # pick a co-lender from a menu\n"
               "  python src/maintest.py --co-lender DCB          # co-lending with a named bank\n"
               "  python src/maintest.py --scheme 853 --amount 550000\n"
               "  python src/maintest.py --env uat                # run the whole flow on UAT (gfau)\n",
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
    parser.add_argument("--login", default="appraiser",
                        help="Login role for the main flow (default: appraiser).")
    parser.add_argument("--amount", type=float,
                        help="Requested loan amount (default 400000; capped at eligibility). "
                             "Over 500000 adds a BM-approval step.")
    args = parser.parse_args()

    # Environment must be set before the suite is constructed -- it picks the base URL and the
    # login mobiles for every role.
    os.environ["GOLD_LOAN_ENV"] = args.env

    # Map the (single) partner choice to the name filter _fetch_partner_scheme_amount matches against.
    partner_env = {"roshan": "ROSHAN PARTNER", "arvog": "ARVOG"}[args.partner]
    os.environ["GOLD_LOAN_PARTNER_NAME"] = partner_env
    if args.amount:
        os.environ["GOLD_LOAN_AMOUNT"] = str(args.amount)
    if args.scheme:
        os.environ["GOLD_LOAN_SCHEME_ID"] = args.scheme

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
    print(f">> Partner: {args.partner} ({partner_env})"
          + (f"  |  scheme: {args.scheme}" if args.scheme else "")
          + f"  |  co-lending: {co_lender_label}")

    asyncio.run(suite.run_e2e_test(login_type=args.login))


if __name__ == "__main__":
    main()