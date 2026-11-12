# Short names for the things we actually run.
#
# search-proxy keeps its two in package.json because it deploys with wrangler.
# This is a Python service, so they live here instead - same idea, one word to
# type and no arguments to remember wrong.

VENV := .venv/bin
PY   := $(VENV)/python

.PHONY: help install lock ui talk test audio live check fmt mic image alerts

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

live:                    ## real Gemini Live calls - spends a few cents
	$(PY) -m pytest -m live

check:                   ## everything CI runs, in the same order
	$(VENV)/ruff check src tests
	$(VENV)/ruff format --check src tests
	$(VENV)/mypy src          # strict; see mypy.ini
	$(PY) -m pytest
	$(VENV)/detect-secrets scan --baseline .secrets.baseline

fmt:                     ## reformat in place
	$(VENV)/ruff format src tests
	$(VENV)/ruff check --fix src tests

image:                   ## build the container and check it answers /healthz
	docker build --build-arg APP_RELEASE=$$(git rev-parse --short HEAD) \
	  -t poorvika-voice:local .
	docker run -d --rm --name poorvika-voice-check -p 8081:8080 \
	  -e GEMINI_API_KEY=not-a-real-key poorvika-voice:local
	@for i in $$(seq 1 30); do \
	  curl -fs http://127.0.0.1:8081/healthz && break || sleep 1; \
	done; echo
	docker rm -f poorvika-voice-check

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
