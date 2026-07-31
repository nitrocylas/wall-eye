"""Unit tests for the pure parts of chat.Chat (no network: update_memory / trim)."""
import chat


def test_update_memory_folds_facts_into_system_message():
    c = chat.Chat()
    c.update_memory(["dentist Friday", "I have a cat"])
    sys_msg = c.history[0]["content"]
    assert sys_msg.startswith(chat.SYSTEM)
    assert "dentist Friday" in sys_msg
    assert "I have a cat" in sys_msg


def test_update_memory_empty_restores_base_prompt():
    c = chat.Chat()
    c.update_memory(["something"])
    c.update_memory([])
    assert c.history[0]["content"] == chat.SYSTEM


def test_memory_lives_in_message_zero_so_trim_keeps_it():
    c = chat.Chat()
    c.update_memory(["remember this"])
    # flood the history, then trim - the memory-bearing system message must survive
    for i in range(40):
        c.history.append({"role": "user", "content": f"msg {i}"})
    c._trim(keep=16)
    assert "remember this" in c.history[0]["content"]
    assert c.history[0]["role"] == "system"
    assert len(c.history) == 17   # system + last 16
