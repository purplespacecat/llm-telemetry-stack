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


def scenario(name: str, gemini: Path, work: Path) -> list[dict]:
    p = subprocess.run(
        [sys.executable, "-I", "-B", str(HERE / "smoke.py"), name, str(EXPORTER), str(gemini), str(work / name)],
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


def agy_mounts() -> list[tuple[Path, str]]:
    """(host source, path under /gemini in the container) for every agy-exporter* service.

    Reads every compose file Compose itself would (COMPOSE_FILE), so override files count.
    Understands both short (`src:dst:ro`) and long (`type: bind` / `source:` / `target:`) syntax.
    """
    env, out = compose_env(), []
    text = "\n".join(f.read_text() for f in COMPOSE_FILES)
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


def main() -> int:
    global COMPOSE_FILES
    ap = argparse.ArgumentParser()
    ap.add_argument("--gemini", type=Path, default=Path.home() / ".gemini")
    ap.add_argument("--compose", type=Path, action="append",
                    help="compose file(s) to check the mounts of; default: COMPOSE_FILE from the environment or .env")
    args = ap.parse_args()
    args.gemini = args.gemini.expanduser().absolute()
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
        check("steps stuck past the grace window are emitted, not pinned", c1["pending_steps_total"] == 0,
              f"{c1['pending_steps_total']} step(s) pinned in pending")

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
