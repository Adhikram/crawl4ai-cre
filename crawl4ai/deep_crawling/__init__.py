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
    CRERealEstateRelevanceFilter,
    CREDomainScopingFilter,
    build_cre_filter_chain,
)
from .cre_scorers import (
    CREKeywordRelevanceScorer,
    CRENewsDeprioritizationScorer,
    CREPageTypePriorityScorer,
    build_cre_composite_scorer,
)

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
    "CRERealEstateRelevanceFilter",
    "CREDomainScopingFilter",
    "build_cre_filter_chain",
    # CRE scorers
    "CREKeywordRelevanceScorer",
    "CRENewsDeprioritizationScorer",
    "CREPageTypePriorityScorer",
    "build_cre_composite_scorer",
]
