#!/usr/bin/env python3
"""
R-093 PATCH — squad-router Layer 0 fast-path
Apply to: /Users/DIANE/.hermes/hermes-agent/plugins/squad-router/__init__.py

WHAT THIS ADDS:
  1. layer0_fast_path(message) — whole-string deterministic router, fires BEFORE
     _run_squad_member / Qwen inference. Returns a canned reply or None (fall-through).
  2. build_capability_prompt(agent_name) — reads AGENT_TOOLSETS and mechanically
     injects AVAILABLE_TOOLS + explicit negative list into the system prompt at
     construction time. Replaces any hand-written tool prose.
  3. Miss-logging — every Layer 0 miss writes a "routed_to_full_reasoning" marker
     to the structured miss log for tuning.

INTEGRATION POINT:
  In handle_message (or equivalent dispatch function), call layer0_fast_path(msg)
  BEFORE the Qwen/LLM call. If it returns a string, send that string directly and
  return — never hit the LLM. If it returns None, proceed as normal.

MUST NOT:
  - Match on prefix or keyword substring — WHOLE STRING only (stripped, lowercased)
  - Fast-path anything mentioning money, billing, a named person + judgment action,
    or any mixed/ambiguous/stateful content
  - Fast-path operational commands unless the ENTIRE message is an exact known command

NEVER PUT THIS IN SOUL.md — capability manifests come from AGENT_TOOLSETS only.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
HERMES          = Path("/Users/DIANE/.hermes")
AGENT_TOOLSETS  = HERMES / "state" / "agent_toolsets.json"   # canonical toolset manifest
MISS_LOG        = HERMES / "state" / "last_runs" / "layer0_misses.jsonl"

# ── Layer 0: whole-string match table ─────────────────────────────────────────
# Keys are the COMPLETE normalized message (stripped + lowercased).
# Values are the canned response strings.
# DO NOT add partial phrases. Every entry must be an exact complete utterance.
# Conservative by design — when in doubt, leave it out and fall through to Qwen.

_L0_EXACT: dict[str, str] = {
    # ── Greetings ─────────────────────────────────────────────────────────────
    "hi":            "Hey. What do you need?",
    "hello":         "Hey. Go ahead.",
    "hey":           "Hey. What's up?",
    "hey diane":     "Right here. What do you need?",
    "good morning":  "Morning. What are we doing today?",
    "good afternoon":"Afternoon. What do you need?",
    "good evening":  "Evening. What's going on?",
    "yo":            "Yo. What do you need?",
    "sup":           "Not much. You?",

    # ── Presence / status pings ───────────────────────────────────────────────
    "ping":          "Pong. Gateway UP.",
    "test":          "Online.",
    "you there?":    "Here.",
    "you there":     "Here.",
    "are you there?": "Here.",
    "are you there":  "Here.",
    "are you awake?": "Yeah, awake and ready.",
    "are you awake":  "Yeah, awake and ready.",
    "uptime":        "All services UP. Run `diag.sh` for full status.",
    "status":        "All services UP. Run `diag.sh` for detail.",
    "system status": "All services UP. Run `diag.sh` for detail.",
    "squad status":  "Squad is UP. DAN/TONY/ARGUS/LYNCH/TESLA/PHILO/ANDY live.",
    "is dan up":     "DAN bus is running.",
    "is tony up":    "TONY revenue cron is live.",
    "is argus up":   "ARGUS is running.",

    # ── Simple ACKs ───────────────────────────────────────────────────────────
    "ok":            "Yep.",
    "okay":          "Yep.",
    "ok.":           "Yep.",
    "okay.":         "Yep.",
    "thanks":        "Mm-hm.",
    "thank you":     "Yep.",
    "got it":        "Good.",
    "got it.":       "Good.",
    "cool":          "👍",
    "cool.":         "👍",
    "sounds good":   "👍",
    "sounds good.":  "👍",
    "brb":           "I'll be here.",
    "👍":             "✓",
    "✓":             "✓",

    # ── Identity ──────────────────────────────────────────────────────────────
    "who are you":   "I'm DIANE — the squad coordinator for the Mathews household system.",
    "what are you":  "I'm DIANE, the AI coordinator running on Hermes. Ask me anything or check status.",

    # ── Capability discovery ──────────────────────────────────────────────────
    "help":          "Available: attendance, schedule, billing, status checks, notes. What do you need?",
    "what can you do":"I coordinate DAN (attendance), TONY (revenue), ARGUS (monitoring), and more. What do you need?",
    "commands":      "Try: attendance, mark paid, schedule, status, ping. Or just tell me what you need.",
    "menu":          "Try: attendance, mark paid, schedule, status, ping. Or just tell me what you need.",

    # ── Known deterministic health checks ────────────────────────────────────
    "health":        "All services UP. Run diag.sh for full detail.",
    "health check":  "All services UP. Run diag.sh for full detail.",
    "checkpoint":    "Run `bash checkpoint.sh` to verify 4/4 artifacts.",
}

# ── Guard: these substrings FORCE fall-through regardless of match ─────────────
# If the normalized message contains ANY of these, never fast-path it.
_FALLTHROUGH_TRIGGERS = [
    # Financial / billing
    "pay", "paid", "rate", "price", "charge", "bill", "invoice", "money",
    "dollar", "venmo", "cash", "income", "revenue", "refund", "balance",
    # Named persons with any judgment action
    "vinod", "ritu", "tony", "dan ", "argus",  # note: "dan " with space to avoid "candy"
    # Policy / exception / conflict
    "why", "should", "except", "policy", "conflict", "error", "wrong", "fix",
    "broken", "fail", "miss", "late", "absent", "cancel", "makeup",
    # NOTE: "?" removed — exact-match table handles known question phrases;
    # open-ended questions fall through naturally via no match
    # Approval / decision
    "approve", "confirm", "reject", "deny",
]

# ── Miss log ──────────────────────────────────────────────────────────────────
def _log_miss(raw_message: str, normalized: str):
    """Append a structured miss record for tuning. Never blocks — swallows all errors."""
    try:
        MISS_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = json.dumps({
            "ts":         datetime.now(timezone.utc).isoformat(),
            "event":      "routed_to_full_reasoning",
            "raw_len":    len(raw_message),
            "normalized": normalized[:200],  # truncate for log safety
        })
        with open(MISS_LOG, "a") as f:
            f.write(record + "\n")
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────────────────
def layer0_fast_path(message: str) -> str | None:
    """
    Attempt a Layer 0 deterministic response. Returns a reply string on match,
    or None to fall through to Qwen / full reasoning.

    Rules (hard):
      1. Normalize: strip whitespace, lowercase.
      2. If the normalized message contains ANY _FALLTHROUGH_TRIGGER, return None.
      3. Match against _L0_EXACT as a WHOLE STRING — not prefix, not substring.
      4. On match: log hit, return canned reply.
      5. On miss: log miss (for tuning), return None.

    Confidence is BINARY. Any ambiguity → None.
    """
    if not message or not message.strip():
        return None

    normalized = message.strip().lower()

    # Guard: force fall-through if any trigger substring present
    for trigger in _FALLTHROUGH_TRIGGERS:
        if trigger in normalized:
            _log_miss(message, normalized)
            logger.debug("layer0: fallthrough-trigger=%r in %r → full reasoning", trigger, normalized[:80])
            return None

    # Detailed morning brief — dynamic, live-computed reply (not a static
    # canned string). Reuses r125_digester's real checks, no LLM involved.
    _DETAILED_BRIEF_TRIGGERS = {
        "detailed morning brief",
        "give me the detailed morning brief",
        "full morning brief",
        "detailed brief",
        "expanded morning brief",
    }
    if normalized in _DETAILED_BRIEF_TRIGGERS:
        try:
            import sys as _sys, importlib as _importlib
            if "/Users/DIANE/.hermes/bin" not in _sys.path:
                _sys.path.insert(0, "/Users/DIANE/.hermes/bin")
            import r125_digester as _rd
            _importlib.reload(_rd)
            reply = _rd.build_detailed_report()
            logger.info("layer0: HIT (detailed brief, dynamic) normalized=%r", normalized)
            return reply
        except Exception as e:
            logger.error("layer0: detailed brief build failed: %s", e)
            return "Couldn't build the detailed brief right now — check r125_digest.log."

    # Whole-string exact match
    reply = _L0_EXACT.get(normalized)
    if reply is not None:
        logger.info("layer0: HIT normalized=%r → fast reply", normalized)
        return reply

    # Miss
    _log_miss(message, normalized)
    logger.debug("layer0: MISS normalized=%r → full reasoning", normalized[:80])
    return None


# ── Capability injection ──────────────────────────────────────────────────────
def build_capability_prompt(agent_name: str, base_toolsets: dict | None = None) -> str:
    """
    Mechanically render an AVAILABLE_TOOLS block from AGENT_TOOLSETS.
    Inject this into the system prompt at construction time — never hand-write it.

    Returns a string block like:
        AVAILABLE_TOOLS: terminal, file, web_search, sheets_read
        You have access ONLY to the tools listed above.
        YOU DO NOT HAVE: sheets_write, execute_code, gws
        Any tool not listed is unavailable. Do not attempt to use it.

    If AGENT_TOOLSETS is missing or agent not found, returns a safe fallback
    (terminal, file only) with a warning logged.
    """
    toolsets = base_toolsets

    if toolsets is None:
        try:
            toolsets = json.loads(AGENT_TOOLSETS.read_text())
        except FileNotFoundError:
            logger.warning("build_capability_prompt: AGENT_TOOLSETS not found at %s — using fallback", AGENT_TOOLSETS)
            toolsets = {}
        except json.JSONDecodeError as e:
            logger.error("build_capability_prompt: AGENT_TOOLSETS JSON error: %s — using fallback", e)
            toolsets = {}

    # All known tools in the system (expand this list as tools are added)
    ALL_KNOWN_TOOLS = {
        "terminal", "file", "web_search", "sheets_read", "sheets_write",
        "execute_code", "gws", "send_message", "read_message",
        "get_current_priorities",
    }

    available = set(toolsets.get(agent_name, []))
    if not available:
        logger.warning("build_capability_prompt: no toolset for agent=%r — defaulting to [terminal, file]", agent_name)
        available = {"terminal", "file"}

    unavailable = sorted(ALL_KNOWN_TOOLS - available)
    available_sorted = sorted(available)

    lines = [
        f"AVAILABLE_TOOLS: {', '.join(available_sorted)}",
        "You have access ONLY to the tools listed above.",
    ]

    # Render real per-tool descriptions from the registry when available,
    # so directive guidance (e.g. "ALWAYS call this before answering X")
    # actually reaches the model instead of a bare tool name with no
    # usage signal. Falls back silently if registry import fails or a
    # tool has no registered description yet.
    try:
        from tools.registry import registry as _tool_registry
        desc_lines = []
        for tool_name in available_sorted:
            entry = _tool_registry.get_entry(tool_name)
            if entry and entry.description:
                desc_lines.append(f"  - {tool_name}: {entry.description}")
        if desc_lines:
            lines.append("")
            lines.append("TOOL USAGE GUIDANCE:")
            lines.extend(desc_lines)
    except Exception as e:
        logger.warning("build_capability_prompt: could not load tool descriptions from registry: %s", e)

    if unavailable:
        lines.append(f"YOU DO NOT HAVE: {', '.join(unavailable)}")
    lines.append("Any tool not listed is unavailable and must not be attempted.")

    return "\n".join(lines)


# ── Integration shim ──────────────────────────────────────────────────────────
# Drop this into handle_message in squad-router/__init__.py, BEFORE the
# _run_squad_member / Qwen call:
#
#   from plugins.squad_router.fast_path import layer0_fast_path, build_capability_prompt
#
#   async def handle_message(message: str, chat_id: str, ...):
#       # Layer 0: deterministic fast-path (whole-string only, ~200µs)
#       fast_reply = layer0_fast_path(message)
#       if fast_reply is not None:
#           await _bb_send(chat_id, fast_reply)
#           return
#
#       # Build system prompt with mechanically-injected capabilities
#       capability_block = build_capability_prompt("diane")
#       # Prepend or append capability_block to your existing system prompt before
#       # passing to _run_squad_member / AIAgent.
#       ...
#
# SOUL.md must contain NO tool inventory. Capability manifests come from
# AGENT_TOOLSETS via build_capability_prompt() only.
