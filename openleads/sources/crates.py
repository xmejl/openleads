"""Keyless crates.io package-owner discovery (no automatic contact guessing).

The crates.io API allows at most one request/second and requires an identifying
User-Agent. Every request from this source is serialized, even across Source
instances, and automatic HTTP retries are disabled so hidden retries cannot
violate the API policy. Network failures return partial results.
"""
from __future__ import annotations

import ipaddress
import re
import threading
import time
from typing import Iterator
from urllib.parse import quote, urlsplit

from openleads._http import get_json
from openleads.models import Entity, Query
from openleads.sources.base import Source

_API = "https://crates.io/api/v1"
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "OpenLeads-crates/5.1 (https://github.com/Samyrrrrrr990/openleads)",
}
_SHARED_HOSTS = {
    "github.com", "github.io", "gitlab.com", "crates.io", "docs.rs",
    "readthedocs.io", "pypi.org", "bitbucket.org", "sourceforge.net",
}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_clock = time.monotonic
_sleep = time.sleep
_request_lock = threading.Lock()
_next_start = 0.0


def _get(url: str, cache):
    """Serialize all crates.io requests, including across search instances.

    Unlike OpenLeads' other sources, retries=0 is mandatory here: the shared
    helper otherwise retries some 429/502 responses sooner than policy allows.
    """
    global _next_start
    with _request_lock:
        now = _clock()
        wait = max(0.0, _next_start - now)
        if wait:
            _sleep(wait)
        _next_start = max(_clock(), _next_start) + 1.05
        return get_json(url, headers=_HEADERS, cache=cache, ttl_ns="dataset", retries=0)


def _domain_for_homepage(homepage: str) -> str:
    """A *project* domain, never evidence of a maintainer's personal email."""
    if not isinstance(homepage, str):
        return ""
    try:
        parts = urlsplit(homepage.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return ""
        _ = parts.port  # malformed ports should not pass through
        hostname = parts.hostname.lower().rstrip(".")
        if hostname.startswith("www."):
            hostname = hostname[4:]
        try:
            ipaddress.ip_address(hostname)
            return ""
        except ValueError:
            pass
        if "." not in hostname or hostname == "localhost" or not hostname.isascii():
            return ""
        # Project homepages are untrusted; never feed private or IP-alias hosts
        # to the downstream email/domain enrichment pipeline.
        if any(hostname == suffix or hostname.endswith("." + suffix)
               for suffix in ("localhost", "local", "internal", "lan")):
            return ""
        if all(re.fullmatch(r"(?:0x[0-9a-f]+|\d+)", part)
               for part in hostname.split(".")):
            return ""
        if any(hostname == host or hostname.endswith("." + host) for host in _SHARED_HOSTS):
            return ""
        if not re.fullmatch(r"[a-z0-9.-]+", hostname) or ".." in hostname:
            return ""
        return hostname
    except (TypeError, ValueError):
        return ""


def _parse_owners(crate: dict, response: dict) -> list[Entity]:
    """Pure parser: only published human display names are treated as people."""
    if not isinstance(crate, dict) or not isinstance(response, dict):
        return []
    name = crate.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return []
    users = response.get("users")
    if not isinstance(users, list):
        return []
    homepage = crate.get("homepage") if isinstance(crate.get("homepage"), str) else ""
    repository = crate.get("repository") if isinstance(crate.get("repository"), str) else ""
    website = homepage if homepage.startswith(("https://", "http://")) else repository
    domain = _domain_for_homepage(homepage)
    result = []
    for owner in users:
        if not isinstance(owner, dict):
            continue
        login = owner.get("login")
        display = owner.get("name")
        if not isinstance(login, str) or not login or not isinstance(display, str) or not display.strip():
            continue
        # Cargo teams may appear as github:organization:team; do not call those people.
        if login.count(":") >= 2:
            continue
        result.append(Entity(
            full_name=display.strip(),
            title="Rust crate maintainer",
            organization=name,
            domain=domain,
            website=website,
            links={"crates": f"https://crates.io/crates/{quote(name, safe='')}",
                   "crates_owner": f"https://crates.io/users/{quote(login, safe='')}",
                   "repository": repository},
            extra={"vertical": "Rust crate maintainers", "crate": name,
                   "owner_login": login, "public_email": "",
                   "domain_evidence": "crate_homepage_not_owner_email" if domain else "none"},
            source="crates",
        ))
    return result


class CratesSource(Source):
    name = "crates"
    kind = "people"
    vertical = "Rust package maintainers"
    description = "Published crates.io owners via its public API (polite, keyless)."

    def search(self, query: Query) -> Iterator[Entity]:
        if query.count <= 0:
            return
        topic = (query.keyword or query.industry or query.text or "").strip()
        if not topic or len(topic) > 100:
            return
        # One page, bounded owner lookups, no crawling or unattended pagination.
        max_crates = min(8, max(query.count * 2, 3), max(1, query.max_companies))
        url = f"{_API}/crates?q={quote(topic, safe='')}&per_page={max_crates}"
        payload = _get(url, self.cache)
        if not isinstance(payload, dict) or not isinstance(payload.get("crates"), list):
            return
        seen: set = set()
        emitted = 0
        for crate in payload["crates"][:max_crates]:
            if not isinstance(crate, dict):
                continue
            name = crate.get("name")
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                continue
            owner_url = f"{_API}/crates/{quote(name, safe='')}/owners"
            for entity in _parse_owners(crate, _get(owner_url, self.cache)):
                login = entity.extra["owner_login"].lower()
                if login in seen:
                    continue
                seen.add(login)
                yield entity
                emitted += 1
                if emitted >= query.count:
                    return
