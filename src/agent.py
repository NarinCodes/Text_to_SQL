"""The agent itself — natural-language question in, executed SQL + rows out.

WHAT THIS DOES        Builds a schema card from the live database, asks the model for
                      SQL, guards it, executes it, and repairs it on failure. Holds the
                      conversation history that makes follow-up questions work.
DEPENDS ON            .utils (get_schema, query_db) and the OpenAI SDK pointed at
                      Fireworks. Knows NOTHING about terminals — cli.py and eval.py both
                      drive it, which is why presentation lives elsewhere.
USED BY               cli.py (interactive) and eval.py (scoring).

KEY FUNCTIONS
  SQLAgent.ask()          The whole loop, ~40 lines: assemble messages -> generate ->
                          guard -> execute -> repair. One model call on the happy path.
  build_schema_card()     Introspects tables, columns, PKs and FK edges into the prompt.
                          The single biggest quality lever; FK edges are what fix joins.
  references_known_table() Structural guard: a question about the data must read the
                          data. Blocks `SELECT 'Abuja'` — model knowledge dressed as your
                          data — without needing the model to cooperate.
  extract_sql()           Strips fences/prose; the `split(";")[0]` is a SAFETY boundary,
                          not tidiness — it makes a trailing DROP unreachable.
  _repeats_last_query()   Catches the model re-running the last query when it doesn't
                          understand a stray word. Scoped to short input on purpose.

TO EXTEND
  New guard -> add it inside the `try` in ask(), and break rather than retry if it is
  not a retryable condition. New prompt rule -> SYSTEM_PROMPT, then re-run the eval:
  rules interact, and two of mine made things worse before they made them better.
"""

# `from __future__ import annotations` makes Python treat every type annotation as a
# string instead of evaluating it at import time. That is what lets us write modern
# syntax like `str | None` and `list[dict]` while still supporting older interpreters,
# and it avoids circular-import problems in annotations.
from __future__ import annotations

import os
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd
# The Fireworks inference API is OpenAI-compatible, so the official OpenAI SDK works
# unchanged — we only repoint `base_url`. That is why there is no Fireworks-specific
# client library anywhere in this project.
from openai import BadRequestError, OpenAI

from .utils import get_schema, query_db

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"
DEFAULT_MODEL = "accounts/fireworks/models/deepseek-v4-flash-0731"

# Generous enough that a long query is never truncated mid-statement.
MAX_TOKENS = 2048
# Reasoning models burn output tokens on hidden chain-of-thought before emitting
# SQL. Disabling it cut median latency ~5x and completion tokens ~10x on this task
# with no accuracy loss, which is what makes the CLI feel interactive.
DEFAULT_REASONING_EFFORT = "none"

# Number of execution attempts per question: the first generation plus repairs.
MAX_ATTEMPTS = 3
# Prior (question, sql) turns kept in context for follow-up questions.
MAX_HISTORY_TURNS = 4

# `{schema_card}` is filled in per session by str.format() in SQLAgent.ask(). Every
# rule below either encodes SQL correctness or fixes a failure seen during evaluation.
SYSTEM_PROMPT = """You are a SQL expert writing queries for a read-only SQLite database.

Rules:
- Return exactly ONE SQLite SELECT statement. No markdown fences, no comments, no prose.
- Use only the tables and columns listed in the schema below. Never invent names.
- Join tables along the foreign-key relationships listed below.
- Use LEFT JOIN when the question implies rows with zero matches should still appear.
- Group by the primary key when display names are not unique.
- Select only the columns the question asks for, plus the column being ranked or aggregated.
- For a superlative (most, top, highest, best, largest), always return the value
  being ranked by as its own column. An answer of "X" alone hides whether X won by
  a wide margin or is tied with hundreds of others.
- Do not return id or primary-key columns unless the question asks for them.
- When the question asks *which* person or entity, return one display-name column
  (e.g. FirstName || ' ' || LastName); when it asks for their details, return the
  individual fields.
- Never modify data (no INSERT, UPDATE, DELETE, DROP, ALTER or CREATE).
- If the question cannot be answered from the tables above — general knowledge, the
  outside world, programming help, chit-chat — reply with exactly NOT_A_DATA_QUESTION
  and nothing else. Never answer from your own knowledge by selecting a literal value.
  This is only for questions with no connection to the data. If the question is about
  things that DO appear in the schema but the exact metric is ambiguous, pick the most
  reasonable interpretation and answer it — do not decline.
- Every message must name data to retrieve: an entity, a metric, or a filter. If it does
  not — a stray word, a fragment, a typo, gibberish, or an acknowledgement like "ok" or
  "hmm" — reply with exactly UNCLEAR_QUESTION and nothing else. Repeating the previous
  query is never the right response to a message you do not understand; a follow-up only
  counts as a follow-up when it actually modifies the earlier request.

Database schema:
{schema_card}"""

# The naive baseline prompt, kept here so the eval harness can measure the lift.
NAIVE_PROMPT = "Convert this question to SQL:\n{question}"

# Sentinel the model returns for a question the database cannot answer.
OUT_OF_SCOPE = "NOT_A_DATA_QUESTION"
# Sentinel for input too vague to identify what is being asked.
UNCLEAR = "UNCLEAR_QUESTION"

# Captures the identifier after FROM/JOIN, tolerating `quoted`, "quoted" and [quoted]
# names. Used to prove a query actually reads from the connected database.
_TABLE_REF = re.compile(r"""\b(?:FROM|JOIN)\s+[`"\[]?([A-Za-z_]\w*)""", re.IGNORECASE)

# Matches a ```sql ... ``` fenced block. DOTALL lets `.` span newlines (SQL is
# multi-line); the capture group grabs just the code inside the fences.
_SQL_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

# pandas wraps SQLite failures in its own DatabaseError, so both must be caught
# for the repair loop to see a failed query. ValueError is included because our own
# helpers raise it for empty responses and read-only violations — catching all three
# in one tuple is what lets a single `except` clause drive the repair loop.
EXECUTION_ERRORS = (sqlite3.Error, pd.errors.DatabaseError, ValueError)

# `\b` is a word boundary, so this matches the keyword DELETE but not a column named
# "DELETED_AT". Defence in depth behind the SELECT-only check in assert_read_only().
_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|PRAGMA)\b",
    re.IGNORECASE,
)


def build_schema_card(conn: sqlite3.Connection) -> str:
    """Render the database schema as a compact text block for the system prompt.

    Includes column types, primary keys and foreign-key edges. The FK edges are the
    part the baseline prompt is missing: without them the model guesses at joins.

    Everything here is read from the live connection, so the agent works against any
    SQLite file without code changes — nothing about Chinook is hard-coded.
    """
    lines: list[str] = []
    edges: list[str] = []

    # get_schema() (in utils.py) returns {table_name: [column_info, ...]}, where each
    # column_info comes from `PRAGMA table_info` and carries name/type/pk fields.
    for table, columns in get_schema(conn).items():
        # Render one signature line per table, e.g.
        #   Album(AlbumId INTEGER PK, Title NVARCHAR(160), ArtistId INTEGER)
        # The ' PK' suffix is appended only when c['pk'] is non-zero.
        cols = ", ".join(
            f"{c['name']} {c['type']}{' PK' if c['pk'] else ''}" for c in columns
        )
        lines.append(f"{table}({cols})")

        # `PRAGMA foreign_key_list` is a second, separate introspection call: table_info
        # knows nothing about relationships. Each row describes one FK, where 'from' is
        # the local column and 'table'/'to' name the referenced table and column.
        for fk in query_db(
            conn, f"PRAGMA foreign_key_list({table})", return_as_df=False
        ):
            edges.append(f"{table}.{fk['from']} -> {fk['table']}.{fk['to']}")

    # Two labelled blocks read better for the model than interleaving edges per table.
    card = "Tables:\n" + "\n".join(f"  {line}" for line in lines)
    if edges:
        card += "\n\nForeign keys:\n" + "\n".join(f"  {edge}" for edge in edges)
    return card


def extract_sql(text: str) -> str:
    """Pull a bare SQL statement out of a model response.

    Models wrap SQL in markdown fences or add a trailing explanation even when told
    not to, so normalise here rather than trusting the instruction to hold.
    """
    if not text or not text.strip():
        # Raising ValueError (a member of EXECUTION_ERRORS) means an empty response
        # is routed into the repair loop rather than crashing the session.
        raise ValueError("Model returned an empty response.")

    # Step 1: if the model used ```sql fences, keep only what is inside them.
    fenced = _SQL_FENCE.search(text)
    sql = (fenced.group(1) if fenced else text).strip()

    # Step 2: drop any prose before the statement ("Here is the query: SELECT ...").
    start = re.search(r"\b(SELECT|WITH)\b", sql, re.IGNORECASE)
    if start:
        sql = sql[start.start() :]

    # Step 3: keep only the first statement. This is a safety boundary, not tidiness:
    # it is what makes a trailing "; DROP TABLE Artist" unreachable. Verified by test.
    return sql.split(";")[0].strip()


def assert_read_only(sql: str) -> None:
    """Reject anything that is not a single read-only SELECT.

    The agent executes model-generated SQL against a live database, so this is a
    hard gate rather than a prompt instruction. Returns None on success and raises on
    violation — the "assert" naming signals it is called for its side effect.
    """
    # A read must begin with SELECT, or with WITH for a common table expression.
    if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.IGNORECASE):
        raise ValueError("Only SELECT queries are allowed.")
    # Second layer: catches a write hidden after a leading WITH clause.
    if _WRITE_KEYWORDS.search(sql):
        raise ValueError("Query contains a non-read-only keyword.")


def references_known_table(sql: str, tables: set[str]) -> bool:
    """True if the query reads from at least one table in the connected database.

    This is the structural guarantee behind out-of-scope handling. A question *about the
    data* must read the data; a query that touches no table cannot be answering one. It
    catches the dangerous case where the model answers from its own knowledge by
    selecting a literal — `SELECT 'Abuja' AS CapitalOfNigeria` — which would otherwise
    execute cleanly and present a hallucination as though it came from the connected
    database. Unlike the prompt instruction it needs no cooperation from the model.
    """
    referenced = {m.group(1).lower() for m in _TABLE_REF.finditer(sql)}
    return bool(referenced & {t.lower() for t in tables})


@dataclass
class AgentResult:
    """One question, and everything the eval harness and CLI need to report on it.

    `@dataclass` is a decorator that writes boilerplate methods for you. Declaring the
    six fields below is enough for Python to generate __init__, __repr__ and __eq__,
    so this is equivalent to a hand-written class with an __init__ that assigns all six
    arguments to self — about twenty lines saved, with no behaviour of our own to hide.

    Use a dataclass when a class exists mainly to *hold related values*; write a normal
    class when its identity is its behaviour. Note that SQLAgent below is also a
    dataclass but does have real behaviour — dataclasses are a boilerplate shortcut,
    not a restriction on what the class may do.
    """

    # Fields with no default must come first; the rest default when omitted.
    question: str
    sql: str | None = None            # None until the model produces something
    rows: pd.DataFrame | None = None  # None if every attempt failed
    error: str | None = None          # last error message, None on success
    attempts: int = 0                 # how many generate/execute cycles were used
    elapsed_s: float = 0.0            # wall-clock time for the whole ask()
    out_of_scope: bool = False        # the database cannot answer this question
    unclear: bool = False             # we could not tell what was being asked

    @property
    def ok(self) -> bool:
        """True when the query executed. `@property` lets callers write `result.ok`
        rather than `result.ok()` — a derived value that reads like an attribute."""
        return self.error is None


@dataclass
class SQLAgent:
    """Stateful agent over a single SQLite connection.

    Holds the things that persist across questions — the database connection, the API
    client, the schema card, and the conversation history.
    """

    conn: sqlite3.Connection
    # `field(default_factory=...)` supplies a default by *calling* a function when each
    # instance is created. This is required for values computed at construction time,
    # and mandatory for mutable defaults: a plain `history: list = []` would create ONE
    # list shared by every SQLAgent ever made — a classic Python bug.
    model: str = field(default_factory=lambda: os.getenv("FIREWORKS_MODEL", DEFAULT_MODEL))
    client: OpenAI = field(default=None)  # injectable, which is what makes tests possible
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT
    history: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Runs immediately after the generated __init__.

        This is the dataclass hook for setup that cannot be expressed as a field
        default — here, building a client only if one was not injected, and doing the
        one-off schema introspection.
        """
        if self.client is None:
            api_key = os.getenv("FIREWORKS_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "FIREWORKS_API_KEY is not set. Add it to .env or export it."
                )
            self.client = OpenAI(base_url=FIREWORKS_BASE_URL, api_key=api_key)
        # Introspect the database ONCE per session. The result is reused on every
        # call, which is also what makes the prompt prefix byte-identical and therefore
        # cacheable by the provider.
        self.schema_card = build_schema_card(self.conn)
        self.table_names = set(get_schema(self.conn))

    def _complete(self, messages: list[dict[str, str]]) -> str:
        """Send a message list to the model and return its text.

        The leading underscore is a Python convention meaning "internal" — not
        enforced, but a signal that callers outside this class should not rely on it.
        """
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,      # deterministic as possible: one right answer exists
            "max_tokens": MAX_TOKENS,
        }
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort

        try:
            # `**kwargs` unpacks the dict into named arguments. Building the dict first
            # is what lets us conditionally include reasoning_effort.
            response = self.client.chat.completions.create(**kwargs)
        except BadRequestError:
            # Not every model accepts reasoning_effort; drop it and stop sending it.
            # Retrying once and clearing the field means we pay this cost at most once
            # per session rather than on every call.
            if not self.reasoning_effort:
                raise  # the rejection was about something else — do not swallow it
            self.reasoning_effort = None
            kwargs.pop("reasoning_effort")
            response = self.client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        content = choice.message.content or ""
        # A reasoning model can spend its whole budget thinking and return EMPTY text
        # with finish_reason == "length". Without this check that surfaces as a
        # baffling "no SQL produced"; here it becomes an actionable message.
        if not content.strip() and choice.finish_reason == "length":
            raise ValueError(
                f"{self.model} used its entire {MAX_TOKENS}-token budget on reasoning "
                "without emitting SQL. Raise MAX_TOKENS or set reasoning_effort='none'."
            )
        return content

    def ask(
        self,
        question: str,
        on_attempt: Callable[[int], None] | None = None,
    ) -> AgentResult:
        """Answer a question: generate SQL, execute it, repair on failure.

        This is the whole agent loop. One model call on the happy path; a second or
        third only when the database rejects what came back.

        `on_attempt` is an optional progress hook called with the attempt number before
        each try. The CLI uses it to say "repairing" instead of leaving the user staring
        at an unchanging spinner for twenty seconds; nothing else depends on it.
        """
        started = time.perf_counter()  # monotonic clock — correct for measuring durations
        result = AgentResult(question=question)

        # Build the message list the chat API expects. Three roles are in play:
        #   system    — standing instructions + the schema card (identical every call)
        #   user      — a question
        #   assistant — what the model previously replied
        # History sits between the system prompt and the new question so the model
        # reads the conversation in order.
        system = SYSTEM_PROMPT.format(schema_card=self.schema_card)
        messages = [{"role": "system", "content": system}]
        messages.extend(self.history)
        messages.append({"role": "user", "content": question})

        # --- the self-repair loop -------------------------------------------------
        # Each pass: generate SQL -> guard it -> execute it. Reaching the end of the
        # `try` without an exception means the query genuinely ran, so we break.
        # Any EXECUTION_ERRORS member means something rejected the SQL, and we append
        # the failure to `messages` so the next pass sees what went wrong.
        for attempt in range(1, MAX_ATTEMPTS + 1):
            result.attempts = attempt
            if on_attempt is not None:
                on_attempt(attempt)
            try:
                raw = self._complete(messages)
                # The model declined. Not an error, and not worth retrying — retrying
                # would only spend two more round-trips reaching the same conclusion.
                if OUT_OF_SCOPE in raw:
                    result.out_of_scope = True
                    result.error = "This question cannot be answered from the database."
                    break
                # Too vague to act on. Also not retryable — and critically, better than
                # the alternative the model reaches for on its own, which is silently
                # re-running the previous turn's query.
                if UNCLEAR in raw:
                    result.unclear = True
                    result.error = "Could not tell what data this question is asking for."
                    break
                sql = extract_sql(raw)
                assert_read_only(sql)
                # Backstop for when the model ignores the instruction above and answers
                # from its own knowledge instead. Also not retryable.
                if not references_known_table(sql, self.table_names):
                    result.sql = sql
                    result.out_of_scope = True
                    result.error = (
                        "That query does not read from any table in this database, so it "
                        "would be answering from the model's own knowledge rather than "
                        "from your data."
                    )
                    break
                # Backstop for stray input the model chose to answer by repeating itself.
                if self._repeats_last_query(question, sql):
                    result.sql = sql
                    result.unclear = True
                    result.error = "That would only re-run your previous query."
                    break
                result.sql = sql
                # Execution IS the validation step. Nothing else proves the SQL is
                # runnable against this schema, which is why there is no SQL parser.
                result.rows = query_db(self.conn, sql)
                result.error = None  # clear any error left by an earlier attempt
                break
            except EXECUTION_ERRORS as exc:
                result.error = str(exc)
                if attempt == MAX_ATTEMPTS:
                    break  # out of budget; return the failure for the caller to report
                # Execution-guided repair: show the model its own SQL and the error.
                # These two messages are appended to the LOCAL list only — they are
                # deliberately never written to self.history, so a failed attempt
                # cannot pollute the context of later questions.
                messages.append({"role": "assistant", "content": result.sql or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"That query failed with: {exc}\n"
                            # Restating the goal is load-bearing, not politeness. Without
                            # it this message is the most recent instruction and reframes
                            # the task as "fix the syntax", so the model repairs the
                            # broken SQL without revisiting what was actually asked —
                            # measured at 0/6 on a follow-up whose constraint came from
                            # an earlier turn, versus 6/6 with the question restated.
                            f"Fix it so that it answers this question: {question}\n"
                            "Return a corrected SQLite SELECT statement, nothing else."
                        ),
                    }
                )

        result.elapsed_s = time.perf_counter() - started
        # Only successful turns become context for follow-up questions.
        if result.ok:
            self._remember(question, result.sql)
        return result

    # Stray input is short by nature ("rest", "ok", "hmm"). Restricting the repeat check
    # to short messages keeps it useful without catching a legitimate restatement, which
    # is a real pattern: "how many albums did this band sell?" followed by the explicit
    # "how many albums did Iron Maiden sell?" produces identical SQL by design.
    REPEAT_CHECK_MAX_WORDS = 3

    def _repeats_last_query(self, question: str, sql: str) -> bool:
        """True when a *short* message produced SQL identical to the previous turn's.

        Asked something it cannot interpret, the model tends to re-run the last query
        rather than admit it does not understand — the observed behaviour for stray
        input like "rest" or "ok". Comparing against history catches that deterministically,
        without depending on the model to police itself.

        The false-positive case is benign: if a user genuinely re-asks the same question
        in different words, telling them it is the query they just ran is still useful.
        """
        if not self.history or len(question.split()) > self.REPEAT_CHECK_MAX_WORDS:
            return False
        normalise = lambda q: " ".join(q.split()).rstrip(";").lower()
        return normalise(self.history[-1]["content"]) == normalise(sql)

    def _remember(self, question: str, sql: str) -> None:
        """Keep the successful turn so follow-up questions have context.

        The stored SQL (rather than the returned rows) is what a follow-up needs to
        modify, and it is far more compact than a result set.
        """
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": sql})
        # Keep only the most recent MAX_HISTORY_TURNS exchanges. Each turn is two
        # messages, so this deletes everything before the last 8 entries: `del lst[:-8]`
        # removes from the start up to the 8th-from-last item. Bounding history bounds
        # both prompt cost and the risk of stale context steering a new question.
        del self.history[: -2 * MAX_HISTORY_TURNS]

    def reset(self) -> None:
        """Forget the conversation. Used by the CLI's `reset` command and by the eval
        harness before each question, so scores measure single-turn accuracy."""
        self.history.clear()

    def ask_naive(self, question: str) -> AgentResult:
        """The naive baseline prompt: no schema, no repair. Used by the eval.

        Deliberately runs on the SAME model as ask(), so any measured difference is
        attributable to the agent design rather than to the model choice.
        """
        started = time.perf_counter()
        result = AgentResult(question=question, attempts=1)
        try:
            raw = self._complete(
                [{"role": "user", "content": NAIVE_PROMPT.format(question=question)}]
            )
            result.sql = extract_sql(raw)
            assert_read_only(result.sql)
            result.rows = query_db(self.conn, result.sql)
        except EXECUTION_ERRORS as exc:
            # No retry here on purpose: this reproduces the naive baseline.
            result.error = str(exc)
        result.elapsed_s = time.perf_counter() - started
        return result
