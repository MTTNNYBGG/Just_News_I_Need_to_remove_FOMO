# Third-party component inventory

Licences below were read from the installed packages' metadata on the build machine (see `requirements*.txt` for pins).
Re-verify after any upgrade, e.g. `pip install pip-licenses && pip-licenses --format=markdown`.
All runtime licences are permissive and can be combined with the project's AGPL-3.0-or-later code.

| Component | Version | Licence | Role |
|---|---|---|---|
| FastAPI | 0.146.0 | MIT | web framework |
| Starlette | 1.9.0 | BSD-3-Clause | ASGI toolkit (via FastAPI) |
| Uvicorn | 0.54.0 | BSD-3-Clause | ASGI server |
| Jinja2 | 3.1.6 | BSD-3-Clause | templates |
| MarkupSafe | 3.0.3 | BSD-3-Clause | templates |
| feedparser | 6.0.14 | BSD-2-Clause | RSS/Atom parsing |
| sgmllib3k | (dependency of feedparser) | **not verified here: metadata unavailable; confirm before release** | feedparser dependency |
| python-multipart | 0.0.32 | Apache-2.0 | form parsing |
| Pydantic / pydantic-core | 2.14.1 / 2.50.1 | MIT | via FastAPI |
| anyio, h11, click, idna, annotated-types, typing-extensions, typing-inspection | see pip | MIT / BSD-3 / PSF-2.0 | transitive |
| SQLite | system | Public domain | storage (Python standard library) |

Development only: pytest 9.1.1 (MIT), httpx 0.28.1 (BSD-3-Clause), httpcore (BSD-3-Clause), certifi (**MPL-2.0**, file-level copyleft; not shipped at runtime, dev/test dependency of httpx).

No proprietary service, API key, telemetry, CDN, web font or third-party script is used. No AI model is used in this release.
