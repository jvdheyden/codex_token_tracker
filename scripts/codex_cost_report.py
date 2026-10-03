#!/usr/bin/env python3
"""Summarize Codex OpenTelemetry JSONL into API-equivalent token cost."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_INPUT = (
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    / "codex-token-tracker"
    / "codex-otel.jsonl"
)

# Standard text-token API prices in USD per 1M tokens.
# Verified 2026-10-03: https://developers.openai.com/api/docs/pricing
# These are short-context rates; long-context multipliers are applied below.
PRICING_USD_PER_1M_TOKENS: dict[str, dict[str, float]] = {
    "gpt-6-astra": {"input": 10.00, "cached_input": 1.00, "cache_write": 12.50, "output": 50.00},
    "gpt-6.1-sol": {"input": 2.00, "cached_input": 0.10, "cache_write": 2.50, "output": 10.00},
    "gpt-6-sol": {"input": 2.00, "cached_input": 0.20, "cache_write": 2.50, "output": 10.00},
    "gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "cache_write": 0.125, "output": 0.50},
    "gpt-5.6-sol": {"input": 4.00, "cached_input": 0.40, "cache_write": 5.00, "output": 20.00},
    "gpt-5.6-terra": {"input": 2.00, "cached_input": 0.20, "cache_write": 2.50, "output": 12.00},
    "gpt-5.6-luna": {"input": 0.20, "cached_input": 0.02, "cache_write": 0.25, "output": 1.20},
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.02, "output": 1.25},
    "gpt-5.3-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
    "gpt-5.2-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
    "gpt-5.2": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
    "gpt-5.1-codex-max": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5.1-codex": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5.1-codex-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00},
    "gpt-5.1": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-codex": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00},
}

# Only models with a documented long-context surcharge belong here. In
# particular, GPT-5.4 mini/nano and the older Codex models retain flat rates.
LONG_CONTEXT_THRESHOLD = 272_000
LONG_CONTEXT_MODELS = frozenset({
    "gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4",
})

MODEL_KEYS = (
    "response.model",
    "gen_ai.response.model",
    "gen_ai.request.model",
    "openai.model",
    "llm.model_name",
    "model_name",
    "model",
)
INPUT_TOKEN_KEYS = (
    "input_token_count",
    "response.usage.input_tokens",
    "usage.input_tokens",
    "gen_ai.usage.input_tokens",
    "input_tokens",
    "prompt_tokens",
)
CACHED_TOKEN_KEYS = (
    "cached_token_count",
    "response.usage.input_tokens_details.cached_tokens",
    "usage.input_tokens_details.cached_tokens",
    "input_tokens_details.cached_tokens",
    "prompt_tokens_details.cached_tokens",
    "cached_input_tokens",
    "cached_tokens",
)
CACHE_WRITE_TOKEN_KEYS = (
    "cache_write_token_count",
    "response.usage.input_tokens_details.cache_write_tokens",
    "usage.input_tokens_details.cache_write_tokens",
    "input_tokens_details.cache_write_tokens",
    "prompt_tokens_details.cache_write_tokens",
    "cache_write_tokens",
)
OUTPUT_TOKEN_KEYS = (
    "output_token_count",
    "response.usage.output_tokens",
    "usage.output_tokens",
    "gen_ai.usage.output_tokens",
    "output_tokens",
    "completion_tokens",
)
REASONING_TOKEN_KEYS = (
    "reasoning_token_count",
    "response.usage.output_tokens_details.reasoning_tokens",
    "usage.output_tokens_details.reasoning_tokens",
    "output_tokens_details.reasoning_tokens",
    "completion_tokens_details.reasoning_tokens",
    "reasoning_tokens",
)
TOKEN_KEYS = INPUT_TOKEN_KEYS + CACHED_TOKEN_KEYS + CACHE_WRITE_TOKEN_KEYS + OUTPUT_TOKEN_KEYS + REASONING_TOKEN_KEYS
CONVERSATION_KEYS = ("conversation.id", "conversation_id")
NANO_TIME_KEYS = ("timeUnixNano", "observedTimeUnixNano", "time_unix_nano")
MILLI_TIME_KEYS = ("timeUnixMilli", "timestamp_ms", "created_ms")
ISO_TIME_KEYS = ("timestamp", "time", "created_at", "createdAt")


@dataclass
class UsageEvent:
    day: str
    conversation_id: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_write_tokens: int = 0


@dataclass
class Summary:
    requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_usd: float = 0.0
    missing_price: bool = False
    cache_write_tokens: int = 0

    def add(self, event: UsageEvent) -> None:
        self.requests += 1
        self.input_tokens += event.input_tokens
        self.cached_input_tokens += event.cached_input_tokens
        self.output_tokens += event.output_tokens
        self.reasoning_tokens += event.reasoning_tokens
        self.cache_write_tokens += event.cache_write_tokens
        cost = estimate_cost(
            event.model, event.input_tokens, event.cached_input_tokens,
            event.output_tokens, event.cache_write_tokens,
        )
        if cost is None:
            self.missing_price = True
        else:
            self.estimated_usd += cost


def otel_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in (
        "stringValue",
        "intValue",
        "doubleValue",
        "boolValue",
        "bytesValue",
        "asString",
        "asInt",
        "asDouble",
        "asBool",
    ):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        raw = value["arrayValue"]
        values = raw.get("values", []) if isinstance(raw, dict) else raw
        return [otel_value(item) for item in values]
    if "kvlistValue" in value:
        raw = value["kvlistValue"]
        values = raw.get("values", []) if isinstance(raw, dict) else raw
        return attributes_to_dict(values)
    return {k: otel_value(v) for k, v in value.items()}


def attributes_to_dict(attrs: Any) -> dict[str, Any]:
    if isinstance(attrs, dict):
        return {str(k): otel_value(v) for k, v in attrs.items()}
    if not isinstance(attrs, list):
        return {}
    output: dict[str, Any] = {}
    for item in attrs:
        if not isinstance(item, dict) or "key" not in item:
            continue
        output[str(item["key"])] = otel_value(item.get("value"))
    return output


def expanded_record(record: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in record.items():
        if key == "attributes":
            output.update(attributes_to_dict(value))
        elif key == "body":
            body = otel_value(value)
            output["body"] = body
            if isinstance(body, dict):
                output.update(body)
        else:
            output[key] = otel_value(value)
    return expand_json_strings(output)


def expand_json_strings(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return value
    if isinstance(value, dict):
        return {str(k): expand_json_strings(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_json_strings(item, depth + 1) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if len(text) <= 2_000_000 and text[:1] in ("{", "["):
            try:
                return expand_json_strings(json.loads(text), depth + 1)
            except json.JSONDecodeError:
                return value
    return value


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    output: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            output[path] = child
            output.update(flatten(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f"{prefix}.{index}" if prefix else str(index)
            output[path] = child
            output.update(flatten(child, path))
    return output


def normalized_key(key: str) -> str:
    return key.replace("_", ".").lower()


def first_value(flat: dict[str, Any], keys: Iterable[str]) -> Any:
    normalized = [(key, normalized_key(key)) for key in flat]
    for wanted in keys:
        wanted_norm = normalized_key(wanted)
        for key, key_norm in normalized:
            if key_norm == wanted_norm or key_norm.endswith("." + wanted_norm):
                value = flat[key]
                if value not in (None, ""):
                    return value
    return None


def as_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return 0
        try:
            return max(0, int(float(text)))
        except ValueError:
            return 0
    return 0


def iter_otel_records(root: dict[str, Any]) -> Iterable[dict[str, Any]]:
    has_otel_batches = bool(root.get("resourceSpans") or root.get("resourceLogs"))

    for resource_span in root.get("resourceSpans", []) or []:
        resource_attrs = attributes_to_dict(resource_span.get("resource", {}).get("attributes", []))
        for scope_span in resource_span.get("scopeSpans", []) or []:
            scope_attrs = attributes_to_dict(scope_span.get("scope", {}).get("attributes", []))
            for span in scope_span.get("spans", []) or []:
                span_attrs = attributes_to_dict(span.get("attributes", []))
                span_base = {
                    **resource_attrs,
                    **scope_attrs,
                    **span_attrs,
                    "span.name": span.get("name"),
                    "span.startTimeUnixNano": span.get("startTimeUnixNano"),
                    "span.endTimeUnixNano": span.get("endTimeUnixNano"),
                }
                for event in span.get("events", []) or []:
                    if isinstance(event, dict):
                        yield {**span_base, **expanded_record(event)}

    for resource_log in root.get("resourceLogs", []) or []:
        resource_attrs = attributes_to_dict(resource_log.get("resource", {}).get("attributes", []))
        for scope_log in resource_log.get("scopeLogs", []) or []:
            scope_attrs = attributes_to_dict(scope_log.get("scope", {}).get("attributes", []))
            for log_record in scope_log.get("logRecords", []) or []:
                if isinstance(log_record, dict):
                    yield {**resource_attrs, **scope_attrs, **expanded_record(log_record)}

    if has_otel_batches:
        return

    for record in iter_dicts(root):
        yield expanded_record(record)


def iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def is_response_completed(record: dict[str, Any]) -> bool:
    flat = flatten(record)
    completed = False
    for value in flat.values():
        if isinstance(value, str) and value.strip() == "response.completed":
            completed = True
            break
    if not completed:
        return False

    # Codex currently emits both websocket and SSE records for response.completed.
    # The SSE record carries usage counters; the websocket record can duplicate
    # the request with no token fields.
    event_name = flat.get("event.name")
    has_token_counts = any(first_value(flat, (key,)) is not None for key in TOKEN_KEYS)
    if event_name == "codex.websocket_event" and not has_token_counts:
        return False
    return True


def event_day(record: dict[str, Any], root: dict[str, Any]) -> str:
    timestamp = find_timestamp(record) or find_timestamp(root)
    if timestamp is None:
        return "unknown"
    return timestamp.astimezone().date().isoformat()


def find_timestamp(value: Any) -> datetime | None:
    flat = flatten(value)
    for key in NANO_TIME_KEYS:
        raw = first_value(flat, (key,))
        if raw is not None:
            parsed = parse_unix_time(raw, "nano")
            if parsed:
                return parsed
    for key in MILLI_TIME_KEYS:
        raw = first_value(flat, (key,))
        if raw is not None:
            parsed = parse_unix_time(raw, "milli")
            if parsed:
                return parsed
    for key in ISO_TIME_KEYS:
        raw = first_value(flat, (key,))
        if raw is not None:
            parsed = parse_iso_time(raw)
            if parsed:
                return parsed
    return None


def parse_unix_time(value: Any, unit: str) -> datetime | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    if unit == "nano":
        seconds = number / 1_000_000_000
    elif unit == "milli":
        seconds = number / 1_000
    else:
        seconds = float(number)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def parse_iso_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def extract_usage(record: dict[str, Any], root: dict[str, Any]) -> UsageEvent:
    flat = flatten(record)
    model = str(first_value(flat, MODEL_KEYS) or "unknown").strip() or "unknown"
    conversation_id = str(first_value(flat, CONVERSATION_KEYS) or "unknown").strip() or "unknown"
    return UsageEvent(
        day=event_day(record, root),
        conversation_id=conversation_id,
        model=model,
        input_tokens=as_int(first_value(flat, INPUT_TOKEN_KEYS)),
        cached_input_tokens=as_int(first_value(flat, CACHED_TOKEN_KEYS)),
        output_tokens=as_int(first_value(flat, OUTPUT_TOKEN_KEYS)),
        reasoning_tokens=as_int(first_value(flat, REASONING_TOKEN_KEYS)),
        cache_write_tokens=as_int(first_value(flat, CACHE_WRITE_TOKEN_KEYS)),
    )


def read_usage_events(path: Path) -> Iterable[UsageEvent]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                root = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(root, dict):
                continue
            for record in iter_otel_records(root):
                if is_response_completed(record):
                    event = extract_usage(record, root)
                    if event.output_tokens == 0:
                        continue
                    yield event


def pricing_model_key(model: str) -> str | None:
    normalized = model.lower().strip()
    if normalized in PRICING_USD_PER_1M_TOKENS:
        return normalized
    # Support dated snapshots, but never price an unknown variant (e.g. a Pro
    # or Cyber model) as its cheaper parent merely because the prefix matches.
    base = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", normalized)
    if base in PRICING_USD_PER_1M_TOKENS:
        return base
    return None


def context_tier(model: str, input_tokens: int) -> str:
    key = pricing_model_key(model)
    if key is None:
        return "unknown"
    if key not in LONG_CONTEXT_MODELS:
        return "all"
    return ">272K" if input_tokens > LONG_CONTEXT_THRESHOLD else "<=272K"


def pricing_for_model(model: str, input_tokens: int = 0) -> dict[str, float] | None:
    key = pricing_model_key(model)
    if key is None:
        return None
    pricing = PRICING_USD_PER_1M_TOKENS[key]
    if key in LONG_CONTEXT_MODELS and input_tokens > LONG_CONTEXT_THRESHOLD:
        # The entire request uses these rates, not just tokens above 272K.
        # Both cached reads and cache writes receive the input multiplier.
        return {kind: rate * (1.5 if kind == "output" else 2) for kind, rate in pricing.items()}
    return pricing


def estimate_cost(
    model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int,
    cache_write_tokens: int = 0,
) -> float | None:
    pricing = pricing_for_model(model, input_tokens)
    if pricing is None:
        return None
    cached = min(cached_input_tokens, input_tokens)
    non_cached = max(0, input_tokens - cached)
    writes = min(cache_write_tokens, non_cached)
    return (
        (non_cached - writes) * pricing["input"]
        + cached * pricing["cached_input"]
        + writes * pricing.get("cache_write", pricing["input"])
        + output_tokens * pricing["output"]
    ) / 1_000_000


def summarize(events: Iterable[UsageEvent], since: str | None, today: bool) -> dict[tuple[str, str, str, str], Summary]:
    since_date = datetime.now().astimezone().date().isoformat() if today else since
    summaries: dict[tuple[str, str, str, str], Summary] = defaultdict(Summary)
    for event in events:
        if since_date and (event.day == "unknown" or event.day < since_date):
            continue
        tier = context_tier(event.model, event.input_tokens)
        summaries[(event.day, event.conversation_id, event.model, tier)].add(event)
    return summaries


def print_text(path: Path, rows: dict[tuple[str, str, str, str], Summary], range_label: str) -> None:
    print("Codex token tracker summary")
    print(f"Input: {path}")
    print(f"Range: {range_label}")
    print("Pricing: Standard API rates (2026-10-03); context tier per request, including cached input")
    print()
    if not rows:
        print("No response.completed events found.")
        return

    print(
        f"{'Day':<10}  {'Conversation':<36}  {'Model':<18} {'Context':<7} {'Req':>5} {'Input':>11} {'Cached':>11} {'Writes':>11} "
        f"{'Total In':>11} {'Output':>11} {'Reasoning':>11} {'Est USD':>12}"
    )
    total = Summary()
    missing_models: set[str] = set()
    for (day, conversation_id, model, tier), row in sorted(rows.items()):
        total.requests += row.requests
        total.input_tokens += row.input_tokens
        total.cached_input_tokens += row.cached_input_tokens
        total.cache_write_tokens += row.cache_write_tokens
        total.output_tokens += row.output_tokens
        total.reasoning_tokens += row.reasoning_tokens
        total.estimated_usd += row.estimated_usd
        if row.missing_price:
            missing_models.add(model)
        cost_text = "n/a" if row.missing_price else f"{row.estimated_usd:.4f}"
        uncached_input = max(0, row.input_tokens - row.cached_input_tokens)
        print(
            f"{day:<10}  {conversation_id:<36}  {model:<18.18} {tier:<7} {row.requests:>5} {uncached_input:>11} "
            f"{row.cached_input_tokens:>11} {row.cache_write_tokens:>11} {row.input_tokens:>11} {row.output_tokens:>11} "
            f"{row.reasoning_tokens:>11} {cost_text:>12}"
        )
    total_cost_text = "n/a" if missing_models and total.estimated_usd == 0 else f"{total.estimated_usd:.4f}"
    total_uncached_input = max(0, total.input_tokens - total.cached_input_tokens)
    print(
        f"{'TOTAL':<10}  {'':<36}  {'':<18} {'':<7} {total.requests:>5} {total_uncached_input:>11} "
        f"{total.cached_input_tokens:>11} {total.cache_write_tokens:>11} {total.input_tokens:>11} {total.output_tokens:>11} "
        f"{total.reasoning_tokens:>11} {total_cost_text:>12}"
    )
    if missing_models:
        print()
        print("Missing pricing for: " + ", ".join(sorted(missing_models)))


def print_csv(rows: dict[tuple[str, str, str, str], Summary]) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(
        [
            "day",
            "conversation_id",
            "model",
            "requests",
            "input_tokens",
            "cached_input_tokens",
            "total_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "estimated_usd",
            "pricing_status",
            "context_tier",
            "cache_write_tokens",
        ]
    )
    for (day, conversation_id, model, tier), row in sorted(rows.items()):
        uncached_input = max(0, row.input_tokens - row.cached_input_tokens)
        writer.writerow(
            [
                day,
                conversation_id,
                model,
                row.requests,
                uncached_input,
                row.cached_input_tokens,
                row.input_tokens,
                row.output_tokens,
                row.reasoning_tokens,
                "" if row.missing_price else f"{row.estimated_usd:.6f}",
                "missing" if row.missing_price else "priced",
                tier,
                row.cache_write_tokens,
            ]
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    summary = subparsers.add_parser("summary", help="print a token and cost summary")
    summary.add_argument(
        "--input",
        type=Path,
        default=Path(os.environ.get("CODEX_TOKEN_TRACKER_JSONL", DEFAULT_INPUT)),
        help=f"collector JSONL path (default: {DEFAULT_INPUT})",
    )
    summary.add_argument("--today", action="store_true", help="show only local-date today")
    summary.add_argument("--since", help="show events on or after YYYY-MM-DD")
    summary.add_argument("--format", choices=("text", "csv"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "summary":
        parser.print_help(sys.stderr)
        return 2
    if args.today and args.since:
        parser.error("--today and --since are mutually exclusive")
    if args.since:
        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            parser.error("--since must be YYYY-MM-DD")
    if not args.input.exists():
        print(f"No telemetry file found: {args.input}", file=sys.stderr)
        return 1

    rows = summarize(read_usage_events(args.input), since=args.since, today=args.today)
    if args.today:
        range_label = "today"
    elif args.since:
        range_label = f"since {args.since}"
    else:
        range_label = "all"

    if args.format == "csv":
        print_csv(rows)
    else:
        print_text(args.input, rows, range_label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
