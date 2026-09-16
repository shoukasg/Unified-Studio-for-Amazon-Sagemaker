# Cross-Domain Data Product Sync: build and test entry points.

PYTHON ?= python3
LAMBDA_DIR := lambda
BUILD_DIR := build
FUNCTIONS := glue_table_sync catalog_sync_mirror

.PHONY: help install test lint package clean verify

help:
	@echo "Targets:"
	@echo "  install   Install dev dependencies"
	@echo "  test      Run unit tests"
	@echo "  lint      Byte-compile all Lambda sources"
	@echo "  package   Build Lambda deployment zips into $(BUILD_DIR)/"
	@echo "  verify    Lint plus tests"
	@echo "  clean     Remove build artifacts"

install:
	$(PYTHON) -m pip install -r requirements-dev.txt

lint:
	$(PYTHON) -m py_compile $(LAMBDA_DIR)/*.py

test:
	$(PYTHON) -m pytest -q

verify: lint test

package: clean
	@mkdir -p $(BUILD_DIR)
	@for fn in $(FUNCTIONS); do \
		echo "Packaging $$fn"; \
		(cd $(LAMBDA_DIR) && zip -q -j ../$(BUILD_DIR)/$$fn.zip $$fn.py); \
	done
	@echo "Artifacts written to $(BUILD_DIR)/"

clean:
	rm -rf $(BUILD_DIR)
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
