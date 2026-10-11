"""URL normalization, page keys, and filesystem-safe slugs."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

LOCAL_SCHEME = "local://"
MAX_SLUG_LENGTH = 120
DEFAULT_PORTS = {"http": 80, "https": 443}
WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def normalize_url(url: str) -> str:
    """Canonical page key: lowercase scheme/host, no fragment or default port,
    no trailing slash (except root), sorted query parameters."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port and parts.port != DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, host, path, query, ""))


def local_key(filename: str) -> str:
    return f"{LOCAL_SCHEME}{filename}"


def is_local_key(key: str) -> bool:
    return key.startswith(LOCAL_SCHEME)


def site_id_from_url(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    host = host.removeprefix("www.")
    return re.sub(r"[^a-z0-9]+", "-", host).strip("-") or "site"


def short_hash(value: str, length: int = 8) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def _clean_segment(segment: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", segment).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9_]+", "-", ascii_text.lower()).strip("-")


def base_slug(key: str) -> str:
    """Slug derived from the URL path (or local filename stem), before collision handling."""
    if is_local_key(key):
        name = key[len(LOCAL_SCHEME):]
        segments = [name.rsplit(".", 1)[0]]
    else:
        segments = [unquote(s) for s in urlsplit(key).path.split("/") if s]

    cleaned = [c for c in (_clean_segment(s) for s in segments) if c]
    if not cleaned:
        slug = "index" if not segments else f"page-{short_hash(key)}"
    else:
        slug = "--".join(cleaned)

    if len(slug) > MAX_SLUG_LENGTH:
        slug = slug[:MAX_SLUG_LENGTH].rstrip("-") + "-" + short_hash(key)
    if slug in WINDOWS_RESERVED:
        slug = f"{slug}-page"
    return slug


def assign_slug(key: str, taken: set[str]) -> str:
    """Unique slug for `key`; adds a short hash of the key only on collision."""
    slug = base_slug(key)
    if slug in taken:
        slug = f"{slug}-{short_hash(key)}"
    return slug
