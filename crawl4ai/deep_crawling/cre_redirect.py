"""
CRE Redirect-Aware Domain Discovery.

Ported from: anax/dash/src/lib/utils/fetchwebsite.ts
  - followRedirectsToFinalDomain()    → follow_redirects_to_final_domain()
  - GlobalUrlTracker.initializeRedirectTracking() → discover_all_redirect_domains()

Key behaviors preserved from the TypeScript original:
  1. Manual redirect following via HEAD requests (up to MAX_REDIRECTS hops).
  2. Loop detection – stops if a URL appears twice in the chain.
  3. SSL error fallback – if HTTPS fails, tries the www / non-www counterpart once.
  4. Bidirectional variation testing – probes http/https × www/non-www variants
     to find all domains that ultimately resolve to the same canonical domain.
  5. Best-URL selection – prefers www-HTTPS > www-HTTP > HTTPS > HTTP.
  6. Comprehensive chain – merges all variant chains into a single set of known
     domains so that the domain-scoping filter accepts any of them.

Usage (async context)::

    from crawl4ai.deep_crawling.cre_redirect import discover_all_redirect_domains

    result = await discover_all_redirect_domains("https://example.com")
    # result.final_url     → "https://www.example.com"
    # result.final_domain  → "example.com"
    # result.all_domains   → {"example.com", "www.example.com", …}

    # Wire into the domain-scoping filter:
    from crawl4ai.deep_crawling.cre_filters import CREDomainScopingFilter
    filter_ = await CREDomainScopingFilter.create_from_url("https://example.com")
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional, Set
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_REDIRECTS = 10
HEAD_TIMEOUT = 30.0   # seconds per HEAD request
ALT_TIMEOUT  = 10.0   # seconds for the SSL-fallback probe


# ---------------------------------------------------------------------------
# Data transfer object
# ---------------------------------------------------------------------------

@dataclass
class RedirectResult:
    """
    Result of resolving a URL through its redirect chain.

    Attributes:
        final_url:      The URL where the chain terminates (no more redirects).
        final_domain:   Normalised base domain of ``final_url`` (no www prefix).
        redirect_chain: Ordered list of every URL seen, starting at the seed URL.
        all_domains:    All unique normalised hostnames encountered (including
                        those from parallel variation probes).
    """
    final_url: str
    final_domain: str
    redirect_chain: List[str]
    all_domains: FrozenSet[str] = field(default_factory=frozenset)

    @property
    def working_variations(self) -> List[str]:
        """All URLs whose normalised domain matches ``final_domain``."""
        return [
            u for u in self.redirect_chain
            if _normalize_hostname(urlparse(u).hostname or "") == self.final_domain
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_hostname(hostname: str) -> str:
    """Strip ``www.`` prefix and lowercase – mirrors normalizeHostname() in TS."""
    return re.sub(r"^www\.", "", (hostname or "").lower())


def _get_www_alternative(url: str) -> Optional[str]:
    """Return the www ↔ non-www counterpart of *url*, or None on parse failure."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host.startswith("www."):
            new_host = host[4:]
        else:
            new_host = f"www.{host}"
        return parsed._replace(netloc=new_host).geturl()
    except Exception:
        return None


def _url_preference_key(url: str):
    """
    Sorting key for best-URL selection.
    Priority order (lower = better):
      0 → www + HTTPS
      1 → www + HTTP
      2 → non-www + HTTPS
      3 → non-www + HTTP
    """
    try:
        p = urlparse(url)
        has_www  = (p.hostname or "").startswith("www.")
        is_https = p.scheme == "https"
        if has_www and is_https:
            return 0
        if has_www and not is_https:
            return 1
        if not has_www and is_https:
            return 2
        return 3
    except Exception:
        return 4


# ---------------------------------------------------------------------------
# Core async functions
# ---------------------------------------------------------------------------

async def follow_redirects_to_final_domain(
    url: str,
    *,
    max_redirects: int = MAX_REDIRECTS,
    timeout: float = HEAD_TIMEOUT,
    _tried_alternative: bool = False,
) -> RedirectResult:
    """
    Follow HTTP redirects for *url* and return the final landing point.

    Uses HEAD requests so no page body is downloaded.  Handles:
    - Manual redirect chasing (up to *max_redirects* hops).
    - Infinite-loop detection.
    - SSL errors → automatic www / non-www fallback (tried once).

    Args:
        url:            Seed URL to resolve.
        max_redirects:  Maximum number of redirect hops to follow.
        timeout:        Per-request timeout in seconds.
        _tried_alternative: Internal flag – prevents infinite recursion when
                            trying SSL fallback.

    Returns:
        A :class:`RedirectResult` with the final URL, normalised domain,
        and the full redirect chain.
    """
    try:
        import aiohttp
    except ImportError:
        # httpx fallback
        return await _follow_redirects_httpx(
            url, max_redirects=max_redirects, timeout=timeout,
            _tried_alternative=_tried_alternative,
        )

    chain: List[str] = [url]
    current = url

    try:
        connector = aiohttp.TCPConnector(ssl=False)          # handles self-signed certs
        timeout_obj = aiohttp.ClientTimeout(total=timeout)

        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout_obj
        ) as session:
            for hop in range(max_redirects):
                try:
                    async with session.head(
                        current,
                        allow_redirects=False,
                        headers={"User-Agent": "Mozilla/5.0 CRE-Crawler/1.0"},
                    ) as resp:
                        status = resp.status

                        if 300 <= status < 400:
                            location = resp.headers.get("Location") or resp.headers.get("location")
                            if not location:
                                logger.warning("Redirect %d with no Location header at %s", status, current)
                                break

                            # Resolve relative locations
                            next_url = urljoin(current, location)

                            if next_url in chain:
                                logger.warning("Redirect loop detected at %s", next_url)
                                break

                            logger.debug("Redirect %d: %s → %s", hop + 1, current, next_url)
                            chain.append(next_url)
                            current = next_url
                        else:
                            break  # 2xx / 4xx / 5xx – stop here

                except aiohttp.ClientSSLError as ssl_err:
                    return await _handle_ssl_error(
                        url, ssl_err, max_redirects, timeout, _tried_alternative
                    )
                except Exception as exc:
                    if _is_ssl_like(exc) and not _tried_alternative:
                        return await _handle_ssl_error(
                            url, exc, max_redirects, timeout, _tried_alternative
                        )
                    raise

        final_domain = _normalize_hostname(urlparse(current).hostname or "")
        logger.info("Final domain after %d redirects: %s (%s)", len(chain) - 1, final_domain, current)
        return RedirectResult(
            final_url=current,
            final_domain=final_domain,
            redirect_chain=chain,
            all_domains=frozenset(_normalize_hostname(urlparse(u).hostname or "") for u in chain),
        )

    except Exception as exc:
        if _is_ssl_like(exc) and not _tried_alternative:
            return await _handle_ssl_error(url, exc, max_redirects, timeout, _tried_alternative)

        logger.error("Error following redirects for %s: %s", url, exc)
        # Graceful fallback to the original URL
        domain = _normalize_hostname(urlparse(url).hostname or "")
        return RedirectResult(
            final_url=url,
            final_domain=domain,
            redirect_chain=[url],
            all_domains=frozenset([domain]),
        )


async def _follow_redirects_httpx(
    url: str,
    *,
    max_redirects: int,
    timeout: float,
    _tried_alternative: bool,
) -> RedirectResult:
    """httpx-based fallback when aiohttp is not available."""
    import httpx

    chain: List[str] = [url]
    current = url

    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=False, timeout=timeout) as client:
            for hop in range(max_redirects):
                try:
                    resp = await client.head(current, headers={"User-Agent": "Mozilla/5.0 CRE-Crawler/1.0"})
                    if 300 <= resp.status_code < 400:
                        location = resp.headers.get("location")
                        if not location:
                            break
                        next_url = urljoin(current, location)
                        if next_url in chain:
                            break
                        chain.append(next_url)
                        current = next_url
                    else:
                        break
                except Exception as exc:
                    if _is_ssl_like(exc) and not _tried_alternative:
                        return await _handle_ssl_error(url, exc, max_redirects, timeout, _tried_alternative)
                    raise

        final_domain = _normalize_hostname(urlparse(current).hostname or "")
        return RedirectResult(
            final_url=current,
            final_domain=final_domain,
            redirect_chain=chain,
            all_domains=frozenset(_normalize_hostname(urlparse(u).hostname or "") for u in chain),
        )

    except Exception as exc:
        if _is_ssl_like(exc) and not _tried_alternative:
            return await _handle_ssl_error(url, exc, max_redirects, timeout, _tried_alternative)
        domain = _normalize_hostname(urlparse(url).hostname or "")
        return RedirectResult(
            final_url=url, final_domain=domain,
            redirect_chain=[url],
            all_domains=frozenset([domain]),
        )


def _is_ssl_like(exc: Exception) -> bool:
    """Best-effort SSL error detection across aiohttp / httpx / requests."""
    msg = str(exc).upper()
    code = getattr(exc, "code", "") or ""
    return (
        "SSL" in msg or "TLS" in msg or "CERTIFICATE" in msg
        or "EPROTO" in str(code).upper()
        or getattr(exc, "errno", None) == "EPROTO"
    )


async def _handle_ssl_error(
    original_url: str,
    exc: Exception,
    max_redirects: int,
    timeout: float,
    already_tried: bool,
) -> RedirectResult:
    """
    SSL error recovery: try the www / non-www counterpart once.
    Mirrors the SSL-fallback branch in followRedirectsToFinalDomain().
    """
    logger.warning("SSL error for %s: %s – trying www/non-www alternative", original_url, exc)

    if already_tried:
        # Cannot recurse again – return fallback
        domain = _normalize_hostname(urlparse(original_url).hostname or "")
        return RedirectResult(
            final_url=original_url, final_domain=domain,
            redirect_chain=[original_url],
            all_domains=frozenset([domain]),
        )

    alt_url = _get_www_alternative(original_url)
    if not alt_url:
        domain = _normalize_hostname(urlparse(original_url).hostname or "")
        return RedirectResult(
            final_url=original_url, final_domain=domain,
            redirect_chain=[original_url],
            all_domains=frozenset([domain]),
        )

    try:
        alt_result = await follow_redirects_to_final_domain(
            alt_url, max_redirects=max_redirects, timeout=ALT_TIMEOUT, _tried_alternative=True
        )
        # Accept alternative if it resolved differently (or if it simply responds)
        logger.info("SSL alternative worked: %s → %s", alt_url, alt_result.final_url)
        return alt_result
    except Exception as alt_exc:
        logger.error("SSL alternative %s also failed: %s", alt_url, alt_exc)
        domain = _normalize_hostname(urlparse(original_url).hostname or "")
        return RedirectResult(
            final_url=original_url, final_domain=domain,
            redirect_chain=[original_url],
            all_domains=frozenset([domain]),
        )


# ---------------------------------------------------------------------------
# Comprehensive bidirectional discovery
# ---------------------------------------------------------------------------

def _generate_variations(base_url: str) -> List[str]:
    """
    Generate www / non-www × http / https variations for *base_url*.

    Mirrors the ``testUrls`` list in ``initializeRedirectTracking()``.
    Priority order matches the TS original (www-HTTPS first).
    """
    try:
        parsed = urlparse(base_url)
        path_qs = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        host = parsed.hostname or ""
        base = re.sub(r"^www\.", "", host)

        candidates = [
            f"https://www.{base}{path_qs}",   # www HTTPS  (highest priority)
            f"https://{host}{path_qs}",        # original hostname HTTPS
            f"http://www.{base}{path_qs}",     # www HTTP
            f"http://{host}{path_qs}",         # original hostname HTTP
            f"https://{base}{path_qs}",        # non-www HTTPS
            f"http://{base}{path_qs}",         # non-www HTTP
        ]
        # Deduplicate while preserving order, remove exact original
        seen: Set[str] = {base_url}
        unique: List[str] = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return unique
    except Exception:
        return []


async def discover_all_redirect_domains(
    base_url: str,
    *,
    max_redirects: int = MAX_REDIRECTS,
    timeout: float = HEAD_TIMEOUT,
    concurrency: int = 4,
) -> RedirectResult:
    """
    Resolve *base_url* and all its www / non-www / http / https variants to
    build a comprehensive set of domains that all "belong" to the same site.

    Ports ``GlobalUrlTracker.initializeRedirectTracking()`` from fetchwebsite.ts.

    Steps:
      1. Follow redirects for the original URL.
      2. Generate www/non-www × http/https variants.
      3. Probe each variant concurrently (up to *concurrency* at a time).
      4. Keep only variants whose ``final_domain`` matches the original.
      5. Select the "best" final URL (www HTTPS preferred).
      6. Merge all redirect chains → ``all_domains`` set.

    Args:
        base_url:     Starting URL (e.g. ``"https://example.com"``).
        max_redirects: Maximum hops per probe.
        timeout:      Per-request timeout in seconds.
        concurrency:  Maximum parallel HEAD requests.

    Returns:
        A :class:`RedirectResult` whose ``all_domains`` contains every hostname
        known to belong to this site.
    """
    logger.info("🔍 Discovering redirect chain for: %s", base_url)

    # Step 1: resolve the original URL
    original = await follow_redirects_to_final_domain(
        base_url, max_redirects=max_redirects, timeout=timeout
    )
    target_domain = original.final_domain
    logger.debug("Original resolves to domain: %s", target_domain)

    # Step 2: generate variations
    variations = _generate_variations(base_url)
    logger.debug("Testing %d variations: %s", len(variations), variations)

    # Step 3: probe variations concurrently
    sem = asyncio.Semaphore(concurrency)

    async def probe(var_url: str) -> Optional[RedirectResult]:
        async with sem:
            try:
                result = await follow_redirects_to_final_domain(
                    var_url, max_redirects=max_redirects, timeout=timeout
                )
                return result
            except Exception as exc:
                logger.debug("Skipped variation %s: %s", var_url, exc)
                return None

    probe_tasks = [asyncio.create_task(probe(v)) for v in variations]
    probe_results = await asyncio.gather(*probe_tasks, return_exceptions=False)

    # Step 4: keep only probes that resolve to the same canonical domain
    working: List[RedirectResult] = [original]
    all_chains: List[List[str]] = [original.redirect_chain]

    for result in probe_results:
        if result is not None and result.final_domain == target_domain:
            logger.debug("✅ Variation resolves to same domain: %s → %s", result.redirect_chain[0], result.final_url)
            working.append(result)
            all_chains.append(result.redirect_chain)

    # Step 5: select best final URL (www HTTPS > www HTTP > HTTPS > HTTP)
    working.sort(key=lambda r: _url_preference_key(r.final_url))
    best = working[0]
    logger.info("✅ Selected best URL: %s (%d variations found)", best.final_url, len(working))

    # Step 6: merge all chains into a comprehensive domain set
    all_urls: Set[str] = set()
    for chain in all_chains:
        all_urls.update(chain)

    all_domains: FrozenSet[str] = frozenset(
        _normalize_hostname(urlparse(u).hostname or "")
        for u in all_urls
        if urlparse(u).hostname
    ) | {target_domain}

    logger.info(
        "📊 Comprehensive redirect info: final=%s domains=%s chain_len=%d",
        best.final_url, sorted(all_domains), len(all_urls),
    )

    return RedirectResult(
        final_url=best.final_url,
        final_domain=best.final_domain,
        redirect_chain=list(all_urls),   # merged chain (unordered – used for domain lookup only)
        all_domains=all_domains,
    )


# ---------------------------------------------------------------------------
# URL normalisation utilities (mirrors normalizeUrl in GlobalUrlTracker)
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """
    Canonical form of *url* for deduplication.

    Rules (mirrors ``normalizeUrl`` / ``normalizeUrlForDeduplication`` in TS):
    - Protocol normalised to ``https``.
    - ``www.`` prefix removed from hostname.
    - Hash fragment stripped.
    - Query string preserved.
    - Trailing slash on root path ensured.
    """
    try:
        p = urlparse(url)
        clean_host = _normalize_hostname(p.hostname or "")
        path = p.path or "/"
        query = f"?{p.query}" if p.query else ""
        return f"https://{clean_host}{path}{query}"
    except Exception:
        return url


def rewrite_url_to_canonical_host(url: str, canonical_url: str) -> str:
    """
    Rewrite *url*'s hostname to match the canonical host from *canonical_url*,
    while keeping the original path and query.

    Ports the ``actualUrlToScrape`` logic inside ``GlobalUrlTracker.addUrl()``.
    Useful when the crawl discovered pages under ``example.com`` but the
    working URL is ``www.example.com``.
    """
    try:
        src = urlparse(url)
        canon = urlparse(canonical_url)
        # Preserve scheme + host from canonical, path+query from source
        rewritten = canon._replace(
            path=src.path or "/",
            query=src.query,
            fragment="",        # always strip fragments
        )
        return rewritten.geturl()
    except Exception:
        return url
