##############################
# General Targets
##############################
.PHONY: check fix test clean

check:
	ruff check --fix
	pre-commit run --all-files
	uv run ty check

fix: check

test:
	uv run pytest tests/

clean:
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name "*.pyo" -delete 2>/dev/null || true
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
