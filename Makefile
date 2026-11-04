# Developer entry points. CI runs the same targets.
SHELL := /bin/bash
PY ?= python3
STACK ?= acme-notes-triage-dev
CONFIG_ENV ?= default
TEMPLATES := template.yaml stacks/*.yaml

.PHONY: install lint fmt validate test test-unit test-integration build deploy teardown samples smoke

install:
	$(PY) -m pip install -r requirements-dev.txt

lint:
	ruff check .
	ruff format --check .
	cfn-lint $(TEMPLATES)
	$(PY) -m json.tool statemachine/triage.asl.json > /dev/null

fmt:
	ruff format .
	ruff check --fix .

validate:
	sam validate --lint

test:
	$(PY) -m pytest --cov=layers/common/acme_common --cov=functions --cov-report=term-missing

test-unit:
	$(PY) -m pytest tests/unit

test-integration:
	$(PY) -m pytest tests/integration

build:
	sam build

## One-command deploy (uses samconfig.toml; LlmProvider=fake needs no secrets)
deploy: build
	sam deploy --config-env $(CONFIG_ENV)

## One-command teardown. The archive bucket must be emptied first (versioned objects included).
teardown:
	@bucket=$$(aws cloudformation describe-stacks --stack-name $(STACK) \
	  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text 2>/dev/null); \
	if [[ -n "$$bucket" && "$$bucket" != "None" ]]; then \
	  echo "Emptying s3://$$bucket"; \
	  $(PY) scripts/empty_bucket.py "$$bucket"; \
	fi
	sam delete --stack-name $(STACK) --no-prompts

samples:
	$(PY) scripts/send_samples.py --stack-name $(STACK) --wait

smoke:
	$(PY) scripts/send_samples.py --stack-name $(STACK) --only 001 --wait --strict
