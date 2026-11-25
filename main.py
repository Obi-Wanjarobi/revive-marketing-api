from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from database import Base, engine, SessionLocal
from core.lead_flow import start_background_sweeper
from routers import (
    auth_router, agents_router, contacts_router,
    campaigns_router, calls_router, blast_router,
    compliance_router, webhooks_router, leads_router,
    pipeline_router, market_router, queue_router,
)

Base.metadata.create_all(bind=engine)

# Lightweight additive migration: create_all() makes missing TABLES but not new
# COLUMNS on existing ones. Add the lead-owner column if it isn't there yet.
if engine.dialect.name == "postgresql":  # sqlite (local tests) gets the columns from create_all
  with engine.begin() as _conn:
    _conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS assigned_agent_id VARCHAR"))
    _conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS notes TEXT"))
    _conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS app_data TEXT"))
    _conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS archived BOOLEAN DEFAULT FALSE"))
    _conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS next_follow_up TIMESTAMP"))
    _conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS follow_up_note VARCHAR"))
    _conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS notes_log TEXT"))
    # 2026-11-25 lead flow (additive)
    _conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMP"))
    _conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_called_at TIMESTAMP"))
    _conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS assignment_log TEXT"))

# Backfill (idempotent): existing owned leads start their stale clock NOW (no mass
# re-assignment on the first sweep), and last_called_at comes from call history.
with engine.begin() as _conn:
    _conn.execute(text("UPDATE contacts SET assigned_at = CURRENT_TIMESTAMP "
                       "WHERE assigned_agent_id IS NOT NULL AND assigned_at IS NULL"))
    _conn.execute(text("UPDATE contacts SET last_called_at = (SELECT MAX(cs.started_at) FROM call_sessions cs "
                       "WHERE cs.contact_id = contacts.id) WHERE last_called_at IS NULL "
                       "AND EXISTS (SELECT 1 FROM call_sessions cs WHERE cs.contact_id = contacts.id)"))

app = FastAPI(title="Revive Marketing Platform API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    # Restricted to the known first-party frontends. The public website posts
    # leads server-side via a Netlify function (not a browser origin), so it does
    # not need to be listed here.
    allow_origins=[
        "https://mypulsemarketing.com",
        "https://www.mypulsemarketing.com",
        "https://marketing.myrevivecapital.com",
        "https://revivemarketing.netlify.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(agents_router.router)
app.include_router(contacts_router.router)
app.include_router(campaigns_router.router)
app.include_router(calls_router.router)
app.include_router(blast_router.router)
app.include_router(compliance_router.router)
app.include_router(webhooks_router.router)
app.include_router(leads_router.router)
app.include_router(pipeline_router.router)
app.include_router(market_router.router)
app.include_router(queue_router.router)


@app.on_event("startup")
def _start_lead_sweeper():
    start_background_sweeper(SessionLocal)


@app.get("/health")
def health():
    return {"status": "ok"}
