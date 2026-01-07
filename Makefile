.PHONY: start stop test clean check migration migrate

start:
	docker compose up --build -d

stop:
	docker compose down -v

test:
	$(MAKE) check
	docker compose run --rm --build api uv run pytest --cov

clean:
	$(MAKE) check
	docker compose run --rm --build api uv run ruff check .
	docker compose run --rm --build api uv run ruff format .
	docker compose run --rm --build api uv run mypy
	docker compose run --rm --build api uv run pip-audit

migration:
	$(MAKE) check
	@if [ -z "$(name)" ]; then echo "Usage: make migration name=..."; exit 1; fi
	docker compose run --rm --build api alembic revision --autogenerate -m "$(name)"

migrate:
	$(MAKE) check
	docker compose run --rm --build api alembic upgrade head

check:
	@services="$$(docker compose ps --status running --services)"; \
	echo "$$services" | grep -qx "api" && echo "$$services" | grep -qx "db" || { \
		echo "Services not running. Run 'make start' first."; \
		exit 1; \
	}
