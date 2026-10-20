# llm-telemetry-stack

A local stack for watching your own Claude Code usage: cost, tokens, sessions, tools and errors, on a Grafana dashboard. Everything runs on your machine.

```text
Claude Code ──OTLP──> OpenTelemetry Collector ──> ClickHouse ──> Grafana
```

## What you get

The **Claude Code usage** dashboard shows:

- Cost, tokens, cache hit ratio, sessions, active time and lines of code for the selected range
- Cost by model and tokens by type over time
- Cost by repository, by source (main loop, subagents, skills, MCP servers) and the most expensive sessions
- Tool calls with failure rate and latency, permission decisions, API errors and API latency
- Commits and pull requests made through Claude Code

## Quick start

Requires Docker with the `docker compose` plugin.

```bash
docker compose up -d
```

Open <http://localhost:3000>. There is no login, because every port is bound to `127.0.0.1`.

## Point Claude Code at it

Merge the `env` block from [`claude-code/settings.json`](claude-code/settings.json) into `~/.claude/settings.json`. With `jq`, when the file already exists:

```bash
jq -s '.[0] * .[1]' ~/.claude/settings.json claude-code/settings.json > /tmp/claude-settings.json \
  && mv /tmp/claude-settings.json ~/.claude/settings.json
```

Then start a new Claude Code session. Claude Code reads these settings at startup, so sessions that are already running don't report. Metrics arrive every 60 seconds and events every 5 seconds.

## Privacy

- **Nothing leaves your machine.** Claude Code sends telemetry only to the endpoint you configure, here the local collector.
- **Prompts and responses are not recorded.** Claude Code redacts them by default; only their length is kept.
- **Your email and account IDs are recorded** as attributes on every data point, as Claude Code sends them.

These optional settings add more detail. Add them to the same `env` block if you want them:

| Setting | Adds |
| --- | --- |
| `OTEL_LOG_TOOL_DETAILS=1` | Real names for custom skills, agents and MCP tools, plus Bash commands and file paths |
| `OTEL_LOG_USER_PROMPTS=1` | Prompt text, and response text unless `OTEL_LOG_ASSISTANT_RESPONSES=0` |

The full list of what Claude Code exports is in the [monitoring docs](https://code.claude.com/docs/en/monitoring-usage).

## About the cost figures

Cost is what the tokens would cost at API list prices. On a Pro, Max, Team or Enterprise subscription it is not a bill, but it is still useful for comparing models, repositories and days. Check your plan limits with `/usage` in Claude Code.

## Querying the data directly

ClickHouse's SQL console is at <http://localhost:8123/play> (user `otel`, password `otel`).

- **`otel.otel_metrics_sum`** holds the metrics. Claude Code sends deltas, so `sum(Value)` over any time range is the total for that range.
- **`otel.otel_logs`** holds the events. The event name is in `LogAttributes['event.name']`.

```sql
SELECT toDate(TimeUnix) AS day, Attributes['model'] AS model, round(sum(Value), 2) AS cost_usd
FROM otel.otel_metrics_sum
WHERE MetricName = 'claude_code.cost.usage'
GROUP BY day, model
ORDER BY day DESC, cost_usd DESC
```

## Configuration

To change credentials or ports, set these in a `.env` file next to `docker-compose.yml`:

| Variable | Default |
| --- | --- |
| `CLICKHOUSE_USER` | `otel` |
| `CLICKHOUSE_PASSWORD` | `otel` |
| `GRAFANA_PORT` | `3000` |
| `CLICKHOUSE_HTTP_PORT` | `8123` |
| `OTLP_GRPC_PORT` | `4317` |
| `OTLP_HTTP_PORT` | `4318` |

If you change `OTLP_GRPC_PORT`, change the port in `OTEL_EXPORTER_OTLP_ENDPOINT` too. Data is kept for one year (`ttl` in [`collector/config.yaml`](collector/config.yaml)).

## Stopping and removing

```bash
docker compose down     # stop, keep the data
docker compose down -v  # stop and delete all data
```

## Troubleshooting

- **No data on the dashboard:** start a new Claude Code session after changing the settings, wait a minute, then check `docker compose logs otel-collector`.
- **Docker installed as a snap (Ubuntu):** keep this repo outside hidden folders and `/tmp`. Snap Docker cannot see them, so the config files mount as empty folders.
- **A port is already in use:** set a different one in `.env`.
- **Managed settings:** if your organisation sets `OTEL_EXPORTER_OTLP_ENDPOINT` in Claude Code managed settings, it replaces your local endpoint.
