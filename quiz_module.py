"""
============================================================
 quiz_module.py — "Create Quiz" backend (Assessment > Create Quiz)
============================================================
Dedicated, self-contained backend for the Create-Quiz feature used by
both the Trainer Dashboard and the Super Admin Dashboard's "Quiz
Management" wizard. Deliberately kept separate from quiz_common.py's
cohort/placement-rule assessment engine (db.assessments) — that
system powers baseline/mid/final cohort exams drawn randomly from a
shared question bank, which is a different feature with different
rules (random sampling, cohort scoring, placement). This module
covers manually authored quizzes: one document per quiz, storing its
own question list inline.

Collection: db.quizzes — the single source of truth for every quiz
created from either dashboard. Nothing about a quiz is ever
hardcoded in the frontend; every read here comes straight from Mongo.

Mounted twice from app.py, once per dashboard, via init_quiz(db, scope):
  - init_quiz(db, scope="trainer")    -> /api/trainer  (own-college scoped)
  - init_quiz(db, scope="super_admin")-> /api/admin     (full platform view)

Both scopes share 100% of the validation, status-computation and
editing-restriction logic below, so the two dashboards can never
disagree about whether a quiz is editable/deletable or what its
current status is.
"""

from datetime import datetime, timezone

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, get_jwt

from quiz_common import ok, error, role_required, now, to_object_id, serialize, log_activity

# ------------------------------------------------------------
# Defaults / constants
# ------------------------------------------------------------
# Seeded into db.quiz_sections exactly once if the collection is empty,
# so the "Section Distribution" / category picker has real DB-backed
# options from the very first run instead of a hardcoded frontend list.
_DEFAULT_QUIZ_SECTIONS = [
    "Communication", "Programming", "Reasoning", "Professionalism", "Interview Readiness",
]

VALID_QUIZ_STATE = {"draft", "published"}


# ------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------
def parse_dt(value):
    """Parse an ISO-ish datetime string (as produced by <input type=datetime-local>
    or a full ISO string) into an aware UTC datetime. Returns None if unparsable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    try:
        # datetime-local inputs look like "2026-08-01T09:30"
        if "T" in s and len(s) <= 16:
            s = s + ":00"
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _naive_utc(dt):
    """pymongo round-trips datetimes as naive UTC (Mongo has no tz concept),
    even though we insert timezone-aware ones — so always normalize to
    naive-UTC before comparing, regardless of which side a value came from."""
    if dt is None or not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def compute_status(doc):
    """Dynamic status, computed fresh from current server time on every
    read — never stored/cached, so it can never go stale.
      draft      -> creator hasn't published it yet
      scheduled  -> published, current time is before start
      active     -> published, current time is within [start, end] ("Live")
      completed  -> published (or was active) and current time is after end
      cancelled  -> manually cancelled (overrides the time-based cycle)
      archived   -> manually archived by Super Admin (overrides everything)
    """
    if doc.get("archived"):
        return "archived"
    if doc.get("cancelled"):
        return "cancelled"
    if doc.get("state") != "published":
        return "draft"
    start = _naive_utc(doc.get("startDateTime"))
    end = _naive_utc(doc.get("endDateTime"))
    n = _naive_utc(now())
    if start and n < start:
        return "scheduled"
    if end and n > end:
        return "completed"
    return "active"


def editable_now(doc):
    """Business rule: fully editable before the quiz starts (draft or
    scheduled). Locked the instant the start time passes, permanently."""
    return compute_status(doc) in ("draft", "scheduled")


def deletable_now(doc):
    """A quiz can only be removed while still a Draft — once scheduled it
    can be Cancelled instead, but not deleted outright."""
    return compute_status(doc) == "draft"


def cancellable_now(doc):
    """Only a Draft or Scheduled quiz can be cancelled (Live/Completed quizzes
    run their course; use Archive to retire a Completed one instead)."""
    return compute_status(doc) in ("draft", "scheduled")


def archivable_now(doc):
    """Only a Completed or Cancelled quiz can be archived."""
    return compute_status(doc) in ("completed", "cancelled")


def edit_block_message(doc):
    status = compute_status(doc)
    if status == "active":
        return "This quiz has already started and can no longer be edited."
    if status == "completed":
        return "This quiz has been completed and is now read-only."
    if status == "cancelled":
        return "This quiz has been cancelled and can no longer be edited."
    if status == "archived":
        return "This quiz has been archived and is now read-only."
    return None


def serialize_quiz(doc):
    out = serialize(doc)
    status = compute_status(doc)
    out["status"] = status
    out["canEdit"] = editable_now(doc)
    out["canDelete"] = deletable_now(doc)
    out["canCancel"] = cancellable_now(doc)
    out["canArchive"] = archivable_now(doc)
    questions = doc.get("questions") or []
    out["totalQuestions"] = len(questions)
    out["totalMarks"] = round(sum(float(q.get("marks") or 0) for q in questions), 2)
    return out


def _actor(db):
    """Resolve {id, role, name} for whoever is making the request, from the JWT."""
    claims = get_jwt()
    uid = get_jwt_identity()
    role = claims.get("role")
    user_doc = None
    oid = to_object_id(uid)
    if oid:
        user_doc = db.users.find_one({"_id": oid})
    name = (user_doc or {}).get("fullName") or ("Super Admin" if role == "super_admin" else "Unknown")
    college = (user_doc or {}).get("college")
    return {"id": uid, "role": role, "name": name, "college": college}


# ------------------------------------------------------------
# Validation — every mandatory field from the spec, checked server-side
# regardless of what the client already validated (defense in depth).
# Accepts both the Trainer wizard's payload shape and the Super Admin
# wizard's payload shape (field names differ slightly between the two
# existing, unmodified UIs) and normalizes them into one document.
# ------------------------------------------------------------
def _first(data, *keys, default=None):
    for k in keys:
        if k in data and data[k] not in (None, ""):
            return data[k]
    return default


def _resolve_college_names(db, colleges):
    """Authoritative college names for a list of college ids — always
    looked up server-side rather than trusted from the client. See the
    long comment in validate_and_normalize for why this matters: a
    quiz's collegeNames must be real names (student eligibility compares
    against the student's college name), never the ids themselves.
    """
    if not colleges:
        return []
    id_oids = [oid for oid in (to_object_id(c) for c in colleges) if oid]
    name_by_id = {
        str(doc["_id"]): doc.get("college_name")
        for doc in db.colleges.find({"_id": {"$in": id_oids}})
    }
    return [name_by_id.get(c, c) for c in colleges]


def normalize_quiz_college_names(db, quiz):
    """Self-heals a legacy quiz document whose collegeNames array
    actually holds college ids — the exact shape produced by the old
    'college_names = college_names or colleges' fallback, before
    validate_and_normalize was fixed to always resolve real names
    server-side. Called on every read path (list/detail/student
    eligibility) so existing quizzes correct themselves automatically
    the first time they're touched — no manual edit-and-republish
    required, and no separate migration step to run beforehand.

    A real college name is never also a valid ObjectId, so that's the
    detection signal: if every entry in collegeNames parses as an
    ObjectId, it's ids-mislabeled-as-names, not real names.
    """
    names = quiz.get("collegeNames") or []
    ids = quiz.get("colleges") or []
    if not names or not ids:
        return quiz
    if not all(to_object_id(n) for n in names):
        return quiz  # already real names (or already empty/mixed) — nothing to heal
    fixed = _resolve_college_names(db, [str(c) for c in ids])
    db.quizzes.update_one({"_id": quiz["_id"]}, {"$set": {"collegeNames": fixed}})
    quiz["collegeNames"] = fixed
    return quiz


def validate_and_normalize(data, db, actor):
    errors = []

    title = str(_first(data, "title", "assessment_name", "name", default="")).strip()
    if not title:
        errors.append("Quiz title is required.")

    description = str(_first(data, "description", "instructions", default="")).strip()

    category = str(_first(data, "category", "assessment_type", "quizType", "type", default="")).strip()
    if not category:
        errors.append("Assessment category is required.")

    cohort_target = str(_first(data, "cohortTarget", "applicable_cohort", "cohort", default="all")).strip()

    colleges = _first(data, "colleges", "applicable_colleges", default=None)
    if colleges is None and _first(data, "college", default=None):
        colleges = [str(_first(data, "college"))]
    colleges = [str(c) for c in (colleges or [])]

    # Always resolve the authoritative college names server-side from the
    # ids themselves — never trust a client-sent collegeNames array. This
    # was the actual root cause of college-scoped quizzes never appearing
    # to any eligible student: the Create Quiz wizard's payload only ever
    # sent college IDs (applicable_colleges), and the old fallback quietly
    # stored those IDs in place of names when collegeNames was absent.
    # student.py's eligibility check compares a quiz's collegeNames
    # against the student's college NAME, so IDs stored there could never
    # match — silently hiding every quiz that targeted specific colleges
    # (quizzes left at "All Colleges" were unaffected, since that path
    # skips the college check entirely).
    college_names = _resolve_college_names(db, colleges)

    start_dt = parse_dt(_first(data, "startDateTime", "start_datetime", "start", "date"))
    end_dt = parse_dt(_first(data, "endDateTime", "end_datetime", "end"))
    if not start_dt:
        errors.append("Quiz start date & time is required.")
    if not end_dt:
        errors.append("Quiz end date & time is required.")
    if start_dt and end_dt and end_dt <= start_dt:
        errors.append("End date & time must be after the start date & time.")

    duration_raw = _first(data, "durationMinutes", "duration", default=None)
    try:
        duration = int(duration_raw)
    except (TypeError, ValueError):
        duration = None
    if not duration or duration < 1:
        errors.append("Duration (in minutes) is required and must be a positive number.")

    # NOTE: Passing Marks has been removed entirely (field, validation, storage) per spec.

    visibility = str(_first(data, "visibility", default="")).strip()
    if not visibility:
        visibility = "colleges" if colleges else "all"

    # --- Questions Available / Questions Displayed ------------------------
    # "Available" = the FULL pool of questions actually authored/uploaded
    # (every one of them is stored). "Displayed" = how many of those a given
    # student is randomly served on their attempt (<= Available). Available
    # is always derived from the real question count, never trusted from the
    # client, so the two can never drift out of sync with what's stored.
    questions_raw = _first(data, "questions", "question_pool", default=[]) or []
    if not isinstance(questions_raw, list) or not questions_raw:
        errors.append("At least one question is required. Enter or upload the full pool of Questions Available.")

    displayed_raw = _first(data, "questionsDisplayed", "questions_displayed", default=None)
    try:
        questions_displayed = int(displayed_raw)
    except (TypeError, ValueError):
        questions_displayed = None
    if not questions_displayed or questions_displayed < 1:
        errors.append("Questions Displayed is required and must be a positive number.")
    elif isinstance(questions_raw, list) and questions_raw and questions_displayed > len(questions_raw):
        errors.append(
            f"Questions Displayed ({questions_displayed}) cannot exceed Questions Available "
            f"({len(questions_raw)} question(s) actually entered/uploaded)."
        )

    # --- Section distribution ---------------------------------------------
    # Counts represent how many questions of each section a student's random
    # draw should contain; they must sum to Questions Displayed, AND the pool
    # (Questions Available) must contain at least that many per section so a
    # valid random draw is always possible.
    section_distribution = _first(data, "sectionDistribution", "section_distribution", default=None)
    if isinstance(section_distribution, dict) and section_distribution:
        clean_dist = {}
        for k, v in section_distribution.items():
            try:
                clean_dist[str(k)] = int(v)
            except (TypeError, ValueError):
                clean_dist[str(k)] = 0
        section_distribution = clean_dist
        dist_sum = sum(section_distribution.values())
        if questions_displayed and dist_sum != questions_displayed:
            errors.append(
                f"Section distribution ({dist_sum}) must sum to Questions Displayed ({questions_displayed})."
            )
    else:
        section_distribution = None

    # --- Difficulty distribution (optional) --------------------------------
    difficulty_distribution = _first(data, "difficultyDistribution", "difficulty_distribution", default=None)
    if isinstance(difficulty_distribution, dict) and any(
        difficulty_distribution.get(k) not in (None, "") for k in ("easy", "medium", "hard")
    ):
        clean_diff = {}
        for k in ("easy", "medium", "hard"):
            try:
                clean_diff[k] = int(difficulty_distribution.get(k) or 0)
            except (TypeError, ValueError):
                clean_diff[k] = 0
        difficulty_distribution = clean_diff
        diff_sum = sum(difficulty_distribution.values())
        if questions_displayed and diff_sum != questions_displayed:
            errors.append(
                f"Difficulty distribution ({diff_sum}) must sum to Questions Displayed ({questions_displayed})."
            )
    else:
        difficulty_distribution = None

    clean_questions = []
    for i, q in enumerate(questions_raw if isinstance(questions_raw, list) else []):
        n = i + 1
        if not isinstance(q, dict):
            errors.append(f"Question {n} is invalid.")
            continue
        text = str(q.get("text") or "").strip()
        if not text:
            errors.append(f"Question {n}: question text is required.")
        raw_options = q.get("options") or []
        options = [str(o).strip() if o is not None else "" for o in raw_options]
        filled_options = [o for o in options if o]
        if len(filled_options) < 2:
            errors.append(f"Question {n}: at least 2 answer options are required.")

        correct = q.get("correct")
        if correct is None:
            correct = q.get("correctAnswers")
        if correct is None:
            correct = []
        # Accept either index-based ([0,2]) or letter-based (["A","C"]) correct answers.
        normalized_correct = []
        letter_map = {"A": 0, "B": 1, "C": 2, "D": 3}
        for c in (correct if isinstance(correct, list) else [correct]):
            if isinstance(c, bool):
                continue
            if isinstance(c, (int, float)):
                normalized_correct.append(int(c))
            elif isinstance(c, str) and c.strip().upper() in letter_map:
                normalized_correct.append(letter_map[c.strip().upper()])
        if not normalized_correct:
            errors.append(f"Question {n}: at least one correct answer must be selected.")
        else:
            for idx in normalized_correct:
                if idx < 0 or idx >= len(options) or not options[idx]:
                    errors.append(f"Question {n}: correct answer references an empty/invalid option.")
                    break

        q_type = str(q.get("type") or "single_choice").strip().lower()
        if q_type not in ("single_choice", "multiple_choice"):
            q_type = "single_choice"
        if q_type == "single_choice" and len(normalized_correct) > 1:
            errors.append(f"Question {n}: Single Choice questions must have exactly one correct answer.")

        marks_raw = q.get("marks")
        try:
            marks = float(marks_raw)
        except (TypeError, ValueError):
            marks = None
        if marks is None or marks <= 0:
            errors.append(f"Question {n}: marks must be a positive number.")

        section = str(q.get("section") or "").strip()
        if section_distribution and not section:
            errors.append(f"Question {n}: a section is required (Section Distribution is configured for this quiz).")

        clean_questions.append({
            "text": text,
            "options": options,
            "correct": sorted(set(normalized_correct)),
            "type": q_type,
            "section": section or None,
            "difficulty": (q.get("difficulty") or "").strip() or None,
            "marks": marks if marks is not None else 0,
            "explanation": (q.get("explanation") or "").strip(),
        })

    # Cross-validate the distributions against what's actually in the pool —
    # a random draw can only ever be as good as what was actually stored.
    if section_distribution and clean_questions:
        pool_by_section = {}
        for q in clean_questions:
            pool_by_section[q["section"]] = pool_by_section.get(q["section"], 0) + 1
        for sect, required in section_distribution.items():
            available = pool_by_section.get(sect, 0)
            if available < required:
                errors.append(
                    f'"{sect}" section requires at least {required} question(s) but only {available} were entered/uploaded.'
                )

    if difficulty_distribution and clean_questions:
        pool_by_diff = {}
        for q in clean_questions:
            key = (q["difficulty"] or "").strip().lower()
            pool_by_diff[key] = pool_by_diff.get(key, 0) + 1
        for level in ("easy", "medium", "hard"):
            required = difficulty_distribution.get(level, 0)
            available = pool_by_diff.get(level, 0)
            if required and available < required:
                errors.append(
                    f'"{level.capitalize()}" difficulty requires at least {required} question(s) but only {available} were entered/uploaded.'
                )

    if errors:
        return None, errors

    normalized = {
        "title": title,
        "description": description,
        "category": category,
        "cohortTarget": cohort_target or "all",
        "colleges": [str(c) for c in colleges],
        "collegeNames": [str(c) for c in college_names],
        "startDateTime": start_dt,
        "endDateTime": end_dt,
        "durationMinutes": duration,
        "visibility": visibility,
        # Available is always the *actual* stored pool size — never trusted
        # from the client — so it can never drift from what's in Mongo.
        "questionsAvailable": len(clean_questions),
        "questionsDisplayed": questions_displayed,
        "difficultyDistribution": difficulty_distribution,
        "sectionDistribution": section_distribution,
        "quizType": str(_first(data, "quizType", "assessment_type", default="manual")).strip().lower() or "manual",
        "questions": clean_questions,
    }
    return normalized, []


def select_random_questions(doc):
    """Stratified random draw of `questionsDisplayed` questions out of the
    full `questions` pool (Questions Available), respecting Section
    Distribution and, if set, Difficulty Distribution. Called once per
    student attempt (NOT at quiz-creation time), so every student gets a
    different, independently-randomized combination from the same pool.
    Returns the drawn question list (each question keeps its original
    pool index under "_poolIndex" so answers can be graded against the
    original document later).
    """
    import random

    questions = list(enumerate(doc.get("questions") or []))
    displayed = doc.get("questionsDisplayed") or len(questions)
    section_dist = doc.get("sectionDistribution")
    difficulty_dist = doc.get("difficultyDistribution")

    def _tag(idx_q, key_fn):
        idx, q = idx_q
        out = dict(q)
        out["_poolIndex"] = idx
        return out

    pool = [_tag(iq, None) for iq in questions]

    if section_dist:
        by_section = {}
        for q in pool:
            by_section.setdefault(q.get("section"), []).append(q)
        chosen = []
        for sect, count in section_dist.items():
            bucket = by_section.get(sect, [])
            random.shuffle(bucket)
            chosen.extend(bucket[:count])
        # Any shortfall (shouldn't happen thanks to create-time validation)
        # is topped up from the remaining pool so the quiz never under-delivers.
        if len(chosen) < displayed:
            chosen_idx = {q["_poolIndex"] for q in chosen}
            remaining = [q for q in pool if q["_poolIndex"] not in chosen_idx]
            random.shuffle(remaining)
            chosen.extend(remaining[: displayed - len(chosen)])
        random.shuffle(chosen)
        return chosen[:displayed]

    if difficulty_dist:
        by_diff = {}
        for q in pool:
            by_diff.setdefault((q.get("difficulty") or "").strip().lower(), []).append(q)
        chosen = []
        for level, count in difficulty_dist.items():
            bucket = by_diff.get(level, [])
            random.shuffle(bucket)
            chosen.extend(bucket[:count])
        if len(chosen) < displayed:
            chosen_idx = {q["_poolIndex"] for q in chosen}
            remaining = [q for q in pool if q["_poolIndex"] not in chosen_idx]
            random.shuffle(remaining)
            chosen.extend(remaining[: displayed - len(chosen)])
        random.shuffle(chosen)
        return chosen[:displayed]

    random.shuffle(pool)
    return pool[:displayed]


# ------------------------------------------------------------
# Blueprint factory
# ------------------------------------------------------------
def init_quiz(db, scope):
    """scope: 'trainer' or 'super_admin'.

    Role hierarchy: Super Admin -> Trainer -> Student, with Trainer
    reporting directly to Super Admin (NOT under College Admin). Per
    project requirements, within the Assessment Management module the
    Trainer and Super Admin have IDENTICAL permissions — full platform
    visibility and control over every quiz, not just ones the trainer
    personally created. College Admin and Student have no access to
    this module at all (enforced by which roles are accepted by
    role_required below).
    """
    bp = Blueprint(f"quiz_{scope}", __name__)
    quizzes = db.quizzes
    quiz_sections = db.quiz_sections
    role = "trainer" if scope == "trainer" else "super_admin"

    def _visible_query(actor):
        # Both Super Admin and Trainer get full platform visibility —
        # Assessment Management grants identical access to both roles.
        return {}

    def _find_owned_or_404(quiz_id, actor, require_ownership=True):
        oid = to_object_id(quiz_id)
        if not oid:
            return None, error("Invalid quiz id.", 404)
        doc = quizzes.find_one({"_id": oid})
        if not doc:
            return None, error("Quiz not found.", 404)
        # NOTE: ownership is intentionally NOT enforced. Trainer has the
        # same full access as Super Admin within Assessment Management,
        # so a Trainer can manage (edit/publish/schedule/cancel/archive/
        # delete) any quiz, not only ones they personally created.
        return doc, None

    # ----------------------------------------------------
    # GET /assessment/sections — category/section picker, DB-backed
    # (seeded once so it is never a hardcoded frontend array).
    # ----------------------------------------------------
    @bp.route("/assessment/sections", methods=["GET"])
    @role_required("trainer", "super_admin")
    def list_sections():
        if quiz_sections.count_documents({}) == 0:
            quiz_sections.insert_many([{"name": n, "createdAt": now()} for n in _DEFAULT_QUIZ_SECTIONS])
        docs = list(quiz_sections.find({}).sort("name", 1))
        return ok({"sections": [{"id": str(d["_id"]), "name": d["name"]} for d in docs]})

    # ----------------------------------------------------
    # Random-draw preview (Part 9) — exercises the same stratified
    # selection a student attempt will use, without exposing correct
    # answers to the eventual student flow. Useful here for trainers/
    # admins to sanity-check their Section/Difficulty Distribution.
    # A student-facing attempt endpoint (returning this shape with
    # correct answers stripped) belongs in student.py and can reuse
    # quiz_module.select_random_questions() directly.
    # ----------------------------------------------------
    @bp.route("/quizzes/<quiz_id>/random-preview", methods=["GET"])
    @role_required(role)
    def random_preview(quiz_id):
        actor = _actor(db)
        doc, err = _find_owned_or_404(quiz_id, actor, require_ownership=False)
        if err:
            return err
        drawn = select_random_questions(doc)
        return ok({
            "questionsDisplayed": doc.get("questionsDisplayed"),
            "questionsAvailable": doc.get("questionsAvailable"),
            "drawn": [{"text": q["text"], "section": q.get("section"), "difficulty": q.get("difficulty")} for q in drawn],
        })

    # ----------------------------------------------------
    # CREATE
    # ----------------------------------------------------
    @bp.route("/quizzes", methods=["POST"])
    @role_required(role)
    def create_quiz():
        data = request.get_json(silent=True) or {}
        actor = _actor(db)
        normalized, errors = validate_and_normalize(data, db, actor)
        if errors:
            return error(errors[0], 422) if len(errors) == 1 else error(
                "; ".join(errors[:5]) + (f" (+{len(errors)-5} more)" if len(errors) > 5 else ""), 422
            )

        requested_state = str(data.get("status") or data.get("state") or "published").strip().lower()
        state = "draft" if requested_state in ("draft", "save_draft") else "published"

        doc = {
            **normalized,
            "state": state,
            "cancelled": False,
            "archived": False,
            "createdBy": actor,
            "createdOn": now(),
            "updatedAt": now(),
            "updatedBy": actor,
        }
        result = quizzes.insert_one(doc)
        doc["_id"] = result.inserted_id
        log_activity(
            db, actor["id"], actor["role"], "quiz_created" if state == "published" else "quiz_draft_saved",
            f'Created quiz "{doc["title"]}"' + (" (draft)" if state == "draft" else ""),
            college=actor.get("college"), meta={"quizId": str(result.inserted_id)},
        )
        return ok({"quiz": serialize_quiz(doc)}, message="Quiz created successfully.", status=201)

    # ----------------------------------------------------
    # DRAFT AUTOSAVE — upserts one draft per wizard session. If "id" is
    # supplied (a previously-returned draft id) it updates that same
    # document; otherwise a new draft is created and its id returned so
    # subsequent autosaves target the same record instead of piling up
    # duplicate drafts.
    # ----------------------------------------------------
    @bp.route("/quizzes/draft", methods=["PUT"])
    @role_required(role)
    def save_draft():
        data = request.get_json(silent=True) or {}
        actor = _actor(db)
        draft_id = data.get("id") or data.get("draftId")

        # Drafts are exempt from full validation (they're incomplete by
        # definition) — only require *something* worth saving.
        title = str(_first(data, "title", "assessment_name", "name", default="")).strip()

        colleges = _first(data, "colleges", "applicable_colleges", default=[]) or []
        payload = {
            "title": title,
            "description": str(_first(data, "description", "instructions", default="")).strip(),
            "category": str(_first(data, "category", "assessment_type", "quizType", "type", default="")).strip(),
            "cohortTarget": str(_first(data, "cohortTarget", "applicable_cohort", "cohort", default="all")).strip(),
            "colleges": [str(c) for c in colleges],
            "collegeNames": _resolve_college_names(db, [str(c) for c in colleges]),
            "startDateTime": parse_dt(_first(data, "startDateTime", "start_datetime", "start")),
            "endDateTime": parse_dt(_first(data, "endDateTime", "end_datetime", "end")),
            "durationMinutes": _first(data, "durationMinutes", "duration", default=None),
            "visibility": _first(data, "visibility", default=("colleges" if colleges else "all")),
            "questionsAvailable": _first(data, "questionsAvailable", "questions_available", default=None),
            "questionsDisplayed": _first(data, "questionsDisplayed", "questions_displayed", default=None),
            "difficultyDistribution": _first(data, "difficultyDistribution", "difficulty_distribution", default=None),
            "sectionDistribution": _first(data, "sectionDistribution", "section_distribution", default=None),
            "quizType": str(_first(data, "quizType", "assessment_type", default="manual")).strip().lower() or "manual",
            "questions": data.get("questions") or data.get("question_pool") or [],
            "state": "draft",
            "updatedAt": now(),
            "updatedBy": actor,
        }

        oid = to_object_id(draft_id) if draft_id else None
        if oid:
            existing = quizzes.find_one({"_id": oid})
            # Ownership is not restricted — Trainer has the same full
            # access as Super Admin within Assessment Management.
            if not existing:
                oid = None  # fall through to creating a fresh draft
        if oid:
            quizzes.update_one({"_id": oid}, {"$set": payload})
            saved_id = oid
        else:
            payload["createdBy"] = actor
            payload["createdOn"] = now()
            saved_id = quizzes.insert_one(payload).inserted_id

        return ok({"id": str(saved_id)}, message="Draft saved.")

    # ----------------------------------------------------
    # LIST
    # ----------------------------------------------------
    @bp.route("/quizzes", methods=["GET"])
    @role_required("trainer", "super_admin")
    def list_quizzes():
        actor = _actor(db)
        cursor = quizzes.find(_visible_query(actor)).sort("createdOn", -1)
        return ok({"quizzes": [serialize_quiz(normalize_quiz_college_names(db, d)) for d in cursor]})

    # ----------------------------------------------------
    # GET ONE
    # ----------------------------------------------------
    @bp.route("/quizzes/<quiz_id>", methods=["GET"])
    @role_required("trainer", "super_admin")
    def get_quiz(quiz_id):
        actor = _actor(db)
        doc, err = _find_owned_or_404(quiz_id, actor, require_ownership=False)
        if err:
            return err
        return ok({"quiz": serialize_quiz(normalize_quiz_college_names(db, doc))})

    # ----------------------------------------------------
    # UPDATE — full edit, blocked once the quiz has started
    # ----------------------------------------------------
    @bp.route("/quizzes/<quiz_id>", methods=["PUT"])
    @role_required(role)
    def update_quiz(quiz_id):
        actor = _actor(db)
        doc, err = _find_owned_or_404(quiz_id, actor)
        if err:
            return err
        if not editable_now(doc):
            return error(edit_block_message(doc), 409)

        data = request.get_json(silent=True) or {}
        normalized, errors = validate_and_normalize(data, db, actor)
        if errors:
            return error(errors[0], 422) if len(errors) == 1 else error(
                "; ".join(errors[:5]) + (f" (+{len(errors)-5} more)" if len(errors) > 5 else ""), 422
            )

        requested_state = str(data.get("status") or data.get("state") or doc.get("state") or "published").strip().lower()
        state = "draft" if requested_state in ("draft", "save_draft") else "published"

        update = {**normalized, "state": state, "updatedAt": now(), "updatedBy": actor}
        quizzes.update_one({"_id": doc["_id"]}, {"$set": update})
        updated = quizzes.find_one({"_id": doc["_id"]})
        log_activity(
            db, actor["id"], actor["role"], "quiz_updated",
            f'Updated quiz "{updated["title"]}"',
            college=actor.get("college"), meta={"quizId": str(doc["_id"])},
        )
        return ok({"quiz": serialize_quiz(updated)}, message="Quiz updated successfully.")

    # ----------------------------------------------------
    # PUBLISH / UNPUBLISH / CANCEL / ARCHIVE — one endpoint, branching on
    # the requested target status, each with its own eligibility rule.
    # ----------------------------------------------------
    @bp.route("/quizzes/<quiz_id>/status", methods=["PATCH"])
    @role_required(role)
    def toggle_status(quiz_id):
        actor = _actor(db)
        doc, err = _find_owned_or_404(quiz_id, actor)
        if err:
            return err

        data = request.get_json(silent=True) or {}
        requested = str(data.get("status") or data.get("state") or "").strip().lower()
        if requested not in ("draft", "published", "active", "scheduled", "cancelled", "archived"):
            return error("status must be one of: draft, published, cancelled, archived.")

        if requested == "cancelled":
            if not cancellable_now(doc):
                return error("Only a Draft or Scheduled quiz can be cancelled.", 409)
            update = {"cancelled": True, "updatedAt": now(), "updatedBy": actor}
            action, verb = "quiz_cancelled", "cancelled"
        elif requested == "archived":
            if not archivable_now(doc):
                return error("Only a Completed or Cancelled quiz can be archived.", 409)
            update = {"archived": True, "updatedAt": now(), "updatedBy": actor}
            action, verb = "quiz_archived", "archived"
        else:
            if not editable_now(doc):
                return error(edit_block_message(doc), 409)
            new_state = "published" if requested in ("published", "active", "scheduled") else "draft"
            update = {"state": new_state, "updatedAt": now(), "updatedBy": actor}
            action = "quiz_published" if new_state == "published" else "quiz_unpublished"
            verb = "published" if new_state == "published" else "moved to draft"

        quizzes.update_one({"_id": doc["_id"]}, {"$set": update})
        updated = quizzes.find_one({"_id": doc["_id"]})
        log_activity(
            db, actor["id"], actor["role"], action, f'Quiz "{updated["title"]}" {verb}',
            college=actor.get("college"), meta={"quizId": str(doc["_id"])},
        )
        return ok({"quiz": serialize_quiz(updated)}, message=f"Quiz {verb}.")

    # ----------------------------------------------------
    # DUPLICATE — clones a quiz as a fresh Draft (new dates required before
    # it can be published again), available once the source is Completed.
    # ----------------------------------------------------
    @bp.route("/quizzes/<quiz_id>/duplicate", methods=["POST"])
    @role_required(role)
    def duplicate_quiz(quiz_id):
        actor = _actor(db)
        doc, err = _find_owned_or_404(quiz_id, actor, require_ownership=False)
        if err:
            return err
        doc = normalize_quiz_college_names(db, doc)
        clone = {k: v for k, v in doc.items() if k != "_id"}
        clone.update({
            "title": f'{doc.get("title","Quiz")} (Copy)',
            "state": "draft", "cancelled": False, "archived": False,
            "createdBy": actor, "createdOn": now(), "updatedAt": now(), "updatedBy": actor,
        })
        new_id = quizzes.insert_one(clone).inserted_id
        clone["_id"] = new_id
        log_activity(
            db, actor["id"], actor["role"], "quiz_duplicated",
            f'Duplicated quiz "{doc.get("title")}"', college=actor.get("college"),
            meta={"sourceQuizId": str(doc["_id"]), "quizId": str(new_id)},
        )
        return ok({"quiz": serialize_quiz(clone)}, message="Quiz duplicated as a new draft.", status=201)

    # ----------------------------------------------------
    # DELETE — only while still a Draft (Scheduled quizzes must be
    # Cancelled instead, never deleted outright)
    # ----------------------------------------------------
    @bp.route("/quizzes/<quiz_id>", methods=["DELETE"])
    @role_required(role)
    def delete_quiz(quiz_id):
        actor = _actor(db)
        doc, err = _find_owned_or_404(quiz_id, actor)
        if err:
            return err
        if not deletable_now(doc):
            return error("Only a Draft quiz can be deleted. Scheduled quizzes must be cancelled instead.", 409)
        quizzes.delete_one({"_id": doc["_id"]})
        log_activity(
            db, actor["id"], actor["role"], "quiz_deleted",
            f'Deleted quiz "{doc.get("title")}"',
            college=actor.get("college"), meta={"quizId": str(doc["_id"])},
        )
        return ok(message="Quiz deleted successfully.")

    return bp
