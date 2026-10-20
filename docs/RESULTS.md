# Results

Every number here comes from a script in `scripts/` that anyone can rerun. Back to the [README](../README.md).

## The waitlist: does it actually help?

`python scripts/simulate.py` runs the real `Clinic` code through 12 weeks of a simulated clinic, once without and once with the waitlist. Both runs use the same patients, requests and cancellations.

The setup models a busy specialty clinic:
- four doctors, each with 16 half-hour slots a day;
- demand at 120% of capacity;
- 15% of appointments cancelled, half of them in the last 48 hours;
- most patients booking 2–10 days ahead;
- 60% of patients agreeing to join the waitlist;
- offers accepted 70% of the time, declined 15%, and ignored 15%.

Three runs with different random seeds (`--seed`):

| | Slots used, without → with waitlist | Gain | Average time from request to visit | Events |
| --- | --- | --- | --- | --- |
| Default seed | 95.7% → **99.6%** | +3.9 points | −7.8% | 12,361 |
| Seed 3 | 95.7% → **97.8%** | +2.1 points | −5.6% | 11,886 |
| Seed 11 | 95.4% → **97.5%** | +2.1 points | −5.0% | 11,847 |

So the waitlist raised slot use by **2–4 percentage points** and cut the average wait by **5–8%** across about 36,000 simulated events. In the default run, 1,789 offers were made and 1,285 accepted (72%), and the average delay past the day the patient wanted fell from 2.60 to 2.01 days.

The gain comes from **late cancellations**. A slot freed a day ahead is rarely taken by a new patient, because most people book further ahead. Someone who is already waiting will gladly take it. These numbers depend on the assumptions above: with fewer late cancellations, or more same-day demand, the gain shrinks. The script's flags (`--load`, `--cancel-rate`, `--same-day-share`, `--waitlist-rate`) let you test other clinics. Results are in `results/simulation.json`.

## The AI search: does it help?

The "Find a time" box can understand requests in two ways: keyword rules, or an LLM through NexusAI. `python scripts/eval_assistant.py` measures both on 60 labeled requests (`evals/assistant_requests.jsonl`), each with the right doctor or specialty, dates and time of day. They range from easy ("Okafor tomorrow") to messy ("dermatolgy apointment nxt week", "my 6 year old needs a sports physical next Wednesday"). "Today" is fixed at Thursday, Oct 15, 2026, so the labels don't go stale.

| | Keyword rules | LLM via NexusAI | **What patients get** (LLM, rules after 3 s) |
| --- | --- | --- | --- |
| Fully correct (every field) | 35/60 (58%) | **58/60 (97%)** | **54/60 (90%)** |
| Doctor or specialty | 55/60 (92%) | 60/60 (100%) | 60/60 (100%) |
| First date | 50/60 (83%) | 59/60 (98%) | 58/60 (97%) |
| Last date | 42/60 (70%) | 58/60 (97%) | 54/60 (90%) |
| Time of day | 58/60 (97%) | 60/60 (100%) | 60/60 (100%) |
| Mean wait | under 1 ms | 1.9 s | 1.4 s |
| Slowest 5% (p95) | under 1 ms | 6.2 s | 3.0 s (the limit) |

Measured on Oct 18, 2026 against the live gateway, with NexusAI's cache bypassed so every answer came from a model. NexusAI's router chose the models: Gemini 3.1 Flash-Lite for 30 answers, Gemini 3.5 Flash-Lite for 29 and Gemini 3.6 Flash for 1. This was before evenings and nights were added to the search. There were no gateway errors, and the run cost about $0.015, roughly **$0.25 per 1,000 searches**. Results are in `results/assistant_eval.json`.

What this shows:
- **The rules get the doctor right but struggle with dates:** "Oct 22", "the 21st", "Tuesday or Wednesday", "in the next 3 days", "tmrw". The LLM handles nearly all of them, typos included.
- **The LLM is sometimes slow:** 11 of 60 answers took longer than 3 seconds. For those, the app uses the rules' answer, so patients never wait more than 3 seconds and still get 90% fully correct.
- **Speed came from one setting.** Gemini models "think" before answering by default. The first run averaged about 10 seconds per request, with single answers taking up to 25 seconds. Setting `GEMINI_REASONING_EFFORT=low` on the NexusAI service brought simple answers down to about 2 seconds, without losing accuracy.
- **The LLM's two misses are borderline:** "in the next 3 days" ended on Saturday instead of Sunday, and "Thursday afternoon", asked on a Thursday, became next Thursday.

Two safeguards sit between the model and the search:
- **A time limit.** A search waits at most 3 seconds for the LLM, then the rules answer. A slow model can make results less precise, never slow.
- **A check against the patient's own words.** If the model narrows the search to a doctor the patient never named ("my son has a fever" → Dr. Okafor), the search widens to that doctor's specialty, so other doctors' open times aren't hidden. Before this check, 3 of 60 answers made that mistake; after it, none did.

One caveat: the prompt spells out the clinic's conventions ("next Tuesday" means Tuesday of next week, children go to Pediatrics even for a rash). Those conventions match how the requests were labeled, so this measures how well each approach follows the clinic's rules. It isn't a general benchmark of language understanding.

## The note check: does it find mistakes?

`python scripts/eval_notes.py` runs the note check on 68 short visit notes (`evals/note_checks.jsonl`), each with the patient's chart:
- **47 notes have one planted mistake each**, with the words that show it. For a contradiction, both sides count, since quoting either one points at the mistake.
- **21 notes are clean and realistic**, and should raise nothing. Some are tricky on purpose: "allergic to penicillin, so azithromycin", "take 1/2 tablet", "return if not better in a week".

| | Rules only | Rules + AI via NexusAI |
| --- | --- | --- |
| Mistakes caught | 22/47 (47%) | **45/47 (96%)** |
| Impossible values / vitals that disagree | 8/8 / 4/4 | 8/8 / 4/4 |
| Allergy conflicts / unbooked follow-ups | 6/6 / 4/4 | 6/6 / 4/4 |
| Contradictions | 0/11 | **11/11** |
| Chart mismatches | 0/6 | **6/6** |
| Missing plans | 0/8 | 6/8 |
| Clean notes flagged | 0/21 | **0/21** |
| False alarms (all notes) | 0 | 1 |
| Time per note | instant | 2.0 s mean, 4.0 s p95 |

Measured on Oct 18, 2026 against the live gateway. The AI was unavailable on 1 of 68 notes (a timeout); the rules still ran on that one.

**How it got there.** The first live run caught 83% of mistakes but raised **15 false alarms** and flagged 4 of 15 clean notes, mostly "no plan" on notes that had one. The fixes were the code checks described above, a stricter prompt, and scoring that accepts either side of a contradiction. To check that the fixes weren't just fitted to those notes, 12 new cases were written afterwards (marked `"added": "round 2"`). On those, the AI caught 5 of 6 mistakes and flagged 0 of 6 clean notes.

**The two misses, and a known trade-off.**
- **Pneumonia with no plan** is a plain model miss.
- **Gastroenteritis with no plan**, in a note that says "continue lisinopril" for blood pressure, is hidden on purpose. The "no plan" check drops the flag when the note has any plan word, so a plan for one problem hides a missing plan for another. I chose fewer false alarms over that catch, because a check that cries wolf gets ignored. A test records the trade-off.

## The history summary: can it be trusted?

`python scripts/eval_summary.py` asks NexusAI for a summary of 30 real demo patients across all four doctors, and checks every line:

| | Result |
| --- | --- |
| Summaries written by the AI | 30/30 (none fell back to the basic summary) |
| Lines kept / per summary | 130 / 4.3 |
| Lines dropped by the source check | 1 |
| Allergy mentioned, when the patient has one | 8/8 |
| **Tamper test:** a copy of every kept line with one number changed (138/88 → 145/88) | **95/95 rejected** |
| Time per summary | 2.4 s mean |
| Cost | $0.028 for 30, about $0.93 per 1,000 summaries |

Measured on Oct 18, 2026 against the live gateway. The one dropped line was the check being too strict: the model wrote an upcoming visit's year, which the record didn't spell out. Upcoming visits now include the year.

The tamper test is the important one. It measures the guard itself rather than the model: a summary line with a reading or date that isn't in the record can't reach the doctor.

To run all three tests against your own gateway:

```bash
python scripts/eval_assistant.py --nexusai-url https://nexusai-latest.onrender.com --show-failures
python scripts/eval_notes.py --nexusai-url https://nexusai-latest.onrender.com --show-misses
python scripts/eval_summary.py --nexusai-url https://nexusai-latest.onrender.com --show-dropped
```

NexusAI's cache is bypassed during the tests, so every answer and every timing comes from a model. CI runs the parts that don't need a model on every push: the search rules must stay above 55%, the note rules must catch 95% of their mistake types with no clean note flagged, and the summary check must reject every tampered number.
