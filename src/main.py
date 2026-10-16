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
import re # Import regex module
import jwt # Import jwt library for decoding

class GoldLoanApiTest:
    def __init__(self):
        # --- Configuration ---
        self.BASE_URL = "https://gold-loan-backend-api.gfau.augmont.com"
        # HMAC_SECRET provided by the user
        self.HMAC_SECRET = "3056301006072a8648ce3d020106052b8104000a0342000499c5f442c3264bcdfb093b0bc820e3f0f6546972856ebec2f8ccc03f49abdb47ffcfcaf4f37e0ec53050760e74014767e30a8a3e891f4db8c83fa27627898f15"

        self.DUMMY_IMAGE_PATH = "./dummy_image.png" # Relative to project root

        # --- Global Variables to store dynamic data ---
        self.auth_token = "" # Will be obtained after login
        self.customer_id = ''
        self.mobile_number = ''
        self.module_id = ''
        self.state_id = ''
        self.city_id = ''
        self.first_name = ''
        self.last_name = ''
        self.customer_kyc_id = ''
        self.file_path = '' # For uploaded files
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
        self.profile_image = ''
        self.signature_proof = ''
        self.age = 0
        self.dob = ''
        self.gender = ''
        self.martial_status = ''
        self.spouse_name = ''
        self.mother_name = ''
        self.identity_proof_number = ''
        self.name_as_per_aadhaar = ''
        self.masked_identity_proof = ''
        self.unmasked_identity_proof = ''
        self.address_proof = ''
        self.address_proof_type_id = ''
        self.customer_details = {} # To store the full customer KYC review object
        self.appraiser_id = ''
        self.appraiser_request_id = ''
        self.master_loan_id = ''
        self.loan_id = ''
        self.reference_code = ''
        self.random_pincode = ''
        self.lead_source = 'API Test Automation'
        self.status_id = '' # For customer creation status
        self.nominee_relation_id = ''
        self.ornament_type_id = ''
        self.karat_id = ''
        self.purpose_id = ''
        self.scheme_id = ''
        self.partner_id = ''
        self.co_lender_bank_id = ''
        self.loan_stage_id = ''
        self.augmont_branch_id = '' # New variable for dynamically fetched branch ID

        self.random = random.Random() # Initialize random object

        # Ensure dummy image exists for file uploads
        if not os.path.exists(self.DUMMY_IMAGE_PATH):
            try:
                with open(self.DUMMY_IMAGE_PATH, "w") as f:
                    f.write("dummy image content")
                print(f"Created {self.DUMMY_IMAGE_PATH} for file uploads.")
            except IOError as e:
                print(f"Could not create {self.DUMMY_IMAGE_PATH}: {e}")
                raise Exception(f"Failed to create {self.DUMMY_IMAGE_PATH}, which is required for file uploads.")

    def _generate_signature(self, api_path: str, request_body: dict = None) -> str:
        if request_body is None:
            request_body = {}

        # Match the Postman pre-request script: full URL including query string.
        url_for_hmac = f"{self.BASE_URL}{api_path}"
        url_for_hmac = url_for_hmac.replace("%20", " ")
        url_for_hmac = re.sub(r'//+', '/', url_for_hmac)
        url_for_hmac = re.sub(r':/([^/])', r'://\1', url_for_hmac)

        final_payload = {}
        if request_body:
            final_payload.update(request_body)

        final_payload["url"] = url_for_hmac

        hmac_string = json.dumps(final_payload, separators=(',', ':'))
        print(f"DEBUG: HMAC String for signature: {hmac_string}")

        # As per Javascript CryptoJs.enc.Utf8.parse(firebaseKey)
        # If firebaseKey is a hex string, it's often implicitly converted to bytes from hex.
        # Reverting to bytes.fromhex() for the key.
        try:
            decoded_hmac_secret = bytes.fromhex(self.HMAC_SECRET)
        except ValueError as e:
            print(f"ERROR: Could not decode HMAC_SECRET from hex. Ensure it's a valid hex string. Error: {e}")
            raise

        print(f"DEBUG: HMAC Secret (decoded from hex, first 10 bytes): {decoded_hmac_secret[:10]}...")

        hashed = hmac.new(
            decoded_hmac_secret,
            hmac_string.encode('utf-8'), # Data is UTF-8 encoded as per Javascript CryptoJs.enc.Utf8.parse(hmacupdate)
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
        fourth_char = 'P' # Type of holder, 'P' for individual
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
        return ''.join(self.random.choice('0123456789') for _ in range(12))

    def _generate_random_address_proof_number(self, length: int = 12) -> str:
        chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
        return ''.join(self.random.choice(chars) for _ in range(length))

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

    # --- API Call Methods ---

    async def login(self):
        # Step 1: Send OTP
        api_path_send_otp = "/api/user-otp/user-send-otp"
        login_mobile_number = "9990009991" # Use a valid test user mobile number (Appraiser role from curl)
        request_body_send_otp = {
            "mobileNumber": login_mobile_number,
            "type": "login",
            "id": None
        }
        signature_send_otp = self._generate_signature(api_path_send_otp, request_body_send_otp)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path_send_otp} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'signature': signature_send_otp}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body_send_otp, indent=2)}")
        async with httpx.AsyncClient() as client:
            response_send_otp = await client.post(
                self.BASE_URL + api_path_send_otp,
                json=request_body_send_otp,
                headers={
                    'Content-Type': 'application/json',
                    'signature': signature_send_otp,
                }
            )
            response_send_otp.raise_for_status()
            response_data_send_otp = response_send_otp.json()
            self.reference_code = response_data_send_otp.get('referenceCode')
            assert self.reference_code, f"Reference code not found in send-otp response: {response_data_send_otp}"
            print(f"OTP sent successfully. Reference Code: {self.reference_code}")
            print(f"Response Body: {json.dumps(response_data_send_otp, indent=2)}")

        # Step 2: Verify Login with OTP
        api_path_verify_login = "/api/auth/verify-login"
        # Assuming a static OTP for testing. Adjust if your test environment requires a real OTP.
        otp_to_verify = 1234
        request_body_verify_login = {
            "referenceCode": self.reference_code,
            "otp": otp_to_verify,
            "type": "login",
            "isFromWeb": True
        }
        signature_verify_login = self._generate_signature(api_path_verify_login, request_body_verify_login)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path_verify_login} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'signature': signature_verify_login}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body_verify_login, indent=2)}")
        async with httpx.AsyncClient() as client:
            response_verify_login = await client.post(
                self.BASE_URL + api_path_verify_login,
                json=request_body_verify_login,
                headers={
                    'Content-Type': 'application/json',
                    'signature': signature_verify_login,
                }
            )
            response_verify_login.raise_for_status()
            response_data_verify_login = response_verify_login.json()
            self.auth_token = response_data_verify_login.get('Token')
            assert self.auth_token, f"Auth Token not found in verify-login response: {response_data_verify_login}"
            print(f"Login successful. Auth Token obtained: {self.auth_token[:30]}...") # Print first 30 chars for debug
            print(f"Response Body: {json.dumps(response_data_verify_login, indent=2)}") # Print full response

            # Decode JWT and print relevant fields
            try:
                decoded_jwt = jwt.decode(self.auth_token, options={"verify_signature": False})
                print(f"Decoded JWT Payload: {json.dumps(decoded_jwt, indent=2)}")
                print(f"  roleName: {decoded_jwt.get('roleName')}")
                print(f"  roleId: {decoded_jwt.get('roleId')}")
                print(f"  userTypeId: {decoded_jwt.get('userTypeId')}")
                print(f"  internalBranchId: {decoded_jwt.get('internalBranchId')}")
            except Exception as e:
                print(f"Error decoding JWT: {e}")

    async def _fetch_and_set_augmont_branch_id(self):
        api_path = "/api/user/get-my-branches"
        signature = self._generate_signature(api_path, request_body=None)

        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}")

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.BASE_URL + api_path,
                    headers=headers
                )
                response.raise_for_status()
                response_data = response.json()
                print(f"Response Body: {json.dumps(response_data, indent=2)}")

                branches = response_data.get('data', [])
                augmont_branch = next((b for b in branches if b.get('branchName') == 'Augmont'), None)

                assert augmont_branch, "Augmont branch not found in the list of branches."
                self.augmont_branch_id = str(augmont_branch['id'])
                print(f"Dynamically fetched Augmont Branch ID: {self.augmont_branch_id}")
        except Exception as e:
            print(f"Warning: Failed to fetch branches dynamically ({e}). Using fallback branch ID '108'.")
            self.augmont_branch_id = "108"


    async def change_user_branch(self, branch_id: str):
        api_path = f"/api/auth/change-user-branch?branchId={branch_id}"

        # --- OPTIONS Preflight Request ---
        options_headers = {
            "Host": "gold-loan-backend-api.gfau.augmont.com",
            "accept": "*/*",
            "access-control-request-method": "GET",
            "access-control-request-headers": "authorization,cache-control,pragma,signature",
            "origin": "https://ap.gfau.augmont.com",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "sec-fetch-dest": "empty",
            "referer": "https://ap.gfau.augmont.com/",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "priority": "u=1, i",
        }
        print(f"\n--- Request: OPTIONS {self.BASE_URL}{api_path} (Preflight) ---")
        print(f"Request Headers (OPTIONS): {json.dumps(options_headers, indent=2)}")
        async with httpx.AsyncClient() as client:
            options_response = await client.options(
                self.BASE_URL + api_path,
                headers=options_headers
            )
            options_response.raise_for_status()
            print(f"OPTIONS preflight successful. Status: {options_response.status_code}")
            print(f"Response Headers (OPTIONS): {json.dumps(dict(options_response.headers), indent=2)}")


        # --- Actual GET Request ---
        signature = self._generate_signature(api_path, request_body=None) # Explicitly pass None for request_body for GET

        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers (GET): {json.dumps(headers, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.get( # Changed to GET request
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            response_data = response.json()
            self.auth_token = response_data.get('Token')
            assert self.auth_token, f"New Auth Token not found in change-user-branch response: {response_data}"
            print(f"User branch changed successfully to {branch_id}. New Auth Token obtained: {self.auth_token[:30]}...")
            print(f"Response Body (GET): {json.dumps(response_data, indent=2)}")

            try:
                decoded_jwt = jwt.decode(self.auth_token, options={"verify_signature": False})
                print(f"Decoded JWT Payload after branch change: {json.dumps(decoded_jwt, indent=2)}")
                print(f"  roleName: {decoded_jwt.get('roleName')}")
                print(f"  roleId: {decoded_jwt.get('roleId')}")
                print(f"  userTypeId: {decoded_jwt.get('userTypeId')}")
                print(f"  internalBranchId: {decoded_jwt.get('internalBranchId')}")
            except Exception as e:
                print(f"Error decoding JWT after branch change: {e}")


    async def _send_otp_verification(self, ref_code: str):
        api_path = "/api/customer/verify-otp"
        request_body = {
            "otp": "1234", # Assuming a static OTP for testing
            "referenceCode": ref_code,
            "type": "lead",
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            assert response.status_code == 200, f"OTP Verification failed: {response.text}"
            print(f'OTP Verified Successfully: {json.dumps(response.json(), indent=2)}')

    async def _send_register_otp(self):
        self.mobile_number = self._generate_random_mobile_number()
        api_path = "/api/customer/send-register-otp"
        request_body = {"mobileNumber": self.mobile_number}
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            response_body = response.json()
            self.reference_code = response_body.get('referenceCode')
            assert self.reference_code, f"Reference code not found in send-register-otp response: {response_body}"
            print(f'OTP Sent. Reference Code: {self.reference_code}')
            print(f"Response Body: {json.dumps(response_body, indent=2)}")
            await self._send_otp_verification(self.reference_code)

    async def _fetch_states(self):
        api_path = "/api/state"
        signature = self._generate_signature(api_path, request_body=None) # GET request

        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            states = response.json().get('data')
            assert states, "No states found."
            self.state_id = str(self.random.choice(states).get('id'))
            print(f'Fetched random state_id: {self.state_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_cities(self, state_id: str):
        api_path = f"/api/city?stateId={state_id}"
        signature = self._generate_signature(api_path, request_body=None) # GET request
        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            cities = response.json().get('data')
            assert cities, "No cities found for the selected state."
            self.city_id = str(self.random.choice(cities).get('id'))
            print(f'Fetched random city_id: {self.city_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_modules(self):
        api_path = "/api/modules/appraiser-request-module?isFor=lead"
        signature = self._generate_signature(api_path, request_body=None) # GET request

        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            modules = response.json()
            gold_loan_module = next((item for item in modules if item.get('moduleName') == 'gold loan'), None)
            assert gold_loan_module, "Gold loan module not found."
            self.module_id = str(gold_loan_module.get('id'))
            print(f'Fetched module_id for "gold loan": {self.module_id}')
            print(f"Response Body: {json.dumps(modules, indent=2)}")

    async def _fetch_status(self):
        api_path = "/api/status"
        signature = self._generate_signature(api_path, request_body=None) # GET request

        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            statuses = response.json().get('data')
            assert statuses, "No statuses found."
            confirm_status = next((item for item in statuses if item.get('statusName') == 'confirm'), None)
            assert confirm_status, "Confirm status not found."
            self.status_id = str(confirm_status.get('id'))
            print(f'Fetched status_id for "confirm": {self.status_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _upload_file(self, reason: str, file_path_on_disk: str = None, document_type: str = None, is_mask: bool = False):
        if file_path_on_disk is None:
            file_path_on_disk = self.DUMMY_IMAGE_PATH

        if not os.path.exists(file_path_on_disk):
            raise FileNotFoundError(f"Dummy image file not found at: {file_path_on_disk}")

        api_path = f"/api/upload-file?customerId={self.customer_id}&reason={reason}"
        if document_type:
            api_path += f"&documentType={urllib.parse.quote(document_type)}"

        signature = self._generate_signature(api_path)

        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        if is_mask:
            headers['isMask'] = 'true'
        if document_type:
            headers['documentType'] = document_type

        # httpx handles multipart/form-data automatically with the 'files' parameter
        files = {'avatar': (os.path.basename(file_path_on_disk), open(file_path_on_disk, 'rb'), 'image/jpeg')}

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps(headers, indent=2)}")
        print(f"Request Files: {files['avatar'][0]} (content not shown)") # Don't print file content
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                files=files,
                headers=headers
            )
            response.raise_for_status()
            response_data = response.json()

            self.file_path = response_data['uploadFile']['path']
            self.file_originalname = response_data['uploadFile']['originalname']

            if is_mask and 'maskedData' in response_data:
                self.masked_identity_proof = response_data['maskedData']['path']
                self.unmasked_identity_proof = response_data['uploadFile']['path']

            print(f'File uploaded successfully. Path: {self.file_path}')
            print(f"Response Body: {json.dumps(response_data, indent=2)}")
            return response_data

    async def add_customer(self):
        api_path = "/api/customer"
        random_names = self._generate_random_name()
        self.first_name = random_names['firstName']
        self.last_name = random_names['lastName']
        self.random_pincode = self._generate_random_pincode()
        self.mobile_number = self._generate_random_mobile_number() # Generate for new customer

        await self._fetch_states()
        await self._fetch_cities(self.state_id)
        await self._fetch_modules()
        await self._fetch_status()

        # The _send_register_otp method already handles sending OTP and verifying it
        # for customer registration.
        await self._send_register_otp()
        await asyncio.sleep(3) # Wait for OTP verification to complete

        request_body = {
            "firstName": self.first_name,
            "lastName": self.last_name,
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
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            self.customer_id = str(response.json()['customer']['id']) # Ensure it's a string
            print(f'Customer created successfully. Customer ID: {self.customer_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def save_customer_info(self):
        api_path = "/api/kyc/customer-info"
        self.random_pan = self._generate_random_pan()
        self.pan_type = self.random.choice(["pan", "form60"])

        upload_result = await self._upload_file("lead", self.DUMMY_IMAGE_PATH)
        if self.pan_type == "pan":
            self.pan_image = upload_result['uploadFile']['path']
            self.form60_image = None
        else:
            self.form60_image = upload_result['uploadFile']['path']
            self.pan_image = None

        request_body = {
            "firstName": self.first_name,
            "lastName": self.last_name,
            "mobileNumber": self.mobile_number,
            "otp": None,
            "panType": self.pan_type,
            "form60": None,
            "panImage": self.pan_image,
            "panCardNumber": self.random_pan,
            "dateOfBirth": None,
            "userType": None,
            "moduleId": self.module_id,
            "organizationTypeId": None,
            "dateOfIncorporation": None,
            "form60Image": self.form60_image,
            "isAutoApproved": False,
            "isPanVerified": False,
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            self.customer_kyc_id = str(response.json()['data']['customerKycId'])
            self.customer_id = str(response.json()['data']['customerId']) # Update customer_id if it changes
            print(f'Save customer info successful. Customer KYC ID: {self.customer_kyc_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_special_categories(self):
        api_path = "/api/special-category"
        signature = self._generate_signature(api_path, request_body=None) # GET request

        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            categories = response.json().get('data')
            assert categories, "No special categories found."
            self.special_category_id = str(self.random.choice(categories).get('id'))
            print(f'Fetched random special_category_id: {self.special_category_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_occupations(self):
        api_path = "/api/occupation/list?search=&from=1&to=25"
        signature = self._generate_signature(api_path, request_body=None) # GET request
        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            occupations = response.json().get('data')
            assert occupations, "No occupations found."
            self.occupation_id = str(self.random.choice(occupations).get('id'))
            print(f'Fetched random occupation_id: {self.occupation_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_religions(self):
        api_path = "/api/religion"
        signature = self._generate_signature(api_path, request_body=None) # GET request
        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            religions = response.json().get('data')
            assert religions, "No religions found."
            self.religion_id = str(self.random.choice(religions).get('id'))
            print(f'Fetched random religion_id: {self.religion_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_physical_challenges(self):
        api_path = "/api/physical-challenge"
        signature = self._generate_signature(api_path, request_body=None) # GET request
        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            challenges = response.json().get('data')
            assert challenges, "No physical challenges found."
            self.physical_challenge_id = str(self.random.choice(challenges).get('id'))
            print(f'Fetched random physical_challenge_id: {self.physical_challenge_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_political_exposed(self):
        api_path = "/api/political-exposed"
        signature = self._generate_signature(api_path, request_body=None) # GET request
        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            exposed = response.json().get('data')
            assert exposed, "No political exposed categories found."
            self.political_exposed_id = str(self.random.choice(exposed).get('id'))
            print(f'Fetched random political_exposed_id: {self.political_exposed_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_annual_incomes(self):
        api_path = "/api/annual-income"
        signature = self._generate_signature(api_path, request_body=None) # GET request
        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            incomes = response.json().get('data')
            assert incomes, "No annual incomes found."
            self.annual_income = self.random.choice(incomes).get('incomeRange')
            print(f'Fetched random annual_income: {self.annual_income}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_qualifications(self):
        api_path = "/api/qualification"
        signature = self._generate_signature(api_path, request_body=None) # GET request
        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            qualifications = response.json().get('data')
            assert qualifications, "No qualifications found."
            self.qualification_id = str(self.random.choice(qualifications).get('id'))
            print(f'Fetched random qualification_id: {self.qualification_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def save_customer_personal_details(self):
        api_path = "/api/kyc/customer-kyc-personal"
        dob_and_age = self._generate_random_dob_and_age()
        self.age = int(dob_and_age['age'])
        self.dob = dob_and_age['dob']
        self.gender = self.random.choice(["m", "f", "o"])
        self.martial_status = self.random.choice(["single", "married", "divorced"])
        spouse_names = self._generate_random_name()
        self.spouse_name = f"{spouse_names['firstName']} {spouse_names['lastName']}"
        mother_names = self._generate_random_name()
        self.mother_name = f"{mother_names['firstName']} {mother_names['lastName']}"

        self.profile_image = (await self._upload_file("customer", self.DUMMY_IMAGE_PATH))['uploadFile']['path']
        self.signature_proof = (await self._upload_file("customer", self.DUMMY_IMAGE_PATH))['uploadFile']['path']

        await self._fetch_special_categories()
        await self._fetch_occupations()
        await self._fetch_religions()
        await self._fetch_physical_challenges()
        await self._fetch_political_exposed()
        await self._fetch_annual_incomes()
        await self._fetch_qualifications()

        request_body = {
            "customerId": self.customer_id,
            "customerKycId": self.customer_kyc_id,
            "profileImage": self.profile_image,
            "alternateMobileNumber": '',
            "gender": self.gender,
            "spouseName": self.spouse_name,
            "martialStatus": self.martial_status,
            "signatureProof": self.signature_proof,
            "occupationId": self.occupation_id,
            "dateOfBirth": self.dob + "T00:00:00.000Z", # Format as ISO string with Z
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
            "religionId": self.religion_id,
            "qualificationId": self.qualification_id,
            "physicalChallengeId": self.physical_challenge_id,
            "politicalExposedId": self.political_exposed_id,
            "specialCategoryId": self.special_category_id,
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            self.customer_details['customerKycPersonal'] = response.json().get('data')
            print('Save customer personal details successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_address_proof_types(self):
        api_path = "/api/address-proof-type"
        signature = self._generate_signature(api_path, request_body=None) # GET request

        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            types = response.json().get('data')
            assert types, "No address proof types found."
            self.address_proof_type_id = str(self.random.choice(types).get('id'))
            print(f'Fetched random address_proof_type_id: {self.address_proof_type_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def save_customer_address(self):
        api_path = "/api/kyc/customer-kyc-address"

        await self._fetch_address_proof_types()

        self.identity_proof_number = self._generate_random_aadhaar_number()
        same_as_permanent = self.random.choice([True, False])
        self.name_as_per_aadhaar = f"{self.first_name} {self.last_name}"

        await self._upload_file("customer", self.DUMMY_IMAGE_PATH, document_type="aadhar", is_mask=True)
        self.address_proof = (await self._upload_file("customer", self.DUMMY_IMAGE_PATH))['uploadFile']['path']

        addresses = []
        permanent_address_details = self._generate_random_address()
        permanent_address = {
            "addressType": "permanent",
            "addressProofTypeId": self.address_proof_type_id,
            "addressProofNumber": self.identity_proof_number,
            "address": permanent_address_details['address'],
            "stateId": self.state_id,
            "cityId": self.city_id,
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
                "addressProofTypeId": self.address_proof_type_id,
                "addressProofNumber": self._generate_random_address_proof_number(12),
                "address": residential_address_details['address'],
                "stateId": self.state_id,
                "cityId": self.city_id,
                "pinCode": self._generate_random_pincode(),
                "landmark": residential_address_details['landmark'],
                "addressProof": [self.address_proof],
                "unMaskedAddressProof": [self.unmasked_identity_proof],
            }
            addresses.append(residential_address)

        request_body = {
            "customerId": self.customer_id,
            "customerKycId": self.customer_kyc_id,
            "identityTypeId": "5", # Assuming a default identity type ID
            "identityProof": [self.masked_identity_proof],
            "identityProofNumber": self.identity_proof_number,
            "moduleId": self.module_id,
            "userType": None,
            "isAutoApproved": False,
            "nameAsPerAadhaar": self.name_as_per_aadhaar,
            "address": addresses,
            "isCityEdit": False,
            "isAahaarVerified": False,
            "unMaskedIdentityProof": [self.unmasked_identity_proof],
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            self.customer_details['customerKycAddress'] = addresses
            print('Save customer address successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def submit_all_kyc_info(self):
        api_path = "/api/kyc/submit-all-kyc-info"

        kyc_personal = self.customer_details.get('customerKycPersonal', {})
        kyc_address = self.customer_details.get('customerKycAddress', [])

        kyc_basic_details = {
            "id": self.customer_kyc_id,
            "profileImage": self.profile_image,
            "firstName": self.first_name,
            "lastName": self.last_name,
            "mobileNumber": self.mobile_number,
            "panCardNumber": self.random_pan,
            "panType": self.pan_type,
            "form60": None,
            "panImage": self.pan_image,
            "identityTypeId": "5",
            "identityProof": [self.masked_identity_proof],
            "identityProofFileName": None,
            "identityProofNumber": self.identity_proof_number,
            "userType": None,
            "organizationTypeId": None,
            "dateOfIncorporation": None,
            "form60Image": self.form60_image,
            "form60Img": None,
            "isCityEdit": None,
            "geoAddress": None,
            "nameAsPerAadhaar": self.name_as_per_aadhaar,
        }

        request_body = {
            "customerId": self.customer_id,
            "customerKycId": self.customer_kyc_id,
            "customerKycPersonal": kyc_personal,
            "customerKycAddress": kyc_address,
            "customerKycBasicDetails": kyc_basic_details,
            "moduleId": self.module_id,
            "userType": None,
            "customerOrganizationDetail": None,
            "isCityEdit": None,
        }

        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print('Submit all KYC info successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def kyc_ops_approval(self):
        api_path = "/api/classification/ops-team"
        request_body = {
            "customerId": self.customer_id,
            "customerKycId": self.customer_kyc_id,
            "kycRatingFromBM": False,
            "kycStatusFromOperationalTeam": "approved",
            "reasonFromOperationalTeam": "",
            "moduleId": self.module_id,
            "userType": None,
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print('KYC ops approval successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_appraisers(self):
        api_path = f"/api/user/appraiser-list?internalBranchId={self.augmont_branch_id}" # Use dynamic branch ID
        signature = self._generate_signature(api_path, request_body=None) # GET request

        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            appraisers = response.json().get('data')
            assert appraisers, "No appraisers found."
            self.appraiser_id = str(self.random.choice(appraisers).get('id'))
            print(f'Fetched random appraiser_id: {self.appraiser_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def create_appraiser_request(self):
        api_path = "/api/appraiser-request"
        await self._fetch_appraisers()

        request_body = {
            "id": None,
            "customerId": self.customer_id,
            "customerName": f'{self.first_name} {self.last_name}',
            "customerUniqueId": f'MPV3Z7LR', # Example from collection, consider making dynamic
            "mobileNumber": self.mobile_number,
            "moduleId": self.module_id,
            "internalBranchId": self.augmont_branch_id, # Use dynamic branch ID
            "appraiserId": self.appraiser_id,
            "loanType": "Fresh Loan", # From collection example
            "trackProccesingTime": True,
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            self.appraiser_request_id = str(response.json()['data']['id'])
            print(f'Appraiser request created successfully. Appraiser Request ID: {self.appraiser_request_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def track_loan_history(self, action: int):
        api_path = "/api/appraiser-request/track-loan-history"
        request_body = {
            "action": action,
            "appraiserRequestId": self.appraiser_request_id,
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print(f'Track loan history (action {action}) successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_loan_purposes(self):
        api_path = "/api/purpose?search=&from=1&to=-1"
        signature = self._generate_signature(api_path, request_body=None) # GET request

        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            purposes = response.json().get('data')
            assert purposes, "No loan purposes found."
            self.purpose_id = self.random.choice(purposes).get('name')
            print(f'Fetched random purpose: {self.purpose_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")
            return self.purpose_id

    async def store_loan_basic_details(self):
        api_path = "/api/loan-process/basic-details"
        purpose = await self._fetch_loan_purposes()
        dob_and_age = self._generate_random_dob_and_age() # Ensure these are set
        self.age = int(dob_and_age['age'])
        self.dob = dob_and_age['dob']

        request_body = {
            "customerUniqueId": f'MPV3Z7LR', # Example from collection, consider making dynamic
            "mobileNumber": self.mobile_number,
            "panCardNumber": self.random_pan,
            "startDate": datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z'), # ISO 8601 with Z
            "customerId": self.customer_id,
            "kycStatus": "approved",
            "purpose": purpose,
            "panType": self.pan_type,
            "loanId": None,
            "scrapId": None,
            "masterLoanId": None,
            "panImg": self.pan_image,
            "partReleaseId": None,
            "requestId": self.appraiser_request_id,
            "customerName": f'{self.first_name} {self.last_name}',
            "form60Img": self.form60_image,
            "age": str(self.age),
            "branchName": "Augmont",
            "branchDistance": None, # Example from collection
            "gender": self.gender,
            "appraiserName": "Abhi userAppraiser", # Placeholder
            "tillDateOutstanding": "3122296.67", # Example from collection
            "appraiserMobileNumber": "9990009991", # Example from collection
            "currentActiveLoans": 2, # Example from collection
            "cpvImageFullUrl": f"{self.BASE_URL}/public/uploads/loan/dummy_cpv.png", # Placeholder
            "annualIncome": self.annual_income,
            "motherName": self.mother_name,
            "religionId": self.religion_id,
            "physicalChallengeId": self.physical_challenge_id,
            "occupationId": self.occupation_id,
            "signatureProof": self.signature_proof,
            "signatureProofImg": f"{self.BASE_URL}/{self.signature_proof}",
            "signatureProofFileName": os.path.basename(self.signature_proof),
            "referenceCustomerNumber": None,
            "qualificationId": self.qualification_id,
            "politicalExposedId": self.political_exposed_id,
            "specialCategoryId": self.special_category_id,
            "incomeGeneratingDocuments": None,
            "purposeType": "Consumption Based", # Example from collection
            "checkPointers": {},
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            self.loan_id = str(response.json()['data']['loanId'])
            self.master_loan_id = str(response.json()['data']['masterLoanId'])
            print(f'Loan basic details stored successfully. Loan ID: {self.loan_id}, Master Loan ID: {self.master_loan_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_nominee_relations(self):
        api_path = "/api/nominee-relation/list"
        signature = self._generate_signature(api_path, request_body=None) # GET request

        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            relations = response.json().get('data')
            assert relations, "No nominee relations found."
            self.nominee_relation_id = self.random.choice(relations).get('name')
            print(f'Fetched random nominee_relation_id: {self.nominee_relation_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")
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
            "guardianAge": 0,
            "guardianRelationship": "",
            "checkPointers": {},
            "loanId": self.loan_id,
            "masterLoanId": self.master_loan_id,
            "loanType": "Fresh Loan",
            "createdBy": "3179", # Placeholder for user ID
            "internalBranchId": self.augmont_branch_id, # Use dynamic branch ID
        }

        if nominee_age_val < 18:
            guardian_names = self._generate_random_name()
            request_body["guardianName"] = f"{guardian_names['firstName']} {guardian_names['lastName']}"
            request_body["guardianAge"] = self.random.randint(30, 70)
            request_body["guardianRelationship"] = 'Parent'

        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print('Nominee details stored successfully.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_karat_details(self):
        api_path = "/api/karat-details" # From collection example, no query params
        signature = self._generate_signature(api_path, request_body=None) # GET request

        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            karats = response.json().get('data')
            assert karats, "No karat details found."
            karat_detail = self.random.choice(karats)
            self.karat_id = karat_detail.get('karat')
            print(f'Fetched random karat_id: {self.karat_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")
            return karat_detail

    async def _fetch_ornament_types(self):
        api_path = "/api/ornament-type?from=1&to=-1&search=" # From collection example
        signature = self._generate_signature(api_path, request_body=None) # GET request

        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            ornament_types_list = response.json().get('data')
            assert ornament_types_list, "No ornament types found."
            ornament_type = self.random.choice(ornament_types_list)
            self.ornament_type_id = str(ornament_type.get('id'))
            print(f'Fetched random ornament_type_id: {self.ornament_type_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

            return ornament_type

    async def store_ornament_details(self):
        api_path = "/api/loan-process/ornaments-details"
        ornament_type = await self._fetch_ornament_types()
        karat_detail = await self._fetch_karat_details()

        gross_weight = self._round_double(self.random.uniform(10, 60), 2) # 10-60 grams
        deduction_weight = self._round_double(self.random.uniform(0, 5), 2) # 0-5 grams
        net_weight = self._round_double(gross_weight - deduction_weight, 2)
        current_gold_rate = self._round_double(self.random.uniform(3000, 8000), 2) # 3000-8000

        ltv_range = karat_detail.get('ltvRange', [90])
        ltv_percent = str(self.random.choice(ltv_range))
        net_wt_after_purity = self._round_double(net_weight * (float(ltv_percent) / 100), 2)

        ornament_image_response = await self._upload_file("loan", self.DUMMY_IMAGE_PATH)
        ornament_image = ornament_image_response['uploadFile']['path']

        ornament = {
            "ornamentType": ornament_type,
            "ornamentTypeId": self.ornament_type_id,
            "quantity": "1",
            "grossWeight": str(gross_weight),
            "netWeight": str(net_weight),
            "deductionWeight": str(deduction_weight),
            "ornamentImage": ornament_image,
            "weightMachineZeroWeight": None,
            "withOrnamentWeight": None,
            "stoneTouch": None,
            "acidTest": None,
            "karat": self.karat_id,
            "ltvRange": ltv_range,
            "purityTest": [],
            "ltvPercent": ltv_percent,
            "ltvAmount": None,
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
            "approxPurityReading": None,
            "scrapAmount": None,
            "purityReading": None,
            "customerConfirmation": None,
            "finalScrapAmountAfterMelting": None,
            "processingCharges": None,
            "packetId": None,
            "netWtAfterPurity": str(net_wt_after_purity),
            "remark": None,
            "isReleased": None,
        }

        loan_ornaments = [ornament]
        total_eligible_amt = sum(o['currentLtvAmount'] for o in loan_ornaments)

        request_body = {
            "loanOrnaments": loan_ornaments,
            "totalEligibleAmt": total_eligible_amt,
            "loanId": self.loan_id,
            "masterLoanId": self.master_loan_id,
            "fullAmount": 0,
            "checkPointers": {}, # From collection example
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print('Ornament details stored successfully.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def _fetch_partner_scheme_amount(self):
        api_path = f"/api/scheme/partner-scheme-amount/1?masterLoanId={self.master_loan_id}"
        signature = self._generate_signature(api_path, request_body=None) # GET request

        print(f"\n--- Request: GET {self.BASE_URL}{api_path} ---")
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'signature': signature,
        }
        print(f"Request Headers: {json.dumps(headers, indent=2)}") # Debug print
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.BASE_URL + api_path,
                headers=headers
            )
            response.raise_for_status()
            schemes = response.json().get('data')
            assert schemes, "No schemes found."
            selected_scheme = self.random.choice(schemes)
            self.scheme_id = str(selected_scheme.get('id'))
            self.partner_id = str(selected_scheme.get('partnerId'))
            self.co_lender_bank_id = str(selected_scheme.get('coLenderBankId')) if selected_scheme.get('coLenderBankId') else None
            print(f'Fetched random schemeId: {self.scheme_id}, partnerId: {self.partner_id}')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")


    async def check_loan_type(self):
        api_path = "/api/loan-process/check-loan-type"
        await self._fetch_partner_scheme_amount() # Fetch scheme and partner IDs

        request_body = {
            "loanAmount": 400000, # Example loan amount from collection
            "securedSchemeId": self.scheme_id,
            "fullAmount": 0,
            "partnerId": self.partner_id,
            "isLoanTransfer": False,
            "isNewLoanFromPartRelease": False,
            "unsecuredSchemeId": None,
            "isUnsecuredSchemeApplied": False,
            "loanTransferExtraAmount": None,
            "isNewLoanFromRenew": False,
            "loanId": self.loan_id,
            "masterLoanId": self.master_loan_id,
            "loanType": "Fresh Loan",
            "createdBy": "3179", # Placeholder
            "internalBranchId": self.augmont_branch_id, # Use dynamic branch ID
            "coLenderBankId": self.co_lender_bank_id,
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print('Check loan type successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def get_interest_rate(self):
        api_path = "/api/loan-process/interest-rate"
        request_body = {
            "securedSchemeId": self.scheme_id,
            "unsecuredSchemeId": None,
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print('Get interest rate successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def generate_interest_table(self):
        api_path = "/api/loan-process/generate-interest-table"
        tenure_months = 4 # Example from collection
        loan_start_date = datetime.date.today().isoformat()
        loan_end_date = (datetime.date.today() + datetime.timedelta(days=tenure_months * 30)).isoformat() # Approx

        request_body = {
            "partnerId": self.partner_id,
            "coLenderBankId": self.co_lender_bank_id,
            "schemeId": self.scheme_id,
            "finalLoanAmount": 400000, # Example from collection
            "tenure": tenure_months,
            "loanStartDate": loan_start_date,
            "loanEndDate": loan_end_date,
            "paymentFrequency": "30 Days", # Example from collection
            "totalFinalInterestAmt": None,
            "unsecuredInterestRate": None,
            "interestRate": 1.2, # Example from collection
            "processingCharge": 0,
            "unsecuredSchemeId": None,
            "securedLoanAmount": 400000,
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
            "securedRpg": 4480, # Example from collection
            "unsecuredPartnerId": None,
            "unsecuredprocessingCharge": 0,
            "securedprocessingCharge": 0,
            "securedExposure": "0.13", # Example from collection
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print('Generate interest table successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def get_final_loan_details(self):
        api_path = "/api/loan-process/final-loan-details"
        loan_start_date = datetime.date.today().isoformat()
        loan_end_date = (datetime.date.today() + datetime.timedelta(days=4 * 30)).isoformat() # Approx 4 months

        # Simplified request body based on collection example
        loan_final_calculator = {
            "partnerId": self.partner_id,
            "coLenderBankId": self.co_lender_bank_id,
            "schemeId": self.scheme_id,
            "finalLoanAmount": 400000,
            "tenure": 4,
            "loanStartDate": loan_start_date,
            "loanEndDate": loan_end_date,
            "paymentFrequency": "30 Days",
            "totalFinalInterestAmt": 19200, # Example
            "unsecuredInterestRate": None,
            "interestRate": 1.2, # Example
            "processingCharge": 0,
            "unsecuredSchemeId": None,
            "securedLoanAmount": 400000,
            "unsecuredLoanAmount": None,
            "isUnsecuredSchemeApplied": False,
            "unsecuredRebateInterest": 0,
            "securedRebateInterest": 1.7, # Example
            "otherAmount": None,
            "loanTransferExtraAmount": None,
            "upfrontInterestAmount": 0,
            "topUpAmount": 0,
            "securedTopUpAmount": 0,
            "unsecuredTopUpAmount": 0,
            "manualCharges": [],
            "unsecuredRpg": None,
            "securedRpg": 4480, # Example
            "unsecuredPartnerId": None,
            "unsecuredprocessingCharge": 0,
            "securedprocessingCharge": 0,
            "securedExposure": "0.13", # Example
        }

        request_body = {
            "manualCharges": [],
            "loanFinalCalculator": loan_final_calculator,
            "interestTable": [
                # Example interest table entries from collection
                {"emiStartDate": loan_start_date, "emiDueDate": (datetime.date.today() + datetime.timedelta(days=29)).isoformat(), "emiEndDate": (datetime.date.today() + datetime.timedelta(days=29)).isoformat(), "noOfDays": 30, "month": "Month 1", "securedInterestAmount": "4800.00", "secureHighestInterestAmount": "6800.00", "unsecuredInterestAmount": 0, "unsecureHighestInterestAmount": 0, "securedRebateAmount": 2000, "unsecuredRebateAmount": 0, "totalAmount": 4800},
                {"emiStartDate": (datetime.date.today() + datetime.timedelta(days=30)).isoformat(), "emiDueDate": (datetime.date.today() + datetime.timedelta(days=59)).isoformat(), "emiEndDate": (datetime.date.today() + datetime.timedelta(days=59)).isoformat(), "noOfDays": 30, "month": "Month 2", "securedInterestAmount": "4800.00", "secureHighestInterestAmount": "6800.00", "unsecuredInterestAmount": 0, "unsecureHighestInterestAmount": 0, "securedRebateAmount": 2000, "unsecuredRebateAmount": 0, "totalAmount": 4800},
                {"emiStartDate": (datetime.date.today() + datetime.timedelta(days=60)).isoformat(), "emiDueDate": (datetime.date.today() + datetime.timedelta(days=89)).isoformat(), "emiEndDate": (datetime.date.today() + datetime.timedelta(days=89)).isoformat(), "noOfDays": 30, "month": "Month 3", "securedInterestAmount": "4800.00", "secureHighestInterestAmount": "6800.00", "unsecuredInterestAmount": 0, "unsecureHighestInterestAmount": 0, "securedRebateAmount": 2000, "unsecuredRebateAmount": 0, "totalAmount": 4800},
                {"emiStartDate": (datetime.date.today() + datetime.timedelta(days=90)).isoformat(), "emiDueDate": (datetime.date.today() + datetime.timedelta(days=119)).isoformat(), "emiEndDate": (datetime.date.today() + datetime.timedelta(days=119)).isoformat(), "noOfDays": 30, "month": "Month 4", "securedInterestAmount": "4800.00", "secureHighestInterestAmount": "6800.00", "unsecuredInterestAmount": 0, "unsecureHighestInterestAmount": 0, "securedRebateAmount": 2000, "unsecuredRebateAmount": 0, "totalAmount": 4800},
            ],
            "loanId": self.loan_id,
            "masterLoanId": self.master_loan_id,
            "ornaments": [
                {"id": 19024,"loanAmount": 478016,"ornamentsCal": 106.7,"rpg": 4480}, # Example from collection
                {"id": 19025,"loanAmount": 482944,"ornamentsCal": 107.8,"rpg": 4480}  # Example from collection
            ],
            "totalEligibleAmt": 960960, # Example
            "checkPointers": {},
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print('Get final loan details successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def store_bank_details(self):
        api_path = "/api/loan-process/bank-details"
        bank_name = "STATE BANK OF INDIA" # Example from collection
        account_number = "00000036150491589" # Example from collection
        ifsc_code = "SBIN0011777" # Example from collection
        account_holder_name = "LIC MUTUAL FUND" # Example from collection


        passbook_proof_1 = (await self._upload_file("loan", self.DUMMY_IMAGE_PATH))['uploadFile']['path']
        passbook_proof_2 = (await self._upload_file("loan", self.DUMMY_IMAGE_PATH))['uploadFile']['path']

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
            "bankBranchName": "SBI CAPITAL MARKET BRANCH, MUMBAI", # Example
            "passbookProof": [passbook_proof_1, passbook_proof_2],
            "passbookProofImage": [],
            "passbookProofImageName": [],
            "account": 1508, # Example
            "detailsFor": "customer",
            "customerTransferBalance": 0,
            "internalBranchTransferBalance": 17882793.96, # Example
            "internalBranchBlockedBalance": 11916017.72, # Example
            "advanceBankTransfer": 0,
            "advanceBankTransactionId": None,
            "advanceCash": 0,
            "advanceCashTransactionId": [],
            "advanceCashTransactionImage": [],
            "advanceCashTransactionImageName": "",
            "advanceCashDeclarationId": [],
            "bTMoney": 0,
            "actualProcessingCharge": 0,
            "toBePaid": 400000, # Example
            "remark": None,
            "upfrontInterestAmount": 0,
            "upfrontTransactionId": None,
            "upfrontReceipt": [],
            "upfrontReceiptImage": [],
            "upfrontReceiptImageName": "",
            "amountDisbursedByCash": 0,
            "amountDisbursedByBank": 0,
            "forOpsApproval": False,
            "isManuallyVerified": True,
            "isVerified": False,
            "isDummyDetails": False,
            "manualVerifiedStatus": "",
            "wavier": 0,
            "processingCharge": 0,
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
            "availableLimitForDisbuserment": 400000,
            "wavierAppraiserCharges": 0,
            "stampDutyCharges": 0,
            "stampDutyDefinitionType": None,
            "stampDutyRemarks": "",
            "totalLimitForCashDisbursal": 200000, # Example
            "multiModeDisbursement": True,
            "checkPointers": {}, # From collection example
            "loanId": self.loan_id,
            "masterLoanId": self.master_loan_id,
            "loanType": "Fresh Loan",
            "createdBy": "3179", # Placeholder
            "internalBranchId": self.augmont_branch_id, # Use dynamic branch ID
        }

        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print('Store bank details successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def add_appraiser_rating(self):
        api_path = "/api/loan-process/appraiser-rating"
        request_body = {
            "applicationFormForAppraiser": True,
            "goldValuationForAppraiser": True,
            "loanStatusForAppraiser": "approved",
            "commentByAppraiser": None,
            "partRelease": None,
            "loanId": self.loan_id,
            "masterLoanId": self.master_loan_id,
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print('Add appraiser rating successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def add_bm_rating(self):
        api_path = "/api/loan-process/bm-rating"
        request_body = {
            "loanId": self.loan_id,
            "masterLoanId": self.master_loan_id,
            "applicationFormForBM": True,
            "goldValuationForBM": True,
            "loanStatusForBM": "approved",
            "commentByBM": None,
            "partRelease": None,
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print('Add BM rating successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

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
            "commentByOperatinalTeam": None,
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
            "loanId": self.loan_id,
            "masterLoanId": self.master_loan_id,
            "loanAmountDigi": "100000.00", # Example
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print('Add Ops rating successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def add_packet_images(self):
        api_path = "/api/loan-process/add-packet-images"
        empty_packet_image = (await self._upload_file("images", self.DUMMY_IMAGE_PATH))['uploadFile']['path']
        sealing_packet_with_weight_image = (await self._upload_file("images", self.DUMMY_IMAGE_PATH))['uploadFile']['path']
        sealing_packet_with_customer_image = (await self._upload_file("images", self.DUMMY_IMAGE_PATH))['uploadFile']['path']
        sealed_packet_image = (await self._upload_file("images", self.DUMMY_IMAGE_PATH))['uploadFile']['path']

        packet_ornament = {
            "packetId": "dummy-packet-id", # Placeholder
            "ornamentsId": [19024, 19025], # Example from collection
            "packetsName": "pac-066", # Example
            "ornamentsName": "Chain, Rings", # Example
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
            "ornamentImageWithXrfMachineReadingData": "",
            "sealedPacket": sealed_packet_image,
            "sealedPacketImage": f'{self.BASE_URL}/{sealed_packet_image}',
            "packetOrnamentArray": [packet_ornament],
            "checkPointers": {}, # From collection example
            "loanType": "Fresh Loan", # Example
            "loanId": self.loan_id,
            "masterLoanId": self.master_loan_id,
            "createdBy": "3179", # Placeholder
            "internalBranchId": self.augmont_branch_id, # Use dynamic branch ID
        }
        signature = self._generate_signature(api_path, request_body)

        print(f"\n--- Request: POST {self.BASE_URL}{api_path} ---")
        print(f"Request Headers: {json.dumps({'Content-Type': 'application/json', 'Authorization': f'Bearer {self.auth_token}', 'signature': signature}, indent=2)}")
        print(f"Request Body: {json.dumps(request_body, indent=2)}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.BASE_URL + api_path,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.auth_token}',
                    'signature': signature,
                }
            )
            response.raise_for_status()
            print('Add packet images successful.')
            print(f"Response Body: {json.dumps(response.json(), indent=2)}")

    async def run_e2e_test(self):
        print('--- Starting Gold Loan E2E Test ---')
        try:
            # Step 1: Login
            await self.login()

            # Step 1.2: Fetch and set Augmont branch ID
            await self._fetch_and_set_augmont_branch_id()

            # Step 1.5: Change User Branch to Augmont
            await self.change_user_branch(self.augmont_branch_id)

            # Step 2: Add Customer
            await self.add_customer()

            # Step 3: KYC Process
            await self.save_customer_info()
            await self.save_customer_personal_details()
            await self.save_customer_address()
            await self.submit_all_kyc_info()
            await self.kyc_ops_approval()

            # Step 4: Apply Loan Process
            await self.create_appraiser_request()
            await self.track_loan_history(3) # Assuming action 3 is a relevant tracking step
            await self.store_loan_basic_details()
            await self.store_nominee_details()
            await self.store_ornament_details()
            await self.check_loan_type()
            await self.get_interest_rate()
            await self.generate_interest_table()
            await self.get_final_loan_details()
            await self.store_bank_details()
            await self.add_appraiser_rating()
            await self.add_bm_rating()
            await self.add_ops_rating()
            await self.add_packet_images() # End of requested flow

            print('\n--- Gold Loan E2E Test Completed Successfully up to Packet Assignment ---')
            print('\n--- Collected Details Summary ---')
            print(f'Auth Token: {self.auth_token[:30]}...')
            print(f'Customer ID: {self.customer_id}')
            print(f'Customer Name: {self.first_name} {self.last_name}')
            print(f'Mobile Number: {self.mobile_number}')
            print(f'State ID: {self.state_id}')
            print(f'City ID: {self.city_id}')
            print(f'Module ID: {self.module_id}')
            print(f'Customer KYC ID: {self.customer_kyc_id}')
            print(f'Appraiser Request ID: {self.appraiser_request_id}')
            print(f'Loan ID: {self.loan_id}')
            print(f'Master Loan ID: {self.master_loan_id}')
            print(f'Scheme ID: {self.scheme_id}')
            print(f'Partner ID: {self.partner_id}')
            print(f'Co-Lender Bank ID: {self.co_lender_bank_id}')
            print(f'Random PAN: {self.random_pan}')
            print(f'Annual Income: {self.annual_income}')
            print(f'Qualification ID: {self.qualification_id}')
            print(f'Religion ID: {self.religion_id}')
            print(f'Physical Challenge ID: {self.physical_challenge_id}')
            print(f'Political Exposed ID: {self.political_exposed_id}')
            print(f'Special Category ID: {self.special_category_id}')
            print(f'Occupation ID: {self.occupation_id}')
            print(f'Nominee Relation ID: {self.nominee_relation_id}')
            print(f'Ornament Type ID: {self.ornament_type_id}')
            print(f'Karat ID: {self.karat_id}')
            print(f'Purpose ID: {self.purpose_id}')
            print(f'Augmont Branch ID: {self.augmont_branch_id}')


        except httpx.HTTPStatusError as e:
            print(f'\n--- Gold Loan E2E Test Failed: HTTP Error ---')
            print(f'Request: {e.request.method} {e.request.url}')
            print(f'Response Status: {e.response.status_code}')
            print(f'Response Body: {e.response.text}')
            raise
        except Exception as e:
            print(f'\n--- Gold Loan E2E Test Failed: {e} ---')
            raise

# To run the test
if __name__ == "__main__":
    test_suite = GoldLoanApiTest()
    asyncio.run(test_suite.run_e2e_test())