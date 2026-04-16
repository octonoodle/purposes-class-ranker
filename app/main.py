import hashlib
from urllib.parse import urlencode
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from .database import Base, engine, get_db, wait_for_db_ready
from .models import Class, Preference, Student

app = FastAPI(title="Class Ranker")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def redirect_with_message(
    path: str,
    message: str,
    extra_params: dict[str, int | str] | None = None,
) -> RedirectResponse:
    params: dict[str, int | str] = {"message": message}
    if extra_params:
        params.update(extra_params)
    return RedirectResponse(
        url=f"{path}?{urlencode(params)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def set_recorder_cookie(response: RedirectResponse, recorded_by: str) -> RedirectResponse:
    normalized = recorded_by.strip()
    if normalized:
        response.set_cookie(
            key="recorder_name",
            value=normalized,
            max_age=60 * 60 * 24 * 365,
            samesite="lax",
        )
    return response


def normalize_class_name(class_name: str) -> str:
    return " ".join(class_name.strip().lower().split())


def class_code_from_name(class_name: str) -> str:
    normalized = normalize_class_name(class_name)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:48]


def ensure_preference_recorded_by_column() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE preferences "
                "ADD COLUMN IF NOT EXISTS recorded_by VARCHAR(200) NOT NULL DEFAULT 'Unknown'"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_preferences_recorded_by "
                "ON preferences (recorded_by)"
            )
        )


def ensure_students_name_index() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_students_name_lower "
                "ON students (lower(name))"
            )
        )


def backfill_students_from_preferences() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO students (name) "
                "SELECT MIN(BTRIM(student_name)) "
                "FROM preferences "
                "WHERE student_name IS NOT NULL AND BTRIM(student_name) <> '' "
                "GROUP BY lower(BTRIM(student_name)) "
                "ON CONFLICT DO NOTHING"
            )
        )


def backfill_class_codes_from_names() -> None:
    with Session(engine) as db:
        with db.begin():
            classes = db.execute(
                select(Class).order_by(Class.id.asc()).with_for_update()
            ).scalars().all()
            if not classes:
                return

            normalized_counts: dict[str, int] = {}
            for class_row in classes:
                normalized_name = normalize_class_name(class_row.class_name)
                if not normalized_name:
                    continue
                normalized_counts[normalized_name] = normalized_counts.get(normalized_name, 0) + 1

            for class_row in classes:
                normalized_name = normalize_class_name(class_row.class_name)
                if not normalized_name:
                    continue
                if normalized_counts.get(normalized_name, 0) != 1:
                    continue
                class_row.class_code = class_code_from_name(class_row.class_name)


def get_student_name_options(db: Session) -> list[str]:
    student_names = db.execute(select(Student.name).order_by(Student.name.asc())).scalars().all()
    legacy_names = db.execute(
        select(Preference.student_name)
        .distinct()
        .order_by(Preference.student_name.asc())
    ).scalars().all()

    names_by_lower: dict[str, str] = {}
    for name in student_names + legacy_names:
        normalized = name.strip()
        if not normalized:
            continue
        lower_key = normalized.lower()
        if lower_key not in names_by_lower:
            names_by_lower[lower_key] = normalized

    return sorted(names_by_lower.values(), key=lambda value: value.lower())


def upsert_student_name(db: Session, student_name: str) -> None:
    normalized = student_name.strip()
    if not normalized:
        return

    existing_student = db.execute(
        select(Student)
        .where(func.lower(Student.name) == normalized.lower())
        .with_for_update()
        .limit(1)
    ).scalar_one_or_none()
    if existing_student is None:
        db.add(Student(name=normalized))


@app.on_event("startup")
def startup() -> None:
    wait_for_db_ready()
    Base.metadata.create_all(bind=engine)
    ensure_preference_recorded_by_column()
    ensure_students_name_index()
    backfill_students_from_preferences()
    backfill_class_codes_from_names()


@app.get("/")
def home() -> RedirectResponse:
    return RedirectResponse(url="/preferences", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/classes")
def classes_page(request: Request, db: Session = Depends(get_db), message: str | None = None) -> Any:
    classes = db.execute(select(Class).order_by(Class.class_name.asc(), Class.class_code.asc())).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="classes.html",
        context={"classes": classes, "message": message},
    )


@app.post("/classes")
def create_class(
    class_name: str = Form(...),
    teacher_name: str = Form(...),
    required_grade: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    normalized_name = class_name.strip()
    normalized_teacher = teacher_name.strip()
    if required_grade < 0 or required_grade > 12:
        return RedirectResponse(
            url="/classes?message=Required+grade+must+be+between+0+and+12",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if not normalized_name or not normalized_teacher:
        return RedirectResponse(
            url="/classes?message=Class+name+and+teacher+name+are+required",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    new_class = Class(
        class_code=class_code_from_name(normalized_name),
        class_name=normalized_name,
        teacher_name=normalized_teacher,
        required_grade=required_grade,
    )

    try:
        with db.begin():
            db.add(new_class)
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url="/classes?message=Class+name+already+exists+(ID+is+a+hash+of+class+name)",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    return RedirectResponse(url="/classes?message=Class+saved", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/preferences")
def preferences_page(
    request: Request,
    db: Session = Depends(get_db),
    message: str | None = None,
    lookup_student_name: str | None = None,
    confirm_needed: int | None = None,
    pending_recorded_by: str | None = None,
    pending_student_name: str | None = None,
    pending_student_grade: int | None = None,
    pending_good_class_id: int | None = None,
    pending_bad_class_id: int | None = None,
) -> Any:
    classes = db.execute(select(Class).order_by(Class.class_name.asc())).scalars().all()
    class_by_id = {row.id: row for row in classes}
    cookie_recorded_by = request.cookies.get("recorder_name", "")
    normalized_lookup_student_name = (lookup_student_name or "").strip()
    student_name_options = get_student_name_options(db)

    good_cls = aliased(Class)
    bad_cls = aliased(Class)
    recent_preferences = db.execute(
        select(
            Preference,
            good_cls.class_name.label("good_class_name"),
            bad_cls.class_name.label("bad_class_name"),
        )
        .join(good_cls, good_cls.id == Preference.good_class_id)
        .join(bad_cls, bad_cls.id == Preference.bad_class_id)
        .order_by(Preference.created_at.desc())
        .limit(50)
    ).all()

    student_preferences = []
    if normalized_lookup_student_name:
        good_lookup_cls = aliased(Class)
        bad_lookup_cls = aliased(Class)
        student_preferences = db.execute(
            select(
                Preference,
                good_lookup_cls.class_name.label("good_class_name"),
                bad_lookup_cls.class_name.label("bad_class_name"),
                good_lookup_cls.class_code.label("good_class_code"),
                bad_lookup_cls.class_code.label("bad_class_code"),
            )
            .join(good_lookup_cls, good_lookup_cls.id == Preference.good_class_id)
            .join(bad_lookup_cls, bad_lookup_cls.id == Preference.bad_class_id)
            .where(func.lower(Preference.student_name) == normalized_lookup_student_name.lower())
            .order_by(Preference.created_at.desc(), Preference.id.desc())
        ).all()

    return templates.TemplateResponse(
        request=request,
        name="preferences.html",
        context={
            "classes": classes,
            "recent_preferences": recent_preferences,
            "student_name_options": student_name_options,
            "lookup_student_name": normalized_lookup_student_name,
            "student_preferences": student_preferences,
            "message": message,
            "confirm_needed": bool(confirm_needed),
            "pending_recorded_by": pending_recorded_by,
            "pending_student_name": pending_student_name,
            "pending_student_grade": pending_student_grade,
            "pending_good_class_id": pending_good_class_id,
            "pending_bad_class_id": pending_bad_class_id,
            "initial_recorded_by": (pending_recorded_by or cookie_recorded_by),
            "pending_good_class_label": (
                f"{class_by_id[pending_good_class_id].class_name} ({class_by_id[pending_good_class_id].class_code}) - {class_by_id[pending_good_class_id].teacher_name}"
                if pending_good_class_id in class_by_id
                else None
            ),
            "pending_bad_class_label": (
                f"{class_by_id[pending_bad_class_id].class_name} ({class_by_id[pending_bad_class_id].class_code}) - {class_by_id[pending_bad_class_id].teacher_name}"
                if pending_bad_class_id in class_by_id
                else None
            ),
        },
    )


@app.get("/api/students/lookup")
def lookup_student(name: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    normalized_name = name.strip()
    if not normalized_name:
        return {"found": False}

    student_row = db.execute(
        select(Student).where(func.lower(Student.name) == normalized_name.lower()).limit(1)
    ).scalar_one_or_none()
    canonical_name = student_row.name if student_row is not None else None
    if canonical_name is None:
        canonical_name = db.execute(
            select(Preference.student_name)
            .where(func.lower(Preference.student_name) == normalized_name.lower())
            .order_by(Preference.created_at.desc(), Preference.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    if canonical_name is None:
        return {"found": False}

    latest_preference = db.execute(
        select(Preference)
        .where(func.lower(Preference.student_name) == canonical_name.lower())
        .order_by(Preference.created_at.desc(), Preference.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_preference is None:
        return {
            "found": True,
            "student_name": canonical_name,
            "latest_preference": None,
        }

    return {
        "found": True,
        "student_name": canonical_name,
        "latest_preference": {
            "student_grade": latest_preference.student_grade,
            "good_class_id": latest_preference.good_class_id,
            "bad_class_id": latest_preference.bad_class_id,
            "recorded_by": latest_preference.recorded_by,
            "recorded_at": (
                latest_preference.created_at.isoformat()
                if latest_preference.created_at is not None
                else None
            ),
        },
    }


@app.post("/preferences")
def create_preference(
    recorded_by: str = Form(...),
    student_name: str = Form(...),
    student_grade: int = Form(...),
    good_class_id: int = Form(...),
    bad_class_id: int = Form(...),
    confirm_overwrite: bool = Form(False),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    overwritten = False
    normalized_recorded_by = recorded_by.strip()
    normalized_student_name = student_name.strip()

    def preference_redirect(message: str) -> RedirectResponse:
        query_string = urlencode(
            {
                "message": message,
                "pending_recorded_by": normalized_recorded_by,
            }
        )
        return set_recorder_cookie(
            RedirectResponse(
                url=f"/preferences?{query_string}",
                status_code=status.HTTP_303_SEE_OTHER,
            ),
            normalized_recorded_by,
        )

    if not normalized_recorded_by:
        return preference_redirect("Recorder name is required")

    if not normalized_student_name:
        return preference_redirect("Student name is required")

    if good_class_id == bad_class_id:
        return preference_redirect("Good class and bad class must be different")

    if student_grade < 9 or student_grade > 12:
        return preference_redirect("Student grade must be between 9 and 12")

    try:
        with db.begin():
            class_ids = db.execute(
                select(Class.id)
                .where(Class.id.in_([good_class_id, bad_class_id]))
                .with_for_update(read=True)
            ).scalars().all()
            if len(set(class_ids)) != 2:
                raise HTTPException(status_code=400, detail="Selected classes do not exist")

            duplicate_preference = db.execute(
                select(Preference.id)
                .where(func.lower(Preference.student_name) == normalized_student_name.lower())
                .where(Preference.good_class_id == good_class_id)
                .where(Preference.bad_class_id == bad_class_id)
                .with_for_update()
                .limit(1)
            ).scalar_one_or_none()
            if duplicate_preference is not None:
                return preference_redirect("Duplicate preference already exists for this student")

            conflicting_preference = db.execute(
                select(Preference)
                .where(func.lower(Preference.student_name) == normalized_student_name.lower())
                .where(Preference.good_class_id == bad_class_id)
                .where(Preference.bad_class_id == good_class_id)
                .order_by(Preference.created_at.desc(), Preference.id.desc())
                .with_for_update()
                .limit(1)
            ).scalar_one_or_none()

            if conflicting_preference is not None and not confirm_overwrite:
                query_string = urlencode(
                    {
                        "message": "Conflicting preference found. Confirm to overwrite",
                        "confirm_needed": 1,
                        "pending_recorded_by": normalized_recorded_by,
                        "pending_student_name": normalized_student_name,
                        "pending_student_grade": student_grade,
                        "pending_good_class_id": good_class_id,
                        "pending_bad_class_id": bad_class_id,
                    }
                )
                return set_recorder_cookie(
                    RedirectResponse(
                    url=f"/preferences?{query_string}",
                    status_code=status.HTTP_303_SEE_OTHER,
                    ),
                    normalized_recorded_by,
                )

            upsert_student_name(db, normalized_student_name)
            if conflicting_preference is not None:
                conflicting_preference.recorded_by = normalized_recorded_by
                conflicting_preference.student_name = normalized_student_name
                conflicting_preference.student_grade = student_grade
                conflicting_preference.good_class_id = good_class_id
                conflicting_preference.bad_class_id = bad_class_id
                overwritten = True
            else:
                db.add(
                    Preference(
                        recorded_by=normalized_recorded_by,
                        student_name=normalized_student_name,
                        student_grade=student_grade,
                        good_class_id=good_class_id,
                        bad_class_id=bad_class_id,
                    )
                )
    except HTTPException:
        db.rollback()
        return preference_redirect("Selected classes do not exist")
    except IntegrityError:
        db.rollback()
        return preference_redirect("Could not save preference")

    if overwritten:
        return preference_redirect("Preference overwritten")

    return preference_redirect("Preference saved")


@app.get("/edit")
def edit_page(
    request: Request,
    db: Session = Depends(get_db),
    message: str | None = None,
    pref_page: int = 1,
) -> Any:
    page_size = 5
    safe_pref_page = pref_page if pref_page > 0 else 1
    classes = db.execute(select(Class).order_by(Class.class_name.asc(), Class.class_code.asc())).scalars().all()
    student_name_options = get_student_name_options(db)
    total_preferences = db.execute(select(func.count(Preference.id))).scalar_one()
    total_pref_pages = max(1, (total_preferences + page_size - 1) // page_size)
    if safe_pref_page > total_pref_pages:
        safe_pref_page = total_pref_pages
    offset = (safe_pref_page - 1) * page_size
    preferences = db.execute(
        select(Preference).order_by(Preference.created_at.desc(), Preference.id.desc())
        .limit(page_size)
        .offset(offset)
    ).scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="edit.html",
        context={
            "classes": classes,
            "preferences": preferences,
            "student_name_options": student_name_options,
            "message": message,
            "pref_page": safe_pref_page,
            "total_pref_pages": total_pref_pages,
            "has_newer": safe_pref_page > 1,
            "has_older": safe_pref_page < total_pref_pages,
            "newer_page": safe_pref_page - 1,
            "older_page": safe_pref_page + 1,
        },
    )


@app.post("/edit/classes/{class_id}")
def edit_class_record(
    class_id: int,
    action: str = Form(...),
    class_name: str = Form(""),
    teacher_name: str = Form(""),
    required_grade: int = Form(0),
    pref_page: int = Form(1),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    page_param = {"pref_page": pref_page if pref_page > 0 else 1}
    try:
        with db.begin():
            class_row = db.execute(
                select(Class).where(Class.id == class_id).with_for_update()
            ).scalar_one_or_none()
            if class_row is None:
                raise HTTPException(status_code=404, detail="Class not found")

            if action == "delete":
                db.delete(class_row)
            elif action == "update":
                normalized_name = class_name.strip()
                normalized_teacher = teacher_name.strip()
                if not normalized_name or not normalized_teacher:
                    return redirect_with_message("/edit", "Class fields cannot be empty", page_param)
                if required_grade < 0 or required_grade > 12:
                    return redirect_with_message("/edit", "Required grade must be between 0 and 12", page_param)

                class_row.class_code = class_code_from_name(normalized_name)
                class_row.class_name = normalized_name
                class_row.teacher_name = normalized_teacher
                class_row.required_grade = required_grade
            else:
                return redirect_with_message("/edit", "Invalid class action", page_param)
    except HTTPException:
        db.rollback()
        return redirect_with_message("/edit", "Class not found", page_param)
    except IntegrityError:
        db.rollback()
        if action == "delete":
            return redirect_with_message("/edit", "Cannot delete class while preferences reference it", page_param)
        return redirect_with_message("/edit", "Could not update class (duplicate class name or invalid value)", page_param)

    if action == "delete":
        return redirect_with_message("/edit", "Class deleted", page_param)
    return redirect_with_message("/edit", "Class updated", page_param)


@app.post("/edit/preferences/{preference_id}")
def edit_preference_record(
    preference_id: int,
    action: str = Form(...),
    recorded_by: str = Form(""),
    student_name: str = Form(""),
    student_grade: int = Form(0),
    good_class_id: int = Form(0),
    bad_class_id: int = Form(0),
    pref_page: int = Form(1),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    page_param = {"pref_page": pref_page if pref_page > 0 else 1}
    try:
        with db.begin():
            preference_row = db.execute(
                select(Preference).where(Preference.id == preference_id).with_for_update()
            ).scalar_one_or_none()
            if preference_row is None:
                raise HTTPException(status_code=404, detail="Preference not found")

            if action == "delete":
                db.delete(preference_row)
            elif action == "update":
                normalized_recorded_by = recorded_by.strip()
                normalized_student_name = student_name.strip()
                if not normalized_recorded_by:
                    return redirect_with_message("/edit", "Recorder name is required", page_param)
                if not normalized_student_name:
                    return redirect_with_message("/edit", "Student name is required", page_param)
                if student_grade < 9 or student_grade > 12:
                    return redirect_with_message("/edit", "Student grade must be between 9 and 12", page_param)
                if good_class_id == bad_class_id:
                    return redirect_with_message("/edit", "Good and bad class must be different", page_param)

                class_ids = db.execute(
                    select(Class.id)
                    .where(Class.id.in_([good_class_id, bad_class_id]))
                    .with_for_update(read=True)
                ).scalars().all()
                if len(set(class_ids)) != 2:
                    return redirect_with_message("/edit", "Selected classes do not exist", page_param)

                duplicate_preference = db.execute(
                    select(Preference.id)
                    .where(func.lower(Preference.student_name) == normalized_student_name.lower())
                    .where(Preference.good_class_id == good_class_id)
                    .where(Preference.bad_class_id == bad_class_id)
                    .where(Preference.id != preference_id)
                    .with_for_update()
                    .limit(1)
                ).scalar_one_or_none()
                if duplicate_preference is not None:
                    return redirect_with_message("/edit", "Duplicate preference already exists for this student", page_param)

                upsert_student_name(db, normalized_student_name)
                preference_row.recorded_by = normalized_recorded_by
                preference_row.student_name = normalized_student_name
                preference_row.student_grade = student_grade
                preference_row.good_class_id = good_class_id
                preference_row.bad_class_id = bad_class_id
            else:
                return redirect_with_message("/edit", "Invalid preference action", page_param)
    except HTTPException:
        db.rollback()
        return redirect_with_message("/edit", "Preference not found", page_param)
    except IntegrityError:
        db.rollback()
        return redirect_with_message("/edit", "Could not update preference", page_param)

    if action == "delete":
        return redirect_with_message("/edit", "Preference deleted", page_param)
    return redirect_with_message("/edit", "Preference updated", page_param)


@app.get("/recorders")
def recorder_rankings_page(
    request: Request,
    sort: str = "total_preferences",
    dir: str = "desc",
    db: Session = Depends(get_db),
) -> Any:
    sort_dir = "asc" if dir == "asc" else "desc"
    total_preferences_expr = func.count(Preference.id)
    unique_students_expr = func.count(func.distinct(Preference.student_name))
    last_recorded_expr = func.max(Preference.created_at)

    recorder_sort_columns = {
        "recorded_by": Preference.recorded_by,
        "total_preferences": total_preferences_expr,
        "unique_students": unique_students_expr,
        "last_recorded_at": last_recorded_expr,
    }
    sort_key = sort if sort in recorder_sort_columns else "total_preferences"
    sort_expr = recorder_sort_columns[sort_key]
    primary_order = sort_expr.asc() if sort_dir == "asc" else sort_expr.desc()

    recorder_rankings = db.execute(
        select(
            Preference.recorded_by.label("recorded_by"),
            total_preferences_expr.label("total_preferences"),
            unique_students_expr.label("unique_students"),
            last_recorded_expr.label("last_recorded_at"),
        )
        .group_by(Preference.recorded_by)
        .order_by(primary_order, Preference.recorded_by.asc())
    ).all()

    return templates.TemplateResponse(
        request=request,
        name="recorders.html",
        context={
            "recorder_rankings": recorder_rankings,
            "sort_key": sort_key,
            "sort_dir": sort_dir,
        },
    )


@app.get("/rankings")
def rankings_page(
    request: Request,
    sort: str = "net_score",
    dir: str = "desc",
    db: Session = Depends(get_db),
) -> Any:
    sort_dir = "asc" if dir == "asc" else "desc"
    wins_subq = (
        select(Preference.good_class_id.label("class_id"), func.count(Preference.id).label("wins"))
        .group_by(Preference.good_class_id)
        .subquery()
    )
    losses_subq = (
        select(Preference.bad_class_id.label("class_id"), func.count(Preference.id).label("losses"))
        .group_by(Preference.bad_class_id)
        .subquery()
    )
    wins_expr = func.coalesce(wins_subq.c.wins, 0)
    losses_expr = func.coalesce(losses_subq.c.losses, 0)
    net_score_expr = wins_expr - losses_expr
    win_rate_expr = case(
        (
            (wins_expr + losses_expr) == 0,
            0.0,
        ),
        else_=(wins_expr / (wins_expr + losses_expr)),
    )

    ranking_sort_columns = {
        "class": Class.class_name,
        "id": Class.class_code,
        "teacher": Class.teacher_name,
        "wins": wins_expr,
        "losses": losses_expr,
        "net_score": net_score_expr,
        "win_rate": win_rate_expr,
    }
    sort_key = sort if sort in ranking_sort_columns else "net_score"
    sort_expr = ranking_sort_columns[sort_key]
    primary_order = sort_expr.asc() if sort_dir == "asc" else sort_expr.desc()

    rankings = db.execute(
        select(
            Class,
            wins_expr.label("wins"),
            losses_expr.label("losses"),
            net_score_expr.label("net_score"),
            win_rate_expr.label("win_rate"),
        )
        .outerjoin(wins_subq, wins_subq.c.class_id == Class.id)
        .outerjoin(losses_subq, losses_subq.c.class_id == Class.id)
        .order_by(primary_order, Class.class_name.asc(), Class.class_code.asc())
    ).all()

    return templates.TemplateResponse(
        request=request,
        name="rankings.html",
        context={
            "rankings": rankings,
            "sort_key": sort_key,
            "sort_dir": sort_dir,
        },
    )
