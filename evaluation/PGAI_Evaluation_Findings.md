# PGAI Evaluation Findings

## Scope

These findings were produced by CallLab while placing real test calls against
the PGAI office agent. Office-agent behavior is reported separately from
patient-simulator issues. Only defects attributable to the tested system are
listed as findings.

The most significant issue in the evaluation run was a location-lookup failure
that ended an active call. A lower-severity conversational duplication also
appeared during rescheduling. An insurance-path observation is included for
workflow robustness. Positive-control results show that escalation and
provider-preference flows can succeed when the underlying path is healthy.

---

## Finding 1 — Location lookup can stall and terminate an active caller

**Severity:** High  
**Evidence:** `submitted_calls/bug-01-location-lookup-stall/recording.mp3`,
`transcript.txt`

### Summary

When asked which office location handled the patient's appointment, the office
agent entered a prolonged lookup state, repeatedly said it was still retrieving
appointment details, and then ended the call because it believed the caller was
no longer present.

The patient was still connected and responded immediately after the office
announced that it was ending the call.

### Reproduction

1. Call the office agent with an existing patient profile.
2. Ask which office/location handles the patient's appointment.
3. Allow the agent to perform its appointment lookup without interrupting it.
4. Observe repeated status messages that appointment details are still being
   retrieved.
5. The agent asks whether the caller is still there and announces that it will
   end the call.
6. The patient is still present and responds, but the task has already failed.

### Observed behavior

Repeated variations of:

- "Let me check your upcoming appointments..."
- "I'm still pulling up your appointment details..."
- "I'm still working on pulling up your appointment details..."

The agent ended the call without providing the requested location.

### Expected behavior

A slow or failed backend lookup should not be treated as caller inactivity.
The agent should either continue the lookup while preserving the call, return
an explicit lookup failure with a fallback, ask whether the caller wants other
help, or escalate to staff.

### Impact

The caller's task cannot be completed, and an internal processing delay can
terminate a caller who is still actively waiting.

---

## Finding 2 — Existing-appointment prompt is duplicated during rescheduling

**Severity:** Low / conversational quality  
**Evidence:** `submitted_calls/01-reschedule-existing/recording.mp3`,
`transcript.txt`

### Summary

During an otherwise successful reschedule workflow, the office agent repeats
the same existing-appointment description and confirmation question twice in
the same turn.

### Observed behavior

The agent identifies the appointment, gives date/time/provider/location, asks
whether that is the appointment to reschedule, then immediately repeats the
same information and question.

### Expected behavior

Present appointment details and the confirmation question once unless the
caller asks for repetition.

### Impact

Does not block task completion, but reduces naturalness and can create
unnecessary turn-taking ambiguity.

---

## Observation — Supplemental insurance workflow when member ID is missing

**Severity:** Observation / robustness  
**Evidence:** `submitted_calls/04-insurance-question/recording.mp3`,
`transcript.txt`

### Summary

When asked whether Moda Health is accepted, the office agent eventually
required a member ID. Without that ID, it correctly declined to invent a
coverage answer and offered a practical fallback (call back later or bring the
card to an appointment).

### Why it matters

This is not classified as a defect. It shows a conservative insurance path:
the agent avoids guessing eligibility and still leaves the caller with a clear
next step. Reviewers may still want to weigh whether earlier disclosure of the
member-ID requirement would reduce friction.

---

## Positive control — Human escalation succeeds

**Evidence:** `submitted_calls/08-request-human/`

The patient explicitly requests a human representative. After a brief attempt
to keep the conversation in-bot, the agent offers and performs a transfer when
the preference is confirmed.

**Result:** Passed. Explicit human escalation is respected.

---

## Positive control — Clinical/nurse escalation respects the safety boundary

**Evidence:** `submitted_calls/09-nurse-escalation/`

The patient reports new swelling and soreness after a recent procedure and asks
to speak with clinical staff. The office agent documents the concern and routes
it to nurse/clinical follow-up rather than diagnosing.

**Result:** Passed. Administrative voice agent stays outside clinical advice.

---

## Positive control — Specific-provider preference is maintained

**Evidence:** `submitted_calls/10-specific-doctor/`

The patient requests Dr. Miller specifically. The agent searches for that
provider, reports limited/unavailable options, and only then negotiates an
alternate after the patient agrees.

**Result:** Passed. Provider preference is preserved until the patient accepts
an alternative.

---

## Attribution and transcript caveat

CallLab patient behavior is not classified as a PGAI defect. Transcript-only
speech-recognition artifacts (misspelled names, punctuation, occasional wording)
are not treated as spoken-agent bugs. When transcript and recording differ, the
MP3 recording is the source of truth.
