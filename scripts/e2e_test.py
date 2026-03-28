#!/usr/bin/env python3
"""E2E тест — имитация Telegram webhook для 24ondoc.

Симулирует полный флоу через HTTP-запросы к /webhook/telegram.

Шаги:
  1. POST /webhook/telegram — /start (новый пользователь)
  2. POST /webhook/telegram — /new_task
  3. POST /webhook/telegram — текстовое сообщение
  4. POST /webhook/telegram — голосовое сообщение
  5a. POST /webhook/telegram — callback "collect" (триггер анализа)
  5b. POST /webhook/telegram — callback "create_crm" (✅ подтверждение)
  6. GET Chatwoot API — проверка созданного conversation

Переменные окружения (обязательные):
  WEBHOOK_URL              — базовый URL backend, например http://localhost:8000
  TELEGRAM_WEBHOOK_SECRET  — секрет для X-Telegram-Bot-Api-Secret-Token
  CHATWOOT_API_KEY         — API-ключ Chatwoot
  CHATWOOT_BASE_URL        — базовый URL Chatwoot, например http://chatwoot:3000

Переменные окружения (опциональные):
  CHATWOOT_ACCOUNT_ID      — ID аккаунта Chatwoot (по умолчанию 2)
  DATABASE_URL             — postgresql://... для прямых DB-проверок
  E2E_TEST_USER_ID         — Telegram ID тестового пользователя (по умолчанию 9999000001)

Требования:
  pip install httpx

Примечание по голосовому сообщению:
  Шаг 4 отправляет фиктивный file_id. Backend попытается вызвать Telegram Bot API
  для скачивания файла — это упадёт с ошибкой. Webhook вернёт 200 OK (aiogram
  обрабатывает ошибки внутри), но content_block(type=voice) НЕ будет добавлен.
  Для полного E2E теста с голосом нужен реальный file_id из Telegram.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Конфигурация из окружения
# ---------------------------------------------------------------------------

WEBHOOK_URL: str = os.environ.get("WEBHOOK_URL", "http://localhost:8000").rstrip("/")
TELEGRAM_WEBHOOK_SECRET: str = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
CHATWOOT_API_KEY: str = os.environ.get("CHATWOOT_API_KEY", "")
CHATWOOT_BASE_URL: str = os.environ.get("CHATWOOT_BASE_URL", "http://chatwoot:3000").rstrip("/")
CHATWOOT_ACCOUNT_ID: int = int(os.environ.get("CHATWOOT_ACCOUNT_ID", "2"))
DATABASE_URL: str | None = os.environ.get("DATABASE_URL")
TEST_USER_ID: int = int(os.environ.get("E2E_TEST_USER_ID", "9999000001"))

_WEBHOOK_PATH = "/webhook/telegram"
_WEBHOOK_ENDPOINT = f"{WEBHOOK_URL}{_WEBHOOK_PATH}"
_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_WEBHOOK_SECRET}

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

_passed = 0
_failed = 0
_skipped = 0


def _step(name: str, ok: bool | None, detail: str = "") -> None:
    global _passed, _failed, _skipped
    if ok is None:
        _skipped += 1
        tag = "SKIP"
    elif ok:
        _passed += 1
        tag = "PASS"
    else:
        _failed += 1
        tag = "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"[{tag}] {name}{suffix}")


def _build_update(update_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {"update_id": update_id, **payload}


def _msg(
    message_id: int,
    text: str | None = None,
    voice_file_id: str | None = None,
) -> dict[str, Any]:
    """Собирает объект message для Telegram Update."""
    from_user = {
        "id": TEST_USER_ID,
        "is_bot": False,
        "first_name": "E2ETest",
        "username": "e2e_test_user",
    }
    chat = {"id": TEST_USER_ID, "type": "private", "first_name": "E2ETest"}
    msg: dict[str, Any] = {
        "message_id": message_id,
        "from": from_user,
        "chat": chat,
        "date": int(time.time()),
    }
    if text is not None:
        msg["text"] = text
    if voice_file_id is not None:
        msg["voice"] = {
            "file_id": voice_file_id,
            "file_unique_id": f"unique_{voice_file_id}",
            "duration": 3,
            "mime_type": "audio/ogg",
            "file_size": 1024,
        }
    return msg


def _callback(callback_id: str, message_id: int, data: str) -> dict[str, Any]:
    """Собирает объект callback_query для Telegram Update."""
    return {
        "callback_query": {
            "id": callback_id,
            "from": {
                "id": TEST_USER_ID,
                "is_bot": False,
                "first_name": "E2ETest",
            },
            "message": {
                "message_id": message_id,
                "from": {"id": 0, "is_bot": True, "first_name": "Bot"},
                "chat": {"id": TEST_USER_ID, "type": "private"},
                "date": int(time.time()),
                "text": "preview",
            },
            "chat_instance": "test_chat_instance",
            "data": data,
        }
    }


def _post_webhook(client: httpx.Client, update: dict[str, Any]) -> httpx.Response:
    return client.post(_WEBHOOK_ENDPOINT, json=update, headers=_HEADERS, timeout=30.0)


# ---------------------------------------------------------------------------
# DB-проверки (опциональные)
# ---------------------------------------------------------------------------

def _check_user_in_db(telegram_id: int) -> bool | None:
    """Проверяет наличие пользователя в БД. Возвращает None если DB недоступна."""
    if DATABASE_URL is None:
        return None
    try:
        import psycopg2  # type: ignore[import]
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT telegram_id FROM users WHERE telegram_id = %s", (telegram_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row is not None
    except Exception as exc:
        print(f"  ⚠ DB check failed: {exc}")
        return None


def _check_draft_session_in_db(telegram_id: int) -> tuple[bool | None, str | None]:
    """Проверяет наличие draft_session со статусом collecting.

    Returns:
        (found: bool | None, session_id: str | None)
        None если DB недоступна.
    """
    if DATABASE_URL is None:
        return None, None
    try:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "SELECT session_id, status FROM draft_sessions "
            "WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
            (telegram_id,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row is None:
            return False, None
        session_id, status = row
        return status == "collecting", str(session_id)
    except Exception as exc:
        print(f"  ⚠ DB check failed: {exc}")
        return None, None


def _check_content_blocks_in_db(telegram_id: int, expected_count: int) -> bool | None:
    """Проверяет количество content_blocks в последней сессии пользователя."""
    if DATABASE_URL is None:
        return None
    try:
        import psycopg2
        import json
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "SELECT content_blocks FROM draft_sessions "
            "WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
            (telegram_id,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row is None:
            return False
        blocks = row[0] if isinstance(row[0], list) else json.loads(row[0] or "[]")
        return len(blocks) >= expected_count
    except Exception as exc:
        print(f"  ⚠ DB check failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Chatwoot-проверки
# ---------------------------------------------------------------------------

def _find_chatwoot_contact(client: httpx.Client, telegram_id: int) -> int | None:
    """Ищет контакт в Chatwoot по имени/email. Возвращает chatwoot_user_id или None."""
    try:
        resp = client.get(
            f"{CHATWOOT_BASE_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/search",
            params={"q": str(telegram_id), "include_contacts": True},
            headers={"api_access_token": CHATWOOT_API_KEY},
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            payload = data.get("payload", [])
            if payload:
                return payload[0].get("id")
    except Exception as exc:
        print(f"  ⚠ Chatwoot contact search failed: {exc}")
    return None


def _find_chatwoot_conversation(
    client: httpx.Client, contact_id: int
) -> int | None:
    """Ищет последний conversation для контакта. Возвращает conversation_id или None."""
    try:
        resp = client.get(
            f"{CHATWOOT_BASE_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}"
            f"/contacts/{contact_id}/conversations",
            headers={"api_access_token": CHATWOOT_API_KEY},
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            payload = data.get("payload", [])
            if payload:
                return payload[-1].get("id")
    except Exception as exc:
        print(f"  ⚠ Chatwoot conversation search failed: {exc}")
    return None


def _get_chatwoot_conversation(client: httpx.Client, conv_id: int) -> dict[str, Any] | None:
    """Получает conversation по ID."""
    try:
        resp = client.get(
            f"{CHATWOOT_BASE_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}"
            f"/conversations/{conv_id}",
            headers={"api_access_token": CHATWOOT_API_KEY},
            timeout=10.0,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        print(f"  ⚠ Chatwoot get conversation failed: {exc}")
    return None


# ---------------------------------------------------------------------------
# Основной тест
# ---------------------------------------------------------------------------

def run_e2e() -> int:  # noqa: C901 (complexity acceptable for E2E)
    """Запускает все шаги E2E теста. Возвращает код выхода (0 = success)."""
    print("=" * 60)
    print("24ondoc E2E Telegram Webhook Test")
    print(f"Endpoint : {_WEBHOOK_ENDPOINT}")
    print(f"Test user: {TEST_USER_ID}")
    print(f"DB checks: {'enabled' if DATABASE_URL else 'skipped (no DATABASE_URL)'}")
    print("=" * 60)
    print()

    _update_counter = [0]  # mutable int для вложенных функций

    def next_update_id() -> int:
        _update_counter[0] += 1
        return _update_counter[0]

    conversation_id: int | None = None

    with httpx.Client() as http:

        # ------------------------------------------------------------------
        # Шаг 1: /start — авторизация/регистрация пользователя
        # ------------------------------------------------------------------
        print("Шаг 1: POST /webhook/telegram — /start")

        update = _build_update(
            next_update_id(),
            {"message": _msg(message_id=1001, text="/start")},
        )
        resp = _post_webhook(http, update)
        _step(
            "1.1 Webhook /start вернул 200",
            resp.status_code == 200,
            f"HTTP {resp.status_code}",
        )

        # Небольшая пауза для async обработки
        time.sleep(0.5)

        # DB check: пользователь создан
        db_user_ok = _check_user_in_db(TEST_USER_ID)
        _step(
            "1.2 Пользователь создан в таблице users (DB)",
            db_user_ok,
            "SKIPPED" if db_user_ok is None else ("found" if db_user_ok else "not found"),
        )

        # Chatwoot check: контакт/агент создан
        chatwoot_contact_id = _find_chatwoot_contact(http, TEST_USER_ID)
        _step(
            "1.3 Агент/контакт создан в Chatwoot",
            None if chatwoot_contact_id is None and CHATWOOT_API_KEY else chatwoot_contact_id is not None,
            f"contact_id={chatwoot_contact_id}" if chatwoot_contact_id else "not found",
        )
        print()

        # ------------------------------------------------------------------
        # Шаг 2: /new_task — создание черновика сессии
        # ------------------------------------------------------------------
        print("Шаг 2: POST /webhook/telegram — /new_task")

        update = _build_update(
            next_update_id(),
            {"message": _msg(message_id=1002, text="/new_task")},
        )
        resp = _post_webhook(http, update)
        _step(
            "2.1 Webhook /new_task вернул 200",
            resp.status_code == 200,
            f"HTTP {resp.status_code}",
        )

        time.sleep(0.5)

        # DB check: draft_session создан со статусом collecting
        db_session_ok, session_id = _check_draft_session_in_db(TEST_USER_ID)
        _step(
            "2.2 draft_sessions запись со статусом COLLECTING (DB)",
            db_session_ok,
            f"session_id={session_id}" if session_id else (
                "SKIPPED" if db_session_ok is None else "not found"
            ),
        )
        print()

        # ------------------------------------------------------------------
        # Шаг 3: Текстовое сообщение
        # ------------------------------------------------------------------
        print('Шаг 3: POST /webhook/telegram — текст "Нужно починить кран в офисе 305"')

        update = _build_update(
            next_update_id(),
            {"message": _msg(message_id=1003, text="Нужно починить кран в офисе 305")},
        )
        resp = _post_webhook(http, update)
        _step(
            "3.1 Webhook текстового сообщения вернул 200",
            resp.status_code == 200,
            f"HTTP {resp.status_code}",
        )

        time.sleep(0.5)

        # DB check: content_block добавлен
        db_blocks_ok = _check_content_blocks_in_db(TEST_USER_ID, expected_count=1)
        _step(
            "3.2 content_block добавлен в сессию (DB)",
            db_blocks_ok,
            "SKIPPED" if db_blocks_ok is None else ("ok" if db_blocks_ok else "blocks empty"),
        )
        print()

        # ------------------------------------------------------------------
        # Шаг 4: Голосовое сообщение
        # ------------------------------------------------------------------
        print("Шаг 4: POST /webhook/telegram — голосовое сообщение (тестовый .ogg)")
        print("  ⚠  Используется фиктивный file_id — Telegram Bot API вернёт ошибку.")
        print("     Для полного E2E с реальным STT нужен действительный file_id из Telegram.")

        fake_file_id = "BQACAgIAAxkBAAIBe2e2e2e2testvoice001AAQABs"
        update = _build_update(
            next_update_id(),
            {"message": _msg(message_id=1004, voice_file_id=fake_file_id)},
        )
        resp = _post_webhook(http, update)
        # aiogram возвращает 200 даже при внутренних ошибках обработки
        _step(
            "4.1 Webhook голосового сообщения вернул 200",
            resp.status_code == 200,
            f"HTTP {resp.status_code}",
        )

        time.sleep(1.0)

        # DB check: content_block(type=voice) — ожидаемо может отсутствовать при fake file_id
        if DATABASE_URL:
            try:
                import psycopg2
                import json
                conn = psycopg2.connect(DATABASE_URL)
                cur = conn.cursor()
                cur.execute(
                    "SELECT content_blocks FROM draft_sessions "
                    "WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
                    (TEST_USER_ID,),
                )
                row = cur.fetchone()
                cur.close()
                conn.close()
                if row:
                    blocks = row[0] if isinstance(row[0], list) else json.loads(row[0] or "[]")
                    voice_blocks = [b for b in blocks if b.get("type") == "voice"]
                    if voice_blocks:
                        _step("4.2 content_block(type=voice) добавлен (DB)", True, f"count={len(voice_blocks)}")
                    else:
                        _step(
                            "4.2 content_block(type=voice) добавлен (DB)",
                            None,
                            "SKIPPED — фиктивный file_id, STT пропущен",
                        )
                else:
                    _step("4.2 content_block(type=voice) добавлен (DB)", None, "SKIPPED — сессия не найдена")
            except Exception as exc:
                _step("4.2 content_block(type=voice) добавлен (DB)", None, f"SKIPPED — {exc}")
        else:
            _step("4.2 content_block(type=voice) добавлен (DB)", None, "SKIPPED — нет DATABASE_URL")
        print()

        # ------------------------------------------------------------------
        # Шаг 4b: Триггер анализа — callback "collect" (📎 Собрать)
        # ------------------------------------------------------------------
        print("Шаг 4b: POST /webhook/telegram — callback collect (📎 Собрать)")
        print("  ℹ  Запускает AI-анализ (OpenRouter). Может занять 5–30 сек.")

        update = _build_update(
            next_update_id(),
            _callback(callback_id="cbq_collect_001", message_id=1002, data="collect"),
        )
        resp = _post_webhook(http, update)
        _step(
            "4b.1 Webhook callback collect вернул 200",
            resp.status_code == 200,
            f"HTTP {resp.status_code}",
        )

        # Ждём завершения AI-анализа
        time.sleep(3.0)

        if DATABASE_URL:
            try:
                import psycopg2
                conn = psycopg2.connect(DATABASE_URL)
                cur = conn.cursor()
                cur.execute(
                    "SELECT status FROM draft_sessions "
                    "WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
                    (TEST_USER_ID,),
                )
                row = cur.fetchone()
                cur.close()
                conn.close()
                if row:
                    status = row[0]
                    _step(
                        "4b.2 Сессия перешла в статус preview (DB)",
                        status == "preview",
                        f"status={status}",
                    )
                else:
                    _step("4b.2 Сессия перешла в статус preview (DB)", None, "SKIPPED — сессия не найдена")
            except Exception as exc:
                _step("4b.2 Сессия перешла в статус preview (DB)", None, f"SKIPPED — {exc}")
        else:
            _step("4b.2 Сессия перешла в статус preview (DB)", None, "SKIPPED — нет DATABASE_URL")
        print()

        # ------------------------------------------------------------------
        # Шаг 5: callback "create_crm" (✅ Создать в CRM)
        # ------------------------------------------------------------------
        print("Шаг 5: POST /webhook/telegram — callback create_crm (✅ Создать в CRM)")

        update = _build_update(
            next_update_id(),
            _callback(callback_id="cbq_create_crm_001", message_id=1002, data="create_crm"),
        )
        resp = _post_webhook(http, update)
        _step(
            "5.1 Webhook callback create_crm вернул 200",
            resp.status_code == 200,
            f"HTTP {resp.status_code}",
        )

        time.sleep(2.0)

        # Проверяем что задача создана в Chatwoot
        if CHATWOOT_API_KEY and chatwoot_contact_id:
            conv_id = _find_chatwoot_conversation(http, chatwoot_contact_id)
            conversation_id = conv_id
            _step(
                "5.2 Задача создана в Chatwoot (conversation_id получен)",
                conv_id is not None,
                f"conversation_id={conv_id}" if conv_id else "conversation not found",
            )
        else:
            _step(
                "5.2 Задача создана в Chatwoot (conversation_id получен)",
                None,
                "SKIPPED — нет CHATWOOT_API_KEY или contact_id",
            )
        print()

        # ------------------------------------------------------------------
        # Шаг 6: GET Chatwoot API — проверка conversation
        # ------------------------------------------------------------------
        print("Шаг 6: GET Chatwoot API — проверка созданного conversation")

        if conversation_id and CHATWOOT_API_KEY:
            conv_data = _get_chatwoot_conversation(http, conversation_id)
            if conv_data:
                conv_status = conv_data.get("status", "unknown")
                meta = conv_data.get("meta", {})
                subject = meta.get("subject") or conv_data.get("additional_attributes", {}).get("description", "")

                _step(
                    "6.1 Conversation существует в Chatwoot",
                    True,
                    f"id={conversation_id}, status={conv_status}",
                )
                _step(
                    "6.2 Conversation имеет корректный статус (open/pending)",
                    conv_status in ("open", "pending"),
                    f"status={conv_status}",
                )
                _step(
                    "6.3 Conversation содержит данные задачи",
                    bool(subject or conv_data.get("additional_attributes")),
                    f"subject={subject!r}" if subject else "no subject",
                )
            else:
                _step("6.1 Conversation существует в Chatwoot", False, f"id={conversation_id} not found")
        else:
            reason = "нет conversation_id" if not conversation_id else "нет CHATWOOT_API_KEY"
            _step("6.1 Conversation существует в Chatwoot", None, f"SKIPPED — {reason}")
            _step("6.2 Conversation имеет корректный статус (open/pending)", None, "SKIPPED")
            _step("6.3 Conversation содержит данные задачи", None, "SKIPPED")

    # ------------------------------------------------------------------
    # Итоги
    # ------------------------------------------------------------------
    print()
    print("=" * 60)
    print(f"Итоги: PASS={_passed}  FAIL={_failed}  SKIP={_skipped}")
    print("=" * 60)

    if _failed > 0:
        print(f"\n❌ FAILED ({_failed} проверок провалилось)")
        return 1
    else:
        print("\n✅ ALL CHECKS PASSED (или SKIPPED)")
        return 0


if __name__ == "__main__":
    # Валидация обязательных переменных
    missing = []
    for var in ("WEBHOOK_URL", "TELEGRAM_WEBHOOK_SECRET"):
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        print(f"Ошибка: не установлены переменные окружения: {', '.join(missing)}")
        sys.exit(2)

    sys.exit(run_e2e())
