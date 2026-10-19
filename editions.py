"""Builds immutable editions from stored articles using the deterministic Analyzer."""
import json
from datetime import timedelta
from .db import iso, jdump, get_state, set_state

def bucket(reason):
    r = reason or ""
    if r.startswith("no rule matched"): return "matched no pathway rule (not an institutional development that reaches people's options)"
    if r.startswith("no documented pathway"): return "pathway exists but nothing in your profile or goals links to it"
    if r.startswith("below materiality"): return "linked to you but scored below your materiality threshold"
    if r.startswith("excluded"): return "in an area you excluded"
    if r.startswith("outside"): return "outside your risks/opportunities emphasis"
    return "awareness-only item hidden by your preference"

_TIER = {"official": 0, "reputable": 1, "other": 2}

def load_stories(conn, now, lookback_days, sources):
    cutoff = iso(now - timedelta(days=lookback_days))
    by = {}
    for r in conn.execute("SELECT * FROM articles WHERE COALESCE(published,first_retrieved)>=? ORDER BY COALESCE(published,first_retrieved)", (cutoff,)):
        by.setdefault(r["story_id"], []).append(dict(r))
    stories = []
    for sid, members in by.items():
        rep = min(members, key=lambda m: (_TIER.get(sources.get(m["source_id"], {}).get("tier", "other"), 2), m["published"] or m["first_retrieved"]))
        stories.append({"story_id": sid, "rep": rep, "members": members})
    return stories

def current_edition(conn):
    cid = get_state(conn, "current_edition_id")
    if not cid:
        return None
    r = conn.execute("SELECT * FROM editions WHERE id=?", (int(cid),)).fetchone()
    return None if r is None else {"id": r["id"], "created_at": r["created_at"], "retrieved_at": r["retrieved_at"],
                                   "payload": json.loads(r["payload"]), "profile_snapshot": json.loads(r["profile_snapshot"])}

def get_edition(conn, eid):
    r = conn.execute("SELECT * FROM editions WHERE id=?", (eid,)).fetchone()
    return None if r is None else {"id": r["id"], "created_at": r["created_at"], "retrieved_at": r["retrieved_at"],
                                   "payload": json.loads(r["payload"]), "profile_snapshot": json.loads(r["profile_snapshot"])}

def build_candidates(conn, analyzer, cfg, profile, goals, now, material_ids, dedup_applied, source_checks):
    """Score everything in the lookback window. Returns (payload, new_or_updated_items). Does not write."""
    stories = load_stories(conn, now, cfg.retrieval["lookback_days"], analyzer.sources)
    ctx = analyzer.prepare(stories, goals)
    items, rejected = [], []
    for s in stories:
        it = analyzer.score(s, profile, goals, ctx, today=now.date())
        if it["reject"]:
            rejected.append({"story_id": it["story_id"], "headline": it["headline"], "url": it["url"],
                             "relevance": it["relevance"], "reason": it["reject"]})
        else:
            items.append(it)
    items.sort(key=lambda i: i["published"] or "", reverse=True)
    items.sort(key=lambda i: -i["relevance"])
    items = items[: cfg.retrieval["max_items"]]
    cur = current_edition(conn)
    cur_ids = {i["story_id"] for i in cur["payload"]["items"]} if cur else set()
    for it in items:
        it["new"] = it["story_id"] not in cur_ids
        it["updated"] = (not it["new"]) and it["story_id"] in material_ids
    rejected.sort(key=lambda r: -r["relevance"])
    summary = {}
    for r in rejected:
        b = bucket(r["reason"]); summary[b] = summary.get(b, 0) + 1
    payload = {"items": items, "rejected": rejected[:150], "rejected_total": len(rejected), "reject_summary": summary,
               "sources": source_checks, "dedup_applied": dedup_applied[:200], "stories_considered": len(stories),
               "params": {"weights": cfg.weights, "materiality": profile["materiality"], "lookback_days": cfg.retrieval["lookback_days"],
                          "near_duplicate_similarity": cfg.retrieval["near_duplicate_similarity"]}}
    return payload, [i for i in items if i["new"] or i["updated"]]

def publish(conn, payload, profile, goals, now, retrieved_at):
    cur = conn.execute("INSERT INTO editions(created_at,retrieved_at,payload,profile_snapshot) VALUES(?,?,?,?)",
                       (iso(now), retrieved_at, jdump(payload), jdump({"profile": profile, "goals": goals})))
    set_state(conn, "current_edition_id", str(cur.lastrowid))
    return cur.lastrowid
