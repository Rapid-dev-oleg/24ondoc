"""Twenty schema bootstrap — idempotent creation of custom objects and fields.

Declarative description of objects/fields the app depends on (Location,
CallRecord, TaskLog + script/escalation fields on Task). `ensure_twenty_schema`
lists existing metadata and creates only what's missing, then patches anything
whose flags drifted from the spec (isNullable, isUnique). Safe to re-run.

Talks to Twenty GraphQL `/metadata` (not REST `/rest/metadata/*`) — the latter
is read-mostly and hides isUnique / isLabelSyncedWithName /
labelIdentifierFieldMetadataId / skipNameField.

Usage:
    from twenty_integration.infrastructure.twenty_adapter import TwentyRestAdapter
    from twenty_integration.infrastructure.bootstrap import ensure_twenty_schema

    adapter = TwentyRestAdapter(base_url=..., api_key=...)
    report = await ensure_twenty_schema(adapter)
    print(report)
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

FieldType = Literal[
    "TEXT",
    "NUMBER",
    "BOOLEAN",
    "DATE_TIME",
    "SELECT",
    "RELATION",
    "RICH_TEXT_V2",
    "PHONES",
]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    type: FieldType
    description: str = ""
    is_nullable: bool = True
    is_unique: bool = False
    # When true, the object's labelIdentifierFieldMetadataId is repointed at
    # this field after creation — its value becomes the record's display name.
    is_label_identifier: bool = False
    options: tuple[dict[str, str], ...] = ()
    relation_target: str | None = None
    relation_type: Literal["MANY_TO_ONE", "ONE_TO_MANY"] = "MANY_TO_ONE"
    # For RELATION fields — label/icon of the auto-generated reverse field
    # on the target object. Must be unique among fields of the target.
    reverse_label: str = ""
    reverse_icon: str = "IconLink"


@dataclass(frozen=True)
class ObjectSpec:
    name_singular: str
    name_plural: str
    label_singular: str
    label_plural: str
    description: str = ""
    icon: str = "IconBuilding"
    # When true, Twenty will NOT auto-create the default `name` TEXT field.
    # Use this when you provide your own label-identifier field (e.g. Location.displayName).
    skip_name_field: bool = False
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)


# ============================================================
# Target schema — single source of truth for the bootstrap
# ============================================================

LOCATION = ObjectSpec(
    name_singular="location",
    name_plural="locations",
    label_singular="Точка",
    label_plural="Точки",
    description="Торговая точка клиента (Аполо 02, Аспект 25 и т.п.). Заводится из xlsx-каталога; имя точки уникально.",
    icon="IconMapPin",
    # Twenty's default `name` field is the label-identifier and its label is
    # not editable in UI. We want our own field "Имя точки" with controlled
    # uniqueness and a Russian label.
    skip_name_field=True,
    fields=(
        # Уникальное человекочитаемое имя точки — «Аполо 02», «Аспект 25».
        # Используется как label-identifier (после создания UPDATE на объекте).
        FieldSpec(
            "displayName",
            "Имя точки",
            "TEXT",
            description="Уникальное имя точки в формате «{Бренд} {номер}».",
            is_nullable=False,
            is_unique=True,
            is_label_identifier=True,
        ),
        FieldSpec("prefix", "Бренд", "TEXT", description="Аполо / Аспект / другой"),
        FieldSpec("number", "Номер точки", "TEXT"),
        # `address` conflicts with Twenty's built-in ADDRESS composite name.
        FieldSpec("locationAddress", "Адрес", "TEXT"),
        # PHONES — composite type so Twenty itself normalizes numbers.
        # Nullable: 346 точек из xlsx идут без телефона.
        FieldSpec("phone", "Телефон", "PHONES", is_nullable=True),
        FieldSpec(
            "anydeskId",
            "AnyDesk ID",
            "TEXT",
            description="Идентификатор удалённого доступа AnyDesk.",
        ),
    ),
)

CALL_RECORD = ObjectSpec(
    name_singular="callRecord",
    name_plural="callRecords",
    label_singular="Звонок",
    label_plural="Звонки",
    description="Входящий/исходящий звонок через АТС Т2. Отвеченные связаны с задачей.",
    icon="IconPhone",
    fields=(
        FieldSpec("atsCallId", "ATS Call ID", "TEXT", is_nullable=False),
        FieldSpec("callerPhone", "Телефон звонящего", "PHONES"),
        FieldSpec("calleePhone", "Телефон оператора", "PHONES"),
        FieldSpec(
            "direction",
            "Направление",
            "SELECT",
            options=(
                {"label": "Входящий", "value": "INCOMING", "color": "blue"},
                {"label": "Исходящий", "value": "OUTGOING", "color": "green"},
            ),
        ),
        FieldSpec("duration", "Длительность (сек)", "NUMBER"),
        FieldSpec(
            "callStatus",
            "Статус звонка",
            "SELECT",
            options=(
                {"label": "Отвечен", "value": "ANSWERED", "color": "green"},
                {"label": "Пропущен", "value": "MISSED", "color": "red"},
                {"label": "Ошибка", "value": "ERROR", "color": "orange"},
            ),
        ),
        FieldSpec("occurredAt", "Время звонка", "DATE_TIME"),
        FieldSpec("transcript", "Транскрипт", "RICH_TEXT_V2"),
        FieldSpec("audioUrl", "Ссылка на аудио", "TEXT"),
    ),
)

TASK_LOG = ObjectSpec(
    name_singular="taskLog",
    name_plural="taskLogs",
    label_singular="Событие задачи",
    label_plural="События задач",
    description="Аудит действий с задачей: создание, назначение, смена статуса, AI-проверки.",
    icon="IconHistory",
    fields=(
        FieldSpec(
            "action",
            "Действие",
            "SELECT",
            options=(
                {"label": "Создана", "value": "CREATED", "color": "gray"},
                {"label": "Назначена", "value": "ASSIGNED", "color": "blue"},
                {"label": "Смена статуса", "value": "STATUS_CHANGED", "color": "yellow"},
                {"label": "Завершена", "value": "COMPLETED", "color": "green"},
                {"label": "Отменена", "value": "CANCELLED", "color": "red"},
                {"label": "Комментарий", "value": "COMMENT_ADDED", "color": "purple"},
                {"label": "Проверен скрипт", "value": "SCRIPT_CHECKED", "color": "pink"},
                {"label": "Проверен повтор", "value": "REPEAT_CHECKED", "color": "orange"},
            ),
        ),
        FieldSpec(
            "actorType",
            "Источник",
            "SELECT",
            options=(
                {"label": "Оператор", "value": "OPERATOR", "color": "blue"},
                {"label": "Администратор", "value": "ADMIN", "color": "purple"},
                {"label": "AI", "value": "SYSTEM_AI", "color": "pink"},
            ),
        ),
        FieldSpec("actorId", "ID актора", "TEXT"),
        FieldSpec("actorName", "Имя актора", "TEXT"),
        FieldSpec("details", "Описание", "RICH_TEXT_V2"),
        FieldSpec("meta", "Мета", "RICH_TEXT_V2",
                  description="JSON с payload события (updatedFields, priority и т.п.)"),
        FieldSpec("occurredAt", "Время события", "DATE_TIME"),
    ),
)

# Custom fields to add to EXISTING objects (task only).
# Person.location* кэш-поля и Person.locationRel были удалены при переходе
# на N:M phone↔location (см. plan + миграцию scripts/migrate_drop_legacy_fields.py).
TASK_EXTRA_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("parentTaskId", "ID родительской задачи", "TEXT",
              description="Ссылка на задачу, повтором которой является эта."),
    FieldSpec("scriptViolations", "Нарушений скрипта", "NUMBER"),
    FieldSpec("scriptMissing", "Отсутствующие фразы", "TEXT"),
    # Телефон заявителя живёт прямо на Task — Person как «бакет для номеров»
    # больше не нужен (см. wipe_client_persons.py + use_cases.py).
    FieldSpec("callerPhone", "Телефон заявителя", "PHONES"),
)

# Relation specs — added AFTER both target & source objects are present.
# IMPORTANT: declare each relation from ONE side only; Twenty auto-generates
# the reverse field on the target. `reverse_label` must be unique per target.
TASK_RELATIONS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "locationRel", "Точка", "RELATION",
        relation_target="location", reverse_label="Задачи точки",
    ),
)
TASKLOG_RELATIONS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "taskRel", "Задача", "RELATION",
        relation_target="task", reverse_label="События задачи",
    ),
)
CALLRECORD_RELATIONS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "personRel", "Контакт", "RELATION",
        relation_target="person", reverse_label="Звонки контакта",
    ),
    FieldSpec(
        "locationRel", "Точка", "RELATION",
        relation_target="location", reverse_label="Звонки точки",
    ),
    FieldSpec(
        "taskRel", "Задача", "RELATION",
        relation_target="task", reverse_label="Звонки задачи",
    ),
)


# ============================================================
# Bootstrap algorithm
# ============================================================


@dataclass
class BootstrapReport:
    objects_created: list[str] = field(default_factory=list)
    objects_existing: list[str] = field(default_factory=list)
    fields_created: list[str] = field(default_factory=list)  # "object.field"
    fields_existing: list[str] = field(default_factory=list)
    fields_patched: list[str] = field(default_factory=list)
    label_identifiers_set: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _create_field_input(
    spec: FieldSpec,
    object_id: str,
    objects_by_name: dict[str, str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "objectMetadataId": object_id,
        "name": spec.name,
        "label": spec.label,
        "type": spec.type,
        "isNullable": spec.is_nullable,
        # We control labels explicitly (Russian), so they should NEVER be
        # auto-derived from `name`. Without this, Twenty UI hides the label
        # editor for the field and reverts label to the camelCased name.
        "isLabelSyncedWithName": False,
    }
    if spec.is_unique:
        payload["isUnique"] = True
    if spec.description:
        payload["description"] = spec.description
    if spec.type == "SELECT" and spec.options:
        # Twenty requires each option to carry an `id` (UUID) and `position`.
        payload["options"] = [
            {**opt, "id": str(uuid.uuid4()), "position": idx}
            for idx, opt in enumerate(spec.options)
        ]
    if spec.type == "RELATION":
        if spec.relation_target is None:
            raise ValueError(f"RELATION field {spec.name!r} needs relation_target")
        target_id = objects_by_name.get(spec.relation_target)
        if target_id is None:
            raise ValueError(f"Unknown relation target object {spec.relation_target!r}")
        payload["relationCreationPayload"] = {
            "type": spec.relation_type,
            "targetObjectMetadataId": target_id,
            "targetFieldLabel": spec.reverse_label or f"{spec.label} ↔",
            "targetFieldIcon": spec.reverse_icon,
        }
    return payload


def _field_needs_patch(existing: dict[str, Any], spec: FieldSpec) -> dict[str, Any]:
    """Detect drift between existing field and spec for properties we own."""
    patch: dict[str, Any] = {}
    if existing.get("isNullable") != spec.is_nullable:
        patch["isNullable"] = spec.is_nullable
    # Only flip uniqueness false → true automatically; flipping true → false
    # could mask data corruption, so leave it for manual correction.
    if spec.is_unique and not existing.get("isUnique"):
        patch["isUnique"] = True
    if existing.get("isLabelSyncedWithName"):
        # We always want the label decoupled from `name` so it can be edited.
        patch["isLabelSyncedWithName"] = False
    if existing.get("label") != spec.label:
        patch["label"] = spec.label
    return patch


async def ensure_twenty_schema(adapter: Any) -> BootstrapReport:
    """Idempotent metadata sync: create missing objects/fields, patch drift.

    Steps:
      1. Pull current objects+fields via GraphQL `/metadata`.
      2. Create missing custom objects (Location, CallRecord, TaskLog).
      3. Create missing non-relation fields on every target object.
      4. Set Object.labelIdentifierFieldMetadataId for objects whose spec has
         a field marked `is_label_identifier=True`.
      5. Patch drift on existing fields (isNullable, isUnique, label).
      6. Re-fetch metadata so relation targets have IDs.
      7. Create missing relation fields.
    """
    report = BootstrapReport()

    objects = await adapter.list_objects_metadata()
    objects_by_name: dict[str, dict[str, Any]] = {o["nameSingular"]: o for o in objects}

    # ---- Step 2: create missing custom objects ----
    for spec in (LOCATION, CALL_RECORD, TASK_LOG):
        if spec.name_singular in objects_by_name:
            report.objects_existing.append(spec.name_singular)
            continue
        try:
            created = await adapter.gql_create_object(
                name_singular=spec.name_singular,
                name_plural=spec.name_plural,
                label_singular=spec.label_singular,
                label_plural=spec.label_plural,
                description=spec.description,
                icon=spec.icon,
                skip_name_field=spec.skip_name_field,
                is_label_synced_with_name=False,
            )
            report.objects_created.append(spec.name_singular)
            objects_by_name[spec.name_singular] = {
                "id": created.get("id", ""),
                "nameSingular": spec.name_singular,
                "labelIdentifierFieldMetadataId": created.get(
                    "labelIdentifierFieldMetadataId"
                ),
                "fields": [],
            }
        except Exception as exc:  # pragma: no cover — surfaced in report
            report.errors.append(f"create_object({spec.name_singular}): {exc}")

    # ---- Step 3: create missing non-relation fields ----
    extras_plan: list[tuple[ObjectSpec | None, str, tuple[FieldSpec, ...]]] = [
        (LOCATION, LOCATION.name_singular, LOCATION.fields),
        (CALL_RECORD, CALL_RECORD.name_singular, CALL_RECORD.fields),
        (TASK_LOG, TASK_LOG.name_singular, TASK_LOG.fields),
        (None, "task", TASK_EXTRA_FIELDS),
    ]

    object_id_lookup = {name: obj.get("id", "") for name, obj in objects_by_name.items()}
    # Track the id of label-identifier fields we (re)create — used in step 4.
    new_label_identifiers: dict[str, str] = {}

    for owning_spec, obj_name, specs in extras_plan:
        obj = objects_by_name.get(obj_name)
        if obj is None:
            report.errors.append(f"target object {obj_name!r} not found")
            continue
        obj_id = obj.get("id", "")
        existing_fields = {f.get("name"): f for f in (obj.get("fields") or [])}
        for f_spec in specs:
            if f_spec.type == "RELATION":
                continue  # handled in step 7
            key = f"{obj_name}.{f_spec.name}"
            existing = existing_fields.get(f_spec.name)
            if existing:
                report.fields_existing.append(key)
                # Drift patch
                patch = _field_needs_patch(existing, f_spec)
                if patch:
                    try:
                        await adapter.gql_update_field(existing["id"], patch)
                        report.fields_patched.append(f"{key}({','.join(patch)})")
                    except Exception as exc:
                        report.errors.append(f"update_field({key}): {exc}")
                if f_spec.is_label_identifier:
                    new_label_identifiers[obj_name] = existing["id"]
                continue
            try:
                created = await adapter.gql_create_field(
                    _create_field_input(f_spec, obj_id, object_id_lookup)
                )
                report.fields_created.append(key)
                if f_spec.is_label_identifier and created.get("id"):
                    new_label_identifiers[obj_name] = created["id"]
            except Exception as exc:
                report.errors.append(f"create_field({key}): {exc}")

    # ---- Step 4: repoint labelIdentifierFieldMetadataId where needed ----
    for obj_name, lid_id in new_label_identifiers.items():
        obj = objects_by_name.get(obj_name) or {}
        current = obj.get("labelIdentifierFieldMetadataId")
        if current == lid_id:
            continue
        try:
            await adapter.gql_update_object(
                obj["id"], {"labelIdentifierFieldMetadataId": lid_id}
            )
            report.label_identifiers_set.append(f"{obj_name} -> {lid_id}")
        except Exception as exc:
            report.errors.append(f"set_label_identifier({obj_name}): {exc}")

    # ---- Step 5: refresh metadata so relation targets have IDs ----
    objects = await adapter.list_objects_metadata()
    objects_by_name = {o["nameSingular"]: o for o in objects}
    object_id_lookup = {name: obj.get("id", "") for name, obj in objects_by_name.items()}

    # ---- Step 6: relation fields ----
    relations_plan: list[tuple[str, tuple[FieldSpec, ...]]] = [
        ("task", TASK_RELATIONS),
        (TASK_LOG.name_singular, TASKLOG_RELATIONS),
        (CALL_RECORD.name_singular, CALLRECORD_RELATIONS),
    ]
    for obj_name, specs in relations_plan:
        obj = objects_by_name.get(obj_name)
        if obj is None:
            report.errors.append(f"target object {obj_name!r} not found (relations)")
            continue
        obj_id = obj.get("id", "")
        existing_field_names = {f.get("name") for f in obj.get("fields", [])}
        for f_spec in specs:
            key = f"{obj_name}.{f_spec.name}"
            if f_spec.name in existing_field_names:
                report.fields_existing.append(key)
                continue
            try:
                await adapter.gql_create_field(
                    _create_field_input(f_spec, obj_id, object_id_lookup)
                )
                report.fields_created.append(key)
            except Exception as exc:
                report.errors.append(f"create_field({key}): {exc}")

    logger.info(
        "bootstrap done: objects_created=%d fields_created=%d fields_patched=%d labels_set=%d errors=%d",
        len(report.objects_created),
        len(report.fields_created),
        len(report.fields_patched),
        len(report.label_identifiers_set),
        len(report.errors),
    )
    return report
