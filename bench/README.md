# chalk's bench

How chalk's choices were made: realistic app screens, a question per target, and a score for whether the first
mark lands on the right thing. Everything here runs on your own machine; the pages are synthetic (fictional
people, courses and hosts).

## Pointing: does the first mark land on the target?

`run.py` renders the pages in `pages/` and `pages-hard/` at 1920x1280 @1.5 (Playwright), asks the model a
spoken-style question per target (`tasks.json`, `tasks-hard.json`), and scores whether the first mark lands on
the element marked `data-gt` (snugly around it, centred on it at about its size, or on a meaningful part of it).

```bash
cd bench && python3 -m venv .venv && .venv/bin/pip install pillow numpy && npm i && npx playwright install chromium
.venv/bin/python run.py prep                                   # render + OCR (cached)
.venv/bin/python run.py run raw-full --model sonnet -j 8 [--tasks tasks-hard.json]
.venv/bin/python run.py show out/results/<file>.json           # misses + a gallery of them
```
It calls `claude -p`, so it needs Claude Code with a login (see the top README for keys).

What it found (hard set: 40 tasks of tiny KiCad/FL/ModelSim icons, datasheet cells, waveform spots, code tokens;
one task = 2.5 points, so differences under ~5 points are noise):

| variant | model | hit | median s |
|---|---|---|---|
| raw boxes on the full-resolution screenshot (`policy.md`) | Opus, effort low | 91% | 3.3 |
| the same | Opus | 92% | 3.9 |
| OCR ids instead of boxes | Opus | 90% | 3.8 |
| raw + a zoomed second look | Opus | 91% | 7.6 |
| grid overlay | Opus | 86% | 4.1 |
| raw boxes | Sonnet | 84-85% | 2.8-3.1 |
| set-of-marks (numbered OCR boxes) | Sonnet | 78% | 3.4 |
| image downscaled to 1280 px | Sonnet | 73% | 3.0 |

So `chalk look` sends the plain full-resolution screenshot and the model answers in pixel boxes; OCR is opt-in
(`--ocr`), and `chalk ask` defaults to Opus at low effort. On the 45-task standard set every method scored
~96%, which is why the hard set exists. Placing labels with an occupancy grid of the screenshot (`layout.py`)
cut the busy screen under the average label from 47% to 12% and labels on top of a mark from 35% to 0%.

Latency (Enter -> first stroke) went from 5.5 s to 2.8-3.7 s by grabbing raw frames instead of PNG + OCR,
starting `claude` while the question is still being typed, drawing each shape as soon as its JSON closes, and
moving the diagram into a second call that runs in parallel.

## Keeping up with the screen

- `test_watch.sh`, `test_adapt.sh`, `test_panel.sh`, `test_scene.sh`, `test_ask.sh`: the real overlay in a
  private headless Sway (`headless-sway.sh`; never your session), driven with a virtual pointer (`vpointer/`)
  and wtype. CI runs them.
- `scroll/`: marks following scrolled content. `run.sh` builds a container with its own headless Sway (on the
  host's GPU when there is one), opens a long web page and a PDF in Chrome, a CSS file in VS Code and a long log
  in foot, circles a line next to a small magenta marker (the ground truth), scrolls with a virtual touchpad and
  wheel, and scores every frame the compositor shows. Following scrolled content took the share of frames with
  the circle within 4 px (and 35 ms) of its target from ~4% to ~65% live, with 32-44 ms lag; replaying recorded
  scrolls through the matcher alone (`check.sh`) tracks 98.7% of frames within 4 px at 1.7 ms a frame.

## Offline tests (no screen)

`../test.sh` runs them: `test_config.py` (settings), `test_convo.py` (follow-up bookkeeping), `test_tex.py`
(MathJax glyphs), `test_scene.py` (scene boards), `test_scenehost.py` (the scene host's frame loop).
