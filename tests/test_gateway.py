from model_interface.formats import assemble
from model_interface.gateway import Run

from conftest import events_for


def run_for(**kw):
    return Run(session="s1", key="team-test", model="coder", format="anthropic", stream=True, **kw)


def request():
    return {"model": "coder", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}


def test_a_run_streams_the_providers_events_and_is_recorded(gateway, provider):
    run = run_for()
    events = list(gateway.stream(run, request()))
    assert events == provider.script and provider.closed
    assert provider.seen[0]["model"] == "fake-model"
    line = gateway.record.runs()[0]
    assert line["run"] == run.id and line["events"] == len(events) and line["stop_reason"] == "end_turn"
    assert line["usage"] == {"input_tokens": 10, "output_tokens": 7} and line["interrupted"] is False
    assert line["first_event_ms"] is not None and line["duration_ms"] is not None


def test_a_stop_mid_stream_closes_the_provider_and_ends_the_message_cleanly(gateway, provider):
    run = run_for()
    seen = []
    for e in gateway.stream(run, request()):
        seen.append(e)
        if len(seen) == 4:                               # mid-text: message_start, block start, two words
            gateway.stops.add(run.id)
    assert [e["type"] for e in seen[4:]] == ["content_block_stop", "message_delta", "message_stop"]
    assert seen[-2]["delta"]["stop_reason"] == "interrupted"
    assert provider.closed and provider.sent == 5       # it was asked for one more event, then closed
    message = assemble(seen)
    assert message["content"] == [{"type": "text", "text": "Hello there, "}] and message["stop_reason"] == "interrupted"
    assert gateway.record.runs()[0]["interrupted"] is True and run.stop_reason == "interrupted"


def test_a_stop_before_the_first_event_still_yields_a_whole_message(gateway, provider):
    run = run_for()
    gateway.stops.add("*")
    events = list(gateway.stream(run, request()))
    assert [e["type"] for e in events] == ["message_start", "message_delta", "message_stop"]
    assert assemble(events)["stop_reason"] == "interrupted" and provider.closed


def test_every_scope_stops_a_run(gateway):
    for scope in ("s1", "team-test", "*"):
        gateway.stops.scopes.clear()
        gateway.stops.add(scope)
        assert assemble(gateway.stream(run_for(), request()))["stop_reason"] == "interrupted", scope
    gateway.stops.scopes.clear()
    gateway.stops.add("someone-else")
    assert assemble(gateway.stream(run_for(), request()))["stop_reason"] == "end_turn"


def test_a_provider_failure_becomes_an_error_event_and_is_recorded(gateway, provider):
    def broken(request):
        yield provider.script[0]
        raise ConnectionError("gone")
    provider.stream = broken
    events = list(gateway.stream(run_for(), request()))
    assert events[-1]["type"] == "error" and "gone" in events[-1]["error"]["message"]
    assert gateway.record.runs()[0]["error"] == "ConnectionError: gone"


def test_records_can_be_filtered_by_session(gateway):
    list(gateway.stream(run_for(), request()))
    list(gateway.stream(Run(session="s2", key="k", model="coder", format="openai", stream=False), request()))
    assert [r["session"] for r in gateway.record.runs()] == ["s2", "s1"]
    assert [r["session"] for r in gateway.record.runs(session="s1")] == ["s1"]
