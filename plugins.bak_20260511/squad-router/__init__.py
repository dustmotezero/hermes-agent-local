import os, subprocess, json, urllib.request, uuid, threading
from pathlib import Path

BB_URL = "http://localhost:1234"
BB_PASSWORD = os.getenv("BLUEBUBBLES_PASSWORD", "IktseMM33")
BB_SEND_TIMEOUT = 30
SUBPROCESS_TIMEOUT = 60
HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
HERMES_BIN = str(HERMES_HOME / "hermes-agent" / "venv" / "bin" / "hermes")
DRIVE_CREATE = str(HERMES_HOME / "tool_server" / "bin" / "drive-create")
DRIVE_LIST = str(HERMES_HOME / "tool_server" / "bin" / "drive-list")
SHEETS_OPS = str(HERMES_HOME / "tool_server" / "bin" / "sheets-ops")
JOBS_JSON = str(HERMES_HOME / "profiles" / "diane" / "cron" / "jobs.json")
PHONE = "+12488448838"

_dispatch_lock = threading.Lock()
_recent_hashes = set()
_hash_lock = threading.Lock()

def _bb_send(chat_id, text):
    if ";" not in chat_id:
        chat_id = f"iMessage;-;{chat_id}"
    chat_id = chat_id.replace("any;-;", "iMessage;-;")
    payload = json.dumps({"chatGuid": chat_id, "tempGuid": f"temp-{uuid.uuid4().hex}", "message": text}).encode()
    req = urllib.request.Request(f"{BB_URL}/api/v1/message/text?password={BB_PASSWORD}", data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=BB_SEND_TIMEOUT) as r:
            return r.status < 300
    except Exception as e:
        import logging
        logging.getLogger("hermes_plugins.squad_router").error(f"squad-router: BB send failed: {e}")
        return False

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

def _direct_read(text, chat_id):
    try:
        import re
        name_match = None
        for pattern in [
            r'(?:read|show|open|check)\s+(?:the\s+)?(?:google\s+)?(?:doc|sheet|file|spreadsheet)\s+["\']?([^"\']+?)["\']?(?:\s|$)',
            r'["\']([^"\']+)["\']',
            r'(?:read|show|open|check)\s+(?:the\s+)?(.+?)(?:\s+(?:doc|sheet|file))?$',
        ]:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                name_match = m.group(1).strip()
                break

        if not name_match:
            words = text.split()
            name_match = ' '.join(words[-3:])

        import sys
        sys.path.insert(0, str(HERMES_HOME / "tool_server"))
        gws = str(HERMES_HOME / ".local" / "bin" / "gws")
        if not Path(gws).exists():
            gws = str(Path.home() / ".local" / "bin" / "gws")

        _bb_send(chat_id, f"Reading that for you...")

        env = {**os.environ, "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND": "file"}
        r = subprocess.run(
            [gws, "drive", "files", "list", "--json"],
            capture_output=True, text=True, timeout=15, env=env,
            cwd=str(HERMES_HOME / "profiles" / "diane")
        )
        files = json.loads(r.stdout) if r.stdout.strip().startswith("[") else []
        name_lower = name_match.lower()
        match = next((f for f in files if name_lower in f.get("name","").lower()), None)

        if not match:
            _bb_send(chat_id, f"Couldn't find a file matching '{name_match}' in Drive.")
            return True

        file_id = match["id"]
        mime = match.get("mimeType","")
        export_dir = str(HERMES_HOME / "profiles" / "diane")

        if "spreadsheet" in mime:
            r2 = subprocess.run(
                [gws, "sheets", "+read", "--spreadsheet", file_id, "--range", "A1:Z200"],
                capture_output=True, text=True, timeout=15, env=env,
                cwd=export_dir
            )
            content = r2.stdout.strip()[:1500]
        else:
            r2 = subprocess.run(
                [gws, "drive", "files", "export", file_id, "text/plain", "export_tmp.txt"],
                capture_output=True, text=True, timeout=15, env=env,
                cwd=export_dir
            )
            export_path = Path(export_dir) / "export_tmp.txt"
            content = export_path.read_text(encoding="utf-8")[:1500] if export_path.exists() else ""

        if not content:
            _bb_send(chat_id, f"File found but couldn't read content.")
            return True

        llm_prompt = f"Summarize this document content in 3-5 sentences for a mobile iMessage reply. Be concise:\n\n{content}"
        llm_payload = json.dumps({"model": "hermes-3", "messages": [{"role": "user", "content": llm_prompt}], "max_tokens": 400}).encode()
        llm_req = urllib.request.Request("http://127.0.0.1:8080/v1/chat/completions", data=llm_payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(llm_req, timeout=45) as resp:
            llm_data = json.loads(resp.read())
        summary = llm_data["choices"][0]["message"]["content"].strip()
        _bb_send(chat_id, f"{match['name']}:\n{summary}")
        return True
    except Exception as e:
        import logging
        logging.getLogger("hermes_plugins.squad_router").error(f"squad-router: read fast-path error: {e}")
        return False

def _direct_roster_query(chat_id):
    try:
        r = subprocess.run(["python3", SHEETS_OPS, "read-day"], capture_output=True, text=True, timeout=15)
        _bb_send(chat_id, r.stdout.strip()[:1500] or "No roster data found.")
        return True
    except Exception:
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

def _run_squad_member(agent, message, chat_id):
    try:
        r = subprocess.run(
            [HERMES_BIN, "-z", "--profile", agent.lower(), message],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT
        )
        out = (r.stdout or "").strip()
        if out:
            _bb_send(chat_id, out[:1500])
        return True
    except subprocess.TimeoutExpired:
        _bb_send(chat_id, f"{agent} is thinking... reply again if no response in 60s.")
        return True
    except Exception:
        return False

SQUAD_MEMBERS = ["dan", "tony", "argus", "lynch", "tesla", "philo", "andy"]

def handle_message(message_text, chat_id, context=None):
    import re, logging
    log = logging.getLogger("hermes_plugins.squad_router")

    text = (message_text or "").strip()
    lower = text.lower()

    key = _dedup_key(chat_id, text)
    with _hash_lock:
        if key in _recent_hashes:
            return None
        _recent_hashes.add(key)
        if len(_recent_hashes) > 200:
            _recent_hashes.clear()

    with _dispatch_lock:
        diane_prefix = lower.startswith("diane")
        
        read_kw = any(w in lower for w in ["read", "show", "open", "check", "summarize", "interpret"])
        drive_kw = any(w in lower for w in ["doc", "sheet", "file", "google", "drive", "spreadsheet", "roadmap"])
        implicit_diane = read_kw and drive_kw and not any(lower.startswith(m) for m in SQUAD_MEMBERS)

        if diane_prefix or implicit_diane:
            sheet_kw = any(w in lower for w in ["read", "show", "open", "check", "summarize"])
            sheet_target = any(w in lower for w in ["doc", "sheet", "file", "google", "drive", "spreadsheet"])
            
            if sheet_kw and sheet_target:
                log.info("squad-router: DIANE sheet-read fast-path")
                _direct_read(text, chat_id)
                return ""

            if any(w in lower for w in ["create", "new", "make"]):
                if any(w in lower for w in ["sheet", "spreadsheet"]):
                    name = _extract_name(text, ["sheet called", "spreadsheet called", "sheet named", "create sheet", "new sheet"])
                    log.info(f"squad-router: DIANE create-sheet fast-path: {name}")
                    _direct_drive_create(name, "application/vnd.google-apps.spreadsheet", chat_id)
                    return ""
                if any(w in lower for w in ["doc", "document"]):
                    name = _extract_name(text, ["doc called", "document called", "doc named", "create doc", "new doc"])
                    log.info(f"squad-router: DIANE create-doc fast-path: {name}")
                    _direct_drive_create(name, "application/vnd.google-apps.document", chat_id)
                    return ""

            if any(w in lower for w in ["list drive", "my drive", "google drive", "what's in drive", "files in drive"]):
                log.info("squad-router: DIANE drive-list fast-path")
                _direct_drive_list(chat_id)
                return ""

        for member in SQUAD_MEMBERS:
            if lower.startswith(member):
                msg_body = text[len(member):].strip().lstrip(",:- ")

                if member == "dan" and any(w in lower for w in ["student", "roster", "schedule", "today", "who"]):
                    log.info("squad-router: DAN roster fast-path")
                    _direct_roster_query(chat_id)
                    return ""

                if member == "argus" and any(w in lower for w in ["cron", "status", "backup", "audit", "jobs"]):
                    log.info("squad-router: ARGUS status fast-path")
                    _direct_argus_status(chat_id)
                    return ""

                log.info(f"squad-router: routing to {member}")
                threading.Thread(target=_run_squad_member, args=(member, msg_body, chat_id), daemon=True).start()
                return ""

    return None

def _pre_gateway_dispatch_hook(event, gateway=None, session_store=None, **kwargs):
    import logging
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
        log.error(f"squad-router hook error: {e}")
        return {"action": "allow"}

def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch_hook)
    import logging
    logging.getLogger("hermes_plugins.squad_router").info("squad-router: registered v2.1 — DIANE fast-paths + DAN/TONY/ARGUS/LYNCH/TESLA/PHILO/ANDY")
