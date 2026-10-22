#!/usr/bin/env python3
"""Runtime smoke harness for agy-exporter.

Drives exporter.sync_once() in-process against a snapshot of ~/.gemini and a
fake OTLP/HTTP collector, then prints one JSON line per poll cycle describing
what the collector received. Every ingested point and log record is
fingerprinted, because ClickHouse does not deduplicate: a fingerprint received
twice is a duplicate row on the dashboards.

One scenario per process, so the exporter's import-time globals (env config,
SUMMARY_CACHE) start clean each time.

usage: smoke.py <scenario> <exporter.py> <gemini_dir> <work_dir>
scenarios: baseline | unwritable-state | partial-success | poisoned-conversation | body-limit | compose-mounts
"""
import contextlib
import http.server
import importlib.util
import io
import json
import os
import socketserver
import sys
import threading
import time
from pathlib import Path

SCENARIO, EXPORTER, GEMINI, WORK = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4])


class Collector:
    mode = "ok"              # "ok" or "partial" (200 + partialSuccess, one item rejected)
    poison = None            # "auto" or a session.id: any request carrying it gets HTTP 400
    limit = 20 * 1024 * 1024 # the collector's default max_request_body_size
    posts: list = []
    seen: dict = {}          # fingerprint -> times ingested, across the whole scenario


C = Collector()


def attrs_of(item):
    return {a["key"]: next(iter(a["value"].values()), None) for a in item.get("attributes", [])}


def digest(body):
    fps, sessions, agents, surfaces, names = [], set(), set(), set(), {}
    for rm in body.get("resourceMetrics", []):
        for sm in rm.get("scopeMetrics", []):
            for m in sm.get("metrics", []):
                for kind in ("sum", "gauge", "histogram"):
                    for dp in (m.get(kind) or {}).get("dataPoints", []):
                        a = attrs_of(dp)
                        sessions.add(a.get("session.id")); agents.add(a.get("agent.name")); surfaces.add(a.get("surface"))
                        names[m["name"]] = names.get(m["name"], 0) + 1
                        val = dp.get("asDouble", dp.get("asInt"))
                        fps.append(("m", m["name"], dp.get("timeUnixNano"), json.dumps(a, sort_keys=True), val))
    for rl in body.get("resourceLogs", []):
        for sl in rl.get("scopeLogs", []):
            for r in sl.get("logRecords", []):
                a = attrs_of(r)
                sessions.add(a.get("session.id")); agents.add(a.get("agent.name")); surfaces.add(a.get("surface"))
                ev = a.get("event.name")
                names[ev] = names.get(ev, 0) + 1
                fps.append(("l", ev, r.get("timeUnixNano"), json.dumps(a, sort_keys=True), json.dumps(r.get("body"))))
    return fps, sessions, agents, surfaces, names


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        rec = {"path": self.path, "bytes": len(raw)}
        C.posts.append(rec)
        if len(raw) > C.limit:
            rec["status"] = 413
            return self.reply(413, {"message": "request body too large"})
        fps, sessions, agents, surfaces, names = digest(json.loads(raw))
        rec.update(items=len(fps), sessions=sessions, agents=agents, surfaces=surfaces, names=names)
        if C.poison == "auto":
            C.poison = sorted(s for s in sessions if s)[0]
        if C.poison and C.poison in sessions:
            rec["status"] = 400
            return self.reply(400, {"message": "rejected"})
        dupe_names = {}
        for fp in fps:
            C.seen[fp] = C.seen.get(fp, 0) + 1
            if C.seen[fp] > 1:
                dupe_names[fp[1]] = dupe_names.get(fp[1], 0) + 1
        rec["dupes"] = sum(dupe_names.values())
        rec["dupe_names"] = dupe_names
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


def cycle(mod, state, label):
    C.posts = []
    mod._calls["n"] = 0
    log = io.StringIO()
    err = None
    t0 = time.monotonic()
    with contextlib.redirect_stdout(log):
        try:
            mod.sync_once(state, TMP, persist=True)
        except Exception as exc:  # main() would swallow this as "Unexpected loop error"
            err = f"{type(exc).__name__}: {exc}"
    convs = state.get("conversations", {})
    lines = log.getvalue().splitlines()
    names = {}
    for p in C.posts:
        for k, v in p.get("names", {}).items():
            names[k] = names.get(k, 0) + v
    out = {
        "cycle": label,
        "seconds": round(time.monotonic() - t0, 2),
        "raised": err,
        "convs_processed": mod._calls["n"],
        "posts": [{k: p[k] for k in ("path", "bytes", "status", "items", "dupes", "dupe_names") if p.get(k)} for p in C.posts],
        "items_ingested": sum(p.get("items", 0) for p in C.posts if p.get("status") == 200),
        "duplicates_ingested": sum(p.get("dupes", 0) for p in C.posts),
        "max_post_bytes": max((p["bytes"] for p in C.posts), default=0),
        "sessions": len(union("sessions")),
        "surfaces": sorted(union("surfaces")),
        "distinct_agent_names": len(union("agents")),
        "state_file_written": Path(os.environ["STATE_FILE"]).exists(),
        "tracked_convs": len(convs),
        "convs_metric_cursor_advanced": sum(1 for c in convs.values() if int(c.get("last_metric_gen_idx", -1)) >= 0),
        "convs_failed": sum(1 for c in convs.values() if "failed_mtime_ns" in c),
        "pending_steps_total": sum(len(c.get("pending_metric_steps", [])) for c in convs.values()),
        "exporter_log": {
            "skipped_conversation": sum("Skipping conversation" in l for l in lines),
            "partial_or_failed_export": sum("Partial/failed OTLP export" in l for l in lines),
            "partialSuccess_rejection": sum("partialSuccess rejection" in l for l in lines),
            "first_error": next((l[:160] for l in lines if "Skipping conversation" in l or "Error" in l), None),
        },
        "emitted": dict(sorted(names.items(), key=lambda kv: -kv[1])) if label.startswith("1") else None,
    }
    print("@@" + json.dumps(out))


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
    C.poison = "auto"
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
else:
    sys.exit(f"unknown scenario {SCENARIO}")
