"""Import торговых точек из xlsx-каталога в Twenty Location.

Каталог:
  Лист "Результат" с колонками: Организация | ID AnyDesk | Адрес | Номер телефона.
  Поле «Организация» имеет формат «{бренд} {номер}» (например «аполо 02»).

В Twenty создаётся / обновляется кастомный объект `location` с уникальным
displayName = «{Бренд capitalize} {номер}». Идемпотентен: повторный запуск
не плодит дубликатов и обновляет только незаполненные поля.

Скрипт НИЧЕГО не удаляет. Прежде чем запускать первый импорт, прогони
scripts/wipe_legacy_locations.py чтобы убрать 140 авто-созданных точек,
которые остались с предыдущей модели (телефон → точка).

Usage:
    TWENTY_BASE_URL=... TWENTY_API_KEY=... \\
        python scripts/import_locations_xlsx.py path/to/file.xlsx [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
import openpyxl  # type: ignore[import-not-found]

# Phone normalization is reused from the backend module so we stay in sync
# with how Twenty stores PHONES composite values.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "src"))
from twenty_integration.infrastructure.phone import (  # noqa: E402
    normalize_ru_phone,
    to_phones_composite,
)

logger = logging.getLogger("import_locations")

# Twenty: 100 write tokens / 60s. Stay below ~1.5 RPS to keep headroom for
# parallel ATS2 traffic that also writes to Twenty.
_RATE_LIMIT_RPS = 1.5
_MIN_INTERVAL = 1.0 / _RATE_LIMIT_RPS
_last_call = [0.0]


async def _throttle() -> None:
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
    if wait > 0:
        await asyncio.sleep(wait)
    _last_call[0] = time.monotonic()


# --- Xlsx parsing ---

# The supplier's xlsx has mojibake from a double-encoded export. These are the
# characters we've actually seen — narrow targeted fix, not a generic decoder.
_MOJIBAKE_FIXES = (
    ("С‘", "ё"),
    ("С’", "ё"),
    ("ЂЂЂ", "№"),
)


def _fix_address(s: str | None) -> str | None:
    if s is None:
        return None
    out = str(s)
    for bad, good in _MOJIBAKE_FIXES:
        out = out.replace(bad, good)
    return out.strip() or None


def _split_org(org: str) -> tuple[str | None, str | None]:
    """«аполо 02» -> ("Аполо", "02"). Returns (None, None) if format unexpected."""
    if not org:
        return None, None
    parts = org.strip().split(None, 1)
    if len(parts) != 2:
        return None, None
    prefix_raw, number = parts[0], parts[1]
    prefix = prefix_raw.strip().capitalize()
    return prefix, number.strip()


def _display_name(prefix: str | None, number: str | None) -> str | None:
    if not prefix or not number:
        return None
    return f"{prefix} {number}"


@dataclass
class LocationRow:
    display_name: str
    prefix: str
    number: str
    address: str | None
    anydesk_id: str | None
    phones: list[str]  # normalized 10-digit national, possibly empty
    raw_org: str


def parse_xlsx(path: str) -> list[LocationRow]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(h or "").strip() for h in rows[0]]
    expected = ["Организация", "ID AnyDesk", "Адрес", "Номер телефона"]
    if header[: len(expected)] != expected:
        raise ValueError(f"unexpected header: {header}")
    parsed: list[LocationRow] = []
    for r in rows[1:]:
        if not r or all(c is None for c in r):
            continue
        org, anydesk, addr, phone = (r + (None,) * 4)[:4]
        prefix, number = _split_org(str(org or ""))
        dn = _display_name(prefix, number)
        if not dn or prefix is None or number is None:
            logger.warning("skipping row with bad Org=%r", org)
            continue
        # Phones may be a single number or a comma-separated string. The
        # current xlsx has at most one, but the schema supports multiple.
        phones: list[str] = []
        if phone not in (None, ""):
            for raw in re.split(r"[,;/\n\r]+", str(phone)):
                national = normalize_ru_phone(raw)
                if national:
                    phones.append(national)
        parsed.append(
            LocationRow(
                display_name=dn,
                prefix=prefix,
                number=number,
                address=_fix_address(str(addr)) if addr else None,
                anydesk_id=str(anydesk).strip() if anydesk not in (None, "") else None,
                phones=phones,
                raw_org=str(org),
            )
        )
    return parsed


# --- Twenty REST helpers ---


async def _list_existing(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    """Map displayName -> Twenty Location dict.

    Twenty REST не понимает `starting_after`, поэтому пагинируем по
    `filter=id[gt]:<last>` + order_by=id[AscNullsFirst]. Лимит 200 — пик
    каталога 402, влезает за 2-3 страницы.
    """
    by_name: dict[str, dict[str, Any]] = {}
    last_id: str | None = None
    page_size = 200
    for _ in range(20):  # hard cap: 20 * 200 = 4000 records
        params: dict[str, Any] = {
            "limit": page_size,
            "order_by": "id[AscNullsFirst]",
        }
        if last_id:
            params["filter"] = f"id[gt]:{last_id}"
        r = await client.get("/rest/locations", params=params)
        r.raise_for_status()
        page = r.json().get("data", {}).get("locations", []) or []
        if not page:
            break
        for loc in page:
            dn = (loc.get("displayName") or "").strip()
            if dn:
                by_name[dn] = loc
        if len(page) < page_size:
            break
        last_id = page[-1].get("id")
        if not last_id:
            break
    return by_name


def _row_to_create_payload(row: LocationRow) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "displayName": row.display_name,
        "prefix": row.prefix,
        "number": row.number,
    }
    if row.address:
        payload["locationAddress"] = row.address
    if row.anydesk_id:
        payload["anydeskId"] = row.anydesk_id
    if row.phones:
        primary = row.phones[0]
        composite = to_phones_composite(primary)
        if len(row.phones) > 1:
            composite["additionalPhones"] = [
                {"number": p, "callingCode": "+7", "countryCode": "RU"}
                for p in row.phones[1:]
            ]
        payload["phone"] = composite
    return payload


def _patch_for_empty_fields(
    row: LocationRow, existing: dict[str, Any]
) -> dict[str, Any]:
    """Build PATCH that only fills empties — never overwrites manual edits."""
    patch: dict[str, Any] = {}
    if row.prefix and not existing.get("prefix"):
        patch["prefix"] = row.prefix
    if row.number and not existing.get("number"):
        patch["number"] = row.number
    if row.address and not existing.get("locationAddress"):
        patch["locationAddress"] = row.address
    if row.anydesk_id and not existing.get("anydeskId"):
        patch["anydeskId"] = row.anydesk_id
    # Phone is composite; only fill primary if it's currently empty.
    existing_phone = existing.get("phone") or {}
    has_primary = bool((existing_phone or {}).get("primaryPhoneNumber"))
    if row.phones and not has_primary:
        composite = to_phones_composite(row.phones[0])
        if len(row.phones) > 1:
            composite["additionalPhones"] = [
                {"number": p, "callingCode": "+7", "countryCode": "RU"}
                for p in row.phones[1:]
            ]
        patch["phone"] = composite
    return patch


@dataclass
class ImportReport:
    parsed: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


async def import_rows(
    rows: list[LocationRow],
    base_url: str,
    api_key: str,
    apply: bool,
) -> ImportReport:
    report = ImportReport(parsed=len(rows))
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    ) as client:
        existing = await _list_existing(client)
        logger.info("existing locations in Twenty: %d", len(existing))

        for idx, row in enumerate(rows, 1):
            try:
                ex = existing.get(row.display_name)
                if ex is None:
                    payload = _row_to_create_payload(row)
                    if not apply:
                        logger.info("[dry] CREATE %s", row.display_name)
                        report.created += 1
                        continue
                    await _throttle()
                    r = await client.post("/rest/locations", json=payload)
                    if r.status_code == 429:
                        logger.warning("rate-limited on create %s — sleep 62s", row.display_name)
                        await asyncio.sleep(62)
                        r = await client.post("/rest/locations", json=payload)
                    if r.status_code >= 400:
                        report.errors.append(
                            f"create {row.display_name}: {r.status_code} {r.text[:200]}"
                        )
                        continue
                    report.created += 1
                else:
                    patch = _patch_for_empty_fields(row, ex)
                    if not patch:
                        report.unchanged += 1
                        continue
                    if not apply:
                        logger.info("[dry] PATCH %s -> %s", row.display_name, list(patch))
                        report.updated += 1
                        continue
                    await _throttle()
                    r = await client.patch(
                        f"/rest/locations/{ex['id']}", json=patch
                    )
                    if r.status_code == 429:
                        logger.warning("rate-limited on patch %s — sleep 62s", row.display_name)
                        await asyncio.sleep(62)
                        r = await client.patch(
                            f"/rest/locations/{ex['id']}", json=patch
                        )
                    if r.status_code >= 400:
                        report.errors.append(
                            f"patch {row.display_name}: {r.status_code} {r.text[:200]}"
                        )
                        continue
                    report.updated += 1
            except Exception as exc:
                report.errors.append(f"{row.display_name}: {exc!r}")

            if idx % 50 == 0:
                logger.info(
                    "progress: %d/%d (created=%d updated=%d unchanged=%d errors=%d)",
                    idx, len(rows), report.created, report.updated,
                    report.unchanged, len(report.errors),
                )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx", help="Path to the xlsx catalog")
    parser.add_argument("--apply", action="store_true", help="actually write to Twenty")
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

    rows = parse_xlsx(args.xlsx)
    print(f"parsed rows: {len(rows)}")
    if not rows:
        return 0

    report = asyncio.run(import_rows(rows, base_url, api_key, apply=args.apply))
    print(
        f"\n=== Import {'(APPLIED)' if args.apply else '(DRY-RUN)'} ===\n"
        f"  parsed:    {report.parsed}\n"
        f"  created:   {report.created}\n"
        f"  updated:   {report.updated}\n"
        f"  unchanged: {report.unchanged}\n"
        f"  errors:    {len(report.errors)}"
    )
    for e in report.errors[:20]:
        print(f"    ! {e}")
    if len(report.errors) > 20:
        print(f"    ... and {len(report.errors) - 20} more")
    return 0 if not report.errors else 1


if __name__ == "__main__":
    sys.exit(main())
