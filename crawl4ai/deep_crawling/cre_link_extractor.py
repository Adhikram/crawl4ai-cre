"""
CRE multi-source link extractor for deep crawling.

Ports the extended link-discovery logic from:
  anax/dash/src/lib/utils/fetchwebsite.ts  —  extractSameDomainLinks() lines 1760–1844

crawl4ai's built-in parser only follows <a href> anchors.  Many CRE sites are
Next.js / React SPAs that surface navigation targets through data attributes
(data-href, data-url, …) or inline JavaScript (router.push(), location.href, …).
This extractor supplements the standard link list with those additional sources.

Usage::

    from crawl4ai.deep_crawling.cre_link_extractor import CRELinkExtractor

    extractor = CRELinkExtractor()
    extra_links = extractor.extract(result.html, source_url, allowed_domains)
    # Returns [{"href": "https://example.com/about", "text": "", "type": "link"}, ...]
"""

from __future__ import annotations

import re
from typing import Dict, FrozenSet, List, Set
from urllib.parse import urljoin, urlparse, urlunparse


# ---------------------------------------------------------------------------
# Compiled regex patterns — mirrors jsUrlPatterns in fetchwebsite.ts L1802-1808
# ---------------------------------------------------------------------------

# Matches data-attribute names we want to harvest
_DATA_ATTR_RE = re.compile(
    r'\bdata-(?:href|url|link|navigation|route|path)\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)

# JavaScript patterns that encode navigation targets
_JS_URL_PATTERNS: List[re.Pattern] = [
    # href=, url:, link:, path:, route:, navigation: "..."
    re.compile(
        r'(?:href|url|link|path|route|navigation)\s*[:=]\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    # router.push("/path") / router.navigate("/path") / router.go("/path")
    re.compile(
        r'router\.(?:push|navigate|go)\(["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    # window.location = "/path"
    re.compile(
        r'window\.location\s*=\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    # location.href = "/path"
    re.compile(
        r'location\.href\s*=\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    # HTML <a href="..."> strings embedded inside JavaScript
    re.compile(
        r'<a[^>]+href=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
]

# Schemes / prefixes that should never be treated as page URLs
_SKIP_PREFIXES: FrozenSet[str] = frozenset([
    "#", "javascript:", "mailto:", "tel:", "data:", "blob:", "void(",
])


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _normalize_host(hostname: str) -> str:
    """Strip www. prefix and lowercase — mirrors normalizeHostname() in TS."""
    return re.sub(r"^www\.", "", hostname.strip().lower())


def _resolve(href: str, base_url: str) -> str | None:
    """
    Resolve *href* against *base_url*.  Returns None if the result is not an
    http/https URL or if parsing fails.
    """
    if not href:
        return None
    # Quick skip for non-navigable schemes/values
    href_stripped = href.strip()
    for prefix in _SKIP_PREFIXES:
        if href_stripped.lower().startswith(prefix):
            return None

    if not href_stripped.startswith("http"):
        try:
            href_stripped = urljoin(base_url, href_stripped)
        except Exception:
            return None

    try:
        parsed = urlparse(href_stripped)
    except Exception:
        return None

    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None

    # Normalize: drop fragment, keep query, ensure path starts with /
    normalized = urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path or "/",
        "",               # params (rarely used)
        parsed.query,
        "",               # fragment — always strip
    ))
    return normalized


def _is_allowed(url: str, allowed_domains: set[str]) -> bool:
    """
    Return True if the URL's hostname (after www. strip) is in *allowed_domains*.
    PDFs bypass domain validation (may be hosted on CDNs).
    """
    if url.lower().endswith(".pdf"):
        return True
    try:
        host = _normalize_host(urlparse(url).hostname or "")
        return host in allowed_domains
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CRELinkExtractor
# ---------------------------------------------------------------------------

class CRELinkExtractor:
    """
    Extracts navigation URLs from HTML that crawl4ai's default <a href> parser
    would miss — specifically data attributes and inline JavaScript patterns.

    Ports ``extractSameDomainLinks`` (lines 1760–1844) from fetchwebsite.ts.

    Args:
        include_data_attrs:  Scan data-href / data-url / … attributes (default True).
        include_js_patterns: Scan inline <script> and JS strings (default True).

    Example::

        extractor = CRELinkExtractor()
        extra = extractor.extract(result.html, source_url, allowed_domains)
        # Returns list of {"href": str, "text": str, "type": str} dicts
        # (same shape as result.links["internal"] entries)
    """

    __slots__ = ("_include_data_attrs", "_include_js_patterns")

    def __init__(
        self,
        include_data_attrs: bool = True,
        include_js_patterns: bool = True,
    ) -> None:
        self._include_data_attrs = include_data_attrs
        self._include_js_patterns = include_js_patterns

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        html: str,
        base_url: str,
        allowed_domains: set[str],
    ) -> List[Dict[str, str]]:
        """
        Scan *html* for additional internal links beyond <a href>.

        Args:
            html:            Raw HTML source of the page.
            base_url:        Absolute URL of the page being scanned (used to
                             resolve relative hrefs).
            allowed_domains: Set of normalized hostnames (no www prefix,
                             lowercase) that are considered "internal".  Obtain
                             from ``CREDomainScopingFilter._allowed_normalized``.

        Returns:
            List of link dicts ``{"href": str, "text": str, "type": str}``
            with only new (non-duplicate) internal URLs.  The list may be
            empty if no additional links are found.
        """
        if not html:
            return []

        seen: Set[str] = set()
        results: List[Dict[str, str]] = []

        if self._include_data_attrs:
            self._extract_data_attrs(html, base_url, allowed_domains, seen, results)

        if self._include_js_patterns:
            self._extract_js_patterns(html, base_url, allowed_domains, seen, results)

        return results

    # ------------------------------------------------------------------
    # Internal extraction methods
    # ------------------------------------------------------------------

    def _extract_data_attrs(
        self,
        html: str,
        base_url: str,
        allowed_domains: set[str],
        seen: Set[str],
        results: List[Dict[str, str]],
    ) -> None:
        """
        Harvest data-href / data-url / data-link / data-navigation /
        data-route / data-path attribute values.

        Mirrors the $('[data-href], [data-url], …').each() block in
        extractSameDomainLinks (L1762–1798).
        """
        for match in _DATA_ATTR_RE.finditer(html):
            href = match.group(1).strip()
            url = _resolve(href, base_url)
            if url and url not in seen and _is_allowed(url, allowed_domains):
                seen.add(url)
                results.append({"href": url, "text": "", "type": "link"})

    def _extract_js_patterns(
        self,
        html: str,
        base_url: str,
        allowed_domains: set[str],
        seen: Set[str],
        results: List[Dict[str, str]],
    ) -> None:
        """
        Apply JavaScript navigation regex patterns over the full HTML source
        (including inline <script> blocks).

        Mirrors the jsUrlPatterns loop in extractSameDomainLinks (L1802–1844).
        """
        for pattern in _JS_URL_PATTERNS:
            for match in pattern.finditer(html):
                href = match.group(1).strip()
                url = _resolve(href, base_url)
                if url and url not in seen and _is_allowed(url, allowed_domains):
                    seen.add(url)
                    results.append({"href": url, "text": "", "type": "link"})
