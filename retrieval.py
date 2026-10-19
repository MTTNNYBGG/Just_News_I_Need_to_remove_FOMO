"""User-triggered retrieval of RSS/Atom feeds. Never called except from RefreshService.refresh()."""
import calendar, os, ssl, urllib.request
from dataclasses import dataclass, field
import feedparser
from .db import iso, utcnow

@dataclass
class FetchResult:
    source_id: str
    ok: bool
    entries: list = field(default_factory=list)
    error: str = ""
    warning: str = ""
    http_status: int | None = None

def _ts(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return iso(__import__("datetime").datetime.fromtimestamp(calendar.timegm(t), __import__("datetime").timezone.utc)) if t else None

def parse_feed_bytes(source_id, data, status=200):
    """Turn raw feed bytes into a FetchResult. Parse failures are failures, not 'no news'."""
    d = feedparser.parse(data)
    entries = [{"url": e.get("link"), "title": e.get("title", ""), "summary": e.get("summary", ""), "published": _ts(e)}
               for e in d.entries]
    if d.bozo and not entries:
        return FetchResult(source_id, False, error=f"Unparseable feed: {d.bozo_exception!r}", http_status=status)
    warn = ""
    if d.bozo:
        warn = f"Feed had format problems but {len(entries)} entries were read."
    elif not entries:
        warn = "Feed was reachable but contained zero entries."
    return FetchResult(source_id, True, entries, warning=warn, http_status=status)

def _ssl_context():
    """Certificate verification is always on. INEWS_CA_BUNDLE adds your own CA file (e.g. a corporate root)."""
    bundle = os.environ.get("INEWS_CA_BUNDLE")
    return ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()

def describe_error(ex):
    msg = f"{type(ex).__name__}: {ex}"
    if "CERTIFICATE_VERIFY_FAILED" in msg:
        msg += (" | Hint: Python could not verify this site's certificate chain. Causes: your Python has no CA store"
                " (macOS python.org: run 'Install Certificates.command'), a proxy/VPN/antivirus inspecting HTTPS,"
                " or the site sending an incomplete chain. Verification stays on; set INEWS_CA_BUNDLE to a CA file"
                " that includes the missing certificate, or set enabled = false for this source.")
    return msg

def make_http_fetcher(user_agent, timeout):
    ctx = _ssl_context()
    def fetch(source):
        req = urllib.request.Request(source["url"], headers={"User-Agent": user_agent,
                                     "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                data = r.read(5_000_000)
                return parse_feed_bytes(source["id"], data, r.status)
        except Exception as ex:  # network, HTTP error, TLS, timeout
            code = getattr(ex, "code", None)
            return FetchResult(source["id"], False, error=describe_error(ex), http_status=code)
    return fetch
