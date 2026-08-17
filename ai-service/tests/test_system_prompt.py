from pathlib import Path

from hw1_ai_service.config import DEFAULT_SYSTEM_PROMPT, LlmConfig, load


def test_default_prompt_states_current_capability_boundary():
    prompt = LlmConfig().system_prompt

    assert prompt == DEFAULT_SYSTEM_PROMPT
    assert "local offline assistant" in prompt
    assert "Do not claim to be Even AI" in prompt
    assert "no tools or persistent memory" in prompt
    assert "no camera, image, or raw-audio input" in prompt
    assert "cannot access the internet, live data" in prompt
    assert "device or sensor state" in prompt
    assert "perform actions beyond replying" in prompt
    assert "without claiming you observed or retrieved it" in prompt
    assert "recent conversation" in prompt
    assert "one to three concise sentences" in prompt


def test_example_prompt_matches_code_default_and_keeps_voice_output_cap():
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg = load(example)

    assert cfg.llm.system_prompt == DEFAULT_SYSTEM_PROMPT
    # Pin the example to the code default rather than a literal, so this can't
    # silently drift when the cap changes (250 as of the 2026-08-11 bump).
    assert cfg.llm.max_tokens == LlmConfig().max_tokens
