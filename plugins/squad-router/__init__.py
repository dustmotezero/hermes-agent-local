import os, subprocess, json, urllib.request, uuid, threading, logging
from pathlib import Path

# FM-45: attach FileHandler to squad-router logger so INFO appears in gateway.log
# Root logger is WARNING by default — this bypasses it entirely.
def _setup_squad_router_logging():
    _sr_log = logging.getLogger("hermes_plugins.squad_router")
    if not any(isinstance(h, logging.FileHandler) for h in _sr_log.handlers):
        _log_path = Path("/Users/DIANE/.hermes/logs/gateway.log")
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        _fh = logging.FileHandler(_log_path)
        _fh.setLevel(logging.INFO)
        _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        _sr_log.addHandler(_fh)
        _sr_log.setLevel(logging.INFO)
        _sr_log.propagate = False
_setup_squad_router_logging()

import importlib.util as _il_util
import importlib.machinery as _il_mach
import importlib.util as _fp_util, importlib.machinery as _fp_mach
def _load_fast_path():
    spec = _fp_util.spec_from_loader("fast_path", _fp_mach.SourceFileLoader("fast_path", "/Users/DIANE/.hermes/hermes-agent/plugins/squad-router/fast_path.py"))
    mod = _fp_util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
_fp = _load_fast_path()
layer0_fast_path = _fp.layer0_fast_path
build_capability_prompt = _fp.build_capability_prompt
def _load_event_ledger():
    spec = _il_util.spec_from_loader("event_ledger", _il_mach.SourceFileLoader("event_ledger", "/Users/DIANE/.hermes/bin/event_ledger.py"))
    mod = _il_util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
_event_ledger = _load_event_ledger()

def _load_agent_handoff():
    spec = _il_util.spec_from_loader("agent_handoff", _il_mach.SourceFileLoader("agent_handoff", "/Users/DIANE/.hermes/bin/agent_handoff.py"))
    mod = _il_util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
_agent_handoff = _load_agent_handoff()

BB_URL = "http://localhost:1234"
BB_PASSWORD = os.getenv("BLUEBUBBLES_PASSWORD", "IktseMM33")
BB_SEND_TIMEOUT = 30
SUBPROCESS_TIMEOUT = 60
HERMES_HOME = Path("/Users/DIANE/.hermes")
HERMES_BIN = "/Users/DIANE/.hermes/hermes-agent/venv/bin/hermes"
DRIVE_CREATE = "/Users/DIANE/.hermes/tool_server/bin/drive-create"
DRIVE_LIST = "/Users/DIANE/.hermes/tool_server/bin/drive-list"
SHEETS_OPS = "/Users/DIANE/.hermes/tool_server/bin/sheets-ops"
JOBS_JSON = "/Users/DIANE/.hermes/cron/jobs.json"
PHONE = "+12488448838"

_dispatch_lock = threading.Lock()

# R-015: Capability Registry — per-agent toolset declaration
# Executor reads from this dict. Anything not listed literally does not exist to the agent.
# Add tools here deliberately; never inherit from host PATH.
AGENT_TOOLSETS = {
    "diane": ["terminal", "file", "web_search", "sheets_read", "current_state", "gws", "browser", "memory"],  # [FIX 2026-09-16] "memory" added -- was missing entirely, meaning DIANE's default iMessage fast-path (_run_squad_member) never had Hindsight auto_retain/auto_recall active. Every non-prefix-matched iMessage conversation (the DEFAULT routing case) was silently bypassing long-term memory since this registry was written. Other squad members (dan, tony, argus, lynch, tesla, philo, andy) still lack "memory" too -- not fixed here, flagged as a known follow-up.  # W3D24: "current_state" is the real registry toolset name for get_current_priorities (registry.register toolset= arg) -- "get_current_priorities" was never a real toolset, so the schema never reached the model despite dispatch (PATCH-025) and capability grant both being correctly wired. [PATCH-033 W3D31] Added "gws" -- real Docs/Drive tool (docs_append, drive_create), wired to tool_server's already-working REST endpoints (see PATCH-031). Prior to this, "sheets_read" was ALSO a phantom toolset name with no matching registry.register() call anywhere -- same bug class as get_current_priorities, never separately caught. "gws" is a REAL registered toolset (see tools/gws_tool.py) -- confirmed via registry.register(toolset="gws", ...).
    # [NEW W3D36] Added "browser" -- real browser_exec tool wrapping the browser-harness CLI (real dependency of browser_use, confirmed installed venv/lib/python3.12/site-packages/browser_harness 0.1.8). Confirmed live end-to-end tonight: real Chrome navigation via subprocess call to browser-harness, real page_info() returned. NOT vision-dependent for basic tasks -- uses accessibility tree per browser-harness's own SKILL.md guidance. Chosen over browser_use.Agent()'s structured AgentOutput tool-calling loop, which was confirmed broken against Qwen3-30B on this hardware tonight (ValidationError: model returns prose instead of JSON). Added to "diane" only for now, deliberately -- other agents (philo, andy) can get "browser" added the same way if/when a real task needs it.
    "dan":   ["terminal", "gws"],
    "tony":  ["terminal"],
    "argus": ["terminal", "file"],
    "lynch": ["file"],
    "tesla": ["file"],
    "philo": ["terminal"],
    "andy":  ["terminal"],
    "hank":  [],
}

class CapabilityViolation(Exception):
    pass

def _get_agent_toolsets(agent):
    import logging as _log
    log = _log.getLogger("hermes_plugins.squad_router")
    name = (agent or "").lower()
    if name not in AGENT_TOOLSETS:
        log.error(f"CAPABILITY_VIOLATION: unknown agent '{name}' requested tools — denied")
        _event_ledger.log_event("squad_router", "capability_violation", name, "denied", {"reason": "agent not in registry"})
        raise CapabilityViolation(f"Agent '{name}' not in capability registry")
    tools = AGENT_TOOLSETS[name]
    log.info(f"CAPABILITY_GRANT: agent={name} toolsets={tools}")
    _event_ledger.log_event("squad_router", "capability_grant", name, "ok", {"toolsets": tools})
    return tools

# Inbound message deduplication — keyed by message GUID, TTL 60s
_seen_guids: dict = {}
_seen_guids_lock = threading.Lock()

def _is_duplicate_message(event) -> bool:
    """Return True if we have seen this message GUID in the last 60s."""
    import time
    guid = None
    # Try common attribute paths
    for attr in ("guid", "message_id", "id"):
        guid = getattr(event, attr, None)
        if guid:
            break
    if not guid:
        src = getattr(event, "source", None)
        if src:
            guid = getattr(src, "message_id", None) or getattr(src, "guid", None)
    if not guid:
        return False
    now = time.time()
    with _seen_guids_lock:
        # Prune old entries
        expired = [k for k, v in _seen_guids.items() if now - v > 60]
        for k in expired:
            del _seen_guids[k]
        if guid in _seen_guids:
            return True
        _seen_guids[guid] = now
        return False
_recent_hashes: dict = {}
_hash_lock = threading.Lock()
_HASH_TTL = 30

def _bb_send(chat_id, text):
    if ";" not in chat_id:
        chat_id = f"iMessage;-;{chat_id}"
    chat_id = chat_id.replace("any;-;", "iMessage;-;")
    payload = json.dumps({"chatGuid": chat_id, "tempGuid": f"temp-{uuid.uuid4().hex}", "message": text}).encode()
    req = urllib.request.Request(f"{BB_URL}/api/v1/message/text?password={BB_PASSWORD}", data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
    except TimeoutError:
        pass  # BB delivers but never sends HTTP response — TimeoutError is success
    except Exception as e:
        return False
    return True

def _normalize_chat_id(chat_id):
    if not chat_id:
        return chat_id
    for prefix in ["any;-;", "iMessage;-;"]:
        if chat_id.startswith(prefix):
            chat_id = chat_id[len(prefix):]
    return chat_id

def _dedup_key(chat_id, text):
    import hashlib
    return hashlib.md5(f"{_normalize_chat_id(chat_id)}:{text[:100]}".encode()).hexdigest()

def _direct_drive_create(title, mime, chat_id):
    try:
        r = subprocess.run([DRIVE_CREATE, title, mime], capture_output=True, text=True, timeout=15)
        out = r.stdout.strip()
        url = next((w for w in out.split() if w.startswith("http")), None)
        if url:
            kind = "doc" if "document" in mime else "sheet"
            _bb_send(chat_id, f"Created your Google {kind.title()}: {title}\n{url}")
        else:
            _bb_send(chat_id, f"Drive create ran but no link returned: {out[:200]}")
        return True
    except Exception as e:
        return False

def _direct_drive_list(chat_id):
    try:
        r = subprocess.run([DRIVE_LIST], capture_output=True, text=True, timeout=15)
        _bb_send(chat_id, r.stdout.strip()[:1500] or "No files found.")
        return True
    except Exception:
        return False

def _extract_name(text, keywords):
    import re
    lower = text.lower()
    for kw in keywords:
        idx = lower.find(kw)
        if idx != -1:
            after = text[idx + len(kw):].strip().strip('"\'').split('\n')[0].strip()
            words_after = after.split()
            if words_after:
                name = ' '.join(words_after[:6]).strip('.,!?')
                if name:
                    return name
    words = text.split()
    return ' '.join(words[-3:]) if len(words) >= 3 else text.strip()

def _diane_read_document(text, chat_id):
    """
    DIANE document read — service account direct, no gws binary.
    Returns raw content for discussion. No LLM summarization.
    """
    import re as _re2, logging as _log2
    log = _log2.getLogger("hermes_plugins.squad_router")
    try:
        from google.oauth2 import service_account as _sa
        from googleapiclient.discovery import build as _gbuild

        _SA_FILE = "/Users/DIANE/.hermes/shared/google-service-account.json"
        _SCOPES = [
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/spreadsheets.readonly",
        ]
        creds = _sa.Credentials.from_service_account_file(_SA_FILE, scopes=_SCOPES)
        drive_svc = _gbuild("drive", "v3", credentials=creds, cache_discovery=False)
        sheets_svc = _gbuild("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()

        # Extract file name from message
        name_match = None
        for pattern in [
            r'(?:read|show|open|check|pull up|load)\s+(?:the\s+)?(?:google\s+)?(?:doc|sheet|file|spreadsheet|document)\s+([\w][\w\s]+?)(?:\s|$)',
            r'([\w][\w\s]{2,})',
            r'(?:read|show|open|check|pull up)\s+(?:the\s+)?(.+?)(?:\s+(?:for me|please))?$',
        ]:
            m = _re2.search(pattern, text, _re2.IGNORECASE)
            if m:
                name_match = m.group(1).strip()
                break
        if not name_match:
            words = text.strip().split()
            name_match = " ".join(words[-3:])

        _bb_send(chat_id, f"Looking for {name_match!r} in Drive...")

        results = drive_svc.files().list(
            q=f"name contains \"{name_match}\" and trashed=false",
            fields="files(id,name,mimeType)",
            pageSize=5
        ).execute()
        files = results.get("files", [])

        if not files:
            _bb_send(chat_id, f"No file found matching \"{name_match}\" in Drive.")
            return True

        name_lower = name_match.lower()
        match = next((f for f in files if name_lower in f["name"].lower()), files[0])
        file_id = match["id"]
        mime = match.get("mimeType", "")
        log.info(f"_diane_read_document: found {match['name']!r} id={file_id} mime={mime}")

        content = ""
        if "spreadsheet" in mime:
            result = sheets_svc.values().get(
                spreadsheetId=file_id, range="A1:Z200"
            ).execute()
            rows = result.get("values", [])
            content = "\n".join("\t".join(row) for row in rows)
        elif "document" in mime:
            export = drive_svc.files().export(
                fileId=file_id, mimeType="text/plain"
            ).execute()
            content = export.decode("utf-8") if isinstance(export, bytes) else str(export)
        else:
            import io
            from googleapiclient.http import MediaIoBaseDownload
            request = drive_svc.files().get_media(fileId=file_id)
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = dl.next_chunk()
            content = buf.getvalue().decode("utf-8", errors="replace")

        if not content.strip():
            _bb_send(chat_id, f"Found {match['name']!r} but it appears empty.")
            return True

        header = f"📄 {match['name']}\n" + "─" * 40 + "\n"
        body = content[:3000]
        if len(content) > 3000:
            body += f"\n\n[{len(content) - 3000} more chars — ask me to continue]"
        chunks = [body[i:i+1500] for i in range(0, len(body), 1500)]
        _bb_send(chat_id, header + chunks[0])
        for chunk in chunks[1:]:
            _bb_send(chat_id, chunk)
        return True

    except Exception as e:
        log.error(f"_diane_read_document error: {e}")
        _bb_send(chat_id, f"Drive read failed: {e}")
        return False


def _diane_schedule(chat_id):
    """DIANE schedule read — today's roster via sheets-ops."""
    try:
        import datetime as _dt
        day = _dt.datetime.now().strftime("%A").lower()
        r = subprocess.run(
            ["python3", SHEETS_OPS, "read-day", day],
            capture_output=True, text=True, timeout=15
        )
        out = r.stdout.strip()
        _bb_send(chat_id, out[:1500] if out else f"No schedule found for {day.title()}.")
        return True
    except Exception as e:
        _bb_send(chat_id, f"Schedule read failed: {e}")
        return False


def _direct_roster_query(chat_id):
    try:
        import datetime as _dt
        today_name = _dt.datetime.now().strftime("%A").lower()
        r = subprocess.run(["python3", "/Users/DIANE/.hermes/tool_server/bin/sheets-ops", "read-day", today_name], capture_output=True, text=True, timeout=15)
        _bb_send(chat_id, r.stdout.strip()[:1500] or "No roster data found.")
        return True
    except Exception:
        return False


def _direct_find_student(name, chat_id):
    try:
        r = subprocess.run(["python3", SHEETS_OPS, "find-student", name], capture_output=True, text=True, timeout=15)
        out = r.stdout.strip()
        _bb_send(chat_id, out[:1500] if out else f"No student found matching '{name}'.")
        return True
    except Exception as e:
        _bb_send(chat_id, f"[DAN] Error looking up student: {e}")
        return False

# R-071: DAN fast-path consolidator — deterministic handling before LLM
_DAN_STOP_WORDS = {"find", "lookup", "look", "up", "search", "get", "check", "show",
                   "what", "when", "where", "who", "is", "are", "does", "the", "a",
                   "an", "dan", "me", "my", "his", "her", "their", "student", "for",
                   "was", "were", "worked", "on", "add", "notes", "attended", "did",
                   "zoom", "lesson", "mate", "cancel", "late", "no", "show", "moved",
                   "moving", "but", "monday", "tuesday", "wednesday", "thursday",
                   "friday", "saturday", "sunday", "mondays", "tuesdays", "wednesdays",
                   "thursdays", "fridays", "saturdays", "sundays", "move", "switch",
                   "change", "switching", "changing"}

_DAN_PENDING_FILE = "/Users/DIANE/.hermes/state/dan_pending.json"

def _dan_get_pending():
    try:
        return json.loads(Path(_DAN_PENDING_FILE).read_text())
    except Exception:
        return None

def _dan_set_pending(data):
    try:
        Path(_DAN_PENDING_FILE).write_text(json.dumps(data))
    except Exception:
        pass


# ── R-115b: roster fuzzy-gate ─────────────────────────────────────────────────
_FASTPATH_MISSES_LOG = "/Users/DIANE/.hermes/logs/fastpath_misses.jsonl"

def _load_active_roster():
    """Return list of student first names + full names from MASTER_ROSTER via sheets_ops."""
    try:
        r = subprocess.run(["python3", SHEETS_OPS, "list-students"],
                           capture_output=True, text=True, timeout=10)
        names = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                names.append(line)
                # also index first name alone
                first = line.split()[0]
                if first not in names:
                    names.append(first)
        return names
    except Exception:
        return []

def _fuzzy_match_roster(name, roster=None):
    """
    Returns (matched_name, confidence) or (None, 0.0).

    R-115c fix (W3D15): now queries find-student directly via sheets-ops
    on every call instead of fuzzy-matching against a locally cached
    roster list. The old version built its roster from a list-students
    sheets-ops command that does not exist -- sheets-ops printed its own
    usage/help text to stdout instead, which got silently parsed as if
    it were a list of student names, so every match was scored against
    garbage and always came back 0.00 regardless of input. find-student
    is the live, authoritative source and already handles same-first
    name ambiguity correctly (e.g. Aiden Ma vs Aiden Hartley) -- no
    local logic needed for that case.

    confidence == 1.0  -> exact single match (FOUND)
    confidence == 0.6  -> ambiguous (multiple rows) -> one-tap confirm
    confidence == 0.0  -> no match -> block fast-path

    The roster parameter is accepted but ignored -- kept only so any
    existing caller that still passes it does not break.
    """
    if not name:
        return None, 0.0
    try:
        r = subprocess.run(
            ["python3", SHEETS_OPS, "find-student", name],
            capture_output=True, text=True, timeout=15
        )
    except Exception as e:
        log.warning(f"R-115c: find-student subprocess failed for {name!r}: {e}")
        return None, 0.0

    out = r.stdout or ""

    if out.startswith("FOUND:"):
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Student:"):
                return line.split("Student:", 1)[1].strip(), 1.0
        log.warning(f"R-115c: find-student FOUND but no Student: line parsed for {name!r}: {out!r}")
        return None, 0.0

    if out.startswith("AMBIGUOUS:"):
        candidates = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Row ") and ":" in line:
                candidates.append(line.split(":", 1)[1].strip())
        if candidates:
            return candidates[0], 0.6
        return None, 0.0

    return None, 0.0


def _log_fastpath_miss(raw_msg, extracted_name, reason):
    """Append a fastpath miss record. Never blocks."""
    import json as _json, datetime as _dt
    try:
        import os as _os
        _os.makedirs(_os.path.dirname(_FASTPATH_MISSES_LOG), exist_ok=True)
        rec = _json.dumps({
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "event": "fastpath_miss",
            "extracted_name": extracted_name,
            "reason": reason,
            "raw_msg": raw_msg[:200],
        })
        with open(_FASTPATH_MISSES_LOG, "a") as _f:
            _f.write(rec + "\n")
    except Exception:
        pass

# ── End R-115b helpers ────────────────────────────────────────────────────────
def _dan_clear_pending():
    try:
        Path(_DAN_PENDING_FILE).unlink(missing_ok=True)
    except Exception:
        pass

def _dan_extract_student(msg):
    """
    Extract the most likely roster student name from msg.

    R-115c fix (W3D15): builds every plausible candidate (each
    capitalized word alone, plus ADJACENT capitalized-word pairs only),
    scores each one with a live find-student lookup via
    _fuzzy_match_roster, and returns whichever scores highest. Fixes
    two failure modes seen live:
      - sentence-initial capitalization mistaken for a name word
        ("Log Boby as attended" -> old code extracted "Log Boby")
      - unrelated capitalized proper nouns elsewhere in the message
        ("Boby worked on the Rothman book" -> old code extracted
        "Boby Rothman", where Rothman is a textbook title, not a name)
    No local roster cache is loaded here -- each candidate is checked
    live against the sheet via _fuzzy_match_roster.
    """
    words = msg.strip().split()
    cap_positions = []
    for i, w in enumerate(words):
        clean = w.strip(".,?!'")
        if clean and clean[0].isupper() and clean.lower() not in _DAN_STOP_WORDS:
            cap_positions.append((i, clean))

    if not cap_positions:
        return None

    candidates = []
    for idx, (i, w) in enumerate(cap_positions):
        candidates.append(w)
        if idx + 1 < len(cap_positions):
            next_i, next_w = cap_positions[idx + 1]
            if next_i == i + 1:
                candidates.append(f"{w} {next_w}")

    best_name, best_conf = None, 0.0
    for cand in candidates:
        _, conf = _fuzzy_match_roster(cand)
        if conf > best_conf:
            best_name, best_conf = cand, conf

    if best_conf > 0.0:
        return best_name

    return " ".join(c for _, c in cap_positions[:2])


def _dan_get_student_duration(student):
    """Look up student duration from MASTER_ROSTER. Returns float hours (0.5, 0.75, 1.0)."""
    try:
        r = subprocess.run(["python3", SHEETS_OPS, "find-student", student],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            if "Duration:" in line:
                import re as _re
                m = _re.search(r"(\d+)\s*min", line)
                if m:
                    minutes = int(m.group(1))
                    return round(minutes / 60, 4)
    except Exception:
        pass
    return 0.5

def _dan_get_student_type(student):
    """Look up Student_Type from sheets-ops find-student. Returns 'EMA', 'LEGACY', 'TRIAL', or 'UNKNOWN'."""
    try:
        r = subprocess.run(
            ["python3", SHEETS_OPS, "find-student", student],
            capture_output=True, text=True, timeout=10
        )
        for line in r.stdout.splitlines():
            if "Student_Type:" in line:
                return line.split("Student_Type:")[-1].strip()
    except Exception as e:
        logging.getLogger("hermes_plugins.squad_router").warning(f"_dan_get_student_type error for {student}: {e}")
    return "UNKNOWN"

def _dan_notify_tony(student, status, duration=0.5, date_str=None, student_type=None):
    try:
        import datetime as _dt
        import hashlib as _hashlib
        _date = date_str or _dt.date.today().isoformat()
        _stype = student_type or _dan_get_student_type(student)
        # Deterministic operation_id keyed on (student, date, status) — retries
        # for the same lesson produce the same ID, caught by TONY dedup guard.
        _dedup_key = f"{student.lower().strip()}|{_date}|{status.lower().strip()}"
        _op_id = "dan_rev_" + _hashlib.sha256(_dedup_key.encode()).hexdigest()[:16]
        _agent_handoff.send_handoff("dan", "tony", "log_revenue", {
            "student":      student,
            "status":       status,
            "duration":     duration,
            "date":         _date,
            "student_type": _stype,
            "operation_id": _op_id,
        }, priority="high")
    except Exception as e:
        logging.getLogger("hermes_plugins.squad_router").error(f"_dan_notify_tony error: {e}")

def _dan_do_status_update(student, status, notes, chat_id, notify=True, duration=None):
    try:
        if duration is None:
            duration = _dan_get_student_duration(student)
        op = _event_ledger.new_operation_id()
        _event_ledger.log_event("dan", "attendance_intent", student, "pending",
                                {"status": status, "notes": notes or ""},
                                operation_id=op, confidence="observed",
                                confidence_reason="dan_iMessage_command")
        if notes:
            r = subprocess.run(["python3", SHEETS_OPS, "multi-update", student, status, notes],
                               capture_output=True, text=True, timeout=15)
        else:
            r = subprocess.run(["python3", SHEETS_OPS, "status-update", student, status],
                               capture_output=True, text=True, timeout=15)
        ack = (r.returncode == 0)
        out = r.stdout.strip() or "Done."
        _bb_send(chat_id, f"[DAN] {student} — {status}. {out[:200]}")
        _event_ledger.log_event("dan", "attendance", student, "ok",
                                {"status": status, "notes": notes or ""},
                                operation_id=op, confidence="verified",
                                confidence_reason="service_account_write",
                                terminal_state="completed", ack_received=ack)
        _dur = 1.0 if "2x" in status.lower() else duration
        if notify and "(paid)" in status.lower():
            _dan_notify_tony(student, status, duration=_dur)
        return True
    except Exception as e:
        _bb_send(chat_id, f"[DAN] Error updating {student}: {e}")
        return True

def _dan_fast_path(msg, chat_id):
    """Deterministic DAN handler. Returns True if handled, False to fall through to LLM."""
    import re as _re
    lower = msg.strip().lower()

    # Check pending clarification first (two-turn conversations)
    pending = _dan_get_pending()
    if pending:
        _dan_clear_pending()
        intent = pending.get("intent")
        student = pending.get("student", "")
        if intent == "lesson_mate_clarification":
            notes = f"Lesson mate — sent: {msg.strip()}"
            _dan_do_status_update(student, "Lesson Mate (Paid)", notes, chat_id)
            return True
        if intent == "zoom_clarification":
            notes = f"Zoom lesson — worked on: {msg.strip()}"
            _dan_do_status_update(student, "ATTENDED (PAID)", notes, chat_id)
            return True
        if intent == "slot_time_clarification":
            day = pending.get("day", "")
            import re as _re
            _WORD_NUMS = {"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,
                          "seven":7,"eight":8,"nine":9,"ten":10,"eleven":11,"twelve":12,
                          "one-thirty":1.5,"two-thirty":2.5,"three-thirty":3.5,
                          "four-thirty":4.5,"five-thirty":5.5,"six-thirty":6.5,
                          "seven-thirty":7.5,"eight-thirty":8.5,"nine-thirty":9.5}
            _t = msg.strip().lower()
            # Replace word numbers
            for word, num in _WORD_NUMS.items():
                if word in _t:
                    hr = int(num)
                    mn = "30" if num % 1 else "00"
                    ampm = "AM" if "am" in _t else "PM"
                    time_str = f"{hr}:{mn} {ampm}"
                    break
            else:
                _tc = _t.replace(" ", "").replace("/say", "").strip()
                # Handle shorthand: 8p, 8:30p, 8a, 8:30a
                _tm = _re.match(r"(\d{1,2})(?::(\d{2}))?([ap]m?)?", _tc)
                if _tm:
                    hr = int(_tm.group(1))
                    mn = _tm.group(2) or "00"
                    suffix = (_tm.group(3) or "").lower()
                    if suffix.startswith("a"):
                        ampm = "AM"
                    else:
                        ampm = "PM"
                    time_str = f"{hr}:{mn} {ampm}"
                else:
                    time_str = msg.strip()
            r = subprocess.run(
                ["python3", SHEETS_OPS, "slot-swap", student, day, time_str],
                capture_output=True, text=True, timeout=20
            )
            out = r.stdout.strip() or r.stderr.strip()
            _bb_send(chat_id, f"[DAN] {out}")
            return True

    # Roster/schedule queries
    if any(w in lower for w in ["roster", "schedule"]):
        _direct_roster_query(chat_id)
        return True

    # Student lookup
    if any(t in lower for t in ["find", "look up", "lookup", "search", "show me"]):
        name = _dan_extract_student(msg)
        if name:
            _direct_find_student(name, chat_id)
            return True

    # Notes update — "add notes for [student] — [text]"
    if "add notes" in lower or "notes for" in lower:
        name = _dan_extract_student(msg)
        m = _re.search(r"[-—]+\s*(.+)$", msg)
        notes = m.group(1).strip() if m else ""
        if name and notes:
            # R-135: event ledger intent
            op = _event_ledger.new_operation_id()
            _event_ledger.log_event("dan", "note_save_intent", name, "pending",
                                    {"notes": notes},
                                    operation_id=op, confidence="observed",
                                    confidence_reason="dan_iMessage_command")
            r = subprocess.run(["python3", SHEETS_OPS, "notes-update", name, notes],
                               capture_output=True, text=True, timeout=15)
            ack = (r.returncode == 0)
            # R-135: event ledger receipt
            _event_ledger.log_event("dan", "note_save", name,
                                    "ok" if ack else "error",
                                    {"notes": notes, "ack": ack},
                                    operation_id=op, confidence="verified",
                                    confidence_reason="service_account_write",
                                    terminal_state="completed", ack_received=ack)
            # R-135: receipt with what was written
            status_word = "saved" if ack else "FAILED"
            _bb_send(chat_id, f"[DAN] Note {status_word} — {name}: {notes[:120]}")
            return True

    # "[student] worked on [x]" → ATTENDED (PAID) + notes
    if "worked on" in lower:
        name = _dan_extract_student(msg)
        m = _re.search(r"worked on (.+)$", msg, _re.IGNORECASE)
        notes = m.group(1).strip() if m else ""
        if name:
            _dan_do_status_update(name, "ATTENDED (PAID)", notes, chat_id)
            return True

    # Late cancel
    if "late cancel" in lower:
        name = _dan_extract_student(msg)
        if name:
            _dan_do_status_update(name, "Late Cancel (Paid)", "", chat_id)
            return True

    # Canceled unpaid (plain "canceled" = unpaid; "late cancel" already caught above)
    if "canceled" in lower and "late" not in lower:
        name = _dan_extract_student(msg)
        if name:
            _dan_do_status_update(name, "Canceled (Unpaid)", "", chat_id, notify=False)
            return True

    # No show / canceled unpaid (legacy phrase match)
    if "no show" in lower or "canceled unpaid" in lower:
        name = _dan_extract_student(msg)
        if name:
            _dan_do_status_update(name, "Canceled (Unpaid)", "", chat_id, notify=False)
            return True

    # Attended (with optional notes)
    if "attended" in lower and "zoom" not in lower:
        name = _dan_extract_student(msg)
        if name:
            m = _re.search(r"attended[,\s]+[-—,]?\s*(.+)$", msg, _re.IGNORECASE)
            notes = m.group(1).strip() if m else ""
            _dan_do_status_update(name, "ATTENDED (PAID)", notes, chat_id)
            return True

    # Lesson mate → clarification
    if "lesson mate" in lower:
        name = _dan_extract_student(msg)
        if name:
            _dan_set_pending({"intent": "lesson_mate_clarification", "student": name})
            _bb_send(chat_id, f"[DAN] Got it — {name} lesson mate. What did you send her?")
            return True

    # Zoom → clarification
    if "zoom" in lower:
        name = _dan_extract_student(msg)
        if name:
            _dan_set_pending({"intent": "zoom_clarification", "student": name})
            _bb_send(chat_id, f"[DAN] Got it — {name} on Zoom. What did you guys work on?")
            return True

    # Moved to [day] → execute if time given, else clarify
    _DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    _WORD_TO_NUM = {"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,
                    "seven":7,"eight":8,"nine":9,"ten":10,"eleven":11,"twelve":12}
    for d in _DAYS:
        if any(f"{v} to {d}" in lower or f"{v} to {d}s" in lower
               for v in ["moved", "moving", "move", "switching", "switch", "changing", "change"]):
            name = _dan_extract_student(msg)
            if name:
                inline_time = None
                # Check digit time: "at 8", "at 8p", "at 8:30", "Thursday at 8"
                tm = _re.search(r"at\s+(\d{1,2})(?::(\d{2}))?([ap]m?)?", lower)
                if tm:
                    hr = int(tm.group(1))
                    mn = tm.group(2) or "00"
                    suffix = (tm.group(3) or "").lower()
                    ampm = "AM" if suffix.startswith("a") else "PM"
                    inline_time = f"{hr}:{mn} {ampm}"
                else:
                    # Check word time: "at eight"
                    for word, num in _WORD_TO_NUM.items():
                        if f"at {word}" in lower:
                            inline_time = f"{num}:00 PM"
                            break
                if inline_time:
                    r = subprocess.run(
                        ["python3", SHEETS_OPS, "slot-swap", name, d.capitalize(), inline_time],
                        capture_output=True, text=True, timeout=20
                    )
                    out = r.stdout.strip() or r.stderr.strip()
                    _bb_send(chat_id, f"[DAN] {out}")
                else:
                    _dan_set_pending({"intent": "slot_time_clarification", "student": name, "day": d.capitalize()})
                    _bb_send(chat_id, f"[DAN] What time on {d.capitalize()}?")
                return True

    # Double session / hour / two lessons
    _double_triggers = ["double", "two lessons", "two sessions", "an hour", "1 hour", "did 2", "did two"]
    if any(t in lower for t in _double_triggers):
        name = _dan_extract_student(msg)
        if name:
            m = _re.search(r"worked on (.+)$", msg, _re.IGNORECASE)
            notes = m.group(1).strip() if m else ""
            _dan_do_status_update(name, "ATTENDED 2X (PAID)", notes, chat_id)
            return True

    return False


def _direct_argus_status(chat_id):
    try:
        data = json.loads(Path(JOBS_JSON).read_text())
        lines = ["ARGUS CRON STATUS:"]
        for j in data.get("jobs", []):
            last = (j.get("last_run_at") or "never")[:16]
            status = j.get("last_status") or "?"
            lines.append(f"  {j['name'][:30]}: {last} [{status}]")
        _bb_send(chat_id, "\n".join(lines))
        return True
    except Exception:
        return False

def _digest_fast_path(chat_id):
    """On-demand financial snapshot — runs r125_digester.py directly.
    The script self-sends via its own _bb_send() call (no chat_id needed —
    BB destination is hardcoded inside the script, same as the 7AM cron)."""
    import subprocess
    import logging
    log = logging.getLogger("hermes_plugins.squad_router")
    try:
        subprocess.run(
            ["/Users/DIANE/.hermes/hermes-agent/venv/bin/python3",
             "/Users/DIANE/.hermes/bin/r125_digester.py"],
            capture_output=True, text=True, timeout=30
        )
        log.info("squad-router: on-demand digest fast-path completed")
    except Exception as e:
        log.error(f"squad-router: digest fast-path error: {e}")
        _bb_send(chat_id, f"Digest fast-path error: {e}")

def _hank_fast_path(cmd, chat_id):
    import sqlite3 as _sq
    import logging
    DB = "/Users/DIANE/.hermes/kanban.db"
    JOURNAL = "/Users/DIANE/.hermes/journal/events.jsonl"
    log = logging.getLogger("hermes_plugins.squad_router")

    def qry(sql):
        with _sq.connect(DB) as c:
            r = c.execute(sql)
            rows = r.fetchall()
            return rows

    def exe(sql):
        with _sq.connect(DB) as c:
            c.executescript(sql)

    def get_status(task_id):
        r = qry(f"SELECT status FROM tasks WHERE id='{task_id}';")
        return r[0][0] if r else None

    def transition(task_id, from_s, to_s, reason=""):
        current = get_status(task_id)
        if not current:
            _bb_send(chat_id, f"HANK: task {task_id} not found.")
            return False
        if current != from_s:
            _bb_send(chat_id, f"HANK: {task_id} is '{current}', not '{from_s}'. Cannot move to '{to_s}'.")
            return False
        completed = ", completed_at=strftime('%s','now')" if to_s == "done" else ""
        blocked = f", blocked_reason=NULL"
        if to_s == "blocked" and reason:
            blocked = f", blocked_reason='{reason.replace(chr(39), chr(39)+chr(39))}'"
        exe(f"""
            UPDATE tasks SET status='{to_s}', last_heartbeat_at=strftime('%s','now') {completed} {blocked} WHERE id='{task_id}';
            INSERT INTO transitions (task_id,from_status,to_status,actor,reason)
            VALUES ('{task_id}','{from_s}','{to_s}','hank','{reason.replace(chr(39), chr(39)+chr(39))}');
        """)
        return True

    def jlog(event, target="", result="success", meta=""):
        import os
        os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
        import datetime
        ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(JOURNAL, "a") as f:
            f.write(f'{{"timestamp":"{ts}","event":"{event}","actor":"hank","target_id":"{target}","result":"{result}","metadata":"{meta}"}}\n')

    parts = cmd.strip().split(None, 1)
    verb = parts[0].lower() if parts else "status"
    args = parts[1] if len(parts) > 1 else ""

    if verb == "status":
        ready = qry("SELECT COUNT(*) FROM tasks WHERE status='ready';")[0][0]
        running = qry("SELECT COUNT(*) FROM tasks WHERE status='running';")[0][0]
        blocked = qry("SELECT COUNT(*) FROM tasks WHERE status='blocked';")[0][0]
        open_t = qry("SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','archived','completed');")[0][0]
        running_tasks = qry("SELECT '  '||id||' '||substr(title,1,40) FROM tasks WHERE status='running' ORDER BY priority;")
        blocked_tasks = qry("SELECT '  '||id||' '||substr(title,1,30)||' ['||COALESCE(blocked_reason,'?')||']' FROM tasks WHERE status='blocked' ORDER BY priority;")
        import datetime
        msg = f"HANK STATUS — {datetime.datetime.now().strftime('%b %d %H:%M')}\nReady:{ready} Running:{running} Blocked:{blocked} Open:{open_t}"
        if running_tasks:
            msg += "\nRunning:\n" + "\n".join(r[0] for r in running_tasks)
        if blocked_tasks:
            msg += "\nBlocked:\n" + "\n".join(r[0] for r in blocked_tasks)
        _bb_send(chat_id, msg)
        jlog("ford_status")

    elif verb == "claim":
        task_id = args.split()[0].upper()
        title_r = qry(f"SELECT title FROM tasks WHERE id='{task_id}';")
        title = title_r[0][0] if title_r else task_id
        if transition(task_id, "ready", "running"):
            exe(f"UPDATE tasks SET assignee='hank' WHERE id='{task_id}';")
            _bb_send(chat_id, f"HANK claimed {task_id} — {title}")
            jlog("ford_claim", task_id)

    elif verb == "done":
        a_parts = args.split(None, 1)
        task_id = a_parts[0].upper()
        notes = a_parts[1] if len(a_parts) > 1 else ""
        title_r = qry(f"SELECT title FROM tasks WHERE id='{task_id}';")
        title = title_r[0][0] if title_r else task_id
        current = get_status(task_id)
        if current in ("running", "blocked"):
            if transition(task_id, current, "done", notes):
                parent_r = qry(f"SELECT parent_id FROM tasks WHERE id='{task_id}';")
                parent_msg = ""
                if parent_r and parent_r[0][0]:
                    pid = parent_r[0][0]
                    left = qry(f"SELECT COUNT(*) FROM tasks WHERE parent_id='{pid}' AND status NOT IN ('done','archived','completed');")[0][0]
                    parent_msg = f"\nProject complete: {pid}" if left == 0 else f"\n({left} tasks remaining on {pid})"
                _bb_send(chat_id, f"HANK closed {task_id} — {title}{parent_msg}")
                jlog("ford_done", task_id, meta=notes)
        else:
            _bb_send(chat_id, f"HANK: {task_id} is '{current}'. Must be running or blocked to close.")

    elif verb == "block":
        a_parts = args.split(None, 1)
        task_id = a_parts[0].upper()
        reason = a_parts[1] if len(a_parts) > 1 else ""
        title_r = qry(f"SELECT title FROM tasks WHERE id='{task_id}';")
        title = title_r[0][0] if title_r else task_id
        if transition(task_id, "running", "blocked", reason):
            _bb_send(chat_id, f"HANK blocked {task_id} — {title}\nReason: {reason}")
            jlog("ford_block", task_id, meta=reason)

    elif verb == "unblock":
        task_id = args.split()[0].upper()
        title_r = qry(f"SELECT title FROM tasks WHERE id='{task_id}';")
        title = title_r[0][0] if title_r else task_id
        if transition(task_id, "blocked", "running"):
            _bb_send(chat_id, f"HANK unblocked {task_id} — {title}")
            jlog("ford_unblock", task_id)

    elif verb == "list":
        rows = qry("SELECT id||' ['||status||'] '||substr(title,1,40) FROM tasks WHERE status IN ('ready','running','blocked') ORDER BY priority,created_at;")
        if not rows:
            _bb_send(chat_id, "HANK: no open tasks.")
        else:
            _bb_send(chat_id, "HANK open tasks:\n" + "\n".join(r[0] for r in rows))
        jlog("ford_list")

    elif verb == "update":
        a_parts = args.split(None, 1)
        task_id = a_parts[0].upper()
        notes = a_parts[1] if len(a_parts) > 1 else ""
        title_r = qry(f"SELECT title FROM tasks WHERE id='{task_id}';")
        if not title_r:
            _bb_send(chat_id, f"HANK: task {task_id} not found.")
            return
        import datetime
        ts = datetime.datetime.now().strftime("%m/%d %H:%M")
        exe(f"UPDATE tasks SET body=COALESCE(body||' | ','')|| '[{ts}] {notes.replace(chr(39), chr(39)+chr(39))}', last_heartbeat_at=strftime('%s','now') WHERE id='{task_id}';")
        _bb_send(chat_id, f"HANK updated {task_id}: {notes}")
        jlog("ford_update", task_id, meta=notes)

    else:
        _bb_send(chat_id, f"HANK: unknown command '{verb}'. Try: status | claim [id] | done [id] | block [id] [reason] | unblock [id] | list | update [id] [notes]")
        jlog("ford_unknown", result="failure", meta=verb)


def _tesla_new_project(idea, chat_id):
    import subprocess as _sp2, json as _json2, urllib.request as _ur
    from datetime import datetime as _dt
    import logging as _logging
    log = _logging.getLogger("hermes_plugins.squad_router")
    open("/tmp/tesla_debug.txt","a").write(f"thread started: {idea[:40]}\n")
    log.info(f"TESLA thread alive, idea={idea[:40]}, chat_id={chat_id}")
    # Kill any prior TESLA inference subprocess (Priority #6)
    import os as _os
    _pid_file = "/Users/DIANE/.hermes/state/tesla_llm.pid"
    _os.makedirs("/Users/DIANE/.hermes/state", exist_ok=True)
    try:
        if _os.path.exists(_pid_file):
            _old_pid = int(open(_pid_file).read().strip())
            try:
                _os.kill(_old_pid, 15)  # SIGTERM
                import time as _t; _t.sleep(2)
                _os.kill(_old_pid, 9)   # SIGKILL if still alive
            except ProcessLookupError:
                pass
            _os.remove(_pid_file)
            log.info(f"TESLA: killed prior subprocess pid={_old_pid}")
    except Exception as _pe:
        log.warning(f"TESLA: pid cleanup failed: {_pe}")
    _bb_send(chat_id, f"TESLA is evaluating: {idea[:80]}...")
    try:
        _today = open("/Users/DIANE/.hermes/today.txt").read().strip()
    except:
        _today = _dt.now().strftime("%Y-%m-%d")
    prompt = f"""You are TESLA, project architect for the DIANE Squad.
Today's date is {_today}. Mat has a new project idea: {idea}

Produce a structured execution plan in this exact format:

PROJECT: {idea}
CAPTURED: {_today}
STATUS: IDEATION

CONCEPT:
[1-2 sentence core idea]

CATEGORY: [product type]
MARKET: [who buys this]
PRODUCTION: [how it is made]

PHASES:
1. [Phase name] — [what happens, who owns it]
2. [Phase name] — [what happens, who owns it]
3. [Phase name] — [what happens, who owns it]

DEPENDENCIES:
- [what is needed before this can proceed]

SQUAD ASSIGNMENTS:
- LYNCH: [aesthetic/visual direction]
- TONY: [financial assessment needed]
- ANDY: [Etsy listing strategy]
- HANK: [execution milestones to drive]

FIRST 3 ACTIONS:
1. [Specific action — owner — timeframe]
2. [Specific action — owner — timeframe]
3. [Specific action — owner — timeframe]

RISKS:
- [key risk and mitigation]

TESLA VERDICT: [PURSUE NOW / BACKLOG / PAUSE FOR RESEARCH / RETIRE]
REASONING: [1-2 sentences on why]

End your response with exactly: ---END---"""

    try:
        env = {
            "HOME": "/Users/DIANE",
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/DIANE/.local/bin",
            "PYTHON_KEYRING_BACKEND": "keyrings.alt.file.PlaintextKeyring",
            "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND": "file",
            "HINDSIGHT_EMBED_API_DATABASE_URL": "pg0://hindsight-embed-hermes",
            "HINDSIGHT_LLM_API_KEY": "sk-no-key-required",
        }
        _tesla_proc = subprocess.Popen(
            [HERMES_BIN, "-z", "--profile", "tesla", prompt],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env
        )
        # Store PID for cancellation (Priority #6)
        try:
            open("/Users/DIANE/.hermes/state/tesla_llm.pid", "w").write(str(_tesla_proc.pid))
        except Exception:
            pass
        try:
            _stdout, _stderr = _tesla_proc.communicate(timeout=300)
            r_stdout = _stdout.decode("utf-8", errors="replace").strip()
            r_returncode = _tesla_proc.returncode
        except subprocess.TimeoutExpired:
            _tesla_proc.kill()
            raise
        # Wrap in a simple namespace so existing code works
        class _R: pass
        r = _R()
        r.stdout = r_stdout
        r.returncode = r_returncode
        out = (r_out or "").strip()
        if "---END---" in out:
            out = out[:out.rfind("---END---")].strip()
        if not out:
            _bb_send(chat_id, "TESLA timed out. Try again.")
            return

        # Save to ACTIVE_PROJECTS.md
        projects_file = "/Users/DIANE/.hermes/profiles/tesla/knowledge/ACTIVE_PROJECTS.md"
        with open(projects_file, "a") as f:
            f.write(f"\n\n---\n\n{out}\n")

        # Store in Hindsight
        summary = f"TESLA PROJECT: {idea} | {out[:300]}"
        subprocess.Popen([
            "/Users/DIANE/.hermes/hermes-agent/venv/bin/python3",
            "/Users/DIANE/.hermes/bin/hindsight-store.py",
            "--text", summary,
            "--tags", "tesla,project,active",
            "--layer", "project",
            "--source", "tesla-fast-path"
        ])

        # Write to TESLA_PROJECT_PIPELINE sheet
        try:
            today = open("/Users/DIANE/.hermes/today.txt").read().strip().split(",")[0].strip()
            from datetime import datetime as _dt2
            today = _dt2.now().strftime("%Y-%m-%d")
        except:
            today = _dt.now().strftime("%Y-%m-%d")
        verdict = "TBD"
        for line in out.split("\n"):
            if "TESLA VERDICT:" in line:
                verdict = line.split("TESLA VERDICT:")[-1].strip()
                break
        row = [idea[:100], "IDEATION", "", "", "", "TBD", "TBD", "", "", "Phase 1", "", "TESLA", today, today, "", verdict, ""]
        try:
            import json as _json3, subprocess as _sp3
            sheet_payload = _json3.dumps({
                "agent": "tesla",
                "spreadsheet_id": "1MObBo9xe7W19Ob7V12AtG47uaNfGe39orTOEQFxfkQw",
                "range": "Sheet1!A2",
                "values": [row]
            })
            _sp3.run([
                "curl", "-s", "-X", "POST",
                "http://127.0.0.1:9000/tools/gws/sheets/update",
                "-H", "Content-Type: application/json",
                "-d", sheet_payload
            ], timeout=20, capture_output=True)
            log.info("tesla sheet write: OK")
        except Exception as _se:
            log.warning(f"tesla sheet write failed: {_se}")

        # R-142: mark task COMPLETE on the board
        try:
            import importlib.util as _ilu3, importlib.machinery as _ilm3
            _spec3 = _ilu3.spec_from_loader("r142_task_board",
                _ilm3.SourceFileLoader("r142_task_board",
                    "/Users/DIANE/.hermes/bin/r142_task_board.py"))
            _tb3 = _ilu3.module_from_spec(_spec3); _spec3.loader.exec_module(_tb3)
            # Find the most recent OPEN task matching this idea
            _open = _tb3.list_tasks(status="OPEN")
            if _open:
                _latest = _open[-1]
                _tb3.update_task(
                    _latest["task_id"],
                    status="COMPLETE",
                    result_summary=f"Verdict: {verdict} | {out[:200]}",
                    artifacts=[{"type": "execution_plan", "content": out[:500]}]
                )
                log.info(f"R-142: task {_latest['task_id']} marked COMPLETE verdict={verdict}")
        except Exception as _tce:
            log.warning(f"R-142: task completion update failed (non-blocking): {_tce}")

        chunks = [out[i:i+1500] for i in range(0, len(out), 1500)]
        for chunk in chunks:
            _bb_send(chat_id, chunk)
        # Create Kanban parent task for this project
        try:
            import subprocess as _ksp
            # Parent task assigned to TESLA
            _ksp.run([
                "/Users/DIANE/.hermes/hermes-agent/venv/bin/python3", "-m", "hermes",
                "kanban", "create", f"TESLA: {idea[:80]}",
                "--assignee", "tesla",
                "--priority", "2"
            ], capture_output=True, timeout=10,
            env={
                "HOME": "/Users/DIANE",
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/DIANE/.local/bin",
                "PYTHON_KEYRING_BACKEND": "keyrings.alt.file.PlaintextKeyring",
            })
            # Parse FIRST 3 ACTIONS and create HANK child tasks
            in_actions = False
            action_count = 0
            for line in out.split("\n"):
                if "FIRST 3 ACTIONS:" in line:
                    in_actions = True
                    continue
                if in_actions and line.strip() and line.strip()[0].isdigit() and action_count < 3:
                    action_text = line.strip().lstrip("123456789. ")[:120]
                    _ksp.run([
                        "/Users/DIANE/.hermes/hermes-agent/venv/bin/python3", "-m", "hermes",
                        "kanban", "create", f"HANK: {action_text}",
                        "--assignee", "hank",
                        "--priority", "2"
                    ], capture_output=True, timeout=10,
                    env={
                        "HOME": "/Users/DIANE",
                        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/DIANE/.local/bin",
                        "PYTHON_KEYRING_BACKEND": "keyrings.alt.file.PlaintextKeyring",
                    })
                    action_count += 1
                elif in_actions and not line.strip():
                    break
            log.info(f"Kanban: TESLA parent + {action_count} HANK tasks created")
        except Exception as _ke:
            log.warning(f"Kanban create failed: {_ke}")

        _bb_send(chat_id, "TESLA plan saved to ACTIVE_PROJECTS.md + Hindsight + PROJECT_PIPELINE sheet + Kanban.")
    except subprocess.TimeoutExpired:
        _bb_send(chat_id, "TESLA took too long. Check ACTIVE_PROJECTS.md later.")
    except Exception as e:
        _bb_send(chat_id, f"TESLA error: {e}")


# PATCH-009: active agent session state
_ACTIVE_AGENT_FILE = "/Users/DIANE/.hermes/state/active_agent.json"

# PATCH-027: active agent TTL -- previously stuck forever once set, requiring
# manual "back to diane" to clear. Now auto-expires after _ACTIVE_AGENT_TTL_SECONDS
# of inactivity so a stuck/misrouted state self-heals instead of silently
# hijacking every future message.
_ACTIVE_AGENT_TTL_SECONDS = 900  # 15 minutes

def _get_active_agent():
    try:
        if os.path.exists(_ACTIVE_AGENT_FILE):
            import time as _ttl_time
            d = json.load(open(_ACTIVE_AGENT_FILE))
            ts = d.get("ts")
            if ts is None or (_ttl_time.time() - ts) > _ACTIVE_AGENT_TTL_SECONDS:
                log.info(f"squad-router: active agent '{d.get('agent')}' expired (TTL), clearing")
                _clear_active_agent()
                return None, None
            return d.get("agent"), d.get("chat_id")
    except Exception:
        pass
    return None, None

def _set_active_agent(agent, chat_id):
    try:
        import time as _ttl_time
        json.dump({"agent": agent, "chat_id": chat_id, "ts": _ttl_time.time()}, open(_ACTIVE_AGENT_FILE, "w"))
    except Exception:
        pass

def _clear_active_agent():
    try:
        if os.path.exists(_ACTIVE_AGENT_FILE):
            os.remove(_ACTIVE_AGENT_FILE)
    except Exception:
        pass

def _notify_tony(student, date_str, status, chat_id):
    try:
        import datetime as _dt
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        task = {
            "task_id": f"dan_to_tony_{ts}",
            "from": "dan",
            "to": "tony",
            "created": _dt.datetime.now().isoformat(),
            "query": f"Log revenue for lesson: {student} on {date_str}, status={status}. Read MASTER_ROSTER for rate. Record in TONY_FINANCIAL_COMMAND income tab.",
            "context": f"DAN just logged a paid lesson. Student: {student}. Date: {date_str}. Status: {status}.",
            "priority": "high",
            "status": "pending"
        }
        inbox = "/Users/DIANE/.hermes/bus/tony/inbox"
        os.makedirs(inbox, exist_ok=True)
        with open(f"{inbox}/dan_to_tony_{ts}.json", "w") as f:
            json.dump(task, f, indent=2)
        logging.getLogger("hermes_plugins.squad_router").info(f"TONY notified: {student} {date_str} {status}")
    except Exception as e:
        logging.getLogger("hermes_plugins.squad_router").error(f"_notify_tony failed: {e}")

# R-057: two-tier inference routing — 8B (8081) for CRUD, 14B (8080) for synthesis
_LIGHT_AGENTS = {"andy", "philo"}  # DAN removed — routes to 14B for judgment/write tasks

def _llm_base_url(agent: str) -> str:
    """Route lightweight CRUD agents to 8B (port 8081), synthesis to 14B (port 8080)."""
    return "http://127.0.0.1:8081/v1" if (agent or "").lower() in _LIGHT_AGENTS else "http://127.0.0.1:8080/v1"

import re as _honesty_re


def _turn_called_tool(messages, tool_name):
    """Scan a run_conversation() messages list for evidence a given tool
    was actually invoked this turn (assistant tool_calls entry OR a
    tool-role response naming it). Returns True only on real evidence,
    never inferred from response text."""
    if not isinstance(messages, list):
        return False
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                fn = (tc.get("function") or {}).get("name") if isinstance(tc, dict) else None
                if fn == tool_name:
                    return True
        if m.get("role") == "tool" and m.get("name") == tool_name:
            return True
    return False


_STATUS_CLAIM_PATTERN = _honesty_re.compile(
    r"\bR-\d{2,3}\b|\bstatus\b|\bpriorit(y|ies)\b|\bcomplete\b|\bpending\b|\bblocked\b|\bkanban\b|\broadmap\b",
    _honesty_re.IGNORECASE,
)


def _looks_like_unverified_status_claim(text, messages):
    """FM-honesty-gate: flags responses that assert roadmap/priority/status
    facts without evidence the model actually called get_current_priorities
    this turn. Narrow, pattern-level check -- not a general truth detector."""
    if not text or not _STATUS_CLAIM_PATTERN.search(text):
        return False
    return not _turn_called_tool(messages, "get_current_priorities")


def _run_squad_member(agent, message, chat_id):
    log = logging.getLogger("hermes_plugins.squad_router")
    if agent.lower() == "diane":
        soul_path = "/Users/DIANE/.hermes/SOUL.md"
    else:
        soul_path = f"/Users/DIANE/.hermes/profiles/{agent.lower()}/SOUL.md"
    try:
        system_prompt = open(soul_path).read()
    except Exception as e:
        log.error(f"_run_squad_member: SOUL.md missing for {agent}: {e}")
        _bb_send(chat_id, f"{agent.upper()} unavailable — SOUL.md missing.")
        return False

    # ── R-141: current_state.md injection (DIANE only) ──────────────────
    if agent.lower() == "diane":
        _state_path = "/Users/DIANE/.hermes/state/current_state.md"
        try:
            _state_content = open(_state_path).read().strip()
            if _state_content:
                system_prompt += "\n\n# CURRENT OPERATIONAL STATE (R-141 nightly synthesis)\n" + _state_content
                log.info(f"r141: current_state.md injected ({len(_state_content)} chars)")
        except FileNotFoundError:
            log.warning("r141: current_state.md not found — skipping (will exist after first 10PM cron)")
        except Exception as _se:
            log.warning(f"r141: current_state.md inject failed (non-blocking): {_se}")

    # [REMOVED 2026-09-17] Manual hindsight-recall.py shell-out retired --
    # the AIAgent's built-in memory provider (auto_recall) now handles this
    # automatically via local_external mode against the real Docker-hosted
    # Hindsight instance (localhost:8888). No per-path wiring needed anymore.

    # R-XXX (W3D14): RUNTIME CONSTRAINTS (sheets-ops) gated to DAN only.
    # Was previously injected unconditionally for every agent, where it acted
    # as the single strongest/most specific directive in the entire prompt
    # for agents that have no sheets-ops involvement at all — outranking the
    # tool-usage capability directive below it for every non-DAN agent.
    if agent.lower() == "dan":
        system_prompt += (
            "\n\n# RUNTIME CONSTRAINTS\n"
            "SHEET ACCESS: Use ONLY sheets-ops. NEVER use file, patch, or read_file tools for sheet data.\n"
            "NEVER escalate to DIANE for sheet operations. You have full access. Just run sheets-ops.\n"
            "When Mat reports a student session, execute ALL steps IN ORDER:\n"
            "STEP 1: date -v-7d +%Y-%m-%d (last week) or date +%Y-%m-%d (today)\n"
            "STEP 2: /Users/DIANE/.hermes/tool_server/bin/sheets-ops find-student \"NAME\"\n"
            "STEP 3: /Users/DIANE/.hermes/tool_server/bin/sheets-ops status-update \"NAME\" \"STATUS\"\n"
            "STEP 4: /Users/DIANE/.hermes/tool_server/bin/sheets-ops ledger-append \"NAME\" \"STATUS\" \"DATE\" \"content\" \"\" \"notes\"\n"
            "STEP 5: /Users/DIANE/.hermes/tool_server/bin/sheets-ops notes-update \"NAME\" \"notes\"\n"
            "STEP 6: Confirm: NAME — DATE — logged: STATUS\n"
            "ALL 6 STEPS MANDATORY. Do not skip. Do not escalate. Do not ask permission.\n"
        )

    # R-097 / build_capability_prompt: inject available tools into system prompt
    try:
        cap = build_capability_prompt(agent)
        if cap:
            system_prompt += f"\n\n{cap}"
            # W3D14: reinforcing directive appended LAST, after all other
            # injected context (state, hindsight recall, runtime constraints).
            # Mirrors the sheets-ops block's imperative style/weight so the
            # tool-usage directive is not structurally outranked, and gets
            # recency (last thing before the user turn) as well as the
            # primacy it already had from the capability block itself.
            system_prompt += (
                "\n\n# TOOL USE IS MANDATORY WHEN LISTED ABOVE\n"
                "If a tool above has usage guidance saying to ALWAYS call it before "
                "answering a certain kind of question, you MUST call that tool first. "
                "Do not answer from memory or guess. Do not skip. Do not ask permission.\n"
            )
    except Exception as _cap_e:
        log.warning(f"_run_squad_member: build_capability_prompt failed for {agent}: {_cap_e}")

    try:
        import sys as _sys
        _agent_path = "/Users/DIANE/.hermes/hermes-agent"
        if _agent_path not in _sys.path:
            _sys.path.insert(0, _agent_path)
        from run_agent import AIAgent

        for k, v in {
            "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND": "file",
            "PYTHON_KEYRING_BACKEND": "keyrings.alt.file.PlaintextKeyring",
            "HINDSIGHT_LLM_API_KEY": "sk-no-key-required",
        }.items():
            os.environ.setdefault(k, v)

        # W3D18: read the actual model name live from whichever process is
        # bound to the target port, instead of hardcoding a string here.
        # The W3D17 fix (hardcoded per-backend strings) was itself a second
        # instance of the same bug class -- found independently hardcoded in
        # FOUR separate files. active_model.get_active_model_name() is the
        # single source of truth all of them now use; nothing here should
        # ever need editing again when a model changes.
        if "/Users/DIANE/.hermes/bin" not in _sys.path:
            _sys.path.insert(0, "/Users/DIANE/.hermes/bin")
        from active_model import get_active_model_name
        _port = 8081 if (agent or "").lower() in _LIGHT_AGENTS else 8080
        try:
            _model_name = get_active_model_name(_port)
        except Exception as _am_e:
            log.error(f"active_model: could not resolve model for port {_port}: {_am_e}")
            _model_name = "unknown"
        agent_obj = AIAgent(
            base_url=_llm_base_url(agent),
            api_key="sk-no-key-required",
            model=_model_name,
            max_iterations=15,
            quiet_mode=True,
            request_overrides={"cache_prompt": True},
            enabled_toolsets=_get_agent_toolsets(agent),
            skip_context_files=True,
        )
        # ── R-XXX: persistent session memory for squad-router fast-path ────
        # Uses the same SessionDB the gateway itself uses, so a "DIANE, ..."
        # iMessage turn is a real, continuing conversation rather than a
        # cold, context-free one-shot every single time. Session id is
        # stable per (agent, chat_id) so the same phone conversation always
        # resumes the same thread.
        conversation_history = None
        _sdb = None
        try:
            import hashlib as _hashlib
            from hermes_state import SessionDB as _SessionDB
            _sdb = _SessionDB()
            _chat_key = _hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]
            _imsg_session_id = f"imessage_{agent.lower()}_{_chat_key}"
            _existing = _sdb.get_session(_imsg_session_id)
            if _existing is None:
                _sdb.create_session(_imsg_session_id, source="bluebubbles_squad_router")
                log.info(f"session_memory: created new persistent session {_imsg_session_id} for {agent}")
            else:
                conversation_history = _sdb.get_messages_as_conversation(_imsg_session_id)
                log.info(f"session_memory: loaded {len(conversation_history)} prior messages for {agent} session {_imsg_session_id}")
        except Exception as _sdb_e:
            log.warning(f"session_memory: could not load/create session (non-blocking, falling back to cold start): {_sdb_e}")

        result = agent_obj.run_conversation(
            user_message=message,
            system_message=system_prompt,
            conversation_history=conversation_history,
        )
        out = (result.get("final_response") or "").strip()

        # [REMOVED 2026-09-17] Manual hindsight-store.py shell-out retired --
        # the AIAgent's built-in memory provider (auto_retain, via
        # finalize_turn -> _sync_external_memory_for_turn) now handles this
        # automatically for every turn, on any code path, against the real
        # Docker-hosted Hindsight instance (localhost:8888). No per-path
        # wiring needed anymore -- this is the actual fix for the "wire
        # into every new code path by hand" problem.

        # Persist this turn so the next message in this same phone
        # conversation has real continuity, same pattern the gateway uses.
        if _sdb is not None:
            try:
                _sdb.append_message(_imsg_session_id, role="user", content=message)
                _sdb.append_message(_imsg_session_id, role="assistant", content=out)
                log.info(f"session_memory: persisted turn for {agent} session {_imsg_session_id}")
            except Exception as _persist_e:
                log.warning(f"session_memory: failed to persist turn (non-blocking): {_persist_e}")
    except Exception as e:
        log.error(f"_run_squad_member: AIAgent error for {agent}: {e}")
        _bb_send(chat_id, f"{agent.upper()} error — {e}")
        return False

    # -- FM-honesty-gate: catch unverified status/priority claims (DIANE only) --
    # Built 2026-07-07 after a live, reproduced case: DIANE answered "R-016
    # status" with a fabricated "complete and active" claim (real kanban.db
    # state: pending, blocked). Capability was granted, the tool-use-mandatory
    # prompt directive was already in place, and she still never called
    # get_current_priorities -- prompt-only enforcement was not sufficient.
    # Narrow, pattern-level check (Finding 19 first concrete fix), not the
    # full R-050 truth-boundaries doctrine.
    #
    # [DISABLED 2026-09-17] The pattern matches bare common words anywhere
    # in the response ("complete", "status", "pending", "blocked",
    # "priority", "kanban", "roadmap") with zero context -- it fired on
    # "Harbor lynx 62 -- confirmed... now complete" (a memory-test reply
    # with no roadmap/priority content at all), forcing an irrelevant
    # get_current_priorities retry that cost ~4 minutes and produced a
    # confusing, unhelpful reply. Fired 5/5 times today, all in a short
    # session -- real, frequent cost, unverified benefit. Reintroduce
    # narrower (e.g. require an R-### reference AND a completion-word
    # co-occurring) only if a real, reproduced hallucination case shows up
    # again, not preemptively.
    if False and agent.lower() == "diane":
        try:
            _turn_messages = result.get("messages") or []
            if _looks_like_unverified_status_claim(out, _turn_messages):
                log.warning(
                    f"honesty-gate: unverified status claim detected for diane, "
                    f"retrying with forced tool-call instruction. text={out[:120]!r}"
                )
                _retry_result = agent_obj.run_conversation(
                    user_message=(
                        message
                        + "\n\n[SYSTEM: Your previous answer made a status/priority "
                        "claim without calling get_current_priorities. Call that "
                        "tool now and answer using ONLY its real return value. "
                        "If the tool result contradicts your previous answer, "
                        "report the tool result, not your previous answer.]"
                    ),
                    system_message=system_prompt,
                )
                _retry_out = (_retry_result.get("final_response") or "").strip()
                _retry_messages = _retry_result.get("messages") or []
                if _retry_out and _turn_called_tool(_retry_messages, "get_current_priorities"):
                    out = _retry_out
                    log.info("honesty-gate: retry called tool successfully, using retry response")
                elif _retry_out:
                    out = (
                        "\u26a0\ufe0f Unverified -- I could not confirm this against live "
                        "priority data. Treat the following as unreliable:\n\n" + _retry_out
                    )
                    log.warning("honesty-gate: retry still did not call tool, shipped with warning label")
        except Exception as _hg_e:
            log.warning(f"honesty-gate: check failed (non-blocking): {_hg_e}")

    # ── R-150: capture log (local-only, never sent to Gemini directly) ──────
    try:
        import json as _cj, datetime as _cdt
        _capture_entry = {
            "timestamp": _cdt.datetime.now(_cdt.timezone.utc).isoformat(),
            "agent": agent,
            "message_in": message[:1000],
            "response_out": out[:1500]
        }
        with open("/Users/DIANE/.hermes/logs/capture_log.jsonl", "a") as _cf:
            _cf.write(_cj.dumps(_capture_entry) + "\n")
    except Exception as _ce:
        log.warning(f"R-150: capture log write failed (non-blocking): {_ce}")

    drift_phrases = ["i am an ai", "as a language model", "as an ai assistant", "i'm an ai", "i am an artificial intelligence"]
    if any(p in out.lower() for p in drift_phrases):
        log.warning(f"PERSONA_DRIFT agent={agent} chat={chat_id}")

    # PATCH-037: strip leaked out-of-band steer markers before send.
    # format_steer_marker() in prompt_builder.py wraps mid-turn user messages
    # in STEER_MARKER_OPEN/CLOSE so the model can distinguish them from tool
    # output. STEER_CHANNEL_NOTE tells the model to trust and act on the
    # marker, but never tells it not to echo the marker itself back in its
    # reply -- confirmed leaking verbatim into a live DIANE reply on 2026-07-01.
    # This is a deterministic backstop (matches this codebase's existing
    # policy of not depending on model instruction-following for correctness,
    # e.g. R-151's comment on the same principle) in addition to a prompt-note
    # fix. Strips the markers and everything between them, anywhere in out.
    if "OUT-OF-BAND USER MESSAGE" in out:
        _steer_pattern = re.compile(
            r"\n*\[OUT-OF-BAND USER MESSAGE.*?\]\n?.*?\n?\[/OUT-OF-BAND USER MESSAGE\]\n*",
            re.DOTALL
        )
        _out_before = out
        out = _steer_pattern.sub("\n", out).strip()
        if out != _out_before:
            log.warning(f"PATCH-037: stripped leaked steer marker from {agent} reply, chat={chat_id}")

    if out:
        # DIANE is the default voice — no prefix. All other agents tag themselves.
        if agent == "diane":
            _bb_send(chat_id, out[:1400])
        else:
            _bb_send(chat_id, f"[{agent.upper()}] {out[:1400]}")
        _set_active_agent(agent, chat_id)

        if agent == "dan":
            import re as _re
            # Detect paid lesson from DAN's natural confirmation output
            paid_keywords = ["attended (paid)", "late cancel (paid)", "lesson mate (paid)", "makeup (paid)", "attended", "late cancel", "lesson mate"]
            out_lower = out.lower()
            if any(kw in out_lower for kw in paid_keywords):
                import datetime as _dt
                student_m = _re.search(r"([A-Z][a-z]+)(?:'s)? status", out)
                date_m = _re.search(r"(\d{4}-\d{2}-\d{2})", out)
                status_m = _re.search(r"(Late Cancel \(Paid\)|Attended \(Paid\)|Lesson Mate \(Paid\)|Makeup \(Paid\))", out, _re.IGNORECASE)
                # Fall back to message for student name if regex misses
                msg_words = message.split()
                student_name = student_m.group(1) if student_m else (msg_words[1].rstrip(".,") if len(msg_words) > 1 else "Unknown")
                # Fall back to last week if no ISO date found in output
                resolved_date = date_m.group(1) if date_m else (_dt.date.today() - _dt.timedelta(days=7)).strftime("%Y-%m-%d")
                _dan_notify_tony(
                    student_name,
                    status_m.group(1) if status_m else "Attended (Paid)",
                    duration=0.5,
                    date_str=resolved_date,
                )

    return True

SQUAD_MEMBERS = ["dan", "tony", "argus", "lynch", "tesla", "philo", "andy", "hank"]

def handle_message(message_text, chat_id, context=None):
    import re, logging
    log = logging.getLogger("hermes_plugins.squad_router")

    # ── R-117: Idea/log prefix capture (pre-LLM, pre-Layer-0) ──────────────────
    _idea_match = re.match(r'^(?:log|idea|note)\s*:\s*(.+)', (message_text or '').strip(), re.IGNORECASE)
    if _idea_match:
        _idea_text = _idea_match.group(1).strip()
        try:
            import subprocess as _isp
            _ir = _isp.run(
                ['/Users/DIANE/.hermes/hermes-agent/venv/bin/python3',
                 '/Users/DIANE/.hermes/bin/idea-log.py',
                 '--text', _idea_text,
                 '--source', 'DIANE',
                 '--category', 'general',
                 '--chat-id', chat_id],
                capture_output=True, text=True, timeout=10
            )
            if _ir.returncode == 0:
                pass  # R-117: BB send handled by idea-log.py
                log.info(f"R-117: idea logged — {_idea_text[:60]}")
            else:
                _bb_send(chat_id, f"⚠️ Idea log failed — {_ir.stderr[:100]}")
                log.warning(f"R-117: idea-log.py failed — {_ir.stderr[:100]}")
        except Exception as _ie:
            _bb_send(chat_id, f"⚠️ Idea log error — {_ie}")
            log.warning(f"R-117: idea log exception — {_ie}")
        return {"action": "skip", "reason": "idea_prefix_capture"}
    # ── End R-117 ─────────────────────────────────────────────────────────────

    # ── R-093: Layer 0 deterministic fast-path (whole-string only) ─────────────
    _l0_reply = layer0_fast_path(message_text)
    if _l0_reply is not None:
        _bb_send(chat_id, _l0_reply)
        return {"action": "skip", "reason": "layer0_fast_path"}
    # ── End Layer 0 ───────────────────────────────────────────────────────────
    # ── R-115b: ^dane, prefix alias (iOS VTT correction) ────────────────────────
    _lower_strip = (message_text or "").strip().lower()
    if re.match(r"^dane,", _lower_strip):
        message_text = re.sub(r"^[Dd]ane,", "dan,", message_text.strip(), count=1)
        _lower_strip = message_text.strip().lower()
    # ── End dane alias ────────────────────────────────────────────────────────
    # ── R-115: DAN auto-route — lesson keywords without "dan," prefix ──────────
    _lesson_keywords = [
        "late cancel", "canceled", "no show", "attended", "lesson mate",
        "worked on", "zoom", "moved to", "did an hour", "did a half",
        "had a lesson", "was a late", "canceled today", "canceled unpaid",
        "holiday", "makeup"
    ]
    _lower_strip = (message_text or "").strip().lower()
    if not _lower_strip.startswith(("dan", "tony", "argus", "lynch", "tesla",
                                     "philo", "andy", "hank", "diane",
                                     "approve ", "pending")):
        if any(kw in _lower_strip for kw in _lesson_keywords):
            _auto_student = _dan_extract_student(message_text or "")
            if _auto_student:
                # R-115b: roster fuzzy-gate — name MUST match active roster
                _roster_match, _roster_conf = _fuzzy_match_roster(_auto_student)
                if _roster_conf < 0.6:
                    log.info(f"R-115b: auto-route BLOCKED — {_auto_student!r} not in roster (conf={_roster_conf:.2f})")
                    _log_fastpath_miss(message_text, _auto_student, f"no_roster_match conf={_roster_conf:.2f}")
                    _bb_send(chat_id, f"No roster match for {_auto_student!r} (conf={_roster_conf:.2f}) — blocked.")
                    return {"action": "skip", "reason": "r115b_no_roster_match"}
                elif _roster_conf < 0.8:
                    log.info(f"R-115b: auto-route AMBIGUOUS — {_auto_student!r} -> {_roster_match!r} conf={_roster_conf:.2f}")
                    _bb_send(chat_id, f"Did you mean {_roster_match}? Reply yes to confirm or correct the name.")
                    return {"action": "skip", "reason": "r115b_ambiguous_confirm"}
                else:
                    log.info(f"R-115: auto-route to DAN — lesson keyword detected, student={_roster_match} (conf={_roster_conf:.2f})")
                    if _dan_fast_path(message_text, chat_id):
                        return {"action": "skip", "reason": "r115_dan_auto_route"}
    # ── End R-115 ─────────────────────────────────────────────────────────────

    # ── R-151: pre-LLM priorities fast-path ──────────────────────────────────
    # W3D16: get_current_priorities is wired into the model's tools=[...] and
    # has an explicit "ALWAYS call this tool first" directive, but the model
    # does not reliably comply -- confirmed via live agent.log trace (zero
    # tool_turns on a direct "what's urgent right now?" query). Rather than
    # depend on instruction-following, fetch the data deterministically here,
    # the same way R-115 (lesson routing) and R-125 (approvals) bypass the
    # model for high-frequency queries we can't afford to get wrong.
    _priority_keywords = [
        "what's urgent", "whats urgent", "what is urgent",
        "current priorities", "current priority",
        "what's pending", "whats pending",
        "system status", "what shipped", "what should we work on",
        "most annoying", "recurring problem", "recurring issue",
        "what's broken", "whats broken", "biggest issue", "biggest problem",
        "what's wrong", "whats wrong", "what needs attention",
        "top priority", "top priorities", "anything urgent",
        "what's next", "whats next", "what should i know",
    ]
    _lower_strip_priorities = (message_text or "").strip().lower().replace("\u2019", "'")
    if any(kw in _lower_strip_priorities for kw in _priority_keywords):
        try:
            from tools.current_state_tools import _get_current_priorities
            _priorities_raw = _get_current_priorities({})
            _priorities_data = json.loads(_priorities_raw)
            if _priorities_data.get("ok"):
                _p_list = _priorities_data.get("priorities", [])
                _blocked_list = _priorities_data.get("blocked", [])
                if not _p_list and not _blocked_list:
                    _reply = "📍 No pending priority items in kanban.db right now."
                else:
                    _lines = ["📍 Current priorities (live kanban.db):\n"]
                    for _p in _p_list:
                        _lines.append(f"• {_p['id']} ({_p['status']}): {_p['title']}")
                    if _blocked_list:
                        _lines.append("\nBlocked:")
                        for _b in _blocked_list:
                            _lines.append(f"• {_b['id']}: {_b['title']}")
                    _reply = "\n".join(_lines)
                _bb_send(chat_id, _reply)
                log.info("R-151: priorities fast-path served from kanban.db (no model turn)")
                return {"action": "skip", "reason": "r151_priorities_fastpath"}
            else:
                log.warning(f"R-151: get_current_priorities returned not-ok: {_priorities_data}")
                _bb_send(chat_id, f"⚠️ Could not fetch live priorities: {_priorities_data.get('error', 'unknown error')}")
                return {"action": "skip", "reason": "r151_priorities_fastpath_error"}
        except Exception as _r151_e:
            log.error(f"R-151: priorities fast-path error: {_r151_e}")
            _bb_send(chat_id, f"⚠️ Priorities fast-path error — {_r151_e}")
            return {"action": "skip", "reason": "r151_priorities_fastpath_exception"}
    # ── End R-151 ─────────────────────────────────────────────────────────────

    # ── R-125: pre-LLM approval command layer ────────────────────────────────
    _stripped = (message_text or "").strip()
    def _maybe_trigger_overdue_reset(chat_id):
        try:
            _reset_r = subprocess.run(
                ["/Users/DIANE/.hermes/hermes-agent/venv/bin/python3",
                 "/Users/DIANE/.hermes/profiles/dan/weekly_roster_reset.py", "--if-due"],
                capture_output=True, text=True, timeout=120
            )
            _reset_out = (_reset_r.stdout or "") + (_reset_r.stderr or "")
            if "NOT DUE YET" in _reset_out:
                log.info("R-CATCHUP: roster reset checked, not due yet")
            elif "ROSTER_RESET: SUCCESS" in _reset_out:
                _bb_send(chat_id, "[DAN] Weekly roster reset ran automatically (was overdue, now caught up).")
                log.info("R-CATCHUP: overdue roster reset auto-triggered successfully")
            elif "already ran this week" in _reset_out:
                log.info("R-CATCHUP: roster reset checked, already ran this week")
            else:
                log.warning(f"R-CATCHUP: roster reset --if-due exited {_reset_r.returncode}, unrecognized output: {_reset_out[:300]}")
        except subprocess.TimeoutExpired:
            log.error("R-CATCHUP: roster reset --if-due timed out after 120s")
        except Exception as _reset_e:
            log.error(f"R-CATCHUP: roster reset --if-due error: {_reset_e}")

    if _stripped.upper().startswith("APPROVE "):
        _op_id = _stripped[8:].strip()
        if _op_id:
            try:
                import importlib.util as _r125_ilu, importlib.machinery as _r125_ilm
                _r125_spec = _r125_ilu.spec_from_loader("r125_approval_resolver",
                    _r125_ilm.SourceFileLoader("r125_approval_resolver",
                        "/Users/DIANE/.hermes/bin/r125_approval_resolver.py"))
                _r125_mod = _r125_ilu.module_from_spec(_r125_spec)
                _r125_spec.loader.exec_module(_r125_mod)
                _r125_ok, _r125_msg = _r125_mod.resolve_approval(_op_id)
                _bb_send(chat_id, _r125_msg)
                log.info(f"R-125: approval {'ok' if _r125_ok else 'failed'} op={_op_id}")
                if _r125_ok:
                    _maybe_trigger_overdue_reset(chat_id)
            except Exception as _r125_e:
                log.error(f"R-125: resolver error: {_r125_e}")
                _bb_send(chat_id, f"⚠️ Approval resolver error — {_r125_e}")
            return {"action": "skip", "reason": "r125_approval_resolver"}
    if _stripped.upper().startswith("REJECT ") or _stripped.upper().startswith("DO NOT APPROVE "):
        if _stripped.upper().startswith("REJECT "):
            _rej_rest = _stripped[7:].strip()
        else:
            _rej_rest = _stripped[15:].strip()
        _rej_parts = _rej_rest.split(None, 1)
        _op_id = _rej_parts[0] if _rej_parts else ""
        _reason = _rej_parts[1] if len(_rej_parts) > 1 else ""
        if _op_id:
            try:
                import importlib.util as _r125_ilu, importlib.machinery as _r125_ilm
                _r125_spec = _r125_ilu.spec_from_loader("r125_approval_resolver",
                    _r125_ilm.SourceFileLoader("r125_approval_resolver",
                        "/Users/DIANE/.hermes/bin/r125_approval_resolver.py"))
                _r125_mod = _r125_ilu.module_from_spec(_r125_spec)
                _r125_spec.loader.exec_module(_r125_mod)
                _r125_ok, _r125_msg = _r125_mod.resolve_rejection(_op_id, _reason)
                _bb_send(chat_id, _r125_msg)
                log.info(f"R-125: rejection {'ok' if _r125_ok else 'failed'} op={_op_id}")
                if _r125_ok:
                    _maybe_trigger_overdue_reset(chat_id)
            except Exception as _r125_e:
                log.error(f"R-125: resolver error: {_r125_e}")
                _bb_send(chat_id, f"⚠️ Rejection resolver error — {_r125_e}")
            return {"action": "skip", "reason": "r125_approval_resolver"}
    if _stripped.upper() in ("PENDING", "PENDING APPROVALS", "APPROVALS"):
        try:
            import importlib.util as _r125_ilu, importlib.machinery as _r125_ilm
            _r125_spec = _r125_ilu.spec_from_loader("r125_approval_resolver",
                _r125_ilm.SourceFileLoader("r125_approval_resolver",
                    "/Users/DIANE/.hermes/bin/r125_approval_resolver.py"))
            _r125_mod = _r125_ilu.module_from_spec(_r125_spec)
            _r125_spec.loader.exec_module(_r125_mod)
            _bb_send(chat_id, _r125_mod.format_pending_summary())
        except Exception as _r125_e:
            _bb_send(chat_id, f"⚠️ Pending approvals error — {_r125_e}")
        return {"action": "skip", "reason": "r125_pending_list"}
    # ── End R-125 ─────────────────────────────────────────────────────────────

    # ── R-092b: on-demand reconciliation rerun ───────────────────────────────
    if _stripped.upper() in ("RERUN RECONCILIATION", "RUN RECONCILIATION"):
        try:
            import re as _recon_re
            _recon_r = subprocess.run(
                ["/Users/DIANE/.hermes/hermes-agent/venv/bin/python3",
                 "/Users/DIANE/.hermes/bin/reconciliation_r092b.py", "--force"],
                capture_output=True, text=True, timeout=60
            )
            _recon_out = _recon_r.stdout.strip() or _recon_r.stderr.strip()
            _op_match = _recon_re.search(r"operation_id:\s*(\S+)", _recon_out)
            if _op_match:
                log.info(f"R-092b: manual rerun complete, operation_id={_op_match.group(1)} (reconciliation_r092b.py already sent the iMessage summary)")
            else:
                _bb_send(chat_id, f"[TONY] Reconciliation rerun finished (exit={_recon_r.returncode}) but couldn't parse operation_id. Raw output:\n{_recon_out[:500]}")
            log.info(f"R-092b: manual rerun triggered, exit={_recon_r.returncode}")
        except subprocess.TimeoutExpired:
            _bb_send(chat_id, "[TONY] Reconciliation rerun timed out after 60s -- check reconciliation.log on the machine.")
            log.error("R-092b: manual rerun timed out")
        except Exception as _recon_e:
            log.error(f"R-092b: manual rerun error: {_recon_e}")
            _bb_send(chat_id, f"⚠️ Reconciliation rerun error -- {_recon_e}")
        return {"action": "skip", "reason": "r092b_manual_rerun"}
    # ── End R-092b rerun ──────────────────────────────────────────────────────

    # ── R-DOC/SHEET create command (PATCH-026) ──────────────────────────────────
    import re as _create_re
    _create_m = _create_re.match(r'^(CREATE (?:DOC|DOCUMENT)|CREATE SHEET|CREATE SPREADSHEET)\s+(.+)$', _stripped, _create_re.IGNORECASE)
    if _create_m:
        _create_kind = _create_m.group(1).upper()
        _create_name = _create_m.group(2).strip()
        _create_mime = "application/vnd.google-apps.spreadsheet" if "SHEET" in _create_kind else "application/vnd.google-apps.document"
        try:
            import urllib.error as _create_urlerr
            _create_payload = json.dumps({"name": _create_name, "mime_type": _create_mime}).encode()
            _create_req = urllib.request.Request(
                "http://localhost:9000/tools/gws/drive/create",
                data=_create_payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            try:
                with urllib.request.urlopen(_create_req, timeout=30) as _create_resp:
                    _create_body = json.loads(_create_resp.read().decode())
            except _create_urlerr.HTTPError as _create_he:
                _create_body = json.loads(_create_he.read().decode())
            if _create_body.get("success"):
                _create_id = _create_body["data"]["id"]
                _create_link = (f"https://docs.google.com/spreadsheets/d/{_create_id}/edit"
                                 if "spreadsheet" in _create_mime else f"https://docs.google.com/document/d/{_create_id}/edit")
                _bb_send(chat_id, f"[DIANE] Created \"{_create_name}\" -- {_create_link}")
            else:
                _bb_send(chat_id, f"[DIANE] Couldn't create it -- {_create_body.get('detail', _create_body.get('error', 'unknown error'))}")
            log.info(f"R-DOC-CREATE: {_create_kind} '{_create_name}' success={_create_body.get('success')}")
        except Exception as _create_e:
            log.error(f"R-DOC-CREATE: error: {_create_e}")
            _bb_send(chat_id, f"⚠️ Create command error -- {_create_e}")
        return {"action": "skip", "reason": "doc_sheet_create"}
    # ── End R-DOC/SHEET create command ───────────────────────────────────────────

    # FM-45: SYS_CANARY hard filter — drop before any routing, agent dispatch, or logging
    if "[SYS_CANARY:" in (message_text or ""):
        log.info(f"squad-router: [SYS_CANARY] inbound dropped — canary filter (FM-45)")
        return {"action": "skip", "reason": "canary_filter"}

    # BACK TO DIANE — clear active agent
    if message_text.strip().lower() in ["back to diane", "back to diane.", "diane"]:
        _clear_active_agent()
        _bb_send(chat_id, "Back to DIANE.")
        return {"action": "skip", "reason": "squad-router"}

    # ACTIVE AGENT PERSISTENCE — route to current agent if one is active
    _active_agent, _active_chat = _get_active_agent()
    if _active_agent and _active_chat == chat_id:
        lower_check = message_text.strip().lower()
        # Don't intercept explicit agent prefix switches
        if not any(lower_check.startswith(m) for m in ["dan", "tony", "argus", "lynch", "tesla", "philo", "andy", "hank", "diane"]):
            # R-071: DAN fast-paths apply even under active agent persistence
            if _active_agent == "dan":
                if _dan_fast_path(message_text, chat_id):
                    log.info(f"squad-router: DAN fast-path handled (active agent): {message_text[:40]}")
                    return {"action": "skip", "reason": "squad-router"}
            log.info(f"squad-router: active agent persistence → {_active_agent.upper()}")
            import threading as _th
            _th.Thread(target=_run_squad_member, args=(_active_agent, message_text, chat_id), daemon=False).start()
            return {"action": "skip", "reason": "squad-router"}

    text = (message_text or "").strip()
    lower = text.lower()

    key = _dedup_key(chat_id, text)
    with _hash_lock:
        import time as _time
        _now = _time.time()
        if key in _recent_hashes and _now - _recent_hashes[key] < _HASH_TTL:
            return None
        _recent_hashes[key] = _now
        expired = [k for k, v in _recent_hashes.items() if _now - v > _HASH_TTL]
        for k in expired:
            del _recent_hashes[k]

    with _dispatch_lock:
        # GAP SOLICITATION FAST-PATH — R-012
        gap_triggers = ["got any more", "keep going", "ask me another", "more?", "next question",
                        "what else", "gap session", "tony gap", "dan gap", "andy gap",
                        "lynch gap", "tesla gap", "argus gap", "stop asking", "that's enough",
                        "no more questions", "enough questions"]
        is_gap = any(t in lower for t in gap_triggers)
        if is_gap:
            try:
                import sys
                sys.path.insert(0, "/Users/DIANE/.hermes/bin")
                import importlib.util
                spec = importlib.util.spec_from_file_location("gap_solicitation", "/Users/DIANE/.hermes/bin/gap_solicitation.py")
                gap_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(gap_mod)
                gap_mod.handle_gap_request(text, chat_id)
            except Exception as e:
                log.error(f"squad-router: gap solicitation error: {e}")
            return {"action": "skip", "reason": "squad-router"}

        # AGENT PREFIX FAST-PATH — explicit "DAN," or "TONY," prefix routes directly, bypasses all intercepts
        for _prefix_agent in ["dan", "tony", "argus", "lynch", "tesla", "philo", "andy", "hank"]:
            if lower.startswith(_prefix_agent + ",") or lower.startswith(_prefix_agent + " "):
                if _prefix_agent == lower.split()[0].rstrip(",").lower():
                    _msg_body = text[len(_prefix_agent):].strip().lstrip(",:- ")
                    # R-071: DAN fast-path — deterministic before LLM
                    if _prefix_agent == "dan" and _dan_fast_path(_msg_body, chat_id):
                        log.info(f"squad-router: DAN fast-path handled (prefix): {_msg_body[:40]}")
                        return {"action": "skip", "reason": "squad-router"}
                    log.info(f"squad-router: prefix fast-path → {_prefix_agent.upper()}")
                    threading.Thread(target=_run_squad_member, args=(_prefix_agent, text, chat_id), daemon=False).start()
                    return {"action": "skip", "reason": "squad-router"}

        # TONY PRODUCT INTERVIEW — route answers when interview is active
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("tony_pi", "/Users/DIANE/.hermes/bin/tony_product_interview.py")
            tony_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tony_mod)
            if tony_mod.is_interview_active():
                stop_words = ["stop", "cancel", "quit", "never mind"]
                if any(s in lower for s in stop_words):
                    state = tony_mod.load_state()
                    state["active"] = False
                    tony_mod.save_state(state)
                    _bb_send(chat_id, "[TONY] Interview cancelled.")
                    return {"action": "skip", "reason": "squad-router"}
                tony_mod.handle_product_answer(text, chat_id)
                return {"action": "skip", "reason": "squad-router"}
        except Exception as e:
            log.error(f"squad-router: tony interview check error: {e}")

        # DATE FAST-PATH — intercept date/time queries instantly
        date_kw = ("date" in lower or "what day" in lower) and any(w in lower for w in ["today", "current", "what"])
        if date_kw:
            try:
                today = __import__('subprocess').run(['date', '+%A %B %d, %Y'], capture_output=True, text=True).stdout.strip()
                now = __import__('subprocess').run(['date', '+%I:%M %p'], capture_output=True, text=True).stdout.strip()
                _bb_send(chat_id, f"Today is {today}. Current time is {now}.")
            except Exception as e:
                _bb_send(chat_id, f"Date error: {e}")
            return {"action": "skip", "reason": "squad-router"}

        diane_prefix = lower.startswith("diane")
        
        read_kw = any(w in lower for w in ["read", "show", "open", "check", "summarize", "interpret", "interpret"])
        drive_kw = any(w in lower for w in ["doc", "sheet", "file", "google", "drive", "spreadsheet", "roadmap"])
        implicit_diane = read_kw and drive_kw and not any(lower.startswith(m) for m in SQUAD_MEMBERS)

        if diane_prefix or implicit_diane:
            store_kw = any(w in lower for w in ["store", "remember", "save", "ingest", "archive"])
            if diane_prefix and store_kw:
                # Detect agent routing: "DIANE have TONY store this: ..."
                agent_tag = "diane"
                extra_tags = "diane-store,inbox"
                for m in SQUAD_MEMBERS:
                    if f"have {m} store" in lower or f"tell {m} store" in lower:
                        agent_tag = m
                        extra_tags = f"diane-store,{m},{m}-store"
                        break

                content_start = max(
                    lower.find("store this"), lower.find("remember this"),
                    lower.find("save this"), lower.find("ingest this"),
                    lower.find("archive this")
                )
                if content_start != -1:
                    remainder = text[content_start:]
                    for _trig in ("store this", "remember this", "save this", "ingest this", "archive this"):
                        if remainder.lower().startswith(_trig):
                            remainder = remainder[len(_trig):]
                            break
                    remainder = remainder.strip()
                    # Only strip a colon if it's immediately at the start (no label between
                    # trigger and colon). If there IS a label ("...this test code: value"),
                    # keep it intact as recall context — R-132-adjacent fix, 2026-09-16.
                    if remainder.startswith(":"):
                        remainder = remainder[1:].strip()
                    store_text = remainder
                else:
                    store_text = text[6:].strip()
                if store_text:
                    import subprocess as _sp
                    _sp.Popen([
                        "/Users/DIANE/.hermes/hermes-agent/venv/bin/python3",
                        "/Users/DIANE/.hermes/bin/hindsight-store.py",
                        "--text", store_text,
                        "--tags", extra_tags,
                        "--layer", "knowledge",
                        "--source", "iMessage"
                    ])
                    agent_label = f"for {agent_tag.upper()} " if agent_tag != "diane" else ""
                    _bb_send(chat_id, f"✅ Stored {agent_label}in Hindsight — {len(store_text)} chars.")
                else:
                    _bb_send(chat_id, "⚠️ DIANE store: no content found. Try: 'DIANE store this: [text]' or 'DIANE have TONY store this: [text]'")
                log.info(f"squad-router: DIANE store-this fast-path (agent={agent_tag})")
                return {"action": "skip", "reason": "squad-router"}

            if diane_prefix and any(w in lower for w in ["context for c", "context for claude", "render context", "rerender context"]):
                log.info("squad-router: DIANE context-for-C fast-path — rendering architect_state")
                import subprocess as _sp
                r1 = _sp.run(["python3", "/Users/DIANE/.hermes/bin/render_architect_state.py"], capture_output=True, text=True, timeout=30)
                r2 = _sp.run(["python3", "/Users/DIANE/.hermes/bin/render_c_context.py"], capture_output=True, text=True, timeout=15)
                if r1.returncode == 0:
                    token = r1.stdout.strip()
                    _bb_send(chat_id, f"Context rendered. {token}")
                else:
                    _bb_send(chat_id, f"Render FAILED — fallback restored. {r1.stderr[:200]}")
                return {"action": "skip", "reason": "squad-router"}

            sheet_kw = any(w in lower for w in ["read", "show", "open", "check", "summarize", "interpret"])
            sheet_target = any(w in lower for w in ["doc", "sheet", "file", "google", "drive", "spreadsheet"])
            
            if sheet_kw:
                log.info("squad-router: DIANE sheet-read fast-path")
                _diane_read_document(text, chat_id)
                return {"action": "skip", "reason": "squad-router"}

            if any(w in lower for w in ["create", "new", "make"]):
                if any(w in lower for w in ["sheet", "spreadsheet"]):
                    name = _extract_name(text, ["sheet called", "spreadsheet called", "sheet named", "create sheet", "new sheet"])
                    log.info(f"squad-router: DIANE create-sheet fast-path: {name}")
                    _direct_drive_create(name, "application/vnd.google-apps.spreadsheet", chat_id)
                    return {"action": "skip", "reason": "squad-router"}
                if any(w in lower for w in ["doc", "document"]):
                    name = _extract_name(text, ["doc called", "document called", "doc named", "create doc", "new doc"])
                    log.info(f"squad-router: DIANE create-doc fast-path: {name}")
                    _direct_drive_create(name, "application/vnd.google-apps.document", chat_id)
                    return {"action": "skip", "reason": "squad-router"}

            if any(w in lower for w in ["list drive", "my drive", "google drive", "what's in drive", "files in drive"]):
                log.info("squad-router: DIANE drive-list fast-path")
                _direct_drive_list(chat_id)
                return {"action": "skip", "reason": "squad-router"}

        for member in SQUAD_MEMBERS:
            if lower.startswith(member):
                msg_body = text[len(member):].strip().lstrip(",:- ")

                if member == "dan":
                    if _dan_fast_path(msg_body, chat_id):
                        log.info(f"squad-router: DAN fast-path handled: {msg_body[:40]}")
                        return {"action": "skip", "reason": "squad-router"}

                if member == "argus" and any(w in lower for w in ["cron", "status", "backup", "audit", "jobs"]):
                    log.info("squad-router: ARGUS status fast-path")
                    _direct_argus_status(chat_id)
                    return {"action": "skip", "reason": "squad-router"}


                if member == "hank":
                    log.info(f"squad-router: HANK fast-path: {msg_body[:50]}")
                    t = threading.Thread(target=_hank_fast_path, args=(msg_body, chat_id), daemon=False)
                    t.start()
                    return {"action": "skip", "reason": "squad-router"}

                if member == "tesla":
                    # R-142: "tesla status" → board summary
                    if "status" in lower:
                        try:
                            import importlib.util as _ilu, importlib.machinery as _ilm
                            _spec = _ilu.spec_from_loader("r142_task_board",
                                _ilm.SourceFileLoader("r142_task_board",
                                    "/Users/DIANE/.hermes/bin/r142_task_board.py"))
                            _tb = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_tb)
                            _bb_send(chat_id, _tb.format_status_summary())
                        except Exception as _tbe:
                            _bb_send(chat_id, f"TESLA board unavailable: {_tbe}")
                        return {"action": "skip", "reason": "r142_tesla_status"}

                    # R-142: "tesla, new project: [idea]" or "tesla, [idea]" → task envelope
                    idea = msg_body.replace("new project:", "").replace("new project", "").strip().lstrip(":").strip()
                    if idea:
                        # Create R-142 task envelope with acceptance_criteria
                        try:
                            import importlib.util as _ilu2, importlib.machinery as _ilm2
                            _spec2 = _ilu2.spec_from_loader("r142_task_board",
                                _ilm2.SourceFileLoader("r142_task_board",
                                    "/Users/DIANE/.hermes/bin/r142_task_board.py"))
                            _tb2 = _ilu2.module_from_spec(_spec2); _spec2.loader.exec_module(_tb2)
                            _task = _tb2.enqueue_task(
                                creator="mat",
                                title=idea[:100],
                                description=idea,
                                acceptance_criteria=[
                                    "TESLA verdict rendered (PURSUE NOW / BACKLOG / PAUSE FOR RESEARCH / RETIRE)",
                                    "Structured execution plan produced with phases and squad assignments",
                                    "First 3 actions defined with owners and timeframes",
                                    "Result delivered via iMessage",
                                ],
                                sandbox=True,
                                timeout_minutes=30,
                                reply_target=chat_id,
                            )
                            log.info(f"R-142: task envelope created task_id={_task['task_id']} title={idea[:50]}")
                        except Exception as _tqe:
                            log.warning(f"R-142: task enqueue failed (non-blocking): {_tqe}")
                        # Run TESLA evaluation (existing path)
                        log.info(f"squad-router: TESLA new project fast-path: {idea[:50]}")
                        t = threading.Thread(target=_tesla_new_project, args=(idea, chat_id), daemon=False)
                        t.start()
                        return {"action": "skip", "reason": "squad-router"}

                log.info(f"squad-router: routing to {member}")
                # R-065: 120s timebox — hung specialist must not stall graph
                def _timed_squad_call(agent=member, msg=msg_body, cid=chat_id):
                    import concurrent.futures as _cf
                    with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                        _fut = _ex.submit(_run_squad_member, agent, msg, cid)
                        try:
                            _fut.result(timeout=120)
                        except _cf.TimeoutError:
                            log.error(f"squad-router: R-065 timebox — {agent} exceeded 120s, aborting")
                            _bb_send(cid, f"[{agent.upper()}] Request timed out after 120s. Please try again.")
                t = threading.Thread(target=_timed_squad_call, daemon=False)
                t.start()
                return {"action": "skip", "reason": "squad-router"}

    # DEFAULT: route to DIANE — she responds to everything not explicitly addressed to a squad member
    log.info(f"squad-router: no prefix match — routing to DIANE (default)")
    import threading as _th
    _th.Thread(target=_run_squad_member, args=("diane", text, chat_id), daemon=False).start()
    return {"action": "skip", "reason": "squad-router"}

def _pre_gateway_dispatch_hook(event, gateway=None, session_store=None, **kwargs):
    log_hook = logging.getLogger('hermes_plugins.squad_router')
    log_hook.info(f'squad-router hook fired: text={getattr(event,"text","")[:40]}')
    _raw = getattr(event, "_raw", None) or getattr(event, "raw", None) or {}
    _etype = _raw.get("type") or _raw.get("event") or ""
    if _etype == "updated-message":
        return {"action": "skip", "reason": "bb-updated-message-filtered"}
    if _is_duplicate_message(event):
        logging.getLogger("hermes_plugins.squad_router").info("squad-router: duplicate GUID suppressed")
        return {"action": "skip", "reason": "squad-router-dedup"}
    log = logging.getLogger("hermes_plugins.squad_router")
    try:
        text = getattr(event, "text", None) or getattr(event, "content", None) or ""
        source = getattr(event, "source", None)
        chat_id = None
        if source:
            chat_id = getattr(source, "chat_id", None) or getattr(source, "user_id", None)
        if not chat_id:
            chat_id = f"iMessage;-;{PHONE}"
        result = handle_message(text, chat_id)
        if result is not None:
            return {"action": "skip", "reason": "squad-router"}
        return {"action": "allow"}
    except Exception as e:
        import traceback
        log.error(f"squad-router hook error: {e}\n{traceback.format_exc()}")
        return {"action": "allow"}

def register(ctx):
    import os
    if os.environ.get("HERMES_DISABLE_SQUAD_FASTPATH", "") == "1":
        logging.getLogger("hermes_plugins.squad_router").warning(
            "squad-router: fast-path DISABLED via HERMES_DISABLE_SQUAD_FASTPATH -- "
            "iMessage now routes through normal gateway/init_agent path (2026-09-20, "
            "reproducible malformed-tool-call-as-text bug on fast-path, see EOD)"
        )
    else:
        ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch_hook)
    import logging
    logging.getLogger("hermes_plugins.squad_router").info("squad-router: registered v2.2 — DIANE fast-paths + DAN/TONY/ARGUS/LYNCH/TESLA/PHILO/ANDY/HANK")