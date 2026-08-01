"""
============================================================
 collegeadmin.py — College Admin Dashboard backend
============================================================
Registered at /api/collegeadmin. Read-only Assessment Management for
the College Admin role, scoped to that admin's own assigned college.

Reuses the exact same data the Trainer/Super Admin "Create Quiz"
feature already writes (db.quizzes / db.quiz_attempts, see
quiz_module.py + student.py) — nothing here is a separate/parallel
data source, so College Admin can never see numbers that disagree
with what Trainers and students actually created/submitted.

Every route is @role_required("college_admin") and every route in
this file is a GET — there is intentionally no create/update/delete
here. College Admin can view assessments and student responses for
their college; they cannot create, edit, delete, publish, schedule,
or modify marks. That authority stays with Trainer/Super Admin
(quiz_module.py) exactly as before.
"""

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity

from quiz_common import ok, error, role_required, to_object_id
from quiz_module import serialize_quiz, normalize_quiz_college_names


def init_collegeadmin(db):
    bp = Blueprint("collegeadmin", __name__)

    users = db.users
    quizzes = db.quizzes
    quiz_attempts = db.quiz_attempts

    def _admin_doc():
        return users.find_one({"_id": to_object_id(get_jwt_identity())})

    def _admin_college():
        return (_admin_doc() or {}).get("college")

    def _quiz_in_college_scope(quiz, college):
        """Mirrors student.py's _quiz_eligible college check: an empty
        collegeNames/colleges list means the quiz targets every college,
        otherwise the admin's college name must appear in that list."""
        allowed = quiz.get("collegeNames") or quiz.get("colleges") or []
        if not allowed:
            return True
        allowed_l = {str(c).strip().lower() for c in allowed}
        return str(college).strip().lower() in allowed_l

    # ==========================================================
    # ASSESSMENT MANAGEMENT — list, scoped to the admin's college
    # ==========================================================
    @bp.route("/assessments", methods=["GET"])
    @role_required("college_admin")
    def list_assessments():
        college = _admin_college()
        if not college:
            return ok({"assessments": [], "college": None})

        rows = []
        for doc in quizzes.find({}).sort("createdOn", -1):
            doc = normalize_quiz_college_names(db, doc)
            if not _quiz_in_college_scope(doc, college):
                continue
            s = serialize_quiz(doc)
            created_by = doc.get("createdBy") or {}
            response_count = quiz_attempts.count_documents({
                "quizId": doc["_id"],
                "status": "submitted",
                "college": college,
            })
            rows.append({
                "id": s["id"],
                "name": s.get("title"),
                "createdBy": created_by.get("name") or created_by.get("role") or "—",
                "status": s.get("status"),
                "totalQuestions": s.get("totalQuestions"),
                "totalMarks": s.get("totalMarks"),
                "scheduledDate": s.get("startDateTime"),
                "endDate": s.get("endDateTime"),
                "responseCount": response_count,
            })
        return ok({"assessments": rows, "college": college})

    # ==========================================================
    # STUDENT RESPONSES — one assessment's submitted attempts,
    # scoped to the admin's college. Read-only: marks/results are
    # entered/validated by Trainer only (see trainer.py).
    # ==========================================================
    @bp.route("/assessments/<assessment_id>/responses", methods=["GET"])
    @role_required("college_admin")
    def assessment_responses(assessment_id):
        college = _admin_college()
        oid = to_object_id(assessment_id)
        if not oid:
            return error("Invalid assessment id.", 404)

        quiz = quizzes.find_one({"_id": oid})
        if not quiz:
            return error("Assessment not found.", 404)
        quiz = normalize_quiz_college_names(db, quiz)

        if not college or not _quiz_in_college_scope(quiz, college):
            return error("Assessment not found.", 404)

        cursor = quiz_attempts.find({
            "quizId": oid,
            "status": "submitted",
            "college": college,
        }).sort("submittedAt", -1)

        responses = []
        for a in cursor:
            overall = a.get("overall") or {}
            submitted_at = a.get("submittedAt")
            responses.append({
                "attemptId": str(a["_id"]),
                "studentId": str(a["studentId"]) if a.get("studentId") else None,
                "studentName": a.get("studentName"),
                "registerNumber": a.get("studentRollNumber"),
                "department": a.get("department"),
                "marksObtained": overall.get("marksObtained"),
                "totalMarks": overall.get("totalMarks"),
                "percentage": overall.get("percentage"),
                "submissionStatus": "Submitted",
                "submittedAt": submitted_at.isoformat() if submitted_at else None,
            })

        return ok({
            "assessment": {
                "id": str(quiz["_id"]),
                "name": quiz.get("title"),
            },
            "responses": responses,
        })

    return bp
