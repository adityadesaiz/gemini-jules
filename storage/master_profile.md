# Master Profile — Strategic Architect Brief

This file is read on every cycle and sent to Gemini as the standing
context for the project. Edit this file to steer the direction of the
autonomous pipeline.

- `bridge.py` — Gemini <-> local Claude Code loop.
- `bridge_jules.py` — Gemini <-> Jules (cloud sessions) loop. See
  `PROTOCOL.md` for the full message-schema/state-machine spec, and
  `storage/pipeline_config.json` (copy `pipeline_config.example.json`
  and fill in your repo) for the machine-readable repo/branch/
  verification-command config that script reads — don't duplicate
  those here, this file is Gemini's human-language brief only.

## Project Goal
(Describe what you want built, in plain language.)

## Constraints
- (e.g. language/framework choices, style rules, things to avoid)

## Definition of Done
- (e.g. tests pass, specific features implemented)
