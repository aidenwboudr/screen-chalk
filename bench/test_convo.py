"""Offline check of follow-up bookkeeping in ask.py: tags, keep/replace, erasures, scene boards, the context the
model gets, and the idle reset. No overlay, no model: draw/send are recorded."""
import json, os, sys, tempfile, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ask  # noqa: E402


class GroundError(Exception):
    pass


class FakeGround:
    GroundError = GroundError


def run_turn(state, sent, drawn, answer, sketch, question):
    send = lambda msg, start=True: sent.append(msg)
    draw = lambda shapes, keep=False, seed=None: drawn.append((shapes[0], keep))
    convo = ask.Convo(state, send)
    turn = ask.Turn(ask.Pen(draw, FakeGround, keep_first=convo.active), convo, send)
    for s in answer["shapes"]:
        turn.mark(s)
    turn.finish(answer, (1920, 1280))
    turn.sketch(sketch)
    convo.end_turn(question, answer.get("say", ""))
    return convo


def main():
    state = tempfile.mkdtemp()
    sent, drawn = [], []
    a1 = {"shapes": [{"kind": "circle", "box": [10, 10, 50, 20], "label": "R1"},
                     {"kind": "box", "box": [100, 10, 50, 20]}], "say": "First. Second. Third. Fourth."}
    s1 = {"draw": True, "at": [900, 100], "scene": {"title": "gain", "objects": [{"id": "o1", "type": "line", "from": [0, 0], "to": [1, 1]}],
                                                  "steps": [{"show": ["o1"], "say": "the gain falls"}]}}
    c = run_turn(state, sent, drawn, a1, s1, "how does it work?")
    first, keep = drawn[0]
    assert first["tag"] == "m1" and keep is False, drawn[0]           # a first question replaces
    assert drawn[1][0]["tag"] == "m2" and drawn[1][1] is True
    card = next(d for d, _ in drawn if d.get("kind") == "card")
    assert card["tag"] == "note" and card["text"] == "First. Second. Third."
    board = next(d for d, _ in drawn if d.get("kind") == "panel")
    assert board["tag"] == "b3" and board["scene"]["title"] == "gain", board
    assert {"op": "remove", "tags": ["note"]} in sent                  # the board's captions say it: no note beside it
    assert c.d["boards"]["b3"] == {"title": "gain", "box": None, "steps": ["the gain falls"]}, c.d["boards"]
    assert c.d["turns"][0]["q"] == "how does it work?"
    assert all(d.get("ttl") == 0 for d, _ in drawn), "answers stay until cleared"

    ctx = ask.Convo(state, lambda *a, **k: None).context()
    assert 'm1: circle at [10, 10, 50, 20] labelled "R1"' in ctx and 'b3: board "gain"' in ctx, ctx
    assert '"remove"' in ctx
    assert '"draw": false' in ask.Convo(state, lambda *a, **k: None).context(for_sketch=True)

    sent.clear(); drawn.clear()
    a2 = {"shapes": [{"kind": "arrow", "box": [300, 300, 20, 20]}], "say": "At 10 kHz it drops.", "remove": ["m2"]}
    s2 = {"draw": False}                                             # the board already up answers it
    c = run_turn(state, sent, drawn, a2, s2, "and at 10 kHz?")
    assert drawn[0][0]["tag"] == "m4" and drawn[0][1] is True, drawn[0]   # a follow-up keeps what's there
    assert {"op": "remove", "tags": ["busy"]} in sent
    assert {"op": "remove", "tags": ["m2"]} in sent and "m2" not in c.d["marks"]
    assert not any(d.get("kind") == "panel" for d, _ in drawn)        # no second board
    assert list(c.d["boards"]) == ["b3"] and len(c.d["turns"]) == 2

    # an old conversation starts over (and clears the screen)
    d = json.load(open(os.path.join(state, "convo.json")))
    d["t"] = time.time() - ask.IDLE_RESET - 5
    json.dump(d, open(os.path.join(state, "convo.json"), "w"))
    sent.clear()
    c = ask.Convo(state, lambda msg, start=True: sent.append(msg))
    assert not c.active and sent == [{"op": "clear"}]
    print("convo ok")


if __name__ == "__main__":
    main()
