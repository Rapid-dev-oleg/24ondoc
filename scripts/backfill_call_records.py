"""Backfill ats_call_records → Twenty CallRecord.

Intended to be run once per environment after Stage 2 bootstrap creates
the Twenty CallRecord object. Iterates `ats_call_records` ordered by
created_at, skips already-synced entries (detected by atsCallId lookup),
rate-limits to stay polite to the Twenty API, and prints a final summary.

Usage:
    TWENTY_BASE_URL=... TWENTY_API_KEY=... DATABASE_URL=... \\
        python scripts/backfill_call_records.py [--limit 10] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# Allow running from repo root: add backend/src to PYTHONPATH
BACKEND_SRC = Path(__file__).resolve().parent.parent / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from ats_processing.application.sync_call_to_twenty import SyncCallToTwentyUseCase  # noqa: E402
from ats_processing.domain.models import CallRecord, CallStatus, SourceType  # noqa: E402
from twenty_integration.infrastructure.twenty_adapter import TwentyRestAdapter  # noqa: E402

# Twenty limits writes to ~100 tokens / 60s. Each record can emit up to
# ~5 POSTs (Person, Location, CallRecord + lookups), so we stay under 15
# records/min = one every 4 seconds.
RATE_LIMIT_RPS = 0.25


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Max rows to process (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Read from DB but don't call Twenty")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    base_url = os.environ.get("TWENTY_BASE_URL", "").strip()
    api_key = os.environ.get("TWENTY_API_KEY", "").strip()
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not base_url or not api_key or not db_url:
        print("ERROR: TWENTY_BASE_URL, TWENTY_API_KEY, DATABASE_URL required",
              file=sys.stderr)
        return 2

    # asyncpg driver if plain postgres://
    if db_url.startswith("postgres://"):
        db_url = "postgresql+asyncpg://" + db_url[len("postgres://"):]
    elif db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
        db_url = "postgresql+asyncpg://" + db_url[len("postgresql://"):]

    adapter = TwentyRestAdapter(base_url=base_url, api_key=api_key)
    use_case = SyncCallToTwentyUseCase(twenty_port=adapter)

    engine = create_async_engine(db_url)
    query = """
        SELECT call_id, audio_url, source, transcription_t2, transcription_whisper,
               duration, caller_phone, agent_ext, detected_agent_id, voice_match_score,
               status, session_id, created_at
        FROM ats_call_records
        ORDER BY created_at ASC
    """
    if args.limit:
        query += f" LIMIT {int(args.limit)}"

    total = created = existing = errors = 0
    min_interval = 1.0 / RATE_LIMIT_RPS
    last_call = 0.0

    async with engine.connect() as conn:
        result = await conn.execute(text(query))
        rows = result.mappings().all()

    print(f"Backfilling {len(rows)} call records (dry_run={args.dry_run})...")

    for row in rows:
        total += 1
        try:
            record = CallRecord(
                call_id=row["call_id"],
                audio_url=row["audio_url"] or "",
                source=SourceType(row["source"]) if row["source"] else SourceType.CALL_T2_WEBHOOK,
                transcription_t2=row["transcription_t2"],
                transcription_whisper=row["transcription_whisper"],
                duration=row["duration"],
                caller_phone=row["caller_phone"],
                agent_ext=row["agent_ext"],
                detected_agent_id=row["detected_agent_id"],
                voice_match_score=row["voice_match_score"],
                status=CallStatus(row["status"]) if row["status"] else CallStatus.NEW,
                session_id=row["session_id"],
                created_at=row["created_at"],
            )
        except Exception:
            logging.exception("Skipping malformed row %s", row.get("call_id"))
            errors += 1
            continue

        if args.dry_run:
            print(f"  [dry] {record.call_id} phone={record.caller_phone} status={record.status}")
            continue

        # Rate limit
        wait = min_interval - (time.monotonic() - last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        last_call = time.monotonic()

        try:
            sr = await use_case.execute(record)
            if sr.twenty_id is None:
                errors += 1
            elif sr.created:
                created += 1
            else:
                existing += 1
            if total % 25 == 0:
                print(f"  progress: {total}/{len(rows)} (created={created}, "
                      f"existing={existing}, errors={errors})")
        except Exception:
            logging.exception("Sync failed for %s", record.call_id)
            errors += 1

    await engine.dispose()

    print("=" * 60)
    print(f"Total processed: {total}")
    print(f"Created in Twenty:  {created}")
    print(f"Already existing:   {existing}")
    print(f"Errors:             {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
