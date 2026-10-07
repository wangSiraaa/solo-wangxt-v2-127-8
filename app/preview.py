"""Reparse-comparison previews (read-only; never a re-ingest).

A preview re-reads the *saved original EML bytes* from the controlled raw
storage and runs the **current** parser over them, then diffs the fresh result
against the facts stored at ingest time. Nothing about the preview mutates
archive state:

* no ingest row is created (the original ingest id is only referenced);
* no attachment is written and no stored download path changes;
* threads are **not** rebuilt or reassigned — the threading section only
  projects what the freshly parsed reference headers *would* contribute.

The diff is grouped into five sections per the archive requirements:
``headers`` (every header except the three identity headers), ``bodies``,
``attachments``, ``defects`` and ``thread_references``.

Every comparison goes through the canonical-view helpers below so that running
the same bytes through the same parser version yields a byte-stable diff.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

from app.parser import PARSER_VERSION, parse_eml
from app.parser.models import ParseStatus, ParsedMessage
from app.storage import ControlledStorage, StorageError
from app.threads import ThreadInput, compute_threads

log = logging.getLogger("emlarchive.preview")

# Identity headers live in the thread_references section, not headers.
THREAD_HEADER_NAMES = {"message-id", "references", "in-reply-to"}

# Long text payloads are clipped inside diffs: previews describe deltas, they
# do not re-store full bodies.
_MAX_TEXT_IN_DIFF = 2000

STATUS_PREVIEWABLE = "previewable"
STATUS_RAW_MISSING = "raw_missing"
STATUS_PARSE_FAILED = "parse_failed"


class PreviewError(Exception):
    """The stored message a preview was requested for does not exist."""


# ---------------------------------------------------------------------------
# canonical views
# ---------------------------------------------------------------------------

def _clip(text: str | None) -> str:
    if text is None:
        return ""
    if len(text) <= _MAX_TEXT_IN_DIFF:
        return text
    return text[:_MAX_TEXT_IN_DIFF] + f"\n…[truncated {len(text) - _MAX_TEXT_IN_DIFF} chars]"


def parsed_header_views(parsed: ParsedMessage) -> list[dict[str, Any]]:
    """All non-identity headers in archive ordinal order."""
    return [
        {"ordinal": h.ordinal, "name": h.name, "value": h.value}
        for h in parsed.headers
        if h.name.lower() not in THREAD_HEADER_NAMES
    ]


def archived_header_views(headers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stored headers (repo rows) in archive ordinal order, identity excluded."""
    return [
        {"ordinal": h["ordinal"], "name": h["name"], "value": h["value"]}
        for h in sorted(headers, key=lambda r: r["ordinal"])
        if h["name"].lower() not in THREAD_HEADER_NAMES
    ]


def _header_occurrence_keys(views: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    """Index header occurrences as (lowercased name, per-name occurrence).

    Matching on occurrence (rather than global ordinal) keeps an inserted or
    removed extra header from shifting every later header into a false
    ``changed`` row; ordinals are kept for display only.
    """
    seen: dict[str, int] = {}
    indexed: dict[tuple[str, int], dict[str, Any]] = {}
    for h in sorted(views, key=lambda r: r["ordinal"]):
        key_name = h["name"].lower()
        occ = seen.get(key_name, 0)
        seen[key_name] = occ + 1
        indexed[(key_name, occ)] = h
    return indexed


def _diff_header_lists(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare header occurrences matched by (lowercased name, occurrence #)."""
    old_by = _header_occurrence_keys(old)
    new_by = _header_occurrence_keys(new)
    added, removed, changed = [], [], []
    for key in sorted(new_by.keys() - old_by.keys()):
        h = new_by[key]
        added.append({"ordinal": h["ordinal"], "name": h["name"], "value": _clip(h["value"])})
    for key in sorted(old_by.keys() - new_by.keys()):
        h = old_by[key]
        removed.append({"ordinal": h["ordinal"], "name": h["name"], "value": _clip(h["value"])})
    for key in sorted(old_by.keys() & new_by.keys()):
        o, n = old_by[key], new_by[key]
        if o["value"] != n["value"]:
            changed.append(
                {
                    "ordinal": n["ordinal"],
                    "name": n["name"],
                    "archived": _clip(o["value"]),
                    "current": _clip(n["value"]),
                }
            )
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "different": bool(added or removed or changed),
    }


def parsed_body_views(parsed: ParsedMessage) -> dict[str, dict[str, Any]]:
    views = {}
    for b in parsed.bodies:
        is_html = b.content_type == "text/html"
        views[b.mime_path] = {
            "mime_path": b.mime_path,
            "content_type": b.content_type,
            "charset": b.charset,
            "declared_charset": b.declared_charset,
            "disposition": b.disposition.value,
            "content_id": b.content_id,
            "content_location": b.content_location,
            "byte_size": b.byte_size,
            "text": _clip(b.plain_text if not is_html else None),
            "safe_html": _clip(b.safe_html if is_html else None),
            "referenced_cids": list(b.referenced_cids),
        }
    return views


def archived_body_views(bodies: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    views = {}
    for b in bodies:
        is_html = b["content_type"] == "text/html"
        views[b["mime_path"]] = {
            "mime_path": b["mime_path"],
            "content_type": b["content_type"],
            "charset": b.get("charset"),
            "declared_charset": b.get("declared_charset"),
            "disposition": b.get("disposition"),
            "content_id": b.get("content_id"),
            "content_location": b.get("content_location"),
            "byte_size": b.get("byte_size"),
            "text": _clip(b.get("text") if not is_html else None),
            "safe_html": _clip(b.get("safe_html") if is_html else None),
            "referenced_cids": list(b.get("referenced_cids") or []),
        }
    return views


_ATT_VIEW_FIELDS = (
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


def parsed_attachment_views(parsed: ParsedMessage) -> dict[str, dict[str, Any]]:
    return {
        a.mime_path: {"mime_path": a.mime_path, **{f: getattr(a, f) for f in _ATT_VIEW_FIELDS}}
        for a in parsed.attachments
    }


def archived_attachment_views(attachments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        a["mime_path"]: {"mime_path": a["mime_path"], **{f: a.get(f) for f in _ATT_VIEW_FIELDS}}
        for a in attachments
    }


def _diff_keyed_views(
    old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]], *, label: str
) -> dict[str, Any]:
    added, removed, changed = [], [], []
    for key in sorted(new.keys() - old.keys()):
        added.append(new[key])
    for key in sorted(old.keys() - new.keys()):
        removed.append(old[key])
    for key in sorted(old.keys() & new.keys()):
        fields = {
            field: {"archived": old[key].get(field), "current": new[key].get(field)}
            for field in sorted(set(old[key]) | set(new[key]))
            if field != "mime_path" and old[key].get(field) != new[key].get(field)
        }
        if fields:
            changed.append({"mime_path": key, "fields": fields})
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "different": bool(added or removed or changed),
        "label": label,
    }


def _defect_counts(defects: list[Any]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for d in defects:
        stage = d.stage if hasattr(d, "stage") else d["stage"]
        level = d.level if hasattr(d, "level") else d["level"]
        key = (stage, level)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _defect_breakdown(counts: dict[tuple[str, str], int]) -> list[dict[str, Any]]:
    return [
        {"stage": stage, "level": level, "count": count}
        for (stage, level), count in sorted(counts.items())
    ]


def _diff_defects(archived: list[dict[str, Any]], parsed: ParsedMessage) -> dict[str, Any]:
    old_counts = _defect_counts(archived)
    new_counts = _defect_counts(parsed.defects)
    added, removed = [], []
    for key in sorted(set(old_counts) | set(new_counts)):
        o, n = old_counts.get(key, 0), new_counts.get(key, 0)
        if n > o:
            added.append({"stage": key[0], "level": key[1], "count": n - o})
        elif o > n:
            removed.append({"stage": key[0], "level": key[1], "count": o - n})
    return {
        "archived_count": len(archived),
        "current_count": len(parsed.defects),
        "added": added,
        "removed": removed,
        "archived": _defect_breakdown(old_counts),
        "current": _defect_breakdown(new_counts),
        "different": old_counts != new_counts,
    }


def parsed_thread_view(parsed: ParsedMessage) -> dict[str, Any]:
    return {
        "message_id": parsed.message_id,
        "references": list(parsed.references),
        "in_reply_to": list(parsed.in_reply_to),
    }


def archived_thread_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    idents = snapshot.get("identifiers") or []
    refs = [i["value"] for i in idents if i["kind"] == "references"]
    irt = [i["value"] for i in idents if i["kind"] == "in_reply_to"]
    return {"message_id": snapshot.get("message_id"), "references": refs, "in_reply_to": irt}


def _diff_tokens(old: list[str], new: list[str]) -> dict[str, Any]:
    old_norm = [t.lower() for t in old]
    new_norm = [t.lower() for t in new]
    return {
        "archived": list(old),
        "current": list(new),
        "added": sorted(set(new_norm) - set(old_norm)),
        "removed": sorted(set(old_norm) - set(new_norm)),
        "reordered": sorted(old_norm) == sorted(new_norm) and old_norm != new_norm,
        "different": old_norm != new_norm,
    }


def _diff_thread_references(snapshot: dict[str, Any], parsed: ParsedMessage) -> dict[str, Any]:
    old = archived_thread_view(snapshot)
    new = parsed_thread_view(parsed)
    mid_changed = (old["message_id"] or None) != (new["message_id"] or None)
    refs = _diff_tokens(old["references"], new["references"])
    irt = _diff_tokens(old["in_reply_to"], new["in_reply_to"])
    return {
        "message_id": {
            "archived": old["message_id"],
            "current": new["message_id"],
            "changed": bool(mid_changed),
        },
        "references": refs,
        "in_reply_to": irt,
        "different": bool(mid_changed or refs["different"] or irt["different"]),
        # Projection only: the stored thread_key is never rewritten by a preview.
        "archived_thread_key": snapshot.get("thread_key"),
        "projected": project_threading(snapshot, parsed),
    }


# ---------------------------------------------------------------------------
# read-only threading projection (no stored thread is touched)
# ---------------------------------------------------------------------------

def _timestamp(date: Any) -> float | None:
    return date.timestamp() if isinstance(date, datetime) else None


def project_threading(snapshot: dict[str, Any], parsed: ParsedMessage) -> dict[str, Any]:
    """Recompute what the thread graph would look like with the fresh headers.

    Every other stored message keeps its archived identity tokens; only the
    previewed message contributes the freshly parsed tokens. The computation is
    pure (``app.threads.compute_threads``) over an in-memory snapshot list, so
    stored ``thread_key`` assignments are left exactly as they were.
    """
    target_pk = snapshot["id"]
    inputs: list[ThreadInput] = [
        ThreadInput(
            m["id"],
            m.get("message_id"),
            list(m.get("references") or []),
            list(m.get("in_reply_to") or []),
            m.get("subject"),
            _timestamp(m.get("date")),
        )
        for m in snapshot.get("other_messages") or []
    ]
    inputs.append(
        ThreadInput(
            target_pk,
            parsed.message_id,
            list(parsed.references),
            list(parsed.in_reply_to),
            parsed.subject,
            _timestamp(parsed.date),
        )
    )
    result = compute_threads(inputs)
    projected_key = result.thread_of.get(target_pk)
    archived_key = snapshot.get("thread_key")
    projected_members = sorted(pk for pk, key in result.thread_of.items() if key == projected_key)
    if archived_key:
        archived_members = sorted(
            {target_pk}
            | {
                m["id"]
                for m in snapshot.get("other_messages") or []
                if m.get("thread_key") == archived_key
            }
        )
    else:
        archived_members = [target_pk]
    fresh_tokens = {t.lower() for t in (parsed.references + parsed.in_reply_to)}
    fresh_mid = (parsed.message_id or "").lower()
    # Membership is the source of truth: key renames without a merge/split do
    # not change how the message is threaded, and never touch stored keys.
    would_change = set(archived_members) != set(projected_members)
    return {
        "thread_key": projected_key,
        "archived_thread_key": archived_key,
        "would_change_thread": would_change,
        "archived_thread_members": archived_members,
        "projected_thread_members": projected_members,
        "members_added": sorted(set(projected_members) - set(archived_members)),
        "members_removed": sorted(set(archived_members) - set(projected_members)),
        "duplicate_ids_involved": sorted(
            mid
            for mid, pks in result.duplicate_ids.items()
            if target_pk in pks or mid in fresh_tokens or mid == fresh_mid
        ),
    }


# ---------------------------------------------------------------------------
# top-level comparison
# ---------------------------------------------------------------------------

def build_diff(snapshot: dict[str, Any], parsed: ParsedMessage) -> dict[str, Any]:
    """Diff freshly parsed facts against the archived snapshot facts."""
    sections = {
        "headers": _diff_header_lists(
            archived_header_views(snapshot.get("headers") or []),
            parsed_header_views(parsed),
        ),
        "bodies": _diff_keyed_views(
            archived_body_views(snapshot.get("bodies") or []),
            parsed_body_views(parsed),
            label="bodies",
        ),
        "attachments": _diff_keyed_views(
            archived_attachment_views(snapshot.get("attachments") or []),
            parsed_attachment_views(parsed),
            label="attachments",
        ),
        "defects": _diff_defects(snapshot.get("defects") or [], parsed),
        "thread_references": _diff_thread_references(snapshot, parsed),
    }
    return {
        "sections": sections,
        "different_sections": [name for name, sec in sections.items() if sec.get("different")],
        "identical": not any(sec.get("different") for sec in sections.values()),
    }


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

class ReparsePreviewService:
    """Create/query reparse-comparison previews without re-ingesting."""

    def __init__(self, repo: Any, raw_storage: ControlledStorage) -> None:
        self._repo = repo
        self._raw = raw_storage

    def _load_snapshot(self, message_pk: int) -> dict[str, Any] | None:
        snapshot = self._repo.get_message_snapshot(message_pk)
        if snapshot is None:
            return None
        snapshot["other_messages"] = self._repo.thread_inputs()
        return snapshot

    def _raw_bytes(self, snapshot: dict[str, Any]) -> tuple[bytes | None, str | None]:
        """Read the saved original bytes; resolve failures to (None, reason)."""
        rel = snapshot.get("raw_path")
        if not rel:
            return None, "no raw_path recorded for ingest"
        try:
            path = self._raw.resolve(rel)
        except StorageError as exc:
            log.warning("preview raw path rejected message_pk=%d: %s", snapshot["id"], exc)
            return None, f"stored raw path rejected: {exc}"
        if not path.is_file():
            return None, "saved original EML is missing from the raw storage"
        try:
            return path.read_bytes(), None
        except OSError as exc:
            log.warning("preview raw read failed message_pk=%d: %s", snapshot["id"], exc)
            return None, f"saved original EML unreadable: {exc}"

    def _record(
        self,
        snapshot: dict[str, Any],
        *,
        status: str,
        raw: dict[str, Any],
        diff: dict[str, Any] | None,
    ) -> dict[str, Any]:
        record = {
            "message_pk": snapshot["id"],
            "ingest_id": snapshot.get("ingest_id"),
            "archived_parser_version": snapshot.get("parser_version"),
            "parser_version": PARSER_VERSION,
            "status": status,
            "raw": raw,
            "diff": diff,
            # Idempotency key component on the repo side.
            "raw_sha256": snapshot.get("raw_sha256"),
        }
        return self._repo.save_reparse_preview(record)

    def preview_message(self, message_pk: int) -> dict[str, Any]:
        """Run (or return the stable existing) reparse preview for one message.

        Idempotent: same stored bytes + same parser version always resolve to
        one stored preview row with a stable diff.
        """
        snapshot = self._load_snapshot(message_pk)
        if snapshot is None:
            raise PreviewError(f"message {message_pk} not found")

        data, reason = self._raw_bytes(snapshot)
        if data is None:
            return self._record(
                snapshot,
                status=STATUS_RAW_MISSING,
                raw={
                    "archived_sha256": snapshot.get("raw_sha256"),
                    "archived_size": snapshot.get("raw_size"),
                    "current_sha256": None,
                    "current_size": None,
                    "digest_matches": False,
                    "path": snapshot.get("raw_path"),
                    "available": False,
                    "reason": reason,
                },
                diff=None,
            )

        current_sha = hashlib.sha256(data).hexdigest()
        archived_sha = snapshot.get("raw_sha256")
        raw_summary = {
            "archived_sha256": archived_sha,
            "archived_size": snapshot.get("raw_size"),
            "current_sha256": current_sha,
            "current_size": len(data),
            "digest_matches": current_sha == archived_sha,
            "path": snapshot.get("raw_path"),
            "available": True,
            "reason": None
            if current_sha == archived_sha
            else "bytes on disk differ from the archived digest",
        }

        parsed = parse_eml(data)
        if parsed.status is ParseStatus.FAILED:
            diff = build_diff(snapshot, parsed)
            diff["fatal_error"] = parsed.fatal_error
            return self._record(
                snapshot, status=STATUS_PARSE_FAILED, raw=raw_summary, diff=diff
            )

        return self._record(
            snapshot, status=STATUS_PREVIEWABLE, raw=raw_summary, diff=build_diff(snapshot, parsed)
        )

    def get_preview(self, preview_id: int) -> dict[str, Any] | None:
        return self._repo.get_reparse_preview(preview_id)

    def get_latest_for_message(self, message_pk: int) -> dict[str, Any] | None:
        return self._repo.get_latest_reparse_preview(message_pk)

    def list_previews(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return self._repo.list_reparse_previews(limit, offset)
