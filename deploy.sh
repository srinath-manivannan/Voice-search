#!/bin/bash
# Mirrors search-proxy/deploy.sh: same environment whitelist, same early check
# that the secrets actually arrived in the job, same three environment names.
#
# What differs is the target. search-proxy deploys a Cloudflare Worker with
# wrangler. A voice session holds a socket open for minutes, which Workers do
# not express, so this ships a container instead - see docs/PIPELINE.md.
#
# Every tag is immutable and names the commit it was built from. That is the
# whole rollback story: there is no :latest to be ambiguous about, the tag
# that was live last week still exists and still runs, and docs/ROLLBACK.md
# says who decides and what to type.
set -euo pipefail

ENV=${1:-development}

case $ENV in
  development|stage|main) ;;
  *) echo "Unknown environment: $ENV (expected development, stage or main)"; exit 1 ;;
esac

for v in GEMINI_API_KEY REGISTRY_URL; do
  if [ -z "${!v:-}" ]; then
    echo "ERROR: $v is empty in this job (branch: ${CI_COMMIT_REF_NAME:-local}, environment: ${CI_ENVIRONMENT_NAME:-none})."
    echo "Check the GitLab variable: name, 'Protected' flag vs this branch, and its Environment scope."
    exit 1
  fi
done

# The full sha, not the short one, so the tag cannot collide and so the thing
# running can always be traced back to a commit that still exists.
RELEASE="${CI_COMMIT_SHA:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
SHORT="${CI_COMMIT_SHORT_SHA:-${RELEASE:0:8}}"
TAG="${REGISTRY_URL}/poorvika-voice:${ENV}-${SHORT}"

# What a rollback would go back to. Printed BEFORE the build, because after a
# bad deploy the useful question is "what was it before?" and nobody wants to
# be reading git history to answer it under pressure.
PREVIOUS="$(git rev-parse --short "HEAD~1" 2>/dev/null || echo unknown)"
echo "Deploying   $ENV"
echo "  release   $SHORT"
echo "  tag       $TAG"
echo "  previous  ${REGISTRY_URL}/poorvika-voice:${ENV}-${PREVIOUS}"
echo "  rollback  docs/ROLLBACK.md"
echo

docker build --build-arg "APP_RELEASE=${SHORT}" -t "$TAG" .

# Confirm the image reports the release it was built with. A tag that says one
# thing while the process inside says another makes every later rollback a
# guess, and this catches it here rather than at three in the morning.
BUILT="$(docker run --rm --entrypoint printenv "$TAG" APP_RELEASE)"
if [ "$BUILT" != "$SHORT" ]; then
  echo "ERROR: image reports APP_RELEASE=$BUILT but was tagged $SHORT"
  exit 1
fi
echo "Built $TAG and it reports release $BUILT."

# The runtime host is a Phase 2 decision - it depends on which telephony
# provider we sign with and which Indian region they terminate in. Until that
# is settled this stops here rather than guessing at a target and half-working.
echo "Push and run target not configured yet - Phase 2, once telephony is chosen."
exit 0
