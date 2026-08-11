"""
============================================================
 trainer.py — Trainer Dashboard backend
============================================================
Registered at /api/trainer. Mirrors superadmin.py's assessment +
cohort + quiz-management behaviour so the two dashboards can never
disagree, but every query here is scoped to the trainer's own
college (trainers only manage their own students).

The Entry Level cohort option and assignment rule is IDENTICAL to
Super Admin — both call the same quiz_common helpers.
"""

import re

from datetime import datetime, timedelta

from flask import Blueprint, request, send_file
from flask_jwt_extended import get_jwt_identity

from quiz_common import (
    ok, error, role_required, now, to_object_id, serialize, iso_utc, fmt_ist,
    attendance_summary,
    VALID_COHORT_TARGETS, ENTRY_LEVEL,
    cohort_counts, record_interview_score_for_cohort,
    log_activity, get_recent_activity,
    list_quiz_results, set_quiz_interview_marks, validate_quiz_result,
    serialize_quiz_result, RESULT_STATUS_INTERVIEW_DONE, RESULT_STATUS_VALIDATED,
    compute_quiz_analytics, list_quiz_responses,
    list_distinct_departments, build_quiz_responses_workbook,
)
from colleges import resolve_active_department, _generate_temp_password, department_public
from reporting import excel_bytes, pdf_bytes
from login import validate_email, validate_mobile


# ------------------------------------------------------------
# Dashboard insights — every value in GET /dashboard/insights is
# computed live from db.assessment_attempts (the same source every
# placement-readiness number in this module uses), scoped to the
# trainer's own college. Nothing here is hardcoded, and nothing is
# cached beyond the lifetime of a single request.
# ------------------------------------------------------------
COHORT_RADAR_LABELS = {
    "A": "Cohort A",
    "B": "Cohort B",
    "C": "Cohort C",
    "entry_level": "Entry Level",
}
RADAR_COHORT_ORDER = ["entry_level", "A", "B", "C"]


def _week_buckets(weeks=8):
    """Monday-anchored calendar weeks, oldest first, ending with the week
    containing today. Returns (start, end, label) triples using naive UTC
    datetimes — the same clock stored submittedAt values live on, matching
    the _month_bounds() convention in superadmin.py. Week boundaries are
    midnight-aligned (Monday 00:00), like _month_bounds() month starts."""
    today = datetime.utcnow()
    anchor = (today - timedelta(days=today.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return [
        (
            anchor - timedelta(weeks=offset),
            anchor - timedelta(weeks=offset - 1),
            (anchor - timedelta(weeks=offset)).strftime("%b %d"),
        )
        for offset in range(weeks - 1, -1, -1)
    ]


def init_trainer(db, bcrypt=None):
    bp = Blueprint("trainer", __name__)

    users = db.users
    questions = db.questions
    assessments = db.assessments
    attempts = db.assessment_attempts
    manual_interviews = db.manual_interviews
    quiz_attempts = db.quiz_attempts

    def _trainer_doc():
        return users.find_one({"_id": to_object_id(get_jwt_identity())})

    def _trainer_college():
        trainer = _trainer_doc()
        return (trainer or {}).get("college")

    # ------------------------------------------------------------
    # Shared student-roster query — backs GET /students AND the
    # /students/export download so a filtered export is always
    # byte-for-byte the same rows the table is showing. Trainers
    # manage the whole platform roster (no trainer↔student binding),
    # so there is deliberately NO college scoping here.
    # ------------------------------------------------------------
    def _student_roster(college="", department="", cohort="", search=""):
        query = {"role": "student"}
        if college:
            query["college"] = {"$in": [c.strip() for c in college.split(",") if c.strip()]}
        if department:
            query["department"] = {"$in": [d.strip() for d in department.split(",") if d.strip()]}
        and_parts = []
        if cohort:
            or_parts = []
            for c in [x.strip() for x in cohort.split(",") if x.strip()]:
                if c == ENTRY_LEVEL:
                    or_parts.append({"cohort": None})
                    or_parts.append({"cohort": {"$exists": False}})
                elif c in {"A", "B", "C"}:
                    or_parts.append({"cohort": c})
            if or_parts:
                and_parts.append({"$or": or_parts})
        if search:
            rx = {"$regex": re.escape(search), "$options": "i"}
            and_parts.append({"$or": [
                {"fullName": rx}, {"rollNumber": rx}, {"email": rx},
            ]})
        if and_parts:
            query["$and"] = and_parts

        docs = list(users.find(query, {"passwordHash": 0}).sort("createdAt", -1))
        ids = [d["_id"] for d in docs]

        att_map = {}
        for r in db.attendance.find(
            {"studentId": {"$in": ids}}, {"studentId": 1, "status": 1}
        ):
            bucket = att_map.setdefault(str(r.get("studentId")), [0, 0])
            if r.get("status") == "present":
                bucket[0] += 1
            elif r.get("status") == "absent":
                bucket[1] += 1

        score_map = {}
        for a in quiz_attempts.find(
            {"status": "submitted", "studentId": {"$in": ids}},
            {"studentId": 1, "overall.percentage": 1},
        ):
            pct = (a.get("overall") or {}).get("percentage")
            if pct is not None:
                score_map.setdefault(str(a.get("studentId")), []).append(pct)

        rows = []
        for d in docs:
            present, absent = att_map.get(str(d["_id"]), [0, 0])
            total = present + absent
            scores = score_map.get(str(d["_id"]), [])
            rows.append({
                "id": str(d["_id"]),
                "fullName": d.get("fullName"),
                "rollNumber": d.get("rollNumber"),
                "email": d.get("email"),
                "mobile": d.get("mobile"),
                "college": d.get("college"),
                "collegeId": str(d["collegeId"]) if d.get("collegeId") else None,
                "department": d.get("department"),
                "departmentId": str(d["departmentId"]) if d.get("departmentId") else None,
                "cohort": d.get("cohort"),
                "attendancePct": round(present / total * 100, 1) if total else None,
                "quizScore": round(sum(scores) / len(scores), 1) if scores else None,
                "status": d.get("approvalStatus") or "approved",
                "createdAt": iso_utc(d.get("createdAt")),
            })
        return rows

    @bp.route("/students", methods=["GET"])
    @role_required("trainer")
    def list_students():
        rows = _student_roster(
            college=(request.args.get("college") or "").strip(),
            department=(request.args.get("department") or "").strip(),
            cohort=(request.args.get("cohort") or "").strip(),
            search=(request.args.get("search") or "").strip(),
        )
        return ok({"students": rows, "total": len(rows)})

    # ==========================================================
    # ASSESSMENT CREATION — same rules as Super Admin, incl. Entry Level
    # ==========================================================
    @bp.route("/assessments", methods=["POST"])
    @role_required("trainer")
    def create_assessment():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        assessment_type = (data.get("type") or "custom").strip().lower()
        cohort_target = (data.get("cohortTarget") or "").strip()
        section_counts = data.get("sectionCounts") or {}

        if not name:
            return error("Assessment name is required.")
        if cohort_target not in VALID_COHORT_TARGETS:
            return error(
                "cohortTarget must be one of: A, B, C, all, entry_level."
            )
        if not isinstance(section_counts, dict) or not section_counts:
            return error("sectionCounts is required, e.g. {'Communication': 10}.")
        for section, count in section_counts.items():
            if not isinstance(count, int) or count <= 0:
                return error(f"Question count for section '{section}' must be a positive integer.")

        status = (data.get("status") or "active").strip().lower()
        if status not in {"draft", "active"}:
            return error("status must be 'draft' or 'active'.")

        # Department dropdown, scoped to the trainer's own (assigned) college —
        # reads from the same departments collection as everywhere else.
        department_id = data.get("departmentId")
        department_name = None
        trainer = _trainer_doc()
        trainer_college_id = trainer.get("collegeId") if trainer else None
        if department_id:
            if not trainer_college_id:
                return error("Your account has no college assigned yet. Contact the Super Admin.")
            department_doc = resolve_active_department(db, department_id, str(trainer_college_id))
            if not department_doc:
                return error("Selected department is invalid or inactive for your college.")
            department_name = department_doc["department_name"]

        doc = {
            "name": name,
            "type": assessment_type,
            "cohortTarget": cohort_target,
            "sectionCounts": {k: int(v) for k, v in section_counts.items()},
            "totalQuestions": sum(int(v) for v in section_counts.values()),
            "scheduledAt": data.get("scheduledAt"),
            "availableFrom": data.get("availableFrom"),
            "availableTo": data.get("availableTo"),
            "college": _trainer_college(),
            "department": department_name,
            "createdBy": "trainer",
            "createdByTrainerId": get_jwt_identity(),
            "createdAt": now(),
            "status": status,
        }
        result = assessments.insert_one(doc)
        doc["_id"] = result.inserted_id
        log_activity(
            db, get_jwt_identity(), "trainer", "assessment_created",
            f'Created assessment "{name}"' + (" (published)" if status == "active" else " (draft)"),
            college=doc["college"], meta={"assessmentId": str(result.inserted_id)},
        )
        return ok({"assessment": serialize(doc)}, message="Assessment created.", status=201)

    @bp.route("/assessments", methods=["GET"])
    @role_required("trainer")
    def list_assessments():
        college = _trainer_college()
        query = {"$or": [{"college": college}, {"createdBy": "super_admin"}]} if college else {}
        cursor = assessments.find(query).sort("createdAt", -1)
        return ok({"assessments": [serialize(a) for a in cursor]})

    def _own_trainer_assessment(assessment_id):
        """Look up an assessment, but only if this trainer created it —
        trainers may publish/unpublish/delete their own assessments only.
        Assessments created by Super Admin are read-only here."""
        oid = to_object_id(assessment_id)
        if not oid:
            return None, error("Invalid assessment id.", 404)
        doc = assessments.find_one({"_id": oid})
        if not doc:
            return None, error("Assessment not found.", 404)
        if doc.get("createdByTrainerId") != get_jwt_identity():
            return None, error("You can only manage assessments you created.", 403)
        return doc, None

    @bp.route("/assessments/<assessment_id>/status", methods=["PATCH"])
    @role_required("trainer")
    def update_assessment_status(assessment_id):
        doc, err = _own_trainer_assessment(assessment_id)
        if err:
            return err
        data = request.get_json(silent=True) or {}
        status = (data.get("status") or "").strip().lower()
        if status not in {"draft", "active"}:
            return error("status must be 'draft' (unpublished) or 'active' (published).")
        assessments.update_one({"_id": doc["_id"]}, {"$set": {"status": status, "updatedAt": now()}})
        updated = assessments.find_one({"_id": doc["_id"]})
        action = "assessment_published" if status == "active" else "assessment_unpublished"
        verb = "published" if status == "active" else "moved to draft"
        log_activity(
            db, get_jwt_identity(), "trainer", action,
            f'Assessment "{doc.get("name")}" {verb}',
            college=doc.get("college"), meta={"assessmentId": str(doc["_id"])},
        )
        return ok({"assessment": serialize(updated)}, message=f"Assessment marked as {status}.")

    @bp.route("/assessments/<assessment_id>", methods=["DELETE"])
    @role_required("trainer")
    def delete_assessment(assessment_id):
        doc, err = _own_trainer_assessment(assessment_id)
        if err:
            return err
        assessments.delete_one({"_id": doc["_id"]})
        log_activity(
            db, get_jwt_identity(), "trainer", "assessment_deleted",
            f'Deleted assessment "{doc.get("name")}"',
            college=doc.get("college"), meta={"assessmentId": str(doc["_id"])},
        )
        return ok(message="Assessment deleted.")

    # ==========================================================
    # COHORTS — Entry Level + A/B/C, scoped to trainer's college.
    # Uses the exact same cohort_counts() helper as Super Admin.
    # ==========================================================
    @bp.route("/cohorts/counts", methods=["GET"])
    @role_required("trainer")
    def get_cohort_counts():
        return ok({"cohortCounts": cohort_counts(db, college=_trainer_college())})

    @bp.route("/cohorts/students", methods=["GET"])
    @role_required("trainer")
    def list_cohort_students():
        cohort = request.args.get("cohort", "").strip()
        # Trainers manage the whole roster — no college scoping.
        query = {"role": "student"}
        if cohort == ENTRY_LEVEL:
            query["$or"] = [{"cohort": None}, {"cohort": {"$exists": False}}]
        elif cohort in {"A", "B", "C"}:
            query["cohort"] = cohort
        docs = users.find(query, {"passwordHash": 0}).sort("createdAt", -1)
        return ok({"students": [serialize(d) for d in docs]})

    # ==========================================================
    # QUIZ MANAGEMENT / QUIZ RESPONSES / MANUAL INTERVIEW — live, college-scoped
    # ==========================================================
    @bp.route("/quiz-management/summary", methods=["GET"])
    @role_required("trainer")
    def quiz_management_summary():
        college = _trainer_college()
        student_ids = [s["_id"] for s in users.find({"role": "student", "college": college}, {"_id": 1})]
        total_attempts = attempts.count_documents({"studentId": {"$in": student_ids}})
        submitted_attempts = attempts.count_documents({"studentId": {"$in": student_ids}, "status": "submitted"})
        in_progress = attempts.count_documents({"studentId": {"$in": student_ids}, "status": "in_progress"})
        return ok({
            "totalQuestions": questions.count_documents({"active": True}),
            "totalAssessments": assessments.count_documents({"college": college}),
            "totalAttempts": total_attempts,
            "submittedAttempts": submitted_attempts,
            "inProgressAttempts": in_progress,
            "cohortCounts": cohort_counts(db, college=college),
        })

    @bp.route("/quiz-responses", methods=["GET"])
    @role_required("trainer")
    def quiz_responses():
        """Raw per-attempt rows for the Trainer's Quiz Responses table —
        scoped to the trainer's own college. Root-cause fix: this used to
        query db.assessment_attempts/db.assessments (the older, largely
        unused cohort-placement engine), which is why this page always
        appeared empty — students actually submit into db.quiz_attempts
        via Quiz Management. Now shares the exact same query Super
        Admin's working Quiz Responses table uses (list_quiz_responses),
        just scoped to this trainer's college, so the two can never
        disagree.
        """
        college = _trainer_college()
        cohort = request.args.get("cohort") or "all"
        quiz_id = request.args.get("quizId") or "all"
        department = request.args.get("department") or "all"
        search = request.args.get("search") or ""
        return ok({"responses": list_quiz_responses(
            db, college=college, cohort=cohort, quiz_id=quiz_id,
            department=department, search=search,
        )})

    @bp.route("/quiz-responses/filters", methods=["GET"])
    @role_required("trainer")
    def quiz_responses_filters():
        """Course/Department dropdown options for the Quiz Responses
        filter bar — distinct values actually in use by this trainer's
        own students, read live from the database, never hardcoded."""
        return ok({"departments": list_distinct_departments(db, college=_trainer_college())})

    @bp.route("/quiz-responses/export", methods=["GET"])
    @role_required("trainer")
    def quiz_responses_export():
        """Backend-generated .xlsx of exactly what the Quiz Responses
        table is currently showing — same filters, same query, same
        rows; nothing is regenerated or recomputed differently here."""
        college = _trainer_college()
        cohort = request.args.get("cohort") or "all"
        quiz_id = request.args.get("quizId") or "all"
        department = request.args.get("department") or "all"
        search = request.args.get("search") or ""
        rows = list_quiz_responses(
            db, college=college, cohort=cohort, quiz_id=quiz_id,
            department=department, search=search, limit=100000,
        )
        buf = build_quiz_responses_workbook(rows)
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="quiz_responses.xlsx",
        )

    @bp.route("/quiz-analytics", methods=["GET"])
    @role_required("trainer")
    def quiz_analytics():
        """Student Participation, Performance Metrics, Leaderboard and
        Section-wise Performance for this trainer's own college — the
        exact same computation Super Admin's Quiz Responses page uses
        (quiz_common.compute_quiz_analytics), just pre-scoped to the
        trainer's college rather than accepting a college filter (a
        trainer only ever sees their own college's data).
        """
        cohort = request.args.get("cohort") or "all"
        quiz_id = request.args.get("quizId") or "all"
        department = request.args.get("department") or "all"
        search = request.args.get("search") or ""
        return ok(compute_quiz_analytics(
            db, college=_trainer_college(), cohort=cohort, quiz_id=quiz_id,
            department=department, search=search,
        ))

    @bp.route("/manual-interview", methods=["GET"])
    @role_required("trainer")
    def manual_interview_list():
        college = _trainer_college()
        student_ids = [s["_id"] for s in users.find({"role": "student", "college": college}, {"_id": 1})]
        docs = manual_interviews.find({"studentId": {"$in": student_ids}}).sort("createdAt", -1)
        rows = []
        for d in docs:
            student = users.find_one({"_id": d["studentId"]}, {"passwordHash": 0})
            rows.append({
                "interviewId": str(d["_id"]),
                "studentId": str(d["studentId"]),
                "studentName": (student or {}).get("fullName"),
                "cohort": (student or {}).get("cohort") or ENTRY_LEVEL,
                "status": d.get("status"),
                "scheduledAt": d.get("scheduledAt"),
                "notes": d.get("notes"),
                "score": d.get("score"),
            })
        return ok({"manualInterviews": rows})

    @bp.route("/manual-interview/<student_id>/schedule", methods=["POST"])
    @role_required("trainer")
    def schedule_manual_interview(student_id):
        """Step 1: schedule (or reschedule) a manual interview for a student.
        This does NOT score the interview or touch cohort generation."""
        data = request.get_json(silent=True) or {}
        oid = to_object_id(student_id)
        if not oid or not users.find_one({"_id": oid, "role": "student"}):
            return error("Invalid student id.", 404)

        doc = {
            "studentId": oid,
            "status": "scheduled",
            "scheduledAt": data.get("scheduledAt"),
            "notes": data.get("notes", ""),
            "score": None,
            "scheduledBy": get_jwt_identity(),
            "createdAt": now(),
            "updatedAt": now(),
        }
        existing = manual_interviews.find_one({"studentId": oid, "status": {"$ne": "completed"}})
        if existing:
            manual_interviews.update_one({"_id": existing["_id"]}, {"$set": {
                "scheduledAt": doc["scheduledAt"], "notes": doc["notes"], "updatedAt": now(),
            }})
            interview_id = existing["_id"]
        else:
            interview_id = manual_interviews.insert_one(doc).inserted_id

        student = users.find_one({"_id": oid})
        log_activity(
            db, get_jwt_identity(), "trainer", "manual_interview_scheduled",
            f'Scheduled manual interview for {(student or {}).get("fullName", "a student")}',
            college=_trainer_college(), student_id=oid,
            meta={"interviewId": str(interview_id)},
        )
        return ok({"interviewId": str(interview_id)}, message="Manual interview scheduled.")

    @bp.route("/manual-interview/<interview_id>/score", methods=["POST"])
    @role_required("trainer")
    def score_manual_interview(interview_id):
        """
        Step 2: record the interview score (0-100). This feeds the
        two-stage cohort generation — once BOTH the assessment score
        and this interview score exist, a cohort (A/B/C) is generated
        automatically using the Super Admin's current Placement Rules.
        """
        data = request.get_json(silent=True) or {}
        oid = to_object_id(interview_id)
        if not oid:
            return error("Invalid interview id.", 404)
        interview = manual_interviews.find_one({"_id": oid})
        if not interview:
            return error("Manual interview not found.", 404)

        score = data.get("score")
        if score is None or not isinstance(score, (int, float)) or not (0 <= score <= 100):
            return error("score is required and must be a number between 0 and 100.")

        manual_interviews.update_one(
            {"_id": oid},
            {"$set": {
                "status": "completed",
                "score": score,
                "notes": data.get("notes", interview.get("notes", "")),
                "conductedBy": get_jwt_identity(),
                "completedAt": now(),
                "updatedAt": now(),
            }},
        )

        new_cohort = record_interview_score_for_cohort(db, interview["studentId"], score)
        student = users.find_one({"_id": interview["studentId"]})
        log_activity(
            db, get_jwt_identity(), "trainer", "manual_interview_scored",
            f'Scored manual interview for {(student or {}).get("fullName", "a student")} — {score}%',
            college=_trainer_college(), student_id=interview["studentId"],
            meta={"interviewId": str(oid)},
        )
        response = {"interviewId": str(oid), "score": score}
        if new_cohort:
            response["cohortGenerated"] = new_cohort
        return ok(response, message="Manual interview scored.")

    # ==========================================================
    # MARKS MANAGEMENT — Create-Quiz results, Interview Verification,
    # and Validation Verification. Deliberately separate from the
    # /quiz-responses + /manual-interview routes above, which power the
    # OLDER random-baseline assessment engine (db.assessments). This
    # block is the workflow for manually authored quizzes (db.quizzes /
    # db.quiz_attempts): a student appears here the instant — and ONLY
    # the instant — they submit a quiz; never before.
    # ==========================================================
    @bp.route("/quiz-results", methods=["GET"])
    @role_required("trainer")
    def quiz_results():
        """Assessment Responses: every student who has actually
        submitted a Create-Quiz quiz, scoped to this trainer's college.
        `search` matches Student Name / Roll Number / Register Number,
        resolved entirely by the database (see quiz_common._student_search_query)."""
        search = request.args.get("search") or ""
        return ok({"results": list_quiz_results(db, college=_trainer_college(), search=search)})

    @bp.route("/quiz-interview-verification", methods=["GET"])
    @role_required("trainer")
    def quiz_interview_verification():
        """Students eligible for interview verification: anyone who has
        submitted at least one quiz. Includes rows already scored (so the
        trainer can see/update them) — the frontend groups by `status`.
        `search` matches Assessment Name / Student Name / Roll Number /
        College / Department, resolved entirely by the database (see
        quiz_common._verification_search_query)."""
        search = request.args.get("search") or ""
        return ok({"results": list_quiz_results(db, college=_trainer_college(), search=search, broad_search=True)})

    @bp.route("/quiz-interview-verification/<attempt_id>/marks", methods=["POST"])
    @role_required("trainer")
    def enter_quiz_interview_marks(attempt_id):
        oid = to_object_id(attempt_id)
        if not oid:
            return error("Invalid quiz result id.", 404)
        data = request.get_json(silent=True) or {}
        updated, err = set_quiz_interview_marks(
            db, oid, data.get("marks"), get_jwt_identity(), college=_trainer_college(),
        )
        if err:
            return error(err)
        log_activity(
            db, get_jwt_identity(), "trainer", "quiz_interview_scored",
            f'Entered interview marks for {updated.get("studentName", "a student")} '
            f'({updated.get("quizTitle", "a quiz")}) — {updated.get("interviewMarks")}%',
            college=_trainer_college(), student_id=updated.get("studentId"),
            meta={"attemptId": attempt_id, "finalAverage": updated.get("finalAverage"),
                  "cohort": updated.get("assignedCohort")},
        )
        return ok({"result": serialize_quiz_result(db, updated)}, message="Interview marks saved.")

    @bp.route("/quiz-validation-verification", methods=["GET"])
    @role_required("trainer")
    def quiz_validation_verification():
        """Only results that have a Final Average + assigned Cohort —
        i.e. interview marks have already been entered. `search` matches
        Assessment Name / Student Name / Roll Number / College /
        Department, same as Interview Verification above."""
        search = request.args.get("search") or ""
        return ok({"results": list_quiz_results(
            db, college=_trainer_college(),
            statuses=[RESULT_STATUS_INTERVIEW_DONE, RESULT_STATUS_VALIDATED],
            search=search, broad_search=True,
        )})

    @bp.route("/quiz-validation-verification/<attempt_id>/validate", methods=["POST"])
    @role_required("trainer")
    def validate_quiz_result_route(attempt_id):
        oid = to_object_id(attempt_id)
        if not oid:
            return error("Invalid quiz result id.", 404)
        updated, err = validate_quiz_result(db, oid, get_jwt_identity(), college=_trainer_college())
        if err:
            return error(err)
        log_activity(
            db, get_jwt_identity(), "trainer", "quiz_result_validated",
            f'Validated result for {updated.get("studentName", "a student")} '
            f'({updated.get("quizTitle", "a quiz")}) — Cohort {updated.get("assignedCohort")}',
            college=_trainer_college(), student_id=updated.get("studentId"),
            meta={"attemptId": attempt_id},
        )
        return ok({"result": serialize_quiz_result(db, updated)}, message="Result validated.")

    # ==========================================================
    # RECENT ACTIVITY — replaces the hardcoded activity-list on the
    # Trainer dashboard. Every entry here comes from log_activity()
    # calls above (and, over time, other trainer/student actions).
    # ==========================================================
    @bp.route("/activity", methods=["GET"])
    @role_required("trainer")
    def recent_activity():
        trainer = _trainer_doc()
        actor_id = str(trainer["_id"]) if trainer and trainer.get("_id") else None
        limit = max(1, min(int(request.args.get("limit", 5)), 50))
        query = {"actorId": actor_id} if actor_id else {}
        rows = get_recent_activity(db, query, limit=limit)
        return ok({"activity": rows})

    # ==========================================================
    # DASHBOARD SUMMARY — real numbers for the hero stat pills.
    # NOTE: "Sessions Today" and "Attendance %" are intentionally
    # left out of this response — there is no workshops/sessions
    # or attendance collection in the backend yet, so those two
    # hero pills cannot be made dynamic until that backend exists
    # (see handover notes). Returning a fabricated number for them
    # would violate the "no placeholder statistics" requirement,
    # so the frontend should hide those two pills for now rather
    # than show stale hardcoded values.
    # ==========================================================
    @bp.route("/dashboard/summary", methods=["GET"])
    @role_required("trainer")
    def dashboard_summary():
        # Trainers manage the whole roster (no trainer↔student binding), so
        # both hero values are platform-wide. Average Score reads the same
        # db.quiz_attempts collection students actually submit into via Quiz
        # Management (db.assessment_attempts is the older, unused engine and
        # stays empty — see quiz-responses notes).
        total_students = users.count_documents({"role": "student"})
        scores = [
            a["overall"]["percentage"]
            for a in quiz_attempts.find(
                {"status": "submitted"},
                {"overall.percentage": 1},
            )
            if a.get("overall", {}).get("percentage") is not None
        ]
        avg_quiz_score = round(sum(scores) / len(scores), 1) if scores else None

        return ok({
            "studentsTrained": total_students,
            "avgQuizScore": avg_quiz_score,
        })

    # ==========================================================
    # DYNAMIC CHARTS — same shape as Super Admin, scoped to college
    # ==========================================================
    @bp.route("/dashboard/charts", methods=["GET"])
    @role_required("trainer")
    def dashboard_charts():
        college = _trainer_college()
        student_ids = [s["_id"] for s in users.find({"role": "student", "college": college}, {"_id": 1})]
        section_agg = {}
        overall_scores = []
        trend_points = []
        for att in attempts.find({"status": "submitted", "studentId": {"$in": student_ids}}).sort("submittedAt", 1):
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

        skill_radar = {section: round(b["sum"] / b["n"], 2) if b["n"] else 0 for section, b in section_agg.items()}
        overall_avg = round(sum(overall_scores) / len(overall_scores), 2) if overall_scores else 0

        return ok({
            "skillRadar": skill_radar,
            "scoreByCategory": skill_radar,
            "overallScore": overall_avg,
            "categoryPercentage": skill_radar,
            "performanceTrend": trend_points,
        })

    # ==========================================================
    # DASHBOARD INSIGHTS — Readiness Trend, Department Ranking,
    # Skill Radar, Avg Improvement ring and Upcoming Sessions, all
    # computed live from db.assessment_attempts / db.workshop_sessions
    # and scoped to the trainer's own college. The frontend renders
    # these five widgets entirely from this one response.
    # ==========================================================
    @bp.route("/dashboard/insights", methods=["GET"])
    @role_required("trainer")
    def dashboard_insights():
        trainer = _trainer_doc()
        # Trainers manage the whole roster — insights aggregate every
        # submitted assessment attempt platform-wide.
        student_ids = [s["_id"] for s in users.find({"role": "student"}, {"_id": 1})]

        empty = {
            "readinessTrend": [],
            "departmentRanking": [],
            "skillRadar": [],
            "improvementRing": None,
            "upcomingSessions": [],
        }
        if not student_ids:
            return ok(empty)

        # Single pass over all submitted attempts feeds every
        # metric below, so all five widgets can never drift apart.
        week_agg = {}             # week index -> [sum, n]
        dept_best = {}            # department -> {studentId: (best_pct, college)}
        cohort_best = {}          # cohort slug -> {studentId: best_pct}
        student_results = {}      # studentId -> {assessmentId: (submittedAt, pct)}

        buckets = _week_buckets()
        for att in attempts.find(
            {"status": "submitted", "studentId": {"$in": student_ids}}
        ).sort("submittedAt", 1):
            pct = (att.get("overall") or {}).get("percentage")
            if pct is None:
                continue
            sid = att.get("studentId")
            submitted_at = att.get("submittedAt")
            dept = att.get("department")

            # 1. Readiness Trend — the platform's weekly average score.
            # A week with no submissions is a missing data point, not a
            # 0% week, so empty weeks are omitted from the trend.
            if submitted_at is not None:
                for idx, (ws, we, _label) in enumerate(buckets):
                    if ws <= submitted_at < we:
                        bucket = week_agg.setdefault(idx, [0.0, 0])
                        bucket[0] += pct
                        bucket[1] += 1
                        break

            # 2. Department Ranking — per-student best attempt, averaged
            # across the department (mirrors the institution-ranking rule).
            # The department's college is the college of that student's
            # best attempt, so a same-named department in two colleges
            # never merges into one misleading row.
            if dept and sid is not None:
                best = dept_best.setdefault(dept, {})
                if sid not in best or pct > best[sid][0]:
                    best[sid] = (pct, att.get("college"))

            # 3. Skill Radar — per-student best attempt per cohort, so each
            # cohort axis is that cohort's average achieved score.
            if sid is not None:
                slug = att.get("cohort")
                slug = slug if slug in COHORT_RADAR_LABELS else ENTRY_LEVEL
                best = cohort_best.setdefault(slug, {})
                if sid not in best or pct > best[sid]:
                    best[sid] = pct

            # 4. Avg Improvement — only the LATEST attempt per assessment
            # counts as that assessment's result, so a student retaking the
            # same assessment never produces a fake zero-delta "improvement".
            if sid is not None and submitted_at is not None:
                assess_key = str(att.get("assessmentId") or att.get("assessmentName") or "")
                per_student = student_results.setdefault(sid, {})
                prev = per_student.get(assess_key)
                if prev is None or submitted_at >= prev[0]:
                    per_student[assess_key] = (submitted_at, pct)

        readiness_trend = [
            {"label": buckets[idx][2], "value": round(week_agg[idx][0] / week_agg[idx][1], 1)}
            for idx in sorted(week_agg)
        ]

        department_ranking = []
        for dept, best_map in dept_best.items():
            if not best_map:
                continue
            top_college = None
            top_pct = -1
            values = []
            for sid, (best_pct, college) in best_map.items():
                values.append(best_pct)
                if best_pct > top_pct:
                    top_pct = best_pct
                    top_college = college
            department_ranking.append({
                "name": dept,
                "college": top_college,
                "value": round(sum(values) / len(values), 1),
            })
        department_ranking.sort(key=lambda r: r["value"], reverse=True)
        department_ranking = department_ranking[:5]

        skill_radar = []
        for slug in RADAR_COHORT_ORDER:
            best_map = cohort_best.get(slug)
            if not best_map:
                continue
            skill_radar.append({
                "label": COHORT_RADAR_LABELS[slug],
                "value": round(sum(best_map.values()) / len(best_map), 1),
            })

        # Average improvement = mean of every per-student delta between two
        # consecutive, distinct assessment results (previous -> subsequent).
        deltas = []
        for per_student in student_results.values():
            ordered = sorted(per_student.values(), key=lambda item: item[0])
            for i in range(1, len(ordered)):
                deltas.append(ordered[i][1] - ordered[i - 1][1])

        improvement_ring = None
        if deltas:
            avg_delta = sum(deltas) / len(deltas)
            improvement_ring = {
                "pct": round(max(0.0, min(100.0, avg_delta)), 1),
                "improvementLabel": "{}{}%".format(
                    "+" if avg_delta > 0 else "", round(avg_delta, 1)
                ),
                "workshopsCount": None,
                "avgRating": None,
            }

        # 5. Upcoming Sessions — this trainer's assigned workshop sessions
        # whose start datetime hasn't passed yet. Dates are stored as
        # server-local wall clock (YYYY-MM-DD + HH:MM), the same convention
        # superadmin.py's session rules use.
        upcoming_sessions = []
        if trainer is not None and trainer.get("_id") is not None:
            trainer_oid = trainer["_id"]
            today = datetime.now()
            if improvement_ring is not None:
                improvement_ring["workshopsCount"] = db.workshop_sessions.count_documents(
                    {"trainerIds": trainer_oid}
                )
            for doc in db.workshop_sessions.find({
                "trainerIds": trainer_oid,
                "status": "scheduled",
                "date": {"$gte": today.strftime("%Y-%m-%d")},
            }):
                date_str = doc.get("date")
                start_time = doc.get("startTime")
                try:
                    start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
                except (ValueError, TypeError):
                    continue
                if start_dt < today:
                    continue
                end_time = doc.get("endTime")
                parsed_date = datetime.strptime(date_str, "%Y-%m-%d")
                time_str = f"{start_time}\u2013{end_time}" if end_time else start_time
                upcoming_sessions.append({
                    "name": doc.get("name"),
                    "meta": f"{parsed_date.strftime('%d %b %Y')} \u2022 {time_str}",
                    "isToday": date_str == today.strftime("%Y-%m-%d"),
                    "_sort_dt": start_dt,
                })
            upcoming_sessions.sort(key=lambda s: s["_sort_dt"])
            for s in upcoming_sessions:
                s.pop("_sort_dt", None)
            upcoming_sessions = upcoming_sessions[:5]

        return ok({
            "readinessTrend": readiness_trend,
            "departmentRanking": department_ranking,
            "skillRadar": skill_radar,
            "improvementRing": improvement_ring,
            "upcomingSessions": upcoming_sessions,
        })

    # ==========================================================
    # STUDENT ROSTER — All Students list, single add, bulk import
    # and PDF/Excel export. Trainers manage the whole platform
    # roster (no trainer↔student binding), so the trainer picks the
    # college on the add form / per bulk row and every query here
    # is platform-wide.
    #
    # New students are created APPROVED (no approval queue) with a
    # server-generated temporary password that is returned EXACTLY
    # once so the trainer can hand it to the student securely.
    # firstLoginVerify forces the student to set their own password
    # + OTP-verify on first login (existing login.py flow). Cohort
    # starts at Entry Level (None) — the identical rule to
    # self-registration (login.py) and Super Admin bulk import.
    # ==========================================================
    @bp.route("/students/departments", methods=["GET"])
    @role_required("trainer")
    def trainer_student_departments():
        """Every active college with its active departments — feeds the
        Add Student college + dynamic department dropdowns."""
        depts = list(db.departments.find({"status": "active"}).sort("department_name", 1))
        by_college = {}
        for d in depts:
            by_college.setdefault(str(d.get("college_id")), []).append(department_public(d))
        colleges = []
        for c in db.colleges.find({"status": "active"}).sort("college_name", 1):
            colleges.append({
                "id": str(c["_id"]),
                "name": c.get("college_name"),
                "departments": by_college.get(str(c["_id"]), []),
            })
        return ok({"colleges": colleges})

    @bp.route("/students", methods=["POST"])
    @role_required("trainer")
    def create_student():
        data = request.get_json(silent=True) or {}
        full_name = (data.get("fullName") or "").strip()
        email = (data.get("email") or "").strip().lower()
        mobile = (data.get("mobile") or "").strip()
        roll_number = (data.get("rollNumber") or "").strip()
        department_id = data.get("departmentId")
        college_id = data.get("collegeId")

        if not full_name:
            return error("Full name is required.")
        if not roll_number:
            return error("Roll Number is required.")
        if not department_id:
            return error("Department is required.")
        if not college_id:
            return error("Select a college.")

        email_err = validate_email(email)
        if email_err:
            return error(email_err)
        mobile_err = validate_mobile(mobile)
        if mobile_err:
            return error(mobile_err)

        if users.find_one({"email": email}):
            return error("An account with this email already exists.")
        if users.find_one({"role": "student", "rollNumber": roll_number}):
            return error("A student with this Roll Number already exists.")

        # Exact college/department relationship check: the department must
        # actually belong to the chosen (active) college.
        college_doc = db.colleges.find_one({"_id": to_object_id(college_id), "status": "active"})
        if not college_doc:
            return error("Selected college is invalid or inactive.")
        department_doc = resolve_active_department(db, department_id, str(college_id))
        if not department_doc:
            return error("Selected department is invalid or inactive for the chosen college.")

        # Password: the trainer may set one (must confirm it) or let the
        # backend generate a random temporary password. Either way the
        # student must change it at first login (firstLoginVerify).
        password = data.get("password") or ""
        confirm_password = data.get("confirmPassword") or ""
        password_generated = False
        if password:
            if password != confirm_password:
                return error("Passwords do not match.")
            if len(password) < 6:
                return error("Password must be at least 6 characters.")
            temp_password = password
        else:
            temp_password = _generate_temp_password()
            password_generated = True

        if bcrypt is None:
            return error("Student creation is not available right now.", 503)

        password_hash = bcrypt.generate_password_hash(temp_password).decode("utf-8")
        user_doc = {
            "fullName": full_name,
            "email": email,
            "mobile": mobile,
            "role": "student",
            "passwordHash": password_hash,
            "approvalStatus": "approved",
            "approvedBy": f"trainer:{get_jwt_identity()}",
            "approvedDate": now(),
            "googleLogin": False,
            "isDeleted": False,
            "firstLoginVerify": True,
            "firstLoginVerifiedAt": None,
            "cohort": None,
            "baselineAssessmentScore": None,
            "interviewScore": None,
            "finalEmployabilityScore": None,
            "cohortAssignedAt": None,
            "rollNumber": roll_number,
            "tneaCode": None,
            "college": college_doc["college_name"],
            "collegeId": college_doc["_id"],
            "department": department_doc["department_name"],
            "departmentId": department_doc["_id"],
            "district": None,
            "employeeId": None,
            "createdAt": now(),
            "updatedAt": now(),
        }
        result = users.insert_one(user_doc)
        log_activity(
            db, get_jwt_identity(), "trainer", "student_added",
            f'Added student {full_name} ({roll_number}) to {department_doc["department_name"]}, {college_doc["college_name"]}',
            college=college_doc["college_name"], student_id=result.inserted_id,
            meta={"department": department_doc["department_name"], "college": college_doc["college_name"]},
        )
        return ok({
            "student": {
                "id": str(result.inserted_id),
                "name": full_name,
                "email": email,
                "rollNumber": roll_number,
                "college": college_doc["college_name"],
                "department": department_doc["department_name"],
            },
            "temporaryPassword": temp_password if password_generated else None,
            "passwordSet": not password_generated,
        }, message="Student created. They must set their own password at first login.", status=201)

    @bp.route("/students/bulk-import", methods=["POST"])
    @role_required("trainer")
    def bulk_import_students():
        data = request.get_json(silent=True) or {}
        rows = data.get("students") or []
        if not isinstance(rows, list) or not rows:
            return error("No student rows provided.")
        if len(rows) > 1000:
            return error("Maximum 1000 rows per import.")
        if bcrypt is None:
            return error("Student import is not available right now.", 503)

        colleges_by_id = {}
        colleges_by_name = {}
        for c in db.colleges.find({}):
            colleges_by_id[str(c["_id"])] = c
            colleges_by_name[str(c.get("college_name") or "").strip().lower()] = c

        existing_emails = {u["email"] for u in users.find(
            {"role": "student", "email": {"$ne": None}}, {"email": 1})}
        existing_rolls = {str(u["rollNumber"]).lower() for u in users.find(
            {"role": "student", "rollNumber": {"$ne": None}}, {"rollNumber": 1})}

        imported = 0
        rejected = []
        created = []
        batch_emails = set()
        batch_rolls = set()

        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                rejected.append({"index": i, "name": "", "reason": "Row is not an object."})
                continue
            name = (r.get("name") or "").strip()
            roll = (r.get("roll") or "").strip()
            email = (r.get("email") or "").strip().lower()
            phone = (r.get("phone") or "").strip()
            dept = (r.get("dept") or "").strip()
            college_ref = (r.get("college") or "").strip()

            reasons = []
            if not name:
                reasons.append("Missing name")
            if not roll:
                reasons.append("Missing roll number")
            email_err = validate_email(email) if email else "Missing email"
            if email_err:
                reasons.append(email_err)
            mobile_err = validate_mobile(phone) if phone else "Missing phone"
            if mobile_err:
                reasons.append(mobile_err)
            if not dept:
                reasons.append("Missing department")
            if not college_ref:
                reasons.append("Missing college")

            college_doc = None
            if college_ref:
                college_doc = colleges_by_id.get(college_ref) or colleges_by_name.get(college_ref.lower())
                if not college_doc:
                    reasons.append(f"Unknown college: {college_ref}")

            dept_doc = None
            if college_doc and dept:
                dept_doc = db.departments.find_one({
                    "college_id": college_doc["_id"],
                    "department_name": {"$regex": f"^{re.escape(dept)}$", "$options": "i"},
                    "status": "active",
                })
                if not dept_doc:
                    reasons.append(f"Department '{dept}' does not exist in {college_doc.get('college_name')}")

            if email:
                if email in existing_emails or email in batch_emails:
                    reasons.append("Email already registered")
            if roll:
                if roll.lower() in existing_rolls or roll.lower() in batch_rolls:
                    reasons.append("Roll number already exists")

            if reasons:
                rejected.append({"index": i, "name": name, "reason": "; ".join(reasons)})
                continue

            temp_password = _generate_temp_password()
            password_hash = bcrypt.generate_password_hash(temp_password).decode("utf-8")
            user_doc = {
                "fullName": name,
                "email": email,
                "mobile": phone,
                "role": "student",
                "passwordHash": password_hash,
                "approvalStatus": "approved",
                "approvedBy": f"trainer:{get_jwt_identity()}",
                "approvedDate": now(),
                "googleLogin": False,
                "isDeleted": False,
                "firstLoginVerify": True,
                "firstLoginVerifiedAt": None,
                "cohort": None,
                "baselineAssessmentScore": None,
                "interviewScore": None,
                "finalEmployabilityScore": None,
                "cohortAssignedAt": None,
                "rollNumber": roll,
                "tneaCode": None,
                "college": college_doc["college_name"],
                "collegeId": college_doc["_id"],
                "department": dept_doc["department_name"],
                "departmentId": dept_doc["_id"],
                "district": None,
                "employeeId": None,
                "createdAt": now(),
                "updatedAt": now(),
            }
            res = users.insert_one(user_doc)
            existing_emails.add(email)
            batch_emails.add(email)
            existing_rolls.add(roll.lower())
            batch_rolls.add(roll.lower())
            imported += 1
            created.append({
                "id": str(res.inserted_id), "name": name, "email": email,
                "temporaryPassword": temp_password,
            })

        if imported:
            log_activity(
                db, get_jwt_identity(), "trainer", "student_bulk_imported",
                f"Bulk imported {imported} student{'s' if imported != 1 else ''} "
                f"({len(rejected)} rejected)",
                meta={"imported": imported, "rejected": len(rejected)},
            )
        return ok({
            "imported": imported,
            "rejected": rejected,
            "students": created,
        }, message=f"{imported} student(s) imported.")

    @bp.route("/students/export", methods=["GET"])
    @role_required("trainer")
    def students_export():
        """Server-side PDF / Excel export of exactly what the All Students
        table is currently showing (same filters, same query)."""
        rows = _student_roster(
            college=(request.args.get("college") or "").strip(),
            department=(request.args.get("department") or "").strip(),
            cohort=(request.args.get("cohort") or "").strip(),
            search=(request.args.get("search") or "").strip(),
        )
        columns = ["Student Name", "Roll Number", "Email", "Mobile", "College",
                   "Department", "Cohort", "Attendance %", "Avg Quiz Score", "Status"]

        def _cohort_label(c):
            if c in {"A", "B", "C"}:
                return f"Cohort {c}"
            return c or "Entry Level"

        data_rows = [[
            r["fullName"], r["rollNumber"], r["email"], r["mobile"], r["college"],
            r["department"], _cohort_label(r["cohort"]),
            r["attendancePct"] if r["attendancePct"] is not None else "—",
            r["quizScore"] if r["quizScore"] is not None else "—",
            r["status"],
        ] for r in rows]
        fmt = (request.args.get("format") or "xlsx").lower()
        if fmt == "pdf":
            buf = pdf_bytes(
                "Student Directory",
                f"Generated {fmt_ist(now(), '%d %b %Y %H:%M')} IST · {len(data_rows)} students",
                columns, data_rows,
            )
            return send_file(buf, mimetype="application/pdf", as_attachment=True,
                             download_name="student_directory.pdf")
        buf = excel_bytes(columns, data_rows, "Students")
        return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name="student_directory.xlsx")

    return bp
