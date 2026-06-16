"""Drop legacy Person.locationRel + locationPrefix/Number/Address.

Эти поля моделировали 1:1 связь Person↔Location, что не подходит для
N:M (один телефон может принадлежать нескольким точкам, выездной
менеджер). Связь точки и контакта теперь вычитывается из CallRecord/Task
на каждый звонок.

Скрипт удаляет четыре поля метаданных через GraphQL `/metadata`. Данные в
этих колонках на проде почти всё равно пустые (см. аудит на 2026-05-06).

Usage:
    TWENTY_BASE_URL=... TWENTY_API_KEY=... \\
        python scripts/migrate_drop_legacy_fields.py [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

import httpx

logger = logging.getLogger("migrate_drop_legacy_fields")

_TARGETS: tuple[tuple[str, str], ...] = (
    ("person", "locationRel"),
    ("person", "locationPrefix"),
    ("person", "locationNumber"),
    ("person", "locationAddress"),
)


_QUERY_OBJECTS = """
query Objects($paging: CursorPaging!, $filter: ObjectFilter!) {
  objects(paging: $paging, filter: $filter) {
    edges { node {
      id nameSingular
      fieldsList { id name }
    } }
  }
}
"""

_MUT_DELETE = """
mutation DeleteField($input: DeleteOneFieldInput!) {
  deleteOneField(input: $input) { id }
}
"""


async def _gql(client: httpx.AsyncClient, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    r = await client.post("/metadata", json={"query": query, "variables": variables})
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"graphql error: {body['errors']}")
    return dict(body.get("data") or {})


async def main_async(base_url: str, api_key: str, apply: bool) -> int:
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    ) as client:
        data = await _gql(client, _QUERY_OBJECTS, {"paging": {"first": 200}, "filter": {}})
        edges = (data.get("objects") or {}).get("edges") or []
        objects = {e["node"]["nameSingular"]: e["node"] for e in edges}

        plan: list[tuple[str, str, str]] = []  # (object_name, field_name, field_id)
        for obj_name, field_name in _TARGETS:
            obj = objects.get(obj_name)
            if not obj:
                print(f"  {obj_name}: object not found, skipping")
                continue
            field = next(
                (f for f in (obj.get("fieldsList") or []) if f.get("name") == field_name),
                None,
            )
            if not field:
                print(f"  {obj_name}.{field_name}: not present, skipping")
                continue
            plan.append((obj_name, field_name, field["id"]))

        if not plan:
            print("nothing to delete")
            return 0

        print("would delete:")
        for o, f, fid in plan:
            print(f"  - {o}.{f} ({fid})")
        if not apply:
            print("\n(dry-run; pass --apply to actually delete)")
            return 0

        ok = 0
        for o, f, fid in plan:
            try:
                await _gql(client, _MUT_DELETE, {"input": {"id": fid}})
                print(f"  deleted {o}.{f}")
                ok += 1
            except Exception as exc:
                logger.exception("delete %s.%s failed: %s", o, f, exc)
        print(f"\ndeleted {ok}/{len(plan)}")
        return 0 if ok == len(plan) else 1


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
