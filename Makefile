.PHONY: up down build test lint format logs ps health twenty-bootstrap backfill-calls \
       staging-up staging-down staging-build staging-logs staging-ps staging-e2e staging-reset

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

test:
	cd backend && pip install -e ".[dev]" -q && pytest src/ -v

lint:
	cd backend && pip install -e ".[dev]" -q && ruff check src/ && ruff format --check src/ && mypy src/ --strict

format:
	cd backend && ruff format src/

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

health:
	docker compose ps --format "table {{.Name}}\t{{.Status}}"

# Idempotently create custom Twenty objects (Location, CallRecord, TaskLog)
# and custom fields on Task/Person. Safe to re-run. Reads TWENTY_BASE_URL
# and TWENTY_API_KEY from .env.
twenty-bootstrap:
	cd backend && uv run python -m src.twenty_integration.infrastructure.bootstrap_cli

# One-off historical sync of ats_call_records into Twenty CallRecord.
# Rate-limited to ~2 rps. Safe to re-run — idempotent by atsCallId.
# Requires TWENTY_BASE_URL, TWENTY_API_KEY, DATABASE_URL in env.
backfill-calls:
	cd backend && uv run python ../scripts/backfill_call_records.py

# --- Staging environment ---

staging-up:
	docker compose -f docker-compose.staging.yml --env-file .env.staging up -d

staging-down:
	docker compose -f docker-compose.staging.yml --env-file .env.staging down

staging-build:
	docker compose -f docker-compose.staging.yml --env-file .env.staging build

staging-logs:
	docker compose -f docker-compose.staging.yml --env-file .env.staging logs -f --tail=100

staging-ps:
	docker compose -f docker-compose.staging.yml --env-file .env.staging ps

staging-e2e:
	@echo "Running E2E tests against staging..."
	WEBHOOK_URL=http://localhost:8100 \
	CHATWOOT_BASE_URL=http://localhost:3100 \
	python scripts/e2e_test.py

staging-reset:
	docker compose -f docker-compose.staging.yml --env-file .env.staging down -v
	@echo "Staging volumes removed. Run 'make staging-up' to recreate."
