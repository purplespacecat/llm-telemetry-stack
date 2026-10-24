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
- Records go out in requests of at most `MAX_BATCH_BYTES`, and `post_otlp()` classifies every answer. Only HTTP 400 means the data is bad. A 400 or 413, or a connection dropped while sending more than `CONNECTION_DROP_SPLIT_BYTES`, splits the request in half and sends both halves, down to single records; a single record that is still refused is dropped and logged. Every other failure (401, 403, 404, 408, 429, 5xx, timeouts) is retried on the next poll and discards nothing. A 2xx with `partialSuccess` is logged and counts as delivered, as the OTLP spec requires. Collector 0.161.0 answers a body over its 20 MiB limit with HTTP 400 `request body too large`, so in this stack an oversized request takes the 400 path.
- A conversation's cursors for a stream (`last_metric_*` and `pending_metric_*`, or `last_log_*` and `pending_log_*`) advance only once every record of that stream is settled. Until then the conversation keeps a ledger of settled record fingerprints (`settled`), saved after every accepted request. A retry after any failure, including a crash, therefore sends only what the collector has not yet answered for good.
- `/state/state.json` (`STATE_FILE`): the exporter exits at startup if the state directory can't take a 64 KiB write. After startup a failed save never exits, because the in-memory cursors are the only record of what the collector has accepted and restarting from the last saved state would send it again. It sends nothing more until a save succeeds. A conversation whose database fails to parse is logged once with a traceback and retried with backoff (`failed_mtime_ns`, `fail_count`, `backoff_polls_remaining`).
- Verify any change to `agy-exporter`, its `Dockerfile` or the compose files with `python3 agy-exporter/smoke/check.py`. It snapshots `~/.gemini` read-only, drives the exporter against a fake OTLP collector through each of the cases above, and exits non-zero if a check fails. Keep test fixtures read-only like the real mount: in a writable directory the exporter's own read creates a `-wal`, which changes the mtime it keys on and hides what it would do in the container.

## Tool details filter

- The snippet sets `OTEL_LOG_TOOL_DETAILS=1` so skill, agent and MCP names arrive unredacted. Without it, user-defined skills show as `custom_skill`.
- The same flag adds Bash commands, file paths and arguments. `transform/drop-tool-arguments` in `collector/config.yaml` copies `mcp_server_name` and `mcp_tool_name` out of `tool_parameters`, then deletes `tool_parameters`, `tool_input` and `tool_result`'s `error` before export. `mcp_server_connection` keeps its `error`, which holds connection messages such as `Connection closed`.
- The filter covers logs only. The snippet doesn't export traces, whose spans would carry `file_path` and `full_command`.
- Verify after changing the filter: run the test session below with `OTEL_LOG_TOOL_DETAILS=1`, a failing Bash `ls` of a unique string and an MCP call, then confirm the string appears nowhere in `otel.otel_logs` (`Body` or `mapValues(LogAttributes)`) and the MCP call has `mcp_server_name`.

## Container stats

- `docker-compose.resources.yml` adds `container-stats`, a second collector (`collector/container-stats.yaml`) that runs only the `docker_stats` receiver and sends OTLP to `otel-collector`. It is a separate service so the Docker socket stays out of the collector that listens on published ports. A read-only bind doesn't limit what the socket can do. The service runs as root, the socket's owner, with `cap_drop: [ALL]`, `no-new-privileges` and a read-only root file system.
- `filter/this-stack` drops every container whose `com.docker.compose.project` label isn't `${COMPOSE_PROJECT_NAME}`, so other containers on the host are never stored.
- Rows have `ServiceName = 'container-stats'`, and `ResourceAttributes['compose.service']` names the container. CPU (`container.cpu.usage.total`, ns), network and block I/O counters go through `cumulative_to_delta`, so each point covers `StartTimeUnix` to `TimeUnix`. The panels compute rates as `sum(Value)` divided by the seconds those points cover, which is right at any bucket size. Memory (`container.memory.usage.total`) stays a cumulative non-monotonic sum, read like a gauge.
- `initial_value: drop` throws away the first point of every series and the point after a counter reset. Without it the first point after the collector starts carries the container's whole lifetime of CPU over a 1-second span (210 s of ClickHouse CPU, charted as 21,000%). A container restart therefore leaves one missing sample. Grafana's stacking counts it as zero, so the rate panels are unstacked lines that bridge the gap with `spanNulls`.
- `container.cpu.utilization` is disabled. Through Podman's one-shot stats it read 294 for a container that `podman stats` showed at 14%.
- Block I/O is reported per device, and a write to an encrypted or LVM volume is counted on the mapper and again on the disk under it. `metrics_transform/collapse-devices` keeps the largest value across devices, and only `read` and `write` are kept.
- At 60 s intervals it stores 6 series per container, about 43,000 rows a day for five containers, under the tables' one-year TTL.

## ClickHouse's own logs

- `clickhouse/config.d/logs.xml` sends the server log to the console at `information` level, so `docker compose logs clickhouse` shows it. The image's default logs at trace level to files inside the container, rotated at 10 × 1 GB; a mostly idle stack wrote 192 MB in 10 hours. Each start logs four `Listen [0.0.0.0]:… Address already in use` warnings: the image's `docker_related_config.xml` listens on both `::` and `0.0.0.0`, and `::` already took the IPv4 ports. They are harmless.
- It removes the `system` log tables nothing here reads: `text_log`, `trace_log`, `metric_log`, `asynchronous_metric_log`, `query_metric_log`, `part_log`, `processors_profile_log` and `background_schedule_pool_log`. `query_log` and `error_log` are kept for 7 days, and `asynchronous_insert_log` keeps the image's 3-day TTL. With the image's defaults, 10 mostly idle hours left 96 MiB of active parts and 235 MiB of merged parts awaiting deletion in `system`, against 0.7 MiB of telemetry. Each table flushes a new part every 7.5 s, and those parts are merged and logged to `part_log` and `text_log`, so most of the volume was ClickHouse logging its own logging. Removing them took an idle ClickHouse from 11% of a CPU core to 6%, and from 221 kB/s of disk writes to 7 kB/s.
- Removing a table from the config stops the writes but keeps the table and its data. Changing a kept table's definition, such as its TTL, renames the old table to `<name>_0`, which has no TTL, and creates a new one. The README's cleanup command drops both kinds. A dropped table leaves the disk 8 minutes later (`database_atomic_delay_before_drop_table_sec`).
- A ClickHouse upgrade can add new log tables with no TTL. After upgrading, list them with `SELECT name FROM system.tables WHERE database = 'system' AND engine LIKE '%MergeTree'`.
- `logs.xml` is a single-file bind mount like `users.d/grafana.xml`, and the logger and system log settings are read at startup. Run `docker compose restart clickhouse` after editing it.

## Access

- Grafana queries ClickHouse as `grafana`, defined in `clickhouse/users.d/grafana.xml` with `SELECT ON otel.*`, `SELECT` on `system.parts` and `system.disks` for the storage panels, and `SHOW TABLES ON system.*` so `system.parts` lists ClickHouse's own tables too. It can't read the contents of any other system table, such as `system.query_log`; a panel that tries fails with `ACCESS_DENIED`.
- ClickHouse reloads `users.d` when it changes, but `users.d/grafana.xml` is a single-file bind mount. An editor that saves by replacing the file leaves the container on the old inode, so run `docker compose restart clickhouse` after editing it.
- ClickHouse has no host port. Run SQL through `docker compose exec clickhouse clickhouse-client`. Don't publish `8123` or `9000` with the default passwords: any website can send requests to `localhost`, and `otel` can write data out with `INSERT INTO FUNCTION url(...)`.
- `GF_SERVER_ENFORCE_DOMAIN` redirects any host other than `localhost` to `GF_SERVER_ROOT_URL`, which blocks DNS rebinding. Opening `127.0.0.1:3000` redirects to `localhost:3000`.
- Grafana 13 ignores `GF_AUTH_ANONYMOUS_ORG_ROLE` and logs "only viewer role is supported", so anonymous visitors are always Viewers.

## Changing the dashboards

- `grafana/dashboards/{cost,efficiency,reliability}.json` (Claude Code, `service.name = 'claude-code'`, tagged `claude-code`) and `grafana/dashboards/agy-{cost,efficiency,reliability}.json` (Antigravity, `service.name = 'agy'`, tagged `agy`) are provisioned read-only. Edit a file, then `docker compose restart grafana`. The provider sets `disableDeletion: false`, so deleting a file removes its dashboard on restart.
- Put a panel on the dashboard whose question it answers: spend on cost, usage and output on efficiency, errors and failures on reliability.
- `grafana/dashboards/stack-resources.json` (tagged `stack`) reads `container-stats` rows and ClickHouse's `system.parts` and `system.disks`. Its stat panels show the state at the end of the time range; the charts cover the range.
- `cost.json` is the home page (`GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH`). Keep the `claude-code` tag on the Claude Code dashboards, the `agy` tag on the `agy-*.json` dashboards and the `stack` tag on `stack-resources.json`; the header links find dashboards by those tags.
- Check panel SQL in ClickHouse before reloading. Replace `$__timeFilter(col)` with `col >= now() - INTERVAL 1 DAY` and `$__timeInterval(col)` with `toStartOfInterval(toDateTime(col), INTERVAL 3600 second)`, then run it as Grafana's user with `docker compose exec clickhouse clickhouse-client --user grafana --password grafana -q "<sql>"`.

## Known issues

- Rootless Podman with SELinux enforcing needs the README workaround. Bind sources under `$HOME` are labelled `user_home_t`, which containers can't read, so every bind mount fails. Rootless Podman also maps container UIDs above 0 into the user's subordinate UID range, so the exporter's default UID 1000 can't read conversation files with mode 0600. Container root maps to the host user. Don't fix this by relabelling (`:z`): on `~/.gemini` it would rewrite the SELinux labels of the user's own files.
- Grafana 13 downloads updates for its bundled data source plugins (Loki, Tempo, Elasticsearch and others) from grafana.com at every start; look for `plugin.backgroundinstaller` in its log. No telemetry leaves the machine, but the README doesn't mention the traffic.

## Generating test data

Send real telemetry from a one-off headless session without changing your own settings:

```bash
env -u CLAUDECODE CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp \
  OTEL_EXPORTER_OTLP_PROTOCOL=grpc OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
  OTEL_METRIC_EXPORT_INTERVAL=2000 OTEL_LOGS_EXPORT_INTERVAL=1000 \
  claude -p "Read README.md and reply with its first line." --max-turns 3
```

`env -u CLAUDECODE` is needed when running this from inside a Claude Code session.
