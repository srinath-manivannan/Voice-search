"""The release identifier, end to end through the files that carry it.

A rollback is only as good as the answer to "what is running?", and that
answer passes through four files that have no other reason to agree: the
Dockerfile that bakes it, deploy.sh that supplies it, the workflow that
checks it, and the config that reads it. Nothing else would notice one of
them drifting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
DEPLOY = (ROOT / "deploy.sh").read_text()
WORKFLOW = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
ROLLBACK = (ROOT / "docs" / "ROLLBACK.md").read_text()


def test_the_image_takes_a_release_and_puts_it_in_the_environment():
    assert "ARG APP_RELEASE=unknown" in DOCKERFILE
    assert "APP_RELEASE=$APP_RELEASE" in DOCKERFILE


def test_the_image_is_labelled_with_it_too():
    """So the question can be answered about an image that is not running -
    which is exactly the situation when you are choosing what to roll back
    to."""
    assert "org.opencontainers.image.revision=$APP_RELEASE" in DOCKERFILE


@pytest.mark.parametrize(
    ("name", "text"),
    [("deploy.sh", DEPLOY), ("the CI workflow", WORKFLOW)],
)
def test_every_build_supplies_the_release(name, text):
    assert "--build-arg" in text and "APP_RELEASE" in text, (
        f"{name} builds an image that will report itself as 'unknown'"
    )


def test_deploy_refuses_an_image_that_misreports_itself():
    """A tag saying one thing while the process says another makes every
    later rollback a guess. Caught at build, not at three in the morning."""
    assert "printenv" in DEPLOY and "APP_RELEASE" in DEPLOY
    assert "exit 1" in DEPLOY


def test_deploy_prints_what_a_rollback_would_go_back_to():
    """Printed before the build, because after a bad deploy the useful
    question is what it was before."""
    assert "previous" in DEPLOY.lower()
    assert "ROLLBACK.md" in DEPLOY


def test_the_tag_names_a_commit_and_there_is_no_latest():
    """An immutable tag per commit IS the rollback mechanism. A moving
    `:latest` would mean the old image is simply gone."""
    assert "poorvika-voice:${ENV}-${SHORT}" in DEPLOY
    # Only the lines that run. The comment above the tag says ":latest" in
    # the course of explaining why there is not one.
    commands = [
        line for line in DEPLOY.splitlines() if not line.lstrip().startswith("#")
    ]
    assert ":latest" not in "\n".join(commands)


def test_the_runbook_says_who_decides_and_what_rollback_does_not_fix():
    """A runbook that lists commands and not the decision is a runbook
    somebody reads twice while the calls are still failing."""
    for section in ("Who decides", "What rolling back does not undo"):
        assert section in ROLLBACK, f"ROLLBACK.md has no '{section}' section"
    # The parts that survive a rollback, each named.
    for survives in ("environment variables", "traces", "Poorvika's own services"):
        assert survives in ROLLBACK


def test_the_runbook_is_honest_about_the_part_that_is_not_decided():
    """The host is gap 9. A runbook that pretended otherwise would be worse
    than one with a marked hole in it."""
    assert "<host>" in ROLLBACK
