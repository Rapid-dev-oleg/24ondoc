"""Stage 2 — Twenty schema bootstrap idempotency tests.

Uses a fake adapter that mimics Twenty's list/create metadata endpoints.
Verifies:
- Missing objects and fields get created.
- Re-running the same bootstrap is a no-op (nothing gets created twice).
- Relations resolve to target object IDs after the first pass.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.twenty_integration.infrastructure.bootstrap import (
    CALL_RECORD,
    LOCATION,
    PERSON_EXTRA_FIELDS,
    PERSON_RELATIONS,
    TASK_EXTRA_FIELDS,
    TASK_LOG,
    TASK_RELATIONS,
    ensure_twenty_schema,
)


class FakeTwentyAdapter:
    """In-memory stand-in for the metadata endpoints of TwentyRestAdapter."""

    def __init__(self, seed_objects: list[dict[str, Any]] | None = None) -> None:
        self._objects: dict[str, dict[str, Any]] = {}
        for obj in seed_objects or []:
            self._objects[obj["nameSingular"]] = {
                "id": obj.get("id", f"obj-{obj['nameSingular']}"),
                "nameSingular": obj["nameSingular"],
                "namePlural": obj.get("namePlural", obj["nameSingular"] + "s"),
                "fields": list(obj.get("fields", [])),
            }
        self.create_object_calls: list[dict[str, Any]] = []
        self.create_field_calls: list[dict[str, Any]] = []

    async def list_objects_metadata(self) -> list[dict[str, Any]]:
        return [dict(o) for o in self._objects.values()]

    async def create_object_metadata(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.create_object_calls.append(spec)
        ns = spec["nameSingular"]
        obj_id = f"obj-{ns}"
        self._objects[ns] = {
            "id": obj_id,
            "nameSingular": ns,
            "namePlural": spec.get("namePlural", ns + "s"),
            "fields": [],
        }
        return {"id": obj_id, **spec}

    async def create_field_metadata(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.create_field_calls.append(spec)
        target_obj = None
        for obj in self._objects.values():
            if obj["id"] == spec["objectMetadataId"]:
                target_obj = obj
                break
        if target_obj is None:
            raise RuntimeError(f"Unknown objectMetadataId {spec['objectMetadataId']}")
        target_obj["fields"].append({"name": spec["name"], "type": spec["type"]})
        return {"id": f"fld-{target_obj['nameSingular']}-{spec['name']}", **spec}


def _seed_with_task_and_person() -> list[dict[str, Any]]:
    return [
        {
            "nameSingular": "task",
            "namePlural": "tasks",
            "fields": [
                {"name": "title", "type": "TEXT"},
                {"name": "povtornoeObrashchenie", "type": "BOOLEAN"},
                {"name": "klient", "type": "RELATION"},
                {"name": "kompaniya", "type": "RELATION"},
            ],
            "id": "obj-task",
        },
        {
            "nameSingular": "person",
            "namePlural": "people",
            "fields": [
                {"name": "name", "type": "FULL_NAME"},
                {"name": "phones", "type": "PHONES"},
                {"name": "telegramid", "type": "TEXT"},
            ],
            "id": "obj-person",
        },
    ]


@pytest.mark.asyncio
async def test_bootstrap_creates_all_missing_objects_and_fields() -> None:
    adapter = FakeTwentyAdapter(seed_objects=_seed_with_task_and_person())

    report = await ensure_twenty_schema(adapter)

    assert set(report.objects_created) == {"location", "callRecord", "taskLog"}
    assert not report.objects_existing
    assert not report.errors

    # All declared task/person extra non-relation fields were created
    task_created = {k for k in report.fields_created if k.startswith("task.")}
    for spec in TASK_EXTRA_FIELDS:
        assert f"task.{spec.name}" in task_created
    for spec in TASK_RELATIONS:
        assert f"task.{spec.name}" in task_created

    person_created = {k for k in report.fields_created if k.startswith("person.")}
    for spec in PERSON_EXTRA_FIELDS + PERSON_RELATIONS:
        assert f"person.{spec.name}" in person_created

    # Location fields created
    for spec in LOCATION.fields:
        assert f"location.{spec.name}" in report.fields_created


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent() -> None:
    adapter = FakeTwentyAdapter(seed_objects=_seed_with_task_and_person())

    await ensure_twenty_schema(adapter)
    created_after_first = len(adapter.create_field_calls) + len(adapter.create_object_calls)

    report2 = await ensure_twenty_schema(adapter)

    # No additional writes
    assert len(adapter.create_object_calls) == len(
        [c for c in adapter.create_object_calls]  # noqa: C416 — keep type
    )
    created_after_second = len(adapter.create_field_calls) + len(adapter.create_object_calls)
    assert created_after_first == created_after_second

    assert not report2.objects_created
    assert not report2.fields_created
    assert set(report2.objects_existing) == {"location", "callRecord", "taskLog"}
    assert not report2.errors


@pytest.mark.asyncio
async def test_relation_fields_carry_target_object_id() -> None:
    adapter = FakeTwentyAdapter(seed_objects=_seed_with_task_and_person())

    await ensure_twenty_schema(adapter)

    rel_calls = [c for c in adapter.create_field_calls if c["type"] == "RELATION"]
    assert rel_calls, "expected some relation fields to be created"
    for call in rel_calls:
        rel = call.get("relationCreationPayload")
        assert rel is not None, f"relation {call['name']} missing relationCreationPayload"
        assert rel["targetObjectMetadataId"], (
            f"relation {call['name']} has empty target id"
        )
        assert rel["type"] in ("MANY_TO_ONE", "ONE_TO_MANY")


@pytest.mark.asyncio
async def test_select_fields_include_options() -> None:
    adapter = FakeTwentyAdapter(seed_objects=_seed_with_task_and_person())

    await ensure_twenty_schema(adapter)

    select_calls = [c for c in adapter.create_field_calls if c["type"] == "SELECT"]
    assert select_calls
    for call in select_calls:
        assert call.get("options"), f"SELECT field {call['name']} missing options"
        for idx, opt in enumerate(call["options"]):
            assert "label" in opt and "value" in opt
            assert "id" in opt, f"SELECT option missing id in {call['name']}"
            assert opt["position"] == idx


@pytest.mark.asyncio
async def test_bootstrap_does_not_touch_existing_custom_fields_on_task() -> None:
    """povtornoeObrashchenie / klient / kompaniya are pre-existing on Task — skip."""
    adapter = FakeTwentyAdapter(seed_objects=_seed_with_task_and_person())

    await ensure_twenty_schema(adapter)

    for call in adapter.create_field_calls:
        if call["objectMetadataId"] == "obj-task":
            assert call["name"] not in {"povtornoeObrashchenie", "klient", "kompaniya"}
