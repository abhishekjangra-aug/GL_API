#!/usr/bin/env python3
"""Free a PAN and/or Aadhaar from the KYC records on TEST / UAT (POST /api/e-kyc/remove).

A real document can be registered against only one customer ("PAN Card already exists!"). This
releases it so `--kyc auto` (or a manual test) can register it on a new customer again.

    python flows/kyc_release_documents.py --env test --from-profile        # the identity profile's PAN (+ full Aadhaar)
    python flows/kyc_release_documents.py --env uat --pan ABCDE1234F
    python flows/kyc_release_documents.py --env test --pan ABCDE1234F --aadhaar 123412341234

The server's answer says what was actually freed (panCardUpdated / identityProofNumberUpdated);
false simply means that number was not registered. Only TEST and UAT are accepted.
To do this automatically before KYC instead, pass --release-kyc-docs to the loan / KYC flows.
"""

import argparse
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # project root

from gold_loan.harness import GoldLoanApiTest  # noqa: E402


def build_parser():
    parser = argparse.ArgumentParser(description="Free a PAN / Aadhaar from the KYC records on TEST or UAT.")
    parser.add_argument("--env", choices=sorted(GoldLoanApiTest.ENVIRONMENTS), default="test",
                        help="Target environment (default: test).")
    parser.add_argument("--pan", help="PAN to release.")
    parser.add_argument("--aadhaar", help="12-digit Aadhaar (identity proof number) to release.")
    parser.add_argument("--from-profile", action="store_true",
                        help="Release the identity profile's PAN (and its Aadhaar, if all 12 digits are given).")
    parser.add_argument("--kyc-profile", metavar="PATH",
                        help="Identity profile for --from-profile (default: config/kyc_identity.json).")
    return parser


async def run(args) -> int:
    if args.from_profile:
        os.environ["GOLD_LOAN_KYC_MODE"] = GoldLoanApiTest.KYC_MODE_AUTO   # loads + validates the profile
    suite = GoldLoanApiTest()
    try:
        pan, aadhaar = args.pan, args.aadhaar
        if args.from_profile:
            pan = pan or suite.kyc_profile.get("panCardNumber")
            digits = re.sub(r"\D", "", str(suite.kyc_profile.get("aadhaarNumber", "")))
            aadhaar = aadhaar or (digits if len(digits) == 12 else None)
        if aadhaar and not re.fullmatch(r"\d{12}", re.sub(r"\D", "", aadhaar)):
            print("--aadhaar must be 12 digits.")
            return 2
        if not pan and not aadhaar:
            print("Nothing to release: give --pan / --aadhaar, or --from-profile.")
            return 2
        await suite.release_kyc_documents(pan=pan, aadhaar=re.sub(r"\D", "", aadhaar) if aadhaar else None)
        return 0
    finally:
        await suite.client.aclose()


def main():
    args = build_parser().parse_args()
    os.environ["GOLD_LOAN_ENV"] = args.env
    if args.kyc_profile:
        os.environ["GOLD_LOAN_KYC_PROFILE"] = args.kyc_profile
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
