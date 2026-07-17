import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


TEMPLATE = Path(__file__).parents[1] / "templates" / "code-quality-node.yml"
SPECIAL_REPORT = "first line\nUnicode: Привет\ncolon: value\npercent: 100%\nlast line\n"
OUTPUT_EXPR = re.compile(
    r"\$\{\{\s*steps\.(lint|typecheck|test|security)\.outputs\.(status|critical|high)\s*\}\}"
)

NPM_STUB = r'''#!/bin/sh
COMMAND="$1"
{
  [ "$#" -eq 0 ] || printf "%s" "$1"
  shift 2>/dev/null || true
  for ARG in "$@"; do printf "\t%s" "$ARG"; done
  printf "\n"
} >> "$NPM_CALLS"
if [ "$COMMAND" = "audit" ]; then
  cat "$AUDIT_STDOUT"
  cat "$AUDIT_STDERR" >&2
  exit "$AUDIT_RC"
fi
cat "$COMMAND_STDOUT"
exit "$COMMAND_RC"
'''

GH_STUB = r'''#!/bin/sh
{
  [ "$#" -eq 0 ] || printf "%s" "$1"
  shift 2>/dev/null || true
  for ARG in "$@"; do printf "\t%s" "$ARG"; done
  printf "\n"
} >> "$GH_CALLS"
case "$1" in
  list) cat "$GH_LIST_STDOUT"; exit "$GH_LIST_RC" ;;
  close) exit "$GH_CLOSE_RC" ;;
  create) exit "$GH_CREATE_RC" ;;
  *) exit 0 ;;
esac
'''

JQ_STUB = r'''#!/usr/bin/env node
const fs = require("fs");

const args = process.argv.slice(2);
let exitStatus = false;
let raw = false;
const variables = {};
let index = 0;
while (index < args.length && args[index].startsWith("-")) {
  if (args[index] === "--arg" || args[index] === "--argjson") {
    variables[args[index + 1]] = args[index + 2];
    index += 3;
  } else {
    exitStatus ||= args[index].includes("e");
    raw ||= args[index].includes("r");
    index += 1;
  }
}
const query = args[index++] || ".";
let data;
try {
  const input = index < args.length
    ? fs.readFileSync(args[index], "utf8")
    : fs.readFileSync(0, "utf8");
  data = JSON.parse(input);
} catch {
  process.exit(4);
}

let value;
let emit = true;
if (query.includes("scripts")) {
  const scripts = data && typeof data.scripts === "object" ? data.scripts : {};
  const candidates = ["typecheck:local", "type-check", "typecheck", "lint", "test"];
  const key = candidates.find(candidate => query.includes(candidate))
    || Object.values(variables).find(candidate => candidate in scripts);
  value = key === undefined ? null : scripts[key];
  if (query.includes("length") && query.includes("> 0")) {
    value = typeof value === "string" && value.length > 0;
  }
} else {
  const comparisons = [
    ...query.matchAll(/severity\s*==\s*["'](critical|high)["']/g),
  ].map(match => match[1]);
  if (query.includes("severity") && comparisons.length === 0) {
    process.exit(2);
  }
  if (/length\s*[+*/-]/.test(query)) {
    process.exit(2);
  }
  if (comparisons.length) {
    if (!query.includes("length")) process.exit(2);
    const findings = data && typeof data.vulnerabilities === "object"
      ? Object.values(data.vulnerabilities)
      : [];
    const counts = comparisons.map(
      severity => findings.filter(finding => finding.severity === severity).length
    );
    if (comparisons.length === 1) {
      value = counts[0];
    } else if (query.includes("@tsv")) {
      value = counts.join("\t");
      raw = true;
    } else if (query.trim().startsWith("{")) {
      value = Object.fromEntries(comparisons.map((severity, position) => [severity, counts[position]]));
    } else {
      value = counts;
    }
  } else if (query === "empty") {
    emit = false;
    value = true;
  } else if (query.includes("type") && query.includes("object")) {
    value = data !== null && typeof data === "object" && !Array.isArray(data);
    if (query.includes("vulnerabilities")) {
      value &&= data.vulnerabilities !== null && typeof data.vulnerabilities === "object";
    }
    if (query.includes("error")) {
      const hasError = Boolean(data && data.error);
      value &&= query.includes("not") || query.includes("null") ? !hasError : hasError;
    }
    if (query.includes("$audit_rc") && query.includes("length > 0")) {
      const vulnerabilityCount = data && typeof data.vulnerabilities === "object"
        ? Object.keys(data.vulnerabilities).length
        : 0;
      value &&= Number(variables.audit_rc) === 0 || vulnerabilityCount > 0;
    }
  } else if (query.includes("error")) {
    const hasError = Boolean(data && data.error);
    value = query.includes("not") || query.includes("null") ? !hasError : hasError;
  } else {
    value = data;
  }
}

if (exitStatus && (value === false || value === null || value === undefined)) {
  process.exit(1);
}
if (emit && value !== undefined) {
  process.stdout.write(
    raw && typeof value === "string" ? String(value) + "\n" : JSON.stringify(value) + "\n"
  );
}
'''


def workflow_steps(source):
    matches = list(re.finditer(r"^      - name: (.+)$", source, re.MULTILINE))
    ends = [match.start() for match in matches[1:]] + [len(source)]
    return [(match.group(1), source[match.start():end]) for match, end in zip(matches, ends)]


def yaml_block(step, key):
    lines = step.splitlines()
    marker = next(index for index, line in enumerate(lines) if line.strip() == f"{key}: |")
    indent = len(lines[marker]) - len(lines[marker].lstrip())
    block = []
    for line in lines[marker + 1:]:
        current = len(line) - len(line.lstrip())
        if line and current <= indent:
            break
        block.append(line[indent + 2:] if line else "")
    return "\n".join(block) + "\n"


def find_bash():
    found = shutil.which("bash")
    if found:
        return found
    roots = filter(None, (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")))
    candidates = [Path(root) / "Git" / "bin" / "bash.exe" for root in roots]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "Git" / "bin" / "bash.exe")
    return str(next((candidate for candidate in candidates if candidate.exists()), ""))


def bash_path(path):
    resolved = Path(path).resolve().as_posix()
    if re.match(r"^[A-Za-z]:/", resolved):
        return f"/{resolved[0].lower()}/{resolved[3:]}"
    return resolved


class CodeQualityTemplateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEMPLATE.read_text(encoding="utf-8")
        cls.steps = workflow_steps(cls.source)
        cls.by_name = dict(cls.steps)
        cls.bash = find_bash()
        if not cls.bash:
            raise RuntimeError("bash is required; Git Bash fallback was not found")
        if not shutil.which("node"):
            raise RuntimeError("Node.js is required to evaluate audit JSON fixtures")

    def run_shell(
        self,
        script,
        *,
        package=None,
        command_rc=0,
        command_stdout=SPECIAL_REPORT,
        audit_rc=0,
        audit_stdout='{"vulnerabilities":{}}',
        audit_stderr="",
        extra_env=None,
    ):
        return self.run_batch(
            [
                {
                    "script": script,
                    "package": package,
                    "command_rc": command_rc,
                    "command_stdout": command_stdout,
                    "audit_rc": audit_rc,
                    "audit_stdout": audit_stdout,
                    "audit_stderr": audit_stderr,
                    "extra_env": extra_env,
                }
            ]
        )[0]

    def run_batch(self, cases):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "bin"
            tools.mkdir()
            for tool, source in (("npm", NPM_STUB), ("jq", JQ_STUB), ("gh", GH_STUB)):
                wrapper = tools / tool
                wrapper.write_text(source, encoding="utf-8")
                wrapper.chmod(0o755)

            case_paths = []
            for index, case in enumerate(cases):
                case_root = root / f"case-{index:02d}"
                case_root.mkdir()
                (case_root / "qr").mkdir()
                package = case.get("package") or {"scripts": {}}
                (case_root / "package.json").write_text(
                    json.dumps(package, ensure_ascii=False), encoding="utf-8"
                )
                command_file = case_root / "command.txt"
                audit_file = case_root / "audit.json"
                audit_error = case_root / "audit.err"
                calls_file = case_root / "npm-calls.jsonl"
                gh_calls_file = case_root / "gh-calls.tsv"
                gh_list_file = case_root / "gh-list.txt"
                output_file = case_root / "github-output"
                command_file.write_text(case.get("command_stdout", SPECIAL_REPORT), encoding="utf-8")
                audit_stdout = case.get("audit_stdout", '{"vulnerabilities":{}}')
                audit_file.write_text(audit_stdout, encoding="utf-8")
                audit_error.write_text(case.get("audit_stderr", ""), encoding="utf-8")
                gh_list_file.write_text(case.get("gh_list_stdout", ""), encoding="utf-8")
                variables = {
                    "GITHUB_OUTPUT": bash_path(output_file),
                    "NPM_CALLS": bash_path(calls_file),
                    "COMMAND_RC": str(case.get("command_rc", 0)),
                    "COMMAND_STDOUT": bash_path(command_file),
                    "AUDIT_RC": str(case.get("audit_rc", 0)),
                    "AUDIT_STDOUT": bash_path(audit_file),
                    "AUDIT_STDERR": bash_path(audit_error),
                    "GH_CALLS": bash_path(gh_calls_file),
                    "GH_LIST_STDOUT": bash_path(gh_list_file),
                    "GH_LIST_RC": str(case.get("gh_list_rc", 0)),
                    "GH_CLOSE_RC": str(case.get("gh_close_rc", 0)),
                    "GH_CREATE_RC": str(case.get("gh_create_rc", 0)),
                    **(case.get("extra_env") or {}),
                }
                exports = "\n".join(
                    f"export {key}={shlex.quote(str(value))}" for key, value in variables.items()
                )
                (case_root / "case.sh").write_text(
                    f"export PATH={shlex.quote(bash_path(tools))}:$PATH\n"
                    f"{exports}\n{case['script']}",
                    encoding="utf-8",
                )
                case_paths.append(case_root)

            master = (
                'set +e\n'
                'for CASE in "$PWD"/case-*; do\n'
                '  (cd "$CASE"; set -e; . ./case.sh)\n'
                '  RC=$?\n'
                '  printf "%s\\n" "$RC" > "$CASE/process-rc"\n'
                'done\n'
                'exit 0\n'
            )
            try:
                process = subprocess.run(
                    [self.bash, "--noprofile", "--norc", "-c", master],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                )
            except subprocess.TimeoutExpired as error:
                self.fail(
                    f"bash fixture batch timed out after 20s; "
                    f"stdout={error.stdout!r}; stderr={error.stderr!r}"
                )
            self.assertEqual(
                process.returncode,
                0,
                f"fixture harness failed: stdout={process.stdout!r}; stderr={process.stderr!r}",
            )

            results = []
            for case_root in case_paths:
                output_file = case_root / "github-output"
                calls_file = case_root / "npm-calls.jsonl"
                gh_calls_file = case_root / "gh-calls.tsv"
                raw_output = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
                output_records = {}
                invalid_output = []
                for line in raw_output.splitlines():
                    if not line or "=" not in line:
                        invalid_output.append(line)
                        continue
                    key, value = line.split("=", 1)
                    output_records.setdefault(key, []).append(value)
                calls = [
                    line.split("\t")
                    for line in calls_file.read_text(encoding="utf-8").splitlines()
                ] if calls_file.exists() else []
                gh_calls = [
                    line.split("\t")
                    for line in gh_calls_file.read_text(encoding="utf-8").splitlines()
                ] if gh_calls_file.exists() else []
                reports = {
                    path.relative_to(case_root).as_posix(): path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                    for path in (case_root / "qr").rglob("*")
                    if path.is_file()
                }
                results.append(
                    SimpleNamespace(
                        rc=int((case_root / "process-rc").read_text(encoding="utf-8")),
                        stdout=process.stdout,
                        stderr=process.stderr,
                        outputs=output_records,
                        invalid_output=invalid_output,
                        raw_output=raw_output,
                        calls=calls,
                        gh_calls=gh_calls,
                        reports=reports,
                    )
                )
            return results

    def test_harness_models_github_bash_errexit_without_pipefail(self):
        result = self.run_shell(
            "false | while read -r line; do :; done\n"
            "printf 'marker=CREATED\\n' >> \"$GITHUB_OUTPUT\"\n"
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.outputs.get("marker"), ["CREATED"])

    def run_step(self, name, **kwargs):
        return self.run_shell(yaml_block(self.by_name[name], "run"), **kwargs)

    def assert_failed_command_capture(self, step, package, expected_call, exit_code):
        result = self.run_step(
            step,
            package=package,
            command_rc=exit_code,
            command_stdout=SPECIAL_REPORT,
        )
        problems = []
        if result.rc != 0:
            problems.append(f"capture step exited {result.rc} under bash -e")
        if result.calls != [expected_call]:
            problems.append(f"npm calls were {result.calls}")
        if result.outputs.get("status") != [str(exit_code)]:
            problems.append(f"status output was {result.outputs.get('status')}")
        if result.invalid_output:
            problems.append(f"invalid GITHUB_OUTPUT lines: {result.invalid_output}")
        if SPECIAL_REPORT not in result.reports.values():
            problems.append(f"complete report was not preserved: {list(result.reports)}")
        if SPECIAL_REPORT.strip() in result.raw_output:
            problems.append("multiline report leaked into GITHUB_OUTPUT")
        self.assertFalse(problems, "; ".join(problems))

    def test_lint_failure_captures_report_and_status_under_bash_errexit(self):
        self.assert_failed_command_capture(
            "ESLint", {"scripts": {"lint": "eslint ."}}, ["run", "lint"], 17
        )

    def test_typecheck_failure_captures_report_and_status_under_bash_errexit(self):
        self.assert_failed_command_capture(
            "TypeScript Check",
            {"scripts": {"typecheck": "tsc --noEmit"}},
            ["run", "typecheck"],
            19,
        )

    def test_test_failure_captures_report_and_status_under_bash_errexit(self):
        self.assert_failed_command_capture(
            "Tests", {"scripts": {"test": "vitest run"}}, ["test"], 23
        )

    def test_lint_and_test_success_capture_report_and_zero_status(self):
        cases = (
            ("ESLint", {"lint": "eslint ."}, ["run", "lint"]),
            ("Tests", {"test": "vitest run"}, ["test"]),
        )
        results = self.run_batch(
            [
                {
                    "script": yaml_block(self.by_name[step], "run"),
                    "package": {"scripts": scripts},
                    "command_stdout": SPECIAL_REPORT,
                }
                for step, scripts, _ in cases
            ]
        )
        for (step, _, expected_call), result in zip(cases, results):
            with self.subTest(step=step):
                self.assertEqual(result.rc, 0)
                self.assertEqual(result.calls, [expected_call])
                self.assertEqual(result.outputs.get("status"), ["0"])
                self.assertEqual(result.invalid_output, [])
                self.assertIn(SPECIAL_REPORT, result.reports.values())

    def test_missing_and_empty_commands_skip_without_npm(self):
        cases = (
            ("ESLint", {}),
            ("ESLint", {"lint": ""}),
            ("TypeScript Check", {}),
            ("TypeScript Check", {"typecheck": "", "type-check": "", "typecheck:local": ""}),
            ("Tests", {}),
            ("Tests", {"test": ""}),
        )
        results = self.run_batch(
            [
                {
                    "script": yaml_block(self.by_name[step], "run"),
                    "package": {"scripts": scripts},
                }
                for step, scripts in cases
            ]
        )
        for (step, scripts), result in zip(cases, results):
            with self.subTest(step=step, scripts=scripts):
                self.assertEqual(result.rc, 0)
                self.assertEqual(result.calls, [])
                self.assertEqual(result.outputs.get("status"), ["skip"])

    def test_typecheck_discovery_executes_first_configured_candidate(self):
        cases = (
            ({"typecheck": "one"}, "typecheck"),
            ({"type-check": "two"}, "type-check"),
            ({"typecheck:local": "three"}, "typecheck:local"),
            ({"typecheck": "one", "type-check": "two", "typecheck:local": "three"}, "typecheck"),
            ({"type-check": "two", "typecheck:local": "three"}, "type-check"),
            ({"typecheck": "", "type-check": "", "typecheck:local": "three"}, "typecheck:local"),
        )
        results = self.run_batch(
            [
                {
                    "script": yaml_block(self.by_name["TypeScript Check"], "run"),
                    "package": {"scripts": scripts},
                }
                for scripts, _ in cases
            ]
        )
        for (scripts, selected), result in zip(cases, results):
            with self.subTest(scripts=scripts):
                self.assertEqual(result.rc, 0)
                self.assertEqual(result.calls, [["run", selected]])
                self.assertEqual(result.outputs.get("status"), ["0"])

    def test_audit_matrix_preserves_payload_counts_and_error_semantics(self):
        vulnerabilities = lambda *severities: {
            "vulnerabilities": {
                f"package-{index}": {"severity": severity}
                for index, severity in enumerate(severities)
            }
        }
        cases = (
            ("zero", json.dumps(vulnerabilities()), 0, "ok", 0, 0),
            ("one-high", json.dumps(vulnerabilities("high")), 1, "high", 0, 1),
            (
                "multiple",
                json.dumps(vulnerabilities("critical", "high", "critical", "high")),
                1,
                "critical",
                2,
                2,
            ),
            ("one-critical", json.dumps(vulnerabilities("critical")), 1, "critical", 1, 0),
            ("low-moderate", json.dumps(vulnerabilities("low", "moderate")), 1, "ok", 0, 0),
            ("nonzero-without-vulnerabilities", json.dumps(vulnerabilities()), 1, "error", 0, 0),
            ("scalar", '"valid-json-scalar"', 0, "error", 0, 0),
            ("array", "[]", 0, "error", 0, 0),
            ("null", "null", 0, "error", 0, 0),
            ("empty", "", 42, "error", 0, 0),
            ("malformed", "{not-json", 1, "error", 0, 0),
            ("error-payload", '{"error":{"code":"ENOAUDIT"}}', 1, "error", 0, 0),
            ("tool-failure", "", 73, "error", 0, 0),
        )
        results = self.run_batch(
            [
                {
                    "script": yaml_block(self.by_name["Security Audit"], "run"),
                    "audit_rc": npm_rc,
                    "audit_stdout": payload,
                    "audit_stderr": "network unavailable\n" if name == "tool-failure" else "",
                }
                for name, payload, npm_rc, _, _, _ in cases
            ]
        )
        failures = []
        for (name, payload, _, status, critical, high), result in zip(cases, results):
            observed = {
                key: values[-1] for key, values in result.outputs.items() if values
            }
            case_problems = []
            if result.rc != 0:
                case_problems.append(f"step rc={result.rc}")
            if result.calls != [["audit", "--json"]]:
                case_problems.append(f"npm calls={result.calls}")
            if observed.get("status") != status:
                case_problems.append(f"status={observed.get('status')!r}")
            if observed.get("critical") != str(critical):
                case_problems.append(f"critical={observed.get('critical')!r}")
            if observed.get("high") != str(high):
                case_problems.append(f"high={observed.get('high')!r}")
            for key in ("critical", "high"):
                values = result.outputs.get(key, [])
                if len(values) != 1 or not values[0].isdigit() or int(values[0]) < 0:
                    case_problems.append(f"{key} is not one scalar non-negative integer: {values}")
            if len(result.outputs.get("status", [])) != 1 or result.invalid_output:
                case_problems.append("status/GITHUB_OUTPUT is not single-line")
            if payload and payload not in result.reports.values():
                case_problems.append("audit stdout was not preserved unchanged in a file")
            if case_problems:
                failures.append(f"{name}: {', '.join(case_problems)}")
        self.assertFalse(failures, "\n".join(failures))

    def gate_step(self):
        evidence = ("Comment on PR", "Send webhook", "Create issue (critical on main)")
        names = [name for name, _ in self.steps]
        last_evidence = max(names.index(name) for name in evidence)
        candidates = [
            (index, name, body)
            for index, (name, body) in enumerate(self.steps)
            if index > last_evidence
            and "steps.typecheck.outputs.status" in body
            and "steps.test.outputs.status" in body
            and "steps.security.outputs.status" in body
        ]
        self.assertEqual(len(candidates), 1, f"final gate candidates: {[name for _, name, _ in candidates]}")
        return candidates[0]

    def gate_case(self, values):
        _, _, body = self.gate_step()
        script = yaml_block(body, "run")

        def replacement(match):
            return shlex.quote(values.get((match.group(1), match.group(2)), ""))

        script = OUTPUT_EXPR.sub(replacement, script)
        env = {}
        for line in body.splitlines():
            match = re.match(r'\s*([A-Za-z_][A-Za-z0-9_]*):\s*["\']?(.*?)["\']?\s*$', line)
            expression = OUTPUT_EXPR.search(line)
            if match and expression:
                env[match.group(1)] = values.get((expression.group(1), expression.group(2)), "")
        return {"script": script, "extra_env": env}

    def test_final_gate_truth_table_runs_after_evidence(self):
        base = {
            ("lint", "status"): "0",
            ("typecheck", "status"): "0",
            ("test", "status"): "0",
            ("security", "status"): "ok",
            ("security", "critical"): "0",
            ("security", "high"): "0",
        }
        cases = (
            ("all-pass", {}, True),
            ("required-skips", {("typecheck", "status"): "skip", ("test", "status"): "skip"}, True),
            ("lint-advisory", {("lint", "status"): "85"}, True),
            ("high-advisory", {("security", "status"): "high", ("security", "high"): "3"}, True),
            ("missing-typecheck", {("typecheck", "status"): ""}, False),
            ("missing-test", {("test", "status"): ""}, False),
            ("missing-security-status", {("security", "status"): ""}, False),
            ("missing-critical-count", {("security", "critical"): ""}, False),
            ("typecheck-required", {("typecheck", "status"): "2"}, False),
            ("test-required", {("test", "status"): "3"}, False),
            (
                "critical-required",
                {("security", "status"): "critical", ("security", "critical"): "1"},
                False,
            ),
            ("audit-error-required", {("security", "status"): "error"}, False),
        )
        failures = []
        try:
            fixtures = [
                self.gate_case({**base, **changes})
                for _, changes, _ in cases
            ]
        except AssertionError as error:
            self.fail(str(error))
        results = self.run_batch(fixtures)
        for (name, _, should_pass), result in zip(cases, results):
            passed = result.rc == 0
            if passed != should_pass:
                failures.append(f"{name}: gate rc={result.rc}, expected {'pass' if should_pass else 'fail'}")
        self.assertFalse(failures, "\n".join(failures))

    def test_reports_expose_policy_and_evidence_precedes_gate(self):
        report = self.by_name["Comment on PR"].lower()
        for label in ("required", "advisory", "skip", "error"):
            self.assertIn(label, report)
        for policy, outcome in (
            ("eslint", "advisory"),
            ("high", "advisory"),
            ("typescript", "required"),
            ("tests", "required"),
            ("critical", "required"),
            ("error", "required"),
        ):
            self.assertRegex(report, rf"(?s){policy}.{{0,240}}{outcome}|{outcome}.{{0,240}}{policy}")
        gate_index, _, _ = self.gate_step()
        names = [name for name, _ in self.steps]
        for evidence in ("Comment on PR", "Send webhook", "Create issue (critical on main)"):
            self.assertLess(names.index(evidence), gate_index)
            self.assertIn("always()", self.by_name[evidence])

    def test_repeated_run_artifacts_keep_single_open_evidence(self):
        comment = self.by_name["Comment on PR"]
        issue = self.by_name["Create issue (critical on main)"]
        self.assertIn("updateComment", comment)
        update_existing = re.search(r"gh issue (?:edit|comment)|gh api[^\n]*(?:PATCH|--method PATCH)", issue)
        close_then_create = (
            "gh issue close" in issue
            and "gh issue create" in issue
            and issue.index("gh issue close") < issue.index("gh issue create")
        )
        self.assertTrue(update_existing or close_then_create)
        self.assertNotIn("workflow_dispatch:", self.source)

    def test_main_push_issue_condition_includes_all_required_failures(self):
        condition = yaml_block(self.by_name["Create issue (critical on main)"], "if")
        with self.subTest(required="typecheck"):
            typecheck_at = condition.find("steps.typecheck.outputs.status")
            self.assertGreaterEqual(typecheck_at, 0)
            self.assertIn("'0'", condition[typecheck_at:typecheck_at + 220])
            self.assertIn("'skip'", condition[typecheck_at:typecheck_at + 220])
        with self.subTest(required="security-error"):
            self.assertRegex(
                condition,
                r"steps\.security\.outputs\.status\s*==\s*['\"]error['\"]",
            )

    def test_pr_comment_search_is_paginated_and_updates_in_place(self):
        comment = self.by_name["Comment on PR"]
        paginated = "github.paginate" in comment or (
            "listComments" in comment
            and "per_page" in comment
            and re.search(r"\bpage\b", comment)
        )
        self.assertTrue(paginated)
        self.assertIn("updateComment", comment)

    def test_same_ref_runs_and_label_less_issue_fallback_stay_single(self):
        concurrency_at = self.source.find("concurrency:")
        issue = self.by_name["Create issue (critical on main)"]
        with self.subTest(behavior="same-ref-serialization"):
            self.assertGreaterEqual(concurrency_at, 0)
            concurrency_body_at = self.source.index("\n", concurrency_at) + 1
            next_top_level = re.search(
                r"(?m)^[A-Za-z_][^:\n]*:", self.source[concurrency_body_at:]
            )
            concurrency_end = (
                concurrency_body_at + next_top_level.start()
                if next_top_level
                else len(self.source)
            )
            concurrency = self.source[concurrency_at:concurrency_end]
            self.assertIn("github.repository", concurrency)
            self.assertIn("github.ref", concurrency)
            self.assertNotRegex(concurrency, r"github\.(?:run_id|sha)")
            self.assertRegex(concurrency, r"cancel-in-progress:\s*false")
        with self.subTest(behavior="label-independent-marker-lookup"):
            lookups = re.findall(
                r"(?:gh issue list|gh api)[^\n]*(?:\\\s*\n[^\n]*)*",
                issue,
            )
            independent_marker_lookup = any(
                "--label" not in lookup
                and re.search(r"(?:--search|title|body|marker)", lookup, re.IGNORECASE)
                for lookup in lookups
            )
            self.assertTrue(independent_marker_lookup, lookups)
        with self.subTest(behavior="label-less-create-fallback"):
            starts = [match.start() for match in re.finditer(r"gh issue create", issue)]
            creates = [
                issue[start:end]
                for start, end in zip(starts, starts[1:] + [len(issue)])
            ]
            self.assertTrue(any("--label" not in command for command in creates))

    def test_main_issue_does_not_create_after_list_or_close_failure(self):
        script = yaml_block(self.by_name["Create issue (critical on main)"], "run")
        github_values = {
            "github.ref_name": "main",
            "github.sha": "0123456789abcdef",
            "github.server_url": "https://github.example",
            "github.repository": "owner/repository",
            "github.run_id": "123",
            "github.repository_owner": "owner",
        }

        def replace_expression(match):
            expression = match.group(1).strip()
            return github_values.get(expression, "0" if expression.startswith("steps.") else "fixture")

        script = re.sub(r"\$\{\{\s*([^}]+?)\s*\}\}", replace_expression, script)
        cases = (
            {
                "name": "close-failure",
                "gh_list_stdout": "42\n",
                "gh_close_rc": 9,
                "extra_env": {"GH_TOKEN": "fixture"},
            },
            {
                "name": "list-failure",
                "gh_list_rc": 7,
                "extra_env": {"GH_TOKEN": "fixture"},
            },
        )
        results = self.run_batch([{"script": script, **case} for case in cases])
        failures = []
        for case, result in zip(cases, results):
            creates = [call for call in result.gh_calls if call[:2] == ["issue", "create"]]
            closes = [call for call in result.gh_calls if call[:2] == ["issue", "close"]]
            problems = []
            if case["name"] == "close-failure" and closes and result.rc == 0:
                problems.append("failed close did not fail the step")
            if result.rc == 0 and case["name"] == "list-failure":
                problems.append("failed list did not fail the step")
            if creates:
                problems.append(f"create was called after failure: {creates}")
            if problems:
                failures.append(f"{case['name']}: {', '.join(problems)}")
        self.assertFalse(failures, "\n".join(failures))

    def test_template_bytes_match_universal_newline_render(self):
        rendered = TEMPLATE.read_text(encoding="utf-8").encode("utf-8")
        self.assertEqual(TEMPLATE.read_bytes(), rendered)

    def test_template_git_attribute_requires_lf_checkouts(self):
        result = subprocess.run(
            ["git", "check-attr", "eol", "--", "templates/code-quality-node.yml"],
            cwd=TEMPLATE.parents[1],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().rsplit(": ", 1)[-1], "lf")


if __name__ == "__main__":
    unittest.main()
