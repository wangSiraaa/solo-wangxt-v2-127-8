"""API acceptance tests for the read-only reparse-comparison preview."""
import json

import pytest

from conftest import SAMPLES


def _post(client, name, data, **params):
    return client.post(
        "/ingest",
        files={"file": (name, data, "message/rfc822")},
        params=params,
    )


def _ingest(c, name="01_multibyte.eml", **params):
    data = (SAMPLES / name).read_bytes()
    r = _post(c, name, data, **params)
    assert r.status_code == 201, r.text
    return r.json(), data


def test_preview_identical_for_current_parser_and_records_versions(client):
    c, _ = client
    body, data = _ingest(c)
    r = c.post(f"/messages/{body['message_pk']}/reparse-preview")
    assert r.status_code == 200, r.text
    pv = r.json()
    assert pv["status"] == "previewable"
    assert pv["parser_version"]
    assert pv["archived_parser_version"] == pv["parser_version"]
    assert pv["ingest_id"] == body["ingest_id"]
    # original-text summary: digest + size of the bytes actually re-read
    assert pv["raw"]["available"] is True
    assert pv["raw"]["digest_matches"] is True
    assert pv["raw"]["current_sha256"] == body["raw_sha256"]
    assert pv["raw"]["current_size"] == len(data)
    assert pv["raw"]["archived_size"] == len(data)
    # current parser re-reading the same archive yields no deltas
    assert pv["diff"]["identical"] is True
    assert pv["diff"]["different_sections"] == []
    assert set(pv["diff"]["sections"]) == {
        "headers", "bodies", "attachments", "defects", "thread_references"
    }


def test_repeated_preview_is_stable_and_idempotent(client):
    c, _ = client
    body, _ = _ingest(c)
    pk = body["message_pk"]
    first = c.post(f"/messages/{pk}/reparse-preview").json()
    second = c.post(f"/messages/{pk}/reparse-preview").json()
    # same stored original + same parser version -> same row, stable diff
    assert first["id"] == second["id"]
    assert first["created_at"] == second["created_at"]
    assert first["diff"] == second["diff"]
    assert first["raw"] == second["raw"]
    # only one preview row exists
    listed = c.get("/reparse-previews").json()
    assert len(listed) == 1 and listed[0]["id"] == first["id"]
    # the diff JSON itself serializes deterministically
    assert json.dumps(first["diff"], sort_keys=True) == json.dumps(second["diff"], sort_keys=True)


def test_preview_is_not_reingest_keeps_ids_paths_and_threads(client):
    c, arch = client
    # two messages referencing a common root so a thread exists
    root, _ = _ingest(c, "01_multibyte.eml")
    child, _ = _ingest(c, "02_cycle_a.eml")
    c.post("/threads/rebuild")

    pk = root["message_pk"]
    before_msg = c.get(f"/messages/{pk}").json()
    before_search = c.get("/search", params={"q": "multi-01"}).json()
    before_threads = c.get("/threads").json()
    before_ingest = c.get(f"/ingests/{root['ingest_id']}").json()
    n_ingests_before = len(c.get("/failures").json())
    child_key_before = c.get(f"/messages/{child['message_pk']}").json()["thread_key"]

    c.post(f"/messages/{pk}/reparse-preview")
    c.post(f"/messages/{pk}/reparse-preview")  # repeated preview

    after_msg = c.get(f"/messages/{pk}").json()
    after_search = c.get("/search", params={"q": "multi-01"}).json()
    after_threads = c.get("/threads").json()
    after_ingest = c.get(f"/ingests/{root['ingest_id']}").json()

    # ingest id / message pk / download paths untouched
    assert after_msg["id"] == before_msg["id"] == pk
    assert after_ingest["id"] == before_ingest["id"]
    assert after_msg["raw_path"] == before_msg["raw_path"]
    assert [a["storage_path"] for a in after_msg["attachments"]] == \
           [a["storage_path"] for a in before_msg["attachments"]]
    # raw bytes still downloadable-equivalent and attachments intact
    assert arch.raw_storage.exists(before_msg["raw_path"])
    # search results and thread assignments unchanged
    assert after_search == before_search
    assert after_threads == before_threads
    assert c.get(f"/messages/{child['message_pk']}").json()["thread_key"] == child_key_before
    # no new ingest rows, no new failure rows from previewing
    assert len(c.get("/failures").json()) == n_ingests_before
    all_messages = c.get("/messages").json()
    assert {m["id"] for m in all_messages} == {root["message_pk"], child["message_pk"]}
    # attachment download still served from the original path
    meta = after_msg["attachments"][0]
    dl = c.get(f"/messages/{pk}/attachments/{meta['id']}/download")
    assert dl.status_code == 200


def test_missing_original_marked_not_previewable_but_metadata_remains(client):
    c, arch = client
    body, _ = _ingest(c)
    pk = body["message_pk"]
    msg = c.get(f"/messages/{pk}").json()
    raw_path = msg["raw_path"]

    # remove the saved original from controlled storage
    abs_path = arch.raw_storage.resolve(raw_path)
    abs_path.unlink()
    assert not arch.raw_storage.exists(raw_path)

    r = c.post(f"/messages/{pk}/reparse-preview")
    assert r.status_code == 200
    pv = r.json()
    assert pv["status"] == "raw_missing"
    assert pv["diff"] is None
    assert pv["raw"]["available"] is False
    assert pv["raw"]["path"] == raw_path
    assert pv["raw"]["reason"]
    # archived summary preserved for provenance despite the missing bytes
    assert pv["raw"]["archived_sha256"] == body["raw_sha256"]
    assert pv["raw"]["archived_size"] == body["raw_size"]

    # old metadata is fully intact and still searchable
    detail = c.get(f"/ingests/{body['ingest_id']}").json()
    assert detail["raw_sha256"] == body["raw_sha256"]
    assert detail["raw_path"] == raw_path
    still = c.get(f"/messages/{pk}").json()
    assert still["raw_sha256"] == body["raw_sha256"]
    assert still["bodies"] and still["attachments"]
    assert c.get("/search", params={"q": "multi-01"}).json()["count"] == 1

    # repeated preview stays raw_missing and stable
    again = c.post(f"/messages/{pk}/reparse-preview").json()
    assert again["id"] == pv["id"] and again["status"] == "raw_missing"
    assert again["diff"] is None and again["raw"] == pv["raw"]

    # restoring the saved original recovers the preview on the SAME row
    restored = arch.raw_storage  # put the bytes back at the recorded shard path
    target = restored.resolve(raw_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((SAMPLES / "01_multibyte.eml").read_bytes())
    recovered = c.post(f"/messages/{pk}/reparse-preview").json()
    assert recovered["id"] == pv["id"]  # identity preserved
    assert recovered["status"] == "previewable"
    assert recovered["raw"]["digest_matches"] is True
    assert recovered["diff"]["identical"] is True
    # query interface returns the marked row
    assert c.get(f"/messages/{pk}/reparse-preview").json()["id"] == pv["id"]
    assert c.get(f"/reparse-previews/{pv['id']}").json()["status"] == "previewable"


def test_preview_query_interfaces_and_404s(client):
    c, _ = client
    body, _ = _ingest(c)
    pk = body["message_pk"]

    # nothing exists yet
    assert c.get(f"/messages/{pk}/reparse-preview").status_code == 404
    assert c.get("/reparse-previews/123").status_code == 404
    # previewing an unknown message is a 404, not an ingest
    assert c.post("/messages/999/reparse-preview").status_code == 404

    pv = c.post(f"/messages/{pk}/reparse-preview").json()
    assert c.get(f"/reparse-previews/{pv['id']}").json()["id"] == pv["id"]
    assert c.get(f"/messages/{pk}/reparse-preview").json()["id"] == pv["id"]
    listed = c.get("/reparse-previews", params={"limit": 10, "offset": 0}).json()
    assert [r["id"] for r in listed] == [pv["id"]]


def test_preview_sections_show_all_five_categories(client):
    c, _ = client
    # defective sample exercises defects + structure; html sample exercises bodies
    body, _ = _ingest(c, "03_missing_id.eml")
    pv = c.post(f"/messages/{body['message_pk']}/reparse-preview").json()
    sections = pv["diff"]["sections"]
    assert sections["defects"]["archived_count"] == sections["defects"]["current_count"]
    assert isinstance(sections["headers"]["changed"], list)
    assert isinstance(sections["bodies"]["added"], list)
    assert isinstance(sections["attachments"]["added"], list)
    tr = sections["thread_references"]
    assert {"message_id", "references", "in_reply_to", "projected"} <= set(tr)
    assert "archived_thread_members" in tr["projected"]
    assert "projected_thread_members" in tr["projected"]


def test_preview_with_thread_merge_scenario_is_projection_only(client):
    c, _ = client
    # ingest child that references a parent; rebuild threads so they are merged
    parent, _ = _ingest(c, "01_multibyte.eml", recompute_threads=False)
    child, child_data = _ingest(c, "02_cycle_a.eml", recompute_threads=False)
    c.post("/threads/rebuild")
    pk = child["message_pk"]
    stored_key = c.get(f"/messages/{pk}").json()["thread_key"]

    pv = c.post(f"/messages/{pk}/reparse-preview").json()
    projected = pv["diff"]["sections"]["thread_references"]["projected"]
    # projection reports members but does not rewrite anything
    assert projected["thread_key"]
    assert stored_key == c.get(f"/messages/{pk}").json()["thread_key"]
    threads_after = c.get("/threads").json()
    assert any(t["thread_key"] == stored_key for t in threads_after)


@pytest.mark.parametrize("sample", ["01_multibyte.eml", "03_missing_id.eml",
                                    "06_corrupt_boundary.eml", "09_html_xss.eml",
                                    "08_traversal.eml", "07_bad_cte.eml"])
def test_preview_stable_across_samples(client, sample):
    c, _ = client
    body, _ = _ingest(c, sample)
    pk = body["message_pk"]
    a = c.post(f"/messages/{pk}/reparse-preview").json()
    b = c.post(f"/messages/{pk}/reparse-preview").json()
    assert a["id"] == b["id"]
    assert a["status"] == b["status"] == "previewable"
    assert a["diff"] == b["diff"]
