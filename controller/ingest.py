"""Resume-safe ingestion of simulator history and event chunks into the store."""

from __future__ import annotations

from typing import Any

from .client import SimClient
from .crash import crash_point
from .store import Store


def ingest_new_chunks(client: SimClient, store: Store) -> dict[str, int]:
    """Fetch every chunk after the stored cursor for history and events.

    Returns a mapping from chunk kind to the number of fetched events.
    """
    ingested: dict[str, int] = {}
    for kind in ("history", "events"):
        cursor = store.get_cursor(kind)
        after = -1 if cursor is None else cursor
        chunks = client.list_chunks(kind=kind, after=after)
        for chunk in sorted(chunks, key=lambda item: int(item["chunk"])):
            chunk_no = int(chunk["chunk"])
            events = client.get_chunk(chunk_no)
            store.save_chunk(kind, chunk_no, events)
            ingested[kind] = ingested.get(kind, 0) + len(events)
            crash_point(
                "mid_ingest",
                {"kind": kind, "chunk": chunk_no, "events": len(events)},
            )
    return ingested
