# v0.0.6 — Cohort mapping: the actual remaining bug (backfill + a precedence fix)

## Context

This is a follow-up to v0.0.5, which fixed the forward-going version of
this bug (`validate_quiz_result` never propagated a Create-Quiz result's
`assignedCohort` to `db.users.cohort`, the field every quiz-eligibility
check reads). Re-audited the whole chain end-to-end again after a report
that the bug was still reproducing, and found two real, concrete gaps
the previous pass didn't cover.

## Root cause of the still-reproducing bug

The v0.0.5 fix only takes effect **going forward** — for any
Interview/Validator Verification result validated *after* that fix was
deployed. It does nothing for a student who was already Validated
*before* that fix existed. That student's `quiz_attempts` document has
always correctly shown `assignedCohort: "B"` (which is exactly why their
own Quiz History / Marks Management view has always correctly displayed
"Cohort B" — the spec's own observation that "the cohort is already
displayed correctly on the student dashboard" is accurate, because that
view reads the attempt-level field directly), but `db.users.cohort` —
the field `_quiz_eligible`/`student_matches_cohort_target` actually
compares against — was never updated for them, and simply deploying the
v0.0.5 code change doesn't retroactively fix data that's already sitting
in the database. This is precisely the "Field names / cached values /
database query conditions" class of bug the spec asked to investigate:
not a logic bug at all (the matching logic was already exactly correct
in v0.0.5, and remains unchanged here), but a **stale-data** bug —
correct code reading incorrect historical data.

Confirmed the read-side logic itself is correct end-to-end
(`quizCreation → cohortTarget → _quiz_eligible → student_matches_cohort_target
→ my_quizzes`, plus every write path for `cohortRanges`/`cohort` in
`superadmin.py`) — no case-sensitivity, ID-vs-name, or field-naming
mismatch was found anywhere in this chain; every cohort value in this
codebase is normalized to the same `{"A","B","C"}`/`"entry_level"`
vocabulary at every write site, and validated as such.

## What changed

### 1. Backfill / migration (spec Part 5: "Synchronize Existing Data")
New `quiz_common.backfill_student_cohorts(db)`:
- For every student with at least one Validated Create-Quiz result,
  re-runs `recompute_cohort_from_quiz_results()` — this is what
  actually fixes an already-affected student like the one in this
  report: their `db.users.cohort` gets corrected retroactively, which
  immediately makes them eligible for whatever cohort-targeted quizzes
  they should have been seeing all along.
- For every student whose cohort is already correct (either engine) but
  has no `db.student_cohort` record yet (assigned before that
  collection existed), mirrors one in without touching `db.users`.
- Idempotent and safe to re-run any number of times — every write is
  either a no-op or a strict correction, never a regression.

Exposed as `POST /admin/student-cohort/backfill` (Super Admin only),
with a "Sync Student Cohorts" button added to the Placement Rules page
(additive only — that page's existing rules form was found to be a
disconnected mock during this investigation, which is a separate,
larger, out-of-scope issue and was left untouched; only this one new,
fully-wired button was added).

**This is the step that actually fixes an already-broken account** —
deploying the code alone is not enough; **run this once after
deploying**, then re-test the exact Roll 225 / Cohort B scenario.

### 2. Precedence bug in the v0.0.5 fix itself
While re-auditing, found `recompute_cohort_from_quiz_results()` (added
in v0.0.5) had no guard against overwriting a cohort that the *other*,
older baseline-assessment+interview engine had already assigned — every
time *any* unrelated Create-Quiz result got validated for a student, it
would silently recompute and overwrite their cohort from Create-Quiz
data alone, even if their real, intended cohort came from the dedicated
older engine. Fixed: `recompute_cohort_from_quiz_results()` now checks
`cohortAssignedFrom` and leaves a student's cohort alone if it was last
set by `"assessment_plus_interview"` — it only ever sets/updates a
cohort that's unset or was itself already Create-Quiz-sourced. The
backfill above relies on this same guard, so it can't undo a
legitimately-assigned cohort from the other engine either.

## Verified
- `quiz_common.py`, `superadmin.py` and every other backend file
  compile (`python3 -m py_compile`).
- All five frontend files' inline `<script>` blocks pass
  `node --check`.
- Traced the full chain by hand once more: quiz creation →
  `cohortTarget` storage → `_quiz_eligible` → `student_matches_
  cohort_target` → `my_quizzes()`, and separately, `validate_quiz_
  result` → `recompute_cohort_from_quiz_results` (with its new guard)
  → `db.users.cohort` → eligibility, and the new backfill function
  against both of those paths.

## What this build does NOT include / could not verify here
- No live MongoDB in this sandbox — the actual fix here is a **data**
  fix as much as a code fix, so it genuinely cannot be verified without
  running `POST /admin/student-cohort/backfill` against your real
  database and re-testing the Roll 225 / Cohort B scenario end-to-end.
  Please do that as the very next step after deploying this.
- The Placement Rules page's "Save Rules" button and score-threshold
  form were discovered to be a disconnected UI mock (not reading from
  or writing to `db.placement_rules` at all) during this investigation.
  That's a real, separate bug worth fixing, but it's outside the scope
  of "cohort mapping & quiz visibility" this pass focused on — flagging
  it explicitly here rather than leaving it undocumented.
- The `recompute_cohort_from_quiz_results` precedence rule
  ("whichever engine assigns first wins, based on `cohortAssignedFrom`")
  is a reasonable default but is still a product decision, not just a
  technical one — worth a deliberate sign-off if your team wants
  different precedence (e.g., "always prefer the most recent
  evaluation regardless of engine").
