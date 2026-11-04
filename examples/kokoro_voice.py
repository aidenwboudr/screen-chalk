"""A voice for `chalk say`: text -> speech with Kokoro (kokoro-onnx), as chalk expects from CHALK_VOICE_CMD.

Reads the text from stdin (or the arguments) and writes raw 24 kHz mono s16le PCM to stdout one sentence at a
time, so playback starts after the first sentence (~0.3 s of synthesis on a desktop CPU) instead of the whole
paragraph.

Setup (once):
  python3 -m venv ~/.local/share/chalk/voice && ~/.local/share/chalk/voice/bin/pip install kokoro-onnx numpy
  mkdir -p ~/.local/share/chalk/voice/models && cd ~/.local/share/chalk/voice/models
  curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx
  curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
then in ~/.config/chalk/env:
  CHALK_VOICE_CMD=~/.local/share/chalk/voice/bin/python /path/to/chalk/examples/kokoro_voice.py

Settings: CHALK_VOICE_MODELS (the folder with the two files above), CHALK_VOICE (af_heart), CHALK_VOICE_SPEED (1.05).
"""
import os, re, sys

MODELS = os.path.expanduser(os.environ.get("CHALK_VOICE_MODELS", "~/.local/share/chalk/voice/models"))
VOICE = os.environ.get("CHALK_VOICE", "af_heart")
SPEED = float(os.environ.get("CHALK_VOICE_SPEED", "1.05"))
RATE = 24000


def sentences(text):
    text = re.sub(r"\s+", " ", text).strip()
    return [s for s in re.split(r"(?<=[.!?;:])\s+(?=[A-Z0-9\"'(])", text) if s.strip()]


def model_file(name):
    """The model files directly in MODELS, or in a kokoro/ folder inside it."""
    for p in (os.path.join(MODELS, name), os.path.join(MODELS, "kokoro", name)):
        if os.path.exists(p):
            return p
    raise SystemExit(f"chalk voice: {name} isn't in {MODELS} (see the setup at the top of {__file__})")


def main():
    text = " ".join(sys.argv[1:]) or sys.stdin.read()
    if not text.strip():
        return
    import numpy as np
    import onnxruntime as ort
    from kokoro_onnx import Kokoro
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = min(8, os.cpu_count() or 1)
    kokoro = Kokoro.from_session(ort.InferenceSession(model_file("kokoro-v1.0.int8.onnx"), opts,
                                                      providers=["CPUExecutionProvider"]),
                                 model_file("voices-v1.0.bin"))
    out = sys.stdout.buffer
    for s in sentences(text):
        pcm, rate = kokoro.create(s, voice=VOICE, speed=SPEED, lang="en-us")
        if rate != RATE:
            raise SystemExit(f"chalk voice: model gave {rate} Hz, expected {RATE}")
        try:
            out.write((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
            out.write(b"\0\0" * (RATE // 8))  # a short breath between sentences
            out.flush()
        except BrokenPipeError:  # hushed
            return


if __name__ == "__main__":
    main()
