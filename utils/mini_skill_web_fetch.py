from __future__ import annotations

import gzip
import ipaddress
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._pending_space = False

    def handle_data(self, data: str) -> None:
        s = str(data or "")
        if not s:
            return
        if self._pending_space and self._parts and not self._parts[-1].endswith(("\n", " ")):
            self._parts.append(" ")
        self._pending_space = False
        self._parts.append(s)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = (tag or "").lower()
        if t in {"p", "br", "hr", "div", "section", "article", "header", "footer", "li", "ul", "ol"}:
            self._parts.append("\n")
        if t in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append("\n")
        if t == "a":
            self._pending_space = True

    def handle_endtag(self, tag: str) -> None:
        t = (tag or "").lower()
        if t in {"p", "div", "section", "article", "header", "footer", "li"}:
            self._parts.append("\n")
        if t in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append("\n")
        if t in {"script", "style"}:
            self._parts.append("\n")

    def get_text(self) -> str:
        raw = "".join(self._parts)
        raw = re.sub(r"[ \t]+\n", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        raw = re.sub(r"[ \t]{2,}", " ", raw)
        return raw.strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def _is_blocked_hostname(hostname: str) -> bool:
    h = (hostname or "").strip().lower().strip(".")
    if not h:
        return True
    if h in {"localhost", "localhost.localdomain"}:
        return True
    if h.endswith(".local"):
        return True
    return False


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except Exception:
        return False
    if addr.is_loopback or addr.is_link_local or addr.is_multicast:
        return False
    if addr.is_private or addr.is_reserved or addr.is_unspecified:
        return False
    return True


def _resolve_public_ips(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return []
    ips: list[str] = []
    for fam, _, _, _, sockaddr in infos:
        if fam == socket.AF_INET and isinstance(sockaddr, tuple):
            ip = sockaddr[0]
        elif fam == socket.AF_INET6 and isinstance(sockaddr, tuple):
            ip = sockaddr[0]
        else:
            continue
        if ip and _is_public_ip(ip):
            ips.append(ip)
    return list(dict.fromkeys(ips))


def _validate_url(url: str) -> tuple[bool, str]:
    u = str(url or "").strip()
    if not u:
        return False, "missing url"
    parsed = urllib.parse.urlsplit(u)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return False, "only http/https urls are allowed"
    if not parsed.hostname:
        return False, "invalid hostname"
    if parsed.username or parsed.password:
        return False, "credentials in url are not allowed"
    if _is_blocked_hostname(parsed.hostname):
        return False, "blocked hostname"
    ips = _resolve_public_ips(parsed.hostname)
    if not ips:
        return False, "hostname does not resolve to a public ip"
    return True, ""


def web_fetch(
    *,
    url: str,
    extract_mode: str = "markdown",
    max_chars: int = 50000,
    timeout_seconds: int = 25,
    max_redirects: int = 3,
    max_bytes: int = 2_000_000,
) -> dict[str, Any]:
    ok, err = _validate_url(url)
    if not ok:
        return {"error": "web_fetch_denied", "detail": err}

    extract_mode_norm = (extract_mode or "markdown").strip().lower()
    if extract_mode_norm not in {"markdown", "text"}:
        extract_mode_norm = "markdown"
    max_chars = int(max_chars or 0)
    if max_chars < 200:
        max_chars = 200
    if max_chars > 200_000:
        max_chars = 200_000
    timeout_seconds = int(timeout_seconds or 0)
    if timeout_seconds < 5:
        timeout_seconds = 5
    if timeout_seconds > 60:
        timeout_seconds = 60
    max_redirects = int(max_redirects or 0)
    if max_redirects < 0:
        max_redirects = 0
    if max_redirects > 5:
        max_redirects = 5
    max_bytes = int(max_bytes or 0)
    if max_bytes < 50_000:
        max_bytes = 50_000
    if max_bytes > 5_000_000:
        max_bytes = 5_000_000

    current = str(url)
    ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ctx))
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
        "Accept-Encoding": "gzip",
    }

    last_content_type = ""
    last_final_url = ""
    raw: bytes = b""

    for _ in range(max_redirects + 1):
        ok2, err2 = _validate_url(current)
        if not ok2:
            return {"error": "web_fetch_denied", "detail": err2}
        req = urllib.request.Request(current, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=timeout_seconds) as resp:
                last_final_url = str(getattr(resp, "url", "") or current)
                last_content_type = str(resp.headers.get("content-type") or "")
                enc = str(resp.headers.get("content-encoding") or "").lower()
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    data = data[:max_bytes]
                if "gzip" in enc:
                    try:
                        data = gzip.decompress(data)
                    except Exception:
                        pass
                raw = data
                break
        except urllib.error.HTTPError as e:
            if e.code in {301, 302, 303, 307, 308}:
                loc = e.headers.get("location") if e.headers else None
                if not loc:
                    return {"error": "web_fetch_failed", "detail": f"http {e.code} without location"}
                current = urllib.parse.urljoin(current, str(loc))
                continue
            return {"error": "web_fetch_failed", "detail": f"http {e.code}"}
        except urllib.error.URLError as e:
            return {"error": "web_fetch_failed", "detail": str(getattr(e, "reason", "") or str(e))}
        except Exception as e:
            return {"error": "web_fetch_failed", "detail": str(e)}
    else:
        return {"error": "web_fetch_failed", "detail": "too many redirects"}

    ct = (last_content_type or "").lower()
    charset = ""
    m = re.search(r"charset=([A-Za-z0-9._-]+)", ct)
    if m:
        charset = m.group(1)
    if not charset:
        charset = "utf-8"
    try:
        text = raw.decode(charset, errors="ignore")
    except Exception:
        text = raw.decode("utf-8", errors="ignore")

    extracted = text
    extractor = "raw"
    if "text/html" in ct or "<html" in text.lower():
        parser = _HtmlToText()
        try:
            parser.feed(text)
        except Exception:
            pass
        extracted = parser.get_text()
        extractor = "html"

    if extract_mode_norm == "markdown":
        out = extracted
    else:
        out = extracted

    if len(out) > max_chars:
        out = out[:max_chars]

    return {
        "url": url,
        "final_url": last_final_url or url,
        "content_type": last_content_type,
        "extractor": extractor,
        "text": out.strip(),
        "truncated": bool(len(extracted) > max_chars),
    }
