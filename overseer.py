#!/usr/bin/env python3
"""Overseer: always-on-top list of live Claude + Hermes + Codex sessions (CLI and desktop apps).

Row: top = name, busy share (last 30 min), session age, model; bottom = folder + git branch, turns,
memory, context size, and a timeline of the context produced over 30 min.
Age colour = prompt-cache window: green < --warn, orange < --limit, red beyond, grey while busy.
A yellow ⚠ (error in the last 30 min) or purple ? (waiting for you) opens a detail line when clicked.
Click a row to focus its terminal (Konsole, tmux, herdr) or app window.

  ./overseer.py          GUI
  ./overseer.py --list   print once to stdout (no GUI)

Themes: dark (default), light, terminal, crt, steampunk, cga, yacht.
Switch live with the ◐ button or right-click > Theme; --theme NAME starts with it.

Account limits bar: see README.
"""
import argparse
import glob
import itertools
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import traceback
from datetime import datetime
from xml.sax.saxutils import escape

from themes import CSS, THEMES, heat, heat_percents, load_theme, save_theme, write_art

HOME = os.path.expanduser("~")
# Data dirs of the watched tools: the tool's env variable if set, else its default. The overseer reads ITS OWN
# environment, so it only follows a variable that is set in the session that launched it (not one set in a
# tool's own session).
CLAUDE_DIR = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or f"{HOME}/.claude")
CODEX = os.path.expanduser(os.environ.get("CODEX_HOME") or f"{HOME}/.codex")
HERMES_DIR = os.path.expanduser(os.environ.get("HERMES_HOME") or f"{HOME}/.hermes")
OPENCODE_DIR = os.path.join(os.path.expanduser(os.environ.get("XDG_DATA_HOME") or f"{HOME}/.local/share"), "opencode")
CLK = os.sysconf("SC_CLK_TCK")


# ---------------------------------------------------------------- /proc helpers
def proc_stat(pid):
    """(ppid, tty_nr, starttime_ticks) or None if the process is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except OSError:
        return None
    rest = data[data.rindex(")") + 2:].split()
    return int(rest[1]), int(rest[4]), int(rest[19])


def proc_start_epoch(pid):
    st = proc_stat(pid)
    if not st:
        return None
    with open("/proc/uptime") as f:
        up = float(f.read().split()[0])
    return time.time() - up + st[2] / CLK


def proc_table():
    """{pid: (ppid, tty_nr, starttime_ticks, comm, cmdline bytes)} for every process: one pass over /proc."""
    table = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                data = f.read()
            rest = data[data.rindex(")") + 2:].split()
            comm = data[data.index("(") + 1:data.rindex(")")]
            try:
                with open(f"/proc/{name}/cmdline", "rb") as f:
                    cmd = f.read()
            except OSError:
                cmd = b""
            table[int(name)] = (int(rest[1]), int(rest[4]), int(rest[19]), comm, cmd)
        except (OSError, ValueError, IndexError):
            continue
    return table


_PROCS = None  # the table of the collection in progress (None outside one)


def procs():
    """The process table of the running collection, or a fresh one when called outside a collection."""
    return _PROCS if _PROCS is not None else proc_table()


def collecting():
    """Context manager: one process table for everything a collection lists, dropped when it ends."""
    class Scope:
        def __enter__(self):
            global _PROCS
            _PROCS = proc_table()

        def __exit__(self, *exc):
            global _PROCS
            _PROCS = None
    return Scope()


class Row:
    def __init__(self, kind, name, cwd, pid, busy, since, note="", model="",
                 ctx=None, window=None, started=None, waiting="", turns=None, error=None, focus_pid=None,
                 url=None):
        self.kind, self.name, self.cwd, self.pid = kind, name, cwd, pid
        self.busy, self.since, self.note = busy, since, note
        self.ctx, self.window, self.started = ctx, window, started  # context tokens, max, epoch
        self.bg_short = None  # background Claude session: first 8 chars of its id
        self.focus_pid = focus_pid  # process whose terminal to focus when it differs from pid (background sessions)
        self.detached = False  # background Claude session no terminal shows: nothing to focus
        self.url = url  # deep link that opens exactly this session in its desktop app
        self.error = error  # (epoch, short text) of the last API error seen, None if none
        self.turns = turns  # number of prompts the user sent in the session
        self.waiting = waiting  # what the agent waits for from the user ("" = not waiting)
        self.model = model.rsplit("/", 1)[-1].removeprefix("claude-")  # short form

    @property
    def age(self):
        return max(0, time.time() - self.since)


# ---------------------------------------------------------------- Claude
def claude_transcript(session_id):
    """(model, context tokens, prompts, last error) of the last real assistant message ("", None, None, None)."""
    for path in glob.glob(f"{CLAUDE_DIR}/projects/*/{session_id}.jsonl"):
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 256_000))
                tail = f.read().decode(errors="replace")
        except OSError:
            continue
        models = [m for m in re.findall(r'"model":"([^"]+)"', tail) if m != "<synthetic>"]
        ctx = None
        for u in reversed(re.findall(r'"usage":\{([^{}]*)', tail)):  # prompt = input + cache r/w
            n = sum(int(v) for v in re.findall(
                r'"(?:input_tokens|cache_creation_input_tokens|cache_read_input_tokens)":(\d+)', u))
            if n:
                ctx = n
                break
        if models or ctx:
            return ((models[-1] if models else ""), ctx, count_lines(path, b'"type":"user"', claude_prompt),
                    claude_error(tail))
    return "", None, None, None


_TRANSCRIPT_START = {}  # session id -> epoch of its first transcript line (never changes, so cached)


def claude_session_start(session_id):
    """Epoch a Claude session really began, or None. The pid file keeps the *process* start: a /clear or a new
    session in the same CLI changes the session id but not that date, so the age would stay old."""
    if session_id in _TRANSCRIPT_START:
        return _TRANSCRIPT_START[session_id]
    start = None
    for path in glob.glob(f"{CLAUDE_DIR}/projects/*/{session_id}.jsonl"):
        try:
            with open(path, "rb") as f:
                m = re.search(rb'"timestamp":"([^"]+)"', f.read(65536))
            start = (datetime.fromisoformat(m.group(1).decode()).timestamp() if m else os.path.getmtime(path))
        except (OSError, ValueError):
            continue
        break
    if start is not None:  # a missing transcript may just not be written yet: try again next refresh
        _TRANSCRIPT_START[session_id] = start
    return start


_COUNTS = {}  # (path, marker) -> (bytes read, lines counted): only new lines are parsed


def count_lines(path, marker, accept=lambda line: True):
    """Number of lines of the jsonl `path` that contain `marker` (bytes) and pass accept(line)."""
    pos, n = _COUNTS.get((path, marker), (0, 0))
    try:
        with open(path, "rb") as f:
            f.seek(pos)
            data = f.read()
    except OSError:
        return n or None
    end = data.rfind(b"\n") + 1  # leave a half-written last line for the next time
    n += sum(1 for line in data[:end].splitlines() if marker in line and accept(line))
    _COUNTS[(path, marker)] = (pos + end, n)
    return n or None


def claude_prompt(line):
    """True for a prompt the user typed (a user line that holds text, not a tool result)."""
    try:
        c = json.loads(line)["message"]["content"]
    except (ValueError, KeyError, TypeError):
        return False
    return isinstance(c, str) or any(b.get("type") == "text" for b in c if isinstance(b, dict))


def claude_error(tail):
    """(epoch, text) of the last API error message (isApiErrorMessage) in a transcript tail, else None."""
    for line in reversed(tail.splitlines()):
        if '"isApiErrorMessage":true' not in line:
            continue
        try:
            d = json.loads(line)
            ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
            text = next((b.get("text", "") for b in d["message"]["content"] if b.get("type") == "text"), "")
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        return ts, (text.strip().splitlines() or ["API error"])[0][:110]
    return None


def desktop_title(session_id):
    """(title, model) the Claude Desktop Code tab stores for CLI session `session_id`."""
    for path in glob.glob(f"{HOME}/.config/Claude/claude-code-sessions/*/*/local_*.json"):
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if d.get("cliSessionId") == session_id:
            return d.get("title") or "", d.get("model") or ""
    return "", ""


def claude_rows():
    """One row per live Claude session: the interactive CLIs, plus the background sessions a CLI forked or parked
    (kind "bg": own process, pid file, name and transcript; they are viewed through a console CLI)."""
    rows, parked, claimed = [], {}, {}  # console pid -> id of the job it shows (None: none), epoch it took it
    for path in glob.glob(f"{CLAUDE_DIR}/sessions/*.json"):
        try:
            with open(path) as f:
                d = json.load(f)
            pid = int(d["pid"])
        except (OSError, ValueError, KeyError):
            continue
        st = proc_stat(pid)
        # pid reuse guard: procStart in the file == starttime in /proc
        if not st or str(st[2]) != str(d.get("procStart")):
            continue
        mode = d.get("kind", "interactive")
        if mode not in ("interactive", "bg"):
            continue
        if mode == "interactive":
            parked[pid] = d.get("parkedJobId")
            claimed[pid] = (d.get("updatedAt") or d["startedAt"]) / 1000
        ts = (d.get("statusUpdatedAt") or d.get("updatedAt") or d["startedAt"]) / 1000
        cwd = d.get("cwd", "")
        name, model = d.get("name") or os.path.basename(cwd), ""
        kind = "claude"
        if str(d.get("entrypoint", "")).startswith("claude-desktop"):  # Code tab of the desktop app
            kind = "claude-desktop"
            title, model = desktop_title(d.get("sessionId", ""))
            name = title or name
        tmodel, ctx, turns, err = claude_transcript(d.get("sessionId", ""))
        if mode == "bg" and not (tmodel or ctx or turns):  # a spare worker nobody uses yet
            continue
        # a new session in a running CLI (/clear...) starts later than the process
        started = max(d["startedAt"] / 1000, claude_session_start(d.get("sessionId", "")) or 0)
        row = Row(kind, name, cwd, pid, d.get("status") in ("busy", "shell"), ts, d.get("status", ""),
                  model or tmodel, ctx=ctx, started=started, turns=turns, error=err,
                  # permission prompt, question, open dialog...
                  waiting=d.get("waitingFor") or "input" if d.get("status") == "waiting" else "")
        # the job id names the job (parkedJobId points to it); it differs from the session id once forked
        row.bg_short = (d.get("jobId") or d.get("sessionId", "")[:8]) if mode == "bg" else None
        rows.append(row)
    displaced = follow_attaches(parked, claimed, {r.bg_short for r in rows if r.bg_short})
    rows = [r for r in rows if r.pid not in displaced]
    link_bg_jobs(rows, [r for r in rows if r.pid in parked], parked)
    # a background session no terminal shows and that is not working is parked and may be frozen: not a live session
    rows = [r for r in rows if not (r.detached and not r.busy)]
    # a console that views a job: its own pid file stops updating (it can stay "shell" or "busy" forever), so the
    # job's row stands for the window
    viewers = {r.focus_pid for r in rows if r.bg_short}
    return [r for r in rows if r.bg_short or r.pid not in viewers]


def follow_attaches(parked, claimed, live):
    """Update `parked` (console pid -> job id) with the attaches made from the session list, and return the
    consoles that went back to that list (their job is now shown elsewhere: no session to show).
    Neither pid file changes on such an attach. Its only trace is daemon/attach-journal/<gesture>.json (console
    pid, time, message count, no job id); the count equals the one daemon/roster.json keeps for the attached job
    at its first paint. Both are undocumented Claude internals: when nothing or several jobs match, the console's
    job becomes unknown (None) rather than a guess."""
    counts = {}
    try:
        with open(f"{CLAUDE_DIR}/daemon/roster.json") as f:
            for job, w in json.load(f).get("workers", {}).items():
                n = next((m.get("msgsInJsonl") for m in w.get("firedInteractiveMarks") or []
                          if m.get("kind") == "content_paint"), None)
                if job in live and n is not None:
                    counts[job] = n
    except (OSError, ValueError, AttributeError):
        pass
    latest = {}  # console pid -> (epoch, message count) of its last attach
    for path in glob.glob(f"{CLAUDE_DIR}/daemon/attach-journal/*.json"):
        try:
            with open(path) as f:
                d = json.load(f)
            pid, at, n = int(d["pid"]), d["startedAtEpochMs"] / 1000, d.get("msgsInJsonl")
        except (OSError, ValueError, KeyError, TypeError):
            continue
        st = proc_stat(pid)
        if pid in parked and st and str(st[2]) == str(d.get("procStart")) and at > latest.get(pid, (0,))[0]:
            latest[pid] = (at, n)
    for pid, (at, n) in latest.items():
        if at > claimed[pid]:
            hit = [job for job, c in counts.items() if c == n]
            parked[pid], claimed[pid] = (hit[0] if len(hit) == 1 else None), at
    displaced = set()
    for job in live:  # a job claimed by several consoles is in the one that took it last
        cs = sorted((c for c, j in parked.items() if j == job), key=claimed.get)
        for c in cs[:-1]:
            parked[c] = None
            displaced.add(c)
    return displaced


def link_bg_jobs(rows, consoles, parked):
    """Set focus_pid (the console that shows it) or detached on each background job row.
    1. A console whose parkedJobId is the job's id shows it.
    2. A console only writes parkedJobId when it parks: if it then moves to another job (a /bg or /new inside
       the job's view), its pid file keeps a job that no longer runs. Such a stale console is linked to the one
       unlinked job of its folder that started after it; with two candidates on either side nothing is linked
       (a wrong link would glue two sessions into one row)."""
    jobs = [r for r in rows if r.bg_short]
    for row in jobs:
        row.focus_pid = next((c.pid for c in consoles if parked.get(c.pid) == row.bg_short), None)
    live = {r.bg_short for r in jobs}
    stale = [c for c in consoles if parked.get(c.pid) and parked[c.pid] not in live]
    loose = [r for r in jobs if r.focus_pid is None]
    for cwd in {c.cwd for c in stale}:
        here_c = [c for c in stale if c.cwd == cwd]
        here_j = [r for r in loose if r.cwd == cwd]
        if len(here_c) == 1 == len(here_j) and here_j[0].started >= here_c[0].started - 5:
            here_j[0].focus_pid = here_c[0].pid
    for row in jobs:
        row.detached = row.focus_pid is None


# ---------------------------------------------------------------- Hermes
def same_ns(pid):
    """True if `pid` shares our mount namespace (False for container processes: their files are not ours)."""
    try:
        return os.readlink(f"/proc/{pid}/ns/mnt") == os.readlink("/proc/self/ns/mnt")
    except OSError:
        return False


def hermes_pids():
    pids = []
    for pid, (_, tty, _, _, cmd) in procs().items():
        if b"hermes_cli.main" not in cmd:
            continue
        args = cmd.split(b"\0")
        if b"gateway" in args[-4:] or b"dashboard" in args[-4:]:
            continue
        if tty != 0 and same_ns(pid):  # has a controlling tty = interactive CLI
            pids.append(pid)
    return pids


def hermes_db(pid):
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
            if target.endswith("/state.db"):
                return target
    except OSError:
        pass
    return None


def hermes_turns(con, sid):
    return con.execute("SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'user'", (sid,)).fetchone()[0] or None


def hermes_reason(db, sid):
    """(epoch, provider, 'HTTP 402: Insufficient Balance') from the session's last 'API call failed' line in the
    profile's logs/agent.log (the metrics file does not say why a call failed), else None."""
    path = os.path.join(os.path.dirname(db), "logs", "agent.log")
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 600_000))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if f"[{sid}]" in line and "API call failed" in line:
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*provider=(\S+) .*summary=(.*?)(?: \(request_id: [^)]*\))?$", line)
            if m:
                return time.mktime(time.strptime(m[1], "%Y-%m-%d %H:%M:%S")), m[2], m[3]
    return None


def hermes_error(db, sid):
    """(epoch, text) of the session's last failed API call in the profile's logs/api_metrics.jsonl, with the
    provider the next call went to (the fallback), else None. Hermes calls its primary first, then falls back."""
    path = os.path.join(os.path.dirname(db), "logs", "api_metrics.jsonl")
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 400_000))
            lines = f.read().decode(errors="replace").splitlines()[1:]  # first line may be cut
    except OSError:
        return None
    calls = []
    for line in lines:
        if sid in line:
            try:
                d = json.loads(line)
                calls.append((d["ts"], d["provider"], d.get("error")))
            except (ValueError, KeyError, TypeError):
                pass
    failed = [c for c in calls if c[2]]
    why = hermes_reason(db, sid)
    if not failed:
        return (why[0], f"{why[1]} failed ({why[2]})") if why else None
    ts, prov, err = failed[-1]
    status = f" ({err['status']})" if isinstance(err, dict) and err.get("status") else ""
    if why and why[1] == prov and abs(why[0] - ts) < 120:
        status = f" ({why[2]})"
    nxt = next((c[1] for c in calls if c[0] > ts), prov)
    return ts, f"{prov} failed{status}" + (f" → fell back to {nxt}" if nxt != prov else "")


GATE_AFTER = 15  # s: a tool call with no result for this long, and no tool process running, is a confirmation gate


def tool_running(pid, table=None):
    """True if `pid` has a descendant that is not a multiprocessing worker (Hermes keeps one idle):
    a tool command is running, so the agent is not waiting for the user."""
    table = procs() if table is None else table
    kids = {}
    for k, st in table.items():
        kids.setdefault(st[0], []).append(k)
    stack = list(kids.get(pid, []))
    while stack:
        k = stack.pop()
        if b"multiprocess" not in table[k][4]:
            return True
        stack += kids.get(k, [])
    return False


def hermes_gate(pid, since, role, tool_calls):
    """'terminal: rm -rf x': the pending tool call when Hermes seems to wait for the user to confirm it.
    The CLI prompt leaves no trace on disk, so this is inferred: the last message is a tool call nobody
    answered for GATE_AFTER s and no tool process runs for it (can be wrong for a slow in-process tool)."""
    if role != "assistant" or not tool_calls or time.time() - since < GATE_AFTER or tool_running(pid):
        return ""
    try:
        f = json.loads(tool_calls)[0]["function"]
        args = json.loads(f.get("arguments") or "{}")
        return f"{f['name']}: " + str(args.get("command") or args or "")[:90].replace("\n", " ")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return "confirmation"


def hermes_ctx(model_config):
    """Context tokens of the last API call, from the session's usage anchor (None if absent)."""
    try:
        a = json.loads(model_config or "{}").get("_usage_anchor") or {}
        return int(a["prompt_tokens"]) + int(a.get("completion_tokens") or 0)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def msg_busy(role, tool_calls):
    """user msg / tool result / assistant asking for tools => still working."""
    return role in ("user", "tool") or bool(tool_calls)


def electron_main(pid, names, cmd=None):
    """True if `pid` is the main (non --type=) process of an Electron app whose exe is in `names`."""
    try:
        exe = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
        if cmd is None:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read()
        return exe in names and b"--type=" not in cmd
    except OSError:
        return False


def electron_app(names):
    """Pid of the running Electron app whose exe is in `names`, else None."""
    return next((pid for pid, st in procs().items() if electron_main(pid, names, st[4])), None)


def hermes_desktop_rows():
    """Open conversations of the Hermes desktop app (only while the app runs).

    Sessions live in the active profile's state.db with created_source 'desktop' (legacy: source
    'desktop'/'gui'); the app closes them (ended_at) when its websocket drops, so ended_at IS NULL
    means open. Sessions untouched since before the app started are ignored (stale)."""
    app = electron_app(("Hermes", "hermes-desktop"))
    if not app:
        return []
    started = proc_start_epoch(app) or time.time()
    rows = []
    # The app can switch profile at any time (active-profile.json goes stale): scan them all.
    for db in [f"{HERMES_DIR}/state.db"] + glob.glob(f"{HERMES_DIR}/profiles/*/state.db"):
        m = re.search(r"/profiles/([^/]+)/state\.db$", db)
        prof = m.group(1) if m else "default"
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
            for sid, title, model, cwd, act, mc, t0 in con.execute(
                    "SELECT id, title, model, cwd, COALESCE(last_activity_at, started_at), model_config, "
                    "started_at FROM sessions "
                    "WHERE ended_at IS NULL AND archived = 0 AND hidden = 0 "
                    "AND (created_source = 'desktop' OR source IN ('desktop', 'gui'))").fetchall():
                m = con.execute("SELECT timestamp, role, tool_calls FROM messages WHERE session_id = ? "
                                "ORDER BY timestamp DESC LIMIT 1", (sid,)).fetchone()
                since = m[0] if m else act
                if since >= started - 5:
                    rows.append(Row("hermes-desktop", f"{prof} · {title or 'new chat'}", cwd or "",
                                    app, bool(m) and msg_busy(m[1], m[2]), since, model=model or "",
                                    ctx=hermes_ctx(mc), started=t0, turns=hermes_turns(con, sid),
                                    error=hermes_error(db, sid)))
            con.close()
        except sqlite3.Error:
            pass
    return rows


def hermes_assign(db, procs):
    """{pid: session id}: the session each CLI process (pid, start epoch, cwd) of one profile db works on.
    Several CLIs share the db and often the folder, and Hermes records no pid per session, so:
    1. a process takes the newest unclaimed session of its own folder that has activity since it started;
    2. a session without a folder (what /new inside a running CLI creates) goes to the process whose
       current session went quiet just before it began (runtime/active_sessions.json cannot help: its ids
       are not the ones in the db)."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        sessions = con.execute(
            "SELECT s.id, s.started_at, s.cwd, MAX(m.timestamp) FROM sessions s JOIN messages m ON m.session_id = s.id "
            "WHERE s.source NOT IN ('oneshot', 'desktop', 'gui') AND COALESCE(s.created_source, '') != 'desktop' "
            "GROUP BY s.id HAVING MAX(m.timestamp) >= ?", (min(p[1] for p in procs) - 5,)).fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    chosen, claimed = {}, set()  # pid -> (id, started_at, cwd, last message epoch)
    for pid, started, cwd in sorted(procs, key=lambda p: -p[1]):  # newest process first
        own = [x for x in sessions if x[2] == cwd and x[3] >= started - 5 and x[0] not in claimed]
        if own:
            chosen[pid] = max(own, key=lambda x: x[3])
            claimed.add(chosen[pid][0])
    for n in sorted((x for x in sessions if not x[2] and x[0] not in claimed), key=lambda x: x[1]):
        cands = [pid for pid, started, _ in procs
                 if n[3] >= started - 5 and (pid not in chosen or chosen[pid][3] <= n[1] + 5)]
        if cands:
            pid = max(cands, key=lambda c: chosen[c][3] if c in chosen else 0)
            chosen[pid] = n
            claimed.add(n[0])
    return {pid: x[0] for pid, x in chosen.items()}


def hermes_rows():
    procs = {}  # db -> [(pid, start epoch, cwd)]
    for pid in hermes_pids():
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = ""
        procs.setdefault(hermes_db(pid), []).append((pid, proc_start_epoch(pid) or time.time(), cwd))
    rows = []
    for db, plist in procs.items():
        mine = hermes_assign(db, plist) if db else {}
        profile = "default"
        m = re.search(r"/profiles/([^/]+)/state\.db$", db or "")
        if m:
            profile = m.group(1)
        for pid, started, cwd in plist:
            since, busy, title, model, ctx, t0, turns, err = started, False, "", "", None, started, None, None
            gate = ""
            if mine.get(pid):
                try:
                    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
                    r = con.execute(
                        "SELECT m.timestamp, m.role, m.tool_calls, s.title, s.model, s.model_config, s.started_at, s.id "
                        "FROM messages m JOIN sessions s ON s.id = m.session_id WHERE s.id = ? "
                        "ORDER BY m.timestamp DESC LIMIT 1", (mine[pid],)).fetchone()
                    if r:
                        turns, err = hermes_turns(con, r[7]), hermes_error(db, r[7])
                    con.close()
                    if r:
                        since, title, model, ctx, t0 = r[0], r[3] or "", r[4] or "", hermes_ctx(r[5]), r[6]
                        # user msg / tool result / assistant asking for tools => still working
                        busy = msg_busy(r[1], r[2])
                        gate = hermes_gate(pid, r[0], r[1], r[2])
                        busy = busy and not gate
                except sqlite3.Error:
                    pass
            rows.append(Row("hermes", f"{profile}" + (f" · {title}" if title else ""),
                            cwd, pid, busy, since, model=model, ctx=ctx, started=t0, turns=turns, error=err,
                            waiting=gate))
    return rows


# ---------------------------------------------------------------- OpenCode
OPENCODE_SUBCMDS = {b"upgrade", b"update", b"uninstall", b"acp", b"api", b"debug", b"auth", b"mcp",
                    b"plugin", b"models", b"stats", b"mini", b"run", b"session", b"service",
                    b"reload", b"pair", b"serve"}


def opencode_tuis():
    """[(pid, session id or None)] of running `opencode` TUIs: tty, no subcommand (the
    `serve --service` background server and `run` are not TUIs). `-s ID` pins the session."""
    tuis = []
    for pid, (_, tty, _, comm, cmd) in procs().items():
        args = cmd.split(b"\0")[1:]
        if comm == "opencode" and (args[:1] or [b""])[0] not in OPENCODE_SUBCMDS and tty != 0 and same_ns(pid):
            sid = next((a.decode() for p, a in itertools.pairwise(args) if p in (b"-s", b"--session")), None)
            tuis.append((pid, sid))
    return tuis


OPENCODE_STALL = 10  # s without a write while a tool runs: the tool most likely waits for an approval or an answer


def opencode_stalled(con, sid, msg):
    """True if the assistant message `msg` of session `sid` ends on a running tool and neither the session nor
    its child sessions wrote anything for OPENCODE_STALL s. OpenCode keeps pending approvals in its server's
    memory only, so this is a guess: a long silent command looks the same."""
    content = msg.get("content") or []
    if not content or (content[-1].get("state") or {}).get("status") != "running":
        return False
    if content[-1].get("name") in ("subagent", "task"):  # a subagent's work is not written until it finishes
        return False
    last = con.execute(
        "WITH RECURSIVE tree(id) AS (SELECT ? UNION SELECT s.id FROM session_v2 s JOIN tree t ON s.parent_id = t.id) "
        "SELECT MAX(time_updated) FROM session_message WHERE session_id IN (SELECT id FROM tree)", (sid,)).fetchone()[0]
    return bool(last) and time.time() - last / 1000 >= OPENCODE_STALL


def opencode_rows():
    """One row per OpenCode TUI. TUIs are clients of a shared background server, so there is no
    per-process session file/fd: the session is the top-level one in the process cwd (session_v2)
    active since the process started (prefer one created since; each session goes to one TUI).
    State comes from the last session_message: type 'idle' = idle since then, else busy.
    No session activity yet = idle since the TUI started."""
    tuis = opencode_tuis()
    if not tuis:
        return []
    try:
        con = sqlite3.connect(f"file:{OPENCODE_DIR}/opencode.db?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return []
    claimed, rows = set(), []
    for pid, pinned in sorted(tuis, key=lambda t: -(proc_start_epoch(t[0]) or 0)):  # newest TUI first
        started = proc_start_epoch(pid) or time.time()
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        sess = None
        try:
            def query(where, *params):
                return con.execute(
                    "SELECT s.id, s.title, s.model, s.time_created, m.type, m.time_created FROM session_v2 s "
                    "JOIN session_message m ON m.session_id = s.id AND m.seq = (SELECT MAX(seq) FROM "
                    "session_message WHERE session_id = s.id AND type IN ('user', 'assistant', 'compaction', 'idle')) "
                    f"WHERE {where} ORDER BY m.time_created DESC", params).fetchall()
            t0 = (started - 5) * 1000
            cands = [c for c in query("s.directory = ? AND s.parent_id IS NULL AND s.time_archived IS NULL "
                                      "AND m.time_created >= ?", cwd, t0) if c[0] not in claimed]
            sess = next((c for c in cands if c[3] >= t0), cands[0] if cands else None)
            if sess:
                claimed.add(sess[0])
            elif pinned:  # `-s ID` resumed, no activity since: show it, but don't claim it
                sess = next(iter(query("s.id = ?", pinned)), None)
        except sqlite3.Error:
            pass
        since, busy, title, model, ctx, t0, turns, err = started, False, "new session", "", None, started, None, None
        gate = ""
        if sess:
            since, busy, title = sess[5] / 1000, sess[4] != "idle", sess[1] or "new session"
            t0 = sess[3] / 1000
            try:
                turns = con.execute("SELECT COUNT(*) FROM session_message WHERE session_id = ? AND type = 'user'",
                                    (sess[0],)).fetchone()[0] or None
                model = json.loads(sess[2] or "{}").get("id", "")
                last = con.execute("SELECT data, time_created FROM session_message WHERE session_id = ? AND "
                                   "type = 'assistant' ORDER BY seq DESC LIMIT 1", (sess[0],)).fetchone()
                msg = json.loads(last[0]) if last else {}
                if busy and opencode_stalled(con, sess[0], msg):
                    gate, busy = "approval?", False
                e = msg.get("error")
                if isinstance(e, dict) and e.get("type") != "aborted":  # aborted = the user hit escape
                    err = (last[1] / 1000, f"{e.get('type', 'error')}: {str(e.get('message', ''))[:90]}")
                # the message being written has no token count yet: size the context from the last finished one
                done = con.execute("SELECT data FROM session_message WHERE session_id = ? AND type = 'assistant' "
                                   "AND json_extract(data, '$.tokens') IS NOT NULL ORDER BY seq DESC LIMIT 1",
                                   (sess[0],)).fetchone()
                tok = (json.loads(done[0]) if done else {}).get("tokens") or {}
                cache = tok.get("cache") or {}
                ctx = sum(int(tok.get(k) or 0) for k in ("input", "output", "reasoning")) + \
                    int(cache.get("read") or 0) + int(cache.get("write") or 0) or None
            except (ValueError, TypeError, AttributeError, sqlite3.Error):
                pass
        rows.append(Row("opencode", title, cwd, pid, busy, since, model=model, ctx=ctx, started=t0, turns=turns,
                        error=err, waiting=gate))
    con.close()
    return rows


# ---------------------------------------------------------------- Codex


def codex_threads(where, args=()):
    """(id, rollout_path, cwd, title, model, updated_epoch, created_epoch) of threads in state_*.sqlite matching
    the SQL `where` (no archived ones, no guardian/subagent threads)."""
    dbs = sorted(glob.glob(f"{CODEX}/state_*.sqlite"))
    try:
        con = sqlite3.connect(f"file:{dbs[-1]}?mode=ro", uri=True, timeout=1)
        rows = con.execute("SELECT id, rollout_path, cwd, COALESCE(NULLIF(name, ''), title), model, "
                           "updated_at_ms / 1000.0, created_at_ms / 1000.0 FROM threads WHERE archived = 0 "
                           f"AND source NOT LIKE '{{%' AND {where}", args).fetchall()
        con.close()
        return rows
    except (IndexError, sqlite3.Error):
        return []


def codex_rollout(path):
    """(last event epoch, busy, model, context tokens, context window) from the tail of a rollout jsonl; None if unreadable or a
    Claude session the app merely imported (its only turn id is 'external-import-*')."""
    try:
        with open(path, "rb") as f:
            start = max(0, os.path.getsize(path) - 256_000)
            f.seek(start)
            lines = f.read().decode(errors="replace").splitlines()[bool(start):]  # first line may be cut
    except OSError:
        return None
    since = busy = model = usage = None
    for line in reversed(lines):
        try:
            d = json.loads(line)
            p, kind = d["payload"], d["type"]
            ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        since = since or ts
        if kind == "event_msg" and usage is None and p.get("type") == "token_count" and p.get("info"):
            usage = p["info"]
        if kind == "turn_context" and model is None:
            model = p.get("model") or ""
        # a turn without task_complete / turn_aborted yet is still running
        if kind == "event_msg" and busy is None and p.get("type") in ("task_started", "task_complete", "turn_aborted"):
            if str(p.get("turn_id", "")).startswith("external-import"):
                return None
            busy = p["type"] == "task_started"
        if busy is not None and model is not None and usage is not None:
            break
    usage = usage or {}
    ctx = (usage.get("last_token_usage") or {}).get("total_tokens")
    return (since, bool(busy), model or "", ctx, usage.get("model_context_window")) if since else None


def codex_row(kind, pid, thread, cwd=""):
    tid, path, tcwd, title, model, _, created = thread
    ro = codex_rollout(path)
    if not ro:
        return None
    title = (title.strip().splitlines() or [""])[0]
    return Row(kind, (title[:70] + ("…" if len(title) > 70 else "")) or "new chat", tcwd or cwd, pid,
               ro[1], ro[0], model=ro[2] or model or "", ctx=ro[3], window=ro[4], started=created,
               turns=count_lines(path, b'"type":"task_started"'),
               url=f"codex://threads/{tid}" if kind == "codex-desktop" else None)


def codex_desktop_rows():
    """Threads of the Codex app (ChatGPT desktop) with activity since the app started.

    Its `codex app-server` (no tty) owns them. At every start the app also re-imports Claude
    sessions as threads: codex_rollout drops those until the user really continues them."""
    app = electron_app(("ChatGPT",))
    if not app:
        return []
    started = proc_start_epoch(app) or time.time()
    rows = [codex_row("codex-desktop", app, t)
            for t in codex_threads("updated_at_ms / 1000.0 >= ?", (started - 5,))]
    return [r for r in rows if r and r.since >= started - 5]


def codex_pids():
    """Interactive Codex TUIs: a `codex` binary with a tty (app-server / exec-server have none)."""
    pids = []
    for pid, (_, tty, _, _, cmd) in procs().items():
        args = cmd.split(b"\0")
        if os.path.basename(args[0]) == b"codex" and not {b"app-server", b"exec"} & set(args) and tty != 0 and same_ns(pid):
            pids.append(pid)
    return pids


def codex_rows():
    """One row per interactive Codex CLI. Its thread is the one whose thread-writer-locks/<id>.lock
    it holds open (in-process mode), else the cli thread in its cwd touched since it started (a TUI
    attached to the shared app-server daemon holds no lock)."""
    rows = []
    for pid in codex_pids():
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
            fds = [os.readlink(f"/proc/{pid}/fd/{fd}") for fd in os.listdir(f"/proc/{pid}/fd")]
        except OSError:
            continue
        ids = [m.group(1) for m in (re.search(r"/thread-writer-locks/([^/]+)\.lock$", t) for t in fds) if m]
        started = proc_start_epoch(pid) or time.time()
        found = codex_threads(f"id IN ({','.join('?' * len(ids))})", ids) if ids else []
        found = found or codex_threads("source = 'cli' AND cwd = ? AND updated_at_ms / 1000.0 >= ?",
                                       (cwd, started - 5))
        cand = [r for r in (codex_row("codex", pid, t, cwd) for t in found) if r]
        if cand:
            rows.append(max(cand, key=lambda r: r.since))
    return rows


def all_rows():
    with collecting():
        rows = (claude_rows() + hermes_rows() + hermes_desktop_rows() + opencode_rows()
                + codex_rows() + codex_desktop_rows())
    rows.sort(key=lambda r: (not r.busy, not r.waiting, r.age))  # busy, waiting, then freshest
    return rows


# ---------------------------------------------------------------- colours
def level(row, warn, limit):
    if row.busy:
        return "busy"
    if row.waiting:
        return "wait"
    if row.age < warn:
        return "ok"
    if row.age < limit:
        return "warn"
    return "late"


def fmt_age(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}h{sec % 3600 // 60:02d}m"
    return f"{sec // 60}m{sec % 60:02d}s"


def fmt_tok(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{round(n / 1000)}k" if n >= 1000 else str(n)


SPARK_SECS, SPARK_CHARS = 1800, 16  # timeline: last 30 min; as text 16 characters (~1m53s each)
SPARK_BARS, SPARK_PITCH, SPARK_H = 48, 2, 14  # in the window: 48 thin bars (~38 s each), 2 px apart, 14 px high
HISTORY = {}  # row key -> [(epoch, ctx tokens or None, busy)], in memory only (starts when the board starts)
_GIT = {}  # cwd -> (epoch, "main*"): git is asked once a minute per folder


def record_history(rows, now=None):
    """Remember each row's context size and busy state; forget sessions that are gone."""
    now = now or time.time()
    keys = {tuple(row_key(r)) for r in rows}
    for k in [k for k in HISTORY if k not in keys]:
        del HISTORY[k]
    for r in rows:
        h = HISTORY.setdefault(tuple(row_key(r)), [])
        h.append((now, r.ctx, r.busy))
        while h[0][0] < now - SPARK_SECS:
            h.pop(0)


def spark_values(row, slots, now=None):
    """Context produced (tokens added) per time slot over the last 30 min, oldest first, as 0..1 heights.
    Slots sit on the clock, not on the row, so every row shares the same time axis. Heights are relative to
    the row's own peak, on a square-root scale so small bursts stay visible next to a spike; 0 = nothing added
    (or no history yet)."""
    now = now or time.time()
    step = SPARK_SECS / slots
    cur = int(now // step)
    added = [0] * slots
    prev = None
    for t, c, _ in HISTORY.get(tuple(row_key(row)), []):
        if not c:
            continue
        i = int(t // step) - (cur - slots + 1)
        if prev is not None and c > prev and 0 <= i < slots:
            added[i] += c - prev
        prev = c
    peak = max(added)
    return [(d / peak) ** 0.5 if d else 0 for d in added]


def sparkline(row, now=None):
    """'▁▁▃▁█▂▁▁': the timeline as text (for --list), SPARK_CHARS characters, ▁ = nothing added."""
    return "".join("▁" if not v else "▁▂▃▄▅▆▇█"[1 + min(6, int(v * 7))]
                   for v in spark_values(row, SPARK_CHARS, now))


def busy_share(row):
    """'62% busy': share of the history samples (one per refresh) where the session was busy."""
    h = HISTORY.get(tuple(row_key(row)), [])
    return f"{round(100 * sum(b for *_, b in h) / len(h))}% busy" if len(h) >= 6 else ""


def git_branch(cwd, now=None):
    """'main' or 'main*' (uncommitted changes to tracked files); '' outside a repo. Cached for a minute."""
    now = now or time.time()
    if cwd in _GIT and now - _GIT[cwd][0] < 60:
        return _GIT[cwd][1]
    out = ""
    try:
        r = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "-b", "--untracked-files=no"],
                           capture_output=True, text=True, timeout=3, check=False)
        lines = r.stdout.splitlines()
        if r.returncode == 0 and lines:
            out = lines[0][3:].split("...")[0].removeprefix("No commits yet on ") + ("*" if len(lines) > 1 else "")
    except (OSError, subprocess.TimeoutExpired):
        pass
    _GIT[cwd] = (now, out)
    return out


RED_CTX, RED_UP, RED_TURNS = 200_000, 3600, 30  # context tokens / session seconds / turns from which the colour is full red


def fmt_up(row):
    """'up 2h10m': session age ('' if unknown)."""
    return "up " + fmt_age(time.time() - row.started) if row.started else ""


def fmt_ctx(row):
    """'156k/475k (33%) ctx' ('' if unknown)."""
    if not row.ctx:
        return ""
    return (fmt_tok(row.ctx) + (f"/{fmt_tok(row.window)} ({round(100 * row.ctx / row.window)}%)"
            if row.window else "") + " ctx")


def fmt_meta(row):
    """'1.3 GB · 7 turns · 156k/475k (33%) ctx': memory (a desktop app: the whole app), turns, context size."""
    return " · ".join(p for p in (fmt_mem(row.pid), f"{row.turns} turns" if row.turns else "", fmt_ctx(row)) if p)


def fmt_where(row):
    """'~/dir · main*': folder and git branch."""
    return " · ".join(p for p in (row.cwd.replace(HOME, "~"), git_branch(row.cwd) if row.cwd else "") if p)


LIMITS = f"{HOME}/.cache/overseer/limits.json"  # written by ~/.claude/statusline.sh (see README)
WINDOWS = (("5h", "five_hour", 5 * 3600), ("week", "seven_day", 7 * 86400))
STALE_AFTER = 600  # s: older used-% values are marked "as of Xm ago"
ERROR_FRESH = 1800  # s: an error younger than this puts a warning sign on the row


def fmt_limits(now=None):
    """'5h 42% · week 61% · 78% of the week gone (as of 23m ago)', or '' without data.
    Used % come from the status line's file (can be old); the elapsed share is computed from the clock."""
    now = now or time.time()
    try:
        with open(LIMITS) as f:
            d = json.load(f)
        limits, updated = d["rate_limits"], float(d["updated"])
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    parts, week_start = [], None
    for label, key, length in WINDOWS:
        w = limits.get(key) or {}
        if "used_percentage" not in w or "resets_at" not in w:
            continue
        over = w["resets_at"] <= now  # window ended: Claude Code stops reporting it, usage restarts from 0
        parts.append(f"{label} {0 if over else round(w['used_percentage'])}%")
        if key == "seven_day":
            week_start = w["resets_at"] - length
            elapsed = max(0, min(100, round(100 * (now - week_start) / length)))
            parts.append(f"{elapsed}% of the week gone")
    if not parts:
        return ""
    text = " · ".join(parts)
    return text + (f" (as of {fmt_age(now - updated)} ago)" if now - updated > STALE_AFTER else "")


def rss_mb(pid):
    """Resident memory of a process in MB, None if unreadable."""
    try:
        with open(f"/proc/{pid}/status") as f:
            m = re.search(r"VmRSS:\s+(\d+) kB", f.read())
        return round(int(m.group(1)) / 1024) if m else None
    except OSError:
        return None


def fmt_mem(pid):
    """'412 MB' or '1.3 GB' ('' if unreadable)."""
    mb = rss_mb(pid)
    return "" if mb is None else f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb} MB"


def recent_error(row, now=None):
    return bool(row.error) and (now or time.time()) - row.error[0] < ERROR_FRESH


def fmt_detail(row, now=None):
    """'waits for: terminal: rm -rf x · 3m05s ago: deepseek failed → fell back to openrouter'."""
    parts = [f"waits for: {row.waiting}" if row.waiting else ""]
    if row.error:
        parts.append(f"{fmt_age((now or time.time()) - row.error[0])} ago: {row.error[1]}")
    return " · ".join(p for p in parts if p)


# ---------------------------------------------------------------- Konsole focus
PACKAGES = {"xdotool": "xdotool", "xprop": "x11-utils", "gdbus": "libglib2.0-bin", "xdg-open": "xdg-utils"}


def missing(*tools):
    """First of `tools` not on PATH, or None."""
    return next((t for t in tools if shutil.which(t) is None), None)


def needs(tool):
    return f"needs {tool} (sudo apt install {PACKAGES[tool]})"


def wayland():
    return bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE") == "wayland"


def gdbus(service, path, method, *args):
    r = subprocess.run(["gdbus", "call", "--session", "--dest", service,
                        "--object-path", path, "--method", method, *args],
                       capture_output=True, text=True, timeout=5, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def nodes(service, path, pattern=r"node (\d+)"):
    r = subprocess.run(["gdbus", "introspect", "--session", "--dest", service,
                        "--object-path", path], capture_output=True, text=True, timeout=5, check=False)
    return re.findall(pattern, r.stdout)


def ancestors(pid):
    seen = []
    while pid > 1 and pid not in seen:
        seen.append(pid)
        st = proc_stat(pid)
        if not st:
            break
        pid = st[0]
    return seen


def environ(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            return dict(x.split(b"=", 1) for x in f.read().split(b"\0") if b"=" in x)
    except OSError:
        return {}


def session_pid(service, sid):
    """Pid of the shell running in Konsole session `sid`."""
    m = re.search(r"\((\d+),\)", gdbus(service, f"/Sessions/{sid}",
                                        "org.kde.konsole.Session.processId") or "")
    return int(m.group(1)) if m else None


def session_list(service, wid):
    return re.findall(r"'(\d+)'", gdbus(service, f"/Windows/{wid}",
                                        "org.kde.konsole.Window.sessionList") or "")


def konsole_tab(pid):
    """(service, session id) of the Konsole tab whose shell is an ancestor of `pid`."""
    chain = set(ancestors(pid))
    # Fast path: env inherited from the tab shell. Session ids survive tab moves.
    env = environ(pid)
    if b"KONSOLE_DBUS_SESSION" in env and b"KONSOLE_DBUS_SERVICE" in env:
        service = env[b"KONSOLE_DBUS_SERVICE"].decode()
        sid = env[b"KONSOLE_DBUS_SESSION"].decode().rsplit("/", 1)[-1]
        if "/" in env[b"KONSOLE_DBUS_SESSION"].decode() and session_pid(service, sid) in chain:
            return service, sid
    names = gdbus("org.freedesktop.DBus", "/org/freedesktop/DBus",
                  "org.freedesktop.DBus.ListNames") or ""
    for service in sorted(set(re.findall(r"org\.kde\.konsole-\d+", names))):
        for sid in nodes(service, "/Sessions"):
            if session_pid(service, sid) in chain:
                return service, sid
    return None


def konsole_x_windows(service):
    """Ids of the WM-managed X windows owned by the Konsole process behind `service`."""
    if missing("xdotool", "xprop"):  # focus_konsole reports the missing tool
        return set()
    try:
        m = re.search(r"(\d+),\)", gdbus("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                            "org.freedesktop.DBus.GetConnectionUnixProcessID", service) or "")
        if not m:
            return set()
        out = subprocess.run(["xdotool", "search", "--pid", m.group(1)],
                             capture_output=True, text=True, timeout=5, check=False).stdout.split()
        root = subprocess.run(["xprop", "-root", "_NET_CLIENT_LIST"],
                              capture_output=True, text=True, timeout=5, check=False).stdout
        managed = {str(int(h, 16)) for h in re.findall(r"0x[0-9a-f]+", root)}
        return {x for x in out if x in managed}
    except (OSError, subprocess.SubprocessError):
        return set()


def konsole_xids(service):
    """{Konsole window id: X window id}. D-Bus has no direct link between the two."""
    live = set()
    for mw in nodes(service, "/konsole", r"node (MainWindow_\d+)"):
        m = re.search(r"(\d+),\)", gdbus(service, f"/konsole/{mw}", "org.kde.KMainWindow.winId") or "")
        if m:
            live.add(m.group(1))
    live |= konsole_x_windows(service)  # Konsole exports a D-Bus object for only some of its windows
    found = {}
    wids = nodes(service, "/Windows")
    sessions = {wid: [environ(session_pid(service, sid) or 0) for sid in session_list(service, wid)]
                for wid in wids}
    # A shell's env names the window it was born in and that window's X id, wherever the tab is now.
    for envs in sessions.values():
        for env in envs:
            xid, birth = env.get(b"WINDOWID", b"").decode(), env.get(b"KONSOLE_DBUS_WINDOW", b"").decode()
            if xid in live and birth.startswith("/Windows/") and birth[9:] in wids:
                found.setdefault(birth[9:], xid)
    # No birth window in the env: WINDOWID is trusted only if all tabs of the window agree on it.
    for wid, envs in sessions.items():
        xids = {env.get(b"WINDOWID", b"").decode() for env in envs}
        if wid not in found and len(xids) == 1 and xids <= live and not xids & set(found.values()):
            found[wid] = xids.pop()
    rest_w = [w for w in wids if w not in found]
    rest_x = live - set(found.values())
    if len(rest_w) == 1 and len(rest_x) == 1:  # a window holding only moved tabs
        found[rest_w[0]] = rest_x.pop()
    return found


def focus_konsole(pid):
    """Select the Konsole tab owning `pid` and raise its window. Returns a status string."""
    try:
        if missing("gdbus"):
            return needs("gdbus")
        tab = konsole_tab(pid)
        if not tab:
            return "not in Konsole"
        service, sid = tab
        wid = next((w for w in nodes(service, "/Windows") if sid in session_list(service, w)), None)
        if not wid:
            return "tab not found"
        gdbus(service, f"/Windows/{wid}", "org.kde.konsole.Window.setCurrentSession", sid)
        display = environ(pid).get(b"DISPLAY", b"").decode()
        if display and display != os.environ.get("DISPLAY"):
            return f"tab selected, but Konsole is on another display ({display})"
        if wayland():
            return "tab selected (Wayland: cannot raise the window)"
        if m := missing("xdotool", "xprop"):
            return needs(m)
        xid = konsole_xids(service).get(wid)
        if not xid:
            return "tab selected (window not found)"
        subprocess.run(["xdotool", "windowactivate", xid], timeout=5, check=False)
        return "ok"
    except (OSError, subprocess.SubprocessError) as e:
        return f"error: {e}"


def tmux(socket, *args):
    r = subprocess.run(["tmux", "-S", socket, *args], capture_output=True, text=True, timeout=5, check=False)
    return r.stdout.splitlines() if r.returncode == 0 else []


def focus_tmux(pid):
    """If `pid` runs in a tmux pane: select that pane and return the attached client's pid.
    Returns (client_pid or None, status)."""
    tm = environ(pid).get(b"TMUX")  # "socket,server_pid,session"
    if not tm:
        return pid, None
    socket = tm.decode().split(",")[0]
    chain = set(ancestors(pid))
    for line in tmux(socket, "list-panes", "-a", "-F", "#{pane_pid}\t#{pane_id}\t#{session_name}"):
        pane_pid, pane, session = line.split("\t", 2)
        if int(pane_pid) in chain:
            break
    else:
        return None, "tmux pane not found"
    clients = [c.split("\t", 2) for c in
               tmux(socket, "list-clients", "-F", "#{client_activity}\t#{client_pid}\t#{client_session}")]
    clients = sorted((c for c in clients if c[2] == session), reverse=True)  # most recent first
    if not clients:
        return None, f"tmux session '{session}' not attached"
    tmux(socket, "select-window", "-t", pane)
    tmux(socket, "select-pane", "-t", pane)
    return int(clients[0][1]), None


def herdr_client_pid():
    """Pid of a running herdr client (the terminal UI), not its background server."""
    out = subprocess.run(["pgrep", "-x", "herdr", "-a"], capture_output=True, text=True, timeout=5, check=False).stdout
    for line in out.splitlines():
        pid, _, cmd = line.partition(" ")
        if "server" not in cmd.split():
            return int(pid)
    return None


def focus_herdr(pid):
    """If `pid` runs in a herdr pane: select that pane/tab/workspace and return the herdr client's pid.
    Returns (client_pid or None, status)."""
    env = environ(pid)
    pane = env.get(b"HERDR_PANE_ID")
    if not pane:
        return pid, None
    herdr = shutil.which("herdr")
    if not herdr:  # fall back to the pane's own binary, only if it is a file we own
        cand = env.get(b"HERDR_BIN_PATH", b"").decode()
        if cand and os.path.isabs(cand) and os.path.isfile(cand) and os.stat(cand).st_uid == os.getuid():
            herdr = cand
    if not herdr:
        return None, "herdr not found"
    r = subprocess.run([herdr, "agent", "focus", pane.decode()], capture_output=True, text=True, timeout=5, check=False)
    if r.returncode:
        return None, f"herdr focus failed: {(r.stderr or r.stdout).strip()[:80]}"
    client = herdr_client_pid()
    return (client, None) if client else (None, "herdr not attached")


def focus_window(pid):
    """Raise the first X window owned by `pid` or one of its ancestors (desktop apps)."""
    if wayland():
        return "app window not raised (Wayland: cannot raise the window)"
    if m := missing("xprop", "xdotool"):
        return needs(m)
    try:
        # Only windows managed by the WM (incl. minimized): Electron also owns hidden helper windows.
        out = subprocess.run(["xprop", "-root", "_NET_CLIENT_LIST"],
                             capture_output=True, text=True, timeout=5, check=False).stdout
        managed = {str(int(h, 16)) for h in re.findall(r"0x[0-9a-f]+", out)}
        for p in ancestors(pid):
            r = subprocess.run(["xdotool", "search", "--pid", str(p)],
                               capture_output=True, text=True, timeout=5, check=False)
            for xid in r.stdout.split():
                if xid in managed:
                    subprocess.run(["xdotool", "windowactivate", xid], timeout=5, check=False)
                    return "ok (app window)"
        return "no window found"
    except (OSError, subprocess.SubprocessError) as e:
        return f"error: {e}"


def open_url(url):
    """Hand a deep link to the desktop app (it raises its window and opens the target). Returns a status string."""
    if m := missing("xdg-open"):
        return needs(m)
    try:
        subprocess.run(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        return "ok (opened in app)"
    except (OSError, subprocess.SubprocessError) as e:
        return f"error: {e}"


def focus_session(pid):
    """Focus the terminal showing `pid` (tmux pane, then Konsole tab), else its app window.
    Returns a status string."""
    try:
        pid, status = focus_herdr(pid)
        if pid and not status:
            pid, status = focus_tmux(pid)
    except (OSError, subprocess.SubprocessError) as e:
        return f"error: {e}"
    status = status or focus_konsole(pid)
    return focus_window(pid) if status == "not in Konsole" else status


def focus_row(r):
    """Bring the session of row `r` to the front. Returns a status string."""
    if r.url:
        return open_url(r.url)
    if r.detached:
        return f"background session, no terminal (claude attach {r.name})"
    return focus_session(r.focus_pid or r.pid)


# ---------------------------------------------------------------- GUI
def run_gui(args):
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
    css = Gtk.CssProvider()
    css.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    theme_css = Gtk.CssProvider()  # the chosen theme, on top of the base CSS
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), theme_css,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    write_art()
    cur_theme = [args.theme or load_theme()]
    theme_css.load_from_data(THEMES[cur_theme[0]].encode())
    win = Gtk.Window(title="Overseer")
    win.set_default_size(540, 120)
    win.set_keep_above(True)
    win.connect("destroy", Gtk.main_quit)
    # Own compact title bar instead of the window manager's one.
    header = Gtk.HeaderBar(show_close_button=True)  # one line: [theme] name | account limits | count + clock [x]
    header.set_decoration_layout(":close")
    win.set_titlebar(header)
    header.set_title("Overseer")  # window title (the bar itself shows its own label)
    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    limits_bar = Gtk.Label(label="")
    limits_bar.get_style_context().add_class("subtitle")
    limits_bar.set_ellipsize(3)
    header.set_custom_title(limits_bar)  # empty when the status line does not share the limits
    status = Gtk.Label(label="")  # "4 · 23:31:01", or what the last click did
    status.get_style_context().add_class("subtitle")
    status.set_ellipsize(3)
    status.set_max_width_chars(22)
    header.pack_end(status)
    win.add(listbox)
    expanded = set()  # keys of rows whose ⚠ / ? detail line is open
    pids = {}
    theme = Gtk.IconTheme.get_default()
    # One logo per product; CLI rows get a ">_" badge, desktop-app rows don't.
    icons = {"claude": "claude-desktop", "hermes": "hermes",  # hermes = Nous girl
             "opencode": "ai.opencode.desktop", "codex": "chatgpt"}

    def spark_area(vals):
        """The timeline as thin bars, drawn in the colour of the theme's .chart class."""
        area = css_class(Gtk.DrawingArea(), "chart")
        area.set_size_request(SPARK_BARS * SPARK_PITCH, SPARK_H)
        area.set_valign(Gtk.Align.CENTER)

        def draw(widget, cr):
            c = widget.get_style_context().get_color(Gtk.StateFlags.NORMAL)
            for i, v in enumerate(vals):
                cr.set_source_rgba(c.red, c.green, c.blue, c.alpha if v else c.alpha * 0.35)
                bar = max(1, round(v * SPARK_H))  # nothing added: a 1 px baseline
                cr.rectangle(i * SPARK_PITCH, SPARK_H - bar, SPARK_PITCH - 1, bar)
                cr.fill()
            return False

        area.connect("draw", draw)
        return area

    def css_class(widget, *names):
        for n in names:
            widget.get_style_context().add_class(n)
        return widget

    def logo(kind):
        product = kind.split("-")[0]
        if theme.lookup_icon(icons[product], 22, 0):  # has_icon() misses /usr/share/pixmaps
            icon = Gtk.Image.new_from_icon_name(icons[product], Gtk.IconSize.DND)
            icon.set_pixel_size(22)
        else:
            icon = Gtk.Label(label=product[0].upper())
        box = Gtk.Overlay()
        box.add(icon)
        box.set_size_request(28, 26)
        if not kind.endswith("-desktop"):
            badge = css_class(Gtk.Label(label=">_"), "badge")
            badge.set_halign(Gtk.Align.END)
            badge.set_valign(Gtk.Align.END)
            box.add_overlay(badge)
        return box

    seen = {}  # session key -> (busy, waiting) at the previous refresh

    def notify(rows):
        """Desktop notification when a session stops working or starts waiting for the user."""
        for r in rows:
            key = (r.kind, r.pid, r.started)  # desktop apps: one pid, many sessions
            was = seen.get(key)
            if was and ((was[0] and not r.busy) or (r.waiting and not was[1])) and not missing("notify-send"):
                msg = f"waits for: {r.waiting}" if r.waiting else "finished"
                subprocess.Popen(["notify-send", "-a", "Overseer", "-i", icons[r.kind.split("-")[0]],
                                  r.name, msg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        seen.clear()
        seen.update({(r.kind, r.pid, r.started): (r.busy, r.waiting) for r in rows})

    def collect():
        """Everything slow (/proc, sqlite, git, files), in the worker thread. Returns what the window shows:
        the rows, with their texts and timeline already computed, so drawing touches no shared state."""
        rows, total = visible_rows()
        record_history(rows)
        for r in rows:
            r.where, r.meta, r.share = fmt_where(r), fmt_meta(r), busy_share(r)
            r.spark = spark_values(r, SPARK_BARS) if r.ctx else []
        if args.notify:
            notify(rows)
        return {"rows": rows, "total": total, "limits": fmt_limits() if args.limits else ""}

    frame = {"rows": [], "total": 0, "limits": ""}  # the last collection; read and replaced on the GTK thread only
    collecting_now = threading.Lock()  # held while a collection runs: a tick that finds it taken is skipped

    def worker():
        try:
            try:
                result = collect()
            except Exception:  # noqa: BLE001  # a failed collection must not stop the refresh loop
                traceback.print_exc()
                return
            GLib.idle_add(apply, result)
        finally:
            collecting_now.release()

    def apply(result):
        frame.update(result)
        refresh()
        return False

    def tick():
        if collecting_now.acquire(blocking=False):
            threading.Thread(target=worker, daemon=True).start()
        return True

    def refresh():
        """Draw the last collection (GTK thread)."""
        for child in listbox.get_children():
            listbox.remove(child)
        pids.clear()
        rows, total = frame["rows"], frame["total"]
        # yacht's title bar is dark teak above white rows: its limits need the colours for dark surfaces
        limits_bar.set_markup(heat_percents(frame["limits"], "dark" if cur_theme[0] == "yacht" else cur_theme[0]))
        for r in rows:
            card = css_class(Gtk.Box(spacing=10), "card", level(r, args.warn, args.limit))
            age = css_class(Gtk.Label(label="busy" if r.busy else fmt_age(r.age)), "age")
            age.set_width_chars(6)
            age.set_valign(Gtk.Align.CENTER)
            top = Gtk.Box(spacing=6)
            name = css_class(Gtk.Label(label=r.name, xalign=0), "name")
            name.set_ellipsize(3)
            top.pack_start(name, True, True, 0)
            if r.model:
                top.pack_end(css_class(Gtk.Label(label=r.model), "model"), False, False, 0)
            th = cur_theme[0]
            up_sec = time.time() - r.started if r.started else 0
            share = r.share
            for markup in (heat("up " + fmt_age(up_sec), up_sec / RED_UP, th) if r.started else "",  # right to left:
                           heat_percents(share, th) if share else ""):  # model, up, busy
                if markup:
                    label = css_class(Gtk.Label(), "cwd")
                    label.set_markup(markup)
                    top.pack_end(label, False, False, 0)
            if recent_error(r) or r.waiting:  # sign (left of busy): ⚠ error, ? waits for you; click to show / hide
                warn = css_class(Gtk.Button(label="⚠" if recent_error(r) else "?"), "plus",
                                 "err" if recent_error(r) else "ask")
                warn.set_relief(Gtk.ReliefStyle.NONE)
                wkey = tuple(row_key(r))
                warn.connect("clicked", lambda _b, k=wkey: toggle(k))
                top.pack_end(warn, False, False, 0)
            bottom = Gtk.Box(spacing=6)
            where = css_class(Gtk.Label(label=r.where, xalign=0), "cwd")
            where.set_ellipsize(1)
            bottom.pack_start(where, True, True, 0)
            chart = spark_area(r.spark)  # rightmost: all charts line up
            bottom.pack_end(chart, False, False, 0)
            if r.meta:
                meta = css_class(Gtk.Label(), "cwd")
                shades = {fmt_ctx(r): (r.ctx or 0) / RED_CTX, f"{r.turns} turns": (r.turns or 0) / RED_TURNS}
                meta.set_markup(" · ".join(
                    heat(p, shades[p], th) if p in shades and p else escape(p) for p in r.meta.split(" · ")))
                bottom.pack_end(meta, False, False, 0)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            text.pack_start(top, False, False, 0)
            text.pack_start(bottom, False, False, 0)
            key = tuple(row_key(r))
            if (recent_error(r) or r.waiting) and key in expanded:
                detail = css_class(Gtk.Label(label=fmt_detail(r), xalign=0), "cwd")
                detail.set_ellipsize(3)
                text.pack_start(detail, False, False, 0)
            card.pack_start(age, False, False, 0)
            card.pack_start(logo(r.kind), False, False, 0)
            card.pack_start(text, True, True, 0)
            row = Gtk.ListBoxRow()
            ebox = Gtk.EventBox()  # gets the right-click before the list box's own click gesture
            ebox.add(card)
            ebox.connect("button-press-event", on_button, r)
            row.add(ebox)
            row.set_tooltip_text(f"{r.kind} · pid {r.pid}" + (f" · waits for: {r.waiting}" if r.waiting else "")
                                 + " — click to focus")
            listbox.add(row)
            pids[row] = r
        hidden = f" · {total - len(rows)} hidden" if total > len(rows) else ""
        status.set_text(f"{len(rows)}{hidden} · {time.strftime('%H:%M:%S')}")
        listbox.show_all()
        return True

    def toggle(key):
        expanded.symmetric_difference_update({key})
        GLib.idle_add(lambda: (refresh(), False)[1])  # not inside the button's own signal: refresh destroys the button

    busy_lock = threading.Lock()

    def focus(r):
        if busy_lock.acquire(blocking=False):  # ignore double-click while one focus runs
            try:
                try:
                    result = focus_row(r)
                except Exception as e:  # noqa: BLE001
                    result = f"error: {e}"
            finally:
                busy_lock.release()
            GLib.idle_add(status.set_text, result)

    def on_click(_lb, row):
        r = pids.get(row)
        if r:
            threading.Thread(target=focus, args=(r,), daemon=True).start()

    def hide(r):
        save_hidden(load_hidden() + [row_key(r)])
        frame["rows"] = [x for x in frame["rows"] if x is not r]
        refresh()

    def show_all_hidden():
        save_hidden([])
        tick()  # the hidden rows are not in the last collection: collect again (skipped if one is running)

    def pick_theme(name):
        cur_theme[0] = name
        save_theme(name)
        theme_css.load_from_data(THEMES[name].encode())
        refresh()  # the heat colours depend on the theme

    def glitch():
        """CRT theme: a short colour-split flicker at random moments (CSS class 'glitch' on the window)."""
        if cur_theme[0] == "crt":
            ctx = win.get_style_context()
            ctx.add_class("glitch")
            GLib.timeout_add(random.choice((70, 110, 160)), lambda: (ctx.remove_class("glitch"), False)[1])
            if random.random() < 0.3:  # sometimes a quick second one
                GLib.timeout_add(260, glitch_once)
        GLib.timeout_add(random.randint(3000, 11000), glitch)
        return False

    def glitch_once():
        ctx = win.get_style_context()
        ctx.add_class("glitch")
        GLib.timeout_add(60, lambda: (ctx.remove_class("glitch"), False)[1])
        return False

    def theme_menu():
        sub = Gtk.Menu()
        for name in THEMES:
            item = Gtk.MenuItem(label=("✓ " if name == cur_theme[0] else "    ") + name)
            item.connect("activate", lambda _i, n=name: pick_theme(n))
            sub.append(item)
        return sub

    def on_theme_button(btn):
        menu = theme_menu()
        menu.show_all()
        menu.popup_at_widget(btn, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)
        menu_ref[0] = menu

    theme_btn = Gtk.Button(label="◐")
    theme_btn.set_tooltip_text("Theme")
    theme_btn.connect("clicked", on_theme_button)
    header.pack_start(theme_btn)
    title = Gtk.Label(label="Overseer")
    title.get_style_context().add_class("title")
    header.pack_start(title)

    def on_button(_w, event, r=None):
        """Right-click menu: hide this session (until its process ends) / show hidden ones."""
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        if r:
            item = Gtk.MenuItem(label="Hide this session")
            item.connect("activate", lambda _i: hide(r))
            menu.append(item)
        if load_hidden():
            item = Gtk.MenuItem(label="Show hidden sessions")
            item.connect("activate", lambda _i: show_all_hidden())
            menu.append(item)
        parent = Gtk.MenuItem(label="Theme")
        parent.set_submenu(theme_menu())
        menu.append(parent)
        if menu.get_children():
            menu.show_all()
            menu.popup_at_pointer(event)
        menu_ref[0] = menu  # keep a reference while it is open
        return True

    menu_ref = [None]
    listbox.connect("row-activated", on_click)
    listbox.connect("button-press-event", on_button)  # empty area: "show hidden" only
    tick()
    GLib.timeout_add_seconds(args.interval, tick)
    GLib.timeout_add(5000, glitch)
    win.show_all()
    Gtk.main()


HIDDEN = f"{HOME}/.cache/overseer/hidden.json"


def row_key(r):
    """Identity of a session across refreshes (desktop apps: one pid, many sessions)."""
    return [r.kind, r.pid, round(r.started or 0, 3)]


def load_hidden():
    try:
        with open(HIDDEN) as f:
            keys = json.load(f)
    except (OSError, ValueError):
        return []
    return [k for k in keys if proc_stat(k[1])]  # process gone: its sessions show again


def save_hidden(keys):
    os.makedirs(os.path.dirname(HIDDEN), exist_ok=True)
    with open(HIDDEN + ".tmp", "w") as f:
        json.dump(keys, f)
    os.replace(HIDDEN + ".tmp", HIDDEN)


def visible_rows():
    hidden = load_hidden()
    rows = all_rows()
    return [r for r in rows if row_key(r) not in hidden], len(rows)


def next_row(rows, limit):
    """The session to handle next: one waiting for you (oldest first), else the idle one closest
    to losing its cache, else the most recent idle one. None if all are busy."""
    idle = [r for r in rows if not r.busy]
    waiting = [r for r in idle if r.waiting]
    alive = [r for r in idle if r.age < limit]
    if waiting or alive:
        return max(waiting or alive, key=lambda r: r.age)
    return min(idle, key=lambda r: r.age, default=None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--warn", type=int, default=240, help="orange after N s idle (default 240)")
    ap.add_argument("--limit", type=int, default=300, help="red after N s idle (default 300)")
    ap.add_argument("--interval", type=int, default=10, help="refresh period in s (default 10)")
    ap.add_argument("--no-notify", dest="notify", action="store_false",
                    help="no desktop notification when a session finishes or waits for you")
    ap.add_argument("--theme", choices=list(THEMES), help="colour theme (default: the last one picked in the right-click menu)")
    ap.add_argument("--no-limits", dest="limits", action="store_false", help="hide the account limits bar")
    ap.add_argument("--list", action="store_true", help="print once and exit")
    ap.add_argument("--focus", type=int, metavar="PID", help="focus terminal tab / app window of PID and exit")
    ap.add_argument("--next", action="store_true",
                    help="focus the session to handle next (waiting for you, else cache closest to expiry)")
    args = ap.parse_args()
    if args.focus:
        print(focus_session(args.focus))
    elif args.next:
        r = next_row(visible_rows()[0], args.limit)
        print(f"{r.name} ({'busy' if r.busy else fmt_age(r.age)}): {focus_row(r)}" if r
              else "no idle session")
    elif args.list:
        for r in all_rows():
            print(f"{level(r, args.warn, args.limit):5} {'busy' if r.busy else fmt_age(r.age):>7} "
                  f"{r.kind:14} pid={r.pid:<8} {r.model:24} {busy_share(r):9} {fmt_up(r):10} {fmt_meta(r):30} {sparkline(r) if r.ctx else '':{SPARK_CHARS}} {r.name}  "
                  f"{r.cwd.replace(HOME, '~')}")
    else:
        run_gui(args)


if __name__ == "__main__":
    main()
