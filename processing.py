"""Normalization, TF-IDF similarity, date/number extraction, and story-level deduplication."""
import hashlib, html, math, re
from collections import Counter
from datetime import date, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from .db import iso, parse_iso

STOP = set("a an the and or of to in on for with by at from as is are was were be been it its this that these those "
           "will would could should may might can has have had not but into over after before about than their his her "
           "they them we our you your says said new".split())
TRACKING = ("utm_", "fbclid", "gclid", "mc_", "ocid", "at_")

def strip_html(t):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(t or ""))).strip()

def canonical_url(u):
    p = urlsplit(u.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(TRACKING)]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", urlencode(q), ""))

def norm_title(t):
    t = re.sub(r"\s[-|]\s[^-|]{2,40}$", "", t.strip())
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()

def tokens(text):
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1 and t not in STOP]

class TfIdf:
    """Plain TF-IDF with cosine similarity. Lexical similarity only; it never asserts causation."""
    def __init__(self, docs):
        self.n = len(docs) or 1
        df = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {t: math.log((1 + self.n) / (1 + c)) + 1 for t, c in df.items()}
        self.default = math.log(1 + self.n) + 1

    def vec(self, toks):
        v = {t: c * self.idf.get(t, self.default) for t, c in Counter(toks).items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    @staticmethod
    def cos(a, b):
        if len(a) > len(b):
            a, b = b, a
        return sum(x * b.get(t, 0.0) for t, x in a.items())

_M = {m: i for i, m in enumerate("january february march april may june july august september october november december".split(), 1)}
_M.update({k[:3]: v for k, v in list(_M.items())})
_MN = "|".join(sorted(_M, key=len, reverse=True))
_D = [re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
      re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MN})\.?,?\s+(\d{{4}})\b", re.I),
      re.compile(rf"\b({_MN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)]

def extract_dates(text):
    out = set()
    for i, rx in enumerate(_D):
        for m in rx.finditer(text):
            try:
                if i == 0: d = date(int(m[1]), int(m[2]), int(m[3]))
                elif i == 1: d = date(int(m[3]), _M[m[2].lower()], int(m[1]))
                else: d = date(int(m[3]), _M[m[1].lower()], int(m[2]))
                out.add(d)
            except ValueError:
                pass
    return sorted(out)

_N = re.compile(r"\d+(?:[.,]\d+)?\s?(?:%|percent|per cent|bn|billion|million|trillion)|[$€£]\s?\d[\d.,]*", re.I)

def numbers(text):
    return {re.sub(r"\s", "", m.group(0).lower()) for m in _N.finditer(text)}

def facts(text):
    return {d.isoformat() for d in extract_dates(text)} | numbers(text)

_ST = [("allegation", re.compile(r"\b(alleg\w+|accus\w+|reportedly|rumou?r\w*|claims? that)\b", re.I)),
       ("forecast", re.compile(r"\b(forecast\w*|projected|predict\w*|expected to|could rise|could fall)\b", re.I)),
       ("announced", re.compile(r"\b(announc\w+|plans? to|propos\w+|set to|will (?:be|introduce|raise|cut|increase)|to introduce)\b", re.I)),
       ("confirmed", re.compile(r"\b(raised|cut|increased|approved|signed|took effect|came into effect|implemented|published|ruled|suspended|closed|reported)\b", re.I))]

def classify_status(text):
    """Heuristic wording-based label; shown in the UI as a heuristic, never as verification."""
    for name, rx in _ST:
        if rx.search(text):
            return name
    return "unclassified"

def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()

def _story_text(conn, story_id):
    return " ".join(f"{r['title']} {r['summary']}" for r in
                    conn.execute("SELECT title,summary FROM articles WHERE story_id=?", (story_id,)))

def ingest(conn, source_id, entries, now, lookback_days, sim_threshold):
    """Insert normalized entries, assigning each to a story. Returns stats incl. dedup rules applied.
    Must run inside the caller's transaction."""
    cutoff = iso(now - timedelta(days=lookback_days + 3))
    window = [dict(r) for r in conn.execute(
        "SELECT * FROM articles WHERE COALESCE(published,first_retrieved)>=?", (cutoff,))]
    prepared = [(e, strip_html(e.get("summary") or "")) for e in entries if e.get("url") and (e.get("title") or "").strip()]
    tf = TfIdf([tokens(w["title"] + " " + w["summary"]) for w in window] +
               [tokens(e["title"] + " " + s) for e, s in prepared])
    for w in window:
        w["_v"] = tf.vec(tokens(w["title"] + " " + w["summary"]))
    res = {"new": 0, "skipped": len(entries) - len(prepared), "merged": [], "material": set()}
    for e, summary in prepared:
        title, canon, pub = e["title"].strip(), canonical_url(e["url"]), e.get("published")
        h = _sha(title + "|" + summary)
        row = conn.execute("SELECT * FROM articles WHERE canon_url=?", (canon,)).fetchone()
        if row:
            if row["content_hash"] != h:
                if facts(title + " " + summary) - facts(row["title"] + " " + row["summary"]):
                    res["material"].add(row["story_id"])
                conn.execute("UPDATE articles SET title=?,summary=?,content_hash=? WHERE id=?", (title, summary, h, row["id"]))
                res["merged"].append({"title": title, "rule": "same_url_changed_content", "of": row["title"]})
            else:
                res["merged"].append({"title": title, "rule": "same_url", "of": row["title"]})
            continue
        vec = tf.vec(tokens(title + " " + summary))
        story, note, of = None, "new_story", None
        nt = norm_title(title)
        for w in window:
            if norm_title(w["title"]) == nt:
                story, note, of = w["story_id"], "same_title", w["title"]; break
        if story is None:
            best, bw = 0.0, None
            for w in window:
                if pub and w["published"] and abs((parse_iso(pub) - parse_iso(w["published"])).days) > 3:
                    continue
                s = TfIdf.cos(vec, w["_v"])
                if s > best:
                    best, bw = s, w
            if bw and best >= sim_threshold:
                story, note, of = bw["story_id"], f"near_duplicate(similarity={best:.2f})", bw["title"]
        new_facts = None
        if story is not None:
            new_facts = facts(title + " " + summary) - facts(_story_text(conn, story))
        cur = conn.execute("INSERT INTO articles(source_id,url,canon_url,title,summary,published,first_retrieved,content_hash,story_id,dedup_note)"
                           " VALUES(?,?,?,?,?,?,?,?,?,?)", (source_id, e["url"], canon, title, summary, pub, iso(now), h, story, note))
        aid = cur.lastrowid
        if story is None:
            story = aid
            conn.execute("UPDATE articles SET story_id=? WHERE id=?", (aid, aid))
        else:
            res["merged"].append({"title": title, "rule": note, "of": of})
            if new_facts:
                res["material"].add(story)
        res["new"] += 1
        window.append({"id": aid, "title": title, "summary": summary, "published": pub, "story_id": story, "_v": vec})
    return res
