"""Independent local news-analysis adapter; no production-agent wiring."""

from src.news_local.models import NewsAnalysisInput, NewsAnalysisResult
from src.news_local.providers import NewsSummarizer
from src.news_local.macro_adapter import adapt_macro_source_results

__all__ = [
    "NewsAnalysisInput",
    "NewsAnalysisResult",
    "NewsSummarizer",
    "adapt_macro_source_results",
]
