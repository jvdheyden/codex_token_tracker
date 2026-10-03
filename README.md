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
Input: /path/to/codex-otel.jsonl
Range: all
Pricing: Standard API rates (2026-10-03); context tier per request, including cached input

Day         Conversation                          Model              Context   Req       Input      Cached      Writes    Total In      Output   Reasoning      Est USD
2026-10-03  example-conversation                  gpt-6-astra        <=272K      1       40000       60000       30000      100000        1000         500       0.5850
2026-10-03  example-conversation                  gpt-6-astra        >272K       1      240000       60000       30000      300000        1000         500       5.1450
TOTAL                                                                            2      280000      120000       60000      400000        2000        1000       5.7300
```

## Pricing

The pricing table is in `scripts/codex_cost_report.py`, verified against
[official OpenAI pricing](https://developers.openai.com/api/docs/pricing) on
**2026-10-03**. These are Standard API rates in USD per million tokens:

| Model | Input | Cached input | Cache writes | Output |
| --- | ---: | ---: | ---: | ---: |
| GPT-6 Astra | 10.00 | 1.00 | 12.50 | 50.00 |
| GPT-6.1 Sol | 2.00 | 0.10 | 2.50 | 10.00 |
| GPT-6 Sol | 2.00 | 0.20 | 2.50 | 10.00 |
| GPT-6 Luna | 0.10 | 0.01 | 0.125 | 0.50 |
| GPT-5.6 Sol | 4.00 | 0.40 | 5.00 | 20.00 |
| GPT-5.6 Terra | 2.00 | 0.20 | 2.50 | 12.00 |
| GPT-5.6 Luna | 0.20 | 0.02 | 0.25 | 1.20 |
| GPT-5.5 | 5.00 | 0.50 | Same as input | 30.00 |
| GPT-5.4 | 2.50 | 0.25 | Same as input | 15.00 |

These rows show rates for requests with **at most 272,000 input tokens**.
Above that threshold, the entire request uses **2x input/cache rates and
1.5x output rates**. The script selects the tier from each event's total input,
including cached reads and cache writes, before summing costs. It does not use
cumulative conversation totals or add output tokens to the threshold.

GPT-5.4 mini/nano, older Codex models, and the other existing entries retain
their flat rates. Dated model snapshots use their base model's rates; unknown
variants remain unpriced instead of inheriting a potentially incorrect rate.
Update the table when prices change; historical logs are re-estimated using
the current table, not the prices in effect on the event date.

The text report uses `Input` for non-cached input, `Cached` for cached input,
and `Total In` for the raw OTEL `input_token_count` value. `Writes` is a subset
of `Input`, not an additional token count: `Input + Cached = Total In`.
Cache-write tokens use their own rate when reported in telemetry, following
[OpenAI's cache accounting](https://developers.openai.com/api/docs/guides/prompt-caching).
Older logs without this field treat all non-cached input at the ordinary rate.

Rows are grouped by local day, Codex `conversation.id`, model, and context tier.
`Context` is `<=272K` or `>272K` for tiered models, `all` for flat-rate models,
and `unknown` for unpriced models. A conversation can have both short- and
long-context rows, including when compaction reduces a later request's input.
CSV preserves the existing columns and appends `context_tier` and
`cache_write_tokens`.

The report skips `response.completed` records with `output_token_count = 0`.
Codex emits these for internal warmup/no-op completions, and `/exit` does not
include them in its session token summary.

Reasoning tokens are reported separately when present, but cost is calculated
from total output tokens, which already include reasoning.

## Limitations

- This counts only records that contain `response.completed`.
- Field names in Codex OTEL output may change. The parser is tolerant of common
  nested shapes, but unknown shapes may be skipped or reported with `unknown`
  model/pricing.
- The collector file can grow over time. Rotate or archive
  `~/.local/share/codex-token-tracker/codex-otel.jsonl` manually if needed.
- Web search tool-call fees and other non-token add-ons are not estimated.
- Estimates use Standard rates even if requests used Fast/Priority, Ultrafast,
  Batch, or Flex processing. Regional processing premiums are not included.
- Internal models such as `codex-auto-review` have no rate in this table. Their
  costs show `n/a`; the total cost includes only models with known pricing.

## Tests

Run the dependency-free pricing and report tests:

```bash
python3 -m unittest discover -s tests -v
```
