"""Research orchestration and Topic Dossier contracts."""

from .contracts import (
    DossierRepository,
    EvidenceCandidate,
    EvidenceGroup,
    ResearchStatus,
    ResearchTopic,
    SearchResponse,
    TopicDossier,
)
from .service import ResearchError, ResearchService

__all__ = [
    "DossierRepository",
    "EvidenceCandidate",
    "EvidenceGroup",
    "ResearchError",
    "ResearchService",
    "ResearchStatus",
    "ResearchTopic",
    "SearchResponse",
    "TopicDossier",
]
