"""Parse-comparison preview ("解析对照预览").

Re-parses the *stored raw EML bytes* of an old ingest with the **current**
parser and diffs the result against the facts archived at ingest time. A
preview is read-only with respect to the archive:

* nothing is written to messages / headers / bodies / attachments / defects /
  identifiers, and threads are never recomputed;
* no files are stored or deleted — the raw EML is only read;
* the only writes are audit rows in the ``parse_previews`` table, each
  recording the parser version and the digest of the original bytes.

Diffs are deterministic: re-previewing the same original with the same parser
yields the same ``diff`` payload and the same ``diff_sha256``.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from typing import Any

from app.parser import PARSER_VERSION, parse_eml
from app.parser.html_sanitizer import escape_html
from app.parser.models import BodyPart, ParsedMessage
from app.repository import Repository
from app.storage import ControlledStorage, StorageError

log = logging.getLogger("emlarchive.preview")

# Long text values are not inlined into the diff; they are represented by a
# digest + length + short snippet so previews stay compact and comparable.
_TEXT_SNIPPET = 80

# Fields compared per category. Attachment storage facts (storage_path,
# stored) are deliberately excluded: they are storage-layer state, not parse
# facts, and a preview must never touch download paths.
_BODY_SCALAR_FIELDS = (
    "content_type",
    "charset",
    "declared_charset",
    "disposition",
    "content_id",
    "content_location",
    "byte_size",
    "referenced_cids",
)
_BODY_TEXT_FIELDS = ("text", "safe_html", "escaped_html", "plain_text")
_ATTACHMENT_FIELDS = (
    "content_type",
    "charset",
    "disposition",
    "filename",
    "raw_filename",
    "content_id",
    "content_location",
    "byte_size",
    "checksum_sha256",
)

DIFF_CATEGORIES = ("headers", "bodies", "attachments", "defects", "references")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text_ref(value: str | None) -> dict[str, Any] | None:
    """Compact, deterministic stand-in for a long text value."""
    if value is None:
        return None
    return {
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "length": len(value),
        "snippet": value[:_TEXT_SNIPPET],
    }


# -- snapshots ---------------------------------------------------------------
def _stored_body_view(row: dict[str, Any]) -> dict[str, Any]:
    view = {f: row.get(f) for f in ("mime_path",) + _BODY_SCALAR_FIELDS + _BODY_TEXT_FIELDS}
    view["referenced_cids"] = list(view["referenced_cids"] or [])
    return view


def _fresh_body_view(body: BodyPart) -> dict[str, Any]:
    """Project a freshly parsed body onto the shape persistence would store."""
    return {
        "mime_path": body.mime_path,
        "content_type": body.content_type,
        "charset": body.charset,
        "declared_charset": body.declared_charset,
        "disposition": body.disposition.value,
        "content_id": body.content_id,
        "content_location": body.content_location,
        "byte_size": body.byte_size,
        "referenced_cids": list(body.referenced_cids),
        # persistence keeps raw text only for text/plain; html keeps the
        # sanitized/escaped projections instead (see save_ingest)
        "text": body.text if body.content_type == "text/plain" else None,
        "safe_html": body.safe_html,
        "escaped_html": escape_html(body.text) if body.content_type == "text/html" else None,
        "plain_text": body.plain_text,
    }


def _stored_snapshot(
    message: dict[str, Any],
    headers: list[dict[str, Any]],
    identifiers: dict[str, list[str]],
    ingest_defects: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "headers": [
            {"ordinal": h["ordinal"], "name": h["name"], "value": h["value"], "raw_value": h["raw_value"]}
            for h in headers
        ],
        "bodies": [_stored_body_view(b) for b in message["bodies"]],
        "attachments": [
            {f: a.get(f) for f in ("mime_path",) + _ATTACHMENT_FIELDS} for a in message["attachments"]
        ],
        "defects": [
            {"stage": d["stage"], "level": d["level"], "message": d["message"]} for d in ingest_defects
        ],
        "references": {
            "message_id": message["message_id"],
            "references": list(identifiers["references"]),
            "in_reply_to": list(identifiers["in_reply_to"]),
        },
    }


def _fresh_snapshot(parsed: ParsedMessage) -> dict[str, Any]:
    return {
        "headers": [
            {"ordinal": h.ordinal, "name": h.name, "value": h.value, "raw_value": h.raw_value}
            for h in parsed.headers
        ],
        "bodies": [_fresh_body_view(b) for b in parsed.bodies],
        "attachments": [
            {
                "mime_path": a.mime_path,
                "content_type": a.content_type,
                "charset": a.charset,
                "disposition": a.disposition.value,
                "filename": a.filename,
                "raw_filename": a.raw_filename,
                "content_id": a.content_id,
                "content_location": a.content_location,
                "byte_size": a.byte_size,
                "checksum_sha256": a.checksum_sha256,
            }
            for a in parsed.attachments
        ],
        "defects": [{"stage": d.stage, "level": d.level, "message": d.message} for d in parsed.defects],
        "references": {
            "message_id": parsed.message_id,
            "references": list(parsed.references),
            "in_reply_to": list(parsed.in_reply_to),
        },
    }


# -- diffs (all deterministic: sorted keys, sequence order preserved) --------
def _diff_headers(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    common = min(len(old), len(new))
    for i in range(common):
        for field in ("name", "value", "raw_value"):
            if old[i][field] != new[i][field]:
                changes.append(
                    {
                        "kind": "changed",
                        "ordinal": i,
                        "field": field,
                        "archived": old[i][field],
                        "reparsed": new[i][field],
                    }
                )
    for i in range(common, len(old)):
        changes.append({"kind": "removed", "ordinal": i, "header": old[i]})
    for i in range(common, len(new)):
        changes.append({"kind": "added", "ordinal": i, "header": new[i]})
    return changes


def _diff_parts(
    old: list[dict[str, Any]],
    new: list[dict[str, Any]],
    scalar_fields: tuple[str, ...],
    text_fields: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Diff mime-path-keyed parts (bodies / attachments)."""
    old_map = {p["mime_path"]: p for p in old}
    new_map = {p["mime_path"]: p for p in new}
    changes: list[dict[str, Any]] = []
    for path in sorted(set(old_map) | set(new_map)):
        if path not in new_map:
            changes.append({"kind": "removed", "mime_path": path})
            continue
        if path not in old_map:
            changes.append({"kind": "added", "mime_path": path})
            continue
        o, n = old_map[path], new_map[path]
        for field in scalar_fields:
            if o.get(field) != n.get(field):
                changes.append(
                    {
                        "kind": "changed",
                        "mime_path": path,
                        "field": field,
                        "archived": o.get(field),
                        "reparsed": n.get(field),
                    }
                )
        for field in text_fields:
            if o.get(field) != n.get(field):
                changes.append(
                    {
                        "kind": "changed",
                        "mime_path": path,
                        "field": field,
                        "archived": _text_ref(o.get(field)),
                        "reparsed": _text_ref(n.get(field)),
                    }
                )
    return changes


def _diff_defects(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Multiset diff: robust to reordering, deterministic via sorting."""
    def key(d: dict[str, Any]) -> tuple[str, str, str]:
        return (d["stage"], d["level"], d["message"])

    old_counts, new_counts = Counter(key(d) for d in old), Counter(key(d) for d in new)
    removed = sorted((old_counts - new_counts).elements())
    added = sorted((new_counts - old_counts).elements())
    return [
        {"kind": "removed", "stage": s, "level": lv, "message": m} for s, lv, m in removed
    ] + [
        {"kind": "added", "stage": s, "level": lv, "message": m} for s, lv, m in added
    ]


def _diff_references(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    if old["message_id"] != new["message_id"]:
        changes.append(
            {
                "kind": "changed",
                "field": "message_id",
                "archived": old["message_id"],
                "reparsed": new["message_id"],
            }
        )
    for field in ("references", "in_reply_to"):
        old_counts, new_counts = Counter(old[field]), Counter(new[field])
        for value in sorted((old_counts - new_counts).elements()):
            changes.append({"kind": "removed", "field": field, "value": value})
        for value in sorted((new_counts - old_counts).elements()):
            changes.append({"kind": "added", "field": field, "value": value})
    return changes


def diff_snapshots(stored: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    """Diff two snapshots into the five report categories."""
    return {
        "headers": _diff_headers(stored["headers"], fresh["headers"]),
        "bodies": _diff_parts(stored["bodies"], fresh["bodies"], _BODY_SCALAR_FIELDS, _BODY_TEXT_FIELDS),
        "attachments": _diff_parts(stored["attachments"], fresh["attachments"], _ATTACHMENT_FIELDS),
        "defects": _diff_defects(stored["defects"], fresh["defects"]),
        "references": _diff_references(stored["references"], fresh["references"]),
    }


def empty_diff() -> dict[str, Any]:
    return {category: [] for category in DIFF_CATEGORIES}


def diff_digest(diff: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(diff).encode("utf-8")).hexdigest()


# -- service -----------------------------------------------------------------
class PreviewService:
    """Build and persist parse-comparison previews (never re-ingests)."""

    def __init__(self, repo: Repository, raw_storage: ControlledStorage) -> None:
        self._repo = repo
        self._raw = raw_storage

    def _read_raw(self, raw_path: str | None) -> tuple[bytes | None, str | None]:
        """Return (data, reason). Exactly one of the two is set."""
        if not raw_path:
            return None, "no raw EML was stored for this ingest"
        try:
            path = self._raw.resolve(raw_path)
        except StorageError:
            # A tampered stored path must not be followed outside the root.
            log.error("preview rejected stored raw path: %r", raw_path)
            return None, "stored raw path is invalid"
        if not path.is_file():
            return None, "raw EML file is missing on disk"
        return path.read_bytes(), None

    def create_preview(self, ingest_id: int) -> dict[str, Any] | None:
        """Preview one ingest; ``None`` if the ingest does not exist.

        The archived facts are only read. The outcome (previewable or not) is
        appended to the parse_previews audit table and returned.
        """
        ingest = self._repo.get_ingest(ingest_id)
        if ingest is None:
            return None
        message_pk = ingest.get("message_pk")

        record: dict[str, Any] = {
            "ingest_id": ingest_id,
            "message_pk": message_pk,
            "parser_version": PARSER_VERSION,
            "raw_sha256": None,
            "raw_size": None,
            "previewable": False,
            "reason": None,
            "reparsed_status": None,
            "diff": empty_diff(),
            "diff_sha256": None,
        }

        reason: str | None = None
        data: bytes | None = None
        if message_pk is None:
            reason = "ingest has no parsed message (status=failed)"
        else:
            data, reason = self._read_raw(ingest.get("raw_path"))

        if data is not None:
            record["raw_sha256"] = hashlib.sha256(data).hexdigest()
            record["raw_size"] = len(data)
            if record["raw_sha256"] != ingest["raw_sha256"]:
                # Bytes on disk are not the archived original: refuse to diff
                # against the wrong source rather than produce a bogus diff.
                log.error(
                    "preview digest mismatch ingest=%d archived=%s on_disk=%s",
                    ingest_id,
                    ingest["raw_sha256"],
                    record["raw_sha256"],
                )
                data, reason = None, "raw bytes on disk do not match the archived digest"

        if data is not None:
            message = self._repo.get_message(message_pk)
            if message is None:  # pragma: no cover - defensive
                data, reason = None, "archived message row is missing"

        if data is not None:
            parsed = parse_eml(data)
            stored = _stored_snapshot(
                message,
                self._repo.get_message_headers(message_pk),
                self._repo.get_message_identifiers(message_pk),
                ingest["defects"],
            )
            fresh = _fresh_snapshot(parsed)
            diff = diff_snapshots(stored, fresh)
            record.update(
                previewable=True,
                reparsed_status=parsed.status.value,
                diff=diff,
                diff_sha256=diff_digest(diff),
            )
        else:
            record["reason"] = reason
            log.info("ingest %d not previewable: %s", ingest_id, reason)

        return self._repo.save_parse_preview(record)
