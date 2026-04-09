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
from functools import lru_cache
from typing import TYPE_CHECKING, Any, FrozenSet, List, Optional, Sequence, Set, Tuple
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

# File extensions that indicate non-HTML resources
_INVALID_EXTENSIONS: FrozenSet[str] = frozenset([
    ".xml", ".zip", ".rar", ".tar", ".gz", ".jpg", ".jpeg", ".png", ".gif",
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

    # ------------------------------------------------------------------
    # Async factory: build a redirect-aware filter from a seed URL
    # ------------------------------------------------------------------

    @classmethod
    async def create_from_url(
        cls,
        base_url: str,
        *,
        allow_pdf_bypass: bool = True,
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
            base_url:        Starting URL to resolve (e.g. ``"https://example.com"``).
            allow_pdf_bypass: Let PDF URLs bypass domain checks (default True).
            max_redirects:   Max redirect hops per probe (default 10).
            timeout:         Per-request timeout in seconds (default 30).
            concurrency:     Max parallel variation probes (default 4).

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

    PDFs always bypass this filter (tearsheets and fund docs are valuable
    regardless of their URL path).

    Args:
        allow_pdf: Let ``.pdf`` URLs bypass the filter (default True).
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
            if self._allow_pdf and url.lower().endswith(".pdf"):
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
        if url.lower().endswith(".pdf"):
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
