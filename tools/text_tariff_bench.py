# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Measure a KoboldCpp worker for text tariff calibration.

The text tariff prices a job from the prompt it sends and the text that comes back. Its rates are
calibrated from a backend's measured prefill and decode cost, so this tool drives a locally running
KoboldCpp instance and records what the tariff needs:

- ``tokens``: how the backend's tokenizer counts a range of text kinds, against the server's
  character-based estimates.
- ``prefill``: prefill seconds and decode seconds per token against prompt size, from prompts that
  share no prefix. Run the server with ``--nofastforward --noshift`` for this series.
- ``fastforward``: how much of a growing conversation is reprocessed, with and without an
  interleaved request from another requester.
- ``truncation``: how many prompt tokens the backend keeps when the prompt and the output do not fit
  the requested context.
- ``report``: fit the cost curves in a ``prefill`` result and derive tariff rates from them.

Standard library only. The HTTP calls go to ``/api/extra/tokencount``, ``/api/v1/generate`` and
``/api/extra/perf``. The perf endpoint's ``last_process_time`` multiplies a per-processed-token figure
by the full context count, so it overstates prefill whenever the backend reused its cache; the
``prefill`` series therefore relies on disjoint prompts and the server flags above, and the
``fastforward`` series reports wall-clock time instead.

Usage::

    python tools/text_tariff_bench.py tokens --corpus-dir docs --results results/
    python tools/text_tariff_bench.py prefill --corpus-dir docs --results results/ --label gpu-3090
    python tools/text_tariff_bench.py report results/prefill_gpu-3090.json
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import sys
import time
import urllib.request
from collections.abc import Sequence
from dataclasses import asdict, dataclass

STYLE_TEMPLATE = "### Instruction:\n{p}\n\n### Response:\n"
BILLING_CHARS_PER_TOKEN = 4
FIT_CHARS_PER_TOKEN = 3
DEFAULT_PROMPT_SIZES = (256, 512, 1024, 2048, 4096, 8192, 12288)
DEFAULT_OUTPUT_TOKENS = 128
MIN_PARAGRAPH_CHARS = 200


@dataclass(frozen=True)
class CostFit:
    """Represents the fitted prefill and decode cost of one backend.

    All times are seconds; ``prompt_tokens`` is the independent variable.
    """

    prefill_constant: float
    prefill_per_token: float
    prefill_per_token_squared: float
    prefill_r_squared: float
    prefill_power_law_exponent: float
    decode_per_token_constant: float
    decode_per_token_per_context_token: float
    decode_r_squared: float


@dataclass(frozen=True)
class DerivedRates:
    """Represents tariff rates in decode-token units, derived from a ``CostFit``.

    One unit is the cost of generating one token at an empty context. The input rates say how many
    units each prompt token costs to process; the context rate says how much each prompt token adds
    to the cost of every output token.
    """

    input_rate: float
    input_quadratic_rate: float
    context_decode_rate: float


def http(port: int, path: str, payload: dict | None = None, timeout: float = 3600) -> dict:
    """Return the JSON a KoboldCpp endpoint answers with.

    Args:
        port: The port KoboldCpp listens on.
        path: The endpoint path.
        payload: The JSON body to POST; a GET when omitted.
        timeout: Seconds to wait for the response.

    Returns:
        The decoded JSON response.
    """
    url = f"http://localhost:{port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def token_count(port: int, text: str) -> int:
    """Return the backend tokenizer's count for ``text``."""
    return int(http(port, "/api/extra/tokencount", {"prompt": text})["value"])


def generate(port: int, prompt: str, max_length: int, max_context_length: int) -> dict:
    """Run one greedy generation and return its timing and token counts.

    Args:
        port: The port KoboldCpp listens on.
        prompt: The prompt to send.
        max_length: Tokens to generate; EOS is banned so exactly this many come back.
        max_context_length: The context the request asks for.

    Returns:
        Wall-clock seconds, the response's prompt and completion counts, and the perf endpoint's
        last-generation figures.
    """
    payload = {
        "prompt": prompt,
        "max_length": max_length,
        "max_context_length": max_context_length,
        "temperature": 0.1,
        "top_k": 1,
        "rep_pen": 1.0,
        "ban_eos_token": True,
        "genkey": f"TARIFFBENCH{random.randint(1000, 9999)}",
    }
    started = time.monotonic()
    result = http(port, "/api/v1/generate", payload)
    wall = time.monotonic() - started
    perf = http(port, "/api/extra/perf")
    first = result["results"][0]
    return {
        "wall_seconds": round(wall, 3),
        "prompt_tokens": first.get("prompt_tokens"),
        "completion_tokens": first.get("completion_tokens"),
        "last_input_count": perf["last_input_count"],
        "last_token_count": perf["last_token_count"],
        "process_seconds": round(perf["last_process_time"], 3),
        "eval_seconds": round(perf["last_eval_time"], 3),
        "process_tps": round(perf["last_process_speed"], 2),
        "eval_tps": round(perf["last_eval_speed"], 2),
        "text_head": first["text"][:60],
    }


def prose_corpus(corpus_dir: pathlib.Path) -> list[str]:
    """Return prose paragraphs from the Markdown files under ``corpus_dir``.

    Tables, fenced code, comments, front matter and headings are skipped so the corpus is running
    text. Whitespace is collapsed so paragraph boundaries do not depend on wrapping.
    """
    paragraphs: list[str] = []
    for path in sorted(corpus_dir.rglob("*.md")):
        for block in path.read_text(encoding="utf-8", errors="replace").split("\n\n"):
            stripped = block.strip()
            if len(stripped) < MIN_PARAGRAPH_CHARS or stripped.startswith(("|", "```", "<!--", "---", "#")):
                continue
            paragraphs.append(" ".join(stripped.split()))
    return paragraphs


def build_prompt(paragraphs: Sequence[str], target_chars: int, seed: int) -> str:
    """Return about ``target_chars`` of corpus text in a shuffle fixed by ``seed``.

    Two prompts built with different seeds start with different paragraphs, so a backend cannot
    fast-forward from one to the other.

    Args:
        paragraphs: The corpus.
        target_chars: The length to assemble; the result is cut to exactly this many characters.
        seed: Fixes the shuffle.

    Returns:
        The prompt text.
    """
    if not paragraphs:
        raise ValueError("the corpus holds no paragraphs")
    rng = random.Random(seed)
    order = list(paragraphs)
    rng.shuffle(order)
    pieces: list[str] = []
    total = 0
    while total < target_chars:
        for paragraph in order:
            pieces.append(paragraph)
            total += len(paragraph) + 2
            if total >= target_chars:
                break
        rng.shuffle(order)
    return "\n\n".join(pieces)[:target_chars]


def trim_to_tokens(port: int, prompt: str, target_tokens: int, tolerance: float = 0.03) -> tuple[str, int]:
    """Cut ``prompt`` until the backend counts at most ``target_tokens * (1 + tolerance)`` tokens."""
    tokens = token_count(port, prompt)
    while tokens > target_tokens * (1 + tolerance):
        prompt = prompt[: int(len(prompt) * target_tokens / tokens)]
        tokens = token_count(port, prompt)
    return prompt, tokens


def tokenizer_samples(corpus_dir: pathlib.Path, source_files: Sequence[pathlib.Path]) -> dict[str, str]:
    """Return the text kinds the ``tokens`` series measures.

    Args:
        corpus_dir: Where the prose comes from.
        source_files: Source files to sample as code; the first 10,000 characters of each.

    Returns:
        Sample name to text.
    """
    paragraphs = prose_corpus(corpus_dir)
    prose = "\n\n".join(paragraphs[:12])
    code = "".join(path.read_text(encoding="utf-8", errors="replace")[:10_000] for path in source_files)
    chat_log = ""
    for index, paragraph in enumerate(paragraphs[12:24]):
        speaker = "User" if index % 2 == 0 else "Assistant"
        chat_log += f"{speaker}: {paragraph[:400]}\n"
    json_body = json.dumps(
        {
            "prompt": paragraphs[0] if paragraphs else "",
            "params": {"max_length": 512, "max_context_length": 8192, "temperature": 0.7, "top_p": 0.92, "stop_sequence": ["\n###"]},
            "models": ["koboldcpp/LLaMA2-13B-Tiefighter", "aphrodite/some-model"],
            "trusted_workers": False,
            "slow_workers": True,
        },
        indent=2,
    )
    non_latin = {
        "chinese": (
            "人工智能生成文本的成本取决于提示的长度和生成的标记数量。志愿者提供计算资源，社区以积分形式回报他们的努力。"
            "较长的上下文需要更多的内存和处理时间，因此定价应当反映这一点。"
        )
        * 4,
        "japanese": (
            "テキスト生成の費用は、プロンプトの長さと生成されるトークン数によって決まります。"
            "ボランティアは計算資源を提供し、コミュニティはその努力に対してクドスで報います。"
            "長い文脈はより多くのメモリと処理時間を必要とします。"
        )
        * 4,
        "russian": (
            "Стоимость генерации текста зависит от длины подсказки и количества создаваемых токенов. "
            "Добровольцы предоставляют вычислительные ресурсы, а сообщество вознаграждает их усилия баллами. "
            "Длинный контекст требует больше памяти и времени обработки."
        )
        * 4,
        "greek": (
            "Το κόστος παραγωγής κειμένου εξαρτάται από το μήκος της προτροπής και τον αριθμό των παραγόμενων διακριτικών. "
            "Οι εθελοντές προσφέρουν υπολογιστικούς πόρους και η κοινότητα ανταμείβει την προσπάθειά τους."
        )
        * 4,
        "emoji_mixed": "🎉🚀 The horde is alive! 🐎🔥 Workers ⚙️ serve models 🧠 and earn kudos 💰 for every job ✅. " * 12,
    }
    samples = {
        "english_prose": prose,
        "instruct_template": STYLE_TEMPLATE.format(p=paragraphs[30] if len(paragraphs) > 30 else prose[:800]),
        "chat_log": chat_log,
        "source_code": code,
        "json_request_body": json_body,
        **non_latin,
        "filler_text_x400": "filler text " * 400,
        "digit_run": " 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1" * 128,
    }
    return {name: text for name, text in samples.items() if text}


def tokenizer_row(name: str, chars: int, tokens: int) -> dict:
    """Return one ``tokens`` result row with the error of each server estimate against ``tokens``."""
    estimate_billing = math.ceil(chars / BILLING_CHARS_PER_TOKEN)
    estimate_fit = math.ceil(chars / FIT_CHARS_PER_TOKEN)
    return {
        "sample": name,
        "chars": chars,
        "tokens": tokens,
        "chars_per_token": round(chars / tokens, 3),
        "billing_estimate": estimate_billing,
        "billing_estimate_error_pct": round((estimate_billing / tokens - 1) * 100, 1),
        "fit_estimate": estimate_fit,
        "fit_estimate_error_pct": round((estimate_fit / tokens - 1) * 100, 1),
    }


def least_squares(rows: Sequence[Sequence[float]], targets: Sequence[float]) -> list[float]:
    """Return the ordinary least-squares coefficients for ``rows @ coefficients = targets``.

    Solves the normal equations by Gauss-Jordan elimination; the systems here have at most three
    unknowns.

    Raises:
        ValueError: When the normal matrix is singular, such as with fewer distinct points than unknowns.
    """
    unknowns = len(rows[0])
    matrix = [[sum(row[i] * row[j] for row in rows) for j in range(unknowns)] for i in range(unknowns)]
    vector = [sum(row[i] * target for row, target in zip(rows, targets, strict=True)) for i in range(unknowns)]
    for pivot_index in range(unknowns):
        pivot = matrix[pivot_index][pivot_index]
        if abs(pivot) < 1e-18:
            raise ValueError("singular system; supply more distinct prompt sizes")
        matrix[pivot_index] = [value / pivot for value in matrix[pivot_index]]
        vector[pivot_index] /= pivot
        for other_index in range(unknowns):
            if other_index == pivot_index:
                continue
            factor = matrix[other_index][pivot_index]
            matrix[other_index] = [a - factor * b for a, b in zip(matrix[other_index], matrix[pivot_index], strict=True)]
            vector[other_index] -= factor * vector[pivot_index]
    return vector


def r_squared(targets: Sequence[float], predictions: Sequence[float]) -> float:
    """Return the coefficient of determination of ``predictions`` against ``targets``."""
    mean = sum(targets) / len(targets)
    residual = sum((t - p) ** 2 for t, p in zip(targets, predictions, strict=True))
    total = sum((t - mean) ** 2 for t in targets)
    return 1.0 if total == 0 else 1 - residual / total


def fit_costs(prompt_tokens: Sequence[int], prefill_seconds: Sequence[float], decode_seconds_per_token: Sequence[float]) -> CostFit:
    """Return the cost curves fitted to one ``prefill`` series.

    Prefill is fitted as ``a + b * P + c * P^2`` and decode seconds per output token as ``d + e * P``.

    Args:
        prompt_tokens: Prompt size of each point.
        prefill_seconds: Prefill time of each point.
        decode_seconds_per_token: Decode time per generated token of each point.

    Returns:
        The fitted coefficients with their goodness of fit.
    """
    a, b, c = least_squares([[1.0, p, float(p) ** 2] for p in prompt_tokens], prefill_seconds)
    prefill_predicted = [a + b * p + c * p * p for p in prompt_tokens]
    log_intercept, exponent = least_squares([[1.0, math.log(p)] for p in prompt_tokens], [math.log(t) for t in prefill_seconds])
    d, e = least_squares([[1.0, p] for p in prompt_tokens], decode_seconds_per_token)
    decode_predicted = [d + e * p for p in prompt_tokens]
    return CostFit(
        prefill_constant=a,
        prefill_per_token=b,
        prefill_per_token_squared=c,
        prefill_r_squared=r_squared(prefill_seconds, prefill_predicted),
        prefill_power_law_exponent=exponent,
        decode_per_token_constant=d,
        decode_per_token_per_context_token=e,
        decode_r_squared=r_squared(decode_seconds_per_token, decode_predicted),
    )


def derive_rates(fit: CostFit) -> DerivedRates:
    """Return tariff rates from a cost fit, in units of one decode token at empty context.

    Args:
        fit: The fitted curves.

    Returns:
        The input, quadratic input and context-decode rates the tariff takes.
    """
    unit = fit.decode_per_token_constant
    return DerivedRates(
        input_rate=fit.prefill_per_token / unit,
        input_quadratic_rate=fit.prefill_per_token_squared / unit,
        context_decode_rate=fit.decode_per_token_per_context_token / unit,
    )


def run_tokens(args: argparse.Namespace) -> None:
    source_files = [pathlib.Path(path) for path in args.source_file]
    rows = [
        tokenizer_row(name, len(text), token_count(args.port, text))
        for name, text in tokenizer_samples(pathlib.Path(args.corpus_dir), source_files).items()
    ]
    _write(args, "tokens", rows)


def run_prefill(args: argparse.Namespace) -> None:
    paragraphs = prose_corpus(pathlib.Path(args.corpus_dir))
    rows = []
    for target_tokens in args.sizes:
        for repetition in range(args.repetitions):
            seed = target_tokens * 10 + repetition
            prompt, tokens = trim_to_tokens(args.port, build_prompt(paragraphs, int(target_tokens * 4.3), seed), target_tokens)
            measured = generate(args.port, prompt, max_length=args.output_tokens, max_context_length=args.context)
            measured.update({"target_tokens": target_tokens, "repetition": repetition, "tokenized_prompt": tokens})
            rows.append(measured)
            print(json.dumps(measured), flush=True)
    _write(args, "prefill", rows)


def run_fastforward(args: argparse.Namespace) -> None:
    paragraphs = prose_corpus(pathlib.Path(args.corpus_dir))
    rows = []

    def step(label: str, prompt: str, max_length: int = 64) -> dict:
        measured = generate(args.port, prompt, max_length=max_length, max_context_length=args.context)
        measured["step"] = label
        measured["prompt_tokens_true"] = token_count(args.port, prompt)
        rows.append(measured)
        print(json.dumps(measured), flush=True)
        return measured

    conversation = build_prompt(paragraphs, 2048 * 4, seed=101) + "\n\nUser: Summarise the above in one sentence.\nAssistant:"
    first = step("A_turn1_cold", conversation)
    conversation += first["text_head"] + " ...\nUser: And what is the main risk?\nAssistant:"
    step("A_turn2_growing", conversation)
    conversation += " The main risk is drift.\nUser: Give one example.\nAssistant:"
    step("A_turn3_growing", conversation)
    step("B_other_requester", build_prompt(paragraphs, 2048 * 4, seed=202) + "\n\nUser: What is this about?\nAssistant:")
    conversation += " One example is a stale cache.\nUser: Thanks. Anything else?\nAssistant:"
    step("A_turn4_after_B", conversation)
    step("A_turn4_repeat_same_prompt", conversation)
    _write(args, "fastforward", rows)


def run_truncation(args: argparse.Namespace) -> None:
    paragraphs = prose_corpus(pathlib.Path(args.corpus_dir))
    long_prompt = build_prompt(paragraphs, 6000 * 4, seed=404)
    short_prompt = paragraphs[0][:400]
    cases = [
        ("long_prompt_ctx4096_len256", long_prompt, 256, 4096),
        ("long_prompt_ctx_beyond_worker_len256", long_prompt, 256, 2 * args.context),
        ("short_prompt_ctx4096_len4000", short_prompt, 4000, 4096),
    ]
    rows = []
    for label, prompt, max_length, context in cases:
        measured = generate(args.port, prompt, max_length=max_length, max_context_length=context)
        measured.update(
            {
                "case": label,
                "true_prompt_tokens": token_count(args.port, prompt),
                "requested_max_length": max_length,
                "requested_ctx": context,
            },
        )
        rows.append(measured)
        print(json.dumps(measured), flush=True)
    _write(args, "truncation", rows)


def report(rows: Sequence[dict]) -> dict:
    """Return the fit and derived rates for a ``prefill`` result, as a JSON-ready mapping."""
    prompt_tokens = [int(row["last_input_count"]) for row in rows]
    prefill_seconds = [float(row["process_seconds"]) for row in rows]
    decode_per_token = [float(row["eval_seconds"]) / int(row["last_token_count"]) for row in rows]
    fit = fit_costs(prompt_tokens, prefill_seconds, decode_per_token)
    rates = derive_rates(fit)
    return {"fit": asdict(fit), "rates": asdict(rates), "points": len(rows)}


def run_report(args: argparse.Namespace) -> None:
    rows = json.loads(pathlib.Path(args.prefill_json).read_text())
    summary = report(rows)
    fit = summary["fit"]
    rates = summary["rates"]
    print(f"points: {summary['points']}")
    prefill_line = (
        f"prefill seconds = {fit['prefill_constant']:.3f} + {fit['prefill_per_token'] * 1000:.3f} ms * P"
        f" + {fit['prefill_per_token_squared'] * 1e6:.4f} us * P^2"
    )
    prefill_quality = f"(R2 {fit['prefill_r_squared']:.4f}, power law P^{fit['prefill_power_law_exponent']:.3f})"
    decode_line = (
        f"decode seconds per token = {fit['decode_per_token_constant'] * 1000:.2f} ms"
        f" + {fit['decode_per_token_per_context_token'] * 1e6:.3f} us * P   (R2 {fit['decode_r_squared']:.4f})"
    )
    rates_line = (
        f"tariff rates (decode-token units): input_rate {rates['input_rate']:.5f},"
        f" input_quadratic_rate {rates['input_quadratic_rate']:.3e}, context_decode_rate {rates['context_decode_rate']:.3e}"
    )
    print(f"{prefill_line}   {prefill_quality}")
    print(decode_line)
    print(rates_line)
    print(json.dumps(summary, indent=2))


def _write(args: argparse.Namespace, series: str, rows: list[dict]) -> None:
    results = pathlib.Path(args.results)
    results.mkdir(parents=True, exist_ok=True)
    out = results / f"{series}_{args.label}.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"wrote {out}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    def measurement(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--port", type=int, default=5001)
        sub.add_argument("--corpus-dir", default="docs", help="Markdown tree to draw prose from")
        sub.add_argument("--results", default="results", help="directory the JSON result is written to")
        sub.add_argument("--label", default="run", help="suffix of the result file")
        sub.add_argument("--context", type=int, default=16384, help="max_context_length to request; at most the worker's --contextsize")
        return sub

    tokens = measurement("tokens", "tokenizer counts against the server's estimates")
    tokens.add_argument("--source-file", action="append", default=[], help="source file to sample as code; repeatable")
    prefill = measurement("prefill", "prefill and decode cost against prompt size")
    prefill.add_argument("--sizes", type=int, nargs="+", default=list(DEFAULT_PROMPT_SIZES))
    prefill.add_argument("--repetitions", type=int, default=2)
    prefill.add_argument("--output-tokens", type=int, default=DEFAULT_OUTPUT_TOKENS)
    measurement("fastforward", "cache reuse on a growing conversation and after an interleaved requester")
    measurement("truncation", "how many prompt tokens survive when prompt plus output exceed the context")
    report_parser = subparsers.add_parser("report", help="fit the cost curves of a prefill result")
    report_parser.add_argument("prefill_json")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run the selected series."""
    args = build_parser().parse_args(argv)
    {
        "tokens": run_tokens,
        "prefill": run_prefill,
        "fastforward": run_fastforward,
        "truncation": run_truncation,
        "report": run_report,
    }[args.subcommand](args)


if __name__ == "__main__":
    main()
