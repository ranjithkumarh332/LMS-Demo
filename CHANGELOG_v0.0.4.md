# v0.0.4 — Assessment Management: filters, search, leaderboard parity, export

## What changed

### Super Admin → Quiz Responses
- **Root-cause fix:** the page's table called `GET /assessments/responses`
  (missing the `/admin` prefix every other Super Admin call uses) — this
  404'd on every load, so `QUIZ_RESPONSES` was effectively always stale/
  empty. On top of that, the table was then filtered *again* in the
  browser (`QUIZ_RESPONSES.filter(...)`) against field names that didn't
  even match the backend's real row shape (`r.student`, `r.trainer`,
  `r.score` don't exist — the actual fields are `studentName`,
  `department`, `overallPercentage`, etc.). Both bugs are fixed: the
  frontend now calls `GET /admin/quiz-responses` (same route name
  Trainer's dashboard uses) and does zero client-side filtering — every
  row already matches every active filter by the time it reaches the
  browser.
- Filter bar redesigned (only this component — nothing else on the page
  changed): search box (Roll Number / Student Name / Register Number),
  College, **Course** (new), Cohort (incl. Entry Level), Assessment, and
  a Clear button. The old Trainer/Status dropdowns were removed — neither
  ever worked (no trainer field exists on an attempt, and every row here
  is already "Completed" by definition), so they were replaced with the
  filters the spec actually asked for rather than kept as decoration.
- Every filter (and the search box, debounced) now triggers a real
  `GET /admin/quiz-responses` + `GET /admin/quiz-analytics` round trip —
  combined filtering works because both are resolved as one Mongo query
  built entirely server-side (`quiz_common.list_quiz_responses` /
  `compute_quiz_analytics`).
- Leaderboard/Section-wise Performance were already correctly wired to
  `GET /admin/quiz-analytics` — that part of the spec was already done;
  it just needed the rest of the page around it fixed so it wasn't
  sitting next to a broken, disconnected table.
- **Export** added: a new `GET /admin/quiz-responses/export` builds an
  in-memory `.xlsx` (openpyxl — same library already used everywhere
  else in this codebase) from exactly the rows the table is currently
  showing, i.e. whatever the current filters/search resolve to. There
  was no export on this page before.
- Super Admin's Quiz Results tab also gained a search box (Name / Roll
  Number / Register Number) — it didn't have one at all before, unlike
  Trainer's equivalent tab.

### Trainer Dashboard → Assessment Management
- **Quiz Responses tab:** the frontend here was already fully built and
  polished (search box, Course filter, Cohort incl. Entry Level, Clear
  button, debounced search, an Export button already wired to
  `/api/trainer/quiz-responses/export`) — but the backend didn't support
  most of what it was already sending: `compute_quiz_analytics` and
  `list_quiz_responses` ignored `department`/`search` entirely, and two
  endpoints the frontend already called didn't exist —
  `GET /quiz-responses/filters` (Course dropdown options) and
  `GET /quiz-responses/export` (the Export button). All three gaps are
  closed; this page should now work exactly as its own frontend code
  already implied it should.
- **Quiz Results tab:** same pattern — `quizResultsSearch` was already
  wired client-side to call the backend with `?search=`, but
  `list_quiz_results()` silently ignored that parameter. Now honored.
- **Quiz Management tab:** Cohort/Status/Search were all client-side only
  (`renderQuizzes()` filtered an already-fetched, unfiltered
  `assessmentsData` array in the browser; `list_quizzes()` ignored every
  query parameter). Now:
  - `GET /quizzes` accepts `cohort`, `status`, and `search` (quiz title)
    and resolves all of them server-side — `cohort`/`search` as a real
    Mongo query clause, `status` in Python right after the query (it's a
    time-derived value — draft/scheduled/active/completed/cancelled/
    archived — computed by `compute_status()`, not a stored field, so it
    can't be a raw `$match`; it's still resolved entirely in the backend,
    before anything is sent to the browser, so no client-side filtering
    is happening here either way).
  - The Status filter now triggers a real reload instead of a
    client-side re-render; Search is debounced (350ms) into the same
    reload.
  - The sidebar's draft-count badge is now its own small, always-
    unfiltered `GET /quizzes?status=draft` call (`refreshAssessmentsBadge`)
    — previously it read off whatever was in `assessmentsData`, which
    would have gone wrong the moment that array became genuinely
    filtered (it would have shown "0 drafts" while a Status filter was
    active, for example).
  - This same backend change (`list_quizzes` accepting query filters) is
    shared by both dashboards — Super Admin's own Quiz Management tab
    keeps its existing client-side `applyMultiFilter` UI untouched (not
    in scope here — the spec's Quiz Management ask was specifically
    "bring Trainer up to Super Admin", not the reverse), but nothing
    about that change is Trainer-only or would need duplicating if it's
    ever wired up there too.

### Shared backend (`quiz_common.py`)
- `_student_search_query(search)` — one shared `$or` clause (Student
  Name OR Roll Number, case-insensitive partial match) used by every
  search box on both dashboards, so "search" can never mean something
  different depending on which page or role is asking. See its
  docstring for why "Register Number" isn't a separate field — this
  platform only has one student identifier (`rollNumber`), so Roll
  Number search already covers it.
- `list_distinct_departments(db, college=None)` — live, never-hardcoded
  Course/Department dropdown options, backing the new
  `GET .../quiz-responses/filters` endpoint on both dashboards.
- `build_quiz_responses_workbook(rows)` — the shared Excel export
  builder both `/quiz-responses/export` endpoints call.
- `compute_quiz_analytics`, `list_quiz_responses`, `list_quiz_results`
  all extended with `department`/`search` (the latter two already had
  `college`/`cohort`/`quiz_id`) — same filters, same semantics, callable
  identically from either dashboard's routes.
- `list_quiz_responses()` now also returns `rollNumber` per row (was
  missing before, even though the underlying attempt document already
  stored it).

## Verified
- Every backend `.py` file compiles (`python3 -m py_compile`).
- Both edited frontend files' inline `<script>` blocks pass
  `node --check` after every edit.
- Manually traced the new `department`/`search`/`cohort`/`status`
  parameters through every call site (both dashboards' JS → both
  Flask blueprints → the shared `quiz_common.py` helpers) to confirm
  there's no longer a gap between what a filter bar sends and what the
  backend actually understands, anywhere in this change.

## What this build does NOT include / could not verify here
- No live MongoDB or installable `pip` packages in this sandbox (same
  standing constraint as the previous two changelogs) — nothing here
  was exercised against a real database or a running Flask server.
  Before deploying, please specifically check:
  - A real multi-filter combination (College + Course + Cohort + search,
    all at once) on Super Admin's Quiz Responses page against actual
    submitted-quiz data, to confirm the combined Mongo query behaves as
    traced.
  - Both `.xlsx` export endpoints against a real `send_file` response
    (content-type, filename, and that Excel actually opens the file
    cleanly) — only the openpyxl workbook-building logic itself was
    reviewed by hand, not executed.
  - The debounced-search-then-full-rerender behavior on Super Admin's
    Quiz Responses/Quiz Results search boxes (focus/cursor restoration
    was added deliberately, but not click/type-tested in a browser).
  - The Trainer Quiz Management badge count now being a second network
    call (`refreshAssessmentsBadge`) — confirm it doesn't visibly lag
    behind the table on a slow connection in a way that's worth further
    optimizing (e.g. deriving it from the mutation response instead).
