# Short names for the things we actually run.
#
# search-proxy keeps its two in package.json because it deploys with wrangler.
# This is a Python service, so they live here instead - same idea, one word to
# type and no arguments to remember wrong.

VENV := .venv/bin
PY   := $(VENV)/python

.PHONY: help install ui talk test audio live check fmt mic

help:                    ## show this list
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/|/' | column -t -s'|'

install:                 ## create the venv and install everything
	python3 -m venv .venv
	$(VENV)/pip install -q -r requirements.txt -r requirements-dev.txt
	@echo "done. copy .env.example to .env and add your key."

ui:                      ## browser UI at http://localhost:8080 (costs money)
	$(VENV)/uvicorn --factory src.web.server:create_app --host 127.0.0.1 --port 8080 --reload

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
	$(VENV)/mypy src
	$(PY) -m pytest
	$(VENV)/detect-secrets scan --baseline .secrets.baseline

fmt:                     ## reformat in place
	$(VENV)/ruff format src tests
	$(VENV)/ruff check --fix src tests

mic:                     ## switch capture to the built-in microphone array
	@pactl set-default-source alsa_input.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__hw_sofhdadsp_6__source
	@echo "default source is now: $$(pactl get-default-source)"
