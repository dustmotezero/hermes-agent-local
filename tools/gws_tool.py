"""
Google Workspace (Docs/Drive) tool — real, structured tool calling DIANE's
tool_server REST endpoints (verified working, see PATCH-031). Registered
under toolset="gws", matching the name already anticipated in
fast_path.py's ALL_KNOWN_TOOLS set but never implemented until now.

Deliberately thin: this tool's job is to call the real API and report the
real result — including real failures. It must never fabricate a success
message, a document ID, or a revision ID that didn't come back from Google.
"""

import json
import urllib.request
import urllib.error

from tools.registry import registry, tool_error

TOOL_SERVER_BASE = "http://localhost:9000"


def _post(path: str, payload: dict, timeout: int = 30):
    url = f"{TOOL_SERVER_BASE}{path}"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500]
        raise RuntimeError(f"tool_server HTTP {e.code}: {detail}")
    except Exception as e:
        raise RuntimeError(f"tool_server request failed: {e}")


DOCS_APPEND_SCHEMA = {
    "name": "docs_append",
    "description": (
        "Append text to an existing Google Doc, via the real Google Docs API "
        "through DIANE's tool_server. Never fabricate a success message or a "
        "document ID — if this call fails, report the real error text."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "description": "The Google Doc's real document ID (from its URL or a prior drive_create result — never invent one)",
            },
            "text": {"type": "string", "description": "Text to append to the end of the document"},
        },
        "required": ["document_id", "text"],
    },
}

DRIVE_CREATE_SCHEMA = {
    "name": "drive_create",
    "description": (
        "Create a new Google Doc or Sheet via the real Google Drive API through "
        "DIANE's tool_server. Returns the real created file ID — never invent one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Title of the new file"},
            "mime_type": {
                "type": "string",
                "description": "Google Workspace MIME type",
                "enum": [
                    "application/vnd.google-apps.document",
                    "application/vnd.google-apps.spreadsheet",
                ],
                "default": "application/vnd.google-apps.document",
            },
        },
        "required": ["name"],
    },
}


def _handle_docs_append(args, **kw):
    document_id = args.get("document_id")
    text = args.get("text")
    if not document_id or not isinstance(document_id, str):
        return tool_error("docs_append: missing required field 'document_id'.")
    if not text or not isinstance(text, str):
        return tool_error("docs_append: missing required field 'text'.")
    try:
        result = _post(
            "/tools/gws/docs/append",
            {"agent": "diane", "document_id": document_id, "text": text},
        )
        if result.get("success"):
            rev = ((result.get("data") or {}).get("writeControl") or {}).get(
                "requiredRevisionId", ""
            )
            return (
                f"Appended text to document {document_id}. "
                f"Confirmed by the real Google API (revision starts {rev[:12]})."
            )
        return tool_error(f"docs_append failed: {json.dumps(result)[:400]}")
    except Exception as e:
        return tool_error(f"docs_append failed: {e}")


def _handle_drive_create(args, **kw):
    name = args.get("name")
    if not name or not isinstance(name, str):
        return tool_error("drive_create: missing required field 'name'.")
    mime_type = args.get("mime_type", "application/vnd.google-apps.document")
    try:
        result = _post(
            "/tools/gws/drive/create",
            {"agent": "diane", "name": name, "mime_type": mime_type},
        )
        if result.get("success"):
            file_id = (result.get("data") or {}).get("id", "")
            return f"Created file '{name}' ({mime_type}). Real ID: {file_id}"
        return tool_error(f"drive_create failed: {json.dumps(result)[:400]}")
    except Exception as e:
        return tool_error(f"drive_create failed: {e}")


SHEETS_APPEND_SCHEMA = {
    "name": "sheets_append",
    "description": (
        "Append one or more rows to a Google Sheet, via the real Google Sheets "
        "API through DIANE's tool_server. Use this to log real records — student "
        "lesson progress, notes, amendments to a prior entry, etc. Never fabricate "
        "a success message; if this call fails, report the real error text."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "spreadsheet_id": {
                "type": "string",
                "description": "The target Google Sheet's real spreadsheet ID (from its URL — never invent one)",
            },
            "range": {
                "type": "string",
                "description": "A1-notation range to append after, e.g. 'Sheet1!A1' — the API finds the next empty row itself",
            },
            "values": {
                "type": "array",
                "description": "List of rows to append. Each row is itself a list of cell values, e.g. [[\"2026-08-06\", \"Matthew\", \"single stroke roll, 16th notes\"]]",
                "items": {"type": "array"},
            },
        },
        "required": ["spreadsheet_id", "range", "values"],
    },
}


def _handle_sheets_append(args, **kw):
    spreadsheet_id = args.get("spreadsheet_id")
    range_ = args.get("range")
    values = args.get("values")
    if not spreadsheet_id or not isinstance(spreadsheet_id, str):
        return tool_error("sheets_append: missing required field 'spreadsheet_id'.")
    if not range_ or not isinstance(range_, str):
        return tool_error("sheets_append: missing required field 'range'.")
    if not values or not isinstance(values, list):
        return tool_error("sheets_append: missing required field 'values' (must be a non-empty list of rows).")
    try:
        result = _post(
            "/tools/gws/sheets/update",
            {"agent": "diane", "spreadsheet_id": spreadsheet_id, "range": range_, "values": values},
        )
        if result.get("success"):
            updates = (result.get("data") or {}).get("updates") or {}
            updated_range = updates.get("updatedRange", "unknown range")
            updated_rows = updates.get("updatedRows", "?")
            return f"Appended {updated_rows} row(s) to {spreadsheet_id} at {updated_range}. Confirmed by the real Google API."
        return tool_error(f"sheets_append failed: {json.dumps(result)[:400]}")
    except Exception as e:
        return tool_error(f"sheets_append failed: {e}")


registry.register(
    name="docs_append",
    toolset="gws",
    schema=DOCS_APPEND_SCHEMA,
    handler=_handle_docs_append,
    emoji="📝",
    max_result_size_chars=5_000,
)

registry.register(
    name="drive_create",
    toolset="gws",
    schema=DRIVE_CREATE_SCHEMA,
    handler=_handle_drive_create,
    emoji="📄",
    max_result_size_chars=5_000,
)

registry.register(
    name="sheets_append",
    toolset="gws",
    schema=SHEETS_APPEND_SCHEMA,
    handler=_handle_sheets_append,
    emoji="📊",
    max_result_size_chars=5_000,
)
