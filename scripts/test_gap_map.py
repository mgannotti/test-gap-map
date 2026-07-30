#!/usr/bin/env python3
"""test-gap-map — the uncovered lines that will actually cost you.

Coverage percentage is a vanity metric. A file that is 40% covered and has not
changed in a year is fine; a file that is 78% covered and changed in thirty
commits is where the next incident is already forming. The number on the
dashboard cannot tell those two apart, so teams write tests for whatever is
lowest, which is rarely whatever is riskiest.

This reads a coverage report — Cobertura XML, LCOV, or coverage.py JSON — and,
when you hand it an exported history, ranks the uncovered code by *risk*:
uncovered lines multiplied by how often the file changes. It writes the ranking.
It does not write tests.

Offline. Read-only. The coverage report is evidence, not a target.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from scoutkit import (  # noqa: E402
    Commit,
    Finding,
    Report,
    Severity,
    classify_path,
    is_source,
    parse_git_log,
    parse_iso_date,
)
from scoutkit.cli import run  # noqa: E402
from scoutkit.io import EvidenceError, read_text  # noqa: E402

SKILL = "test-gap-map"
TITLE = "Test Gap Map — the uncovered lines that will actually cost you"

DEFAULT_TOP = 15

# A file touched in at least this many commits is changing often enough that an
# uncovered line in it is a live liability, not a dormant one.
HIGH_CHURN_COMMITS = 5
# Below this many uncovered lines, a gap is not worth a high-severity flag on its
# own however busy the file is.
SUBSTANTIAL_UNCOVERED = 10
# Commits that make a zero-coverage file actively dangerous.
ZERO_COVERAGE_CHURN = 3
# "High overall" for the vanity-metric finding.
HIGH_OVERALL = 0.80
# A file whose uncovered lines are mostly error handling is worse than the count
# suggests: the untested paths are the ones that only run when something breaks.
ERROR_PATH_MIN = 3
ERROR_PATH_SHARE = 0.4

_ERROR_PATH = re.compile(
    r"\b(?:except|catch|rescue|raise|throw|panic|abort|reject|"
    r"errors\.(?:Is|As|New)|log\.(?:error|fatal|panic))\b"
    r"|if\s+err\s*!=\s*nil",
    re.I,
)


@dataclass
class CoverageFile:
    path: str
    covered: int = 0
    uncovered: int = 0
    missing_lines: list[int] | None = None

    @property
    def total(self) -> int:
        return self.covered + self.uncovered

    @property
    def rate(self) -> float | None:
        return (self.covered / self.total) if self.total else None


# --------------------------------------------------------------------------- #
# Format detection and parsing
# --------------------------------------------------------------------------- #
def parse_coverage(text: str) -> tuple[str, list[CoverageFile]]:
    """Auto-detect the coverage format by content and parse it.

    Raises :class:`EvidenceError` when the input is not one of the three
    supported formats — never a silent empty success.
    """
    if not text or not text.strip():
        raise EvidenceError("the coverage report is empty")

    head = text.lstrip()[:1]
    if head == "{":
        return "coverage.py", _parse_coveragepy(text)
    if head == "<":
        return "cobertura", _parse_cobertura(text)
    if "SF:" in text:
        return "lcov", _parse_lcov(text)
    raise EvidenceError(
        "unrecognized coverage format. Supported: Cobertura XML (<coverage>), "
        "LCOV (SF:/DA:), and coverage.py JSON ({\"files\": ...})"
    )


def _parse_cobertura(text: str) -> list[CoverageFile]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise EvidenceError(f"invalid Cobertura XML: {exc}") from exc
    if root.tag != "coverage" and root.find(".//coverage") is None and root.find(".//class") is None:
        raise EvidenceError("XML is not a Cobertura coverage report (no <coverage>/<class>)")

    covered: dict[str, set] = defaultdict(set)
    missing: dict[str, set] = defaultdict(set)
    seen: set[str] = set()
    for cls in root.iter("class"):
        filename = cls.get("filename")
        if not filename:
            continue
        seen.add(filename)
        lines_el = cls.find("lines")
        if lines_el is None:
            continue
        for line in lines_el.findall("line"):
            try:
                number = int(line.get("number", "0"))
                hits = int(line.get("hits", "0"))
            except (TypeError, ValueError):
                continue
            (covered if hits > 0 else missing)[filename].add(number)

    files = []
    for filename in sorted(seen):
        cov = covered[filename]
        miss = missing[filename] - cov
        files.append(CoverageFile(
            path=filename, covered=len(cov), uncovered=len(miss),
            missing_lines=sorted(miss),
        ))
    return files


def _parse_lcov(text: str) -> list[CoverageFile]:
    covered: dict[str, set] = defaultdict(set)
    missing: dict[str, set] = defaultdict(set)
    order: list[str] = []
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            current = line[3:].strip()
            if current not in covered and current not in missing:
                order.append(current)
        elif line.startswith("DA:") and current is not None:
            parts = line[3:].split(",")
            if len(parts) < 2:
                continue
            try:
                number, hits = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            (covered if hits > 0 else missing)[current].add(number)
        elif line.startswith("end_of_record"):
            current = None

    files = []
    for path in dict.fromkeys(order):
        cov = covered[path]
        miss = missing[path] - cov
        files.append(CoverageFile(
            path=path, covered=len(cov), uncovered=len(miss), missing_lines=sorted(miss),
        ))
    return files


def _parse_coveragepy(text: str) -> list[CoverageFile]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"invalid coverage.py JSON: {exc}") from exc
    if not isinstance(data, dict) or "files" not in data or not isinstance(data["files"], dict):
        raise EvidenceError("JSON is not a coverage.py report (missing top-level \"files\")")

    files = []
    for path in sorted(data["files"]):
        info = data["files"][path]
        if not isinstance(info, dict):
            continue
        summary = info.get("summary", {}) if isinstance(info.get("summary"), dict) else {}

        def count(key: str) -> int:
            """A summary value that is not a number is a broken report, not a zero."""
            raw = summary.get(key, 0) or 0
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                raise EvidenceError(
                    f"coverage.py report: {path} has a non-numeric \"{key}\" ({raw!r})"
                )
            try:
                return int(raw)
            except (TypeError, ValueError) as exc:
                raise EvidenceError(
                    f"coverage.py report: {path} has a non-numeric \"{key}\" ({raw!r})"
                ) from exc

        missing_lines = info.get("missing_lines")
        executed = info.get("executed_lines")
        if isinstance(missing_lines, list):
            uncovered = len(missing_lines)
            missing_numbers = sorted(int(n) for n in missing_lines if isinstance(n, int))
        else:
            uncovered = count("missing_lines")
            missing_numbers = None
        if isinstance(executed, list):
            covered = len(executed)
        else:
            covered = count("covered_lines")
        num_statements = summary.get("num_statements")
        if isinstance(num_statements, int) and num_statements >= covered + uncovered:
            # trust the reported statement total when it is consistent
            uncovered = max(uncovered, num_statements - covered)
        files.append(CoverageFile(
            path=path, covered=covered, uncovered=uncovered, missing_lines=missing_numbers,
        ))
    return files


# --------------------------------------------------------------------------- #
# History matching
# --------------------------------------------------------------------------- #
def _norm(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _history_index(commits: list[Commit]) -> dict:
    file_commits: Counter = Counter()
    file_churn: Counter = Counter()
    for commit in commits:
        seen: set[str] = set()
        for change in commit.files:
            key = _norm(change.path)
            file_churn[key] += change.churn
            if key not in seen:
                file_commits[key] += 1
                seen.add(key)
    by_basename: dict[str, list[str]] = defaultdict(list)
    for key in file_commits:
        by_basename[key.rsplit("/", 1)[-1]].append(key)
    return {"commits": file_commits, "churn": file_churn, "by_basename": by_basename}


def _lookup(index: dict, cov_path: str) -> tuple[int, int]:
    key = _norm(cov_path)
    if key in index["commits"]:
        return index["commits"][key], index["churn"][key]
    matches = index["by_basename"].get(key.rsplit("/", 1)[-1], [])
    if len(matches) == 1:
        return index["commits"][matches[0]], index["churn"][matches[0]]
    return 0, 0


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def analyze(args: argparse.Namespace) -> Report:
    fmt, files = parse_coverage(read_text(Path(args.input)))

    as_of = parse_iso_date(getattr(args, "as_of", None)) if getattr(args, "as_of", None) else None
    commits: list[Commit] = []
    history_path = getattr(args, "history", None)
    if history_path:
        commits = parse_git_log(read_text(Path(history_path)))
        if as_of is not None:
            commits = [c for c in commits if c.when is None or c.when <= as_of]
    index = _history_index(commits) if commits else None

    source_root = getattr(args, "source_root", None)
    top = max(1, int(getattr(args, "top", DEFAULT_TOP) or DEFAULT_TOP))

    report = Report(skill=SKILL, subject=Path(args.input).name)

    def add(code: str, severity: str, title: str, detail: str, locator: str,
            fix: str, evidence: str = "") -> None:
        report.add(Finding(code=code, severity=severity, title=title, detail=detail,
                           locator=locator, evidence=evidence, recommendation=fix))

    # attach churn to each coverage file
    rows: list[dict] = []
    for cf in files:
        commits_n, churn = _lookup(index, cf.path) if index is not None else (0, 0)
        score = cf.uncovered * commits_n if index is not None else cf.uncovered
        rows.append({
            "path": cf.path,
            "covered": cf.covered,
            "uncovered": cf.uncovered,
            "total": cf.total,
            "coverage": round(100 * cf.rate, 1) if cf.rate is not None else None,
            "commits": commits_n,
            "churn": churn,
            "score": score,
            "role": classify_path(cf.path),
            "_file": cf,
        })
    rows.sort(key=lambda r: (-r["score"], -r["uncovered"], r["path"]))

    covered_total = sum(r["covered"] for r in rows)
    line_total = sum(r["total"] for r in rows)
    overall = (covered_total / line_total) if line_total else None

    # --- TG005: a report that says success while measuring nothing -----------
    if not rows:
        add("TG005", Severity.HIGH, "Coverage reported over zero files",
            f"The {fmt} report parsed cleanly but contains no files. A run that "
            f"measures nothing and exits successfully reads as 100% on every "
            f"dashboard while testing nothing.",
            args.input,
            "Fix the coverage configuration so it actually instruments your source, "
            "then re-run. Treat an empty report as a failed run, not a passing one.")

    # --- TG001 / TG002 / TG007: churn-weighted findings (need history) -------
    if index is not None:
        emitted = 0
        for r in rows:
            if r["role"] == "test":
                continue
            if (r["commits"] >= HIGH_CHURN_COMMITS
                    and r["uncovered"] >= SUBSTANTIAL_UNCOVERED and r["covered"] > 0):
                if emitted < top:
                    add("TG001", Severity.HIGH, "High-churn code with a substantial coverage gap",
                        f"{r['path']} has {r['uncovered']} uncovered line(s) and changed in "
                        f"{r['commits']} commit(s) (churn {r['churn']}). Coverage is "
                        f"{r['coverage']}%. This is exactly where the next regression lands: "
                        f"code that moves constantly and is only partly tested.",
                        r["path"],
                        "Write tests here before the next change, starting with the "
                        "uncovered lines in the paths that change most.",
                        f"risk score {r['score']}")
                    emitted += 1
        if emitted >= top:
            report.note(f"More than {top} files met the high-churn coverage-gap bar; only the "
                        f"top {top} are listed as TG001 findings. See the ranking section for the rest.")

        for r in rows:
            if (r["role"] != "test" and r["covered"] == 0 and r["total"] > 0
                    and r["commits"] >= ZERO_COVERAGE_CHURN):
                add("TG002", Severity.HIGH, "A frequently changed file with zero coverage",
                    f"{r['path']} has no coverage at all and changed in {r['commits']} "
                    f"commit(s). Every change to it ships untested.",
                    r["path"],
                    "Add at least a smoke test that imports and exercises this file, then "
                    "build out from there.",
                    f"churn {r['churn']}")

        if overall is not None and overall >= HIGH_OVERALL:
            busy_below = [
                r for r in rows
                if r["commits"] >= HIGH_CHURN_COMMITS and r["coverage"] is not None
                and r["coverage"] < round(100 * overall, 1)
            ]
            if busy_below:
                names = ", ".join(r["path"] for r in busy_below[:4])
                add("TG007", Severity.LOW, "High overall coverage hiding busy, under-tested files",
                    f"Overall coverage is {round(100 * overall, 1)}%, but the busiest files "
                    f"sit below that average ({names}). The headline number is being carried "
                    f"by stable code while the code that changes is thinner.",
                    busy_below[0]["path"],
                    "Ignore the overall percentage. Bring the busiest files up to at least "
                    "the average before celebrating the headline.")
    else:
        # Without history the churn-weighted findings cannot run — but silence
        # here would read as a clean result, which is the failure mode this pack
        # exists to avoid. These two fire on the coverage report alone, at lower
        # severity because they cannot tell a busy file from a dormant one.
        emitted = 0
        for r in rows:
            if r["role"] == "test" or r["covered"] == 0 or r["total"] == 0:
                continue
            if r["uncovered"] < SUBSTANTIAL_UNCOVERED:
                continue
            if emitted >= top:
                break
            add("TG008", Severity.MEDIUM, "A substantial coverage gap, unweighted",
                f"{r['path']} has {r['uncovered']} uncovered line(s) at {r['coverage']}% "
                f"coverage. Without a history export there is no way to tell whether this "
                f"is code that changes weekly or code nobody has touched in a year, so it "
                f"is reported on size alone.",
                r["path"],
                "Pass --history to rank this against how often the file actually changes, "
                "then write tests from the top of that ranking.",
                f"{r['uncovered']} uncovered line(s)")
            emitted += 1

        for r in rows:
            if r["role"] != "test" and r["covered"] == 0 and r["total"] > 0:
                add("TG009", Severity.HIGH, "A file with no coverage at all",
                    f"{r['path']} has {r['total']} measurable line(s) and not one of them is "
                    f"executed by the suite. This needs no churn data to be worth acting on: "
                    f"every change to it ships untested.",
                    r["path"],
                    "Add a smoke test that imports and exercises this file, then build out "
                    "from there.",
                    f"{r['total']} unexecuted line(s)")

        report.note(
            "No history was supplied, so files are ranked by uncovered line count alone. "
            "That is a weaker ranking: it cannot tell a busy 78%-covered file from a "
            "dormant 40%-covered one, and TG001/TG002/TG007 could not run at all. "
            "Export a history and pass --history for a risk-weighted ranking: "
            "git log --numstat --date=short "
            '--pretty=format:"%H%x09%an%x09%ae%x09%ad%x09%s" > history.txt'
        )

    # --- TG006: files in history that the report never measured --------------
    if index is not None and rows:
        cov_norms = {_norm(r["path"]) for r in rows}
        cov_basenames = {n.rsplit("/", 1)[-1] for n in cov_norms}
        unmeasured = sorted({
            key for key in index["commits"]
            if is_source(key) and key not in cov_norms
            and key.rsplit("/", 1)[-1] not in cov_basenames
        })
        if unmeasured:
            shown = ", ".join(unmeasured[:5]) + ("…" if len(unmeasured) > 5 else "")
            add("TG006", Severity.MEDIUM, "Source in history that coverage never measured",
                f"{len(unmeasured)} source file(s) appear in the history but not in the "
                f"coverage report ({shown}). A file that is never measured reads as 100% "
                f"to every dashboard while being tested not at all.",
                unmeasured[0],
                "Widen the coverage configuration to include these files, or confirm they "
                "are genuinely out of scope.",
                f"{len(unmeasured)} unmeasured file(s)")
    else:
        unmeasured = []

    # --- TG004: a test file that is itself uncovered -------------------------
    dead_tests = sorted(r["path"] for r in rows
                        if r["role"] == "test" and r["covered"] == 0 and r["total"] > 0)
    if dead_tests:
        shown = ", ".join(dead_tests[:5]) + ("…" if len(dead_tests) > 5 else "")
        add("TG004", Severity.MEDIUM, "A test file the suite never ran",
            f"{len(dead_tests)} test file(s) show zero coverage of their own lines "
            f"({shown}). A test that never executes is worse than no test: it reports as "
            f"reassurance the suite is not providing.",
            dead_tests[0],
            "Find out why these are not collected — a naming mismatch, a skipped module, "
            "an import error — and make the runner pick them up.")

    # --- TG003: uncovered error-handling paths (needs source root) -----------
    if source_root:
        _error_path_finding(add, rows, Path(source_root), top, report)
    elif any(r["role"] != "test" and r["_file"].missing_lines is not None for r in rows):
        report.note(
            "TG003 (uncovered error-handling paths) was skipped: pass --source-root "
            "pointing at the repository so the uncovered lines can be read and classified."
        )

    # --- sections & summary --------------------------------------------------
    ranking = [{k: r[k] for k in
                ("path", "covered", "uncovered", "total", "coverage", "commits", "churn", "score")}
               for r in rows[:top]]
    report.sections = {
        "format": fmt,
        "ranking": ranking,
        "totals": {
            "files": len(rows),
            "covered_lines": covered_total,
            "uncovered_lines": sum(r["uncovered"] for r in rows),
            "line_rate": round(overall, 4) if overall is not None else None,
        },
        "unmeasured": unmeasured,
    }
    report.summary = {
        "format": fmt,
        "files": len(rows),
        "covered_lines": covered_total,
        "uncovered_lines": sum(r["uncovered"] for r in rows),
        "line_rate": round(100 * overall, 1) if overall is not None else None,
        "history": index is not None,
        "commits": len(commits),
        "as_of": as_of.isoformat() if as_of else None,
        "ranked_by": "risk (uncovered lines x commits)" if index is not None
        else "uncovered lines (no history)",
    }

    report.note(
        "Coverage percentage is a vanity metric. This ranks by uncovered lines "
        "weighted by change frequency, so the top of the list is where a test is "
        "worth the most — not merely where the percentage is lowest."
    )
    report.note(
        "This reports where tests are missing. It does not write them: use /tdd to "
        "actually write the tests once you know where they belong."
    )
    report.note(
        "File paths from the coverage report are matched to history by exact path or "
        "unique basename. A file that moved or is reported under an unusual path may not "
        "match, and would then rank as if it never changed."
    )
    report.decide_verdict()
    return report


def _error_path_finding(add, rows, source_root: Path, top: int, report: Report) -> None:
    offenders: list[tuple[str, int, int]] = []
    read_any = False
    for r in rows:
        cf: CoverageFile = r["_file"]
        if r["role"] == "test" or not cf.missing_lines:
            continue
        target = source_root / _norm(cf.path)
        if not target.is_file():
            continue
        read_any = True
        try:
            source_lines = read_text(target).splitlines()
        except (EvidenceError, OSError):
            continue
        error_hits = 0
        for number in cf.missing_lines:
            if 1 <= number <= len(source_lines) and _ERROR_PATH.search(source_lines[number - 1]):
                error_hits += 1
        if error_hits >= ERROR_PATH_MIN and error_hits >= ERROR_PATH_SHARE * len(cf.missing_lines):
            offenders.append((cf.path, error_hits, len(cf.missing_lines)))

    if offenders:
        offenders.sort(key=lambda t: (-t[1], t[0]))
        worst = offenders[0]
        names = ", ".join(f"{p} ({e}/{m})" for p, e, m in offenders[:4])
        add("TG003", Severity.MEDIUM, "Uncovered lines concentrated in error handling",
            f"In {len(offenders)} file(s), most uncovered lines are error-handling paths "
            f"({names}). The untested code is exactly what runs when something has already "
            f"gone wrong — the worst place for a surprise.",
            worst[0],
            "Add tests that force the failure: raise the exception, return the error, trip "
            "the guard. Those are the lines that matter when it counts.",
            f"{worst[1]} of {worst[2]} uncovered lines are error handling")
    elif not read_any:
        report.note(
            "TG003 could not read any source under --source-root: the coverage paths did "
            "not resolve there. Point --source-root at the directory the coverage paths "
            "are relative to."
        )


def _extend(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--history",
                        help="an exported git log --numstat history, for churn weighting")
    parser.add_argument("--as-of", dest="as_of",
                        help="ignore commits after this date (YYYY-MM-DD) for reproducible runs")
    parser.add_argument("--source-root",
                        help="repository root, so uncovered error-handling paths can be read (TG003)")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP,
                        help=f"rows to show in the ranking (default: {DEFAULT_TOP})")


def main(argv: list[str] | None = None) -> int:
    return run(
        argv, skill=SKILL, title=TITLE,
        description="Rank uncovered code by churn so tests get written where they matter.",
        analyze=analyze, extend=_extend,
    )


if __name__ == "__main__":
    raise SystemExit(main())
