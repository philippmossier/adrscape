.PHONY: setup up down check fmt health

setup:  ## install deps into .venv, create .env if missing
	uv sync
	test -f .env || cp .env.example .env

up:     ## start Postgres+pgvector and Neo4j in the background
	podman compose up -d

down:   ## stop containers (volumes kept)
	podman compose down

check:  ## lint, format check, typecheck, tests
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright
	uv run pytest

fmt:    ## autofix lint + format
	uv run ruff check --fix .
	uv run ruff format .

health: ## check DB connectivity
	uv run adrscape health
