from model_interface.formats import PLATFORM_RESULT, PLATFORM_USE
from model_interface.transcript import unfold

from conftest import call, text


def folded(*blocks):
    return [{"role": "user", "content": "fix the tests"}, {"role": "assistant", "content": list(blocks)}]


def result(id, out):
    return {"type": PLATFORM_RESULT, "tool_use_id": id, "content": out}


def shape(messages):
    return [(m["role"], [b["type"] for b in m["content"]] if isinstance(m["content"], list) else m["content"]) for m in messages]


def test_a_run_with_two_platform_calls_unfolds_into_the_real_turns():
    messages = folded(text("Running."), {**call("t1", cmd="test"), "type": PLATFORM_USE}, result("t1", "FAIL"),
                      {**call("t2", cmd="fix"), "type": PLATFORM_USE}, result("t2", "OK"), text("Fixed."))
    assert shape(unfold(messages)) == [
        ("user", "fix the tests"),
        ("assistant", ["text", "tool_use"]), ("user", ["tool_result"]),
        ("assistant", ["tool_use"]), ("user", ["tool_result"]),
        ("assistant", ["text"]),
    ]


def test_plain_messages_pass_through_untouched():
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": [text("hello")]}, {"role": "user", "content": "bye"}]
    assert unfold(messages) == messages


def test_a_call_of_theirs_stays_in_the_assistant_turn():
    messages = folded({**call("t1", cmd="ls"), "type": PLATFORM_USE}, result("t1", "a.py"), call("s1", "search", q="x"))
    assert shape(unfold(messages)) == [("user", "fix the tests"), ("assistant", ["tool_use"]), ("user", ["tool_result"]), ("assistant", ["tool_use"])]


def test_consecutive_user_turns_are_merged_so_a_result_and_the_next_message_share_one_turn():
    messages = folded({**call("t1", cmd="ls"), "type": PLATFORM_USE}, result("t1", "a.py")) + [{"role": "user", "content": "now push"}]
    assert shape(unfold(messages)) == [("user", "fix the tests"), ("assistant", ["tool_use"]), ("user", ["tool_result", "text"])]
