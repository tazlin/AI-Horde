# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The analysis half of ``tools/text_tariff_bench.py``: prompt construction, estimate errors, curve fits.

The HTTP half talks to a live KoboldCpp and is exercised by running the tool; the parts a calibration
depends on for its numbers are checked here without a backend.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

TOOL_PATH = pathlib.Path(__file__).resolve().parents[2] / "tools" / "text_tariff_bench.py"


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("text_tariff_bench", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestCorpusAndPrompts:
    def test_prose_corpus_keeps_only_running_text(self, bench, tmp_path: pathlib.Path):
        (tmp_path / "a.md").write_text(
            "---\ntitle: x\n---\n\n# Heading\n\n| a | b |\n| - | - |\n\n```\ncode block\n```\n\n"
            + "A paragraph of running text that is long enough to be kept by the corpus filter, " * 4
            + "\n\nshort\n",
        )
        paragraphs = bench.prose_corpus(tmp_path)
        assert len(paragraphs) == 1
        assert paragraphs[0].startswith("A paragraph of running text")

    def test_prompts_from_different_seeds_do_not_share_a_prefix(self, bench):
        paragraphs = [f"Paragraph number {index} " * 20 for index in range(40)]
        first = bench.build_prompt(paragraphs, 4000, seed=1)
        second = bench.build_prompt(paragraphs, 4000, seed=2)
        assert len(first) == len(second) == 4000
        assert first[:200] != second[:200]

    def test_a_prompt_is_reproducible_for_a_seed(self, bench):
        paragraphs = [f"Paragraph number {index} " * 20 for index in range(40)]
        assert bench.build_prompt(paragraphs, 3000, seed=7) == bench.build_prompt(paragraphs, 3000, seed=7)

    def test_an_empty_corpus_is_refused(self, bench):
        with pytest.raises(ValueError, match="no paragraphs"):
            bench.build_prompt([], 100, seed=1)


class TestTokenizerRow:
    def test_errors_are_relative_to_the_true_count(self, bench):
        # 4,000 chars counted as 800 tokens: /4 says 1000 (+25%), /3 says 1334 (+66.8%).
        row = bench.tokenizer_row("prose", 4000, 800)
        assert row["chars_per_token"] == 5.0
        assert row["billing_estimate"] == 1000
        assert row["billing_estimate_error_pct"] == 25.0
        assert row["fit_estimate"] == 1334
        assert row["fit_estimate_error_pct"] == 66.8

    def test_a_dense_script_undercounts(self, bench):
        # 300 chars counted as 200 tokens: /4 says 75, 62.5% under.
        row = bench.tokenizer_row("cjk", 300, 200)
        assert row["billing_estimate_error_pct"] == -62.5


class TestCostFit:
    def test_exact_curves_are_recovered(self, bench):
        prompts = [256, 512, 1024, 2048, 4096, 8192, 12288]
        prefill = [0.5 + 0.007 * p + 8e-7 * p * p for p in prompts]
        decode = [0.04 + 1e-5 * p for p in prompts]

        fit = bench.fit_costs(prompts, prefill, decode)

        assert fit.prefill_constant == pytest.approx(0.5, rel=1e-6)
        assert fit.prefill_per_token == pytest.approx(0.007, rel=1e-6)
        assert fit.prefill_per_token_squared == pytest.approx(8e-7, rel=1e-6)
        assert fit.prefill_r_squared == pytest.approx(1.0)
        assert fit.decode_per_token_constant == pytest.approx(0.04, rel=1e-6)
        assert fit.decode_per_token_per_context_token == pytest.approx(1e-5, rel=1e-6)
        assert fit.decode_r_squared == pytest.approx(1.0)

    def test_the_power_law_exponent_of_a_linear_prefill_is_about_one(self, bench):
        prompts = [256, 512, 1024, 2048, 4096, 8192]
        fit = bench.fit_costs(prompts, [0.01 * p for p in prompts], [0.04] * len(prompts))
        assert fit.prefill_power_law_exponent == pytest.approx(1.0, abs=1e-6)

    def test_rates_are_expressed_in_decode_token_units(self, bench):
        prompts = [256, 512, 1024, 2048, 4096, 8192, 12288]
        fit = bench.fit_costs(prompts, [0.5 + 0.008 * p + 8e-7 * p * p for p in prompts], [0.04 + 1e-5 * p for p in prompts])

        rates = bench.derive_rates(fit)

        # One prompt token costs 0.008 / 0.04 = 0.2 of a decode token; the context rate is 1e-5 / 0.04.
        assert rates.input_rate == pytest.approx(0.2, rel=1e-6)
        assert rates.input_quadratic_rate == pytest.approx(2e-5, rel=1e-6)
        assert rates.context_decode_rate == pytest.approx(2.5e-4, rel=1e-6)

    def test_too_few_distinct_sizes_is_an_error(self, bench):
        with pytest.raises(ValueError, match="singular"):
            bench.fit_costs([1024, 1024], [1.0, 1.0], [0.04, 0.04])

    def test_report_reads_the_prefill_rows_the_tool_writes(self, bench):
        rows = [
            {
                "last_input_count": p,
                "process_seconds": 0.5 + 0.007 * p + 8e-7 * p * p,
                "eval_seconds": 128 * (0.04 + 1e-5 * p),
                "last_token_count": 128,
            }
            for p in (256, 512, 1024, 2048, 4096, 8192, 12288)
        ]
        summary = bench.report(rows)
        assert summary["points"] == 7
        assert summary["rates"]["input_rate"] == pytest.approx(0.175, rel=1e-6)
