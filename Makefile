.PHONY: lint test test-parallel

PYTHON ?= .venv/bin/python
JOBS ?= 8

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

# Focused run while working: make test TESTS="tests/test_x.py tests/test_y.py"
test:
	@test -n "$(TESTS)" || { echo 'set TESTS=...; use make test-parallel for the full suite'; exit 2; }
	$(PYTHON) -m pytest -x -q -p no:cacheprovider $(TESTS)

# Full default suite once per branch, as concurrent file shards (no pytest-xdist).
test-parallel:
	$(PYTHON) dev/pytest_parallel.py --jobs $(JOBS)
