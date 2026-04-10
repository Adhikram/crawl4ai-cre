"""
CRE (Commercial Real Estate) URL Filters for deep crawling.

Ported from: anax/dash/src/lib/utils/fetchwebsite.ts
  - isValidPageUrl()      → CREValidPageFilter
  - isNewsUrl()           → CRENewsFilter
  - isRealEstateRelated() → CRERealEstateRelevanceFilter
  - GlobalUrlTracker.addUrl() domain scoping → CREDomainScopingFilter
  - followRedirectsToFinalDomain()        → CREDomainScopingFilter.create_from_url()
  - GlobalUrlTracker.initializeRedirectTracking() → CREDomainScopingFilter.create_from_url()

These filters are designed to focus deep crawls on CRE investment-criteria pages
while skipping system paths, media files, news/blog content, and off-domain URLs.

Quick start::

    from crawl4ai.deep_crawling.cre_filters import CREDomainScopingFilter, async_build_cre_filter_chain

    # Build a redirect-aware domain filter in one call
    domain_filter = await CREDomainScopingFilter.create_from_url("https://example.com")

    # Or build the full pipeline immediately
    filter_chain = await async_build_cre_filter_chain("https://example.com")
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Literal, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from .filters import URLFilter

if TYPE_CHECKING:
    from ..types import CrawlResult


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
    "/private/", "/secure/", "/protected/", "/blog",
    "/.well-known", "/robots.txt", "/favicon.ico",
    "/sitemap.xml", "/sitemap_index.xml",
])

# File extensions that indicate non-HTML resources (never crawl-worthy as HTML)
_INVALID_EXTENSIONS: FrozenSet[str] = frozenset([
    ".xml", ".zip", ".rar", ".tar", ".gz", ".jpg", ".jpeg", ".png", ".gif",
    ".svg", ".mp4", ".avi", ".mov", ".css", ".js", ".json", ".txt",
    ".rss", ".atom", ".ico", ".woff", ".woff2", ".ttf", ".eot",
])

# Document file extensions whose raw bytes can be extracted to readable text.
# These bypass domain-scoping, news, and irrelevant-pattern filters so the
# deep crawler always fetches them — their content is extracted server-side
# just like PDF tearsheets (CSV/Excel spreadsheets, Word docs, PowerPoint).
_DOC_EXTENSIONS: FrozenSet[str] = frozenset([
    ".pdf",
    ".csv",
    ".xls", ".xlsx",
    ".doc", ".docx",
    ".ppt", ".pptx",
])


def _is_doc_url(url: str) -> bool:
    """Return True if *url* points to an extractable document file."""
    clean = url.lower().split("?")[0].split("#")[0]
    return any(clean.endswith(ext) for ext in _DOC_EXTENSIONS)

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
      * Allow document files (PDF, CSV, XLS/XLSX, DOC/DOCX, PPT/PPTX)
        unconditionally — their bytes are extracted to readable HTML.
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

            # Document files are always valid — PDF tearsheets, CSV/Excel data
            # sheets, Word docs, and PowerPoint decks are all extractable.
            if _is_doc_url(path):
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
        # Document files are never news (tearsheets, data sheets, fund docs)
        if _is_doc_url(url):
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
      * Document files (PDF, CSV, XLS/XLSX, DOC/DOCX, PPT/PPTX) bypass domain
        validation — they are commonly hosted on CDNs or third-party storage.

    Usage::

        filter_ = CREDomainScopingFilter(
            base_domain="example.com",
            extra_domains=["www.example.com", "example-redirect.com"],
        )
    """

    __slots__ = ("_allowed_normalized", "_allow_pdf_bypass", "_allow_doc_bypass")

    def __init__(
        self,
        base_domain: str,
        extra_domains: Optional[List[str]] = None,
        allow_pdf_bypass: bool = True,
        allow_doc_bypass: bool = True,
    ):
        super().__init__(name="CREDomainScopingFilter")
        self._allow_pdf_bypass = allow_pdf_bypass
        self._allow_doc_bypass = allow_doc_bypass

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
        # Document files (PDF, CSV, Excel, Word, PPT) may be on CDNs – bypass
        if (self._allow_pdf_bypass or self._allow_doc_bypass) and _is_doc_url(url):
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

    # ------------------------------------------------------------------
    # Async factory: build a redirect-aware filter from a seed URL
    # ------------------------------------------------------------------

    @classmethod
    async def create_from_url(
        cls,
        base_url: str,
        *,
        allow_pdf_bypass: bool = True,
        allow_doc_bypass: bool = True,
        max_redirects: int = 10,
        timeout: float = 30.0,
        concurrency: int = 4,
    ) -> "CREDomainScopingFilter":
        """
        Async factory that resolves redirect chains for *base_url* and
        returns a :class:`CREDomainScopingFilter` pre-seeded with every
        domain discovered in the redirect chain.

        Ports ``GlobalUrlTracker.initializeRedirectTracking()`` from
        fetchwebsite.ts — resolves www/non-www × http/https variations
        bidirectionally so the filter accepts URLs from any of them.

        Args:
            base_url:         Starting URL to resolve (e.g. ``"https://example.com"``).
            allow_pdf_bypass: Let PDF URLs bypass domain checks (default True).
            allow_doc_bypass: Let CSV/XLS/DOC/PPT URLs bypass domain checks
                              (default True — they often live on CDNs/storage).
            max_redirects:    Max redirect hops per probe (default 10).
            timeout:          Per-request timeout in seconds (default 30).
            concurrency:      Max parallel variation probes (default 4).

        Returns:
            A fully initialised :class:`CREDomainScopingFilter` whose
            ``_allowed_normalized`` set includes every domain found in
            the redirect chain.

        Example::

            filter_ = await CREDomainScopingFilter.create_from_url(
                "https://example.com"
            )
            # Now accepts URLs from example.com AND www.example.com
            assert filter_.apply("https://www.example.com/about")
        """
        from .cre_redirect import discover_all_redirect_domains

        result = await discover_all_redirect_domains(
            base_url,
            max_redirects=max_redirects,
            timeout=timeout,
            concurrency=concurrency,
        )

        # ``result.all_domains`` is already normalised (no www prefix)
        extra = sorted(result.all_domains - {result.final_domain})
        instance = cls(
            base_domain=result.final_domain,
            extra_domains=extra,
            allow_pdf_bypass=allow_pdf_bypass,
            allow_doc_bypass=allow_doc_bypass,
        )
        return instance


# ---------------------------------------------------------------------------
# 5. Utility: build a complete CRE filter chain
# ---------------------------------------------------------------------------

def build_cre_filter_chain(
    base_domain: str,
    extra_domains: Optional[List[str]] = None,
    allow_news: bool = False,
    strict_cre_relevance: bool = False,
    news_threshold: Optional[int] = 10,
):
    """
    Convenience factory that wires up the standard CRE filter pipeline.

    Order:
      1. CREValidPageFilter           – drop binary/system URLs fast
      2. CREDomainScopingFilter       – keep only same-domain URLs
      3. CRENewsThresholdFilter       – skip news after *news_threshold* non-news
                                        pages have been crawled (default 10).
                                        Pass ``news_threshold=None`` for the
                                        binary CRENewsFilter (reject all news).
      4. CRERealEstateRelevanceFilter – keep only CRE-relevant paths
                                        (only when strict_cre_relevance=True)

    Returns a :class:`~crawl4ai.deep_crawling.filters.FilterChain`.

    .. note::
        Use :func:`async_build_cre_filter_chain` if you want automatic
        redirect-chain discovery for the domain-scoping filter.
    """
    from .filters import FilterChain

    filters: list = [
        CREValidPageFilter(),
        CREDomainScopingFilter(base_domain, extra_domains),
    ]

    if not allow_news:
        if news_threshold is not None:
            filters.append(CRENewsThresholdFilter(min_non_news_before_skip=news_threshold))
        else:
            filters.append(CRENewsFilter())

    if strict_cre_relevance:
        filters.append(CRERealEstateRelevanceFilter(strict=True))

    return FilterChain(filters)


async def async_build_cre_filter_chain(
    base_url: str,
    *,
    allow_news: bool = False,
    strict_cre_relevance: bool = False,
    allow_pdf_bypass: bool = True,
    max_redirects: int = 10,
    timeout: float = 30.0,
    concurrency: int = 4,
    news_threshold: Optional[int] = 10,
):
    """
    Async factory that combines redirect discovery with the standard CRE
    filter pipeline.

    Unlike :func:`build_cre_filter_chain` (which accepts a pre-known domain),
    this function:
      1. Resolves *base_url* through its full redirect chain.
      2. Collects all www / non-www / http / https variants.
      3. Builds a :class:`CREDomainScopingFilter` that accepts all of them.
      4. Assembles the complete filter pipeline in the standard order.

    Args:
        base_url:             Seed URL of the site to crawl.
        allow_news:           Keep news / blog URLs (default False).
        strict_cre_relevance: Also filter out non-CRE keyword URLs (default False).
        allow_pdf_bypass:     Let PDF URLs bypass domain checks (default True).
        max_redirects:        Max redirect hops (default 10).
        timeout:              Per-request timeout (default 30 s).
        concurrency:          Max parallel variation probes (default 4).
        news_threshold:       When set, use :class:`CRENewsThresholdFilter`
                              instead of the binary :class:`CRENewsFilter`.
                              News URLs are passed until *news_threshold*
                              non-news pages have been crawled, then skipped.
                              Set to ``None`` to use the binary filter (reject
                              all news from the start), or pass ``allow_news=True``
                              to skip news filtering entirely (default 10).

    Returns:
        A fully initialised :class:`~crawl4ai.deep_crawling.filters.FilterChain`.

    Example::

        from crawl4ai.deep_crawling.cre_filters import async_build_cre_filter_chain

        chain = await async_build_cre_filter_chain("https://example.com")
        assert chain.apply("https://www.example.com/about")    # same-domain, CRE
        assert not chain.apply("https://other.com/page")       # off-domain
    """
    from .filters import FilterChain

    domain_filter = await CREDomainScopingFilter.create_from_url(
        base_url,
        allow_pdf_bypass=allow_pdf_bypass,
        max_redirects=max_redirects,
        timeout=timeout,
        concurrency=concurrency,
    )

    filters: list = [
        CREValidPageFilter(),
        domain_filter,
    ]

    if not allow_news:
        if news_threshold is not None:
            filters.append(CRENewsThresholdFilter(min_non_news_before_skip=news_threshold))
        else:
            filters.append(CRENewsFilter())

    if strict_cre_relevance:
        filters.append(CRERealEstateRelevanceFilter(strict=True))

    return FilterChain(filters)


# ---------------------------------------------------------------------------
# 5. CREIrrelevantPatternFilter — ports irrelevantPatterns from
#    calculatePageRelevance() in fetchwebsite.ts (lines 1201–1229)
# ---------------------------------------------------------------------------

# Module-level compiled patterns for zero per-call overhead.
# Each pattern targets a URL path segment that signals a page with no
# investment-criteria content.  Numeric slugs (/events/123) are captured
# via \d+ — something the string-prefix list in _EXCLUDED_PATHS cannot do.
_IRRELEVANT_PATTERNS: List[re.Pattern] = [
    re.compile(r"/press", re.IGNORECASE),
    re.compile(r"/events/\d+", re.IGNORECASE),
    re.compile(r"/careers/\d+", re.IGNORECASE),
    re.compile(r"/jobs/\d+", re.IGNORECASE),
    re.compile(r"/gallery", re.IGNORECASE),
    re.compile(r"/photos", re.IGNORECASE),
    re.compile(r"/videos", re.IGNORECASE),
    re.compile(r"/media", re.IGNORECASE),
    re.compile(r"/downloads", re.IGNORECASE),
    re.compile(r"/files", re.IGNORECASE),
    re.compile(r"/privacy", re.IGNORECASE),
    re.compile(r"/terms", re.IGNORECASE),
    re.compile(r"/legal", re.IGNORECASE),
    re.compile(r"/cookie", re.IGNORECASE),
    re.compile(r"/disclaimer", re.IGNORECASE),
    re.compile(r"/sitemap", re.IGNORECASE),
    re.compile(r"/rss", re.IGNORECASE),
    re.compile(r"/feed", re.IGNORECASE),
    re.compile(r"/api", re.IGNORECASE),
    re.compile(r"/admin", re.IGNORECASE),
    re.compile(r"/login", re.IGNORECASE),
    re.compile(r"/register", re.IGNORECASE),
    re.compile(r"/signup", re.IGNORECASE),
    re.compile(r"/signin", re.IGNORECASE),
    re.compile(r"/documents", re.IGNORECASE),
    re.compile(r"/news/\d+", re.IGNORECASE),
]


class CREIrrelevantPatternFilter(URLFilter):
    """
    Rejects URLs whose path matches a regex from the irrelevant-pattern list.

    Ports the ``irrelevantPatterns`` array from ``calculatePageRelevance()``
    in fetchwebsite.ts (lines 1201–1229).

    The key advantage over the string-prefix list in ``_EXCLUDED_PATHS`` is
    that these patterns use ``\\d+`` to match **dated / numbered slugs** such
    as ``/events/12345`` or ``/news/67890`` — pages that would slip through
    prefix-only filtering.

    Document files (PDF, CSV, XLS/XLSX, DOC/DOCX, PPT/PPTX) always bypass
    this filter — tearsheets, data sheets, and fund docs are valuable
    regardless of their URL path.

    Args:
        allow_pdf: Let document file URLs bypass the filter (default True).
    """

    __slots__ = ("_allow_pdf",)

    def __init__(self, allow_pdf: bool = True) -> None:
        super().__init__(name="CREIrrelevantPatternFilter")
        self._allow_pdf = allow_pdf

    @staticmethod
    @lru_cache(maxsize=10_000)
    def _is_irrelevant(path: str) -> bool:
        return any(p.search(path) for p in _IRRELEVANT_PATTERNS)

    def apply(self, url: str) -> bool:
        try:
            if self._allow_pdf and _is_doc_url(url):
                self._update_stats(True)
                return True
            path = urlparse(url).path
            result = not self._is_irrelevant(path.lower())
            self._update_stats(result)
            return result
        except Exception:
            self._update_stats(False)
            return False


# ---------------------------------------------------------------------------
# 6. CRENewsThresholdFilter — ports getNextPendingUrl() threshold logic from
#    GlobalUrlTracker in fetchwebsite.ts
#    (MIN_NON_NEWS_PAGES_BEFORE_SKIPPING_NEWS = 10, lines 190–192, 2419–2487)
# ---------------------------------------------------------------------------

class CRENewsThresholdFilter(URLFilter):
    """
    Dynamic news-URL filter that mirrors the adaptive queue logic from
    ``GlobalUrlTracker.getNextPendingUrl()`` in fetchwebsite.ts.

    Behaviour
    ---------
    * While the count of **non-news pages successfully crawled** is below
      *min_non_news_before_skip*, every URL is passed through.
    * Once the threshold is reached, **news URLs are silently dropped** from
      the queue so the crawl can focus entirely on business / IC content.

    Unlike the binary :class:`CRENewsFilter`, this filter self-adjusts: it
    only starts blocking news after enough high-value pages have been found.

    Wiring
    ------
    The crawl strategy must call :meth:`record_crawled` after each successful
    page result so the internal counter stays accurate::

        for result in crawl_results:
            threshold_filter.record_crawled(result.url)

    The strategies (:class:`BFSDeepCrawlStrategy`, :class:`BestFirstCrawlingStrategy`)
    automatically detect this filter in their chain and call ``record_crawled``
    for you when you build them with :func:`async_build_cre_filter_chain`.

    Args:
        min_non_news_before_skip: Non-news page count after which news URLs
                                   are rejected (default 10, matching the JS
                                   constant ``MIN_NON_NEWS_PAGES_BEFORE_SKIPPING_NEWS``).
    """

    __slots__ = ("_threshold", "_non_news_count", "_lock")

    def __init__(self, min_non_news_before_skip: int = 10) -> None:
        super().__init__(name="CRENewsThresholdFilter")
        self._threshold: int = min_non_news_before_skip
        self._non_news_count: int = 0
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Helpers — reuse _NEWS_PATTERNS already defined in this module
    # ------------------------------------------------------------------

    @staticmethod
    def _is_news(url: str) -> bool:
        if _is_doc_url(url):
            return False
        url_lower = url.lower()
        return any(pat in url_lower for pat in _NEWS_PATTERNS)

    # ------------------------------------------------------------------
    # Counter update (called by the crawl strategy after each result)
    # ------------------------------------------------------------------

    def record_crawled(self, url: str) -> None:
        """
        Increment the non-news counter when a non-news URL has been crawled.

        Call this after every successful :class:`~crawl4ai.types.CrawlResult`
        so the threshold comparison stays accurate.
        """
        if not self._is_news(url):
            with self._lock:
                self._non_news_count += 1

    @property
    def non_news_crawled(self) -> int:
        """Current count of non-news pages that have been crawled."""
        with self._lock:
            return self._non_news_count

    # ------------------------------------------------------------------
    # URLFilter.apply implementation
    # ------------------------------------------------------------------

    def apply(self, url: str) -> bool:
        """
        Return True (allow) or False (reject).

        * Below threshold: all URLs pass.
        * At/above threshold: news URLs are rejected; non-news pass.
        """
        with self._lock:
            count = self._non_news_count

        if count < self._threshold:
            # Haven't collected enough non-news pages yet — keep all URLs
            self._update_stats(True)
            return True

        # Threshold reached — drop news, pass everything else
        result = not self._is_news(url)
        self._update_stats(result)
        return result


# ---------------------------------------------------------------------------
# 7. Bot / WAF challenge detection
# ---------------------------------------------------------------------------

# URL path fragments that appear in WAF challenge redirect targets
_CHALLENGE_URL_PATTERNS: FrozenSet[str] = frozenset([
    "sgcaptcha",         # Stackpath Shield
    "/.well-known/captcha",
    "cdn-cgi/challenge", # Cloudflare
    "cdn-cgi/l/chk_",
    "/__cf_chl",
    "/_Incapsula_Resource",  # Imperva / Incapsula
    "/distil_r_captcha",     # Distil Networks
])

# HTTP response-header key/value pairs that definitively identify a challenge
_CHALLENGE_HEADERS: List[tuple] = [
    ("sg-captcha", "challenge"),      # Stackpath Shield
    ("cf-mitigated", "challenge"),    # Cloudflare
    ("x-sucuri-cache", ""),           # Sucuri — presence alone is enough
]

# HTML <title> strings served by WAF challenge pages
_CHALLENGE_TITLES: FrozenSet[str] = frozenset([
    "robot challenge screen",
    "just a moment...",      # Cloudflare
    "access denied",
    "attention required",    # Cloudflare
    "checking your browser", # various
    "one moment, please",    # Cloudflare
    "ddos protection",
    "security check",
    "please wait",
])


def is_bot_challenge_response(result: "CrawlResult") -> bool:
    """
    Return True when *result* is a WAF / bot-protection challenge page rather
    than real site content.

    Detects the following WAF vendors:
      * **Stackpath Shield** — ``sg-captcha: challenge`` header;
        ``redirected_url`` containing ``sgcaptcha`` or ``/.well-known/captcha``;
        or ``sgcaptcha`` found in the raw HTML (covers both the 220-byte
        meta-refresh redirect *and* the full JS challenge page).
      * **Cloudflare** — ``cf-mitigated: challenge`` header;
        ``redirected_url`` containing ``cdn-cgi/challenge`` or ``/__cf_chl``.
      * **Imperva / Incapsula** — redirect URL containing
        ``/_Incapsula_Resource`` or ``/distil_r_captcha``.
      * **Generic** — HTTP 202 + ``x-robots-tag: noindex`` (common WAF
        pattern), or a known challenge ``<title>``.

    This function is intentionally **read-only** — it never mutates *result*.
    Callers (crawl strategies) use it to decide whether to skip link discovery
    and page-count tracking for the offending result.

    Args:
        result: A :class:`~crawl4ai.types.CrawlResult` from any crawl strategy.

    Returns:
        True if the response is a WAF/bot challenge, False otherwise.

    Example::

        if is_bot_challenge_response(result):
            logger.warning("Bot challenge detected on %s — skipping", result.url)
            continue
    """
    headers: dict = result.response_headers or {}
    headers_lower = {k.lower(): v.lower() for k, v in headers.items()}

    # 1. Known challenge response-header key/value pairs
    for key, value in _CHALLENGE_HEADERS:
        header_val = headers_lower.get(key.lower(), "")
        if value == "" and header_val:
            # Presence-only check (e.g. x-sucuri-cache)
            return True
        if value and header_val == value.lower():
            return True

    # 2. Challenge URL patterns in the redirect target
    redirected = (result.redirected_url or "").lower()
    if redirected and any(pat in redirected for pat in _CHALLENGE_URL_PATTERNS):
        return True

    # 3. HTTP 202 + x-robots-tag: noindex  (Stackpath pattern)
    if result.status_code == 202 and "noindex" in headers_lower.get("x-robots-tag", ""):
        return True

    # 4. HTML <title> check (last-resort, cheapest string comparison)
    title = ((result.metadata or {}).get("title") or "").lower().strip()
    if title in _CHALLENGE_TITLES:
        return True

    # 5. HTML body content fingerprint (catches WAFs that complete the JS
    #    redirect back to the original URL before we capture redirected_url,
    #    so checks 1-2 would otherwise miss them).
    #
    #    a) Stackpath Shield — "sgcaptcha" appears in both the tiny 220-byte
    #       meta-refresh redirect AND the full JS challenge page; status is
    #       202 for both.
    #    b) Generic "Checking the site connection security" body text used by
    #       Stackpath Shield and several other CDN WAFs.
    #    c) Cloudflare / generic — "checking your browser" body text; also
    #       "robot-suspicion" image asset served by Stackpath's CDN.
    html_lower = (result.html or "").lower()
    if result.status_code == 202 and (
        "sgcaptcha" in html_lower
        or "checking the site connection security" in html_lower
        or "robot-suspicion" in html_lower
    ):
        return True

    return False


# ---------------------------------------------------------------------------
# 8. CRE keyword constants
#    Ported from anax/dash/src/lib/validation/rules/shared/
# ---------------------------------------------------------------------------

# --- advisor-keywords.ts ---
CRE_ADVISOR_KEYWORDS: List[str] = [
    "advisory", "Registered Investment Advisor", "RIA",
    "fee-only", "fee-based",
    "wealth management", "financial advisory", "financial advisor", "investment advisor",
    "capital marketplace", "capital platform", "investment marketplace", "investment platform",
    "crowdfunding", "capital exchange", "investor portal", "deal marketplace",
    "asset marketplace", "investment gallery", "offering gallery", "listing platform",
    "syndication platform", "sponsorship portal", "secondary market", "distribution platform",
    "M&A advisory", "capital advisor", "valuation advisor",
    "commercial real estate advisory", "cre advisory", "real estate advisory",
    "real estate services firm",
    "commercial brokerage", "cre brokerage", "brokerage firm",
    "real estate brokerage", "boutique brokerage",
    "capital markets", "capital markets advisory", "debt and equity advisory",
    "equity placement firm", "structured finance group", "investment banking",
    "valuation and advisory", "appraisal firm", "commercial appraisal",
    "real estate consulting", "cre consulting", "property tax consulting",
    "investment advisory", "capital raising", "raising capital",
    "equity raising", "debt placement", "arranging financing",
]

# --- investor-keywords.ts ---
CRE_INVESTOR_GROUP1: List[str] = [
    "reit", "real estate investment trust", "cre reit",
    "commercial real estate", "private equity real estate",
    "real estate", "real estate private equity", "real estate investment",
    "real estate development", "real estate fund", "real estate operator",
    "real estate sponsor", "family office real estate", "cre",
    "commerical property", "commerical properties", "commerical assets",
    "income producing property", "multifamily assets", "multifamily properties",
    "office assets", "medical office building",
    "industrial properties", "industrial assets",
    "logistics facility", "logistics center", "distribution center",
    "warehouse assets", "mixed-use project", "self-storage facility",
    "data center property", "life science real estate",
    "single-tenant net lease", "triple net properties", "cold storage facility",
]

CRE_INVESTOR_GROUP2: List[str] = [
    "acquisition of assets", "acquisition of Commerical Real Estate",
    "acquires properties", "acquiring commercial real estate",
    "property disposition", "off-market transactions",
    "invests in real estate", "capital deployment", "allocating capital",
    "equity placement", "debt origination", "joint venture equity",
    "preferred equity investment", "ground-up development",
    "property redevelopment", "asset repositioning",
    "value-add strategy", "core-plus strategy",
    "opportunistic real estate", "adaptive reuse",
]

CRE_INVESTOR_GROUP3: List[str] = [
    "portfolio", "properties", "assets", "real estate",
    "cap rate", "noi", "leasing", "tenant",
]

RRE_INVESTOR_KEYWORDS: List[str] = [
    "single-family", "sfr", "fix-and-flip", "homebuilder", "1-4 unit",
]

# --- lender-keywords.ts ---
CRE_LENDER_KEYWORDS: List[str] = [
    "cre lender", "cre lending", "cre debt", "cre financing", "cre originator",
    "commercial real estate lender", "commercial real estate lending",
    "commercial real estate debt", "commercial lender", "commercial mortgage lender",
    "real estate lender", "real estate lending", "real estate debt fund",
    "real estate credit strategy", "real estate banking",
    "balance sheet lender", "bridge lender", "mezzanine lender",
    "private lender", "direct lender", "cmbs lender", "life company", "debt fund",
]

NON_RECOURSE_LENDER_KEYWORDS: List[str] = [
    "cmbs", "agency", "fannie mae", "freddie mac", "hud",
    "life insurance company", "non-recourse", "nonrecourse", "non recourse",
    "no personal guarantee", "no personal guaranty",
    "limited recourse", "bad boy carve-outs", "bad boy carveouts",
    "standard carve-outs", "standard carveouts",
]

FULL_RECOURSE_KEYWORDS: List[str] = [
    "full recourse", "full-recourse", "fullrecourse",
    "personal guarantee", "personal guaranty",
    "recourse loan", "recourse financing",
    "balance sheet lender", "bank", "commercial bank",
]

# --- service-keywords.ts ---
CRE_SERVICE_KEYWORDS: List[str] = [
    "property management", "facility management",
    "real estate insurance", "title insurance", "escrow",
    "lease administration", "hvac", "heating and cooling",
    "subcontractor", "general contractor", "construction services",
    "mechanical contractor", "plumbing contractor", "electrical contractor",
    "roofing contractor", "installation services", "repair services",
    "maintenance services",
]

# --- tech-keywords.ts ---
PROPTECH_KEYWORDS: List[str] = [
    "proptech", "real estate technology", "contech", "cre tech", "saas",
]

VC_KEYWORDS: List[str] = [
    "venture capital", "vc", "seed stage", "early stage",
    "invests in startups", "portfolio company",
]

# --- strategy-keywords.ts ---
DEVELOPMENT_KEYWORDS: List[str] = [
    "shovel-ready", "shovel ready", "condo development",
    "ground-up development", "ground up development",
    "ground-up", "ground up", "new construction", "development projects",
    "spec development", "pre-development", "entitlement", "land development",
    "construction loans", "development financing", "adaptive reuse",
    "major redevelopment",
]

OPPORTUNISTIC_FOCUS_KEYWORDS: List[str] = [
    "ground-up development", "shovel-ready", "entitlement", "adaptive reuse",
    "distressed", "special situations", "deep value-add",
    "high-conviction", "event-driven", "turnaround", "rescue capital",
]

VALUE_ADD_FOCUS_KEYWORDS: List[str] = [
    "heavy renovation", "minor construction", "repositioning",
    "lease-up strategy", "transitional asset", "operational improvement",
    "growth-focused",
]

CORE_PLUS_FOCUS_KEYWORDS: List[str] = [
    "light renovation", "lease-up", "stabilization", "enhanced core",
    "income + growth", "stable with upside", "yield-plus",
]

CORE_FOCUS_KEYWORDS: List[str] = [
    "fully stabilized", "bondable lease", "trophy asset",
    "institutional core", "prime", "long-term hold",
    "bond-like income", "yield-focused", "low-risk", "core income strategy",
]

# --- capital-position-keywords.ts ---
CAPITAL_POSITION_KEYWORDS: Dict[str, str] = {
    "a-note": "Senior Debt",
    "bridge financing": "Senior Debt",
    "first lien": "Senior Debt",
    "first mortgage": "Senior Debt",
    "general debt": "Senior Debt",
    "secured loan": "Senior Debt",
    "whole loan purchase": "Senior Debt",
    "stretch senior": "Stretch Senior",
    "b-note": "Mezzanine",
    "junior debt": "Mezzanine",
    "mezzanine": "Mezzanine",
    "second lien": "Mezzanine",
    "subordinate debt": "Mezzanine",
    "hybrid capital": "Preferred Equity",
    "non-convertible preferred": "Preferred Equity",
    "participating preferred": "Preferred Equity",
    "preferred": "Preferred Equity",
    "pref equity": "Preferred Equity",
    "co-invest": "Joint Venture (JV)",
    "joint venture": "Joint Venture (JV)",
    "jv": "Joint Venture (JV)",
    "jv equity": "Joint Venture (JV)",
    "minority equity": "Joint Venture (JV)",
    "participating equity": "Joint Venture (JV)",
    "common equity": "Common Equity",
    "general equity": "Common Equity",
    "majority interest": "Common Equity",
    "majority owner": "Common Equity",
    "majority stake": "Common Equity",
    "residual equity": "Common Equity",
    "co-gp": "Co-GP",
    "general partner": "General Partner (GP)",
    "gp": "General Partner (GP)",
    "gp equity": "General Partner (GP)",
    "management interest": "General Partner (GP)",
    "promote": "General Partner (GP)",
    "sponsor": "General Partner (GP)",
    "institutional capital": "Limited Partner (LP)",
    "limited partner": "Limited Partner (LP)",
    "lp": "Limited Partner (LP)",
    "lp equity": "Limited Partner (LP)",
    "non-control investor": "Limited Partner (LP)",
    "passive equity": "Limited Partner (LP)",
    "third party": "Third Party",
}

# --- external-industry-keywords.ts ---
EXTERNAL_INDUSTRY_E1: List[str] = [
    "aerospace", "automotive", "biotech", "chemicals", "consumer goods",
    "cybersecurity", "e-commerce", "edtech", "energy infrastructure",
    "fintech", "hospitals", "media", "medical devices", "mining",
    "oil and gas", "power generation", "renewables", "saas",
    "software", "supply chain", "telecom", "travel tech",
]

EXTERNAL_INDUSTRY_E2: List[str] = [
    "invests in", "acquires", "develops", "portfolio of", "fund focused on",
]


# ---------------------------------------------------------------------------
# 9. CRE Markdown Relevance Scorer
#    Ports calculateEnhancedPageRelevance() from enhanced-page-ranking.ts
#    and calculatePageRelevance() from fetchwebsite.ts
# ---------------------------------------------------------------------------

# --- Static business keyword sets (mirrors ConsolidatedMappingsService defaults) ---

_BK_PROPERTY_TYPES: List[str] = [
    # Core asset classes
    "multifamily", "apartment", "office", "retail", "industrial",
    "hospitality", "hotel", "mixed-use", "self-storage", "data center",
    "life science", "medical office", "healthcare",
    # Net-lease / specialty
    "single-tenant net lease", "net lease", "triple net", "nnn",
    "cold storage", "warehouse", "logistics", "distribution",
    # Residential
    "single-family", "single family rental", "sfr", "build-to-rent", "btr",
    "manufactured housing", "mobile home park", "senior housing",
    # Land / development
    "land", "ground lease",
]

_BK_STRATEGIES: List[str] = [
    "Core", "Core Plus", "Value-Add", "Opportunistic",
]

_BK_CAPITAL_POSITIONS: List[str] = [
    "Senior Debt", "Stretch Senior", "Mezzanine",
    "Preferred Equity", "Common Equity",
    "General Partner (GP)", "Limited Partner (LP)",
    "Joint Venture (JV)", "Co-GP",
]

_BK_LOAN_PROGRAMS: List[str] = [
    "Bridge", "Construction", "Permanent", "CMBS",
    "Agency", "Fannie Mae", "Freddie Mac", "HUD", "FHA",
    "Life Company", "SBA", "USDA",
]

_BK_LOAN_TYPES: List[str] = [
    "Bridge", "Construction", "Rehab/Value-Add", "Permanent",
    "Acquisition", "Refinance", "Mezzanine",
]

_BK_RECOURSE_LOAN: List[str] = [
    "Non-Recourse",
    "Limited Recourse w/ Bad Boy Carve Outs",
    "Full-Recourse",
]

_BK_STRUCTURED_TRANCHES: List[str] = [
    "Whole Loan", "Subordinate", "A-Note", "B-Note",
    "Participation", "Syndication",
]

_BK_US_REGIONS: List[str] = [
    "National", "Southeast", "Northeast", "Midwest",
    "Southwest", "West Coast", "Mountain West", "Sunbelt",
    "Mid-Atlantic", "Pacific Northwest", "Gulf Coast",
    "nyc metro", "south florida", "dc metro", "dallas-fort worth", "dfw",
]

_BK_US_STATES: List[str] = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]

# --- Investment criteria human terms (from enhanced-page-ranking.ts) ---

_IC_UNIVERSAL_TERMS: List[str] = [
    "investment focus", "investment strategy", "investment approach",
    "investment philosophy", "investment criteria", "investment parameters",
    "investment guidelines", "investment preferences", "investment objectives",
    "target investments", "investment thesis", "investment mandate", "investment profile",
    "deal size", "minimum deal size", "maximum deal size", "deal size range",
    "transaction size", "minimum investment", "maximum investment",
    "investment size", "deal size requirements",
    "capital requirements", "minimum capital", "maximum capital", "capital range",
    "geographic focus", "target markets", "target regions", "geographic preferences",
    "market focus", "regional focus", "geographic criteria", "location preferences",
    "target locations", "geographic scope", "market coverage", "regional coverage",
    "property types", "asset types", "property focus", "asset focus",
    "property preferences", "target properties", "property criteria",
    "asset criteria", "property requirements",
    "decision making process", "approval process", "underwriting process",
]

_IC_DEBT_TERMS: List[str] = [
    "loan terms", "loan structure", "financing terms", "loan parameters", "loan criteria",
    "interest rate", "rate structure", "rate environment", "pricing", "loan pricing",
    "loan to value", "ltv", "loan to cost", "ltc", "debt service coverage", "dscr",
    "loan amount", "loan size", "financing amount", "loan capacity", "credit facility",
    "credit line", "revolving credit", "term loan", "bridge financing", "permanent loan",
    "origination fee", "loan fee", "financing fee", "arrangement fee", "commitment fee",
    "exit fee", "prepayment penalty", "yield maintenance", "defeasance", "prepayment",
    "loan costs", "financing costs", "transaction costs", "closing costs",
    "closing timeline", "closing process", "timeline", "processing time", "approval time",
    "underwriting time", "due diligence period", "closing period", "funding timeline",
    "sofr", "libor", "prime rate", "treasury rate", "benchmark rate", "index rate",
    "rate lock", "rate protection", "hedging", "interest rate hedge",
    "recourse", "non-recourse", "personal guarantee", "corporate guarantee",
]

_IC_EQUITY_TERMS: List[str] = [
    "target return", "required return", "minimum return", "return expectations",
    "return targets", "irr", "internal rate of return", "yield", "yield requirements",
    "yield targets", "cash flow", "cash on cash", "equity multiple",
    "return multiple", "profit multiple",
    "hold period", "investment horizon", "holding period", "exit strategy", "disposition",
    "risk tolerance", "risk profile", "risk parameters", "risk criteria", "risk management",
    "leverage", "leverage ratio", "debt capacity", "credit quality", "credit requirements",
    "occupancy", "occupancy requirements", "tenant requirements", "lease requirements",
    "due diligence", "underwriting", "approval process", "decision process",
    "ownership requirement", "ownership percentage", "control requirements",
    "proof of funds", "financial statements", "audited financials", "tax returns",
    "bank statements",
]

# --- Financial data regex patterns (from enhanced-page-ranking.ts) ---

_FDP_DEAL_SIZE: List[re.Pattern] = [
    re.compile(r"\$[\d,]+(?:\.\d+)?[kmb]?\s*(?:million|billion|m|b)?", re.IGNORECASE),
    re.compile(r"minimum\s*(?:deal\s*size|investment)[:\s]*\$?[\d,]+(?:\.\d+)?[kmb]?", re.IGNORECASE),
    re.compile(r"maximum\s*(?:deal\s*size|investment)[:\s]*\$?[\d,]+(?:\.\d+)?[kmb]?", re.IGNORECASE),
    re.compile(r"deal\s*size[:\s]*\$?[\d,]+(?:\.\d+)?[kmb]?\s*[-–]\s*\$?[\d,]+(?:\.\d+)?[kmb]?", re.IGNORECASE),
    re.compile(r"transaction\s*size[:\s]*\$?[\d,]+(?:\.\d+)?[kmb]?", re.IGNORECASE),
    re.compile(r"investment\s*size[:\s]*\$?[\d,]+(?:\.\d+)?[kmb]?", re.IGNORECASE),
]

_FDP_LOAN_TERMS: List[re.Pattern] = [
    re.compile(r"ltv[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"ltc[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"dscr[:\s]*\d+(?:\.\d+)?", re.IGNORECASE),
    re.compile(r"loan\s*to\s*value[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"loan\s*to\s*cost[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"debt\s*service\s*coverage[:\s]*\d+(?:\.\d+)?", re.IGNORECASE),
    re.compile(r"min\s*loan\s*dscr[:\s]*\d+(?:\.\d+)?", re.IGNORECASE),
    re.compile(r"max\s*loan\s*dscr[:\s]*\d+(?:\.\d+)?", re.IGNORECASE),
]

_FDP_INTEREST_RATES: List[re.Pattern] = [
    re.compile(r"sofr[:\s]*[+-]?\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"libor[:\s]*[+-]?\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"prime\s*rate[:\s]*[+-]?\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"treasury\s*rate[:\s]*[+-]?\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"interest\s*rate[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"loan\s*interest\s*rate[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"benchmark\s*rate[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"index\s*rate[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
]

_FDP_RETURNS: List[re.Pattern] = [
    re.compile(r"minimum\s*internal\s*rate\s*of\s*return[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"minimum\s*yield\s*on\s*cost[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"minimum\s*equity\s*multiple[:\s]*\d+(?:\.\d+)?", re.IGNORECASE),
    re.compile(r"target\s*return[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"irr[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"yield[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
    re.compile(r"equity\s*multiple[:\s]*\d+(?:\.\d+)?", re.IGNORECASE),
    re.compile(r"cash\s*on\s*cash[:\s]*\d+(?:\.\d+)?%?", re.IGNORECASE),
]

_FDP_TIMELINES: List[re.Pattern] = [
    re.compile(r"min\s*loan\s*term[:\s]*\d+\s*(?:years?|months?)", re.IGNORECASE),
    re.compile(r"max\s*loan\s*term[:\s]*\d+\s*(?:years?|months?)", re.IGNORECASE),
    re.compile(r"closing\s*time[:\s]*\d+\s*(?:days?|weeks?)", re.IGNORECASE),
    re.compile(r"min\s*hold\s*period[:\s]*\d+\s*(?:years?|months?)", re.IGNORECASE),
    re.compile(r"max\s*hold\s*period[:\s]*\d+\s*(?:years?|months?)", re.IGNORECASE),
    re.compile(r"processing\s*time[:\s]*\d+\s*(?:days?|weeks?)", re.IGNORECASE),
    re.compile(r"approval\s*time[:\s]*\d+\s*(?:days?|weeks?)", re.IGNORECASE),
    re.compile(r"underwriting\s*time[:\s]*\d+\s*(?:days?|weeks?)", re.IGNORECASE),
]

# --- Investment-focused URL patterns ---

_IFU_HIGH: List[re.Pattern] = [
    re.compile(r"investment[_-]?criteria", re.IGNORECASE),
    re.compile(r"deal[_-]?size", re.IGNORECASE),
    re.compile(r"loan[_-]?terms", re.IGNORECASE),
    re.compile(r"property[_-]?types", re.IGNORECASE),
    re.compile(r"capital[_-]?position", re.IGNORECASE),
    re.compile(r"investment[_-]?parameters", re.IGNORECASE),
    re.compile(r"financing[_-]?criteria", re.IGNORECASE),
    re.compile(r"lending[_-]?criteria", re.IGNORECASE),
    re.compile(r"investment[_-]?requirements", re.IGNORECASE),
    re.compile(r"deal[_-]?parameters", re.IGNORECASE),
]

_IFU_MEDIUM: List[re.Pattern] = [
    re.compile(r"investment[_-]?strategy", re.IGNORECASE),
    re.compile(r"investment[_-]?approach", re.IGNORECASE),
    re.compile(r"investment[_-]?philosophy", re.IGNORECASE),
    re.compile(r"target[_-]?markets", re.IGNORECASE),
    re.compile(r"geographic[_-]?focus", re.IGNORECASE),
    re.compile(r"investment[_-]?focus", re.IGNORECASE),
    re.compile(r"investment[_-]?guidelines", re.IGNORECASE),
    re.compile(r"investment[_-]?preferences", re.IGNORECASE),
    re.compile(r"investment[_-]?objectives", re.IGNORECASE),
    re.compile(r"investment[_-]?mandate", re.IGNORECASE),
]

_IFU_LOW: List[re.Pattern] = [
    re.compile(r"investment", re.IGNORECASE),
    re.compile(r"financing", re.IGNORECASE),
    re.compile(r"lending", re.IGNORECASE),
    re.compile(r"capital", re.IGNORECASE),
    re.compile(r"funding", re.IGNORECASE),
    re.compile(r"loans", re.IGNORECASE),
    re.compile(r"debt", re.IGNORECASE),
    re.compile(r"equity", re.IGNORECASE),
]

# --- Investment information density patterns ---

_IID_HEADERS: List[re.Pattern] = [
    re.compile(r"investment\s*criteria", re.IGNORECASE),
    re.compile(r"lending\s*criteria", re.IGNORECASE),
    re.compile(r"financing\s*requirements", re.IGNORECASE),
    re.compile(r"deal\s*parameters", re.IGNORECASE),
    re.compile(r"investment\s*parameters", re.IGNORECASE),
    re.compile(r"target\s*investments", re.IGNORECASE),
    re.compile(r"investment\s*guidelines", re.IGNORECASE),
    re.compile(r"investment\s*preferences", re.IGNORECASE),
    re.compile(r"investment\s*objectives", re.IGNORECASE),
    re.compile(r"investment\s*mandate", re.IGNORECASE),
]

_IID_STRUCTURED: List[re.Pattern] = [
    re.compile(r"•\s*[^•\n]+"),
    re.compile(r"-\s*[^-\n]+"),
    re.compile(r"\d+\.\s*[^\d\n]+"),
    re.compile(r"minimum[:\s]*[^\n]+", re.IGNORECASE),
    re.compile(r"maximum[:\s]*[^\n]+", re.IGNORECASE),
    re.compile(r"requirements[:\s]*[^\n]+", re.IGNORECASE),
    re.compile(r"criteria[:\s]*[^\n]+", re.IGNORECASE),
    re.compile(r"parameters[:\s]*[^\n]+", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CRERelevanceResult:
    """
    Full relevance result for a crawled markdown page.

    Mirrors the ``EnhancedRankingFactorsExtended`` interface returned by
    ``calculatePageRelevance()`` in fetchwebsite.ts.

    ``matched_keywords`` is the flat merged view of all per-category keyword
    hits; ``investment_criteria_breakdown`` and ``business_context_breakdown``
    carry the category-level detail.
    """

    # ── core scores ──────────────────────────────────────────────────────────
    total_score: float = 0.0
    investment_criteria_field_score: float = 0.0
    financial_data_density_score: float = 0.0
    business_context_score: float = 0.0
    investment_focused_url_score: float = 0.0
    investment_information_density_score: float = 0.0
    financial_metrics_detection_score: float = 0.0
    enhanced_depth_penalty_score: float = 0.0

    # ── legacy / compatibility scores ────────────────────────────────────────
    url_structure_score: float = 0.0
    content_quality_score: float = 0.0
    page_type_score: float = 0.0
    geographic_relevance_score: float = 0.0
    url_quality_bonus: float = 0.0

    # ── matched keywords (flat + by-category) ────────────────────────────────
    matched_keywords: Dict[str, List[str]] = field(default_factory=dict)
    """
    Keys correspond to keyword categories::

        {
          "universal_criteria": [...],
          "debt_criteria":      [...],
          "equity_criteria":    [...],
          "capital_positions":  [...],
          "property_types":     [...],
          "strategies":         [...],
          "loan_programs":      [...],
          "loan_types":         [...],
          "recourse_loans":     [...],
          "structured_tranches":[...],
          "investment_url_kw":  [...],
          "geo_regions":        [...],
          "geo_states":         [...],
        }
    """

    # ── detailed breakdowns ──────────────────────────────────────────────────
    investment_criteria_breakdown: Dict[str, Any] = field(default_factory=lambda: {
        "universal_criteria_found": [],
        "debt_criteria_found": [],
        "equity_criteria_found": [],
        "total_criteria_count": 0,
    })

    financial_data_breakdown: Dict[str, int] = field(default_factory=lambda: {
        "deal_size_mentions": 0,
        "loan_term_mentions": 0,
        "interest_rate_mentions": 0,
        "return_mentions": 0,
        "timeline_mentions": 0,
        "total_financial_mentions": 0,
    })

    business_context_breakdown: Dict[str, List[str]] = field(default_factory=lambda: {
        "capital_positions_found": [],
        "property_types_found": [],
        "strategies_found": [],
        "loan_programs_found": [],
        "loan_types_found": [],
        "recourse_loans_found": [],
        "structured_tranches_found": [],
    })

    url_analysis: Dict[str, Any] = field(default_factory=lambda: {
        "depth": 0,
        "is_investment_focused": False,
        "investment_keywords_in_url": [],
        "url_quality_indicators": [],
    })


# ---------------------------------------------------------------------------
# Helper: compile a word-boundary regex for a keyword phrase
# ---------------------------------------------------------------------------

def _kw_regex(phrase: str) -> re.Pattern:
    escaped = re.escape(phrase.lower())
    return re.compile(r"\b" + escaped + r"\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Scorer class
# ---------------------------------------------------------------------------

class CREMarkdownRelevanceScorer:
    """
    Score a crawled page's markdown content for CRE investment-criteria
    relevance and return the matched keywords.

    Ports ``calculateEnhancedPageRelevance()`` (enhanced-page-ranking.ts) and
    the ``calculatePageRelevance()`` wrapper (fetchwebsite.ts).

    Usage::

        result = CREMarkdownRelevanceScorer.score(url, markdown_text)
        print(result.total_score, result.matched_keywords)
    """

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    @classmethod
    def score(cls, url: str, markdown: str) -> CRERelevanceResult:
        """
        Score *url* + *markdown* content for CRE relevance.

        Args:
            url:      The page URL (used for URL-structure scoring).
            markdown: The page's rendered markdown / plain-text content.

        Returns:
            :class:`CRERelevanceResult` with ``total_score`` and per-category
            ``matched_keywords``.
        """
        url_lower = url.lower()
        content_lower = markdown.lower()

        # Check for irrelevant URL patterns first (mirrors calculatePageRelevance).
        # Document files (PDF/CSV/Excel/Word/PPT) are always scored fully.
        is_doc = _is_doc_url(url_lower)
        if not is_doc and any(p.search(url_lower) for p in _IRRELEVANT_PATTERNS):
            return CRERelevanceResult(total_score=-10.0, url_quality_bonus=-10.0)

        result = CRERelevanceResult()

        # 1. Investment criteria field detection (0-100)
        result.investment_criteria_field_score = cls._score_investment_criteria(
            url_lower, content_lower, result
        )

        # 2. Financial data density (0-50)
        result.financial_data_density_score = cls._score_financial_data(
            content_lower, result
        )

        # 3. Business context scoring (0-60)
        result.business_context_score = cls._score_business_context(
            url_lower, content_lower, result
        )

        # 4. Investment-focused URL (0-30)
        result.investment_focused_url_score = cls._score_investment_url(
            url_lower, result
        )

        # 5. Investment information density (0-40)
        result.investment_information_density_score = cls._score_info_density(
            content_lower
        )

        # 6. Financial metrics detection (0-35)
        result.financial_metrics_detection_score = cls._score_financial_metrics(
            content_lower
        )

        # 7. Enhanced depth penalty (-10 to 20)
        result.enhanced_depth_penalty_score = cls._score_depth_penalty(
            url,
            result.investment_criteria_field_score,
            result.financial_data_density_score,
            result.url_analysis,
        )

        # 8. Legacy scores
        result.url_structure_score = cls._score_url_structure(url)
        result.content_quality_score = cls._score_content_quality(markdown, content_lower)
        result.page_type_score = cls._score_page_type(url_lower, content_lower)
        result.geographic_relevance_score = cls._score_geographic(url_lower, content_lower, result)
        result.url_quality_bonus = cls._score_url_quality_bonus(url_lower)

        # 9. Total
        result.total_score = (
            result.investment_criteria_field_score
            + result.financial_data_density_score
            + result.business_context_score
            + result.investment_focused_url_score
            + result.investment_information_density_score
            + result.financial_metrics_detection_score
            + result.enhanced_depth_penalty_score
            + result.url_structure_score
            + result.content_quality_score
            + result.page_type_score
            + result.geographic_relevance_score
            + result.url_quality_bonus
        )

        # Author-page penalty
        if (
            "/author/" in url_lower
            or "author/" in url_lower
            or "/bio/" in url_lower
            or "biography" in url_lower
            or result.page_type_score == 1
        ):
            result.total_score = max(0.0, result.total_score - 50)

        return result

    # ------------------------------------------------------------------
    # 1. Investment criteria field detection (0-100)
    # ------------------------------------------------------------------

    @classmethod
    def _score_investment_criteria(
        cls, url_lower: str, content_lower: str, result: CRERelevanceResult
    ) -> float:
        score = 0.0
        breakdown = result.investment_criteria_breakdown

        def _check_terms(terms: List[str], url_pts: int, content_pts: int, max_content: int) -> List[str]:
            found: List[str] = []
            nonlocal score
            for term in terms:
                rx = _kw_regex(term)
                if rx.search(url_lower):
                    score += url_pts
                    found.append(term)
                hits = len(rx.findall(content_lower))
                if hits:
                    score += min(hits * content_pts, max_content)
                    if term not in found:
                        found.append(term)
            return found

        universal = _check_terms(_IC_UNIVERSAL_TERMS, 8, 4, 12)
        debt = _check_terms(_IC_DEBT_TERMS, 6, 3, 9)
        equity = _check_terms(_IC_EQUITY_TERMS, 6, 3, 9)

        breakdown["universal_criteria_found"] = universal
        breakdown["debt_criteria_found"] = debt
        breakdown["equity_criteria_found"] = equity
        breakdown["total_criteria_count"] = len(universal) + len(debt) + len(equity)

        # Merge into matched_keywords
        result.matched_keywords["universal_criteria"] = universal
        result.matched_keywords["debt_criteria"] = debt
        result.matched_keywords["equity_criteria"] = equity

        return min(score, 100.0)

    # ------------------------------------------------------------------
    # 2. Financial data density (0-50)
    # ------------------------------------------------------------------

    @classmethod
    def _score_financial_data(
        cls, content_lower: str, result: CRERelevanceResult
    ) -> float:
        score = 0.0
        bd = result.financial_data_breakdown

        def _count(patterns: List[re.Pattern]) -> int:
            return sum(len(p.findall(content_lower)) for p in patterns)

        ds = _count(_FDP_DEAL_SIZE);      bd["deal_size_mentions"] = ds;      score += min(ds * 8, 20)
        lt = _count(_FDP_LOAN_TERMS);     bd["loan_term_mentions"] = lt;       score += min(lt * 6, 15)
        ir = _count(_FDP_INTEREST_RATES); bd["interest_rate_mentions"] = ir;   score += min(ir * 5, 10)
        rt = _count(_FDP_RETURNS);        bd["return_mentions"] = rt;          score += min(rt * 4, 8)
        tm = _count(_FDP_TIMELINES);      bd["timeline_mentions"] = tm;        score += min(tm * 3, 6)
        bd["total_financial_mentions"] = ds + lt + ir + rt + tm

        return min(score, 50.0)

    # ------------------------------------------------------------------
    # 3. Business context scoring (0-60)
    # ------------------------------------------------------------------

    @classmethod
    def _score_business_context(
        cls, url_lower: str, content_lower: str, result: CRERelevanceResult
    ) -> float:
        score = 0.0
        kw = result.matched_keywords

        def _match(terms: List[str], pts: int) -> List[str]:
            nonlocal score
            found: List[str] = []
            for term in terms:
                rx = _kw_regex(term)
                if rx.search(content_lower) or rx.search(url_lower):
                    score += pts
                    found.append(term)
            return found

        kw["capital_positions"]   = _match(_BK_CAPITAL_POSITIONS, 8)
        kw["property_types"]      = _match(_BK_PROPERTY_TYPES, 4)
        kw["strategies"]          = _match(_BK_STRATEGIES, 5)
        kw["loan_programs"]       = _match(_BK_LOAN_PROGRAMS, 3)
        kw["loan_types"]          = _match(_BK_LOAN_TYPES, 3)
        kw["recourse_loans"]      = _match(_BK_RECOURSE_LOAN, 2)
        kw["structured_tranches"] = _match(_BK_STRUCTURED_TRANCHES, 2)

        # Mirror into business_context_breakdown for back-compat
        result.business_context_breakdown["capital_positions_found"]   = kw["capital_positions"]
        result.business_context_breakdown["property_types_found"]      = kw["property_types"]
        result.business_context_breakdown["strategies_found"]          = kw["strategies"]
        result.business_context_breakdown["loan_programs_found"]       = kw["loan_programs"]
        result.business_context_breakdown["loan_types_found"]          = kw["loan_types"]
        result.business_context_breakdown["recourse_loans_found"]      = kw["recourse_loans"]
        result.business_context_breakdown["structured_tranches_found"] = kw["structured_tranches"]

        return min(score, 60.0)

    # ------------------------------------------------------------------
    # 4. Investment-focused URL (0-30)
    # ------------------------------------------------------------------

    @classmethod
    def _score_investment_url(
        cls, url_lower: str, result: CRERelevanceResult
    ) -> float:
        score = 0.0
        found: List[str] = []

        for p in _IFU_HIGH:
            m = p.search(url_lower)
            if m:
                score += 8
                found.append(m.group(0))

        for p in _IFU_MEDIUM:
            m = p.search(url_lower)
            if m:
                score += 4
                found.append(m.group(0))

        for p in _IFU_LOW:
            m = p.search(url_lower)
            if m:
                score += 2
                found.append(m.group(0))

        unique_found = list(dict.fromkeys(found))
        result.url_analysis["is_investment_focused"] = score > 0
        result.url_analysis["investment_keywords_in_url"] = unique_found
        result.matched_keywords["investment_url_kw"] = unique_found

        return min(score, 30.0)

    # ------------------------------------------------------------------
    # 5. Investment information density (0-40)
    # ------------------------------------------------------------------

    @staticmethod
    def _score_info_density(content_lower: str) -> float:
        score = 0.0

        headers = sum(len(p.findall(content_lower)) for p in _IID_HEADERS)
        score += min(headers * 5, 20)

        structured = sum(len(p.findall(content_lower)) for p in _IID_STRUCTURED)
        score += min(structured * 2, 20)

        return min(score, 40.0)

    # ------------------------------------------------------------------
    # 6. Financial metrics detection (0-35)
    # ------------------------------------------------------------------

    @staticmethod
    def _score_financial_metrics(content_lower: str) -> float:
        score = 0.0
        weights = {"dealSize": 4, "loanTerms": 3, "interestRates": 3, "returns": 3, "timelines": 2}
        groups = {
            "dealSize":      _FDP_DEAL_SIZE,
            "loanTerms":     _FDP_LOAN_TERMS,
            "interestRates": _FDP_INTEREST_RATES,
            "returns":       _FDP_RETURNS,
            "timelines":     _FDP_TIMELINES,
        }
        for key, patterns in groups.items():
            cat_hits = sum(len(p.findall(content_lower)) for p in patterns)
            score += min(cat_hits * weights.get(key, 2), 7)
        return min(score, 35.0)

    # ------------------------------------------------------------------
    # 7. Enhanced depth penalty (-10 to 20)
    # ------------------------------------------------------------------

    @staticmethod
    def _score_depth_penalty(
        url: str,
        ic_score: float,
        fd_score: float,
        url_analysis: Dict[str, Any],
    ) -> float:
        try:
            from urllib.parse import urlparse as _up
            segments = [s for s in _up(url).path.split("/") if s]
            depth = len(segments)
            url_analysis["depth"] = depth

            if depth == 0:
                score = 20.0
            elif depth == 1:
                score = 18.0
            elif depth == 2:
                score = 15.0
            elif depth == 3:
                score = 10.0
            else:
                score = max(5 - (depth - 3) * 2, -10)

            if ic_score > 20:
                score += 5
            if fd_score > 20:
                score += 3
        except Exception:
            score = 10.0

        return max(min(score, 20.0), -10.0)

    # ------------------------------------------------------------------
    # 8. Legacy scores
    # ------------------------------------------------------------------

    @staticmethod
    def _score_url_structure(url: str) -> float:
        try:
            segments = [s for s in urlparse(url).path.split("/") if s]
            depth = len(segments)
            if depth == 0:
                return 25.0
            elif depth == 1:
                return 20.0
            elif depth == 2:
                return 15.0
            else:
                return 10.0
        except Exception:
            return 5.0

    @staticmethod
    def _score_content_quality(content: str, content_lower: str) -> float:
        author_indicators = [
            "author page", "biography", "about the author",
            "personal background", "education", "career history",
            "professional background", "personal life",
        ]
        if any(ind in content_lower for ind in author_indicators):
            return max(1.0, len(content) / 1000)

        length = len(content)
        if 500 <= length <= 3000:
            return 15.0
        elif 100 <= length < 500:
            return 12.0
        elif length > 5000:
            return 8.0
        elif length >= 50:
            return 5.0
        return 0.0

    @staticmethod
    def _score_page_type(url_lower: str, content_lower: str) -> float:
        author_kw = ["/author/", "author/", "authors/", "/authors/",
                     "biography", "bio/", "/bio/", "profile/", "/profile/"]
        if any(k in url_lower for k in author_kw) or "author page" in content_lower or "biography" in content_lower:
            return 1.0
        if any(url_lower.endswith(f"/{k}") or url_lower.endswith(k) for k in ["home", "index", ""]):
            return 12.0
        for k in ["about", "company", "story", "mission", "vision", "who-we-are"]:
            if k in url_lower:
                return 10.0
        for k in ["investment", "philosophy", "strategy", "approach", "criteria", "portfolio", "funds"]:
            if k in url_lower:
                return 11.0
        for k in ["services", "capabilities", "expertise", "what-we-do"]:
            if k in url_lower:
                return 9.0
        for k in ["contact", "apply", "get-started"]:
            if k in url_lower:
                return 6.0
        for k in ["team", "leadership", "management", "principals", "partners", "executives"]:
            if k in url_lower:
                return 3.0
        return 0.0

    @classmethod
    def _score_geographic(
        cls, url_lower: str, content_lower: str, result: CRERelevanceResult
    ) -> float:
        score = 0.0
        regions_found: List[str] = []
        states_found: List[str] = []

        for region in _BK_US_REGIONS:
            rx = _kw_regex(region)
            if rx.search(url_lower):
                score += 2
                regions_found.append(region)
            elif rx.search(content_lower):
                score += 1
                regions_found.append(region)

        for state in _BK_US_STATES:
            rx = _kw_regex(state)
            if rx.search(url_lower):
                score += 1
                states_found.append(state)
            elif rx.search(content_lower):
                score += 0.5
                states_found.append(state)

        result.matched_keywords["geo_regions"] = list(dict.fromkeys(regions_found))
        result.matched_keywords["geo_states"] = list(dict.fromkeys(states_found))
        return min(score, 15.0)

    @staticmethod
    def _score_url_quality_bonus(url_lower: str) -> float:
        bonus = 0.0
        if re.match(r"^https?://[^/]+/[a-z-]+/?$", url_lower):
            bonus += 3
        all_kw = (
            _BK_PROPERTY_TYPES + _BK_STRATEGIES + _BK_CAPITAL_POSITIONS
            + _BK_LOAN_PROGRAMS + _BK_LOAN_TYPES + _BK_RECOURSE_LOAN
            + _BK_STRUCTURED_TRANCHES
        )
        if any(k and k.lower() in url_lower for k in all_kw):
            bonus += 2
        q = url_lower.split("?", 1)
        if len(q) > 1 and len(q[1].split("&")) > 3:
            bonus -= 3
        if len(url_lower) > 100:
            bonus -= 2
        if re.search(r"/\d+", url_lower):
            bonus -= 1
        return max(min(bonus, 10.0), -5.0)


# ---------------------------------------------------------------------------
# 10. CRE Page Ranking
#     Ports CREPageRankingService.ts — adds the CRE-focused scoring layer
#     on top of CREMarkdownRelevanceScorer (the base enhanced-ranking layer).
# ---------------------------------------------------------------------------

# CRE-specific high-priority keywords (from CREPageRankingService.ts)
_CRE_HP_KEYWORDS: List[str] = [
    # Loan Programs & Products
    "loan program", "lending program", "financing program", "credit facility",
    "bridge loan", "construction loan", "permanent loan", "mezzanine loan",
    "hard money", "private money", "commercial mortgage", "commercial loan",
    "debt financing", "equity financing", "preferred equity", "senior debt",
    "junior debt", "subordinate debt", "first mortgage", "second mortgage",
    # Investment Criteria & Terms
    "investment criteria", "lending criteria", "financing criteria", "loan criteria",
    "deal parameters", "loan parameters", "financing terms", "loan terms",
    "underwriting criteria", "credit criteria", "approval criteria",
    # Property Types
    "multifamily", "multi-family", "office", "retail", "industrial", "warehouse",
    "hospitality", "hotel", "mixed-use", "mixed use", "self-storage", "self storage",
    "senior housing", "student housing", "medical office", "data center",
    "manufacturing", "logistics", "distribution", "flex", "land", "development",
    # Financial Terms
    "loan to value", "ltv", "loan to cost", "ltc", "dscr", "debt service coverage",
    "interest rate", "sofr", "libor", "prime rate", "benchmark rate",
    "origination fee", "exit fee", "prepayment", "yield maintenance",
    "recourse", "non-recourse", "personal guarantee",
    # Investment Strategies
    "value-add", "value add", "opportunistic", "core", "core-plus", "core plus",
    "distressed", "turnaround", "repositioning", "stabilization",
    # Deal Types
    "acquisition", "refinance", "recapitalization", "construction", "renovation",
    "redevelopment", "ground-up", "ground up", "take-out", "takeout",
    # Capital Positions
    "equity", "debt", "mezzanine", "preferred equity", "common equity",
    # Geographic Terms
    "primary market", "secondary market", "tertiary market", "gateway city",
    "sunbelt", "coastal", "urban", "suburban",
]

# CRE-specific high-priority URL patterns
_CRE_HP_URL_PATTERNS: List[re.Pattern] = [
    re.compile(r"loan[_-]?program", re.IGNORECASE),
    re.compile(r"lending[_-]?program", re.IGNORECASE),
    re.compile(r"financing[_-]?program", re.IGNORECASE),
    re.compile(r"investment[_-]?criteria", re.IGNORECASE),
    re.compile(r"lending[_-]?criteria", re.IGNORECASE),
    re.compile(r"financing[_-]?criteria", re.IGNORECASE),
    re.compile(r"loan[_-]?terms", re.IGNORECASE),
    re.compile(r"financing[_-]?terms", re.IGNORECASE),
    re.compile(r"deal[_-]?size", re.IGNORECASE),
    re.compile(r"property[_-]?types", re.IGNORECASE),
    re.compile(r"capital[_-]?position", re.IGNORECASE),
    re.compile(r"transactions", re.IGNORECASE),
    re.compile(r"portfolio", re.IGNORECASE),
    re.compile(r"deals", re.IGNORECASE),
    re.compile(r"investments", re.IGNORECASE),
]

# Non-CRE page indicators
_NON_CRE_TEAM = [
    "/team", "/leadership", "/management", "/principals",
    "/partners", "/executives", "/about/team", "/people",
]
_NON_CRE_AUTHOR  = ["/author", "/authors", "/bio", "/biography", "/profile"]
_NON_CRE_GENERAL = ["/company", "/story", "/mission", "/vision", "/careers", "/jobs", "/contact"]
_NON_CRE_NEWS    = ["/news", "/blog", "/press", "/media", "/insights", "/updates", "/announcements"]

# CRE page-type detection patterns
_PT_LOAN_PROGRAM: List[re.Pattern] = [
    re.compile(r"loan program", re.IGNORECASE),
    re.compile(r"lending program", re.IGNORECASE),
    re.compile(r"financing program", re.IGNORECASE),
    re.compile(r"credit facility", re.IGNORECASE),
    re.compile(r"bridge loan", re.IGNORECASE),
    re.compile(r"construction loan", re.IGNORECASE),
    re.compile(r"permanent loan", re.IGNORECASE),
    re.compile(r"commercial mortgage", re.IGNORECASE),
    re.compile(r"/debt/", re.IGNORECASE),
    re.compile(r"/debt$", re.IGNORECASE),
    re.compile(r"/debt-financing", re.IGNORECASE),
    re.compile(r"/debt-lending", re.IGNORECASE),
]
_PT_INVESTMENT_CRITERIA: List[re.Pattern] = [
    re.compile(r"investment criteria", re.IGNORECASE),
    re.compile(r"lending criteria", re.IGNORECASE),
    re.compile(r"financing criteria", re.IGNORECASE),
    re.compile(r"loan criteria", re.IGNORECASE),
    re.compile(r"deal parameters", re.IGNORECASE),
    re.compile(r"loan parameters", re.IGNORECASE),
    re.compile(r"underwriting criteria", re.IGNORECASE),
]
_PT_FINANCING_TERMS: List[re.Pattern] = [
    re.compile(r"loan terms", re.IGNORECASE),
    re.compile(r"financing terms", re.IGNORECASE),
    re.compile(r"interest rate", re.IGNORECASE),
    re.compile(r"\bltv\b", re.IGNORECASE),
    re.compile(r"\bltc\b", re.IGNORECASE),
    re.compile(r"\bdscr\b", re.IGNORECASE),
    re.compile(r"origination fee", re.IGNORECASE),
    re.compile(r"exit fee", re.IGNORECASE),
    re.compile(r"prepayment", re.IGNORECASE),
]
_PT_TRANSACTIONS: List[re.Pattern] = [
    re.compile(r"transactions", re.IGNORECASE),
    re.compile(r"\bdeals\b", re.IGNORECASE),
    re.compile(r"\bportfolio\b", re.IGNORECASE),
    re.compile(r"case studies", re.IGNORECASE),
    re.compile(r"deal history", re.IGNORECASE),
]
_PT_PROPERTY_FOCUS: List[re.Pattern] = [
    re.compile(r"property types", re.IGNORECASE),
    re.compile(r"asset types", re.IGNORECASE),
    re.compile(r"target properties", re.IGNORECASE),
    re.compile(r"property focus", re.IGNORECASE),
    re.compile(r"multifamily", re.IGNORECASE),
    re.compile(r"\boffice\b", re.IGNORECASE),
    re.compile(r"\bretail\b", re.IGNORECASE),
    re.compile(r"industrial", re.IGNORECASE),
    re.compile(r"hospitality", re.IGNORECASE),
    re.compile(r"/equity/", re.IGNORECASE),
    re.compile(r"/equity$", re.IGNORECASE),
    re.compile(r"/equity-investment", re.IGNORECASE),
    re.compile(r"/equity-fund", re.IGNORECASE),
]

_CRE_PAGE_TYPE_PATTERNS: Dict[str, List[re.Pattern]] = {
    "loan_program":          _PT_LOAN_PROGRAM,
    "investment_criteria":   _PT_INVESTMENT_CRITERIA,
    "financing_terms":       _PT_FINANCING_TERMS,
    "transactions":          _PT_TRANSACTIONS,
    "property_focus":        _PT_PROPERTY_FOCUS,
}

CREPageType = Literal[
    "loan_program", "investment_criteria", "financing_terms",
    "transactions", "property_focus", "general", "team", "author",
]

# CRE property types for property-focus scoring
_CRE_PROPERTY_TYPES: List[str] = [
    "multifamily", "multi-family", "office", "retail", "industrial", "warehouse",
    "hospitality", "hotel", "mixed-use", "mixed use", "self-storage", "self storage",
    "senior housing", "student housing", "medical office", "data center",
]


@dataclass
class CREPageRankingResult:
    """
    Full CRE-focused ranking for a single crawled page.

    Mirrors ``CREPageRankingResult`` from CREPageRankingService.ts.
    Use :func:`rank_page_for_cre` to produce one of these.
    """
    # CRE-specific sub-scores
    cre_keyword_score: float = 0.0          # 0-150
    cre_content_type_score: float = 0.0     # 0-100
    cre_investment_info_score: float = 0.0  # 0-80
    cre_property_focus_score: float = 0.0   # 0-60
    cre_financial_terms_score: float = 0.0  # 0-50
    non_cre_penalty: float = 0.0            # 0 to -100
    total_score: float = 0.0

    # Detected page type
    cre_page_type: CREPageType = "general"

    # Matched CRE keyword strings
    cre_keywords_found: List[str] = field(default_factory=list)
    cre_content_indicators: List[str] = field(default_factory=list)

    # Base enhanced-ranking factors (from CREMarkdownRelevanceScorer)
    base_rank_factors: Optional[CRERelevanceResult] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dict — suitable for storing in ``rank_factors``."""
        base = {}
        if self.base_rank_factors:
            br = self.base_rank_factors
            base = {
                # top-level scores
                "investment_criteria_field_score":     br.investment_criteria_field_score,
                "financial_data_density_score":        br.financial_data_density_score,
                "business_context_score":              br.business_context_score,
                "investment_focused_url_score":        br.investment_focused_url_score,
                "investment_information_density_score": br.investment_information_density_score,
                "financial_metrics_detection_score":   br.financial_metrics_detection_score,
                "enhanced_depth_penalty_score":        br.enhanced_depth_penalty_score,
                "url_structure_score":                 br.url_structure_score,
                "content_quality_score":               br.content_quality_score,
                "page_type_score":                     br.page_type_score,
                "geographic_relevance_score":          br.geographic_relevance_score,
                "url_quality_bonus":                   br.url_quality_bonus,
                "base_total_score":                    br.total_score,
                # detailed breakdowns
                "investment_criteria_breakdown":       br.investment_criteria_breakdown,
                "financial_data_breakdown":            dict(br.financial_data_breakdown),
                "business_context_breakdown":          {
                    k: v for k, v in br.business_context_breakdown.items()
                },
                "url_analysis":                        {
                    k: v for k, v in br.url_analysis.items()
                },
                "matched_keywords":                    {
                    k: v for k, v in br.matched_keywords.items()
                },
            }

        return {
            **base,
            "cre_ranking": {
                "cre_keyword_score":        self.cre_keyword_score,
                "cre_content_type_score":   self.cre_content_type_score,
                "cre_investment_info_score": self.cre_investment_info_score,
                "cre_property_focus_score": self.cre_property_focus_score,
                "cre_financial_terms_score": self.cre_financial_terms_score,
                "non_cre_penalty":          self.non_cre_penalty,
                "cre_page_type":            self.cre_page_type,
                "cre_keywords_found":       self.cre_keywords_found,
                "cre_content_indicators":   self.cre_content_indicators,
            },
            "total_score": self.total_score,
        }


# ---------------------------------------------------------------------------
# CRE ranking sub-scorers
# ---------------------------------------------------------------------------

def _score_cre_keywords(url_lower: str, content_lower: str) -> Tuple[float, List[str]]:
    """
    CRE keyword density score (0-150).
    Mirrors ``calculateCREKeywordScore()`` in CREPageRankingService.ts.
    """
    score = 0.0
    found: Set[str] = set()

    for kw in _CRE_HP_KEYWORDS:
        kw_l = kw.lower()
        rx = _kw_regex(kw_l)
        if rx.search(url_lower):
            score += 10
            found.add(kw)
        hits = len(rx.findall(content_lower))
        if hits:
            score += min(hits * 5, 20)
            found.add(kw)

    # URL-pattern bonus (15 pts each)
    for p in _CRE_HP_URL_PATTERNS:
        if p.search(url_lower):
            score += 15

    return min(score, 150.0), sorted(found)


def _score_cre_content_type(
    url_lower: str,
    content_lower: str,
) -> Tuple[float, CREPageType, List[str]]:
    """
    CRE content-type score (0-100) + page-type label.
    Mirrors ``calculateCREContentTypeScore()`` in CREPageRankingService.ts.
    """
    score = 0.0
    page_type: CREPageType = "general"
    indicators: List[str] = []

    # --- STEP 1: Detect non-CRE overview pages first ---
    is_team_url    = any(ind in url_lower for ind in _NON_CRE_TEAM)
    is_author_url  = any(ind in url_lower for ind in _NON_CRE_AUTHOR)
    is_about_url   = "/about" in url_lower and "/about/team" not in url_lower

    has_cre_kw     = any(kw.lower() in content_lower for kw in _CRE_HP_KEYWORDS)
    has_cre_url_kw = bool(re.search(r"loan|lending|financing|investment|deal|transaction|news|blog", url_lower, re.I))

    is_team_content = not is_team_url and any(
        w in content_lower for w in [
            "team", "members", "people", "executives", "leadership",
            "management", "principals", "partners", "partnerships",
        ]
    )
    is_author_content = not is_author_url and any(
        w in content_lower for w in ["author", "biography", "profile "]
    )

    # Team
    if is_team_url or (is_team_content and not has_cre_url_kw):
        page_type = "team"
        indicators.append("Team page (URL)" if is_team_url else "Team page (content)")
        return 0.0, page_type, indicators

    # Author
    if is_author_url or (is_author_content and not has_cre_url_kw):
        page_type = "author"
        indicators.append("Author page (URL)" if is_author_url else "Author page (content)")
        return 0.0, page_type, indicators

    # About without CRE content
    if is_about_url and not has_cre_url_kw and not has_cre_kw:
        page_type = "general"
        indicators.append("About page (URL) - no CRE content detected")
        return 0.0, page_type, indicators

    # --- STEP 2: Detect CRE page types ---
    url_based_type: Optional[CREPageType] = None
    content_based_type: Optional[CREPageType] = None
    cre_type_score = 0.0

    for type_name, patterns in _CRE_PAGE_TYPE_PATTERNS.items():
        for p in patterns:
            if p.search(url_lower):
                cre_type_score += 30
                if url_based_type is None:
                    url_based_type = type_name  # type: ignore[assignment]
                    page_type = type_name  # type: ignore[assignment]
                indicators.append(f"URL: {type_name}")

    if url_based_type is None:
        for type_name, patterns in _CRE_PAGE_TYPE_PATTERNS.items():
            for p in patterns:
                if p.search(content_lower):
                    cre_type_score += 20
                    if content_based_type is None:
                        content_based_type = type_name  # type: ignore[assignment]
                        page_type = type_name  # type: ignore[assignment]
                    indicators.append(f"Content: {type_name}")

    cre_type_detected = url_based_type is not None or content_based_type is not None

    # News pages
    is_news_url = any(ind in url_lower for ind in _NON_CRE_NEWS)
    is_news_content = not is_news_url and not cre_type_detected and any(
        w in content_lower for w in ["news", "press release", "latest updates", "announcements"]
    )
    if (is_news_url or is_news_content) and not cre_type_detected:
        page_type = "transactions"
        score = 30.0 if is_news_url else 20.0
        indicators.append("News page (URL)" if is_news_url else "News page (content)")
    elif cre_type_detected:
        score = cre_type_score

    return min(score, 100.0), page_type, indicators


def _score_cre_investment_info(content_lower: str) -> float:
    """
    CRE investment-info density (0-80).
    Mirrors ``calculateCREInvestmentInfoScore()`` in CREPageRankingService.ts.
    """
    score = 0.0

    criteria_pats = [
        re.compile(r"investment criteria", re.IGNORECASE),
        re.compile(r"lending criteria",    re.IGNORECASE),
        re.compile(r"financing criteria",  re.IGNORECASE),
        re.compile(r"loan criteria",       re.IGNORECASE),
        re.compile(r"deal parameters",     re.IGNORECASE),
        re.compile(r"loan parameters",     re.IGNORECASE),
    ]
    criteria_hits = sum(len(p.findall(content_lower)) for p in criteria_pats)
    score += min(criteria_hits * 10, 30)

    deal_size_hits = len(re.findall(
        r"\$[\d,]+(?:\.\d+)?[kmb]?\s*(?:million|billion|m|b)?",
        content_lower, re.IGNORECASE,
    ))
    score += min(deal_size_hits * 3, 20)

    # Capital position mentions (from static list)
    for cp in _BK_CAPITAL_POSITIONS:
        if _kw_regex(cp).search(content_lower):
            score += 5

    # Strategy mentions
    for strat in _BK_STRATEGIES:
        if _kw_regex(strat).search(content_lower):
            score += 3

    return min(score, 80.0)


def _score_cre_property_focus(content_lower: str) -> float:
    """
    CRE property-focus score (0-60).
    Mirrors ``calculateCREPropertyFocusScore()`` in CREPageRankingService.ts.
    """
    score = 0.0

    for pt in _CRE_PROPERTY_TYPES:
        hits = len(_kw_regex(pt).findall(content_lower))
        score += min(hits * 4, 12)

    for pt in _BK_PROPERTY_TYPES:
        if _kw_regex(pt).search(content_lower):
            score += 2

    return min(score, 60.0)


def _score_cre_financial_terms(content_lower: str) -> float:
    """
    CRE financial-terms score (0-50).
    Mirrors ``calculateCREFinancialTermsScore()`` in CREPageRankingService.ts.
    """
    score = 0.0

    loan_pats = [
        re.compile(r"ltv[:\s]*\d+",              re.IGNORECASE),
        re.compile(r"loan to value[:\s]*\d+",    re.IGNORECASE),
        re.compile(r"ltc[:\s]*\d+",              re.IGNORECASE),
        re.compile(r"loan to cost[:\s]*\d+",     re.IGNORECASE),
        re.compile(r"dscr[:\s]*\d+",             re.IGNORECASE),
        re.compile(r"debt service coverage[:\s]*\d+", re.IGNORECASE),
    ]
    lt_hits = sum(len(p.findall(content_lower)) for p in loan_pats)
    score += min(lt_hits * 5, 20)

    rate_pats = [
        re.compile(r"sofr[:\s]*[+-]?\d+",        re.IGNORECASE),
        re.compile(r"libor[:\s]*[+-]?\d+",       re.IGNORECASE),
        re.compile(r"prime rate[:\s]*[+-]?\d+",  re.IGNORECASE),
        re.compile(r"interest rate[:\s]*\d+",    re.IGNORECASE),
        re.compile(r"benchmark rate[:\s]*\d+",   re.IGNORECASE),
    ]
    rate_hits = sum(len(p.findall(content_lower)) for p in rate_pats)
    score += min(rate_hits * 4, 15)

    fee_pats = [
        re.compile(r"origination fee", re.IGNORECASE),
        re.compile(r"exit fee",        re.IGNORECASE),
        re.compile(r"prepayment",      re.IGNORECASE),
        re.compile(r"yield maintenance", re.IGNORECASE),
        re.compile(r"\brecourse\b",    re.IGNORECASE),
        re.compile(r"non-recourse",    re.IGNORECASE),
    ]
    fee_hits = sum(len(p.findall(content_lower)) for p in fee_pats)
    score += min(fee_hits * 3, 15)

    return min(score, 50.0)


def _score_non_cre_penalty(
    url_lower: str,
    content_lower: str,
    detected_page_type: CREPageType,
) -> float:
    """
    Non-CRE penalty (0 to -100).
    Mirrors ``calculateNonCREPenalty()`` in CREPageRankingService.ts.
    """
    penalty = 0.0
    has_cre = any(kw.lower() in url_lower or kw.lower() in content_lower for kw in _CRE_HP_KEYWORDS)
    is_about = "/about" in url_lower and "/about/team" not in url_lower

    if any(ind in url_lower for ind in _NON_CRE_TEAM) or detected_page_type == "team":
        if not has_cre:
            penalty -= 80

    if any(ind in url_lower for ind in _NON_CRE_AUTHOR) or detected_page_type == "author":
        if not has_cre:
            penalty -= 100

    if is_about and not has_cre:
        penalty -= 30

    if any(ind in url_lower for ind in _NON_CRE_GENERAL):
        if not has_cre:
            penalty -= 40

    if any(ind in url_lower for ind in _NON_CRE_NEWS):
        if not has_cre:
            penalty -= 20

    return max(penalty, -100.0)


def rank_page_for_cre(
    url: str,
    markdown: str,
    base_result: Optional[CRERelevanceResult] = None,
) -> CREPageRankingResult:
    """
    Full two-layer CRE ranking for a single page.

    Layer 1 (base): ``CREMarkdownRelevanceScorer.score()`` — enhanced
    relevance factors (investment criteria fields, financial data density,
    business context, …).

    Layer 2 (CRE): CRE-specific scoring (keyword density, content-type
    detection, investment-info density, property focus, financial terms,
    non-CRE penalty).

    Combined formula (mirrors fetchwebsite.ts)::

        total = max(0,
            base_total_score * 0.40
            + cre_keyword_score     * 0.25
            + cre_content_type_score * 0.20
            + cre_investment_info_score * 0.15
            + cre_property_focus_score  * 0.10
            + cre_financial_terms_score * 0.10
            + non_cre_penalty
        )

    Args:
        url:         Page URL.
        markdown:    Rendered markdown / plain text content.
        base_result: Pre-computed :class:`CRERelevanceResult`; if *None*,
                     it is computed automatically.

    Returns:
        :class:`CREPageRankingResult` with all sub-scores and breakdowns.
    """
    if base_result is None:
        base_result = CREMarkdownRelevanceScorer.score(url, markdown)

    url_lower     = url.lower()
    content_lower = markdown.lower()

    ranking = CREPageRankingResult(base_rank_factors=base_result)

    ranking.cre_keyword_score, ranking.cre_keywords_found = _score_cre_keywords(
        url_lower, content_lower
    )
    (
        ranking.cre_content_type_score,
        ranking.cre_page_type,
        ranking.cre_content_indicators,
    ) = _score_cre_content_type(url_lower, content_lower)

    ranking.cre_investment_info_score  = _score_cre_investment_info(content_lower)
    ranking.cre_property_focus_score   = _score_cre_property_focus(content_lower)
    ranking.cre_financial_terms_score  = _score_cre_financial_terms(content_lower)
    ranking.non_cre_penalty            = _score_non_cre_penalty(
        url_lower, content_lower, ranking.cre_page_type
    )

    base_weighted = base_result.total_score * 0.40
    cre_weighted  = (
        ranking.cre_keyword_score       * 0.25
        + ranking.cre_content_type_score  * 0.20
        + ranking.cre_investment_info_score * 0.15
        + ranking.cre_property_focus_score  * 0.10
        + ranking.cre_financial_terms_score * 0.10
    )
    ranking.total_score = max(0.0, base_weighted + cre_weighted + ranking.non_cre_penalty)

    return ranking


# ---------------------------------------------------------------------------
# 11. Canonical CRE result projection
#     Used by both the CLI (cli.py) and the API (deploy/docker/api.py) to
#     produce a consistent output shape for every crawled page.
# ---------------------------------------------------------------------------

def _extract_markdown_text_from_result(result: Any) -> str:
    """
    Pull plain-text markdown from a ``CrawlResult`` (or any duck-typed object).

    Handles:
    * ``result._markdown`` being a ``MarkdownGenerationResult`` with ``.raw_markdown``
    * ``result.markdown`` being a ``StringCompatibleMarkdown`` (string subclass)
    * ``result.markdown`` being a plain string or None
    """
    # Prefer the private MarkdownGenerationResult (most complete)
    md_obj = getattr(result, "_markdown", None)
    if md_obj is not None and hasattr(md_obj, "raw_markdown"):
        return md_obj.raw_markdown or ""

    # Fall back to the public .markdown property
    md = getattr(result, "markdown", None)
    if md is None:
        return ""
    if hasattr(md, "raw_markdown"):
        return md.raw_markdown or str(md)
    return str(md)


def _serialise_markdown(result: Any) -> Dict[str, Any]:
    """
    Return a JSON-serialisable markdown dict from a ``CrawlResult``.

    Prefers ``result._markdown.model_dump()`` (full ``MarkdownGenerationResult``
    with citations, fit_markdown, …).  Falls back to a minimal dict with
    ``raw_markdown`` only.
    """
    md_obj = getattr(result, "_markdown", None)
    if md_obj is not None and hasattr(md_obj, "model_dump"):
        return md_obj.model_dump()
    raw = _extract_markdown_text_from_result(result)
    return {"raw_markdown": raw}


def project_cre_result(result: Any, seed_url: str = "") -> Dict[str, Any]:
    """
    Build the canonical CRE result dict for a single ``CrawlResult``.

    This is the single source of truth used by:
    * **API** – ``deploy/docker/api.py`` (sync + streaming endpoints)
    * **CLI** – ``crawl4ai/cli.py`` (``--cre`` flag output)

    Notes
    -----
    * ``metadata`` already contains ``depth`` and ``parent_url`` (injected by
      all three deep-crawl strategies) — those are **not** duplicated as
      separate top-level keys.
    * All scalar / diagnostic fields from ``CrawlResult`` are included so
      callers have the full picture without a second model_dump() call.

    Returned fields
    ---------------
    url                    str
    success                bool
    error_message          str
    status_code            int | None
    redirected_url         str | None
    redirected_status_code int | None
    cache_status           str | None
    response_headers       dict
    ssl_certificate        any
    dispatch_result        any
    network_requests       list | None
    console_messages       list | None
    tables                 list
    crawl_stats            dict | None
    markdown               dict  – {raw_markdown, markdown_with_citations, …}
    media                  dict  – {images: [...], videos: [...], audios: [...]}
    links                  dict  – {internal: [...], external: [...]}
    metadata               dict  – already contains ``depth`` and ``parent_url``
    rank_factors           dict  – full two-layer CRE relevance breakdown

    Args:
        result:   A ``CrawlResult`` instance (or any compatible duck-typed object).
        seed_url: Fallback when ``result.url`` is absent.
    """
    page_url: str = getattr(result, "url", None) or seed_url

    # metadata — deep-crawl strategies inject depth + parent_url here
    raw_metadata: Dict[str, Any] = getattr(result, "metadata", None) or {}

    markdown_text = _extract_markdown_text_from_result(result)
    markdown_dict = _serialise_markdown(result)

    # media + links — may be plain dicts or Pydantic models
    media = getattr(result, "media", None) or {}
    links = getattr(result, "links", None) or {}
    if hasattr(media, "model_dump"):
        media = media.model_dump()
    if hasattr(links, "model_dump"):
        links = links.model_dump()

    # dispatch_result — Pydantic model or plain dict
    dispatch_result = getattr(result, "dispatch_result", None)
    if dispatch_result is not None and hasattr(dispatch_result, "model_dump"):
        dispatch_result = dispatch_result.model_dump()

    # ssl_certificate — Pydantic model or plain value
    ssl_cert = getattr(result, "ssl_certificate", None)
    if ssl_cert is not None and hasattr(ssl_cert, "model_dump"):
        ssl_cert = ssl_cert.model_dump()

    # Compute both CRE scoring layers
    rank_factors: Dict[str, Any] = {}
    try:
        base    = CREMarkdownRelevanceScorer.score(page_url, markdown_text)
        ranking = rank_page_for_cre(page_url, markdown_text, base_result=base)
        rank_factors = ranking.to_dict()
    except Exception:
        pass  # silently omit if scoring fails

    return {
        # ── identity ──────────────────────────────────────────────────────
        "url":                    page_url,
        "success":                getattr(result, "success", False),
        # ── diagnostics ───────────────────────────────────────────────────
        "error_message":          getattr(result, "error_message", "") or "",
        "status_code":            getattr(result, "status_code", None),
        "redirected_url":         getattr(result, "redirected_url", None),
        "redirected_status_code": getattr(result, "redirected_status_code", None),
        "cache_status":           getattr(result, "cache_status", None),
        "response_headers":       getattr(result, "response_headers", None) or {},
        "ssl_certificate":        ssl_cert,
        "dispatch_result":        dispatch_result,
        "network_requests":       getattr(result, "network_requests", None),
        "console_messages":       getattr(result, "console_messages", None),
        "tables":                 getattr(result, "tables", None) or [],
        "crawl_stats":            getattr(result, "crawl_stats", None),
        # ── content ───────────────────────────────────────────────────────
        "markdown":               markdown_dict,
        "media":                  media,
        "links":                  links,
        # metadata already holds depth + parent_url from the crawl strategy
        "metadata":               raw_metadata,
        # ── CRE scoring ───────────────────────────────────────────────────
        "rank_factors":           rank_factors,
    }


async def retry_if_bot_challenge(
    result: "CrawlResult",
    url: str,
    crawler: Any,
    base_config: Any,
    logger: Any = None,
    retry_delays: Sequence[float] = (2.0, 5.0, 10.0, 30.0, 60.0),
) -> "CrawlResult":
    """
    If *result* is a WAF/bot challenge, retry the URL with increasing delays.

    Mirrors the per-page fallback in ``fetchwebsite.ts``:
    first retry after 2 s so the Proof-of-Work JS can complete; then after
    5 s if still challenged.  Each retry reuses the **same** browser context,
    so any WAF session cookie set on a successful pass persists for all
    subsequent pages — no global delay is needed.

    Args:
        result:        The initial :class:`CrawlResult` to inspect.
        url:           The URL that was crawled.
        crawler:       The :class:`AsyncWebCrawler` instance.
        base_config:   The :class:`CrawlerRunConfig` used for the original crawl.
        logger:        Optional logger; warnings are emitted at WARNING level.
        retry_delays:  Sequence of per-retry delays in seconds (default: 2 s, 5 s).

    Returns:
        The first non-challenge result, or the last result when every retry
        is still a challenge (callers should then skip the page).
    """
    for delay in retry_delays:
        if not is_bot_challenge_response(result):
            return result

        if logger:
            logger.warning(
                f"⚠ Bot/WAF challenge on {url} — retrying with delay={delay}s"
            )

        retry_config = base_config.clone(
            deep_crawl_strategy=None,
            stream=False,
            delay_before_return_html=delay,
        )
        try:
            retry_results = await crawler.arun_many(urls=[url], config=retry_config)
            for r in retry_results:
                result = r
                break  # single URL → take the first result
        except Exception as exc:
            if logger:
                logger.warning(f"⚠ Retry request failed for {url}: {exc}")
            return result

    return result
