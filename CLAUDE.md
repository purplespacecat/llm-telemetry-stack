# llm-telemetry-stack

Docker Compose stack that stores Claude Code and Antigravity (`agy`) OpenTelemetry data in ClickHouse and shows it in Grafana. Public repo: keep it generic, with no employer-specific names, endpoints or settings.

`README.md` is for users: setup, running, what the dashboard shows. Keep it short; internals and gotchas go here.

## Data shape (Claude Code)

- Claude Code exports metrics with delta temporality (`AggregationTemporality = 1` in `otel.otel_metrics_sum`), so `sum(Value)` per bucket is the usage in that bucket. Don't use `max` or rate functions.
- Event names live in `LogAttributes['event.name']`. The exporter's `EventName` column stays empty for Claude Code events (collector 0.161.0, Claude Code 2.1.303).
- Standard attributes (`session.id`, `user.*`, `vcs.*`) sit in each row's `Attributes` / `LogAttributes` map, not in `ResourceAttributes`.
- `skill_activated` events carry `skill.name`, `skill.source` and `invocation_trigger` (`user-slash`, `claude-proactive`, `nested-skill`). Cost and token counters carry `skill.name` for the skill active during the request.
- MCP calls always arrive as `tool_name = 'mcp_tool'`, even with `OTEL_LOG_TOOL_DETAILS=1`. The server and tool names exist only inside `tool_parameters`.
- Failed `tool_result` events carry `error_type` (such as `ShellError`) with or without the flag.
- `hook_execution_complete` is per hook event and matcher (`hook_name` such as `PreToolUse:Bash`), not per hook command. `hook_registered` lists each configured hook with `hook_source` and `plugin.name`.
- `mcp_server_connection` rows with `status = 'disconnected'` carry the connection lifetime in `duration_ms`, not the connect time.

## Data shape (Antigravity / `agy`)

- Antigravity does not export OTLP natively. `agy-exporter` reads its local SQLite stores and sends `agy.*` metrics and log events with `ResourceAttributes['service.name'] = 'agy'`. It runs under the `agy` Compose profile as `${AGY_UID}:${AGY_GID}` (default `1000:1000`). The image creates `/state` with mode 0777, so a fresh state volume is writable by whichever uid that is.
- Each Antigravity surface is an opt-in Compose override listed in `COMPOSE_FILE`: `docker-compose.agy-cli.yml` for `~/.gemini/antigravity-cli` and `docker-compose.agy-desktop.yml` for `~/.gemini/antigravity`. The base file mounts neither, so a machine with only one of the apps can start. Each override mounts exactly what the exporter reads, read-only: `conversations/`, `brain/` and `conversation_summaries.db`.
- Don't mount a whole surface directory: it holds `antigravity-oauth-token`, `settings.json` and `history.jsonl`. Don't mount `-wal` or `-shm` files one by one either. SQLite deletes and recreates them, and Docker turns a missing short-syntax source into a root-owned directory inside `~/.gemini`. The overrides use long syntax with `bind.create_host_path: false`, so a missing path fails `compose up` instead.
- Per-conversation databases (`conversations/<id>.db`) are snapshotted into `:memory:` with `sqlite3.Connection.backup()`, opened `mode=ro` with an `immutable=1` fallback. This reads committed rows that exist only in a live `-wal` behind the read-only mount. Typed wire accessors (`wire_str`, `wire_int`, `wire_msg`, `wire_msg_list`) decode the Cortex protobuf blobs in `gen_metadata`, `steps` and `trajectory_metadata_blob`; the field numbers are in the `exporter.py` docstring.
- `agent.name` comes from the conversation's own `trajectory_metadata_blob`. A subagent's trajectory carries its agent definition (field 4) and its invocation (field 8), each with the name at field 1. `conversation_summaries.db` is only the fallback: mounted as a single file it is read without its `-wal`, so rows for new conversations can stay invisible until Antigravity checkpoints.
- `transcript.jsonl` supplements `steps` with command exit codes (`Command exited with code N`), per-step slash commands and `<USER_SETTINGS_CHANGE>` model changes.
- Skill activations use one `extract_activated_skill()` guard for both cost and token attribution and `agy.skill_activated`. `agy.tool_decision` is one record per step: Antigravity raises a permission request per sub-command of a compound command, and those requests share every attribute.
- A step with status 0, 1, 2 or 9 is in flight. It stays in `pending_*_steps` until it finishes, or until its conversation has gone `ABANDON_IDLE_SEC` (env, default 6 h) without a write. A step's age proves nothing, because a running tool call or subagent doesn't write to its parent's database; the window has to outlast the longest one. A conversation with steps in flight is revisited at `abandon_check_ns` even if nothing on disk changes.
- An abandoned step with a tool call becomes `agy.tool_result` with `success="false"` and `error_type="abandoned"`. An unanswered permission prompt (status 9) gets `decision="unanswered"`. An abandoned step without a tool call, such as a planner step cut off mid-stream, produces no `tool_result`.
- Records go out in requests of at most `MAX_BATCH_BYTES`, and `post_otlp()` classifies every answer. Only HTTP 400 means the data is bad. A 400 or 413, or a connection dropped while sending more than `CONNECTION_DROP_SPLIT_BYTES`, splits the request in half and sends both halves, down to single records; a single record that is still refused is dropped and logged. Every other failure (401, 403, 404, 408, 429, 5xx, timeouts) is retried on the next poll and discards nothing. A 2xx with `partialSuccess` is logged and counts as delivered, as the OTLP spec requires.
- A conversation's cursors for a stream (`last_metric_*` and `pending_metric_*`, or `last_log_*` and `pending_log_*`) advance only once every record of that stream is settled. Until then the conversation keeps a ledger of settled record fingerprints (`settled`), saved after every accepted request. A retry after any failure, including a crash, therefore sends only what the collector has not yet answered for good.
- `/state/state.json` (`STATE_FILE`): the exporter exits at startup if the state directory can't take a 64 KiB write. After startup a failed save never exits, because the in-memory cursors are the only record of what the collector has accepted and restarting from the last saved state would send it again. It sends nothing more until a save succeeds. A conversation whose database fails to parse is logged once with a traceback and retried with backoff (`failed_mtime_ns`, `fail_count`, `backoff_polls_remaining`).
- Verify any change to `agy-exporter`, its `Dockerfile` or the compose files with `python3 agy-exporter/smoke/check.py`. It snapshots `~/.gemini` read-only, drives the exporter against a fake OTLP collector through each of the cases above, and exits non-zero if a check fails. Keep test fixtures read-only like the real mount: in a writable directory the exporter's own read creates a `-wal`, which changes the mtime it keys on and hides what it would do in the container.

## Tool details filter

- The snippet sets `OTEL_LOG_TOOL_DETAILS=1` so skill, agent and MCP names arrive unredacted. Without it, user-defined skills show as `custom_skill`.
- The same flag adds Bash commands, file paths and arguments. `transform/drop-tool-arguments` in `collector/config.yaml` copies `mcp_server_name` and `mcp_tool_name` out of `tool_parameters`, then deletes `tool_parameters`, `tool_input` and `tool_result`'s `error` before export. `mcp_server_connection` keeps its `error`, which holds connection messages such as `Connection closed`.
- The filter covers logs only. The snippet doesn't export traces, whose spans would carry `file_path` and `full_command`.
- Verify after changing the filter: run the test session below with `OTEL_LOG_TOOL_DETAILS=1`, a failing Bash `ls` of a unique string and an MCP call, then confirm the string appears nowhere in `otel.otel_logs` (`Body` or `mapValues(LogAttributes)`) and the MCP call has `mcp_server_name`.

## Access

- Grafana queries ClickHouse as `grafana`, defined in `clickhouse/users.d/grafana.xml` with only `SELECT ON otel.*`. A panel reading anything outside `otel` fails with `ACCESS_DENIED`.
- ClickHouse has no host port. Run SQL through `docker compose exec clickhouse clickhouse-client`. Don't publish `8123` or `9000` with the default passwords: any website can send requests to `localhost`, and `otel` can write data out with `INSERT INTO FUNCTION url(...)`.
- `GF_SERVER_ENFORCE_DOMAIN` redirects any host other than `localhost` to `GF_SERVER_ROOT_URL`, which blocks DNS rebinding. Opening `127.0.0.1:3000` redirects to `localhost:3000`.
- Grafana 13 ignores `GF_AUTH_ANONYMOUS_ORG_ROLE` and logs "only viewer role is supported", so anonymous visitors are always Viewers.

## Changing the dashboards

- `grafana/dashboards/{cost,efficiency,reliability}.json` (Claude Code, `service.name = 'claude-code'`, tagged `claude-code`) and `grafana/dashboards/agy-{cost,efficiency,reliability}.json` (Antigravity, `service.name = 'agy'`, tagged `agy`) are provisioned read-only. Edit a file, then `docker compose restart grafana`. The provider sets `disableDeletion: false`, so deleting a file removes its dashboard on restart.
- Put a panel on the dashboard whose question it answers: spend on cost, usage and output on efficiency, errors and failures on reliability.
- `cost.json` is the home page (`GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH`). Keep the `claude-code` tag on the Claude Code dashboards and the `agy` tag on the `agy-*.json` dashboards; the header links find dashboards by those tags.
- Check panel SQL in ClickHouse before reloading. Replace `$__timeFilter(col)` with `col >= now() - INTERVAL 1 DAY` and `$__timeInterval(col)` with `toStartOfInterval(toDateTime(col), INTERVAL 3600 second)`, then run it as Grafana's user with `docker compose exec clickhouse clickhouse-client --user grafana --password grafana -q "<sql>"`.

## Generating test data

Send real telemetry from a one-off headless session without changing your own settings:

```bash
env -u CLAUDECODE CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp \
  OTEL_EXPORTER_OTLP_PROTOCOL=grpc OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
  OTEL_METRIC_EXPORT_INTERVAL=2000 OTEL_LOGS_EXPORT_INTERVAL=1000 \
  claude -p "Read README.md and reply with its first line." --max-turns 3
```

`env -u CLAUDECODE` is needed when running this from inside a Claude Code session.
