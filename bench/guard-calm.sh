#!/usr/bin/env bash
# Guardrail for the calm marks policy: the grounding hard set must not drop (opus low, 2 repeats).
set -euo pipefail
cd "$(dirname "$0")"
.venv/bin/python run.py run raw-full+policy=calm --model opus --effort low --tasks tasks-hard.json --repeat 2 -j 3 > out/guard-calm.log 2>&1
grep '^{"variant"' out/guard-calm.log
