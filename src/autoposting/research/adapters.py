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
from ..reader import ReaderService, normalize_url
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
    """Adapt :class:`ReaderService`'s ArticleDocument to a generic reader.

    ``fallback_resolver`` is optional on purpose: the research layer must not
    import the database, so the caller injects a lookup that returns
    already-collected content (``content_text`` / ``title``) for a URL.  That
    content is handed to the reader as ``rss_content``, which reuses the
    existing feed-first extraction instead of adding a second parser.  A
    resolver is never used to retry or to work around a block.
    """

    def __init__(
        self,
        reader: ReaderService | None = None,
        fallback_resolver: Callable[[str], Mapping[str, object] | None] | None = None,
    ) -> None:
        self.reader = reader or ReaderService()
        self.fallback_resolver = fallback_resolver

    def read(self, candidate: EvidenceCandidate) -> object:
        rss_content, collected_title = self._collected_content(candidate.url)
        return self.reader.read(
            candidate.url,
            title_hint=collected_title or candidate.title,
            source_type="evidence",
            rss_content=rss_content,
        )

    def _collected_content(self, url: str) -> tuple[str | None, str]:
        """Look up already-collected content without any new request."""

        if self.fallback_resolver is None:
            return None, ""
        # The store keys on the canonical url, and the candidate may still carry
        # a fragment or tracking parameter.  Canonicalise before the lookup;
        # the caller's own url is untouched for the actual read.
        lookup = normalize_url(url) or url
        try:
            value = self.fallback_resolver(lookup)
        except Exception:
            # A fallback lookup must never turn a read into an exception.
            return None, ""
        if not isinstance(value, Mapping):
            return None, ""
        content = value.get("content_text") or value.get("content")
        if not isinstance(content, str) or not content.strip():
            return None, ""
        title = value.get("title")
        return content, str(title).strip() if isinstance(title, str) else ""
