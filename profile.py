"""Profile and goals. Stored locally in SQLite; only fields the user chooses to provide."""
import json
from .db import iso, utcnow, jdump

SITUATIONS = {
    "student": "Student", "prospective_student": "Prospective student", "job_seeker": "Job seeker",
    "employed": "Employed professional", "business_owner": "Freelancer or business owner",
    "borrower": "Borrower", "saver_investor": "Saver or investor", "renter": "Renter",
    "property_owner": "Property owner", "commuter": "Commuter", "migrant": "International student or migrant",
    "essential_services": "Consumer of essential services", "technical_professional": "Engineer, researcher or developer",
}
ACTIVITIES = {
    "studying": "Studying, applying to universities, completing qualifications",
    "employment": "Searching for employment and earning income",
    "expenses": "Paying tuition, accommodation, taxes and other expenses",
    "borrowing_saving": "Borrowing, saving, investing or purchasing goods",
    "travel_commute": "Travelling, commuting, using transport",
    "healthcare": "Accessing healthcare",
    "housing": "Renting, moving or purchasing property",
    "tech_use": "Using AI tools, software, computers, internet services",
    "building_research": "Building technical projects, research, acquiring skills",
    "applications": "Applying for scholarships, grants, permits, licences, credentials",
    "business": "Running a business or buying materials and equipment",
}
HORIZONS = {"immediate": "Immediate", "weeks": "Coming weeks", "months": "Coming months", "long": "Long-term"}
STYLES = {"direct": "Direct", "technical": "Technical", "plain": "Plain-language", "analytical": "Analytical"}
DETAIL = {"brief": "Brief", "standard": "Standard", "full": "Full"}
EMPHASIS = {"both": "Risks and opportunities", "risks": "Risks only", "opportunities": "Opportunities only"}
PRIORITIES = {"low": 0.7, "medium": 0.85, "high": 1.0}

DEFAULT_PROFILE = {
    "situations": [], "activities": [], "country": "", "region": "", "city": "",
    "detail": "standard", "style": "plain", "interests": [], "excluded_categories": [],
    "horizon": "weeks", "emphasis": "both", "include_awareness": True, "materiality": None,
}

def _pick(values, allowed):
    return [v for v in values if v in allowed]

def clean_profile(raw, category_ids, default_materiality):
    p = dict(DEFAULT_PROFILE)
    p["situations"] = _pick(raw.get("situations", []), SITUATIONS)
    p["activities"] = _pick(raw.get("activities", []), ACTIVITIES)
    p["interests"] = _pick(raw.get("interests", []), category_ids)
    p["excluded_categories"] = _pick(raw.get("excluded_categories", []), category_ids)
    for k in ("country", "region", "city"):
        p[k] = str(raw.get(k, "")).strip()[:80]
    for k, allowed in (("detail", DETAIL), ("style", STYLES), ("horizon", HORIZONS), ("emphasis", EMPHASIS)):
        v = raw.get(k, DEFAULT_PROFILE[k])
        p[k] = v if v in allowed else DEFAULT_PROFILE[k]
    p["include_awareness"] = bool(raw.get("include_awareness", True))
    try:
        m = int(raw.get("materiality"))
        p["materiality"] = max(0, min(100, m))
    except (TypeError, ValueError):
        p["materiality"] = default_materiality
    return p

def load_profile(conn, default_materiality=40):
    r = conn.execute("SELECT data FROM profile WHERE id=1").fetchone()
    p = dict(DEFAULT_PROFILE)
    if r:
        p.update(json.loads(r["data"]))
    if p.get("materiality") is None:
        p["materiality"] = default_materiality
    return p

def save_profile(conn, p):
    conn.execute("INSERT INTO profile(id,data,updated_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE "
                 "SET data=excluded.data, updated_at=excluded.updated_at", (jdump(p), iso(utcnow())))

def clean_goal(raw):
    title = str(raw.get("title", "")).strip()[:120]
    if not title:
        return None
    pr = raw.get("priority", "medium")
    return {
        "title": title, "outcome": str(raw.get("outcome", "")).strip()[:300],
        "priority": pr if pr in PRIORITIES else "medium",
        "target_date": str(raw.get("target_date", "")).strip()[:10],
        "activities": _pick(raw.get("activities", []), ACTIVITIES),
        "dependencies": str(raw.get("dependencies", "")).strip()[:300],
        "constraints": str(raw.get("constraints", "")).strip()[:300],
    }

def list_goals(conn):
    return [{**json.loads(r["data"]), "id": r["id"]} for r in conn.execute("SELECT * FROM goals ORDER BY id")]

def add_goal(conn, g):
    conn.execute("INSERT INTO goals(data) VALUES(?)", (jdump(g),))

def delete_goal(conn, gid):
    conn.execute("DELETE FROM goals WHERE id=?", (gid,))

def export_all(conn):
    return {
        "exported_at": iso(utcnow()),
        "profile": load_profile(conn), "goals": list_goals(conn),
        "editions": [{"id": r["id"], "created_at": r["created_at"], "retrieved_at": r["retrieved_at"],
                      "payload": json.loads(r["payload"]), "profile_snapshot": json.loads(r["profile_snapshot"])}
                     for r in conn.execute("SELECT * FROM editions ORDER BY id")],
        "checks": [dict(r) for r in conn.execute("SELECT * FROM checks ORDER BY id")],
    }

def delete_data(conn, scope):
    conn.execute("BEGIN IMMEDIATE")
    if scope in ("profile", "everything"):
        conn.execute("DELETE FROM profile"); conn.execute("DELETE FROM goals")
    if scope in ("editions", "everything"):
        conn.execute("DELETE FROM editions"); conn.execute("DELETE FROM checks")
        conn.execute("DELETE FROM state WHERE key IN ('current_edition_id','last_refresh_result','last_check_at','last_check_outcome')")
    if scope == "everything":
        conn.execute("DELETE FROM articles")
    conn.execute("COMMIT")
