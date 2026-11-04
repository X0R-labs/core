.PHONY: install dev test lint seed serve smoke docker clean

install:
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

dev:
	.venv/bin/x0r serve --reload

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/ruff check src tests

seed:
	.venv/bin/x0r seed

serve:
	.venv/bin/x0r serve

smoke:
	.venv/bin/x0r smoke

docker:
	docker compose up --build

clean:
	rm -rf .venv build dist *.egg-info .pytest_cache .ruff_cache x0r.db
	find . -name "__pycache__" -type d -exec rm -rf {} +
