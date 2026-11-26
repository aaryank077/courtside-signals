"""Measure the rule-based parser and the LLM text-to-SQL path on the same questions.

    venv/bin/python -m evals.run                               # rules + Claude Opus 5.5
    venv/bin/python -m evals.run --model claude-sonnet-5-5
    venv/bin/python -m evals.run --model ollama/qwen2.5-coder:3b   # free, local (Ollama)
    venv/bin/python -m evals.run --rules-only                  # no API calls
    venv/bin/python -m evals.run --limit 10                    # first 10 questions
    venv/bin/python -m evals.run --report                      # tables from saved results

Everything runs against evals/snapshot.db, a frozen copy of courtside.db, so
the expected answers in evals/questions.jsonl don't drift as new games are
played. The rules path runs offline (COURTSIDE_OFFLINE=1) on a temporary copy,
so it can't change the snapshot.

Per-question results go to evals/results/<model>.jsonl; --report turns every
saved results file into the markdown tables used in the README.
"""

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ["COURTSIDE_OFFLINE"] = "1"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.scoring import grade_llm, grade_rules, rules_answered  # noqa: E402

EVALS = ROOT / "evals"
QUESTIONS = EVALS / "questions.jsonl"
SNAPSHOT = EVALS / "snapshot.db"
RESULTS = EVALS / "results"

CATEGORY_ORDER = ["player_averages", "game_count", "best_season", "best_games",
                  "career_stat", "league_leaders", "compare_players", "record",
                  "streak", "recent_games", "all_time", "all_nba",
                  "beyond_rules", "unanswerable"]


def load_cases(path=QUESTIONS):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------- running

def run_rules(cases):
    """Rules path on a scratch copy of the snapshot. Returns {id: record}."""
    from engine import handlers
    from engine.parser import ask

    tmp = Path(tempfile.mkdtemp())
    shutil.copy(SNAPSHOT, tmp / "snapshot.db")
    handlers.DB_PATH = tmp / "snapshot.db"
    handlers._FAILED_PATH = tmp / "failed_queries.jsonl"  # keep the real log clean

    out = {}
    for case in cases:
        start = time.perf_counter()
        try:
            result = ask(case["question"])
        except Exception as e:  # a crash is a failure, not a broken eval
            result = {"category": "crash", "answer": f"{type(e).__name__}: {e}",
                      "error": True, "table": None, "data": {}}
        latency = time.perf_counter() - start
        ok, failure = grade_rules(case, result)
        out[case["id"]] = {"correct": ok, "failure": failure, "latency_s": latency,
                           "answered": rules_answered(result),
                           "category": result.get("category"),
                           "answer": result.get("answer")}
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def run_llm(cases, model, workers):
    from engine.llm_sql import LOCAL_PREFIX, answer
    if model.startswith(LOCAL_PREFIX):
        client = None
        workers = 1  # one CPU-bound local model: parallel requests would just queue
    else:
        import anthropic
        client = anthropic.Anthropic()

    def one(case):
        result = answer(case["question"], client=client, model=model, db_path=SNAPSHOT)
        ok, failure = grade_llm(case, result)
        print(f"  {case['id']:20} {'ok ' if ok else 'BAD'} {result['latency_s']:5.1f}s "
              f"{failure or ''}", file=sys.stderr, flush=True)
        return case["id"], {"model": model, "correct": ok, "failure": failure,
                            "latency_s": result["latency_s"], "cost_usd": result["cost_usd"],
                            "usage": result["usage"], "attempts": result["attempts"],
                            "status": result["status"], "sql": result["sql"],
                            "reason": result["reason"], "rows": result["rows"][:5]}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, cases))


def hybrid(case, rules, llm):
    """Rules first; fall back to the LLM only when the rules didn't answer."""
    if rules["answered"]:
        return {"correct": rules["correct"], "failure": rules["failure"], "source": "rules",
                "latency_s": rules["latency_s"], "cost_usd": 0.0}
    return {"correct": llm["correct"], "failure": llm["failure"], "source": "llm",
            "latency_s": rules["latency_s"] + llm["latency_s"], "cost_usd": llm["cost_usd"]}


# ---------------------------------------------------------------- reporting

def _pct(xs):
    return f"{100 * sum(xs) / len(xs):.0f}%" if xs else "–"


def _acc(records, key):
    """Accuracy of one approach, or "pending" if it hasn't been run."""
    if any(r[key] is None for r in records):
        return "pending"
    return _pct([r[key]["correct"] for r in records])


def _quantile(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _ms(x):
    return f"{x * 1000:.1f} ms" if x < 1 else f"{x:.1f} s"


def report(records, model):
    """Markdown tables for one model's run."""
    by_cat = defaultdict(list)
    for r in records:
        by_cat[r["category"]].append(r)

    ran = [k for k in ("rules", "llm", "hybrid") if all(r[k] is not None for r in records)]
    lines = [f"**Accuracy by category** (`{model}`, {len(records)} questions)", "",
             "| Category | n | Rules | LLM | Rules → LLM fallback |",
             "|---|---:|---:|---:|---:|"]
    for cat in CATEGORY_ORDER + sorted(set(by_cat) - set(CATEGORY_ORDER)):
        rs = by_cat.get(cat)
        if not rs:
            continue
        lines.append(f"| `{cat}` | {len(rs)} | {_acc(rs, 'rules')} | {_acc(rs, 'llm')} | "
                     f"{_acc(rs, 'hybrid')} |")
    for label, rs in (("**12 rule categories**", [r for r in records if r["rules_scope"]]),
                      ("**All**", records)):
        lines.append(f"| {label} | {len(rs)} | {_acc(rs, 'rules')} | {_acc(rs, 'llm')} | "
                     f"{_acc(rs, 'hybrid')} |")

    lines += ["", "**Latency and cost per question**", "",
              "| | p50 latency | p95 latency | cost / question | LLM calls |",
              "|---|---:|---:|---:|---:|"]
    for key, label in (("rules", "Rules"), ("llm", "LLM"), ("hybrid", "Rules → LLM fallback")):
        if key not in ran:
            lines.append(f"| {label} | pending | pending | pending | pending |")
            continue
        lat = [r[key]["latency_s"] for r in records]
        cost = [r[key].get("cost_usd", 0.0) for r in records]
        calls = (0 if key == "rules" else
                 len(records) if key == "llm" else
                 sum(r["hybrid"]["source"] == "llm" for r in records))
        lines.append(f"| {label} | {_ms(statistics.median(lat))} | {_ms(_quantile(lat, 0.95))} | "
                     f"${statistics.mean(cost):.4f} | {calls} |")

    lines += ["", "**Why answers were wrong**", "",
              "| Failure | Rules | LLM | Rules → LLM fallback |", "|---|---:|---:|---:|"]
    counts = defaultdict(lambda: defaultdict(int))
    for r in records:
        for key in ran:
            if r[key]["failure"]:
                counts[r[key]["failure"]][key] += 1
    for failure in sorted(counts, key=lambda f: -sum(counts[f].values())):
        c = counts[failure]
        cells = [str(c[k]) if k in ran else "pending" for k in ("rules", "llm", "hybrid")]
        lines.append(f"| `{failure}` | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def model_table(runs):
    """One row per saved model run."""
    lines = ["| Model | LLM alone | Rules → LLM fallback | LLM p50 latency | LLM cost / question |",
             "|---|---:|---:|---:|---:|"]
    for model, records in runs.items():
        lat = [r["llm"]["latency_s"] for r in records]
        cost = [r["llm"]["cost_usd"] for r in records]
        lines.append(f"| `{model}` | {_pct([r['llm']['correct'] for r in records])} | "
                     f"{_pct([r['hybrid']['correct'] for r in records])} | "
                     f"{_ms(statistics.median(lat))} | ${statistics.mean(cost):.4f} |")
    return "\n".join(lines)


def load_results():
    runs = {}
    for path in sorted(RESULTS.glob("*.jsonl")):
        with open(path, encoding="utf-8") as f:
            records = [json.loads(line) for line in f]
        runs[records[0]["llm"].get("model", path.stem)] = records
    return runs


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rules-only", action="store_true")
    ap.add_argument("--report", action="store_true", help="print tables from saved results")
    args = ap.parse_args()

    if args.report:
        runs = load_results()
        if not runs:
            sys.exit("No saved results in evals/results/. Run the eval first.")
        for model, records in runs.items():
            print(report(records, model), "\n")
        if len(runs) > 1:
            print(model_table(runs))
        return

    cases = load_cases()[:args.limit]
    print(f"{len(cases)} questions; rules...", file=sys.stderr)
    rules = run_rules(cases)
    if args.rules_only:
        records = [{"id": c["id"], "category": c["category"], "rules_scope": c["rules_scope"],
                    "rules": rules[c["id"]], "llm": None, "hybrid": None} for c in cases]
        print(report(records, "rules only"))
        print("\nRules failures:", file=sys.stderr)
        for case in cases:
            r = rules[case["id"]]
            if not r["correct"]:
                print(f"  {case['id']:20} {r['failure']:22} {case['question']}", file=sys.stderr)
        return

    print(f"LLM ({args.model}, {args.workers} workers)...", file=sys.stderr)
    llm = run_llm(cases, args.model, args.workers)
    records = [{"id": c["id"], "category": c["category"], "question": c["question"],
                "rules_scope": c["rules_scope"], "rules": rules[c["id"]], "llm": llm[c["id"]],
                "hybrid": hybrid(c, rules[c["id"]], llm[c["id"]])} for c in cases]

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{args.model.replace('/', '--').replace(':', '_')}.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    print(report(records, args.model))
    print(f"\nSaved {out.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
