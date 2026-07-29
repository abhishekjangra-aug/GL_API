#!/usr/bin/env python3
"""bcrypt password recovery / verification tool.

IMPORTANT — read this first
===========================
The passwords stored for Gold Loan user logins are **bcrypt hashes** (see
docs/password-hash-format.md). bcrypt is a ONE-WAY hash: it cannot be decrypted. There is no key,
no inverse function, nothing that turns

    $2b$10$11oJtQhZliu3qkQZXPkpw.uxkmZjvjBx9atQ0R/GGbB4SVtcLW9Gm

back into the original password.

So this is not a "decryptor" — no such thing can exist for bcrypt. It is a **recovery tool**: it does
the only thing that is actually possible, which is *guess-and-check*. It hashes candidate passwords
with the salt and cost baked into the target hash and compares. That is exactly how the login server
verifies a password (bcrypt.checkpw), and exactly how password crackers like hashcat/john work.

Intended use
------------
Recovering / auditing passwords for accounts on YOUR OWN test environment (this harness's TEST/UAT
DB). Do not run it against hashes you are not authorised to recover.

Reality check on speed
----------------------
bcrypt is deliberately slow. At cost factor 10 (2^10 = 1024 rounds) a single core does roughly
10-30 guesses/second. That means:
  * a known / weak / dictionary password  -> found in seconds to minutes,
  * a strong random password              -> effectively never (that is the whole point of bcrypt).
Brute-forcing anything beyond ~5-6 characters is not practical here; use --wordlist.

Usage
-----
    # 1) Just verify one candidate against a hash (the honest primary use):
    python tools/password_recover.py --hash '$2b$10$...' --password 'Gold@123'

    # 2) Try a wordlist file (one candidate per line):
    python tools/password_recover.py --hash '$2b$10$...' --wordlist rockyou.txt

    # 3) Try the small built-in list of common/likely passwords:
    python tools/password_recover.py --hash '$2b$10$...' --common

    # 4) Brute force over a character set (small keyspaces only!):
    python tools/password_recover.py --hash '$2b$10$...' --brute --max-len 4 --charset digits

    # 5) Combine sources; add mangling rules (case + digit/symbol suffixes) to a wordlist:
    python tools/password_recover.py --hash '$2b$10$...' --common --wordlist words.txt --rules

    # Inspect a hash without cracking anything:
    python tools/password_recover.py --hash '$2b$10$...' --info

Requires:  pip install bcrypt
"""
from __future__ import annotations

import argparse
import itertools
import string
import sys
import time

try:
    import bcrypt
except ImportError:
    sys.exit(
        "This tool needs the `bcrypt` package.\n"
        "    pip install bcrypt\n"
        "(also added to requirements.txt)"
    )


# ------------------------------------------------------------------------------------------------
# Hash parsing / info
# ------------------------------------------------------------------------------------------------
def parse_hash(h: str) -> dict:
    """Split a bcrypt modular-crypt string into its parts. Raises ValueError if it isn't bcrypt."""
    h = h.strip()
    parts = h.split("$")
    # A valid bcrypt hash is:  '' , variant, cost, salt+hash   -> 4 elements after split
    if len(parts) != 4 or parts[0] != "" or parts[1] not in ("2", "2a", "2b", "2x", "2y"):
        raise ValueError(
            f"Not a bcrypt hash: {h!r}\n"
            "Expected the form $2b$<cost>$<22-char-salt><31-char-hash>."
        )
    variant, cost_str, tail = parts[1], parts[2], parts[3]
    if not cost_str.isdigit() or len(tail) != 53:
        raise ValueError(f"Malformed bcrypt hash (bad cost or length): {h!r}")
    return {
        "variant": variant,
        "cost": int(cost_str),
        "salt_b64": tail[:22],
        "hash_b64": tail[22:],
        "full": h,
    }


def print_info(info: dict) -> None:
    print("Hash breakdown")
    print("  full     :", info["full"])
    print(f"  variant  : {info['variant']}  (bcrypt)")
    print(f"  cost     : {info['cost']}  (2^{info['cost']} = {1 << info['cost']} rounds)")
    print(f"  salt     : {info['salt_b64']}  (22 chars)")
    print(f"  hash     : {info['hash_b64']}  (31 chars)")
    print("  note     : bcrypt is one-way; the original password is not stored and cannot be derived.")


# ------------------------------------------------------------------------------------------------
# Candidate sources
# ------------------------------------------------------------------------------------------------
COMMON_PASSWORDS = [
    "password", "Password", "Password1", "Password@123", "password123", "Password123",
    "123456", "12345678", "123456789", "1234567890", "qwerty", "abc123", "admin", "Admin@123",
    "admin123", "Admin123", "welcome", "Welcome@123", "Welcome1", "letmein", "changeme",
    "test", "test123", "Test@123", "Test1234", "user", "user123", "root", "toor",
    "gold", "gold123", "Gold@123", "goldloan", "GoldLoan@123", "augmont", "Augmont@123",
    "augmont123", "Augmont@2024", "Augmont@2025", "appraiser", "Appraiser@123", "partner",
    "Partner@123", "india123", "India@123", "iloveyou", "monkey", "dragon", "sunshine",
    "1234", "0000", "1111", "121212", "654321", "qwerty123", "Qwerty@123",
]

CHARSETS = {
    "digits": string.digits,
    "lower": string.ascii_lowercase,
    "upper": string.ascii_uppercase,
    "alpha": string.ascii_letters,
    "alnum": string.ascii_letters + string.digits,
    "all": string.ascii_letters + string.digits + "@#$_.!-",
}


def read_wordlist(path: str):
    """Yield candidates from a wordlist file, tolerant of encoding (rockyou is latin-1)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            word = line.rstrip("\r\n")
            if word:
                yield word


def apply_rules(word: str):
    """A few cheap, high-yield mangling rules per base word (case + common suffixes)."""
    seen = set()
    bases = {word, word.lower(), word.upper(), word.capitalize()}
    suffixes = ["", "1", "12", "123", "1234", "@123", "@1234", "!", "@", "2024", "2025", "01"]
    for base in bases:
        for suf in suffixes:
            cand = base + suf
            if cand not in seen:
                seen.add(cand)
                yield cand


def brute_force(charset: str, max_len: int, min_len: int = 1):
    for length in range(min_len, max_len + 1):
        for combo in itertools.product(charset, repeat=length):
            yield "".join(combo)


def build_candidates(args) -> "tuple":
    """Return (iterator_of_candidates, human_description). Sources are chained in order."""
    sources = []
    descr = []
    if args.password is not None:
        sources.append([args.password])
        descr.append("1 supplied password")
    if args.common:
        sources.append(list(COMMON_PASSWORDS))
        descr.append(f"{len(COMMON_PASSWORDS)} common passwords")
    if args.wordlist:
        sources.append(read_wordlist(args.wordlist))
        descr.append(f"wordlist {args.wordlist}")
    if args.brute:
        charset = CHARSETS[args.charset]
        sources.append(brute_force(charset, args.max_len, args.min_len))
        space = sum(len(charset) ** n for n in range(args.min_len, args.max_len + 1))
        descr.append(f"brute {args.charset}[{args.min_len}..{args.max_len}] (~{space:,} combos)")

    base = itertools.chain.from_iterable(sources)
    if args.rules:
        base = itertools.chain.from_iterable(apply_rules(w) for w in base)
        descr.append("+rules")
    return base, ", ".join(descr) if descr else "(nothing)"


# ------------------------------------------------------------------------------------------------
# The check
# ------------------------------------------------------------------------------------------------
def try_recover(target: str, candidates, limit: int, report_every: int) -> "tuple":
    """Return (found_password_or_None, tried_count, elapsed_seconds)."""
    target_bytes = target.encode()
    start = time.time()
    tried = 0
    for cand in candidates:
        if limit and tried >= limit:
            break
        tried += 1
        try:
            # bcrypt only uses the first 72 bytes of the password.
            if bcrypt.checkpw(cand.encode("utf-8", "replace")[:72], target_bytes):
                return cand, tried, time.time() - start
        except ValueError:
            # bad/malformed target hash — surface immediately
            raise
        if report_every and tried % report_every == 0:
            rate = tried / max(time.time() - start, 1e-9)
            print(f"  ...tried {tried:,} ({rate:,.0f}/s, last: {cand!r})", file=sys.stderr)
    return None, tried, time.time() - start


# ------------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="bcrypt password recovery / verification (guess-and-check; bcrypt cannot be "
                    "decrypted). For your own test accounts only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples are in the module docstring at the top of this file.",
    )
    p.add_argument("--hash", required=True, help="The bcrypt hash to target, e.g. '$2b$10$...'.")
    p.add_argument("--info", action="store_true", help="Just print the hash breakdown and exit.")

    # candidate sources
    p.add_argument("--password", help="Verify this single candidate against the hash.")
    p.add_argument("--common", action="store_true", help="Try the built-in common-password list.")
    p.add_argument("--wordlist", help="Path to a wordlist file (one candidate per line).")
    p.add_argument("--brute", action="store_true", help="Brute force over --charset (small only!).")
    p.add_argument("--charset", choices=sorted(CHARSETS), default="alnum",
                   help="Character set for --brute (default: alnum).")
    p.add_argument("--min-len", type=int, default=1, help="Min length for --brute (default 1).")
    p.add_argument("--max-len", type=int, default=4, help="Max length for --brute (default 4).")
    p.add_argument("--rules", action="store_true",
                   help="Expand each candidate with case + common suffix mangling rules.")

    # control
    p.add_argument("--limit", type=int, default=0, help="Stop after N attempts (0 = no limit).")
    p.add_argument("--report-every", type=int, default=2000,
                   help="Progress line every N attempts to stderr (0 = quiet).")
    args = p.parse_args()

    try:
        info = parse_hash(args.hash)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2

    print_info(info)
    print()

    if args.info:
        return 0

    if not any([args.password is not None, args.common, args.wordlist, args.brute]):
        print("No candidate source given. Add one of: --password, --common, --wordlist, --brute.\n"
              "(bcrypt cannot be reversed — you can only test guesses.)", file=sys.stderr)
        return 2

    if args.brute:
        space = sum(len(CHARSETS[args.charset]) ** n
                    for n in range(args.min_len, args.max_len + 1))
        est_seconds = space / 15.0  # ~15 guesses/s at cost 10, order-of-magnitude only
        if space > 5_000_000:
            print(f"WARNING: brute keyspace is ~{space:,} combos. At bcrypt cost {info['cost']} that is "
                  f"on the order of {est_seconds/3600:,.1f} hours on one core. Consider --wordlist "
                  f"or a smaller --max-len.\n", file=sys.stderr)

    candidates, descr = build_candidates(args)
    print(f"Trying: {descr}")
    print("(guess-and-check against the stored salt+cost; bcrypt is intentionally slow)\n")

    found, tried, elapsed = try_recover(args.hash, candidates, args.limit, args.report_every)
    rate = tried / max(elapsed, 1e-9)
    print()
    if found is not None:
        print("=" * 60)
        print(f"  RECOVERED: {found!r}")
        print("=" * 60)
        print(f"  after {tried:,} attempts in {elapsed:.2f}s ({rate:,.0f}/s)")
        return 0
    print(f"Not found. Tried {tried:,} candidate(s) in {elapsed:.2f}s ({rate:,.0f}/s).")
    print("This does NOT mean the hash is broken — a strong password simply isn't guessable this way.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
