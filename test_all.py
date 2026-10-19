"""All feed content below is SYNTHETIC test data. These tests prove the logic under simulated feeds;
they do not prove that any real feed is reachable (see README: verifying live feeds)."""
import json, threading, time
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from app.config import load_config, ConfigError
from app.db import connect, utcnow, iso
from app.refresh import RefreshService
from app.retrieval import FetchResult, parse_feed_bytes
from app.main import create_app
from app import profile as pf, editions as ed
from app.opportunity import evaluate_goal

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
PUB = "2026-10-18T09:00:00Z"
TITLE = "Central bank raises interest rate by 0.5% to 8%"
SUMMARY = "The bank announced the increase, effective 2026-11-01, citing inflation. Borrowers on variable rates will pay more."

class Clock:
    def __init__(self): self.t = T0
    def __call__(self): return self.t
    def advance(self, **kw): self.t += timedelta(**kw)

class Feeds:
    """Fake fetcher: per-source entries or an error; counts calls."""
    def __init__(self): self.data, self.calls, self.delay, self.started = {}, 0, 0, threading.Event()
    def __call__(self, source):
        self.calls += 1; self.started.set(); time.sleep(self.delay)
        v = self.data.get(source["id"], [])
        return FetchResult(source["id"], False, error="simulated timeout") if v is None else FetchResult(source["id"], True, v)

def E(i, title, summary="", pub=PUB):
    return {"url": f"https://example.test/{i}", "title": title, "summary": summary, "published": pub}

@pytest.fixture
def env(tmp_path):
    cfg = load_config()
    cfg.sources = [{"id": "a", "name": "Source A", "url": "https://a.test/f", "tier": "reputable", "countries": []},
                   {"id": "b", "name": "Source B", "url": "https://b.test/f", "tier": "official", "countries": []}]
    cfg.retrieval["min_seconds_between_fetches"] = 0
    cfg.retrieval["partial_publish"] = False   # strict mode is the default under test; partial mode has its own tests
    clock, feeds, db = Clock(), Feeds(), tmp_path / "t.db"
    svc = RefreshService(cfg, db, feeds, clock)
    class Env: pass
    e = Env(); e.cfg, e.clock, e.feeds, e.db, e.svc = cfg, clock, feeds, db, svc
    e.conn = lambda: connect(db)
    def set_profile(**kw):
        c = connect(db); base = {"situations": ["borrower"], "activities": ["borrowing_saving"], "materiality": 30}
        base.update(kw); pf.save_profile(c, pf.clean_profile(base, [x["id"] for x in cfg.categories], 40)); c.close()
    e.set_profile = set_profile; set_profile()
    return e

# ---------- ranking ----------
def test_ranking_orders_by_relevance_and_filters_unrelated(env):
    env.set_profile(activities=["borrowing_saving", "tech_use"])
    env.feeds.data = {"a": [E(1, "Provider confirms data breach affecting user accounts", "Accounts were exposed, the provider said."),
                            E(2, TITLE, SUMMARY), E(3, "Local team wins the cup final", "A great match.")], "b": []}
    assert env.svc.refresh()["status"] == "new_edition"
    c = env.conn(); items = ed.current_edition(c)["payload"]["items"]
    assert items[0]["headline"] == TITLE and items[0]["relevance"] >= items[1]["relevance"]
    rej = ed.current_edition(c)["payload"]["rejected"]
    assert any("no rule matched" in r["reason"] for r in rej)

def test_no_pathway_for_unrelated_profile(env):
    env.set_profile(situations=[], activities=[])
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": []}
    assert env.svc.refresh()["status"] == "no_new"

def test_factors_are_explained_and_confidence_separate(env):
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": []}; env.svc.refresh()
    it = ed.current_edition(env.conn())["payload"]["items"][0]
    assert {f["factor"] for f in it["factors"]} == {"impact", "exposure", "goal", "time", "reach", "action"}
    assert it["confidence"]["label"] in ("High", "Medium", "Low") and "relevance" not in it["confidence"]
    assert round(sum(f["points"] for f in it["factors"])) == it["relevance"]
    assert it["status"] == "announced" and it["unknowns"]

def test_exclusion_and_emphasis(env):
    env.set_profile(excluded_categories=["financial", "markets"])
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": []}
    assert env.svc.refresh()["status"] == "no_new"

# ---------- deduplication ----------
def test_dedup_rules(env):
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY), {**E(9, TITLE, SUMMARY), "url": "https://example.test/1?utm_source=x#frag"}],
                      "b": [E(2, TITLE + " - Source B", SUMMARY), E(3, "Central bank raises interest rate by 0.5% to 8%", SUMMARY + " Analysts agree.")]}
    r = env.svc.refresh()
    c = env.conn(); assert c.execute("SELECT COUNT(DISTINCT story_id) FROM articles").fetchone()[0] == 1
    rules = {d["rule"].split("(")[0] for d in ed.current_edition(c)["payload"]["dedup_applied"]}
    assert "same_url" in rules and ({"same_title", "near_duplicate"} & rules)
    assert len(ed.current_edition(c)["payload"]["items"]) == 1

def test_repetitive_commentary_does_not_requalify_but_material_update_does(env):
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": []}; assert env.svc.refresh()["status"] == "new_edition"
    env.clock.advance(minutes=5)
    env.feeds.data["b"] = [E(2, TITLE, SUMMARY + " Analysts agree.")]          # same facts
    assert env.svc.refresh()["status"] == "no_new"
    env.clock.advance(hours=4)
    env.feeds.data["b"] = [E(2, TITLE, SUMMARY + " Analysts agree."), E(5, TITLE, SUMMARY + " A further 1% rise is due from 2026-12-01.")]
    assert env.svc.refresh()["status"] == "new_edition"
    assert ed.current_edition(env.conn())["payload"]["items"][0]["updated"]

# ---------- editions ----------
def test_new_edition_archives_previous_and_is_immutable(env):
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": []}; env.svc.refresh()
    first = ed.get_edition(env.conn(), 1)
    env.feeds.data["b"] = [E(7, "Government raises VAT rate to 16%", "The finance ministry confirmed the tax rate change, effective 2026-11-15.")]
    env.set_profile(situations=["borrower", "employed"], activities=["borrowing_saving", "expenses"])
    assert env.svc.refresh()["status"] == "new_edition"
    c = env.conn()
    assert ed.get_edition(c, 1) == first and ed.current_edition(c)["id"] == 2
    with pytest.raises(Exception, match="immutable"):
        c.execute("UPDATE editions SET payload='{}' WHERE id=1")

# ---------- cooldown ----------
def test_empty_check_starts_persistent_cooldown(env):
    r = env.svc.refresh()
    assert r["status"] == "no_new" and r["cooldown_until"] == iso(T0 + timedelta(hours=3))
    calls = env.feeds.calls
    assert env.svc.refresh()["status"] == "blocked_cooldown"          # repeat click
    other_tab = RefreshService(env.cfg, env.db, env.feeds, env.clock)   # new process/tab: same DB state
    assert other_tab.refresh()["status"] == "blocked_cooldown"
    assert env.feeds.calls == calls                                      # no fetch happened while blocked
    env.clock.advance(hours=2, minutes=59); assert env.svc.refresh()["status"] == "blocked_cooldown"
    env.clock.advance(minutes=2);           assert env.svc.refresh()["status"] == "no_new"

def test_cooldown_enforced_over_http_across_clients(env):
    app = create_app(cfg=env.cfg, db_path=str(env.db), fetcher=env.feeds, clock=env.clock)
    a, b = TestClient(app), TestClient(app)
    a.post("/refresh"); n = env.feeds.calls
    b.post("/refresh"); a.post("/refresh")
    assert env.feeds.calls == n
    assert "disabled" in b.get("/").text and "Next eligible check" in b.get("/").text

def test_failure_is_not_reported_as_no_news_and_starts_no_cooldown(env):
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": None}
    r = env.svc.refresh()
    assert r["status"] == "failed" and "NOT evidence" in r["message"]
    c = env.conn()
    assert ed.current_edition(c) is None and c.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0
    st = env.svc.status(c)
    assert st["cooldown_until"] is None and st["last_check_at"] is None and st["can_refresh"]

def test_partial_failure_keeps_current_edition(env):
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": []}; env.svc.refresh()
    env.feeds.data = {"a": [E(2, "Government raises VAT rate to 16%", "Confirmed, effective 2026-11-15.")], "b": None}
    env.clock.advance(minutes=5); assert env.svc.refresh()["status"] == "failed"
    assert ed.current_edition(env.conn())["id"] == 1

def test_no_sources_is_failure(env):
    env.cfg.sources = []; assert env.svc.refresh()["status"] == "failed"

def test_politeness_gap_is_separate_from_cooldown(env):
    env.cfg.retrieval["min_seconds_between_fetches"] = 60
    env.feeds.data = {"a": None, "b": []}
    assert env.svc.refresh()["status"] == "failed"
    assert env.svc.refresh()["status"] == "blocked_backoff"
    env.clock.advance(seconds=61); assert env.svc.refresh()["status"] == "failed"

def test_concurrent_refresh_only_one_fetches(env):
    env.feeds.delay = 0.6; env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": []}
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("first", env.svc.refresh())); t.start()
    env.feeds.started.wait(5)
    out["second"] = RefreshService(env.cfg, env.db, env.feeds, env.clock).refresh(); t.join()
    assert out["second"]["status"] == "blocked_in_progress" and out["first"]["status"] == "new_edition"
    assert env.feeds.calls == 2   # two sources, once each

def test_stale_lock_recovers(env):
    c = env.conn(); from app.db import set_state; set_state(c, "refresh_started", iso(T0 - timedelta(minutes=30)))
    assert env.svc.refresh()["status"] == "no_new"

# ---------- empty results & UI ----------
def test_empty_state_message(env):
    app = create_app(cfg=env.cfg, db_path=str(env.db), fetcher=env.feeds, clock=env.clock); c = TestClient(app)
    assert "No edition yet" in c.get("/").text
    c.post("/refresh"); assert "nothing new qualified" in c.get("/", ).text

def test_pages_and_item_detail_and_what_if(env):
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": []}
    app = create_app(cfg=env.cfg, db_path=str(env.db), fetcher=env.feeds, clock=env.clock); c = TestClient(app); c.post("/refresh")
    for p in ("/", "/editions", "/editions/1", "/profile", "/map", "/opportunities", "/sources", "/data/export"):
        assert c.get(p).status_code == 200
    sid = ed.current_edition(env.conn())["payload"]["items"][0]["story_id"]
    page = c.get(f"/editions/1/item/{sid}"); assert "Score breakdown" in page.text and "would score" in page.text
    assert c.get("/editions?q=Central").text.count("Edition #1") >= 1 and "No saved editions" in c.get("/editions?q=zzz").text

def test_export_delete_and_origin_guard(env):
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": []}
    app = create_app(cfg=env.cfg, db_path=str(env.db), fetcher=env.feeds, clock=env.clock); c = TestClient(app); c.post("/refresh")
    assert json.loads(c.get("/data/export").text)["editions"]
    assert c.post("/data/delete", data={"scope": "everything", "confirm": "no"}).status_code == 400
    assert c.post("/refresh", headers={"origin": "https://evil.example"}).status_code == 403
    c.post("/data/delete", data={"scope": "everything", "confirm": "DELETE"})
    assert json.loads(c.get("/data/export").text)["editions"] == []

# ---------- retrieval parsing ----------
RSS = b"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title><item><title>Hello</title><link>https://x.test/1</link><pubDate>Mon, 19 Oct 2026 08:00:00 GMT</pubDate></item></channel></rss>"
def test_feed_parsing():
    r = parse_feed_bytes("s", RSS); assert r.ok and r.entries[0]["published"] == "2026-10-19T08:00:00Z"
    assert not parse_feed_bytes("s", b"<html>not a feed").ok
    e = parse_feed_bytes("s", b"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title></channel></rss>")
    assert e.ok and "zero entries" in e.warning

# ---------- opportunity timing ----------
def _series(years, low_month=7):
    return [[f"{y}-{m:02d}", 80.0 if m == low_month else 100.0] for y in years for m in range(1, 13)]
def test_timing_is_evidence_gated():
    g = {"title": "Buy laptop", "activities": ["tech_use"]}
    rec = lambda s, url="https://x.test/d": {"title": "Prices", "kind": "historical_series", "activities": ["tech_use"], "better": "low", "evidence_url": url, "series": s, "_file": "f.toml"}
    assert evaluate_goal(g, [rec(_series([2022, 2023, 2024, 2025]))])[0]["verdict"] == "Demonstrated seasonal pattern"
    assert evaluate_goal(g, [rec(_series([2024, 2025]))])[0]["verdict"] == "Insufficient evidence"
    assert evaluate_goal(g, [rec(_series([2022, 2023, 2024]), url="")])[0]["verdict"] == "Insufficient evidence"
    assert evaluate_goal(g, [])[0]["verdict"] == "Insufficient evidence"
    noisy = [[f"{y}-{m:02d}", 100.0 + ((y * 7 + m * 13) % 17)] for y in (2022, 2023, 2024, 2025) for m in range(1, 13)]
    assert evaluate_goal(g, [rec(noisy)])[0]["verdict"] in ("Weak evidence", "Demonstrated seasonal pattern")

# ---------- config ----------
def test_relationships_require_sources(tmp_path):
    import shutil; shutil.copytree("config", tmp_path / "c")
    (tmp_path / "c" / "institutions.toml").write_text('[[institutions]]\nid="x"\nname="X"\ncategory="financial"\n[[institutions.relationships]]\nto="y"\ntype="owns"\n')
    with pytest.raises(ConfigError): load_config(tmp_path / "c")


def test_near_duplicate_with_different_wording_is_merged(env):
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)],
                      "b": [E(2, "Central bank lifts interest rate by 0.5% to 8%", SUMMARY)]}   # different title, same event
    env.svc.refresh(); c = env.conn()
    assert c.execute("SELECT COUNT(DISTINCT story_id) FROM articles").fetchone()[0] == 1
    assert any(d["rule"].startswith("near_duplicate(similarity=") for d in ed.current_edition(c)["payload"]["dedup_applied"])

def test_dissimilar_stories_are_not_merged(env):
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": [E(2, "Provider confirms data breach affecting user accounts", "Accounts were exposed, the provider said.")]}
    env.svc.refresh(); assert env.conn().execute("SELECT COUNT(DISTINCT story_id) FROM articles").fetchone()[0] == 2


def test_certificate_errors_get_actionable_hint_and_verification_stays_on(monkeypatch):
    import ssl, urllib.error
    from app.retrieval import describe_error, _ssl_context
    msg = describe_error(urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")))
    assert "INEWS_CA_BUNDLE" in msg and "enabled = false" in msg
    monkeypatch.delenv("INEWS_CA_BUNDLE", raising=False)
    assert _ssl_context().verify_mode == ssl.CERT_REQUIRED


def test_reevaluate_after_profile_change_works_during_cooldown_without_fetching(env):
    env.set_profile(situations=[], activities=[])
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": []}
    assert env.svc.refresh()["status"] == "no_new"                      # empty profile -> cooldown starts
    calls, cd = env.feeds.calls, env.svc.status(env.conn())["cooldown_until"]
    r = env.svc.reevaluate(); assert r["status"] == "local_none" and "no documented pathway" in r["message"] or "nothing in your profile" in r["message"]
    env.set_profile()                                                    # user now fills in profile
    assert env.svc.refresh()["status"] == "blocked_cooldown"           # network refresh still blocked
    r = env.svc.reevaluate()
    assert r["status"] == "local_new_edition" and ed.current_edition(env.conn())["payload"]["items"]
    assert env.feeds.calls == calls and env.svc.status(env.conn())["cooldown_until"] == cd

def test_reevaluate_with_no_stored_articles_does_not_fetch(env):
    r = env.svc.reevaluate(); assert r["status"] == "local_blocked" and env.feeds.calls == 0

def test_reevaluate_route(env):
    app = create_app(cfg=env.cfg, db_path=str(env.db), fetcher=env.feeds, clock=env.clock); c = TestClient(app)
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": []}; env.set_profile(situations=[], activities=[]); c.post("/refresh")
    env.set_profile(); c.post("/reevaluate"); h = c.get("/").text
    assert "Last local re-evaluation" in h and "Central bank raises interest rate" in h


@pytest.mark.parametrize("headline,rule", [
    ("Bank of England cuts interest rates to 3.75%", "borrowing-costs"), ("Mortgage rates fall for third month", "borrowing-costs"),
    ("Energy bills to rise as price cap increases", "utilities-supply"), ("Thousands of jobs at risk as firm announces redundancies", "labour-market"),
    ("Hackers steal customer data in cyber attack", "cyber-security"), ("Visa rules tightened for students", "immigration-visas")])
def test_realistic_headlines_match_expected_rule(headline, rule):
    from app.scoring import Analyzer
    a = Analyzer(load_config()); low = headline.lower()
    assert rule in {r["id"] for r in a.rules if any(rx.search(low) for _, rx in r["_rx"])}

def test_unrelated_headlines_still_match_nothing():
    from app.scoring import Analyzer
    a = Analyzer(load_config())
    for h in ("Local team wins the cup final", "Celebrity couple announce engagement", "Rain expected over the weekend"):
        assert not [r for r in a.rules if any(rx.search(h.lower()) for _, rx in r["_rx"])], h


def test_partial_publish_labels_edition_and_never_starts_cooldown(env):
    env.cfg.retrieval["partial_publish"] = True
    env.feeds.data = {"a": [E(1, TITLE, SUMMARY)], "b": None}
    r = env.svc.refresh(); c = env.conn()
    assert r["status"] == "new_edition" and "PARTIAL" in r["message"]
    assert ed.current_edition(c)["payload"]["partial"] is True
    st = env.svc.status(c); assert st["cooldown_until"] is None and st["last_check_at"] is None
    env.clock.advance(minutes=5); env.feeds.data = {"a": [], "b": None}
    r = env.svc.refresh(); assert r["status"] == "no_new_partial" and env.svc.status(env.conn())["cooldown_until"] is None
    env.clock.advance(minutes=5); env.feeds.data = {"a": None, "b": None}
    assert env.svc.refresh()["status"] == "failed"

def test_institution_country_link_and_seed(env, tmp_path):
    from app.seed import seed
    assert seed("config/profile.seed.json", env.db, env.cfg) == 3 and seed("config/profile.seed.json", env.db, env.cfg) == 0
    p = pf.load_profile(env.conn()); assert p["city"] == "Vanderbijlpark" and "migrant" in p["situations"] and "street" not in json.dumps(p).lower()
    env.feeds.data = {"a": [E(1, "Eskom announces electricity tariff increase of 12% from 2026-11-01", "Households will pay more.")], "b": []}
    assert env.svc.refresh()["status"] == "new_edition"
    it = ed.current_edition(env.conn())["payload"]["items"][0]
    assert it["institution"]["name"] == "Eskom" and "tied to your country" in next(f for f in it["factors"] if f["factor"] == "exposure")["why"]

def test_south_africa_student_headlines_match():
    from app.scoring import Analyzer
    a = Analyzer(load_config())
    for h, rule in [("NSFAS announces new funding rules for 2027", "education-costs"), ("Home Affairs changes study permit requirements", "immigration-visas"),
                    ("Naira slides as CBN changes forex rules", "cross-border-money"), ("ArcelorMittal announces retrenchments at Vanderbijlpark", "local-industry-employment"),
                    ("Mobile data prices cut by Vodacom", "ai-tech-access")]:
        assert rule in {r["id"] for r in a.rules if any(rx.search(h.lower()) for _, rx in r["_rx"])}, h


def test_shipped_configs_are_valid_and_use_https():
    import tomllib
    cfg = load_config()
    assert cfg.enabled_sources and all(s["url"].startswith("https://") and s["tier"] in ("official", "reputable", "other") for s in cfg.sources)
    assert {"businesstech", "mybroadband", "techcentral", "businessday-ng"} <= {s["id"] for s in cfg.enabled_sources}
    cand = tomllib.load(open("config/sources.candidates.toml", "rb"))["sources"]
    assert cand and not any(s["enabled"] for s in cand)

WP_RSS = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>Sample WordPress feed</title>
<item><title>Eskom tariff increase of 12% &#8211; what to expect</title><link>https://example.test/news/energy/1/</link><dc:creator><![CDATA[A Writer]]></dc:creator>
<pubDate>Mon, 19 Oct 2026 15:00:00 +0000</pubDate><category><![CDATA[Energy]]></category><guid isPermaLink="false">https://example.test/?p=1</guid>
<description><![CDATA[Households will pay more from 2026-11-01.]]></description></item></channel></rss>"""
def test_wordpress_style_rss_parses():
    r = parse_feed_bytes("wp", WP_RSS)
    assert r.ok and len(r.entries) == 1 and r.entries[0]["published"] == "2026-10-19T15:00:00Z"
    assert "tariff increase of 12%" in r.entries[0]["title"] and "2026-11-01" in r.entries[0]["summary"]
