"""Ask courtside questions from the terminal.

    venv/bin/python ask.py                          # interactive: type questions, "exit" or a blank line to quit
    venv/bin/python ask.py "How many 30 point games does Luka have vs LAL?"
    venv/bin/python ask.py --debug "..."            # also show how the question was parsed
    venv/bin/python ask.py --llm "..."              # rules first, Claude writes SQL if they can't
                                                    # (needs ANTHROPIC_API_KEY)
"""

import sys

import pandas as pd

from engine.parser import ask

pd.set_option("display.width", 120)
pd.set_option("display.max_columns", 20)


def show_llm(question, rows=10):
    from engine.llm_sql import ask_hybrid
    result = ask_hybrid(question)
    if result["source"] == "rules":
        return show(question, rows=rows)
    print(f"  [llm: {result['status']}, {result['attempts']} call(s), "
          f"{result['latency_s']:.1f}s, ${result['cost_usd']:.4f}]")
    if result["sql"]:
        print(f"  {result['sql']}")
    if result["status"] != "ok":
        print(f"Couldn't answer that: {result['reason']}\n")
        return
    table = pd.DataFrame(result["rows"], columns=result["columns"])
    print(table.head(rows).to_string(index=False), "\n")


def show(question, debug=False, rows=10, llm=False):
    if llm:
        return show_llm(question, rows)
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
    llm = "--llm" in args
    args = [a for a in args if a not in ("--debug", "--llm")]

    if args:
        show(" ".join(args), debug, llm=llm)
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
        show(question, debug, llm=llm)


if __name__ == "__main__":
    main()
