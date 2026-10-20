# llm-telemetry-stack

Docker Compose stack that stores Claude Code OpenTelemetry data in ClickHouse and shows it in Grafana. Public repo: keep it generic, with no employer-specific names, endpoints or settings.

`README.md` is for users: setup, running, what the dashboard shows. Keep it short; internals and gotchas go here.

## Data shape

- Claude Code exports metrics with delta temporality (`AggregationTemporality = 1` in `otel.otel_metrics_sum`), so `sum(Value)` per bucket is the usage in that bucket. Don't use `max` or rate functions.
- Event names live in `LogAttributes['event.name']`. The exporter's `EventName` column stays empty for Claude Code events (collector 0.161.0, Claude Code 2.1.303).
- Standard attributes (`session.id`, `user.*`, `vcs.*`) sit in each row's `Attributes` / `LogAttributes` map, not in `ResourceAttributes`.
- `skill_activated` events carry `skill.name`, `skill.source` and `invocation_trigger` (`user-slash`, `claude-proactive`, `nested-skill`). Cost and token counters carry `skill.name` for the skill active during the request.

## Tool details filter

- The snippet sets `OTEL_LOG_TOOL_DETAILS=1` so skill, agent and MCP names arrive unredacted. Without it, user-defined skills show as `custom_skill`.
- The same flag adds Bash commands, file paths and arguments. `transform/drop-tool-arguments` in `collector/config.yaml` deletes `tool_parameters`, `tool_input` and `tool_result`'s `error` before export. It covers logs only; the snippet doesn't export traces, whose spans would carry `file_path` and `full_command`.
- Verify after changing the filter: run the test session below with `OTEL_LOG_TOOL_DETAILS=1` and a Bash `echo` of a unique string, then confirm the string appears nowhere in `otel.otel_logs` (`Body` or `mapValues(LogAttributes)`).

## Access

- Grafana queries ClickHouse as `grafana`, defined in `clickhouse/users.d/grafana.xml` with only `SELECT ON otel.*`. A panel reading anything outside `otel` fails with `ACCESS_DENIED`.
- ClickHouse has no host port. Run SQL through `docker compose exec clickhouse clickhouse-client`. Don't publish `8123` or `9000` with the default passwords: any website can send requests to `localhost`, and `otel` can write data out with `INSERT INTO FUNCTION url(...)`.
- `GF_SERVER_ENFORCE_DOMAIN` redirects any host other than `localhost` to `GF_SERVER_ROOT_URL`, which blocks DNS rebinding. Opening `127.0.0.1:3000` redirects to `localhost:3000`.
- Grafana 13 ignores `GF_AUTH_ANONYMOUS_ORG_ROLE` and logs "only viewer role is supported", so anonymous visitors are always Viewers.

## Changing the dashboard

- `grafana/dashboards/claude-code.json` is provisioned read-only. Edit the file, then `docker compose restart grafana`.
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
