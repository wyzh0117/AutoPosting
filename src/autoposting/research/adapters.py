"""Adapters that reuse the retained related-search engine and Reader services.

Because this adapter is now the *only* consumer of the related-search engine,
it is also the place the engine's log/credential boundary has to be honoured:
failure details pass through :func:`redact_related_detail` before they can
reach a diagnostic that the research API and the dossier will carry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from ..background import RelatedSearchError, redact_related_detail, search_related_news
from ..diagnostics import make_diagnostic
from ..reader import ReaderService
from .contracts import EvidenceCandidate, SearchResponse


class SearchAdapter(Protocol):
    def search(
        self,
        query: str,
        *,
        adapters: tuple[str, ...],
        limit: int,
    ) -> SearchResponse:
        ...


class EvidenceReader(Protocol):
    def read(self, candidate: EvidenceCandidate) -> object:
        ...


class RelatedNewsSearchAdapter:
    """Bridge the existing bounded related-search implementation."""

    def __init__(
        self,
        searcher: Callable[..., object] = search_related_news,
        *,
        fetcher: Callable[[str], object] | None = None,
    ) -> None:
        self.searcher = searcher
        self.fetcher = fetcher

    def search(
        self,
        query: str,
        *,
        adapters: tuple[str, ...],
        limit: int,
    ) -> SearchResponse:
        try:
            report = self.searcher(query, fetcher=self.fetcher, limit=limit)
        except RelatedSearchError as exc:
            diagnostic = make_diagnostic(
                "related_search",
                "research_search",
                code="research_search_failed",
                cause_code=exc.cause_code,
                detail=redact_related_detail(exc.detail),
            )
            return SearchResponse(
                query=query,
                adapters=adapters,
                degraded=True,
                cause_code=exc.cause_code,
                diagnostics=(diagnostic,),
            )
        except Exception as exc:
            diagnostic = make_diagnostic(
                "related_search",
                "research_search",
                code="research_search_failed",
                cause_code="unknown_error",
                detail=redact_related_detail(exc),
            )
            return SearchResponse(
                query=query,
                adapters=adapters,
                degraded=True,
                cause_code="unknown_error",
                diagnostics=(diagnostic,),
            )
        results = getattr(report, "results", None)
        if not isinstance(results, (list, tuple)):
            results = []
        return SearchResponse(
            query=str(getattr(report, "query", query) or query),
            results=tuple(item for item in results if isinstance(item, Mapping)),
            adapters=adapters,
            degraded=bool(getattr(report, "degraded", False)),
            cause_code=str(getattr(report, "cause_code", "") or ""),
        )


class ReaderServiceEvidenceAdapter:
    """Adapt :class:`ReaderService`'s ArticleDocument to a generic reader."""

    def __init__(self, reader: ReaderService | None = None) -> None:
        self.reader = reader or ReaderService()

    def read(self, candidate: EvidenceCandidate) -> object:
        return self.reader.read(
            candidate.url,
            title_hint=candidate.title,
            source_type="evidence",
        )
