"""
============================================================
 superadmin.py — Super Admin Dashboard backend
============================================================
Registered at /api/admin. Covers everything the Super Admin
Dashboard needs, all backed by real MongoDB data (no hardcoded
sample values):

  - Master Excel question-bank upload (ONE workbook, many sheets)
  - Assessment creation (incl. the new "Entry Level" cohort option
    and the random-question-count-per-section selector)
  - Assessment list (feeds the Assessment Results page dropdown)
  - Quiz Management / Quiz Responses stats, with dynamic Quiz Responses
    analytics (participation, performance, leaderboard, section-wise)
  - Cohort breakdown (A / B / C / Entry Level) — uses the SAME
    cohort helpers as trainer.py so the two dashboards can never
    disagree about who is in which cohort.
  - Dynamic charts: Skill Radar, Score by Category, Overall Score,
    Category Percentage, Performance Trends — all computed live
    from db.assessment_attempts.

Only Super Admin (role="super_admin") can call these routes.
"""

import logging
import re
from datetime import datetime

from flask import Blueprint, request, send_file
from flask_jwt_extended import get_jwt_identity
from pymongo.errors import PyMongoError, OperationFailure, ConfigurationError

# Dedicated logger for this module, same pattern as quiz_module.py /
# question_bank.py — goes through whatever handler/level app.py
# configures, so Save Rules failures are always visible server-side
# even when the HTTP response has to stay generic for the client.
logger = logging.getLogger("superadmin")

from quiz_common import (
    ok, error, role_required, now, to_object_id, serialize, iso_utc,
    VALID_COHORT_TARGETS, VALID_COHORTS, ENTRY_LEVEL,
    parse_master_workbook, cohort_counts,
    get_placement_rules, top_cohort_label, student_cohort_label,
    compute_cohort_recalculation_ops,
    list_quiz_results, set_quiz_interview_marks, validate_quiz_result,
    serialize_quiz_result, RESULT_STATUS_INTERVIEW_DONE, RESULT_STATUS_VALIDATED,
    compute_quiz_analytics, list_quiz_responses,
    list_distinct_departments, build_quiz_responses_workbook,
    backfill_student_cohorts,
    log_activity,
)
from colleges import resolve_active_college, resolve_active_department, _generate_temp_password

# Roles managed from the Super Admin "User Management" page. Super Admin
# itself is the single hardcoded account (see login.py) and never appears
# here — nothing to activate/deactivate/delete for it.
MANAGED_USER_ROLES = ["student", "trainer", "college_admin"]

ROLE_ALIASES = {
    "student": "student",
    "students": "student",
    "trainer": "trainer",
    "trainers": "trainer",
    "college_admin": "college_admin",
    "collegeadmin": "college_admin",
    "college admin": "college_admin",
    "college_admins": "college_admin",
}


# ==============================================================
# SCHEDULE SESSION — Super Admin "Schedule Session" module.
# Backing collection: db.workshop_sessions. Every dropdown (College,
# Department, Trainer) is resolved server-side against the real
# db.colleges / db.departments / db.users collections — nothing here
# is ever accepted as a free-text/hardcoded value from the client, only
# ids that are re-validated on every write. Colleges/Departments/
# Trainers are all stored as arrays of ids (+ a denormalized array of
# display names, snapshotted at schedule time so the session's own
# history never changes retroactively if a college is later renamed).
# Designed so Student/Trainer/College Admin/Management Office session
# views can be added later purely as new read endpoints against this
# same collection — no schema change required (see spec item 12).
# ==============================================================
SESSION_COHORT_OPTIONS = ("All Cohorts", "Cohort A", "Cohort B", "Cohort C")
SESSION_ATTENDANCE_OPTIONS = ("mandatory", "optional")
SESSION_STATUS_OPTIONS = ("scheduled", "completed", "cancelled")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# ------------------------------------------------------------
# ATTENDANCE MODULE — Super Admin "Mark Attendance" / "View Records".
# Backing collection: db.attendance. One document per student per date
# (enforced by a unique index on (studentId, date) created in app.py),
# so re-marking the same student on the same date always updates the
# existing record instead of creating a duplicate. Every filter value
# (college / department / cohort / trainer) is resolved server-side
# against db.colleges / db.departments / db.users / db.attendance —
# nothing is hardcoded, nothing is accepted as free text.
# ==============================================================
ATTENDANCE_STATUSES = ("present", "absent")
ATTENDANCE_STATUS_LABELS = {"present": "Present", "absent": "Absent", "not_marked": "Not Yet Marked"}
# Cohort labels — derived from the platform's single cohort system
# (VALID_COHORTS + ENTRY_LEVEL), NOT a separate hardcoded list.
ATTENDANCE_COHORT_LABELS = {
    "A": "Cohort A – Placement Ready",
    "B": "Cohort B – Near Ready",
    "C": "Cohort C",
    ENTRY_LEVEL: "Entry Level",
}
_ATTENDANCE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_optional_id_list(raw):
    """Like _parse_id_list but a filter list that may be empty/absent
    (an empty selection means 'all'). Returns (object_ids, error)."""
    if raw is None or raw == "":
        return [], None
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    elif isinstance(raw, list):
        parts = [str(p) for p in raw]
    else:
        return None, "Invalid id list."
    seen = set()
    oids = []
    for item in parts:
        oid = to_object_id(item)
        if not oid:
            return None, "One of the selected ids is invalid."
        if oid not in seen:
            seen.add(oid)
            oids.append(oid)
    return oids, None


def _attendance_student_query(db, college_oids, department_oids, cohorts):
    """Mongo filter for students matching the Mark/View filters. An empty
    list on any dimension means 'no restriction' (i.e. all). Cohorts is a
    list of 'A'/'B'/'C'/'entry_level' values; 'all' disables the filter."""
    query = {
        "role": "student",
        "approvalStatus": {"$in": ["approved", "suspended"]},
        "isDeleted": {"$ne": True},
    }
    if college_oids:
        query["collegeId"] = {"$in": college_oids}
    if department_oids:
        query["departmentId"] = {"$in": department_oids}
    if cohorts and "all" not in cohorts:
        branches = []
        if ENTRY_LEVEL in cohorts:
            branches.append({"cohort": {"$nin": list(VALID_COHORTS)}})
        for c in cohorts:
            if c in VALID_COHORTS:
                branches.append({"cohort": c})
        if branches:
            query["$or"] = branches
    return query


def _attendance_student_public(doc):
    """Serialize a student user doc for the attendance mark/list views.
    cohortLabel uses the same labels the rest of the platform uses."""
    cohort = student_cohort_label(doc)
    return {
        "id": str(doc["_id"]),
        "name": doc.get("fullName") or doc.get("email") or "—",
        "rollNumber": doc.get("rollNumber") or "—",
        "college": doc.get("college") or "—",
        "collegeId": str(doc["collegeId"]) if doc.get("collegeId") else None,
        "department": doc.get("department") or "—",
        "departmentId": str(doc["departmentId"]) if doc.get("departmentId") else None,
        "cohort": cohort,
        "cohortLabel": ATTENDANCE_COHORT_LABELS.get(cohort, cohort),
    }


def _parse_id_list(raw, field_label):
    """Normalize a request field that must be a non-empty list of id
    strings. Returns (object_ids, error_message) — object_ids is a
    deduped list preserving order; error_message is None on success."""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return None, f"{field_label} must be a list of ids."
    if not raw:
        return None, f"Select at least one {field_label.lower()}."
    seen = set()
    oids = []
    for item in raw:
        oid = to_object_id(item)
        if not oid:
            return None, f"One of the selected {field_label.lower()} is invalid."
        if oid not in seen:
            seen.add(oid)
            oids.append(oid)
    return oids, None


def _resolve_colleges(db, college_ids):
    """Every id must correspond to an existing, active college. Returns
    (docs_by_id, error) — docs_by_id preserves the request order."""
    docs = {d["_id"]: d for d in db.colleges.find({"_id": {"$in": college_ids}, "status": "active"})}
    missing = [str(oid) for oid in college_ids if oid not in docs]
    if missing:
        return None, "One or more selected colleges is invalid or inactive."
    return [docs[oid] for oid in college_ids], None


def _resolve_departments(db, department_ids):
    """Returns (docs_in_request_order, college_names_by_id, error)."""
    docs = {d["_id"]: d for d in db.departments.find({"_id": {"$in": department_ids}, "status": "active"})}
    missing = [str(oid) for oid in department_ids if oid not in docs]
    if missing:
        return None, None, "One or more selected departments is invalid or inactive."
    college_names = {c["_id"]: c.get("college_name") for c in db.colleges.find({}, {"college_name": 1})}
    ordered = [docs[oid] for oid in department_ids]
    return ordered, college_names, None


def _resolve_trainers(db, trainer_ids):
    docs = {
        d["_id"]: d for d in db.users.find(
            {"_id": {"$in": trainer_ids}, "role": "trainer", "approvalStatus": {"$in": ["approved", "suspended"]}}
        )
    }
    missing = [str(oid) for oid in trainer_ids if oid not in docs]
    if missing:
        return None, "One or more selected trainers is invalid."
    return [docs[oid] for oid in trainer_ids], None


def _attendance_window_open(doc):
    """True only for a Mandatory session, and only while 'now' falls
    within that session's own Session Date + Start/End Time (spec §10).
    Optional sessions never expose an attendance window/button."""
    if doc.get("attendanceRequirement") != "mandatory":
        return False
    date_str, start, end = doc.get("date"), doc.get("startTime"), doc.get("endTime")
    if not (date_str and start and end):
        return False
    try:
        start_dt = datetime.strptime(f"{date_str} {start}", "%Y-%m-%d %H:%M")
        end_dt = datetime.strptime(f"{date_str} {end}", "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    return start_dt <= datetime.now() <= end_dt


def workshop_session_public(doc):
    if not doc:
        return None
    colleges = doc.get("collegeNames", [])
    departments = doc.get("departmentNames", [])
    trainers = doc.get("trainerNames", [])
    return {
        "id": str(doc["_id"]),
        "name": doc.get("name"),
        "collegeIds": [str(x) for x in doc.get("collegeIds", [])],
        "colleges": colleges,
        "college": ", ".join(colleges) or "—",
        "departmentIds": [str(x) for x in doc.get("departmentIds", [])],
        "departments": departments,
        "department": ", ".join(departments) or "—",
        "trainerIds": [str(x) for x in doc.get("trainerIds", [])],
        "trainers": trainers,
        "trainer": ", ".join(trainers) or "—",
        "cohort": doc.get("cohort"),
        "date": doc.get("date"),
        "startTime": doc.get("startTime"),
        "endTime": doc.get("endTime"),
        "time": f"{doc.get('startTime', '')} \u2013 {doc.get('endTime', '')}",
        "venue": doc.get("venue"),
        "attendanceRequirement": doc.get("attendanceRequirement"),
        "attendanceWindowOpen": _attendance_window_open(doc),
        "status": doc.get("status", "scheduled"),
        "createdBy": doc.get("createdBy"),
        "createdAt": iso_utc(doc.get("createdAt")),
        "updatedAt": iso_utc(doc.get("updatedAt")),
    }


def _user_public_row(doc):
    """Serialize a db.users document for the User Management table. Every
    field here is read straight off the real MongoDB document — no mock
    values. `college` is intentionally omitted for trainers: trainers are
    not directly associated with a single college on this platform, so the
    Trainer table should never show one (see colleges.py assign_trainer_college,
    which lets a trainer be linked to more than one college over time)."""
    role = doc.get("role")
    last_login = doc.get("lastLoginAt")
    created_at = doc.get("createdAt")
    is_active = doc.get("approvalStatus") == "approved"
    return {
        "id": str(doc["_id"]),
        "name": doc.get("fullName") or "—",
        "email": doc.get("email") or "—",
        "role": role,
        "college": doc.get("college") if role != "trainer" else None,
        "department": doc.get("department"),
        "rollNumber": doc.get("rollNumber"),
        "employeeId": doc.get("employeeId"),
        "collegeAdminId": doc.get("tneaCode") if role == "college_admin" else None,
        "status": "active" if is_active else "inactive",
        # Both keys populated: `lastLogin` is what the frontend table reads,
        # `lastLoginAt` (ISO, or null) is kept for anything that wants the
        # raw value. Never a dummy date — null means "never logged in".
        "lastLogin": iso_utc(last_login),
        "lastLoginAt": iso_utc(last_login),
        "createdAt": iso_utc(created_at),
    }


def init_superadmin(db, bcrypt=None):
    bp = Blueprint("superadmin", __name__)

    questions = db.questions
    assessments = db.assessments
    attempts = db.assessment_attempts
    users = db.users

    # ==========================================================
    # 1. MASTER EXCEL UPLOAD — ONE workbook, every sheet = a section
    # ==========================================================
    @bp.route("/questions/upload", methods=["POST"])
    @role_required("super_admin", "trainer")
    def upload_master_workbook():
        if "file" not in request.files:
            return error("No file uploaded. Attach the master Excel workbook as 'file'.")
        f = request.files["file"]
        if not f.filename:
            return error("Empty file upload.")
        if not f.filename.lower().endswith((".xlsx", ".xlsm")):
            return error("Only .xlsx / .xlsm workbooks are supported.")

        try:
            parsed_questions, parse_errors = parse_master_workbook(f.stream, uploaded_by="super_admin")
        except Exception as exc:  # noqa: BLE001 — surface parse failures to the admin
            return error(f"Could not read workbook: {exc}")

        if not parsed_questions:
            return error(
                "No valid questions were found in this workbook. "
                + " ".join(parse_errors) if parse_errors else
                "No valid questions were found in this workbook."
            )

        # Persist every parsed question — never keep in memory only.
        result = questions.insert_many(parsed_questions)

        section_counts = {}
        for q in parsed_questions:
            section_counts[q["section"]] = section_counts.get(q["section"], 0) + 1

        return ok({
            "insertedCount": len(result.inserted_ids),
            "sectionCounts": section_counts,
            "warnings": parse_errors,
        }, message=f"{len(result.inserted_ids)} questions stored across {len(section_counts)} sections.")

    @bp.route("/questions/stats", methods=["GET"])
    @role_required("super_admin", "trainer")
    def question_bank_stats():
        pipeline = [
            {"$match": {"active": True}},
            {"$group": {"_id": "$section", "count": {"$sum": 1}}},
        ]
        stats = {row["_id"]: row["count"] for row in questions.aggregate(pipeline)}
        return ok({"sectionCounts": stats, "totalQuestions": sum(stats.values())})

    # ==========================================================
    # 2. ASSESSMENT CREATION — includes the Entry Level cohort option
    #    and per-section random-question-count selection.
    # ==========================================================
    @bp.route("/assessments", methods=["POST"])
    @role_required("super_admin")
    def create_assessment():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        assessment_type = (data.get("type") or "custom").strip().lower()
        cohort_target = (data.get("cohortTarget") or "").strip()
        section_counts = data.get("sectionCounts") or {}
        scheduled_at = data.get("scheduledAt")
        available_from = data.get("availableFrom")
        available_to = data.get("availableTo")

        if not name:
            return error("Assessment name is required.")
        if cohort_target not in VALID_COHORT_TARGETS:
            return error(
                "cohortTarget must be one of: Cohort A, Cohort B, Cohort C, "
                "All Cohorts, or Entry Level (send as 'A' / 'B' / 'C' / 'all' / 'entry_level')."
            )
        if not isinstance(section_counts, dict) or not section_counts:
            return error("sectionCounts is required, e.g. {'Communication': 10, 'Programming': 15}.")
        for section, count in section_counts.items():
            if not isinstance(count, int) or count <= 0:
                return error(f"Question count for section '{section}' must be a positive integer.")

        # Optional college/department scoping — both dropdowns read from the
        # SAME colleges/departments collections as Student Registration and
        # Super Admin's College Management (one source of truth, no
        # duplicate/hardcoded data). Leaving collegeId blank targets every
        # college, matching the existing "All Cohorts" style behaviour.
        college_id = data.get("collegeId")
        department_id = data.get("departmentId")
        college_name = None
        department_name = None
        if college_id:
            college_doc = resolve_active_college(db, college_id)
            if not college_doc:
                return error("Selected college is invalid or inactive.")
            college_name = college_doc["college_name"]
            if department_id:
                department_doc = resolve_active_department(db, department_id, college_id)
                if not department_doc:
                    return error("Selected department is invalid or inactive for this college.")
                department_name = department_doc["department_name"]

        # Warn (not block) if the question bank can't fully satisfy the request yet —
        # the random engine will surface the actual shortage at attempt-start time too.
        doc = {
            "name": name,
            "type": assessment_type,  # baseline | mid | final | custom
            "cohortTarget": cohort_target,  # A | B | C | all | entry_level
            "sectionCounts": {k: int(v) for k, v in section_counts.items()},
            "totalQuestions": sum(int(v) for v in section_counts.values()),
            "scheduledAt": scheduled_at,
            "availableFrom": available_from,
            "availableTo": available_to,
            "college": college_name,
            "department": department_name,
            "createdBy": "super_admin",
            "createdAt": now(),
            "status": "active",
        }
        result = assessments.insert_one(doc)
        doc["_id"] = result.inserted_id
        return ok({"assessment": serialize(doc)}, message="Assessment created.", status=201)

    @bp.route("/assessments", methods=["GET"])
    @role_required("super_admin", "trainer", "student")
    def list_assessments():
        """Feeds the Assessment Results page dropdown — every new assessment
        appears here automatically, no code change required."""
        cursor = assessments.find({}).sort("createdAt", -1)
        return ok({"assessments": [serialize(a) for a in cursor]})

    @bp.route("/assessments/<assessment_id>", methods=["GET"])
    @role_required("super_admin", "trainer")
    def get_assessment(assessment_id):
        oid = to_object_id(assessment_id)
        if not oid:
            return error("Invalid assessment id.", 404)
        doc = assessments.find_one({"_id": oid})
        if not doc:
            return error("Assessment not found.", 404)
        return ok({"assessment": serialize(doc)})

    @bp.route("/assessments/<assessment_id>", methods=["DELETE"])
    @role_required("super_admin")
    def delete_assessment(assessment_id):
        oid = to_object_id(assessment_id)
        if not oid:
            return error("Invalid assessment id.", 404)
        result = assessments.delete_one({"_id": oid})
        if result.deleted_count == 0:
            return error("Assessment not found.", 404)
        return ok(message="Assessment deleted.")

    # ==========================================================
    # 3. COHORTS — Entry Level + A/B/C, shared logic with trainer.py
    # ==========================================================
    @bp.route("/cohorts/counts", methods=["GET"])
    @role_required("super_admin")
    def get_cohort_counts():
        return ok({"cohortCounts": cohort_counts(db)})

    @bp.route("/cohorts/students", methods=["GET"])
    @role_required("super_admin")
    def list_cohort_students():
        cohort = request.args.get("cohort", "").strip()
        query = {"role": "student"}
        if cohort == ENTRY_LEVEL:
            query["$or"] = [{"cohort": None}, {"cohort": {"$exists": False}}]
        elif cohort in {"A", "B", "C"}:
            query["cohort"] = cohort
        docs = users.find(query, {"passwordHash": 0}).sort("createdAt", -1)
        return ok({"students": [serialize(d) for d in docs]})

    # ==========================================================
    # 4. QUIZ MANAGEMENT / QUIZ RESPONSES — live stats + dynamic analytics
    # ==========================================================
    @bp.route("/quiz-management/summary", methods=["GET"])
    @role_required("super_admin")
    def quiz_management_summary():
        total_questions = questions.count_documents({"active": True})
        total_assessments = assessments.count_documents({})
        total_attempts = attempts.count_documents({})
        submitted_attempts = attempts.count_documents({"status": "submitted"})
        in_progress = attempts.count_documents({"status": "in_progress"})
        return ok({
            "totalQuestions": total_questions,
            "totalAssessments": total_assessments,
            "totalAttempts": total_attempts,
            "submittedAttempts": submitted_attempts,
            "inProgressAttempts": in_progress,
            "cohortCounts": cohort_counts(db),
        })

    @bp.route("/quiz-responses", methods=["GET"])
    @role_required("super_admin")
    def quiz_responses():
        """Raw per-attempt rows for the Super Admin Quiz Responses table.

        ROOT-CAUSE FIX: this used to run its own hand-rolled aggregation
        against db.assessment_attempts/db.assessments (the older, largely
        unused cohort-placement engine) with no College/Course/Cohort/
        search filters at all — which is also why the frontend's filter
        bar was doing its own (incorrect, unrequested) client-side
        filtering on top of it. Now shares the exact same
        list_quiz_responses() helper Trainer's identically-named endpoint
        uses, just unscoped by college unless one is explicitly requested,
        so the two dashboards can never disagree about what a "response"
        is or how it's filtered.
        """
        college = request.args.get("college") or "all"
        cohort = request.args.get("cohort") or "all"
        quiz_id = request.args.get("quizId") or "all"
        department = request.args.get("department") or "all"
        search = request.args.get("search") or ""
        return ok({"responses": list_quiz_responses(
            db,
            college=None if college == "all" else college,
            cohort=cohort, quiz_id=quiz_id, department=department, search=search,
        )})

    @bp.route("/quiz-responses/filters", methods=["GET"])
    @role_required("super_admin")
    def quiz_responses_filters():
        """Course/Department dropdown options for the Quiz Responses
        filter bar — distinct values actually in use across every
        college (or one college, if ?college= is given), read live from
        the database, never hardcoded."""
        college = request.args.get("college") or "all"
        return ok({"departments": list_distinct_departments(
            db, college=None if college == "all" else college,
        )})

    @bp.route("/quiz-responses/export", methods=["GET"])
    @role_required("super_admin")
    def quiz_responses_export():
        """Backend-generated .xlsx of exactly what the Quiz Responses
        table is currently showing — same filters, same query, same rows."""
        college = request.args.get("college") or "all"
        cohort = request.args.get("cohort") or "all"
        quiz_id = request.args.get("quizId") or "all"
        department = request.args.get("department") or "all"
        search = request.args.get("search") or ""
        rows = list_quiz_responses(
            db,
            college=None if college == "all" else college,
            cohort=cohort, quiz_id=quiz_id, department=department, search=search,
            limit=100000,
        )
        buf = build_quiz_responses_workbook(rows)
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="quiz_responses.xlsx",
        )

    @bp.route("/quiz-analytics", methods=["GET"])
    @role_required("super_admin")
    def quiz_analytics():
        """Dynamic analytics for the Quiz Responses page — Student
        Participation, Performance Metrics, Leaderboard (top 10) and
        Section-wise Performance. See quiz_common.compute_quiz_analytics
        for the shared computation Trainer's equivalent endpoint also
        uses, so the two dashboards can never disagree.
        """
        college = request.args.get("college") or "all"
        cohort = request.args.get("cohort") or "all"
        quiz_id = request.args.get("quizId") or "all"
        department = request.args.get("department") or "all"
        search = request.args.get("search") or ""
        return ok(compute_quiz_analytics(
            db,
            college=None if college == "all" else college,
            cohort=cohort,
            quiz_id=quiz_id,
            department=department,
            search=search,
        ))

    @bp.route("/assessments/responses", methods=["GET"])
    @role_required("super_admin")
    def assessment_responses_all():
        """Deprecated alias of /quiz-responses (kept for backward
        compatibility — nothing in this codebase calls it anymore, the
        frontend now calls /quiz-responses directly, matching Trainer's
        endpoint name). Shares the exact same filtered helper, so even if
        something external still calls this, it can never disagree with
        the main table.
        """
        college = request.args.get("college") or "all"
        cohort = request.args.get("cohort") or "all"
        quiz_id = request.args.get("quizId") or "all"
        department = request.args.get("department") or "all"
        search = request.args.get("search") or ""
        return ok({"responses": list_quiz_responses(
            db,
            college=None if college == "all" else college,
            cohort=cohort, quiz_id=quiz_id, department=department, search=search,
        )})

    # ==========================================================
    # MARKS MANAGEMENT — Create-Quiz results, Interview Verification,
    # and Validation Verification, platform-wide (no college filter).
    # Mirrors trainer.py's routes exactly so both dashboards can never
    # disagree; see the header comment in trainer.py for the full
    # rationale on why this is separate from /quiz-responses above.
    # ==========================================================
    @bp.route("/quiz-results", methods=["GET"])
    @role_required("super_admin")
    def quiz_results_all():
        """`search` matches Student Name / Roll Number / Register Number,
        resolved entirely by the database (see
        quiz_common._student_search_query) — same matching rules Trainer's
        identically-named endpoint uses."""
        search = request.args.get("search") or ""
        return ok({"results": list_quiz_results(db, search=search)})

    @bp.route("/quiz-interview-verification", methods=["GET"])
    @role_required("super_admin")
    def quiz_interview_verification_all():
        """`search` matches Assessment Name / Student Name / Roll Number /
        College / Department, resolved entirely by the database (see
        quiz_common._verification_search_query)."""
        search = request.args.get("search") or ""
        return ok({"results": list_quiz_results(db, search=search, broad_search=True)})

    @bp.route("/quiz-interview-verification/<attempt_id>/marks", methods=["POST"])
    @role_required("super_admin")
    def enter_quiz_interview_marks_admin(attempt_id):
        oid = to_object_id(attempt_id)
        if not oid:
            return error("Invalid quiz result id.", 404)
        data = request.get_json(silent=True) or {}
        updated, err = set_quiz_interview_marks(db, oid, data.get("marks"), get_jwt_identity())
        if err:
            return error(err)
        log_activity(
            db, get_jwt_identity(), "super_admin", "quiz_interview_scored",
            f'Entered interview marks for {updated.get("studentName", "a student")} '
            f'({updated.get("quizTitle", "a quiz")}) — {updated.get("interviewMarks")}%',
            college=updated.get("college"), student_id=updated.get("studentId"),
            meta={"attemptId": attempt_id, "finalAverage": updated.get("finalAverage"),
                  "cohort": updated.get("assignedCohort")},
        )
        return ok({"result": serialize_quiz_result(db, updated)}, message="Interview marks saved.")

    @bp.route("/quiz-validation-verification", methods=["GET"])
    @role_required("super_admin")
    def quiz_validation_verification_all():
        search = request.args.get("search") or ""
        return ok({"results": list_quiz_results(
            db, statuses=[RESULT_STATUS_INTERVIEW_DONE, RESULT_STATUS_VALIDATED],
            search=search, broad_search=True,
        )})

    @bp.route("/quiz-validation-verification/<attempt_id>/validate", methods=["POST"])
    @role_required("super_admin")
    def validate_quiz_result_route_admin(attempt_id):
        oid = to_object_id(attempt_id)
        if not oid:
            return error("Invalid quiz result id.", 404)
        updated, err = validate_quiz_result(db, oid, get_jwt_identity())
        if err:
            return error(err)
        log_activity(
            db, get_jwt_identity(), "super_admin", "quiz_result_validated",
            f'Validated result for {updated.get("studentName", "a student")} '
            f'({updated.get("quizTitle", "a quiz")}) — Cohort {updated.get("assignedCohort")}',
            college=updated.get("college"), student_id=updated.get("studentId"),
            meta={"attemptId": attempt_id},
        )
        return ok({"result": serialize_quiz_result(db, updated)}, message="Result validated.")

    # ==========================================================
    # PLACEMENT RULES — single source of truth for cohort thresholds.
    # Every module (student, trainer, super_admin) reads this same
    # collection via quiz_common.get_placement_rules(); nothing anywhere
    # else in the codebase hardcodes a score range or a weight.
    # ==========================================================
    @bp.route("/placement-rules", methods=["GET"])
    @role_required("super_admin", "trainer")
    def get_rules():
        return ok({"placementRules": serialize(get_placement_rules(db))})

    @bp.route("/placement-rules", methods=["PUT"])
    @role_required("super_admin")
    def update_rules():
        """
        Update the three cohort thresholds and/or the assessment/interview
        weighting. Every save is fully automatic: validate -> persist ->
        immediately recalculate every existing student's Final Employability
        Score and cohort against the brand-new rules -> those cohorts are
        visible everywhere else in the platform on their very next read,
        since every module reads db.placement_rules fresh via
        quiz_common.get_placement_rules() / cohort_from_score(). There is no
        opt-in "recalculate" flag and no background job — recalculation
        always happens as part of Save Rules.

        Body: {
          "placementReadyThreshold": 80,   // Marks >= this -> Cohort A
          "nearReadyThreshold": 50,        // Marks >= this -> Cohort B
          "highRiskThreshold": 50,         // Marks <  this -> Cohort C
          "assessmentWeight": 70,          // percentage, 0-100
          "interviewWeight": 30            // percentage, 0-100 — must sum to 100 with assessmentWeight
        }
        """
        data = request.get_json(silent=True) or {}

        def as_number(key):
            val = data.get(key)
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                raise ValueError(key)

        try:
            placement_ready = as_number("placementReadyThreshold")
            near_ready = as_number("nearReadyThreshold")
            high_risk = as_number("highRiskThreshold")
            assessment_weight = as_number("assessmentWeight")
            interview_weight = as_number("interviewWeight")
        except ValueError as bad_key:
            return error(f"'{bad_key}' must be a number.")

        current = get_placement_rules(db)

        # Merge onto the currently-saved rules so a partial update (e.g.
        # weights only) still validates against real, current values.
        effective_placement_ready = placement_ready if placement_ready is not None else current.get("placementReadyThreshold", 75)
        effective_near_ready = near_ready if near_ready is not None else current.get("nearReadyThreshold", 50)
        effective_high_risk = high_risk if high_risk is not None else current.get("highRiskThreshold", 50)
        effective_assessment_weight = assessment_weight if assessment_weight is not None else current.get("assessmentWeight", 50)
        effective_interview_weight = interview_weight if interview_weight is not None else current.get("interviewWeight", 50)

        # --- Validation -------------------------------------------------
        for label, val in (
            ("Placement Ready threshold", effective_placement_ready),
            ("Near Ready threshold", effective_near_ready),
            ("High Risk threshold", effective_high_risk),
        ):
            if val < 0 or val > 100:
                return error(f"{label} must be between 0 and 100.")

        if effective_placement_ready < effective_near_ready:
            return error("Placement Ready threshold must be greater than or equal to Near Ready threshold.")

        if round(effective_assessment_weight + effective_interview_weight, 2) != 100:
            return error("Assessment Weight and Interview Weight must add up to exactly 100.")

        # High Risk and Near Ready describe the same B/C boundary from
        # opposite sides ("Near Ready starts at X" == "High Risk ends at
        # X") — keep them in sync regardless of which field the Super
        # Admin actually edited.
        if high_risk is not None and near_ready is None:
            effective_near_ready = effective_high_risk
        else:
            effective_high_risk = effective_near_ready

        update_doc = {
            "updatedAt": now(),
            "updatedBy": "super_admin",
            "placementReadyThreshold": effective_placement_ready,
            "nearReadyThreshold": effective_near_ready,
            "highRiskThreshold": effective_high_risk,
            "assessmentWeight": effective_assessment_weight,
            "interviewWeight": effective_interview_weight,
        }
        # Snapshot of every field we're about to overwrite, so a failed
        # recalculation can be rolled back to exactly what was there before
        # (used only on the no-transaction-support fallback path below —
        # the transaction path rolls back automatically on any error).
        previous_rules_snapshot = {
            k: current.get(k) for k in (
                "placementReadyThreshold", "nearReadyThreshold", "highRiskThreshold",
                "assessmentWeight", "interviewWeight", "updatedAt", "updatedBy",
            )
        }

        def apply_rules_and_recalculate(session=None):
            """Save the new rules, then recalculate EVERY existing student
            against them in a handful of bulk_write() calls (no per-student
            find_one()/update_one() loop — see compute_cohort_recalculation_ops
            docstring). Runs either inside a Mongo transaction (preferred,
            atomic) or, on deployments without transaction support, directly
            — see the try/except below for how each path is made safe."""
            db.placement_rules.update_one(
                {"_id": current["_id"]}, {"$set": update_doc}, session=session,
            )
            updated_rules = db.placement_rules.find_one({"_id": current["_id"]}, session=session)
            users_ops, student_cohort_ops, recalculated_count = compute_cohort_recalculation_ops(
                db, updated_rules,
            )
            if users_ops:
                db.users.bulk_write(users_ops, ordered=False, session=session)
            if student_cohort_ops:
                db.student_cohort.bulk_write(student_cohort_ops, ordered=False, session=session)
            return updated_rules, recalculated_count

        def rollback_rules(reason):
            """Best-effort rollback: undo the rules write so the database
            is never left with new rules but stale, un-recalculated
            cohorts. Failure to roll back is logged but never masks the
            original error returned to the client."""
            try:
                db.placement_rules.update_one(
                    {"_id": current["_id"]}, {"$set": previous_rules_snapshot},
                )
            except Exception:
                logger.exception(
                    "Placement rules rollback itself failed after: %s", reason,
                )

        # --- Save + recalculate, atomically where the deployment supports it.
        # Any failure anywhere in this flow is logged with a full traceback
        # server-side, and always turned into a clear, specific JSON error
        # response — Save Rules must never surface a bare/empty 500.
        updated = recalculated = None
        try:
            try:
                with db.client.start_session() as txn_session:
                    with txn_session.start_transaction():
                        updated, recalculated = apply_rules_and_recalculate(session=txn_session)
            except (OperationFailure, ConfigurationError, NotImplementedError) as exc:
                # Transactions require a replica set / mongos. A standalone
                # MongoDB (no replica set) rejects them outright, and some
                # drivers/test doubles don't implement sessions at all —
                # in every one of these cases, fall back to a direct,
                # non-transactional apply. Atlas / any replica-set
                # deployment never hits this branch; the transaction above
                # already gives full atomic rollback there.
                logger.warning(
                    "Placement rules: transactions unavailable on this "
                    "deployment (%s: %s); falling back to a direct, "
                    "non-transactional save + recalculate.",
                    type(exc).__name__, exc,
                )
                updated, recalculated = apply_rules_and_recalculate(session=None)
        except PyMongoError as exc:
            logger.exception("Placement rules save/recalculate failed (database error).")
            rollback_rules(exc)
            return error(
                f"Could not save placement rules: a database error occurred ({exc}). "
                "Any partial changes were rolled back — please try again.", 500,
            )
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            # Anything unexpected (bad data shape, driver incompatibility,
            # etc.) still must not leak as a bare, message-less 500 — the
            # frontend has nothing useful to show the Super Admin otherwise.
            logger.exception("Placement rules save/recalculate failed (unexpected error).")
            rollback_rules(exc)
            return error(
                f"Could not save placement rules due to an unexpected server error: {exc}. "
                "Any partial changes were rolled back — please check server logs and try again.",
                500,
            )

        logger.info(
            "Placement rules updated by %s — %s student(s) recalculated.",
            get_jwt_identity(), recalculated,
        )
        return ok({"placementRules": serialize(updated), "recalculatedStudents": recalculated},
                   message="Placement rules updated.")

    @bp.route("/student-cohort/backfill", methods=["POST"])
    @role_required("super_admin")
    def student_cohort_backfill():
        """
        Cohort-mapping bug fix, Part 5 ("Synchronize Existing Data"): run
        this once after deploying the cohort-mapping fix to retroactively
        correct any student whose Create-Quiz result was Validated
        BEFORE that fix existed — their db.users.cohort (the field every
        quiz-eligibility check reads) never moved, even though their own
        Quiz History page correctly showed their assigned cohort all
        along. Also backfills db.student_cohort for anyone whose cohort
        was already correct but predates that collection.

        Safe to re-run any number of times — see
        quiz_common.backfill_student_cohorts()'s docstring for exactly
        why nothing here can regress an already-correct student.
        """
        result = backfill_student_cohorts(db)
        log_activity(
            db, get_jwt_identity(), "super_admin", "student_cohort_backfill",
            f"Ran cohort backfill — {result['promoted']} student(s) corrected, "
            f"{result['mirroredOnly']} mirrored into StudentCohort, "
            f"{result['checked']} candidate(s) checked.",
        )
        return ok(result, message=(
            f"Backfill complete — {result['promoted']} student(s) had their cohort corrected, "
            f"{result['mirroredOnly']} more were mirrored into the StudentCohort collection."
        ))

    # ==========================================================
    # 5. DYNAMIC CHARTS — Skill Radar, Category %, Overall, Trends
    # ==========================================================
    @bp.route("/dashboard/charts", methods=["GET"])
    @role_required("super_admin")
    def dashboard_charts():
        cohort = request.args.get("cohort")
        match = {"status": "submitted"}
        if cohort:
            pipeline_students = list(users.find(
                {"role": "student", **({"cohort": cohort} if cohort != ENTRY_LEVEL else
                                        {"$or": [{"cohort": None}, {"cohort": {"$exists": False}}]})},
                {"_id": 1},
            ))
            match["studentId"] = {"$in": [s["_id"] for s in pipeline_students]}

        # Skill Radar / Score by Category: average % per section across all submitted attempts
        section_agg = {}
        overall_scores = []
        trend_points = []
        for att in attempts.find(match).sort("submittedAt", 1):
            overall = att.get("overall", {})
            if "percentage" in overall:
                overall_scores.append(overall["percentage"])
                trend_points.append({
                    "date": iso_utc(att.get("submittedAt")),
                    "percentage": overall["percentage"],
                })
            for section, s in att.get("sectionScores", {}).items():
                bucket = section_agg.setdefault(section, {"sum": 0.0, "n": 0})
                bucket["sum"] += s.get("percentage", 0)
                bucket["n"] += 1

        skill_radar = {
            section: round(b["sum"] / b["n"], 2) if b["n"] else 0
            for section, b in section_agg.items()
        }
        overall_avg = round(sum(overall_scores) / len(overall_scores), 2) if overall_scores else 0

        return ok({
            "skillRadar": skill_radar,
            "scoreByCategory": skill_radar,
            "overallScore": overall_avg,
            "categoryPercentage": skill_radar,
            "performanceTrend": trend_points,
        })

    # ==========================================================
    # 6. USER MANAGEMENT — Students, Trainers, College Admins, all
    #    read live from db.users. No hardcoded records anywhere.
    # ==========================================================
    @bp.route("/users", methods=["GET"])
    @role_required("super_admin")
    def list_users():
        role_param = (request.args.get("role") or request.args.get("userType") or "all").strip().lower()
        status_param = (request.args.get("status") or "all").strip().lower()
        search = (request.args.get("search") or "").strip()

        if role_param in ("all", ""):
            roles = MANAGED_USER_ROLES
        elif role_param in ROLE_ALIASES:
            roles = [ROLE_ALIASES[role_param]]
        else:
            return error("Invalid role filter.")

        # Only real, approved-at-some-point accounts show up here — pending
        # applications live in the Verification queues, and soft-deleted
        # accounts never appear in any normal listing.
        query = {
            "role": {"$in": roles},
            "approvalStatus": {"$in": ["approved", "suspended"]},
            "isDeleted": {"$ne": True},
        }
        if status_param == "active":
            query["approvalStatus"] = "approved"
        elif status_param == "inactive":
            query["approvalStatus"] = "suspended"
        elif status_param not in ("all", ""):
            return error("status must be 'active' or 'inactive'.")

        if search:
            pattern = re.escape(search)
            query["$or"] = [
                {"fullName": {"$regex": pattern, "$options": "i"}},
                {"email": {"$regex": pattern, "$options": "i"}},
                {"rollNumber": {"$regex": pattern, "$options": "i"}},
                {"employeeId": {"$regex": pattern, "$options": "i"}},
                {"tneaCode": {"$regex": pattern, "$options": "i"}},
            ]

        docs = users.find(query).sort("fullName", 1)
        rows = [_user_public_row(d) for d in docs]
        return ok({"users": rows, "total": len(rows)})

    def _find_managed_user(user_id):
        oid = to_object_id(user_id)
        if not oid:
            return None, error("Invalid user id.", 404)
        doc = users.find_one({"_id": oid, "role": {"$in": MANAGED_USER_ROLES}})
        if not doc:
            return None, error("User not found.", 404)
        return doc, None

    # Activate / Deactivate — reuses the SAME approvalStatus field that
    # already gates login() ("approved" required to sign in; anything else
    # is rejected). Deactivating also clears currentSessionId so a user who
    # is mid-session is signed out immediately, not just blocked next time.
    @bp.route("/users/<user_id>/status", methods=["PATCH"])
    @role_required("super_admin")
    def set_user_status(user_id):
        user, err = _find_managed_user(user_id)
        if err:
            return err
        if user.get("isDeleted"):
            return error("This account has been deleted and can no longer be modified.")
        if user.get("approvalStatus") not in ("approved", "suspended"):
            return error("Only approved accounts can be activated or deactivated.")

        data = request.get_json(silent=True) or {}
        requested = (data.get("status") or "").strip().lower()
        if requested not in ("active", "inactive"):
            return error("status must be 'active' or 'inactive'.")

        is_activating = requested == "active"
        new_approval_status = "approved" if is_activating else "suspended"
        update = {"approvalStatus": new_approval_status, "updatedAt": now()}
        if not is_activating:
            update["currentSessionId"] = None
        users.update_one({"_id": user["_id"]}, {"$set": update})
        verb = "activated" if is_activating else "deactivated"
        log_activity(
            db, actor_id="super_admin", actor_role="super_admin",
            action=f"user_{verb}",
            description=f"{verb.capitalize()} {user.get('fullName') or user.get('email')}",
        )
        return ok(message=f"User {verb}.")

    # Soft delete — never a hard delete. Deleted users can never log in
    # (see login.py) and are excluded from every listing above by the
    # isDeleted:$ne:true filter, but the record itself is left in place
    # and fully recoverable (e.g. by clearing isDeleted directly in the
    # database) unless a genuine permanent-delete feature is added later.
    @bp.route("/users/<user_id>", methods=["DELETE"])
    @role_required("super_admin")
    def soft_delete_user(user_id):
        user, err = _find_managed_user(user_id)
        if err:
            return err
        if user.get("isDeleted"):
            return ok(message="User already deleted.")
        users.update_one(
            {"_id": user["_id"]},
            {"$set": {
                "isDeleted": True,
                "deletedAt": now(),
                "currentSessionId": None,
                "updatedAt": now(),
            }},
        )
        log_activity(
            db, actor_id="super_admin", actor_role="super_admin",
            action="user_deleted",
            description=f"Deleted {user.get('fullName') or user.get('email')}",
        )
        return ok(message="User deleted.")

    # Reset credentials — same random-temp-password pattern already used
    # for Trainers (see colleges.py admin_reset_trainer_password), now
    # available for every managed role from one place.
    @bp.route("/users/<user_id>/reset-password", methods=["POST"])
    @role_required("super_admin")
    def reset_user_password(user_id):
        user, err = _find_managed_user(user_id)
        if err:
            return err
        if user.get("isDeleted"):
            return error("This account has been deleted and can no longer be modified.")
        if bcrypt is None:
            return error("Password reset is not available right now.", 503)

        temp_password = _generate_temp_password()
        password_hash = bcrypt.generate_password_hash(temp_password).decode("utf-8")
        users.update_one(
            {"_id": user["_id"]},
            {"$set": {
                "passwordHash": password_hash,
                "currentSessionId": None,
                "updatedAt": now(),
            }},
        )
        return ok({"temporaryPassword": temp_password}, message="Credentials reset.")

    # ==========================================================
    # 7. DEPARTMENT SUMMARY — every department across every college,
    #    read live from db.departments/db.colleges/db.users. Powers the
    #    Department Management page and gives the Super Admin a single,
    #    real count of departments platform-wide.
    # ==========================================================
    @bp.route("/departments/summary", methods=["GET"])
    @role_required("super_admin")
    def department_summary():
        colleges_by_id = {c["_id"]: c.get("college_name") for c in db.colleges.find({}, {"college_name": 1})}
        top_cohort = top_cohort_label(db)

        rows = []
        for d in db.departments.find({}).sort("department_name", 1):
            student_ids_query = {
                "role": "student",
                "departmentId": d["_id"],
                "isDeleted": {"$ne": True},
            }
            student_count = users.count_documents(student_ids_query)
            scored = list(users.find(
                {**student_ids_query, "finalEmployabilityScore": {"$ne": None}},
                {"finalEmployabilityScore": 1, "cohort": 1},
            ))
            avg_score = (
                round(sum(s["finalEmployabilityScore"] for s in scored) / len(scored), 1)
                if scored else None
            )
            ready_count = sum(1 for s in scored if s.get("cohort") == top_cohort)
            placement_pct = round((ready_count / len(scored)) * 100, 1) if scored else None

            rows.append({
                "id": str(d["_id"]),
                "name": d.get("department_name"),
                "collegeId": str(d["college_id"]),
                "college": colleges_by_id.get(d["college_id"], "—"),
                "status": d.get("status", "active"),
                "students": student_count,
                "avgScore": avg_score,
                "placement": placement_pct,
            })

        return ok({
            "departments": rows,
            "totalDepartments": len(rows),
            "totalColleges": len(colleges_by_id),
        })

    # ==========================================================
    # SUPER ADMIN — SCHEDULE SESSION (db.workshop_sessions).
    # College/Department/Trainer are all multi-select, DB-backed, and
    # re-validated here regardless of what the client already checked.
    # Session Type and Maximum Students no longer exist anywhere in this
    # workflow; Attendance Requirement (Mandatory/Optional) replaces
    # Maximum Students. See module-level docstring above for the schema.
    # ==========================================================
    @bp.route("/interventions/sessions", methods=["GET"])
    @role_required("super_admin")
    def list_workshop_sessions():
        docs = db.workshop_sessions.find({}).sort("createdAt", -1)
        return ok({"sessions": [workshop_session_public(d) for d in docs]})

    def _validate_session_payload(data):
        """Shared by create + edit. Returns (fields_to_set, error_message)."""
        name = (data.get("name") or "").strip()
        if not name:
            return None, "Session name is required."

        college_ids, err = _parse_id_list(data.get("collegeIds"), "Colleges")
        if err:
            return None, err
        college_docs, err = _resolve_colleges(db, college_ids)
        if err:
            return None, err

        department_ids, err = _parse_id_list(data.get("departmentIds"), "Departments")
        if err:
            return None, err
        department_docs, college_names_by_id, err = _resolve_departments(db, department_ids)
        if err:
            return None, err

        trainer_ids, err = _parse_id_list(data.get("trainerIds"), "Trainers")
        if err:
            return None, err
        trainer_docs, err = _resolve_trainers(db, trainer_ids)
        if err:
            return None, err

        cohort = (data.get("cohort") or "").strip()
        if cohort not in SESSION_COHORT_OPTIONS:
            return None, "Applicable Cohort must be one of: " + ", ".join(SESSION_COHORT_OPTIONS) + "."

        date_str = (data.get("date") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            return None, "A valid Session Date is required."
        start_time = (data.get("startTime") or "").strip()
        end_time = (data.get("endTime") or "").strip()
        if not (_TIME_RE.match(start_time) and _TIME_RE.match(end_time)):
            return None, "A valid Start Time and End Time are required."
        if end_time <= start_time:
            return None, "End Time must be after Start Time."

        venue = (data.get("venue") or "").strip()
        if not venue:
            return None, "Venue is required."

        attendance_requirement = (data.get("attendanceRequirement") or "").strip().lower()
        if attendance_requirement not in SESSION_ATTENDANCE_OPTIONS:
            return None, "Attendance Requirement must be 'Mandatory' or 'Optional'."

        fields = {
            "name": name,
            "collegeIds": college_ids,
            "collegeNames": [c.get("college_name") for c in college_docs],
            "departmentIds": department_ids,
            "departmentNames": [
                f"{d.get('department_name')} ({college_names_by_id.get(d['college_id'], '—')})"
                for d in department_docs
            ],
            "trainerIds": trainer_ids,
            "trainerNames": [t.get("fullName") or "—" for t in trainer_docs],
            "cohort": cohort,
            "date": date_str,
            "startTime": start_time,
            "endTime": end_time,
            "venue": venue,
            "attendanceRequirement": attendance_requirement,
        }
        return fields, None

    @bp.route("/interventions/schedule", methods=["POST"])
    @role_required("super_admin")
    def schedule_workshop_session():
        data = request.get_json(silent=True) or {}
        fields, err = _validate_session_payload(data)
        if err:
            return error(err)

        actor_id = get_jwt_identity()
        doc = dict(fields)
        doc.update({
            "status": "scheduled",
            "createdBy": {"id": actor_id, "name": "Super Admin"},
            "createdAt": now(),
            "updatedAt": now(),
        })
        result = db.workshop_sessions.insert_one(doc)
        doc["_id"] = result.inserted_id

        log_activity(
            db, actor_id, "super_admin", "workshop_session_scheduled",
            f"Scheduled session \"{fields['name']}\"",
            meta={"sessionId": str(doc["_id"])},
        )
        return ok({"session": workshop_session_public(doc)}, message="Session scheduled.", status=201)

    @bp.route("/interventions/sessions/<session_id>", methods=["PATCH"])
    @role_required("super_admin")
    def update_workshop_session(session_id):
        oid = to_object_id(session_id)
        if not oid:
            return error("Invalid session id.", 404)
        existing = db.workshop_sessions.find_one({"_id": oid})
        if not existing:
            return error("Session not found.", 404)

        data = request.get_json(silent=True) or {}

        # Status-only update (e.g. cancel), separate from the full edit form.
        if "status" in data and len(data) == 1:
            status = (data.get("status") or "").strip().lower()
            if status not in SESSION_STATUS_OPTIONS:
                return error("status must be one of: " + ", ".join(SESSION_STATUS_OPTIONS) + ".")
            db.workshop_sessions.update_one({"_id": oid}, {"$set": {"status": status, "updatedAt": now()}})
            updated = db.workshop_sessions.find_one({"_id": oid})
            return ok({"session": workshop_session_public(updated)}, message="Session updated.")

        fields, err = _validate_session_payload(data)
        if err:
            return error(err)
        fields["updatedAt"] = now()
        db.workshop_sessions.update_one({"_id": oid}, {"$set": fields})
        updated = db.workshop_sessions.find_one({"_id": oid})

        log_activity(
            db, get_jwt_identity(), "super_admin", "workshop_session_updated",
            f"Updated session \"{fields['name']}\"",
            meta={"sessionId": str(oid)},
        )
        return ok({"session": workshop_session_public(updated)}, message="Session updated.")

    @bp.route("/interventions/sessions/<session_id>", methods=["DELETE"])
    @role_required("super_admin")
    def delete_workshop_session(session_id):
        oid = to_object_id(session_id)
        if not oid:
            return error("Invalid session id.", 404)
        existing = db.workshop_sessions.find_one({"_id": oid})
        if not existing:
            return error("Session not found.", 404)
        db.workshop_sessions.delete_one({"_id": oid})
        log_activity(
            db, get_jwt_identity(), "super_admin", "workshop_session_deleted",
            f"Deleted session \"{existing.get('name', '—')}\"",
            meta={"sessionId": str(oid)},
        )
        return ok(message="Session deleted.")

    # ==========================================================
    # SUPER ADMIN — ATTENDANCE MODULE (db.attendance).
    # All four dropdowns on Mark Attendance + View Records are DB-backed:
    #   colleges    -> db.colleges (active only)
    #   departments -> db.departments (active only, joined by college)
    #   cohorts     -> the platform's shared cohort system (A/B/C/Entry)
    #   trainers    -> db.users role=trainer (approved/suspended)
    # No student records are ever returned until "Fetch Records" is
    # clicked, and re-marking a student on an existing date upserts the
    # same (studentId, date) document rather than creating a duplicate.
    # ==========================================================
    @bp.route("/attendance/filters", methods=["GET"])
    @role_required("super_admin")
    def attendance_filters():
        college_docs = list(db.colleges.find({"status": "active"}).sort("college_name", 1))
        dept_docs = list(db.departments.find({"status": "active"}).sort("department_name", 1))
        trainer_docs = list(db.users.find(
            {"role": "trainer", "approvalStatus": {"$in": ["approved", "suspended"]}}
        ).sort("fullName", 1))
        return ok({
            "colleges": [{"id": str(c["_id"]), "name": c.get("college_name")} for c in college_docs],
            "departments": [
                {"id": str(d["_id"]), "collegeId": str(d["college_id"]), "name": d.get("department_name")}
                for d in dept_docs
            ],
            "cohorts": [{"value": "all", "label": "All Cohorts"}] + [
                {"value": c, "label": ATTENDANCE_COHORT_LABELS[c]} for c in sorted(VALID_COHORTS)
            ] + [{"value": ENTRY_LEVEL, "label": ATTENDANCE_COHORT_LABELS[ENTRY_LEVEL]}],
            "trainers": [{"id": str(t["_id"]), "name": t.get("fullName") or "—"} for t in trainer_docs],
        })

    @bp.route("/attendance/students", methods=["GET"])
    @role_required("super_admin")
    def list_attendance_students():
        college_ids, err = _parse_optional_id_list(request.args.get("collegeIds"))
        if err:
            return error(err)
        department_ids, err = _parse_optional_id_list(request.args.get("departmentIds"))
        if err:
            return error(err)
        cohort_param = (request.args.get("cohorts") or "").strip()
        cohorts = [c for c in cohort_param.split(",") if c.strip()] if cohort_param else []
        allowed = set(VALID_COHORTS) | {ENTRY_LEVEL, "all"}
        if any(c not in allowed for c in cohorts):
            return error("Invalid cohort filter.")
        query = _attendance_student_query(db, college_ids, department_ids, cohorts)
        students = list(db.users.find(query).sort("fullName", 1))
        return ok({"students": [_attendance_student_public(d) for d in students], "total": len(students)})

    @bp.route("/attendance/mark", methods=["POST"])
    @role_required("super_admin")
    def mark_attendance():
        data = request.get_json(silent=True) or {}
        date_str = (data.get("date") or "").strip()
        if not _ATTENDANCE_DATE_RE.match(date_str):
            return error("A valid date (YYYY-MM-DD) is required.")

        marks = data.get("marks")
        if not isinstance(marks, list) or not marks:
            return error("No attendance marks provided.")
        if len(marks) > 2000:
            return error("Too many records in one submission.")

        student_ids = []
        status_by_oid = {}
        for m in marks:
            sid = m.get("studentId")
            status = (m.get("status") or "").strip().lower()
            if status not in ATTENDANCE_STATUSES:
                return error("Attendance status must be 'present' or 'absent'.")
            oid = to_object_id(sid)
            if not oid:
                return error("One of the student ids is invalid.")
            if oid not in status_by_oid:
                status_by_oid[oid] = status
                student_ids.append(oid)

        students_by_id = {d["_id"]: d for d in db.users.find(
            {"_id": {"$in": student_ids}, "role": "student"})}
        missing = [str(oid) for oid in student_ids if oid not in students_by_id]
        if missing:
            return error("One or more selected students no longer exist.")

        actor_id = get_jwt_identity()
        now_ts = now()
        inserted = updated = 0
        for oid, status in status_by_oid.items():
            student = students_by_id[oid]
            cohort = student_cohort_label(student)
            payload = {
                "studentId": oid,
                "date": date_str,
                "studentName": student.get("fullName") or "—",
                "rollNumber": student.get("rollNumber") or "—",
                "collegeId": student.get("collegeId"),
                "college": student.get("college") or "—",
                "departmentId": student.get("departmentId"),
                "department": student.get("department") or "—",
                "cohort": cohort,
                "status": status,
                "markedBy": {"id": str(actor_id), "name": "Super Admin", "role": "super_admin"},
                "markedAt": now_ts,
                "updatedAt": now_ts,
            }
            result = db.attendance.update_one(
                {"studentId": oid, "date": date_str},
                {"$set": payload},
                upsert=True,
            )
            if result.upserted_id:
                inserted += 1
            else:
                updated += 1

        present = sum(1 for s in status_by_oid.values() if s == "present")
        absent = len(status_by_oid) - present
        log_activity(
            db, actor_id, "super_admin", "attendance_marked",
            f"Marked attendance for {len(status_by_oid)} students on {date_str}",
            meta={"date": date_str, "present": present, "absent": absent},
        )
        return ok({
            "saved": len(status_by_oid),
            "inserted": inserted,
            "updated": updated,
            "present": present,
            "absent": absent,
        }, message="Attendance saved.")

    @bp.route("/attendance/records", methods=["GET"])
    @role_required("super_admin")
    def list_attendance_records():
        college_ids, err = _parse_optional_id_list(request.args.get("collegeIds"))
        if err:
            return error(err)
        department_ids, err = _parse_optional_id_list(request.args.get("departmentIds"))
        if err:
            return error(err)
        trainer_ids, err = _parse_optional_id_list(request.args.get("trainerIds"))
        if err:
            return error(err)
        date_str = (request.args.get("date") or "").strip()
        if not _ATTENDANCE_DATE_RE.match(date_str):
            return error("A valid date (YYYY-MM-DD) is required.")

        # All students matching the college/department scope (cohort is not a
        # View Records filter), so "Not Yet Marked" can be derived for the
        # selected date.
        student_query = _attendance_student_query(db, college_ids, department_ids, [])
        students = list(db.users.find(student_query).sort("fullName", 1))
        student_ids = [d["_id"] for d in students]

        record_query = {"date": date_str, "studentId": {"$in": student_ids}}
        if trainer_ids:
            record_query["markedBy.id"] = {"$in": [str(t) for t in trainer_ids]}
        records = list(db.attendance.find(record_query))
        record_by_student = {r["studentId"]: r for r in records}

        rows = []
        for student in students:
            marked = record_by_student.get(student["_id"])
            cohort = student_cohort_label(student)
            rows.append({
                "studentId": str(student["_id"]),
                "studentName": student.get("fullName") or "—",
                "rollNumber": student.get("rollNumber") or "—",
                "college": student.get("college") or "—",
                "department": student.get("department") or "—",
                "cohort": cohort,
                "cohortLabel": ATTENDANCE_COHORT_LABELS.get(cohort, cohort),
                "status": marked["status"] if marked else "not_marked",
                "markedBy": marked["markedBy"] if marked else None,
                "markedAt": marked["markedAt"] if marked else None,
            })

        present = sum(1 for r in rows if r["status"] == "present")
        absent = sum(1 for r in rows if r["status"] == "absent")
        not_marked = sum(1 for r in rows if r["status"] == "not_marked")
        return ok({
            "records": rows,
            "date": date_str,
            "present": present,
            "absent": absent,
            "notMarked": not_marked,
            "total": len(rows),
        })

    return bp
