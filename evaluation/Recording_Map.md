# Recording Map

The assessment used a 15-scenario evaluation pool. **Ten calls were curated
for the primary submission.** One additional location-lookup failure is kept
separately as bug evidence. The remaining pool recordings are **not**
published.

Recordings were matched to scenarios by call order, timing, duration, and
file identity against the corresponding run artifacts—not by guessing from
export filenames alone.

## Primary submitted calls (10)

| Folder | Scenario ID | Export source |
|---|---|---|
| `submitted_calls/01-reschedule-existing/` | `02_reschedule_existing` | `002.mp3` |
| `submitted_calls/02-medication-refill/` | `04_medication_refill` | `004.mp3` |
| `submitted_calls/03-office-hours/` | `05_office_hours` | `005.mp3` |
| `submitted_calls/04-insurance-question/` | `07_insurance_question` | `007.mp3` |
| `submitted_calls/05-unclear-request/` | `08_unclear_request` | `008.mp3` |
| `submitted_calls/06-interruption/` | `11_interruption` | `011.mp3` |
| `submitted_calls/07-multi-intent/` | `12_multi_intent` | `012.mp3` |
| `submitted_calls/08-request-human/` | `13_request_human` | `013.mp3` |
| `submitted_calls/09-nurse-escalation/` | `14_nurse_escalation` | `014.mp3` |
| `submitted_calls/10-specific-doctor/` | `15_specific_doctor` | `015.mp3` |

## Separate bug evidence

| Folder | Scenario ID | Export source |
|---|---|---|
| `submitted_calls/bug-01-location-lookup-stall/` | `06_location_question` | `006.mp3` |

## Not submitted from the same pool

| Export | Scenario ID | Reason held back |
|---|---|---|
| `001.mp3` | `01_basic_scheduling` | Not needed for curated breadth |
| `003.mp3` | `03_cancel_appointment` | Incomplete close on this take |
| `009.mp3` | `09_change_mind` | Not selected for primary set |
| `010.mp3` | `10_conflicting_information` | Not selected for primary set |

Original exports stay local. Only curated copies under `submitted_calls/` are
published.
