# Contributing

Issues and pull requests are welcome: bugs, other compositors, better marks, new panel or scene features.

- **Run the checks** before a PR: `./test.sh` (no screen needed), and for anything touching the overlay, panels,
  watch or the hotkey, the headless-Sway tests (`bench/test_watch.sh`, `test_adapt.sh`, `test_panel.sh`,
  `test_scene.sh`, `test_ask.sh`). They start their own private Sway, so they never draw on your session. CI runs
  both.
- **Measure what you change.** chalk was tuned with benches (`bench/README.md`): if a change is meant to make
  marks land better, follow scrolling better, or answer faster, include the bench numbers before and after.
- **Keep the overlay on the system python** (`/usr/bin/python3` with distro GTK bindings) and the CLI stdlib-only
  where it runs without a screen (`chalk`, `chalk-mcp`).
- **Another compositor:** the Sway-specific parts are the `swaymsg` calls (outputs, windows, workspaces) in
  `chalk`, `track.py` and `overlay.py`. A backend that answers the same questions for another compositor, chosen at
  runtime, is the way in.
- **No personal data in fixtures.** Bench pages and tests use made-up people, mail, hosts and courses.
- Don't commit API keys or screenshots of your own screen. CI runs gitleaks.

By contributing you agree your work is released under the MIT license (LICENSE).
