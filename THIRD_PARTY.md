# Third-party material in this repo

| What | Where | License |
|---|---|---|
| Caveat (handwriting font), The Caveat Project Authors | `fonts/Caveat.ttf` | SIL Open Font License 1.1, `fonts/OFL.txt` |
| Patrick Hand (handwriting font), Patrick Wagesreiter | `fonts/PatrickHand.ttf` | SIL Open Font License 1.1, `fonts/PatrickHand-OFL.txt` |
| Source Sans 3, Adobe | `fonts/SourceSans3.ttf` | SIL Open Font License 1.1, `fonts/LICENSE-source-sans-3.txt` |
| Source Serif 4, Adobe | `fonts/SourceSerif4.ttf` | SIL Open Font License 1.1, `fonts/LICENSE-source-serif-4.txt` |
| wlr-screencopy-unstable-v1 protocol, Simon Ser and Andri Yngvason | `cap/wlr-screencopy-unstable-v1.xml` | MIT (notice in the file) |
| wlr-virtual-pointer-unstable-v1 protocol, Josef Gajdusek | `bench/vpointer/wlr-virtual-pointer-unstable-v1.xml` | MIT (notice in the file) |

Installed at setup, not stored here:

| What | How | License |
|---|---|---|
| MathJax (`mathjax-full` 3.2.1) and its dependencies | `npm ci` in `tex/` (pinned by `tex/package-lock.json`) | Apache-2.0 (MathJax, mhchemparser, mj-context-menu, speech-rule-engine); MIT (@xmldom/xmldom, commander, esm, wicked-good-xpath) |
| Playwright (bench only) | `npm i` in `bench/` | Apache-2.0 |
| kokoro-onnx and the Kokoro model (optional voice example) | `examples/kokoro_voice.py`'s setup steps | MIT (kokoro-onnx), Apache-2.0 (Kokoro-82M) |

The bench pages in `bench/pages*/` imitate the look of common apps (a mail client, a dashboard, an IDE, a chat app)
to test pointing; all names, messages, courses and hosts in them are made up.
