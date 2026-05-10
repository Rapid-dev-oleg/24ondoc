"""Twenty schema bootstrap idempotency tests.

The bootstrap now talks to GraphQL `/metadata` (gql_create_object,
gql_create_field, gql_update_object, gql_update_field). FakeTwentyAdapter
mimics that surface in memory and lets us assert no duplicate writes on
re-runs and that label-identifier repointing happens for Location.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from src.twenty_integration.infrastructure.bootstrap import (
    CALL_RECORD,
    LOCATION,
    TASK_EXTRA_FIELDS,
    TASK_RELATIONS,
    ensure_twenty_schema,
)


class FakeTwentyAdapter:
    """In-memory stand-in for the GraphQL metadata surface of TwentyRestAdapter."""

    def __init__(self, seed_objects: list[dict[str, Any]] | None = None) -> None:
        self._objects: dict[str, dict[str, Any]] = {}
        for obj in seed_objects or []:
            self._objects[obj["nameSingular"]] = {
                "id": obj.get("id", f"obj-{obj['nameSingular']}"),
                "nameSingular": obj["nameSingular"],
                "namePlural": obj.get("namePlural", obj["nameSingular"] + "s"),
                "isLabelSyncedWithName": obj.get("isLabelSyncedWithName", False),
                "labelIdentifierFieldMetadataId": obj.get(
                    "labelIdentifierFieldMetadataId"
                ),
                "fields": list(obj.get("fields", [])),
            }
        self.gql_create_object_calls: list[dict[str, Any]] = []
        self.gql_create_field_calls: list[dict[str, Any]] = []
        self.gql_update_object_calls: list[tuple[str, dict[str, Any]]] = []
        self.gql_update_field_calls: list[tuple[str, dict[str, Any]]] = []

    async def list_objects_metadata(self) -> list[dict[str, Any]]:
        return [dict(o) for o in self._objects.values()]

    async def gql_create_object(
        self,
        *,
        name_singular: str,
        name_plural: str,
        label_singular: str,
        label_plural: str,
        description: str = "",
        icon: str = "IconBuilding",
        skip_name_field: bool = False,
        is_label_synced_with_name: bool = False,
    ) -> dict[str, Any]:
        spec = {
            "nameSingular": name_singular,
            "namePlural": name_plural,
            "labelSingular": label_singular,
            "labelPlural": label_plural,
            "description": description,
            "icon": icon,
            "skipNameField": skip_name_field,
            "isLabelSyncedWithName": is_label_synced_with_name,
        }
        self.gql_create_object_calls.append(spec)
        obj_id = f"obj-{name_singular}"
        # Mimic Twenty's auto-creation of the default `name` TEXT field unless skipped.
        seed_fields: list[dict[str, Any]] = []
        seed_lid: str | None = None
        if not skip_name_field:
            name_field_id = f"fld-{name_singular}-name"
            seed_fields.append({
                "id": name_field_id,
                "name": "name",
                "type": "TEXT",
                "isNullable": True,
                "isUnique": False,
                "isLabelSyncedWithName": False,
                "label": "Name",
            })
            seed_lid = name_field_id
        self._objects[name_singular] = {
            "id": obj_id,
            "nameSingular": name_singular,
            "namePlural": name_plural,
            "isLabelSyncedWithName": is_label_synced_with_name,
            "labelIdentifierFieldMetadataId": seed_lid,
            "fields": seed_fields,
        }
        return {
            "id": obj_id,
            "nameSingular": name_singular,
            "labelIdentifierFieldMetadataId": seed_lid,
        }

    async def gql_create_field(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.gql_create_field_calls.append(dict(spec))
        target = next(
            (o for o in self._objects.values() if o["id"] == spec["objectMetadataId"]),
            None,
        )
        if target is None:
            raise RuntimeError(f"Unknown objectMetadataId {spec['objectMetadataId']}")
        fid = f"fld-{target['nameSingular']}-{spec['name']}"
        target["fields"].append({
            "id": fid,
            "name": spec["name"],
            "type": spec["type"],
            "label": spec.get("label", ""),
            "isNullable": spec.get("isNullable", True),
            "isUnique": spec.get("isUnique", False),
            "isLabelSyncedWithName": spec.get("isLabelSyncedWithName", False),
        })
        return {
            "id": fid,
            "name": spec["name"],
            "label": spec.get("label", ""),
            "type": spec["type"],
            "isUnique": spec.get("isUnique", False),
            "isNullable": spec.get("isNullable", True),
            "isLabelSyncedWithName": spec.get("isLabelSyncedWithName", False),
        }

    async def gql_update_object(self, object_id: str, update: dict[str, Any]) -> dict[str, Any]:
        self.gql_update_object_calls.append((object_id, dict(update)))
        target = next((o for o in self._objects.values() if o["id"] == object_id), None)
        if target is None:
            raise RuntimeError(f"Unknown object {object_id}")
        target.update(update)
        return {"id": object_id, **update}

    async def gql_update_field(self, field_id: str, update: dict[str, Any]) -> dict[str, Any]:
        self.gql_update_field_calls.append((field_id, dict(update)))
        for obj in self._objects.values():
            for f in obj.get("fields", []):
                if f.get("id") == field_id:
                    f.update(update)
                    return {"id": field_id, **update}
        raise RuntimeError(f"Unknown field {field_id}")


def _seed_with_task_and_person() -> list[dict[str, Any]]:
    return [
        {
            "nameSingular": "task",
            "namePlural": "tasks",
            "id": "obj-task",
            "labelIdentifierFieldMetadataId": "fld-task-title",
            "fields": [
                {"id": "fld-task-title", "name": "title", "type": "TEXT", "label": "Title"},
                {"id": "fld-task-povtor", "name": "povtornoeObrashchenie", "type": "BOOLEAN"},
                {"id": "fld-task-klient", "name": "klient", "type": "RELATION"},
                {"id": "fld-task-kompaniya", "name": "kompaniya", "type": "RELATION"},
            ],
        },
        {
            "nameSingular": "person",
            "namePlural": "people",
            "id": "obj-person",
            "labelIdentifierFieldMetadataId": "fld-person-name",
            "fields": [
                {"id": "fld-person-name", "name": "name", "type": "FULL_NAME"},
                {"id": "fld-person-phones", "name": "phones", "type": "PHONES"},
                {"id": "fld-person-tg", "name": "telegramid", "type": "TEXT"},
            ],
        },
    ]


@pytest.mark.asyncio
async def test_bootstrap_creates_all_missing_objects_and_fields() -> None:
    adapter = FakeTwentyAdapter(seed_objects=_seed_with_task_and_person())

    report = await ensure_twenty_schema(adapter)

    assert set(report.objects_created) == {"location", "callRecord"}
    assert not report.objects_existing
    assert not report.errors

    task_created = {k for k in report.fields_created if k.startswith("task.")}
    for spec in TASK_EXTRA_FIELDS + TASK_RELATIONS:
        assert f"task.{spec.name}" in task_created

    # Location is created with skip_name_field=True; all our spec fields are
    # created by us, including displayName as the new label-identifier.
    for spec in LOCATION.fields:
        assert f"location.{spec.name}" in report.fields_created
    assert any("location ->" in s for s in report.label_identifiers_set), (
        "Location.displayName must become labelIdentifier"
    )

    # No write to person except nothing — person extras were removed when the
    # data model migrated to N:M phone↔location.
    assert not any(k.startswith("person.") for k in report.fields_created)


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent() -> None:
    adapter = FakeTwentyAdapter(seed_objects=_seed_with_task_and_person())

    await ensure_twenty_schema(adapter)
    creates_first = (
        len(adapter.gql_create_object_calls) + len(adapter.gql_create_field_calls)
    )

    report2 = await ensure_twenty_schema(adapter)
    creates_second = (
        len(adapter.gql_create_object_calls) + len(adapter.gql_create_field_calls)
    )

    assert creates_first == creates_second, "second pass must not create anything"
    assert not report2.objects_created
    assert not report2.fields_created
    assert set(report2.objects_existing) == {"location", "callRecord"}
    assert not report2.errors


@pytest.mark.asyncio
async def test_relation_fields_carry_target_object_id() -> None:
    adapter = FakeTwentyAdapter(seed_objects=_seed_with_task_and_person())

    await ensure_twenty_schema(adapter)

    rel_calls = [c for c in adapter.gql_create_field_calls if c["type"] == "RELATION"]
    assert rel_calls
    for call in rel_calls:
        rel = call.get("relationCreationPayload")
        assert rel is not None, f"relation {call['name']} missing relationCreationPayload"
        assert rel["targetObjectMetadataId"]
        assert rel["type"] in ("MANY_TO_ONE", "ONE_TO_MANY")


@pytest.mark.asyncio
async def test_select_fields_include_options() -> None:
    adapter = FakeTwentyAdapter(seed_objects=_seed_with_task_and_person())

    await ensure_twenty_schema(adapter)

    select_calls = [c for c in adapter.gql_create_field_calls if c["type"] == "SELECT"]
    assert select_calls
    for call in select_calls:
        assert call.get("options"), f"SELECT field {call['name']} missing options"
        for idx, opt in enumerate(call["options"]):
            assert {"label", "value", "id", "position"} <= opt.keys()
            # ids must be UUIDs
            uuid.UUID(str(opt["id"]))
            assert opt["position"] == idx


@pytest.mark.asyncio
async def test_displayname_marked_unique_with_label_identifier() -> None:
    adapter = FakeTwentyAdapter(seed_objects=_seed_with_task_and_person())

    await ensure_twenty_schema(adapter)

    # displayName field was created with isUnique=True
    dn_calls = [
        c for c in adapter.gql_create_field_calls
        if c["objectMetadataId"] == "obj-location" and c["name"] == "displayName"
    ]
    assert len(dn_calls) == 1
    dn = dn_calls[0]
    assert dn.get("isUnique") is True
    assert dn.get("isNullable") is False

    # And updateOneObject was called to repoint labelIdentifierFieldMetadataId.
    upd = [(oid, u) for oid, u in adapter.gql_update_object_calls
           if oid == "obj-location" and "labelIdentifierFieldMetadataId" in u]
    assert upd, "Location.labelIdentifierFieldMetadataId must be set to displayName.id"
