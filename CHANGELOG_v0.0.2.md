# v0.0.2 — Create Quiz backend wiring

## What changed (quiz_module.py only — no frontend UI files touched)

1. **Section Distribution grid (`sectionConfig`) support.**
   The redesigned Create Quiz wizard (both Trainer and Super Admin) sends
   a granular per-section, per-difficulty Available/Display grid called
   `sectionConfig` instead of the old flat `sectionDistribution` /
   `difficultyDistribution` / `questionsDisplayed` fields. The backend
   previously only understood the old flat shape, so every quiz created
   from the new wizard would have failed validation. `validate_and_normalize()`
   now detects and fully validates `sectionConfig`:
   - Display can never exceed Available per (section, difficulty) cell.
   - Available per cell must **exactly** match the number of questions
     actually entered/uploaded for that cell (spec: "no more, no less").
   - Every authored/uploaded question must belong to a configured cell.
   - Questions Available / Questions Displayed / the flat distributions
     stored on the quiz doc are all derived from this grid, never trusted
     from the client.
   The old flat-field shape is still accepted for any other caller, so
   nothing about legacy quizzes changes.

2. **Difficulty enum validation.** Every question's Difficulty is now
   validated server-side to be exactly Easy / Medium / Hard (case-insensitive
   in, stored Title-cased) whenever a quiz uses the Section Distribution
   grid — previously any string was accepted.

3. **Duplicate assessment name check.** `validate_and_normalize()` now
   rejects a title that already exists (case-insensitive), on both create
   and edit (edit excludes the quiz's own id).

4. **Combined section+difficulty random draw.** `select_random_questions()`
   (used once per student attempt) now draws the exact Hard/Medium/Easy
   Display counts *per section* when a quiz has `sectionConfig` stored,
   instead of only being able to stratify by section OR by difficulty.
   Falls back to the old logic for quizzes created before this change.

5. **Bulk Upload — two new endpoints**, mounted under both `/api/trainer`
   and `/api/admin` (same as every other quiz route), which the frontend
   was already calling but which didn't exist yet:
   - `GET /quizzes/bulk-upload-template` — generates and streams an
     `.xlsx` template (openpyxl) with the exact columns the validator
     below expects, dropdown data validation for Question Type / Section
     (pulled live from `db.quiz_sections`, never hardcoded) / Difficulty,
     three worked sample rows (Easy/Medium/Hard), and an Instructions
     sheet.
   - `POST /quizzes/bulk-upload-validate` — accepts the uploaded file,
     parses every row, and returns valid questions + a row-numbered error
     list (`{row, issue}`) for anything wrong: missing required columns,
     empty required cells, invalid Question Type, unknown section (must
     exactly match a configured section), invalid Difficulty, dangling/blank
     correct-answer references, non-positive Marks, and duplicate question
     text within the file. Also returns a per-section Hard/Medium/Easy
     summary of the valid rows, which the wizard uses to auto-populate the
     Available fields.

6. **Drafts** (`PUT /quizzes/draft`) now also persist `sectionConfig`, so
   resuming/editing a draft repopulates the Section Distribution grid.

## Verified
- All backend `.py` files compile cleanly (`python3 -m py_compile`).
- The bulk-upload row-parsing algorithm was unit-tested standalone against
  a generated workbook (valid rows, unknown section, invalid difficulty,
  duplicate question) — produced the expected valid/invalid split and
  row-numbered messages.
- Confirmed `trainer.html` and `super_admin.html` send byte-identical
  `sectionConfig` / bulk-upload payload shapes, so this fix covers both
  dashboards from one change.

## What this build does NOT include (pre-existing, unmodified, and already
found to be working end-to-end during the audit for this pass)
- College/section live dropdowns, draft autosave, quiz list/detail/edit/
  duplicate/cancel/archive/delete, and student eligibility filtering
  (college + cohort/Entry Level + status + date window) were already
  fully wired to real DB-backed endpoints before this change — see
  `quiz_module.py` and the "STUDENT QUIZ MODULE" section of `student.py`.

## Recommended next steps before go-live
- Run this against a real MongoDB instance end-to-end (create a quiz via
  both Manual Entry and Bulk Upload from both dashboards, confirm a
  matching student account sees/attempts it).
- `flask_bcrypt`, `flask_jwt_extended`, `flask_limiter`, `flask_cors`, and
  `pymongo` were not installable in the sandbox this change was made in
  (no network egress), so `app.py` itself could not be booted for a live
  smoke test here — only static compilation and isolated logic tests were
  possible. Please run the existing test/staging flow before deploying.
