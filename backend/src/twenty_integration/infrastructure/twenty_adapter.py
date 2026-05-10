"""Twenty Integration — REST API Adapter."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from twenty_integration.domain.models import TwentyMember, TwentyPerson, TwentyTask
from twenty_integration.domain.ports import TwentyCRMPort
from twenty_integration.infrastructure.phone import (
    normalize_ru_phone,
    to_phones_composite,
)

logger = logging.getLogger(__name__)


class TwentyRestAdapter(TwentyCRMPort):
    """HTTP-клиент для Twenty REST API."""

    def __init__(self, base_url: str, api_key: str) -> None:
        """Инициализировать адаптер.

        Args:
            base_url: Base URL Twenty API (e.g., https://api.twenty.com)
            api_key: API ключ для аутентификации
        """
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    async def list_workspace_members(self) -> list[TwentyMember]:
        """Получить список участников рабочего пространства."""
        try:
            response = await self._client.get("/rest/workspaceMembers")
            response.raise_for_status()
            data = response.json()

            members = []
            items = data.get("data", {}).get("workspaceMembers", [])
            for item in items:
                name = item.get("name", {})
                member = TwentyMember(
                    twenty_id=item.get("id", ""),
                    first_name=name.get("firstName", ""),
                    last_name=name.get("lastName", ""),
                    email=item.get("userEmail", ""),
                )
                members.append(member)
            return members
        except httpx.HTTPError:
            return []

    async def find_person_by_telegram_id(self, telegram_id: int) -> TwentyPerson | None:
        """Найти контакт по Telegram ID."""
        try:
            response = await self._client.get(
                "/rest/people",
                params={"filter": f"telegramid[eq]:{telegram_id}"},
            )
            response.raise_for_status()
            data = response.json()

            items = data.get("data", {}).get("people", [])
            if not items:
                return None

            item = items[0]
            person = TwentyPerson(
                twenty_id=item.get("id", ""),
                telegram_id=telegram_id,
                name=item.get("name", {}).get("firstName", ""),
            )
            return person
        except httpx.HTTPError:
            return None

    async def find_locations_by_phone(self, phone: str) -> list[dict[str, Any]]:
        """Все Location, у которых данный телефон совпадает с primary либо
        есть в additionalPhones jsonb.

        Twenty REST `filter` не умеет искать внутри jsonb, поэтому делаем
        два прохода: точный поиск по primary + локальная фильтрация
        дополнительных номеров (читаем не более 100 за раз).
        """
        national = normalize_ru_phone(phone)
        if not national:
            return []
        results: list[dict[str, Any]] = []
        try:
            # Pass 1: primary phone — fast path, indexed.
            response = await self._client.get(
                "/rest/locations",
                params={"filter": f"phone.primaryPhoneNumber[eq]:{national}"},
            )
            response.raise_for_status()
            primary = response.json().get("data", {}).get("locations", []) or []
            results.extend(primary)
        except httpx.HTTPError:
            logger.exception("find_locations_by_phone primary failed phone=%s", national)

        # Pass 2: scan recent locations and check additionalPhones locally.
        # Cheap because there are O(hundreds) of locations, not millions.
        try:
            seen_ids = {loc.get("id") for loc in results}
            response = await self._client.get(
                "/rest/locations",
                params={"limit": 100},
            )
            response.raise_for_status()
            for loc in response.json().get("data", {}).get("locations", []) or []:
                if loc.get("id") in seen_ids:
                    continue
                additional = ((loc.get("phone") or {}).get("additionalPhones")) or []
                for ap in additional:
                    raw = ap.get("number") if isinstance(ap, dict) else ap
                    if normalize_ru_phone(str(raw)) == national:
                        results.append(loc)
                        break
        except httpx.HTTPError:
            logger.exception("find_locations_by_phone additional failed phone=%s", national)
        return results

    async def find_location_by_display_name(
        self, display_name: str
    ) -> dict[str, Any] | None:
        """Найти Location по уникальному displayName (используется AI-резолвом)."""
        if not display_name:
            return None
        try:
            response = await self._client.get(
                "/rest/locations",
                params={"filter": f"displayName[eq]:{display_name}"},
            )
            response.raise_for_status()
            items = response.json().get("data", {}).get("locations", []) or []
            return items[0] if items else None
        except httpx.HTTPError:
            logger.exception("find_location_by_display_name failed name=%s", display_name)
            return None

    async def add_phone_to_location(
        self, location_id: str, phone: str
    ) -> bool:
        """Learn-by-resolve: append `phone` to Location.phone if missing.

        - If primary is empty → write into primary.
        - If primary holds a different number → append to additionalPhones.
        - If `phone` already equals primary or appears in additionalPhones
          (after normalization) → no-op.
        """
        national = normalize_ru_phone(phone)
        if not national or not location_id:
            return False
        try:
            response = await self._client.get(f"/rest/locations/{location_id}")
            response.raise_for_status()
            loc = (response.json().get("data") or {}).get("location") or {}
        except httpx.HTTPError:
            logger.exception("add_phone_to_location: GET failed loc=%s",
                             location_id)
            return False

        cur = (loc.get("phone") or {}).copy()
        primary_norm = normalize_ru_phone(cur.get("primaryPhoneNumber"))
        additional = list(cur.get("additionalPhones") or [])
        additional_norm = {
            normalize_ru_phone(
                ap.get("number") if isinstance(ap, dict) else ap
            )
            for ap in additional
        }
        additional_norm.discard(None)

        if primary_norm == national or national in additional_norm:
            return False

        if not primary_norm:
            new_phone: dict[str, Any] = {
                "primaryPhoneNumber": national,
                "primaryPhoneCountryCode": "RU",
                "primaryPhoneCallingCode": "+7",
                "additionalPhones": additional,
            }
        else:
            new_phone = {
                "primaryPhoneNumber": cur.get("primaryPhoneNumber"),
                "primaryPhoneCountryCode":
                    cur.get("primaryPhoneCountryCode") or "RU",
                "primaryPhoneCallingCode":
                    cur.get("primaryPhoneCallingCode") or "+7",
                "additionalPhones": additional + [{
                    "number": national,
                    "callingCode": "+7",
                    "countryCode": "RU",
                }],
            }
        try:
            r = await self._client.patch(
                f"/rest/locations/{location_id}", json={"phone": new_phone},
            )
            r.raise_for_status()
            logger.info(
                "add_phone_to_location: %s ← +%s (slot=%s)",
                location_id, national,
                "primary" if not primary_norm else "additional",
            )
            return True
        except httpx.HTTPError:
            logger.exception("add_phone_to_location: PATCH failed loc=%s",
                             location_id)
            return False

    async def list_location_display_names(self) -> list[str]:
        """Все displayName из Twenty Location. Используется AI-промптом как enum.

        Возвращаем сразу до 200 (текущая база — 402 точки, REST по умолчанию
        отдаёт страницу 60; берём страницы пока есть данные).
        """
        names: list[str] = []
        cursor: str | None = None
        # Hard guard: we don't expect > 5 pages of 100 in a workspace.
        for _ in range(10):
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["starting_after"] = cursor
            try:
                response = await self._client.get("/rest/locations", params=params)
                response.raise_for_status()
                page = response.json().get("data", {}).get("locations", []) or []
            except httpx.HTTPError:
                logger.exception("list_location_display_names failed")
                break
            if not page:
                break
            for loc in page:
                dn = (loc.get("displayName") or "").strip()
                if dn:
                    names.append(dn)
            if len(page) < 100:
                break
            cursor = page[-1].get("id")
            if not cursor:
                break
        return names

    async def find_call_record_by_ats_id(self, ats_call_id: str) -> dict[str, Any] | None:
        """Найти Twenty CallRecord по внешнему ATS ID (для upsert)."""
        if not ats_call_id:
            return None
        try:
            response = await self._client.get(
                "/rest/callRecords",
                params={"filter": f"atsCallId[eq]:{ats_call_id}"},
            )
            response.raise_for_status()
            items = response.json().get("data", {}).get("callRecords", [])
            return items[0] if items else None
        except httpx.HTTPError:
            logger.exception("find_call_record_by_ats_id failed id=%s", ats_call_id)
            return None

    async def create_call_record(
        self,
        ats_call_id: str,
        *,
        caller_phone: str | None = None,
        callee_phone: str | None = None,
        client_phone: str | None = None,
        agent_phone: str | None = None,
        direction: str = "INCOMING",
        duration: int | None = None,
        call_status: str = "ANSWERED",
        occurred_at: datetime | None = None,
        transcript: str | None = None,
        location_rel_id: str | None = None,
        task_rel_id: str | None = None,
        operator_rel_id: str | None = None,
        call_kind: str | None = None,
    ) -> dict[str, Any]:
        """Создать CallRecord в Twenty. Для upsert см. sync_call_record.

        direction: INCOMING | OUTGOING (см. bootstrap.CALL_RECORD.direction).
        call_status: ANSWERED | MISSED | ERROR.
        call_kind:   FIRST | REPEAT (snapshot of Task.povtornoeObrashchenie).
        """
        payload: dict[str, Any] = {
            "atsCallId": ats_call_id,
            "direction": direction,
            "callStatus": call_status,
        }
        if caller_phone:
            national = normalize_ru_phone(caller_phone)
            # Leave callerPhone empty when the input can't be normalized —
            # Twenty would reject a malformed PHONES composite.
            if national:
                payload["callerPhone"] = to_phones_composite(national)
        if callee_phone:
            national = normalize_ru_phone(callee_phone)
            if national:
                payload["calleePhone"] = to_phones_composite(national)
        if client_phone:
            national = normalize_ru_phone(client_phone)
            if national:
                payload["clientPhone"] = to_phones_composite(national)
        if agent_phone:
            national = normalize_ru_phone(agent_phone)
            if national:
                payload["agentPhone"] = to_phones_composite(national)
        if duration is not None:
            payload["duration"] = duration
        if occurred_at is not None:
            payload["occurredAt"] = occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if transcript:
            payload["transcript"] = {"markdown": transcript}
        if location_rel_id:
            payload["locationRelId"] = location_rel_id
        if task_rel_id:
            payload["taskRelId"] = task_rel_id
        if operator_rel_id:
            payload["operatorRelId"] = operator_rel_id
        if call_kind:
            payload["callKind"] = call_kind

        response = await self._client.post("/rest/callRecords", json=payload)
        if response.status_code >= 400:
            logger.error(
                "Twenty create_call_record failed: %s %s",
                response.status_code,
                response.text[:300],
            )
        response.raise_for_status()
        data = response.json().get("data", {})
        return dict(data.get("createCallRecord", data))

    async def update_call_record(
        self,
        call_record_id: str,
        *,
        task_rel_id: str | None = None,
        location_rel_id: str | None = None,
        transcript: str | None = None,
        callee_phone: str | None = None,
        client_phone: str | None = None,
        agent_phone: str | None = None,
        direction: str | None = None,
        operator_rel_id: str | None = None,
        call_kind: str | None = None,
    ) -> None:
        """Патч существующей Twenty CallRecord (обычно — прикрепить task после факта)."""
        patch: dict[str, Any] = {}
        if task_rel_id is not None:
            patch["taskRelId"] = task_rel_id
        if location_rel_id is not None:
            patch["locationRelId"] = location_rel_id
        if transcript is not None:
            patch["transcript"] = {"markdown": transcript}
        if callee_phone is not None:
            national = normalize_ru_phone(callee_phone)
            if national:
                patch["calleePhone"] = to_phones_composite(national)
        if client_phone is not None:
            national = normalize_ru_phone(client_phone)
            if national:
                patch["clientPhone"] = to_phones_composite(national)
        if agent_phone is not None:
            national = normalize_ru_phone(agent_phone)
            if national:
                patch["agentPhone"] = to_phones_composite(national)
        if direction is not None:
            patch["direction"] = direction
        if operator_rel_id is not None:
            patch["operatorRelId"] = operator_rel_id
        if call_kind is not None:
            patch["callKind"] = call_kind
        if not patch:
            return
        response = await self._client.patch(
            f"/rest/callRecords/{call_record_id}", json=patch
        )
        if response.status_code >= 400:
            logger.error(
                "Twenty update_call_record failed: %s %s",
                response.status_code,
                response.text[:300],
            )
            response.raise_for_status()

    async def update_call_record_script_check(
        self, call_record_id: str, violations: int, missing: list[str],
    ) -> None:
        """Per-call script-check result. Stores `violations` (NUMBER)
        and `missing` (joined RU phrases as TEXT) on the CallRecord."""
        patch = {
            "scriptViolations": int(violations),
            "scriptMissing": ", ".join(missing) if missing else "",
        }
        r = await self._client.patch(
            f"/rest/callRecords/{call_record_id}", json=patch
        )
        if r.status_code >= 400:
            logger.error(
                "update_call_record_script_check failed: %s %s",
                r.status_code, r.text[:300],
            )
            r.raise_for_status()

    async def recompute_task_aggregates(self, task_id: str) -> None:
        """Snapshot Task.scriptViolationsTotal + callRecordCount from the
        related CallRecord set. Cheap denormalisation for flat reports."""
        if not task_id:
            return
        try:
            r = await self._client.get(
                "/rest/callRecords",
                params={"filter": f"taskRelId[eq]:{task_id}", "limit": 100},
            )
            r.raise_for_status()
            crs = r.json().get("data", {}).get("callRecords", []) or []
        except httpx.HTTPError:
            logger.exception("recompute_task_aggregates: GET CRs failed task=%s", task_id)
            return
        total = sum(int(c.get("scriptViolations") or 0) for c in crs)
        count = len(crs)
        # Patch task — only if values would change (skip in-vain writes
        # so we don't burn the 100/60s write quota).
        try:
            t = await self._client.get(f"/rest/tasks/{task_id}")
            t.raise_for_status()
            cur = (t.json().get("data") or {}).get("task") or {}
        except httpx.HTTPError:
            cur = {}
        patch: dict[str, Any] = {}
        if int(cur.get("scriptViolationsTotal") or 0) != total:
            patch["scriptViolationsTotal"] = total
        if int(cur.get("callRecordCount") or 0) != count:
            patch["callRecordCount"] = count
        if not patch:
            return
        try:
            r = await self._client.patch(f"/rest/tasks/{task_id}", json=patch)
            r.raise_for_status()
        except httpx.HTTPError:
            logger.exception("recompute_task_aggregates: PATCH failed task=%s", task_id)

    # ---- Operator ----

    async def find_operator_by_phone(
        self, work_phone: str,
    ) -> dict[str, Any] | None:
        national = normalize_ru_phone(work_phone)
        if not national:
            return None
        try:
            r = await self._client.get(
                "/rest/operators",
                params={"filter": f"workPhone.primaryPhoneNumber[eq]:{national}"},
            )
            r.raise_for_status()
            items = r.json().get("data", {}).get("operators", []) or []
            return items[0] if items else None
        except httpx.HTTPError:
            logger.exception("find_operator_by_phone failed phone=%s", national)
            return None

    async def find_operator_by_member(
        self, member_id: str,
    ) -> dict[str, Any] | None:
        if not member_id:
            return None
        try:
            r = await self._client.get(
                "/rest/operators",
                params={"filter": f"memberRelId[eq]:{member_id}"},
            )
            r.raise_for_status()
            items = r.json().get("data", {}).get("operators", []) or []
            return items[0] if items else None
        except httpx.HTTPError:
            logger.exception("find_operator_by_member failed member=%s", member_id)
            return None

    async def list_operators(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(20):
            params: dict[str, Any] = {"limit": 100,
                                       "order_by": "id[AscNullsFirst]"}
            if cursor:
                params["filter"] = f"id[gt]:{cursor}"
            try:
                r = await self._client.get("/rest/operators", params=params)
                r.raise_for_status()
                page = r.json().get("data", {}).get("operators", []) or []
            except httpx.HTTPError:
                logger.exception("list_operators failed")
                break
            if not page:
                break
            out.extend(page)
            if len(page) < 100:
                break
            cursor = page[-1].get("id")
            if not cursor:
                break
        return out

    async def create_operator(
        self,
        display_name: str,
        *,
        work_phone: str | None = None,
        member_rel_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"displayName": display_name}
        if work_phone:
            national = normalize_ru_phone(work_phone)
            if national:
                payload["workPhone"] = to_phones_composite(national)
        if member_rel_id:
            payload["memberRelId"] = member_rel_id
        r = await self._client.post("/rest/operators", json=payload)
        if r.status_code >= 400:
            logger.error("create_operator failed: %s %s",
                         r.status_code, r.text[:300])
        r.raise_for_status()
        data = r.json().get("data", {})
        return dict(data.get("createOperator", data))

    async def find_recent_tasks_by_caller_phone(
        self, caller_phone: str, since: datetime, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """All non-callback tasks of a given client phone since `since`."""
        national = normalize_ru_phone(caller_phone)
        if not national:
            return []
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            response = await self._client.get(
                "/rest/tasks",
                params={
                    "filter": (
                        f"callerPhone.primaryPhoneNumber[eq]:{national}"
                        f",createdAt[gt]:{since_iso}"
                    ),
                    "order_by": "createdAt[DescNullsLast]",
                    "limit": limit,
                },
            )
            response.raise_for_status()
            items = response.json().get("data", {}).get("tasks", []) or []
            return [t for t in items if not t.get("isOutgoingCallback")]
        except httpx.HTTPError:
            logger.exception(
                "find_recent_tasks_by_caller_phone failed phone=%s", national,
            )
            return []

    async def find_recent_task_by_caller_phone(
        self, caller_phone: str, since: datetime,
    ) -> dict[str, Any] | None:
        """Most-recent non-callback Task whose Task.callerPhone matches.

        Used to attach OUTGOING CallRecord-ы (callbacks operatоrов клиенту)
        к тикету, который этот же клиент завёл ранее. Skips tasks marked
        isOutgoingCallback=true (those ARE the callback-only tickets we
        flagged in the historical backfill — pointing to one of them
        defeats the purpose).
        """
        national = normalize_ru_phone(caller_phone)
        if not national:
            return None
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            response = await self._client.get(
                "/rest/tasks",
                params={
                    "filter": (
                        f"callerPhone.primaryPhoneNumber[eq]:{national}"
                        f",createdAt[gt]:{since_iso}"
                    ),
                    "order_by": "createdAt[DescNullsLast]",
                    "limit": 5,
                },
            )
            response.raise_for_status()
            items = response.json().get("data", {}).get("tasks", []) or []
            for t in items:
                if not t.get("isOutgoingCallback"):
                    return t
            return None
        except httpx.HTTPError:
            logger.exception(
                "find_recent_task_by_caller_phone failed phone=%s", national,
            )
            return None

    async def find_recent_tasks_by_location_id(
        self, location_id: str, since: datetime, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Tasks attached to this Location (by locationRelId) since a given time.

        Used by the repeat detector to look up prior tasks from the same
        outlet within a 3-day window.
        """
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            response = await self._client.get(
                "/rest/tasks",
                params={
                    "filter": f"locationRelId[eq]:{location_id},createdAt[gt]:{since_iso}",
                    "order_by": "createdAt[DescNullsLast]",
                    "limit": limit,
                },
            )
            response.raise_for_status()
            return list(response.json().get("data", {}).get("tasks", []))
        except httpx.HTTPError:
            logger.exception("find_recent_tasks_by_location_id failed loc=%s", location_id)
            return []

    async def create_person(self, telegram_id: int, name: str) -> TwentyPerson:
        """Создать контакт."""
        try:
            payload = {
                "name": {"firstName": name},
                "telegramid": str(telegram_id),
            }
            response = await self._client.post("/rest/people", json=payload)
            response.raise_for_status()
            data = response.json()

            created = data.get("data", {}).get("createPerson", {})
            person = TwentyPerson(
                twenty_id=created.get("id", ""),
                telegram_id=telegram_id,
                name=created.get("name", {}).get("firstName", ""),
            )
            return person
        except httpx.HTTPError as e:
            raise RuntimeError(f"Failed to create person: {e}") from e

    # --- Metadata API ---
    # The REST `/rest/metadata/*` surface is deliberately limited: ObjectForUpdate
    # only exposes `isActive`, FieldForUpdate hides `isUnique` and
    # `isLabelSyncedWithName`. Everything below talks to GraphQL `/metadata`,
    # which exposes the full surface (skipNameField on create, isUnique on
    # fields, labelIdentifierFieldMetadataId on objects).

    async def _gql_metadata(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/metadata",
            json={"query": query, "variables": variables},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            logger.error("Twenty GraphQL metadata error: %s", body["errors"])
            raise RuntimeError(f"Twenty metadata error: {body['errors']}")
        return dict(body.get("data") or {})

    _OBJECT_FIELDS_FRAGMENT = """
        id
        nameSingular
        namePlural
        labelSingular
        labelPlural
        icon
        isCustom
        isActive
        isSystem
        isLabelSyncedWithName
        labelIdentifierFieldMetadataId
        fieldsList {
            id
            name
            label
            type
            isCustom
            isSystem
            isActive
            isNullable
            isUnique
            isLabelSyncedWithName
            options
            settings
        }
    """

    async def list_objects_metadata(self) -> list[dict[str, Any]]:
        """Все объекты + их поля. Через GraphQL `/metadata`.

        В отличие от REST `/rest/metadata/objects`, здесь возвращаются
        labelIdentifierFieldMetadataId, isLabelSyncedWithName, isUnique —
        что нужно bootstrap-у.
        """
        query = (
            "query Objects($paging: CursorPaging!, $filter: ObjectFilter!) {"
            "  objects(paging: $paging, filter: $filter) {"
            "    edges { node {"
            + self._OBJECT_FIELDS_FRAGMENT
            + "    } } } }"
        )
        data = await self._gql_metadata(
            query, {"paging": {"first": 200}, "filter": {}}
        )
        edges = ((data.get("objects") or {}).get("edges")) or []
        objects: list[dict[str, Any]] = []
        for edge in edges:
            node = dict(edge.get("node") or {})
            # Normalize: legacy callers expect `fields` (flat list)
            node["fields"] = list(node.pop("fieldsList", None) or [])
            objects.append(node)
        return objects

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
        """Создать кастомный объект через `createOneObject`.

        skip_name_field=True предотвращает автогенерацию дефолтного TEXT-поля
        `name`. Тогда первое создаваемое нами поле станет
        labelIdentifierFieldMetadataId объекта.
        """
        mutation = """
        mutation CreateObject($input: CreateOneObjectInput!) {
          createOneObject(input: $input) { id nameSingular labelIdentifierFieldMetadataId }
        }
        """
        variables = {
            "input": {
                "object": {
                    "nameSingular": name_singular,
                    "namePlural": name_plural,
                    "labelSingular": label_singular,
                    "labelPlural": label_plural,
                    "description": description,
                    "icon": icon,
                    "skipNameField": skip_name_field,
                    "isLabelSyncedWithName": is_label_synced_with_name,
                }
            }
        }
        data = await self._gql_metadata(mutation, variables)
        return dict(data.get("createOneObject") or {})

    async def gql_update_object(
        self, object_id: str, update: dict[str, Any]
    ) -> dict[str, Any]:
        """Обновить objectMetadata. Поддерживает labelIdentifierFieldMetadataId,
        labelSingular/Plural, isLabelSyncedWithName и т.п.
        """
        mutation = """
        mutation UpdateObject($input: UpdateOneObjectInput!) {
          updateOneObject(input: $input) {
            id nameSingular labelIdentifierFieldMetadataId isLabelSyncedWithName
          }
        }
        """
        data = await self._gql_metadata(
            mutation, {"input": {"id": object_id, "update": update}}
        )
        return dict(data.get("updateOneObject") or {})

    async def gql_create_field(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Создать поле через `createOneField`. Принимает полный CreateFieldInput:
        type, name, label, objectMetadataId, isNullable, isUnique,
        isLabelSyncedWithName, options, settings, relationCreationPayload.
        """
        mutation = """
        mutation CreateField($input: CreateOneFieldMetadataInput!) {
          createOneField(input: $input) {
            id name label type isUnique isNullable isLabelSyncedWithName
          }
        }
        """
        data = await self._gql_metadata(mutation, {"input": {"field": spec}})
        return dict(data.get("createOneField") or {})

    async def gql_update_field(
        self, field_id: str, update: dict[str, Any]
    ) -> dict[str, Any]:
        mutation = """
        mutation UpdateField($input: UpdateOneFieldMetadataInput!) {
          updateOneField(input: $input) {
            id name label isUnique isNullable isLabelSyncedWithName isActive
          }
        }
        """
        data = await self._gql_metadata(
            mutation, {"input": {"id": field_id, "update": update}}
        )
        return dict(data.get("updateOneField") or {})

    async def gql_delete_field(self, field_id: str) -> bool:
        mutation = """
        mutation DeleteField($input: DeleteOneFieldInput!) {
          deleteOneField(input: $input) { id }
        }
        """
        data = await self._gql_metadata(mutation, {"input": {"id": field_id}})
        return bool((data.get("deleteOneField") or {}).get("id"))

    async def fetch_task_field_options(self) -> dict[str, list[dict[str, str]]]:
        """Запросить актуальные списки kategoriya и vazhnost из метаданных Twenty."""
        result: dict[str, list[dict[str, str]]] = {"kategoriya": [], "vazhnost": []}
        try:
            response = await self._client.get("/rest/metadata/objects")
            response.raise_for_status()
            objects = response.json().get("data", {}).get("objects", [])
            for obj in objects:
                if obj.get("nameSingular") != "task":
                    continue
                for fld in obj.get("fields", []):
                    name = fld.get("name", "")
                    if name in ("kategoriya", "vazhnost"):
                        options = fld.get("options", [])
                        result[name] = [
                            {"label": o["label"], "value": o["value"]}
                            for o in options
                            if "label" in o and "value" in o
                        ]
                break
        except Exception:
            logger.exception("Failed to fetch task field options from Twenty metadata")
        logger.info(
            "fetch_task_field_options: kategoriya=%d, vazhnost=%d",
            len(result["kategoriya"]),
            len(result["vazhnost"]),
        )
        return result

    async def create_task(
        self,
        title: str,
        body: str,
        due_at: datetime | None,
        assignee_id: str | None,
        kategoriya: str | None = None,
        vazhnost: str | None = None,
        *,
        location_rel_id: str | None = None,
        caller_phone: str | None = None,
        call_record_rel_id: str | None = None,
        povtornoe_obrashchenie: bool | None = None,
        parent_task_id: str | None = None,
        istochnik: str | None = None,
        obrashchenie_kind: str | None = None,
    ) -> TwentyTask:
        """Создать задачу."""
        try:
            payload: dict[str, Any] = {
                "status": "TODO",
                "title": title,
                "bodyV2": {"markdown": body},
            }

            if due_at is not None:
                payload["dueAt"] = due_at.strftime("%Y-%m-%dT%H:%M:%SZ")

            if assignee_id is not None:
                payload["assigneeId"] = assignee_id

            if kategoriya is not None:
                payload["kategoriya"] = kategoriya

            if vazhnost is not None:
                payload["vazhnost"] = vazhnost

            if caller_phone is not None:
                national = normalize_ru_phone(caller_phone)
                # Skip malformed phones — Twenty rejects ill-formed PHONES.
                if national:
                    payload["callerPhone"] = to_phones_composite(national)

            if location_rel_id is not None:
                payload["locationRelId"] = location_rel_id

            if call_record_rel_id is not None:
                payload["callRecordRelId"] = call_record_rel_id

            if povtornoe_obrashchenie is not None:
                payload["povtornoeObrashchenie"] = povtornoe_obrashchenie

            if parent_task_id is not None:
                payload["parentTaskId"] = parent_task_id

            if istochnik is not None:
                # SELECT options on Task.istochnikObrashcheniya:
                # ZVONOK | POCHTA | SMS | TELEGRAM | TORGOVYY | DRUGOE
                payload["istochnikObrashcheniya"] = istochnik

            if obrashchenie_kind is not None:
                # SELECT options on Task.obrashchenieKind:
                # NEW | REPEAT | SYSTEM (chain length in 3-day window)
                payload["obrashchenieKind"] = obrashchenie_kind

            response = await self._client.post("/rest/tasks", json=payload)
            if response.status_code >= 400:
                logger.error(
                    "Twenty create_task failed: %s %s payload=%s",
                    response.status_code,
                    response.text[:300],
                    payload,
                )
            response.raise_for_status()
            data = response.json()

            created = data.get("data", {}).get("createTask", {})
            task = TwentyTask(
                twenty_id=created.get("id", ""),
                title=created.get("title", ""),
                body=created.get("bodyV2", {}).get("markdown", ""),
                status=created.get("status", "TODO"),
                due_at=_parse_datetime(created.get("dueAt")),
                assignee_id=created.get("assigneeId"),
                person_id=None,
            )
            return task
        except httpx.HTTPError as e:
            raise RuntimeError(f"Failed to create task: {e}") from e

    async def link_person_to_task(self, task_id: str, person_id: str) -> None:
        """Связать контакт с задачей."""
        try:
            payload = {
                "taskId": task_id,
                "targetPersonId": person_id,
            }
            response = await self._client.post("/rest/taskTargets", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"Failed to link person to task: {e}") from e

    _ATTACHMENT_FILE_FIELD_ID = "0d953c19-1809-41e8-8f78-80d18836bd9d"

    async def upload_file(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> str | None:
        """Загрузить файл в Twenty через GraphQL multipart upload. Возвращает file ID."""
        import json as _json

        operations = _json.dumps(
            {
                "query": (
                    "mutation UploadFilesFieldFile($file: Upload!, $fieldMetadataId: String!) "
                    "{ uploadFilesFieldFile(file: $file, fieldMetadataId: $fieldMetadataId) "
                    "{ id path } }"
                ),
                "variables": {"file": None, "fieldMetadataId": self._ATTACHMENT_FILE_FIELD_ID},
            }
        )
        files = {
            "operations": (None, operations, "application/json"),
            "map": (None, '{"0":["variables.file"]}', "application/json"),
            "0": (filename, file_bytes, content_type),
        }
        try:
            response = await self._client.post("/metadata", files=files)  # type: ignore[arg-type]
            response.raise_for_status()
            data = response.json()
            uploaded = data.get("data", {}).get("uploadFilesFieldFile", {})
            file_id = uploaded.get("id")
            if not file_id:
                logger.warning("Twenty upload_file: no id in response: %s", data)
            return file_id  # type: ignore[no-any-return]
        except httpx.HTTPError as e:
            logger.warning("Twenty upload_file failed for %s: %s", filename, e)
            return None

    async def create_attachment(self, task_id: str, name: str, uploaded_file_id: str) -> None:
        """Создать attachment с загруженным файлом, привязать к задаче."""
        try:
            payload = {
                "name": name,
                "file": [{"fileId": uploaded_file_id, "label": name}],
                "targetTaskId": task_id,
            }
            response = await self._client.post("/rest/attachments", json=payload)
            if response.status_code >= 400:
                logger.warning(
                    "Twenty create_attachment failed: %s %s",
                    response.status_code,
                    response.text[:300],
                )
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("Failed to create attachment %s: %s", name, e)

    async def update_task_body(self, task_id: str, body: str) -> None:
        """Обновить body задачи."""
        try:
            payload = {"bodyV2": {"markdown": body}}
            response = await self._client.patch(f"/rest/tasks/{task_id}", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("Failed to update task body %s: %s", task_id, e)

    # -- TaskCRMPort protocol methods (stub implementations for migration) --

    async def get_conversations(
        self, assignee_id: str, status: str = "open", page: int = 1
    ) -> list[Any]:
        """Получить задачи пользователя из Twenty CRM."""
        try:
            # Map status: open → TODO/V_RABOTE
            params: dict[str, str] = {
                "filter": f"assigneeId[eq]:{assignee_id}",
                "limit": "20",
            }
            response = await self._client.get("/rest/tasks", params=params)
            response.raise_for_status()
            data = response.json()
            tasks = data.get("data", {}).get("tasks", [])

            from enum import Enum
            from types import SimpleNamespace

            class _Status(Enum):
                TODO = "TODO"
                V_RABOTE = "V_RABOTE"
                VYPOLNENO = "VYPOLNENO"
                KORZINA = "KORZINA"

            result = []
            for t in tasks:
                task_status = t.get("status", "TODO")
                # Filter by requested status
                if status == "open" and task_status not in ("TODO", "V_RABOTE"):
                    continue
                try:
                    s = _Status(task_status)
                except ValueError:
                    s = _Status.TODO
                result.append(
                    SimpleNamespace(
                        task_id=t.get("id", ""),
                        title=t.get("title", ""),
                        status=s,
                        assignee_crm_id=assignee_id,
                    )
                )
            return result
        except Exception:
            logger.exception("Failed to get tasks from Twenty")
            return []

    async def update_task_status(self, task_id: str, status: str) -> None:
        """Update task status in Twenty CRM."""
        payload = {"status": status}
        response = await self._client.patch(f"/rest/tasks/{task_id}", json=payload)
        response.raise_for_status()

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        try:
            r = await self._client.get(f"/rest/tasks/{task_id}")
            r.raise_for_status()
            payload = r.json().get("data", {})
            task = payload.get("task") or payload
            return dict(task) if task else None
        except httpx.HTTPError:
            logger.exception("Twenty get_task failed: %s", task_id)
            return None

    async def update_task_script_check(
        self, task_id: str, violations: int, missing: list[str]
    ) -> None:
        """Store the check_script result on a Task (Stage 7 fields)."""
        payload = {
            "scriptViolations": violations,
            "scriptMissing": "; ".join(missing) if missing else "",
        }
        r = await self._client.patch(f"/rest/tasks/{task_id}", json=payload)
        if r.status_code >= 400:
            logger.error(
                "Twenty update_task_script_check failed: %s %s",
                r.status_code, r.text[:300],
            )
            r.raise_for_status()

    async def update_conversation_status(self, task_id: int, status: str) -> None:
        """Обновить статус задачи (legacy stub)."""

    async def update_conversation_assignee(self, task_id: int, assignee_id: int) -> None:
        """Переназначить задачу (stub)."""

    async def add_message(self, task_id: int, content: str, private: bool = True) -> None:
        """Добавить комментарий к задаче (stub)."""

    async def close(self) -> None:
        """Закрыть HTTP клиент."""
        await self._client.aclose()


def _parse_datetime(value: str | None) -> datetime | None:
    """Парсить ISO datetime строку."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
