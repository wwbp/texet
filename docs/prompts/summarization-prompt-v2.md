# Weekly summarization prompt v2

Proposal, not deployed. Drafted from the memory test run on 13–14 Aug 2026
(50 isolated utterances, 10 five-turn threads, 10 summaries, 10 recall probes
against production's prompts and model).

## Deployment

- Paste the prompt text below into **admin console → Summarization Prompts →
  create**. The newest row wins, so creating a row deploys it; there is no code
  release and no restart.
- `summarization_prompts` is currently **empty in production**, which means the
  summarizer is running on `DEFAULT_SUMMARIZATION_PROMPT` in
  `app/response/crud.py`. Creating the first row is what takes over from it.
- The summarizer runs on `openai / gpt-4o-mini`, not on the reply model. That
  is pinned explicitly in `app/summary/service.py`; changing the model on the
  System Prompts form does not move it.

## What the test showed

The summary is the participant's entire memory once the week rolls over — the
recall probes confirmed the bot answers from it and nothing else. So whatever
the summary drops is gone, and whatever it keeps gets replayed back to the
participant next week.

| Observed | Consequence |
|---|---|
| All 10 summaries kept at least one deliberately trivial detail (a coffee spill, mismatched socks, bubble tea flavours) | Probes then "remembered" the trivia: p02's recall led with the coffee spill, ahead of starting a master's programme |
| Medication named in the thread became "medication" in the summary (p08: escitalopram) | Next week's bot cannot refer to a regimen the participant explicitly told it about |
| Occupation dropped while the change to it was kept (p09: "considering a dispatcher role", with the bus driving gone) | A decision is preserved with nothing left to make sense of it |
| A disclosed lapse softened to "a rough patch a few weeks back" (p04) | Reasonable discretion in tone, but the clinical event blurs on each retelling |
| 2 of 10 summaries appended a suggested agenda for next week | Not asked for; the summary is context, not a plan |
| The street address disclosed in p09's first turn appeared in no summary | Correct, and worth stating explicitly rather than leaving to luck |

## Prompt text

```
You are writing the memory that a text-based study companion will carry into
next week. It is the only record of this week that survives: the conversation
itself will no longer be visible. Write 4-6 sentences of plain prose.

Keep, in order of priority:
- Health and recovery facts the participant stated about themselves: conditions,
  medications by name, treatment, time in recovery, and any lapse, setback or
  change of regimen. Record these plainly and without euphemism.
- Anything that changed this week, together with the situation it changed from.
  A decision, application, diagnosis or loss is not usable next week unless the
  context it happened against is stated with it.
- The people, animals and routines the participant leans on, by name.
- Stated preferences about how they want to be texted, in their own terms.
- Their own words for what is helping and what is hard.

Leave out:
- One-off incidental detail with no bearing on how they are doing: meals,
  weather, errands, small mishaps, purchases. If it would be odd for a friend to
  raise it a week later, it does not belong here.
- Home addresses, phone numbers, employer names and any other direct identifier,
  even when the participant volunteers them. Say "at home" or "at work" instead.
- Anything the participant did not say. Do not infer, diagnose or interpret.
- Praise, encouragement, and suggestions for what to discuss next week.

Write about the participant in the third person, as continuous prose with no
headings or lists. Prefer their phrasing to clinical paraphrase.
```

## Checking it

Re-run the memory test against a candidate prompt and compare the Run C and
Run D sheets:

```
scripts/memory_test/  # see the module docstrings for the phase order
```

The two things to look for are whether the trivia is gone from the summaries and
whether the recall probes still name medications, conditions and the people the
participant relies on.
