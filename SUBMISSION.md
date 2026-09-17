# CallLab Submission

## Project

CallLab is an autonomous AI patient simulator for testing conversational
healthcare-office agents over real phone calls. It places outbound Twilio
calls, streams live audio through Twilio Media Streams, and speaks as a
YAML-driven patient (Jane Doe) using OpenAI Realtime.

Start here for assessment review. Technical setup lives in
[README.md](README.md). System design lives in
[ARCHITECTURE.md](ARCHITECTURE.md).

## GitHub

https://github.com/ash-the-dev/CallLab

## Walkthrough Loom

https://www.loom.com/share/69da982fb5aa4c2eb365d4dda4e95338

## AI Debugging Loom

[TO BE ADDED]

## Evaluation Findings

PGAI office-agent findings from the assessment run:

- PDF: [evaluation/PGAI_Evaluation_Findings.pdf](evaluation/PGAI_Evaluation_Findings.pdf)
- Markdown: [evaluation/PGAI_Evaluation_Findings.md](evaluation/PGAI_Evaluation_Findings.md)
- Call manifest: [evaluation/Call_Manifest.md](evaluation/Call_Manifest.md)
- Recording map: [evaluation/Recording_Map.md](evaluation/Recording_Map.md)

## Primary Submitted Calls (10)

Exactly ten curated complete calls. Each folder contains `recording.mp3` and
`transcript.txt`. The MP3 is authoritative if wording differs.

| # | Scenario | Purpose | Result | Recording | Transcript |
|---|---|---|---|---|---|
| 01 | Reschedule existing | Move an existing appointment | Completed | [mp3](submitted_calls/01-reschedule-existing/recording.mp3) | [txt](submitted_calls/01-reschedule-existing/transcript.txt) |
| 02 | Medication refill | Refill request / support path | Completed via transfer | [mp3](submitted_calls/02-medication-refill/recording.mp3) | [txt](submitted_calls/02-medication-refill/transcript.txt) |
| 03 | Office hours | Informational hours question | Completed | [mp3](submitted_calls/03-office-hours/recording.mp3) | [txt](submitted_calls/03-office-hours/transcript.txt) |
| 04 | Insurance question | Plan acceptance / missing member ID | Completed | [mp3](submitted_calls/04-insurance-question/recording.mp3) | [txt](submitted_calls/04-insurance-question/transcript.txt) |
| 05 | Unclear request | Clarify vague intent into a task | Completed | [mp3](submitted_calls/05-unclear-request/recording.mp3) | [txt](submitted_calls/05-unclear-request/transcript.txt) |
| 06 | Interruption | Recover after a natural barge-in | Completed | [mp3](submitted_calls/06-interruption/recording.mp3) | [txt](submitted_calls/06-interruption/transcript.txt) |
| 07 | Multi-intent | Schedule first, then insurance | Completed | [mp3](submitted_calls/07-multi-intent/recording.mp3) | [txt](submitted_calls/07-multi-intent/transcript.txt) |
| 08 | Request human | Explicit human escalation | Completed via transfer | [mp3](submitted_calls/08-request-human/recording.mp3) | [txt](submitted_calls/08-request-human/transcript.txt) |
| 09 | Nurse escalation | Non-emergency clinical routing | Completed via clinical follow-up | [mp3](submitted_calls/09-nurse-escalation/recording.mp3) | [txt](submitted_calls/09-nurse-escalation/transcript.txt) |
| 10 | Specific doctor | Prefer a named provider | Completed | [mp3](submitted_calls/10-specific-doctor/recording.mp3) | [txt](submitted_calls/10-specific-doctor/transcript.txt) |

## Separate Bug Evidence (not one of the 10)

Location lookup stall that terminated an active caller. Supporting evidence
only; not counted toward the required ten complete calls.

- [recording](submitted_calls/bug-01-location-lookup-stall/recording.mp3)
- [transcript](submitted_calls/bug-01-location-lookup-stall/transcript.txt)

## Architecture

[ARCHITECTURE.md](ARCHITECTURE.md) — starts with a short architecture summary,
then deeper turn-taking / media-path detail.

## Notes

- Recordings are MP3.
- Transcripts include both sides of the call.
- All submitted calls were made to the assessment line.
- When transcript wording and audio differ, treat the MP3 as authoritative.
- Raw local run folders under `calls/` are not published.
- CallLab harness notes (not PGAI findings) are in [BUGS.md](BUGS.md).
