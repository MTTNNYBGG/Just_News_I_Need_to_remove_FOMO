"""Load a starter profile and goals from JSON: python -m app.seed config/profile.seed.json
Overwrites the profile; adds goals whose titles do not already exist. Local only."""
import json, os, sys
from .config import load_config
from .db import connect
from . import profile as pf

def seed(path, db_path, cfg):
    data = json.load(open(path))
    c = connect(db_path)
    try:
        pf.save_profile(c, pf.clean_profile(data.get("profile", {}), [x["id"] for x in cfg.categories], cfg.thresholds["materiality_default"]))
        have = {g["title"] for g in pf.list_goals(c)}
        added = 0
        for g in data.get("goals", []):
            g = pf.clean_goal(g)
            if g and g["title"] not in have:
                pf.add_goal(c, g); added += 1
        return added
    finally:
        c.close()

if __name__ == "__main__":
    n = seed(sys.argv[1], os.environ.get("INEWS_DB", "data/inews.sqlite3"), load_config())
    print(f"Profile saved; {n} goal(s) added.")
