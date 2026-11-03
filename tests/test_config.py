"""Config is the only place the knobs live, so the defaults are worth asserting."""

import pytest

from src.config import load


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    return monkeypatch


def test_missing_key_fails_loudly_rather_than_running_without_one(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        load("development")


def test_phase0_defaults_carry_into_both_environments(env):
    for name in ("development", "main"):
        cfg = load(name)
        # Measured: 0 disables thinking, worth about Rs 0.14/min net.
        assert cfg.thinking_budget == 0
        # Measured: 15 words roughly halves reply tokens against 25.
        assert cfg.reply_word_cap == 15


def test_system_instruction_carries_the_word_cap_and_the_no_inventing_rule(env):
    cfg = load("development")
    assert "under 15 words" in cfg.system_instruction
    assert "Never invent" in cfg.system_instruction


def test_word_cap_is_overridable_for_measurement(env):
    env.setenv("REPLY_WORD_CAP", "25")
    assert "under 25 words" in load("development").system_instruction
