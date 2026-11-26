# Short names for the things we actually run.
#
# search-proxy keeps its two in package.json because it deploys with wrangler.
# This is a Python service, so they live here instead - same idea, one word to
# type and no arguments to remember wrong.

VENV := .venv/bin
PY   := $(VENV)/python

.PHONY: help install hooks lock ui talk test audio live e2e check audit fmt mic image alerts greeting profile guided-voice neutrality-scan

# The exception list for the dev lockfile scan, turned into flags. Comments
# and blank lines dropped; see .pip-audit-ignore for what is in it and why.
#
# Expanded here and only here. Both pipelines call `make audit` rather than
# repeating these commands, because an ignore list written out in three
# places is an ignore list that is correct in one of them.
AUDIT_IGNORE := $(shell awk '!/^#/ && NF {printf "--ignore-vuln %s ", $$1}' .pip-audit-ignore)

# Where the audit tools live. Local runs use the venv; CI installs into the
# system python and overrides this with an empty `make audit AUDIT_BIN=`,
# which is what lets one target serve both without a second copy of it.
AUDIT_BIN ?= $(VENV)/

help:                    ## show this list
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/|/' | column -t -s'|'

install:                 ## create the venv and install the locked versions
	python3 -m venv .venv
	$(VENV)/pip install -q --upgrade pip
	# --require-hashes: every artefact must match the hash that was reviewed,
	# or nothing installs. Both files in one command, which is why the dev
	# lockfile is constrained against the runtime one.
	$(VENV)/pip install -q --require-hashes -r requirements.txt -r requirements-dev.txt
	@echo "done. copy .env.example to .env and add your key."
	@echo "then run: make hooks"

hooks:                   ## install the pre-commit hooks into .git/hooks
	# Run once per clone. Without it .pre-commit-config.yaml is a file that
	# does nothing, which is the usual way a repo ends up with hooks it
	# believes are running. `pre-commit run -a` checks everything by hand.
	$(VENV)/pre-commit install --install-hooks
	$(VENV)/pre-commit install --hook-type pre-push

lock:                    ## recompile the lockfiles after editing a .in file
	# requirements.in is the intent; requirements.txt is what installs. Edit
	# the first, run this, review the diff - a new transitive dependency then
	# arrives as a line somebody approves rather than as whatever the index
	# served that morning. --allow-unsafe pins pip and setuptools too, which
	# --require-hashes needs and the name has long stopped meaning unsafe.
	$(VENV)/pip-compile --generate-hashes --strip-extras --allow-unsafe \
	  --quiet --output-file=requirements.txt requirements.in
	$(VENV)/pip-compile --generate-hashes --strip-extras --allow-unsafe \
	  --quiet --output-file=requirements-dev.txt requirements-dev.in

ui:                      ## browser UI at http://localhost:8080 (costs money)
	$(VENV)/uvicorn --factory src.web.app:create_app --host 127.0.0.1 --port 8080 --reload --reload-dir src

talk:                    ## talk to the agent in the terminal (costs money)
	$(PY) -m src.tools.talk --seconds 180

test:                    ## the fast tests - no API, no devices, no money
	$(PY) -m pytest

audio:                   ## device tests - needs a microphone and speaker
	$(PY) -m pytest -m audio

e2e:                     ## widget browser tests - Chromium and WebKit
	$(PY) -m playwright install chromium webkit
	$(PY) -m pytest -m e2e --browser chromium --browser webkit --override-ini="addopts=-q --strict-markers"

live:                    ## real Gemini Live calls - spends a few cents
	$(PY) -m pytest -m live

neutrality-scan:         ## retailer deny-list and widget media APIs
	$(PY) -m src.tools.neutrality_scan

check:                   ## everything CI runs, in the same order
	$(VENV)/ruff check src tests
	$(VENV)/ruff format --check src tests
	$(VENV)/mypy src          # strict; see mypy.ini
	$(PY) -m pytest
	$(VENV)/detect-secrets scan --baseline .secrets.baseline
	$(MAKE) audit             # needs the network; see below

audit:                   ## known holes: CVEs in what we install, bugs in what we wrote
	# The only step here that reaches the network - pip-audit asks the PyPI
	# advisory database. Offline it fails, which is correct: an audit that
	# silently passes because it could not ask is worse than no audit.
	#
	# requirements.txt is what the image installs and what ships. Strict,
	# with no ignore list at all: a finding here blocks the release.
	$(AUDIT_BIN)pip-audit --strict --requirement requirements.txt
	# The dev lockfile is tooling and never enters the image, so the
	# documented exceptions in .pip-audit-ignore apply here - and nowhere
	# else, and nothing beyond what that file lists by id.
	$(AUDIT_BIN)pip-audit --strict --requirement requirements-dev.txt $(AUDIT_IGNORE)
	# The other half of the question: holes we wrote, rather than installed.
	$(AUDIT_BIN)bandit -c bandit.yaml -q -r src

fmt:                     ## reformat in place
	$(VENV)/ruff format src tests
	$(VENV)/ruff check --fix src tests

# The estate (stock, price, offers, EMI) is the Search-proxy Worker's product
# service run in-process. The image cannot reach a sibling checkout, so the
# four files it imports are copied in here and committed, with the commit
# they came from in vendor/estate/SOURCE.
ESTATE_SOURCE ?= ../../Search-proxy/products-search-engine
vendor-estate:           ## refresh vendor/estate from the Search-proxy checkout
	mkdir -p vendor/estate/src/worker vendor/estate/src/tools
	cp $(ESTATE_SOURCE)/src/worker/product.py $(ESTATE_SOURCE)/src/worker/apis.py $(ESTATE_SOURCE)/src/worker/adapters.py vendor/estate/src/worker/
	cp $(ESTATE_SOURCE)/src/tools/worker_shim.py vendor/estate/src/tools/
	echo "search-proxy $$(git -C $(ESTATE_SOURCE) rev-parse HEAD) copied $$(date -u +%F)" > vendor/estate/SOURCE

image:                   ## build the container and check it answers /healthz
	docker build --build-arg APP_RELEASE=$$(git rev-parse --short HEAD) \
	  -t shopping-assistant:local .
	docker run -d --rm --name shopping-assistant-check -p 8081:8080 \
	  -e GEMINI_API_KEY=not-a-real-key shopping-assistant:local
	@for i in $$(seq 1 30); do \
	  curl -fs http://127.0.0.1:8081/healthz && break || sleep 1; \
	done; echo
	docker rm -f shopping-assistant-check

greeting:                ## record the opening line once - costs a few paise
	# Run after changing GREETING in persona.py, the Live model, or the
	# playback sample rate. Every call then plays this instead of paying a
	# generation to read out a sentence that cannot vary. Deleting
	# src/agent/greeting.pcm goes back to a generated greeting.
	$(PY) -m src.tools.record_greeting
	# "Oru nimisham, paarkiren", played while a tool round trip is in flight.
	# Two searches back to back is six seconds of dead line, and a caller who
	# hears nothing speaks again - which reads as an interruption and flushes
	# the answer they were waiting for. Measured on the calls of 2026-11-16.
	$(PY) -m src.tools.record_greeting --holding

guided-voice:            ## record the guided call's fixed lines - a few rupees, once
	# Every question the guided call asks is fixed text, so it is recorded
	# and played rather than generated. Run after editing a sentence in
	# src/guided/script.py - only the lines that changed are recorded again -
	# and with ARGS=--all after changing VOICE_NAME. Each take is played back
	# to the text model and refused if it is not the line that was written.
	$(PY) -m src.tools.record_guided $(ARGS)

profile:                 ## what one call costs in CPU - no network, no money
	$(PY) -m src.tools.profile_call --seconds 180 --profile

prompt:                  ## size of the standing prompt, section by section - free
	$(PY) -m src.tools.count_prompt

estate:                  ## which per-item services are reachable (ARGS=--live to call them)
	$(PY) -m src.tools.check_estate $(ARGS)

alerts:                  ## regenerate deploy/alerts.yml from the rule definitions
	# The thresholds live in src/observability/alerts.py, beside the reason
	# each one exists. Generated rather than written so the rule that fires
	# cannot drift from the one that was reviewed - a test fails if the
	# committed file is stale.
	$(PY) -c "from src.observability.alerts import rules_yaml; \
	  open('deploy/alerts.yml','w').write(rules_yaml())"
	@echo "wrote deploy/alerts.yml"

mic:                     ## switch capture to the built-in microphone array
	@pactl set-default-source alsa_input.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__hw_sofhdadsp_6__source
	@echo "default source is now: $$(pactl get-default-source)"
