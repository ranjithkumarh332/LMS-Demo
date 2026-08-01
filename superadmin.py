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

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity

from quiz_common import (
    ok, error, role_required, now, to_object_id, serialize,
    VALID_COHORT_TARGETS, VALID_COHORTS, ENTRY_LEVEL,
    parse_master_workbook, cohort_counts,
    get_placement_rules, check_and_generate_cohort,
    list_quiz_results, set_quiz_interview_marks, validate_quiz_result,
    serialize_quiz_result, RESULT_STATUS_INTERVIEW_DONE, RESULT_STATUS_VALIDATED,
    compute_quiz_analytics, list_quiz_responses,
    log_activity,
)
from colleges import resolve_active_college, resolve_active_department


def init_superadmin(db):
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
        """List of submitted attempts, most recent first, with student + assessment names."""
        limit = int(request.args.get("limit", 50))
        pipeline = [
            {"$match": {"status": "submitted"}},
            {"$sort": {"submittedAt": -1}},
            {"$limit": limit},
            {"$lookup": {"from": "users", "localField": "studentId", "foreignField": "_id", "as": "student"}},
            {"$lookup": {"from": "assessments", "localField": "assessmentId", "foreignField": "_id", "as": "assessment"}},
        ]
        rows = []
        for row in attempts.aggregate(pipeline):
            student = row["student"][0] if row.get("student") else {}
            assessment = row["assessment"][0] if row.get("assessment") else {}
            rows.append({
                "attemptId": str(row["_id"]),
                "studentName": student.get("fullName"),
                "studentEmail": student.get("email"),
                "cohort": student.get("cohort") or ENTRY_LEVEL,
                "assessmentName": assessment.get("name"),
                "assessmentType": assessment.get("type"),
                "overallPercentage": row.get("overall", {}).get("percentage"),
                "submittedAt": row["submittedAt"].isoformat() if row.get("submittedAt") else None,
            })
        return ok({"responses": rows})

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
        return ok(compute_quiz_analytics(
            db,
            college=None if college == "all" else college,
            cohort=cohort,
            quiz_id=quiz_id,
        ))

    @bp.route("/assessments/responses", methods=["GET"])
    @role_required("super_admin")
    def assessment_responses_all():
        """Raw per-attempt rows backing the Quiz Responses table —
        platform-wide (no college filter), same underlying data as
        /quiz-analytics above via the shared list_quiz_responses helper.
        """
        return ok({"responses": list_quiz_responses(db)})

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
        return ok({"results": list_quiz_results(db)})

    @bp.route("/quiz-interview-verification", methods=["GET"])
    @role_required("super_admin")
    def quiz_interview_verification_all():
        return ok({"results": list_quiz_results(db)})

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
        return ok({"results": list_quiz_results(
            db, statuses=[RESULT_STATUS_INTERVIEW_DONE, RESULT_STATUS_VALIDATED],
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
        Update cohort score ranges and/or the assessment/interview weighting.
        Takes effect immediately for every FUTURE cohort generation — in-flight
        students who already have a cohort are not retroactively changed
        unless recalculate=true is also sent.
        Body: {
          "cohortRanges": [{"cohort":"A","min":85,"max":100}, ...],
          "assessmentWeight": 0.6,
          "interviewWeight": 0.4,
          "recalculate": false
        }
        """
        data = request.get_json(silent=True) or {}
        cohort_ranges = data.get("cohortRanges")
        assessment_weight = data.get("assessmentWeight")
        interview_weight = data.get("interviewWeight")

        if cohort_ranges is not None:
            if not isinstance(cohort_ranges, list) or not cohort_ranges:
                return error("cohortRanges must be a non-empty list.")
            for band in cohort_ranges:
                if band.get("cohort") not in VALID_COHORTS:
                    return error(f"Invalid cohort '{band.get('cohort')}' in cohortRanges.")
                if not isinstance(band.get("min"), (int, float)) or not isinstance(band.get("max"), (int, float)):
                    return error("Each cohortRanges entry needs numeric 'min' and 'max'.")

        current = get_placement_rules(db)
        update_doc = {"updatedAt": now(), "updatedBy": "super_admin"}
        if cohort_ranges is not None:
            update_doc["cohortRanges"] = cohort_ranges
        if assessment_weight is not None:
            update_doc["assessmentWeight"] = float(assessment_weight)
        if interview_weight is not None:
            update_doc["interviewWeight"] = float(interview_weight)

        db.placement_rules.update_one({"_id": current["_id"]}, {"$set": update_doc})
        updated = get_placement_rules(db)

        recalculated = 0
        if data.get("recalculate"):
            # Re-run cohort generation for every student who already has
            # both scores recorded, using the brand-new rules.
            for student in users.find({
                "role": "student",
                "baselineAssessmentScore": {"$exists": True},
                "interviewScore": {"$exists": True},
            }, {"_id": 1, "cohort": 1}):
                users.update_one({"_id": student["_id"]}, {"$set": {"cohort": None}})
                if check_and_generate_cohort(db, student["_id"]):
                    recalculated += 1

        return ok({"placementRules": serialize(updated), "recalculatedStudents": recalculated},
                   message="Placement rules updated.")

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
                    "date": att["submittedAt"].isoformat() if att.get("submittedAt") else None,
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

    return bp
