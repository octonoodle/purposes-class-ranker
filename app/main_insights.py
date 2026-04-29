from collections import defaultdict
from typing import Any

from fastapi import Depends, FastAPI, Form, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session

from .database import Base, engine, get_db, wait_for_db_ready
from .models import Class, Preference

app = FastAPI(title="Class Ranker Insights")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

TWELFTH_GRADE_ONLY_COOKIE = "show_twelfth_grade_only"
EXCLUDED_CLASSES_COOKIE = "excluded_class_ids"


def is_twelfth_grade_only_enabled(request: Request) -> bool:
    return request.cookies.get(TWELFTH_GRADE_ONLY_COOKIE, "0") == "1"


def get_excluded_class_ids(request: Request) -> set[int]:
    raw_cookie_value = (request.cookies.get(EXCLUDED_CLASSES_COOKIE) or "").strip()
    if not raw_cookie_value:
        return set()

    parsed_ids: set[int] = set()
    for chunk in raw_cookie_value.split(","):
        normalized = chunk.strip()
        if not normalized:
            continue
        try:
            parsed_value = int(normalized)
        except ValueError:
            continue
        if parsed_value > 0:
            parsed_ids.add(parsed_value)
    return parsed_ids


@app.on_event("startup")
def startup() -> None:
    wait_for_db_ready()
    Base.metadata.create_all(bind=engine)


@app.get("/")
def home() -> RedirectResponse:
    return RedirectResponse(url="/visualization", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/settings/grade-filter")
def set_global_grade_filter(
    twelfth_only: str | None = Form(None),
    return_to: str = Form("/visualization"),
) -> RedirectResponse:
    target = return_to if return_to.startswith("/") else "/visualization"
    response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=TWELFTH_GRADE_ONLY_COOKIE,
        value="1" if twelfth_only == "1" else "0",
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
    )
    return response


@app.get("/required-vs-optional")
def required_vs_optional_page(
    request: Request,
    db: Session = Depends(get_db),
) -> Any:
    twelfth_only = is_twelfth_grade_only_enabled(request)
    excluded_class_ids = get_excluded_class_ids(request)
    wins_stmt = select(Preference.good_class_id.label("class_id"), func.count(Preference.id).label("wins"))
    losses_stmt = select(Preference.bad_class_id.label("class_id"), func.count(Preference.id).label("losses"))
    if excluded_class_ids:
        wins_stmt = (
            wins_stmt
            .where(~Preference.good_class_id.in_(excluded_class_ids))
            .where(~Preference.bad_class_id.in_(excluded_class_ids))
        )
        losses_stmt = (
            losses_stmt
            .where(~Preference.good_class_id.in_(excluded_class_ids))
            .where(~Preference.bad_class_id.in_(excluded_class_ids))
        )
    if twelfth_only:
        wins_stmt = wins_stmt.where(Preference.student_grade == 12)
        losses_stmt = losses_stmt.where(Preference.student_grade == 12)
    wins_subq = wins_stmt.group_by(Preference.good_class_id).subquery()
    losses_subq = losses_stmt.group_by(Preference.bad_class_id).subquery()
    wins_expr = func.coalesce(wins_subq.c.wins, 0)
    losses_expr = func.coalesce(losses_subq.c.losses, 0)
    total_ratings_expr = wins_expr + losses_expr
    net_score_expr = wins_expr - losses_expr
    win_rate_expr = case(
        (
            total_ratings_expr == 0,
            0.0,
        ),
        else_=(wins_expr / total_ratings_expr),
    )

    class_scores_stmt = (
        select(
            Class.id,
            Class.required_grade,
            wins_expr.label("wins"),
            losses_expr.label("losses"),
            total_ratings_expr.label("total_ratings"),
            net_score_expr.label("net_score"),
            win_rate_expr.label("win_rate"),
        )
        .outerjoin(wins_subq, wins_subq.c.class_id == Class.id)
        .outerjoin(losses_subq, losses_subq.c.class_id == Class.id)
    )
    if excluded_class_ids:
        class_scores_stmt = class_scores_stmt.where(~Class.id.in_(excluded_class_ids))
    class_scores = db.execute(class_scores_stmt).all()

    required_rows = [row for row in class_scores if row.required_grade > 0]
    optional_rows = [row for row in class_scores if row.required_grade == 0]

    def summarize(rows: list[Any]) -> dict[str, float | int]:
        class_count = len(rows)
        if class_count == 0:
            return {
                "class_count": 0,
                "avg_net_score": 0.0,
                "avg_win_rate": 0.0,
                "avg_total_ratings": 0.0,
            }

        total_net_score = sum(float(row.net_score or 0) for row in rows)
        total_win_rate = sum(float(row.win_rate or 0) for row in rows)
        total_ratings = sum(float(row.total_ratings or 0) for row in rows)
        return {
            "class_count": class_count,
            "avg_net_score": total_net_score / class_count,
            "avg_win_rate": total_win_rate / class_count,
            "avg_total_ratings": total_ratings / class_count,
        }

    required_summary = summarize(required_rows)
    optional_summary = summarize(optional_rows)
    comparison = {
        "net_score_diff": float(required_summary["avg_net_score"]) - float(optional_summary["avg_net_score"]),
        "win_rate_diff": float(required_summary["avg_win_rate"]) - float(optional_summary["avg_win_rate"]),
        "ratings_diff": float(required_summary["avg_total_ratings"]) - float(optional_summary["avg_total_ratings"]),
    }

    return templates.TemplateResponse(
        request=request,
        name="required_vs_optional.html",
        context={
            "required_summary": required_summary,
            "optional_summary": optional_summary,
            "comparison": comparison,
        },
    )


@app.get("/visualization")
def comparisons_visualization_page(
    request: Request,
    db: Session = Depends(get_db),
) -> Any:
    twelfth_only = is_twelfth_grade_only_enabled(request)
    excluded_class_ids = get_excluded_class_ids(request)
    preferences_stmt = select(Preference.good_class_id, Preference.bad_class_id)
    if excluded_class_ids:
        preferences_stmt = (
            preferences_stmt
            .where(~Preference.good_class_id.in_(excluded_class_ids))
            .where(~Preference.bad_class_id.in_(excluded_class_ids))
        )
    if twelfth_only:
        preferences_stmt = preferences_stmt.where(Preference.student_grade == 12)
    preferences = db.execute(preferences_stmt).all()

    classes_stmt = select(Class).order_by(Class.class_name.asc(), Class.class_code.asc())
    if twelfth_only:
        active_class_ids: set[int] = set()
        for good_class_id, bad_class_id in preferences:
            active_class_ids.add(good_class_id)
            active_class_ids.add(bad_class_id)
        if active_class_ids:
            classes_stmt = classes_stmt.where(Class.id.in_(active_class_ids))
        else:
            classes_stmt = classes_stmt.where(text("1 = 0"))
    if excluded_class_ids:
        classes_stmt = classes_stmt.where(~Class.id.in_(excluded_class_ids))
    classes = db.execute(classes_stmt).scalars().all()

    class_by_id = {class_row.id: class_row for class_row in classes}
    wins_by_class: dict[int, int] = {class_row.id: 0 for class_row in classes}
    losses_by_class: dict[int, int] = {class_row.id: 0 for class_row in classes}
    pair_counts: dict[tuple[int, int], dict[str, int]] = {}

    total_preferences = 0
    for good_class_id, bad_class_id in preferences:
        if good_class_id not in class_by_id or bad_class_id not in class_by_id:
            continue

        total_preferences += 1
        wins_by_class[good_class_id] += 1
        losses_by_class[bad_class_id] += 1

        pair_key = tuple(sorted((good_class_id, bad_class_id)))
        bucket = pair_counts.setdefault(
            pair_key,
            {"a_id": pair_key[0], "b_id": pair_key[1], "a_wins": 0, "b_wins": 0, "total": 0},
        )
        if good_class_id == pair_key[0]:
            bucket["a_wins"] += 1
        else:
            bucket["b_wins"] += 1
        bucket["total"] += 1

    nodes: list[dict[str, Any]] = []
    for class_row in classes:
        wins = wins_by_class[class_row.id]
        losses = losses_by_class[class_row.id]
        nodes.append(
            {
                "id": class_row.id,
                "name": class_row.class_name,
                "teacher": class_row.teacher_name,
                "wins": wins,
                "losses": losses,
                "total": wins + losses,
                "net": wins - losses,
            }
        )

    edges: list[dict[str, Any]] = []
    for bucket in pair_counts.values():
        a_wins = bucket["a_wins"]
        b_wins = bucket["b_wins"]
        winner_id: int | None = None
        loser_id: int | None = None
        if a_wins > b_wins:
            winner_id = bucket["a_id"]
            loser_id = bucket["b_id"]
        elif b_wins > a_wins:
            winner_id = bucket["b_id"]
            loser_id = bucket["a_id"]

        total = bucket["total"]
        imbalance = abs(a_wins - b_wins) / total if total else 0.0
        edges.append(
            {
                "a_id": bucket["a_id"],
                "b_id": bucket["b_id"],
                "a_wins": a_wins,
                "b_wins": b_wins,
                "total": total,
                "winner_id": winner_id,
                "loser_id": loser_id,
                "imbalance": imbalance,
            }
        )

    total_classes = len(classes)
    possible_pairs = (total_classes * (total_classes - 1)) // 2
    observed_pairs = len(pair_counts)
    pair_coverage = (observed_pairs / possible_pairs) if possible_pairs else 0.0

    viz_payload = {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "total_classes": total_classes,
            "total_preferences": total_preferences,
            "possible_pairs": possible_pairs,
            "observed_pairs": observed_pairs,
            "pair_coverage": pair_coverage,
        },
    }

    return templates.TemplateResponse(
        request=request,
        name="visualization.html",
        context={"viz_payload": viz_payload},
    )


@app.get("/affinities")
def class_affinities_page(
    request: Request,
    selected_class_id: int | None = None,
    db: Session = Depends(get_db),
) -> Any:
    twelfth_only = is_twelfth_grade_only_enabled(request)
    excluded_class_ids = get_excluded_class_ids(request)
    preference_stmt = select(
        Preference.student_name,
        Preference.good_class_id,
        Preference.bad_class_id,
    )
    if excluded_class_ids:
        preference_stmt = (
            preference_stmt
            .where(~Preference.good_class_id.in_(excluded_class_ids))
            .where(~Preference.bad_class_id.in_(excluded_class_ids))
        )
    if twelfth_only:
        preference_stmt = preference_stmt.where(Preference.student_grade == 12)
    preferences = db.execute(preference_stmt).all()

    classes_stmt = select(Class).order_by(Class.class_name.asc(), Class.class_code.asc())
    if twelfth_only:
        active_class_ids: set[int] = set()
        for _, good_class_id, bad_class_id in preferences:
            active_class_ids.add(good_class_id)
            active_class_ids.add(bad_class_id)
        if active_class_ids:
            classes_stmt = classes_stmt.where(Class.id.in_(active_class_ids))
        else:
            classes_stmt = classes_stmt.where(text("1 = 0"))
    if excluded_class_ids:
        classes_stmt = classes_stmt.where(~Class.id.in_(excluded_class_ids))
    classes = db.execute(classes_stmt).scalars().all()
    class_by_id = {class_row.id: class_row for class_row in classes}

    if not classes:
        return templates.TemplateResponse(
            request=request,
            name="affinities.html",
            context={
                "classes": [],
                "selected_class_id": None,
                "selected_class": None,
                "insight_data": None,
                "message": "Add classes and preferences to generate affinity insights.",
            },
        )

    if selected_class_id is None:
        selected_class_id = classes[0].id
    if selected_class_id not in class_by_id:
        selected_class_id = classes[0].id
    selected_class = class_by_id[selected_class_id]

    liked_selected_by_students: set[str] = set()
    disliked_selected_by_students: set[str] = set()
    for student_name, good_class_id, bad_class_id in preferences:
        normalized_student = student_name.strip().lower()
        if not normalized_student:
            continue
        if good_class_id == selected_class_id:
            liked_selected_by_students.add(normalized_student)
        if bad_class_id == selected_class_id:
            disliked_selected_by_students.add(normalized_student)

    liked_cohort_like_classes: dict[int, set[str]] = defaultdict(set)
    liked_cohort_dislike_classes: dict[int, set[str]] = defaultdict(set)
    disliked_cohort_dislike_classes: dict[int, set[str]] = defaultdict(set)
    disliked_cohort_like_classes: dict[int, set[str]] = defaultdict(set)

    for student_name, good_class_id, bad_class_id in preferences:
        normalized_student = student_name.strip().lower()
        if not normalized_student:
            continue

        if normalized_student in liked_selected_by_students:
            if good_class_id != selected_class_id and good_class_id in class_by_id:
                liked_cohort_like_classes[good_class_id].add(normalized_student)
            if bad_class_id != selected_class_id and bad_class_id in class_by_id:
                liked_cohort_dislike_classes[bad_class_id].add(normalized_student)

        if normalized_student in disliked_selected_by_students:
            if bad_class_id != selected_class_id and bad_class_id in class_by_id:
                disliked_cohort_dislike_classes[bad_class_id].add(normalized_student)
            if good_class_id != selected_class_id and good_class_id in class_by_id:
                disliked_cohort_like_classes[good_class_id].add(normalized_student)

    def summarize_class_set_counts(
        class_to_students: dict[int, set[str]],
        cohort_size: int,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for class_id, student_set in class_to_students.items():
            class_row = class_by_id.get(class_id)
            if class_row is None:
                continue
            support = len(student_set)
            confidence = (support / cohort_size) if cohort_size else 0.0
            rows.append(
                {
                    "class_id": class_id,
                    "class_name": class_row.class_name,
                    "teacher_name": class_row.teacher_name,
                    "support": support,
                    "confidence": confidence,
                }
            )
        rows.sort(key=lambda row: (-row["support"], row["class_name"].lower(), row["teacher_name"].lower()))
        return rows[:limit]

    liked_cohort_size = len(liked_selected_by_students)
    disliked_cohort_size = len(disliked_selected_by_students)
    insight_data = {
        "liked_cohort_size": liked_cohort_size,
        "disliked_cohort_size": disliked_cohort_size,
        "liked_also_enjoy": summarize_class_set_counts(liked_cohort_like_classes, liked_cohort_size),
        "liked_but_not_enjoy": summarize_class_set_counts(liked_cohort_dislike_classes, liked_cohort_size),
        "disliked_also_dislike": summarize_class_set_counts(disliked_cohort_dislike_classes, disliked_cohort_size),
        "disliked_but_enjoy": summarize_class_set_counts(disliked_cohort_like_classes, disliked_cohort_size),
    }

    return templates.TemplateResponse(
        request=request,
        name="affinities.html",
        context={
            "classes": classes,
            "selected_class_id": selected_class_id,
            "selected_class": selected_class,
            "insight_data": insight_data,
        },
    )
