# SAM `BuildMethod: makefile` target for the shared layer.
#
# SAM runs this from a scratch copy of layers/common/, so the repo root is located via this
# Makefile's own path. Two things the default Python builder can't do:
#   1. install manylinux *aarch64* wheels regardless of the build host's architecture;
#   2. bundle config/extraction_registry.yaml into the layer (the single source of truth).
MAKEFILE_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
REPO_ROOT    := $(abspath $(MAKEFILE_DIR)/../..)
PYTHON       ?= python3
SITE         := $(ARTIFACTS_DIR)/python

build-CommonLayer:
	mkdir -p "$(SITE)"
	$(PYTHON) -m pip install --quiet --no-cache-dir \
		--requirement "$(MAKEFILE_DIR)/requirements.txt" \
		--target "$(SITE)" \
		--platform manylinux2014_aarch64 --implementation cp --python-version 3.12 \
		--only-binary=:all: --upgrade
	# boto3/botocore come with the Lambda runtime (pulled in transitively by aws-xray-sdk);
	# shipping a second copy would add ~25 MB to every cold start for nothing.
	rm -rf "$(SITE)"/botocore* "$(SITE)"/boto3* "$(SITE)"/s3transfer* "$(SITE)/bin"
	cp -R "$(MAKEFILE_DIR)/acme_common" "$(SITE)/"
	mkdir -p "$(SITE)/acme_common/config"
	cp "$(REPO_ROOT)/config/extraction_registry.yaml" "$(SITE)/acme_common/config/"
	find "$(SITE)" -type d -name "__pycache__" -prune -exec rm -rf {} +
