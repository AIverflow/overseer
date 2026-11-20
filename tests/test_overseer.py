"""Unit tests for overseer.py: pure helpers, with fixtures built in temp folders (no /proc, no real home)."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import overseer


def row(**kw):
    args = {"kind": "claude", "name": "x", "cwd": "/x", "pid": 1, "busy": False, "since": time.time()}
    args.update(kw)
    return overseer.Row(**args)


class Formatting(unittest.TestCase):
    def test_fmt_age(self):
        self.assertEqual(overseer.fmt_age(0), "0m00s")
        self.assertEqual(overseer.fmt_age(65), "1m05s")
        self.assertEqual(overseer.fmt_age(3600), "1h00m")
        self.assertEqual(overseer.fmt_age(7500), "2h05m")

    def test_fmt_tok(self):
        self.assertEqual(overseer.fmt_tok(999), "999")
        self.assertEqual(overseer.fmt_tok(1500), "2k")
        self.assertEqual(overseer.fmt_tok(156_000), "156k")
        self.assertEqual(overseer.fmt_tok(1_250_000), "1.2M")


class Levels(unittest.TestCase):
    def test_level(self):
        now = time.time()
        self.assertEqual(overseer.level(row(busy=True), 240, 300), "busy")
        self.assertEqual(overseer.level(row(waiting="ok?"), 240, 300), "wait")
        self.assertEqual(overseer.level(row(since=now - 10), 240, 300), "ok")
        self.assertEqual(overseer.level(row(since=now - 250), 240, 300), "warn")
        self.assertEqual(overseer.level(row(since=now - 400), 240, 300), "late")

    def test_next_row(self):
        now = time.time()
        busy = row(name="busy", busy=True)
        fresh = row(name="fresh", since=now - 10)
        old = row(name="old", since=now - 200)
        gone = row(name="gone", since=now - 900)
        waiting = row(name="waiting", since=now - 50, waiting="y/n")
        self.assertIsNone(overseer.next_row([busy], 300))
        self.assertIs(overseer.next_row([busy, fresh, old], 300), old)  # closest to losing its cache
        self.assertIs(overseer.next_row([fresh, old, waiting], 300), waiting)
        self.assertIs(overseer.next_row([gone, row(name="g2", since=now - 1200)], 300), gone)  # all late: most recent


class Limits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = overseer.LIMITS
        self.addCleanup(setattr, overseer, "LIMITS", self.old)
        overseer.LIMITS = os.path.join(self.tmp.name, "limits.json")
        self.now = 1_000_000_000.0

    def write(self, updated, five, week):
        with open(overseer.LIMITS, "w") as f:
            json.dump({"updated": updated, "rate_limits": {
                "five_hour": {"used_percentage": 42.4, "resets_at": five},
                "seven_day": {"used_percentage": 61.0, "resets_at": week}}}, f)

    def test_fresh(self):
        self.write(self.now - 30, self.now + 3600, self.now + 7 * 86400 / 2)
        self.assertEqual(overseer.fmt_limits(self.now), "5h 42% · week 61% · 50% of the week gone")

    def test_stale(self):
        self.write(self.now - 1380, self.now + 3600, self.now + 7 * 86400 / 2)
        self.assertTrue(overseer.fmt_limits(self.now).endswith("(as of 23m00s ago)"))

    def test_window_over(self):
        self.write(self.now - 30, self.now - 1, self.now + 7 * 86400 / 2)
        self.assertTrue(overseer.fmt_limits(self.now).startswith("5h 0% · week 61%"))

    def test_missing_file(self):
        self.assertEqual(overseer.fmt_limits(self.now), "")


class Transcripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    @staticmethod
    def event(ts, kind, payload):
        return json.dumps({"timestamp": ts.isoformat().replace("+00:00", "Z"), "type": kind, "payload": payload})

    def rollout(self, *events):
        path = os.path.join(self.tmp.name, "rollout.jsonl")
        with open(path, "w") as f:
            f.write("\n".join(events) + "\n")
        return path

    def test_codex_busy_and_idle(self):
        t = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        ctx = self.event(t, "turn_context", {"model": "gpt-x"})
        usage = self.event(t, "event_msg", {"type": "token_count", "info": {
            "last_token_usage": {"total_tokens": 1234}, "model_context_window": 9999}})
        start = self.event(t, "event_msg", {"type": "task_started", "turn_id": "t1"})
        done = self.event(t, "event_msg", {"type": "task_complete", "turn_id": "t1"})
        busy = overseer.codex_rollout(self.rollout(ctx, start, usage))
        self.assertEqual(busy, (t.timestamp(), True, "gpt-x", 1234, 9999))
        idle = overseer.codex_rollout(self.rollout(ctx, start, usage, done))
        self.assertFalse(idle[1])

    def test_codex_external_import_and_missing(self):
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        path = self.rollout(self.event(t, "event_msg", {"type": "task_complete", "turn_id": "external-import-1"}))
        self.assertIsNone(overseer.codex_rollout(path))
        self.assertIsNone(overseer.codex_rollout(os.path.join(self.tmp.name, "nope.jsonl")))

    def test_claude_error(self):
        def line(ts, text, err=True):
            d = {"timestamp": ts, "message": {"content": [{"type": "text", "text": text}]}}
            if err:
                d["isApiErrorMessage"] = True
            return json.dumps(d, separators=(",", ":"))
        tail = "\n".join([line("2026-01-01T00:00:00Z", "old\nmore"), line("2026-01-01T00:01:00Z", "overloaded\nretry"),
                          line("2026-01-01T00:02:00Z", "fine", err=False), "garbage"])
        ts, text = overseer.claude_error(tail)
        self.assertEqual(text, "overloaded")
        self.assertEqual(ts, datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(overseer.claude_error(line("2026-01-01T00:00:00Z", "fine", err=False)))


class HermesAssign(unittest.TestCase):
    def test_two_processes_one_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "state.db")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE sessions (id TEXT, started_at REAL, cwd TEXT, source TEXT, created_source TEXT)")
            con.execute("CREATE TABLE messages (session_id TEXT, timestamp REAL)")
            for sid, started, last in (("s-old", 1000, 1500), ("s-new", 2000, 2500)):
                con.execute("INSERT INTO sessions VALUES (?, ?, '/proj', 'cli', NULL)", (sid, started))
                con.execute("INSERT INTO messages VALUES (?, ?)", (sid, last))
            con.commit()
            con.close()
            procs = [(11, 990, "/proj"), (22, 1990, "/proj")]  # (pid, start epoch, cwd)
            # the newest process claims the most recently active session, the older one gets the other
            self.assertEqual(overseer.hermes_assign(db, procs), {22: "s-new", 11: "s-old"})
            self.assertEqual(overseer.hermes_assign(os.path.join(tmp, "missing.db"), procs), {})


    def test_new_inside_a_running_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "state.db")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE sessions (id TEXT, started_at REAL, cwd TEXT, source TEXT, created_source TEXT)")
            con.execute("CREATE TABLE messages (session_id TEXT, timestamp REAL)")
            # /new: the old session goes quiet, then a session without a folder begins in the same process
            for sid, started, last, cwd in (("s-old", 1000, 1500, "/proj"), ("s-new", 1600, 1700, None)):
                con.execute("INSERT INTO sessions VALUES (?, ?, ?, 'cli', NULL)", (sid, started, cwd))
                con.execute("INSERT INTO messages VALUES (?, ?)", (sid, last))
            con.commit()
            con.close()
            self.assertEqual(overseer.hermes_assign(db, [(11, 990, "/proj")]), {11: "s-new"})


class ClaudeJobs(unittest.TestCase):
    """claude_rows() on a fake CLAUDE_CONFIG_DIR; the pid files point to real `sleep` processes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir, self.procs, self.old_dir = self.tmp.name, [], overseer.CLAUDE_DIR
        os.makedirs(f"{self.dir}/sessions")
        os.makedirs(f"{self.dir}/projects/p")
        overseer.CLAUDE_DIR = self.dir
        overseer._TRANSCRIPT_START.clear()  # keyed by session id, which the tests reuse
        overseer._COUNTS.clear()

    def tearDown(self):
        overseer.CLAUDE_DIR = self.old_dir
        for p in self.procs:
            p.kill()
            p.wait()
        self.tmp.cleanup()

    def session(self, kind, sid, cwd="/proj", started=1000, transcript=False, **extra):
        p = subprocess.Popen(["sleep", "60"])
        self.procs.append(p)
        d = {"pid": p.pid, "procStart": str(overseer.proc_stat(p.pid)[2]), "sessionId": sid, "cwd": cwd,
             "startedAt": started * 1000, "kind": kind, "status": "idle", "name": f"{kind}-{sid[:4]}"}
        d.update(extra)
        with open(f"{self.dir}/sessions/{p.pid}.json", "w") as f:
            json.dump(d, f)
        if transcript:
            with open(f"{self.dir}/projects/p/{sid}.jsonl", "w") as f:
                ts = datetime.fromtimestamp(started, timezone.utc).isoformat()
                f.write(f'{{"type":"user","timestamp":"{ts}","message":{{"content":"hi"}}}}\n'
                        '{"type":"assistant","message":{"model":"claude-sonnet-5-5","usage":{"input_tokens":42}}}\n')
        return p.pid

    def rows(self):
        return {r.pid: r for r in overseer.claude_rows()}

    def test_stale_console_and_idle_job(self):
        # the console parked job 94cea380, which is gone; it now views the job 3d1a01bc started after it
        console = self.session("interactive", "b67ac192-x", parkedJobId="94cea380")
        job = self.session("bg", "f14b865f-x", started=1020, transcript=True, jobId="3d1a01bc")
        rows = self.rows()
        self.assertEqual(list(rows), [job])
        self.assertEqual((rows[job].focus_pid, rows[job].model, rows[job].turns), (console, "sonnet-5-5", 1))

    def test_job_id_differs_from_session_id(self):
        console = self.session("interactive", "863299db-x", parkedJobId="6b47ceb6")
        job = self.session("bg", "82ef89bd-x", started=1020, transcript=True, jobId="6b47ceb6")
        self.assertEqual({p: r.focus_pid for p, r in self.rows().items()}, {job: console})

    def test_two_stale_consoles_one_folder_link_nothing(self):
        a = self.session("interactive", "aaaa-x", parkedJobId="dead0001")
        b = self.session("interactive", "bbbb-x", parkedJobId="dead0002")
        self.session("bg", "cccc-x", started=1020, transcript=True, jobId="cccc0001")
        self.assertEqual(sorted(self.rows()), sorted([a, b]))  # the idle unlinked job is dropped, both consoles stay

    def test_console_without_parked_job_is_not_linked(self):
        plain = self.session("interactive", "aaaa-x")
        stale = self.session("interactive", "bbbb-x", parkedJobId="dead0001")
        job = self.session("bg", "cccc-x", started=1020, transcript=True, jobId="cccc0001")
        rows = self.rows()
        self.assertEqual(sorted(rows), sorted([plain, job]))
        self.assertEqual(rows[job].focus_pid, stale)

    def test_job_older_than_the_stale_console_is_not_linked(self):
        stale = self.session("interactive", "bbbb-x", started=2000, parkedJobId="dead0001")
        self.session("bg", "cccc-x", started=1000, transcript=True, jobId="cccc0001")
        self.assertEqual(list(self.rows()), [stale])

    def daemon(self, counts, attaches):
        """roster.json with {job id: message count}, and attach-journal entries [(console pid, epoch, count)]."""
        os.makedirs(f"{self.dir}/daemon/attach-journal")
        workers = {j: {"firedInteractiveMarks": [{"kind": "content_paint", "msgsInJsonl": n}]} for j, n in counts.items()}
        with open(f"{self.dir}/daemon/roster.json", "w") as f:
            json.dump({"workers": workers}, f)
        for i, (pid, at, n) in enumerate(attaches):
            with open(f"{self.dir}/daemon/attach-journal/g{i}.json", "w") as f:
                json.dump({"pid": pid, "procStart": str(overseer.proc_stat(pid)[2]), "startedAtEpochMs": at * 1000,
                           "msgsInJsonl": n}, f)

    def test_job_moved_to_another_console(self):
        # parked in console A, then attached in console B from the session list: neither pid file changes,
        # A stays on the list (no session), B still names its own earlier job
        a = self.session("interactive", "aaaa-x", parkedJobId="cccc0001")
        b = self.session("interactive", "bbbb-x", started=1500, parkedJobId="dddd0001")
        job = self.session("bg", "cccc-x", started=1020, transcript=True, jobId="cccc0001")
        other = self.session("bg", "dddd-x", started=1600, transcript=True, jobId="dddd0001")
        self.daemon({"cccc0001": 90, "dddd0001": 27}, [(a, 1030, 90), (b, 1700, 90)])
        rows = self.rows()
        self.assertEqual(list(rows), [job])  # B's earlier job is idle and shown nowhere: dropped
        self.assertEqual(rows[job].focus_pid, b)
        self.assertNotIn(other, rows)

    def test_attach_with_an_ambiguous_count_guesses_nothing(self):
        a = self.session("interactive", "aaaa-x", parkedJobId="cccc0001")
        b = self.session("interactive", "bbbb-x", started=1500)
        job = self.session("bg", "cccc-x", started=1020, transcript=True, jobId="cccc0001")
        self.session("bg", "dddd-x", started=1600, transcript=True, jobId="dddd0001")
        self.daemon({"cccc0001": 90, "dddd0001": 90}, [(b, 1700, 90)])
        rows = self.rows()
        self.assertEqual(rows[job].focus_pid, a)  # B's attach can't be told apart: the pid files rule
        self.assertIn(b, rows)


class SparkAndGit(unittest.TestCase):
    def setUp(self):
        self.old_history = dict(overseer.HISTORY)
        self.addCleanup(lambda: (overseer.HISTORY.clear(), overseer.HISTORY.update(self.old_history)))
        overseer.HISTORY.clear()
        self.old_git = dict(overseer._GIT)
        self.addCleanup(lambda: (overseer._GIT.clear(), overseer._GIT.update(self.old_git)))
        overseer._GIT.clear()

    def test_spark_values(self):
        r = row(pid=7, started=5.0)
        step = overseer.SPARK_SECS / 4  # 4 slots of 450 s
        now = step * 1000 + 10
        overseer.HISTORY[tuple(overseer.row_key(r))] = [
            (step * 997 + 5, 100, False), (step * 998 + 5, 200, True), (step * 1000 + 5, 600, True)]
        self.assertEqual(overseer.spark_values(r, 4, now), [0, 0.5, 0, 1.0])
        self.assertEqual(overseer.spark_values(row(pid=8), 4, now), [0, 0, 0, 0])  # no history

    def test_git_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(overseer.git_branch(os.path.join(tmp, "nowhere")), "")
            repo = os.path.join(tmp, "repo")
            os.mkdir(repo)
            git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
            subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
            Path(repo, "a.txt").write_text("1\n")
            subprocess.run([*git, "add", "a.txt"], check=True)
            subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
            now = time.time()
            self.assertEqual(overseer.git_branch(repo, now), "main")
            Path(repo, "a.txt").write_text("2\n")
            self.assertEqual(overseer.git_branch(repo, now + 1), "main")  # cached for a minute
            self.assertEqual(overseer.git_branch(repo, now + 120), "main*")


class ProcTable(unittest.TestCase):
    def test_table_has_this_process(self):
        ppid, tty, start, _comm, cmd = overseer.proc_table()[os.getpid()]
        self.assertEqual(ppid, os.getppid())
        self.assertEqual((tty, start), overseer.proc_stat(os.getpid())[1:])
        self.assertIn(b"python", cmd.split(b"\0")[0])

    def test_tool_running_uses_the_table(self):
        table = {1: (0, 0, 0, "a", b"hermes"), 2: (1, 0, 0, "b", b"python multiprocessing.spawn"),
                 3: (2, 0, 0, "c", b"sleep 5"), 4: (9, 0, 0, "d", b"other")}
        self.assertTrue(overseer.tool_running(1, table))  # a grandchild that is a real command
        del table[3]
        self.assertFalse(overseer.tool_running(1, table))  # only the idle worker is left

    def test_table_is_kept_only_during_a_collection(self):
        with overseer.collecting():
            first = overseer.procs()
            self.assertIs(overseer.procs(), first)
        self.assertIsNone(overseer._PROCS)
        self.assertIsNot(overseer.procs(), first)


if __name__ == "__main__":
    unittest.main()
