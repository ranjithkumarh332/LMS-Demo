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

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity

from quiz_common import (
    ok, error, role_required, now, to_object_id, serialize,
    VALID_COHORT_TARGETS, ENTRY_LEVEL,
    cohort_counts, record_interview_score_for_cohort,
    log_activity, get_recent_activity,
    list_quiz_results, set_quiz_interview_marks, validate_quiz_result,
    serialize_quiz_result, RESULT_STATUS_INTERVIEW_DONE, RESULT_STATUS_VALIDATED,
    compute_quiz_analytics, list_quiz_responses,
)
from colleges import resolve_active_department


def init_trainer(db):
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
        query = {"role": "student", "college": _trainer_college()}
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
        return ok({"responses": list_quiz_responses(db, college=college, cohort=cohort, quiz_id=quiz_id)})

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
        return ok(compute_quiz_analytics(db, college=_trainer_college(), cohort=cohort, quiz_id=quiz_id))

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
        submitted a Create-Quiz quiz, scoped to this trainer's college."""
        return ok({"results": list_quiz_results(db, college=_trainer_college())})

    @bp.route("/quiz-interview-verification", methods=["GET"])
    @role_required("trainer")
    def quiz_interview_verification():
        """Students eligible for interview verification: anyone who has
        submitted at least one quiz. Includes rows already scored (so the
        trainer can see/update them) — the frontend groups by `status`."""
        return ok({"results": list_quiz_results(db, college=_trainer_college())})

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
        i.e. interview marks have already been entered."""
        return ok({"results": list_quiz_results(
            db, college=_trainer_college(),
            statuses=[RESULT_STATUS_INTERVIEW_DONE, RESULT_STATUS_VALIDATED],
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
        college = _trainer_college()
        limit = int(request.args.get("limit", 20))
        rows = get_recent_activity(db, {"college": college}, limit=limit)
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
        college = _trainer_college()
        total_students = users.count_documents({"role": "student", "college": college})
        student_ids = [s["_id"] for s in users.find({"role": "student", "college": college}, {"_id": 1})]

        scores = [
            att["overall"]["percentage"]
            for att in attempts.find(
                {"status": "submitted", "studentId": {"$in": student_ids}},
                {"overall.percentage": 1},
            )
            if att.get("overall", {}).get("percentage") is not None
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
                    "date": att["submittedAt"].isoformat() if att.get("submittedAt") else None,
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

    return bp
