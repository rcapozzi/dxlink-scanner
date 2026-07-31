# Scanner Makefile

.PHONY: install sync test lint format typecheck run clean help

## install: Install dependencies
install:
	uv sync

## sync: Sync dependencies
sync: install

## test: Run tests with coverage
test:
	uv run pytest tests/ -v

## lint: Run ruff linter
lint:
	uv run ruff check src/ tests/

## format: Format code with ruff
format:
	uv run ruff format src/ tests/

## format-check: Check formatting without modifying
format-check:
	uv run ruff format --check src/ tests/

## typecheck: Run mypy type checker
typecheck:
	uv run mypy src/

## run: Start the scanner
run:
	uv run python -m scanner --config scanner.yaml --verbose

## clean: Remove build artifacts
clean:
	rm -rf .venv __pycache__ .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov

## help: Show this help
help:
	@echo "Available targets:"
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/## //' | column -t -s ':'
