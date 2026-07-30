"""Tests for test-gap-map.

The point of the skill is that coverage percentage lies, so the tests lean on
the cases that expose the lie: the same coverage gap ranked differently once
churn is known, a file that is never measured and therefore reads as perfect, a
test that never ran, and a report that measures nothing yet exits a success. All
three input formats are exercised, along with the malformed versions of each.
"""

from __future__ import annotations

import json

import pytest

from scoutkit.io import EvidenceError
from test_gap_map import analyze, parse_coverage


class Args:
    def __init__(self, **kw):
        self.input = kw.pop("input")
        self.history = kw.pop("history", None)
        self.as_of = kw.pop("as_of", None)
        self.source_root = kw.pop("source_root", None)
        self.top = kw.pop("top", 15)
        for k, v in kw.items():
            setattr(self, k, v)


def codes(report):
    return {f.code for f in report.findings}


# --------------------------------------------------------------------------- #
# builders for each coverage format
# --------------------------------------------------------------------------- #
def cobertura(files: dict[str, list[int]]) -> str:
    classes = ""
    for filename, hits in files.items():
        lines = "".join(f'<line number="{i + 1}" hits="{h}"/>' for i, h in enumerate(hits))
        classes += f'<class name="c" filename="{filename}"><lines>{lines}</lines></class>'
    return (
        '<?xml version="1.0" ?><coverage line-rate="0"><packages><package>'
        f"<classes>{classes}</classes></package></packages></coverage>"
    )


def lcov(files: dict[str, list[int]], *, checksums: bool = False) -> str:
    out = []
    for filename, hits in files.items():
        out.append(f"SF:{filename}")
        for i, h in enumerate(hits):
            out.append(f"DA:{i + 1},{h}" + (",abc123" if checksums else ""))
        out.append("end_of_record")
    return "\n".join(out) + "\n"


def covpy(files: dict[str, tuple[list[int], list[int]]]) -> str:
    payload: dict = {"files": {}}
    for filename, (executed, missing) in files.items():
        payload["files"][filename] = {
            "executed_lines": executed,
            "missing_lines": missing,
            "summary": {
                "num_statements": len(executed) + len(missing),
                "covered_lines": len(executed),
                "missing_lines": len(missing),
            },
        }
    return json.dumps(payload)


def make_history(counts: dict[str, int], *, date: str = "2026-05-01") -> str:
    lines = []
    n = 0
    for path, k in counts.items():
        for _ in range(k):
            n += 1
            sha = f"{n:040d}"
            lines.append("\t".join([sha, "Dev", "dev@x.test", date, f"c{n}"]))
            lines.append(f"20\t5\t{path}")
    return "\n".join(lines) + "\n"


COVERED = [1] * 15
UNCOVERED = [0] * 15


# --------------------------------------------------------------------------- #
# format detection + parsing
# --------------------------------------------------------------------------- #
def test_cobertura_is_detected_and_parsed():
    fmt, files = parse_coverage(cobertura({"app/a.py": [1, 1, 0, 0]}))
    assert fmt == "cobertura"
    assert files[0].covered == 2 and files[0].uncovered == 2
    assert files[0].missing_lines == [3, 4]


def test_lcov_is_detected_and_parsed():
    fmt, files = parse_coverage(lcov({"app/a.py": [1, 0, 0]}))
    assert fmt == "lcov"
    assert files[0].covered == 1 and files[0].uncovered == 2


def test_lcov_da_with_checksum_is_parsed():
    fmt, files = parse_coverage(lcov({"app/a.py": [1, 1, 0]}, checksums=True))
    assert fmt == "lcov"
    assert files[0].uncovered == 1


def test_coveragepy_json_is_detected_and_parsed():
    fmt, files = parse_coverage(covpy({"app/a.py": ([1, 2, 3], [4, 5])}))
    assert fmt == "coverage.py"
    assert files[0].covered == 3 and files[0].uncovered == 2
    assert files[0].missing_lines == [4, 5]


def test_an_empty_report_is_an_evidence_error(write):
    with pytest.raises(EvidenceError):
        analyze(Args(input=str(write("cov.xml", "   \n"))))


def test_an_unrecognized_format_is_an_evidence_error(write):
    with pytest.raises(EvidenceError):
        analyze(Args(input=str(write("cov.txt", "just some words, not coverage"))))


def test_malformed_cobertura_is_an_evidence_error(write):
    with pytest.raises(EvidenceError):
        analyze(Args(input=str(write("cov.xml", "<coverage><packages><cls"))))


def test_malformed_json_is_an_evidence_error(write):
    with pytest.raises(EvidenceError):
        analyze(Args(input=str(write("cov.json", '{"files": {'))))


def test_json_without_files_key_is_an_evidence_error(write):
    with pytest.raises(EvidenceError):
        analyze(Args(input=str(write("cov.json", '{"totals": {"percent": 90}}'))))


# --------------------------------------------------------------------------- #
# findings
# --------------------------------------------------------------------------- #
def test_empty_report_reports_tg005(write):
    empty = '<?xml version="1.0" ?><coverage><packages></packages></coverage>'
    report = analyze(Args(input=str(write("cov.xml", empty))))
    finding = next(f for f in report.findings if f.code == "TG005")
    assert finding.severity == "high"


def test_high_churn_gap_is_tg001(write):
    cov = cobertura({"app/payments.py": COVERED + UNCOVERED})
    history = make_history({"app/payments.py": 6})
    report = analyze(Args(input=str(write("cov.xml", cov)),
                          history=str(write("h.txt", history))))
    finding = next(f for f in report.findings if f.code == "TG001")
    assert finding.severity == "high"
    assert "payments.py" in finding.locator


def test_low_churn_gap_is_not_tg001(write):
    """The same coverage gap, rarely changed, is not the finding the skill exists for."""
    cov = cobertura({"app/payments.py": COVERED + UNCOVERED})
    history = make_history({"app/payments.py": 1})
    report = analyze(Args(input=str(write("cov.xml", cov)),
                          history=str(write("h.txt", history))))
    assert "TG001" not in codes(report)


def test_zero_coverage_frequently_changed_is_tg002(write):
    cov = cobertura({"app/gateway.py": [0] * 12, "app/ok.py": [1] * 5})
    history = make_history({"app/gateway.py": 4})
    report = analyze(Args(input=str(write("cov.xml", cov)),
                          history=str(write("h.txt", history))))
    finding = next(f for f in report.findings if f.code == "TG002")
    assert "gateway.py" in finding.locator


def test_uncovered_test_file_is_tg004(write):
    cov = cobertura({"tests/test_x.py": [0] * 8, "app/x.py": [1] * 5})
    report = analyze(Args(input=str(write("cov.xml", cov))))
    finding = next(f for f in report.findings if f.code == "TG004")
    assert "test_x.py" in finding.locator


def test_file_in_history_absent_from_report_is_tg006(write):
    cov = cobertura({"app/a.py": [1, 1, 0]})
    history = make_history({"app/a.py": 2, "app/b.py": 2})
    report = analyze(Args(input=str(write("cov.xml", cov)),
                          history=str(write("h.txt", history))))
    finding = next(f for f in report.findings if f.code == "TG006")
    assert "app/b.py" in finding.detail


def test_error_paths_flagged_with_source_root(write, repo):
    source = (
        "def charge(amount):\n"       # 1
        "    result = amount * 2\n"   # 2
        "    raise ValueError('x')\n" # 3  error
        "    except KeyError:\n"      # 4  error
        "    log.error('boom')\n"     # 5  error
        "    value = 1\n"             # 6
        "    return value\n"          # 7
    )
    root = repo({"app/payments.py": source})
    cov = cobertura({"app/payments.py": [1, 1, 0, 0, 0, 0, 0]})
    report = analyze(Args(input=str(write("cov.xml", cov)), source_root=str(root)))
    finding = next(f for f in report.findings if f.code == "TG003")
    assert "payments.py" in finding.locator


def test_error_paths_skipped_without_source_root_is_noted(write):
    cov = cobertura({"app/payments.py": [1, 1, 0, 0, 0]})
    report = analyze(Args(input=str(write("cov.xml", cov))))
    assert "TG003" not in codes(report)
    assert any("source-root" in n.lower() for n in report.notes)


def test_high_overall_hiding_busy_files_is_tg007(write):
    cov = cobertura({"app/stable.py": [1] * 90, "app/busy.py": [1] * 10 + [0] * 10})
    history = make_history({"app/busy.py": 6, "app/stable.py": 1})
    report = analyze(Args(input=str(write("cov.xml", cov)),
                          history=str(write("h.txt", history))))
    assert "TG007" in codes(report)


# --------------------------------------------------------------------------- #
# ranking, history window, reproducibility, template
# --------------------------------------------------------------------------- #
def test_without_history_ranks_by_uncovered_and_notes(write):
    cov = cobertura({"app/a.py": [1] + [0] * 20, "app/b.py": [1, 0]})
    report = analyze(Args(input=str(write("cov.xml", cov))))
    assert report.summary["ranked_by"].startswith("uncovered lines")
    assert report.sections["ranking"][0]["path"] == "app/a.py"
    assert any("weaker" in n.lower() for n in report.notes)
    assert "TG001" not in codes(report)


def test_as_of_excludes_later_commits(write):
    cov = cobertura({"app/payments.py": COVERED + UNCOVERED})
    early = make_history({"app/payments.py": 2}, date="2026-05-01")
    late = _shift(make_history({"app/payments.py": 4}, date="2026-06-10"))
    hist_path = str(write("h.txt", early + late))
    report = analyze(Args(input=str(write("cov.xml", cov)), history=hist_path,
                          as_of="2026-05-15"))
    assert report.summary["commits"] == 2
    assert "TG001" not in codes(report)


def _shift(history: str) -> str:
    """Give the 'late' commits distinct SHAs so they are not merged with the early set."""
    out = []
    for line in history.splitlines():
        parts = line.split("\t")
        if len(parts) == 5 and parts[0].isdigit():
            parts[0] = "f" + parts[0][1:]
        out.append("\t".join(parts))
    return "\n".join(out) + "\n"


def test_report_is_reproducible(write):
    cov = cobertura({"app/payments.py": COVERED + UNCOVERED, "app/b.py": [1, 0, 0]})
    history = make_history({"app/payments.py": 6, "app/b.py": 2})
    cov_path = str(write("cov.xml", cov))
    hist_path = str(write("h.txt", history))
    first = analyze(Args(input=cov_path, history=hist_path)).to_dict()
    second = analyze(Args(input=cov_path, history=hist_path)).to_dict()
    assert first == second


def test_all_three_formats_agree_on_the_gap(write):
    cob = analyze(Args(input=str(write("c.xml", cobertura({"app/a.py": [1, 1, 0, 0]})))))
    lc = analyze(Args(input=str(write("c.info", lcov({"app/a.py": [1, 1, 0, 0]})))))
    js = analyze(Args(input=str(write("c.json", covpy({"app/a.py": ([1, 2], [3, 4])})))))
    assert cob.summary["uncovered_lines"] == lc.summary["uncovered_lines"] == js.summary["uncovered_lines"] == 2


def test_the_bundled_template_runs(template):
    report = analyze(Args(input=str(template("test-gap-map", "coverage.example.xml"))))
    assert report.summary["format"] == "cobertura"
    assert report.summary["files"] == 5
    assert report.sections["ranking"][0]["path"] == "app/payments.py"


def test_the_bundled_template_with_history_flags_tg001(write, template):
    history = make_history({"app/payments.py": 7})
    report = analyze(Args(input=str(template("test-gap-map", "coverage.example.xml")),
                          history=str(write("h.txt", history))))
    assert "TG001" in codes(report)


# --------------------------------------------------------------------------- #
# Regression: found by dogfooding against this pack's own coverage report.
# Without --history every finding was gated behind the churn index, so a report
# with 421 uncovered lines produced zero findings and verdict=pass. Silence that
# reads as a clean result is the one failure mode this pack exists to avoid.
# --------------------------------------------------------------------------- #

def test_a_substantial_gap_is_reported_even_without_history(write):
    report = analyze(Args(input=str(write("c.xml", cobertura({
        "src/big.py": [1] * 40 + [0] * 25,
    })))))
    assert "TG008" in codes(report)
    assert report.verdict != "pass", "a 25-line gap must not report as clean"


def test_a_small_gap_without_history_is_left_alone(write):
    report = analyze(Args(input=str(write("c.xml", cobertura({
        "src/small.py": [1] * 40 + [0] * 2,
    })))))
    assert "TG008" not in codes(report)


def test_zero_coverage_without_history_is_high(write):
    report = analyze(Args(input=str(write("c.xml", cobertura({
        "src/never_run.py": [0] * 30,
        "src/fine.py": [1] * 30,
    })))))
    finding = next(f for f in report.findings if f.code == "TG009")
    assert finding.severity == "high"
    assert "src/never_run.py" in finding.locator


def test_the_unweighted_findings_do_not_fire_when_history_is_present(write):
    """With churn known, TG001/TG002 are the right findings — TG008/TG009 would duplicate them."""
    report = analyze(Args(
        input=str(write("c.xml", cobertura({"src/big.py": [1] * 40 + [0] * 25}))),
        history=str(write("h.txt", make_history({"src/big.py": 6}))),
    ))
    assert "TG008" not in codes(report)
    assert "TG009" not in codes(report)


def test_a_test_file_is_never_reported_as_an_unweighted_gap(write):
    report = analyze(Args(input=str(write("c.xml", cobertura({
        "tests/test_thing.py": [1] * 5 + [0] * 30,
    })))))
    assert "TG008" not in codes(report)


# --------------------------------------------------------------------------- #
# Adversarial regressions.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ['"x"', 'null', '[]', '{}'])
def test_a_non_numeric_summary_is_an_evidence_error_not_a_crash(write, value):
    """int() on a non-numeric summary raised ValueError straight through analyze."""
    payload = '{"files": {"a.py": {"summary": {"missing_lines": 2, "covered_lines": %s}}}}' % value
    try:
        analyze(Args(input=str(write("cov.json", payload))))
    except EvidenceError:
        return
    except ValueError as exc:
        raise AssertionError(f"raw ValueError escaped: {exc}") from exc


def test_a_byte_order_mark_does_not_hide_the_format(tmp_path):
    """PowerShell 5.1 and many editors write a BOM; lstrip() does not remove it."""
    body = ('<?xml version="1.0" ?><coverage line-rate="0.5"><packages><package><classes>'
            '<class name="c" filename="app/core.py"><lines>'
            '<line number="1" hits="1"/><line number="2" hits="0"/>'
            '</lines></class></classes></package></packages></coverage>')
    plain = tmp_path / "plain.xml"
    plain.write_bytes(body.encode("utf-8"))
    withbom = tmp_path / "bom.xml"
    withbom.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))

    a = analyze(Args(input=str(plain))).to_dict()
    b = analyze(Args(input=str(withbom))).to_dict()
    a.pop("subject", None)
    b.pop("subject", None)
    assert a == b, "a BOM changed the result"
