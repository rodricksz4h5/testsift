# testsift

Exact, local analysis of JUnit XML test reports, as three Hermes tools.

Every major test runner and CI system can write JUnit XML: pytest
(`--junitxml`), Maven Surefire/Failsafe, Gradle, Jest (`jest-junit`), Vitest
(`--reporter=junit`), Go (`go-junit-report`, `gotestsum --junitfile`), PHPUnit,
.NET (`JunitXml.TestLogger`), Playwright, Cypress. A real suite's report is
thousands of test cases with full stack traces, far too much to put in a model's
context, and "which tests broke and why" is exactly the question a coding agent
gets asked after a red CI run. testsift reads the report on disk and returns
**exact** answers.

## Why a tool and not a prompt or skill

Three things a model can't do reliably over a file it can't read whole:

- **Exact counts** across one report or a whole directory of them
  (`target/surefire-reports/`, a downloaded CI artifact).
- **Failures grouped by cause.** 300 failing tests are often one or two root
  causes. Messages that differ only in numbers, ids, hashes or quoted values
  share a group:

  ```
  AssertionError: AssertionError: expected even, got <NUM>    x2
      test_sample::test_parity[3], test_sample::test_parity[17]
  ```

- **Run-to-run diffs.** Given a baseline run and a new run, which tests
  *newly* fail, which were fixed, which were already failing, what was added or
  removed and what got much slower. This separates "my change broke it" from
  "it was already red".

## Tools

All three always return a JSON string; a bad path or an unreadable report
returns `{ "ok": false, "error": "..." }` rather than raising. `path` (and
`base`/`head`) is a report file, or a directory searched recursively for
`*.xml` reports. XML that isn't a test report (`pom.xml`, `coverage.xml`) is
skipped and listed in `files_skipped`.

### `test_report_summary`

| arg | type | required | default | meaning |
|---|---|---|---|---|
| `path` | string | yes | - | report file or directory |
| `max_groups` | integer | no | `10` | failure groups to return (cap 100) |
| `slowest` | integer | no | `10` | slowest tests to return (cap 100) |

Returns `tests`, `passed`, `failed`, `errors`, `skipped`, `duration_seconds`,
`failure_groups:[{signature,count,example_message,tests}]`,
`suites_with_most_failures`, `slowest:[{test,seconds}]` and `flaky_suspects`
(tests with Surefire `flakyFailure`/`rerunFailure` entries, or the same test id
both passing and failing in the input).

### `test_report_failures`

| arg | type | required | default | meaning |
|---|---|---|---|---|
| `path` | string | yes | - | report file or directory |
| `contains` | string | no | - | only failures whose id, type or message contains this (case-insensitive) |
| `include_output` | boolean | no | `false` | add the tail of each failing test's system-out/system-err |
| `offset` | integer | no | `0` | failures to skip |
| `limit` | integer | no | `20` | failures to return (cap 200) |

Each failure has `test` (`classname::name`), `status` (`failed` or `error`),
`suite`, `file`/`line` when the report has them, `type`, `message`, `detail`
(the trace, as its first and last 2,000 characters when longer) and `report`.

### `test_report_compare`

| arg | type | required | default | meaning |
|---|---|---|---|---|
| `base` | string | yes | - | baseline report file or directory |
| `head` | string | yes | - | new report file or directory |
| `limit` | integer | no | `50` | entries per list (cap 500) |

Returns totals for both sides and `newly_failing`, `added_failing`, `fixed`,
`still_failing`, `added`, `removed` and `slower` (at least twice as slow and at
least one second slower), each with a `*_total` count.

## How results are counted

- A test is identified by `classname::name` (or `name` alone when there is no
  classname). When the same id appears more than once, the worst status wins
  (error > failed > skipped > passed) and its times are added up.
- A test case with both `<failure>` and `<error>` counts as an error.
- Surefire reruns (`flakyFailure`, `flakyError`, `rerunFailure`, `rerunError`)
  don't change the final status; they mark the test as a flaky suspect.
- `time` attributes that aren't a non-negative number are ignored. A thousands
  separator is accepted only together with a decimal point (`1,234.5`), since
  `1,5` is ambiguous.
- A report cut off mid-write (a killed CI job) keeps the test cases before the
  break and is listed under `files_with_parse_errors`.

## Safety and disclosure

- **Reads only the paths you pass**, read-only. Never writes, deletes or runs
  anything.
- **Honours Hermes' read denylist.** Inside the agent every file, including
  each file found in a directory, goes through the same check Core's
  `read_file` uses, so credential stores, internal caches and `.env` files are
  refused, also through symlinks.
- **Regular files only.** Paths under `/proc` and `/dev` (including symlinks
  that resolve there) are refused, and so is anything that isn't a regular
  file (FIFOs, sockets, devices), before it is opened. Symlinked directories
  are not followed during a directory walk.
- **Safe XML.** Reports are parsed by a streaming expat parser that refuses any
  DOCTYPE or entity declaration, so entity expansion ("billion laughs") and
  external entities can't be used. JUnit reports never need either.
- **Bounded.** Reports are streamed in 64 KB pieces. Each failure keeps at most
  4,000 characters of trace and 1,000 of output tail, however large the report.
  A call reads at most 5,000 files and 2,000,000 test cases; hitting either is
  flagged in the result.
- **No network, no credentials, no telemetry, no background processes, no
  self-update.** Standard library only.

## Install

```bash
hermes plugins install rodricksz4h5/testsift
```

## License

MIT, see [LICENSE](./LICENSE).
