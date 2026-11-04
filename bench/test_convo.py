"""Offline check of follow-up bookkeeping in ask.py: tags, keep/replace, erasures, scene boards, the context the
model gets, and the idle reset. No overlay, no model: draw/send are recorded."""
import json, os, sys, tempfile, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ask  # noqa: E402


class GroundError(Exception):
    pass


class FakeGround:
    GroundError = GroundError


def fake_send(sent, live=None):
    """Records what's sent; answers {"op": "tags"} with `live`, as the overlay would (None: an old overlay)."""
    def send(msg, start=True):
        sent.append(msg)
        if msg.get("op") == "tags" and live is not None:
            return {"ok": True, "tags": live}
    return send


def run_turn(state, sent, drawn, answer, sketch, question, live=None, contexts=None):
    send = fake_send(sent, live)
    draw = lambda shapes, keep=False, seed=None: drawn.append((shapes[0], keep))
    convo = ask.Convo(state, send)
    if contexts is not None:
        contexts += [convo.context(), convo.context(for_sketch=True)]
    turn = ask.Turn(ask.Pen(draw, FakeGround, keep_first=convo.active), convo, send)
    for s in answer["shapes"]:
        turn.mark(s)
    turn.finish(answer, (1920, 1280))
    turn.sketch(sketch) or turn.restore((sketch or {}).get("restore"))
    convo.end_turn(question, answer.get("say", ""))
    return convo


def closed_and_back():
    """The user closes the board and a mark fades, then asks again: the follow-up is told what's gone (not that
    it's "already marked"), and can put the closed board back exactly as it was."""
    state = tempfile.mkdtemp()
    sent, drawn = [], []
    a1 = {"shapes": [{"kind": "circle", "box": [10, 10, 50, 20], "label": "the box"},
                     {"kind": "arrow", "box": [100, 10, 50, 20]}], "say": "Type N*B*A*omega."}
    s1 = {"draw": True, "at": [900, 100], "scene": {"title": "emf peak", "objects": [{"id": "o1", "line": [0, 0, 1, 1]}],
                                                  "steps": [{"show": ["o1"], "say": "sin peaks at 1"}]}}
    run_turn(state, sent, drawn, a1, s1, "help")
    c = json.load(open(os.path.join(state, "convo.json")))
    assert c["specs"]["b3"]["scene"]["objects"] == s1["scene"]["objects"], c["specs"]   # kept whole, to put back

    # m1 faded (they typed in the box it circled), b3 closed with its red dot; m2 still drawn
    sent.clear(); drawn.clear(); ctx = []
    a2 = {"shapes": [{"kind": "circle", "box": [10, 10, 50, 20]}], "say": "Here it is again."}
    c = run_turn(state, sent, drawn, a2, {"draw": False, "restore": ["b3"]}, "help", live={"m2": "shown"}, contexts=ctx)
    marks_ctx, sketch_ctx = ctx
    on, off = marks_ctx.split("NOT in front of the user now")
    assert "m2: arrow" in on and "m1" not in on and "b3" not in on, marks_ctx
    assert "m1: circle" in off and "gone" in off and 'b3: board "emf peak"' in off and "closed it" in off, marks_ctx
    assert '"restore"' in sketch_ctx and '"restore"' not in marks_ctx
    board = next(d for d, _ in drawn if d.get("kind") == "panel")
    assert board["tag"] == "b3" and board["scene"] == s1["scene"], board            # back as it was
    assert "b3" in c.d["boards"] and not c.d["closed"] and "m1" not in c.d["marks"], c.d

    # next turn: b3 counts as on screen again; m1 isn't mentioned any more
    ctx = []
    run_turn(state, [], [], {"shapes": [], "say": "ok"}, {"draw": False}, "thanks",
             live={"m2": "shown", "b3": "shown", "m4": "shown"}, contexts=ctx)
    assert 'b3: board "emf peak"' in ctx[0] and "NOT in front" not in ctx[0], ctx[0]

    # a board tucked away as a tab counts as off screen and can come back; a shown one or an unknown tag can't
    drawn.clear()
    c = run_turn(state, [], drawn, {"shapes": [], "say": "see it"}, {"draw": False, "restore": ["b3", "b9", "m2"]},
                 "where did the diagram go", live={"m2": "shown", "b3": "docked", "m4": "shown"}, contexts=ctx)
    assert "tucked away" in ctx[-2], ctx[-2]
    assert [d["tag"] for d, _ in drawn if d.get("kind") == "panel"] == ["b3"], drawn
    drawn.clear()
    run_turn(state, [], drawn, {"shapes": [], "say": "ok"}, {"draw": False, "restore": ["b3"]}, "again",
             live={"m2": "shown", "b3": "shown", "m4": "shown"})
    assert not any(d.get("kind") == "panel" for d, _ in drawn), "a board that's up isn't drawn twice"

    # an overlay that can't say (older, or not running) leaves the bookkeeping as it was
    c = ask.Convo(state, fake_send([], live=None))
    assert "b3" in c.d["boards"] and "m2" in c.d["marks"] and "NOT in front" not in c.context()

    # the model erasing a board drops it for good; only the last few closed boards are kept
    c.remove(["b3"])
    assert "b3" not in c.d["specs"]
    for i in range(ask.KEEP_CLOSED + 2):
        c.add_board(f"b{20 + i}", s1)
    c.sync({})
    assert list(c.d["closed"]) == [f"b{20 + i}" for i in range(2, ask.KEEP_CLOSED + 2)] and len(c.d["specs"]) == ask.KEEP_CLOSED
    print("closed and back ok")


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
    closed_and_back()
