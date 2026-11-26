"""Offline tests for the LLM text-to-SQL path and the eval. No network, no API key:
the model is replaced by a fake client that returns canned responses.

    venv/bin/python tests/test_llm_sql.py
    venv/bin/python -m pytest tests

They run against evals/snapshot.db (committed), so they work in CI.
"""

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ["COURTSIDE_OFFLINE"] = "1"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import handlers  # noqa: E402
from engine.llm_sql import (QueryTimeout, UnsafeSQL, answer, ask_hybrid,  # noqa: E402
                            cost_usd, run_readonly, system_prompt, EXAMPLES)
from evals.run import CATEGORY_ORDER, load_cases  # noqa: E402
from evals.scoring import grade_llm, grade_rules, score, value_match  # noqa: E402

SNAPSHOT = ROOT / "evals" / "snapshot.db"
CURRY_GAMES = "SELECT COUNT(*) FROM games WHERE player_name = 'Stephen Curry'"


class FakeClient:
    """Stands in for anthropic.Anthropic(). Each create() pops the next canned reply:
    a dict (returned as the model's JSON), an Exception (raised), or "refusal"."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        usage = SimpleNamespace(input_tokens=100, output_tokens=50,
                                cache_creation_input_tokens=0, cache_read_input_tokens=2000)
        if reply == "refusal":
            return SimpleNamespace(stop_reason="refusal", content=[], usage=usage)
        text = SimpleNamespace(type="text", text=json.dumps(reply))
        return SimpleNamespace(stop_reason="end_turn", content=[text], usage=usage)


def sql(query):
    return {"sql": query, "cannot_answer": ""}


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- read-only guards

def test_reads_work():
    columns, rows, truncated = run_readonly(CURRY_GAMES, SNAPSHOT)
    assert rows[0][0] > 1000 and not truncated


def test_writes_and_escapes_are_refused():
    before = _digest(SNAPSHOT)
    attacks = ["DELETE FROM games",
               "WITH x AS (SELECT 1) DELETE FROM games",   # starts with WITH, still a write
               "UPDATE games SET pts = 100",
               "DROP TABLE games",
               "PRAGMA writable_schema = ON",
               "ATTACH DATABASE '/tmp/x.db' AS x",
               "SELECT 1; DROP TABLE games"]
    for attack in attacks:
        try:
            run_readonly(attack, SNAPSHOT)
        except (UnsafeSQL, sqlite3.Error):
            continue
        raise AssertionError(f"ran: {attack}")
    assert _digest(SNAPSHOT) == before, "snapshot changed"


def test_runaway_query_times_out():
    forever = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT MAX(n) FROM r"
    try:
        run_readonly(forever, SNAPSHOT, timeout_s=0.2)
    except QueryTimeout:
        return
    raise AssertionError("no timeout")


def test_row_cap():
    _, rows, truncated = run_readonly("SELECT * FROM games", SNAPSHOT, max_rows=50)
    assert len(rows) == 50 and truncated


def test_prompt_examples_run():
    prompt = system_prompt(SNAPSHOT)
    assert "Nikola Jokić" in prompt and "{" not in prompt.split("EXAMPLES")[0].replace("{day}", "")
    for _, query in EXAMPLES:
        run_readonly(query, SNAPSHOT)


# ---------------------------------------------------------------- answer(): mocked model

def test_answer_ok():
    client = FakeClient(sql(CURRY_GAMES))
    r = answer("How many games has Curry played?", client=client, db_path=SNAPSHOT)
    assert r["status"] == "ok" and r["rows"][0][0] > 1000 and r["attempts"] == 1
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert r["cost_usd"] > 0


def test_bad_sql_is_repaired_once():
    client = FakeClient(sql("SELECT pointz FROM games"), sql(CURRY_GAMES))
    r = answer("q", client=client, db_path=SNAPSHOT)
    assert r["status"] == "ok" and r["attempts"] == 2
    retry = client.calls[1]["messages"]
    assert retry[-1]["role"] == "user" and "no such column" in retry[-1]["content"]


def test_gives_up_after_two_failures():
    client = FakeClient(sql("SELECT nope FROM games"), sql("SELECT still_nope FROM games"))
    r = answer("q", client=client, db_path=SNAPSHOT)
    assert r["status"] == "sql_error" and r["attempts"] == 2 and not r["rows"]


def test_write_is_not_retried():
    client = FakeClient(sql("DELETE FROM games"), sql(CURRY_GAMES))
    r = answer("Delete Curry's games", client=client, db_path=SNAPSHOT)
    assert r["status"] == "unsafe" and len(client.calls) == 1


def test_cannot_answer_refusal_and_api_error():
    r = answer("q", client=FakeClient({"sql": "", "cannot_answer": "no playoff data"}),
               db_path=SNAPSHOT)
    assert r["status"] == "cannot_answer" and "playoff" in r["reason"]
    r = answer("q", client=FakeClient("refusal"), db_path=SNAPSHOT)
    assert r["status"] == "cannot_answer"
    r = answer("q", client=FakeClient(ConnectionError("offline")), db_path=SNAPSHOT)
    assert r["status"] == "api_error"


def test_local_model_path():
    """ollama/<name> goes to the local server with the same schema; no Anthropic client."""
    from engine import llm_sql
    sent = []

    def fake_post(path, body, timeout=600):
        sent.append((path, body))
        return {"message": {"content": json.dumps(sql(CURRY_GAMES))},
                "prompt_eval_count": 1800, "eval_count": 40}

    old = llm_sql._ollama_post
    llm_sql._ollama_post = fake_post
    try:
        r = answer("How many games has Curry played?", model="ollama/qwen2.5-coder:3b",
                   db_path=SNAPSHOT)
    finally:
        llm_sql._ollama_post = old
    assert r["status"] == "ok" and r["rows"][0][0] > 1000 and r["cost_usd"] == 0.0
    path, body = sent[0]
    assert path == "/api/chat" and body["model"] == "qwen2.5-coder:3b"
    assert body["format"]["required"] == ["sql", "cannot_answer"]
    assert body["messages"][0]["role"] == "system"


def test_cost():
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert abs(cost_usd("claude-opus-5-5", usage) - 4.0) < 1e-9
    assert cost_usd("claude-opus-5-5", {"cache_read_input_tokens": 1_000_000}) == 0.2
    assert cost_usd("some-other-model", usage) == 0.0


def test_hybrid_uses_rules_first():
    import tempfile
    old = handlers.DB_PATH, handlers._FAILED_PATH
    handlers.DB_PATH = SNAPSHOT
    handlers._FAILED_PATH = Path(tempfile.mkdtemp()) / "failed_queries.jsonl"
    try:
        client = FakeClient()  # no replies: any LLM call would fail
        r = ask_hybrid("Career-high Steph Curry", client=client, db_path=SNAPSHOT)
        assert r["source"] == "rules" and not client.calls

        client = FakeClient(sql("SELECT COUNT(*) FROM games WHERE player_name = 'Paul Reed'"))
        r = ask_hybrid("How many games has Paul Reed played in total?", client=client,
                       db_path=SNAPSHOT)
        assert r["source"] == "llm" and r["status"] == "ok"
    finally:
        handlers.DB_PATH, handlers._FAILED_PATH = old


# ---------------------------------------------------------------- scoring

def test_value_match():
    assert value_match(30.1, 30.06) and not value_match(30.1, 30.2)
    assert value_match(8, 8.0) and not value_match(8, 9)
    assert value_match(0.454, 0.4537) and value_match(0.454, 45.4) and value_match(0.454, "45.4%")
    assert value_match("Luka Dončić", "luka doncic")
    assert value_match("2015-16", "22015")
    assert value_match("Jan 03, 2021", "2021-01-03")
    assert value_match(26806, "26,806")


def test_score_modes():
    pick = {"expected": [["Luka Dončić"]], "match": "ordered"}
    assert score(pick, [["Luka Dončić", 33.5], ["Shai Gilgeous-Alexander", 31.0]])
    assert not score(pick, [["Shai Gilgeous-Alexander", 33.5], ["Luka Dončić", 31.0]])
    compare = {"expected": [["Stephen Curry", 24.8], ["Luka Dončić", 29.4]], "match": "rows"}
    assert score(compare, [["Luka Dončić", 29.4], ["Stephen Curry", 24.8]])
    assert not score(compare, [["Luka Dončić", 24.8], ["Stephen Curry", 29.4]])  # swapped
    count = {"expected": [[10]], "match": "values"}
    assert score(count, [[10]])
    # a number buried in a big result doesn't count as the answer
    assert not score(count, [[1, 2], [3, 10], [5, 6], [7, 8]])


def test_rules_game_log_is_not_the_answer():
    case = {"category": "beyond_rules", "rules_scope": False,
            "expected": [[10]], "match": "values"}
    import pandas as pd
    result = {"category": "game_count", "answer": "Stephen Curry has 329 games with 30+ pts.",
              "data": {"count": 329},
              "table": pd.DataFrame({"pts": [32, 36], "ast": [7, 10]})}
    assert grade_rules(case, result) == (False, "wrong_value")


def test_unanswerable_grading():
    case = {"category": "unanswerable", "expected": None}
    assert grade_llm(case, {"status": "cannot_answer"})[0]
    assert grade_llm(case, {"status": "ok", "rows": []})[0]
    assert grade_llm(case, {"status": "ok", "rows": [[3]]}) == (False, "answered_unanswerable")
    assert grade_rules(case, {"category": "unknown"})[0]


# ---------------------------------------------------------------- the test set

def test_question_set():
    cases = load_cases()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)) and len(cases) >= 100
    counts = {cat: sum(c["category"] == cat for c in cases) for cat in CATEGORY_ORDER}
    assert all(n >= 6 for n in counts.values()), counts
    assert set(c["category"] for c in cases) <= set(CATEGORY_ORDER)


def test_gold_sql_reproduces_expected():
    """Every stored answer is what its gold SQL returns on the snapshot."""
    for case in load_cases():
        if case["gold_sql"] is None:
            assert case["category"] == "unanswerable"
            continue
        _, rows, _ = run_readonly(case["gold_sql"], SNAPSHOT)
        assert rows == case["expected"], f"{case['id']}: {rows} != {case['expected']}"


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {test.__name__}\n{e}")
    sys.exit(1 if failed else 0)
