"""Refresh state machine. All gating state is in SQLite so reloads, other tabs and restarts see the same truth."""
import json
from datetime import timedelta
from .db import connect, utcnow, iso, parse_iso, get_state, set_state, del_state, jdump
from .processing import ingest
from .scoring import Analyzer
from . import editions
from .profile import load_profile, list_goals

class RefreshService:
    def __init__(self, cfg, db_path, fetcher, clock=utcnow):
        self.cfg, self.db_path, self.fetcher, self.clock = cfg, db_path, fetcher, clock
        self.analyzer = Analyzer(cfg)
        connect(db_path).close()

    # ---- read-only status -------------------------------------------------
    def status(self, conn):
        now = self.clock()
        cd = get_state(conn, "cooldown_until")
        active = bool(cd) and now < parse_iso(cd)
        started = get_state(conn, "refresh_started")
        busy = bool(started) and now - parse_iso(started) < timedelta(minutes=self.cfg.retrieval["stale_lock_minutes"])
        last = get_state(conn, "last_fetch_at")
        gap = self.cfg.retrieval["min_seconds_between_fetches"]
        backoff = bool(last) and (now - parse_iso(last)).total_seconds() < gap and not active and not busy
        reason = ("cooldown" if active else "in_progress" if busy else "backoff" if backoff else None)
        return {"can_refresh": reason is None, "reason": reason, "cooldown_until": cd if active else None,
                "last_check_at": get_state(conn, "last_check_at"), "last_check_outcome": get_state(conn, "last_check_outcome"),
                "last_result": json.loads(get_state(conn, "last_refresh_result") or "null"),
                "backoff_seconds": gap}

    def _blocked(self, st):
        msgs = {"cooldown": f"Blocked: the last complete check found nothing new. Next eligible check: {st['cooldown_until']}.",
                "in_progress": "Blocked: another refresh is already running.",
                "backoff": f"Blocked: please wait {st['backoff_seconds']} seconds between fetch rounds."}
        return {"status": f"blocked_{st['reason']}", "message": msgs[st["reason"]], "sources": [],
                "cooldown_until": st["cooldown_until"]}

    # ---- local only: re-score ALREADY STORED articles. Never fetches, never touches the cooldown.
    def reevaluate(self):
        conn = connect(self.db_path)
        try:
            now = self.clock()
            conn.execute("BEGIN IMMEDIATE")
            st = self.status(conn)
            n = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            if st["reason"] == "in_progress" or n == 0:
                conn.execute("ROLLBACK")
                msg = "A refresh is running; try again in a moment." if n else "No stored articles yet. Press Refresh once to retrieve some."
                return {"status": "local_blocked", "message": msg, "at": iso(now)}
            profile = load_profile(conn, self.cfg.thresholds["materiality_default"]); goals = list_goals(conn)
            last = json.loads(get_state(conn, "last_refresh_result") or "null")
            payload, fresh = editions.build_candidates(conn, self.analyzer, self.cfg, profile, goals, now, set(), [], (last or {}).get("sources", []))
            if fresh:
                eid = editions.publish(conn, payload, profile, goals, now, get_state(conn, "last_check_at") or iso(now))
                res = {"status": "local_new_edition", "at": iso(now),
                       "message": f"Edition #{eid} published from stored articles with {len(fresh)} new item(s). Nothing was fetched; the cooldown is unchanged."}
            else:
                why = "; ".join(f"{v}: {k}" for k, v in sorted(payload["reject_summary"].items(), key=lambda kv: -kv[1])) or "no stored stories in the lookback window"
                sample = [r["headline"] for r in payload["rejected"] if r["reason"].startswith("no rule matched")][:15]
                res = {"status": "local_none", "at": iso(now), "unmatched_sample": sample,
                       "message": f"Re-evaluated {payload['stories_considered']} stored stories under your current profile; none newly qualify. Reasons: {why}. Nothing was fetched."}
            set_state(conn, "last_local_result", jdump(res))
            conn.execute("COMMIT")
            return res
        except Exception:
            conn.execute("ROLLBACK"); raise
        finally:
            conn.close()

    # ---- the one place retrieval happens ---------------------------------------
    def refresh(self):
        conn = connect(self.db_path)
        try:
            now = self.clock()
            conn.execute("BEGIN IMMEDIATE")
            st = self.status(conn)
            if not st["can_refresh"]:
                conn.execute("ROLLBACK")
                return self._blocked(st)
            set_state(conn, "refresh_started", iso(now)); set_state(conn, "last_fetch_at", iso(now))
            conn.execute("COMMIT")
            try:
                result = self._run(conn, now)
            finally:
                conn.execute("BEGIN IMMEDIATE"); del_state(conn, "refresh_started"); conn.execute("COMMIT")
            return result
        finally:
            conn.close()

    def _run(self, conn, started):
        sources = self.cfg.enabled_sources
        if not sources:
            return self._record(conn, started, "failed", "No enabled sources are configured; nothing was checked.", [], {})
        results = [self.fetcher(s) for s in sources]
        checks = [{"id": s["id"], "name": s.get("name", s["id"]), "ok": r.ok, "entries": len(r.entries),
                   "error": r.error, "warning": r.warning, "http_status": r.http_status, "queried_at": iso(self.clock())}
                  for s, r in zip(sources, results)]
        failed = [c for c in checks if not c["ok"]]
        partial = bool(failed)
        if failed and not (self.cfg.retrieval.get("partial_publish", False) and len(failed) < len(checks)):
            msg = (f"{len(failed)} of {len(checks)} sources failed. This is NOT evidence that there is no news. "
                   "Your current edition was kept unchanged and no cooldown was started.")
            return self._record(conn, started, "failed", msg, checks, {})
        profile = load_profile(conn, self.cfg.thresholds["materiality_default"])
        goals = list_goals(conn)
        now = self.clock()
        conn.execute("BEGIN IMMEDIATE")
        try:
            merged, material = [], set()
            for s, r in zip(sources, results):
                if not r.ok:
                    continue
                stats = ingest(conn, s["id"], r.entries, now, self.cfg.retrieval["lookback_days"],
                               self.cfg.retrieval["near_duplicate_similarity"])
                merged += stats["merged"]; material |= stats["material"]
            payload, fresh = editions.build_candidates(conn, self.analyzer, self.cfg, profile, goals, now, material, merged, checks)
            payload["partial"] = partial
            names = ", ".join(c["name"] for c in failed)
            if fresh:
                eid = editions.publish(conn, payload, profile, goals, now, iso(now))
                note = f" PARTIAL: {len(failed)} source(s) failed and are missing ({names}); no cooldown was started." if partial else ""
                out = self._finish(conn, now, "new_edition", f"Edition #{eid} published with {len(fresh)} new or updated item(s). The previous edition remains in the archive.{note}", checks, None, complete=not partial)
            elif partial:
                out = self._finish(conn, now, "no_new_partial", f"Nothing new qualified among the {len(checks) - len(failed)} working source(s), but {len(failed)} failed ({names}), so this is NOT a complete check and no cooldown was started.", checks, None, complete=False)
            else:
                cd = iso(now + timedelta(hours=self.cfg.retrieval["cooldown_hours"]))
                out = self._finish(conn, now, "no_new", f"All {len(checks)} sources were checked successfully and nothing new qualified. "
                                   f"Next eligible check: {cd}.", checks, cd)
            conn.execute("COMMIT")
            return out
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _finish(self, conn, now, outcome, msg, checks, cooldown, complete=True):
        res = {"status": outcome, "message": msg, "sources": checks, "cooldown_until": cooldown, "at": iso(now)}
        if complete:
            set_state(conn, "last_check_at", iso(now)); set_state(conn, "last_check_outcome", outcome)
        if cooldown:
            set_state(conn, "cooldown_until", cooldown)
        else:
            del_state(conn, "cooldown_until")
        set_state(conn, "last_refresh_result", jdump(res))
        conn.execute("INSERT INTO checks(started_at,finished_at,outcome,detail) VALUES(?,?,?,?)",
                     (iso(now), iso(self.clock()), outcome, jdump(res)))
        return res

    def _record(self, conn, started, outcome, msg, checks, _):
        conn.execute("BEGIN IMMEDIATE")
        res = {"status": outcome, "message": msg, "sources": checks, "cooldown_until": None, "at": iso(self.clock())}
        set_state(conn, "last_refresh_result", jdump(res))
        conn.execute("INSERT INTO checks(started_at,finished_at,outcome,detail) VALUES(?,?,?,?)",
                     (iso(started), iso(self.clock()), outcome, jdump(res)))
        conn.execute("COMMIT")
        return res
