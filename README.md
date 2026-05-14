# Codex Token Tracker

Local-only token and estimated API cost tracking for Codex CLI usage on this
machine.

This project keeps all telemetry local:

- Codex exports OpenTelemetry to `127.0.0.1:4318`.
- A local OpenTelemetry Collector container writes JSONL to
  `~/.local/share/codex-token-tracker/codex-otel.jsonl`.
- The report script parses that JSONL and estimates API-equivalent cost from a
  small pricing table in `scripts/codex_cost_report.py`.

The estimate is not billing truth. It is a local approximation of what similar
usage would have cost through the API.

## Files

- `ops/otel/otel-codex.yaml` - local collector config.
- `scripts/ensure_otel_collector.sh` - idempotently starts the local collector.
- `scripts/codex_cost_report.py` - parses collector JSONL and prints summaries.

There is no venv, Makefile, database, or third-party Python package dependency.

## Codex Config

Add or keep this in `~/.codex/config.toml`:

```toml
[features]
codex_hooks = true

[otel]
environment = "local"
log_user_prompt = false
exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs", protocol = "binary" } }
trace_exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/traces", protocol = "binary" } }
```

`log_user_prompt = false` keeps prompt text out of exported telemetry.

Create or merge this into `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "bash -lc 'exec \"$HOME/codex_token_tracker/scripts/ensure_otel_collector.sh\"'",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

The hook starts the collector when Codex starts or resumes. It is safe to run
repeatedly and does not emit stdout on success.

## Start And Stop

Start or ensure the collector is running:

```bash
bash ~/codex_token_tracker/scripts/ensure_otel_collector.sh
```

The helper runs the collector as your current UID/GID so the container can write
to `~/.local/share/codex-token-tracker`. If it finds a `restarting` or `dead`
collector container, it removes and recreates it with the current settings.

Stop it:

```bash
docker stop codex-otel-collector
```

Start it again:

```bash
docker start codex-otel-collector
```

If the container is stuck restarting, run:

```bash
bash ~/codex_token_tracker/scripts/ensure_otel_collector.sh
docker ps --filter name=codex-otel-collector
docker logs --tail 80 codex-otel-collector
```

## Stored Telemetry

The collector filters the raw Codex OTEL stream before writing JSONL:

- keep log records where `event.name == "codex.sse_event"`
- keep only `event.kind == "response.completed"`
- drop traces, stream deltas, websocket deltas, and tool-call fragments

That keeps the stored file focused on the final usage counters needed for cost
estimation. The file exporter is configured with `append: true`; without that,
the OpenTelemetry Collector truncates the JSONL whenever it opens the file after
a restart.

After changing `ops/otel/otel-codex.yaml`, recreate the collector:

```bash
bash ~/codex_token_tracker/scripts/ensure_otel_collector.sh --recreate
```

If you intentionally want to discard old noisy telemetry after confirming the
report works, archive first, truncate the file, then recreate the collector so
it opens the visible file rather than a deleted inode:

```bash
cp ~/.local/share/codex-token-tracker/codex-otel.jsonl \
  ~/.local/share/codex-token-tracker/codex-otel.before-filter.jsonl
: > ~/.local/share/codex-token-tracker/codex-otel.jsonl
bash ~/codex_token_tracker/scripts/ensure_otel_collector.sh --recreate
```

If Docker is missing or not running, the startup script exits successfully with
a warning so Codex startup is not blocked. In that case no telemetry is
collected until Docker is available and the collector is running.

The hook uses plain `docker`, not `sudo docker`. Check this before relying on
the hook:

```bash
docker info
```

If `sudo docker run hello-world` works but `docker info` says permission
denied, add your user to Docker's non-root access group and start a new login
session:

```bash
sudo usermod -aG docker "$USER"
newgrp docker
docker info
```

On some systems you may need to log out and back in, or restart Docker, before
the new group membership applies. The important acceptance check is that
`docker info` works without `sudo`; Codex hooks cannot answer sudo password
prompts.

The first run may need to pull `otel/opentelemetry-collector-contrib:latest`.
Run the startup script manually once before relying on the SessionStart hook if
you want Codex startup to stay fast.

## Reports

Print all collected usage:

```bash
python3 ~/codex_token_tracker/scripts/codex_cost_report.py summary
```

Print only today:

```bash
python3 ~/codex_token_tracker/scripts/codex_cost_report.py summary --today
```

Print since a date:

```bash
python3 ~/codex_token_tracker/scripts/codex_cost_report.py summary --since 2026-04-01
```

CSV:

```bash
python3 ~/codex_token_tracker/scripts/codex_cost_report.py summary --format csv
```

Use a non-default JSONL file:

```bash
python3 ~/codex_token_tracker/scripts/codex_cost_report.py summary --input /path/to/codex-otel.jsonl
```

## Example Output

```text
Codex token tracker summary
Input: /home/jvdh/.local/share/codex-token-tracker/codex-otel.jsonl
Range: all

Day         Conversation                          Model                Req       Input      Cached    Total In      Output   Reasoning      Est USD
2026-04-21  019db1e4-eb34-7ca2-a3da-940c5a1648f6  gpt-5.4                3       85432      125000      210432       18420       12000       0.4963
2026-04-21  019db1f2-58b6-77d2-9a7d-0d8efef2d624  gpt-5.4-mini           2       20110       30000       50110        6400        4100       0.0618
TOTAL                                                                  5      105542      155000      260542       24820       16100       0.5581
```

## Pricing

The pricing table is in `scripts/codex_cost_report.py` near the top of the
file. Update it when OpenAI pricing changes.

The text report uses `Input` for non-cached input, `Cached` for cached input,
and `Total In` for the raw OTEL `input_token_count` value. This mirrors Codex's
exit summary shape: `input=... (+ cached) output=...`.

Rows are grouped by local day, Codex `conversation.id`, and model.

The report skips `response.completed` records with `output_token_count = 0`.
Codex emits these for internal warmup/no-op completions, and `/exit` does not
include them in its session token summary.

Current defaults include standard text-token pricing for GPT-5.4, GPT-5.4 mini,
GPT-5.4 nano, GPT-5.3-Codex, GPT-5.2-Codex, and related models. Reasoning tokens
are reported separately when present, but cost is calculated from total output
tokens because OpenAI bills reasoning tokens as output tokens.

For very large GPT-5.4 sessions, OpenAI documents higher rates above the 272K
input-token threshold. This script does not try to infer that full-session
threshold policy from individual events; treat very large-session estimates as
approximate.

## Limitations

- This counts only records that contain `response.completed`.
- Field names in Codex OTEL output may change. The parser is tolerant of common
  nested shapes, but unknown shapes may be skipped or reported with `unknown`
  model/pricing.
- The collector file can grow over time. Rotate or archive
  `~/.local/share/codex-token-tracker/codex-otel.jsonl` manually if needed.
- Web search tool-call fees and other non-token add-ons are not estimated.
