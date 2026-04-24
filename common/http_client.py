"""Shared HTTP helper for all *-updater.py scripts.

All updaters need to fetch URLs (Redfin TSV, BLS JSON, Zillow CSV, USDA PDFs, etc.).
Previously each did a bare ``urllib.request.urlopen(req)`` with no timeout or retry —
one flaky CDN response could hang CI or produce a silent stale-data run.

Usage::

    from src.http_client import fetch_bytes, fetch_text

    raw = fetch_bytes("https://example.com/data.csv")
    text = fetch_text("https://example.com/page.html")
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any

_DEFAULT_UA      = "Mozilla/5.0 (compatible; Hawaii-Affordability-Tracker/1.0)"
_DEFAULT_TIMEOUT = 45    # seconds — generous for large Redfin TSV and DBEDT XLSX
_DEFAULT_RETRIES = 2     # total extra attempts after the first failure


def fetch_bytes(
    url: str,
    *,
    headers:  dict[str, str] | None = None,
    data:     bytes | None = None,
    timeout:  int = _DEFAULT_TIMEOUT,
    retries:  int = _DEFAULT_RETRIES,
    backoff:  float = 2.0,
    ssl_ctx:  Any = None,
) -> bytes:
    """Fetch *url* and return the raw response body as bytes.

    Parameters
    ----------
    url:      Target URL.
    headers:  Extra request headers.  ``User-Agent`` is set to a sensible
              default if not provided.
    data:     If given, the request is a POST with this body.
    timeout:  Per-attempt socket timeout in seconds (default 45).
    retries:  Number of *extra* attempts after the first failure (default 2).
              Total attempts = retries + 1.
    backoff:  Multiplicative back-off factor between attempts (default 2×).
    ssl_ctx:  Optional SSL context (e.g. to disable cert verification for
              internal mirrors).  Passed directly to ``urlopen``.

    Raises
    ------
    urllib.error.URLError / OSError on final failure (after all retries).
    """
    req_headers = {"User-Agent": _DEFAULT_UA}
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=req_headers)

    last_exc: Exception = RuntimeError("no attempts made")
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            kwargs: dict[str, Any] = {"timeout": timeout}
            if ssl_ctx is not None:
                kwargs["context"] = ssl_ctx
            with urllib.request.urlopen(req, **kwargs) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
            if attempt < retries:
                print(f"    [http] {url[:80]}… attempt {attempt+1} failed: {exc}. "
                      f"Retrying in {delay:.0f}s…")
                time.sleep(delay)
                delay *= backoff
    raise last_exc


def fetch_text(
    url: str,
    *,
    encoding: str = "utf-8",
    **kwargs: Any,
) -> str:
    """Convenience wrapper around :func:`fetch_bytes` that decodes the response."""
    return fetch_bytes(url, **kwargs).decode(encoding, errors="replace")
