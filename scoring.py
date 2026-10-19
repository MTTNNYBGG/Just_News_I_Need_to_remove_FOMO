"""Deterministic, inspectable relevance analysis. No model is involved.
relevance = 100 * gate * sum(weight_i * factor_i), with every factor and weight returned for display.
gate = 0 unless an activity, situation or goal links you to the pathway (location alone never opens it)."""
import re
from datetime import date
from .processing import TfIdf, tokens, extract_dates, numbers, classify_status
from .profile import PRIORITIES

_CUE = re.compile(r"\b(deadline|takes? effect|effective (?:from|on)|applications? (?:open|close)|comes? into (?:force|effect)|from next)\b", re.I)
_HOPS = {1: 1.0, 2: 0.66, 3: 0.33}
_STATUS_ADJ = {"confirmed": 0.0, "announced": -0.05, "forecast": -0.15, "allegation": -0.25, "unclassified": -0.05}
_REC = {"action_now": "Review now (time-sensitive)",
        "preparation": "Preparation worth considering",
        "awareness": "Awareness only: no action identified"}

def _label(v, hi, mid):
    return "High" if v >= hi else "Medium" if v >= mid else "Low"

class Analyzer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sources = {s["id"]: s for s in cfg.sources}
        self.rules = cfg.rules
        for r in self.rules:
            r["_rx"] = [(k, re.compile(r"(?<![a-z0-9])" + re.escape(k.lower()) + r"(?:s|es|ed|d|ing)?(?![a-z0-9])")) for k in r["keywords"]]
        self.insts = cfg.institutions
        for i in self.insts:
            i["_rx"] = [re.compile(r"(?<![a-z0-9])" + re.escape(a.lower()) + r"(?![a-z0-9])") for a in i.get("aliases", [i["name"]])]

    def prepare(self, stories, goals):
        docs = [tokens(s["rep"]["title"] + " " + s["rep"]["summary"]) for s in stories]
        gdocs = [tokens(f'{g["title"]} {g.get("outcome","")} {g.get("dependencies","")}') for g in goals]
        tf = TfIdf(docs + gdocs)
        return {"tf": tf, "goal_vecs": {g["id"]: tf.vec(d) for g, d in zip(goals, gdocs)}}

    def _institution(self, low):
        for i in self.insts:
            if any(rx.search(low) for rx in i["_rx"]):
                return i
        return None

    def _location(self, profile, src, inst, low, rule):
        city, region, country = (profile.get(k, "").lower() for k in ("city", "region", "country"))
        if city and city in low: return 1.0, f"text mentions your city ({profile['city']})"
        if region and region in low: return 1.0, f"text mentions your region ({profile['region']})"
        if country and country in low: return 0.8, f"text mentions your country ({profile['country']})"
        sc = [c.lower() for c in src.get("countries", [])] + [c.lower() for c in (inst or {}).get("countries", [])]
        if country and country in sc: return 0.8, "source or institution is tied to your country"
        if rule.get("global"): return 0.5, "pathway is global (supply chain, market or technology)"
        return 0.0, "no location link found"

    def _evaluate(self, rule, hits, story, profile, goals, ctx, today, low, text, inst):
        rep, cfg = story["rep"], self.cfg
        src = self.sources.get(rep["source_id"], {})
        w = cfg.weights
        act = sorted(set(rule["activities"]) & set(profile["activities"]))
        sit = sorted(set(rule["situations"]) & set(profile["situations"]))
        loc, loc_why = self._location(profile, src, inst, low, rule)
        interest = 0.1 if set(rule.get("categories", [])) & set(profile.get("interests", [])) else 0.0
        exposure = min(1.0, 0.5 * bool(act) + 0.3 * bool(sit) + 0.2 * loc + interest)
        has_mag = bool(numbers(text))
        impact = rule["impact"] * (1.0 if has_mag else 0.85)
        # goals
        goal_val, goal_hit = 0.0, None
        avec = ctx["tf"].vec(tokens(text))
        for g in goals:
            ov = sorted(set(rule["activities"]) & set(g.get("activities", [])))
            cos = TfIdf.cos(avec, ctx["goal_vecs"].get(g["id"], {}))
            v = max(0.8 if ov else 0.0, min(1.0, 3 * cos)) * PRIORITIES.get(g.get("priority"), 0.85)
            if v > goal_val:
                goal_val, goal_hit = v, {"id": g["id"], "title": g["title"], "shared_activities": ov, "text_similarity": round(cos, 2)}
        # time
        dates = extract_dates(text)
        future = [d for d in dates if d >= today]
        horizon = cfg.horizon_days.get(profile.get("horizon", "weeks"), 45)
        if future:
            delta = (min(future) - today).days
            time_v, time_why = (1.0, f"date {min(future).isoformat()} falls within your horizon ({delta} days)") if delta <= horizon \
                else (0.4, f"date {min(future).isoformat()} is beyond your horizon ({delta} days)")
        elif _CUE.search(text):
            time_v, time_why = 0.4, "wording suggests a deadline or effective date, but none is stated"
        else:
            time_v, time_why = 0.0, "no date or deadline found in the text"
        reach = _HOPS.get(rule.get("hops", 2), 0.66)
        action = rule["actionability"]
        factors = [
            ("impact", impact, "rule severity" + (" (magnitude stated in text)" if has_mag else " reduced 15%: no amount stated")),
            ("exposure", exposure, f"activities {act or 'none'}, situations {sit or 'none'}, location: {loc_why}" + (", area of interest" if interest else "")),
            ("goal", goal_val, f"goal '{goal_hit['title']}'" if goal_hit else "no related goal"),
            ("time", time_v, time_why),
            ("reach", reach, f"{rule.get('hops', 2)} step(s) between the institution and you"),
            ("action", action, "how actionable this kind of development usually is"),
        ]
        gate = 1.0 if (act or sit or goal_val > 0) else 0.0   # location/interest refine relevance but never create it
        parts = [{"factor": n, "value": round(v, 2), "weight": w[n], "points": round(100 * gate * w[n] * v, 1), "why": why}
                 for n, v, why in factors]
        relevance = round(sum(p["points"] for p in parts))
        status = classify_status(text)
        n_src = len({m["source_id"] for m in story["members"]})
        conf = self.cfg.tiers.get(src.get("tier", "other"), 0.5) + _STATUS_ADJ[status] + (0.1 if n_src > 1 else 0.0)
        conf = max(0.0, min(1.0, conf))
        conf_why = [f"source tier '{src.get('tier', 'other')}'", f"wording classified as '{status}' (heuristic)",
                    f"{n_src} independent source(s) in this story"]
        rec = "awareness"
        if action >= 0.6 and time_v >= 0.7 and status in ("confirmed", "announced"):
            rec = "action_now"
        elif action >= 0.4 and relevance >= profile["materiality"]:
            rec = "preparation"
        unknowns = ["Your personal exposure is inferred from your profile and the rule; it is not verified."]
        if not dates: unknowns.append("No effective date or deadline is stated in the retrieved text.")
        if not has_mag: unknowns.append("No amount or size of change is stated in the retrieved text.")
        if n_src < 2: unknowns.append("Only one source in the retrieved set; not independently corroborated here.")
        unknowns.append("Event date is not independently verified; only the feed's publication date is shown.")
        band = _label(relevance, self.cfg.thresholds["band_high"], self.cfg.thresholds["band_medium"])
        link = []
        if act: link.append("your activities (" + ", ".join(act) + ")")
        if sit: link.append("your situation (" + ", ".join(sit) + ")")
        if goal_hit: link.append(f"your goal '{goal_hit['title']}'")
        if loc >= 0.8: link.append(loc_why)
        connection = ("Linked through " + "; ".join(link) + ".") if link else "No direct link to your profile was found."
        return {
            "story_id": story["story_id"], "rule_id": rule["id"], "rule_label": rule.get("label", rule["id"]),
            "headline": rep["title"], "url": rep["url"], "source_id": rep["source_id"],
            "source_name": src.get("name", rep["source_id"]), "tier": src.get("tier", "other"),
            "published": rep["published"], "retrieved": rep["first_retrieved"],
            "dates_mentioned": [d.isoformat() for d in dates], "status": status,
            "institution": {"id": inst["id"], "name": inst["name"], "category": inst["category"]} if inst else None,
            "categories": rule.get("categories", []), "direction": rule.get("direction", "mixed"),
            "relevance": relevance, "band": band, "factors": parts, "matched_keywords": hits,
            "impact": round(impact, 2), "impact_label": _label(impact, 0.6, 0.4),
            "likelihood": round(exposure, 2), "likelihood_label": _label(exposure, 0.7, 0.4),
            "goal": goal_hit,
            "confidence": {"value": round(conf, 2), "label": _label(conf, 0.75, 0.55), "reasons": conf_why},
            "mechanism": rule["mechanism"], "connection": connection, "consequence": rule["consequence"],
            "monitor": rule.get("monitor", "") + (f" Dates in the text: {', '.join(d.isoformat() for d in dates)}." if dates else ""),
            "recommended": rec, "recommended_label": _REC[rec],
            "timing_note": time_why,
            "evidence": {"excerpt": rep["summary"][:300],
                         "members": [{"title": m["title"], "url": m["url"], "source": self.sources.get(m["source_id"], {}).get("name", m["source_id"]),
                                      "published": m["published"], "dedup": m.get("dedup_note")} for m in story["members"]]},
            "unknowns": unknowns, "reject": None,
        }

    def score(self, story, profile, goals, ctx, today=None):
        today = today or date.today()
        rep = story["rep"]
        text = f'{rep["title"]}. {rep["summary"]}'
        low = text.lower()
        inst = self._institution(low)
        best, matched = None, False
        for rule in self.rules:
            hits = [k for k, rx in rule["_rx"] if rx.search(low)]
            if not hits:
                continue
            matched = True
            it = self._evaluate(rule, hits, story, profile, goals, ctx, today, low, text, inst)
            cats = set(rule.get("categories", [])) | ({inst["category"]} if inst else set())
            direction = rule.get("direction", "mixed")
            if cats & set(profile.get("excluded_categories", [])):
                it["reject"] = "excluded area"
            elif (profile["emphasis"] == "risks" and direction == "opportunity") or (profile["emphasis"] == "opportunities" and direction == "risk"):
                it["reject"] = f"outside your emphasis ({profile['emphasis']})"
            elif it["relevance"] == 0:
                it["reject"] = "no documented pathway to your profile or goals"
            elif it["relevance"] < profile["materiality"]:
                it["reject"] = f"below materiality threshold ({it['relevance']} < {profile['materiality']})"
            elif it["recommended"] == "awareness" and not profile["include_awareness"] and it["band"] != "High":
                it["reject"] = "awareness-only item (hidden by your preference)"
            if best is None or (best["reject"] and not it["reject"]) or (bool(best["reject"]) == bool(it["reject"]) and it["relevance"] > best["relevance"]):
                best = it
        if not matched:
            return {"story_id": story["story_id"], "headline": rep["title"], "url": rep["url"], "relevance": 0,
                    "reject": "no rule matched: no documented pathway from this development to a person's options"}
        return best
