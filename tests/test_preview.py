"""Acceptance tests for the parse-comparison preview (解析对照预览).

A preview re-parses the stored raw EML with the current parser and records the
difference against the archived facts — it is *not* a re-ingest: ingest ids,
download paths and thread assignments must remain untouched.
"""
from dataclasses import replace
from datetime import datetime, timezone

from conftest import SAMPLES

from app.parser import PARSER_VERSION
from app.parser.models import Defect

DIFF_CATEGORIES = ("headers", "bodies", "attachments", "defects", "references")

EML_WITH_PARTS = (
    "From: Alice <alice@example.com>\r\n"
    "To: Bob <bob@example.com>\r\n"
    "Subject: Old report\r\n"
    "Date: Mon, 29 Sep 2025 07:00:00 +0000\r\n"
    "Message-ID: <old-1@example.com>\r\n"
    "References: <root-0@example.com>\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: multipart/mixed; boundary=BB\r\n"
    "\r\n"
    "--BB\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "hello body\r\n"
    "--BB\r\n"
    "Content-Type: application/octet-stream\r\n"
    "Content-Disposition: attachment; filename=data.bin\r\n"
    "Content-Transfer-Encoding: base64\r\n"
    "\r\n"
    "AAECAwQ=\r\n"
    "--BB--\r\n"
).encode()


def _post(client, name, data, **params):
    return client.post(
        "/ingest",
        files={"file": (name, data, "message/rfc822")},
        params=params,
    )


def _ingest(client, name):
    r = _post(client, name, (SAMPLES / name).read_bytes())
    assert r.status_code == 201, r.text
    return r.json()


def test_preview_empty_diff_is_stable_and_has_no_side_effects(client):
    c, arch = client
    # a thread pair plus a message with bodies/attachments/cids
    _ingest(c, "05_same_subject_root.eml")
    _ingest(c, "05_same_subject_other.eml")
    target = _ingest(c, "01_multibyte.eml")
    ingest_id, pk = target["ingest_id"], target["message_pk"]

    # snapshot the world the preview must not disturb
    before = {
        "messages": c.get("/messages").json(),
        "search": c.get("/search", params={"q": "GB18030"}).json(),
        "threads": c.get("/threads").json(),
        "message": c.get(f"/messages/{pk}").json(),
        "ingest": c.get(f"/ingests/{ingest_id}").json(),
    }
    att = before["message"]["attachments"][0]
    dl_before = c.get(f"/messages/{pk}/attachments/{att['id']}/download")

    # same parser -> no differences, but a full audit record
    p1 = c.post(f"/ingests/{ingest_id}/parse-preview")
    assert p1.status_code == 201, p1.text
    p1 = p1.json()
    assert p1["previewable"] is True
    assert p1["reason"] is None
    assert p1["parser_version"] == PARSER_VERSION
    assert p1["raw_sha256"] == target["raw_sha256"]
    assert p1["raw_size"] == target["raw_size"]
    assert p1["reparsed_status"] == target["status"]
    assert set(p1["diff"]) == set(DIFF_CATEGORIES)
    assert all(p1["diff"][k] == [] for k in DIFF_CATEGORIES)
    assert p1["diff_sha256"]

    # repeated preview of the same original -> identical diff (stable)
    p2 = c.post(f"/ingests/{ingest_id}/parse-preview").json()
    assert p2["id"] != p1["id"]
    assert p2["diff"] == p1["diff"]
    assert p2["diff_sha256"] == p1["diff_sha256"]

    # query interface: list per ingest (newest first) + fetch by id
    listed = c.get(f"/ingests/{ingest_id}/parse-previews").json()
    assert [p["id"] for p in listed] == [p2["id"], p1["id"]]
    one = c.get(f"/parse-previews/{p1['id']}")
    assert one.status_code == 200
    assert one.json()["diff_sha256"] == p1["diff_sha256"]

    # nothing about the archive changed: no re-ingest, same ids, same threads
    assert c.get("/messages").json() == before["messages"]
    assert c.get("/search", params={"q": "GB18030"}).json() == before["search"]
    assert c.get("/threads").json() == before["threads"]
    assert c.get(f"/messages/{pk}").json() == before["message"]
    assert c.get(f"/ingests/{ingest_id}").json() == before["ingest"]
    dl_after = c.get(f"/messages/{pk}/attachments/{att['id']}/download")
    assert dl_after.status_code == 200
    assert dl_after.content == dl_before.content
    assert dl_after.headers["content-disposition"] == dl_before.headers["content-disposition"]


def test_preview_reflects_parser_upgrade(client, monkeypatch):
    c, _ = client
    r = _post(c, "old.eml", EML_WITH_PARTS)
    ingest_id = r.json()["ingest_id"]

    from app.parser import parse_eml as real_parse

    def upgraded_parser(data: bytes):
        parsed = real_parse(data)
        parsed.headers[0] = replace(parsed.headers[0], value="rewritten")
        parsed.message_id = "renamed-1@example.com"
        parsed.references.append("extra-ref@example.com")
        parsed.bodies[0].charset = "utf-16"
        parsed.attachments[0].filename = "renamed.bin"
        parsed.defects.append(Defect(stage="0", level="NewCheck", message="new parser finding"))
        return parsed

    monkeypatch.setattr("app.preview.parse_eml", upgraded_parser)
    monkeypatch.setattr("app.preview.PARSER_VERSION", "eml-parser/2.0.0")

    p = c.post(f"/ingests/{ingest_id}/parse-preview").json()
    assert p["previewable"] is True
    assert p["parser_version"] == "eml-parser/2.0.0"
    diff = p["diff"]
    assert any(ch["kind"] == "changed" and ch["field"] == "value" for ch in diff["headers"])
    assert any(ch["kind"] == "changed" and ch["field"] == "charset" for ch in diff["bodies"])
    assert any(ch["kind"] == "changed" and ch["field"] == "filename" for ch in diff["attachments"])
    assert diff["defects"] == [
        {"kind": "added", "stage": "0", "level": "NewCheck", "message": "new parser finding"}
    ]
    refs = {(ch["kind"], ch["field"]) for ch in diff["references"]}
    assert ("changed", "message_id") in refs
    assert ("added", "references") in refs

    # stable under repetition with the same (upgraded) parser
    p2 = c.post(f"/ingests/{ingest_id}/parse-preview").json()
    assert p2["diff"] == p["diff"]
    assert p2["diff_sha256"] == p["diff_sha256"]


def test_preview_missing_raw_is_marked_unpreviewable_and_metadata_survives(client):
    c, arch = client
    target = _ingest(c, "03_missing_id.eml")
    ingest_id, pk = target["ingest_id"], target["message_pk"]
    msg_before = c.get(f"/messages/{pk}").json()
    ing_before = c.get(f"/ingests/{ingest_id}").json()
    search_before = c.get("/search", params={"q": "Draft with no identity"}).json()

    # remove the original bytes from the controlled raw store
    raw_path = ing_before["raw_path"]
    assert arch.raw_storage.resolve(raw_path).unlink() is None

    p = c.post(f"/ingests/{ingest_id}/parse-preview")
    assert p.status_code == 201
    p = p.json()
    assert p["previewable"] is False
    assert "missing" in p["reason"]
    assert p["raw_sha256"] is None
    assert p["reparsed_status"] is None
    assert p["diff_sha256"] is None
    assert all(p["diff"][k] == [] for k in DIFF_CATEGORIES)

    # the not-previewable record is queryable ...
    assert c.get(f"/parse-previews/{p['id']}").json()["previewable"] is False
    assert len(c.get(f"/ingests/{ingest_id}/parse-previews").json()) == 1

    # ... and the archived metadata is untouched
    assert c.get(f"/messages/{pk}").json() == msg_before
    assert c.get(f"/ingests/{ingest_id}").json() == ing_before
    assert c.get("/search", params={"q": "Draft with no identity"}).json() == search_before


def test_preview_failed_ingest_is_unpreviewable(client):
    """An ingest that failed at parse time stored no raw EML and no message."""
    c, arch = client
    if not hasattr(arch.repo, "ingests"):  # memory backend only
        return
    # seed a failed ingest row directly (the API rejects empty uploads, and
    # the tolerant stdlib parser rarely reaches status=failed via HTTP)
    arch.repo.ingests[99] = {
        "id": 99,
        "received_at": datetime.now(timezone.utc),
        "source_name": "broken.eml",
        "status": "failed",
        "raw_sha256": "0" * 64,
        "raw_size": 3,
        "raw_path": None,
        "fatal_error": "empty input: no RFC822 message present",
        "defect_count": 1,
    }
    p = c.post("/ingests/99/parse-preview").json()
    assert p["previewable"] is False
    assert "failed" in p["reason"]
    assert p["raw_sha256"] is None


def test_preview_rejects_tampered_raw_path(client):
    c, arch = client
    target = _ingest(c, "03_missing_id.eml")
    ingest_id = target["ingest_id"]
    if hasattr(arch.repo, "ingests"):  # memory backend: emulate a tampered DB row
        arch.repo.ingests[ingest_id]["raw_path"] = "../../etc/passwd"
        p = c.post(f"/ingests/{ingest_id}/parse-preview").json()
        assert p["previewable"] is False
        assert "invalid" in p["reason"]


def test_preview_rejects_digest_mismatch(client):
    c, arch = client
    target = _ingest(c, "03_missing_id.eml")
    ingest_id = target["ingest_id"]
    raw_path = c.get(f"/ingests/{ingest_id}").json()["raw_path"]
    # someone replaced the bytes on disk: not the archived original anymore
    arch.raw_storage.resolve(raw_path).write_bytes(b"From: x@y\r\n\r\nforged\r\n")
    p = c.post(f"/ingests/{ingest_id}/parse-preview").json()
    assert p["previewable"] is False
    assert "digest" in p["reason"]


def test_preview_unknown_ids_404(client):
    c, _ = client
    assert c.post("/ingests/9999/parse-preview").status_code == 404
    assert c.get("/ingests/9999/parse-previews").status_code == 404
    assert c.get("/parse-previews/9999").status_code == 404
