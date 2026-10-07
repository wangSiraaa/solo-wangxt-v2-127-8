"""End-to-end checks against the real PostgreSQL schema/SQL.

These run only when EMLARCH_RUN_PG_TESTS=1 and the test DSN is reachable.
They re-ingest representative samples and exercise SQL that has no in-memory
equivalent (JSONB aggregates, LATERAL identifier rollups, ILIKE joins).
"""
import pytest

from conftest import SAMPLES

pytestmark = pytest.mark.pg


def _post(c, name, **params):
    data = (SAMPLES / name).read_bytes()
    return c.post(
        "/ingest",
        files={"file": (name, data, "message/rfc822")},
        params=params,
    )


def test_health_reports_pg(pg_client):
    c, _ = pg_client
    assert c.get("/health").json()["backend"] == "postgresql"


def test_schema_persists_nested_multipart(pg_client):
    c, _ = pg_client
    r = _post(c, "01_multibyte.eml")
    assert r.status_code == 201, r.text
    pk = r.json()["message_pk"]
    msg = c.get(f"/messages/{pk}").json()
    assert msg["message_id"] == "multi-01@example.com"
    assert msg["tree_json"]["content_type"] == "multipart/mixed"
    # all four textual parts (incl. embedded message/rfc822 body)
    ctypes = sorted(b["content_type"] for b in msg["bodies"])
    assert ctypes.count("text/plain") == 3
    assert "text/html" in ctypes
    # binary attachments: inline gif + pdf
    atts = {(a["mime_path"], a["stored"]) for a in msg["attachments"]}
    assert ("1.3.2", True) in atts
    assert ("3", True) in atts
    # provenance: ingest id links raw digest to parsed result
    ing = c.get(f"/ingests/{msg['ingest_id']}").json()
    assert ing["raw_sha256"] == msg["raw_sha256"]


def test_failed_and_defective_rows_queryable(pg_client):
    c, _ = pg_client
    _post(c, "06_corrupt_boundary.eml")
    _post(c, "03_missing_id.eml")
    fails = c.get("/failures").json()
    assert len(fails) == 2
    # defect stages are populated for location
    assert all(d["stage"] for f in fails for d in f["defects"])


def test_threading_sql_roundtrip(pg_client):
    c, _ = pg_client
    for n in ["02_cycle_a.eml", "02_cycle_b.eml", "04_duplicate_id_a.eml",
              "04_duplicate_id_b.eml", "05_same_subject_root.eml",
              "05_same_subject_other.eml", "01_multibyte.eml"]:
        _post(c, n, recompute_threads=False)
    rebuilt = c.post("/threads/rebuild").json()
    assert rebuilt["threads"] >= 4
    assert any({"cycle-a@example.com", "cycle-b@example.com"} <= set(cyc)
               for cyc in rebuilt["cycles"])
    assert "dup-1@example.com" in rebuilt["duplicate_ids"]
    assert any(w["reason"] == "subject_match_only" for w in rebuilt["weak_suggestions"])

    # thread detail via LATERAL identifier rollup
    threads = c.get("/threads").json()
    cycle_thread = next(t for t in threads if t["message_count"] == 2)
    detail = c.get(f"/threads/{cycle_thread['thread_key']}").json()
    assert len(detail["messages"]) == 2
    # multibyte message is in parent-root thread, with refs rollups present
    multi = c.get("/search", params={"q": "multi-01"}).json()["results"][0]
    md = c.get(f"/messages/{multi['id']}").json()
    assert md["thread_key"]


def test_search_sql_joins(pg_client):
    c, _ = pg_client
    _post(c, "01_multibyte.eml")
    _post(c, "09_html_xss.eml")
    assert c.get("/search", params={"q": "GB18030"}).json()["count"] == 1
    assert c.get("/search", params={"q": "café"}).json()["count"] >= 1
    # header value search (From)
    assert c.get("/search", params={"q": "sigs@example.com"}).json()["count"] == 1
    assert c.get("/search", params={"q": "nothing-matches-zzz"}).json()["count"] == 0


def test_idempotent_schema_init(pg_client):
    c, arch = pg_client
    # creating a second repository over the same DSN must not error on DDL
    arch.repo.init_schema()
    assert c.get("/health").status_code == 200


def test_reparse_preview_pg_idempotent_and_read_only(pg_client):
    c, arch = pg_client
    r = _post(c, "01_multibyte.eml")
    pk = r.json()["message_pk"]
    ingest_id = r.json()["ingest_id"]
    msg_before = c.get(f"/messages/{pk}").json()
    ing_before = c.get(f"/ingests/{ingest_id}").json()
    assert ing_before.get("parser_version")

    p1 = c.post(f"/messages/{pk}/reparse-preview")
    assert p1.status_code == 200, p1.text
    j1 = p1.json()
    assert j1["status"] == "previewable"
    assert j1["diff"]["identical"] is True
    assert j1["raw"]["digest_matches"] is True
    j2 = c.post(f"/messages/{pk}/reparse-preview").json()
    # stable identity and diff for the same (original, parser version)
    assert j2["id"] == j1["id"]
    assert j2["created_at"] == j1["created_at"]
    assert j2["diff"] == j1["diff"]

    # read-only: no new messages/ingests; paths and threads untouched
    assert c.get(f"/messages/{pk}").json()["raw_path"] == msg_before["raw_path"]
    assert c.get("/messages").json() and len(c.get("/messages").json()) == 1
    assert c.get(f"/ingests/{ingest_id}").json()["raw_sha256"] == ing_before["raw_sha256"]

    # query interfaces
    listed = c.get("/reparse-previews").json()
    assert len(listed) == 1 and listed[0]["id"] == j1["id"]
    assert c.get(f"/messages/{pk}/reparse-preview").json()["id"] == j1["id"]


def test_reparse_preview_pg_raw_missing(pg_client):
    c, arch = pg_client
    r = _post(c, "01_multibyte.eml")
    pk = r.json()["message_pk"]
    raw_path = c.get(f"/messages/{pk}").json()["raw_path"]
    arch.raw_storage.resolve(raw_path).unlink()

    pv = c.post(f"/messages/{pk}/reparse-preview").json()
    assert pv["status"] == "raw_missing"
    assert pv["diff"] is None
    assert pv["raw"]["archived_sha256"] == r.json()["raw_sha256"]
    # archived metadata still present and searchable
    assert c.get("/search", params={"q": "multi-01"}).json()["count"] == 1
    # stable row across repeated attempts
    assert c.post(f"/messages/{pk}/reparse-preview").json()["id"] == pv["id"]
