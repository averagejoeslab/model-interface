"""The tap: a stream's lines folded into a record by shape, never by name.

One merge serves every wire format. Strings grow, objects merge, lists of objects that carry an `index`
merge entry by entry, anything else is set, `type` is set once, null never erases, and nothing the
provider sent is renamed or dropped. A Reading applies that merge to a stream as its lines pass, piece
by piece, telling a listener as it goes; what counts as the head, a piece and a delta is the envelope
grammar of the door the stream came through.
"""
import copy
import json
import time

from jiter import from_json


def indexed(items: list) -> bool:
    """Whether a list is of objects that each say where they go."""
    return bool(items) and all(isinstance(x, dict) and "index" in x for x in items)


def merge(acc: dict, delta: dict, grow: bool = True) -> dict:
    """Fold `delta` into `acc` by the shape of each value, never by the name of its key.

    grow=True is for the pieces of a stream; grow=False is for a head that is restated, whose values are set.
    Values are copied in, never shared, so one delta can be folded into two places.
    """
    for k, v in delta.items():
        a = acc.get(k)
        if grow and a is not None and (v is None or k == "type"):
            continue
        if grow and isinstance(a, str) and isinstance(v, str):
            acc[k] = a + v
        elif isinstance(a, dict) and isinstance(v, dict):
            merge(a, v, grow)
        elif isinstance(a, list) and isinstance(v, list) and indexed(v):
            for item in v:
                while len(a) <= item["index"]:
                    a.append({})
                merge(a[item["index"]], item, grow)
        elif grow and isinstance(a, list) and isinstance(v, list):
            a.extend(copy.deepcopy(v))
        else:
            acc[k] = copy.deepcopy(v)
    return acc


def parse_json(text: str):
    """JSON text as a value: whole when it is whole, as far as it got when it is not, None when it is nothing yet."""
    try:
        return json.loads(text)
    except ValueError:
        try:
            return from_json(text.encode(), partial_mode="trailing-strings")
        except ValueError:
            return None


def data_of(line: str):
    """The JSON of a data line, or None for anything else (event names, blanks, [DONE])."""
    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload and payload != "[DONE]":
            return json.loads(payload)
    return None


def strings_of(value) -> str:
    """Every string anywhere in a value, joined: what a rule means by `text`."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return "\n".join(t for t in (strings_of(v) for v in (value.values() if isinstance(value, dict) else value)) if t)
    return ""


class Reading:
    """Reads a stream's lines into its record, piece by piece, telling a listener as it goes.

    The record is the live object: a piece appears when it opens, grows with each delta, and gets its end time
    when it closes. Every piece carries `at`, where it lives in the response, and `started`/`ended`. Nothing
    else is added.
    """

    def __init__(self, stream, listener=lambda kind, **payload: None):
        self.stream = stream
        self.listener = listener
        self.open = None                 # the piece that hasn't closed yet, or None

    def feed(self, line: str):
        data = data_of(line)
        if data is not None:
            self.take(data)

    def take(self, data: dict): ...

    def head(self, fields: dict):
        """What the provider says about the response outside its pieces; only what changed is told."""
        changed = {k: v for k, v in fields.items() if k not in self.stream.response or self.stream.response[k] != v}
        if changed:
            merge(self.stream.response, changed, grow=False)
            self.listener("stream.response", response=changed)

    def open_piece(self, at: str, piece: dict):
        self.close_piece()
        block = {"at": at, "started": time.time(), "ended": None, **piece}
        self.stream.blocks.append(block)
        self.open = block
        self.listener("block.started", at=at, block=block)
        return block

    def grow(self, at: str, delta: dict):
        """A delta for the piece at `at`: opens it if it is not the open one, then grows it by shape."""
        if self.open is None or self.open["at"] != at:
            self.open_piece(at, {})
        merge(self.open, delta)
        self.settle(self.open)
        self.listener("block.delta", at=at, delta=delta)

    def settle(self, piece: dict):
        """Anything the door derives from a piece's own fields, kept current as it grows."""

    def close_piece(self):
        if self.open is None:
            return
        self.open["ended"] = time.time()
        self.settle(self.open)
        self.listener("block.ended", at=self.open["at"], block=self.open)
        self.open = None

    def stop(self, reason):
        """The stream's end: the door's own word, set where the door keeps it."""
        self.close_piece()
        self.stream.response[self.stream.door.stop_field] = reason
