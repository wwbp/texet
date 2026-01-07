.PHONY: start stop test clean check

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

check:
	@services="$$(docker compose ps --status running --services)"; \
	echo "$$services" | grep -qx "api" && echo "$$services" | grep -qx "db" || { \
		echo "Services not running. Run 'make start' first."; \
		exit 1; \
	}
