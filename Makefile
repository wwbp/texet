.PHONY: start stop test clean check migration migrate smoke api-key

start:
	docker compose up --build -d

stop:
	docker compose down -v

test:
	$(MAKE) check
	docker compose run --rm --build api uv run pytest --cov

clean:
	docker compose run --rm --build api uv run ruff check .
	docker compose run --rm --build api uv run ruff format .
	docker compose run --rm --build api uv run mypy
	docker compose run --rm --build api uv run pip-audit

migration:
	$(MAKE) check
	@if [ -z "$(name)" ]; then echo "Usage: make migration name=..."; exit 1; fi
	docker compose run --rm --build -v $(CURDIR):/app api alembic revision --autogenerate -m "$(name)"

migrate:
	$(MAKE) check
	docker compose run --rm --build -v $(CURDIR):/app api alembic upgrade head

smoke:
	bash scripts/e2e_smoke.sh

api-key:
	$(MAKE) check
	docker compose run --rm --build api uv run python -m app.auth.cli

check:
	@services="$$(docker compose ps --status running --services)"; \
	echo "$$services" | grep -qx "db" || { \
		echo "Database not running. Run 'make start' or 'docker compose up -d db' first."; \
		exit 1; \
	}
