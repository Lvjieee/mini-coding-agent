from __future__ import annotations

from enum import Enum


class SearchMode(str, Enum):
    DENSE_DISTILLED = "dense_distilled"
    KEYWORD_VERBATIM = "keyword_verbatim"
    HYBRID_CROSS = "hybrid_cross"


class RoomType(str, Enum):
    FILE = "file"
    CONCEPT = "concept"
    WORKFLOW = "workflow"


class IngestStatus(str, Enum):
    """Ingest state machine for a single exchange.

    pending   -> verbatim evidence persisted, distillation not yet complete
    distilled -> distilled object persisted, embedding not yet attached
    done      -> object + embedding persisted (fully retrievable)
    """

    PENDING = "pending"
    DISTILLED = "distilled"
    DONE = "done"
