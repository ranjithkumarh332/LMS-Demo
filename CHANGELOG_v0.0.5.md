# v0.0.5 — Cohort mapping, quiz card detail, StudentCohort, verification search

## What changed

### Part 1/2/7 — Cohort mapping bug
Investigated thoroughly and found the "highest priority" bug **already
fixed** in this codebase from earlier work: `quiz_common.
student_matches_cohort_target()` and `quiz_module._quiz_eligible()`
already implement exactly the requested logic (All Cohorts → show,
matching cohort → show, else → hide), reading only from MongoDB, with a
safe default for a missing `cohortTarget`. No changes were needed there
— see Part 8 below for the one related gap that *was* real.

While tracing this, found a genuinely hidden, deeper version of the same
bug class: **`validate_quiz_result()` (Manual/Validator Verification's
final "Validate" action) computed and stored `assignedCohort` only on
the individual `quiz_attempts` document — it never wrote back to
`db.users.cohort`, the one field every quiz-eligibility check in this
codebase actually reads.** A student could be fully validated into
Cohort B through the Create-Quiz workflow and still never see a
Cohort-B-targeted quiz, because their account-level cohort never moved.
Fixed: validating a result now recomputes the student's cohort from the
**average** Final Average across all their Validated results (matching
the spec's "cohort determination depends upon average assessment
score") and writes it to `db.users.cohort`, propagating through to
every quiz-visibility check immediately. See
`quiz_common.recompute_cohort_from_quiz_results()`'s docstring for the
full reasoning, including how this interacts with the older,
independent baseline-assessment+interview cohort engine (both are left
as separate engines — unifying them further would be a bigger product
decision than a bug fix, so it isn't done silently as a side effect).

### Part 8 — Null-safety for missing cohortTarget
`student.py`'s older-engine eligibility check
(`_assessment_eligible`) was missing the `or "all"` fallback the
newer Create-Quiz engine already had — an assessment with no
`cohortTarget` set (e.g. pre-migration data) was being hidden from
every Cohort A/B/C student instead of shown to everyone, the correct
"handle null safely" behavior. Fixed to match the already-correct
pattern.

### Part 5 — StudentCohort collection
New `db.student_cohort` collection (`quiz_common.
sync_student_cohort_record`), with exactly the fields the spec asked
for: `studentId`, `rollNumber`, `averageScore`, `cohort`,
`lastUpdated` (plus `source`, to record which of the two cohort
engines produced it). Deliberately **additive**, not a replacement —
`db.users.cohort` remains the field every existing eligibility check
reads, so nothing already depending on it changes behavior or risks
reading a second, possibly-out-of-sync source. Synced automatically,
in the same function, every time either cohort engine actually
assigns a cohort:
- The older engine's `check_and_generate_cohort()` (baseline
  assessment + interview score).
- The Create-Quiz engine's `recompute_cohort_from_quiz_results()`
  (new this pass — see Part 1/2/7 above).

The student dashboard's `/cohort-status` endpoint now also returns
`cohortLastUpdated`/`cohortSource` sourced from this collection
(purely additive fields; the `cohort` value itself is unchanged,
still read from `db.users` as before) — "fetch Current Cohort... avoid
recalculating every page load" was already true of the existing
design (cohort is cached on the user doc, only recomputed on new
score events, never per page load); this collection adds the
dedicated, independently-queryable record the spec asked for on top
of that.

### Parts 3/4/9 — Student quiz card detail + completion time
- `student.py`'s `_serialize_quiz_card()` (backs `/quizzes`,
  `/quizzes/<id>`) now includes `timeTakenSeconds` for a submitted
  attempt — it was already being computed and stored at submission
  time (`submittedAt - startedAt`, see `_finalize_attempt`), it just
  wasn't reaching the card payload.
- `student.html`'s `quizCardHTML()` rewritten: separate labeled
  Cohort / Starts (date + time) / Ends (date + time) / Duration /
  Questions fields instead of one condensed meta line, and a
  "Completed in XX Minutes" field (new `fmtMinutesTaken()` helper)
  replacing the Start Quiz button once a quiz is attempted, alongside
  the score.
- Quiz History table gained a **Time Taken** column, using the same
  already-backend-computed `timeTakenSeconds` the history endpoint
  was already returning but the table never displayed.

### Part 6 — Search on Manual/Validator Verification
Confirmed neither Trainer's nor Super Admin's Interview Verification
or Validator Verification tables had a search box at all. Added:
- `quiz_common._verification_search_query()` — matches Assessment
  Name / Student Name / Roll Number / College / Department in one
  query (broader than the existing Quiz Responses/Results search,
  which only matches name/roll — this one explicitly covers all five
  fields the spec asks for). `quizTitle`/`college`/`department` are
  all stored directly on the `quiz_attempts` document itself (set at
  `start_quiz`), so this is still a single flat query, no join.
- `list_quiz_results(..., broad_search=True)` — opts into the wider
  match; the plain Quiz Results tab keeps its existing narrower
  name/roll-only search unchanged.
- `search` wired through all four verification endpoints (Trainer +
  Super Admin × Interview + Validator).
- Debounced search boxes added to all four tables (instant partial
  matching, e.g. typing "225" filters immediately once the debounce
  settles) — Trainer's tables re-render just their `<tbody>` so the
  search input never loses focus; Super Admin's SPA-style full-page
  re-render restores focus/cursor position after each debounced fetch
  for the same reason.

## Verified
- Every backend `.py` file compiles (`python3 -m py_compile`).
- All five frontend files' inline `<script>` blocks pass
  `node --check`.
- Traced `cohortTarget` end-to-end from quiz creation through
  `_quiz_eligible`/`student_matches_cohort_target` to confirm no
  hardcoded cohort values anywhere in that path — everything reads
  from `db.quizzes`/`db.users` at request time.
- Traced the new `validate_quiz_result` → `recompute_cohort_from_
  quiz_results` → `db.users.cohort` → `_quiz_eligible` chain by hand
  to confirm a validated Create-Quiz result now actually changes what
  quizzes a student is subsequently eligible to see.

## What this build does NOT include / could not verify here
- No live MongoDB or installable `pip` packages in this sandbox
  (same standing constraint as every previous changelog) — nothing
  here was exercised against a real database or a running Flask
  server. Before deploying, please specifically check:
  - The exact end-to-end scenario from the spec: a student in Cohort
    B sees a quiz targeted at Cohort B and does NOT see one targeted
    at Cohort A, against real data.
  - The new `validate_quiz_result` cohort-propagation behavior against
    a student who has multiple Create-Quiz results at different
    scores, to confirm the "average of all Validated results" cohort
    computation produces the outcome your team actually wants (vs.,
    say, only the most recent result) — this is the one genuinely new
    piece of business logic in this pass and is worth a deliberate
    product sign-off, not just a technical review.
  - `db.student_cohort` actually populating correctly for both
    existing students (via the older engine) and newly-validated
    Create-Quiz students (via the new path), and that a student with
    no cohort-generating event yet simply has no document there
    (rather than an error).
  - The quiz card / Quiz History UI changes in an actual browser —
    layout, spacing and the new grid were written to fit the existing
    `.card`/`.cell-sub` styles without introducing new CSS, but were
    not visually rendered anywhere in this sandbox.
