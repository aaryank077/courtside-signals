"""Ask courtside questions from the terminal.

    venv/bin/python ask.py                          # interactive: type questions, "exit" or a blank line to quit
    venv/bin/python ask.py "How many 30 point games does Luka have vs LAL?"
    venv/bin/python ask.py --debug "..."            # also show how the question was parsed
"""

import sys

import pandas as pd

from engine.parser import ask

pd.set_option("display.width", 120)
pd.set_option("display.max_columns", 20)


def show(question, debug=False, rows=10):
    result = ask(question)
    if debug:
        print(f"  [{result['category']}] {result['params']}")
    print(result["answer"])
    table = result["table"]
    if table is not None and not table.empty:
        print(table.head(rows).to_string(index=False))
        if len(table) > rows:
            print(f"... {len(table) - rows} more rows")
    print()


def main():
    args = sys.argv[1:]
    debug = "--debug" in args
    args = [a for a in args if a != "--debug"]

    if args:
        show(" ".join(args), debug)
        return

    print("Ask a question (type exit, or press Enter on a blank line, to quit).\n")
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question or question.lower() in {"exit", "quit", "q", "esc"}:
            break
        show(question, debug)


if __name__ == "__main__":
    main()
