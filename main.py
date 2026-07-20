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
    # Restricted to the known first-party frontends. The public website posts
    # leads server-side via a Netlify function (not a browser origin), so it does
    # not need to be listed here.
    allow_origins=[
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


@app.get("/health")
def health():
    return {"status": "ok"}
