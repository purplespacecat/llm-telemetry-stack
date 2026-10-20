# llm-telemetry-stack

A local Grafana dashboard for your Claude Code usage: cost, tokens, sessions, tools and errors. Everything runs on your machine.

```text
Claude Code ──OTLP──> OpenTelemetry Collector ──> ClickHouse ──> Grafana
```

## Set up

You need Docker with the `docker compose` plugin.

1. Start the stack:

   ```bash
   docker compose up -d
   ```

2. Turn on telemetry by merging [`claude-code/settings.json`](claude-code/settings.json) into `~/.claude/settings.json`:

   ```bash
   jq -s '.[0] * .[1]' ~/.claude/settings.json claude-code/settings.json > /tmp/claude-settings.json \
     && mv /tmp/claude-settings.json ~/.claude/settings.json
   ```

   If you don't have `~/.claude/settings.json` yet, copy the file instead.

3. Start a new Claude Code session and open <http://localhost:3000>. Data appears within a minute.

## Dashboard

**Claude Code usage** opens as the home page, with no login.

- **Totals:** cost, tokens, cache hit ratio, sessions, active time, lines of code
- **Over time:** cost by model, tokens by type
- **Breakdowns:** cost by repository, cost by source (main loop, subagents, skills, MCP servers), top sessions
- **Tools:** calls, failure rate and latency per tool, permission decisions
- **API:** errors by status code, latency
- **Git:** commits and pull requests made by Claude Code
- **Skills:** activations over time, whether you or Claude triggered each skill, and sessions with no skills by repository

Cost is priced at API list rates. On a Pro, Max, Team or Enterprise plan it isn't what you pay; check your limits with `/usage` in Claude Code.

## Privacy

- Data stays on your machine. Grafana listens on `127.0.0.1` only, and ClickHouse isn't exposed outside Docker.
- Prompts and responses aren't recorded. Your email and account ID are.
- Skill, agent and MCP names are recorded. The setting that enables them (`OTEL_LOG_TOOL_DETAILS`) also sends Bash commands, file paths and tool arguments; the collector drops those before they're stored.
- To record prompt and response text, add `OTEL_LOG_USER_PROMPTS=1` to the same `env` block.

## Stop

```bash
docker compose down     # stop, keep the data
docker compose down -v  # stop and delete all data
```

Data is kept for one year.

## Configuration

To change credentials or ports, set these in a `.env` file next to `docker-compose.yml`:

| Variable | Default |
| --- | --- |
| `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD` | `otel` / `otel` |
| `GRAFANA_CLICKHOUSE_PASSWORD` | `grafana` |
| `GRAFANA_PORT` | `3000` |
| `OTLP_GRPC_PORT` / `OTLP_HTTP_PORT` | `4317` / `4318` |

If you change `OTLP_GRPC_PORT`, change the port in `OTEL_EXPORTER_OTLP_ENDPOINT` in your Claude Code settings too.

## Query with SQL

```bash
docker compose exec clickhouse clickhouse-client --user otel --password otel
```

Metrics are in `otel.otel_metrics_sum`, where `sum(Value)` gives the total for any range. Events are in `otel.otel_logs`, named by `LogAttributes['event.name']`.

```sql
SELECT toDate(TimeUnix) AS day, round(sum(Value), 2) AS cost_usd
FROM otel.otel_metrics_sum
WHERE MetricName = 'claude_code.cost.usage'
GROUP BY day
ORDER BY day DESC
```

## Troubleshooting

- **No data:** settings only apply to new sessions. Wait a minute, then check `docker compose logs otel-collector`.
- **Docker installed as a snap (Ubuntu):** keep the repo outside hidden folders and `/tmp`, or the config files mount as empty folders.
- **Organisation-managed settings:** a managed `OTEL_EXPORTER_OTLP_ENDPOINT` replaces yours, and Bash commands and file paths then go to that collector unfiltered. Remove `OTEL_LOG_TOOL_DETAILS` if that matters to you.
