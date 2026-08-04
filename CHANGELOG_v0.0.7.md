# v0.0.7 — Dashboard header vs. snapshot cohort mismatch (Roll 25CS225 report)

## Symptom (from screenshot)
Same student, same page load, two different cohorts shown at once:
- Dashboard header chip: **"Cohort: Entry Level"**
- Employability Snapshot: **"you're currently in Cohort B"**, Overall Score
  56.59/100, 3 assessments completed.

## Root cause
v0.0.5/v0.0.6 correctly made `db.users.cohort` (the field the header and
every quiz-eligibility check read) get written by
`recompute_cohort_from_quiz_results()`, which only counts a result once
its `resultStatus` reaches **`Validated`** — a deliberate admin
confirmation gate, separate from a trainer entering interview marks.

`compute_overall_performance()` — the function behind the Dashboard Home
snapshot's Overall Score / Overall Cohort / "you're currently in Cohort
X" text — was still counting a result as "completed" the moment it hit
**`Interview Completed`**, one step *before* validation
(`resultStatus in [Interview Completed, Validated]`).

So for a student with 3 results that have interview marks entered but
haven't been through the separate Validate step yet:
- `compute_overall_performance()` includes them → computes and displays
  a live "Cohort B" purely from provisional, unconfirmed marks.
- `recompute_cohort_from_quiz_results()` correctly excludes them (not
  Validated yet) → `db.users.cohort` correctly stays `entry_level`.

Both numbers were individually correct given their own rule — the bug
was that two different "completed" definitions fed two different
cohort computations shown side-by-side on the same page. This is
exactly the class of bug the original spec's Part 1 ("There should be
only one current cohort for every student") was written to prevent,
and it survived v0.0.5/v0.0.6 because those passes fixed the
write-path for the *assigned* cohort but didn't audit the *other*
cohort figure the same dashboard also renders.

## Fix
`compute_overall_performance()`'s `completed` query now matches
`recompute_cohort_from_quiz_results()` exactly: `resultStatus ==
Validated` only (dropped `Interview Completed` from the `$in`).

Both the header and the snapshot are still served by the one shared
`/cohort-status` route (`student.py`), reading `db.users.cohort` and
`compute_overall_performance()` respectively — but now both are
computed from the identical underlying result set, so they're
structurally unable to diverge again, rather than just happening to
agree until the next student who has un-validated results.

## Effect
- A student with results that only have interview marks entered
  (not yet validated) will now show 0 assessments / no Overall
  Score/Cohort on the snapshot until a Validator confirms them — matching
  what the header and quiz-eligibility already correctly showed.
- Once validated, both header and snapshot flip to the same cohort in
  the same request cycle (no separate sync step needed — validation
  already calls `recompute_cohort_from_quiz_results`, and the snapshot
  recomputes live on every `/cohort-status` call).

## Verified
- `python3 -m py_compile` on every backend file.
- All frontend inline `<script>` blocks parse (`node --check` equivalent).
- No other caller of `compute_overall_performance()` depends on the
  `Interview Completed` results it used to include (only
  `/cohort-status` and `/readiness`, both audited).

## Still required (unchanged from v0.0.6)
This is a code fix. If Roll 25CS225's 3 results are actually already
Validated in the live database (not just Interview Completed) and the
mismatch persists after deploying this, that's the v0.0.6 stale-data
case instead — run `POST /admin/student-cohort/backfill` once, then
re-check. The two bugs (provisional-vs-validated counting, and
pre-fix stale data) are independent and this build fixes only the
former; run the backfill regardless, since it's idempotent and cheap.
