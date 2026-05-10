"""Add a `TREBUET_RAZBORA` («Требует разбора») option to Task.kategoriya.

Idempotent. Run on prod once after deploying the intent classifier;
the classifier emits this value when no domain category fits or when
intent=NEEDS_REVIEW so the operator can triage the task by hand.

Usage:
    TWENTY_BASE_URL=... TWENTY_API_KEY=... \\
        python scripts/add_kategoriya_trebuet_razbora.py [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from typing import Any

import httpx

VALUE = "TREBUET_RAZBORA"
LABEL = "Требует разбора"
COLOR = "gray"


_QUERY_OBJECTS = """
query Objects($paging: CursorPaging!, $filter: ObjectFilter!) {
  objects(paging: $paging, filter: $filter) {
    edges { node {
      id nameSingular
      fieldsList { id name type options { id label value color position } }
    } }
  }
}
"""

_MUT_UPDATE = """
mutation UpdateField($input: UpdateOneFieldMetadataInput!) {
  updateOneField(input: $input) {
    id name options { id value label }
  }
}
"""


async def _gql(
    client: httpx.AsyncClient, query: str, variables: dict[str, Any],
) -> dict[str, Any]:
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
        data = await _gql(
            client, _QUERY_OBJECTS,
            {"paging": {"first": 200}, "filter": {}},
        )
        edges = (data.get("objects") or {}).get("edges") or []
        task_obj = next(
            (e["node"] for e in edges if e["node"]["nameSingular"] == "task"),
            None,
        )
        if task_obj is None:
            print("task object not found", file=sys.stderr)
            return 2
        kat_field = next(
            (f for f in (task_obj.get("fieldsList") or [])
             if f.get("name") == "kategoriya"),
            None,
        )
        if kat_field is None:
            print("task.kategoriya field not found", file=sys.stderr)
            return 2

        existing = list(kat_field.get("options") or [])
        if any((o.get("value") or "").upper() == VALUE for o in existing):
            print(f"option {VALUE} already present — nothing to do")
            return 0

        next_position = max(
            (int(o.get("position") or 0) for o in existing),
            default=-1,
        ) + 1
        new_opt = {
            "id": str(uuid.uuid4()),
            "label": LABEL,
            "value": VALUE,
            "color": COLOR,
            "position": next_position,
        }
        # Twenty needs the FULL options list on update — anything we omit is
        # dropped. Re-emit existing ones verbatim and append the new one.
        full_options = [
            {
                "id": o["id"],
                "label": o["label"],
                "value": o["value"],
                "color": o.get("color") or "gray",
                "position": int(o.get("position") or 0),
            }
            for o in existing
        ] + [new_opt]

        print(
            f"would add option to task.kategoriya: value={VALUE!r}, "
            f"label={LABEL!r}, position={next_position}",
        )
        print(f"existing options: {len(existing)} → after: {len(full_options)}")
        if not apply:
            print("\n(dry-run; pass --apply to actually update)")
            return 0

        await _gql(
            client, _MUT_UPDATE,
            {"input": {
                "id": kat_field["id"],
                "update": {"options": full_options},
            }},
        )
        print(f"added option {VALUE} to task.kategoriya")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    base_url = os.environ.get("TWENTY_BASE_URL")
    api_key = os.environ.get("TWENTY_API_KEY")
    if not base_url or not api_key:
        print("TWENTY_BASE_URL and TWENTY_API_KEY required", file=sys.stderr)
        return 2
    return asyncio.run(main_async(base_url, api_key, apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
