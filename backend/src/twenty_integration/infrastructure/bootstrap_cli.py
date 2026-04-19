"""CLI entrypoint for `make twenty-bootstrap` — idempotent schema setup."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from src.twenty_integration.infrastructure.bootstrap import ensure_twenty_schema
from src.twenty_integration.infrastructure.twenty_adapter import TwentyRestAdapter


async def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    base_url = os.environ.get("TWENTY_BASE_URL", "").strip()
    api_key = os.environ.get("TWENTY_API_KEY", "").strip()
    if not base_url or not api_key:
        print("ERROR: TWENTY_BASE_URL and TWENTY_API_KEY must be set", file=sys.stderr)
        return 2

    adapter = TwentyRestAdapter(base_url=base_url, api_key=api_key)
    report = await ensure_twenty_schema(adapter)

    print("=" * 60)
    print(f"Objects created:   {report.objects_created}")
    print(f"Objects existing:  {report.objects_existing}")
    print(f"Fields created:    {len(report.fields_created)}")
    for key in report.fields_created:
        print(f"  + {key}")
    print(f"Fields existing:   {len(report.fields_existing)}")
    if report.errors:
        print(f"ERRORS ({len(report.errors)}):", file=sys.stderr)
        for err in report.errors:
            print(f"  ! {err}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
