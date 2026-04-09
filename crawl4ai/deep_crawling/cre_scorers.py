"""
CRE (Commercial Real Estate) URL Scorers for deep crawling.

Ported from: anax/dash/src/lib/utils/fetchwebsite.ts
  - realEstateKeywords list  → CREKeywordRelevanceScorer
  - excludeKeywords list     → (negative component inside same scorer)
  - isNewsUrl() pattern      → CRENewsDeprioritizationScorer
  - computeIcRagPageTotalScore() page_type_score logic → CREPageTypePriorityScorer

Scoring philosophy (mirrors fetchwebsite.ts prioritisation):
  * High-value business pages (/about, /strategy, /fund, /portfolio, …)  → score ≥ 0.7
  * Pages that match exclude keywords (/careers, /contact, /gallery, …)  → score ~0.1
  * News / blog / press pages                                             → score ~0.2
  * All other same-domain pages                                           → score 0.5

Combine with :class:`CompositeScorer` to build a weighted priority queue.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, FrozenSet, List, Optional

from .scorers import URLScorer
from .cre_filters import (
    _CRE_KEYWORDS,
    _EXCLUDE_KEYWORDS,
    _NEWS_PATTERNS,
)


# ---------------------------------------------------------------------------
# 1. CREKeywordRelevanceScorer
# ---------------------------------------------------------------------------

class CREKeywordRelevanceScorer(URLScorer):
    """
    Scores URLs based on CRE keyword presence in the path.

    Ports the priority logic from ``fetchwebsite.ts``:
      * URL contains a CRE keyword  → high score (configurable, default 1.0)
      * URL contains an exclude kw  → low score  (configurable, default 0.1)
      * Neither                     → neutral score (default 0.5)

    Args:
        cre_keywords:     Override the default CRE keyword set.
        exclude_keywords: Override the default exclude keyword set.
        cre_score:        Score returned when a CRE keyword is matched.
        exclude_score:    Score returned when an exclude keyword is matched.
        neutral_score:    Score returned when neither group matches.
        weight:           Overall scorer weight for composite use.
    """

    __slots__ = (
        "_cre_keywords",
        "_exclude_keywords",
        "_cre_score",
        "_exclude_score",
        "_neutral_score",
    )

    def __init__(
        self,
        cre_keywords: Optional[FrozenSet[str]] = None,
        exclude_keywords: Optional[FrozenSet[str]] = None,
        cre_score: float = 1.0,
        exclude_score: float = 0.1,
        neutral_score: float = 0.5,
        weight: float = 1.0,
    ):
        super().__init__(weight=weight)
        self._cre_keywords: FrozenSet[str] = cre_keywords or _CRE_KEYWORDS
        self._exclude_keywords: FrozenSet[str] = exclude_keywords or _EXCLUDE_KEYWORDS
        self._cre_score = cre_score
        self._exclude_score = exclude_score
        self._neutral_score = neutral_score

    @lru_cache(maxsize=10_000)
    def _calculate_score(self, url: str) -> float:
        url_lower = url.lower()

        # Exclude keywords have priority – return low score immediately
        for kw in self._exclude_keywords:
            if kw in url_lower:
                return self._exclude_score

        # CRE keywords → high score
        for kw in self._cre_keywords:
            if kw in url_lower:
                return self._cre_score

        return self._neutral_score


# ---------------------------------------------------------------------------
# 2. CRENewsDeprioritizationScorer
# ---------------------------------------------------------------------------

class CRENewsDeprioritizationScorer(URLScorer):
    """
    Heavily penalises news / editorial URLs, mirroring the deprioritization
    strategy in ``GlobalUrlTracker.getNextPendingUrl()`` from fetchwebsite.ts.

    PDFs are never considered news (they often contain tearsheets/fund overviews).

    Args:
        news_score:       Score for news/blog/press URLs (default 0.15).
        business_score:   Score for non-news URLs (default 0.85).
        weight:           Overall scorer weight.
    """

    __slots__ = ("_news_score", "_business_score")

    def __init__(
        self,
        news_score: float = 0.15,
        business_score: float = 0.85,
        weight: float = 1.0,
    ):
        super().__init__(weight=weight)
        self._news_score = news_score
        self._business_score = business_score

    @staticmethod
    @lru_cache(maxsize=10_000)
    def _is_news(url: str) -> bool:
        if url.lower().endswith(".pdf"):
            return False
        url_lower = url.lower()
        return any(p in url_lower for p in _NEWS_PATTERNS)

    @lru_cache(maxsize=10_000)
    def _calculate_score(self, url: str) -> float:
        return self._news_score if self._is_news(url) else self._business_score


# ---------------------------------------------------------------------------
# 3. CREPageTypePriorityScorer
# ---------------------------------------------------------------------------

# Maps well-known CRE path segments to priority scores.
# Mirrors the implicit page_type scoring in computeIcRagPageTotalScore()
# and the realEstateKeywords / excludeKeywords tiering in fetchwebsite.ts.
_PAGE_TYPE_SCORES: Dict[str, float] = {
    # Tier 1 – highest IC extraction value
    "criteria": 1.0,
    "investment": 0.95,
    "strategy": 0.95,
    "approach": 0.90,
    "philosophy": 0.90,
    "fund": 0.90,
    "funds": 0.90,
    "portfolio": 0.85,
    "investments": 0.85,
    "capital": 0.85,
    "assets": 0.80,
    "properties": 0.80,
    # Tier 2 – company identity pages
    "about": 0.75,
    "leadership": 0.75,
    "team": 0.70,
    "management": 0.70,
    "principals": 0.70,
    "partners": 0.70,
    "overview": 0.70,
    # Tier 3 – services / capabilities
    "services": 0.65,
    "capabilities": 0.65,
    "expertise": 0.65,
    "what-we-do": 0.65,
    "our-services": 0.65,
    # Tier 4 – real estate verticals
    "multifamily": 0.60,
    "commercial": 0.60,
    "industrial": 0.60,
    "office": 0.60,
    "retail": 0.60,
    "hospitality": 0.55,
    "development": 0.55,
    "acquisition": 0.55,
    "disposition": 0.55,
    # Tier 5 – weak signal pages (match but lower priority)
    "story": 0.50,
    "history": 0.50,
    "mission": 0.50,
    "vision": 0.50,
}


class CREPageTypePriorityScorer(URLScorer):
    """
    Assigns priority scores based on recognised CRE page-type path segments.

    Tiered scoring mirrors the IC-extraction priority used in the Anax
    ``CompanyWebCrawlerProcessor`` / ``computeIcRagPageTotalScore``:
      * /criteria, /investment, /strategy → highest (≈ 1.0)
      * /about, /leadership, /team        → medium  (≈ 0.7)
      * /story, /history, /mission        → lower   (≈ 0.5)
      * Everything else                   → neutral (default_score)

    Args:
        page_type_scores:  Override the built-in score map.
        default_score:     Score for URLs matching no known page type.
        weight:            Overall scorer weight.
    """

    __slots__ = ("_scores", "_default_score")

    def __init__(
        self,
        page_type_scores: Optional[Dict[str, float]] = None,
        default_score: float = 0.40,
        weight: float = 1.0,
    ):
        super().__init__(weight=weight)
        self._scores: Dict[str, float] = page_type_scores or _PAGE_TYPE_SCORES
        self._default_score = default_score

    @lru_cache(maxsize=10_000)
    def _calculate_score(self, url: str) -> float:
        url_lower = url.lower()
        best = self._default_score
        for segment, score in self._scores.items():
            if segment in url_lower and score > best:
                best = score
        return best


# ---------------------------------------------------------------------------
# 4. Convenience factory: build a composite CRE scorer
# ---------------------------------------------------------------------------

def build_cre_composite_scorer(
    keyword_weight: float = 0.4,
    news_weight: float = 0.3,
    page_type_weight: float = 0.3,
):
    """
    Return a :class:`~crawl4ai.deep_crawling.scorers.CompositeScorer`
    that combines all three CRE-specific scorers with configurable weights.

    Default weights reflect the fetchwebsite.ts priority logic:
      * CRE keyword relevance  40 %
      * News deprioritization  30 %
      * Page-type priority     30 %

    Example::

        scorer = build_cre_composite_scorer()
        strategy = BFSDeepCrawlStrategy(
            max_depth=3,
            url_scorer=scorer,
            score_threshold=0.35,
        )
    """
    from .scorers import CompositeScorer

    return CompositeScorer(
        scorers=[
            CREKeywordRelevanceScorer(weight=keyword_weight),
            CRENewsDeprioritizationScorer(weight=news_weight),
            CREPageTypePriorityScorer(weight=page_type_weight),
        ],
        normalize=True,
    )
