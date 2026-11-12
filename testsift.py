"""testsift core — exact, local analysis of JUnit XML test reports.

Three tools:

``test_report_summary``   Totals (passed / failed / errors / skipped), failures
                          grouped by a normalised cause signature, the slowest
                          tests and the suites that fail most, over one report
                          file or a directory of them.
``test_report_failures``  The failing tests themselves: id, suite, file/line,
                          failure type, message and a bounded head+tail of the
                          trace, filterable and paged.
``test_report_compare``   Diff two runs: newly failing, fixed, still failing,
                          added and removed tests, and tests that got much slower.

JUnit XML is what pytest (--junitxml), Maven Surefire/Failsafe, Gradle, Jest
(jest-junit), Vitest, Go (go-junit-report/gotestsum), PHPUnit, .NET (JunitXml
logger), Playwright, Cypress and most CI systems emit. Reports from a large suite
do not fit in a model's context; this reads them on disk and returns exact counts.

Standard library only (``xml.parsers.expat``, ``re``, ``os``, ``json``). No
network, no credentials, no background work, no self-update. The only side
effect is reading the paths the caller supplies, read-only. XML is parsed in a
streaming expat parser that refuses DOCTYPE and entity declarations, so entity
expansion ("billion laughs") and external entities cannot be used.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections import Counter
from typing import Any, Callable, Optional
from xml.parsers import expat

# --- Bounds that keep one call cheap and its output readable ----------------------
MAX_FILES = 5_000               # report files read from one directory
MAX_CASES = 2_000_000           # test cases read per call; flagged when hit
READ_CHUNK = 1 << 16            # bytes fed to the parser at a time
DETAIL_CHARS = 4_000            # failure text kept per test (head + tail)
OUTPUT_CHARS = 1_000            # system-out / system-err tail kept per failing test
MESSAGE_CHARS = 1_000
DEFAULT_GROUPS = 10
HARD_GROUPS = 100
DEFAULT_SLOWEST = 10
HARD_SLOWEST = 100
DEFAULT_LIMIT = 20
HARD_LIMIT = 200
DEFAULT_COMPARE_LIMIT = 50
HARD_COMPARE_LIMIT = 500
EXAMPLES_PER_GROUP = 5

# Worst status wins when a test case carries several result elements, and when
# the same test id appears more than once.
_RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
# Surefire reruns: flaky* = failed, then passed on a rerun; rerun* = every rerun
# failed too (a consistent failure, so not flaky).
_FLAKY_TAGS = {"flakyFailure", "flakyError"}
_RERUN_TAGS = {"rerunFailure", "rerunError"}


# --- Paths (same guards as Hermes' read_file) -------------------------------------
def _read_block_error(abs_path: str) -> Any:
    """Honour Hermes' read denylist when running inside the agent.

    The host refuses reads of its credential stores, internal skill-hub caches
    and project ``.env`` files; testsift must not become a side channel around
    that guard. It defers to the same check Core's ``read_file`` uses (which
    resolves symlinks itself). Outside a Hermes runtime the module is absent and
    there is no denylist to apply, so the check no-ops.
    """
    try:
        from agent.file_safety import get_read_block_error
    except Exception:
        return None
    try:
        return get_read_block_error(abs_path)
    except Exception:
        return None


# Like Hermes' read_file, never read device, fd or process files: they can block
# forever (/dev/zero, a FIFO) or leak process state (/proc/<pid>/environ).
_REFUSED_ROOTS = ("/proc", "/dev")


def _under_refused_root(path: str) -> bool:
    """True if the path, or the file a symlink resolves to, is under /proc or /dev."""
    candidates = [os.path.abspath(path)]
    try:
        candidates.append(os.path.realpath(path))
    except (OSError, ValueError):
        pass
    return any(
        candidate == root or candidate.startswith(root + "/")
        for candidate in candidates
        for root in _REFUSED_ROOTS
    )


def _check(path: str) -> int:
    """Apply the read guards to one path and return its st_mode.

    Runs before anything is opened; ``stat`` does not open the file.
    """
    # The raw string is checked too, as Core does: abspath() turns an NT namespace
    # path such as \??\UNC\host\share into C:\??\..., which the check no longer
    # recognises, while stat() on the raw path would still reach the network.
    blocked = _read_block_error(path) or _read_block_error(os.path.abspath(path))
    if blocked:
        raise ValueError(blocked)
    if _under_refused_root(path):
        raise ValueError(f"Refusing to read {path}: device and /proc paths are not test reports")
    try:
        return os.stat(path).st_mode
    except FileNotFoundError:
        raise ValueError(f"Path not found: {path}") from None
    except OSError as exc:
        raise ValueError(f"Cannot access {path}: {exc.strerror or exc}") from None


def _report_files(path: Any) -> tuple[str, list[str], list[dict], bool]:
    """Resolve the caller's path to the report files to read.

    A file is read as one report. A directory is walked (symlinked directories
    are not followed) for ``*.xml`` files in sorted order; every file found goes
    through the same guards as a directly named one.
    Returns (resolved path, files, skipped entries, files_capped).
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("No report path provided")
    path = os.path.expanduser(path)
    mode = _check(path)
    if stat.S_ISREG(mode):
        return path, [path], [], False
    if not stat.S_ISDIR(mode):
        raise ValueError(f"Refusing to read {path}: not a regular file or directory")

    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            if name.lower().endswith(".xml"):
                found.append(os.path.join(dirpath, name))
    capped = len(found) > MAX_FILES
    files: list[str] = []
    skipped: list[dict] = []
    for candidate in found[:MAX_FILES]:
        try:
            if stat.S_ISREG(_check(candidate)):
                files.append(candidate)
            else:
                skipped.append({"file": candidate, "reason": "not a regular file"})
        except ValueError as exc:
            skipped.append({"file": candidate, "reason": str(exc)})
    if not files and not skipped:
        raise ValueError(f"No .xml report files under {path}")
    return path, files, skipped, capped


# --- Bounded text capture ---------------------------------------------------------
class _Clip:
    """Keep the head and tail of a text stream, never more than ``limit`` chars."""

    __slots__ = ("limit", "head", "tail", "total")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.head = ""
        self.tail = ""
        self.total = 0

    def add(self, chunk: str) -> None:
        self.total += len(chunk)
        half = self.limit // 2
        if len(self.head) < half:
            room = half - len(self.head)
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail = (self.tail + chunk)[-(self.limit - half):]

    def text(self) -> str:
        omitted = self.total - len(self.head) - len(self.tail)
        if omitted > 0:
            return f"{self.head}\n... [{omitted} characters omitted] ...\n{self.tail}"
        return self.head + self.tail

    def tail_text(self, limit: int) -> str:
        joined = self.head + self.tail if self.total <= self.limit else self.tail
        return joined[-limit:]


# --- Streaming JUnit parser -------------------------------------------------------
class _Refused(Exception):
    pass


class _NotJUnit(Exception):
    pass


def _parse_time(raw: Optional[str]) -> Optional[float]:
    """Parse a ``time`` attribute in seconds; None when absent or not a number.

    Some emitters write thousands separators ("1,234.5"); those are accepted only
    when a decimal point is also present, since a lone comma is ambiguous.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if "," in raw:
        if "." not in raw:
            return None
        raw = raw.replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _parse_file(path: str, on_case: Callable[[dict], bool]) -> dict:
    """Stream one report, calling ``on_case`` per test case.

    ``on_case`` returns False to stop early (case cap). Returns file stats.
    Raises _NotJUnit for well-formed XML that is not a test report, _Refused
    for DOCTYPE/entity declarations, and expat.ExpatError for malformed XML
    (cases read before the error have already been delivered).
    """
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def refuse_doctype(*_args: Any) -> None:
        raise _Refused("DOCTYPE and entity declarations are not allowed in a test report")

    parser.StartDoctypeDeclHandler = refuse_doctype
    parser.EntityDeclHandler = refuse_doctype
    parser.ExternalEntityRefHandler = refuse_doctype

    state: dict[str, Any] = {"root": None, "cases": 0, "stopped": False}
    suites: list[str] = []
    case: Optional[dict] = None
    capture: Optional[_Clip] = None
    depth_in_case = 0

    def start(name: str, attrs: dict) -> None:
        nonlocal case, capture, depth_in_case
        if state["root"] is None:
            state["root"] = name
            if name not in ("testsuites", "testsuite"):
                raise _NotJUnit(f"not a JUnit report (root element <{name}>)")
        if state["stopped"]:
            return
        if case is not None:
            depth_in_case += 1
            if depth_in_case != 1:
                return
            if name in ("failure", "error"):
                status = "failed" if name == "failure" else "error"
                if _RANK[status] > _RANK[case["status"]]:
                    case["status"] = status
                if case["detail"] is None:
                    case["type"] = attrs.get("type") or None
                    case["message"] = (attrs.get("message") or "")[:MESSAGE_CHARS]
                    case["detail"] = _Clip(DETAIL_CHARS)
                    capture = case["detail"]
                else:
                    case["extra_results"] += 1
            elif name == "skipped":
                if case["status"] == "passed":
                    case["status"] = "skipped"
                    case["message"] = (attrs.get("message") or "")[:MESSAGE_CHARS]
            elif name in _FLAKY_TAGS:
                case["flaky_reruns"] += 1
            elif name in _RERUN_TAGS:
                case["failed_reruns"] += 1
            elif name == "system-out":
                capture = case["out"]
            elif name == "system-err":
                capture = case["err"]
            return
        if name == "testsuite":
            suites.append(attrs.get("name") or "")
        elif name == "testcase":
            case = {
                "classname": attrs.get("classname") or "",
                "name": attrs.get("name") or "",
                "file": attrs.get("file") or None,
                "line": attrs.get("line") or None,
                "time": _parse_time(attrs.get("time")),
                "suite": next((s for s in reversed(suites) if s), None),
                "status": "passed",
                "type": None,
                "message": "",
                "detail": None,
                "extra_results": 0,
                "flaky_reruns": 0,
                "failed_reruns": 0,
                "out": _Clip(OUTPUT_CHARS * 2),
                "err": _Clip(OUTPUT_CHARS * 2),
            }
            depth_in_case = 0

    def end(name: str) -> None:
        nonlocal case, capture, depth_in_case
        if state["stopped"]:
            return
        if case is not None:
            if depth_in_case > 0:
                if depth_in_case == 1:
                    capture = None
                depth_in_case -= 1
                return
            if name == "testcase":
                state["cases"] += 1
                if not on_case(case):
                    state["stopped"] = True
                case = None
                capture = None
            return
        if name == "testsuite" and suites:
            suites.pop()

    def chars(data: str) -> None:
        if capture is not None:
            capture.add(data)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.buffer_text = True

    with open(path, "rb") as handle:
        while not state["stopped"]:
            chunk = handle.read(READ_CHUNK)
            if not chunk:
                parser.Parse(b"", True)
                break
            parser.Parse(chunk, False)
    if state["root"] is None:
        raise _NotJUnit("empty report")
    return {"cases": state["cases"], "stopped": state["stopped"]}


def _test_id(case: dict) -> str:
    if case["classname"] and case["name"]:
        return f"{case['classname']}::{case['name']}"
    return case["name"] or case["classname"] or "<unnamed>"


class _Run:
    """All test cases read from one report path, keyed by test id."""

    def __init__(self, raw_path: Any) -> None:
        self.path, files, self.skipped_files, self.files_capped = _report_files(raw_path)
        self.files_read: list[dict] = []
        self.cases: list[dict] = []
        self.cases_capped = False
        for file in files:
            if self.cases_capped:
                self.skipped_files.append({"file": file, "reason": f"case cap ({MAX_CASES}) reached"})
                continue
            self._read(file)

    def _read(self, file: str) -> None:
        before = len(self.cases)

        def on_case(case: dict) -> bool:
            if len(self.cases) >= MAX_CASES:
                self.cases_capped = True
                return False
            case["report"] = file
            self.cases.append(case)
            return True

        entry: dict[str, Any] = {"file": file}
        try:
            _parse_file(file, on_case)
        except _NotJUnit as exc:
            self.skipped_files.append({"file": file, "reason": str(exc)})
            return
        except _Refused as exc:
            del self.cases[before:]
            self.skipped_files.append({"file": file, "reason": str(exc)})
            return
        except expat.ExpatError as exc:
            problem = f"{expat.ErrorString(exc.code)} at line {exc.lineno}"
            if len(self.cases) == before:
                self.skipped_files.append({"file": file, "reason": f"malformed XML: {problem}"})
                return
            # A report cut off mid-write (killed CI job) keeps the cases it has.
            entry["parse_error"] = problem
            entry["note"] = "cases before the error are included"
        except OSError as exc:
            self.skipped_files.append({"file": file, "reason": f"could not read: {exc.strerror or exc}"})
            return
        entry["cases"] = len(self.cases) - before
        self.files_read.append(entry)

    def by_id(self) -> dict[str, dict]:
        """Collapse cases to one entry per test id (worst status wins)."""
        merged: dict[str, dict] = {}
        for case in self.cases:
            key = _test_id(case)
            seen = merged.get(key)
            if seen is None:
                merged[key] = {
                    "status": case["status"],
                    "statuses": {case["status"]},
                    "time": case["time"],
                    "count": 1,
                    "case": case,
                }
                continue
            seen["count"] += 1
            seen["statuses"].add(case["status"])
            if case["time"] is not None:
                seen["time"] = (seen["time"] or 0.0) + case["time"]
            if _RANK[case["status"]] > _RANK[seen["status"]]:
                seen["status"] = case["status"]
                seen["case"] = case
        return merged

    def header(self) -> dict:
        out: dict[str, Any] = {
            "path": self.path,
            "files_read": len(self.files_read),
            "files_skipped": self.skipped_files[:50],
        }
        errors = [f for f in self.files_read if "parse_error" in f]
        if errors:
            out["files_with_parse_errors"] = errors[:50]
        if len(self.skipped_files) > 50:
            out["files_skipped_total"] = len(self.skipped_files)
        if self.files_capped:
            out["files_capped"] = MAX_FILES
        if self.cases_capped:
            out["cases_capped"] = MAX_CASES
        return out


# --- Failure signatures -----------------------------------------------------------
_MASKS = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b"), "<IP>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"\b[0-9a-fA-F]{32,64}\b"), "<HEX>"),
    (re.compile(r"\"[^\"]*\"|'[^']*'"), "<STR>"),
    (re.compile(r"-?\b\d[\d.,]*\b"), "<NUM>"),
]
_WS_RX = re.compile(r"\s+")
# Messages some emitters write for every failure (go-junit-report and gotestsum
# use "Failed"); they say nothing about the cause, so the trace is used instead.
_GENERIC_MESSAGES = {"failed", "failure", "fail", "error", "test failed"}
# Runner chatter in Go test output that precedes the actual failure line.
_GO_NOISE_RX = re.compile(r"^(?:=== (?:RUN|PAUSE|CONT|NAME)\b|--- (?:FAIL|PASS|SKIP):|FAIL\b|PASS\b|ok\b)")
# pytest wraps fixture errors: failed on setup with "RuntimeError: ...".
_PYTEST_PHASE_RX = re.compile(r'^failed on (setup|teardown) with "(.*)"$', re.S)


def _informative_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not _GO_NOISE_RX.match(line):
            return line
    return ""


def _cause_text(case: dict) -> str:
    """The line that best states why a test failed, before masking."""
    text = _first_line(case["message"])
    if text.lower().rstrip(".") in _GENERIC_MESSAGES:
        text = ""
    if not text and case["detail"] is not None:
        text = _informative_line(case["detail"].head)
    phase = _PYTEST_PHASE_RX.match(text)
    if phase:
        text = f"{phase.group(1)}: {phase.group(2)}"
    return text


def _signature(case: dict) -> str:
    """A cause key shared by failures that differ only in variable values."""
    text = _cause_text(case)
    for rx, repl in _MASKS:
        text = rx.sub(repl, text)
    text = _WS_RX.sub(" ", text).strip()[:300]
    kind = case["type"] or case["status"]
    return f"{kind}: {text}" if text else kind


def _location(case: dict) -> dict:
    out: dict[str, Any] = {}
    if case["file"]:
        out["file"] = case["file"]
    if case["line"]:
        out["line"] = case["line"]
    return out


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": message, **extra})


def _int_arg(args: dict, key: str, default: int, cap: int, minimum: int = 1) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        value = default
    return min(value, cap)


# --- Schemas ----------------------------------------------------------------------
_PATH_DESC = (
    "A JUnit XML report file, or a directory searched recursively for *.xml reports "
    "(non-report XML such as pom.xml or coverage.xml is skipped and listed)."
)

TEST_REPORT_SUMMARY = {
    "name": "test_report_summary",
    "description": (
        "Summarise local JUnit XML test results (pytest --junitxml, Maven Surefire, "
        "Gradle, Jest/Vitest junit reporters, go-junit-report, PHPUnit, .NET, CI "
        "artifacts): exact passed/failed/error/skipped counts, failures grouped by "
        "cause (messages that differ only in numbers, ids or quoted values share one "
        "group, with a count and example tests), the slowest tests and the suites with "
        "the most failures. Use it first on a test run too large to read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": _PATH_DESC},
            "max_groups": {
                "type": "integer",
                "description": f"Failure groups to return. Default {DEFAULT_GROUPS}, capped at {HARD_GROUPS}.",
            },
            "slowest": {
                "type": "integer",
                "description": f"Slowest tests to return. Default {DEFAULT_SLOWEST}, capped at {HARD_SLOWEST}.",
            },
        },
        "required": ["path"],
    },
}

TEST_REPORT_FAILURES = {
    "name": "test_report_failures",
    "description": (
        "List the failing and erroring tests in local JUnit XML results, in report "
        "order: test id, suite, file/line when the report has them, failure type, "
        "message and the start and end of the trace. Filter with 'contains' (matched "
        "against id, type and message) and page with offset/limit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": _PATH_DESC},
            "contains": {
                "type": "string",
                "description": "Only failures whose id, type or message contains this text (case-insensitive).",
            },
            "include_output": {
                "type": "boolean",
                "description": "Also return the tail of each failing test's system-out/system-err. Default false.",
            },
            "offset": {"type": "integer", "description": "Failures to skip. Default 0."},
            "limit": {
                "type": "integer",
                "description": f"Failures to return. Default {DEFAULT_LIMIT}, capped at {HARD_LIMIT}.",
            },
        },
        "required": ["path"],
    },
}

TEST_REPORT_COMPARE = {
    "name": "test_report_compare",
    "description": (
        "Compare two local JUnit XML test runs (e.g. main vs a branch, or before vs "
        "after a change): tests that newly fail, tests that were fixed, tests that "
        "passed before and are skipped now, tests still failing, tests added or "
        "removed, and tests that became much slower (at least "
        "twice as slow and one second slower). Tests are matched by classname and name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "base": {"type": "string", "description": "Baseline report file or directory. " + _PATH_DESC},
            "head": {"type": "string", "description": "New report file or directory to compare against the baseline."},
            "limit": {
                "type": "integer",
                "description": f"Entries per list. Default {DEFAULT_COMPARE_LIMIT}, capped at {HARD_COMPARE_LIMIT}.",
            },
        },
        "required": ["base", "head"],
    },
}


# --- Handlers (always return a JSON string) ---------------------------------------
def _load(raw: Any) -> _Run:
    run = _Run(raw)
    if not run.files_read:
        reasons = "; ".join(f"{s['file']}: {s['reason']}" for s in run.skipped_files[:5])
        raise ValueError(f"No JUnit test report could be read from {run.path}" + (f" ({reasons})" if reasons else ""))
    return run


def test_report_summary(args: dict, **kwargs) -> str:
    try:
        run = _load(args.get("path"))
    except ValueError as exc:
        return _err(str(exc))
    max_groups = _int_arg(args, "max_groups", DEFAULT_GROUPS, HARD_GROUPS)
    slowest_n = _int_arg(args, "slowest", DEFAULT_SLOWEST, HARD_SLOWEST)

    tests = run.by_id()
    totals: Counter = Counter(entry["status"] for entry in tests.values())
    duration = sum(case["time"] for case in run.cases if case["time"] is not None)

    groups: dict[str, dict] = {}
    suite_failures: Counter = Counter()
    for test_id, entry in tests.items():
        if entry["status"] not in ("failed", "error"):
            continue
        case = entry["case"]
        sig = _signature(case)
        group = groups.setdefault(sig, {"signature": sig, "count": 0, "example_message": "", "tests": []})
        group["count"] += 1
        if not group["example_message"]:
            group["example_message"] = _cause_text(case)[:300]
        if len(group["tests"]) < EXAMPLES_PER_GROUP:
            group["tests"].append(test_id)
        suite_failures[case["suite"] or case["classname"] or "<no suite>"] += 1
    ranked = sorted(groups.values(), key=lambda g: (-g["count"], g["signature"]))

    timed = [(entry["time"], test_id) for test_id, entry in tests.items() if entry["time"] is not None]
    timed.sort(key=lambda t: (-t[0], t[1]))

    flaky = sorted(
        test_id for test_id, entry in tests.items()
        if entry["case"]["flaky_reruns"] or ("passed" in entry["statuses"] and entry["statuses"] & {"failed", "error"})
    )
    return json.dumps({
        "ok": True,
        **run.header(),
        "tests": len(tests),
        "test_cases_read": len(run.cases),
        "passed": totals["passed"],
        "failed": totals["failed"],
        "errors": totals["error"],
        "skipped": totals["skipped"],
        "duration_seconds": round(duration, 3),
        "failure_groups_total": len(ranked),
        "failure_groups": ranked[:max_groups],
        "suites_with_most_failures": [
            {"suite": s, "failures": n} for s, n in sorted(suite_failures.items(), key=lambda x: (-x[1], x[0]))[:10]
        ],
        "slowest": [{"test": t, "seconds": round(s, 3)} for s, t in timed[:slowest_n]],
        "flaky_suspects": flaky[:50],
        "flaky_suspects_total": len(flaky),
    })


def test_report_failures(args: dict, **kwargs) -> str:
    try:
        run = _load(args.get("path"))
    except ValueError as exc:
        return _err(str(exc))
    contains = args.get("contains")
    needle = contains.lower() if isinstance(contains, str) and contains.strip() else None
    include_output = args.get("include_output") is True
    offset = _int_arg(args, "offset", 0, 10**9, minimum=0)
    limit = _int_arg(args, "limit", DEFAULT_LIMIT, HARD_LIMIT)

    matched: list[dict] = []
    total = 0
    for case in run.cases:
        if case["status"] not in ("failed", "error"):
            continue
        test_id = _test_id(case)
        if needle and needle not in f"{test_id}\n{case['type'] or ''}\n{case['message']}".lower():
            continue
        total += 1
        if total <= offset or len(matched) >= limit:
            continue
        item: dict[str, Any] = {
            "test": test_id,
            "status": case["status"],
            "suite": case["suite"],
            **_location(case),
            "type": case["type"],
            "message": case["message"],
            "detail": case["detail"].text() if case["detail"] else "",
            "report": case["report"],
        }
        if case["time"] is not None:
            item["seconds"] = case["time"]
        if case["extra_results"]:
            item["more_results_in_case"] = case["extra_results"]
        if case["failed_reruns"]:
            item["failed_reruns"] = case["failed_reruns"]
        if include_output:
            item["system_out_tail"] = case["out"].tail_text(OUTPUT_CHARS)
            item["system_err_tail"] = case["err"].tail_text(OUTPUT_CHARS)
        matched.append(item)
    return json.dumps({
        "ok": True,
        **run.header(),
        "filter": contains if needle else None,
        "total_matching": total,
        "offset": offset,
        "returned": len(matched),
        "truncated": offset + len(matched) < total,
        "failures": matched,
    })


def test_report_compare(args: dict, **kwargs) -> str:
    try:
        base = _load(args.get("base"))
        head = _load(args.get("head"))
    except ValueError as exc:
        return _err(str(exc))
    limit = _int_arg(args, "limit", DEFAULT_COMPARE_LIMIT, HARD_COMPARE_LIMIT)
    old, new = base.by_id(), head.by_id()
    bad = ("failed", "error")

    def brief(test_id: str, entry: dict) -> dict:
        case = entry["case"]
        out: dict[str, Any] = {"test": test_id, "status": entry["status"]}
        if entry["status"] in bad:
            out["message"] = _cause_text(case)[:300]
        return out

    newly_failing, fixed, still_failing, added, removed, slower = [], [], [], [], [], []
    newly_skipped = []
    for test_id in sorted(new):
        now = new[test_id]
        was = old.get(test_id)
        if was is None:
            added.append(brief(test_id, now))
            continue
        if now["status"] in bad and was["status"] not in bad:
            newly_failing.append({**brief(test_id, now), "was": was["status"]})
        elif now["status"] in bad:
            still_failing.append(brief(test_id, now))
        elif was["status"] in bad:
            fixed.append({"test": test_id, "status": now["status"], "was": was["status"]})
        elif now["status"] == "skipped" and was["status"] == "passed":
            newly_skipped.append({"test": test_id, "was": "passed"})
        if was["time"] is not None and now["time"] is not None:
            delta = now["time"] - was["time"]
            if delta >= 1.0 and now["time"] >= 2 * was["time"]:
                slower.append({"test": test_id, "base_seconds": round(was["time"], 3),
                               "head_seconds": round(now["time"], 3), "delta_seconds": round(delta, 3)})
    for test_id in sorted(old):
        if test_id not in new:
            removed.append(brief(test_id, old[test_id]))
    slower.sort(key=lambda s: (-s["delta_seconds"], s["test"]))
    added_failing = [a for a in added if a["status"] in bad]

    def counts(tests: dict) -> dict:
        c = Counter(e["status"] for e in tests.values())
        return {"tests": len(tests), "passed": c["passed"], "failed": c["failed"],
                "errors": c["error"], "skipped": c["skipped"]}

    return json.dumps({
        "ok": True,
        "base": {**base.header(), **counts(old)},
        "head": {**head.header(), **counts(new)},
        "newly_failing_total": len(newly_failing),
        "newly_failing": newly_failing[:limit],
        "added_failing_total": len(added_failing),
        "added_failing": added_failing[:limit],
        "fixed_total": len(fixed),
        "fixed": fixed[:limit],
        "newly_skipped_total": len(newly_skipped),
        "newly_skipped": newly_skipped[:limit],
        "still_failing_total": len(still_failing),
        "still_failing": still_failing[:limit],
        "added_total": len(added),
        "added": added[:limit],
        "removed_total": len(removed),
        "removed": removed[:limit],
        "slower_total": len(slower),
        "slower": slower[:limit],
    })
