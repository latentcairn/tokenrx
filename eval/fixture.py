# -*- coding: utf-8 -*-
"""평가용 가짜 프로젝트(ledgerkit), 대본, 숨은 채점 스크립트를 seed 하나로 늘 똑같이 만든다.

    files, spec = build("autocompact", seed)
    write(dest, files)

files는 모델이 보는 샌드박스({상대 경로: 내용}). spec에는 대본(script), 정답(needles, truth),
숨은 채점 스크립트(hidden), 가짜 claude가 쓰는 모범 답안(edits, mutations, evidence)이 들어 있다.
숨은 채점 스크립트는 대화가 다 끝난 뒤에만 샌드박스에 넣는다.
"""
import hashlib
import json
import os
import random

from . import sim

FILE_TOKENS = 14_000   # 큰 파일 하나의 목표 크기(Read 도구 한도 25K보다 넉넉히 작게)

VERBS = [("reconcile", "reconciles"), ("normalize", "normalizes"), ("aggregate", "aggregates"),
         ("validate", "validates"), ("merge", "merges"), ("resolve", "resolves"), ("allocate", "allocates"),
         ("settle", "settles"), ("post", "posts"), ("rebalance", "rebalances"), ("audit", "audits"),
         ("archive", "archives"), ("index", "indexes"), ("load", "loads"), ("render", "renders"),
         ("parse", "parses"), ("score", "scores"), ("schedule", "schedules"), ("match", "matches"),
         ("collect", "collects"), ("flush", "flushes"), ("route", "routes")]
NOUNS = ["entries", "postings", "accounts", "ledger", "batch", "journal", "invoice", "payment", "refund",
         "balance", "currency", "period", "statement", "transfer", "fee", "rate", "voucher", "tax", "line",
         "bucket", "cursor", "snapshot", "receipt", "settlement", "vendor", "budget", "forecast", "export"]
ADJS = ["pending", "daily", "monthly", "open", "closed", "foreign", "net", "gross", "partial", "stale",
        "late", "manual", "nightly", "local", "shared", "draft"]
TEAMS = ["platform", "billing", "risk", "data", "ops", "treasury"]


def _sentence(rng):
    v, vs = rng.choice(VERBS)
    n1, n2, n3 = rng.sample(NOUNS, 3)
    a = rng.choice(ADJS)
    return rng.choice([
        f"The {n1} service {vs} {a} {n2} before they reach the {n3} stage.",
        f"When a {n1} record is seen twice, the second copy is ignored and a warning is logged.",
        f"Operators can override the default {n1} limit with the `LK_{n2.upper()}_LIMIT` setting.",
        f"{n1.capitalize()} older than {rng.randint(7, 400)} days are moved to cold storage every night.",
        f"Each {a} {n1} keeps a reference to its {n2}, so callers must {v} them in the same transaction.",
        f"The {rng.choice(TEAMS)} team owns the {n1} pipeline; ask them before changing how {n2} are stored.",
        f"If the {n1} feed is late, the scheduler retries {rng.randint(2, 9)} times with a linear back-off.",
        f"Amounts are kept as Decimal end to end; converting {n1} to float is a known source of drift.",
        f"Use {v}_{n1}() rather than touching the {n2} table directly.",
        f"A {a} {n1} cannot be edited once its {n2} is closed; create an adjusting {n3} instead.",
    ])


def _doc(rng, n):
    return " ".join(_sentence(rng) for _ in range(n))


# 함수 틀: (이름, rng) → 줄 목록. 일부는 리뷰에서 찾을 만한 버그를 일부러 넣는다.
def _f_filter(name, rng):
    bug = rng.random() < 0.25
    return [f"def {name}(entries, *, limit={rng.choice([50, 100, 250, 500])}, strict=False):",
            f'    """{_doc(rng, 2)}',
            "",
            "    Entries without an account are skipped, or raise ValueError when ``strict`` is set.",
            '    """',
            "    result = []",
            "    seen = set()",
            "    for idx, entry in enumerate(entries):",
            f"        if idx {'>' if bug else '>='} limit:",
            "            break",
            '        account = entry.get("account")',
            "        if not account:",
            "            if strict:",
            '                raise ValueError(f"entry {idx} has no account")',
            "            continue",
            '        key = (account, entry.get("date"))',
            "        if key in seen:",
            "            continue",
            "        seen.add(key)",
            "        result.append(entry)",
            "    return result"]


def _f_totals(name, rng):
    bug = rng.random() < 0.25
    field = rng.choice(["amount", "net", "gross", "fee"])
    conv = 'float(row.get(field, 0))' if bug else 'Decimal(str(row.get(field, "0")))'
    return [f'def {name}(rows, field="{field}"):',
            f'    """Return the total of ``field`` per account. {_doc(rng, 1)}"""',
            "    totals = {}",
            "    for row in rows:",
            f"        value = {conv}",
            f'        account = row.get("account", "{rng.choice(NOUNS)}")',
            '        totals[account] = totals.get(account, Decimal("0")) + value',
            "    return totals"]


def _f_window(name, rng):
    bug = rng.random() < 0.25
    cond = "start < day <= end" if bug else "start <= day < end"
    return [f"def {name}(items, start, end):",
            f'    """Keep the {rng.choice(NOUNS)} whose date falls in [start, end). {_doc(rng, 1)}"""',
            "    out = []",
            "    for item in items:",
            '        day = datetime.date.fromisoformat(item["date"])',
            f"        if {cond}:",
            "            out.append(item)",
            "    return out"]


def _f_memo(name, rng):
    bug = rng.random() < 0.3
    return [f"def {name}(key, loader, cache={'{}' if bug else 'None'}):",
            f'    """Memoize {rng.choice(NOUNS)} lookups by key. {_doc(rng, 1)}"""',
            *([] if bug else ["    if cache is None:", "        cache = {}"]),
            "    if key not in cache:",
            "        cache[key] = loader(key)",
            "    return cache[key]"]


def _f_config(name, rng):
    return [f"def {name}(text):",
            f'    """Parse ``key=value`` lines of a {rng.choice(NOUNS)} config. {_doc(rng, 1)}"""',
            "    config = {}",
            "    for lineno, raw in enumerate(text.splitlines(), 1):",
            "        line = raw.strip()",
            '        if not line or line.startswith("#"):',
            "            continue",
            '        if "=" not in line:',
            '            raise ValueError(f"line {lineno}: expected key=value")',
            '        key, _, value = line.partition("=")',
            "        config[key.strip()] = value.strip()",
            "    return config"]


def _f_retry(name, rng):
    bug = rng.random() < 0.25
    rng_expr = "range(1, attempts)" if bug else "range(1, attempts + 1)"
    return [f"def {name}(operation, attempts={rng.randint(2, 6)}, delay={rng.choice([0.1, 0.5, 1.0])}):",
            f'    """Run ``operation`` up to ``attempts`` times. {_doc(rng, 1)}"""',
            "    last = None",
            f"    for attempt in {rng_expr}:",
            "        try:",
            "            return operation()",
            "        except Exception as exc:  # noqa: BLE001",
            "            last = exc",
            f'            log.warning("{name} attempt %d failed: %s", attempt, exc)',
            "            time.sleep(delay * attempt)",
            f'    raise RuntimeError("{name} failed") from last']


def _f_pages(name, rng):
    return [f"def {name}(items, page_size={rng.choice([20, 50, 100])}):",
            f'    """Split {rng.choice(NOUNS)} into pages. {_doc(rng, 1)}"""',
            "    pages = []",
            "    for start in range(0, len(items), page_size):",
            "        pages.append(items[start:start + page_size])",
            "    return pages"]


def _f_class(name, rng):
    camel = "".join(p.capitalize() for p in name.split("_"))
    return [f"class {camel}:",
            f'    """{_doc(rng, 2)}"""',
            "",
            f"    def __init__(self, capacity={rng.choice([64, 128, 1024])}):",
            "        self.capacity = capacity",
            "        self._items = collections.OrderedDict()",
            "",
            "    def add(self, account, value):",
            "        if account in self._items:",
            "            self._items.move_to_end(account)",
            "        self._items[account] = value",
            "        if len(self._items) > self.capacity:",
            "            self._items.popitem(last=False)",
            "",
            "    def get(self, account, default=None):",
            "        return self._items.get(account, default)",
            "",
            "    def __len__(self):",
            "        return len(self._items)"]


TEMPLATES = [_f_filter, _f_totals, _f_window, _f_memo, _f_config, _f_retry, _f_pages, _f_class]
HEADER = ["import collections", "import datetime", "import logging", "import time", "from decimal import Decimal",
          "", "log = logging.getLogger(__name__)"]


def _module(rng, title, target=FILE_TOKENS, head=()):
    """target 토큰 정도의 그럴듯한 파이썬 모듈. head는 맨 앞에 넣을 함수 줄들."""
    lines = ['"""' + title + ".", "", _doc(rng, 4), "", _doc(rng, 3), '"""'] + HEADER + ["", ""]
    if head:
        lines += list(head) + ["", ""]
    names = set()
    while sim.tokens("\n".join(lines)) < target:
        v = rng.choice(VERBS)[0]
        name = f"{v}_{rng.choice(ADJS)}_{rng.choice(NOUNS)}"
        if name in names:
            continue
        names.add(name)
        lines += rng.choice(TEMPLATES)(name, rng) + ["", ""]
    return "\n".join(lines).rstrip() + "\n"


def _markdown(rng, title, target=FILE_TOKENS):
    out = [f"# {title}", "", _doc(rng, 4), ""]
    while sim.tokens("\n".join(out)) < target:
        n = rng.choice(NOUNS)
        out += [f"## {rng.choice(ADJS).capitalize()} {n}", "", _doc(rng, rng.randint(3, 6)), ""]
        kind = rng.random()
        if kind < 0.3:
            out += [f"- {_sentence(rng)}" for _ in range(rng.randint(3, 5))] + [""]
        elif kind < 0.5:
            out += ["| Field | Type | Notes |", "|---|---|---|"]
            out += [f"| {rng.choice(NOUNS)}_{rng.choice(['id', 'at', 'count', 'state'])} | "
                    f"{rng.choice(['str', 'int', 'Decimal', 'date'])} | {_sentence(rng)} |" for _ in range(4)] + [""]
        elif kind < 0.6:
            v = rng.choice(VERBS)[0]
            out += ["```python", f"from ledgerkit.core import {n}", f"{n}.{v}_{rng.choice(ADJS)}_{n}(rows)", "```", ""]
    return "\n".join(out).rstrip() + "\n"


def _package(rng, pkg, count, used=()):
    files, used = {}, set(used)
    for _ in range(count):
        name = rng.choice([n for n in NOUNS if n not in used])
        used.add(name)
        files[f"{pkg}/{name}.py"] = _module(rng, f"{name.capitalize()} helpers for ledgerkit")
    files[f"{pkg}/__init__.py"] = f'"""{pkg.split("/")[-1]} package."""\n'
    return files


def _docs(rng, names):
    return {f"docs/{n}.md": _markdown(rng, n.replace("-", " ").capitalize()) for n in names}


BASE = {
    "README.md": "# ledgerkit\n\nSmall double-entry ledger toolkit used by our finance services.\n\n"
                 "- `ledgerkit/core`: money, dates, postings\n- `ledgerkit/io`: readers and writers\n"
                 "- `ledgerkit/services`: batch jobs\n- `docs/`: design notes\n\n"
                 "Run the tests with `python -m unittest discover -s tests`.\n",
    "ledgerkit/__init__.py": '"""ledgerkit: double-entry ledger toolkit."""\n__version__ = "0.9.3"\n',
    "tests/test_smoke.py": "import unittest\n\nimport ledgerkit\n\n\nclass SmokeTest(unittest.TestCase):\n"
                           "    def test_version(self):\n        self.assertTrue(ledgerkit.__version__)\n\n\n"
                           'if __name__ == "__main__":\n    unittest.main()\n',
}

READ_ALL = "Read every .py file in {pkg}/ completely (they are long: read all of each file, not just the start)."
HIDDEN_NAME = "_tokenrx_hidden_check.py"


def write(dest, files):
    for rel, text in files.items():
        path = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


def file_tokens(files):
    return {rel: sim.tokens(text) for rel, text in files.items()}


def build(exp, seed):
    if exp == "autocompact":
        return _autocompact(seed)
    if exp == "handoff":
        return _handoff(seed)
    raise ValueError(exp)


def _step(sid, text, reads=(), extra_requests=0, extra_tokens=0, out=800, learn=(), edits=None, mutations=None,
          reply="", phase="task"):
    return {"id": sid, "text": text, "phase": phase, "reads": list(reads), "extra_requests": extra_requests,
            "extra_tokens": extra_tokens, "out": out, "learn": list(learn), "edits": edits or {},
            "mutations": mutations or {}, "reply": reply}


def _review(sid, pkg, files, learn=()):
    reads = sorted(r for r in files if r.startswith(pkg + "/") and not r.endswith("__init__.py"))
    text = (f"Review the code in {pkg}/ for bugs. " + READ_ALL.format(pkg=pkg) +
            " Then list the 5 most serious problems you found as `path:line - problem`. Don't change any files yet.")
    return _step(sid, text, reads, extra_requests=1, extra_tokens=200, out=1200, learn=learn,
                 reply="Findings:\n" + "\n".join(f"- {r}:{12 + 7 * i} - possible off-by-one" for i, r in enumerate(reads[:5])))


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SEP_DESC = {";": "a semicolon (`;`)", "|": "a pipe (`|`)", "\t": "a tab character",
            " | ": "` | ` (space, pipe, space)", " :: ": "` :: ` (space, two colons, space)"}

# ---------------------------------------------------------------- 실험 1: 자동 압축 기준 400K

MONEY_BUG = '    return "%.2f" % round(float(value), 2)'
MONEY_FIX = '    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP"))'


def _autocompact(seed):
    rng = random.Random(f"autocompact:{seed}")
    files = dict(BASE)
    protected_name = rng.choice(["fmt", "numfmt", "formatting"])
    for pkg in ("ledgerkit/core", "ledgerkit/io", "ledgerkit/services", "ledgerkit/plugins"):
        files.update(_package(rng, pkg, 8 if pkg != "ledgerkit/core" else 7))
    protected = f"ledgerkit/core/{protected_name}.py"
    money = ["def money(value):", '    """Format an amount with two decimals, rounding half up."""', MONEY_BUG]
    files[protected] = _module(rng, "Number formatting shared with the billing service", head=money)
    files.update(_docs(rng, ["architecture", "plugins", "postings", "exports", "operations", "currencies",
                             "retention", "glossary"]))
    files["ledgerkit/export/__init__.py"] = '"""Exporters."""\n'
    files["ledgerkit/export/report.py"] = ('"""Export report for ledger entries."""\n\n\n'
                                          "def build_report(entries):\n    raise NotImplementedError\n")
    files["tests/test_report.py"] = VISIBLE_REPORT_TEST

    n = rng.randint(1200, 9800)
    sep = rng.choice([";", "|", "\t"])
    prefix = rng.choice(["lkx", "rpt", "zq", "ldg", "exr"])
    tag = rng.choice(["x-", "tmp_", "int.", "zz-"])
    ticket = f"{rng.choice(['LK', 'FIN', 'LED'])}-{rng.randint(1000, 9999)}"
    needles = {"max_rows": n, "sep": sep, "naming": prefix, "internal": tag, "protected": protected, "ticket": ticket}

    t1 = ("We'll work in this repo (ledgerkit) for a while: first a code review, then a small feature. "
          "Here are the requirements for the export feature you'll build at the end. Remember them; I won't repeat them.\n\n"
          f"1. In ledgerkit/export/report.py, define MAX_BATCH_ROWS = {n}. build_report() must raise ValueError "
          "when it gets more entries than that.\n"
          f"2. Report fields are separated by {SEP_DESC[sep]}, never by commas.\n"
          f"3. Naming rule: every function you define in report.py other than build_report must start with `{prefix}_`.\n"
          f"4. Entries whose account starts with `{tag}` are internal: leave them out of the report lines and the total.\n"
          f"5. {protected} is shared with another team. Never modify it, not even to fix a bug; "
          "work around problems in your own code.\n"
          f"6. This work is ticket {ticket}. Put the ticket id in report.py's module docstring.\n\n"
          "Don't write any code yet. Reply with one short line confirming.")
    impl = ("Now implement build_report(entries) in ledgerkit/export/report.py.\n"
            "Input: a list of dicts with 'account' (str), 'date' ('YYYY-MM-DD') and 'amount' (a decimal string such as "
            "'12.5' or '-3').\n"
            "Output: a string. The first line is the header: account, date and amount joined by the separator. Then one "
            "line per entry, sorted by date and then account, with the amount rounded half-up to 2 decimal places. The "
            "last line is TOTAL, an empty field and the sum of the amounts (2 decimal places), joined by the separator. "
            "Every line ends with a newline.\n"
            "Also add a function that returns per-account totals as a dict of account -> Decimal.\n"
            "Follow all the requirements I gave you at the start, and use the project's existing helpers where it makes "
            "sense. When you're done, run `python -m unittest discover -s tests` and fix any failures without breaking "
            "the requirements.")
    quiz = ("Last question. Answer from memory, without opening or searching any files: what were the specifics of the "
            "six requirements I gave you at the start? Reply with only a JSON object with these keys: max_batch_rows "
            "(number), separator (the character itself; write \"\\t\" for a tab), helper_prefix (the required "
            "function-name prefix, without the trailing underscore), internal_prefix (the account prefix of internal "
            "entries), protected_file (repo-relative path), ticket.")

    rp = "ledgerkit/export/report.py"
    script = [_step("needles", t1, out=60, learn=list(needles), reply="Got it: six requirements noted."),
              _review("review-core", "ledgerkit/core", files),
              _review("review-io", "ledgerkit/io", files),
              _review("review-services", "ledgerkit/services", files),
              _review("review-plugins", "ledgerkit/plugins", files),
              _step("review-docs", "Read every file in docs/ completely (they are long: read all of each file) and list "
                    "up to 5 places where the docs and the code you've seen disagree. Don't change any files.",
                    sorted(r for r in files if r.startswith("docs/")), extra_requests=1, extra_tokens=200, out=1000,
                    reply="- docs/postings.md says amounts are floats; the code uses Decimal."),
              _step("impl", impl, [rp, "tests/test_report.py", protected], extra_requests=6, extra_tokens=6000, out=600,
                    edits={rp: REPORT_REF.format(n=n, sep=sep, prefix=prefix, tag=tag, ticket=ticket)},
                    mutations={"max_rows": [[rp, f"MAX_BATCH_ROWS = {n}", "MAX_BATCH_ROWS = 1000"]],
                               "sep": [[rp, f"SEPARATOR = {sep!r}", "SEPARATOR = ','"]],
                               "naming": [[rp, f"{prefix}_", "_"]],
                               "internal": [[rp, f"INTERNAL_PREFIX = {tag!r}", "INTERNAL_PREFIX = '\\x00'"]],
                               "protected": [[protected, MONEY_BUG, MONEY_FIX]],
                               "ticket": [[rp, f"Ticket: {ticket}\n", ""]]},
                    reply="Implemented build_report; all tests pass.")]
    truth = {"max_batch_rows": n, "separator": sep, "helper_prefix": prefix, "internal_prefix": tag,
             "protected_file": protected, "ticket": ticket}
    kinds = {"max_batch_rows": "int", "separator": "sep", "helper_prefix": "prefix", "internal_prefix": "text",
             "protected_file": "path", "ticket": "text"}
    params = {"max_rows": n, "sep": sep, "prefix": prefix, "tag": tag, "protected": protected,
              "protected_sha": _sha(files[protected]), "ticket": ticket}
    spec = {"exp": "autocompact", "seed": seed, "needles": needles, "script": script,
            "quiz_step": _quiz_step(quiz, {"max_batch_rows": "max_rows", "separator": "sep", "helper_prefix": "naming",
                                           "internal_prefix": "internal", "protected_file": "protected",
                                           "ticket": "ticket"}),
            "truth": truth, "kinds": kinds, "checks": list(needles) + ["visible_tests"],
            "hidden": HIDDEN_HEAD + json.dumps(params) + HIDDEN_MID + HIDDEN_AUTOCOMPACT + HIDDEN_TAIL,
            "evidence": {}}
    return files, spec


def _quiz_step(text, keymap):
    s = _step("quiz", text, out=200, phase="quiz")
    s["quiz"] = keymap
    return s


VISIBLE_REPORT_TEST = '''import unittest

from ledgerkit.export import report

ROWS = [
    {"account": "rent", "date": "2026-03-02", "amount": "1200"},
    {"account": "coffee", "date": "2026-03-01", "amount": "2.675"},
    {"account": "bank", "date": "2026-03-02", "amount": "-15.5"},
]


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.lines = report.build_report(ROWS).splitlines()
        header = self.lines[0]
        self.assertTrue(header.startswith("account") and header.endswith("amount"), header)
        self.sep = header[len("account"):header.index("date")]
        self.assertTrue(self.sep)

    def test_header(self):
        self.assertEqual(self.lines[0], self.sep.join(("account", "date", "amount")))

    def test_sorted_by_date_then_account(self):
        body = [line.split(self.sep) for line in self.lines[1:-1]]
        self.assertEqual([r[0] for r in body], ["coffee", "bank", "rent"])

    def test_rounding_half_up(self):
        body = {line.split(self.sep)[0]: line.split(self.sep) for line in self.lines[1:-1]}
        self.assertEqual(body["coffee"][2], "2.68")
        self.assertEqual(body["bank"][2], "-15.50")
        self.assertEqual(body["rent"][2], "1200.00")

    def test_total_line(self):
        self.assertEqual(self.lines[-1].split(self.sep), ["TOTAL", "", "1187.18"])

    def test_trailing_newline(self):
        self.assertTrue(report.build_report(ROWS).endswith("\\n"))


if __name__ == "__main__":
    unittest.main()
'''

REPORT_REF = '''"""Export report for ledger entries.

Ticket: {ticket}
"""
from decimal import ROUND_HALF_UP, Decimal

MAX_BATCH_ROWS = {n}
SEPARATOR = {sep!r}
INTERNAL_PREFIX = {tag!r}


def {prefix}_amount(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def {prefix}_visible(entries):
    return [e for e in entries if not str(e["account"]).startswith(INTERNAL_PREFIX)]


def {prefix}_totals(entries):
    totals = {{}}
    for e in {prefix}_visible(entries):
        totals[e["account"]] = totals.get(e["account"], Decimal("0")) + Decimal(str(e["amount"]))
    return totals


def build_report(entries):
    entries = list(entries)
    if len(entries) > MAX_BATCH_ROWS:
        raise ValueError("too many entries: %d > %d" % (len(entries), MAX_BATCH_ROWS))
    rows = sorted({prefix}_visible(entries), key=lambda e: (e["date"], e["account"]))
    lines = [SEPARATOR.join(("account", "date", "amount"))]
    total = Decimal("0")
    for e in rows:
        amount = {prefix}_amount(e["amount"])
        total += amount
        lines.append(SEPARATOR.join((e["account"], e["date"], str(amount))))
    lines.append(SEPARATOR.join(("TOTAL", "", str({prefix}_amount(total)))))
    return "\\n".join(lines) + "\\n"
'''

# ---------------------------------------------------------------- 실험 2: 자리비움 경고 인수인계

def _handoff(seed):
    rng = random.Random(f"handoff:{seed}")
    files = dict(BASE)
    for pkg, count in (("ledgerkit/core", 5), ("ledgerkit/io", 4), ("ledgerkit/services", 3)):
        files.update(_package(rng, pkg, count))
    files.update(_docs(rng, ["architecture", "plugins"]))
    files["ledgerkit/plugins/__init__.py"] = '"""Plugins."""\n'
    files["ledgerkit/cli/__init__.py"] = '"""Command line tools."""\n'

    team = rng.choice(["ledger-core", "payments-infra", "recon-squad", "fin-platform"])
    err = rng.choice(["DuplicatePluginError", "PluginAlreadyRegistered", "PluginNameConflict"])
    p1 = rng.choice([10, 50, 100])
    p2 = rng.choice([15, 25, 35, 40, 60, 75])
    envvar = rng.choice(["LK_DISABLED_PLUGINS", "LEDGERKIT_PLUGINS_OFF", "LKX_MUTE"])
    sep = rng.choice(["\t", " | ", " :: "])
    empty = f"E{rng.randint(100, 999)}: " + rng.choice(["no plugins registered", "plugin list is empty", "nothing to list"])
    needles = {"owner": team, "error_class": err, "empty": empty, "sep": sep, "env_var": envvar, "default_priority": p2}

    reg, treg, cli, tcli = ("ledgerkit/plugins/registry.py", "tests/test_registry.py", "ledgerkit/cli/plugins.py",
                            "tests/test_cli.py")
    owner_rule = f"every new Python module you create must start with the exact line `# owner: {team}`."
    empty_rule = f"when we build the CLI, `plugins list` with nothing registered must print exactly `{empty}`."
    sep_rule = f"It prints one line per plugin: the name, then {SEP_DESC[sep]}, then the priority, in ordered() order."
    env_rule = f"Plugins named in the environment variable {envvar} (comma-separated) are left out of the list."
    p2_rule = f"the default priority should be {p2}, not {p1}."
    fmt = dict(team=team, err=err, sep=sep, envvar=envvar, empty=empty)
    script = [
        _step("orient", "Over the next few steps we'll add a plugin registry to ledgerkit. First get oriented: "
              + READ_ALL.format(pkg="ledgerkit/core") + " Summarize the architecture in at most 6 bullets. "
              "A house rule for this whole task: " + owner_rule,
              sorted(r for r in files if r.startswith("ledgerkit/core/") and not r.endswith("__init__.py")),
              extra_requests=1, out=900, learn=["owner"], reply="- core holds money, dates and postings helpers."),
        _step("registry", "Decisions for the registry: plugin names are lowercase kebab-case (regex ^[a-z][a-z0-9-]*$); "
              f"registering a name twice raises {err}, a subclass of KeyError defined in the registry module; the default "
              f"priority is {p1}; ordered() returns (name, priority) tuples sorted by ascending priority, ties broken by "
              f"name. Implement it in {reg} as a class PluginRegistry with register(name, factory, priority=<default>), "
              f"get(name) and ordered(). Add tests in {treg} and run `python -m unittest discover -s tests`. "
              "One more decision for later (no code for it yet): " + empty_rule,
              ["ledgerkit/plugins/__init__.py"], extra_requests=5, extra_tokens=4000, out=500,
              learn=["error_class", "empty"],
              edits={reg: REGISTRY_REF.format(p=p1, **fmt), treg: REGISTRY_TEST_REF.format(**fmt)},
              reply="Registry implemented; 5 tests pass."),
        _step("review-io", "Read every .py file in ledgerkit/io/ completely. Does anything there conflict with the "
              "registry design (naming rules, priorities, duplicate handling)? List up to 5 findings. No code changes.",
              sorted(r for r in files if r.startswith("ledgerkit/io/") and not r.endswith("__init__.py")),
              extra_requests=1, out=900, reply="- ledgerkit/io uses CamelCase names for readers."),
        _step("cli-decisions", "Decisions for the CLI we'll build later (no code yet; just confirm in one line): the "
              f"command is `plugins list`. {sep_rule} {env_rule}", out=60, learn=["sep", "env_var"],
              reply="Confirmed."),
        _step("review-services", "Read every .py file in ledgerkit/services/ completely and list up to 5 bugs as "
              "`path:line - problem`. Don't fix them.",
              sorted(r for r in files if r.startswith("ledgerkit/services/") and not r.endswith("__init__.py")),
              extra_requests=1, out=900, reply="- ledgerkit/services: retry loop runs one time too few."),
        _step("priority", "Change of plan: " + p2_rule + " Update the registry and its tests, then run the tests.",
              [reg, treg], extra_requests=3, extra_tokens=1500, out=300, learn=["default_priority"],
              edits={reg: REGISTRY_REF.format(p=p2, **fmt)}, reply=f"Default priority is now {p2}; tests pass."),
        _step("review-docs", "Read docs/architecture.md and docs/plugins.md completely and list anything in them that is "
              "now outdated given our decisions (max 4 bullets). No edits.",
              ["docs/architecture.md", "docs/plugins.md"], out=700, reply="- docs/plugins.md predates the registry."),
        _step("docs", "Add a short 'Plugin registry' section (5-8 lines) to the end of docs/plugins.md describing the "
              "registry API as implemented.", [reg], extra_requests=2, extra_tokens=800, out=200,
              edits={"docs/plugins.md": {"append": DOCS_APPEND}}, reply="Added the section."),
    ]
    followup = _step(
        "followup", f"Next, build the CLI we agreed on: create {cli} with a function main(argv, registry, env=None) that "
        "returns the output text of `plugins list` (argv == ['list']) as a string instead of printing it; env defaults "
        f"to os.environ. Follow every decision we made in this conversation. Add tests in {tcli} and run all the tests.",
        [reg, treg], extra_requests=6, extra_tokens=5000, out=500,
        edits={cli: CLI_REF.format(**fmt), tcli: CLI_TEST_REF.format(**fmt)},
        mutations={"owner": [[cli, f"# owner: {team}\n", ""]], "sep": [[cli, f"SEPARATOR = {sep!r}", "SEPARATOR = ' '"]],
                   "env_var": [[cli, f"ENV_VAR = {envvar!r}", "ENV_VAR = 'PLUGINS_DISABLED'"]],
                   "empty": [[cli, f"EMPTY_MESSAGE = {empty!r}", "EMPTY_MESSAGE = 'No plugins.'"]]},
        reply="CLI added; all tests pass.")
    quiz = ("Last question. Answer from memory, without opening or searching any files. Reply with only a JSON object "
            "with these keys: owner_team (the team in the module header rule), error_class (the duplicate-name "
            "exception), default_priority (the current default, a number), separator (between name and priority; write "
            "\"\\t\" for a tab), env_var (the variable that hides plugins), empty_message (the exact text for an empty "
            "list).")
    truth = {"owner_team": team, "error_class": err, "default_priority": p2, "separator": sep, "env_var": envvar,
             "empty_message": empty}
    kinds = {"owner_team": "text", "error_class": "text", "default_priority": "int", "separator": "sep",
             "env_var": "text", "empty_message": "text"}
    # 새 대화가 메모나 다시 읽은 파일에서 이 글귀를 찾으면 그 결정을 안다고 본다(가짜 claude용)
    evidence = {"owner": [owner_rule, f"# owner: {team}"], "error_class": [f"class {err}", f"raises {err}"],
                "empty": [empty_rule], "sep": [sep_rule], "env_var": [env_rule],
                "default_priority": [p2_rule, f"DEFAULT_PRIORITY = {p2}"]}
    params = {"team": team, "err": err, "p2": p2, "sep": sep, "env_var": envvar, "empty": empty}
    spec = {"exp": "handoff", "seed": seed, "needles": needles, "script": script, "followup": followup,
            "quiz_step": _quiz_step(quiz, {"owner_team": "owner", "error_class": "error_class",
                                           "default_priority": "default_priority", "separator": "sep",
                                           "env_var": "env_var", "empty_message": "empty"}),
            "truth": truth, "kinds": kinds,
            "checks": ["owner", "sep", "env_var", "empty", "default_priority", "registry", "visible_tests"],
            "hidden": HIDDEN_HEAD + json.dumps(params) + HIDDEN_MID + HIDDEN_HANDOFF + HIDDEN_TAIL,
            "evidence": evidence}
    return files, spec


REGISTRY_REF = '''# owner: {team}
"""Plugin registry."""
import re

NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
DEFAULT_PRIORITY = {p}


class {err}(KeyError):
    """Raised when a plugin name is registered twice."""


class PluginRegistry:
    def __init__(self):
        self._plugins = {{}}

    def register(self, name, factory, priority=DEFAULT_PRIORITY):
        if not NAME_RE.match(name):
            raise ValueError("invalid plugin name: %r" % name)
        if name in self._plugins:
            raise {err}(name)
        self._plugins[name] = (factory, priority)

    def get(self, name):
        return self._plugins[name][0]

    def ordered(self):
        return sorted(((n, p) for n, (f, p) in self._plugins.items()), key=lambda x: (x[1], x[0]))
'''

REGISTRY_TEST_REF = '''# owner: {team}
import unittest

from ledgerkit.plugins import registry


class RegistryTest(unittest.TestCase):
    def test_order(self):
        reg = registry.PluginRegistry()
        reg.register("b", object, priority=1)
        reg.register("a", object, priority=1)
        reg.register("c", object, priority=0)
        self.assertEqual(reg.ordered(), [("c", 0), ("a", 1), ("b", 1)])

    def test_duplicate(self):
        reg = registry.PluginRegistry()
        reg.register("a", object)
        with self.assertRaises(registry.{err}):
            reg.register("a", object)

    def test_default_priority(self):
        reg = registry.PluginRegistry()
        reg.register("x", object)
        self.assertEqual(reg.ordered(), [("x", registry.DEFAULT_PRIORITY)])

    def test_bad_name(self):
        with self.assertRaises(ValueError):
            registry.PluginRegistry().register("Bad_Name", object)


if __name__ == "__main__":
    unittest.main()
'''

DOCS_APPEND = '''
## Plugin registry

`ledgerkit.plugins.registry.PluginRegistry` keeps plugins by name.

- `register(name, factory, priority=DEFAULT_PRIORITY)` adds a plugin; names are lowercase kebab-case.
- Registering a name twice raises the registry's duplicate-name error.
- `get(name)` returns the factory.
- `ordered()` returns `(name, priority)` pairs by ascending priority, then name.
'''

CLI_REF = '''# owner: {team}
"""`plugins list` command."""
import os

ENV_VAR = {envvar!r}
SEPARATOR = {sep!r}
EMPTY_MESSAGE = {empty!r}


def main(argv, registry, env=None):
    env = os.environ if env is None else env
    if list(argv) != ["list"]:
        raise SystemExit("usage: plugins list")
    hidden = {{n.strip() for n in env.get(ENV_VAR, "").split(",") if n.strip()}}
    rows = [(n, p) for n, p in registry.ordered() if n not in hidden]
    if not rows:
        return EMPTY_MESSAGE + "\\n"
    return "".join("%s%s%s\\n" % (n, SEPARATOR, p) for n, p in rows)
'''

CLI_TEST_REF = '''# owner: {team}
import unittest

from ledgerkit.cli import plugins
from ledgerkit.plugins.registry import PluginRegistry


class CliTest(unittest.TestCase):
    def registry(self):
        reg = PluginRegistry()
        reg.register("beta", object, priority=2)
        reg.register("alpha", object, priority=1)
        return reg

    def test_list(self):
        sep = plugins.SEPARATOR
        self.assertEqual(plugins.main(["list"], self.registry(), env={{}}), "alpha%s1\\nbeta%s2\\n" % (sep, sep))

    def test_hidden(self):
        out = plugins.main(["list"], self.registry(), env={{plugins.ENV_VAR: "beta"}})
        self.assertEqual(out, "alpha%s1\\n" % plugins.SEPARATOR)

    def test_empty(self):
        self.assertEqual(plugins.main(["list"], PluginRegistry(), env={{}}), plugins.EMPTY_MESSAGE + "\\n")


if __name__ == "__main__":
    unittest.main()
'''

# ---------------------------------------------------------------- 숨은 채점 스크립트(샌드박스 안에서 돈다)

HIDDEN_HEAD = '''# -*- coding: utf-8 -*-
"""TokenRx 평가의 숨은 채점. 대화가 끝난 뒤에만 복사해 넣고 돌린다. 결과는 JSON 한 줄."""
import hashlib
import inspect
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
P = json.loads(r\'\'\''''

HIDDEN_MID = '''\'\'\')
results, errors = {}, {}


def check(name):
    def wrap(fn):
        try:
            results[name] = bool(fn())
        except BaseException as exc:  # noqa: BLE001  모델이 쓴 코드가 무엇을 던지든 실패로 센다
            results[name] = False
            errors[name] = ("%s: %s" % (type(exc).__name__, exc))[:300]
        return fn
    return wrap

'''

HIDDEN_AUTOCOMPACT = '''
try:
    from ledgerkit.export import report
except BaseException as exc:  # noqa: BLE001
    report = None
    errors["import"] = ("%s: %s" % (type(exc).__name__, exc))[:300]


def entry(account, date, amount):
    return {"account": account, "date": date, "amount": amount}


@check("max_rows")
def _max_rows():
    n = P["max_rows"]
    if getattr(report, "MAX_BATCH_ROWS", None) != n:
        return False
    rows = [entry("acct", "2026-01-01", "1") for _ in range(n)]
    report.build_report(rows)
    try:
        report.build_report(rows + [entry("acct", "2026-01-01", "1")])
    except ValueError:
        return True
    return False


@check("sep")
def _sep():
    out = report.build_report([entry("a", "2026-01-01", "1")])
    return out.splitlines()[0] == P["sep"].join(("account", "date", "amount"))


@check("naming")
def _naming():
    own = [n for n, f in inspect.getmembers(report, inspect.isfunction)
           if f.__module__ == report.__name__ and n != "build_report"]
    return bool(own) and all(n.startswith(P["prefix"] + "_") for n in own)


@check("internal")
def _internal():
    rows = [entry("cash", "2026-01-02", "10"), entry(P["tag"] + "probe", "2026-01-01", "99.99"),
            entry("bank", "2026-01-03", "-2.5")]
    lines = report.build_report(rows).splitlines()
    sep = lines[0][len("account"):lines[0].index("date")]   # 구분자 요구 사항과 따로 채점하려고 머리줄에서 읽는다
    body = lines[1:-1]
    return len(body) == 2 and not any(x.startswith(P["tag"]) for x in body) and lines[-1].split(sep)[-1] == "7.50"


@check("protected")
def _protected():
    with open(os.path.join(HERE, *P["protected"].split("/")), "rb") as f:
        return hashlib.sha256(f.read().replace(b"\\r\\n", b"\\n")).hexdigest() == P["protected_sha"]


@check("ticket")
def _ticket():
    return P["ticket"] in (report.__doc__ or "")
'''

HIDDEN_HANDOFF = '''
try:
    from ledgerkit.plugins import registry as regmod
    from ledgerkit.cli import plugins as cli
except BaseException as exc:  # noqa: BLE001
    regmod = cli = None
    errors["import"] = ("%s: %s" % (type(exc).__name__, exc))[:300]


def make(pairs):
    reg = regmod.PluginRegistry()
    for name, prio in pairs:
        if prio is None:
            reg.register(name, object)
        else:
            reg.register(name, object, priority=prio)
    return reg


def run(reg, env):
    return cli.main(["list"], reg, env=env)


@check("owner")
def _owner():
    with open(os.path.join(HERE, "ledgerkit", "cli", "plugins.py"), encoding="utf-8") as f:
        lines = [x.rstrip("\\r\\n") for x in f if x.strip()]
    lines = [x for x in lines if not x.startswith("#!") and "coding" not in x]
    return bool(lines) and lines[0].strip() == "# owner: " + P["team"]


@check("sep")
def _sep():
    out = run(make([("beta", 20), ("alpha", 10)]), {})
    return out.splitlines() == ["alpha%s10" % P["sep"], "beta%s20" % P["sep"]]


@check("env_var")
def _env_var():
    out = run(make([("alpha", 10), ("beta", 20), ("gamma", 30)]), {P["env_var"]: "beta,gamma"})
    lines = out.splitlines()
    return len(lines) == 1 and lines[0].startswith("alpha")


@check("empty")
def _empty():
    return run(regmod.PluginRegistry(), {}).strip() == P["empty"]


@check("default_priority")
def _default_priority():
    reg = make([("solo", None)])
    out = run(reg, {}).strip()
    return [tuple(x) for x in reg.ordered()] == [("solo", P["p2"])] and out.startswith("solo") \\
        and out.endswith(str(P["p2"]))


@check("registry")
def _registry():
    reg = make([("b-one", 5), ("a-two", 5), ("c", 1)])
    if [tuple(x)[0] for x in reg.ordered()] != ["c", "a-two", "b-one"]:
        return False
    err = getattr(regmod, P["err"])
    try:
        reg.register("c", object)
    except err:
        return issubclass(err, KeyError)
    return False
'''

HIDDEN_TAIL = '''
print(json.dumps({"checks": results, "errors": errors}))
'''
