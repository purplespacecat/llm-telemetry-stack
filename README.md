# llm-telemetry-stack

Local Grafana dashboards for your Claude Code and Antigravity (`agy`) usage: what it costs, how you use it and what fails. Everything runs on your machine.

```text
Claude Code ─────────────────OTLP──┐
                                   ├──> OpenTelemetry Collector ──> ClickHouse ──> Grafana
~/.gemini ──> agy-exporter ──OTLP──┘
```

## Set up

You need Docker with the `docker compose` plugin.

1. Start the stack:

   ```bash
   docker compose up -d --build                 # Claude Code only
   docker compose --profile agy up -d --build   # Claude Code and Antigravity
   ```

   `--build` rebuilds the Antigravity exporter when you pull a new version.

2. **Antigravity:** list the Antigravity apps you use in a `COMPOSE_FILE` line in a `.env` file next to `docker-compose.yml`, then start the stack with `--profile agy`:

   ```bash
   COMPOSE_FILE=docker-compose.yml:docker-compose.agy-cli.yml:docker-compose.agy-desktop.yml   # CLI and desktop app
   COMPOSE_FILE=docker-compose.yml:docker-compose.agy-cli.yml                                  # CLI only
   COMPOSE_FILE=docker-compose.yml:docker-compose.agy-desktop.yml                              # desktop app only
   ```

   The exporter reads your Antigravity history in `~/.gemini` read-only and never sees your login tokens. It exports existing sessions once, then new turns as they happen.

3. **Claude Code:** Turn on telemetry by merging [`claude-code/settings.json`](claude-code/settings.json) into `~/.claude/settings.json`:

   ```bash
   jq -s '.[0] * .[1]' ~/.claude/settings.json claude-code/settings.json > /tmp/claude-settings.json \
     && mv /tmp/claude-settings.json ~/.claude/settings.json
   ```

   If you don't have `~/.claude/settings.json` yet, copy the file instead.

4. Start a new Claude Code session and open <http://localhost:3000>. Data appears within a minute.

## Dashboards

There are three dashboards for Claude Code and three for Antigravity, linked from each other's header. **Claude Code cost** opens as the home page, with no login.

- **Cost:** spend, cost per session and per active hour, tokens, cache hit ratio; cost by model, repository, source (main loop, subagents, skills, MCP servers) and session. Claude Code also shows background requests by source, context size per session, compactions, tokens by model, and subagent runs with their model and cost.
- **Efficiency:** active time, lines of code, commits and pull requests; which skills ran, whether you or the agent triggered them, and sessions with no skills; tool latency, permission prompts and decisions; MCP servers used. Claude Code also shows hooks and hook time, and Antigravity shows subagents.
- **Reliability:** API errors, error rate, latency and failed calls; failed tool calls by tool and error type. Claude Code also shows exhausted retries, MCP connection failures and hook errors, and Antigravity shows permission denials and subagent failures.

Cost is priced at API list rates. On a Pro, Max, Team or Enterprise plan it isn't what you pay; check your limits with `/usage` in Claude Code.

Every request re-sends the whole conversation, so context size drives most of the cost. If **Context size** stays high, run `/compact` or start a new session instead of resuming an old one.

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
| `COMPOSE_FILE` | `docker-compose.yml` (add the Antigravity files as in step 2) |
| `AGY_CLI_DIR` / `AGY_APP_DIR` | `~/.gemini/antigravity-cli` / `~/.gemini/antigravity` |
| `AGY_UID` / `AGY_GID` | `1000` / `1000`, the user the Antigravity exporter runs as |

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
- **No Antigravity data:** check `docker compose logs agy-exporter`. If it reports a surface directory not found, that app's file is missing from `COMPOSE_FILE` (setup step 2).
- **`bind source path does not exist` when starting:** `COMPOSE_FILE` lists an Antigravity app you haven't used on this machine. Remove its file from the line.
- **Docker installed as a snap (Ubuntu):** keep the repo outside hidden folders and `/tmp`, or the config files mount as empty folders.
- **Organisation-managed settings:** a managed `OTEL_EXPORTER_OTLP_ENDPOINT` replaces yours, and Bash commands and file paths then go to that collector unfiltered. Remove `OTEL_LOG_TOOL_DETAILS` if that matters to you.
