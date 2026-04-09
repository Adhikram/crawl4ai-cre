"""
CRE (Commercial Real Estate) URL Filters for deep crawling.

Ported from: anax/dash/src/lib/utils/fetchwebsite.ts
  - isValidPageUrl()      → CREValidPageFilter
  - isNewsUrl()           → CRENewsFilter
  - isRealEstateRelated() → CRERealEstateRelevanceFilter
  - GlobalUrlTracker.addUrl() domain scoping → CREDomainScopingFilter

These filters are designed to focus deep crawls on CRE investment-criteria pages
while skipping system paths, media files, news/blog content, and off-domain URLs.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import FrozenSet, List, Optional, Set
from urllib.parse import urlparse

from .filters import URLFilter


# ---------------------------------------------------------------------------
# Shared constants (mirrors fetchwebsite.ts keyword lists)
# ---------------------------------------------------------------------------

# Paths that are never crawl-worthy (system/admin/media routes)
_EXCLUDED_PATHS: FrozenSet[str] = frozenset([
    "/wp-json", "/wp-admin", "/wp-content", "/wp-includes",
    "/api/", "/graphql", "/rest/", "/v1/", "/v2/", "/v3/",
    "/feed", "/rss", "/atom", "/sitemap",
    "/admin", "/login", "/register", "/signup", "/signin",
    "/dashboard", "/panel", "/control",
    "/cgi-bin", "/bin/", "/lib/", "/tmp/", "/temp/",
    "/config", "/settings", "/setup", "/install",
    "/backup", "/backups", "/cache", "/logs",
    "/test", "/tests", "/debug", "/dev", "/staging",
    "/search", "/search/", "/query", "/query/",
    "/ajax", "/ajax/", "/xhr", "/xhr/",
    "/embed", "/embed/", "/widget", "/widget/",
    "/track", "/track/", "/analytics", "/stats", "/public/",
    "/private/", "/secure/", "/protected/",
    "/.well-known", "/robots.txt", "/favicon.ico",
    "/sitemap.xml", "/sitemap_index.xml",
])

# File extensions that indicate non-HTML resources
_INVALID_EXTENSIONS: FrozenSet[str] = frozenset([
    ".xml", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".tar", ".gz", ".jpg", ".jpeg", ".png", ".gif",
    ".svg", ".mp4", ".avi", ".mov", ".css", ".js", ".json", ".txt",
    ".csv", ".rss", ".atom", ".ico", ".woff", ".woff2", ".ttf", ".eot",
])

# Extensions that render HTML (safe to crawl)
_VALID_EXTENSIONS: FrozenSet[str] = frozenset([
    ".html", ".htm", ".php",
    ".aspx", ".asp",
    ".jsp", ".jspx",
    ".cfm", ".cfml",
    ".shtml", ".shtm",
    ".xhtml",
    ".do", ".action",
])

# Query parameters that indicate API / system usage
_EXCLUDED_QUERY_PARAMS: FrozenSet[str] = frozenset([
    "format=json", "format=xml", "format=rss", "format=atom",
    "callback=", "jsonp=", "action=", "method=",
    "api_key=", "token=", "auth=", "key=",
    "debug=", "test=", "dev=", "staging=",
])

# News / editorial path segments – deprioritised during crawl
_NEWS_PATTERNS: FrozenSet[str] = frozenset([
    "/news/", "/blog/", "/article/", "/press-release/",
    "/press/", "/media/", "/insights/", "/updates/",
    "/thought-leadership/",
])

# Keywords that exclude a URL from "CRE relevant" classification
_EXCLUDE_KEYWORDS: FrozenSet[str] = frozenset([
    "/sitemap", "gallery", "contact", "/posts", "/videos",
    "/media", "/blog", "/news", "/events", "/careers",
    "/jobs", "/privacy", "/terms", "/legal", "/cookie",
    "/disclaimer", "/application", "/applications", "/apply",
])

# Keywords that mark a URL as CRE-relevant
_CRE_KEYWORDS: FrozenSet[str] = frozenset([
    # Company information
    "about", "story", "mission", "vision", "goal", "our-company",
    "company", "firm", "overview", "leadership", "team", "management",
    "principals", "partners", "executives", "founders", "who-we-are",
    "our-team",
    # Services / capabilities
    "capabilities", "services", "expertise", "experience", "history",
    "background", "specialties", "focus-areas", "practice-areas",
    "what-we-do", "our-services",
    # Investment / finance
    "investment", "philosophy", "approach", "strategy", "focus",
    "criteria", "portfolio", "investments", "assets", "properties",
    "fund", "funds", "capital",
    # Real-estate specific
    "real-estate", "realestate", "property", "development",
    "construction", "acquisition", "disposition", "leasing",
    "commercial", "residential", "industrial", "retail", "office",
    "multifamily", "hospitality", "logistics", "warehouse",
    "asset-management", "property-management",
    "real-estate-investment", "commercial-real-estate",
])


# ---------------------------------------------------------------------------
# 1. CREValidPageFilter — ports isValidPageUrl()
# ---------------------------------------------------------------------------

class CREValidPageFilter(URLFilter):
    """
    Rejects URLs that are obviously not crawl-worthy HTML pages.

    Logic mirrors ``isValidPageUrl`` from fetchwebsite.ts:
      * Allow PDF files unconditionally.
      * Reject known non-HTML file extensions.
      * Reject paths that have an extension not in the allowed web-page list.
      * Reject system / admin / API path prefixes.
      * Reject URLs whose query string contains API-style parameters.
    """

    __slots__ = ("_allow_pdf",)

    def __init__(self, allow_pdf: bool = True):
        super().__init__(name="CREValidPageFilter")
        self._allow_pdf = allow_pdf

    @staticmethod
    @lru_cache(maxsize=10_000)
    def _classify(url: str) -> bool:
        try:
            parsed = urlparse(url)
            path = parsed.path.lower()

            # PDFs are always valid (tearsheets, fund overviews, etc.)
            if path.endswith(".pdf"):
                return True

            # Reject known binary / media / data extensions
            if any(path.endswith(ext) for ext in _INVALID_EXTENSIONS):
                return False

            # If there IS an extension, it must be a known web-page extension
            has_extension = "." in path.split("/")[-1]  # dot in last segment
            if has_extension and not any(path.endswith(ext) for ext in _VALID_EXTENSIONS):
                return False

            # Reject system paths
            if any(
                path.startswith(ep) or path == ep.rstrip("/")
                for ep in _EXCLUDED_PATHS
            ):
                return False

            # Reject API-style query params
            query = parsed.query.lower()
            if any(param in query for param in _EXCLUDED_QUERY_PARAMS):
                return False

            return True
        except Exception:
            return False

    def apply(self, url: str) -> bool:
        result = self._classify(url)
        self._update_stats(result)
        return result


# ---------------------------------------------------------------------------
# 2. CRENewsFilter — ports isNewsUrl()
# ---------------------------------------------------------------------------

class CRENewsFilter(URLFilter):
    """
    Rejects (or, when ``deprioritize_only=True``, just marks) news / blog URLs.

    Set ``reverse=True`` to pass ONLY news URLs (useful for dedicated news crawls).
    By default the filter *rejects* news URLs so the crawl focuses on business pages.
    """

    __slots__ = ("_reverse",)

    def __init__(self, reverse: bool = False):
        super().__init__(name="CRENewsFilter")
        self._reverse = reverse

    @staticmethod
    @lru_cache(maxsize=10_000)
    def _is_news(url: str) -> bool:
        # PDFs are never news
        if url.lower().endswith(".pdf"):
            return False
        url_lower = url.lower()
        return any(pattern in url_lower for pattern in _NEWS_PATTERNS)

    def apply(self, url: str) -> bool:
        is_news = self._is_news(url)
        # Default: pass non-news URLs (is_news=False → result=True)
        result = is_news if self._reverse else not is_news
        self._update_stats(result)
        return result


# ---------------------------------------------------------------------------
# 3. CRERealEstateRelevanceFilter — ports isRealEstateRelated()
# ---------------------------------------------------------------------------

class CRERealEstateRelevanceFilter(URLFilter):
    """
    Passes URLs that appear CRE-relevant based on keyword matching.

    Logic mirrors ``isRealEstateRelated`` from fetchwebsite.ts:
      * If the URL contains any exclude keyword → reject.
      * If the URL contains any CRE keyword → accept.
      * Otherwise → reject (strict mode) or accept (permissive mode).

    Args:
        strict:  When True (default), URLs without any CRE keyword are rejected.
                 When False, only explicitly excluded URLs are rejected.
    """

    __slots__ = ("_strict",)

    def __init__(self, strict: bool = True):
        super().__init__(name="CRERealEstateRelevanceFilter")
        self._strict = strict

    @lru_cache(maxsize=10_000)
    def _classify(self, url: str) -> bool:
        url_lower = url.lower()

        # Reject explicitly excluded paths
        for kw in _EXCLUDE_KEYWORDS:
            if kw in url_lower:
                return False

        # Accept if any CRE keyword is present
        for kw in _CRE_KEYWORDS:
            if kw in url_lower:
                return True

        # Strict mode: reject unlabelled URLs; permissive mode: accept them
        return not self._strict

    def apply(self, url: str) -> bool:
        result = self._classify(url)
        self._update_stats(result)
        return result


# ---------------------------------------------------------------------------
# 4. CREDomainScopingFilter — ports GlobalUrlTracker domain validation
# ---------------------------------------------------------------------------

class CREDomainScopingFilter(URLFilter):
    """
    Restricts crawl to URLs on the target domain (and its www / redirect variants).

    Ports the domain-scoping logic from ``GlobalUrlTracker.addUrl()`` in
    fetchwebsite.ts:
      * Normalizes hostnames by stripping the ``www.`` prefix for comparison.
      * Accepts URLs whose normalised hostname matches any domain in
        ``allowed_domains`` (the original domain + redirect-chain domains).
      * PDF files bypass domain validation (they may live on CDNs).

    Usage::

        filter_ = CREDomainScopingFilter(
            base_domain="example.com",
            extra_domains=["www.example.com", "example-redirect.com"],
        )
    """

    __slots__ = ("_allowed_normalized", "_allow_pdf_bypass")

    def __init__(
        self,
        base_domain: str,
        extra_domains: Optional[List[str]] = None,
        allow_pdf_bypass: bool = True,
    ):
        super().__init__(name="CREDomainScopingFilter")
        self._allow_pdf_bypass = allow_pdf_bypass

        domains: List[str] = [base_domain] + (extra_domains or [])
        self._allowed_normalized: FrozenSet[str] = frozenset(
            self._normalize_host(d) for d in domains if d
        )

    @staticmethod
    def _normalize_host(hostname: str) -> str:
        """Strip www. prefix and lowercase – mirrors normalizeHostname() in TS."""
        return re.sub(r"^www\.", "", hostname.lower())

    @lru_cache(maxsize=10_000)
    def _extract_normalized_host(self, url: str) -> str:
        try:
            return self._normalize_host(urlparse(url).hostname or "")
        except Exception:
            return ""

    def apply(self, url: str) -> bool:
        # PDFs may be hosted on CDNs – bypass domain check
        if self._allow_pdf_bypass and url.lower().endswith(".pdf"):
            self._update_stats(True)
            return True

        host = self._extract_normalized_host(url)
        result = host in self._allowed_normalized
        self._update_stats(result)
        return result

    def add_domain(self, domain: str) -> None:
        """Dynamically extend the allowed-domain set (e.g. after following a redirect)."""
        self._allowed_normalized = self._allowed_normalized | {self._normalize_host(domain)}
        # Clear LRU cache so new domains are picked up
        self._extract_normalized_host.cache_clear()


# ---------------------------------------------------------------------------
# 5. Utility: build a complete CRE filter chain
# ---------------------------------------------------------------------------

def build_cre_filter_chain(
    base_domain: str,
    extra_domains: Optional[List[str]] = None,
    allow_news: bool = False,
    strict_cre_relevance: bool = False,
):
    """
    Convenience factory that wires up the standard CRE filter pipeline.

    Order:
      1. CREValidPageFilter       – drop binary/system URLs fast
      2. CREDomainScopingFilter   – keep only same-domain URLs
      3. CRENewsFilter            – drop news URLs (unless allow_news=True)
      4. CRERealEstateRelevanceFilter – keep only CRE-relevant paths
                                       (only when strict_cre_relevance=True)

    Returns a :class:`~crawl4ai.deep_crawling.filters.FilterChain`.
    """
    from .filters import FilterChain

    filters: list = [
        CREValidPageFilter(),
        CREDomainScopingFilter(base_domain, extra_domains),
    ]

    if not allow_news:
        filters.append(CRENewsFilter())

    if strict_cre_relevance:
        filters.append(CRERealEstateRelevanceFilter(strict=True))

    return FilterChain(filters)
