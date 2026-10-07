"""Small deterministic ParsedMessage + archived-snapshot builders for tests.

The snapshot mirrors exactly what the memory/PG repositories persist at ingest
time, so ``build_diff(snapshot_from(parsed), parsed)`` is empty by construction
and tests only need to mutate the archived side to simulate an old parser.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.parser.html_sanitizer import escape_html
from app.parser.models import (
    Address,
    Attachment,
    BodyPart,
    ContentDisposition,
    Defect,
    Header,
    ParsedMessage,
    ParseStatus,
    PartNode,
)

_DEFAULT_DATE = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def make_parsed(
    *,
    message_id: str | None = "msg-1@example.com",
    references: list[str] | None = None,
    in_reply_to: list[str] | None = None,
    subject: str | None = "测试主题 / subject",
    date: datetime | None = _DEFAULT_DATE,
    body_text: str = "plain body 正文",
    charset: str = "utf-8",
    attachment: bool = False,
    att_filename: str = "doc.pdf",
    extra_headers: list[tuple[str, str]] | None = None,
    defects: list[Defect] | None = None,
) -> ParsedMessage:
    references = references or []
    in_reply_to = in_reply_to or []
    defects = list(defects or [])

    raw_header_pairs: list[tuple[str, str]] = [
        ("From", "Sender <sender@example.com>"),
        ("To", "Recipient <to@example.com>"),
    ]
    if subject is not None:
        raw_header_pairs.append(("Subject", subject))
    if date is not None:
        raw_header_pairs.append(("Date", "Tue, 02 Jan 2024 03:04:05 +0000"))
    if message_id is not None:
        raw_header_pairs.append(("Message-ID", f"<{message_id}>"))
    if references:
        raw_header_pairs.append(("References", " ".join(f"<{r}>" for r in references)))
    if in_reply_to:
        raw_header_pairs.append(("In-Reply-To", " ".join(f"<{r}>" for r in in_reply_to)))
    raw_header_pairs.extend(extra_headers or [])
    headers = [
        Header(name=n, value=v, raw_value=v, ordinal=i)
        for i, (n, v) in enumerate(raw_header_pairs)
    ]

    bodies: list[BodyPart] = []
    attachments: list[Attachment] = []
    tree_children: list[PartNode] = []

    if not attachment:
        body = BodyPart(
            mime_path="1",
            content_type="text/plain",
            charset=charset,
            declared_charset=charset,
            disposition=ContentDisposition.INLINE,
            content_id=None,
            content_location=None,
            byte_size=len(body_text.encode(charset, errors="replace")),
            text=body_text,
            plain_text=body_text,
        )
        bodies.append(body)
        tree_children.append(
            PartNode("1", "text/plain", ContentDisposition.INLINE, None, False, False, body.byte_size)
        )

    if attachment:
        payload = b"%PDF-1.4 fake attachment bytes"
        attachments.append(
            Attachment(
                mime_path="2",
                content_type="application/pdf",
                charset=None,
                disposition=ContentDisposition.ATTACHMENT,
                filename=att_filename,
                raw_filename=att_filename,
                content_id=None,
                content_location=None,
                byte_size=len(payload),
                checksum_sha256=hashlib.sha256(payload).hexdigest(),
                storage_path=None,
                bytes_to_persist=payload,
            )
        )
        tree_children.append(
            PartNode("2", "application/pdf", ContentDisposition.ATTACHMENT, att_filename, False, False, len(payload))
        )

    tree = PartNode(
        mime_path="0",
        content_type="multipart/mixed",
        disposition=ContentDisposition.UNKNOWN,
        filename=None,
        is_multipart=True,
        is_embedded_message=False,
        byte_size=0,
        children=tree_children,
    )

    raw_sha = hashlib.sha256(b"fixture-original-bytes").hexdigest()
    return ParsedMessage(
        message_id=message_id,
        references=list(references),
        in_reply_to=list(in_reply_to),
        subject=subject,
        raw_subject=subject,
        date=date,
        from_=[Address("Sender", "sender@example.com", "Sender <sender@example.com>")],
        to=[Address("Recipient", "to@example.com", "Recipient <to@example.com>")],
        cc=[],
        bcc=[],
        reply_to=[],
        sender=[],
        headers=headers,
        tree=tree,
        bodies=bodies,
        attachments=attachments,
        defects=defects,
        status=ParseStatus.DEFECTIVE if defects else ParseStatus.OK,
        fatal_error=None,
        raw_sha256=raw_sha,
        raw_size=32,
    )


def make_snapshot(parsed: ParsedMessage) -> dict[str, Any]:
    """The archived facts a repository would hold for ``parsed``."""
    identifiers: list[dict[str, Any]] = []
    if parsed.message_id:
        identifiers.append({"kind": "message_id", "value": parsed.message_id, "ordinal": 0})
    identifiers.extend(
        {"kind": "references", "value": v, "ordinal": i} for i, v in enumerate(parsed.references)
    )
    identifiers.extend(
        {"kind": "in_reply_to", "value": v, "ordinal": i} for i, v in enumerate(parsed.in_reply_to)
    )

    bodies = []
    for b in parsed.bodies:
        bodies.append(
            {
                "mime_path": b.mime_path,
                "content_type": b.content_type,
                "charset": b.charset,
                "declared_charset": b.declared_charset,
                "disposition": b.disposition.value,
                "content_id": b.content_id,
                "content_location": b.content_location,
                "byte_size": b.byte_size,
                "text": b.text if b.content_type == "text/plain" else None,
                "safe_html": b.safe_html,
                "escaped_html": escape_html(b.text) if b.content_type == "text/html" else None,
                "plain_text": b.plain_text,
                "referenced_cids": list(b.referenced_cids),
            }
        )

    attachments = [
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
            "storage_path": a.storage_path,
            "stored": a.storage_path is not None,
        }
        for a in parsed.attachments
    ]

    return {
        "id": 1,
        "ingest_id": 1,
        "message_id": parsed.message_id,
        "subject": parsed.subject,
        "date": parsed.date,
        "thread_key": None,
        "raw_sha256": parsed.raw_sha256,
        "raw_size": parsed.raw_size,
        "raw_path": "00/raw-fixture.bin",
        "parser_version": "1.0.0",
        "headers": [
            {"ordinal": h.ordinal, "name": h.name, "value": h.value, "raw_value": h.raw_value}
            for h in parsed.headers
        ],
        "identifiers": identifiers,
        "bodies": bodies,
        "attachments": attachments,
        "defects": [
            {"stage": d.stage, "level": d.level, "message": d.message} for d in parsed.defects
        ],
        "other_messages": [],
    }
