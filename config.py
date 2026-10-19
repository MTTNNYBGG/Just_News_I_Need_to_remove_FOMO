"""Configuration loading and validation. All ranking inputs come from files in config/."""
import os, tomllib
from dataclasses import dataclass, field
from pathlib import Path

class ConfigError(Exception):
    pass

@dataclass
class Config:
    sources: list
    rules: list
    institutions: list
    categories: list
    weights: dict
    thresholds: dict
    retrieval: dict
    tiers: dict
    horizon_days: dict
    timing_dir: Path
    user_agent: str = "inews-personal/0.1"

    @property
    def enabled_sources(self):
        return [s for s in self.sources if s.get("enabled", True)]

def _load(path):
    with open(path, "rb") as f:
        return tomllib.load(f)

def load_config(config_dir=None, timing_dir=None):
    cdir = Path(config_dir or os.environ.get("INEWS_CONFIG_DIR", "config"))
    src_path = cdir / "sources.toml"
    if not src_path.exists():
        src_path = cdir / "sources.example.toml"
    src = _load(src_path)
    scoring = _load(cdir / "scoring.toml")
    rules = _load(cdir / "rules.toml").get("rules", [])
    inst_doc = _load(cdir / "institutions.toml")
    institutions = inst_doc.get("institutions", [])
    for inst in institutions:
        for rel in inst.get("relationships", []):
            if not rel.get("source", "").startswith(("http://", "https://")):
                raise ConfigError(f"Relationship {inst.get('id')} -> {rel.get('to')} has no source URL; "
                                  "the institutional map only accepts evidenced relationships.")
    w = scoring["weights"]
    if abs(sum(w.values()) - 1.0) > 1e-6:
        raise ConfigError("Scoring weights must sum to 1.0")
    for r in rules:
        for k in ("id", "keywords", "activities", "situations", "mechanism", "consequence"):
            if k not in r:
                raise ConfigError(f"Rule {r.get('id', '?')} missing '{k}'")
    ids = [s["id"] for s in src.get("sources", [])]
    if len(ids) != len(set(ids)):
        raise ConfigError("Duplicate source ids")
    return Config(
        sources=src.get("sources", []), rules=rules, institutions=institutions,
        categories=inst_doc.get("categories", []), weights=w, thresholds=scoring["thresholds"],
        retrieval=scoring["retrieval"], tiers=scoring["tiers"], horizon_days=scoring["horizon_days"],
        timing_dir=Path(timing_dir or os.environ.get("INEWS_TIMING_DIR", "data/timing")),
        user_agent=src.get("app", {}).get("user_agent", "inews-personal/0.1"),
    )
