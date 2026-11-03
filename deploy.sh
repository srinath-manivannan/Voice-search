#!/bin/bash
# Mirrors search-proxy/deploy.sh: same environment whitelist, same early check
# that the secrets actually arrived in the job, same three environment names.
#
# What differs is the target. search-proxy deploys a Cloudflare Worker with
# wrangler. A voice session holds a socket open for minutes, which Workers do
# not express, so this ships a container instead - see docs/ADR-001.
set -e

ENV=${1:-development}

case $ENV in
  development|stage|main) ;;
  *) echo "Unknown environment: $ENV (expected development, stage or main)"; exit 1 ;;
esac

for v in GEMINI_API_KEY REGISTRY_URL; do
  if [ -z "${!v}" ]; then
    echo "ERROR: $v is empty in this job (branch: ${CI_COMMIT_REF_NAME:-local}, environment: ${CI_ENVIRONMENT_NAME:-none})."
    echo "Check the GitLab variable: name, 'Protected' flag vs this branch, and its Environment scope."
    exit 1
  fi
done

TAG="${REGISTRY_URL}/poorvika-voice:${ENV}-${CI_COMMIT_SHORT_SHA:-local}"
echo "Building $TAG"
docker build -t "$TAG" .

# The runtime host is a Phase 2 decision - it depends on which telephony
# provider we sign with and which Indian region they terminate in. Until that
# is settled this stops here rather than guessing at a target and half-working.
echo "Built $TAG."
echo "Push and run target not configured yet - Phase 2, once telephony is chosen."
exit 0
