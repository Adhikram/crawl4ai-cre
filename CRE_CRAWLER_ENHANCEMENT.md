# CRE Crawler Enhancement Guide

> **Purpose** — This document is the authoritative specification for the Commercial Real Estate (CRE) crawling enhancements built on top of [Crawl4AI](https://github.com/unclecode/crawl4ai). Attach it to any crawler integration to define exactly what filtering, scoring, WAF-handling, PDF extraction, and API behaviour is expected.

---

## Table of Contents

1. [Background & Motivation](#1-background--motivation)
2. [Architecture Overview](#2-architecture-overview)
3. [Commit History](#3-commit-history)
4. [Feature Deep-Dives](#4-feature-deep-dives)
   - [4.1 URL Filtering Pipeline](#41-url-filtering-pipeline)
   - [4.2 URL Scoring & Priority Queue](#42-url-scoring--priority-queue)
   - [4.3 Redirect-Aware Domain Discovery](#43-redirect-aware-domain-discovery)
   - [4.4 Multi-Source Link Extraction](#44-multi-source-link-extraction)
   - [4.5 Bot / WAF Challenge Detection](#45-bot--waf-challenge-detection)
   - [4.6 Retry Mechanism for Bot Challenges](#46-retry-mechanism-for-bot-challenges)
   - [4.7 PDF Document Interception](#47-pdf-document-interception)
   - [4.8 Anti-Bot Browser Defaults](#48-anti-bot-browser-defaults)
   - [4.9 CRE Deep-Crawl API Endpoints](#49-cre-deep-crawl-api-endpoints)
   - [4.10 HTML Field Stripping](#410-html-field-stripping)
5. [Port Map: fetchwebsite.ts → Python](#5-port-map-fetchwebsitets--python)
6. [Quick-Start Integration Guide](#6-quick-start-integration-guide)
7. [Configuration Reference](#7-configuration-reference)
8. [Score Tiers & Threshold Guide](#8-score-tiers--threshold-guide)
9. [WAF Vendor Coverage](#9-waf-vendor-coverage)
10. [Testing](#10-testing)
11. [Dependencies](#11-dependencies)

---

## 1. Background & Motivation

The Anax platform (`anax/dash`) already contains a sophisticated TypeScript crawler in `src/lib/utils/fetchwebsite.ts` that was purpose-built for extracting Investment Criteria (IC) data from Commercial Real Estate firm websites. That crawler encodes years of domain knowledge:

- Which URL patterns indicate high-value CRE content vs. noise (news, careers, gallery)
- How to resolve redirect chains so that domain scoping survives smart proxies and CDN rewrites
- How to discover navigation links from SPA data attributes and inline JavaScript
- How to detect and recover from WAF / bot-protection challenge pages (Cloudflare, Stackpath Shield, Imperva)
- How to extract text from PDF tearsheets / fund overviews instead of Chrome's viewer HTML

This enhancement project **ports all of that knowledge into crawl4ai** so that any Python-based crawler that integrates with crawl4ai automatically inherits the full CRE intelligence layer — without needing to reproduce the TypeScript logic.

---

## 2. Architecture Overview

```
                         ┌─────────────────────────────────────┐
                         │        Crawl4AI Deep Crawl           │
                         │   (BFS / DFS / BestFirst strategy)   │
                         └──────────────┬──────────────────────┘
                                        │ discovered URL
                                        ▼
                         ┌─────────────────────────────────────┐
                         │         CRE Filter Chain             │
                         │                                      │
                         │  1. CREValidPageFilter               │  ← drop binary/system URLs fast
                         │  2. CREDomainScopingFilter           │  ← redirect-aware domain lock
                         │  3. CRENewsThresholdFilter           │  ← adaptive news gating
                         │  4. CRERealEstateRelevanceFilter     │  ← keyword match (optional)
                         │  5. CREIrrelevantPatternFilter       │  ← regex slug rejection
                         └──────────────┬──────────────────────┘
                                        │ accepted URL
                                        ▼
                         ┌─────────────────────────────────────┐
                         │         CRE Composite Scorer         │
                         │                                      │
                         │  CREKeywordRelevanceScorer   40 %   │
                         │  CRENewsDeprioritizationScorer 30 % │
                         │  CREPageTypePriorityScorer   30 %   │
                         └──────────────┬──────────────────────┘
                                        │ priority score ∈ [0, 1]
                                        ▼
                         ┌─────────────────────────────────────┐
                         │       Playwright Browser Context     │
                         │  • Anti-bot UA + timing defaults     │
                         │  • PDF byte interception             │
                         │  • Bot/WAF challenge retry           │
                         └──────────────┬──────────────────────┘
                                        │ CrawlResult
                                        ▼
                         ┌─────────────────────────────────────┐
                         │    CRELinkExtractor (supplement)     │
                         │  • data-href / data-url attributes   │
                         │  • router.push() / location.href     │
                         └─────────────────────────────────────┘
```

---

## 3. Commit History

All CRE enhancements were landed in the following commits (oldest → newest):

| Commit | Date (IST) | Summary |
|--------|-----------|---------|
| `1f7d5e3` | 2026-04-09 23:38 | **CRE Filtering added** — initial `cre_filters.py` and `cre_scorers.py` |
| `76642bd` | 2026-04-10 00:48 | **CRE redirect discovery & multi-source link extraction** — `cre_redirect.py`, `CRELinkExtractor`, `CREDomainScopingFilter.create_from_url`, updated BFS/BestFirst strategies |
| `dfeeb48` | 2026-04-10 01:22 | **Bot challenge detection & anti-bot browser defaults** — `is_bot_challenge_response`, CRE-specific browser config in strategies |
| `055e5f7` | 2026-04-10 01:28 | **Retry mechanism for bot/WAF challenges** — `retry_if_bot_challenge` integrated into BFS, DFS, BestFirst |
| `18fa79c` | 2026-04-10 01:59 | **CRE deep-crawl API endpoints** — `CRECrawlRequest` schema, `/crawl/cre` and `/crawl/cre/stream` and `/crawl/cre/job` endpoints, HTML-stripping |
| `8fe7d2d` | 2026-04-10 02:07 | **PDF document interception** — Playwright intercepts PDF bytes, extracts text via pypdf |
| `8239af2` | 2026-04-10 02:21 | **PDF crawling test & improved bot detection** — `test_pdf.py`, refined `is_bot_challenge_response` HTML fingerprinting |
| `db6642d` | 2026-04-10 02:46 | **fit_html field stripping** — removes `fit_html` from API responses alongside `html` and `raw_html` |
| `b8322b8` | 2026-04-10 03:23 | **API documentation & playground enhancements** — `API_COLLECTION.md`, CRE options in `index.html`, `/blog` excluded from deep crawl |

---

## 4. Feature Deep-Dives

### 4.1 URL Filtering Pipeline

**File:** `crawl4ai/deep_crawling/cre_filters.py`

The pipeline is a `FilterChain` of five composable filters executed in order. Each filter is a subclass of `URLFilter` with an `apply(url) -> bool` method.

#### CREValidPageFilter

Rejects URLs that are definitively not crawlable HTML pages. Ports `isValidPageUrl()` from `fetchwebsite.ts`.

**Allows:**
- `.pdf` files unconditionally (tearsheets, fund overviews)
- Clean paths with no file extension
- Paths with known web-page extensions (`.html`, `.htm`, `.php`, `.aspx`, `.jsp`, etc.)

**Rejects:**
- Known binary/media extensions: `.xml`, `.zip`, `.jpg`, `.png`, `.mp4`, `.css`, `.js`, `.json`, …
- System & admin path prefixes: `/wp-admin`, `/wp-json`, `/api/`, `/graphql`, `/admin`, `/login`, `/dashboard`, `/sitemap`, …
- API-flavoured query parameters: `format=json`, `callback=`, `api_key=`, `token=`, …

```python
from crawl4ai.deep_crawling.cre_filters import CREValidPageFilter

f = CREValidPageFilter()
assert f.apply("https://example.com/about")           # True  — clean path
assert f.apply("https://example.com/fund.pdf")        # True  — PDF allowed
assert f.apply("https://example.com/api/data.json")   # False — API endpoint
assert f.apply("https://example.com/logo.png")        # False — image
```

#### CRENewsFilter

Binary filter: rejects any URL whose path contains a news/editorial segment. Ports `isNewsUrl()`.

News segments rejected by default: `/news/`, `/blog/`, `/article/`, `/press-release/`, `/press/`, `/media/`, `/insights/`, `/updates/`, `/thought-leadership/`

Set `reverse=True` to build a dedicated news crawl.

#### CRENewsThresholdFilter *(adaptive, preferred over CRENewsFilter)*

Ports the adaptive queue logic from `GlobalUrlTracker.getNextPendingUrl()` — the constant `MIN_NON_NEWS_PAGES_BEFORE_SKIPPING_NEWS = 10` in `fetchwebsite.ts`.

**Behaviour:**
- While fewer than `min_non_news_before_skip` non-news pages have been crawled: **all URLs pass** (news included, so breadth is preserved early on).
- Once the threshold is reached: **news URLs are silently dropped** so the crawl focuses entirely on business / IC content.

The crawl strategy automatically calls `threshold_filter.record_crawled(url)` after each successful result.

```python
from crawl4ai.deep_crawling.cre_filters import CRENewsThresholdFilter

tf = CRENewsThresholdFilter(min_non_news_before_skip=10)
# Before 10 non-news pages: news passes
assert tf.apply("https://example.com/news/q1-update")   # True  (threshold not met)
# After recording 10 non-news pages via tf.record_crawled(...)
assert tf.apply("https://example.com/news/q1-update")   # False (threshold met)
assert tf.apply("https://example.com/about")            # True  (non-news always passes)
```

#### CRERealEstateRelevanceFilter

Ports `isRealEstateRelated()`. Uses two keyword lists:

**Exclude keywords** (immediate rejection): `/sitemap`, `gallery`, `contact`, `/posts`, `/videos`, `/media`, `/blog`, `/news`, `/events`, `/careers`, `/jobs`, `/privacy`, `/terms`, `/legal`, `/cookie`, `/disclaimer`, `/application`, `/apply`

**CRE keywords** (positive signal): `about`, `strategy`, `fund`, `portfolio`, `investment`, `criteria`, `capital`, `assets`, `properties`, `leadership`, `team`, `multifamily`, `commercial`, `industrial`, `retail`, `hospitality`, `development`, `acquisition`, `real-estate`, and 30+ more.

In `strict=True` mode (default), URLs matching neither group are rejected. In `strict=False`, only explicitly excluded URLs are blocked.

#### CREIrrelevantPatternFilter

Uses compiled regex patterns to catch **dated/numbered slugs** that string-prefix filters miss:

```
/events/12345    /careers/67890    /news/2024-article    /gallery    /videos
```

Ports the `irrelevantPatterns` array from `calculatePageRelevance()` in `fetchwebsite.ts` (lines 1201–1229).

#### Building the Complete Chain

```python
# Synchronous (pre-known domain)
from crawl4ai.deep_crawling.cre_filters import build_cre_filter_chain

chain = build_cre_filter_chain(
    base_domain="example.com",
    allow_news=False,
    strict_cre_relevance=False,
    news_threshold=10,          # None for binary filter
)

# Async (redirect-aware — RECOMMENDED for production)
from crawl4ai.deep_crawling.cre_filters import async_build_cre_filter_chain

chain = await async_build_cre_filter_chain(
    "https://example.com",
    allow_news=False,
    strict_cre_relevance=False,
    news_threshold=10,
)
```

---

### 4.2 URL Scoring & Priority Queue

**File:** `crawl4ai/deep_crawling/cre_scorers.py`

Three scorers combine via `CompositeScorer` to produce a priority score ∈ [0, 1] for each candidate URL. The `BestFirstCrawlingStrategy` uses this score to build a max-heap so the highest-value pages are always crawled first.

#### CREKeywordRelevanceScorer (weight 40 %)

| URL contains | Score |
|---|---|
| Any CRE keyword (`investment`, `fund`, `portfolio`, …) | 1.0 |
| Any exclude keyword (`contact`, `careers`, `gallery`, …) | 0.1 |
| Neither | 0.5 |

#### CRENewsDeprioritizationScorer (weight 30 %)

| URL type | Score |
|---|---|
| News / blog / press / insights | 0.15 |
| Business / product page | 0.85 |

PDFs are never classified as news.

#### CREPageTypePriorityScorer (weight 30 %)

Tiered scoring by recognised path segment:

| Tier | Segments | Score |
|------|----------|-------|
| 1 — Highest IC value | `criteria`, `investment`, `strategy`, `approach`, `philosophy` | 0.90 – 1.0 |
| 2 — Company identity | `about`, `leadership`, `team`, `management`, `principals`, `partners` | 0.70 – 0.75 |
| 3 — Services | `services`, `capabilities`, `expertise`, `what-we-do` | 0.65 |
| 4 — RE verticals | `multifamily`, `commercial`, `industrial`, `retail`, `office`, `hospitality` | 0.55 – 0.60 |
| 5 — Weak signal | `story`, `history`, `mission`, `vision` | 0.50 |
| Default | Everything else | 0.40 |

#### Representative Composite Scores

```
/investment-criteria  → ~0.955      /fund               → ~0.925
/about                → ~0.880      /team               → ~0.865
/commercial-re        → ~0.835      /multifamily        → ~0.835
/loans                → ~0.575      / (homepage)        → ~0.575
/contact  (exclude)   → ~0.415      /careers            → ~0.415
/blog/post (news)     → ~0.205      /news/article       → ~0.205
```

#### Quick Start

```python
from crawl4ai.deep_crawling.cre_scorers import build_cre_composite_scorer
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

scorer = build_cre_composite_scorer(
    keyword_weight=0.4,
    news_weight=0.3,
    page_type_weight=0.3,
)

strategy = BFSDeepCrawlStrategy(
    max_depth=4,
    url_scorer=scorer,
    score_threshold=0.45,   # skip contact / careers / news
    max_pages=200,
)
```

---

### 4.3 Redirect-Aware Domain Discovery

**File:** `crawl4ai/deep_crawling/cre_redirect.py`

Many CRE firm domains redirect through smart proxies, CDN rewrites, or www ↔ non-www chains. Without tracking those chains, the domain-scoping filter incorrectly rejects valid internal pages.

Ports two functions from `fetchwebsite.ts`:
- `followRedirectsToFinalDomain()` → `follow_redirects_to_final_domain()`
- `GlobalUrlTracker.initializeRedirectTracking()` → `discover_all_redirect_domains()`

#### What It Does

1. **Follows the redirect chain** for the seed URL using `HEAD` requests (no body downloaded).
2. **Detects loops** — stops if a URL appears twice in the chain.
3. **SSL error fallback** — if HTTPS fails, automatically tries the www / non-www counterpart once.
4. **Bidirectional variation probing** — generates and concurrently probes all combinations of `http/https × www/non-www`.
5. **Best-URL selection** — prefers `www-HTTPS > www-HTTP > HTTPS > HTTP`.
6. **Comprehensive domain set** — merges all variant chains into `all_domains` so the scoping filter accepts any of them.

#### RedirectResult

```python
@dataclass
class RedirectResult:
    final_url: str          # canonical landing URL
    final_domain: str       # normalised (no www.) domain
    redirect_chain: list    # every URL seen across all probes
    all_domains: frozenset  # all normalised hostnames encountered
```

#### Usage

```python
from crawl4ai.deep_crawling.cre_redirect import discover_all_redirect_domains

result = await discover_all_redirect_domains("https://example.com")
# result.final_url     → "https://www.example.com"
# result.final_domain  → "example.com"
# result.all_domains   → frozenset({"example.com", "www.example.com"})

# Directly via CREDomainScopingFilter factory (most convenient)
from crawl4ai.deep_crawling.cre_filters import CREDomainScopingFilter

domain_filter = await CREDomainScopingFilter.create_from_url("https://example.com")
assert domain_filter.apply("https://www.example.com/about")  # True
```

---

### 4.4 Multi-Source Link Extraction

**File:** `crawl4ai/deep_crawling/cre_link_extractor.py`

Crawl4AI's built-in parser only follows standard `<a href>` anchors. Many CRE firm sites are built with Next.js, React, or other SPA frameworks that surface navigation targets through:

- **Data attributes** — `data-href`, `data-url`, `data-link`, `data-navigation`, `data-route`, `data-path`
- **Inline JavaScript** — `router.push("/path")`, `router.navigate()`, `window.location = "..."`, `location.href = "..."`, and `href=` / `url:` key-value pairs in script blocks

`CRELinkExtractor` supplements the standard link list with these additional sources.

Ports `extractSameDomainLinks()` from `fetchwebsite.ts` (lines 1760–1844).

#### Usage

```python
from crawl4ai.deep_crawling.cre_link_extractor import CRELinkExtractor

extractor = CRELinkExtractor(
    include_data_attrs=True,    # scan data-href / data-url / …
    include_js_patterns=True,   # scan inline JS navigation
)

extra_links = extractor.extract(
    html=result.html,
    base_url="https://example.com/team",
    allowed_domains={"example.com"},   # normalised, no www
)
# Returns: [{"href": "https://example.com/about", "text": "", "type": "link"}, …]
```

The BFS and BestFirst strategies automatically call `CRELinkExtractor` when CRE scoring is active, merging the results into the standard link queue.

---

### 4.5 Bot / WAF Challenge Detection

**File:** `crawl4ai/deep_crawling/cre_filters.py` — `is_bot_challenge_response()`

WAF challenge pages must **not** count towards page limits or trigger link discovery — otherwise the crawler wastes its entire page budget on blocked responses.

#### Detection Layers (in priority order)

| Layer | What is checked | WAF vendors covered |
|-------|----------------|---------------------|
| 1. Response headers | `sg-captcha: challenge`, `cf-mitigated: challenge`, presence of `x-sucuri-cache` | Stackpath Shield, Cloudflare, Sucuri |
| 2. Redirect URL | `sgcaptcha`, `/.well-known/captcha`, `cdn-cgi/challenge`, `/__cf_chl`, `/_Incapsula_Resource`, `/distil_r_captcha` | Stackpath, Cloudflare, Imperva, Distil |
| 3. HTTP status + robots tag | `202` + `x-robots-tag: noindex` | Stackpath (common pattern) |
| 4. `<title>` content | "just a moment…", "attention required", "checking your browser", "one moment, please", "ddos protection", "security check", "please wait" | Cloudflare, generic |
| 5. HTML body fingerprint | `sgcaptcha` in body, "checking the site connection security", "robot-suspicion" image asset | Stackpath Shield |

```python
from crawl4ai.deep_crawling.cre_filters import is_bot_challenge_response

if is_bot_challenge_response(result):
    # do not count towards page limit; do not extract links
    continue
```

---

### 4.6 Retry Mechanism for Bot Challenges

**File:** `crawl4ai/deep_crawling/cre_filters.py` — `retry_if_bot_challenge()`

When a WAF challenge is detected, the page is retried with increasing delays. This mirrors the per-page fallback in `fetchwebsite.ts`:

> First retry after 2 s so the Proof-of-Work JS can complete; then after 5 s if still challenged. The browser session cookie set on a successful pass persists for all subsequent pages — no global delay is needed.

The retry sequence (configurable): **2 s → 5 s → 10 s → 30 s → 60 s**

Each retry uses `crawler.arun_many()` with the same session so WAF cookies accumulated during the first pass are automatically sent.

```python
from crawl4ai.deep_crawling.cre_filters import retry_if_bot_challenge

result = await retry_if_bot_challenge(
    result=result,
    url=url,
    crawler=crawler,
    base_config=config,
    logger=logger,
    retry_delays=(2.0, 5.0, 10.0),
)
if is_bot_challenge_response(result):
    logger.warning("Persistent WAF challenge on %s — skipping", url)
    continue
```

All three deep-crawl strategies (`BFSDeepCrawlStrategy`, `DFSDeepCrawlStrategy`, `BestFirstCrawlingStrategy`) automatically call `retry_if_bot_challenge` before recording a page or extracting its links.

---

### 4.7 PDF Document Interception

**File:** `crawl4ai/async_crawler_strategy.py` — inside `AsyncPlaywrightCrawlerStrategy`

When Playwright navigates to a `.pdf` URL, Chrome opens its built-in PDF viewer and `page.content()` returns viewer HTML containing no extractable text. This interception resolves that.

#### How It Works

1. After the page loads, the response `Content-Type` and URL suffix are checked.
2. If `application/pdf` is detected (or the URL ends `.pdf`), `response.body()` is called to retrieve the raw bytes.
3. The bytes are sanity-checked: real PDFs start with `%PDF`. Non-PDF bytes (WAF HTML served at a .pdf URL) are logged and skipped.
4. The bytes are written to a temp file and processed by `NaivePDFProcessorStrategy` (via `pypdf`).
5. The result is converted to structured HTML (`<div class="pdf-page" data-page="N">…</div>`) so the markdown generator produces readable output.
6. The `status_code` is overridden to `200` and `Content-Type` set to `text/html` so downstream processors behave normally.

This runs **after** the bot-challenge redirect chain, meaning WAF session cookies already established during the initial request are reused — no extra authentication step needed.

```
Dependency added: pypdf (deploy/docker/requirements.txt)
```

---

### 4.8 Anti-Bot Browser Defaults

When CRE scoring is active, the crawl strategies automatically apply stealth browser settings to reduce WAF detection rates:

| Setting | Value | Purpose |
|---------|-------|---------|
| `user_agent` | Randomised desktop Chrome UA | Avoid headless detection |
| `simulate_user` | `True` | Random mouse movements and scroll events |
| `magic` | `True` | Crawl4AI's built-in anti-fingerprint mode |
| `delay_before_return_html` | `2.0` s | Allow JS/SPA hydration and PoW scripts to complete |
| `page_timeout` | `60 000` ms | Extra headroom for slow CRE firm sites |

These defaults are applied only when the CRE filter chain is present — standard crawls are unaffected.

---

### 4.9 CRE Deep-Crawl API Endpoints

**Files:** `deploy/docker/api.py`, `deploy/docker/server.py`, `deploy/docker/schemas.py`

Three new endpoints expose the full CRE crawl stack over HTTP, suitable for direct integration from JavaScript frontends.

#### Request Schema — `CRECrawlRequest`

```json
{
  "url": "https://example-cre-firm.com",
  "strategy": "dfs",
  "max_pages": 500,
  "max_depth": 10,
  "include_news": false,
  "no_html": true,
  "webhook_config": null
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | required | Seed URL |
| `strategy` | string | `"dfs"` | `"dfs"`, `"bfs"`, or `"best-first"` |
| `max_pages` | int | `500` | Hard page cap (1–5000) |
| `max_depth` | int | `10` | Max link-hop depth (1–20) |
| `include_news` | bool | `false` | Pass news/blog URLs through filter |
| `no_html` | bool | `true` | Strip `html`, `raw_html`, `fit_html` fields |
| `webhook_config` | object | `null` | Webhook for async job notifications |

#### Endpoints

| Method | Path | Behaviour |
|--------|------|-----------|
| `POST` | `/crawl/cre` | Synchronous — returns all results when crawl completes |
| `POST` | `/crawl/cre/stream` | NDJSON stream — one result per line as pages are crawled |
| `POST` | `/crawl/cre/job` | Background job — returns `job_id`; sends webhook on completion |

All three endpoints apply the full CRE pipeline automatically:
- `async_build_cre_filter_chain` (redirect-aware domain scoping)
- `build_cre_composite_scorer`
- Anti-bot browser defaults
- `CRELinkExtractor` supplemental link discovery
- `retry_if_bot_challenge` per-page retry
- PDF interception
- HTML field stripping (when `no_html=true`)

See `API_COLLECTION.md` for full request/response examples.

---

### 4.10 HTML Field Stripping

**Files:** `deploy/docker/api.py`, `deploy/docker/server.py`

CRE crawl payloads can be large. The `_strip_html` helper removes three fields from each result before returning it:

```python
for field in ("html", "raw_html", "fit_html"):
    result.pop(field, None)
```

This typically reduces per-result payload size by 60–80 % while retaining `markdown`, `extracted_content`, `metadata`, `links`, and all other structured fields needed for IC extraction.

Controlled by the `no_html: true` field on `CRECrawlRequest`.

---

## 5. Port Map: fetchwebsite.ts → Python

| TypeScript (fetchwebsite.ts) | Python (crawl4ai-cre) | File |
|---|---|---|
| `isValidPageUrl()` | `CREValidPageFilter` | `cre_filters.py` |
| `isNewsUrl()` | `CRENewsFilter` / `CRENewsThresholdFilter` | `cre_filters.py` |
| `isRealEstateRelated()` | `CRERealEstateRelevanceFilter` | `cre_filters.py` |
| `irrelevantPatterns[]` | `CREIrrelevantPatternFilter` | `cre_filters.py` |
| `GlobalUrlTracker.addUrl()` domain scoping | `CREDomainScopingFilter` | `cre_filters.py` |
| `MIN_NON_NEWS_PAGES_BEFORE_SKIPPING_NEWS = 10` | `CRENewsThresholdFilter(min_non_news_before_skip=10)` | `cre_filters.py` |
| `followRedirectsToFinalDomain()` | `follow_redirects_to_final_domain()` | `cre_redirect.py` |
| `GlobalUrlTracker.initializeRedirectTracking()` | `discover_all_redirect_domains()` | `cre_redirect.py` |
| `normalizeUrl()` | `normalize_url()` | `cre_redirect.py` |
| `actualUrlToScrape` rewrite | `rewrite_url_to_canonical_host()` | `cre_redirect.py` |
| `extractSameDomainLinks()` data attrs | `CRELinkExtractor._extract_data_attrs()` | `cre_link_extractor.py` |
| `extractSameDomainLinks()` JS patterns | `CRELinkExtractor._extract_js_patterns()` | `cre_link_extractor.py` |
| `realEstateKeywords[]` scoring | `CREKeywordRelevanceScorer` | `cre_scorers.py` |
| `excludeKeywords[]` deprioritisation | negative component of `CREKeywordRelevanceScorer` | `cre_scorers.py` |
| `getNextPendingUrl()` news deprioritisation | `CRENewsDeprioritizationScorer` | `cre_scorers.py` |
| `computeIcRagPageTotalScore()` page_type | `CREPageTypePriorityScorer` | `cre_scorers.py` |
| WAF/bot challenge page detection | `is_bot_challenge_response()` | `cre_filters.py` |
| Per-page retry with delay | `retry_if_bot_challenge()` | `cre_filters.py` |
| PDF text extraction fallback | `AsyncPlaywrightCrawlerStrategy` PDF interception | `async_crawler_strategy.py` |

---

## 6. Quick-Start Integration Guide

### Minimal CRE Crawl (Python)

```python
import asyncio
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
from crawl4ai.deep_crawling.cre_filters import async_build_cre_filter_chain
from crawl4ai.deep_crawling.cre_scorers import build_cre_composite_scorer

async def crawl_cre_firm(seed_url: str):
    filter_chain = await async_build_cre_filter_chain(seed_url)
    scorer       = build_cre_composite_scorer()

    strategy = BFSDeepCrawlStrategy(
        max_depth=5,
        filter_chain=filter_chain,
        url_scorer=scorer,
        score_threshold=0.45,
        max_pages=300,
    )

    browser_config = BrowserConfig(
        headless=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    )

    run_config = CrawlerRunConfig(
        deep_crawl_strategy=strategy,
        simulate_user=True,
        magic=True,
        delay_before_return_html=2.0,
        page_timeout=60_000,
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        results = await crawler.arun(seed_url, config=run_config)
        for r in results:
            print(r.url, "—", len(r.markdown or ""), "chars")

asyncio.run(crawl_cre_firm("https://example-cre-firm.com"))
```

### Via Docker API (curl)

```bash
# Synchronous crawl
curl -X POST http://localhost:11235/crawl/cre \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example-cre-firm.com",
    "strategy": "bfs",
    "max_pages": 200,
    "max_depth": 6,
    "include_news": false,
    "no_html": true
  }'

# Streaming crawl (NDJSON)
curl -X POST http://localhost:11235/crawl/cre/stream \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example-cre-firm.com", "max_pages": 100}' \
  --no-buffer

# Background job with webhook
curl -X POST http://localhost:11235/crawl/cre/job \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example-cre-firm.com",
    "webhook_config": {
      "url": "https://your-backend.com/webhooks/crawl-done",
      "headers": {"Authorization": "Bearer TOKEN"}
    }
  }'
```

---

## 7. Configuration Reference

### `build_cre_filter_chain` / `async_build_cre_filter_chain`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` / `base_domain` | str | required | Seed URL or pre-known domain |
| `allow_news` | bool | `False` | Pass news/blog/press URLs through |
| `strict_cre_relevance` | bool | `False` | Also require CRE keyword in URL |
| `news_threshold` | int \| None | `10` | Non-news pages before blocking news; `None` for binary filter |
| `allow_pdf_bypass` | bool | `True` | PDFs bypass domain check |
| `max_redirects` | int | `10` | Max redirect hops during domain discovery |
| `timeout` | float | `30.0` | Per-request timeout (seconds) |
| `concurrency` | int | `4` | Parallel variation probes |

### `build_cre_composite_scorer`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `keyword_weight` | float | `0.4` | Weight of `CREKeywordRelevanceScorer` |
| `news_weight` | float | `0.3` | Weight of `CRENewsDeprioritizationScorer` |
| `page_type_weight` | float | `0.3` | Weight of `CREPageTypePriorityScorer` |

### `retry_if_bot_challenge`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `retry_delays` | sequence[float] | `(2.0, 5.0, 10.0, 30.0, 60.0)` | Delay (seconds) before each retry |

---

## 8. Score Tiers & Threshold Guide

Choose your `score_threshold` based on how focused vs. broad you want the crawl:

| Threshold | Pages included | Best for |
|-----------|---------------|----------|
| `0.55` | Criteria, investment, strategy, fund, portfolio, about, team only | Tight IC extraction |
| `0.45` | Tier A/B + company identity; skips news, contact, careers | Standard CRE IC crawl |
| `0.35` | Adds RE verticals (multifamily, commercial, industrial) | Asset class coverage |
| `0.25` | Generic pages too; still blocks news/blog | Full site scan |
| `0.0` | No threshold — all domain-scoped pages | Archive / audit |

---

## 9. WAF Vendor Coverage

| Vendor | Detection method | Retry strategy |
|--------|-----------------|----------------|
| Cloudflare | `cf-mitigated: challenge` header; `/__cf_chl` redirect; "Just a moment…" title | 2 s + 5 s delays for PoW JS completion |
| Stackpath Shield | `sg-captcha: challenge` header; `sgcaptcha` in URL/body; HTTP 202 + noindex; "checking the site connection security" body | Same retry sequence |
| Sucuri | Presence of `x-sucuri-cache` header | Retry with delay |
| Imperva / Incapsula | `/_Incapsula_Resource` in redirect URL | Retry with delay |
| Distil Networks | `/distil_r_captcha` in redirect URL | Retry with delay |
| Generic CDN WAF | HTTP 202 + `x-robots-tag: noindex` | Retry with delay |

---

## 10. Testing

```bash
# Run the PDF crawl test
python test_pdf.py

# Results saved to
cat data/test_pdf_results.json
```

The test script (`test_pdf.py`) demonstrates:
- Session cookie management across a multi-URL crawl
- Bot challenge handling on PDF URLs
- Result capture and JSON serialisation

---

## 11. Dependencies

| Package | Purpose | Where required |
|---------|---------|----------------|
| `crawl4ai` | Base crawler | All |
| `aiohttp` | Async HTTP for redirect discovery (primary) | `cre_redirect.py` |
| `httpx` | Async HTTP fallback when aiohttp unavailable | `cre_redirect.py` |
| `pypdf` | PDF text extraction | `async_crawler_strategy.py`, `deploy/docker/requirements.txt` |

---

## Appendix: CRE Keyword Lists

### CRE Keywords (positive signal)

`about`, `story`, `mission`, `vision`, `goal`, `our-company`, `company`, `firm`, `overview`, `leadership`, `team`, `management`, `principals`, `partners`, `executives`, `founders`, `who-we-are`, `our-team`, `capabilities`, `services`, `expertise`, `experience`, `history`, `background`, `specialties`, `focus-areas`, `practice-areas`, `what-we-do`, `our-services`, `investment`, `philosophy`, `approach`, `strategy`, `focus`, `criteria`, `portfolio`, `investments`, `assets`, `properties`, `fund`, `funds`, `capital`, `real-estate`, `realestate`, `property`, `development`, `construction`, `acquisition`, `disposition`, `leasing`, `commercial`, `residential`, `industrial`, `retail`, `office`, `multifamily`, `hospitality`, `logistics`, `warehouse`, `asset-management`, `property-management`, `real-estate-investment`, `commercial-real-estate`

### Exclude Keywords (negative signal)

`/sitemap`, `gallery`, `contact`, `/posts`, `/videos`, `/media`, `/blog`, `/news`, `/events`, `/careers`, `/jobs`, `/privacy`, `/terms`, `/legal`, `/cookie`, `/disclaimer`, `/application`, `/applications`, `/apply`

### News Path Segments (deprioritised)

`/news/`, `/blog/`, `/article/`, `/press-release/`, `/press/`, `/media/`, `/insights/`, `/updates/`, `/thought-leadership/`

### Excluded System Paths

`/wp-json`, `/wp-admin`, `/wp-content`, `/wp-includes`, `/api/`, `/graphql`, `/rest/`, `/v1/`, `/v2/`, `/v3/`, `/feed`, `/rss`, `/atom`, `/sitemap`, `/admin`, `/login`, `/register`, `/signup`, `/signin`, `/dashboard`, `/panel`, `/control`, `/cgi-bin`, `/bin/`, `/lib/`, `/tmp/`, `/temp/`, `/config`, `/settings`, `/setup`, `/install`, `/backup`, `/backups`, `/cache`, `/logs`, `/test`, `/tests`, `/debug`, `/dev`, `/staging`, `/search`, `/search/`, `/query`, `/query/`, `/ajax`, `/ajax/`, `/xhr`, `/xhr/`, `/embed`, `/embed/`, `/widget`, `/widget/`, `/track`, `/track/`, `/analytics`, `/stats`, `/public/`, `/private/`, `/secure/`, `/protected/`, `/blog`, `/.well-known`, `/robots.txt`, `/favicon.ico`, `/sitemap.xml`, `/sitemap_index.xml`
