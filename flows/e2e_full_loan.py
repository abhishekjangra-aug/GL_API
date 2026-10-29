#!/usr/bin/env python3
"""Run the COMPLETE gold loan journey end to end.

Customer/KYC -> appraiser request -> basic details -> ornaments -> scheme -> bank details ->
appraiser rating -> packet -> BM rating (when the server asks) -> upload documents (+ LIP / IGL
documents when required) -> ops verification + rating -> partner approval + disbursement ->
submit packet -> load loan details. Every run first generates its own packet.

    python flows/e2e_full_loan.py
    python flows/e2e_full_loan.py --env uat
    python flows/e2e_full_loan.py --customer MSWS4ZR1 --partner arvog --purpose "Agricultural Purposes"

All flags: python flows/e2e_full_loan.py --help
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # project root

from gold_loan.harness import main  # noqa: E402

if __name__ == "__main__":
    main()
