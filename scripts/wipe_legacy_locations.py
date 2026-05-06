"""Удалить все Twenty Location, оставшиеся от прежней модели «телефон → точка».

После перехода на N:M (один телефон может относиться к нескольким точкам,
точка может иметь несколько телефонов) каталог точек ведётся импортом из
xlsx. Старые 140 авто-созданных Location без brand/number/displayName
больше не нужны и могут запутывать резолв.

Удаление безопасно: FK person/task/_callRecord/noteTarget/attachment/
taskTarget/timelineActivity/favorite на Location используют ON DELETE SET
NULL — связанные записи не пропадают, просто потеряют locationRelId.

Usage:
    TWENTY_BASE_URL=... TWENTY_API_KEY=... \\
        python scripts/wipe_legacy_locations.py [--apply]

Без --apply: dry-run, печатает счёт удаляемых.
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

logger = logging.getLogger("wipe_legacy_locations")

_RATE_LIMIT_RPS = 1.5
_MIN_INTERVAL = 1.0 / _RATE_LIMIT_RPS
_last_call = [0.0]


async def _throttle() -> None:
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
    if wait > 0:
        await asyncio.sleep(wait)
    _last_call[0] = time.monotonic()


async def _list_all(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(20):
        params: dict[str, Any] = {"limit": 100}
        if cursor:
            params["starting_after"] = cursor
        r = await client.get("/rest/locations", params=params)
        r.raise_for_status()
        page = r.json().get("data", {}).get("locations", []) or []
        if not page:
            break
        items.extend(page)
        if len(page) < 100:
            break
        cursor = page[-1].get("id")
        if not cursor:
            break
    return items


async def main_async(base_url: str, api_key: str, apply: bool) -> int:
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    ) as client:
        items = await _list_all(client)
        print(f"locations to delete: {len(items)}")
        for sample in items[:5]:
            phone = (sample.get("phone") or {}).get("primaryPhoneNumber")
            print(
                f"  {sample.get('id')} displayName={sample.get('displayName')!r} "
                f"phone={phone!r} prefix={sample.get('prefix')!r} number={sample.get('number')!r}"
            )
        if not apply:
            print("\n(dry-run; pass --apply to actually delete)")
            return 0
        deleted = 0
        for i, loc in enumerate(items, 1):
            await _throttle()
            try:
                r = await client.delete(f"/rest/locations/{loc['id']}")
                if r.status_code == 429:
                    logger.warning("rate-limited at %s — sleep 62s", loc["id"])
                    await asyncio.sleep(62)
                    r = await client.delete(f"/rest/locations/{loc['id']}")
                if r.status_code >= 400:
                    logger.error("DELETE %s failed: %s %s", loc["id"], r.status_code, r.text[:200])
                else:
                    deleted += 1
            except Exception:
                logger.exception("DELETE %s crashed", loc["id"])
            if i % 25 == 0:
                print(f"  progress: {i}/{len(items)} deleted={deleted}")
        print(f"\ndeleted {deleted}/{len(items)}")
        return 0 if deleted == len(items) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    base_url = os.environ.get("TWENTY_BASE_URL")
    api_key = os.environ.get("TWENTY_API_KEY")
    if not base_url or not api_key:
        print("TWENTY_BASE_URL and TWENTY_API_KEY required", file=sys.stderr)
        return 2
    return asyncio.run(main_async(base_url, api_key, apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
