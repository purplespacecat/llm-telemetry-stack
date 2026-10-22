#!/usr/bin/env python3
"""Antigravity (agy) OpenTelemetry Exporter Sidecar.

Reads local Antigravity conversation databases and transcripts from ~/.gemini
(both antigravity-cli and antigravity desktop) and exports OpenTelemetry
metrics and logs via OTLP/HTTP JSON to the OpenTelemetry Collector.

Reverse-engineered protobuf schema notes (Antigravity / Cortex SQLite store):
- `trajectory_metadata_blob.data` (CortexTrajectoryMetadata):
  - field 1 (WorkspaceInfo):
    - field 1 (string): workspace URI (`file:///...`)
    - field 3 (GitInfo): field 1 (string) = `vcs.repository.name`, field 2 = remote URL
    - field 4 (string): `vcs.branch.name`
  - field 2 (google.protobuf.Timestamp): trajectory creation time
- `gen_metadata.data` (GeneratorMetadataEntry):
  - field 1 (GeneratorMetadata):
    - field 4 (TokenUsage):
      - field 2 (varint): uncached `input_tokens`
      - field 3 (varint): `output_tokens`
      - field 5 (varint): `cache_read_tokens`
      - field 9 (varint): `thinking_tokens`
      - field 10 (varint): `response_tokens`
    - field 11 (google.protobuf.Duration): total generation duration
    - field 12 (google.protobuf.Duration): time to first token (TTFT)
    - field 19 (string): model ID slug (e.g. `gemini-pro-default`)
    - field 20 (repeated KeyValue): metadata map (`last_step_index`, `request_id`, etc.)
    - field 21 (string): human-readable model display name (e.g. `Gemini 4 Argon (Medium)`)
  - field 2 (packed repeated varint): step indices produced by this generation
- `steps` table:
  - `step_type`: 14 = USER_INPUT, 15 = PLANNER_RESPONSE, 17/23 = ERROR_MESSAGE, etc.
  - `status`: 1 = PENDING, 2 = RUNNING, 3 = DONE, 6 = CANCELED, 7 = ERROR, 9 = WAITING_FOR_USER
  - `metadata` (CortexStepMetadata):
    - field 1 (Timestamp): created_at
    - field 4 (ToolCallInfo): field 1 = call_id, field 2 = tool_name, field 3 = args JSON
    - field 6 (Timestamp): started_at
    - field 8 (Timestamp): completed_at
    - field 26 (StepStatusTransitions): repeated field 1 {field 1 = status, field 2 = Timestamp}
  - `permissions` (CortexStepPermissions):
    - repeated field 2 (PermissionRequest):
      - field 2 (varint): 1 = accepted, 0 = rejected
      - field 3 (varint): >0 when auto-approved by config/session rule, omitted when user prompted
      - field 5 (string): permission scope (`command`, `unsandboxed`, `read_file`, `write_file`, `mcp`)
  - `error_details` (CortexErrorDetails):
    - field 1 / field 2 (string): user-facing / short error message
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

GEMINI_DIR = Path(os.environ.get("GEMINI_DIR", "/gemini"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/state/state.json"))
OTLP_HTTP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318").rstrip("/")
POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "5"))
IN_FLIGHT_STEP_GRACE_SEC = 120.0

# API list prices in USD per 1M tokens: (input_per_1m, cache_read_per_1m, output_per_1m)
MODEL_PRICING: list[tuple[str, tuple[float, float, float]]] = [
    ("claude-opus", (15.00, 1.50, 75.00)),
    ("opus", (15.00, 1.50, 75.00)),
    ("claude-sonnet", (3.00, 0.30, 15.00)),
    ("sonnet", (3.00, 0.30, 15.00)),
    ("flash-lite", (0.10, 0.025, 0.40)),
    ("flash", (0.30, 0.075, 2.50)),
    ("argon", (1.25, 0.3125, 10.00)),
    ("pro", (1.25, 0.3125, 10.00)),
]
DEFAULT_PRICING = (1.25, 0.3125, 10.00)

SKILL_PATH_RE = re.compile(r"/skills/([^/]+)/SKILL\.md")
EXIT_CODE_RE = re.compile(r"(?:Command exited with code|command failed with exit code:?)\s+(\d+)", re.IGNORECASE)
API_ERROR_RE = re.compile(
    r"API error(?:\s*\(attempt\s*(\d+)\))?:\s*([A-Z_]+)?(?:\s*\(code\s*(\d+)\))?:\s*(.*)",
    re.IGNORECASE,
)


def first_line(text: Any) -> str:
    """Safely return the first non-empty line of a string or bytes without IndexError."""
    if not text:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    lines = str(text).splitlines()
    return lines[0].strip() if lines else ""


def parse_wire(buf: bytes | None) -> dict[int, list[Any]]:
    """Minimal protobuf wire format decoder using only the standard library."""
    if not buf:
        return {}
    i, n = 0, len(buf)
    fields: dict[int, list[Any]] = {}
    while i < n:
        tag, shift = 0, 0
        while i < n:
            b = buf[i]
            i += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        fn, wt = tag >> 3, tag & 7
        if fn == 0:
            break
        if wt == 0:
            val, shift = 0, 0
            while i < n:
                b = buf[i]
                i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
        elif wt == 2:
            ln, shift = 0, 0
            while i < n:
                b = buf[i]
                i += 1
                ln |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if i + ln > n:
                break
            val = buf[i : i + ln]
            i += ln
        elif wt == 5:
            if i + 4 > n:
                break
            val = struct.unpack("<f", buf[i : i + 4])[0]
            i += 4
        elif wt == 1:
            if i + 8 > n:
                break
            val = struct.unpack("<d", buf[i : i + 8])[0]
            i += 8
        else:
            break
        fields.setdefault(fn, []).append(val)
    return fields


def parse_packed_varints(buf: bytes) -> list[int]:
    i, n = 0, len(buf)
    out: list[int] = []
    while i < n:
        val, shift = 0, 0
        while i < n:
            b = buf[i]
            i += 1
            val |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        out.append(val)
    return out


def proto_ts_ns(buf: bytes | None) -> int:
    if not buf:
        return 0
    f = parse_wire(buf)
    sec = f.get(1, [0])[0]
    nanos = f.get(2, [0])[0]
    if not isinstance(sec, int) or sec <= 0:
        return 0
    return sec * 1_000_000_000 + (nanos if isinstance(nanos, int) else 0)


def proto_duration_ms(buf: bytes | None) -> float:
    if not buf:
        return 0.0
    f = parse_wire(buf)
    sec = f.get(1, [0])[0]
    nanos = f.get(2, [0])[0]
    return (sec if isinstance(sec, int) else 0) * 1000.0 + (nanos if isinstance(nanos, int) else 0) / 1_000_000.0


def estimate_cost_usd(model: str, input_tokens: int, cache_read_tokens: int, output_tokens: int) -> float:
    m_lower = model.lower()
    in_rate, cache_rate, out_rate = DEFAULT_PRICING
    for key, rates in MODEL_PRICING:
        if key in m_lower:
            in_rate, cache_rate, out_rate = rates
            break
    return (
        (input_tokens * in_rate)
        + (cache_read_tokens * cache_rate)
        + (output_tokens * out_rate)
    ) / 1_000_000.0


def classify_skill_source(path_str: str) -> str:
    if "/builtin/skills/" in path_str:
        return "builtin"
    if "/.gemini/config/plugins/" in path_str or "/plugins/" in path_str:
        return "plugin"
    if "/.gemini/config/skills/" in path_str:
        return "user"
    return "workspace"


def classify_error_type(err_msg: str, exit_code: int | None = None) -> str:
    if exit_code is not None and exit_code != 0:
        return f"ExitCode_{exit_code}"
    low = err_msg.lower()
    if "user denied permission" in low:
        return "UserPermissionDenied"
    if "permission denied" in low or "permission check failed" in low:
        return "PermissionBoundary"
    if "timed out" in low or "timeout" in low or "deadline exceeded" in low:
        return "Timeout"
    if "context canceled" in low or "cancelled" in low:
        return "Canceled"
    if "invalid_args" in low or "invalid tool call" in low or "invalid task id" in low:
        return "InvalidArgs"
    if "mcp tool execution" in low or "httperror" in low:
        return "McpError"
    if "no summary returned" in low:
        return "EmptyModelSummary"
    if "failed to resolve subagent" in low:
        return "SubagentConfigError"
    return "ToolError"


def otlp_kv(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for k, v in attrs.items():
        if v is None:
            continue
        out.append({"key": k, "value": {"stringValue": str(v)}})
    return out


def make_sum_metric(name: str, data_points: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": name,
        "sum": {
            "aggregationTemporality": 1,  # AGGREGATION_TEMPORALITY_DELTA
            "isMonotonic": True,
            "dataPoints": data_points,
        },
    }


def make_dp(ts_ns: int, value: float, attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        "startTimeUnixNano": str(ts_ns),
        "timeUnixNano": str(ts_ns),
        "asDouble": float(value),
        "attributes": otlp_kv(attrs),
    }


def make_log_record(ts_ns: int, event_name: str, attrs: dict[str, Any], body: str = "") -> dict[str, Any]:
    merged = {"event.name": event_name, **attrs}
    return {
        "timeUnixNano": str(ts_ns),
        "observedTimeUnixNano": str(ts_ns),
        "body": {"stringValue": body or event_name},
        "attributes": otlp_kv(merged),
    }


def post_otlp(path: str, payload: dict[str, Any]) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OTLP_HTTP_ENDPOINT}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        print(f"[agy-exporter] Failed POST {path}: {exc}", flush=True)
        return False


def copy_sqlite_with_wal(src_db: Path, dst_db: Path) -> None:
    """Copy a SQLite database and its -wal journal (excluding -shm so SQLite rebuilds it cleanly)."""
    shutil.copy2(src_db, dst_db)
    src_wal = Path(str(src_db) + "-wal")
    dst_wal = Path(str(dst_db) + "-wal")
    dst_shm = Path(str(dst_db) + "-shm")
    if dst_shm.exists():
        dst_shm.unlink()
    if src_wal.exists():
        shutil.copy2(src_wal, dst_wal)
    elif dst_wal.exists():
        dst_wal.unlink()


def cleanup_sqlite_copy(dst_db: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(dst_db) + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


class SummaryCache:
    """Caches conversation_summaries.db per surface, reloading only when mtime changes."""

    def __init__(self) -> None:
        self._mtimes: dict[str, int] = {}
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    def get(self, surface_dir: Path, tmp_dir: Path) -> dict[str, dict[str, Any]]:
        db_file = surface_dir / "conversation_summaries.db"
        if not db_file.exists():
            return {}
        wal_file = Path(str(db_file) + "-wal")
        mtime = max(
            db_file.stat().st_mtime_ns,
            wal_file.stat().st_mtime_ns if wal_file.exists() else 0,
        )
        key = str(surface_dir)
        if self._mtimes.get(key) == mtime and key in self._data:
            return self._data[key]

        dst = tmp_dir / f"summaries_{surface_dir.name}.db"
        summaries: dict[str, dict[str, Any]] = {}
        try:
            copy_sqlite_with_wal(db_file, dst)
            conn = sqlite3.connect(str(dst))
            try:
                rows = conn.execute(
                    "SELECT conversation_id, workspace_uris, agent_name, parent_conversation_id, nesting_depth "
                    "FROM conversation_summaries"
                ).fetchall()
            finally:
                conn.close()
            for cid, ws_json, agent_name, parent_cid, depth in rows:
                repo = ""
                try:
                    uris = json.loads(ws_json) if ws_json else []
                    if uris:
                        u = uris[0].replace("file://", "").rstrip("/")
                        repo = os.path.basename(u)
                except Exception:
                    pass
                summaries[cid] = {
                    "repository": repo,
                    "agent_name": agent_name or "",
                    "parent_conversation_id": parent_cid or "",
                    "nesting_depth": int(depth or 0),
                }
            self._mtimes[key] = mtime
            self._data[key] = summaries
        except Exception as exc:
            print(f"[agy-exporter] Error reading {db_file}: {exc}", flush=True)
        finally:
            cleanup_sqlite_copy(dst)
        return self._data.get(key, {})


SUMMARY_CACHE = SummaryCache()


def load_transcript_map(surface_dir: Path, cid: str, min_step_idx: int = 0) -> dict[int, dict[str, Any]]:
    t_path = surface_dir / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
    if not t_path.exists():
        return {}
    steps: dict[int, dict[str, Any]] = {}
    try:
        with t_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and "step_index" in obj:
                        s_idx = int(obj["step_index"])
                        if s_idx >= min_step_idx or obj.get("type") == "USER_INPUT":
                            steps[s_idx] = obj
                except Exception:
                    continue
    except Exception:
        pass
    return steps


def process_conversation_db(
    surface: str,
    surface_dir: Path,
    db_path: Path,
    summary_info: dict[str, Any],
    conv_state: dict[str, Any],
    tmp_dir: Path,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
    dict[str, Any],
    bool,
]:
    """Extract new generation and step telemetry for a single conversation.

    Returns:
        gen_metrics: metrics derived from `gen_metadata` (committed with `last_gen_idx`)
        step_metrics: metrics derived from `steps` (committed with `last_step_idx`)
        step_logs: log records derived from `gen_metadata` and `steps`
        next_state: updated conversation cursor dictionary
        has_in_flight: True if a trailing step is still running so mtime should be re-checked
    """
    cid = db_path.stem
    dst = tmp_dir / f"{surface}_{cid}.db"
    copy_sqlite_with_wal(db_path, dst)

    gen_metrics: dict[str, list[dict[str, Any]]] = {}
    step_metrics: dict[str, list[dict[str, Any]]] = {}
    gen_logs: list[dict[str, Any]] = []
    step_logs: list[dict[str, Any]] = []
    has_in_flight = False

    def add_gen_metric(name: str, ts_ns: int, val: float, attrs: dict[str, Any]) -> None:
        gen_metrics.setdefault(name, []).append(make_dp(ts_ns, val, attrs))

    def add_step_metric(name: str, ts_ns: int, val: float, attrs: dict[str, Any]) -> None:
        step_metrics.setdefault(name, []).append(make_dp(ts_ns, val, attrs))

    conn = sqlite3.connect(str(dst))
    try:
        # 1. Extract repository and branch from trajectory_metadata_blob
        repo = summary_info.get("repository", "")
        branch = ""
        traj_created_ns = 0
        row = conn.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1").fetchone()
        if row and row[0]:
            tm = parse_wire(row[0])
            traj_created_ns = proto_ts_ns(tm.get(2, [b""])[0])
            ws_info = parse_wire(tm.get(1, [b""])[0])
            git_info = parse_wire(ws_info.get(3, [b""])[0])
            git_repo = git_info.get(1, [b""])[0].decode("utf-8", errors="replace")
            if git_repo:
                repo = git_repo
            elif not repo:
                ws_uri = ws_info.get(1, [b""])[0].decode("utf-8", errors="replace").replace("file://", "").rstrip("/")
                if ws_uri:
                    repo = os.path.basename(ws_uri)
            branch = ws_info.get(4, [b""])[0].decode("utf-8", errors="replace")

        nesting_depth = summary_info.get("nesting_depth", 0)
        query_source = "subagent" if nesting_depth > 0 else "main"
        agent_name = summary_info.get("agent_name", "")

        base_attrs = {
            "session.id": cid,
            "surface": surface,
            "vcs.repository.name": repo,
            "vcs.branch.name": branch,
            "query_source": query_source,
            "agent.name": agent_name,
        }

        last_metric_gen_idx = int(conv_state.get("last_metric_gen_idx", conv_state.get("last_gen_idx", -1)))
        last_log_gen_idx = int(conv_state.get("last_log_gen_idx", conv_state.get("last_gen_idx", -1)))
        last_metric_step_idx = int(conv_state.get("last_metric_step_idx", conv_state.get("last_step_idx", -1)))
        last_log_step_idx = int(conv_state.get("last_log_step_idx", conv_state.get("last_step_idx", -1)))
        pending_metric_steps: set[int] = {int(x) for x in conv_state.get("pending_metric_steps", conv_state.get("pending_steps", []))}
        pending_log_steps: set[int] = {int(x) for x in conv_state.get("pending_log_steps", conv_state.get("pending_steps", []))}

        min_gen_idx = min(last_metric_gen_idx, last_log_gen_idx)
        min_step_idx = min(last_metric_step_idx, last_log_step_idx)
        all_pending_steps = pending_metric_steps | pending_log_steps

        max_db_gen = conn.execute("SELECT coalesce(max(idx), -1) FROM gen_metadata").fetchone()[0]
        max_db_step = conn.execute("SELECT coalesce(max(idx), -1) FROM steps").fetchone()[0]
        if max_db_gen <= min_gen_idx and max_db_step <= min_step_idx and not all_pending_steps:
            return gen_metrics, step_metrics, [], conv_state, False

        # Load steps and parse wire metadata once per step
        step_rows = conn.execute(
            "SELECT idx, step_type, status, metadata, permissions, error_details FROM steps ORDER BY idx ASC"
        ).fetchall()

        min_needed_step = min([min_step_idx + 1] + list(all_pending_steps)) if all_pending_steps else (min_step_idx + 1)
        transcript_steps = load_transcript_map(surface_dir, cid, min_step_idx=max(0, min_needed_step))

        parsed_steps: list[tuple[int, int, int, dict[int, list[Any]], bytes | None, bytes | None, str, str]] = []
        step_ts_map: dict[int, int] = {}
        step_status_map: dict[int, int] = {}
        active_skill_by_step: dict[int, str] = {}
        active_mcp_by_step: dict[int, str] = {}
        current_skill = conv_state.get("active_skill", "")
        current_model = conv_state.get("current_model", "auto")
        last_slash_skill = ""

        for idx, stype, status, meta_blob, perm_blob, err_blob in step_rows:
            m = parse_wire(meta_blob)
            t_created = proto_ts_ns(m.get(1, [b""])[0])
            if t_created > 0:
                step_ts_map[idx] = t_created
            step_status_map[idx] = status

            t_step = transcript_steps.get(idx, {})
            content_str = t_step.get("content") or ""
            if stype == 14 and content_str:
                m_change = re.search(r"changed setting `Model Selection` from .* to ([^.\n]+)\.", content_str)
                if m_change:
                    current_model = m_change.group(1).strip()
                m_req = re.search(r"<USER_REQUEST>\s*/([a-zA-Z0-9_-]+)", content_str)
                if m_req:
                    last_slash_skill = m_req.group(1).strip()

            tc = parse_wire(m.get(4, [b""])[0])
            tname = tc.get(2, [b""])[0].decode("utf-8", errors="replace")
            targs_raw = tc.get(3, [b""])[0].decode("utf-8", errors="replace")
            if tname in ("read_file", "view_file") and "SKILL.md" in targs_raw:
                sm = SKILL_PATH_RE.search(targs_raw)
                if sm:
                    current_skill = sm.group(1)
            if tname == "call_mcp_tool":
                try:
                    parsed_args = json.loads(targs_raw)
                    srv = parsed_args.get("ServerName", "")
                    if srv:
                        active_mcp_by_step[idx] = srv
                except Exception:
                    pass
            elif tname.startswith("mcp_"):
                parts = tname.split("_")
                if len(parts) >= 3:
                    active_mcp_by_step[idx] = parts[1]
            active_skill_by_step[idx] = current_skill
            parsed_steps.append((idx, stype, status, m, perm_blob, err_blob, tname, targs_raw))

        conv_state["active_skill"] = current_skill
        conv_state["current_model"] = current_model

        # Emit session count once per conversation
        if not conv_state.get("session_counted"):
            first_ts = step_ts_map.get(0) or step_ts_map.get(1) or traj_created_ns or int(time.time() * 1e9)
            add_gen_metric("agy.session.count", first_ts, 1.0, base_attrs)
            conv_state["session_counted"] = True

        # 2. Process gen_metadata (API requests, tokens, cost, latency, TTFT)
        gen_rows = conn.execute(
            "SELECT idx, data FROM gen_metadata WHERE idx > ? ORDER BY idx ASC", (min_gen_idx,)
        ).fetchall()

        now_ns = int(time.time() * 1e9)

        for g_idx, g_data in gen_rows:
            f = parse_wire(g_data)
            step_indices = parse_packed_varints(f.get(2, [b""])[0])
            first_step_idx: int | None = step_indices[0] if step_indices else None

            # Guard against in-flight generation race: if the planner step is still in-flight, wait for next poll
            if first_step_idx is not None and step_status_map.get(first_step_idx) in (0, 1, 2):
                step_age = (now_ns - step_ts_map.get(first_step_idx, now_ns)) / 1e9
                if step_age < IN_FLIGHT_STEP_GRACE_SEC:
                    has_in_flight = True
                break

            need_gen_metric = g_idx > last_metric_gen_idx
            need_gen_log = g_idx > last_log_gen_idx

            for m1 in f.get(1, []):
                gm = parse_wire(m1)
                kv = {
                    parse_wire(x).get(1, [b""])[0].decode("utf-8", errors="replace"): parse_wire(x)
                    .get(2, [b""])[0]
                    .decode("utf-8", errors="replace")
                    for x in gm.get(20, [])
                }
                if first_step_idx is None and "last_step_index" in kv:
                    try:
                        first_step_idx = int(kv["last_step_index"]) + 1
                    except ValueError:
                        first_step_idx = 0

                resolved_step_idx = first_step_idx if first_step_idx is not None else 0

                model_display = gm.get(21, [b""])[0].decode("utf-8", errors="replace")
                model_slug = gm.get(19, [b""])[0].decode("utf-8", errors="replace")
                model_name = model_display or model_slug or current_model or "unknown"

                usage = parse_wire(gm.get(4, [b""])[0])
                in_tok = int(usage.get(2, [0])[0] or 0)
                out_tok = int(usage.get(3, [0])[0] or 0)
                cache_tok = int(usage.get(5, [0])[0] or 0)
                think_tok = int(usage.get(9, [0])[0] or 0)

                dur_ms = proto_duration_ms(gm.get(11, [b""])[0])
                ttft_ms = proto_duration_ms(gm.get(12, [b""])[0])

                ts_ns = step_ts_map.get(resolved_step_idx) or traj_created_ns or now_ns
                skill_for_turn = active_skill_by_step.get(resolved_step_idx, "")
                mcp_for_turn = ""
                for s_i in step_indices:
                    if s_i in active_mcp_by_step:
                        mcp_for_turn = active_mcp_by_step[s_i]
                        break

                turn_attrs = {
                    **base_attrs,
                    "model": model_name,
                    "skill.name": skill_for_turn,
                    "mcp_server.name": mcp_for_turn,
                }

                if need_gen_metric:
                    cost_usd = estimate_cost_usd(model_name, in_tok, cache_tok, out_tok)
                    if cost_usd > 0:
                        add_gen_metric("agy.cost.usage", ts_ns, cost_usd, turn_attrs)

                    if in_tok > 0:
                        add_gen_metric("agy.token.usage", ts_ns, float(in_tok), {**turn_attrs, "type": "input"})
                    if cache_tok > 0:
                        add_gen_metric("agy.token.usage", ts_ns, float(cache_tok), {**turn_attrs, "type": "cacheRead"})
                    if out_tok > 0:
                        add_gen_metric("agy.token.usage", ts_ns, float(out_tok), {**turn_attrs, "type": "output"})

                    if dur_ms > 0:
                        add_gen_metric("agy.active_time.total", ts_ns, dur_ms / 1000.0, {**base_attrs, "type": "cli"})

                if need_gen_log:
                    gen_logs.append(
                        make_log_record(
                            ts_ns,
                            "agy.api_request",
                            {
                                **turn_attrs,
                                "duration_ms": f"{dur_ms:.1f}",
                                "ttft_ms": f"{ttft_ms:.1f}",
                                "input_tokens": str(in_tok),
                                "cache_read_tokens": str(cache_tok),
                                "output_tokens": str(out_tok),
                                "thinking_tokens": str(think_tok),
                            },
                        )
                    )
            last_metric_gen_idx = max(last_metric_gen_idx, g_idx)
            last_log_gen_idx = max(last_log_gen_idx, g_idx)

        conv_state["last_metric_gen_idx"] = last_metric_gen_idx
        conv_state["last_log_gen_idx"] = last_log_gen_idx
        conv_state["last_gen_idx"] = min(last_metric_gen_idx, last_log_gen_idx)

        # 3. Process steps (Tools, Skills, Permissions, Subagents, LOC/Commits/PRs, Errors)
        new_pending_metric_steps: set[int] = set()
        new_pending_log_steps: set[int] = set()

        for idx, stype, status, m, perm_blob, err_blob, tname, targs_raw in parsed_steps:
            need_step_metric = (idx > last_metric_step_idx) or (idx in pending_metric_steps)
            need_step_log = (idx > last_log_step_idx) or (idx in pending_log_steps)
            if not need_step_metric and not need_step_log:
                continue

            t_created = proto_ts_ns(m.get(1, [b""])[0])
            t_started = proto_ts_ns(m.get(6, [b""])[0]) or t_created
            t_completed = proto_ts_ns(m.get(8, [b""])[0]) or t_started

            # If a step is still in-flight (PENDING/RUNNING/WAITING_FOR_USER), do not block subsequent
            # completed steps (e.g. backgrounded commands). Keep it in pending_*_steps so if it finishes
            # later (triggering a DB mtime update), it will be exported then; only set has_in_flight
            # while the step is recent (< IN_FLIGHT_STEP_GRACE_SEC) so old abandoned steps don't poll.
            if status in (0, 1, 2, 9):
                age_sec = (now_ns - (t_created or now_ns)) / 1e9
                if age_sec < IN_FLIGHT_STEP_GRACE_SEC:
                    has_in_flight = True
                if need_step_metric:
                    new_pending_metric_steps.add(idx)
                if need_step_log:
                    new_pending_log_steps.add(idx)
                last_metric_step_idx = max(last_metric_step_idx, idx)
                last_log_step_idx = max(last_log_step_idx, idx)
                continue

            # Check status transitions in field 26: status=9 is waiting on user permission,
            # and the last status=2 is when tool execution actually began after approval.
            t_exec_start = t_started
            t_wait_start = 0
            for sub in m.get(26, []):
                s26 = parse_wire(sub)
                for item in s26.get(1, []):
                    it = parse_wire(item)
                    st_code = it.get(1, [0])[0]
                    st_ts = proto_ts_ns(it.get(2, [b""])[0])
                    if st_ts <= 0:
                        continue
                    if st_code == 9 and t_wait_start == 0:
                        t_wait_start = st_ts
                    elif st_code == 2:
                        t_exec_start = st_ts
                    elif st_code in (3, 6, 7):
                        t_completed = st_ts

            ts_ns = t_created or traj_created_ns or now_ns
            dur_ms = max(0.0, (t_completed - t_exec_start) / 1_000_000.0)

            args_dict: dict[str, Any] = {}
            if targs_raw:
                try:
                    parsed = json.loads(targs_raw)
                    if isinstance(parsed, dict):
                        args_dict = parsed
                except Exception:
                    pass

            t_step = transcript_steps.get(idx, {})
            t_type = t_step.get("type", "")
            t_content = t_step.get("content") or ""
            t_err = t_step.get("error") or ""

            err_msg = ""
            if err_blob:
                ef = parse_wire(err_blob)
                raw_bytes = (ef.get(1) or ef.get(2) or [b""])[0]
                err_msg = first_line(raw_bytes)
            if not err_msg and t_err:
                err_msg = first_line(t_err)

            if not need_step_metric:
                add_step_metric_fn = lambda *a, **kw: None
            else:
                add_step_metric_fn = add_step_metric

            def emit_step_log(rec: dict[str, Any]) -> None:
                if need_step_log:
                    step_logs.append(rec)

            # Check for API / Model output errors (ERROR_MESSAGE step)
            if t_type == "ERROR_MESSAGE" or (stype in (17, 23) and (err_msg or "Error" in t_content)):
                raw_err = err_msg or first_line(t_content) or "Model error"
                m_api = API_ERROR_RE.search(raw_err)
                if m_api:
                    attempt = m_api.group(1) or "1"
                    status_code = m_api.group(3) or m_api.group(2) or "500"
                    clean_err = m_api.group(2) or "APIError"
                elif "invalid_args" in raw_err or "invalid tool call" in raw_err.lower():
                    attempt = "1"
                    status_code = "invalid_args"
                    clean_err = "Invalid tool call arguments"
                elif "model output error" in raw_err.lower():
                    attempt = "1"
                    status_code = "empty_output"
                    clean_err = "Empty model output"
                else:
                    attempt = "1"
                    status_code = "error"
                    clean_err = "Execution error"

                emit_step_log(
                    make_log_record(
                        ts_ns,
                        "agy.api_error",
                        {
                            **base_attrs,
                            "model": current_model,
                            "status_code": status_code,
                            "error": clean_err,
                            "attempt": attempt,
                            "duration_ms": f"{dur_ms:.1f}",
                        },
                    )
                )

            # Permission decisions
            if perm_blob:
                pf = parse_wire(perm_blob)
                for p_item_raw in pf.get(2, []):
                    p_item = parse_wire(p_item_raw)
                    accepted_flag = p_item.get(2, [0])[0] == 1
                    rule_type = p_item.get(3, [0])[0]
                    scope = p_item.get(5, [b"tool"])[0].decode("utf-8", errors="replace")
                    if "user denied permission" in err_msg.lower():
                        decision = "reject"
                        perm_source = "user"
                    elif not accepted_flag and status == 7:
                        decision = "reject"
                        perm_source = "config" if "hardcoded" in err_msg.lower() else "user"
                    else:
                        decision = "accept"
                        perm_source = "config" if rule_type > 0 else "user_prompt"

                    # User prompt wait time counts towards user active time (capped at 60s to exclude away-from-keyboard time)
                    if perm_source.startswith("user") and t_wait_start > 0 and t_exec_start > t_wait_start:
                        wait_s = min(60.0, (t_exec_start - t_wait_start) / 1e9)
                        if wait_s > 0.1:
                            add_step_metric_fn("agy.active_time.total", ts_ns, wait_s, {**base_attrs, "type": "user"})

                    emit_step_log(
                        make_log_record(
                            ts_ns,
                            "agy.tool_decision",
                            {
                                **base_attrs,
                                "tool_name": tname or scope,
                                "scope": scope,
                                "source": perm_source,
                                "decision": decision,
                            },
                        )
                    )
            elif "permission denied" in err_msg.lower() or "user denied permission" in err_msg.lower():
                perm_source = "user" if "user denied" in err_msg.lower() else "config"
                emit_step_log(
                    make_log_record(
                        ts_ns,
                        "agy.tool_decision",
                        {
                            **base_attrs,
                            "tool_name": tname or "unknown",
                            "scope": tname or "tool",
                            "source": perm_source,
                            "decision": "reject",
                        },
                    )
                )

            # Tool execution results
            if tname:
                if dur_ms > 0:
                    add_step_metric_fn("agy.active_time.total", ts_ns, dur_ms / 1000.0, {**base_attrs, "type": "cli"})

                exit_code: int | None = None
                if t_content:
                    m_exit = EXIT_CODE_RE.search(t_content)
                    if m_exit:
                        exit_code = int(m_exit.group(1))

                success = (status == 3 and stype != 17) and not err_msg and (exit_code is None or exit_code == 0)
                error_type = "" if success else classify_error_type(err_msg or t_content, exit_code)

                mcp_server_name = ""
                mcp_tool_name = ""
                logged_tool_name = tname
                if tname == "call_mcp_tool":
                    logged_tool_name = "mcp_tool"
                    mcp_server_name = str(args_dict.get("ServerName") or "")
                    mcp_tool_name = str(args_dict.get("ToolName") or "")
                elif tname.startswith("mcp_"):
                    parts = tname.split("_", 2)
                    if len(parts) == 3:
                        logged_tool_name = "mcp_tool"
                        mcp_server_name = parts[1]
                        mcp_tool_name = parts[2]

                emit_step_log(
                    make_log_record(
                        ts_ns,
                        "agy.tool_result",
                        {
                            **base_attrs,
                            "tool_name": logged_tool_name,
                            "mcp_server_name": mcp_server_name,
                            "mcp_tool_name": mcp_tool_name,
                            "duration_ms": f"{dur_ms:.1f}",
                            "success": "true" if success else "false",
                            "error_type": error_type,
                        },
                    )
                )

                # Skill activation detection
                if tname in ("read_file", "view_file"):
                    path_arg = str(args_dict.get("Path") or args_dict.get("AbsolutePath") or "")
                    sm = SKILL_PATH_RE.search(path_arg)
                    if sm and success:
                        skill_name = sm.group(1)
                        skill_source = classify_skill_source(path_arg)
                        trigger = (
                            "user-slash"
                            if (last_slash_skill and last_slash_skill in skill_name)
                            else "agy-proactive"
                        )
                        emit_step_log(
                            make_log_record(
                                ts_ns,
                                "agy.skill_activated",
                                {
                                    **base_attrs,
                                    "skill.name": skill_name,
                                    "skill.source": skill_source,
                                    "invocation_trigger": trigger,
                                },
                            )
                        )

                # Subagent invocation detection
                if tname == "invoke_subagent":
                    subagents = args_dict.get("Subagents")
                    items = subagents if isinstance(subagents, list) else [args_dict]
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        emit_step_log(
                            make_log_record(
                                ts_ns,
                                "agy.subagent_invoked",
                                {
                                    **base_attrs,
                                    "agent.type": str(item.get("TypeName") or item.get("AgentName") or "self"),
                                    "agent.role": str(item.get("Role") or "subagent"),
                                    "model": str(item.get("Model") or "inherit"),
                                    "workspace_mode": str(item.get("Workspace") or "inherit"),
                                    "success": "true" if success else "false",
                                    "error_type": error_type,
                                },
                            )
                        )

                # Lines of code, commits, pull requests
                if success:
                    if tname in ("write_file", "write_to_file"):
                        content_val = str(args_dict.get("Content") or args_dict.get("CodeContent") or "")
                        target_path = str(args_dict.get("Path") or args_dict.get("TargetFile") or "")
                        if content_val and "/.gemini/" not in target_path:
                            added = len(content_val.splitlines())
                            if added > 0:
                                add_step_metric_fn("agy.lines_of_code.count", ts_ns, float(added), {**base_attrs, "type": "added"})
                    elif tname in ("edit_file", "replace_file_content", "multi_replace_file_content"):
                        chunks = args_dict.get("ReplacementChunks")
                        if isinstance(chunks, list):
                            added = sum(len(str(c.get("ReplacementContent") or "").splitlines()) for c in chunks if isinstance(c, dict))
                            removed = sum(len(str(c.get("TargetContent") or "").splitlines()) for c in chunks if isinstance(c, dict))
                        else:
                            added = len(str(args_dict.get("Replacement") or args_dict.get("ReplacementContent") or "").splitlines())
                            removed = len(str(args_dict.get("Target") or args_dict.get("TargetContent") or "").splitlines())
                        if added > 0:
                            add_step_metric_fn("agy.lines_of_code.count", ts_ns, float(added), {**base_attrs, "type": "added"})
                        if removed > 0:
                            add_step_metric_fn("agy.lines_of_code.count", ts_ns, float(removed), {**base_attrs, "type": "removed"})
                    elif tname == "run_command":
                        cmd_line = str(args_dict.get("CommandLine") or "")
                        if re.search(r"\bgit\s+commit\b", cmd_line):
                            add_step_metric_fn("agy.commit.count", ts_ns, 1.0, base_attrs)
                        if re.search(r"\bgh\s+pr\s+create\b", cmd_line):
                            add_step_metric_fn("agy.pull_request.count", ts_ns, 1.0, base_attrs)

            last_metric_step_idx = max(last_metric_step_idx, idx)
            last_log_step_idx = max(last_log_step_idx, idx)

        conv_state["last_metric_step_idx"] = last_metric_step_idx
        conv_state["last_log_step_idx"] = last_log_step_idx
        conv_state["last_step_idx"] = min(last_metric_step_idx, last_log_step_idx)
        conv_state["pending_metric_steps"] = sorted(new_pending_metric_steps)[-50:]
        conv_state["pending_log_steps"] = sorted(new_pending_log_steps)[-50:]
        conv_state["pending_steps"] = sorted(new_pending_metric_steps | new_pending_log_steps)[-50:]

    finally:
        conn.close()
        cleanup_sqlite_copy(dst)

    return gen_metrics, step_metrics, gen_logs + step_logs, conv_state, has_in_flight


def export_conversation_batch(
    metrics_by_name: dict[str, list[dict[str, Any]]],
    logs: list[dict[str, Any]],
) -> tuple[bool, bool]:
    """Post metrics and logs for a single conversation, returning (metrics_ok, logs_ok)."""
    resource_attrs = otlp_kv({"service.name": "agy"})
    metrics_ok = True
    logs_ok = True

    if metrics_by_name:
        metrics_list = [make_sum_metric(name, dps) for name, dps in metrics_by_name.items() if dps]
        if metrics_list:
            metrics_payload = {
                "resourceMetrics": [
                    {
                        "resource": {"attributes": resource_attrs},
                        "scopeMetrics": [
                            {
                                "scope": {"name": "agy-exporter"},
                                "metrics": metrics_list,
                            }
                        ],
                    }
                ]
            }
            metrics_ok = post_otlp("/v1/metrics", metrics_payload)

    if logs:
        logs_payload = {
            "resourceLogs": [
                {
                    "resource": {"attributes": resource_attrs},
                    "scopeLogs": [
                        {
                            "scope": {"name": "agy-exporter"},
                            "logRecords": logs,
                        }
                    ],
                }
            ]
        }
        logs_ok = post_otlp("/v1/logs", logs_payload)

    return metrics_ok, logs_ok


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"conversations": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def merge_metric_dicts(*dicts: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    merged: dict[str, list[dict[str, Any]]] = {}
    for d in dicts:
        for k, v in d.items():
            if v:
                merged.setdefault(k, []).extend(v)
    return merged


def sync_once(state: dict[str, Any], tmp_dir: Path, persist: bool = True) -> bool:
    """Process all conversations with per-conversation isolation and per-conversation state commits."""
    surfaces = [
        ("cli", GEMINI_DIR / "antigravity-cli"),
        ("app", GEMINI_DIR / "antigravity"),
    ]
    conv_states: dict[str, Any] = state.setdefault("conversations", {})
    any_exported = False
    total_dps = 0
    total_logs = 0

    for surface_name, surface_dir in surfaces:
        conv_dir = surface_dir / "conversations"
        if not conv_dir.exists():
            continue
        summaries = SUMMARY_CACHE.get(surface_dir, tmp_dir)
        for db_path in sorted(conv_dir.glob("*.db")):
            cid = db_path.stem
            try:
                wal_path = Path(str(db_path) + "-wal")
                mtime = max(
                    db_path.stat().st_mtime_ns,
                    wal_path.stat().st_mtime_ns if wal_path.exists() else 0,
                )
                prev_state = conv_states.get(cid, {})
                if prev_state.get("mtime_ns") == mtime and not prev_state.get("has_in_flight"):
                    continue

                gen_m, step_m, logs, candidate_state, has_in_flight = process_conversation_db(
                    surface_name,
                    surface_dir,
                    db_path,
                    summaries.get(cid, {}),
                    dict(prev_state),
                    tmp_dir,
                )
                combined_metrics = merge_metric_dicts(gen_m, step_m)
                dps_count = sum(len(v) for v in combined_metrics.values())

                if dps_count == 0 and not logs:
                    # Nothing new to export; record mtime so we don't re-read until modified
                    candidate_state["mtime_ns"] = mtime
                    candidate_state["has_in_flight"] = has_in_flight
                    if conv_states.get(cid) != candidate_state:
                        conv_states[cid] = candidate_state
                        if persist:
                            save_state(state)
                    continue

                metrics_ok, logs_ok = export_conversation_batch(combined_metrics, logs)
                next_state = dict(prev_state)
                next_state["active_skill"] = candidate_state.get("active_skill", "")
                next_state["current_model"] = candidate_state.get("current_model", "auto")

                if metrics_ok:
                    next_state["session_counted"] = candidate_state.get("session_counted", False)
                    next_state["last_metric_gen_idx"] = candidate_state["last_metric_gen_idx"]
                    next_state["last_metric_step_idx"] = candidate_state["last_metric_step_idx"]
                    next_state["pending_metric_steps"] = candidate_state["pending_metric_steps"]
                    if dps_count > 0:
                        any_exported = True
                        total_dps += dps_count

                if logs_ok:
                    next_state["last_log_gen_idx"] = candidate_state["last_log_gen_idx"]
                    next_state["last_log_step_idx"] = candidate_state["last_log_step_idx"]
                    next_state["pending_log_steps"] = candidate_state["pending_log_steps"]
                    if logs:
                        any_exported = True
                        total_logs += len(logs)

                next_state["last_gen_idx"] = min(
                    int(next_state.get("last_metric_gen_idx", -1)),
                    int(next_state.get("last_log_gen_idx", -1)),
                )
                next_state["last_step_idx"] = min(
                    int(next_state.get("last_metric_step_idx", -1)),
                    int(next_state.get("last_log_step_idx", -1)),
                )
                next_state["pending_steps"] = sorted(
                    set(next_state.get("pending_metric_steps", []))
                    | set(next_state.get("pending_log_steps", []))
                )

                if metrics_ok and logs_ok:
                    next_state["mtime_ns"] = mtime
                    next_state["has_in_flight"] = has_in_flight
                else:
                    print(
                        f"[agy-exporter] Partial/failed OTLP export for {cid} "
                        f"(metrics_ok={metrics_ok}, logs_ok={logs_ok}); committed succeeded stream cursor",
                        flush=True,
                    )

                conv_states[cid] = next_state
                if persist:
                    save_state(state)
            except Exception as exc:
                print(
                    f"[agy-exporter] Skipping conversation {cid} due to error: {exc}\n"
                    f"{traceback.format_exc()}",
                    flush=True,
                )
                continue

    if any_exported:
        print(
            f"[agy-exporter] Exported {total_dps} metric points and {total_logs} log events",
            flush=True,
        )
    return any_exported


def main() -> None:
    print(f"[agy-exporter] Watching {GEMINI_DIR} -> {OTLP_HTTP_ENDPOINT}", flush=True)
    state = load_state()
    with tempfile.TemporaryDirectory(prefix="agy-db-") as tmp_str:
        tmp_dir = Path(tmp_str)
        while True:
            try:
                sync_once(state, tmp_dir, persist=True)
            except Exception as exc:
                print(f"[agy-exporter] Unexpected loop error: {exc}", flush=True)
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
