"""Config is the only place the knobs live, so the defaults are worth asserting."""

import pytest

from src.config import devconfig, load, prodconfig


@pytest.fixture
def env(monkeypatch):
    """A developer's .env must not decide what these tests assert.

    `create_app` calls `load_dotenv()`, which writes the real .env into
    `os.environ` for the rest of the process. A machine with GROK_MEMORY=1
    then fails the "off by default" test. Clearing the knobs here leaves
    runtime loading alone.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    for name in (
        "GROK_MEMORY",
        "XAI_API_KEY",
        "GROK_MODEL",
        "GROK_SUMMARY_EVERY_TURNS",
        "GROK_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
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


def test_grok_memory_is_off_until_asked_for(env):
    cfg = load("development")
    assert cfg.grok_memory_enabled is False
    assert cfg.grok_model == "grok-4.3"
    assert cfg.grok_summary_every_turns == 4
    assert cfg.xai_api_key == ""


def test_grok_memory_reads_the_flag_and_the_model(env):
    env.setenv("GROK_MEMORY", "1")
    env.setenv("XAI_API_KEY", "xai-test")
    env.setenv("GROK_MODEL", "grok-4.20-0309-non-reasoning")
    env.setenv("GROK_SUMMARY_EVERY_TURNS", "6")
    cfg = load("development")
    assert cfg.grok_memory_enabled is True
    assert cfg.xai_api_key == "xai-test"
    assert cfg.grok_model == "grok-4.20-0309-non-reasoning"
    assert cfg.grok_summary_every_turns == 6


def test_word_cap_is_overridable_for_measurement(env):
    env.setenv("REPLY_WORD_CAP", "25")
    assert "under 25 words" in load("development").system_instruction


# --- the window has a floor, and it is the prompt -----------------------------
#
# These exist because a configuration that broke every call with a tool in it
# shipped with a green suite. The cost tests all built their own Shape, so
# nothing in the project ever compared the shipped trigger against the prompt
# it has to hold, and the only thing that noticed was a caller.


def _fixed_prompt_tokens() -> int:
    """What the Live API carries on every turn, before anyone speaks.

    Characters over four, which measures about 15% low against the 1,415 the
    API itself reported - so a margin built on this is conservative, which is
    the direction to be wrong in.
    """
    import json

    from src.agent.persona import system_instruction
    from src.agent.tools.declarations import tool_config

    return (len(system_instruction("voice", 100)) + len(json.dumps(tool_config()))) // 4


@pytest.mark.parametrize("defaults", [devconfig, prodconfig])
def test_the_compression_trigger_clears_the_prompt_it_has_to_hold(defaults):
    """400/100 closed the socket on the first tool call. 1007, every time.

    Compression always keeps the system instruction, so a trigger below it
    fires on every turn and empties the window of everything else - including
    the function call that the function response has to follow. The Live API
    answers that with a close frame, and the caller hears the call stop.

    Twice the prompt is not a magic number: it is the smallest margin that
    held in the probes, and 4,000 against a ~1,214-token estimate clears it
    comfortably. If this fails because the prompt grew, raise the trigger -
    do not lower the assertion.
    """
    if not defaults.CONTEXT_TRIGGER_TOKENS:
        return  # compression off: nothing to compress wrongly
    assert defaults.CONTEXT_TRIGGER_TOKENS >= 2 * _fixed_prompt_tokens()


@pytest.mark.parametrize("defaults", [devconfig, prodconfig])
def test_the_window_keeps_room_for_the_conversation_itself(defaults):
    """Surviving the tool call is not the bar. Remembering is.

    At 2,000/1,500 the call stayed up and the agent answered "Endha model
    pathi kekureenga?" to four turns running, because every turn emptied the
    window of what it had just said. The target has to leave room for an
    exchange or two of real conversation after the prompt is kept.

    One exchange is roughly 250 audio tokens: a few seconds of caller, a
    sentence or two of agent, at 25 tokens a second.
    """
    if not defaults.CONTEXT_TRIGGER_TOKENS:
        return
    room = defaults.CONTEXT_TARGET_TOKENS - _fixed_prompt_tokens()
    assert room >= 4 * 250, f"only {room} tokens left for the conversation"
