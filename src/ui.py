"""Every pixel the CLI prints. No agent logic, no database logic.

WHAT THIS DOES        Banner, SQL panel, result tables, the three "cannot answer" panels,
                      help, schema browser, session summary. Owns colour and layout.
DEPENDS ON            .utils (get_schema, query_db) for the schema browser, plus rich.
USED BY               cli.py only. agent.py and eval.py never import this.

KEY FUNCTIONS
  highlight_sql()   Hand-rolled SQL colouring using ONLY the terminal's 16-colour
                    palette, with identifiers left at the default foreground. A syntax
                    theme picks fixed colours and renders identifiers near-white, which
                    is invisible on a light terminal. This is the fix for that.
  _build_table()    One table renderer shared by print_rows (20-row cap) and
                    print_all_rows (`more`). Numeric columns right-align.
  _cell()           DISPLAY formatting: thousands separators, integers without decimals,
                    NULL as an em dash. Distinct from eval._normalize(), which is for
                    comparison. Do not merge them.
  print_out_of_scope / print_unclear   Two different panels on purpose: "outside this
                    database" is a scope answer, "not sure what you mean" is a
                    comprehension answer. Telling a user their typo is out of scope is
                    confusing.

TO EXTEND
  New output state -> add a print_* here and one dispatch branch in cli.py. Keep colours
  as named rich styles (they map to ANSI and adapt to the user's theme), never hex.
"""

from __future__ import annotations

import re
import sqlite3

import pandas as pd
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .utils import get_schema, query_db

# Colour roles, named once so the palette stays consistent across every surface.
ACCENT = "cyan"
MUTED = "grey58"
GOOD = "green"
BAD = "red"

# Rows printed before the table is truncated. The full count is always reported.
MAX_DISPLAY_ROWS = 20
# SQL wraps inside a panel no wider than this, so a maximised terminal doesn't stretch
# one query across 200 columns.
SQL_PANEL_WIDTH = 96

_SQL_KEYWORDS = re.compile(
    r"\b(SELECT|DISTINCT|FROM|WHERE|GROUP|ORDER|BY|HAVING|LIMIT|OFFSET|JOIN|LEFT|RIGHT"
    r"|INNER|OUTER|FULL|CROSS|ON|AS|AND|OR|NOT|IN|IS|NULL|LIKE|BETWEEN|CASE|WHEN|THEN"
    r"|ELSE|END|WITH|UNION|ALL|ASC|DESC|COUNT|SUM|AVG|MIN|MAX|ROUND|CAST|COALESCE"
    r"|STRFTIME|JULIANDAY|RANK|DENSE_RANK|ROW_NUMBER|OVER|PARTITION)\b",
    re.IGNORECASE,
)
_SQL_STRING = re.compile(r"'[^']*'")
_SQL_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
# NULL renders as a dash so it is distinguishable from an empty string.
NULL_GLYPH = "—"
# Shown when we have no more specific reason for not understanding the input.
DEFAULT_UNCLEAR = "I couldn't tell what you're asking for."

BANNER = r"""
 ████████╗███████╗██╗  ██╗████████╗    ██████╗     ███████╗ ██████╗ ██╗
 ╚══██╔══╝██╔════╝╚██╗██╔╝╚══██╔══╝    ╚════██╗    ██╔════╝██╔═══██╗██║
    ██║   █████╗   ╚███╔╝    ██║        █████╔╝    ███████╗██║   ██║██║
    ██║   ██╔══╝   ██╔██╗    ██║       ██╔═══╝     ╚════██║██║▄▄ ██║██║
    ██║   ███████╗██╔╝ ██╗   ██║       ███████╗    ███████║╚██████╔╝███████╗
    ╚═╝   ╚══════╝╚═╝  ╚═╝   ╚═╝       ╚══════╝    ╚══════╝ ╚══▀▀═╝ ╚══════╝
"""
# Cool-to-warm ramp down the six banner rows; purely decorative.
_BANNER_RAMP = ["bright_cyan", "bright_cyan", "cyan", "cyan", "blue", "blue"]

EXAMPLES = [
    "What are the top 5 best-selling genres by total sales?",
    "Which artists have tracks in more than 3 different genres?",
    "List all albums by the artist 'AC/DC'.",
]


def make_console() -> Console:
    """One shared console. Rich honours NO_COLOR and strips styling when piped."""
    return Console(highlight=False)


def _cell(value: object) -> str:
    """Format one value for display: thousands separators, no spurious decimals."""
    if value is None:
        return NULL_GLYPH
    # numpy scalars from a DataFrame are not always Python int/float.
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        if pd.isna(value):
            return NULL_GLYPH
        number = float(value)
        return f"{int(number):,}" if number.is_integer() else f"{number:,.2f}"
    text = str(value).strip()
    return text if text else NULL_GLYPH


def print_banner(console: Console, db_path: str, conn: sqlite3.Connection, model: str) -> None:
    """Splash screen: identity, then what this session is actually connected to."""
    for line, colour in zip(BANNER.strip("\n").splitlines(), _BANNER_RAMP):
        console.print(line, style=colour, highlight=False)

    tables = len(get_schema(conn))
    short_model = model.rsplit("/", 1)[-1]  # drop the accounts/fireworks/models/ prefix
    console.print(f"  [{MUTED}]Conversational SQL for any SQLite database[/]\n")
    console.print(
        f"  [{ACCENT}]●[/] [bold]{db_path}[/]   "
        f"[{MUTED}]{tables} tables[/]   [{MUTED}]{short_model}[/]"
    )
    # Two short lines rather than one long one: the hint must not wrap at 80 columns.
    console.print(
        f"  [{MUTED}]Ask anything in plain English — follow-up questions keep their context.[/]"
    )
    console.print(
        f"  [bold {ACCENT}]help[/][{MUTED}] commands   ·   [/]"
        f"[bold {ACCENT}]schema[/][{MUTED}] explore tables   ·   [/]"
        f"[bold {ACCENT}]exit[/][{MUTED}] quit[/]\n"
    )


def highlight_sql(sql: str) -> Text:
    """Colour SQL using only the terminal's own 16-colour palette.

    Identifiers deliberately keep the default foreground — the one colour guaranteed
    legible against whatever background the user has. A syntax theme picks fixed colours
    instead: monokai renders identifiers near-white, which is invisible on a light
    terminal. Named colours here emit ANSI codes, so the terminal maps them to its own
    palette and the result reads on both light and dark.
    """
    text = Text(sql)
    # Strings applied last so quoted content wins over keyword/number matches inside it.
    for pattern, style in ((_SQL_NUMBER, "magenta"), (_SQL_KEYWORDS, "bold blue"),
                           (_SQL_STRING, "green")):
        for match in pattern.finditer(sql):
            text.stylize(style, match.start(), match.end())
    return text


def print_sql(console: Console, sql: str) -> None:
    """The generated query, highlighted inside its own frame.

    Showing the SQL is a trust feature: a practitioner who can read the query can decide
    whether to believe the number, which is the whole concern with a customer-facing
    BI tool.
    """
    console.print(
        Panel(
            highlight_sql(sql),
            title=f"[{ACCENT}]SQL[/]",
            title_align="left",
            border_style=MUTED,
            padding=(0, 1),
            width=min(console.width, SQL_PANEL_WIDTH),
        )
    )


def _build_table(rows: pd.DataFrame, limit: int | None) -> Table:
    """Render a frame as a table. `limit=None` means every row."""
    table = Table(
        box=box.SIMPLE_HEAD,
        header_style=f"bold {ACCENT}",
        border_style=MUTED,
        show_edge=False,
        pad_edge=False,
        padding=(0, 2, 0, 0),
    )
    for column in rows.columns:
        # Numeric columns align right so digits line up and magnitudes are comparable.
        numeric = pd.api.types.is_numeric_dtype(rows[column])
        table.add_column(str(column), justify="right" if numeric else "left", overflow="fold")

    shown = rows if limit is None else rows.head(limit)
    for row in shown.itertuples(index=False):
        table.add_row(*(_cell(v) for v in row))
    return table


def print_rows(console: Console, rows: pd.DataFrame) -> None:
    """Results as a real table: aligned columns, right-aligned numbers."""
    if rows.empty:
        console.print(f"  [{MUTED}]No rows matched.[/]\n")
        return
    console.print(_build_table(rows, MAX_DISPLAY_ROWS))


def print_all_rows(console: Console, rows: pd.DataFrame) -> None:
    """Every row of the last result, printed straight to the terminal.

    Deliberately not routed through a system pager: `less` takes over the screen and
    offers its own dense help, which is a jarring hand-off from a purpose-built CLI.
    Printing lets the terminal's own scrollback do the job the user already knows.
    """
    if rows.empty:
        console.print(f"  [{MUTED}]No rows to show.[/]\n")
        return
    console.print(_build_table(rows, None))
    console.print(f"  [{MUTED}]{len(rows):,} rows shown.[/]\n")


def print_result_meta(console: Console, row_count: int, shown: int, elapsed: float, attempts: int) -> None:
    """One dim line of provenance under the data — never competing with it."""
    parts = [f"{row_count:,} row{'s' if row_count != 1 else ''}"]
    if shown < row_count:
        parts.append(f"showing first {shown}")
        parts.append("[bold]more[/] for all")
    parts.append(f"{elapsed:.2f}s")
    # Surface self-repair when it happens: it is a feature, and silence would hide it.
    if attempts > 1:
        # Kept short so the meta line does not wrap at 80 columns.
        parts.append(f"self-repaired ×{attempts - 1}")
    console.print(f"  [{MUTED}]{'  ·  '.join(parts)}[/]\n")


def print_failure(console: Console, error: str, attempts: int) -> None:
    """A failed turn, framed so it reads as a handled outcome rather than a crash."""
    # pandas prefixes its DatabaseError with the full query text, which the SQL panel
    # directly above already shows. Strip the duplicate so the actual cause leads.
    if error.startswith("Execution failed on sql") and "': " in error:
        error = error.split("': ", 1)[1]
    console.print(
        Panel(
            f"[{BAD}]{error}[/]\n\n[{MUTED}]Tried {attempts} times. Rephrasing the question, "
            f"or running [bold]schema[/] to check the available tables, usually helps.[/]",
            title=f"[{BAD}]Could not answer that[/]",
            title_align="left",
            border_style=BAD,
            padding=(0, 1),
        )
    )
    console.print()


def print_out_of_scope(console: Console, conn: sqlite3.Connection) -> None:
    """A question the database cannot answer. Deliberately not styled as an error.

    Declining is correct behaviour, not a malfunction — so this is neutral, says plainly
    why, and points at what the tool *can* do. The alternative the agent must never take
    is answering from model knowledge, which would look identical to real data.
    """
    tables = ", ".join(list(get_schema(conn))[:6])
    console.print(
        Panel(
            f"I can only answer questions about the data in [bold]this database[/].\n\n"
            f"[{MUTED}]It holds {tables} and others — run [/][bold]schema[/]"
            f"[{MUTED}] to see everything, or ask about sales, customers, tracks, "
            f"invoices, playlists…[/]",
            title=f"[{ACCENT}]Outside this database[/]",
            title_align="left",
            border_style=MUTED,
            padding=(0, 1),
        )
    )
    console.print()


def print_unclear(console: Console, detail: str | None = None) -> None:
    """Input we could not interpret. A request to rephrase, not a refusal.

    Distinct from print_out_of_scope: telling someone their typo is "outside this
    database" is confusing. The failure here is comprehension, not scope.
    """
    console.print(
        Panel(
            f"{detail or DEFAULT_UNCLEAR}\n\n"
            f"[{MUTED}]Try naming what you want to see — [/]\"top 5 genres by sales\", "
            f"\"customers in Brazil\", \"longest tracks\"[{MUTED}] — or run [/]"
            f"[bold]schema[/][{MUTED}] to see what's available.[/]",
            title=f"[{ACCENT}]Not sure what you mean[/]",
            title_align="left",
            border_style=MUTED,
            padding=(0, 1),
        )
    )
    console.print()


def print_help(console: Console) -> None:
    commands = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ACCENT}", border_style=MUTED,
                     show_edge=False, pad_edge=False)
    commands.add_column("Command")
    commands.add_column("What it does")
    commands.add_row("help", "Show this message")
    commands.add_row("schema", "List the tables, row counts, and relationships")
    commands.add_row("more", "Show every row of the last result")
    commands.add_row("reset", "Forget the conversation so far")
    commands.add_row("exit / quit", "End the session (Ctrl-D also works)")
    console.print(commands)
    console.print(f"\n  [{MUTED}]Follow-ups keep their context — try:[/]")
    console.print(f"  [{MUTED}]  \"top 5 genres by sales\" → \"now just for 2021\" → \"only the top 2\"[/]")
    console.print(f"\n  [{MUTED}]Example questions:[/]")
    for example in EXAMPLES:
        console.print(f"  [{MUTED}]  · {example}[/]")
    console.print()


def print_schema(console: Console, conn: sqlite3.Connection) -> None:
    """What can I ask about? — the first question any user of a new database has."""
    schema = get_schema(conn)
    table = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ACCENT}", border_style=MUTED,
                     show_edge=False, pad_edge=False)
    table.add_column("Table")
    table.add_column("Rows", justify="right")
    table.add_column("Columns", overflow="fold")

    for name, columns in schema.items():
        count = query_db(conn, f"SELECT COUNT(*) AS n FROM `{name}`").iloc[0]["n"]
        table.add_row(name, f"{int(count):,}", ", ".join(c["name"] for c in columns))
    console.print(table)

    # Foreign keys are what make joins correct, and `help` promises to show them.
    edges = []
    for name in schema:
        for fk in query_db(conn, f"PRAGMA foreign_key_list(`{name}`)", return_as_df=False):
            edges.append(f"{name}.{fk['from']} → {fk['table']}.{fk['to']}")
    if edges:
        console.print(f"\n  [bold {ACCENT}]Relationships[/]")
        for edge in edges:
            console.print(f"  [{MUTED}]  {edge}[/]")

    console.print(f"\n  [{MUTED}]{len(schema)} tables, {len(edges)} relationships. The agent "
                  f"reads all of this automatically — you never need to name a column "
                  f"exactly.[/]\n")


def print_goodbye(console: Console, asked: int, answered: int, seconds: float) -> None:
    """Close the session with what happened, not just a farewell."""
    if asked:
        console.print(
            f"  [{MUTED}]Session:[/] [bold]{asked}[/] question{'s' if asked != 1 else ''}  ·  "
            f"[{GOOD}]{answered} answered[/]  ·  [{MUTED}]{seconds:.1f}s of query time[/]"
        )
    console.print(f"  [{ACCENT}]Thanks for using Text2SQL.[/]\n")
