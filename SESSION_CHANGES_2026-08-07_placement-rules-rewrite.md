# Session Changes — Placement Rules: root-cause fix for "Could not save placement rules"

## Why this wasn't a ground-up rewrite

Before touching anything, I audited the existing Placement Rules stack end to
end: `db.placement_rules` (single collection), `quiz_common.get_placement_rules()`
/ `cohort_from_score()` / `compute_cohort_recalculation_ops()` (single shared
cohort engine), and `superadmin.py`'s single `GET/PUT /placement-rules` pair
(no duplicate/legacy routes exist anywhere in the codebase — confirmed by
grepping every `.py` and `.html` file for `placement_rules`/`placement-rules`).
This *is* already a single, authoritative implementation, consistent with the
Aug 5–7 session logs in this repo. Rewriting it from scratch would have thrown
away correct, already-tested logic (batched bulk-write recalculation,
transactional save, precedence rules between the two cohort engines) and risked
reintroducing bugs that were already fixed. So this session did a targeted,
verified fix of the actual defect, plus the layout fix — not a rewrite.

## Root cause of "Could not save placement rules" — found and reproduced

I reproduced the failure with an isolated Flask test client + mongomock
harness (`init_superadmin(db=..., bcrypt=...)`, matching how `app.py` wires
the blueprint), rather than guessing:

`PUT /api/admin/placement-rules` wrapped its save-and-recalculate flow in a
MongoDB transaction (`db.client.start_session()` / `start_transaction()`),
and its `except` clauses only caught `OperationFailure` and `PyMongoError`.
**Any other exception raised while opening the session/transaction —
`NotImplementedError`, `ConfigurationError`, or any driver/environment-
specific error that isn't one of those two exception classes — was
completely unhandled.** Flask's default handler then returned a bare 500
with no JSON body. The frontend's error path,
`showToast((res.data && res.data.message) || 'Could not save placement rules.', true)`,
had no `res.data.message` to show, so every user saw the same generic
fallback string regardless of what actually went wrong — with nothing in
the response and, before this session, nothing logged server-side either,
making it effectively undebuggable from the outside.

This class of failure is exactly what a non-Atlas / non-replica-set / driver-
incompatible MongoDB deployment (or a session/transaction hiccup on an
otherwise-healthy one) would trigger in production, which matches the
reported symptom precisely.

## Fix — `superadmin.py`

- Added a module logger (`logging.getLogger("superadmin")`), matching the
  existing pattern in `quiz_module.py` / `question_bank.py`.
- The transaction-unavailable fallback now catches `OperationFailure`,
  `ConfigurationError`, **and `NotImplementedError`**, logs a `warning`, and
  falls back to the direct (non-transactional) save + recalculate path —
  instead of only catching `OperationFailure`.
- Wrapped the entire save/recalculate flow in an outer `try/except` with a
  final catch-all `except Exception`, so **no failure mode can ever produce
  a bare/message-less 500 again**. Every failure path now:
  - logs the full exception with `logger.exception(...)` (visible in server
    logs / log aggregators for real debugging), and
  - returns a specific `error(...)` JSON response the frontend can actually
    display, and
  - attempts to roll back the placement-rules document to its previous
    values via a new `rollback_rules()` helper, so a failed recalculation
    never leaves the database with new rules but stale cohorts.
- Added an `info`-level log line on every successful save
  (`"Placement rules updated by %s — %s student(s) recalculated."`).

Verified against the running blueprint (Flask test client + mongomock,
`pymongo==4.8.0` pinned to match `requirements.txt`):
- Fallback path (simulating a deployment where transactions aren't
  available) now completes successfully — save, recalculate, and reload all
  work, `recalculatedStudents` reports the correct count.
- Validation errors (weights not summing to 100, Placement Ready < Near
  Ready) still return clear 400s as before.
- A save followed by a fresh `GET` returns exactly what was saved (i.e.
  persistence survives "refresh" — Section 12 of the spec).
- `python3 -m py_compile superadmin.py` passes.

## Fix — `frontend/super_admin.html` (layout, Section 7 of the spec)

Root cause of the "only half page width" complaint: the Placement Rules
panel had a hardcoded `style="max-width:640px;"` directly on its `.panel`
wrapper — no other page in the dashboard does this, which is why every other
Super Admin page already spans the full content width and this one didn't.

- Removed the `max-width:640px` inline style.
- Added a `.form-panel-wide` modifier (3 columns ≥900px, 2 columns
  600–900px, 1 column <640px — same breakpoints/spacing/card styling as the
  existing `.form-panel`, `.panel`, `.form-group` rules already used
  everywhere else) and applied it to the Placement Rules form only. No
  colors, spacing tokens, card chrome, or other pages were touched — this is
  a layout-only change, per the "do not redesign" instruction.

Verified: extracted and `node --check`'d the full inline script block of
`super_admin.html` after the edit — passes.

## Follow-up fix — the actual crash behind the generic error message

The exception-handling fix above did its job: it turned a bare, message-less
500 into a real error message. That surfaced the *actual* underlying bug:

```
Could not save placement rules due to an unexpected server error:
unsupported operand type(s) for *: 'NoneType' and 'float'.
```

**Root cause:** `login.py` creates every new student with
`baselineAssessmentScore: None` and `interviewScore: None` as placeholders
(not yet scored — see lines ~502-503 and ~744-745). `compute_cohort_recalculation_ops()`'s
"Engine 1" query in `quiz_common.py` selected candidates with:

```python
{"baselineAssessmentScore": {"$exists": True}, "interviewScore": {"$exists": True}}
```

`$exists: True` only checks the key is present — it matches a field that's
present *and set to `None`* just as readily as one with a real score. So
**every unscored student** (the common case for any freshly-registered
student) was pulled into the batch, and:

```python
final_score = round((student["baselineAssessmentScore"] * a_weight + ...) / total_weight, 2)
```

crashed on `None * a_weight` the moment any such student existed in the
collection — which, on a real platform with active registrations, is most
of the time.

The single-student equivalent, `check_and_generate_cohort()`, already had
this right — it explicitly does `if assessment_score is None or
interview_score is None: return None` before ever multiplying. The batch
version just used the wrong Mongo filter.

**Fix:** changed the query to `{"$ne": None}` on both fields (matching the
non-null pattern already used for Engine 2's `finalAverage: {"$ne": None}`
a few lines below it), so only students with an actual recorded score are
included.

Verified with a Flask test client + mongomock seeded to match real
`login.py` behavior — one fully-scored student plus several freshly-
registered students with the real `None`/`None` placeholders:
- Before the fix: reproduced the exact `unsupported operand type(s) for *:
  'NoneType' and 'float'` error from the screenshot.
- After the fix: save succeeds, the scored student is correctly
  recalculated and moved cohorts, and the unscored students are left alone
  (still no cohort — correct, since they haven't been scored yet).
- `python3 -m py_compile quiz_common.py` passes.

Files: `quiz_common.py`

## Not changed (already correct, re-confirmed this session)

- `quiz_common.get_placement_rules()` / `cohort_from_score()` /
  `compute_final_employability_score()` / `compute_cohort_recalculation_ops()`
  — single shared engine, no hardcoded thresholds, batched `bulk_write()`
  (no N+1), used by Student/Trainer/Super Admin/College Admin alike.
- Client-side validation in `saveCohortRules()` (range checks, threshold
  ordering, weight-sum-to-100) — already present and correct.
- `nearReadyThreshold` / `highRiskThreshold` are intentionally kept in sync
  as two views of the same Cohort B/C boundary (see the comment in
  `update_rules()`) — this is the mechanism that prevents overlapping score
  ranges (Section 8), not a bug. Worth knowing: because the form always
  submits both fields together, `nearReadyThreshold` (not the High Risk
  input) is the value that wins on every save from this UI. If you'd rather
  the High Risk field be independently authoritative, that's a small,
  separate product decision, not a bug fix.
- Every dashboard (Student, Trainer, College Admin, Super Admin, Reports,
  Analytics) reads `db.users.cohort` fresh on every request via the shared
  `cohort_counts()` / `student_cohort_label()` helpers — no caching, so
  nothing further was needed for Section 6 ("update everywhere").

## Not verified in this environment

No live MongoDB (Atlas or otherwise) is reachable from this sandbox (no
network egress to `mongodb+srv://`), so this was verified with mongomock +
an isolated Flask test client rather than the real deployment. Recommend one
real smoke test before considering this closed: change thresholds from the
Super Admin UI against a real seeded college, confirm the success toast
shows a real `recalculatedStudents` count, refresh the page and confirm the
values persisted, and check server logs for the new `"Placement rules
updated by..."` line.

## Files touched

- `superadmin.py` — exception handling / logging fix in `update_rules()`.
- `quiz_common.py` — fixed the `None`-vs-`$exists` query bug in
  `compute_cohort_recalculation_ops()` that was the actual crash.
- `frontend/super_admin.html` — layout fix (`pagePlacementRules()` +
  new `.form-panel-wide` CSS rule).

No files were added or deleted. No database schema changes were needed —
`db.placement_rules` was already correctly structured.
