# 24ondoc — Служба технической поддержки

## Описание

Система автоматизации службы технической поддержки компании 24ondoc. Компания занимается обслуживанием торгового оборудования, кассовых аппаратов, программ 1С, ЕГАИС, ЭДО, сканеров, ОФД и фискальных накопителей.

### Основные функции

- **Telegram-бот** — приём заявок от клиентов (текст, голос, фото, файлы), AI-анализ и создание задач в CRM
- **ATS2 Poller** — автоматический сбор звонков из АТС Теле2, транскрипция, AI-классификация и создание задач
- **Twenty CRM** — управление задачами, автоматическое определение категории и важности
- **AI-классификация** — анализ обращений через OpenRouter (Claude Sonnet 4.6), подбор категории и важности из справочников CRM

---

## Архитектура

```
Telegram Bot ──► FastAPI Backend ──► Twenty CRM (24ondoc.ru)
                      │
ATS2 Poller ──────────┤
(Теле2 звонки)        │
                      ├──► PostgreSQL + pgvector
                      ├──► Redis (FSM, кеш)
                      ├──► Groq Whisper (транскрипция)
                      └──► OpenRouter AI (классификация)
```

---

## Сервисы (Docker Compose)

| Сервис | Образ | Назначение |
|--------|-------|------------|
| **nginx** | nginx:1.25-alpine | Reverse proxy, SSL (Let's Encrypt) |
| **certbot** | certbot/certbot | Автообновление SSL-сертификатов |
| **backend** | custom (Python 3.11) | FastAPI, Telegram webhook, ATS2 poller |
| **postgres** | pgvector/pgvector:pg15 | БД (PostgreSQL 15 + pgvector) |
| **redis** | redis:7-alpine | FSM-состояния, кеш, poll timestamp |
| **pgadmin** | dpage/pgadmin4 | Web UI для PostgreSQL |
| **twenty** | twentycrm/twenty | CRM-система (24ondoc.ru) |
| **minio** | minio/minio | Object storage (голосовые семплы) |
| **pg-backup** | postgres-backup-local:15 | Ежедневные бекапы БД |
| **whisper** | openai-whisper-asr | Self-hosted Whisper STT (опционально, профиль `whisper`) |

---

## Сервер

- **IP:** 89.124.95.91
- **Хостинг:** VDSina
- **Hostname:** v722264.hosted-by-vdsina.com
- **ОС:** Ubuntu (kernel 6.8.0)
- **Проект на сервере:** /app/24ondoc
- **Репозиторий:** git@github.com:Rapid-dev-oleg/24ondoc.git

---

## Внешние сервисы и ключи

### OpenRouter (AI-классификация)
- **Ключ:** `OPENROUTER_API_KEY`
- **Primary модель:** `anthropic/claude-sonnet-4.6`
- **Fallback модель:** `openrouter/free`
- **Используется для:** классификация обращений, подбор категории/важности из справочников Twenty

### Groq (транскрипция голоса)
- **Ключ:** `GROQ_API_KEY`
- **Модель:** `whisper-large-v3-turbo`
- **Используется для:** транскрипция голосовых из Telegram и звонков ATS2

### Telegram Bot
- **Токен:** `TELEGRAM_BOT_TOKEN`
- **Username:** `TELEGRAM_BOT_USERNAME`
- **Webhook:** `https://24ondoc.ru/webhook/telegram`
- **Secret:** `TELEGRAM_WEBHOOK_SECRET`

### Twenty CRM
- **URL:** https://24ondoc.ru
- **API ключ:** `TWENTY_API_KEY`
- **Кастомные поля задач:**
  - `kategoriya` (SELECT) — 15 категорий (1С, ЕГАИС, ККТ, Эквайринг и др.)
  - `vazhnost` (SELECT) — Критично, Высокая, Средняя, Низкая

### ATS2 (Теле2 — звонки)
- **API:** `https://ats2.t2.ru/crm/openapi`
- **Access Token:** `ATS2_ACCESS_TOKEN` (JWT, обновляется автоматически)
- **Refresh Token:** `ATS2_REFRESH_TOKEN` (JWT, обновляется автоматически)
- **Прокси:** `ATS2_PROXY_URL` (http://login:pass@ip:port)
- **Интервал опроса:** `ATS2_POLL_INTERVAL_SEC` (default: 60)
- **Включён:** `ATS2_ENABLED` (true/false)
- Токены сохраняются в `.env` автоматически после refresh
- Можно обновить вручную через Telegram: `/ats2_access_token`, `/ats2_refresh_token`

### OpenAI (fallback транскрипция)
- **Ключ:** `OPENAI_API_KEY`
- **Используется:** последний fallback для Whisper STT

### MinIO (Object Storage)
- **Endpoint:** `MINIO_ENDPOINT` (minio:9000)
- **Ключи:** `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`
- **Bucket:** voice-samples

---

## Переменные окружения (.env)

### Обязательные
```
DATABASE_URL=postgresql+asyncpg://user:pass@postgres/db
POSTGRES_USER=
POSTGRES_PASSWORD=
POSTGRES_DB=
REDIS_URL=redis://redis:6379/1
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
OPENROUTER_API_KEY=
T2_WEBHOOK_SECRET=
ADMIN_JWT_SECRET=
ADMIN_PASSWORD=
TWENTY_API_KEY=
TWENTY_APP_SECRET=
```

### Опциональные
```
GROQ_API_KEY=                  # Groq Whisper (primary STT)
OPENAI_API_KEY=                # OpenAI Whisper (fallback STT)
ATS2_ACCESS_TOKEN=             # ATS2 Теле2
ATS2_REFRESH_TOKEN=            # ATS2 Теле2
ATS2_PROXY_URL=                # Прокси для ATS2
ATS2_ENABLED=false             # Включить ATS2 poller
ATS2_POLL_INTERVAL_SEC=60      # Интервал опроса
MINIO_ACCESS_KEY=              # MinIO
MINIO_SECRET_KEY=              # MinIO
TELEGRAM_BOT_USERNAME=         # Для invite-ссылок
TELEGRAM_WEBHOOK_BASE_URL=https://24ondoc.ru
LOG_LEVEL=INFO
```

---

## Команды Telegram-бота

### Для всех пользователей
| Команда | Описание |
|---------|----------|
| `/start` | Начать работу / регистрация по invite-ссылке |
| `/new_task` | Создать задачу (текст, голос, фото, файл → AI-анализ → CRM) |
| `/my_tasks` | Список моих задач |
| `/settings` | Настройки профиля (имя, email, голосовой семпл) |

### Только для администраторов
| Команда | Описание |
|---------|----------|
| `/add_member` | Добавить участника (invite-ссылка) |
| `/add_admin` | Добавить администратора (invite-ссылка) |
| `/operators` | Привязка оператора к Twenty |
| `/health` | Статус сервисов + синхронизация ATS2 звонков |
| `/logs` | 10 последних заявок со статусами |
| `/ats2_access_token` | Обновить ATS2 access token |
| `/ats2_refresh_token` | Обновить ATS2 refresh token |

---

## Потоки создания задач

### Telegram → CRM
1. Пользователь отправляет текст/голос/фото/файл
2. Нажимает "Отправить"
3. AI классифицирует обращение (Claude Sonnet 4.6)
4. AI подбирает категорию и важность из справочников Twenty
5. Пользователь видит превью (заголовок, описание, категория, важность)
6. Нажимает "Создать задачу" → задача в Twenty CRM

### ATS2 звонки → CRM
1. ATS2 Poller опрашивает API Теле2 каждые 60 сек
2. Новый звонок → транскрипция (ATS2 STT → fallback Groq Whisper)
3. AI классифицирует транскрипцию
4. AI подбирает категорию и важность
5. Задача создаётся в Twenty CRM автоматически

---

## Деплой

```bash
cd /app/24ondoc
git fetch origin && git reset --hard origin/main
docker compose up -d --build --force-recreate backend
```
