from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import Base, engine
from routers import (
    auth_router, agents_router, contacts_router,
    campaigns_router, calls_router, blast_router,
    compliance_router, webhooks_router, leads_router,
)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Revive Marketing Platform API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to the deployed frontend domain before go-live
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


@app.get("/health")
def health():
    return {"status": "ok"}
