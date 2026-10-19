"""Opportunity Timing: evidence-gated. Only user-supplied, sourced records are evaluated.
Without qualifying evidence the answer is always 'Insufficient evidence'. No biological or personal-state inference."""
import tomllib
from datetime import date
from pathlib import Path

MIN_YEARS = 3

def load_timing(directory):
    recs = []
    d = Path(directory)
    if d.is_dir():
        for f in sorted(d.glob("*.toml")):
            with open(f, "rb") as fh:
                for r in tomllib.load(fh).get("records", []):
                    r["_file"] = f.name
                    recs.append(r)
    return recs

def _seasonal(rec):
    pts = {}
    for ym, v in rec.get("series", []):
        try:
            y, m = int(ym[:4]), int(ym[5:7]); pts[(y, m)] = float(v)
        except (ValueError, IndexError):
            continue
    years = sorted({y for y, _ in pts})
    full = [y for y in years if sum((y, m) in pts for m in range(1, 13)) >= 10]
    if len(full) < MIN_YEARS:
        return None, f"Only {len(full)} year(s) with near-complete monthly data; at least {MIN_YEARS} are required."
    low_better = rec.get("better", "low") == "low"
    idx = {m: [] for m in range(1, 13)}
    ranks = {m: 0 for m in range(1, 13)}
    for y in full:
        vals = {m: pts[(y, m)] for m in range(1, 13) if (y, m) in pts}
        mean = sum(vals.values()) / len(vals)
        for m, v in vals.items():
            idx[m].append(v / mean)
        order = sorted(vals, key=lambda m: vals[m], reverse=not low_better)
        for m in order[: max(1, len(order) // 3)]:
            ranks[m] += 1
    avg = {m: sum(v) / len(v) for m, v in idx.items() if v}
    best = (min if low_better else max)(avg, key=avg.get)
    consistency = ranks[best] / len(full)
    return {"month": best, "avg_vs_year_mean_pct": round((avg[best] - 1) * 100, 1), "years": len(full),
            "consistency": round(consistency, 2)}, ""

def evaluate_goal(goal, records, today=None):
    today = today or date.today()
    rows = []
    for r in records:
        if not (set(r.get("activities", [])) & set(goal.get("activities", []))):
            continue
        base = {"goal": goal["title"], "title": r.get("title", r.get("id")), "mechanism": r.get("mechanism", "Not documented"),
                "evidence_url": r.get("evidence_url", ""), "benefit": r.get("potential_benefit", "Not documented"),
                "cost": r.get("cost_of_acting", "Not documented"), "waiting_risk": r.get("risk_of_waiting", "Not documented"),
                "source_file": r["_file"]}
        if not base["evidence_url"].startswith(("http://", "https://")):
            rows.append({**base, "verdict": "Insufficient evidence", "detail": "Record has no evidence URL, so it cannot be verified."}); continue
        kind = r.get("kind")
        if kind in ("published_calendar", "official_deadline"):
            try:
                s, e = date.fromisoformat(r["window_start"]), date.fromisoformat(r.get("window_end", r["window_start"]))
            except (KeyError, ValueError):
                rows.append({**base, "verdict": "Insufficient evidence", "detail": "Window dates are missing or malformed."}); continue
            state = "open now" if s <= today <= e else f"opens in {(s - today).days} days" if s > today else "closed for this cycle"
            rows.append({**base, "verdict": "Published schedule", "window": f"{s} to {e}", "state": state,
                         "likelihood": "Scheduled per the cited source; institutions can change dates.",
                         "detail": "Based on a published date, not a statistical pattern."})
        elif kind == "historical_series":
            res, why = _seasonal(r)
            if res is None:
                rows.append({**base, "verdict": "Insufficient evidence", "detail": why})
            elif res["consistency"] >= 0.67:
                rows.append({**base, "verdict": "Demonstrated seasonal pattern", "window": f"month {res['month']}",
                             "likelihood": f"Advantageous month in {round(res['consistency']*res['years'])} of {res['years']} years (consistency {res['consistency']}); "
                                           f"on average {res['avg_vs_year_mean_pct']}% vs the year's mean. Past pattern, not a guarantee.",
                             "detail": "Computed from the supplied series."})
            else:
                rows.append({**base, "verdict": "Weak evidence", "detail": f"Best month was advantageous in only {res['consistency']:.0%} of {res['years']} years; not treated as a pattern."})
        else:
            rows.append({**base, "verdict": "Insufficient evidence", "detail": f"Unknown record kind '{kind}'."})
    if not rows:
        rows.append({"goal": goal["title"], "title": "No timing data", "verdict": "Insufficient evidence",
                     "detail": "No sourced timing records match this goal's activities. Add records under data/timing/."})
    return rows
