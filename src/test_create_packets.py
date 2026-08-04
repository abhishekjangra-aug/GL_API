# src/test_create_packets.py
"""Bulk packet creation + appraiser assignment (admin-driven).

Standalone companion to `maintest.py`. It exercises ONLY the packet half of the
Gold Loan flow -- no customer, no KYC, no loan -- so you can seed a pool of empty
packets bound to an appraiser ahead of time (or just regression-test the packet
endpoints on their own).

What it does, per packet:
    1. POST /api/packet          (ADMIN)  -> create an empty packet
    2. GET  /api/packet?from=1&to=N (ADMIN)  -> resolve its numeric id from the listing
    3. PUT  /api/packet/{id}     (ADMIN)  -> assign it to the appraiser (userType 'appraiser')

Why two logins: packet create/assign are ADMIN-only, but the packet must be bound
to the APPRAISER's user id. So the run logs in as the appraiser first purely to
read its user id + branch from the JWT, then swaps `auth_token` to an admin token
for the actual calls -- exactly what `add_packet_images` does in the main harness.
Role tokens are cached per mobile by `_role_token`, so each role logs in once even
across many packets (the OTP endpoint rate-limits repeat sends to a number).

Usage:
    python src/test_create_packets.py                          # 5 packets on TEST, assigned to the appraiser
    python src/test_create_packets.py --count 20               # 20 packets
    python src/test_create_packets.py --env uat --count 10     # on UAT (gfau)
    python src/test_create_packets.py --count 5 --no-assign    # create only, leave unassigned
    python src/test_create_packets.py --appraiser-id 1473      # bind to a specific appraiser user id
    python src/test_create_packets.py --count 50 --delay 0.3   # throttle between packets

Exit code is 0 only if every requested packet was created (and assigned, unless
--no-assign). Failures are collected and listed at the end rather than aborting
the batch, so one bad packet doesn't cost you the other 19.
"""

import argparse
import asyncio
import os
import sys

# Allow `python src/test_create_packets.py` from any cwd: put this file's dir on the path
# so `maintest` resolves whether or not the project root is the working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402  (after sys.path fix)

from maintest import GoldLoanApiTest  # noqa: E402


class PacketBatchTest:
    """Creates N packets as admin and assigns each to an appraiser.

    Wraps a `GoldLoanApiTest` instance rather than subclassing it: everything needed
    (auth, HMAC signing, create_packet, assign_packet, the listing lookup) already
    lives on that class and is reused as-is, so this file stays a thin driver and
    does not duplicate any request bodies.
    """

    def __init__(self, suite: GoldLoanApiTest, count: int, assign: bool,
                 appraiser_id=None, delay: float = 0.0, listing_page_size: int = 50):
        self.suite = suite
        self.count = count
        self.assign = assign
        self.appraiser_id_override = appraiser_id
        self.delay = delay
        self.listing_page_size = listing_page_size
        self.results = []          # one dict per attempted packet
        self.appraiser_id = None
        self.appraiser_token = ""

    # --- setup -------------------------------------------------------------------------------

    async def resolve_appraiser(self, login_type: str = "appraiser"):
        """Log in as the appraiser to learn the user id + branch the packets bind to.

        `--appraiser-id` skips nothing here: we still need a session, and the branch id
        comes from this JWT. It only overrides which user the packet is assigned to.
        """
        s = self.suite
        s._log_step("Authentication (appraiser - for user id + branch)")
        mobile = s._get_mobile_number_for_login_type(login_type)
        await s.login(mobile)
        assert s.auth_token, "Login failed: auth_token empty after login."
        self.appraiser_token = s.auth_token
        self.appraiser_id = self.appraiser_id_override or s.logged_in_user_id
        assert self.appraiser_id, (
            "Could not determine the appraiser user id (JWT had no 'id'). "
            "Pass --appraiser-id explicitly."
        )
        print(f"Appraiser user id : {self.appraiser_id}"
              + ("  (overridden via --appraiser-id)" if self.appraiser_id_override else ""))
        print(f"Internal branch id: {s.internal_branch_id}")

    # --- the batch ---------------------------------------------------------------------------

    async def create_one(self, index: int) -> dict:
        """Create a single packet and (optionally) assign it. Returns a result record.

        Clears `available_packet` first: `create_packet` leaves the PREVIOUS packet in
        place when its listing lookup fails, which in a loop would make `assign_packet`
        silently re-assign packet N-1. Resetting it turns that into a visible skip.
        """
        s = self.suite
        s.available_packet = {}
        s._created_packet_unique = None
        record = {"n": index, "unique": None, "id": None,
                  "created": False, "assigned": False, "error": None}
        try:
            await s.create_packet()
            record["unique"] = getattr(s, "_created_packet_unique", None)
            record["created"] = True

            packet = s.available_packet if isinstance(s.available_packet, dict) else {}
            packet_id = packet.get("id")
            if not packet_id and record["unique"] and self.listing_page_size > 50:
                # create_packet's own lookup is hardcoded to the first 50 rows. On a busy
                # environment a fresh packet can fall past that, so retry once wider.
                print(f"  -> retrying id lookup over {self.listing_page_size} listing rows")
                packet = await s._find_packet_by_unique_id(
                    record["unique"], page_size=self.listing_page_size) or {}
                if packet.get("id"):
                    s.available_packet = packet
                packet_id = packet.get("id")
            if not packet_id:
                record["error"] = (f"created, but id not found in the first "
                                   f"{max(self.listing_page_size, 50)} listing rows -- not assigned")
                return record
            # Guard against assigning a stale row if the listing ever returns a mismatch.
            listed_unique = packet.get("packetUniqueId") or packet.get("barcodeNumber")
            if record["unique"] and listed_unique and listed_unique != record["unique"]:
                record["error"] = (f"listing returned {listed_unique!r} for "
                                   f"{record['unique']!r} -- not assigned")
                return record
            record["id"] = packet_id

            if self.assign:
                await s.assign_packet(self.appraiser_id)
                record["assigned"] = True
        except httpx.HTTPStatusError as e:
            record["error"] = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        except Exception as e:  # noqa: BLE001 - one bad packet must not kill the batch
            record["error"] = f"{type(e).__name__}: {e}"
        return record

    async def run(self):
        s = self.suite
        s._banner(
            "GOLD LOAN - BULK PACKET CREATION + ASSIGNMENT",
            f"env={s.env_name.upper()}   packets={self.count}   "
            f"assign={'yes' if self.assign else 'no'}   base={s.BASE_URL}",
        )
        try:
            await self.resolve_appraiser()

            s._log_step(f"Create {self.count} packet(s) as ADMIN"
                        + (f" and assign to appraiser {self.appraiser_id}" if self.assign else ""))
            # Packet create/assign are admin-only; swap the token for the whole batch and
            # restore the appraiser session afterwards (same pattern as add_packet_images).
            saved_token = s.auth_token
            try:
                s.auth_token = await s._admin_token()
                for i in range(1, self.count + 1):
                    print(s._c(f"\n{s._G['corner']} packet {i}/{self.count}", "cyan", "bold"))
                    self.results.append(await self.create_one(i))
                    if self.delay and i < self.count:
                        await asyncio.sleep(self.delay)
            finally:
                s.auth_token = saved_token

            passed = self.report()
            s._print_run_metrics(passed=passed)
            return passed
        except Exception as e:
            s._banner("RESULT - FAILED", f"{type(e).__name__}: {e}")
            self.report()
            s._print_run_metrics(passed=False)
            raise

    # --- reporting ---------------------------------------------------------------------------

    def report(self) -> bool:
        """Print the per-packet table and the verdict. Returns True if the batch fully succeeded."""
        s = self.suite
        ok = [r for r in self.results
              if r["created"] and (r["assigned"] or not self.assign) and not r["error"]]
        bad = [r for r in self.results if r not in ok]

        print("\n" + s._c(f"{s._G['square']} PACKETS", "bold", "cyan"))
        header = f"  {'#':<4} {'packetUniqueId / barcode':<22} {'id':<10} {'status'}"
        print(s._c(header, "gray"))
        for r in self.results:
            if r["error"]:
                status, color = f"{s._G['fail']} {r['error']}", "red"
            elif r["assigned"]:
                status, color = f"{s._G['ok']} created + assigned to {self.appraiser_id}", "green"
            elif r["created"]:
                status, color = f"{s._G['ok']} created (unassigned)", "green"
            else:
                status, color = f"{s._G['fail']} not created", "red"
            print(s._c(f"  {r['n']:<4} {str(r['unique'] or '-'):<22} "
                       f"{str(r['id'] or '-'):<10} {status}", color))

        s._print_summary_table("BATCH SUMMARY", {
            "Environment": f"{s.env_name.upper()} ({s.BASE_URL})",
            "Requested": self.count,
            "Succeeded": len(ok),
            "Failed": len(bad),
            "Appraiser id": self.appraiser_id if self.assign else "(not assigned)",
            "Branch id": s.internal_branch_id,
            "Packet ids": ", ".join(str(r["id"]) for r in ok if r["id"]) or "-",
        })

        passed = bool(self.results) and not bad
        s._banner(
            "RESULT - PASSED" if passed else "RESULT - FAILED",
            f"{len(ok)}/{self.count} packet(s) "
            + ("created + assigned" if self.assign else "created"),
        )
        return passed


def main():
    parser = argparse.ArgumentParser(
        prog="test_create_packets.py",
        description="Create multiple Gold Loan packets as ADMIN and assign each to an appraiser.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python src/test_create_packets.py                       # 5 packets on TEST\n"
               "  python src/test_create_packets.py --count 20            # 20 packets\n"
               "  python src/test_create_packets.py --env uat --count 10  # on UAT (gfau)\n"
               "  python src/test_create_packets.py --no-assign           # create only\n"
               "  python src/test_create_packets.py --appraiser-id 1473   # bind to a given appraiser\n",
    )
    parser.add_argument("--env", choices=sorted(GoldLoanApiTest.ENVIRONMENTS),
                        default=os.getenv("GOLD_LOAN_ENV", GoldLoanApiTest.DEFAULT_ENV).strip().lower(),
                        help="Target environment (default: %(default)s). 'test' = gfat, 'uat' = gfau.")
    parser.add_argument("--count", "-n", type=int, default=5,
                        help="How many packets to create (default: %(default)s).")
    parser.add_argument("--no-assign", dest="assign", action="store_false",
                        help="Create the packets but leave them unassigned (skip the PUT).")
    parser.add_argument("--appraiser-id", type=int, default=None,
                        help="Assign to this appraiser USER id instead of the logged-in appraiser's.")
    parser.add_argument("--login", default="appraiser",
                        help="Role logged in to source the user id + branch (default: %(default)s).")
    parser.add_argument("--branch", default=None,
                        help="Override internalUserBranch (default: taken from the login JWT).")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds to wait between packets (default: %(default)s). "
                             "Use a small value for large batches.")
    parser.add_argument("--listing-size", type=int, default=50,
                        help="Rows fetched from /api/packet when resolving a new packet's numeric "
                             "id (default: %(default)s). Raise it if lookups start missing.")
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be at least 1.")

    # Environment must be set BEFORE the suite is constructed: it selects the base URL
    # and every role login mobile.
    os.environ["GOLD_LOAN_ENV"] = args.env
    if args.branch:
        os.environ["GOLD_LOAN_INTERNAL_BRANCH_ID"] = str(args.branch)

    suite = GoldLoanApiTest()
    print(f">> Environment: {args.env.upper()} ({suite.BASE_URL})")
    print(f">> Packets: {args.count}   |  assign to appraiser: "
          f"{'yes' if args.assign else 'no'}"
          + (f" (id {args.appraiser_id})" if args.appraiser_id else ""))

    batch = PacketBatchTest(
        suite,
        count=args.count,
        assign=args.assign,
        appraiser_id=args.appraiser_id,
        delay=args.delay,
        listing_page_size=args.listing_size,
    )

    async def _run():
        try:
            return await batch.run()
        finally:
            await suite.client.aclose()

    try:
        passed = asyncio.run(_run())
    except Exception:
        sys.exit(1)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
