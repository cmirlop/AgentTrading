"""Independent local news-analysis adapter; no production-agent wiring."""

from src.news_local.models import NewsAnalysisInput, NewsAnalysisResult
from src.news_local.providers import NewsSummarizer

__all__ = ["NewsAnalysisInput", "NewsAnalysisResult", "NewsSummarizer"]
