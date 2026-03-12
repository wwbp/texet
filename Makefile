.PHONY: help start down reset check test qa-required migration migrate smoke smoke-moderation api-key lint fix type audit

help:
	@awk 'BEGIN{FS=":.*##"} /^[a-zA-Z_-]+:.*##/{printf "%-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

start: ## Build and start the stack
	docker compose up --build -d

down: ## Stop the stack (keep volumes)
	docker compose down

reset: ## Stop the stack and remove volumes
	docker compose down -v

check: ## Ensure the database container is running
	@services="$$(docker compose ps --status running --services)"; \
	echo "$$services" | grep -qx "db" || { \
		echo "Database not running. Run 'make start' first."; \
		exit 1; \
	}

test: check ## Run tests with coverage
	docker compose run --rm --build api uv run pytest --cov

lint: ## Lint (no fixes)
	docker compose run --rm --build api uv run ruff check .

fix: ## Auto-fix lint and format
	docker compose run --rm --build -v $(CURDIR):/app api uv run ruff check --fix .
	docker compose run --rm --build -v $(CURDIR):/app api uv run ruff format .

type: ## Type check
	docker compose run --rm --build api uv run mypy

audit: ## Dependency audit
	docker compose run --rm --build api uv run pip-audit

migration: check ## Create a new migration (name=...)
	@if [ -z "$(name)" ]; then echo "Usage: make migration name=..."; exit 1; fi
	docker compose run --rm --build -v $(CURDIR):/app api alembic revision --autogenerate -m "$(name)"

migrate: check ## Apply migrations
	docker compose run --rm --build -v $(CURDIR):/app api alembic upgrade head

smoke: check ## Run end-to-end smoke test
	bash scripts/e2e_smoke.sh

smoke-moderation: ## Run live moderation smoke test (requires OPENAI_API_KEY)
	docker compose run --rm --build api env PYTHONPATH=/app uv run python scripts/moderation_smoke.py

api-key: check ## Create an API key
	docker compose run --rm --build api uv run python -m app.auth.cli
