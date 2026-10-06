"""Context window assembly + title derivation (ai-chat-spec-gen spec R3)."""

from app.services.chat import build_converse_messages, derive_title


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def test_keeps_recent_messages_within_budget():
    history = [_msg("user", "a" * 100), _msg("assistant", "b" * 100), _msg("user", "c" * 100)]
    # Budget for all three: kept in order, starting with user
    all_kept = build_converse_messages(history, char_budget=350)
    assert len(all_kept) == 3
    assert all_kept[0]["content"][0]["text"] == "a" * 100
    # Budget for two: window picks (b, c), then leading assistant b is dropped
    # to satisfy Bedrock's user-first alternation — only c remains
    trimmed = build_converse_messages(history, char_budget=250)
    assert len(trimmed) == 1
    assert trimmed[0]["content"][0]["text"] == "c" * 100
    assert trimmed[0]["role"] == "user"


def test_always_keeps_latest_message_even_if_over_budget():
    history = [_msg("user", "x" * 500)]
    messages = build_converse_messages(history, char_budget=10)
    assert len(messages) == 1


def test_drops_leading_assistant_messages():
    history = [_msg("assistant", "orphan"), _msg("user", "hi"), _msg("assistant", "hello")]
    messages = build_converse_messages(history, char_budget=10_000)
    assert messages[0]["role"] == "user"


def test_title_derivation_truncates():
    assert derive_title("Build me a bot") == "Build me a bot"
    long = "x" * 100
    assert len(derive_title(long)) == 60
    assert derive_title("   ") == "New session"


# ------------------------------------------- brownfield substrate (spec R2)


def _session_with(substrate=None, source=None):
    from app.models import ChatSession

    return ChatSession(substrate=substrate, substrate_source=source)


def test_substrate_block_absent_when_no_substrate():
    from app.services.chat import substrate_prompt_block

    assert substrate_prompt_block(None) == ""
    assert substrate_prompt_block(_session_with()) == ""
    assert substrate_prompt_block(_session_with(substrate="   ")) == ""


def test_substrate_block_carries_content_and_source():
    from app.services.chat import substrate_prompt_block

    block = substrate_prompt_block(
        _session_with(substrate="SYSTEM: you are a support bot", source="prod bot v3")
    )
    assert "BROWNFIELD MIGRATION CONTEXT" in block
    assert "--- SUBSTRATE START ---" in block
    assert "SYSTEM: you are a support bot" in block
    assert "prod bot v3" in block
    # Honesty flag ships regardless of C1 status (spec R2.2)
    assert "cannot consume connectors yet" in block


def test_substrate_block_clips_at_budget_with_marker():
    from app.services.chat import SUBSTRATE_PROMPT_BUDGET, substrate_prompt_block

    block = substrate_prompt_block(_session_with(substrate="x" * (SUBSTRATE_PROMPT_BUDGET + 500)))
    assert "[substrate truncated]" in block
    # Clip applies to the substrate text, not the framing
    inner = block.split("--- SUBSTRATE START ---")[1].split("--- SUBSTRATE END ---")[0]
    assert inner.count("x") == SUBSTRATE_PROMPT_BUDGET


def test_system_prompt_concatenates_substrate_block():
    from app.services.chat import chat_system_prompt, substrate_prompt_block

    block = substrate_prompt_block(_session_with(substrate="tools: search, jira"))
    system = chat_system_prompt("power", "", block)
    assert system.endswith(block)
    # Without substrate the prompt is byte-identical to the pre-feature shape
    assert chat_system_prompt("power", "") == chat_system_prompt("power", "", "")
