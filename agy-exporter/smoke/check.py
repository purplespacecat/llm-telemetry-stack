#!/usr/bin/env python3
"""Acceptance checks for agy-exporter, run against a read-only snapshot of real Antigravity data.

Snapshots ~/.gemini, drives the exporter through smoke.py's scenarios against a fake
OTLP collector, and checks one invariant per line. Exits 1 if any check fails.

usage: python3 agy-exporter/smoke/check.py [--gemini ~/.gemini]
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPORTER = HERE.parent / "exporter.py"
DOCKERFILE = HERE.parent / "Dockerfile"
REPO = HERE.parent.parent
COMPOSE_FILES = [REPO / "docker-compose.yml"]

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def scenario(name: str, gemini: Path, work: Path, tag: str | None = None) -> list[dict]:
    p = subprocess.run(
        [sys.executable, "-I", "-B", str(HERE / "smoke.py"), name, str(EXPORTER), str(gemini), str(work / (tag or name))],
        capture_output=True, text=True, timeout=900,
    )
    cycles = [json.loads(line[2:]) for line in p.stdout.splitlines() if line.startswith("@@")]
    if not cycles:
        raise RuntimeError(f"scenario {name} produced no output:\n{p.stdout[-1500:]}\n{p.stderr[-1500:]}")
    return cycles


def compose_env() -> dict[str, str]:
    env: dict[str, str] = {}
    dotenv = REPO / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("\"'")
    return {**env, **os.environ}  # the shell environment overrides .env, as in Compose


def interpolate(value: str, env: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name, op, default = m.group(1), m.group(2) or "", m.group(3) or ""
        val = env.get(name)
        return val if (val if op.startswith(":") else val is not None) else default
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:?-([^}]*))?\}", sub, value)


def agy_mounts(files: list[Path] | None = None) -> list[tuple[Path, str]]:
    """(host source, path under /gemini in the container) for every agy-exporter* service.

    Reads every compose file Compose itself would (COMPOSE_FILE), so override files count.
    Understands both short (`src:dst:ro`) and long (`type: bind` / `source:` / `target:`) syntax.
    """
    env, out = compose_env(), []
    text = "\n".join(f.read_text() for f in (files or COMPOSE_FILES))
    for svc in re.finditer(r"^  (agy-exporter[\w-]*):\n(.*?)(?=^  \S|^\S|\Z)", text, re.S | re.M):
        lines = svc.group(2).splitlines()
        start = next((i for i, l in enumerate(lines) if l.rstrip() == "    volumes:"), None)
        if start is None:
            continue
        entries, cur = [], None
        for line in lines[start + 1:]:
            if line.strip() and not line.startswith("      "):
                break
            if line.startswith("      - "):
                if cur:
                    entries.append(cur)
                body = line[8:].strip()
                if re.match(r"^[a-z_]+:\s", body):  # long syntax, first key on the dash line
                    k, _, v = body.partition(":")
                    cur = {k.strip(): v.strip()}
                else:
                    parts = re.findall(r"(?:\$\{[^}]*\}|[^:])+", body)
                    cur = {"source": parts[0], "target": parts[1] if len(parts) > 1 else ""}
            elif cur is not None and re.match(r"^\s{8,}[a-z_]+:", line):
                k, _, v = line.strip().partition(":")
                cur.setdefault(k.strip(), v.strip())
        if cur:
            entries.append(cur)
        for e in entries:
            target = e.get("target", "")
            if not target.startswith("/gemini"):
                continue
            src = Path(os.path.expanduser(interpolate(e.get("source", ""), env)))
            if not src.is_absolute():
                src = (REPO / src).resolve()
            out.append((src, target[len("/gemini"):].strip("/")))
    return out


def compose_view(snapshot: Path, real_gemini: Path, view: Path) -> list[str]:
    """Recreate, from the snapshot, what the agy-exporter container would see."""
    mounted = []
    for src, rel in agy_mounts():
        mounted.append(rel or "/")
        try:
            snap_src = snapshot / src.relative_to(real_gemini)
        except ValueError:
            snap_src = src  # a source outside ~/.gemini, e.g. an empty placeholder
        dst = view / rel
        if snap_src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(snap_src, dst)
    return mounted


READER_FILES = re.compile(r"^(antigravity-cli|antigravity)/(conversations/|brain/|conversation_summaries\.db(-wal|-shm)?$)")


def exposed_files(real_gemini: Path) -> list[str]:
    """Paths the mounts would expose that the exporter never reads. Lists names only; opens nothing."""
    bad = []
    for src, rel in agy_mounts():
        if src.is_file():
            candidates = [rel]
        elif src.is_dir():
            candidates = [str(Path(rel) / Path(d).relative_to(src) / f)
                          for d, _, files in os.walk(src) for f in files]
        else:
            candidates = []
        bad += [c for c in candidates if not READER_FILES.match(c.lstrip("./"))]
    sensitive = re.compile(r"oauth|token|credential|settings|history|mcp_config|account", re.I)
    return sorted(bad, key=lambda p: (not sensitive.search(p), p))


def set_writable(root: Path, writable: bool) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for p in [Path(dirpath)] + [Path(dirpath) / n for n in filenames]:
            if p.is_symlink():
                continue
            mode = p.stat().st_mode
            p.chmod(mode | 0o200 if writable else mode & ~0o222)


def api_error_checks() -> None:
    os.environ.setdefault("STATE_FILE", "/nonexistent/state.json")
    spec = importlib.util.spec_from_file_location("exporter_api", EXPORTER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["exporter_api"] = mod
    spec.loader.exec_module(mod)
    fn = getattr(mod, "classify_api_error", None)
    cases = [  # the first three are verbatim strings from real Antigravity sessions
        ("API error (attempt 1): request failed: stream idle timeout (5m0s): context deadline exceeded",
         {"attempt": "1"}, "error"),
        ("There was a problem parsing the tool call.", {}, "error"),
        ("Error: model output error: model output must contain either output text or tool calls, these cannot both be empty",
         {"status_code": "empty_output"}, None),
        ("API error (attempt 2): RESOURCE_EXHAUSTED (code 429): quota",
         {"attempt": "2", "status_code": "429", "error": "RESOURCE_EXHAUSTED"}, None),
        ("API Error (attempt 2): RESOURCE_EXHAUSTED (code 429): quota",
         {"attempt": "2", "status_code": "429", "error": "RESOURCE_EXHAUSTED"}, None),
    ]
    for raw, want, not_status in cases:
        if fn is None:
            check(f"API error classified: {raw[:48]}…", False,
                  "classify_api_error(raw) -> {attempt, status_code, error} not found in exporter.py")
            continue
        got = fn(raw)
        ok = all(str(got.get(k)) == v for k, v in want.items()) and (not_status is None or got.get("status_code") != not_status)
        check(f"API error classified: {raw[:48]}…", ok, f"got {got}")

    settings = ("<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from Gemini 3.6 Flash (High) to "
                "Gemini 3.8 Flash (High). No need to comment on this change if the user doesn't ask about it. If "
                "reporting what model you are, please use a human readable name instead of the exact string.\n"
                "</USER_SETTINGS_CHANGE>")  # verbatim from a real session
    cases = [
        ("a model change is read from the settings block", f"<USER_REQUEST>\nhi\n</USER_REQUEST>\n{settings}",
         "Gemini 3.8 Flash (High)"),
        ("a model change quoted in a request is ignored", f"<USER_REQUEST>\n{settings[23:-24]}\n</USER_REQUEST>", None),
    ]
    regex = getattr(mod, "MODEL_CHANGE_RE", None)
    for name, content, want in cases:
        m = regex.search(content) if regex else None
        got = m.group(1) if m else None
        check(name, regex is not None and got == want, f"got {got!r}, want {want!r}")


def main() -> int:
    global COMPOSE_FILES, EXPORTER
    ap = argparse.ArgumentParser()
    ap.add_argument("--gemini", type=Path, default=Path.home() / ".gemini")
    ap.add_argument("--exporter", type=Path, default=EXPORTER, help="exporter.py to test (default: the one in the repo)")
    ap.add_argument("--compose", type=Path, action="append",
                    help="compose file(s) to check the mounts of; default: COMPOSE_FILE from the environment or .env")
    args = ap.parse_args()
    args.gemini = args.gemini.expanduser().absolute()
    EXPORTER = args.exporter.expanduser().absolute()
    if args.compose:
        COMPOSE_FILES = [p.expanduser().absolute() for p in args.compose]
    else:
        env = compose_env()
        listed = env.get("COMPOSE_FILE")
        if listed:
            sep = env.get("COMPOSE_PATH_SEPARATOR", os.pathsep)
            COMPOSE_FILES = [(REPO / p).resolve() for p in listed.split(sep) if p]

    work = Path(tempfile.mkdtemp(prefix="agy-check-"))
    snap, view = work / "gemini", work / "composeview"
    try:
        subprocess.run([sys.executable, "-I", "-B", str(HERE / "snapshot.py"), str(args.gemini), str(snap)],
                       check=True, capture_output=True, text=True)
        set_writable(snap, False)  # model the :ro bind mount
        expected = sorted(p.stem for p in snap.glob("*/conversations/*.db"))
        surfaces_present = sorted({"cli" if p.parts[-3] == "antigravity-cli" else "app"
                                   for p in snap.glob("*/conversations/*.db")})

        base = scenario("baseline", snap, work)
        c1, c2, c3 = base
        dupes = next((p.get("dupe_names") for p in c1["posts"] if p.get("dupe_names")), {})
        check("every conversation is exported", c1["sessions"] == len(expected),
              f"{c1['sessions']}/{len(expected)} conversations")
        check("both surfaces are exported", c1["surfaces"] == surfaces_present, f"{c1['surfaces']}")
        check("no conversation fails to parse", c1["convs_failed"] == 0, f"{c1['convs_failed']} failed")
        check("a cold start ingests no duplicate records", c1["duplicates_ingested"] == 0,
              f"{c1['duplicates_ingested']} duplicates {dupes}")
        check("unchanged data is not re-processed", c2["convs_processed"] == 0 and c2["items_ingested"] == 0,
              f"processed {c2['convs_processed']}, ingested {c2['items_ingested']}")
        check("a restart resumes from persisted cursors", c3["items_ingested"] == 0,
              f"re-ingested {c3['items_ingested']} items")
        idle_window = 3600  # ABANDON_IDLE_SEC as smoke.py sets it
        stale = [p for p in c1["pending"] if p["idle_sec"] >= idle_window]
        check("no step stays pending in a conversation idle past the abandonment window", not stale,
              f"pending in idle conversations: {stale}")

        last = scenario("unwritable-state", snap, work)[-1]
        check("an unwritable state dir never causes re-export after restart", last["duplicates_ingested"] == 0,
              f"{last['duplicates_ingested']} duplicates after restart")

        part = scenario("partial-success", snap, work)
        resent = sum(c["duplicates_ingested"] for c in part[1:])
        check("a partialSuccess response advances the cursor", resent == 0,
              f"{resent} items re-sent over {len(part) - 1} later polls")

        pois = scenario("poisoned-conversation", snap, work)[-1]
        check("one rejected conversation does not block the others",
              pois["convs_metric_cursor_advanced"] >= pois["tracked_convs"] - 1,
              f"{pois['convs_metric_cursor_advanced']}/{pois['tracked_convs']} cursors advanced")

        lim = scenario("body-limit", snap, work)
        check("a collector body limit below the batch size still makes progress",
              lim[-1]["convs_metric_cursor_advanced"] == lim[-1]["tracked_convs"]
              and sum(c["duplicates_ingested"] for c in lim[1:]) == 0,
              f"{lim[-1]['convs_metric_cursor_advanced']}/{lim[-1]['tracked_convs']} cursors advanced, "
              f"largest POST {max(c['max_post_bytes'] for c in lim) / 1e6:.1f} MB")

        ls1, ls2 = scenario("long-running-step", snap, work)
        check("a step still running in a live session is not reported as abandoned",
              ls1["abandoned_results"] + ls2["abandoned_results"] == 0,
              f"{ls1['abandoned_results'] + ls2['abandoned_results']} abandoned tool_result(s)")
        check("a long-running step's real result is exported when it completes",
              ls2["emitted"].get("agy.tool_result", 0) == 1,
              f"{ls2['emitted'].get('agy.tool_result', 0)} tool_result(s) exported when the step completed")

        cs1, cs2, cs3 = scenario("crashed-session", snap, work)
        check("in-flight steps stay pending while the session was active recently",
              cs1["abandoned_results"] == 0, f"{cs1['abandoned_results']} reported abandoned after 60 s idle")
        check("in-flight tool steps of an idle session are emitted as abandoned, once",
              cs2["abandoned_results"] == 2 and cs3["items_ingested"] == 0,
              f"{cs2['abandoned_results']} abandoned after an hour idle (want 2), {cs3['items_ingested']} re-sent")
        check("an unanswered permission prompt is not recorded as an approval",
              sum(cs2["decisions"].values()) == 1 and cs2["decisions"].get("accept", 0) == 0,
              f"decisions when abandoned: {cs2['decisions']}")
        check("an abandoned step without a tool call emits no tool_result",
              cs2["unknown_tool_results"] == 0 and cs2["emitted"].get("agy.tool_result", 0) == 2,
              f"{cs2['emitted'].get('agy.tool_result', 0)} tool_results (want 2), "
              f"{cs2['unknown_tool_results']} with tool_name 'unknown'")

        q1, q2 = scenario("session-goes-quiet", snap, work)
        check("a silent session's in-flight steps are exported once the idle window passes, with no write",
              q1["abandoned_results"] == 0 and q2["abandoned_results"] == 2,
              f"abandoned {q1['abandoned_results']} while fresh and {q2['abandoned_results']} after the window "
              f"(want 0 and 2)")

        me = scenario("misconfigured-endpoint", snap, work)
        got = sum(c["items_ingested"] for c in me)
        check("an auth or routing error (401, 404) holds the data and recovers once fixed",
              me[-1]["convs_metric_cursor_advanced"] == me[-1]["tracked_convs"]
              and me[-1]["convs_log_cursor_advanced"] == me[-1]["tracked_convs"]
              and got == c1["items_ingested"] and sum(c["duplicates_ingested"] for c in me) == 0,
              f"{got}/{c1['items_ingested']} items once fixed, "
              f"{me[-1]['convs_metric_cursor_advanced']}/{me[-1]['tracked_convs']} metric cursors advanced")

        sp = scenario("subbatch-partial-failure", snap, work)
        bad = [r["cycle"] for r in sp
               if r["duplicates_ingested"] or r["items_ingested"] != c1["items_ingested"]
               or r["convs_metric_cursor_advanced"] != r["tracked_convs"]
               or r["convs_log_cursor_advanced"] != r["tracked_convs"]]
        check("a request failing mid-delivery is retried without re-sending accepted data", not bad,
              f"wrong for: {bad}; duplicates per case {[r['duplicates_ingested'] for r in sp]}")

        cr = scenario("crash-mid-delivery", snap, work)
        bad = [r["cycle"] for r in cr
               if r["duplicates_ingested"] or r["items_ingested"] != c1["items_ingested"]
               or r["convs_metric_cursor_advanced"] != r["tracked_convs"]
               or r["convs_log_cursor_advanced"] != r["tracked_convs"]]
        check("a crash between requests neither loses nor duplicates anything after the restart", not bad,
              f"wrong for: {bad}; duplicates per case {[r['duplicates_ingested'] for r in cr]}")

        pr1, pr2, pr3 = scenario("poisoned-record", snap, work)
        got = pr1["items_ingested"] + pr2["items_ingested"] + pr3["items_ingested"]
        check("a refused record is dropped alone and the rest of its conversation is exported",
              pr3["poison_ingested"] == 0 and got == c1["items_ingested"] - 1,
              f"{got}/{c1['items_ingested'] - 1} items ingested")
        check("a transient logs failure is retried for an idle conversation",
              pr2["convs_log_cursor_advanced"] == pr2["tracked_convs"],
              f"{pr2['convs_log_cursor_advanced']}/{pr2['tracked_convs']} log cursors advanced after the retry")
        check("a refused record is not re-sent when its conversation changes",
              pr3["items_ingested"] == 0 and "400" not in pr3["statuses"],
              f"after the change: {pr3['items_ingested']} items, responses {pr3['statuses']}")

        cl = scenario("close-on-large", snap, work)
        check("a collector that drops the connection on oversized bodies still makes progress",
              cl[-1]["convs_metric_cursor_advanced"] == cl[-1]["tracked_convs"]
              and sum(c["duplicates_ingested"] for c in cl) == 0,
              f"{cl[-1]['convs_metric_cursor_advanced']}/{cl[-1]['tracked_convs']} cursors advanced, "
              f"responses per poll {[c['statuses'] for c in cl]}")

        sf1, sf2 = scenario("save-fails-after-post", snap, work)
        check("a failed state save after a POST neither exits nor duplicates after a restart",
              sf1["exited"] is None and sf1["duplicates_ingested"] + sf2["duplicates_ingested"] == 0
              and sf2["state_file_written"],
              f"exited={sf1['exited']}, duplicates {sf1['duplicates_ingested']}+{sf2['duplicates_ingested']}, "
              f"state written={sf2['state_file_written']}")
        check("nothing more is sent while the state cannot be saved",
              sf1["posts_per_poll"][0] > 0 and sf1["posts_per_poll"][1:3] == [0, 0],
              f"POSTs per poll {sf1['posts_per_poll']} (disk full for polls 1-3)")

        ds1, ds2 = scenario("damaged-state", snap, work)
        check("each state save fsyncs the file before the rename and the directory after it",
              ds1["saves_fsynced_in_order"], f"{ds1['saves']} saves, not all in order")
        check("a damaged state file stops the exporter instead of exporting everything again",
              ds1["items_ingested"] > 0 and ds2["exited"] not in (None, 0) and ds2["items_ingested"] == 0
              and ds2["state_file_unchanged"],
              f"exited={ds2['exited']}, sent {ds2['items_ingested']} items ({ds2['duplicates_ingested']} duplicates) "
              f"after the restart, damaged file kept={ds2['state_file_unchanged']}")

        sk = scenario("skill-per-turn", snap, work)[0]
        check("a skill is credited only with requests in the user turn that read it", sk["later_turn"] == 0,
              f"{sk['later_turn']} of {sk['credited']} skill-credited requests are from a later turn")

        ug1, ug2 = scenario("unindexed-generation", snap, work)
        check("a generation without step indices waits for its planner step to finish",
              ug1["api_requests"] == ug1["generations"] - 1 and ug2["api_requests"] == 1,
              f"{ug1['api_requests']}/{ug1['generations']} requests exported while the step streamed, "
              f"{ug2['api_requests']} when it finished (want {ug1['generations'] - 1}, then 1)")

        base_only = agy_mounts([REPO / "docker-compose.yml"])
        check("the base compose file mounts no Antigravity data (each surface is an opt-in override)",
              not base_only, f"docker-compose.yml alone mounts {[rel for _, rel in base_only]}")

        exposed = exposed_files(args.gemini)
        check("the container's mounts expose nothing the exporter doesn't read (credentials, history)",
              not exposed, f"{len(exposed)} extra file(s), e.g. {exposed[:4]}")

        mounted = compose_view(snap, args.gemini, view)
        cm = scenario("compose-mounts", view, work)[0]
        check("the container's mounts expose everything the exporter reads",
              cm["sessions"] == c1["sessions"] and cm["surfaces"] == c1["surfaces"]
              and cm["distinct_agent_names"] == c1["distinct_agent_names"],
              f"container sees {cm['sessions']}/{c1['sessions']} conversations, surfaces {cm['surfaces']}, "
              f"agent names {cm['distinct_agent_names']}/{c1['distinct_agent_names']} (mounts: {mounted})")

        nosum = work / "no-summaries"
        for surface in ("antigravity-cli", "antigravity"):
            for sub in ("conversations", "brain"):
                if (snap / surface / sub).exists():
                    (nosum / surface).mkdir(parents=True, exist_ok=True)
                    os.symlink(snap / surface / sub, nosum / surface / sub)
        ns = scenario("compose-mounts", nosum, work, tag="no-summaries")[0]
        if c1["distinct_agent_names"]:
            check("agent names come from each conversation's own database, not only the summaries file",
                  ns["distinct_agent_names"] == c1["distinct_agent_names"],
                  f"{ns['distinct_agent_names']}/{c1['distinct_agent_names']} agent names without conversation_summaries.db")
        else:
            check("agent names come from each conversation's own database (no subagent in the data: not exercised)", True)

        wal = subprocess.run([sys.executable, "-I", "-B", str(HERE / "walcheck.py"), str(EXPORTER)],
                             capture_output=True, text=True, timeout=120)
        m = re.search(r"exporter sees: (\d+)", wal.stdout)
        check("live un-checkpointed WAL rows are visible through a read-only mount",
              bool(m) and m.group(1) == "100", (wal.stdout.strip().splitlines() or [wal.stderr.strip()])[0])

        df = DOCKERFILE.read_text().splitlines()
        user_at = next((i for i, l in enumerate(df) if l.strip().startswith("USER")), len(df))
        check("the image creates /state for the non-root user before USER",
              any(l.strip().startswith("RUN") and "/state" in l for l in df[:user_at]),
              "no RUN line creating/chowning /state before USER")

        api_error_checks()
    finally:
        if snap.exists():
            set_writable(snap, True)
        shutil.rmtree(work, ignore_errors=True)

    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {'' if ok else detail}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
