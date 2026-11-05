# Common tasks. Run `make help` to list them.
#
# Override the interpreter if python3.15 is not on your PATH, e.g.
#   make setup PYTHON=/opt/homebrew/bin/python3.15

PYTHON ?= python3.15
VENV   := .venv
BIN    := $(VENV)/bin
STAMP  := $(VENV)/.installed

PIP_COMPILE := CUSTOM_COMPILE_COMMAND="make lock" $(BIN)/pip-compile --quiet \
	--allow-unsafe --generate-hashes --strip-extras

.DEFAULT_GOAL := help
.PHONY: help setup run test lock agent

help: ## List available targets
	@grep -E '^[a-z][a-zA-Z_-]*:.*## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "} {printf "  make %-8s %s\n", $$1, $$2}'

setup: $(STAMP) ## Create .venv (Python 3.15) and install the locked runtime + dev deps

# Reinstall whenever a lock file changes.
$(STAMP): requirements.txt requirements-dev.txt
	@test -x $(BIN)/python || $(PYTHON) -m venv $(VENV)
	@$(BIN)/python -c 'import sys; assert sys.version_info[:2] == (3, 15), \
		f"$(VENV) uses Python {sys.version.split()[0]}, need 3.15: delete $(VENV) and re-run make setup"'
	$(BIN)/python -m pip install --require-hashes -r requirements.txt -r requirements-dev.txt
	@touch $@

run: $(STAMP) ## Run the app on http://localhost:8080
	$(BIN)/python app.py

test: $(STAMP) ## Run the test suite (pass pytest options with ARGS="-k search -x")
	$(BIN)/python -m pytest $(ARGS)

# TASK, BUDGET_USD, TIMEOUT_MIN, WORKTREE and DRY_RUN given on the make command line reach
# the script through the environment, so the task text needs no extra quoting here.
agent: ## Headless Claude task with spend + time caps: make agent TASK="..." [BUDGET_USD=2 TIMEOUT_MIN=20 DRY_RUN=1]
	@scripts/agent.sh

lock: $(STAMP) ## Regenerate requirements*.txt from requirements*.in (add UPGRADE=1 to bump versions)
	$(PIP_COMPILE) $(if $(UPGRADE),--upgrade) -o requirements.txt requirements.in
	$(PIP_COMPILE) $(if $(UPGRADE),--upgrade) -o requirements-dev.txt requirements-dev.in
