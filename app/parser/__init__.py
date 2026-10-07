from .eml_parser import PARSER_VERSION, ParseFailure, parse_eml
from .html_sanitizer import escape_html, sanitize_html, strip_to_text
from .models import (
    Address,
    Attachment,
    BodyPart,
    ContentDisposition,
    Defect,
    ParseStatus,
    PartNode,
    ParsedMessage,
)

__all__ = [
    "parse_eml",
    "ParseFailure",
    "PARSER_VERSION",
    "sanitize_html",
    "escape_html",
    "strip_to_text",
    "ParsedMessage",
    "BodyPart",
    "Attachment",
    "Address",
    "Defect",
    "PartNode",
    "ParseStatus",
    "ContentDisposition",
]
