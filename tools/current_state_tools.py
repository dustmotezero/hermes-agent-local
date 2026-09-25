"""Current-state tools - live priority/status retrieval for DIANE's normal
chat path (NOT the dispatcher/orchestrator kanban toolset in kanban_tools.py).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from pathlib import Path

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

HERMES = os.path.expanduser("~/.hermes")
KANBAN = f"{HERMES}/kanban.db"
ARCHITECT_STATE = f"{HERMES}/state/_compiled/architect_state.json"


def check_current_state_requirements() -> bool:
    return True


def _refresh_runtime_state() -> None:
    """Refresh per-service booleans, then re-derive system_state from them.

    Two scripts, two halves: update_runtime_state.py writes the live
    UP/DOWN booleans; state_machine.py --evaluate derives system_state
    from those booleans. Calling only the first leaves system_state
    stale and self-contradicting. Call both, in that order.
    """
    try:
        subprocess.run(
            ["python3", f"{HERMES}/bin/update_runtime_state.py"],
            capture_output=True,
            timeout=20,
        )
    except Exception:
        logger.debug("runtime_state refresh failed", exc_info=True)

    try:
        subprocess.run(
            ["python3", f"{HERMES}/bin/state_machine.py", "--evaluate"],
            capture_output=True,
            timeout=20,
        )
    except Exception:
        logger.debug("system_state re-evaluation failed", exc_info=True)


def _get_current_priorities(args: dict, **kw) -> str:
    _refresh_runtime_state()

    limit = args.get("limit", 5)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    if limit < 1 or limit > 20:
        return tool_error("limit must be between 1 and 20")

    try:
        if not Path(KANBAN).exists():
            return tool_error(f"kanban.db not found at {KANBAN}")

        with sqlite3.connect(KANBAN, timeout=5) as db:
            rows = db.execute(
                "SELECT id, title, status, priority, tier, blocked_by, "
                "owner_agent, target_date, notes "
                "FROM roadmap WHERE status='pending' AND tier<=1 "
                "ORDER BY tier, priority LIMIT ?",
                (limit,),
            ).fetchall()
            blocked_rows = db.execute(
                "SELECT id, title FROM roadmap WHERE status='blocked'"
            ).fetchall()

        priorities = [
            {
                "id": r[0], "title": r[1], "status": r[2], "priority": r[3],
                "tier": r[4], "blocked_by": r[5], "owner_agent": r[6],
                "target_date": r[7], "notes": r[8],
            }
            for r in rows
        ]
        blocked = [{"id": r[0], "title": r[1]} for r in blocked_rows]

        runtime_state = {}
        rendered_at = None
        if Path(ARCHITECT_STATE).exists():
            try:
                s = json.loads(Path(ARCHITECT_STATE).read_text())
                runtime_state = s.get("runtime_state", {})
                rendered_at = s.get("rendered_at")
            except Exception:
                logger.debug("architect_state.json read/parse failed", exc_info=True)

        return json.dumps({
            "ok": True,
            "priorities": priorities,
            "blocked": blocked,
            "runtime_state": runtime_state,
            "architect_state_rendered_at": rendered_at,
            "source": "live kanban.db + architect_state.json - not cached, not memorized",
        })
    except Exception as e:
        logger.exception("get_current_priorities failed")
        return tool_error(f"get_current_priorities: {e}")


GET_CURRENT_PRIORITIES_SCHEMA = {
    "name": "get_current_priorities",
    "description": (
        "Fetch the live current priorities, system/service state, and "
        "blocked items from kanban.db and architect_state.json. "
        "ALWAYS call this tool before answering any question about "
        "current priorities, what's urgent, what shipped, system status, "
        "or 'what should we work on' - never answer those from memory. "
        "This tool re-checks live service health AND re-derives the "
        "overall system_state before returning. Facts about current "
        "state change daily; this tool is the only source of truth for "
        "them. If you answer a status/priority question without "
        "calling this tool first, you are guessing. "
        "This tool is SYNCHRONOUS and returns its full result immediately "
        "in the same response -- it does NOT spawn a background process. "
        "Do not call the process tool, do not poll, do not invent a "
        "session_id. Call get_current_priorities directly and read its "
        "return value."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max number of priority rows to return. Default 5, max 20.",
            },
        },
        "required": [],
    },
}

registry.register(
    name="get_current_priorities",
    toolset="current_state",
    schema=GET_CURRENT_PRIORITIES_SCHEMA,
    handler=_get_current_priorities,
    check_fn=check_current_state_requirements,
    emoji="📍",
)
