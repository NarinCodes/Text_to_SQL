"""Evaluation harness — scores the agent by EXECUTING its SQL and comparing rows.

WHAT THIS DOES        Runs every dev question through the agent, executes the generated
                      SQL, compares returned rows to the gold result, prints accuracy
                      overall and by tier, and writes dev_answers.json.
DEPENDS ON            .agent (SQLAgent, AgentResult) and .utils (load_db). Nothing
                      depends on this module — it is a leaf, safe to change freely.
RUN IT                uv run python -m src.eval [--baseline]

KEY FUNCTIONS
  result_matches()  The heart of it. Executes nothing itself — takes a DataFrame and the
                    gold rows and decides "same answer?" Three deliberate tolerances:
                    order-insensitive multiset, floats rounded to 2dp, and projection
                    onto the gold columns.
  evaluate()        The loop. Note `agent.reset()` per question: every question is scored
                    standalone, so conversation context never inflates the score.
  summarize()       Turns rows into the human-readable string in dev_answers.json.
  _normalize()      Comparison formatting (2dp everywhere) — NOT display formatting.
                    ui._cell() is the display one. Keeping them separate is deliberate:
                    "21" reads wrong as "21.00", but 21 and 21.0 must compare equal.

TO EXTEND
  Point QUESTIONS_PATH at the new file. Each item needs `question` + `expected_result`
  (a list of row dicts); `tier` is only used for the per-tier breakdown. If the new set
  has no gold rows, you need a different oracle — say so rather than faking one.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

from .agent import AgentResult, SQLAgent
from .utils import load_db

DATA_DIR = Path("data")
QUESTIONS_PATH = DATA_DIR / "dev_questions_with_answers.json"
ANSWERS_PATH = Path("dev_answers.json")

# Floats in the gold results are money rounded to cents; compare at that precision.
FLOAT_PRECISION = 2
# Rows shown in the human-readable `answer` summary written to dev_answers.json.
SUMMARY_ROWS = 10


def _normalize(value: Any) -> str:
    """Reduce a cell to a comparable string, so 826.65 == 826.6500000000001.

    Everything becomes a string so that ints and floats compare equal when they mean
    the same number (21 vs 21.0), which SQLite's loose typing makes common.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    # `not isinstance(value, bool)` matters because in Python bool IS a subclass of
    # int — without it, True would be formatted as "1.00".
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{round(float(value), FLOAT_PRECISION):.{FLOAT_PRECISION}f}"
    return str(value).strip()


def _as_multiset(rows: list[tuple[str, ...]]) -> dict[tuple[str, ...], int]:
    """Count occurrences of each row: {row_tuple: how_many_times}.

    A multiset (bag) rather than a set, because duplicate rows are meaningful — two
    different playlists both named "Music" with 0 tracks must not collapse into one.
    """
    counts: dict[tuple[str, ...], int] = {}
    for row in rows:
        counts[row] = counts.get(row, 0) + 1
    return counts


def result_matches(predicted: pd.DataFrame | None, expected: list[dict]) -> bool:
    """Compare predicted rows to the gold rows. THE most important function here.

    Why not compare SQL text: many different queries answer the same question. Gold
    groups by `g.Name`; the agent groups by `g.GenreId, g.Name`, which is arguably more
    correct. String comparison calls that wrong. So we execute and compare ROWS.

    Three deliberate tolerances, each earning its place:

    1. ORDER-INSENSITIVE MULTISET. Several dev questions have tied values whose ordering
       is not deterministic (24 countries, many at exactly 5.37). Compared as a bag, so
       sequence is ignored but duplicates still have to match — q_006 returns two
       distinct playlists both named "Music", and collapsing them would be wrong.
    2. FLOATS ROUNDED TO 2dp. Money. 826.6500000000001 == 826.65.
    3. PROJECTION ONTO THE GOLD COLUMNS. If the agent returns extra columns, the gold
       columns must all be present and correct. Names matched first, falling back to
       position — TotalSpending vs TotalSpent is an alias difference, not an error.

    Worked examples:
        gold [{"Name": "Rock", "Total": 826.65}]
          predicted Name,Total          -> True
          predicted Total,Name          -> True   (names match, projected into order)
          predicted Id,Name,Total       -> True   (extra column ignored)
          predicted Genre,Revenue       -> True   (2 cols, positional fallback)
          predicted Name only           -> False  (gold column missing)
          rows in a different order     -> True   (multiset)
          the same row twice            -> False  (multiset counts duplicates)

    WHERE THIS IS WEAK:
    * The positional fallback is a guess. Two same-width results with genuinely
      different columns would compare equal. It has not bitten us, but it could.
    * It cannot judge a question with no gold answer, so it does not scale to
      production traffic without a different oracle.
    * It says nothing about whether the SQL was *sensible* — only whether the rows
      match. q_003 fails here on column shape while the values are perfectly correct.
    """
    if predicted is None:
        return False  # every attempt failed
    if not expected:
        return predicted.empty  # gold expects no rows

    gold = pd.DataFrame(expected)

    if set(gold.columns).issubset(predicted.columns):
        # Best case: every gold column name is present. Select just those, in gold's
        # order, so extra columns the agent added are ignored rather than penalised.
        projected = predicted[list(gold.columns)]
    elif len(predicted.columns) == len(gold.columns):
        # Names differ but the shape matches — an alias difference such as
        # TotalSpending vs TotalSpent. Compare positionally.
        projected = predicted
    else:
        # Different number of columns and names do not line up: genuinely wrong shape.
        return False

    # Convert both frames to lists of stringified row tuples, then compare as bags.
    to_rows = lambda df: [tuple(_normalize(v) for v in row) for row in df.itertuples(index=False)]
    return _as_multiset(to_rows(projected)) == _as_multiset(to_rows(gold))


def _display(value: Any) -> str:
    """Format one cell for a human to read.

    Deliberately different from _normalize(), which pads every number to two decimals
    so that 21 and 21.0 compare equal. That is right for grading and wrong for reading:
    a count of 21 customers should not appear as "21.00".
    """
    if value is None:
        return ""
    # numpy scalars (int64/float64) come out of a DataFrame and are not always
    # instances of Python's int/float, so normalise them to native types first.
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        if pd.isna(value):
            return ""
        number = float(value)
        # Whole numbers print as integers with thousands separators (3034 -> "3,034");
        # genuine decimals keep cent precision.
        if number.is_integer():
            return f"{int(number):,}"
        return f"{round(number, FLOAT_PRECISION):,.{FLOAT_PRECISION}f}"
    return str(value).strip()


def summarize(result: AgentResult) -> str:
    """Human-readable one-liner describing the rows, for dev_answers.json.

    Follows the phrasing of the gold answers: the first column carries the
    row and any remaining columns follow in parentheses.

        one column   -> "For Those About To Rock We Salute You, Let There Be Rock"
        two columns  -> "Rock (826.65), Latin (382.14)"
        three        -> "Helena Holy (49.62, 1)"

    Units and currency symbols are not inferred — a column name cannot reliably tell
    us that a number is dollars rather than a count, and inventing "$" would be a
    fabrication.
    """
    if not result.ok:
        return f"FAILED: {result.error}"
    if result.rows.empty:
        return "No rows returned."

    head = result.rows.head(SUMMARY_ROWS)
    parts = []
    # itertuples preserves each column's own dtype; iterrows would upcast a mixed
    # int/float row to float and reintroduce the "21.00" problem.
    for row in head.itertuples(index=False):
        cells = [_display(v) for v in row]
        parts.append(cells[0] if len(cells) == 1 else f"{cells[0]} ({', '.join(cells[1:])})")

    rendered = ", ".join(parts)
    remaining = len(result.rows) - len(head)
    return rendered + (f", ... and {remaining} more" if remaining else "")


def evaluate(run_baseline: bool = False) -> None:
    """Run every dev question, score it, and print a report.

    Side effect: writes dev_answers.json.
    """
    load_dotenv()
    questions = json.loads(QUESTIONS_PATH.read_text())
    conn = load_db()
    agent = SQLAgent(conn=conn)

    answers: dict[str, dict[str, str]] = {}
    latencies: list[float] = []
    by_tier: dict[int, list[bool]] = {}  # tier -> [was_correct, ...]
    correct = baseline_correct = 0

    for item in questions:
        # Each question is scored standalone; conversation context is a CLI feature.
        # Without this reset, question N could be answered using question N-1's
        # context, which would inflate the score against a single-turn benchmark.
        agent.reset()
        result = agent.ask(item["question"])
        is_correct = result_matches(result.rows, item["expected_result"])

        # bool is a subclass of int in Python, so `correct += is_correct` adds 1 or 0.
        correct += is_correct
        latencies.append(result.elapsed_s)
        by_tier.setdefault(item["tier"], []).append(is_correct)
        answers[item["id"]] = {"sql": result.sql or "", "answer": summarize(result)}

        status = "PASS" if is_correct else "FAIL"
        line = (
            f"{item['id']} [tier {item['tier']}] {status}  "
            f"{result.elapsed_s:5.2f}s  attempts={result.attempts}"
        )
        if run_baseline:
            # Same model, naive prompt — isolates agent design from
            # model choice, so the reported lift cannot be explained away.
            baseline = agent.ask_naive(item["question"])
            hit = result_matches(baseline.rows, item["expected_result"])
            baseline_correct += hit
            line += f"  | baseline {'PASS' if hit else 'FAIL'}"
        print(line)
        # Print diagnostics inline for failures so the harness is usable for debugging,
        # not just scoring. chr(10) is a newline — f-strings cannot contain backslashes.
        if not is_correct:
            print(f"    question: {item['question']}")
            print(f"    sql:      {(result.sql or '').replace(chr(10), ' ')}")
            print(f"    error:    {result.error or 'wrong result'}")

    ANSWERS_PATH.write_text(json.dumps(answers, indent=2) + "\n")

    total = len(questions)
    print(f"\nAgent accuracy: {correct}/{total} ({correct / total:.0%})")
    for tier in sorted(by_tier):
        hits = by_tier[tier]
        print(f"  tier {tier}: {sum(hits)}/{len(hits)}")
    if run_baseline:
        print(f"Baseline accuracy: {baseline_correct}/{total} ({baseline_correct / total:.0%})")

    # Percentiles over only 10 samples are indicative, not reliable — P95 especially.
    ordered = sorted(latencies)
    p50 = statistics.median(ordered)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    print(f"Latency: P50 {p50:.2f}s  P95 {p95:.2f}s  (target: P50 < 3s)")
    print(f"Wrote {ANSWERS_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Score the agent on the dev questions.")
    parser.add_argument(
        "--baseline",
        action="store_true",  # presence of the flag sets it True; no value needed
        help="also run the naive baseline prompt for comparison",
    )
    evaluate(run_baseline=parser.parse_args().baseline)


if __name__ == "__main__":
    main()
