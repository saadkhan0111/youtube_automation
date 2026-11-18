"""Fetch user-provided reference pages and extract readable text (no external dependencies)."""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

MAX_BYTES = 2_000_000
MAX_CHARS = 15_000
UA = "Mozilla/5.0 (compatible; InfoVideoStudio/1.0; research of user-provided references)"

# Tests can replace: fn(url) -> (status, content_type, body_bytes)
http_get_hook = None


@dataclass
class FetchedPage:
    url: str
    ok: bool
    title: str | None = None
    text: str = ""
    error: str | None = None


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "div", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def normalize_url(url: str) -> str:
    """Comparable form: lowercase host, no tracking parameters, no fragment, no trailing slash."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
    path = p.path.rstrip("/") or ""
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return urlunparse((p.scheme.lower() or "https", host, path, "", urlencode(query), ""))


def _get(url: str) -> tuple[int, str, bytes]:
    if http_get_hook is not None:
        return http_get_hook(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read(MAX_BYTES)


def fetch(url: str) -> FetchedPage:
    try:
        status, ctype, body = _get(url)
    except urllib.error.HTTPError as exc:
        return FetchedPage(url, False, error=f"HTTP {exc.code}")
    except Exception as exc:
        return FetchedPage(url, False, error=f"{exc.__class__.__name__}: {str(exc)[:120]}")
    if status >= 400:
        return FetchedPage(url, False, error=f"HTTP {status}")
    charset = re.search(r"charset=([\w-]+)", ctype or "")
    html = body.decode(charset.group(1) if charset else "utf-8", errors="replace")
    if "html" not in (ctype or "html").lower():
        text = html
        title = None
    else:
        ex = _TextExtractor()
        try:
            ex.feed(html)
        except Exception:
            pass
        text = "".join(ex.parts)
        title = re.sub(r"\s+", " ", ex.title).strip() or None
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if len(text) < 200:
        return FetchedPage(url, False, title=title, text=text,
                           error="Page has almost no readable text (it may need JavaScript or block automated access).")
    return FetchedPage(url, True, title=title, text=text[:MAX_CHARS])
