.PHONY: help start down reset check test qa-required migration migrate smoke smoke-moderation api-key lint fix type audit load load-perf worker-logs

help:
	@awk 'BEGIN{FS=":.*##"} /^[a-zA-Z_-]+:.*##/{printf "%-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ── Stack ──────────────────────────────────────────────────────────────────

start: ## Build and start the stack
	docker compose up --build -d

down: ## Stop the stack (keep volumes)
	docker compose down

worker-logs: ## Follow the reply worker logs
	docker compose logs -f worker

reset: ## Stop the stack and remove volumes
	docker compose down -v

check: ## Ensure the database container is running
	@services="$$(docker compose ps --status running --services)"; \
	echo "$$services" | grep -qx "db" || { \
		echo "Database not running. Run 'make start' first."; \
		exit 1; \
	}

# ── Development ────────────────────────────────────────────────────────────

migration: check ## Create a new migration (name=...)
	@if [ -z "$(name)" ]; then echo "Usage: make migration name=..."; exit 1; fi
	docker compose run --rm --build -v $(CURDIR):/app api alembic revision --autogenerate -m "$(name)"

migrate: check ## Apply pending migrations
	docker compose run --rm --build -v $(CURDIR):/app api alembic upgrade head

api-key: check ## Create an API key
	docker compose run --rm --build api uv run python -m app.auth.cli

# ── Quality ────────────────────────────────────────────────────────────────

lint: ## Lint (no fixes)
	docker compose run --rm --build api uv run ruff check .

fix: ## Auto-fix lint and format
	docker compose run --rm --build -v $(CURDIR):/app api sh -c 'uv run ruff check --fix . && uv run ruff format .'

type: ## Type check
	docker compose run --rm --build api uv run mypy

audit: ## Dependency security audit
	docker compose run --rm --build api uv run pip-audit

qa-required: lint type audit ## Run all required quality checks (lint, types, security)

# ── Testing ────────────────────────────────────────────────────────────────

test: check ## Run tests with coverage
	docker compose run --rm --build -v $(CURDIR):/app api uv run pytest --cov

smoke: check ## End-to-end smoke test (also runs moderation smoke at the end)
	bash scripts/e2e_smoke.sh

smoke-moderation: ## Live moderation smoke test only (requires OPENAI_API_KEY)
	docker compose run --rm --build api env PYTHONPATH=/app uv run python scripts/moderation_smoke.py

load: ## Locust load test UI (HOST=http://localhost:8000, requires TEXET_API_KEY)
	uv run locust --host $${HOST:-http://localhost:8000}

load-hub: ## Hub load test UI (HOST=..., requires HUB_API_KEY and DATABASE_URL_STAGING)
	TEST_MODE=true uv run locust -f locust_hub.py --host $${HOST:?HOST is required}

load-perf: ## Headless perf run: USERS=500 @ ~100 rps for DURATION=5m (requires TEXET_API_KEY; api must run with MOCK_EXTERNAL_APIS=true)
	@mkdir -p perf-results
	uv run locust --host $${HOST:-http://localhost:8000} --headless \
		--users $${USERS:-500} --spawn-rate $${SPAWN_RATE:-25} --run-time $${DURATION:-5m} \
		--csv perf-results/run --csv-full-history --html perf-results/run.html
