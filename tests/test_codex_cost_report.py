import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts import codex_cost_report as report


class PricingTests(unittest.TestCase):
    def test_threshold_is_strict_and_prices_the_whole_request(self):
        for tokens, expected in [(271_999, 2.72049), (272_000, 2.7205), (272_001, 5.44077)]:
            with self.subTest(tokens=tokens):
                self.assertAlmostEqual(report.estimate_cost("gpt-6-astra", tokens, 0, 10), expected)

    def test_cached_tokens_count_toward_context_length(self):
        self.assertAlmostEqual(
            report.estimate_cost("gpt-6-astra", 272_001, 272_000, 10), 0.54477,
        )

    def test_cache_writes_replace_ordinary_input_charges(self):
        self.assertAlmostEqual(
            report.estimate_cost("gpt-6-astra", 100_000, 60_000, 1_000, 30_000), 0.585,
        )
        self.assertAlmostEqual(
            report.estimate_cost("gpt-6-astra", 300_000, 200_000, 1_000, 50_000), 2.725,
        )

    def test_sol_versions_have_different_cache_read_rates(self):
        self.assertAlmostEqual(
            report.estimate_cost("gpt-6.1-sol", 100_000, 80_000, 1_000, 10_000), 0.063,
        )
        self.assertAlmostEqual(
            report.estimate_cost("gpt-6-sol", 100_000, 80_000, 1_000, 10_000), 0.071,
        )

    def test_older_models_apply_only_their_documented_surcharges(self):
        self.assertAlmostEqual(report.estimate_cost("gpt-5.5", 300_000, 200_000, 1_000), 1.245)
        self.assertAlmostEqual(report.estimate_cost("gpt-5.4", 300_000, 200_000, 1_000), 0.6225)
        self.assertAlmostEqual(report.estimate_cost("gpt-5.4-mini", 300_000, 100_000, 1_000), 0.162)
        self.assertEqual(report.context_tier("gpt-5.4-mini", 300_000), "all")
        self.assertAlmostEqual(
            report.estimate_cost("gpt-5.4", 100_000, 60_000, 1_000, 30_000), 0.13,
        )

    def test_dated_snapshots_work_without_guessing_unknown_variants(self):
        self.assertAlmostEqual(
            report.estimate_cost(" GPT-5.5-2026-04-23 ", 300_000, 200_000, 1_000), 1.245,
        )
        for model in ("gpt-5.5-pro", "gpt-5.5-cyber", "gpt-6-astra-new", "codex-auto-review"):
            with self.subTest(model=model):
                self.assertIsNone(report.estimate_cost(model, 300_000, 0, 1_000))
                self.assertEqual(report.context_tier(model, 300_000), "unknown")

    def test_long_request_does_not_change_future_short_request_rates(self):
        report.estimate_cost("gpt-6-astra", 300_000, 0, 1_000)
        self.assertAlmostEqual(report.estimate_cost("gpt-6-astra", 100_000, 0, 1_000), 1.05)

    def test_inconsistent_cache_counts_cannot_charge_more_input_tokens(self):
        self.assertAlmostEqual(report.estimate_cost("gpt-6-astra", 100, 200, 10, 200), 0.0006)
        self.assertAlmostEqual(report.estimate_cost("gpt-6-astra", 100, 50, 10, 200), 0.001175)


class ReportTests(unittest.TestCase):
    def test_grouping_uses_each_request_including_after_context_shrinks(self):
        def event(tokens):
            return report.UsageEvent("2026-10-03", "conversation", "gpt-6-astra", tokens, 100_000, 1_000, 500)

        rows = report.summarize([event(170_000), event(170_000), event(300_000), event(170_000)], None, False)
        short = rows[("2026-10-03", "conversation", "gpt-6-astra", "<=272K")]
        long = rows[("2026-10-03", "conversation", "gpt-6-astra", ">272K")]
        self.assertEqual(short.requests, 3)
        self.assertEqual(short.input_tokens, 510_000)
        self.assertAlmostEqual(short.estimated_usd, 2.55)
        self.assertEqual(long.requests, 1)
        self.assertAlmostEqual(long.estimated_usd, 4.275)

    def test_nested_usage_parses_cache_writes(self):
        event = report.extract_usage({
            "response": {"model": "gpt-6.1-sol", "usage": {
                "input_tokens": 100_000, "output_tokens": 1_000,
                "input_tokens_details": {"cached_tokens": 80_000, "cache_write_tokens": 10_000},
                "output_tokens_details": {"reasoning_tokens": 500},
            }},
        }, {})
        summary = report.Summary()
        summary.add(event)
        self.assertEqual(summary.cache_write_tokens, 10_000)
        self.assertEqual(summary.reasoning_tokens, 500)
        self.assertAlmostEqual(summary.estimated_usd, 0.063)

    def test_collector_jsonl_to_text_and_csv(self):
        def record(tokens, output=1_000, model="gpt-6-astra"):
            fields = {
                "event.name": "codex.sse_event", "event.kind": "response.completed",
                "conversation.id": "conversation", "model": model,
                "input_token_count": tokens, "cached_token_count": 60_000,
                "cache_write_token_count": 30_000, "output_token_count": output,
                "reasoning_token_count": 500,
            }
            return {
                "timeUnixNano": "1791028800000000000",
                "attributes": [{"key": key, "value": {
                    "intValue" if isinstance(value, int) else "stringValue": str(value),
                }} for key, value in fields.items()],
            }

        batch = {"resourceLogs": [{"scopeLogs": [{"logRecords": [
            record(100_000), record(300_000), record(300_000, output=0),
            record(100_000, model="unpriced-model"),
        ]}]}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            path.write_text(json.dumps(batch) + "\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(report.main(["summary", "--input", str(path), "--format", "csv"]), 0)
            rows = list(csv.DictReader(io.StringIO(output.getvalue())))
            self.assertEqual(len(rows), 3)
            self.assertEqual([row["context_tier"] for row in rows], ["<=272K", ">272K", "unknown"])
            self.assertEqual([row["estimated_usd"] for row in rows], ["0.585000", "5.145000", ""])
            self.assertTrue(all(row["requests"] == "1" for row in rows))
            self.assertTrue(all(row["cache_write_tokens"] == "30000" for row in rows))
            self.assertEqual(rows[-1]["pricing_status"], "missing")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(report.main(["summary", "--input", str(path)]), 0)
            text = output.getvalue()
            for expected in ("Context", "Writes", "<=272K", ">272K", "TOTAL", "Missing pricing for: unpriced-model"):
                self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
