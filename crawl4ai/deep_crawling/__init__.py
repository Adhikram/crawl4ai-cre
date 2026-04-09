# deep_crawling/__init__.py
from .base_strategy import DeepCrawlDecorator, DeepCrawlStrategy
from .bfs_strategy import BFSDeepCrawlStrategy
from .bff_strategy import BestFirstCrawlingStrategy
from .dfs_strategy import DFSDeepCrawlStrategy
from .filters import (
    FilterChain,
    ContentTypeFilter,
    DomainFilter,
    URLFilter,
    URLPatternFilter,
    FilterStats,
    ContentRelevanceFilter,
    SEOFilter,
)
from .scorers import (
    KeywordRelevanceScorer,
    URLScorer,
    CompositeScorer,
    DomainAuthorityScorer,
    FreshnessScorer,
    PathDepthScorer,
    ContentTypeScorer,
)

# CRE (Commercial Real Estate) extensions — ported from anax/dash fetchwebsite.ts
from .cre_filters import (
    CREValidPageFilter,
    CRENewsFilter,
    CREIrrelevantPatternFilter,
    CRENewsThresholdFilter,
    CRERealEstateRelevanceFilter,
    CREDomainScopingFilter,
    build_cre_filter_chain,
    async_build_cre_filter_chain,
    is_bot_challenge_response,
    retry_if_bot_challenge,
)
from .cre_scorers import (
    CREKeywordRelevanceScorer,
    CRENewsDeprioritizationScorer,
    CREPageTypePriorityScorer,
    build_cre_composite_scorer,
)
# Redirect utilities (async, HTTP-based)
from .cre_redirect import (
    RedirectResult,
    follow_redirects_to_final_domain,
    discover_all_redirect_domains,
    normalize_url,
    rewrite_url_to_canonical_host,
)
# CRE multi-source link extractor (data attributes + JS patterns)
from .cre_link_extractor import CRELinkExtractor

__all__ = [
    # Base strategies
    "DeepCrawlDecorator",
    "DeepCrawlStrategy",
    "BFSDeepCrawlStrategy",
    "BestFirstCrawlingStrategy",
    "DFSDeepCrawlStrategy",
    # Generic filters
    "FilterChain",
    "ContentTypeFilter",
    "DomainFilter",
    "URLFilter",
    "URLPatternFilter",
    "FilterStats",
    "ContentRelevanceFilter",
    "SEOFilter",
    # Generic scorers
    "KeywordRelevanceScorer",
    "URLScorer",
    "CompositeScorer",
    "DomainAuthorityScorer",
    "FreshnessScorer",
    "PathDepthScorer",
    "ContentTypeScorer",
    # CRE filters
    "CREValidPageFilter",
    "CRENewsFilter",
    "CREIrrelevantPatternFilter",
    "CRENewsThresholdFilter",
    "CRERealEstateRelevanceFilter",
    "CREDomainScopingFilter",
    "build_cre_filter_chain",
    "async_build_cre_filter_chain",
    "is_bot_challenge_response",
    "retry_if_bot_challenge",
    # CRE scorers
    "CREKeywordRelevanceScorer",
    "CRENewsDeprioritizationScorer",
    "CREPageTypePriorityScorer",
    "build_cre_composite_scorer",
    # CRE redirect utilities
    "RedirectResult",
    "follow_redirects_to_final_domain",
    "discover_all_redirect_domains",
    "normalize_url",
    "rewrite_url_to_canonical_host",
    # CRE link extractor
    "CRELinkExtractor",
]
