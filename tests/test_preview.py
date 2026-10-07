"""Unit tests for the read-only reparse-comparison engine (app.preview)."""
from __future__ import annotations

from app.parser.models import Defect
from app.preview import (
    archived_attachment_views,
    archived_body_views,
    archived_header_views,
    build_diff,
    parsed_attachment_views,
    parsed_body_views,
    parsed_header_views,
    project_threading,
)
from parser_fixtures import make_parsed, make_snapshot


def _snapshot_from_parsed(parsed, **overrides):
    """Archive snapshot whose facts are exactly the parsed facts."""
    snap = make_snapshot(parsed)
    snap.update(overrides)
    return snap


def test_identical_facts_produce_empty_diff():
    parsed = make_parsed(message_id="a@example.com", subject="Hello")
    snap = _snapshot_from_parsed(parsed)
    diff = build_diff(snap, parsed)
    assert diff["identical"] is True
    assert diff["different_sections"] == []
    for name, section in diff["sections"].items():
        assert section["different"] is False, name


def test_stable_diff_regardless_of_representation_order():
    """Stored facts from the DB may arrive in any order; the diff is stable."""
    parsed_a = make_parsed(message_id="a@example.com", subject="Hello")
    parsed_b = make_parsed(message_id="a@example.com", subject="Hello")
    snap1 = _snapshot_from_parsed(parsed_a)
    snap2 = _snapshot_from_parsed(parsed_b)
    # shuffle stored rows
    snap2["headers"] = list(reversed(snap2["headers"]))
    snap2["bodies"] = list(reversed(snap2["bodies"]))
    snap2["attachments"] = list(reversed(snap2["attachments"]))
    snap2["defects"] = list(reversed(snap2["defects"]))
    assert build_diff(snap1, parsed_a) == build_diff(snap2, parsed_b)


def test_header_diff_excludes_identity_headers():
    parsed = make_parsed(
        message_id="new@example.com",
        references=["r@example.com"],
        subject="Hello",
        extra_headers=[("X-Archived-By", "v1")],
    )
    snap = _snapshot_from_parsed(parsed)
    # simulate the archive having a different Message-ID and an old header
    snap["identifiers"] = [
        {"kind": "message_id", "value": "old@example.com", "ordinal": 0},
        {"kind": "references", "value": "r@example.com", "ordinal": 0},
    ]
    snap["message_id"] = "old@example.com"
    changed_ordinal = next(h.ordinal for h in parsed.headers
                           if h.name.lower() == "x-archived-by")
    snap["headers"] = [
        h for h in snap["headers"] if h["name"].lower() != "x-archived-by"
    ] + [{"ordinal": changed_ordinal,
          "name": "X-Archived-By", "value": "v0", "raw_value": "v0"}]

    diff = build_diff(snap, parsed)
    headers = diff["sections"]["headers"]
    changed_names = {(h["name"], h["ordinal"]) for h in headers["changed"]}
    assert ("X-Archived-By", changed_ordinal) in changed_names
    # identity headers never leak into the header section
    all_names = {h["name"].lower() for h in headers["added"] + headers["removed"] + headers["changed"]}
    assert not (all_names & {"message-id", "references", "in-reply-to"})
    tr = diff["sections"]["thread_references"]
    assert tr["message_id"]["changed"] is True
    assert tr["message_id"]["archived"] == "old@example.com"
    assert tr["message_id"]["current"] == "new@example.com"
    assert diff["identical"] is False


def test_body_diff_detects_charset_and_text_and_classification_changes():
    parsed = make_parsed(body_text="new body text", charset="utf-8")
    snap = _snapshot_from_parsed(parsed)
    # archive stored the part with a fallback charset and different text
    for b in snap["bodies"]:
        b["charset"] = "gb18030"
        b["text"] = "old body text"
        b["plain_text"] = "old body text"

    diff = build_diff(snap, parsed)
    bodies = diff["sections"]["bodies"]
    changed = bodies["changed"][0]
    assert set(changed["fields"]) == {"charset", "text"}

    # part reclassified from body (1) to attachment: appears on both sides
    parsed2 = make_parsed(attachment=True)
    snap2 = _snapshot_from_parsed(make_parsed(attachment=False, body_text="x"))
    diff2 = build_diff(snap2, parsed2)
    assert diff2["sections"]["bodies"]["removed"]
    assert diff2["sections"]["attachments"]["added"]


def test_attachment_diff_compares_metadata_not_storage_paths():
    parsed = make_parsed(attachment=True, att_filename="new.pdf")
    snap = _snapshot_from_parsed(parsed)
    snap["attachments"][0]["filename"] = "old.pdf"
    # storage-side fields the preview must not care about / cannot change
    snap["attachments"][0]["storage_path"] = "ab/kept-on-disk.bin"
    snap["attachments"][0]["stored"] = True

    diff = build_diff(snap, parsed)
    fields = diff["sections"]["attachments"]["changed"][0]["fields"]
    assert set(fields) == {"filename"}


def test_defect_diff_counts_by_stage_and_level():
    parsed = make_parsed(defects=[
        Defect("0", "StartBoundaryNotFoundDefect", "boundary problem"),
        Defect("1.1", "CharsetFallback", "used utf-8"),
    ])
    snap = _snapshot_from_parsed(make_parsed(defects=[
        Defect("0", "StartBoundaryNotFoundDefect", "boundary problem"),
        Defect("0", "MalformedDate", "bad date"),
    ]))
    diff = build_diff(snap, parsed)
    defects = diff["sections"]["defects"]
    assert defects["different"] is True
    added = {(d["stage"], d["level"], d["count"]) for d in defects["added"]}
    removed = {(d["stage"], d["level"], d["count"]) for d in defects["removed"]}
    assert ("1.1", "CharsetFallback", 1) in added
    assert ("0", "MalformedDate", 1) in removed


def test_thread_projection_is_pure_and_detects_merge():
    # Archived world: the target's own id was mis-extracted (no edges).
    parsed = make_parsed(
        message_id="child@example.com",
        references=["root@example.com"],
        subject="Re: topic",
    )
    archived_target = make_parsed(
        message_id="child@example.com",
        references=[],
        subject="Re: topic",
    )
    root = make_parsed(message_id="root@example.com", subject="topic")
    snap = _snapshot_from_parsed(archived_target)
    snap["id"] = 10
    snap["thread_key"] = "thread-child@example.com"
    snap["other_messages"] = [
        {"id": 11, "message_id": "root@example.com", "subject": "topic",
         "date": None, "thread_key": "thread-root@example.com",
         "references": [], "in_reply_to": []}
    ]
    projection = project_threading(snap, parsed)
    assert projection["would_change_thread"] is True
    assert projection["members_added"] == [11]
    assert projection["thread_key"] == "thread-root@example.com"


def test_thread_projection_unchanged_when_references_match():
    parsed = make_parsed(message_id="a@example.com", references=["r@example.com"])
    snap = _snapshot_from_parsed(parsed)
    snap["id"] = 1
    snap["thread_key"] = "thread-r@example.com"
    snap["other_messages"] = [
        {"id": 2, "message_id": "r@example.com", "subject": None,
         "date": None, "thread_key": "thread-r@example.com",
         "references": [], "in_reply_to": []}
    ]
    projection = project_threading(snap, parsed)
    assert projection["would_change_thread"] is False
    assert projection["members_added"] == [] and projection["members_removed"] == []


def test_header_insertion_does_not_cascade_into_changed_headers():
    old = make_parsed(extra_headers=[("X-A", "1"), ("X-B", "2")])
    new = make_parsed(extra_headers=[("X-A", "1"), ("X-NEW", "n"), ("X-B", "2")])
    snap = _snapshot_from_parsed(old)
    diff = build_diff(snap, new)
    headers = diff["sections"]["headers"]
    added = [(h["name"], h["value"]) for h in headers["added"]]
    assert added == [("X-NEW", "n")]
    assert headers["removed"] == [] and headers["changed"] == []


def test_simulated_parser_upgrade_flags_old_fallback_charset():
    """The core use case: old parser fell back; new parser honors declared."""
    old = make_parsed(body_text="mojibake", charset="gb18030")
    new = make_parsed(body_text="正确正文", charset="utf-8")
    # archive was produced with an OLD parser build that recorded a fallback
    for b in old.bodies:
        b.declared_charset = "utf-8"
    snap = _snapshot_from_parsed(old)
    for b in snap["bodies"]:
        b["declared_charset"] = "utf-8"
        b["charset"] = "gb18030"
        b["text"] = "mojibake"
        b["plain_text"] = "mojibake"

    diff = build_diff(snap, new)
    fields = diff["sections"]["bodies"]["changed"][0]["fields"]
    assert {"charset", "text"} <= set(fields)
    assert fields["charset"] == {"archived": "gb18030", "current": "utf-8"}
    assert fields["text"]["current"].endswith("正确正文")
    assert "bodies" in diff["different_sections"]


def test_canonical_views_only_include_safe_fields():
    parsed = make_parsed(attachment=True, att_filename="f.bin")
    av = parsed_attachment_views(parsed)
    assert all("storage_path" not in v and "stored" not in v for v in av.values())
    bv = parsed_body_views(make_parsed(body_text="t"))
    assert all("escaped_html" not in v for v in bv.values())
    # archived views tolerate the extra DB columns
    snap = _snapshot_from_parsed(parsed)
    assert archived_attachment_views(snap["attachments"]).keys() == av.keys()
    assert archived_body_views(_snapshot_from_parsed(make_parsed(body_text="t"))["bodies"])
    assert parsed_header_views(parsed) and archived_header_views(snap["headers"])
