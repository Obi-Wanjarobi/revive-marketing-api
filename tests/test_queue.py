"""Lead flow / floor-manager tests (sqlite). Run: DATABASE_URL=sqlite:///./test.db pytest -q tests"""
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_queue.db")
os.environ.setdefault("LEADS_INGEST_KEY", "test-ingest-key-1234567890")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from auth import create_access_token, hash_password  # noqa: E402
from core import lead_flow as lf  # noqa: E402
from database import Base, SessionLocal, engine  # noqa: E402
from models import Agent, AgentRole, AppSetting, Contact, SuppressedNumber  # noqa: E402

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def mk_agent(db, name, role=AgentRole.agent, created=None):
    a = Agent(name=name, username=name.lower(), password_hash=hash_password("x" * 8), role=role,
              created_at=created or datetime.utcnow())
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def hdr(a):
    return {"Authorization": f"Bearer {create_access_token(a.id)}"}


def mk_lead(db, owner=None, consent=True, tz="America/Los_Angeles", assigned_at=None, **kw):
    c = Contact(first_name=kw.pop("first", "Pat"), last_name="Lee", phone=kw.pop("phone", "+19495550100"),
                timezone=tz, consent_call=consent, **kw)
    db.add(c)
    if owner:
        lf.set_owner(c, owner.id, "test", "intake_test", assigned_at or datetime.utcnow())
    db.commit()
    db.refresh(c)
    return c


# A Wednesday 10:00 Pacific = 18:00 UTC (PST).
WED_10AM_PT = datetime(2026, 12, 2, 18, 0)


def test_callable_hours_skip_nights():
    c = Contact(timezone="America/Los_Angeles")
    # 8:00pm PT -> next day 10:00am PT : callable = 1h (8-9pm) + 2h (8-10am) = 3h
    start = datetime(2026, 12, 3, 4, 0)   # 20:00 PT Dec 2
    end = datetime(2026, 12, 3, 18, 0)    # 10:00 PT Dec 3
    assert abs(lf.callable_hours_between(c, start, end) - 3.0) < 1e-6


def test_web_lead_gets_one_owner_and_clock():
    db = SessionLocal()
    a = mk_agent(db, "Ann", created=datetime(2026, 1, 1))
    b = mk_agent(db, "Bob", created=datetime(2026, 1, 2))
    ids = []
    for i in range(4):
        r = client.post("/leads", json={"first_name": f"L{i}", "phone": f"+1949555010{i}"},
                        headers={"X-Ingest-Key": "test-ingest-key-1234567890"})
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    db.expire_all()
    owners = [db.get(Contact, i).assigned_agent_id for i in ids]
    assert owners.count(a.id) == 2 and owners.count(b.id) == 2
    c = db.get(Contact, ids[0])
    assert c.assigned_at is not None and '"intake_web"' in c.assignment_log
    assert c.consent_call is False  # web claim never becomes consent
    db.close()


def test_stale_lead_moves_to_someone_else_and_is_logged():
    db = SessionLocal()
    a = mk_agent(db, "Ann", created=datetime(2026, 1, 1))
    b = mk_agent(db, "Bob", created=datetime(2026, 1, 2))
    c = mk_lead(db, owner=a, assigned_at=WED_10AM_PT)
    # 3h later: still with Ann
    r = lf.run_sweep(db, now=WED_10AM_PT + timedelta(hours=3))
    assert r["moved_stale"] == 0
    # 5h later (all within 8am-9pm PT): moves to Bob
    r = lf.run_sweep(db, now=WED_10AM_PT + timedelta(hours=5))
    assert r["moved_stale"] == 1
    db.expire_all()
    c = db.get(Contact, c.id)
    assert c.assigned_agent_id == b.id
    assert lf._log(c)[-1]["reason"] == "stale" and lf._log(c)[-1]["from"] == a.id
    db.close()


def test_called_lead_and_scheduled_followup_stay_put():
    db = SessionLocal()
    a = mk_agent(db, "Ann", created=datetime(2026, 1, 1))
    mk_agent(db, "Bob", created=datetime(2026, 1, 2))
    called = mk_lead(db, owner=a, assigned_at=WED_10AM_PT, phone="+19495550111")
    called.last_called_at = WED_10AM_PT + timedelta(minutes=5)
    sched = mk_lead(db, owner=a, assigned_at=WED_10AM_PT, phone="+19495550112")
    sched.next_follow_up = WED_10AM_PT + timedelta(days=2)
    db.commit()
    r = lf.run_sweep(db, now=WED_10AM_PT + timedelta(hours=8))
    assert r["moved_stale"] == 0
    db.close()


def test_no_consent_dnc_suppressed_never_rotate_or_queue():
    db = SessionLocal()
    a = mk_agent(db, "Ann", created=datetime(2026, 1, 1))
    mk_agent(db, "Bob", created=datetime(2026, 1, 2))
    mk_lead(db, owner=a, consent=False, assigned_at=WED_10AM_PT, phone="+19495550121")
    mk_lead(db, owner=a, assigned_at=WED_10AM_PT, phone="+19495550122", dnc_flag=True)
    s = mk_lead(db, owner=a, assigned_at=WED_10AM_PT, phone="+19495550123")
    db.add(SuppressedNumber(phone=s.phone, reason="stop_keyword"))
    db.commit()
    r = lf.run_sweep(db, now=WED_10AM_PT + timedelta(hours=10))
    assert r["moved_stale"] == 0 and r["assigned_unowned"] == 0
    q = client.get("/queue/me", headers=hdr(a)).json()
    assert q["uncalled"] == [] and q["follow_ups_due"] == []
    assert q["blocked"] == {"no_consent": 1, "dnc_or_suppressed": 2, "no_phone": 0}
    db.close()


def test_overnight_does_not_count():
    db = SessionLocal()
    a = mk_agent(db, "Ann", created=datetime(2026, 1, 1))
    mk_agent(db, "Bob", created=datetime(2026, 1, 2))
    # assigned 7pm PT, checked 9am PT next day -> 2h + 1h = 3h callable < 4
    mk_lead(db, owner=a, assigned_at=datetime(2026, 12, 3, 3, 0))
    r = lf.run_sweep(db, now=datetime(2026, 12, 3, 17, 0))
    assert r["moved_stale"] == 0
    db.close()


def test_inactive_owner_and_unowned_get_rehomed():
    db = SessionLocal()
    a = mk_agent(db, "Ann", created=datetime(2026, 1, 1))
    b = mk_agent(db, "Bob", created=datetime(2026, 1, 2))
    c1 = mk_lead(db, owner=a, phone="+19495550131")
    c2 = mk_lead(db, phone="+19495550132")
    a.active = False
    db.commit()
    r = lf.run_sweep(db)
    assert r["moved_from_inactive"] == 1 and r["assigned_unowned"] == 1
    db.expire_all()
    assert db.get(Contact, c1.id).assigned_agent_id == b.id
    assert db.get(Contact, c2.id).assigned_agent_id == b.id
    db.close()


def test_max_rotations_flags_for_manager():
    db = SessionLocal()
    a = mk_agent(db, "Ann", created=datetime(2026, 1, 1))
    mk_agent(db, "Bob", created=datetime(2026, 1, 2))
    adm = mk_agent(db, "Jay", role=AgentRole.admin, created=datetime(2026, 1, 3))
    c = mk_lead(db, owner=a, assigned_at=WED_10AM_PT)
    # Three stale hand-offs already happened
    for _ in range(3):
        lf.set_owner(c, c.assigned_agent_id, "test", "stale", WED_10AM_PT)
    c.assigned_at = WED_10AM_PT
    db.commit()
    r = lf.run_sweep(db, now=WED_10AM_PT + timedelta(hours=6))
    assert r["flagged_for_manager"] == 1 and r["moved_stale"] == 0
    team = client.get("/queue/team", headers=hdr(adm)).json()
    assert [x["contact_id"] for x in team["needs_manager"]] == [c.id]
    db.close()


def test_dial_stops_clock_and_queue_endpoints():
    db = SessionLocal()
    a = mk_agent(db, "Ann", created=datetime(2026, 1, 1))
    b = mk_agent(db, "Bob", created=datetime(2026, 1, 2))
    c = mk_lead(db, owner=a)
    q = client.get("/queue/me", headers=hdr(a)).json()
    assert [x["contact_id"] for x in q["uncalled"]] == [c.id]
    assert "phone" not in q["uncalled"][0] and "email" not in q["uncalled"][0]
    s = client.get("/queue/summary", headers=hdr(a)).json()
    assert s["uncalled"] == 1 and s["next_up"]["contact_id"] == c.id
    # Bob can't read Ann's numbers; Bob's own queue is empty
    assert client.get(f"/queue/summary?agent_id={a.id}", headers=hdr(b)).status_code == 403
    assert client.get("/queue/team", headers=hdr(b)).status_code == 403
    assert client.get(f"/queue/history/{c.id}", headers=hdr(b)).status_code == 403
    # Real dial path (quiet-hours clock pinned open for the test)
    import core.compliance as comp
    orig = comp.is_within_quiet_hours
    comp.is_within_quiet_hours = lambda contact: True
    try:
        r = client.post("/calls/dial", json={"contact_id": c.id}, headers=hdr(a))
    finally:
        comp.is_within_quiet_hours = orig
    assert r.status_code == 200, r.text
    q = client.get("/queue/me", headers=hdr(a)).json()
    assert q["uncalled"] == []
    db.close()


def test_manual_reassign_logged_and_settings_admin_only():
    db = SessionLocal()
    a = mk_agent(db, "Ann", created=datetime(2026, 1, 1))
    b = mk_agent(db, "Bob", created=datetime(2026, 1, 2))
    adm = mk_agent(db, "Jay", role=AgentRole.admin, created=datetime(2026, 1, 3))
    c = mk_lead(db, owner=a)
    r = client.patch(f"/contacts/{c.id}/assign", json={"agent_id": b.id}, headers=hdr(adm))
    assert r.status_code == 200 and r.json()["assigned_agent_id"] == b.id
    h = client.get(f"/queue/history/{c.id}", headers=hdr(b)).json()
    assert h["log"][-1]["reason"] == "manual" and h["log"][-1]["by"] == adm.id
    assert client.put("/queue/settings", json={"stale_hours": 2}, headers=hdr(a)).status_code == 403
    r = client.put("/queue/settings", json={"stale_hours": 2, "stale_enabled": False}, headers=hdr(adm))
    assert r.json() == {"stale_enabled": False, "stale_hours": 2.0, "max_rotations": 3}
    # disabled -> sweep is a no-op, dry run still previews
    db.expire_all()
    c = db.get(Contact, c.id)
    c.assigned_at = WED_10AM_PT
    db.commit()
    assert lf.run_sweep(db, now=WED_10AM_PT + timedelta(hours=5))["moved_stale"] == 0
    assert client.post("/queue/sweep?dry_run=true", headers=hdr(adm)).status_code == 200
    db.close()


def test_q_summary_server_key(monkeypatch):
    from core.config import get_settings
    db = SessionLocal()
    a = mk_agent(db, "Ann", created=datetime(2026, 1, 1))
    a.email = "Ann@Revive.test"
    adm = mk_agent(db, "Jay", role=AgentRole.admin, created=datetime(2026, 1, 3))
    adm.email = "jay@revive.test"
    db.commit()
    mk_lead(db, owner=a)
    # unset key -> closed
    monkeypatch.setattr(get_settings(), "q_floor_key", "")
    assert client.get("/queue/q/summary?email=ann@revive.test").status_code == 503
    monkeypatch.setattr(get_settings(), "q_floor_key", "k" * 32)
    assert client.get("/queue/q/summary?email=ann@revive.test", headers={"X-Q-Key": "wrong"}).status_code == 401
    r = client.get("/queue/q/summary?email=ann@revive.test", headers={"X-Q-Key": "k" * 32})
    assert r.status_code == 200 and r.json()["me"]["uncalled"] == 1 and "team" not in r.json()
    assert "+1949" not in str(r.json())  # no lead phone numbers leave
    r = client.get("/queue/q/summary?email=jay@revive.test", headers={"X-Q-Key": "k" * 32}).json()
    assert r["is_admin"] and {p["name"] for p in r["team"]} == {"Ann", "Jay"}
    assert client.get("/queue/q/summary?email=nobody@x.test", headers={"X-Q-Key": "k" * 32}).status_code == 404
    db.close()
