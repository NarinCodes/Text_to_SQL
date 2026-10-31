# Agents.md — Text-to-SQL CLI Agent

Guidance for AI coding agents (and humans) working in this repository. Read this
before making changes. It defines **what we are building, why, the architecture, the
quality levers, conventions, and the definition of done.**

---

## 1. Project goal

Build an **interactive command-line text-to-SQL agent**: a user launches a terminal
session, points it at a database, and asks questions in natural language; the agent
generates SQL, executes it, and returns the query **plus** the results — and supports
follow-up questions to refine or explore. It must be **demonstrably better than a naive
`"Convert this question to SQL: {question}"` prompt** in correctness and reliability.

Reference database: **Chinook** — a SQLite database modelling a digital music store
(11 tables: `Artist`, `Album`, `Track`, `Genre`, `MediaType`, `Customer`, `Employee`,
`Invoice`, `InvoiceLine`, `Playlist`, `PlaylistTrack`). The design should not hard-code
to Chinook — the agent learns the schema by introspection, so it works on any SQLite DB.

Inference runs on **open-weight models served by Fireworks AI** (OpenAI-compatible API),
chosen to hit three product constraints at once:

- **Quality:** valid, executable SQL with no hallucinated tables/columns or wrong JOINs.
- **Latency:** interactive feel — target **< 3s P50 end-to-end** (question → results).
- **Cost:** open-weight models instead of frontier proprietary ones, to make per-query
  economics work at scale (order 10⁴–10⁵ queries/day).

**Guiding principles (in priority order):**
1. **Correctness of results** over exact SQL syntax. Two different queries that return
   the same rows are both correct.
2. **Clean, readable, easy-to-explain code.** Every major decision should be easy to
   justify. Avoid over-engineering.
3. **Robustness:** handle SQL that fails to execute, empty results, and ambiguous
   questions gracefully — never dump a raw stack trace at the user.
4. **The interface is part of the product.** The terminal experience is a core
   feature, not decoration. Concretely: never leave the
   user staring at a silent cursor during a slow call (show a spinner); give SQL,
   results and provenance three distinct levels of visual weight rather than one; format
   data *as data* (right-aligned numerics, thousands separators, an explicit NULL
   glyph); surface self-repair when it fires, both mid-flight and in the footer, rather
   than hiding the most interesting thing the agent does; and never truncate without an
   escape hatch — cap the display, report the true count, and offer a paged `more`.
5. **Evidence it works:** validate against the dev set and know where it still
   fails.

---

## 2. What "correct" means

Grade a generated query by **executing it and comparing the returned rows to a
gold/expected result set**, order-insensitively and type-normalized:

- Normalize each value: round floats to 2 dp, stringify, map `None`/NaN to `""`.
  (`bool` is a subclass of `int` in Python — exclude it explicitly or `True` formats
  as `1.00`.)
- **Project** the predicted frame onto the gold columns: match by column *name* first,
  falling back to *position* when names differ but the column count matches. Extra
  columns the agent returned are ignored; a differing column count is a mismatch. This
  is what stops an alias difference (`TotalSpending` vs `TotalSpent`) reading as wrong.
- Compare the two row lists as **multisets** — order-insensitive, duplicates significant.
- Match → score `1`, else `0`. Accuracy = matches ÷ N over the dev set.

Do **not** sort values *within* a row: that makes column order irrelevant and would
match `(Rock, 826.65)` against `(826.65, Rock)`.

Don't assume results are non-empty, and remember correct queries may legitimately
include **zero-valued rows** (e.g. `LEFT JOIN` groups with no matches) and repeated group
labels. Float rounding matters (monetary sums like `826.65`).

This "execution-result match" is the evaluation metric (it mirrors how the dev
set is scored: `evaluation: "sql_result_match"`). It is also why we don't grade on SQL
string similarity — there are many correct queries for one question.

---

## 3. Architecture

```
                ┌──────────────────────────────────────────────┐
                │  Schema card (built once by introspection):   │
                │  tables, columns+types, PK/FK relationships)  │
                └──────────────────────────────────────────────┘
                                   │ grounds every prompt
                                   ▼
user question ──▶ prompt builder ──▶ Fireworks LLM ──▶ extract SQL
   (+ chat history for follow-ups)                        │
                                                          ▼
                                              ┌──────────────────────┐
                                              │  Execute (SQLite,     │
                                              │  read-only guard)     │
                                              └──────────────────────┘
                                                 │ ok            │ error
                                                 ▼               ▼
                                          format + show    self-repair: feed
                                          SQL + results    error+SQL back to
                                          in the CLI       LLM, retry (≤2)
```

Turn loop: build prompt (schema card + conversation history + question) → generate SQL
→ execute → on error, **self-repair** up to 2 attempts → format results → print SQL and
a readable table → keep the exchange in history for follow-ups.

Read-only is enforced by inspecting the SQL, not by the connection: extraction keeps only
the text before the first `;` (so a trailing `DROP TABLE` is unreachable), then the query
must match `^(SELECT|WITH)` and contain no write keywords. Verified by stubbing the model
to return `SELECT 1; DROP TABLE Artist` — only `SELECT 1` executes, table count unchanged.
A `mode=ro` URI connection would make the whole class moot and is the obvious hardening.
**No statement timeout is implemented** — that is a gap, not a feature.

---

## 4. Quality levers (how we beat the baseline)

These are the concrete reasons the agent outperforms the naive prompt. Build them in
this order; each is independently valuable.

1. **Schema grounding (biggest lever).** Introspect the DB and inject a compact **schema
   card** — every table, its columns and types, primary keys, and **foreign-key
   relationships** — into the system prompt. This kills the top failure modes
   (hallucinated columns, wrong/invented JOINs). FK edges are the single highest-value
   addition for join-heavy questions.
2. **A disciplined system prompt.** Target dialect = **SQLite**; output exactly one
   read-only `SELECT`; use only names present in the schema; no markdown, no prose.
3. **Reliable SQL extraction.** Strip code fences / stray text so what we execute is
   pure SQL. **Measured, then declined:** a pydantic-validated
   `{"sql": ...}` structured output was A/B'd against raw extraction over the dev set —
   identical accuracy (9/10 both), identical latency, no token overhead, zero parse
   failures either way. Not adopted, because it removes only the fence-stripping: the
   semicolon split is a safety boundary and the read-only guard is needed regardless,
   and depending on `response_format` narrows model portability. Revisit when the
   response needs a *second* field (confidence, tables consulted, an explanation).
4. **Execution-guided self-repair.** If SQLite raises an error (bad column, syntax,
   ambiguous name), feed the failing SQL **and the error text** back to the model for a
   bounded retry (≤2). Directly addresses "what happens when the SQL doesn't execute."
   **The retry prompt must restate the goal.** A repair message of the form "that query
   failed with {error}; return a corrected statement" becomes the *most recent
   instruction* and silently redefines the task as "fix the syntax" — the original
   question is by then several messages back. Measured: a follow-up whose constraint came
   from an earlier turn was kept 6/6 on a clean first attempt and **0/6 after two repair
   rounds**, returning 76 artists where 5 were expected while every internal signal
   reported success. Adding `Fix it so that it answers this question: {question}` to the
   repair prompt restores 6/6. Generalises to any retry loop over an LLM: **the retry
   prompt is the task definition.**
   **Know the remaining blind spot:** repair converts *crashes* into answers and is
   completely blind to valid SQL that returns wrong rows. Only *always displaying the SQL*
   protects the user from that class.
5. **Conversation context.** Keep prior questions/SQL in the message history so
   follow-ups ("now just for 2021", "rank them") resolve against what came before.
   Store the SQL rather than the rows — compact, and it is what a follow-up modifies.
   Persist **successful turns only**, and cap the window (4 turns) so prompt cost and
   stale-context risk stay bounded.
6. **Output-shape rules.** Beyond correctness, the prompt constrains presentation: no id
   columns unless asked, one display-name column when the question asks *which* entity,
   and — for any superlative — the value being ranked by, as its own column.
   **Why that last one is not cosmetic:** asked "which song sold the most in 2024?" the
   agent returned a single name. The SQL was correct; the answer was misleading.
   `InvoiceLine.Quantity` is `1` for all 2,240 Chinook rows, so 447 tracks tie and
   `LIMIT 1` picks an arbitrary winner. Nothing errors and the query faithfully
   implements the question — the question has no unique answer. Returning "Fast As a
   Shark, 1" makes that visible instantly; returning "Fast As a Shark" does not. This is a
   fourth failure shape — *correct SQL, misleading answer* — sitting between "wrong
   scope" and "misread intent". Detecting the tie and **saying so** is deliberately not
   solved by prompting: that would fit one dataset's quirk rather than the class, and it
   is the clearest argument for a QA stage in a multi-agent design.
   These lifted the dev score 8/10 → 9/10 — but they were derived from the shape of the
   gold answers, so they are fitted to this dev set and need re-validating on held-out
   questions. Record that honestly rather than quoting 9/10 unqualified.

**Ablate before believing a prompt fix.** A rule telling the model to carry prior filters
forward *looked* like it fixed the dropped-constraint bug, on the strength of one run.
Controlled testing (6 runs per condition, one variable changed) showed it did nothing —
6/6 without it, 5/6 with — and the real cause was the repair prompt. The rule was removed.
Prompt text is not free: it cost ~59 tokens on every call for no measurable benefit.

Deliberately **light-touch** semantic hints (a couple of synonyms / metric definitions,
e.g. "sales/revenue" → `SUM(UnitPrice*Quantity)`) are fine if they help specific dev
questions, but the schema card + FK edges do most of the work. Don't build a heavy
semantic-model framework for a project this size.

---

## 5. Components

| Component | Responsibility | Status |
|---|---|---|
| `utils` | `load_db`, `query_db`, `get_schema`, `print_table_schema` over SQLite. Reuse these. | build |
| `schema card` | Render `get_schema` output + FK edges (`PRAGMA foreign_key_list`) into a compact prompt block. | build |
| `agent` | `NL question (+history) → SQL → execute → self-repair → result`. The core logic. | build |
| `cli` | Interactive REPL: read question, dispatch commands, hand questions to the agent. Control flow only. Runnable via `uv run cli`. | build |
| `ui` | All terminal rendering: banner, syntax-highlighted SQL panel, result tables, error panels, `help`/`schema`/`more` browsers. Keeps presentation out of both the loop and the agent, so a `--json` mode touches one file. | build |
| `inference` | Thin Fireworks client via the **OpenAI SDK** (`base_url=https://api.fireworks.ai/inference/v1`). Model from env. | build |
| `eval harness` | Run the agent over the 10 dev questions, compare to `expected_result`, report accuracy by tier, write `dev_answers.json`, and compare vs the naive baseline. | build |

---

## 6. Evaluation & metrics

- **Primary:** result-match accuracy over the 10 dev questions
  (`data/dev_questions_with_answers.json` has `gold_sql` + `expected_result`), reported
  **overall and by tier** (tier 1 = simple filters/aggregates, tier 2 = joins/grouping,
  tier 3 = window functions / `HAVING`).
- **Baseline comparison:** run the naive baseline prompt through the same harness to
  quantify the lift (this is the "demonstrably better" evidence).
- **Latency:** measure end-to-end P50/P95 per question (question → results), against the
  **< 3s P50** target. (P50 = median — the typical case; P95 = 95th percentile — the slow
  tail, which is where a self-repair round-trip shows up. Report both, not the average.)
- **Output artifact:** `dev_answers.json` at the **project root** (not `data/`) —
  an object keyed by `q_00x`, each holding the generated `sql` and a human-readable
  `answer` summary. Format the summary for *reading*, not
  for machine comparison: whole numbers as integers with thousands separators (`3,034`, not
  `3034.00`), first column then remaining columns in parentheses. Use a display
  formatter distinct from the comparison normalizer, and iterate rows with
  `itertuples()` — `iterrows()` upcasts a mixed int/float row to float.

**Measured results (stable across repeated runs):**

| Metric | Agent | Naive baseline |
|---|---|---|
| Accuracy | **9/10** (t1 3/4, t2 4/4, t3 2/2) | **1/10** |
| Latency P50 | **1.15s** (target < 3s) | — |
| Tokens/query | 772 in / 54 out | — |

The single failure is presentation, not logic: for "names and email addresses" the agent
returns one concatenated display-name column where gold splits first and last. Values
identical. Further prompt tuning there would be fitting to the answer key.

**The harness calls `agent.reset()` before every question**, so all ten are scored as
independent single-turn requests — deliberate, since the dev questions are not a
conversation and carrying history would make the score depend on question order. The
consequence: **follow-up handling has zero automated coverage** and is verified only by
hand. A multi-turn eval set (seed question + scripted follow-ups with their own gold
results) is the fix.

---

## 7. Model choice (quality vs latency vs cost)

Pick an **open-weight** model from the Fireworks library and **verify the exact ID is
available to your account** — IDs vary by account and change over time; an unavailable or
undeployed ID returns HTTP `404 NOT_FOUND`. Enumerate what you can actually call with the
OpenAI SDK: `client.models.list()`.

- **Prefer a fast model that emits raw SQL directly.** Verified default:
  **`deepseek-v4-flash-0731`** — ~1.1s per call, returns clean executable SQL, comfortably
  under the interactive latency target.
- **⚠️ Reasoning-model gotcha (real):** several catalog models are
  "reasoning/thinking" style and return **empty `content` at a small `max_tokens`** because
  the budget is spent on hidden reasoning tokens. Either give a large token budget and read
  their reasoning output, or — better for a latency-bound CLI — pick a non-reasoning model
  that returns SQL directly. Always assert the response is non-empty before executing.
- **Latency-bound:** a smaller/faster model; **quality-bound:** a larger instruct model —
  each trades against the other on tier-3 (window/`HAVING`) accuracy.

- **⚙️ Biggest single lever found: turn reasoning OFF.** For a task this constrained the
  hidden chain-of-thought is wasted work billed at the output rate. Setting
  `reasoning_effort="none"` cut median latency **6.15s → 1.20s** and completion tokens
  **574.8 → 53.7 (10.7×)** with **no accuracy change**. Measure this as an explicit axis
  rather than accepting the default. Send it defensively: catch `BadRequestError` once,
  drop the parameter, and stop sending it, so the agent still runs against models that
  reject it.

Use temperature 0 for determinism; keep the happy path to **one** LLM call (self-repair
adds a second only on failure).

**Do not set `max_tokens` small.** The earlier advice to keep it "modest" is wrong for
reasoning models and caused a real failure: at `max_tokens=512` the model spent the entire
budget thinking and returned **empty content with `finish_reason == "length"`**, which
surfaces as a baffling "no SQL produced". Use 2048, and assert explicitly on that
condition so the error message names the cause.

### 7b. Unit economics

Price with *measured* tokens, not estimates. At 772 in / 54 out per query and a reference
30k queries/day workload:

| Configuration | Per month | Per user/month |
|---|---|---|
| Reasoning on, no caching | $502 | $0.50 |
| **Reasoning off — as shipped** | **$187** | **$0.19** |
| Reasoning off + prompt caching | $40 | $0.04 |

Against frontier proprietary pricing for the same architecture, open-weight serving is
**~13× cheaper** (a $2.50/M-in, $15/M-out model lands near $2,498/month on this workload).
Two caveats worth stating rather than hiding:

- **The prompt is 98% cacheable** — a fixed 757-token prefix vs a ~15-token question — and
  cached input is priced ~31× below uncached. But caching is per *warm prefix*, and each
  tenant database has its own schema and therefore its own prefix. At 30 queries/day/user a
  prefix is touched once per ~48 min and may never stay warm. Treat the cached figure as a
  **ceiling on the saving, not a forecast**, until hit rates are measured.
- **The cheapest proprietary tier is near cost parity** (~$202/month). The argument for
  open weights there is *quality at that price point* plus optionality — fine-tuning on
  execution-result reward, self-hosting, no exposure to unilateral pricing — not raw cost.

Configuration is a first-class cost lever: disabling reasoning saved more than a 2.7×
vendor discount would have.

---

## 8. Environment & setup

- **The database is generated, not shipped.** `./setup.sh` downloads the official Chinook
  SQL dump (`lerocha/chinook-database` on GitHub) and builds `data/Chinook.db` via
  `sqlite3`. Prerequisites checked by the script: **`uv`, `curl`, `sqlite3`** (it aborts if
  any is missing). Sanity check after building: 11 tables, ~275 artists / ~3.5k tracks /
  ~412 invoices.
- **Run sequence:**
  ```bash
  ./setup.sh                 # build data/Chinook.db
  uv sync                    # install deps into .venv
  cp .env.example .env       # then set FIREWORKS_API_KEY (+ FIREWORKS_MODEL)
  uv run cli                 # or: python -m src.cli
  ```
- **Config via `.env`** (gitignored), loaded with `python-dotenv`. **Call `load_dotenv()`
  at startup**. `.env.example` is the committed template with
  placeholder values; the two coexist (never commit `.env`). Vars: `FIREWORKS_API_KEY`,
  `FIREWORKS_MODEL`.
- **Fireworks is OpenAI-compatible:**
  ```python
  import os
  from openai import OpenAI
  client = OpenAI(base_url="https://api.fireworks.ai/inference/v1",
                  api_key=os.environ["FIREWORKS_API_KEY"])
  client.chat.completions.create(model=os.environ["FIREWORKS_MODEL"],
                                 messages=[...], temperature=0)
  ```
- **Smoke-test before building.** One chat completion must return before writing agent
  code — it confirms the key is valid and the account is **activated** (activation can be
  gated on a payment method even when credits are present; a valid key that still errors on
  a real model is the tell).

---

## 9. Conventions

- **Language / tooling:** Python ≥3.11, dependency management with **uv** (`uv sync`,
  `uv run cli`). Dependencies: `openai`, `pandas`, `pydantic`, `python-dotenv`,
  `ipython`, plus **`rich`** for terminal rendering. Add a dependency only when it
  *reduces* net code — `rich` replaces ~150 lines of hand-rolled ANSI, table layout and
  spinner logic with ~40.
- **Separate presentation from logic.** The agent must know nothing about a terminal and
  the REPL must hold no agent logic. This is what keeps the agent reusable by the eval
  harness and would make a `--json` or `--quiet` mode a single-file change.
- **Secrets:** only via `.env` / environment (`FIREWORKS_API_KEY`, `FIREWORKS_MODEL`).
  **Never** hardcode keys. `.env` is gitignored; a pre-commit hook in `.githooks/`
  blocks accidental secret commits.
- **SQL safety:** execute **read-only** — a single `SELECT`; reject writes/DDL; set a
  statement timeout; treat empty results as a valid outcome, not a crash.
- **Refuse out-of-scope questions; never answer from model knowledge.** A read-only guard
  protects the *database*; this protects the *user*. Asked "what is the capital of
  Nigeria?", an unguarded agent emits `SELECT 'Abuja' AS CapitalOfNigeria` — valid SQL
  that executes cleanly and renders in the results table **indistinguishable from real
  data**. For a BI tool that is worse than a crash. Two layers: a prompt sentinel
  (`NOT_A_DATA_QUESTION`, ~63 tokens, no extra call) and a **structural backstop** —
  reject any query that reads from no table in the schema, because a question *about the
  data* must read the data. The backstop needs no cooperation from the model and runs
  before execution. Measured 0 false positives / 0 false negatives over 20 in-scope and
  10 out-of-scope questions; a false positive (refusing a real question) is the worse
  error, which is why the backstop is structural rather than keyword-matching. Declined
  turns must not enter conversation history — otherwise the rejected literal leaks into
  the next question's context.
- **Guards misfire in both directions; measure both.** Two false positives found by a
  user exercising the CLI, neither caught by the author's own test set:
  (a) the scope rule was **over-broad** — Chinook sells *tracks*, not albums, so "how many
  albums did Iron Maiden sell?" has no direct mapping and the model treated an ambiguous
  *metric* as an out-of-scope *question*. Reserve refusal for what has no connection to
  the data, and ask for a reasonable interpretation when the entities are in the schema
  (3/3 declined before, 0/3 after). (b) the repeat-detection backstop rejected a
  legitimate restatement — "how many albums did **this band** sell?" then the explicit
  "…did **Iron Maiden** sell?" produce identical SQL by design. Scope that check to
  messages of ~3 words or fewer, which is what stray input actually looks like.
  **The general lesson: a guard measured against examples its own author wrote will
  understate its false-positive rate**, because they are phrased the way the author
  phrases questions. Same argument as held-out eval sets, applied to guards.
- **Handle uninterpretable input as its own state.** A stray word ("rest", "ok") is
  neither a data question nor a general-knowledge one, and an LLM's fallback is to
  **silently re-run the previous query** — a plausible table with no signal the input was
  never understood. A prompt rule alone was measured unreliable (clean at 9/9, then leaked
  in a longer session), so pair it with a deterministic check comparing generated SQL
  against the previous turn's and rejecting an exact match. Surface it as a *rephrase*
  prompt, not a scope refusal: the failure is comprehension. Guard the obvious regression
  — real follow-ups ("now just for 2021") are also short and context-dependent, so verify
  they still pass.
- **Error handling:** structured, user-facing messages; the self-repair path owns retry
  logic; never surface a raw traceback in the CLI — *including at startup*. A missing
  database file or absent API key are the first things a new user hits; catch them around
  construction and exit with one plain line, not a stack trace.
- **⚠️ Mind the exception contract across library boundaries.** `pandas.read_sql_query`
  catches SQLite's exception and re-raises `pandas.errors.DatabaseError`, which is *not* a
  subclass of `sqlite3.Error`. An `except (sqlite3.Error, ...)` retry loop therefore never
  fires — the repair path was silently dead until this was found. Keep one shared tuple
  (`EXECUTION_ERRORS`) so every call site agrees, and **cover any retry path with a test
  that actually forces the failure** (inject a stub client returning broken SQL). A retry
  loop nobody has seen fire is a retry loop that does not work.
- **Dependency injection for testability:** let the LLM client be passed in and default to
  `None`, building a real one only when absent. That single choice is what makes the repair
  loop testable without a network.
- **Schema-agnostic:** learn the DB by introspection; don't hard-code Chinook table or
  column names in the agent logic.
- **Explainability:** small, well-named functions; comment where a *decision* was made
  (why schema grounding, why result-match, why self-repair), not the obvious.

---

## 10. Definition of done

1. `uv run cli` (and `python -m src.cli`) launches an interactive session that takes a
   question, shows the generated SQL and results, supports follow-ups, offers `help`
   and `schema`, and exits on `exit`/`quit`.
2. The agent grounds on the introspected schema and recovers from a failed query via
   bounded self-repair.
3. The eval harness reports result-match accuracy on the 10 dev questions (overall + by
   tier) and quantifies the lift over the naive baseline; `dev_answers.json` is produced.
4. Latency is measured against the < 3s P50 target.
5. The `README` documents exact run steps and required env vars; secrets and private
   materials are not committed.

**Status: met.** 9/10 result-match vs 1/10 for the naive baseline
on the same model, P50 1.15s, `dev_answers.json` at the project root, repair loop covered by
a stub-client test, read-only guard verified against an injection attempt. Known gaps
recorded rather than smoothed over: one presentational miss, two prompt rules fitted to the
dev set, no automated coverage of conversation handling, unstable P95.

---

## 10b. Optimise for explainability, not polish

The system works and is measured. From here, **every change should be optimised for being
understood and extended**, not for additional polish. A feature nobody can
explain is worth less than one that is smaller and fully understood.

**Each `src/*.py` opens with an orientation header** — what the module does, what it
depends on, what depends on it, the functions that matter most, and how to extend it. Read
those before the code, and keep them accurate: they are the map, and a stale map is worse
than none.

**The most valuable findings came from *using* the tool, not testing it** — the
retry-prompt bug, both guard false positives, the 447-way tie. Each has a measurement
behind it. A test suite written by the author of the code will systematically miss this
class of problem; budget time for unscripted use.

## 11. Next steps / scaling (out of scope for now)

Directly motivated by the cost/latency/scale constraints:

- **Fine-tune a small open model (RFT)** on execution-result reward so a 7B-class model
  matches larger ones on this schema — lower cost and latency at scale.
- **Self-hosted serving (vLLM/SGLang)** on dedicated GPUs for steady high volume:
  paged-attention/continuous batching for throughput, control over latency and $/query;
  the managed-vs-self-hosted crossover is utilization-driven.
- **Richer semantic layer** (Cortex Analyst-style: curated metrics, synonyms, verified
  queries) as schemas grow.
- **Guardrails & ops:** read-only *connections* (`mode=ro`) beneath the SQL guard,
  statement timeouts, query cost/row limits, a larger eval set with CI, latency/quality
  monitoring and drift tracking.
- **Validate prompt-cache hit rates per tenant** before promising the cached cost figure —
  see §7b. This is the highest-value cost measurement still outstanding.
- **A multi-turn eval set** so follow-up handling stops being verified only by hand.
- **Adopt an agent framework if and when operations demand it** — see §12.

---

## 12. Non-goals

- No write/DDL support — read-only analytics only.
- No multi-tenant web UI, auth, or orchestration platform in this project.
- **No agent framework (LangChain / LangGraph) — a considered choice, not an oversight.**
  Both map cleanly onto this problem (`SQLDatabase.get_table_info()`, a LangGraph
  conditional edge from *execute* back to *generate* is exactly this repair loop). At ~260
  lines the framework would replace ~40 of them with configuration while adding indirection
  over control flow that has to be explained, and `get_table_info()`'s
  DDL-plus-sample-rows default is several times the tokens of the hand-built schema card —
  which matters once tokens drive both latency and cost. The calculus flips on *operational*
  needs, not on SQL generation: per-request tracing, checkpointing/resumability,
  human-in-the-loop approval for expensive queries, or genuine branching tool use.
- Self-repair is bounded (≤2 retries); no unbounded agentic loops.
- Don't hard-code answers to the dev questions; don't chase perfect accuracy over a
  clean, explainable system.
