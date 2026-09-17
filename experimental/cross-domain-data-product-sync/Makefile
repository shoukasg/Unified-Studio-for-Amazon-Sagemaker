# Cross-Domain Data Product Sync: build and test entry points.

PYTHON ?= python3
VENV := .venv
VENV_PY := $(VENV)/bin/python
LAMBDA_DIR := lambda
BUILD_DIR := build
FUNCTIONS := glue_table_sync catalog_sync_mirror

.PHONY: help install test lint package clean verify

help:
	@echo "Targets:"
	@echo "  install   Create a local virtualenv and install dev dependencies"
	@echo "  test      Run unit tests (auto-installs deps if needed)"
	@echo "  lint      Byte-compile all Lambda sources"
	@echo "  package   Build Lambda deployment zips into $(BUILD_DIR)/"
	@echo "  verify    Lint plus tests"
	@echo "  clean     Remove build artifacts and the local virtualenv"

# Create an isolated virtualenv so tests do not depend on the system Python
# (avoids PEP 668 "externally-managed environment" errors on macOS/Homebrew).
install: $(VENV_PY)

$(VENV_PY):
	$(PYTHON) -m venv $(VENV)
	$(VENV_PY) -m pip install --quiet --upgrade pip
	$(VENV_PY) -m pip install --quiet -r requirements-dev.txt

lint:
	$(PYTHON) -m py_compile $(LAMBDA_DIR)/*.py

test: $(VENV_PY)
	$(VENV_PY) -m pytest -q

verify: lint test

package: clean
	@mkdir -p $(BUILD_DIR)
	@for fn in $(FUNCTIONS); do \
		echo "Packaging $$fn"; \
		(cd $(LAMBDA_DIR) && zip -q -j ../$(BUILD_DIR)/$$fn.zip $$fn.py); \
	done
	@echo "Artifacts written to $(BUILD_DIR)/"

clean:
	rm -rf $(BUILD_DIR) $(VENV)
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
