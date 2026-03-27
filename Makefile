.PHONY: up down build test lint format logs ps health

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
