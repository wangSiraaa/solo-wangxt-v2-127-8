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

    # -- parse previews (app/preview.py) ------------------------------------
    def get_message_headers(self, message_pk: int) -> list[dict[str, Any]]:
        """All stored headers of a message, ordered by ordinal."""
        ...

    def get_message_identifiers(self, message_pk: int) -> dict[str, list[str]]:
        """``{"references": [...], "in_reply_to": [...]}`` ordered by ordinal."""
        ...

    def save_parse_preview(self, record: dict[str, Any]) -> dict[str, Any]:
        """Append one preview audit row; returns it with id/created_at set."""
        ...

    def get_parse_preview(self, preview_id: int) -> dict[str, Any] | None: ...
    def list_parse_previews(self, ingest_id: int, limit: int, offset: int) -> list[dict[str, Any]]: ...
