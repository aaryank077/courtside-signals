"""Grade an answer by its values, not its SQL.

Each test case stores `expected`: the rows its gold SQL returns on the frozen
snapshot. An answer is correct when those values show up in what the system
returned. The two systems return different shapes, so each gets a "pool":

- LLM: the rows its SQL returned.
- Rules: the headline answer text and the handler's `data` dict, plus its
  table for list and comparison answers.

match = "values":  every expected value appears somewhere in the pool (numbers
                   only, e.g. a count or a player's averages).
match = "ordered": expected row i's values appear in returned row i. Used for
                   top-k lists and for single picks ("who leads...", "which
                   season..."), so a top-5 list with the right name 3rd fails.
match = "rows":    each expected row appears whole inside one returned row
                   (two players compared: values must sit next to the right name).
The rules' headline sentence counts as part of the first row.

Value equality is forgiving about format, strict about the number:
- integers must match exactly (8 games is not 9)
- other numbers within half a unit of the expected rounding: |x| >= 1 within
  0.051 (30.06 ~ 30.1); |x| < 1 is a proportion, within 0.0015, and 45.4 also
  matches 0.454 (a percent written as a percent)
- names ignore case and accents; '22015' and '2015-16' are the same season;
  'Jan 03, 2021' and '2021-01-03' are the same date
"""

import math
import re
import unicodedata
from datetime import datetime

POOL_ROWS = 10

_SEASON_ID = re.compile(r"^2(\d{4})$")
_SEASON = re.compile(r"\b((?:19|20)\d\d)-(\d\d)\b")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d\d)-(\d\d)\b")
_TEXT_DATE = re.compile(r"\b([A-Z][a-z]{2}) (\d\d), (\d{4})\b")
_NUMBER = re.compile(r"(?<![\w.])-?(?:\d[\d,]*)?\.?\d+%?")


def _plain(text):
    text = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in text if not unicodedata.combining(c)).lower().strip()


def canon(value):
    """One canonical form: float, or normalized string."""
    if value is None:
        return None
    if hasattr(value, "item"):  # numpy scalars
        value = value.item()
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and math.isnan(value) else float(value)
    s = str(value).strip()
    m = _SEASON_ID.match(s)
    if m:
        year = int(m.group(1))
        return f"{year}-{str(year + 1)[-2:]}"
    try:
        return datetime.strptime(s, "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    num = s.replace(",", "")
    try:
        return float(num[:-1]) / 100 if num.endswith("%") else float(num)
    except ValueError:
        return _plain(s)


def _num_match(expected, got):
    if float(expected).is_integer() and abs(expected) >= 1:
        return abs(got - expected) < 1e-6
    tol = 0.051 if abs(expected) >= 1 else 0.0015
    if abs(got - expected) <= tol:
        return True
    # a proportion written as a percent: 45.4 for 0.454
    return abs(expected) < 1 and abs(got / 100 - expected) <= tol


def value_match(expected, got):
    e, g = canon(expected), canon(got)
    if e is None or g is None:
        return False
    if isinstance(e, float) and isinstance(g, float):
        return _num_match(e, g)
    return isinstance(e, str) and isinstance(g, str) and e == g


def text_values(text):
    """Values mentioned in an answer sentence: seasons, dates, numbers, and the
    text itself (for names, matched as substrings)."""
    text = str(text or "")
    out = [f"{a}-{b}" for a, b in _SEASON.findall(text)]
    out += ["-".join(m) for m in _ISO_DATE.findall(text)]
    out += [datetime.strptime(" ".join(m), "%b %d %Y").strftime("%Y-%m-%d")
            for m in _TEXT_DATE.findall(text)]
    stripped = _ISO_DATE.sub(" ", _SEASON.sub(" ", text))
    for n in _NUMBER.findall(stripped):
        out.append(n)
        if n.endswith("%"):
            out.append(n[:-1])  # also as a percent number
    return out


def _flatten(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _flatten(v)
    else:
        yield obj


def _in_pool(expected, cells, text=""):
    if any(value_match(expected, c) for c in cells):
        return True
    e = canon(expected)
    return isinstance(e, str) and bool(text) and e in _plain(text)


def score(case, rows, headline_cells=(), text=""):
    """True if `rows` (list of lists) plus the headline answer contain the expected values."""
    expected = case["expected"]
    rows = [list(r) for r in (rows or [])][:max(POOL_ROWS, len(expected))]
    headline = list(headline_cells)
    match = case.get("match", "values")
    if match == "ordered":
        def row_has(i, exp_row):
            cells = (rows[i] if i < len(rows) else []) + (headline if i == 0 else [])
            return all(_in_pool(e, cells, text if i == 0 else "") for e in exp_row)
        return all(row_has(i, exp_row) for i, exp_row in enumerate(expected))
    if match == "rows":
        return all(any(all(_in_pool(e, r) for e in exp_row) for r in rows)
                   for exp_row in expected)
    # "values": the answer must be the result, not somewhere in supporting rows.
    # Rules: the headline only (their tables are game logs full of small numbers).
    # LLM: a small result (<= 3 rows), or else its first row.
    if headline or text:
        cells = headline
    else:
        cells = [c for r in (rows if len(rows) <= 3 else rows[:1]) for c in r]
    return all(_in_pool(e, cells, text) for exp_row in expected for e in exp_row)


# ---------------------------------------------------------------- per system

def rules_pool(result):
    """(rows, headline cells, text) from a rules result dict."""
    table = result.get("table")
    rows = []
    if table is not None and not getattr(table, "empty", True):
        rows = [list(r) for r in table.itertuples(index=False)]
    data = {k: v for k, v in (result.get("data") or {}).items() if k != "chart"}
    text = result.get("answer", "")
    return rows, list(_flatten(data)) + text_values(text), text


def rules_answered(result):
    return result.get("category") != "unknown" and not result.get("error")


def llm_answered(result):
    return result.get("status") == "ok" and any(
        v is not None for r in result.get("rows", []) for v in r)


def grade_rules(case, result):
    """(correct, failure label or None)."""
    if case["category"] == "unanswerable":
        return (True, None) if not rules_answered(result) else (False, "answered_unanswerable")
    if result.get("category") == "unknown":
        return False, "no_parse"
    if result.get("error"):
        return False, "handler_error"
    rows, cells, text = rules_pool(result)
    if score(case, rows, cells, text):
        return True, None
    if case["rules_scope"] and result.get("category") != case["category"]:
        return False, "wrong_category"
    return False, "wrong_value"


def grade_llm(case, result):
    if case["category"] == "unanswerable":
        return (True, None) if not llm_answered(result) else (False, "answered_unanswerable")
    status = result.get("status")
    if status == "cannot_answer":
        return False, "refused"
    if status != "ok":
        return False, status  # sql_error, timeout, unsafe, api_error
    if not llm_answered(result):
        return False, "empty_result"
    return (True, None) if score(case, result["rows"]) else (False, "wrong_value")
