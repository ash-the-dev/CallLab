# Bugs

This file tracks CallLab implementation bugs and engineering notes.
PGAI evaluation findings are in `evaluation/PGAI_Evaluation_Findings.md`.

Open issues and known gaps in the CallLab harness itself. Add a new section
when something breaks; move it to **Fixed** after it ships.

## Open

### One early PGAI run had no usable recording

- **Seen:** During an early multi-scenario PGAI pass, one call produced no
  usable Twilio recording for submission evidence.
- **Impact:** That specific run cannot be used as recording evidence as-is.
- **Notes:** Later passes should be checked per Call SID / recording SID rather
  than assuming local `calls/` folders imply a downloaded recording (CallLab
  does not download Twilio recordings locally).

### Deferred office audio can hit the byte cap

- **Seen:** Diagnostics can log deferred office audio releases at the
  deferred-audio byte/chunk limit during long patient turns.
- **Impact:** Oldest office audio from that patient turn may be dropped before
  replay, which can hurt mid-call understanding.
- **Notes:** Do not change deferred-audio behavior until telephony work is
  explicitly revisited.

## Fixed

### Scenario selection stuck on one scenario

- **Was:** `run.py` always defaulted to `01_basic_scheduling`.
- **Fix:** Automatic cycling through discovered numbered scenarios, with
  `.calllab_state.json` persistence and manual `--scenario` override.

### Scenario identity drift in basic scheduling

- **Was:** Patient name `Jane Doe` with a leftover rule naming Maya Thompson.
- **Fix:** Scenario rules now stay in character as Jane Doe only.

### Call artifacts not gitignored

- **Was:** `calls/` could be committed with transcripts and Twilio SIDs.
- **Fix:** `calls/` added to `.gitignore`.

### Config required PGAI destination on import

- **Was:** Importing `src/config.py` failed if `PGAI_TEST_NUMBER` was missing,
  even for `--self-test`.
- **Fix:** Destination numbers are validated by `run.py` for the selected mode.

### Hardcoded personal number in Twilio smoke test

- **Was:** `src/test_twilio.py` contained a personal E.164 number.
- **Fix:** Smoke test reads `SELF_TEST_NUMBER` from the environment only.
