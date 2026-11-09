default:
    @just --list

sync:
    uv sync

fmt:
    uv run ruff format src tests

lint:
    uv run ruff check src tests

test:
    uv run pytest -q

check: fmt lint test
