"""One-shot cleanup before migrating Location.phone + CallRecord.callerPhone
from TEXT to PHONES composite.

Deletes, in this order:
  1. All CallRecord records (they reference Person/Location).
  2. All Location records.
  3. Person records that are (a) created by our phone-based sync path —
     identified by telegramid being empty — AND (b) not referenced by any
     Task via the `klientId` field. Anything with a telegramid, or touched
     by a Task, is left alone.

Then drops the two problematic field_metadata entries so that the next
bootstrap run can re-create them as PHONES:
  - Location.phone   (TEXT)  → will be re-created as PHONES
  - CallRecord.callerPhone (TEXT) → same

Usage:
    TWENTY_BASE_URL=... TWENTY_API_KEY=... python scripts/wipe_twenty_phone_data.py
        [--apply]   # without this flag: prints what would be deleted

Safety: without --apply nothing is changed. With --apply, each DELETE is
logged so the operator can audit.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import Any

import httpx

# Twenty rate-limits writes to 100 tokens / 60s. Stay under 1.5 RPS to leave
# headroom and avoid bursting at cold start.
_RATE_LIMIT_RPS = 1.5
_MIN_INTERVAL = 1.0 / _RATE_LIMIT_RPS
_last_call = [0.0]


async def _throttle() -> None:
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
    if wait > 0:
        await asyncio.sleep(wait)
    _last_call[0] = time.monotonic()

logger = logging.getLogger(__name__)


async def _get_all(client: httpx.AsyncClient, path: str, plural: str) -> list[dict[str, Any]]:
    """Fetch records. Twenty's REST caps at limit=100; we assume the working
    set is below that (true on this project — verified via live audit). If
    a batch returns exactly 100, warn so the operator notices.
    """
    r = await client.get(path, params={"limit": 100})
    r.raise_for_status()
    items = r.json().get("data", {}).get(plural, [])
    if len(items) == 100:
        print(f"  WARN: {plural} returned 100 items — might be truncated", file=sys.stderr)
    return items


async def _delete_many(
    client: httpx.AsyncClient,
    path: str,
    ids: list[str],
    label: str,
    apply: bool,
) -> int:
    if not ids:
        print(f"  {label}: nothing to delete")
        return 0
    if not apply:
        print(f"  {label}: would delete {len(ids)} (dry-run)")
        for i in ids[:5]:
            print(f"    - {i}")
        if len(ids) > 5:
            print(f"    ... and {len(ids) - 5} more")
        return 0
    deleted = 0
    for i, rid in enumerate(ids, 1):
        await _throttle()
        try:
            r = await client.delete(f"{path}/{rid}")
            if r.status_code == 429:
                # Back off for the full window and retry once
                logger.warning("rate-limited on %s/%s — sleeping 62s", label, rid)
                await asyncio.sleep(62)
                r = await client.delete(f"{path}/{rid}")
            if r.status_code >= 400:
                logger.error("DELETE %s/%s failed: %s %s", path, rid, r.status_code,
                             r.text[:200])
            else:
                deleted += 1
        except Exception:
            logger.exception("DELETE %s/%s crashed", path, rid)
        if i % 20 == 0:
            print(f"  {label}: {i}/{len(ids)} deleted")
    print(f"  {label}: deleted {deleted}/{len(ids)}")
    return deleted


def _task_klient_id(t: dict[str, Any]) -> str | None:
    for k in ("klientId", "klient"):
        v = t.get(k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict) and v.get("id"):
            return str(v["id"])
    return None


async def _drop_text_fields(client: httpx.AsyncClient, apply: bool) -> None:
    """Drop Location.phone and CallRecord.callerPhone metadata so bootstrap
    can recreate them as PHONES."""
    r = await client.get("/rest/metadata/objects")
    r.raise_for_status()
    objects = r.json().get("data", {}).get("objects", [])
    by_name = {o["nameSingular"]: o for o in objects}
    targets: list[tuple[str, str]] = []  # (field_id, label)
    for obj_name, field_name in (("location", "phone"), ("callRecord", "callerPhone")):
        obj = by_name.get(obj_name)
        if not obj:
            print(f"  metadata: object {obj_name} not found, skipping")
            continue
        for f in obj.get("fields", []):
            if f.get("name") == field_name:
                targets.append((f["id"], f"{obj_name}.{field_name}"))
                break
        else:
            print(f"  metadata: {obj_name}.{field_name} field not found, skipping")
    if not targets:
        return
    if not apply:
        print("  metadata: would delete these field definitions (dry-run):")
        for fid, label in targets:
            print(f"    - {label} ({fid})")
        return
    for fid, label in targets:
        r = await client.delete(f"/rest/metadata/fields/{fid}")
        if r.status_code >= 400:
            logger.error("metadata DELETE %s failed: %s %s", label, r.status_code,
                         r.text[:200])
        else:
            print(f"  metadata: deleted {label}")


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Perform deletions. Without this flag nothing is changed.")
    parser.add_argument("--skip-metadata", action="store_true",
                        help="Skip dropping Location.phone / CallRecord.callerPhone field metadata.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    base = os.environ.get("TWENTY_BASE_URL", "").strip().rstrip("/")
    key = os.environ.get("TWENTY_API_KEY", "").strip()
    if not base or not key:
        print("ERROR: TWENTY_BASE_URL and TWENTY_API_KEY required", file=sys.stderr)
        return 2

    headers = {"Authorization": f"Bearer {key}"}
    print(f"Twenty: {base}   apply={args.apply}")

    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=30) as client:
        # ---- 1. Audit ----
        people = await _get_all(client, "/rest/people", "people")
        locations = await _get_all(client, "/rest/locations", "locations")
        calls = await _get_all(client, "/rest/callRecords", "callRecords")
        tasks = await _get_all(client, "/rest/tasks", "tasks")

        people_no_tg = [p for p in people if not (p.get("telegramid") or "").strip()]
        protected = {_task_klient_id(t) for t in tasks}
        protected.discard(None)
        people_to_delete = [p for p in people_no_tg if p["id"] not in protected]
        people_protected_by_task = [p for p in people_no_tg if p["id"] in protected]

        print(f"\nAUDIT:")
        print(f"  people total={len(people)} no_tg={len(people_no_tg)} "
              f"to_delete={len(people_to_delete)} "
              f"protected_by_task={len(people_protected_by_task)}")
        print(f"  locations total={len(locations)} to_delete={len(locations)}")
        print(f"  callRecords total={len(calls)} to_delete={len(calls)}")
        print(f"  tasks total={len(tasks)} (never touched)")

        if people_protected_by_task:
            print("\n  SKIPPED Person (referenced by Task.klient):")
            for p in people_protected_by_task[:20]:
                print(f"    - {p['id']}")

        # ---- 2. Delete in dependency order ----
        print("\nDELETIONS:")
        await _delete_many(client, "/rest/callRecords",
                           [c["id"] for c in calls], "callRecords", args.apply)
        await _delete_many(client, "/rest/locations",
                           [l["id"] for l in locations], "locations", args.apply)
        await _delete_many(client, "/rest/people",
                           [p["id"] for p in people_to_delete], "people", args.apply)

        # ---- 3. Drop TEXT field definitions ----
        if not args.skip_metadata:
            print("\nMETADATA:")
            await _drop_text_fields(client, args.apply)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
