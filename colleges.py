"""
============================================================
 colleges.py — College & Department Management
============================================================
Registered at /api (so routes come out as /api/admin/colleges/...
and /api/public/colleges/...). Owns two brand-new collections:

  - db.colleges     { _id, college_name, status, created_at }
  - db.departments  { _id, college_id, department_name, status, created_at }

This is the SINGLE SOURCE OF TRUTH for every college/department
dropdown on the platform:

  - Student Registration            -> /api/public/colleges (+ departments)
  - Create Quiz (Super Admin)       -> /api/public/colleges (+ departments)
  - Create Quiz (Trainer)           -> /api/public/colleges (+ departments)
  - Super Admin > College Management -> /api/admin/colleges (+ departments)
  - Trainer college/department assignment after approval -> /api/admin/trainers/<id>/assign

Only Super Admin can create/update/delete colleges and departments.
The /api/public/* routes are read-only, unauthenticated (they power the
pre-login registration form) and only ever return status="active" rows.
============================================================
"""

from flask import Blueprint, request

from quiz_common import ok, error, role_required, now, to_object_id


# ------------------------------------------------------------
# Module-level resolvers — imported directly by login.py,
# superadmin.py and trainer.py so nobody re-implements this lookup.
# ------------------------------------------------------------
def resolve_active_college(db, college_id):
    """Return the college doc if college_id is a valid, ACTIVE college. Else None."""
    oid = to_object_id(college_id)
    if not oid:
        return None
    return db.colleges.find_one({"_id": oid, "status": "active"})


def resolve_active_department(db, department_id, college_id=None):
    """Return the department doc if department_id is valid, ACTIVE, and
    (when college_id is given) actually belongs to that college. Else None."""
    oid = to_object_id(department_id)
    if not oid:
        return None
    query = {"_id": oid, "status": "active"}
    if college_id:
        col_oid = to_object_id(college_id)
        if not col_oid:
            return None
        query["college_id"] = col_oid
    return db.departments.find_one(query)


def college_public(doc):
    if not doc:
        return None
    return {
        "_id": str(doc["_id"]),
        "college_name": doc.get("college_name"),
        "status": doc.get("status", "active"),
        "created_at": doc["created_at"].isoformat() if doc.get("created_at") else None,
    }


def department_public(doc):
    if not doc:
        return None
    return {
        "_id": str(doc["_id"]),
        "college_id": str(doc["college_id"]),
        "department_name": doc.get("department_name"),
        "status": doc.get("status", "active"),
        "created_at": doc["created_at"].isoformat() if doc.get("created_at") else None,
    }


def init_colleges(db):
    bp = Blueprint("colleges", __name__)

    colleges = db.colleges
    departments = db.departments
    users = db.users

    # ==========================================================
    # SUPER ADMIN — COLLEGE MANAGEMENT (CRUD)
    # ==========================================================
    @bp.route("/admin/colleges", methods=["GET"])
    @role_required("super_admin")
    def admin_list_colleges():
        docs = colleges.find({}).sort("college_name", 1)
        return ok({"colleges": [college_public(d) for d in docs]})

    @bp.route("/admin/colleges", methods=["POST"])
    @role_required("super_admin")
    def admin_add_college():
        data = request.get_json(silent=True) or {}
        name = (data.get("collegeName") or data.get("college_name") or "").strip()
        if not name:
            return error("College name is required.")
        if colleges.find_one({"college_name": {"$regex": f"^{name}$", "$options": "i"}}):
            return error("A college with this name already exists.")
        doc = {"college_name": name, "status": "active", "created_at": now()}
        result = colleges.insert_one(doc)
        doc["_id"] = result.inserted_id
        return ok({"college": college_public(doc)}, message="College added.", status=201)

    @bp.route("/admin/colleges/<college_id>", methods=["PATCH"])
    @role_required("super_admin")
    def admin_update_college(college_id):
        oid = to_object_id(college_id)
        if not oid:
            return error("Invalid college id.", 404)
        data = request.get_json(silent=True) or {}
        update = {}
        if "collegeName" in data or "college_name" in data:
            name = (data.get("collegeName") or data.get("college_name") or "").strip()
            if not name:
                return error("College name cannot be empty.")
            update["college_name"] = name
        if "status" in data:
            status = (data.get("status") or "").strip().lower()
            if status not in ("active", "inactive"):
                return error("status must be 'active' or 'inactive'.")
            update["status"] = status
        if not update:
            return error("Nothing to update.")
        update["updated_at"] = now()
        result = colleges.update_one({"_id": oid}, {"$set": update})
        if result.matched_count == 0:
            return error("College not found.", 404)
        return ok(message="College updated.")

    @bp.route("/admin/colleges/<college_id>", methods=["DELETE"])
    @role_required("super_admin")
    def admin_delete_college(college_id):
        oid = to_object_id(college_id)
        if not oid:
            return error("Invalid college id.", 404)
        result = colleges.delete_one({"_id": oid})
        if result.deleted_count == 0:
            return error("College not found.", 404)
        departments.delete_many({"college_id": oid})
        return ok(message="College and its departments deleted.")

    # ==========================================================
    # SUPER ADMIN — DEPARTMENT MANAGEMENT (CRUD), scoped to a college
    # ==========================================================
    @bp.route("/admin/colleges/<college_id>/departments", methods=["GET"])
    @role_required("super_admin")
    def admin_list_departments(college_id):
        oid = to_object_id(college_id)
        if not oid:
            return error("Invalid college id.", 404)
        docs = departments.find({"college_id": oid}).sort("department_name", 1)
        return ok({"departments": [department_public(d) for d in docs]})

    @bp.route("/admin/colleges/<college_id>/departments", methods=["POST"])
    @role_required("super_admin")
    def admin_add_department(college_id):
        oid = to_object_id(college_id)
        if not oid:
            return error("Invalid college id.", 404)
        if not colleges.find_one({"_id": oid}):
            return error("College not found.", 404)
        data = request.get_json(silent=True) or {}
        name = (data.get("departmentName") or data.get("department_name") or "").strip()
        if not name:
            return error("Department name is required.")
        if departments.find_one(
            {"college_id": oid, "department_name": {"$regex": f"^{name}$", "$options": "i"}}
        ):
            return error("This department already exists for the selected college.")
        doc = {
            "college_id": oid,
            "department_name": name,
            "status": "active",
            "created_at": now(),
        }
        result = departments.insert_one(doc)
        doc["_id"] = result.inserted_id
        return ok({"department": department_public(doc)}, message="Department added.", status=201)

    @bp.route("/admin/departments/<department_id>", methods=["PATCH"])
    @role_required("super_admin")
    def admin_update_department(department_id):
        oid = to_object_id(department_id)
        if not oid:
            return error("Invalid department id.", 404)
        data = request.get_json(silent=True) or {}
        update = {}
        if "departmentName" in data or "department_name" in data:
            name = (data.get("departmentName") or data.get("department_name") or "").strip()
            if not name:
                return error("Department name cannot be empty.")
            update["department_name"] = name
        if "status" in data:
            status = (data.get("status") or "").strip().lower()
            if status not in ("active", "inactive"):
                return error("status must be 'active' or 'inactive'.")
            update["status"] = status
        if not update:
            return error("Nothing to update.")
        update["updated_at"] = now()
        result = departments.update_one({"_id": oid}, {"$set": update})
        if result.matched_count == 0:
            return error("Department not found.", 404)
        return ok(message="Department updated.")

    @bp.route("/admin/departments/<department_id>", methods=["DELETE"])
    @role_required("super_admin")
    def admin_delete_department(department_id):
        oid = to_object_id(department_id)
        if not oid:
            return error("Invalid department id.", 404)
        result = departments.delete_one({"_id": oid})
        if result.deleted_count == 0:
            return error("Department not found.", 404)
        return ok(message="Department deleted.")

    # ==========================================================
    # TRAINER DIRECTORY — read-only list of approved trainers, DB-backed.
    # Powers the "All Trainers" dropdowns on Assessment Management ->
    # Quiz Management and Quiz Responses (Super Admin + Trainer), which
    # previously had no live data source. Only approved trainer accounts
    # are returned; nothing here is hardcoded, so the list stays in sync
    # automatically as trainers register/get approved.
    # ==========================================================
    @bp.route("/admin/trainers", methods=["GET"])
    @role_required("super_admin", "trainer")
    def admin_list_trainers():
        docs = users.find(
            {"role": "trainer", "approvalStatus": "approved"}
        ).sort("fullName", 1)

        def _trainer_public(d):
            name = d.get("fullName") or "—"
            # Both key styles are populated on purpose: super_admin.html's
            # dropdowns read t.id / t.name, while trainer.html's existing
            # loadTrainerOptions() (workshop scheduling) already reads
            # t._id / t.fullName. Keeping both means neither frontend file
            # has to change how it accesses this response.
            return {
                "id": str(d["_id"]),
                "_id": str(d["_id"]),
                "name": name,
                "fullName": name,
                "email": d.get("email"),
                "college": d.get("college"),
                "department": d.get("department"),
                "status": "active",
            }

        return ok({"trainers": [_trainer_public(d) for d in docs]})

    # ==========================================================
    # SUPER ADMIN — assign college/department to an approved Trainer.
    # Trainers no longer choose these at registration time (per Phase 1
    # requirements); this closes the loop after approval.
    # ==========================================================
    @bp.route("/admin/trainers/<user_id>/assign", methods=["PUT"])
    @role_required("super_admin")
    def assign_trainer_college(user_id):
        oid = to_object_id(user_id)
        if not oid:
            return error("Invalid user id.", 404)
        trainer = users.find_one({"_id": oid, "role": "trainer"})
        if not trainer:
            return error("Trainer not found.", 404)

        data = request.get_json(silent=True) or {}
        college_id = data.get("collegeId")
        department_id = data.get("departmentId")
        if not college_id and not department_id:
            return error("Provide collegeId and/or departmentId to assign.")

        update = {"updatedAt": now()}

        effective_college_id = college_id or (
            str(trainer["collegeId"]) if trainer.get("collegeId") else None
        )

        if college_id:
            college_doc = resolve_active_college(db, college_id)
            if not college_doc:
                return error("Selected college is invalid or inactive.")
            update["college"] = college_doc["college_name"]
            update["collegeId"] = college_doc["_id"]

        if department_id:
            dept_doc = resolve_active_department(db, department_id, effective_college_id)
            if not dept_doc:
                return error("Selected department is invalid or inactive for this college.")
            update["department"] = dept_doc["department_name"]
            update["departmentId"] = dept_doc["_id"]

        users.update_one({"_id": oid}, {"$set": update})
        return ok(message="Trainer college/department assigned.")

    # ==========================================================
    # PUBLIC — read-only dropdowns. No auth required (pre-login
    # registration form needs these). Only ACTIVE rows are returned.
    # ==========================================================
    @bp.route("/public/colleges", methods=["GET"])
    def public_colleges():
        docs = colleges.find({"status": "active"}).sort("college_name", 1)
        return ok({"colleges": [college_public(d) for d in docs]})

    @bp.route("/public/colleges/<college_id>/departments", methods=["GET"])
    def public_departments(college_id):
        oid = to_object_id(college_id)
        if not oid:
            return error("Invalid college id.", 404)
        docs = departments.find({"college_id": oid, "status": "active"}).sort("department_name", 1)
        return ok({"departments": [department_public(d) for d in docs]})

    return bp
