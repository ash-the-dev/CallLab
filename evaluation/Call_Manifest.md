# Call Manifest

Selected evidence from the fresh 15-scenario PGAI evaluation run. The primary
set has ten completed calls. One additional failed location-lookup call is kept
as bug evidence only.

| # | Scenario ID | Scenario | What was tested | Result | Recording | Transcript |
|---|---|---|---|---|---|---|
| 01 | `02_reschedule_existing` | Reschedule existing appointment | Existing-appointment lookup, alternate availability, reschedule confirmation | Completed | [recording.mp3](../submitted_calls/01-reschedule-existing/recording.mp3) | [transcript.txt](../submitted_calls/01-reschedule-existing/transcript.txt) |
| 02 | `04_medication_refill` | Medication refill request | Refill workflow when no refillable medication is available; support escalation | Completed via transfer | [recording.mp3](../submitted_calls/02-medication-refill/recording.mp3) | [transcript.txt](../submitted_calls/02-medication-refill/transcript.txt) |
| 03 | `05_office_hours` | Office hours question | Simple informational response and clean close | Completed | [recording.mp3](../submitted_calls/03-office-hours/recording.mp3) | [transcript.txt](../submitted_calls/03-office-hours/transcript.txt) |
| 04 | `07_insurance_question` | Insurance acceptance question | Insurance lookup with missing member ID and fallback guidance | Completed | [recording.mp3](../submitted_calls/04-insurance-question/recording.mp3) | [transcript.txt](../submitted_calls/04-insurance-question/transcript.txt) |
| 05 | `08_unclear_request` | Unclear request clarification | Clarifying a vague request into a schedulable task | Completed | [recording.mp3](../submitted_calls/05-unclear-request/recording.mp3) | [transcript.txt](../submitted_calls/05-unclear-request/transcript.txt) |
| 06 | `11_interruption` | Natural interruption handling | Recovery after an interruption/correction during a reschedule flow | Completed | [recording.mp3](../submitted_calls/06-interruption/recording.mp3) | [transcript.txt](../submitted_calls/06-interruption/transcript.txt) |
| 07 | `12_multi_intent` | Multi-intent scheduling and insurance | Primary scheduling goal while handling a secondary insurance question | Completed | [recording.mp3](../submitted_calls/07-multi-intent/recording.mp3) | [transcript.txt](../submitted_calls/07-multi-intent/transcript.txt) |
| 08 | `13_request_human` | Request a human representative | Explicit human-escalation preference | Completed via transfer | [recording.mp3](../submitted_calls/08-request-human/recording.mp3) | [transcript.txt](../submitted_calls/08-request-human/transcript.txt) |
| 09 | `14_nurse_escalation` | Nurse / clinical escalation | Clinical concern routed to clinical staff without diagnosis | Completed via clinical follow-up | [recording.mp3](../submitted_calls/09-nurse-escalation/recording.mp3) | [transcript.txt](../submitted_calls/09-nurse-escalation/transcript.txt) |
| 10 | `15_specific_doctor` | Specific doctor preference | Provider preference, unavailable-provider handling, fallback negotiation | Completed | [recording.mp3](../submitted_calls/10-specific-doctor/recording.mp3) | [transcript.txt](../submitted_calls/10-specific-doctor/transcript.txt) |

## Additional bug evidence

| ID | Scenario ID | What failed | Recording | Transcript |
|---|---|---|---|---|
| bug-01 | `06_location_question` | Location lookup stall that terminated an active caller | [recording.mp3](../submitted_calls/bug-01-location-lookup-stall/recording.mp3) | [transcript.txt](../submitted_calls/bug-01-location-lookup-stall/transcript.txt) |

See `evaluation/PGAI_Evaluation_Findings.md` for analysis. Treat MP3 audio as
authoritative when it differs from automated transcript wording.
