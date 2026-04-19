"""Twenty schema bootstrap — idempotent creation of custom objects and fields.

Declarative description of objects/fields the app depends on (Location,
CallRecord, TaskLog + location/script/escalation fields on Task & Person).
`ensure_twenty_schema` lists existing metadata and creates only what's
missing. Safe to re-run.

Usage:
    from twenty_integration.infrastructure.twenty_adapter import TwentyRestAdapter
    from twenty_integration.infrastructure.bootstrap import ensure_twenty_schema

    adapter = TwentyRestAdapter(base_url=..., api_key=...)
    report = await ensure_twenty_schema(adapter)
    print(report)
"""
from __future__ import annotations

import logging
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
]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    type: FieldType
    description: str = ""
    is_nullable: bool = True
    options: tuple[dict[str, str], ...] = ()
    relation_target: str | None = None
    relation_type: Literal["MANY_TO_ONE", "ONE_TO_MANY"] = "MANY_TO_ONE"


@dataclass(frozen=True)
class ObjectSpec:
    name_singular: str
    name_plural: str
    label_singular: str
    label_plural: str
    description: str = ""
    icon: str = "IconBuilding"
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)


# ============================================================
# Target schema — single source of truth for the bootstrap
# ============================================================

LOCATION = ObjectSpec(
    name_singular="location",
    name_plural="locations",
    label_singular="Точка",
    label_plural="Точки",
    description="Торговая точка клиента (Апполо 32, Аспект 10 и т.п.). Ключ — телефон.",
    icon="IconMapPin",
    fields=(
        FieldSpec("phone", "Телефон", "TEXT", is_nullable=False),
        FieldSpec("prefix", "Бренд", "TEXT", description="Апполо / Аспект / другой"),
        FieldSpec("number", "Номер точки", "TEXT"),
        FieldSpec("address", "Адрес", "TEXT"),
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
        FieldSpec("callerPhone", "Телефон звонящего", "TEXT"),
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
        FieldSpec("occurredAt", "Время события", "DATE_TIME"),
    ),
)

# Custom fields to add to EXISTING objects (task, person).
# Relations are defined after the target object exists.
TASK_EXTRA_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("parentTaskId", "ID родительской задачи", "TEXT",
              description="Ссылка на задачу, повтором которой является эта."),
    FieldSpec("scriptViolations", "Нарушений скрипта", "NUMBER"),
    FieldSpec("scriptMissing", "Отсутствующие фразы", "TEXT"),
    FieldSpec("assignedAt", "Время назначения", "DATE_TIME"),
    FieldSpec("completedAt", "Время завершения", "DATE_TIME"),
)

PERSON_EXTRA_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("locationPrefix", "Бренд (кэш)", "TEXT"),
    FieldSpec("locationNumber", "Номер точки (кэш)", "TEXT"),
    FieldSpec("locationAddress", "Адрес (кэш)", "TEXT"),
)

# Relation specs — added AFTER both target & source objects are present.
TASK_RELATIONS: tuple[FieldSpec, ...] = (
    FieldSpec("locationRel", "Точка", "RELATION", relation_target="location"),
    FieldSpec("callRecordRel", "Звонок", "RELATION", relation_target="callRecord"),
)
PERSON_RELATIONS: tuple[FieldSpec, ...] = (
    FieldSpec("locationRel", "Точка", "RELATION", relation_target="location"),
)
TASKLOG_RELATIONS: tuple[FieldSpec, ...] = (
    FieldSpec("taskRel", "Задача", "RELATION", relation_target="task"),
)
CALLRECORD_RELATIONS: tuple[FieldSpec, ...] = (
    FieldSpec("personRel", "Контакт", "RELATION", relation_target="person"),
    FieldSpec("locationRel", "Точка", "RELATION", relation_target="location"),
    FieldSpec("taskRel", "Задача", "RELATION", relation_target="task"),
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
    errors: list[str] = field(default_factory=list)


def _field_spec_to_payload(spec: FieldSpec, object_id: str, objects_by_name: dict[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "objectMetadataId": object_id,
        "name": spec.name,
        "label": spec.label,
        "type": spec.type,
        "isNullable": spec.is_nullable,
    }
    if spec.description:
        payload["description"] = spec.description
    if spec.type == "SELECT" and spec.options:
        payload["options"] = list(spec.options)
    if spec.type == "RELATION":
        if spec.relation_target is None:
            raise ValueError(f"RELATION field {spec.name!r} needs relation_target")
        target_id = objects_by_name.get(spec.relation_target)
        if target_id is None:
            raise ValueError(f"Unknown relation target object {spec.relation_target!r}")
        payload["settings"] = {
            "relationType": spec.relation_type,
            "targetObjectMetadataId": target_id,
        }
    return payload


async def ensure_twenty_schema(adapter: Any) -> BootstrapReport:
    """Идемпотентно создаёт в Twenty отсутствующие объекты и поля.

    Порядок:
      1. Забираем текущие objects+fields.
      2. Создаём отсутствующие кастомные объекты (Location, CallRecord, TaskLog).
      3. Добавляем недостающие НЕ-relation поля на всех нужных объектах.
      4. Повторно забираем metadata (появились новые ID).
      5. Добавляем relation-поля.
    """
    report = BootstrapReport()

    objects = await adapter.list_objects_metadata()
    objects_by_name: dict[str, dict[str, Any]] = {o["nameSingular"]: o for o in objects}

    # ---- Шаг 2: создать отсутствующие кастомные объекты ----
    for spec in (LOCATION, CALL_RECORD, TASK_LOG):
        if spec.name_singular in objects_by_name:
            report.objects_existing.append(spec.name_singular)
            continue
        try:
            created = await adapter.create_object_metadata(
                {
                    "nameSingular": spec.name_singular,
                    "namePlural": spec.name_plural,
                    "labelSingular": spec.label_singular,
                    "labelPlural": spec.label_plural,
                    "description": spec.description,
                    "icon": spec.icon,
                    "isLabelSyncedWithName": False,
                }
            )
            report.objects_created.append(spec.name_singular)
            # The created payload usually contains id; stash it so following
            # field creations don't need another GET round-trip.
            objects_by_name[spec.name_singular] = {
                "id": created.get("id", ""),
                "nameSingular": spec.name_singular,
                "fields": [],
            }
        except Exception as exc:  # pragma: no cover — surfaced in report
            report.errors.append(f"create_object({spec.name_singular}): {exc}")

    # ---- Шаг 3: добавить не-relation поля ----
    # Map: object_nameSingular -> (ObjectSpec or None if reusing existing) -> list of extra non-relation fields
    extras_plan: list[tuple[str, tuple[FieldSpec, ...]]] = [
        (LOCATION.name_singular, LOCATION.fields),
        (CALL_RECORD.name_singular, CALL_RECORD.fields),
        (TASK_LOG.name_singular, TASK_LOG.fields),
        ("task", TASK_EXTRA_FIELDS),
        ("person", PERSON_EXTRA_FIELDS),
    ]

    object_id_lookup = {name: obj.get("id", "") for name, obj in objects_by_name.items()}

    for obj_name, specs in extras_plan:
        obj = objects_by_name.get(obj_name)
        if obj is None:
            report.errors.append(f"target object {obj_name!r} not found")
            continue
        obj_id = obj.get("id", "")
        existing_field_names = {f.get("name") for f in obj.get("fields", [])}
        for f_spec in specs:
            if f_spec.type == "RELATION":
                continue  # handled in step 5
            key = f"{obj_name}.{f_spec.name}"
            if f_spec.name in existing_field_names:
                report.fields_existing.append(key)
                continue
            try:
                await adapter.create_field_metadata(
                    _field_spec_to_payload(f_spec, obj_id, object_id_lookup)
                )
                report.fields_created.append(key)
            except Exception as exc:
                report.errors.append(f"create_field({key}): {exc}")

    # ---- Шаг 4: refresh metadata so relation targets have IDs ----
    objects = await adapter.list_objects_metadata()
    objects_by_name = {o["nameSingular"]: o for o in objects}
    object_id_lookup = {name: obj.get("id", "") for name, obj in objects_by_name.items()}

    # ---- Шаг 5: relation fields ----
    relations_plan: list[tuple[str, tuple[FieldSpec, ...]]] = [
        ("task", TASK_RELATIONS),
        ("person", PERSON_RELATIONS),
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
                await adapter.create_field_metadata(
                    _field_spec_to_payload(f_spec, obj_id, object_id_lookup)
                )
                report.fields_created.append(key)
            except Exception as exc:
                report.errors.append(f"create_field({key}): {exc}")

    logger.info(
        "bootstrap done: objects_created=%d fields_created=%d errors=%d",
        len(report.objects_created),
        len(report.fields_created),
        len(report.errors),
    )
    return report
