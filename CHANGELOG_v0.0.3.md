# v0.0.3 — Create Quiz: bug fixes + Question Bank

## What changed

### Part 1 — Bulk Upload parser (root cause fix)
The old parser only ever read ONE worksheet (`"Questions"` if present, else
whichever sheet was first). Compared against `quiz_common.py`'s sibling
parser (`parse_master_workbook`, used for the platform's other
cohort/placement question bank), which treats **every worksheet as a
section**, this is almost certainly why a 5-section workbook (one sheet
per section) only ever showed ~2 questions: everything past the first
sheet was silently ignored.

`bulk_upload_validate()` in `quiz_module.py` now:
- Iterates **every worksheet** in the workbook, never just the first one.
- Auto-detects, per sheet, whether it's a "flat" sheet with its own
  Section column, or a "one sheet per section" layout where the sheet's
  **title** supplies the section (matching the convention already used by
  `parse_master_workbook`). Both layouts can be mixed in the same file.
- Never terminates on a bad row or an unparseable sheet — a sheet with no
  recognizable header (e.g. the template's "Instructions" sheet) is
  logged and skipped, not treated as a fatal error; every other sheet
  keeps being read.
- Never stops at a duplicate/invalid row — every row is validated and
  reported (`{row, sheet, issue}`), only a fully-blank row is skipped.
- Logs extensively via the standard `logging` module (`logger =
  logging.getLogger("quiz_module")`): sheet scanned, header columns
  resolved, per-row section/question/issues, and a final summary
  (`sheets_parsed`, `sheets_skipped`, `total_rows`, valid/invalid counts).
- Returns `sheetsParsed` / `sheetsSkipped` in the response alongside the
  existing `questions` / `errors` / `sectionSummary` / `totalRows`, and
  each error now also carries which `sheet` it came from.

Frontend (`trainer.html` + `super_admin.html`): the row-error table and
the downloadable validation-report CSV both gained a **Sheet** column.

### Part 2 — Manual Entry: duplicate navigation removed
The bottom-of-wizard "Next →" button doubled as a second, conflicting
question-to-question stepper on Step 2 (Manual Entry), fighting with the
top "← Prev Q / Next Q →" bar for the same job — and its enable/disable
state was driven by Section Distribution validity, which had nothing to
do with the question-stepping label it was showing, so it could look
"stuck" independent of anything the trainer was doing on the current
question.

Fixed by giving each bar exactly one job, as the spec asked ("only one
navigation system should exist"):
- **Top bar** (`← Prev Q` / `Next Q →`, `goToQuestion()`) is now the only
  way to move between individual questions. Unchanged — this already
  worked correctly.
- **Bottom bar** (`qzNextBtn`) now always means "finish this step and
  continue to Review". It no longer increments/decrements the current
  question index at all. If questions are incomplete when clicked, it
  jumps to the first incomplete one and says so — it does not just
  silently stay disabled.
- Every disabled state on this button now carries a `title` tooltip
  explaining exactly why (Part 3: "never disable without explanation").

Applied identically to both `trainer.html` and `super_admin.html`.

### Part 3 — Submit validation
- Backend: `create_quiz` / `update_quiz` now return a full `errors` array
  in the JSON body (not just a single truncated string) via a new
  `_validation_error_response()` helper, so the frontend can show every
  problem at once.
- Added: **Invalid cohort selection** validation (`VALID_COHORT_TARGETS`
  from `quiz_common.py`) — previously any string was silently accepted.
- Added: **Duplicate question detected** for Manual Entry (same
  section + question text) — Bulk Upload already had this check, Manual
  Entry never did.
- Frontend: `publishQuiz()` in both dashboards now renders the full error
  list as a bulleted list when the backend returns more than one, instead
  of only ever showing the first/truncated message.

### Parts 4–8 — Question Bank
New module `question_bank.py`, collection `db.question_bank` (kept
deliberately separate from `db.questions`, which is `quiz_common.py`'s
unrelated cohort/placement-exam bank — see that file's own docstring).

- **Auto-population (Part 5):** every question that passes validation on
  a Manual Entry or Bulk Upload quiz (either dashboard — Trainer and
  Super Admin have identical permissions in this module) is mirrored into
  `db.question_bank` on create/update. Deduped by a SHA-256 hash of
  `section + normalized question text + normalized options`
  (`questionHash`, unique-indexed) — re-uploading the same question is a
  no-op, not a second bank entry.
- **New quiz-creation mode (Part 4/6):** a third "Question Bank" radio
  card, **Super Admin only** (enforced both client-side, by only adding
  the option to `super_admin.html`, and server-side, by rejecting it in
  `validate_and_normalize` for any other role). Selecting it hides both
  the Manual Entry and Bulk Upload panels — only Section Distribution is
  shown, with Available always read live from the bank (read-only, same
  pattern Bulk Upload already used for its own Available fields) via a
  new `GET /question-bank/summary` endpoint.
- **Random draw at publish (Part 7):** `draw_questions_from_bank()` runs
  at the moment a Question-Bank-sourced quiz is actually published —
  from `create_quiz`, `update_quiz`, or the quiz-list Publish action
  (`PATCH /quizzes/<id>/status`) — never at draft-save time, so a draft
  never locks in a random set before the admin is ready. Selection is
  without replacement per (section, difficulty) cell, so duplicates
  within one quiz are structurally impossible; the same bank question can
  still appear in other quizzes later (explicitly allowed reuse, per
  spec).
- **Pre-publish validation (Part 8):** `validate_question_bank_config()`
  checks every requested Display count against the live bank count and
  fails with the spec's exact style of message (e.g. "Only 7 Hard
  question(s) available... Requested: 10. Please upload more Hard
  questions.") — checked once during Step-2 validation and again,
  atomically, right before the draw is committed (a lightweight
  race-safety net against another admin publishing off the same bank
  seconds earlier).

### Parts 9–11 — Backend/DB quality
- `db.question_bank` indexes added in `app.py`: unique `questionHash`,
  compound `(section, difficulty, active)` for the draw/summary queries,
  and `uploadedAt` for listing.
- Extensive logging added throughout the bulk parser and the Question
  Bank sync/draw/validate helpers (`logging.getLogger("quiz_module")` /
  `"question_bank"`) — sheet/row/section detail on DEBUG, summaries and
  failures on INFO/WARNING.
- A QuestionBank sync failure is caught and logged, never allowed to
  block the quiz itself from saving (`sync_questions_to_bank()` never
  raises).
- Quiz documents created from Question Bank mode store each drawn
  question's `questionBankId` for traceability back to its bank source.

## Verified
- Every backend `.py` file compiles cleanly (`python3 -m py_compile`),
  including the new `question_bank.py`.
- Both edited frontend files' inline `<script>` blocks pass
  `node --check` (syntax-only — no DOM/runtime available in this
  sandbox), and the two *un*touched dashboards (`student.html`,
  `college_admin.html`) were checked the same way as a baseline — all
  four pass, so nothing in the shared JS conventions was broken.
- Manually traced every new/changed endpoint against `validate_and_normalize`,
  `select_random_questions`, and `serialize_quiz` to confirm no existing
  Manual Entry / Bulk Upload code path changed behavior for quizzes that
  don't use Question Bank mode.

## What this build does NOT include / could not verify here
- No live MongoDB or installable `pip` packages in this sandbox (same
  constraint noted in v0.0.2's changelog) — nothing here was exercised
  against a real database or a running Flask server. Please run the
  existing test/staging flow before deploying, in particular:
  - A real multi-sheet, multi-section `.xlsx` upload through both
    dashboards, to confirm the Part 1 fix against an actual file (the
    row-parsing logic itself was traced by hand, not executed).
  - A full Question Bank publish cycle end-to-end: Trainer uploads
    populate the bank → Super Admin's Question Bank wizard shows live
    Available counts → publish draws and locks in a set → a student
    attempt renders it correctly via the existing `select_random_questions`
    path (untouched by this change, but worth confirming the
    `questionBankId`-tagged shape still flows through it cleanly).
  - Concurrent-publish race behavior on `_finalize_question_bank_pool`'s
    safety check (two admins publishing off a nearly-exhausted bank
    section at the same time).
- No UI/browser testing (no headless browser available here) — the
  Question Bank step's live-fetch-on-open behavior, the simplified
  Manual Entry footer button, and the new error-list rendering were all
  reviewed by hand and syntax-checked, not click-tested.
