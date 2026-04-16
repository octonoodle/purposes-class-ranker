# Class Ranker

Web app for collecting binary class preferences and ranking classes.

## Stack
- Backend: FastAPI
- Database: PostgreSQL (transactional, row-level locking, concurrent-safe writes)
- ORM: SQLAlchemy
- UI: Server-rendered Jinja templates

## Data captured
### Preference record
- Recorded by (person entering data)
- Student name
- Student grade
- Good class
- Bad class

### Class record
- Class name
- Class ID (`class_code`) auto-generated as a hash of class name
- Teacher name
- Required grade (`0` means not required)

## Run
1. Start Postgres:
```bash
docker compose up -d db
```

2. Create and activate a virtual env:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Set database URL:
```bash
# Local Homebrew Postgres over unix socket:
export DATABASE_URL=postgresql+psycopg://classranker:classranker@/classranker?host=/tmp

# If using Docker Postgres instead:
# export DATABASE_URL=postgresql+psycopg://classranker:classranker@localhost:5432/classranker
```

5. Start the app:
```bash
uvicorn app.main:app
```

6. Open:
- http://127.0.0.1:8000/preferences
- http://127.0.0.1:8000/classes
- http://127.0.0.1:8000/edit
- http://127.0.0.1:8000/rankings
- http://127.0.0.1:8000/stats

## Notes on concurrency
- Inserts run in database transactions.
- PostgreSQL handles concurrent writers safely.
- Preference writes take row-level read locks on selected classes during validation.

## Built with Codex 5.3
Codex 5.3 played a major implementation role in this project. It handled most of the end-to-end engineering work, including:
- selecting and scaffolding the FastAPI + SQLAlchemy + PostgreSQL stack
- building the core data models, routes, templates, and ranking logic
- implementing concurrency-safe preference writes and validation rules
- iterating on mobile/UI improvements, search/select UX, and edit flows
- applying and verifying changes quickly across backend, frontend, and docs
