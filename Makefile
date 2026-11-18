.PHONY: install seed test demo-auth run web web-dev lint check record-fixtures auth-fixtures clean

install:
	uv sync
	cd web && npm ci

# Load the recorded fixtures into var/freefood.sqlite3 through the real sync pipeline (offline).
seed:
	uv run python -m app.seed

test:
	uv run pytest

# Validate YALECONNECT_COOKIE / YALECONNECT_ICS_FEEDS, run one full sync, print anonymous vs authenticated counts.
demo-auth:
	uv run python -m app.auth demo

run:
	uv run app

web:
	cd web && npm install && npm run build

web-dev:
	cd web && npm run dev

lint:
	uv run ruff check app tests scripts

# Operator-only: re-record live responses (touches yaleconnect.yale.edu) and rebuild the auth fixtures.
record-fixtures:
	uv run python scripts/record_fixtures.py --out tests/data/fixtures
	uv run python scripts/build_auth_fixtures.py

auth-fixtures:
	uv run python scripts/build_auth_fixtures.py

clean:
	rm -rf var .pytest_cache .ruff_cache web/dist
