#!/usr/bin/env python3
"""Runtime smoke harness for agy-exporter.

Drives the exporter in-process against a snapshot of ~/.gemini and a fake
OTLP/HTTP collector, then prints one JSON line per poll cycle describing what
the collector received. Every ingested point and log record is fingerprinted,
because ClickHouse does not deduplicate: a fingerprint received twice is a
duplicate row on the dashboards.

One scenario per process, so the exporter's import-time globals (env config,
SUMMARY_CACHE) start clean each time.

usage: smoke.py <scenario> <exporter.py> <gemini_dir> <work_dir>
scenarios: baseline | unwritable-state | partial-success | poisoned-conversation | body-limit |
           compose-mounts | long-running-step | crashed-session | misconfigured-endpoint |
           subbatch-partial-failure | poisoned-record | close-on-large | save-fails-after-post |
           damaged-state | skill-per-turn | unindexed-generation
"""
import collections
import contextlib
import errno
import glob
import http.server
import importlib.util
import io
import json
import os
import socket
import socketserver
import sqlite3
import sys
import threading
import time
from pathlib import Path

SCENARIO, EXPORTER, GEMINI, WORK = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4])
os.environ.setdefault("ABANDON_IDLE_SEC", "3600")  # a fixed window, so results don't depend on defaults


class Collector:
    def __init__(self):
        self.limit = 20 * 1024 * 1024  # the collector's default max_request_body_size
        self.close_on_large = False     # over the limit: drop the connection instead of answering 413
        self.mode = "ok"                # "ok" or "partial" (200 + partialSuccess, one item rejected)
        self.poison_session = None      # "auto" or a session.id: any request carrying it gets HTTP 400
        self.poison_metric = None       # the first data point of this metric becomes the poison record
        self.poison_fp = None
        self.poison_sid = None
        self.poison_ingested = 0
        self.error_by_cycle = {}        # cycle number -> HTTP status returned for every request in it
        self.fail_once_after = None     # the Nth request that would be accepted gets 503 instead, once
        self.fail_first_logs = False    # the first /v1/logs request gets 503, once
        self.cycle = 0
        self.accepted = 0
        self.posts = []
        self.seen = {}                  # fingerprint -> times ingested, across the whole scenario


C = Collector()


def attrs_of(item):
    return {a["key"]: next(iter(a["value"].values()), None) for a in item.get("attributes", [])}


def digest(body):
    """Fingerprints plus the per-record attributes the checks read."""
    fps, records = [], []
    for rm in body.get("resourceMetrics", []):
        for sm in rm.get("scopeMetrics", []):
            for m in sm.get("metrics", []):
                for kind in ("sum", "gauge", "histogram"):
                    for dp in (m.get(kind) or {}).get("dataPoints", []):
                        a = attrs_of(dp)
                        val = dp.get("asDouble", dp.get("asInt"))
                        fps.append(("m", m["name"], dp.get("timeUnixNano"), json.dumps(a, sort_keys=True), val))
                        records.append(("m", m["name"], a))
    for rl in body.get("resourceLogs", []):
        for sl in rl.get("scopeLogs", []):
            for r in sl.get("logRecords", []):
                a = attrs_of(r)
                ev = a.get("event.name")
                fps.append(("l", ev, r.get("timeUnixNano"), json.dumps(a, sort_keys=True), json.dumps(r.get("body"))))
                records.append(("l", ev, a))
    return fps, records


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        rec = {"path": self.path, "bytes": length}
        C.posts.append(rec)
        if length > C.limit:
            if C.close_on_large:
                rec["status"] = "closed"
                self.close_connection = True
                with contextlib.suppress(OSError):
                    self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.rfile.read(length)
            rec["status"] = 413
            return self.reply(413, {"message": "request body too large"})
        raw = self.rfile.read(length)
        if C.cycle in C.error_by_cycle:
            rec["status"] = C.error_by_cycle[C.cycle]
            return self.reply(rec["status"], {"message": "misconfigured endpoint"})
        fps, records = digest(json.loads(raw))
        sessions = {a.get("session.id") for _, _, a in records}
        rec["items"] = len(fps)
        rec["sessions"], rec["agents"] = sessions, {a.get("agent.name") for _, _, a in records}
        rec["surfaces"] = {a.get("surface") for _, _, a in records}
        if C.fail_first_logs and self.path.endswith("/logs"):
            C.fail_first_logs = False
            rec["status"] = 503
            return self.reply(503, {"message": "temporarily unavailable"})
        if C.poison_session == "auto":
            C.poison_session = sorted(s for s in sessions if s)[0]
        if C.poison_session and C.poison_session in sessions:
            rec["status"] = 400
            return self.reply(400, {"message": "rejected"})
        if C.poison_metric and C.poison_fp is None:
            for fp in fps:
                if fp[0] == "m" and fp[1] == C.poison_metric:
                    C.poison_fp, C.poison_sid = fp, json.loads(fp[3]).get("session.id")
                    break
        if C.poison_fp is not None and C.poison_fp in fps:
            rec["status"] = 400
            return self.reply(400, {"message": "invalid data point"})
        if C.fail_once_after is not None:
            C.accepted += 1
            if C.accepted == C.fail_once_after:
                C.fail_once_after = None
                rec["status"] = 503
                return self.reply(503, {"message": "temporarily unavailable"})
        dupe_names = {}
        for fp in fps:
            C.seen[fp] = C.seen.get(fp, 0) + 1
            if C.seen[fp] > 1:
                dupe_names[fp[1]] = dupe_names.get(fp[1], 0) + 1
            if fp == C.poison_fp:
                C.poison_ingested += 1
        rec["dupes"], rec["dupe_names"] = sum(dupe_names.values()), dupe_names
        rec["names"] = collections.Counter(name for _, name, _ in records)
        rec["abandoned"] = sum(1 for k, n, a in records if n == "agy.tool_result" and a.get("error_type") == "abandoned")
        rec["unknown_tools"] = sum(1 for k, n, a in records if n == "agy.tool_result" and a.get("tool_name") == "unknown")
        rec["decisions"] = collections.Counter(a.get("decision") for k, n, a in records if n == "agy.tool_decision")
        rec["api_requests"] = [(a["session.id"], int(fp[2]), a.get("skill.name", ""))
                               for fp in fps if fp[:2] == ("l", "agy.api_request") for a in [json.loads(fp[3])]]
        rec["status"] = 200
        if C.mode == "partial":
            key = "rejectedDataPoints" if self.path.endswith("/metrics") else "rejectedLogRecords"
            return self.reply(200, {"partialSuccess": {key: "1", "errorMessage": "1 item rejected"}})
        self.reply(200, {})

    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def load_module():
    """A fresh exporter instance: re-reads env config, empty SUMMARY_CACHE. Simulates a restart."""
    name = f"exporter_{time.monotonic_ns()}"
    spec = importlib.util.spec_from_file_location(name, EXPORTER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    calls = {"n": 0}
    real = mod.process_conversation_db

    def counted(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    mod.process_conversation_db = counted
    mod._calls = calls
    return mod


def union(key):
    out = set()
    for p in C.posts:
        out |= p.get(key, set())
    return out - {None, ""}


def db_mtime_ns(db: Path) -> int:
    wal = Path(f"{db}-wal")
    return max(db.stat().st_mtime_ns, wal.stat().st_mtime_ns if wal.exists() else 0)


def find_db(cid: str) -> Path | None:
    hits = glob.glob(f"{os.environ['GEMINI_DIR']}/*/conversations/{cid}.db")
    return Path(hits[0]) if hits else None


def summarize(state, label, seconds, err, processed, lines):
    convs = state.get("conversations", {})
    names, decisions = collections.Counter(), collections.Counter()
    for p in C.posts:
        names.update(p.get("names", {}))
        decisions.update(p.get("decisions", {}))
    pending = []
    for cid, c in convs.items():
        n = len(set(c.get("pending_metric_steps", [])) | set(c.get("pending_log_steps", [])))
        db = find_db(cid)
        if n and db:
            pending.append({"cid": cid[:8], "steps": n, "idle_sec": round((time.time_ns() - db_mtime_ns(db)) / 1e9)})
    return {
        "cycle": label,
        "seconds": round(seconds, 2),
        "raised": err,
        "convs_processed": processed,
        "posts": [{k: p[k] for k in ("path", "bytes", "status", "items", "dupes", "dupe_names") if p.get(k)} for p in C.posts],
        "statuses": dict(collections.Counter(str(p.get("status")) for p in C.posts)),
        "items_ingested": sum(p.get("items", 0) for p in C.posts if p.get("status") == 200),
        "duplicates_ingested": sum(p.get("dupes", 0) for p in C.posts),
        "max_post_bytes": max((p["bytes"] for p in C.posts), default=0),
        "sessions": len(union("sessions")),
        "surfaces": sorted(union("surfaces")),
        "distinct_agent_names": len(union("agents")),
        "emitted": dict(names.most_common()),
        "abandoned_results": sum(p.get("abandoned", 0) for p in C.posts),
        "unknown_tool_results": sum(p.get("unknown_tools", 0) for p in C.posts),
        "decisions": dict(decisions),
        "poison_ingested": C.poison_ingested,
        "state_file_written": Path(os.environ["STATE_FILE"]).exists(),
        "tracked_convs": len(convs),
        "convs_metric_cursor_advanced": sum(1 for c in convs.values() if int(c.get("last_metric_gen_idx", -1)) >= 0),
        "convs_log_cursor_advanced": sum(1 for c in convs.values() if int(c.get("last_log_gen_idx", -1)) >= 0),
        "convs_failed": sum(1 for c in convs.values() if "failed_mtime_ns" in c),
        "pending_steps_total": sum(p["steps"] for p in pending),
        "pending": pending,
        "exporter_log": {
            "skipped_conversation": sum("Skipping conversation" in l for l in lines),
            "partial_or_failed_export": sum("Partial/failed OTLP export" in l for l in lines),
            "partialSuccess_rejection": sum("partialSuccess rejection" in l for l in lines),
            "first_error": next((l[:160] for l in lines if "Skipping conversation" in l or "Error" in l), None),
        },
    }


def cycle(mod, state, label, quiet=False):
    C.posts = []
    C.cycle += 1
    mod._calls["n"] = 0
    log = io.StringIO()
    err = None
    t0 = time.monotonic()
    with contextlib.redirect_stdout(log):
        try:
            mod.sync_once(state, TMP, persist=True)
        except Exception as exc:  # main() decides what an escaping exception means
            err = f"{type(exc).__name__}: {exc}"
    out = summarize(state, label, time.monotonic() - t0, err, mod._calls["n"], log.getvalue().splitlines())
    if not quiet:
        print("@@" + json.dumps(out))
    return out


def fresh_state():
    for p in STATE_DIR.glob("*"):
        p.unlink()


def build_fixture(name: str) -> tuple[Path, Path, dict[str, int]]:
    """Copy one real CLI conversation into a writable surface of its own.

    Picks a conversation with no step in flight that has a tool step without
    permission requests, a tool step with one, and a planner (type 15) step.
    """
    probe = load_module()
    for src in sorted(Path(GEMINI, "antigravity-cli", "conversations").glob("*.db")):
        c = sqlite3.connect(f"file:{src}?immutable=1", uri=True)
        try:
            rows = c.execute("select idx, step_type, status, metadata, permissions from steps order by idx").fetchall()
        except sqlite3.Error:
            continue
        if any(r[2] in (0, 1, 2, 9) for r in rows):
            continue
        pick = {}
        for idx, stype, status, meta, perms in rows:
            tool = probe.wire_str(probe.wire_msg(probe.parse_wire(meta), 4), 2) if meta else ""
            has_perm = bool(perms) and bool(probe.wire_msg_list(probe.parse_wire(perms), 2))
            if status == 3 and tool and not has_perm:
                pick["tool"] = idx
            elif status == 3 and tool and has_perm:
                pick["tool_with_permission"] = idx
            elif status == 3 and stype == 15:
                pick["planner"] = idx
        if len(pick) == 3:
            dst = WORK / name / "gemini" / "antigravity-cli" / "conversations" / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            source, dest = sqlite3.connect(f"file:{src}?immutable=1", uri=True), sqlite3.connect(dst)
            source.backup(dest)
            dest.close(); source.close()
            return WORK / name / "gemini", dst, pick
    sys.exit("no conversation in the snapshot has a tool step, a tool step with permissions and a planner step")


def strand_three_steps(db: Path, pick: dict[str, int]) -> None:
    """Leave a tool call running, a permission prompt unanswered and a planner step mid-stream, and
    delete every other step, so all step records the collector receives come from these three."""
    c = sqlite3.connect(db)
    keep = (pick["tool"], pick["tool_with_permission"], pick["planner"])
    c.execute("delete from steps where idx not in (?, ?, ?)", keep)
    c.execute("update steps set status = 2 where idx in (?, ?)", (pick["tool"], pick["planner"]))
    c.execute("update steps set status = 9 where idx = ?", (pick["tool_with_permission"],))
    c.commit()
    c.close()


def set_writable(root: Path, writable: bool) -> None:
    """Make a fixture tree read-only like the container's :ro mount, or writable again for cleanup.

    Read-only matters: in a writable directory the exporter's own read creates a -wal, which changes
    the mtime it keys on and hides whether it would revisit an unchanged conversation by itself.
    """
    for dirpath, _, files in os.walk(root):
        for p in [Path(dirpath)] + [Path(dirpath) / f for f in files]:
            mode = p.stat().st_mode
            p.chmod(mode | 0o200 if writable else mode & ~0o222)


def strip_field(blob: bytes, field: int) -> bytes:
    """Remove every occurrence of a top-level field from an encoded protobuf message."""
    def varint(i):
        n = shift = 0
        while True:
            b = blob[i]
            n |= (b & 0x7F) << shift
            i, shift = i + 1, shift + 7
            if b < 0x80:
                return n, i

    out, i = bytearray(), 0
    while i < len(blob):
        start = i
        key, i = varint(i)
        wire_type = key & 7
        if wire_type == 0:
            _, i = varint(i)
        elif wire_type == 1:
            i += 8
        elif wire_type == 2:
            n, i = varint(i)
            i += n
        elif wire_type == 5:
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        if key >> 3 != field:
            out += blob[start:i]
    return bytes(out)


def set_idle(db: Path, seconds: float) -> None:
    t = time.time_ns() - int(seconds * 1e9)
    for p in (db, Path(f"{db}-wal")):
        if p.exists():
            os.utime(p, ns=(t, t))


class Stop(Exception):
    pass


def run_main(mod, polls):
    """Run the exporter's main() as the container does, for `polls` polls. Returns (exit code, POSTs per poll, log lines)."""
    n = {"sleeps": 0, "seen": 0}
    per_poll = []

    class Clock:
        def __getattr__(self, attr):
            return getattr(time, attr)

        def sleep(self, _seconds):
            per_poll.append(len(C.posts) - n["seen"])
            n["seen"] = len(C.posts)
            n["sleeps"] += 1
            if n["sleeps"] >= polls:
                raise Stop()

    mod.time = Clock()
    C.posts = []
    exited = None
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        try:
            mod.main()
        except Stop:
            pass
        except SystemExit as exc:
            exited = exc.code
    return exited, per_poll, log.getvalue().splitlines()


def report(label, result, **extra):
    exited, per_poll, lines = result
    print("@@" + json.dumps({
        "cycle": label,
        "exited": exited,
        "posts_per_poll": per_poll,
        "items_ingested": sum(p.get("items", 0) for p in C.posts if p.get("status") == 200),
        "duplicates_ingested": sum(p.get("dupes", 0) for p in C.posts),
        "state_file_written": Path(os.environ["STATE_FILE"]).exists(),
        "fatal": next((l for l in lines if "FATAL" in l), None),
        **extra,
    }))


WORK.mkdir(parents=True, exist_ok=True)
STATE_DIR = WORK / "state"
STATE_DIR.mkdir(exist_ok=True)
TMP = WORK / "tmp"
TMP.mkdir(exist_ok=True)

srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ.update(
    GEMINI_DIR=GEMINI,
    STATE_FILE=str(STATE_DIR / "state.json"),
    OTEL_EXPORTER_OTLP_ENDPOINT=f"http://127.0.0.1:{srv.server_address[1]}",
)

if SCENARIO == "baseline":
    mod = load_module(); st = mod.load_state()
    cycle(mod, st, "1 cold start")
    cycle(mod, st, "2 next poll, nothing changed")
    mod = load_module(); st = mod.load_state()
    cycle(mod, st, "3 after a restart, nothing changed")

elif SCENARIO == "unwritable-state":
    STATE_DIR.chmod(0o555)  # what a root-owned named volume looks like to uid 1000
    try:
        mod = load_module(); st = mod.load_state()
        cycle(mod, st, "1 cold start, state dir not writable")
        cycle(mod, st, "2 next poll")
        mod = load_module(); st = mod.load_state()
        cycle(mod, st, "3 after a restart")
    finally:
        STATE_DIR.chmod(0o755)

elif SCENARIO == "partial-success":
    C.mode = "partial"
    mod = load_module(); st = mod.load_state()
    for i in (1, 2, 3):
        cycle(mod, st, f"{i} collector rejects 1 item per request")

elif SCENARIO == "poisoned-conversation":
    C.poison_session = "auto"
    mod = load_module(); st = mod.load_state()
    cycle(mod, st, "1 collector rejects requests carrying one conversation")
    cycle(mod, st, "2 next poll")

elif SCENARIO == "body-limit":
    C.limit = 1_000_000  # well under one cold-start batch
    mod = load_module(); st = mod.load_state()
    for i in (1, 2, 3):
        cycle(mod, st, f"{i} collector body limit 1 MB")

elif SCENARIO == "compose-mounts":
    mod = load_module(); st = mod.load_state()
    cycle(mod, st, "1 cold start, only the compose mounts visible")

elif SCENARIO == "long-running-step":
    # A session that is alive (its connection holds the -wal open) with a step running for longer
    # than any fixed grace window. It must stay pending, then be exported with its real result.
    gemini, db, pick = build_fixture("long-running-step")
    os.environ["GEMINI_DIR"] = str(gemini)
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("pragma wal_autocheckpoint=0")
    writer.execute("update steps set status = 2 where idx = ?", (pick["tool"],))
    try:
        mod = load_module(); st = mod.load_state()
        cycle(mod, st, "1 live session, one step still running")
        writer.execute("update steps set status = 3 where idx = ?", (pick["tool"],))
        cycle(mod, st, "2 the step completes")
    finally:
        writer.close()

elif SCENARIO == "crashed-session":
    # Three steps left in flight: a tool call, a permission prompt nobody answered, and a planner step.
    os.environ["ABANDON_IDLE_SEC"] = "600"
    gemini, db, pick = build_fixture("crashed-session")
    os.environ["GEMINI_DIR"] = str(gemini)
    strand_three_steps(db, pick)
    set_writable(gemini, False)
    try:
        mod = load_module(); st = mod.load_state()
        set_idle(db, 60)
        cycle(mod, st, "1 three steps in flight, session idle for 60 s")
        set_idle(db, 3600)
        cycle(mod, st, "2 the same session idle for an hour")
        cycle(mod, st, "3 next poll")
    finally:
        set_writable(gemini, True)

elif SCENARIO == "session-goes-quiet":
    # A crashed session is never written to again, so nothing on disk changes: the exporter has to
    # come back to it by itself once the idle window has passed.
    os.environ["ABANDON_IDLE_SEC"] = "2"
    gemini, db, pick = build_fixture("session-goes-quiet")
    os.environ["GEMINI_DIR"] = str(gemini)
    strand_three_steps(db, pick)
    set_idle(db, 0)
    set_writable(gemini, False)
    try:
        mod = load_module(); st = mod.load_state()
        cycle(mod, st, "1 three steps in flight, session just written to")
        time.sleep(2.5)
        cycle(mod, st, "2 no writes since, idle window passed")
    finally:
        set_writable(gemini, True)

elif SCENARIO == "crash-mid-delivery":
    # The process dies between two requests. Whatever the collector accepted before that must
    # already be on disk, or the restart sends it again.
    class Crash(BaseException):
        pass

    for k in range(3, 9):
        fresh_state()
        C.__init__()
        C.limit = 600_000  # many requests per cycle, so the crash lands mid-delivery
        mod = load_module(); st = mod.load_state()
        real_post, calls = mod.post_otlp, {"n": 0}

        def dying_post(path, payload, real_post=real_post, calls=calls, k=k):
            calls["n"] += 1
            if calls["n"] == k:
                raise Crash()
            return real_post(path, payload)

        mod.post_otlp = dying_post
        C.posts = []
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                mod.sync_once(st, TMP, persist=True)
            except Crash:
                pass
        before = C.posts
        mod = load_module(); st = mod.load_state()  # restart from what was saved
        runs = [cycle(mod, st, f"k={k} after restart {i}", quiet=True) for i in (1, 2, 3)]
        print("@@" + json.dumps({
            "cycle": f"crash before request #{k}",
            "items_ingested": sum(p.get("items", 0) for p in before if p.get("status") == 200)
                              + sum(r["items_ingested"] for r in runs),
            "duplicates_ingested": sum(p.get("dupes", 0) for p in before) + sum(r["duplicates_ingested"] for r in runs),
            "tracked_convs": runs[-1]["tracked_convs"],
            "convs_metric_cursor_advanced": runs[-1]["convs_metric_cursor_advanced"],
            "convs_log_cursor_advanced": runs[-1]["convs_log_cursor_advanced"],
        }))

elif SCENARIO == "misconfigured-endpoint":
    C.error_by_cycle = {1: 404, 2: 401}
    mod = load_module(); st = mod.load_state()
    cycle(mod, st, "1 endpoint answers 404")
    cycle(mod, st, "2 endpoint answers 401")
    cycle(mod, st, "3 endpoint fixed")

elif SCENARIO == "subbatch-partial-failure":
    # Whichever request fails mid-delivery, nothing already accepted may be sent twice.
    for k in range(1, 9):
        fresh_state()
        C.__init__()
        C.limit, C.fail_once_after = 600_000, k
        mod = load_module(); st = mod.load_state()
        runs = [cycle(mod, st, f"k={k} poll {i}", quiet=True) for i in (1, 2, 3, 4)]
        last = runs[-1]
        print("@@" + json.dumps({
            "cycle": f"request #{k} fails once",
            "items_ingested": sum(r["items_ingested"] for r in runs),
            "duplicates_ingested": sum(r["duplicates_ingested"] for r in runs),
            "tracked_convs": last["tracked_convs"],
            "convs_metric_cursor_advanced": last["convs_metric_cursor_advanced"],
            "convs_log_cursor_advanced": last["convs_log_cursor_advanced"],
            "posts": sum(len(r["posts"]) for r in runs),
        }))

elif SCENARIO == "poisoned-record":
    # The collector refuses one data point; the first logs request also fails transiently.
    C.poison_metric, C.fail_first_logs = "agy.commit.count", True
    mod = load_module(); st = mod.load_state()
    cycle(mod, st, "1 one data point refused, logs fail once")
    cycle(mod, st, "2 next poll, nothing changed on disk")
    db = find_db(C.poison_sid) if C.poison_sid else None
    original = db.stat().st_mtime_ns if db else None
    try:
        if db:
            os.utime(db, None)  # the session keeps being written to
        cycle(mod, st, "3 the conversation holding the refused point changes")
    finally:
        if db:
            os.utime(db, ns=(original, original))

elif SCENARIO == "close-on-large":
    C.limit, C.close_on_large = 1_000_000, True
    mod = load_module(); st = mod.load_state()
    for i in (1, 2, 3):
        cycle(mod, st, f"{i} collector drops the connection on bodies over 1 MB")

elif SCENARIO == "save-fails-after-post":
    # The disk fills after a request was accepted. The exporter must neither exit (restarting from
    # the last saved state re-sends what was accepted) nor keep exporting what it cannot record.
    mod = load_module()
    saves = {"n": 0}
    real_save = mod.save_state

    def failing_save(state):
        saves["n"] += 1
        if saves["n"] <= 3:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_save(state)

    mod.save_state = failing_save
    report("1 disk full for the first three saves, 5 polls", run_main(mod, polls=5))
    report("2 restart with a working disk, 2 polls", run_main(load_module(), polls=2))

elif SCENARIO == "damaged-state":
    # A crash or power loss soon after a save can leave state.json empty or cut short. Starting over
    # from empty state would export every conversation again, so the exporter must stop instead.
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def recording_fsync(fd):
        events.append(("fsync", os.fstat(fd).st_ino))
        return real_fsync(fd)

    def recording_replace(src, dst, **kw):
        events.append(("replace", os.stat(src).st_ino))
        return real_replace(src, dst, **kw)

    os.fsync, os.replace = recording_fsync, recording_replace
    first = run_main(load_module(), polls=2)
    os.fsync, os.replace = real_fsync, real_replace

    # Each rename must come after an fsync of the file it moves and before an fsync of the directory.
    state_file = Path(os.environ["STATE_FILE"])
    dir_ino = state_file.parent.stat().st_ino
    renames = [i for i, e in enumerate(events) if e[0] == "replace"]
    bounds = [-1] + renames + [len(events)]
    in_order = bool(renames) and all(
        ("fsync", events[r][1]) in events[bounds[k] + 1:r] and ("fsync", dir_ino) in events[r + 1:bounds[k + 2]]
        for k, r in enumerate(renames)
    )
    report("1 cold start, 2 polls", first, saves=len(renames), saves_fsynced_in_order=in_order)

    damaged = state_file.read_bytes()[: state_file.stat().st_size // 2]
    state_file.write_bytes(damaged)
    report("2 restart with state.json cut in half, 2 polls", run_main(load_module(), polls=2),
           state_file_unchanged=state_file.read_bytes() == damaged)

elif SCENARIO == "skill-per-turn":
    # Reading a skill's SKILL.md shapes the rest of that user turn. Requests in later turns must not
    # be credited to it, or one skill read early in a long conversation takes most of its cost.
    mod = load_module(); st = mod.load_state()
    cycle(mod, st, "1 cold start", quiet=True)
    by_session = collections.defaultdict(list)
    for p in C.posts:
        for sid, ts, skill in p.get("api_requests", []):
            if skill:
                by_session[sid].append((ts, skill))
    credited = later_turn = unmatched = 0
    for sid, reqs in by_session.items():
        c = sqlite3.connect(f"file:{find_db(sid)}?mode=ro&immutable=1", uri=True)
        idx_by_ts, turn_starts, activations = {}, [], []
        for idx, stype, status, meta in c.execute("select idx, step_type, status, metadata from steps order by idx"):
            m = mod.parse_wire(meta)
            ts = mod.proto_ts_ns(mod.wire_msg(m, 1))
            if ts:
                idx_by_ts[ts] = idx
            if stype == 14:
                turn_starts.append(idx)
            tc = mod.wire_msg(m, 4)
            try:
                args = json.loads(mod.wire_str(tc, 3) or "{}")
            except ValueError:
                args = {}
            # Looser than the exporter's success test, so the check can only under-report.
            act = mod.extract_activated_skill(mod.wire_str(tc, 2), args if isinstance(args, dict) else {}, status == 3)
            if act:
                activations.append((idx, act[0]))
        c.close()
        for ts, skill in reqs:
            if ts not in idx_by_ts:
                unmatched += 1
                continue
            credited += 1
            i = idx_by_ts[ts]
            turn = max((u for u in turn_starts if u <= i), default=-1)
            if not any(name == skill and turn < a <= i for a, name in activations):
                later_turn += 1
    print("@@" + json.dumps({"cycle": "1 cold start", "credited": credited, "later_turn": later_turn,
                             "unmatched": unmatched}))

elif SCENARIO == "unindexed-generation":
    # A generation that lists no step indices, whose planner step is still streaming. It must wait for
    # that step like any other generation, or its usage is read before it is final.
    gemini, db, _ = build_fixture("unindexed-generation")
    os.environ["GEMINI_DIR"] = str(gemini)
    probe = load_module()
    c = sqlite3.connect(db)
    for g_idx, data in c.execute("select idx, data from gen_metadata order by idx desc").fetchall():
        f = probe.parse_wire(data)
        steps = probe.parse_packed_varints(probe.wire_bytes(f, 2))
        kvs = [{probe.wire_str(x, 1): probe.wire_str(x, 2) for x in probe.wire_msg_list(gm, 20)}
               for gm in probe.wire_msg_list(f, 1)]
        if steps and any(kv.get("last_step_index") == str(steps[0] - 1) for kv in kvs):
            break
    else:
        sys.exit("no generation in the fixture records last_step_index")
    total = c.execute("select count(*) from gen_metadata").fetchone()[0]
    c.execute("update gen_metadata set data = ? where idx = ?", (strip_field(data, 2), g_idx))
    c.execute("update steps set status = 2 where idx = ?", (steps[0],))
    c.commit()
    mod = load_module(); st = mod.load_state()
    first = cycle(mod, st, "1 planner step streaming", quiet=True)
    c.execute("update steps set status = 3 where idx = ?", (steps[0],))
    c.commit()
    c.close()
    second = cycle(mod, st, "2 planner step done", quiet=True)
    for label, out in (("1 planner step streaming", first), ("2 planner step done", second)):
        print("@@" + json.dumps({"cycle": label, "generations": total,
                                 "api_requests": out["emitted"].get("agy.api_request", 0)}))

else:
    sys.exit(f"unknown scenario {SCENARIO}")
