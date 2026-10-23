# llm-telemetry-stack

Docker Compose stack that stores Claude Code OpenTelemetry data in ClickHouse and shows it in Grafana. Public repo: keep it generic, with no employer-specific names, endpoints or settings.

`README.md` is for users: setup, running, what the dashboard shows. Keep it short; internals and gotchas go here.

## Data shape

- Claude Code exports metrics with delta temporality (`AggregationTemporality = 1` in `otel.otel_metrics_sum`), so `sum(Value)` per bucket is the usage in that bucket. Don't use `max` or rate functions.
- Event names live in `LogAttributes['event.name']`. The exporter's `EventName` column stays empty for Claude Code events (collector 0.161.0, Claude Code 2.1.303).
- Standard attributes (`session.id`, `user.*`, `vcs.*`) sit in each row's `Attributes` / `LogAttributes` map, not in `ResourceAttributes`.
- `skill_activated` events carry `skill.name`, `skill.source` and `invocation_trigger` (`user-slash`, `claude-proactive`, `nested-skill`). Cost and token counters carry `skill.name` for the skill active during the request.
- MCP calls always arrive as `tool_name = 'mcp_tool'`, even with `OTEL_LOG_TOOL_DETAILS=1`. The server and tool names exist only inside `tool_parameters`.
- Failed `tool_result` events carry `error_type` (such as `ShellError`) with or without the flag.
- `hook_execution_complete` is per hook event and matcher (`hook_name` such as `PreToolUse:Bash`), not per hook command. `hook_registered` lists each configured hook with `hook_source` and `plugin.name`.
- `mcp_server_connection` rows with `status = 'disconnected'` carry the connection lifetime in `duration_ms`, not the connect time.
- Metrics carry a coarse `query_source` (`main`, `subagent`, `auxiliary`). `api_request` events carry the detailed one: `repl_main_thread` (or `sdk` in headless runs), `agent:builtin:<name>`, `agent:custom`, and background requests such as `agent_summary`, `away_summary`, `prompt_suggestion` and `compact`. Panels that need the detail read events.
- A request's context size is `input_tokens + cache_read_tokens + cache_creation_tokens` on `api_request`. The main thread is a `query_source` starting with `repl_main_thread`, or `sdk`.
- `subagent_completed` carries `agent_type`, `final_model`, `duration_ms`, `total_tokens` and `total_tool_uses`. Its `agent_type` matches `agent.name` on the subagent's `api_request` events. `compaction` carries `trigger` (`manual` or `auto`), `pre_tokens` and `post_tokens`.
- Cost is exported per request (`cost_usd`) only, never per token type.

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

- `grafana/dashboards/{cost,efficiency,reliability}.json` are provisioned read-only. Edit a file, then `docker compose restart grafana`. The provider sets `disableDeletion: false`, so deleting a file removes its dashboard on restart.
- Put a panel on the dashboard whose question it answers: spend on cost, usage and output on efficiency, errors and failures on reliability.
- `cost.json` is the home page (`GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH`). Keep the `claude-code` tag on every dashboard; the header links find dashboards by that tag.
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
