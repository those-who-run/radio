# The repository's checks. `make check` is the Definition of done (AGENTS.md): it
# runs what CI runs, the commit hooks on every file (CI's `checks / checks`), the
# Python checks in python.yml's order (CI's `python / check`) and `make test` (CI's
# `test`, which calls this same target so the two can't drift).
# Needs uv, python3 and pre-commit; uv fetches the Python in .python-version, and the
# guard copy check and the first `make venv` need the network.

PY ?= python3
# The tools, as python.yml runs them: from the project's environment, which
# `make venv` installs from uv.lock.
RUN := uv run --no-sync
# The lowest total coverage the tests may reach, in per cent: the coverage-floor
# ci.yml passes to python.yml and pyproject.toml's fail_under
# (tests/test_python_floor.py keeps the three equal).
COVERAGE_FLOOR := 90

.PHONY: help
help:
	@echo "check     - everything a PR must pass: the commit hooks on every file, the Python checks, then make test (CI runs all three)"
	@echo "test      - the repository's own checks: links, the guard copy, and the tests in tests/ (CI's test job)"
	@echo "venv      - install the project and its tools from uv.lock into .venv"
	@echo "python    - the Python checks, as CI's python / check runs them: lock, format, lint, types, py-test"
	@echo "py-test   - the package's tests under coverage, with the floor; a skipped test fails"
	@echo "fmt       - format and fix the Python code with ruff"
	@echo "lock      - re-resolve uv.lock after changing pyproject.toml"
	@echo "run       - run radio locally, text logs (TWR_NATS_URL and the rest from the environment)"
	@echo "image     - build the image as ghcr.io/those-who-run/twr-radio:dev"
	@echo "hooks     - install the commit hooks; a new clone or worktree runs it once"
	@echo "links     - every internal link and anchor in the Markdown resolves (offline)"
	@echo "guard     - .claude/ holds those-who-run/.github's guard and settings at the pinned commit"

.PHONY: check
check:
	pre-commit run --all-files
	$(MAKE) python
	$(MAKE) test

.PHONY: test
test: links guard
	$(PY) -m unittest discover -s tests

.PHONY: venv
venv:
	uv sync --frozen

# python.yml's order: the lock, the install, format, lint, types, the tests.
.PHONY: python
python:
	uv lock --check
	$(MAKE) venv
	$(RUN) ruff format --check
	$(RUN) ruff check
	$(RUN) mypy
	$(MAKE) py-test

.PHONY: py-test
py-test:
	$(RUN) coverage run -m pytest -rs
	$(RUN) coverage report --fail-under=$(COVERAGE_FLOOR)

.PHONY: fmt
fmt:
	$(RUN) ruff format
	$(RUN) ruff check --fix

.PHONY: lock
lock:
	uv lock

.PHONY: run
run:
	uv run radio

.PHONY: image
image:
	podman build -t ghcr.io/those-who-run/twr-radio:dev .

.PHONY: hooks
hooks:
	pre-commit install

.PHONY: links
links:
	$(PY) scripts/docs_links.py

.PHONY: guard
guard:
	$(PY) scripts/guard_copy.py
