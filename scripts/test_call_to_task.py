"""One-shot test: run the full call → task flow against N recent calls.

Purpose: verify end-to-end that the production code path still works
after the reshuffle. For each selected call:
  1. Run AI classify on its transcript.
  2. Build a synthetic DraftSession with that ClassificationResult.
  3. Call CreateTwentyTaskFromSession.execute() — the real use case,
     which exercises Person/Location resolve, DetectRepeat, create_task
     with povtornoeObrashchenie + parentTaskId.
  4. Call SyncCallToTwentyUseCase.execute() — which will update the
     already-existing Twenty CallRecord with taskRelId and run
     check_script.
  5. Tasks are prefixed "[TEST] " so they can be found and deleted
     later in Twenty UI.

Safety:
  * --dry-run by default — prints what WOULD happen, changes nothing.
  * --apply to actually create. Safe to interrupt mid-run.
  * --limit 3 by default.
  * Handles errors per-call; a failure on one call doesn't stop the
    others.

Usage (inside backend container):
    TWENTY_BASE_URL=... TWENTY_API_KEY=... DATABASE_URL=... \\
    OPENROUTER_API_KEY=... python3 /tmp/test_call_to_task.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path("/app/src")))  # inside container
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from ai_classification.domain.models import (  # noqa: E402
    Category, ClassificationEntities, ClassificationResult, Priority,
)
from ai_classification.infrastructure.openrouter_adapter import OpenRouterAdapter  # noqa: E402
from ats_processing.application.sync_call_to_twenty import SyncCallToTwentyUseCase  # noqa: E402
from ats_processing.domain.models import CallRecord, CallStatus, SourceType  # noqa: E402
from telegram_ingestion.domain.models import (  # noqa: E402
    DraftSession, SessionStatus, SourceType as IngestSource,
)
from twenty_integration.application.use_cases import CreateTwentyTaskFromSession  # noqa: E402
from twenty_integration.infrastructure.twenty_adapter import TwentyRestAdapter  # noqa: E402

logger = logging.getLogger(__name__)


async def _pick_calls(engine, limit: int) -> list[CallRecord]:
    """Last N answered calls with a non-empty transcript."""
    q = text("""
        SELECT call_id, audio_url, source, transcription_t2, transcription_whisper,
               duration, caller_phone, agent_ext, detected_agent_id, voice_match_score,
               status, session_id, twenty_task_id, created_at
        FROM ats_call_records
        WHERE COALESCE(transcription_whisper, transcription_t2) IS NOT NULL
          AND caller_phone IS NOT NULL
          AND status IN ('created','preview','processing')
        ORDER BY created_at DESC
        LIMIT :lim
    """)
    async with engine.connect() as conn:
        rows = (await conn.execute(q, {"lim": limit})).mappings().all()
    out = []
    for row in rows:
        out.append(CallRecord(
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
            twenty_task_id=row["twenty_task_id"],
            created_at=row["created_at"],
        ))
    return out


def _build_session(call: CallRecord, ai_result: ClassificationResult) -> DraftSession:
    """Synthesize a DraftSession that CreateTwentyTaskFromSession will accept."""
    session = DraftSession(
        session_id=call.session_id or uuid.uuid4(),
        user_id=0,  # dummy operator id; assignee will be None
        status=SessionStatus.PREVIEW,
        source_type=IngestSource.CALL_T2,
    )
    session.ai_result = ai_result
    return session


async def _process(
    call: CallRecord,
    ai: OpenRouterAdapter,
    create_task_uc: CreateTwentyTaskFromSession,
    sync_call_uc: SyncCallToTwentyUseCase,
    title_prefix: str,
    apply: bool,
) -> dict:
    transcript = call.get_best_transcription() or ""
    print(f"\n── call {call.call_id}  phone={call.caller_phone}  status={call.status.value}")
    print(f"   transcript: {transcript[:180]!r}")

    # 1. classify
    classify = await ai.classify(transcript)
    ai_result = ClassificationResult(
        source_text=transcript,
        title=title_prefix + (classify.title or "без заголовка")[:120],
        description=classify.description or "",
        category=classify.category or Category.BUG,
        priority=classify.priority or Priority.MEDIUM,
        deadline=None,
        entities=classify.entities or ClassificationEntities(),
        assignee_hint=None,
    )
    print(f"   classified:  title={ai_result.title!r}  cat={ai_result.category}  "
          f"prio={ai_result.priority}")

    if not apply:
        print("   [dry-run] would create task in Twenty; skipped")
        return {"call_id": call.call_id, "applied": False}

    # 2. create task via real use case
    session = _build_session(call, ai_result)
    task = await create_task_uc.execute(
        session=session,
        telegram_id=0,
        user_name="TestRunner",
        caller_phone=call.caller_phone,
        dialogue_text=transcript,
    )
    print(f"   ✅ task={task.twenty_id}  title={task.title!r}")

    # 3. re-run call sync so CallRecord.taskRelId picks up the new link
    #    (ats_call_records.twenty_task_id is already set by the use case)
    call.twenty_task_id = task.twenty_id
    sr = await sync_call_uc.execute(call, task_id=task.twenty_id)
    print(f"   ✅ sync result: callRecord_twenty_id={sr.twenty_id}  "
          f"created={sr.created}  linked={sr.linked_task}")

    return {"call_id": call.call_id, "applied": True, "task_id": task.twenty_id}


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--apply", action="store_true",
                        help="Actually create tasks (default: dry-run).")
    parser.add_argument("--title-prefix", default="[TEST] ",
                        help="Prefix added to task titles for easy cleanup.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    base_url = os.environ.get("TWENTY_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("TWENTY_API_KEY", "").strip()
    db_url = os.environ.get("DATABASE_URL", "").strip()
    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip() \
        or os.environ.get("OPENAI_API_KEY", "").strip()
    if not (base_url and api_key and db_url and or_key):
        print("ERROR: need TWENTY_BASE_URL, TWENTY_API_KEY, DATABASE_URL, OPENROUTER_API_KEY",
              file=sys.stderr)
        return 2

    if db_url.startswith("postgres://"):
        db_url = "postgresql+asyncpg://" + db_url[len("postgres://"):]
    elif db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
        db_url = "postgresql+asyncpg://" + db_url[len("postgresql://"):]

    engine = create_async_engine(db_url)
    adapter = TwentyRestAdapter(base_url=base_url, api_key=api_key)
    ai = OpenRouterAdapter(api_key=or_key)

    create_task_uc = CreateTwentyTaskFromSession(port=adapter, ai_port=ai)
    sync_call_uc = SyncCallToTwentyUseCase(twenty_port=adapter, script_ai=ai)

    calls = await _pick_calls(engine, args.limit)
    print(f"Picked {len(calls)} call(s) (apply={args.apply})")

    summary = []
    for c in calls:
        try:
            result = await _process(
                c, ai, create_task_uc, sync_call_uc, args.title_prefix, args.apply,
            )
            summary.append(result)
        except Exception as exc:
            logger.exception("call %s failed", c.call_id)
            summary.append({"call_id": c.call_id, "error": str(exc)})

    print("\n" + "=" * 60)
    for r in summary:
        print(f"  {r}")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
