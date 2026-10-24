# Reply and round-summary wording

Load this file at step 5 of `siula-shepherd`, before you post replies or the round summary.

## Inline replies

Keep wording neutral and evidence-based:

- **Fixed**: `Fixed in <short-sha>: <what changed and how it was verified>.`
- **Follow-up**: `Valid follow-up: <ticket, issue, or PR link>; keeping it outside this PR because <reason>.`
- **Already addressed**: `Already addressed: <current-code evidence>.`
- **No change**: `Keeping the current approach because <evidence or trade-off>.`
- **No change, fails safe**: `Fails safe: <worst result>, stopped by <bound>. Not fixing in this PR, because each push starts a new full review<; tracked in <link>>.`
- **Needs decision**: `Decision needed: <specific question or missing evidence>.`

A `Noise` entry gets no reply.

## Round summary

Post one PR-level summary in this format for every round that posts. Keep each cell to one line. Do not repeat every inline explanation.

```markdown
**Review round <n> (<old SHA> → <new SHA>): <verdict>**

| Comment | Class | Disposition | Change |
| --- | --- | --- | --- |
| <one-line claim> | edges | Fix | <one-line change> |
| <one-line claim> | edges | No change | <one-line reason> |
| <one-line claim> | design | Needs decision | <shared root> |

Validation: <checks run>. CI: <state>.
Earlier rounds: <one line per earlier round with its verdict and outcome, omitted in round 1>.
Open: <gaps, Follow-up, or Needs decision items, or omit the line>.
```

- Add one row per claim, in ledger order. Deduplicated claims share one row. A `Noise` entry gets no row.
- Use the dispositions from step 3: Fix, Follow-up, Already addressed, No change, or Needs decision. For a row that is not Fix, put the reason or next action in the Change column.
- Use the class and verdict from step 3b. Claims settled in step 3a show `—` in the Class column. A guard claim that fails safe in step 3c keeps `edges`.
- For a round with no push, write the header as `(<head SHA>, no push)`.
- When `sync` reported `done` with the target `ready_with_gaps`, start the Open line with `Ready with gaps:` and list each gap. Never call such a PR ready to merge.
