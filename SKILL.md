---
name: test-gap-map
description: Rank uncovered code by risk — uncovered lines multiplied by how often the file changes — from a Cobertura XML, LCOV, or coverage.py JSON report, so testing effort goes where the next incident is forming rather than wherever the percentage is lowest. Trigger when the user says "/test-gap-map", "where should I add tests", "which uncovered code matters most", "rank my coverage gaps", "what's risky and untested", or hands over a coverage report. This reports where tests are missing and does NOT write them — use /tdd to actually write tests. For mapping a whole repository's structure use /repo-cartographer. It reads a coverage report and invokes no git command.
---

# Test Gap Map

Coverage percentage is a vanity metric. A file that is 40% covered and has not
changed in a year is fine. A file that is 78% covered and changed in thirty
commits is where the next incident is already forming. The number on the
dashboard cannot tell those two apart — so teams write tests for whatever is
reddest, which is almost never whatever is riskiest.

This ranks the uncovered code by **risk = uncovered lines × how often the file
changes**, and says so plainly, so the top of the list is where a test is worth
the most.

## What this is not

- **`/tdd`** writes tests. This does not write a single test — it tells you
  *where* the missing ones would pay off, and you (or `/tdd`) write them.
- **`/repo-cartographer`** maps a whole repository's structure and never reads
  coverage. This reads only coverage and history.
- No network, no git path: it reads a coverage report you already generated.

## Inputs

A coverage report in any of three formats — the format is auto-detected from the
content, you do not declare it:

- **Cobertura XML** — `<coverage><packages><package><classes><class filename= line-rate=><lines><line number= hits=`
- **LCOV** — `SF:` / `DA:line,hits` / `end_of_record`
- **coverage.py JSON** — `{"files": {"path": {"summary": …, "missing_lines": […]}}}`

```
python scripts/test_gap_map.py --input coverage.xml --outdir out/test-gap-map
```

Add an exported history to turn the ranking from "most uncovered lines" into
"most risk", and `--as-of` to pin the window for a reproducible run:

```
git log --numstat --date=short \
    --pretty=format:"%H%x09%an%x09%ae%x09%ad%x09%s" > history.txt

python scripts/test_gap_map.py --input coverage.info \
    --history history.txt --as-of 2026-07-30 --source-root .
```

`--source-root` points at the repository so uncovered lines can be read and the
error-handling gaps (`TG003`) picked out. `--top N` (default 15) sets how many
rows the ranking shows. A bundled `templates/coverage.example.xml` is a working
Cobertura example.

## How to run it

Point `--input` at the report your test run already produced. The tool parses it,
weights each file by how often history touched it, ranks the gaps, and writes
JSON, Markdown, and a self-contained HTML dashboard to `--outdir`.

## What it detects

- `TG001` **high-churn code with a substantial coverage gap** — the finding the
  skill exists for. High: code that moves constantly and is only partly tested is
  where the next regression lands.
- `TG002` **a frequently changed file with zero coverage** — every change ships
  untested. High.
- `TG005` **coverage reported over zero files** — a run that measures nothing and
  exits successfully, reading as 100% on every dashboard. High.
- `TG006` **source in history that coverage never measured** — an unmeasured file
  reads as perfect while being tested not at all.
- `TG004` **a test file the suite never ran** — a test showing zero coverage of
  its own lines is reassurance the suite is not actually providing.
- `TG003` **uncovered lines concentrated in error handling** — the untested code
  is exactly what runs once something has already gone wrong. Needs
  `--source-root`.
- `TG007` **high overall coverage hiding busy, under-tested files** — the
  headline number carried by stable code while the churning code is thin. Low.

## Limits — state these when you report

- **This does not write tests.** It ranks where they are missing. Handing the top
  of the list to `/tdd` is the next step; this tool never touches your test
  suite.
- **Risk needs history.** Without `--history`, files are ranked by uncovered line
  count alone, and `TG001`, `TG002`, `TG006`, and `TG007` are skipped. The report
  says the ranking is weaker and why — it never invents churn.
- **Path matching is heuristic.** Coverage paths are matched to history by exact
  path or unique basename. A file that moved, or is reported under an unusual
  path, may not match and would then rank as if it never changed.
- **`TG003` needs the source.** Without `--source-root` the uncovered lines cannot
  be read, so the error-handling analysis is skipped and noted.
- **Coverage counts lines, not correctness.** A covered line is a line that ran
  during the suite, not a line that is *tested*. A high rank here is a strong
  signal; a low rank is not a guarantee.

## Guardrails

Reads one coverage report and, optionally, one history file and a read-only
source root. Writes three artifacts to your output directory and nothing else. No
git invocation, no network, no test execution, no cloud writes.
