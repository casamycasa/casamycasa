"""SpaceBase3000 survey -- form and backend in one service.

Serves the review form at "/" and stores responses per survey (enkät) and
per session (frågetillfälle) in a SQLite file inside the container.

Model
-----
survey     -- a questionnaire, e.g. "presentation-review", with its question schema
session    -- one occasion the survey is run for (title, presenter, date)
response   -- one respondent's answers for one session; re-submitting with the
              same respondent token replaces the earlier answers
"""
from __future__ import annotations

import csv
import io
import json
import os
import secrets
import sqlite3
import statistics
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterator, Literal

from pathlib import Path as FsPath

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------- config

DB_PATH = os.environ.get("DB_PATH", "/data/spacebase.db")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
# Empty by default: the form is served from the same origin, so no CORS is needed.
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
MAX_TEXT = int(os.environ.get("MAX_TEXT_LENGTH", "2000"))
# When true (default), anyone using the form may start a new round and edit its
# title/presenter/date, as in the original form. Set to false to require ADMIN_TOKEN.
OPEN_SESSIONS = os.environ.get("OPEN_SESSIONS", "true").lower() in ("1", "true", "yes")
STATIC_DIR = FsPath(__file__).parent / "static"

SLUG = r"^[a-z0-9][a-z0-9-]{1,62}$"
TOKEN = r"^[A-Za-z0-9_-]{16,128}$"

DEFAULT_SURVEY = {
    "slug": "presentation-review",
    "title": "Staff presentation review",
    "questions": [
        {"id": "overall", "type": "scale", "label": "Overall, how was the presentation?",
         "min": 1, "max": 5, "required": True},
        {"id": "content", "type": "scale", "label": "The content was relevant and useful to my work",
         "min": 1, "max": 5},
        {"id": "delivery", "type": "scale", "label": "The delivery was clear and engaging",
         "min": 1, "max": 5},
        {"id": "length", "type": "choice", "label": "How was the length?",
         "options": ["Too short", "About right", "Too long"]},
        {"id": "more", "type": "choice", "label": "Would you want more sessions like this?",
         "options": ["Yes", "Maybe", "No"]},
        {"id": "good", "type": "text", "label": "What worked well?"},
        {"id": "better", "type": "text", "label": "What could be better next time?"},
        {"id": "name", "type": "text", "label": "Your name", "max_length": 80},
    ],
}

# --------------------------------------------------------------------------- database

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS surveys (
    slug        TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    questions   TEXT NOT NULL,            -- JSON list of question definitions
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    survey_slug TEXT NOT NULL REFERENCES surveys(slug) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    presenter   TEXT NOT NULL DEFAULT '',
    held_on     TEXT,                     -- ISO date
    is_open     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_survey ON sessions(survey_slug, created_at);
CREATE TABLE IF NOT EXISTS responses (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    respondent_token TEXT NOT NULL,       -- random id kept by the browser, never a person
    answers          TEXT NOT NULL,       -- JSON object keyed by question id
    submitted_at     TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (session_id, respondent_token)
);
CREATE INDEX IF NOT EXISTS idx_responses_session ON responses(session_id, updated_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO surveys (slug, title, questions, created_at) VALUES (?,?,?,?)",
            (DEFAULT_SURVEY["slug"], DEFAULT_SURVEY["title"],
             json.dumps(DEFAULT_SURVEY["questions"]), now()),
        )


# --------------------------------------------------------------------------- models

class Question(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    type: Literal["scale", "choice", "text"]
    label: str = Field(min_length=1, max_length=300)
    required: bool = False
    min: int | None = None
    max: int | None = None
    options: list[str] | None = None
    max_length: int | None = Field(default=None, ge=1, le=10000)


class SurveyIn(BaseModel):
    slug: str = Field(pattern=SLUG)
    title: str = Field(min_length=1, max_length=200)
    questions: list[Question] = Field(min_length=1, max_length=50)

    @field_validator("questions")
    @classmethod
    def check_questions(cls, qs: list[Question]) -> list[Question]:
        ids = [q.id for q in qs]
        if len(ids) != len(set(ids)):
            raise ValueError("question ids must be unique")
        for q in qs:
            if q.type == "scale" and (q.min is None or q.max is None or q.min >= q.max):
                raise ValueError(f"scale question '{q.id}' needs min < max")
            if q.type == "choice" and not q.options:
                raise ValueError(f"choice question '{q.id}' needs options")
        return qs


class SessionIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    presenter: str = Field(default="", max_length=120)
    held_on: date | None = None


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    presenter: str | None = Field(default=None, max_length=120)
    held_on: date | None = None
    is_open: bool | None = None


class ResponseIn(BaseModel):
    answers: dict[str, Any]


# --------------------------------------------------------------------------- helpers

def is_admin(authorization: str) -> bool:
    supplied = authorization.removeprefix("Bearer ").strip()
    return bool(ADMIN_TOKEN) and secrets.compare_digest(supplied, ADMIN_TOKEN)


def require_admin(authorization: str = Header(default="")) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(503, "ADMIN_TOKEN is not configured on the server")
    if not is_admin(authorization):
        raise HTTPException(401, "Missing or wrong admin token")


def require_session_editor(authorization: str = Header(default="")) -> bool:
    """Starting/editing a round: open to the form when OPEN_SESSIONS, else admin only."""
    admin = is_admin(authorization)
    if not (OPEN_SESSIONS or admin):
        require_admin(authorization)
    return admin


def get_survey(conn: sqlite3.Connection, slug: str) -> dict:
    row = conn.execute("SELECT * FROM surveys WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise HTTPException(404, f"Survey '{slug}' not found")
    return {"slug": row["slug"], "title": row["title"],
            "questions": json.loads(row["questions"]), "created_at": row["created_at"]}


def session_dict(row: sqlite3.Row, count: int | None = None) -> dict:
    d = {"id": row["id"], "survey": row["survey_slug"], "title": row["title"],
         "presenter": row["presenter"], "held_on": row["held_on"],
         "is_open": bool(row["is_open"]), "created_at": row["created_at"]}
    if count is not None:
        d["responses"] = count
    return d


def get_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Session not found")
    return row


def validate_answers(questions: list[dict], answers: dict[str, Any]) -> dict[str, Any]:
    known = {q["id"]: q for q in questions}
    unknown = set(answers) - set(known)
    if unknown:
        raise HTTPException(422, f"Unknown question ids: {', '.join(sorted(unknown))}")
    clean: dict[str, Any] = {}
    for qid, q in known.items():
        v = answers.get(qid)
        if v is None or (isinstance(v, str) and not v.strip()):
            if q.get("required"):
                raise HTTPException(422, f"Question '{qid}' is required")
            continue
        if q["type"] == "scale":
            if isinstance(v, bool) or not isinstance(v, int) or not q["min"] <= v <= q["max"]:
                raise HTTPException(422, f"'{qid}' must be a whole number {q['min']}-{q['max']}")
        elif q["type"] == "choice":
            if v not in q["options"]:
                raise HTTPException(422, f"'{qid}' must be one of: {', '.join(q['options'])}")
        else:
            if not isinstance(v, str):
                raise HTTPException(422, f"'{qid}' must be text")
            v = v.strip()
            limit = q.get("max_length") or MAX_TEXT
            if len(v) > limit:
                raise HTTPException(422, f"'{qid}' is longer than {limit} characters")
        clean[qid] = v
    return clean


def summarize(questions: list[dict], rows: list[sqlite3.Row]) -> dict:
    answers = [json.loads(r["answers"]) for r in rows]
    out: dict[str, Any] = {}
    for q in questions:
        vals = [a[q["id"]] for a in answers if q["id"] in a]
        if q["type"] == "scale":
            dist = {str(k): 0 for k in range(q["min"], q["max"] + 1)}
            for v in vals:
                dist[str(v)] += 1
            out[q["id"]] = {"type": "scale", "count": len(vals),
                            "average": round(statistics.fmean(vals), 2) if vals else None,
                            "distribution": dist}
        elif q["type"] == "choice":
            out[q["id"]] = {"type": "choice", "count": len(vals),
                            "counts": {o: vals.count(o) for o in q["options"]}}
        else:
            out[q["id"]] = {"type": "text", "count": len(vals)}
    # Free-text comments, newest first. "name" is shown only if the respondent filled it in.
    text_ids = [q["id"] for q in questions if q["type"] == "text" and q["id"] != "name"]
    comments = []
    for r, a in sorted(zip(rows, answers), key=lambda p: p[0]["updated_at"], reverse=True):
        texts = {k: a[k] for k in text_ids if a.get(k)}
        if texts:
            comments.append({"answers": texts, "name": a.get("name") or None,
                             "overall": a.get("overall"), "updated_at": r["updated_at"]})
    return {"responses": len(rows), "questions": out, "comments": comments}


# --------------------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="SpaceBase3000 survey", version="1.1.0", lifespan=lifespan)
if CORS_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["Authorization", "Content-Type"])


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health() -> dict:
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


# ---- surveys (enkäter)

@app.get("/api/surveys")
def list_surveys() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT slug FROM surveys ORDER BY created_at").fetchall()
        return [get_survey(conn, r["slug"]) for r in rows]


@app.get("/api/surveys/{slug}")
def read_survey(slug: str = Path(pattern=SLUG)) -> dict:
    with connect() as conn:
        return get_survey(conn, slug)


@app.put("/api/surveys/{slug}", dependencies=[Depends(require_admin)])
def upsert_survey(body: SurveyIn, slug: str = Path(pattern=SLUG)) -> dict:
    if body.slug != slug:
        raise HTTPException(422, "slug in body must match the URL")
    qs = json.dumps([q.model_dump(exclude_none=True) for q in body.questions])
    with connect() as conn:
        conn.execute(
            "INSERT INTO surveys (slug, title, questions, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(slug) DO UPDATE SET title = excluded.title, questions = excluded.questions",
            (slug, body.title, qs, now()))
        return get_survey(conn, slug)


# ---- sessions (frågetillfällen)

@app.get("/api/surveys/{slug}/sessions")
def list_sessions(slug: str = Path(pattern=SLUG)) -> list[dict]:
    with connect() as conn:
        get_survey(conn, slug)
        rows = conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM responses r WHERE r.session_id = s.id) AS n "
            "FROM sessions s WHERE survey_slug = ? ORDER BY created_at DESC, s.rowid DESC", (slug,)).fetchall()
        return [session_dict(r, r["n"]) for r in rows]


@app.get("/api/surveys/{slug}/sessions/current")
def current_session(slug: str = Path(pattern=SLUG)) -> dict:
    """The newest open session -- what the form should collect answers for."""
    with connect() as conn:
        get_survey(conn, slug)
        row = conn.execute(
            "SELECT * FROM sessions WHERE survey_slug = ? AND is_open = 1 "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "No open session for this survey")
        return session_dict(row)


@app.post("/api/surveys/{slug}/sessions", status_code=201,
          dependencies=[Depends(require_session_editor)])
def create_session(body: SessionIn, slug: str = Path(pattern=SLUG)) -> dict:
    sid = uuid.uuid4().hex
    with connect() as conn:
        get_survey(conn, slug)
        conn.execute(
            "INSERT INTO sessions (id, survey_slug, title, presenter, held_on, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (sid, slug, body.title.strip(), body.presenter.strip(),
             body.held_on.isoformat() if body.held_on else None, now()))
        return session_dict(get_session(conn, sid), 0)


@app.get("/api/sessions/{session_id}")
def read_session(session_id: str) -> dict:
    with connect() as conn:
        return session_dict(get_session(conn, session_id))


@app.patch("/api/sessions/{session_id}")
def patch_session(body: SessionPatch, session_id: str,
                  admin: bool = Depends(require_session_editor)) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if "is_open" in fields and not admin:
        raise HTTPException(403, "Only an admin can open or close a session")
    if "held_on" in fields and fields["held_on"] is not None:
        fields["held_on"] = fields["held_on"].isoformat()
    if "is_open" in fields:
        fields["is_open"] = int(fields["is_open"])
    with connect() as conn:
        get_session(conn, session_id)
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE sessions SET {sets} WHERE id = ?", (*fields.values(), session_id))
        return session_dict(get_session(conn, session_id))


@app.delete("/api/sessions/{session_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_session(session_id: str) -> Response:
    with connect() as conn:
        get_session(conn, session_id)
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return Response(status_code=204)


# ---- responses (svar)

@app.put("/api/sessions/{session_id}/responses/{token}")
def submit_response(body: ResponseIn, session_id: str, token: str = Path(pattern=TOKEN)) -> dict:
    """Create or replace this respondent's answers for the session.

    `token` is a random id the browser generates once and keeps; it lets a person
    change their answers without creating a second response, and is never shown.
    """
    with connect() as conn:
        s = get_session(conn, session_id)
        if not s["is_open"]:
            raise HTTPException(409, "This session is closed for new answers")
        survey = get_survey(conn, s["survey_slug"])
        clean = validate_answers(survey["questions"], body.answers)
        ts = now()
        conn.execute(
            "INSERT INTO responses (id, session_id, respondent_token, answers, submitted_at, updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(session_id, respondent_token) "
            "DO UPDATE SET answers = excluded.answers, updated_at = excluded.updated_at",
            (uuid.uuid4().hex, session_id, token, json.dumps(clean, ensure_ascii=False), ts, ts))
        row = conn.execute(
            "SELECT answers, submitted_at, updated_at FROM responses "
            "WHERE session_id = ? AND respondent_token = ?", (session_id, token)).fetchone()
        return {"answers": json.loads(row["answers"]), "submitted_at": row["submitted_at"],
                "updated_at": row["updated_at"]}


@app.get("/api/sessions/{session_id}/responses/{token}")
def read_own_response(session_id: str, token: str = Path(pattern=TOKEN)) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT answers, submitted_at, updated_at FROM responses "
            "WHERE session_id = ? AND respondent_token = ?", (session_id, token)).fetchone()
        if not row:
            raise HTTPException(404, "No answers saved for this respondent")
        return {"answers": json.loads(row["answers"]), "submitted_at": row["submitted_at"],
                "updated_at": row["updated_at"]}


@app.get("/api/sessions/{session_id}/summary")
def session_summary(session_id: str) -> dict:
    """Aggregated tally for the live panel: averages, distributions, comments."""
    with connect() as conn:
        s = get_session(conn, session_id)
        survey = get_survey(conn, s["survey_slug"])
        rows = conn.execute("SELECT answers, updated_at FROM responses WHERE session_id = ?",
                            (session_id,)).fetchall()
        return {"session": session_dict(s), **summarize(survey["questions"], rows)}


@app.get("/api/sessions/{session_id}/export.csv", dependencies=[Depends(require_admin)])
def export_csv(session_id: str) -> Response:
    with connect() as conn:
        s = get_session(conn, session_id)
        survey = get_survey(conn, s["survey_slug"])
        rows = conn.execute("SELECT answers, submitted_at, updated_at FROM responses "
                            "WHERE session_id = ? ORDER BY submitted_at", (session_id,)).fetchall()
    qids = [q["id"] for q in survey["questions"]]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["submitted_at", "updated_at", *qids])
    for r in rows:
        a = json.loads(r["answers"])
        w.writerow([r["submitted_at"], r["updated_at"], *[a.get(q, "") for q in qids]])
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="session-{session_id}.csv"'})
