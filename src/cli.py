"""Interactive REPL — the thing a user actually runs. Control flow only.

WHAT THIS DOES        Parses --db, loads the database, builds the agent, then loops:
                      read a line, dispatch commands, hand questions to the agent, print
                      whatever comes back. Roughly 100 lines and no agent logic at all.
DEPENDS ON            .agent (SQLAgent), .ui (every piece of rendering), .utils (load_db)
USED BY               Nothing — this is the entry point (`uv run cli`, per pyproject).

KEY FUNCTIONS
  main()          The REPL. Three things worth noting: commands
                  are checked before the agent is ever called; the spinner wraps
                  agent.ask() so a slow query never looks like a hang; and the result is
                  dispatched across four states, not two — ok / unclear / out-of-scope /
                  failed.
  show_attempt()  Inner callback passed to agent.ask(); updates the spinner mid-flight
                  when a repair fires. The agent stays terminal-agnostic because this
                  is a plain callable, not a rich object.

TO EXTEND
  A new command is one `if question.lower() == "..."` branch before the agent call.
  A --json or --quiet mode should change ui.py only; if it forces changes here, the
  presentation boundary has leaked.
"""

from __future__ import annotations

import argparse
import sqlite3

from dotenv import load_dotenv

from . import ui
from .agent import MAX_ATTEMPTS as agent_max_attempts
from .agent import SQLAgent
from .utils import load_db


def main() -> None:
    """Entry point registered in pyproject.toml as the `cli` script."""
    parser = argparse.ArgumentParser(description="Interactive text-to-SQL CLI.")
    parser.add_argument(
        "--db", default="data/Chinook.db", help="path to a SQLite database file"
    )
    args = parser.parse_args()

    # load_dotenv() reads .env into os.environ so SQLAgent can find the API key.
    # It does not overwrite variables already exported in the shell.
    load_dotenv()
    console = ui.make_console()
    # Startup failures — missing database, missing API key — are the two things a new
    # user hits first, so they get a plain message rather than a traceback. Both raise
    # before the prompt appears, which is the right time to fail.
    try:
        conn = load_db(args.db)
        agent = SQLAgent(conn=conn)
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
        console.print(f"  [{ui.BAD}]Could not start:[/] {exc}\n")
        raise SystemExit(1)
    ui.print_banner(console, args.db, conn, agent.model)

    asked = answered = 0
    query_seconds = 0.0
    last_rows = None  # retained so `more` can re-render the full result set

    # --- REPL: read, evaluate, print, loop -----------------------------------
    while True:
        try:
            question = console.input(f"[bold {ui.ACCENT}]›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            # Ctrl-D (EOF) or Ctrl-C at the prompt: leave cleanly, no traceback.
            console.print()
            break

        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            break
        if question.lower() == "help":
            ui.print_help(console)
            continue
        if question.lower() == "schema":
            ui.print_schema(console, conn)
            continue
        if question.lower() == "more":
            if last_rows is None:
                console.print(f"  [{ui.MUTED}]Ask a question first.[/]\n")
            else:
                ui.print_all_rows(console, last_rows)
            continue
        if question.lower() == "reset":
            agent.reset()
            console.print(f"  [{ui.MUTED}]Conversation context cleared.[/]\n")
            continue

        console.print()
        try:
            # A live spinner is the difference between "working" and "hung" on a query
            # that takes ten seconds. This is the whole reason the loop owns a console.
            with console.status(f"[{ui.MUTED}]Thinking…[/]", spinner="dots") as spinner:

                def show_attempt(n: int) -> None:
                    """Name what is happening when a repair adds a second round-trip."""
                    if n > 1:
                        spinner.update(
                            f"[{ui.MUTED}]Query failed — repairing "
                            f"(attempt {n} of {agent_max_attempts})…[/]"
                        )

                result = agent.ask(question, on_attempt=show_attempt)
        except KeyboardInterrupt:
            # Ctrl-C *during* a request abandons that question, not the session.
            console.print(f"  [{ui.MUTED}]Cancelled.[/]\n")
            continue
        except Exception as exc:  # network failure, bad model id, missing key
            # Broad by design: an interactive tool should survive any single bad turn.
            # agent.ask() handles SQL-level failures internally, so anything reaching
            # here is environmental.
            console.print(f"  [{ui.BAD}]Request failed:[/] {exc}\n")
            continue

        asked += 1
        query_seconds += result.elapsed_s

        # An out-of-scope question is a declined request, not a failed one. Show the
        # boundary rather than the SQL: printing a rejected `SELECT 'Abuja'` would only
        # advertise the answer the agent just refused to stand behind.
        if result.out_of_scope:
            ui.print_out_of_scope(console, conn)
            continue
        if result.unclear:
            ui.print_unclear(console, result.error)
            continue

        # Always show the SQL, even on failure — it is the audit trail.
        if result.sql:
            ui.print_sql(console, result.sql)
        if result.ok:
            answered += 1
            last_rows = result.rows
            ui.print_rows(console, result.rows)
            ui.print_result_meta(
                console,
                row_count=len(result.rows),
                shown=min(len(result.rows), ui.MAX_DISPLAY_ROWS),
                elapsed=result.elapsed_s,
                attempts=result.attempts,
            )
        else:
            ui.print_failure(console, result.error, result.attempts)

    ui.print_goodbye(console, asked, answered, query_seconds)


# Runs only when executed directly (python -m src.cli), not when imported.
if __name__ == "__main__":
    main()
