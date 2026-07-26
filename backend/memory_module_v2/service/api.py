"""Service API: distill_session, search_memory, get_exchange."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from config import get_settings
from graph.llm import build_embedding_config_from_settings, get_embedding_model

from ..domain.enums import IngestStatus, SearchMode
from ..domain.models import (
    DistillSessionResult,
    ExchangeEvidence,
    MemorySearchFilters,
    MemorySearchResponse,
)
from ..distill.distiller import distill_exchange
from ..ingest.exchange_segmenter import _is_substantive_assistant, segment_exchanges
from ..ingest.text_cleaner import clean_text
from ..ingest.session_reader import load_session_raw, read_session
from ..retrieval.service import retrieval_search
from ..storage.repos import ExchangesRepo, ObjectsRepo
from .config import get_memory_v2_config

logger = logging.getLogger(__name__)

exchanges_repo = ExchangesRepo()
objects_repo = ObjectsRepo()


def distill_session(session_id: str, *, force: bool = False) -> DistillSessionResult:
    """Distill a session into exchanges and structured objects.

    State-machine-driven and crash-resumable. Each exchange advances through
    ``pending -> distilled -> done`` and every transition is committed
    atomically together with the row it depends on:

    * ``pending``   : verbatim evidence persisted (index-free source of truth).
    * ``distilled`` : distilled object persisted; committed in the *same*
      transaction that flips the state, so a crash can never leave
      "evidence written but object missing" (the defect that the old
      existence-only idempotency check silently skipped).
    * ``done``      : embedding attached; again committed with the state flip.

    On restart the work set is every exchange whose status != ``done``. An
    exchange already at ``distilled`` only needs its embedding recomputed, so the
    expensive LLM distillation step is skipped ("only redo unfinished steps").
    ``force=True`` reprocesses everything from scratch.
    """
    config = get_memory_v2_config()
    result = DistillSessionResult(
        session_id=session_id,
        started_at=datetime.now(timezone.utc),
    )

    messages = read_session(session_id)
    if not messages:
        logger.warning("No messages found for session %s", session_id)
        result.finished_at = datetime.now(timezone.utc)
        return result

    exchanges = segment_exchanges(
        session_id,
        messages,
        min_exchange_chars=config.min_exchange_chars,
        max_ply_len=config.max_ply_len,
        min_assistant_chars=config.min_assistant_chars,
    )
    result.exchanges_total = len(exchanges)

    status_map = {} if force else exchanges_repo.get_status_map(session_id)

    # Persist newly-seen exchanges as 'pending' (evidence layer / source of truth).
    new_exchanges = [ex for ex in exchanges if ex.exchange_id not in status_map]
    result.exchanges_new = len(new_exchanges)
    exchanges_repo.upsert_batch(new_exchanges)

    # Work set = everything not already 'done'. 'done' exchanges are skipped.
    done = IngestStatus.DONE.value
    work = [ex for ex in exchanges if force or status_map.get(ex.exchange_id) != done]
    result.objects_skipped = len(exchanges) - len(work)
    result.exchanges_resumed = sum(
        1 for ex in work if status_map.get(ex.exchange_id) in (
            IngestStatus.PENDING.value, IngestStatus.DISTILLED.value
        )
    )

    settings = get_settings()
    emb_config = build_embedding_config_from_settings(settings)
    embedding_model = get_embedding_model(emb_config)

    for exchange in work:
        prior = status_map.get(exchange.exchange_id)
        try:
            if prior == IngestStatus.DISTILLED.value:
                # Resume: object already written, only embedding is missing.
                existing = objects_repo.get_by_exchange_id(exchange.exchange_id)
            else:
                existing = None

            if existing is not None:
                object_id = existing["object_id"]
                distill_text = existing["distill_text"]
            else:
                # pending / new / force: run distillation, checkpoint atomically.
                obj = distill_exchange(exchange)
                objects_repo.upsert_object_mark_distilled(obj)
                object_id = obj.object_id
                distill_text = obj.distill_text
                result.objects_created += 1

            # distilled -> done: attach embedding, atomically flip state.
            try:
                emb = embedding_model.embed_query(distill_text)
            except Exception as emb_exc:
                # Leave the exchange at 'distilled'; the next run resumes here
                # and retries only the embedding step.
                logger.warning("Embedding failed for %s: %s", object_id, emb_exc)
                result.errors.append({
                    "exchange_id": exchange.exchange_id,
                    "error": f"embedding failed: {emb_exc}",
                })
                continue

            objects_repo.attach_embedding_mark_done(object_id, exchange.exchange_id, emb)
        except Exception as exc:
            logger.exception("Distillation failed for exchange %s", exchange.exchange_id)
            result.errors.append({
                "exchange_id": exchange.exchange_id,
                "error": str(exc),
            })

    result.finished_at = datetime.now(timezone.utc)
    return result


def search_memory(
    query: str,
    *,
    mode: SearchMode = SearchMode.HYBRID_CROSS,
    top_k: int = 10,
    filters: MemorySearchFilters | None = None,
    debug: bool = False,
) -> MemorySearchResponse:
    """Search memory using the two-layer architecture."""
    return retrieval_search(
        query=query,
        mode=mode,
        top_k=top_k,
        filters=filters,
        debug=debug,
    )


def get_exchange(
    session_id: str,
    ply_start: int,
    ply_end: int,
) -> ExchangeEvidence:
    """Evidence drilldown: fetch verbatim messages for an exchange."""
    config = get_memory_v2_config()
    data = load_session_raw(session_id)
    if data is None:
        return ExchangeEvidence(
            session_id=session_id,
            ply_start=ply_start,
            ply_end=ply_end,
        )

    all_messages = data.get("messages", [])
    sliced = all_messages[ply_start: ply_end + 1]

    messages_out: list[dict[str, Any]] = []
    snippet_parts: list[str] = []
    for i, msg in enumerate(sliced):
        msg_index = ply_start + i
        entry: dict[str, Any] = {
            "msg_index": msg_index,
            "role": msg.get("role", "unknown"),
            "content": msg.get("content", ""),
        }
        if msg.get("tool_calls"):
            entry["tool_calls"] = msg["tool_calls"]
        messages_out.append(entry)

        role = msg.get("role", "").upper()
        content = msg.get("content", "")
        if not content:
            continue
        if role == "USER":
            snippet_parts.append(f"{role}: {content}")
        elif role == "ASSISTANT" and _is_substantive_assistant(
            content,
            min_chars=config.min_assistant_chars,
        ):
            snippet_parts.append(f"{role}: {clean_text(content)}")

    return ExchangeEvidence(
        session_id=session_id,
        ply_start=ply_start,
        ply_end=ply_end,
        messages=messages_out,
        verbatim_snippet="\n\n".join(snippet_parts),
    )
