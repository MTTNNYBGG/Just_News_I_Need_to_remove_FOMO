"""FastAPI app. Server-rendered; no external assets, cookies, or third-party requests."""
import json, os
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from .config import load_config
from .db import connect, utcnow, get_state
from .refresh import RefreshService
from .retrieval import make_http_fetcher
from . import editions as ed, profile as pf
from .opportunity import load_timing, evaluate_goal
from .processing import tokens

HERE = Path(__file__).parent

def create_app(cfg=None, db_path=None, fetcher=None, clock=utcnow):
    cfg = cfg or load_config()
    db_path = db_path or os.environ.get("INEWS_DB", "data/inews.sqlite3")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    fetcher = fetcher or make_http_fetcher(cfg.user_agent, cfg.retrieval["http_timeout_seconds"])
    svc = RefreshService(cfg, db_path, fetcher, clock)
    app = FastAPI(title="Institutional Intelligence", docs_url=None, redoc_url=None)
    tpl = Jinja2Templates(directory=str(HERE / "templates"))
    cat_ids = [c["id"] for c in cfg.categories]
    cat_label = {c["id"]: c["label"] for c in cfg.categories}
    tpl.env.globals.update(SITUATIONS=pf.SITUATIONS, ACTIVITIES=pf.ACTIVITIES, HORIZONS=pf.HORIZONS, STYLES=pf.STYLES,
                           DETAIL=pf.DETAIL, EMPHASIS=pf.EMPHASIS, categories=cfg.categories, cat_label=cat_label)

    def page(request, name, **ctx):
        return tpl.TemplateResponse(request, name, ctx)

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        if request.method == "POST":
            o = request.headers.get("origin") or request.headers.get("referer")
            if o and urlsplit(o).netloc != request.headers.get("host"):
                return PlainTextResponse("Cross-origin POST rejected", status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        c = connect(db_path)
        try:
            prof = pf.load_profile(c, cfg.thresholds["materiality_default"])
            return page(request, "index.html", st=svc.status(c), edition=ed.current_edition(c), profile=prof,
                        goals=pf.list_goals(c), nav="current", sources_enabled=len(cfg.enabled_sources),
                        local=json.loads(get_state(c, "last_local_result") or "null"))
        finally:
            c.close()

    @app.post("/refresh")
    def refresh():
        svc.refresh()   # outcome is persisted in state and rendered on the home page
        return RedirectResponse("/", status_code=303)

    @app.post("/reevaluate")
    def reevaluate():
        svc.reevaluate()   # local only: no network access, cooldown untouched
        return RedirectResponse("/", status_code=303)

    @app.get("/editions", response_class=HTMLResponse)
    def archive(request: Request, q: str = ""):
        c = connect(db_path)
        try:
            rows = []
            for r in c.execute("SELECT * FROM editions ORDER BY id DESC"):
                p = json.loads(r["payload"])
                heads = [i["headline"] for i in p["items"]]
                if q and not any(q.lower() in h.lower() for h in heads):
                    continue
                rows.append({"id": r["id"], "created_at": r["created_at"], "n": len(p["items"]), "heads": heads[:3]})
            return page(request, "editions.html", rows=rows, q=q, nav="editions")
        finally:
            c.close()

    @app.get("/editions/{eid}", response_class=HTMLResponse)
    def edition(request: Request, eid: int):
        c = connect(db_path)
        try:
            e = ed.get_edition(c, eid)
            if not e:
                return PlainTextResponse("No such edition", status_code=404)
            return page(request, "edition.html", edition=e, profile=e["profile_snapshot"]["profile"], nav="editions")
        finally:
            c.close()

    @app.get("/editions/{eid}/item/{sid}", response_class=HTMLResponse)
    def item(request: Request, eid: int, sid: int):
        c = connect(db_path)
        try:
            e = ed.get_edition(c, eid)
            it = next((i for i in e["payload"]["items"] if i["story_id"] == sid), None) if e else None
            if not it:
                return PlainTextResponse("No such item", status_code=404)
            # what-if: rescore the stored story under the CURRENT profile (read-only; does not change any edition)
            prof = pf.load_profile(c, cfg.thresholds["materiality_default"]); goals = pf.list_goals(c)
            stories = [s for s in ed.load_stories(c, utcnow(), 3650, svc.analyzer.sources) if s["story_id"] == sid]
            now_item = None
            if stories:
                ctx = svc.analyzer.prepare(stories, goals)
                now_item = svc.analyzer.score(stories[0], prof, goals, ctx, today=date.today())
            return page(request, "item.html", edition=e, it=it, now_item=now_item, weights=cfg.weights, nav="editions")
        finally:
            c.close()

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request):
        c = connect(db_path)
        try:
            return page(request, "profile.html", profile=pf.load_profile(c, cfg.thresholds["materiality_default"]),
                        goals=pf.list_goals(c), nav="profile")
        finally:
            c.close()

    @app.post("/profile")
    async def profile_save(request: Request):
        f = await request.form()
        raw = {k: f.getlist(k) for k in ("situations", "activities", "interests", "excluded_categories")}
        raw.update({k: f.get(k, "") for k in ("country", "region", "city", "detail", "style", "horizon", "emphasis", "materiality")})
        raw["include_awareness"] = f.get("include_awareness") == "on"
        c = connect(db_path)
        try:
            pf.save_profile(c, pf.clean_profile(raw, cat_ids, cfg.thresholds["materiality_default"]))
        finally:
            c.close()
        return RedirectResponse("/profile", status_code=303)

    @app.post("/goals")
    async def goal_add(request: Request):
        f = await request.form()
        g = pf.clean_goal({**{k: f.get(k, "") for k in ("title", "outcome", "priority", "target_date", "dependencies", "constraints")},
                           "activities": f.getlist("activities")})
        if g:
            c = connect(db_path)
            try:
                pf.add_goal(c, g)
            finally:
                c.close()
        return RedirectResponse("/profile", status_code=303)

    @app.post("/goals/{gid}/delete")
    def goal_delete(gid: int):
        c = connect(db_path)
        try:
            pf.delete_goal(c, gid)
        finally:
            c.close()
        return RedirectResponse("/profile", status_code=303)

    @app.get("/map", response_class=HTMLResponse)
    def inst_map(request: Request):
        c = connect(db_path)
        try:
            prof = pf.load_profile(c, cfg.thresholds["materiality_default"])
        finally:
            c.close()
        rules = []
        for r in cfg.rules:
            rules.append({**r, "act": sorted(set(r["activities"]) & set(prof["activities"])),
                          "sit": sorted(set(r["situations"]) & set(prof["situations"]))})
        return page(request, "map.html", institutions=cfg.institutions, rules=rules, nav="map")

    @app.get("/opportunities", response_class=HTMLResponse)
    def opportunities(request: Request):
        c = connect(db_path)
        try:
            goals = pf.list_goals(c)
        finally:
            c.close()
        recs = load_timing(cfg.timing_dir)
        return page(request, "opportunities.html", goals=goals, results={g["id"]: evaluate_goal(g, recs) for g in goals},
                    n_records=len(recs), nav="opps")

    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request):
        c = connect(db_path)
        try:
            checks = [{"at": r["finished_at"], "outcome": r["outcome"], "detail": json.loads(r["detail"])}
                      for r in c.execute("SELECT * FROM checks ORDER BY id DESC LIMIT 15")]
            return page(request, "sources.html", sources=cfg.sources, checks=checks, nav="sources")
        finally:
            c.close()

    @app.get("/data/export")
    def export():
        c = connect(db_path)
        try:
            return JSONResponse(pf.export_all(c), headers={"Content-Disposition": "attachment; filename=inews-export.json"})
        finally:
            c.close()

    @app.post("/data/delete")
    def delete(scope: str = Form(...), confirm: str = Form("")):
        if confirm != "DELETE" or scope not in ("profile", "editions", "everything"):
            return PlainTextResponse("Type DELETE to confirm and choose a valid scope.", status_code=400)
        c = connect(db_path)
        try:
            pf.delete_data(c, scope)
        finally:
            c.close()
        return RedirectResponse("/profile", status_code=303)

    return app
