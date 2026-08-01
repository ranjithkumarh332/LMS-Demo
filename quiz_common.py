"""
============================================================
 quiz_common.py — Shared Quiz Engine
============================================================
Everything that is used by MORE THAN ONE role (super_admin,
trainer, student) lives here so the three role modules
(superadmin.py, trainer.py, student.py) never duplicate logic
and can never drift out of sync with each other. This is the
single source of truth for:

  - Cohort rules (Entry Level -> A/B/C), used identically by
    the Super Admin Dashboard and the Trainer Dashboard.
  - Master Excel workbook parsing -> question bank persistence.
  - The random question engine (per-section random sampling).
  - Score calculation (per-section + overall).
  - Small JSON helpers / auth decorator, mirroring login.py's
    style so all modules feel like one codebase.

Collections used (created lazily by Mongo on first insert):
  - db.questions            one question bank, shared across cohorts
  - db.assessments          assessment definitions (admin/trainer created)
  - db.assessment_attempts  one doc per student attempt (in-progress or submitted)
  - db.activity_log         one doc per logged action, powers every
                            "Recent Activity" panel (student/trainer/admin).
                            See log_activity() / get_recent_activity() below.
============================================================
"""

import os
import random
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import jsonify
from flask_jwt_extended import jwt_required, get_jwt, get_jwt_identity

# ------------------------------------------------------------
# Cohort configuration
# ------------------------------------------------------------
# A student with no cohort assigned yet is "Entry Level". Cohort
# generation is a TWO-STAGE process (see check_and_generate_cohort
# below): a real cohort (A/B/C) is only assigned once BOTH the
# assessment score AND the manual interview score exist, combined
# into a Final Employability Score using the Placement Rules that
# the Super Admin configures in db.placement_rules. There are NO
# hardcoded score ranges or weights anywhere in this file — every
# threshold is read from the database, every time, so a Super Admin
# edit takes effect immediately with zero code changes.
VALID_COHORTS = {"A", "B", "C"}
VALID_COHORT_TARGETS = {"A", "B", "C", "all", "entry_level"}
ENTRY_LEVEL = "entry_level"

# Fallback seed ONLY used the very first time the platform boots and
# db.placement_rules is completely empty, so the system has something
# sane to operate with before a Super Admin configures real rules.
# This is a one-time DB seed, not a code-level threshold: once seeded,
# every subsequent read/write goes through db.placement_rules and this
# constant is never consulted again.
_DEFAULT_PLACEMENT_RULES_SEED = {
    "cohortRanges": [
        {"cohort": "A", "min": 75, "max": 100},
        {"cohort": "B", "min": 50, "max": 74.999},
        {"cohort": "C", "min": 0, "max": 49.999},
    ],
    "assessmentWeight": 0.6,
    "interviewWeight": 0.4,
}

SECTION_ALIASES = {
    # sheet-name normalisation so "communication", "Communication ",
    # "COMMUNICATION" etc. all map to one canonical section key.
}


def now():
    return datetime.now(timezone.utc)


def error(message, status=400):
    return jsonify(success=False, message=message), status


def ok(payload=None, message=None, status=200):
    body = {"success": True}
    if message:
        body["message"] = message
    if payload:
        body.update(payload)
    return jsonify(body), status


def role_required(*allowed_roles):
    """Same contract as login.py's role_required, duplicated locally so
    quiz_common has no import-time dependency on login.py."""
    def decorator(fn):
        @jwt_required()
        def wrapper(*args, **kwargs):
            claims = get_jwt()
            if claims.get("role") not in allowed_roles:
                return error("You are not authorized to perform this action.", 403)
            return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator


def to_object_id(id_str):
    try:
        return ObjectId(id_str)
    except (InvalidId, TypeError):
        return None


def serialize(doc, extra_id_fields=None):
    """Turn a Mongo doc into JSON-safe dict: _id -> id, datetimes -> isoformat."""
    if doc is None:
        return None
    out = {}
    for k, v in doc.items():
        if k == "_id":
            out["id"] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, ObjectId):
            out[k] = str(v)
        else:
            out[k] = v
    return out


# ------------------------------------------------------------
# Cohort helpers — SHARED between Super Admin and Trainer dashboards.
# Both dashboards must call these functions rather than reimplementing
# the rule, so behaviour can never diverge between the two.
# ------------------------------------------------------------
def student_cohort_label(user_doc):
    """Returns 'A' / 'B' / 'C' / 'entry_level' for a student user doc."""
    cohort = (user_doc or {}).get("cohort")
    return cohort if cohort in VALID_COHORTS else ENTRY_LEVEL


# ------------------------------------------------------------
# Placement Rules — SINGLE SOURCE OF TRUTH for cohort thresholds.
# Configured by the Super Admin, stored in db.placement_rules, and
# read fresh on every calculation by every module (student, trainer,
# super_admin). Nothing in this codebase hardcodes a score range.
# ------------------------------------------------------------
def get_placement_rules(db):
    """Fetch the active Placement Rules doc, seeding a sane default
    exactly once if the collection has never been configured."""
    rules = db.placement_rules.find_one({"active": True}, sort=[("updatedAt", -1)])
    if rules:
        return rules
    seed = dict(_DEFAULT_PLACEMENT_RULES_SEED)
    seed["active"] = True
    seed["createdAt"] = now()
    seed["updatedAt"] = now()
    seed["updatedBy"] = "system_default_seed"
    result = db.placement_rules.insert_one(seed)
    seed["_id"] = result.inserted_id
    return seed


def cohort_from_score(db, final_score):
    """Final Employability Score -> cohort, using whatever ranges are
    CURRENTLY configured in db.placement_rules (fetched fresh, every call)."""
    rules = get_placement_rules(db)
    for band in sorted(rules.get("cohortRanges", []), key=lambda b: -b["min"]):
        if band["min"] <= final_score <= band.get("max", 100):
            return band["cohort"]
    # Score fell outside every configured band (e.g. gap in ranges) —
    # fall back to the lowest-bound cohort rather than silently failing.
    bands = rules.get("cohortRanges", [])
    return min(bands, key=lambda b: b["min"])["cohort"] if bands else "C"


def compute_final_employability_score(db, assessment_percentage, interview_percentage):
    """Weighted combination of assessment score + interview score, using
    the weights currently configured in db.placement_rules."""
    rules = get_placement_rules(db)
    a_weight = rules.get("assessmentWeight", 0.5)
    i_weight = rules.get("interviewWeight", 0.5)
    total_weight = a_weight + i_weight or 1
    final_score = (
        (assessment_percentage * a_weight) + (interview_percentage * i_weight)
    ) / total_weight
    return round(final_score, 2)


def record_assessment_score_for_cohort(db, student_id, assessment_type, percentage):
    """
    Step 1 of cohort generation. Called after ANY assessment is scored.
    Stores the score on the student record when it's a baseline assessment,
    but does NOT assign a cohort yet — cohort generation additionally
    requires the manual interview score (see check_and_generate_cohort).
    """
    if assessment_type != "baseline":
        return
    db.users.update_one(
        {"_id": student_id},
        {"$set": {
            "baselineAssessmentScore": percentage,
            "baselineAssessmentScoredAt": now(),
        }},
    )
    check_and_generate_cohort(db, student_id)


def record_interview_score_for_cohort(db, student_id, interview_percentage):
    """Step 2 of cohort generation. Called when a manual interview is
    scored. Stores the score, then checks whether cohort generation
    can now proceed."""
    db.users.update_one(
        {"_id": student_id},
        {"$set": {
            "interviewScore": interview_percentage,
            "interviewScoredAt": now(),
        }},
    )
    check_and_generate_cohort(db, student_id)


def check_and_generate_cohort(db, student_id):
    """
    Cohort is generated ONLY when BOTH the assessment score and the
    manual interview score exist. Until then the student stays Entry
    Level, sees no cohort-specific assessments, and has no cohort.

    Workflow: assessment score + interview score -> weighted Final
    Employability Score (weights from Placement Rules) -> cohort
    (ranges from Placement Rules) -> assigned to student.

    Returns the new cohort string, or None if generation didn't happen
    (either already assigned, or one of the two inputs is still missing).
    """
    student = db.users.find_one({"_id": student_id})
    if not student or student.get("cohort") in VALID_COHORTS:
        return None  # already has a real cohort — rule doesn't re-fire

    assessment_score = student.get("baselineAssessmentScore")
    interview_score = student.get("interviewScore")
    if assessment_score is None or interview_score is None:
        return None  # still missing one of the two required inputs

    final_score = compute_final_employability_score(db, assessment_score, interview_score)
    new_cohort = cohort_from_score(db, final_score)

    db.users.update_one(
        {"_id": student_id},
        {"$set": {
            "cohort": new_cohort,
            "finalEmployabilityScore": final_score,
            "cohortAssignedAt": now(),
            "cohortAssignedFrom": "assessment_plus_interview",
        }},
    )
    return new_cohort


RESULT_STATUS_PENDING = "Interview Pending"
RESULT_STATUS_INTERVIEW_DONE = "Interview Completed"
RESULT_STATUS_VALIDATED = "Validated"


def init_quiz_result_fields():
    """Fields merged onto a quiz_attempts doc the moment a quiz is
    submitted, so every submitted attempt is immediately a well-formed
    'marks' record — never a partial/undefined shape."""
    return {
        "interviewMarks": None,
        "finalAverage": None,
        "assignedCohort": None,
        "resultStatus": RESULT_STATUS_PENDING,
        "interviewEnteredBy": None,
        "interviewEnteredAt": None,
        "validatedBy": None,
        "validatedAt": None,
    }


def serialize_quiz_result(db, attempt, quiz=None):
    """One row of Marks Management data — shape shared by Student 'My
    Results', Trainer/Super Admin 'Assessment Responses', 'Interview
    Verification' and 'Validation Verification'. Every field comes
    straight from the stored attempt document; nothing is recomputed
    on the fly except quiz title fallback."""
    overall = attempt.get("overall") or {}
    if quiz is None:
        quiz = db.quizzes.find_one({"_id": attempt.get("quizId")}) or {}
    submitted_at = attempt.get("submittedAt")
    entered_at = attempt.get("interviewEnteredAt")
    validated_at = attempt.get("validatedAt")
    return {
        "id": str(attempt["_id"]),
        "attemptId": str(attempt["_id"]),
        "studentId": str(attempt["studentId"]),
        "studentName": attempt.get("studentName"),
        "rollNumber": attempt.get("studentRollNumber"),
        "department": attempt.get("department"),
        "college": attempt.get("college"),
        "quizId": str(attempt["quizId"]),
        "quizTitle": attempt.get("quizTitle") or quiz.get("title"),
        "obtainedMarks": overall.get("marksObtained"),
        "totalMarks": overall.get("totalMarks"),
        "percentageMarks": overall.get("percentage"),
        "submittedAt": submitted_at.isoformat() if submitted_at else None,
        "interviewMarks": attempt.get("interviewMarks"),
        "finalAverage": attempt.get("finalAverage"),
        "cohort": attempt.get("assignedCohort"),
        "status": attempt.get("resultStatus") or RESULT_STATUS_PENDING,
        "interviewEnteredAt": entered_at.isoformat() if entered_at else None,
        "validatedAt": validated_at.isoformat() if validated_at else None,
    }


def compute_quiz_analytics(db, college=None, cohort="all", quiz_id="all"):
    """Dynamic analytics for the Quiz Responses page — Student
    Participation, Performance Metrics, Leaderboard (top 10) and
    Section-wise Performance — computed live from db.quiz_attempts (and,
    for section-wise, each quiz's own question pool). No hardcoded
    values; recomputed on every call rather than cached, so it always
    reflects the latest submissions.

    college=None means unscoped (Super Admin, sees every college); pass
    a college name to scope everything to it (Trainer). cohort/quiz_id
    of "all" mean unfiltered on that dimension — same semantics either
    caller uses, so Super Admin and Trainer can never compute this
    differently from each other.
    """
    query = {"status": "submitted"}
    if college:
        query["college"] = college
    if cohort and cohort != "all":
        query["cohort"] = cohort
    if quiz_id and quiz_id != "all":
        oid = to_object_id(quiz_id)
        if oid is not None:
            query["quizId"] = oid

    submitted = list(db.quiz_attempts.find(query))

    # Students Attempted / Yet To Attempt — scoped to the same
    # college/cohort filters, over every registered student.
    student_query = {"role": "student"}
    if college:
        student_query["college"] = college
    if cohort and cohort != "all":
        if cohort == ENTRY_LEVEL:
            student_query["$or"] = [{"cohort": None}, {"cohort": {"$exists": False}}]
        else:
            student_query["cohort"] = cohort
    total_students = db.users.count_documents(student_query)
    attempted_ids = {str(a["studentId"]) for a in submitted if a.get("studentId")}
    students_attempted = len(attempted_ids)
    students_yet_to_attempt = max(0, total_students - students_attempted)

    # Performance Metrics
    scores = [a.get("overall", {}).get("percentage") for a in submitted
              if a.get("overall", {}).get("percentage") is not None]
    average_score = round(sum(scores) / len(scores), 2) if scores else 0
    highest_score = max(scores) if scores else 0
    lowest_score = min(scores) if scores else 0

    # Leaderboard — top 10 by score
    ranked = sorted(submitted, key=lambda a: a.get("overall", {}).get("percentage") or 0, reverse=True)[:10]
    leaderboard = [{
        "studentName": a.get("studentName"),
        "college": a.get("college"),
        "score": a.get("overall", {}).get("percentage"),
    } for a in ranked]

    # Section-wise Performance — recomputed read-only from each quiz's
    # question pool + the stored answers, without touching the student
    # quiz-submission flow itself.
    section_totals, section_correct = {}, {}
    quizzes_cache = {}
    for a in submitted:
        qid = a.get("quizId")
        if qid not in quizzes_cache:
            quizzes_cache[qid] = db.quizzes.find_one({"_id": qid}) or {}
        pool = quizzes_cache[qid].get("questions") or []
        answers = a.get("answers") or {}
        for pq in (a.get("pooledQuestions") or []):
            idx = pq.get("poolIndex")
            if idx is None or idx >= len(pool):
                continue
            q = pool[idx]
            section = q.get("section") or "General"
            section_totals[section] = section_totals.get(section, 0) + 1
            raw = answers.get(str(idx))
            given = raw if isinstance(raw, list) else ([raw] if raw not in (None, "") else [])
            try:
                given_idx = sorted({int(x) for x in given})
            except (TypeError, ValueError):
                given_idx = []
            correct_idx = sorted(set(q.get("correct") or []))
            if given_idx and given_idx == correct_idx:
                section_correct[section] = section_correct.get(section, 0) + 1

    section_performance = []
    for section, total in sorted(section_totals.items()):
        correct = section_correct.get(section, 0)
        pct = round((correct / total) * 100, 2) if total else 0
        section_performance.append({"section": section, "percentage": pct})

    return {
        "studentsAttempted": students_attempted,
        "studentsYetToAttempt": students_yet_to_attempt,
        "averageScore": average_score,
        "highestScore": highest_score,
        "lowestScore": lowest_score,
        "leaderboard": leaderboard,
        "sectionPerformance": section_performance,
    }


def list_quiz_responses(db, college=None, cohort="all", quiz_id="all", limit=200):
    """Raw per-attempt rows for the Quiz Responses table — the same
    db.quiz_attempts query compute_quiz_analytics() aggregates, just
    returned as individual rows instead of summarized. Shared so the
    table and the analytics above can never disagree about which
    attempts are in scope.
    """
    query = {"status": "submitted"}
    if college:
        query["college"] = college
    if cohort and cohort != "all":
        query["cohort"] = cohort
    if quiz_id and quiz_id != "all":
        oid = to_object_id(quiz_id)
        if oid is not None:
            query["quizId"] = oid

    cursor = db.quiz_attempts.find(query).sort("submittedAt", -1).limit(int(limit))
    rows = []
    for a in cursor:
        rows.append({
            "attemptId": str(a["_id"]),
            "studentName": a.get("studentName"),
            "college": a.get("college"),
            "department": a.get("department"),
            "cohort": a.get("cohort") or ENTRY_LEVEL,
            "quizId": str(a["quizId"]) if a.get("quizId") else None,
            "assessmentName": a.get("quizTitle"),
            "overallPercentage": a.get("overall", {}).get("percentage"),
            "status": "Completed",
            "submittedAt": a["submittedAt"].isoformat() if a.get("submittedAt") else None,
        })
    return rows


def list_quiz_results(db, college=None, student_id=None, statuses=None, limit=500):
    """Submitted Create-Quiz attempts, newest first, optionally scoped to
    one college (Trainer) or left unscoped (Super Admin sees everyone).
    Never returns an attempt that hasn't actually been submitted — a
    student who never finished a quiz never appears anywhere here."""
    query = {"status": "submitted"}
    if college:
        query["college"] = college
    if student_id is not None:
        query["studentId"] = student_id
    if statuses:
        query["resultStatus"] = {"$in": list(statuses)}
    cursor = db.quiz_attempts.find(query).sort("submittedAt", -1).limit(int(limit))
    quizzes_cache = {}
    rows = []
    for attempt in cursor:
        qid = attempt.get("quizId")
        if qid not in quizzes_cache:
            quizzes_cache[qid] = db.quizzes.find_one({"_id": qid}) or {}
        rows.append(serialize_quiz_result(db, attempt, quiz=quizzes_cache[qid]))
    return rows


def get_quiz_result_or_none(db, attempt_id, college=None):
    """Look up a single submitted quiz_attempts doc, optionally enforcing
    that it belongs to a given college (trainer scoping)."""
    query = {"_id": attempt_id, "status": "submitted"}
    if college:
        query["college"] = college
    return db.quiz_attempts.find_one(query)


def set_quiz_interview_marks(db, attempt_id, marks, entered_by, college=None):
    """
    Trainer/Super Admin action: enter (or update) interview marks (0-100)
    for one quiz result. Immediately recomputes:
        Final Average = (Quiz Percentage + Interview Marks) / 2
    and assigns a cohort using the CURRENT db.placement_rules ranges
    (never hardcoded). Returns (updated_attempt_doc, error_message).
    Re-calling this (Super Admin "update interview marks") simply
    overwrites the previous marks/average/cohort with the new values.
    """
    attempt = get_quiz_result_or_none(db, attempt_id, college=college)
    if not attempt:
        return None, "Quiz result not found, or you do not have access to it."
    if marks is None or isinstance(marks, bool) or not isinstance(marks, (int, float)):
        return None, "Interview marks are required and must be a number."
    if marks < 0 or marks > 100:
        return None, "Interview marks must be between 0 and 100."

    quiz_percentage = (attempt.get("overall") or {}).get("percentage") or 0.0
    final_average = round((float(quiz_percentage) + float(marks)) / 2, 2)
    cohort = cohort_from_score(db, final_average)

    db.quiz_attempts.update_one(
        {"_id": attempt_id},
        {"$set": {
            "interviewMarks": round(float(marks), 2),
            "finalAverage": final_average,
            "assignedCohort": cohort,
            "resultStatus": RESULT_STATUS_INTERVIEW_DONE,
            "interviewEnteredBy": str(entered_by) if entered_by is not None else None,
            "interviewEnteredAt": now(),
        }},
    )
    return db.quiz_attempts.find_one({"_id": attempt_id}), None


def validate_quiz_result(db, attempt_id, validated_by, college=None):
    """Trainer/Super Admin confirms a fully-scored result (Interview
    Completed -> Validated). Cannot validate a result that hasn't had
    interview marks entered yet."""
    attempt = get_quiz_result_or_none(db, attempt_id, college=college)
    if not attempt:
        return None, "Quiz result not found, or you do not have access to it."
    if attempt.get("resultStatus") not in (RESULT_STATUS_INTERVIEW_DONE, RESULT_STATUS_VALIDATED):
        return None, "Interview marks must be entered before this result can be validated."

    db.quiz_attempts.update_one(
        {"_id": attempt_id},
        {"$set": {
            "resultStatus": RESULT_STATUS_VALIDATED,
            "validatedBy": str(validated_by) if validated_by is not None else None,
            "validatedAt": now(),
        }},
    )
    return db.quiz_attempts.find_one({"_id": attempt_id}), None


def cohort_counts(db, college=None):
    """Shared by Super Admin + Trainer dashboards: counts of students in
    Cohort A / B / C / Entry Level. `college` optionally scopes to one college
    (trainers only see their own college's students)."""
    match = {"role": "student"}
    if college:
        match["college"] = college
    pipeline = [
        {"$match": match},
        {"$group": {"_id": {"$ifNull": ["$cohort", ENTRY_LEVEL]}, "count": {"$sum": 1}}},
    ]
    counts = {"A": 0, "B": 0, "C": 0, ENTRY_LEVEL: 0}
    for row in db.users.aggregate(pipeline):
        key = row["_id"] if row["_id"] in VALID_COHORTS else ENTRY_LEVEL
        counts[key] = counts.get(key, 0) + row["count"]
    return counts


def cohort_query_for_target(cohort_target):
    """Turn an assessment's cohortTarget into a Mongo filter fragment for
    matching eligible students in db.users."""
    if cohort_target == "all":
        return {}
    if cohort_target == ENTRY_LEVEL:
        return {"$or": [{"cohort": None}, {"cohort": {"$exists": False}}]}
    if cohort_target in VALID_COHORTS:
        return {"cohort": cohort_target}
    return {"_id": None}  # invalid target matches nobody


def student_matches_cohort_target(student_doc, cohort_target):
    if cohort_target == "all":
        return True
    if cohort_target == ENTRY_LEVEL:
        return student_cohort_label(student_doc) == ENTRY_LEVEL
    return student_doc.get("cohort") == cohort_target


# ------------------------------------------------------------
# Master Excel workbook parsing (ONE upload -> many sheets -> question bank)
# ------------------------------------------------------------
QUESTION_COL_ALIASES = {
    "question": {"question", "questions", "question text", "questiontext"},
    "option_a": {"option a", "optiona", "a", "option1"},
    "option_b": {"option b", "optionb", "b", "option2"},
    "option_c": {"option c", "optionc", "c", "option3"},
    "option_d": {"option d", "optiond", "d", "option4"},
    "answer": {"answer", "correct answer", "correctanswer", "correct option", "correct"},
    "difficulty": {"difficulty", "level"},
}


def _match_column(header_cells):
    """Map a header row (list of cell values) to column-index-by-role."""
    mapping = {}
    for idx, cell in enumerate(header_cells):
        if cell is None:
            continue
        key = str(cell).strip().lower()
        for role, aliases in QUESTION_COL_ALIASES.items():
            if key in aliases:
                mapping[role] = idx
                break
    return mapping


def parse_master_workbook(file_stream, uploaded_by):
    """
    Parse ONE master Excel workbook (openpyxl). Every sheet is a section
    (e.g. Communication, Programming, Reasoning, Professionalism,
    Career Readiness). Each sheet has ~100 rows of questions.

    Returns (questions: list[dict ready for insert_many], errors: list[str]).
    Nothing is written to the DB here — the caller decides whether to persist.
    """
    from openpyxl import load_workbook

    wb = load_workbook(file_stream, read_only=True, data_only=True)
    questions = []
    errors = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        section = sheet_name.strip()
        rows = ws.iter_rows(values_only=True)
        try:
            header = next(rows)
        except StopIteration:
            continue

        col_map = _match_column(list(header))
        if "question" not in col_map or "answer" not in col_map:
            errors.append(
                f"Sheet '{sheet_name}': could not find required 'Question' and "
                f"'Answer' columns — sheet skipped."
            )
            continue

        for row_idx, row in enumerate(rows, start=2):
            if row is None or all(c is None for c in row):
                continue
            question_text = row[col_map["question"]] if col_map.get("question") is not None else None
            if not question_text or not str(question_text).strip():
                continue

            options = []
            for opt_key in ("option_a", "option_b", "option_c", "option_d"):
                if opt_key in col_map and col_map[opt_key] < len(row):
                    val = row[col_map[opt_key]]
                    if val is not None and str(val).strip():
                        options.append(str(val).strip())

            answer = row[col_map["answer"]] if col_map.get("answer") is not None else None
            if answer is None or not str(answer).strip():
                errors.append(f"Sheet '{sheet_name}' row {row_idx}: missing answer — skipped.")
                continue

            difficulty = None
            if "difficulty" in col_map and col_map["difficulty"] < len(row):
                d_val = row[col_map["difficulty"]]
                if d_val is not None and str(d_val).strip():
                    difficulty = str(d_val).strip().lower()

            questions.append({
                "section": section,
                "questionText": str(question_text).strip(),
                "options": options,
                "correctAnswer": str(answer).strip(),
                "difficulty": difficulty,
                "createdAt": now(),
                "createdBy": uploaded_by,
                "active": True,
            })

        if not any(q["section"] == section for q in questions) and not errors:
            errors.append(f"Sheet '{sheet_name}': no valid questions found.")

    return questions, errors


# ------------------------------------------------------------
# Random Question Engine
# ------------------------------------------------------------
def select_random_questions(db, section_counts):
    """
    section_counts: {"Communication": 10, "Programming": 15, ...}
    Returns (selected_questions: list[dict], shortage: dict section->deficit)
    Uses MongoDB's $sample for true random, non-sequential selection —
    a fresh random draw every time this is called (i.e. every student
    attempt gets its own random set).
    """
    selected = []
    shortage = {}
    for section, count in section_counts.items():
        if not count or count <= 0:
            continue
        pipeline = [
            {"$match": {"section": section, "active": True}},
            {"$sample": {"size": int(count)}},
        ]
        docs = list(db.questions.aggregate(pipeline))
        if len(docs) < count:
            shortage[section] = count - len(docs)
        selected.extend(docs)
    random.shuffle(selected)
    return selected, shortage


def select_random_questions_v2(db, assessment):
    """
    Preferred selection entry point for student.start_assessment().

    If the assessment document defines a difficultyDistribution, e.g.
    {"easy": 10, "medium": 10, "hard": 5}, this draws exactly that many
    questions per difficulty level — independently shuffled per level,
    then the merged set shuffled again — mirroring quiz_module's
    difficulty-based draw for the separate manually-authored-quiz
    feature, so both systems behave identically for this kind of config.

    Today, the Trainer/Super Admin "Create Assessment" wizard has no
    difficultyDistribution field (only sectionCounts), so every existing
    assessment falls through to the original select_random_questions()
    above completely unchanged. This function only activates the
    difficulty-based path the moment such a field is added to an
    assessment document — no other code needs to change when that
    happens.
    """
    dist = assessment.get("difficultyDistribution")
    if not dist:
        return select_random_questions(db, assessment.get("sectionCounts", {}))

    selected = []
    shortage = {}
    for level in ("easy", "medium", "hard"):
        count = int(dist.get(level) or 0)
        if count <= 0:
            continue
        pipeline = [
            {"$match": {"difficulty": level, "active": True}},
            {"$sample": {"size": count}},
        ]
        docs = list(db.questions.aggregate(pipeline))
        if len(docs) < count:
            shortage[level] = count - len(docs)
        selected.extend(docs)
    random.shuffle(selected)
    return selected, shortage


def strip_answers(questions):
    """Student-facing question payload: never leak correctAnswer to the client."""
    safe = []
    for q in questions:
        safe.append({
            "id": str(q["_id"]),
            "section": q["section"],
            "questionText": q["questionText"],
            "options": q.get("options", []),
            "difficulty": q.get("difficulty"),
        })
    return safe


# ------------------------------------------------------------
# Scoring
# ------------------------------------------------------------
def calculate_score(db, question_ids, answers):
    """
    question_ids: list[ObjectId] — the exact questions this attempt used.
    answers: {question_id_str: submitted_answer_str}
    Returns dict: {
        sectionScores: {section: {"correct": n, "total": n, "percentage": p}},
        overall: {"correct": n, "total": n, "percentage": p, "attempted": n,
                  "wrong": n, "unanswered": n}
    }
    """
    questions = list(db.questions.find({"_id": {"$in": question_ids}}))
    section_totals = {}
    section_correct = {}
    total = 0
    correct = 0
    attempted = 0
    unanswered = 0

    for q in questions:
        section = q["section"]
        qid = str(q["_id"])
        section_totals[section] = section_totals.get(section, 0) + 1
        total += 1
        submitted = answers.get(qid)
        if submitted is None or str(submitted).strip() == "":
            unanswered += 1
            continue
        attempted += 1
        is_correct = str(submitted).strip().lower() == str(q["correctAnswer"]).strip().lower()
        if is_correct:
            section_correct[section] = section_correct.get(section, 0) + 1
            correct += 1

    section_scores = {}
    for section, sec_total in section_totals.items():
        sec_correct = section_correct.get(section, 0)
        pct = round((sec_correct / sec_total) * 100, 2) if sec_total else 0.0
        section_scores[section] = {"correct": sec_correct, "total": sec_total, "percentage": pct}

    overall_pct = round((correct / total) * 100, 2) if total else 0.0
    return {
        "sectionScores": section_scores,
        "overall": {
            "correct": correct,
            "total": total,
            "percentage": overall_pct,
            "attempted": attempted,
            "wrong": attempted - correct,
            "unanswered": unanswered,
        },
    }


# ------------------------------------------------------------
# Activity Logging — single source of truth for every "Recent
# Activity" panel across Student / Trainer / Super Admin. Call
# log_activity() right after any state-changing action succeeds;
# read it back with get_recent_activity().
# ------------------------------------------------------------
def log_activity(db, actor_id, actor_role, action, description, college=None, student_id=None, meta=None):
    """
    actor_id:    the ObjectId/str of whoever performed the action (trainer,
                 student, or super admin). Stored as string for easy querying.
    actor_role:  'student' | 'trainer' | 'super_admin'
    action:      short machine-readable code, e.g. 'assessment_created',
                 'assessment_published', 'assessment_submitted',
                 'manual_interview_scheduled', 'manual_interview_scored',
                 'report_downloaded', 'profile_updated', 'password_changed'.
    description: human-readable sentence for display, e.g.
                 'Created assessment "Communication Baseline"'.
    college:     used to scope trainer/admin "Recent Activity" queries.
    student_id:  if this activity is about a specific student (e.g. a
                 trainer scoring their interview), stored so the STUDENT's
                 own Recent Activity can also show it.
    meta:        free-form dict with extra ids (assessmentId, interviewId...).
    """
    db.activity_log.insert_one({
        "actorId": str(actor_id) if actor_id is not None else None,
        "actorRole": actor_role,
        "action": action,
        "description": description,
        "college": college,
        "studentId": str(student_id) if student_id is not None else None,
        "meta": meta or {},
        "createdAt": now(),
    })


def get_recent_activity(db, query=None, limit=20):
    """Returns newest-first activity rows, serialized for direct JSON use."""
    cursor = db.activity_log.find(query or {}).sort("createdAt", -1).limit(limit)
    rows = []
    for d in cursor:
        rows.append({
            "id": str(d["_id"]),
            "action": d.get("action"),
            "description": d.get("description"),
            "actorRole": d.get("actorRole"),
            "meta": d.get("meta", {}),
            "createdAt": d["createdAt"].isoformat() if d.get("createdAt") else None,
        })
    return rows
