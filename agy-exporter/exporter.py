#!/usr/bin/env python3
"""Antigravity (agy) OpenTelemetry Exporter Sidecar.

Reads local Antigravity conversation databases and transcripts from mounted
subtrees (antigravity-cli and antigravity desktop) and exports OpenTelemetry
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
    - field 20 (repeated KeyValue): metadata map (`last_step_index`, `request_id`, `root_cascade_id`, `cascade_id`)
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

import contextlib
import hashlib
import http.client
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
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
# A step still in flight is exported as abandoned only after its conversation has gone this long
# without a write. A running tool call or subagent does not write to its parent's database, so this
# must outlast the longest one you expect.
ABANDON_IDLE_SEC = float(os.environ.get("ABANDON_IDLE_SEC", str(6 * 3600)))
MAX_BATCH_BYTES = 4 * 1024 * 1024
# A connection dropped while sending a body this large is taken as the collector refusing its size.
CONNECTION_DROP_SPLIT_BYTES = 256 * 1024

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
# "...from Gemini 3.6 Flash (High) to Gemini 3.8 Flash (High). No need to comment on this change if the user ...":
# model names contain dots, so the name ends at a dot followed by whitespace. Matched only inside the settings
# block, so a request quoting this sentence doesn't change the model.
MODEL_CHANGE_RE = re.compile(r"<USER_SETTINGS_CHANGE>[\s\S]*?changed setting `Model Selection` from .*? to (.+?)\.(?:\s|$)")
# Case-sensitive uppercase group for status identifier so lowercase prose (e.g. 'overloaded') never matches group 2
API_ERROR_RE = re.compile(
    r"[Aa][Pp][Ii]\s+[Ee]rror(?:\s*\(attempt\s*(\d+)\))?:\s*([A-Z][A-Z0-9_]+)(?:\s*\(code\s*(\d+)\))?(?::\s*(.*))?"
)
API_ATTEMPT_RE = re.compile(r"[Aa][Pp][Ii]\s+[Ee]rror(?:\s*\(attempt\s*(\d+)\))?")


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
    if not isinstance(buf, (bytes, bytearray, memoryview)) or not buf:
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
            val = bytes(buf[i : i + ln])
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


def wire_bytes(fields: dict[int, list[Any]], fn: int) -> bytes:
    """Return the first length-delimited bytes value for field `fn`, or b'' if absent or wrong wire type."""
    for v in fields.get(fn, ()):
        if isinstance(v, bytes):
            return v
    return b""


def wire_bytes_list(fields: dict[int, list[Any]], fn: int) -> list[bytes]:
    """Return all length-delimited bytes values for field `fn`."""
    return [v for v in fields.get(fn, ()) if isinstance(v, bytes)]


def wire_str(fields: dict[int, list[Any]], fn: int) -> str:
    """Return the first length-delimited string value for field `fn`, or ''."""
    b = wire_bytes(fields, fn)
    return b.decode("utf-8", errors="replace") if b else ""


def wire_int(fields: dict[int, list[Any]], fn: int, default: int = 0) -> int:
    """Return the first integer varint value for field `fn`, or `default`."""
    for v in fields.get(fn, ()):
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return default


def wire_msg(fields: dict[int, list[Any]], fn: int) -> dict[int, list[Any]]:
    """Decode the first embedded message at field `fn`."""
    return parse_wire(wire_bytes(fields, fn))


def wire_msg_list(fields: dict[int, list[Any]], fn: int) -> list[dict[int, list[Any]]]:
    """Decode all repeated embedded messages at field `fn`."""
    return [parse_wire(b) for b in wire_bytes_list(fields, fn)]


def parse_packed_varints(buf: bytes) -> list[int]:
    if not isinstance(buf, (bytes, bytearray)) or not buf:
        return []
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


def proto_ts_ns(msg: dict[int, list[Any]]) -> int:
    sec = wire_int(msg, 1, 0)
    nanos = wire_int(msg, 2, 0)
    if sec <= 0:
        return 0
    return sec * 1_000_000_000 + max(0, nanos)


def proto_duration_ms(msg: dict[int, list[Any]]) -> float:
    sec = wire_int(msg, 1, 0)
    nanos = wire_int(msg, 2, 0)
    return sec * 1000.0 + nanos / 1_000_000.0


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


def extract_activated_skill(tname: str, args_dict: dict[str, Any], success: bool) -> tuple[str, str] | None:
    """Single source of truth for skill activation detection across cost tagging and log events."""
    if not success or tname not in ("read_file", "view_file"):
        return None
    path_arg = str(args_dict.get("Path") or args_dict.get("AbsolutePath") or "")
    if not path_arg:
        return None
    sm = SKILL_PATH_RE.search(path_arg)
    if not sm:
        return None
    return sm.group(1), classify_skill_source(path_arg)


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


def classify_api_error(raw: str) -> dict[str, str]:
    """Classify an Antigravity API/model error string into {attempt, status_code, error}."""
    m_att = API_ATTEMPT_RE.search(raw)
    attempt = (m_att.group(1) if m_att and m_att.group(1) else None) or "1"

    m_api = API_ERROR_RE.search(raw)
    if m_api:
        grpc_status = m_api.group(2)
        http_code = m_api.group(3)
        return {
            "attempt": m_api.group(1) or attempt,
            "status_code": http_code or grpc_status,
            "error": grpc_status,
        }

    low = raw.lower()
    if "timed out" in low or "timeout" in low or "deadline exceeded" in low:
        return {"attempt": attempt, "status_code": "timeout", "error": "Timeout"}
    if "invalid_args" in low or "invalid tool call" in low or "problem parsing the tool call" in low:
        return {"attempt": attempt, "status_code": "invalid_args", "error": "Invalid tool call arguments"}
    if "model output error" in low or "output text or tool calls" in low:
        return {"attempt": attempt, "status_code": "empty_output", "error": "Empty model output"}
    if "context canceled" in low or "cancelled" in low or "canceled" in low:
        return {"attempt": attempt, "status_code": "canceled", "error": "Canceled"}
    if m_att:
        m_code = re.search(r"\(code\s*(\d+)\)", raw)
        return {
            "attempt": attempt,
            "status_code": m_code.group(1) if m_code else "500",
            "error": "APIError",
        }
    return {"attempt": attempt, "status_code": "error", "error": "Execution error"}


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


_LAST_POST_ERROR_LOG: dict[tuple[str, str], float] = {}


def _log_post_error(path: str, kind: str, message: str) -> None:
    """Print a POST failure at most once a minute per (path, kind), so a dead endpoint doesn't flood the log."""
    now = time.monotonic()
    if now - _LAST_POST_ERROR_LOG.get((path, kind), float("-inf")) >= 60:
        _LAST_POST_ERROR_LOG[(path, kind)] = now
        print(f"[agy-exporter] {message}", flush=True)


def post_otlp(path: str, payload: dict[str, Any]) -> str:
    """Send an OTLP/HTTP JSON payload and classify the outcome.

    Returns "ok"; "retry" (keep the data and try again next poll); "bad_request"
    (HTTP 400: this payload will never be accepted); or "too_large" (HTTP 413, or the
    connection dropped while sending a large body: split it and send the halves).
    Only a 400 says anything about the data. Auth, routing and availability errors
    (401, 403, 404, 408, 429, 5xx) are "retry", so a misconfigured endpoint never causes
    data to be discarded. Per the OTLP spec, 2xx with `partialSuccess` means the
    collector kept the rest of the batch and the client must not retry it, so it is "ok".
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OTLP_HTTP_ENDPOINT}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw_body = resp.read()
            if raw_body:
                try:
                    body_json = json.loads(raw_body.decode("utf-8", errors="replace"))
                    if isinstance(body_json, dict):
                        ps = body_json.get("partialSuccess")
                        if isinstance(ps, dict):
                            rejected_pts = int(ps.get("rejectedDataPoints") or 0)
                            rejected_logs = int(ps.get("rejectedLogRecords") or 0)
                            err_msg = str(ps.get("errorMessage") or "").strip()
                            if rejected_pts > 0 or rejected_logs > 0 or err_msg:
                                print(
                                    f"[agy-exporter] OTLP partialSuccess on {path}: "
                                    f"rejectedDataPoints={rejected_pts}, rejectedLogRecords={rejected_logs}, "
                                    f"errorMessage={err_msg!r}",
                                    flush=True,
                                )
                except Exception as parse_exc:
                    print(f"[agy-exporter] Malformed OTLP 2xx response on {path}: {parse_exc}", flush=True)
            return "ok"
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        kind = {400: "bad_request", 413: "too_large"}.get(exc.code, "retry")
        _log_post_error(path, f"http-{exc.code}", f"HTTP {exc.code} POST {path} ({kind}): {err_body or exc.reason}")
        return kind
    except Exception as exc:
        reason = getattr(exc, "reason", exc)  # URLError wraps the socket error raised while sending
        dropped = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, http.client.RemoteDisconnected)
        kind = "too_large" if isinstance(reason, dropped) and len(data) > CONNECTION_DROP_SPLIT_BYTES else "retry"
        _log_post_error(path, type(reason).__name__, f"Failed POST {path} ({kind}, {len(data)} bytes): {exc}")
        return kind


def snapshot_sqlite_to_memory(db_path: Path, tmp_dir: Path) -> sqlite3.Connection:
    """Atomically snapshot a SQLite database into an in-memory connection using sqlite3.backup().

    Tries `mode=ro` first (which atomically reads active WAL + SHM pages via SQLite locks),
    falls back to `immutable=1` when no `-wal` exists on a read-only directory, and falls back
    to a temporary file copy only if an uncheckpointed `-wal` exists without `-shm` on a `:ro` mount.
    """
    dst = sqlite3.connect(":memory:")
    wal_path = Path(str(db_path) + "-wal")
    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            src.backup(dst)
            return dst
        finally:
            src.close()
    except sqlite3.Error:
        if not wal_path.exists():
            try:
                src = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
                try:
                    src.backup(dst)
                    return dst
                finally:
                    src.close()
            except Exception:
                dst.close()
                raise

    # Fallback for crashed WAL without -shm on a read-only directory
    tmp_db = tmp_dir / f"snap_{os.getpid()}_{db_path.name}"
    tmp_wal = Path(str(tmp_db) + "-wal")
    try:
        shutil.copy2(db_path, tmp_db)
        if wal_path.exists():
            shutil.copy2(wal_path, tmp_wal)
        src = sqlite3.connect(str(tmp_db))
        try:
            src.backup(dst)
            return dst
        finally:
            src.close()
    except Exception:
        dst.close()
        raise
    finally:
        for p in (tmp_db, tmp_wal, Path(str(tmp_db) + "-shm")):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass


class SummaryCache:
    """Caches conversation_summaries.db per surface if present, reloading only when mtime changes."""

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

        summaries: dict[str, dict[str, Any]] = {}
        try:
            conn = snapshot_sqlite_to_memory(db_file, tmp_dir)
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
        return self._data.get(key, {})


SUMMARY_CACHE = SummaryCache()
_WARNED_MISSING_SURFACES: set[str] = set()


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
    """Extract new generation and step telemetry for a single conversation."""
    cid = db_path.stem
    wal_file = Path(str(db_path) + "-wal")
    conv_mtime_ns = max(db_path.stat().st_mtime_ns, wal_file.stat().st_mtime_ns if wal_file.exists() else 0)
    gen_metrics: dict[str, list[dict[str, Any]]] = {}
    step_metrics: dict[str, list[dict[str, Any]]] = {}
    gen_logs: list[dict[str, Any]] = []
    step_logs: list[dict[str, Any]] = []
    has_in_flight = False

    def add_gen_metric(name: str, ts_ns: int, val: float, attrs: dict[str, Any]) -> None:
        gen_metrics.setdefault(name, []).append(make_dp(ts_ns, val, attrs))

    def add_step_metric(name: str, ts_ns: int, val: float, attrs: dict[str, Any]) -> None:
        step_metrics.setdefault(name, []).append(make_dp(ts_ns, val, attrs))

    conn = snapshot_sqlite_to_memory(db_path, tmp_dir)
    try:
        # 1. Extract repository and branch from trajectory_metadata_blob
        repo = summary_info.get("repository", "")
        branch = ""
        traj_created_ns = 0
        own_agent_name = ""
        row = conn.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1").fetchone()
        if row and row[0]:
            tm = parse_wire(row[0])
            traj_created_ns = proto_ts_ns(wire_msg(tm, 2))
            # A subagent's trajectory carries its agent definition (field 4) and invocation (field 8),
            # both with the agent name at field 1. Read here, it is visible as soon as the conversation
            # exists, unlike conversation_summaries.db, whose new rows can sit in an unmounted -wal.
            own_agent_name = wire_str(wire_msg(tm, 4), 1) or wire_str(wire_msg(tm, 8), 1)
            ws_info = wire_msg(tm, 1)
            git_info = wire_msg(ws_info, 3)
            git_repo = wire_str(git_info, 1)
            if git_repo:
                repo = git_repo
            elif not repo:
                ws_uri = wire_str(ws_info, 1).replace("file://", "").rstrip("/")
                if ws_uri:
                    repo = os.path.basename(ws_uri)
            branch = wire_str(ws_info, 4)

        has_parent = False
        try:
            p_cnt = conn.execute("SELECT count() FROM parent_references").fetchone()[0]
            has_parent = bool(p_cnt and p_cnt > 0)
        except sqlite3.Error:
            pass

        nesting_depth = int(summary_info.get("nesting_depth", 0) or (1 if has_parent else 0))
        query_source = "subagent" if nesting_depth > 0 else "main"
        agent_name = own_agent_name or str(summary_info.get("agent_name", ""))

        base_attrs = {
            "session.id": cid,
            "surface": surface,
            "vcs.repository.name": repo,
            "vcs.branch.name": branch,
            "query_source": query_source,
            "agent.name": agent_name,
        }

        last_metric_gen_idx = int(conv_state.get("last_metric_gen_idx", -1))
        last_log_gen_idx = int(conv_state.get("last_log_gen_idx", -1))
        pending_metric_gens: set[int] = {int(x) for x in conv_state.get("pending_metric_gens", [])}
        pending_log_gens: set[int] = {int(x) for x in conv_state.get("pending_log_gens", [])}

        last_metric_step_idx = int(conv_state.get("last_metric_step_idx", -1))
        last_log_step_idx = int(conv_state.get("last_log_step_idx", -1))
        pending_metric_steps: set[int] = {int(x) for x in conv_state.get("pending_metric_steps", [])}
        pending_log_steps: set[int] = {int(x) for x in conv_state.get("pending_log_steps", [])}

        min_gen_idx = min([last_metric_gen_idx, last_log_gen_idx] + [g - 1 for g in (pending_metric_gens | pending_log_gens)])
        min_step_idx = min(last_metric_step_idx, last_log_step_idx)
        all_pending_steps = pending_metric_steps | pending_log_steps
        all_pending_gens = pending_metric_gens | pending_log_gens

        max_db_gen = conn.execute("SELECT coalesce(max(idx), -1) FROM gen_metadata").fetchone()[0]
        max_db_step = conn.execute("SELECT coalesce(max(idx), -1) FROM steps").fetchone()[0]
        if max_db_gen <= min_gen_idx and max_db_step <= min_step_idx and not all_pending_steps and not all_pending_gens:
            return gen_metrics, step_metrics, [], conv_state, False

        # Load steps and parse wire metadata once per step
        step_rows = conn.execute(
            "SELECT idx, step_type, status, metadata, permissions, error_details FROM steps ORDER BY idx ASC"
        ).fetchall()

        min_needed_step = min([min_step_idx + 1] + list(all_pending_steps)) if all_pending_steps else (min_step_idx + 1)
        transcript_steps = load_transcript_map(surface_dir, cid, min_step_idx=max(0, min_needed_step))

        parsed_steps: list[
            tuple[
                int,
                int,
                int,
                dict[int, list[Any]],
                bytes | None,
                bytes | None,
                str,
                dict[str, Any],
                str,
                str,
                bool,
                int | None,
                tuple[str, str] | None,
            ]
        ] = []
        step_ts_map: dict[int, int] = {}
        step_status_map: dict[int, int] = {}
        active_skill_by_step: dict[int, str] = {}
        active_mcp_by_step: dict[int, str] = {}
        slash_by_step: dict[int, str] = {}
        model_by_step: dict[int, str] = {}
        query_source_by_step: dict[int, str] = {}

        current_skill = ""
        current_model = "auto"
        current_turn_slash = ""

        for idx, stype, status, meta_blob, perm_blob, err_blob in step_rows:
            m = parse_wire(meta_blob)
            t_created = proto_ts_ns(wire_msg(m, 1))
            if t_created > 0:
                step_ts_map[idx] = t_created
            step_status_map[idx] = status

            t_step = transcript_steps.get(idx, {})
            t_content = str(t_step.get("content") or "")
            t_err = str(t_step.get("error") or "")

            # A new user turn: a skill read in an earlier turn no longer drives what the model does.
            if stype == 14:
                current_skill = ""
                current_turn_slash = ""
                if t_content:
                    m_change = MODEL_CHANGE_RE.search(t_content)
                    if m_change:
                        current_model = m_change.group(1).strip()
                    m_req = re.search(r"<USER_REQUEST>\s*/([a-zA-Z0-9_-]+)", t_content)
                    if m_req:
                        current_turn_slash = m_req.group(1).strip()

            tc = wire_msg(m, 4)
            tname = wire_str(tc, 2)
            targs_raw = wire_str(tc, 3)
            args_dict: dict[str, Any] = {}
            if targs_raw:
                try:
                    parsed = json.loads(targs_raw)
                    if isinstance(parsed, dict):
                        args_dict = parsed
                except Exception:
                    pass

            err_msg = ""
            if err_blob:
                ef = parse_wire(err_blob)
                err_msg = first_line(wire_str(ef, 1) or wire_str(ef, 2))
            if not err_msg and t_err:
                err_msg = first_line(t_err)

            exit_code: int | None = None
            if t_content:
                m_exit = EXIT_CODE_RE.search(t_content)
                if m_exit:
                    exit_code = int(m_exit.group(1))

            success = (status == 3 and stype not in (17, 23)) and not err_msg and (exit_code is None or exit_code == 0)
            activated_skill = extract_activated_skill(tname, args_dict, success)
            if activated_skill is not None:
                current_skill = activated_skill[0]

            if tname == "call_mcp_tool":
                srv = str(args_dict.get("ServerName") or "")
                if srv:
                    active_mcp_by_step[idx] = srv
            elif tname.startswith("mcp_"):
                parts = tname.split("_")
                if len(parts) >= 3:
                    active_mcp_by_step[idx] = parts[1]

            active_skill_by_step[idx] = current_skill
            slash_by_step[idx] = current_turn_slash
            model_by_step[idx] = current_model
            parsed_steps.append(
                (
                    idx,
                    stype,
                    status,
                    m,
                    perm_blob,
                    err_blob,
                    tname,
                    args_dict,
                    t_content,
                    err_msg,
                    success,
                    exit_code,
                    activated_skill,
                )
            )

        now_ns = int(time.time() * 1e9)
        first_ts = step_ts_map.get(0) or step_ts_map.get(1) or traj_created_ns or conv_mtime_ns
        # Only a conversation that has stopped being written to can have abandoned steps. A step's age
        # says nothing: a five-minute build in a live session is still running.
        session_idle = (now_ns - conv_mtime_ns) / 1e9 >= ABANDON_IDLE_SEC

        # Emit session count once per conversation
        if not conv_state.get("session_counted"):
            add_gen_metric("agy.session.count", first_ts, 1.0, base_attrs)
            conv_state["session_counted"] = True

        # 2. Process gen_metadata (API requests, tokens, cost, latency, TTFT)
        gen_rows = conn.execute(
            "SELECT idx, data FROM gen_metadata WHERE idx > ? ORDER BY idx ASC", (min_gen_idx,)
        ).fetchall()

        new_pending_metric_gens: set[int] = set()
        new_pending_log_gens: set[int] = set()

        for g_idx, g_data in gen_rows:
            need_gen_metric = (g_idx > last_metric_gen_idx) or (g_idx in pending_metric_gens)
            need_gen_log = (g_idx > last_log_gen_idx) or (g_idx in pending_log_gens)
            if not need_gen_metric and not need_gen_log:
                continue

            f = parse_wire(g_data)
            gen_msgs = wire_msg_list(f, 1)
            gen_kvs = [{wire_str(x, 1): wire_str(x, 2) for x in wire_msg_list(gm, 20)} for gm in gen_msgs]
            step_indices = parse_packed_varints(wire_bytes(f, 2))
            first_step_idx: int | None = step_indices[0] if step_indices else None
            # Without step indices, the planner step follows `last_step_index`. Resolve it before the
            # in-flight check, which would otherwise never see this generation's planner step.
            if first_step_idx is None:
                kv = next((kv for kv in gen_kvs if "last_step_index" in kv), None)
                if kv is not None:
                    try:
                        first_step_idx = int(kv["last_step_index"]) + 1
                    except ValueError:
                        first_step_idx = 0

            # Defer a generation whose planner step is still streaming, so its usage is read once complete.
            # Never `break`: later generations must not wait behind this one.
            if first_step_idx is not None and step_status_map.get(first_step_idx) in (0, 1, 2) and not session_idle:
                has_in_flight = True
                if need_gen_metric:
                    new_pending_metric_gens.add(g_idx)
                if need_gen_log:
                    new_pending_log_gens.add(g_idx)
                last_metric_gen_idx = max(last_metric_gen_idx, g_idx)
                last_log_gen_idx = max(last_log_gen_idx, g_idx)
                continue

            for gm, kv in zip(gen_msgs, gen_kvs):
                resolved_step_idx = first_step_idx if first_step_idx is not None else 0

                model_display = wire_str(gm, 21)
                model_slug = wire_str(gm, 19)
                model_name = model_display or model_slug or model_by_step.get(resolved_step_idx, "auto") or "unknown"

                # Propagate generation's model to its step range for accurate step/error attribution
                for s_i in step_indices or [resolved_step_idx]:
                    model_by_step[s_i] = model_name

                root_cid = kv.get("root_cascade_id", "")
                casc_id = kv.get("cascade_id", "")
                turn_source = "subagent" if (root_cid and casc_id and root_cid != casc_id) else query_source
                for s_i in step_indices or [resolved_step_idx]:
                    query_source_by_step[s_i] = turn_source

                usage = wire_msg(gm, 4)
                in_tok = wire_int(usage, 2, 0)
                out_tok = wire_int(usage, 3, 0)
                cache_tok = wire_int(usage, 5, 0)
                think_tok = wire_int(usage, 9, 0)

                dur_ms = proto_duration_ms(wire_msg(gm, 11))
                ttft_ms = proto_duration_ms(wire_msg(gm, 12))

                ts_ns = step_ts_map.get(resolved_step_idx) or traj_created_ns or conv_mtime_ns
                skill_for_turn = active_skill_by_step.get(resolved_step_idx, "")
                mcp_for_turn = ""
                for s_i in step_indices:
                    if s_i in active_mcp_by_step:
                        mcp_for_turn = active_mcp_by_step[s_i]
                        break

                turn_attrs = {
                    **base_attrs,
                    "query_source": turn_source,
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
                        add_gen_metric("agy.active_time.total", ts_ns, dur_ms / 1000.0, {**turn_attrs, "type": "cli"})

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
        conv_state["pending_metric_gens"] = sorted(new_pending_metric_gens)
        conv_state["pending_log_gens"] = sorted(new_pending_log_gens)

        # 3. Process steps (Tools, Skills, Permissions, Subagents, LOC/Commits/PRs, Errors)
        new_pending_metric_steps: set[int] = set()
        new_pending_log_steps: set[int] = set()

        last_known_model = "auto"
        last_known_query_source = query_source
        for idx, stype, status, m, perm_blob, err_blob, tname, args_dict, t_content, err_msg, success, exit_code, activated_skill in parsed_steps:
            step_model = model_by_step.get(idx, last_known_model)
            if step_model and step_model != "auto":
                last_known_model = step_model
            else:
                step_model = last_known_model

            if stype == 14 and idx not in query_source_by_step:
                last_known_query_source = query_source
            step_query_source = query_source_by_step.get(idx, last_known_query_source)
            last_known_query_source = step_query_source
            step_attrs = {**base_attrs, "query_source": step_query_source}

            t_created = proto_ts_ns(wire_msg(m, 1))

            need_step_metric = (idx > last_metric_step_idx) or (idx in pending_metric_steps)
            need_step_log = (idx > last_log_step_idx) or (idx in pending_log_steps)
            if not need_step_metric and not need_step_log:
                continue

            t_started = proto_ts_ns(wire_msg(m, 6)) or t_created
            t_completed = proto_ts_ns(wire_msg(m, 8)) or t_started

            # Keep an in-flight step pending until it finishes, or until its conversation has been idle for
            # ABANDON_IDLE_SEC; only then does it fall through and get exported as abandoned.
            if status in (0, 1, 2, 9) and not session_idle:
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
            for s26 in wire_msg_list(m, 26):
                for it in wire_msg_list(s26, 1):
                    st_code = wire_int(it, 1, 0)
                    st_ts = proto_ts_ns(wire_msg(it, 2))
                    if st_ts <= 0:
                        continue
                    if st_code == 9 and t_wait_start == 0:
                        t_wait_start = st_ts
                    elif st_code == 2:
                        t_exec_start = st_ts
                    elif st_code in (3, 6, 7):
                        t_completed = st_ts

            ts_ns = t_created or traj_created_ns or conv_mtime_ns
            # Guard against zero t_exec_start producing a ~56-year duration when only t_completed is set
            if t_exec_start > 0 and t_completed >= t_exec_start:
                dur_ms = (t_completed - t_exec_start) / 1_000_000.0
            else:
                dur_ms = 0.0

            t_step = transcript_steps.get(idx, {})
            t_type = str(t_step.get("type") or "")

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
                classified = classify_api_error(raw_err)

                emit_step_log(
                    make_log_record(
                        ts_ns,
                        "agy.api_error",
                        {
                            **step_attrs,
                            "model": step_model,
                            "status_code": classified["status_code"],
                            "error": classified["error"],
                            "attempt": classified["attempt"],
                            "duration_ms": f"{dur_ms:.1f}",
                        },
                    )
                )

            # Permission decisions
            if perm_blob:
                pf = parse_wire(perm_blob)
                user_prompted_in_step = False
                scopes: list[str] = []
                decisions: list[str] = []
                sources: list[str] = []
                for p_item in wire_msg_list(pf, 2):
                    accepted_flag = wire_int(p_item, 2, 0) == 1
                    rule_type = wire_int(p_item, 3, 0)
                    scope = wire_str(p_item, 5) or "tool"
                    if status == 9:
                        # Still waiting for an answer when the session went idle: nobody decided.
                        decision = "unanswered"
                        perm_source = "user"
                    elif "user denied permission" in err_msg.lower():
                        decision = "reject"
                        perm_source = "user"
                    elif not accepted_flag and status == 7:
                        decision = "reject"
                        perm_source = "config" if "hardcoded" in err_msg.lower() else "user"
                    else:
                        decision = "accept"
                        perm_source = "config" if rule_type > 0 else "user_prompt"

                    if perm_source.startswith("user") and decision != "unanswered":
                        user_prompted_in_step = True

                    scopes.append(scope)
                    decisions.append(decision)
                    sources.append(perm_source)

                if scopes:
                    step_decision = next((d for d in ("reject", "unanswered") if d in decisions), "accept")
                    if "user" in sources:
                        step_source = "user"
                    elif "user_prompt" in sources:
                        step_source = "user_prompt"
                    else:
                        step_source = "config"
                    joined_scope = ",".join(dict.fromkeys(scopes))
                    emit_step_log(
                        make_log_record(
                            ts_ns,
                            "agy.tool_decision",
                            {
                                **step_attrs,
                                "tool_name": tname or joined_scope,
                                "scope": joined_scope,
                                "source": step_source,
                                "decision": step_decision,
                            },
                        )
                    )

                # Emit user prompt wait time at most once per step (never multiplied by N permission items)
                if user_prompted_in_step and t_wait_start > 0 and t_exec_start > t_wait_start:
                    wait_s = min(60.0, (t_exec_start - t_wait_start) / 1e9)
                    if wait_s > 0.1:
                        add_step_metric_fn("agy.active_time.total", ts_ns, wait_s, {**step_attrs, "type": "user"})
            elif "permission denied" in err_msg.lower() or "user denied permission" in err_msg.lower():
                perm_source = "user" if "user denied" in err_msg.lower() else "config"
                emit_step_log(
                    make_log_record(
                        ts_ns,
                        "agy.tool_decision",
                        {
                            **step_attrs,
                            "tool_name": tname or "unknown",
                            "scope": tname or "tool",
                            "source": perm_source,
                            "decision": "reject",
                        },
                    )
                )

            # Tool execution results. A step reaching here still in flight was abandoned; only steps that
            # carry a tool call produce a tool_result (a planner step stuck mid-stream is not a failed tool).
            is_abandoned = status in (0, 1, 2, 9)
            if tname:
                if dur_ms > 0:
                    add_step_metric_fn("agy.active_time.total", ts_ns, dur_ms / 1000.0, {**step_attrs, "type": "cli"})

                if is_abandoned:
                    error_type = "abandoned"
                else:
                    error_type = "" if success else classify_error_type(err_msg or t_content, exit_code)

                mcp_server_name = ""
                mcp_tool_name = ""
                logged_tool_name = tname or "unknown"
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
                            **step_attrs,
                            "tool_name": logged_tool_name,
                            "mcp_server_name": mcp_server_name,
                            "mcp_tool_name": mcp_tool_name,
                            "duration_ms": f"{dur_ms:.1f}",
                            "success": "true" if success else "false",
                            "error_type": error_type,
                        },
                    )
                )

                # Skill activation event using the exact same `activated_skill` tuple as pass 1
                if activated_skill is not None:
                    skill_name, skill_source = activated_skill
                    step_slash = slash_by_step.get(idx, "")
                    trigger = (
                        "user-slash"
                        if (step_slash and step_slash in skill_name)
                        else "agy-proactive"
                    )
                    emit_step_log(
                        make_log_record(
                            ts_ns,
                            "agy.skill_activated",
                            {
                                **step_attrs,
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
                                    **step_attrs,
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
                                add_step_metric_fn("agy.lines_of_code.count", ts_ns, float(added), {**step_attrs, "type": "added"})
                    elif tname in ("edit_file", "replace_file_content", "multi_replace_file_content"):
                        chunks = args_dict.get("ReplacementChunks")
                        if isinstance(chunks, list):
                            added = sum(len(str(c.get("ReplacementContent") or "").splitlines()) for c in chunks if isinstance(c, dict))
                            removed = sum(len(str(c.get("TargetContent") or "").splitlines()) for c in chunks if isinstance(c, dict))
                        else:
                            added = len(str(args_dict.get("Replacement") or args_dict.get("ReplacementContent") or "").splitlines())
                            removed = len(str(args_dict.get("Target") or args_dict.get("TargetContent") or "").splitlines())
                        if added > 0:
                            add_step_metric_fn("agy.lines_of_code.count", ts_ns, float(added), {**step_attrs, "type": "added"})
                        if removed > 0:
                            add_step_metric_fn("agy.lines_of_code.count", ts_ns, float(removed), {**step_attrs, "type": "removed"})
                    elif tname == "run_command":
                        cmd_line = str(args_dict.get("CommandLine") or "")
                        if re.search(r"\bgit\s+commit\b", cmd_line):
                            add_step_metric_fn("agy.commit.count", ts_ns, 1.0, step_attrs)
                        if re.search(r"\bgh\s+pr\s+create\b", cmd_line):
                            add_step_metric_fn("agy.pull_request.count", ts_ns, 1.0, step_attrs)

            last_metric_step_idx = max(last_metric_step_idx, idx)
            last_log_step_idx = max(last_log_step_idx, idx)

        conv_state["last_metric_step_idx"] = last_metric_step_idx
        conv_state["last_log_step_idx"] = last_log_step_idx
        conv_state["pending_metric_steps"] = sorted(new_pending_metric_steps)
        conv_state["pending_log_steps"] = sorted(new_pending_log_steps)

    finally:
        conn.close()

    return gen_metrics, step_metrics, gen_logs + step_logs, conv_state, has_in_flight


def build_metrics_payload(metrics_by_name: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    metrics_list = [make_sum_metric(name, dps) for name, dps in metrics_by_name.items() if dps]
    if not metrics_list:
        return None
    resource_attrs = otlp_kv({"service.name": "agy"})
    return {
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


def build_logs_payload(logs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not logs:
        return None
    resource_attrs = otlp_kv({"service.name": "agy"})
    return {
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



class StateLoadError(Exception):
    """The state file exists but can't be read, so the exporter can't tell what it has already sent."""


def load_state() -> dict[str, Any]:
    """Read the saved cursors. A missing file means a first start.

    A file that can't be read is fatal: starting over from empty state would export every
    conversation again and duplicate all of it in ClickHouse.
    """
    if not STATE_FILE.exists():
        return {"conversations": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError(f"expected a JSON object, found {type(state).__name__}")
        convs = state.get("conversations")
        if isinstance(convs, dict):
            for c in convs.values():
                if not isinstance(c, dict):
                    continue
                if "last_gen_idx" in c:
                    legacy_gen = int(c.pop("last_gen_idx"))
                    c.setdefault("last_metric_gen_idx", legacy_gen)
                    c.setdefault("last_log_gen_idx", legacy_gen)
                if "last_step_idx" in c:
                    legacy_step = int(c.pop("last_step_idx"))
                    c.setdefault("last_metric_step_idx", legacy_step)
                    c.setdefault("last_log_step_idx", legacy_step)
                if "pending_steps" in c:
                    legacy_pending = [int(x) for x in c.pop("pending_steps")]
                    c.setdefault("pending_metric_steps", list(legacy_pending))
                    c.setdefault("pending_log_steps", list(legacy_pending))
        return state
    except (OSError, ValueError, TypeError) as exc:
        raise StateLoadError(
            f"cannot read {STATE_FILE}: {exc}. Starting from empty state would export every "
            "conversation again; see 'Troubleshooting' in the README"
        ) from exc


class StatePersistError(Exception):
    """The cursor state cannot be saved, so nothing more may be exported until it can."""


# Set when the collector accepted data whose cursors could only be kept in memory. The next cycle
# saves the state before anything else, and exports nothing until that succeeds.
_STATE_UNSAVED = False


def ensure_state_dir_writable() -> None:
    """Fail before any POST unless the state directory takes a write of realistic size.

    A zero-byte probe succeeds on a full disk, so this writes 64 KiB and fsyncs it.
    """
    state_dir = STATE_FILE.parent
    probe = state_dir / f".write_probe_{os.getpid()}"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        try:
            with probe.open("wb") as f:
                f.write(b"\0" * 65536)
                f.flush()
                os.fsync(f.fileno())
        finally:
            with contextlib.suppress(OSError):
                probe.unlink()
    except OSError as exc:
        raise StatePersistError(f"state directory {state_dir} is not writable by uid={os.getuid()}: {exc}") from exc


def save_state(state: dict[str, Any]) -> None:
    """Replace the state file atomically and durably.

    Without the fsync, a crash or power loss soon after the rename can leave an empty state file on
    file systems that don't flush a renamed file by themselves, such as XFS.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(state, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)
    # Persist the rename too. Where a directory can't be fsynced, a crash can at worst bring back the
    # previous state file, which is intact.
    with contextlib.suppress(OSError):
        dir_fd = os.open(STATE_FILE.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def persist_state(state: dict[str, Any]) -> None:
    """Save the state; on failure, remember that memory is ahead of disk and raise StatePersistError."""
    global _STATE_UNSAVED
    try:
        save_state(state)
    except OSError as exc:
        _STATE_UNSAVED = True
        raise StatePersistError(f"cannot write {STATE_FILE} as uid={os.getuid()}: {exc}") from exc
    _STATE_UNSAVED = False


def merge_metric_dicts(*dicts: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    merged: dict[str, list[dict[str, Any]]] = {}
    for d in dicts:
        for k, v in d.items():
            if v:
                merged.setdefault(k, []).extend(v)
    return merged


def record_fingerprint(stream: str, name: str, record: dict[str, Any]) -> str:
    """Stable identity of one data point or log record, so a retry never sends it twice."""
    blob = json.dumps([stream, name, record], sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:24]


def _export_item(cid: str, stream: str, name: str, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "cid": cid,
        "name": name,
        "record": record,
        "fp": record_fingerprint(stream, name, record),
        "size": len(json.dumps(record)) + len(name) + 32,
    }


def _chunk_by_bytes(items: list[dict[str, Any]], budget: int) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    cur_bytes = 0
    for it in items:
        if cur and cur_bytes + it["size"] > budget:
            chunks.append(cur)
            cur, cur_bytes = [], 0
        cur.append(it)
        cur_bytes += it["size"]
    if cur:
        chunks.append(cur)
    return chunks


def _stream_payload(stream: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if stream == "m":
        by_name: dict[str, list[dict[str, Any]]] = {}
        for it in items:
            by_name.setdefault(it["name"], []).append(it["record"])
        return build_metrics_payload(by_name)
    return build_logs_payload([it["record"] for it in items])


def deliver_stream(stream: str, items: list[dict[str, Any]], settle: Any) -> bool:
    """Send items in requests of at most MAX_BATCH_BYTES and settle each with the collector.

    A request the collector refuses as bad (400) or too large (413) is split in half and both halves
    are sent, down to single records; a single record still refused is settled as dropped.
    `settle(stream, items, accepted)` runs after every request the collector answers for good.
    Returns False when a retryable failure stops delivery; whatever was settled stays settled.
    """
    path = "/v1/metrics" if stream == "m" else "/v1/logs"

    def send(chunk: list[dict[str, Any]]) -> bool:
        payload = _stream_payload(stream, chunk)
        if payload is None:
            return True
        outcome = post_otlp(path, payload)
        if outcome == "ok":
            settle(stream, chunk, True)
            return True
        if outcome == "retry":
            return False
        if len(chunk) == 1:
            settle(stream, chunk, False)
            return True
        mid = len(chunk) // 2
        return send(chunk[:mid]) and send(chunk[mid:])

    return all(send(chunk) for chunk in _chunk_by_bytes(items, MAX_BATCH_BYTES))


_CURSOR_KEYS = {
    "m": ("session_counted", "last_metric_gen_idx", "last_metric_step_idx", "pending_metric_steps", "pending_metric_gens"),
    "l": ("last_log_gen_idx", "last_log_step_idx", "pending_log_steps", "pending_log_gens"),
}
# Written by earlier versions and no longer read.
_RETIRED_KEYS = ("export_rejected_mtime_ns", "summary_grace_waited", "last_gen_idx", "last_step_idx", "pending_steps")


def sync_once(state: dict[str, Any], tmp_dir: Path, persist: bool = True) -> bool:
    """Export the new telemetry of every changed conversation.

    Each conversation is read in isolation. Its records are delivered in size-bounded requests, and its
    cursors for a stream (metrics or logs) advance only once every record of that stream is settled:
    accepted, or refused by the collector as bad data. Until then the conversation keeps a ledger of
    the settled records ("settled"), saved after every accepted request, so a retry after any failure,
    a crash included, sends only what the collector has not answered for good.
    """
    if persist:
        if _STATE_UNSAVED:
            persist_state(state)  # memory is ahead of disk: catch up before sending anything new
        ensure_state_dir_writable()

    surfaces = [
        ("cli", GEMINI_DIR / "antigravity-cli"),
        ("app", GEMINI_DIR / "antigravity"),
    ]
    conv_states: dict[str, Any] = state.setdefault("conversations", {})
    staged: list[dict[str, Any]] = []
    state_dirty = False
    now_ns = time.time_ns()

    for surface_name, surface_dir in surfaces:
        conv_dir = surface_dir / "conversations"
        if not conv_dir.exists():
            if surface_name not in _WARNED_MISSING_SURFACES:
                _WARNED_MISSING_SURFACES.add(surface_name)
                print(
                    f"[agy-exporter] Surface '{surface_name}' directory not found at {conv_dir}; skipping",
                    flush=True,
                )
            continue
        summaries = SUMMARY_CACHE.get(surface_dir, tmp_dir)
        for db_path in sorted(conv_dir.glob("*.db")):
            cid = db_path.stem
            prev_state = dict(conv_states.get(cid, {}))
            try:
                wal_path = Path(str(db_path) + "-wal")
                mtime = max(
                    db_path.stat().st_mtime_ns,
                    wal_path.stat().st_mtime_ns if wal_path.exists() else 0,
                )
            except OSError:
                continue

            backoff_rem = int(prev_state.get("backoff_polls_remaining", 0))
            if backoff_rem > 0:
                prev_state["backoff_polls_remaining"] = backoff_rem - 1
                conv_states[cid] = prev_state
                state_dirty = True
                continue

            if prev_state.get("failed_mtime_ns") == mtime:
                continue
            # Unchanged since the last full export: skip, unless steps were in flight and the conversation
            # has now been idle long enough to export them as abandoned.
            if prev_state.get("mtime_ns") == mtime and not (
                prev_state.get("has_in_flight") and now_ns >= int(prev_state.get("abandon_check_ns", 0))
            ):
                continue

            try:
                gen_m, step_m, logs, candidate_state, has_in_flight = process_conversation_db(
                    surface_name,
                    surface_dir,
                    db_path,
                    summaries.get(cid, {}),
                    dict(prev_state),
                    tmp_dir,
                )
            except Exception as exc:
                fail_count = int(prev_state.get("fail_count", 0)) + 1
                backoff_polls = min(60, 2 ** fail_count)
                if fail_count == 1:
                    print(
                        f"[agy-exporter] Skipping conversation {cid} (mtime={mtime}, fail_count={fail_count}, "
                        f"backoff_polls={backoff_polls}) due to error: {exc}\n{traceback.format_exc()}",
                        flush=True,
                    )
                else:
                    print(
                        f"[agy-exporter] Skipping conversation {cid} (mtime={mtime}, fail_count={fail_count}, "
                        f"backoff_polls={backoff_polls}) due to error: {exc}",
                        flush=True,
                    )
                prev_state["failed_mtime_ns"] = mtime
                prev_state["fail_count"] = fail_count
                prev_state["backoff_polls_remaining"] = backoff_polls
                conv_states[cid] = prev_state
                state_dirty = True
                continue

            items = {
                "m": [
                    _export_item(cid, "m", m_name, dp)
                    for m_name, dps in merge_metric_dicts(gen_m, step_m).items()
                    for dp in dps
                ],
                "l": [_export_item(cid, "l", "", rec) for rec in logs],
            }
            staged.append(
                {
                    "cid": cid,
                    "mtime": mtime,
                    "has_in_flight": has_in_flight,
                    "items": items,
                    "prev_state": prev_state,
                    "candidate_state": candidate_state,
                }
            )

    # Working state per staged conversation: committed cursors plus a private copy of its ledger.
    work: dict[str, dict[str, Any]] = {}
    for item in staged:
        st = dict(item["prev_state"])
        st["settled"] = {s: list(v) for s, v in item["prev_state"].get("settled", {}).items()}
        work[item["cid"]] = st

    sent = {"m": 0, "l": 0}
    refused: dict[tuple[str, str], int] = {}

    def settle(stream: str, chunk: list[dict[str, Any]], accepted: bool) -> None:
        nonlocal state_dirty
        for it in chunk:
            work[it["cid"]]["settled"].setdefault(stream, []).append(it["fp"])
            conv_states[it["cid"]] = work[it["cid"]]
            if not accepted:
                refused[(it["cid"], stream)] = refused.get((it["cid"], stream), 0) + 1
        state_dirty = True
        if accepted:
            sent[stream] += len(chunk)
            if persist:
                # Record what the collector now holds before sending anything else.
                persist_state(state)
                state_dirty = False

    pending = {"m": [], "l": []}
    for item in staged:
        for s in ("m", "l"):
            ledger = set(work[item["cid"]]["settled"].get(s, []))
            pending[s].extend(x for x in item["items"][s] if x["fp"] not in ledger)
    delivered = {s: deliver_stream(s, pending[s], settle) for s in ("m", "l")}

    for (cid, s), n in sorted(refused.items()):
        kind = "data point(s)" if s == "m" else "log record(s)"
        print(f"[agy-exporter] Collector refused {n} {kind} from conversation {cid} as invalid; dropped them", flush=True)
    if not all(delivered.values()):
        print(
            f"[agy-exporter] Partial/failed OTLP export (metrics delivered={delivered['m']}, "
            f"logs delivered={delivered['l']}); the rest is retried next poll",
            flush=True,
        )

    for item in staged:
        cid, st, cand = item["cid"], work[item["cid"]], item["candidate_state"]
        settled = st["settled"]
        done = {}
        for s in ("m", "l"):
            ledger = set(settled.get(s, []))
            done[s] = all(x["fp"] in ledger for x in item["items"][s])
            if done[s]:
                for key in _CURSOR_KEYS[s]:
                    if key in cand:
                        st[key] = cand[key]
                settled.pop(s, None)
        if not settled:
            st.pop("settled")
        if done["m"] and done["l"]:
            for key in ("failed_mtime_ns", "fail_count", "backoff_polls_remaining") + _RETIRED_KEYS:
                st.pop(key, None)
            st["mtime_ns"] = item["mtime"]
            st["has_in_flight"] = item["has_in_flight"]
            if item["has_in_flight"]:
                st["abandon_check_ns"] = item["mtime"] + int(ABANDON_IDLE_SEC * 1e9)
            else:
                st.pop("abandon_check_ns", None)
        if conv_states.get(cid) != st:
            conv_states[cid] = st
            state_dirty = True

    if state_dirty and persist:
        persist_state(state)

    if sent["m"] or sent["l"]:
        print(f"[agy-exporter] Exported {sent['m']} metric points and {sent['l']} log events", flush=True)
    return bool(sent["m"] or sent["l"])


def main() -> None:
    print(f"[agy-exporter] Watching {GEMINI_DIR} -> {OTLP_HTTP_ENDPOINT}", flush=True)
    try:
        ensure_state_dir_writable()
        state = load_state()
    except (StatePersistError, StateLoadError) as exc:
        # Nothing has been sent yet, so exiting loses nothing, and the restart loop makes it visible.
        print(f"[agy-exporter] FATAL: {exc}", flush=True)
        sys.exit(1)
    last_paused_log = float("-inf")
    with tempfile.TemporaryDirectory(prefix="agy-db-") as tmp_str:
        tmp_dir = Path(tmp_str)
        while True:
            try:
                sync_once(state, tmp_dir, persist=True)
            except StatePersistError as exc:
                # Never exit here: the cursors in memory are the only record of what the collector has
                # already accepted, and restarting from the last saved state would send it again.
                if time.monotonic() - last_paused_log >= 60:
                    last_paused_log = time.monotonic()
                    print(f"[agy-exporter] Export paused until state can be saved: {exc}", flush=True)
            except Exception as exc:
                print(f"[agy-exporter] Unexpected loop error: {exc}", flush=True)
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
