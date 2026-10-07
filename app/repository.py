"""Repository interfaces used by the service layer.

Two implementations exist: :mod:`app.pg_repository` (PostgreSQL) and
:mod:`app.memory_repository` (tests / ephemeral runs). The API only depends on
this interface, so business logic is testable without a database.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence

from app.parser.models import ParsedMessage


class Repository(Protocol):
    def init_schema(self) -> None: ...

    def save_ingest(
        self,
        parsed: ParsedMessage,
        *,
        raw_relpath: str | None,
        stored_attachments: Sequence[tuple[Any, str]],
        source_name: str | None,
        status: str,
        fatal_error: str | None,
    ) -> dict[str, Any]:
        """Persist one EML (headers, parts, attachments metadata, defects).

        Returns ``{"ingest_id": int, "message_id": int | None}``. Failed parses
        get an ingest row with message_id=None.
        """
        ...

    def rebuild_threads(self) -> dict[str, Any]:
        """Recompute all thread assignments from stored headers."""
        ...

    def get_message(self, message_pk: int) -> dict[str, Any] | None: ...
    def get_ingest(self, ingest_id: int) -> dict[str, Any] | None: ...
    def list_messages(self, limit: int, offset: int) -> list[dict[str, Any]]: ...
    def search_messages(self, query: str, limit: int, offset: int) -> dict[str, Any]: ...
    def get_thread(self, thread_key: str) -> dict[str, Any] | None: ...
    def list_threads(self, limit: int, offset: int) -> list[dict[str, Any]]: ...
    def list_failures(self, limit: int, offset: int) -> list[dict[str, Any]]: ...
    def get_attachment(self, attachment_id: int) -> dict[str, Any] | None: ...
    def get_attachment_by_message(self, message_pk: int, attachment_id: int) -> dict[str, Any] | None: ...

    # -- reparse previews (read-only comparison; never a re-ingest) --------
    def get_message_snapshot(self, message_pk: int) -> dict[str, Any] | None:
        """Archived facts for one message in the shape the preview diff needs."""
        ...

    def thread_inputs(self) -> list[dict[str, Any]]:
        """Identity tokens/subject/date of every stored message (read-only)."""
        ...

    def save_reparse_preview(self, record: dict[str, Any]) -> dict[str, Any]:
        """Idempotently store a reparse preview; same (message, raw, parser
        version) always resolves to the same row."""
        ...

    def get_reparse_preview(self, preview_id: int) -> dict[str, Any] | None: ...
    def get_latest_reparse_preview(self, message_pk: int) -> dict[str, Any] | None: ...
    def list_reparse_previews(self, limit: int, offset: int) -> list[dict[str, Any]]: ...
