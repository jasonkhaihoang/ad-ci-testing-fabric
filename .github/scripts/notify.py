"""
Post two PR comments:
  1. Ephemeral workspace notification (URL + checklist).
  2. Static analysis details (summary table + per-tool sub-sections).

Reads JSON reports from the reports/ directory.
Posts via the GitHub CLI (gh) using the GH_TOKEN env var.
"""

import json
import os
import subprocess
import sys
import tempfile
from collections import Counter


FABRIC_WORKSPACE_URL = "https://app.fabric.microsoft.com/groups/{workspace_id}/list?experience=fabric-developer"
COMMENT_MARKER = "<!-- ephemeral-workspace-ready -->"
DETAILS_COMMENT_MARKER = "<!-- static-analysis-details -->"


def load_report(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def icon(passed: bool) -> str:
    return "✅" if passed else "❌"


_VIOLATION_CAP = 20


def _format_naming_violations_table(violations: list) -> str:
    remainder = max(0, len(violations) - _VIOLATION_CAP)
    rows = "\n".join(
        f"| `{v['model']}` | `{v['path']}` | {v['issue']} |"
        for v in violations[:_VIOLATION_CAP]
    )
    tail = f"\n\n_…and {remainder} more_" if remainder else ""
    return (
        f"<details>\n<summary>Naming violations ({len(violations)})</summary>\n\n"
        f"| Model | Path | Reason |\n"
        f"|-------|------|--------|\n"
        f"{rows}{tail}\n\n"
        f"</details>\n"
    )


def format_ruff(report: list) -> tuple[bool, str]:
    """Returns (passed, sub_section_markdown)."""
    issues = report if isinstance(report, list) else []
    count = len(issues)
    if count == 0:
        return True, "#### Ruff\n\n✅ No issues\n"

    rule_counts = Counter(item.get("code", "unknown") for item in issues)
    lines = "\n".join(
        f"- `{rule}` — {n} violation(s)"
        for rule, n in sorted(rule_counts.items())
    )
    section = (
        f"#### Ruff\n\n"
        f"❌ {count} issue(s)\n\n"
        f"<details>\n<summary>Per-rule breakdown</summary>\n\n"
        f"{lines}\n\n"
        f"</details>\n"
    )
    return False, section


def format_sqlfluff(report) -> tuple[bool, str]:
    """Returns (passed, sub_section_markdown)."""
    file_results = report if isinstance(report, list) else report.get("files", [])
    file_counts: dict[str, int] = {}
    total = 0
    for file_result in file_results:
        filepath = file_result.get("filepath", "unknown")
        n = len(file_result.get("violations", []))
        if n:
            file_counts[filepath] = n
            total += n

    if total == 0:
        return True, "#### SQLFluff\n\n✅ No violations\n"

    lines = "\n".join(
        f"- `{fp}` — {n} violation(s)"
        for fp, n in sorted(file_counts.items())
    )
    section = (
        f"#### SQLFluff\n\n"
        f"❌ {total} violation(s)\n\n"
        f"<details>\n<summary>Per-file breakdown</summary>\n\n"
        f"{lines}\n\n"
        f"</details>\n"
    )
    return False, section


def format_gitleaks(report: dict | list) -> tuple[bool, str]:
    """Returns (passed, sub_section_markdown). Never includes raw secret values."""
    findings = report if isinstance(report, list) else report.get("findings", [])
    count = len(findings)
    if count == 0:
        return True, "#### Gitleaks\n\n✅ No secrets found\n"

    lines = []
    for finding in findings:
        secret_type = finding.get("RuleID") or finding.get("Description", "unknown")
        file_path = finding.get("File", "unknown")
        line_num = finding.get("StartLine", "?")
        lines.append(f"- `{secret_type}` in `{file_path}` line {line_num}")

    detail = "\n".join(lines)
    section = (
        f"#### Gitleaks\n\n"
        f"❌ **{count} secret(s) found — BLOCK**\n\n"
        f"<details>\n<summary>Findings (type · file · line)</summary>\n\n"
        f"{detail}\n\n"
        f"</details>\n"
    )
    return False, section


def format_scorecard_section(report: dict) -> tuple[bool, str]:
    """Returns (passed, sub_section_markdown)."""
    if not report:
        return False, "#### dbt Scorecard\n\n⚠️ Scorecard unavailable — `dbt parse` may have failed.\n"
    desc = report.get("description_coverage_pct", 0)
    col = report.get("column_coverage_pct", 0)
    pk = report.get("pk_test_coverage_pct", 0)
    violations = report.get("naming_violation_count", 0)
    model_count = report.get("model_count", 0)
    checks = [
        ("Model descriptions", desc >= 80, f"{desc}%"),
        ("Column descriptions", col >= 80, f"{col}%"),
        ("PK test coverage", pk >= 80, f"{pk}%"),
        ("Naming conventions", violations == 0, f"{violations} violation(s)"),
    ]
    computed_passed = all(passed for _, passed, _ in checks)
    # Trust the report's own `passed` field when present (scorecard.py writes it);
    # fall back to the computed value for backward compat.
    section_passed = bool(report.get("passed", computed_passed))
    table = "| Check | Status | Result |\n|-------|--------|--------|\n"
    for check, passed, result in checks:
        table += f"| {check} | {icon(passed)} | {result} |\n"
    section = f"#### dbt Scorecard\n\n_{model_count} model(s) analysed_\n\n{table}"
    violations_list = report.get("naming_violations", [])
    if violations_list:
        section += _format_naming_violations_table(violations_list)
    return section_passed, section


def format_gate_0(compile_result: dict, build_empty_result: dict, schema_gate: dict) -> tuple[bool, str]:
    """Returns (gate_0_passed, sub_section_markdown)."""
    def _check_ok(report: dict) -> bool | None:
        if not report:
            return None
        return bool(report.get("passed"))

    compile_ok = _check_ok(compile_result)
    build_empty_ok = _check_ok(build_empty_result)
    sg_ok = _check_ok(schema_gate)
    gate_passed = all(v is not False for v in [compile_ok, build_empty_ok, sg_ok])

    def _item(report: dict, label: str) -> str:
        if not report:
            return f"| {label} | ⚠️ Unavailable |\n"
        return f"| {label} | {icon(bool(report.get('passed')))} |\n"

    violations = schema_gate.get("violations", [])
    evaluated = schema_gate.get("models_evaluated", 0)
    if not schema_gate:
        sg_cell = "⚠️ Unavailable"
    elif sg_ok:
        sg_cell = f"✅ {evaluated} model(s) evaluated"
    else:
        sg_cell = f"❌ {len(violations)} violation(s)"

    table = (
        "| Check | Result |\n|-------|--------|\n"
        + _item(compile_result, "dbt compile")
        + _item(build_empty_result, "dbt build --empty")
        + f"| Schema gate | {sg_cell} |\n"
    )

    overall_icon = icon(gate_passed)
    section = f"### Gate 0 — Static Analysis {overall_icon}\n\n{table}"

    if violations:
        lines = "\n".join(
            f"- `{v['model']}` (`{v['path']}`): {', '.join(v['issues'])}"
            for v in violations
        )
        section += (
            f"\n<details>\n<summary>Schema violations</summary>\n\n"
            f"{lines}\n\n</details>\n"
        )

    return gate_passed, section


def build_comment(workspace_id: str, workspace_name: str, head_branch: str) -> str:
    """Workspace notification comment — no static analysis section."""
    ws_url = FABRIC_WORKSPACE_URL.format(workspace_id=workspace_id)
    return f"""{COMMENT_MARKER}
## Ephemeral Workspace Ready

**Workspace:** [{workspace_name}]({ws_url})
**Branch:** `{head_branch}`

### Developer Checklist
- [ ] Open the workspace and run the notebook cells in order:
  - **Cell: Clone** — `dbt clone --select state:modified+` *(resets D and D+ to prod state)*
  - **Cell: Build** — `dbt build --select state:modified+ --defer`
  - **Cell: Test** — `dbt test --select state:modified+ --store-failures`
- [ ] Review any dbt test failures in the workspace
- [ ] Validate results meet acceptance criteria from intent spec
- [ ] Mark PR ready for review

> CI reports available as workflow artifacts.
"""


def _detail_ruff(report) -> str:
    issues = report if isinstance(report, list) else []
    if not issues:
        return "No issues"
    rule_counts = Counter(item.get("code", "unknown") for item in issues)
    top = ", ".join(f"`{r}` ×{n}" for r, n in sorted(rule_counts.items())[:3])
    suffix = f" (+{len(rule_counts) - 3} more rules)" if len(rule_counts) > 3 else ""
    return f"{len(issues)} issue(s) — {top}{suffix}"


def _detail_sqlfluff(report) -> str:
    file_results = report if isinstance(report, list) else report.get("files", [])
    total = sum(len(f.get("violations", [])) for f in file_results)
    files = sum(1 for f in file_results if f.get("violations"))
    if total == 0:
        return "No violations"
    return f"{total} violation(s) across {files} file(s)"


def _detail_gitleaks(report) -> str:
    findings = report if isinstance(report, list) else report.get("findings", [])
    if not findings:
        return "No secrets found"
    return f"{len(findings)} secret(s) found — merge blocked"


def _detail_scorecard(report: dict) -> str:
    if not report:
        return "⚠️ Unavailable"
    if report.get("passed"):
        return "All thresholds met"
    desc = report.get("description_coverage_pct", 0)
    col = report.get("column_coverage_pct", 0)
    pk = report.get("pk_test_coverage_pct", 0)
    violations = report.get("naming_violation_count", 0)
    parts = []
    if desc < 80:
        parts.append(f"Desc {desc}%")
    if col < 80:
        parts.append(f"Col {col}%")
    if pk < 80:
        parts.append(f"PK {pk}%")
    if violations > 0:
        parts.append(f"{violations} naming violations")
    return " · ".join(parts) + " (need ≥80% / 0)" if parts else "Failed"


def _detail_compile(report: dict | None) -> str:
    if not report:
        return "⚠️ Unavailable"
    if report.get("passed"):
        return "Compiled successfully"
    errors = report.get("errors", [])
    if errors:
        return f"Compilation failed — `{errors[0].get('model', 'unknown')}`"
    return "Compilation failed"


def _detail_build_empty(report: dict | None) -> str:
    if not report:
        return "⚠️ Unavailable"
    if report.get("passed"):
        return "Built successfully"
    errors = report.get("errors", [])
    if errors:
        return f"Build failed — `{errors[0].get('model', 'unknown')}`"
    return "Build failed"


def _collapsible_dbt_errors(name: str, errors: list) -> str:
    count = len(errors)
    lines = []
    for err in errors[:10]:
        model = err.get("model", "unknown")
        msg = (err.get("message") or "")[:200]
        lines.append(f"- `{model}` — {msg}" if msg else f"- `{model}`")
    if count > 10:
        lines.append(f"- … and {count - 10} more")
    return (
        f"<details>\n<summary>{name} — failing models</summary>\n\n"
        + "\n".join(lines)
        + "\n\n</details>\n"
    )


def _collapsible_compile(report: dict | None) -> str:
    if not report:
        return ""
    errors = report.get("errors", [])
    return _collapsible_dbt_errors("dbt compile", errors) if errors else ""


def _collapsible_build_empty(report: dict | None) -> str:
    if not report:
        return ""
    errors = report.get("errors", [])
    return _collapsible_dbt_errors("dbt build --empty", errors) if errors else ""


def _detail_schema_gate(report: dict | None) -> str:
    if not report:
        return "⚠️ Unavailable"
    evaluated = report.get("models_evaluated", 0)
    violations = report.get("violations", [])
    if report.get("passed"):
        return f"{evaluated} model(s) evaluated — all pass" if evaluated else "0 changed models"
    return f"{len(violations)} violation(s) across {evaluated} model(s)"


def _collapsible_ruff(report) -> str:
    issues = report if isinstance(report, list) else []
    if not issues:
        return ""
    rule_counts = Counter(item.get("code", "unknown") for item in issues)
    rows = "\n".join(f"| `{r}` | {n} |" for r, n in sorted(rule_counts.items()))
    return (
        f"<details>\n<summary>ruff — per-rule breakdown</summary>\n\n"
        f"| Rule | Count |\n|------|-------|\n{rows}\n\n</details>\n"
    )


def _collapsible_sqlfluff(report) -> str:
    file_results = report if isinstance(report, list) else report.get("files", [])
    file_counts = {
        f.get("filepath", "unknown"): len(f.get("violations", []))
        for f in file_results if f.get("violations")
    }
    if not file_counts:
        return ""
    rows = "\n".join(f"| `{fp}` | {n} |" for fp, n in sorted(file_counts.items()))
    return (
        f"<details>\n<summary>sqlfluff — per-file breakdown</summary>\n\n"
        f"| File | Violations |\n|------|------------|\n{rows}\n\n</details>\n"
    )


def _collapsible_gitleaks(report) -> str:
    findings = report if isinstance(report, list) else report.get("findings", [])
    if not findings:
        return ""
    lines = []
    for f in findings:
        secret_type = f.get("RuleID") or f.get("Description", "unknown")
        lines.append(f"| `{secret_type}` | `{f.get('File', 'unknown')}` | {f.get('StartLine', '?')} |")
    rows = "\n".join(lines)
    return (
        f"<details>\n<summary>gitleaks — findings (no secret values shown)</summary>\n\n"
        f"| Type | File | Line |\n|------|------|------|\n{rows}\n\n</details>\n"
    )


def _collapsible_scorecard(report: dict) -> str:
    if not report or report.get("passed"):
        return ""
    desc = report.get("description_coverage_pct", 0)
    col = report.get("column_coverage_pct", 0)
    pk = report.get("pk_test_coverage_pct", 0)
    violations = report.get("naming_violation_count", 0)
    model_count = report.get("model_count", 0)
    checks = [
        ("Model descriptions", desc >= 80, f"{desc}% (need ≥80%)"),
        ("Column descriptions", col >= 80, f"{col}% (need ≥80%)"),
        ("PK test coverage", pk >= 80, f"{pk}% (need ≥80%)"),
        ("Naming conventions", violations == 0, f"{violations} violations (need 0)"),
    ]
    rows = "\n".join(f"| {c} | {icon(p)} | {v} |" for c, p, v in checks)
    return (
        f"<details>\n<summary>dbt Scorecard — {model_count} model(s) analysed</summary>\n\n"
        f"| Metric | Status | Value |\n|--------|--------|-------|\n{rows}\n\n</details>\n"
    )


def _collapsible_schema_gate(report: dict | None) -> str:
    if not report:
        return ""
    violations = report.get("violations", [])
    if not violations:
        return ""
    lines = "\n".join(
        f"- `{v['model']}` (`{v['path']}`): {', '.join(v['issues'])}"
        for v in violations
    )
    return (
        f"<details>\n<summary>schema gate — violations</summary>\n\n"
        f"{lines}\n\n</details>\n"
    )


def build_details_comment(
    ruff: list,
    sqlfluff: dict,
    gitleaks: dict | list,
    scorecard: dict,
    compile_result: dict | None = None,
    build_empty_result: dict | None = None,
    schema_gate: dict | None = None,
) -> str:
    """Gate 0 — Static Analysis comment: unified table + collapsible drill-downs."""
    ruff_passed, _ = format_ruff(ruff)
    sql_passed, _ = format_sqlfluff(sqlfluff)
    gl_passed, _ = format_gitleaks(gitleaks)
    sc_passed, _ = format_scorecard_section(scorecard)

    has_gate_0 = bool(compile_result or build_empty_result or schema_gate)
    gate_0_passed = True
    if has_gate_0:
        gate_0_passed, _ = format_gate_0(
            compile_result or {}, build_empty_result or {}, schema_gate or {}
        )

    overall_passed = ruff_passed and sql_passed and gl_passed and sc_passed and gate_0_passed
    overall_icon = icon(overall_passed)

    def _status(report: dict | None) -> str:
        if not report:
            return "⚠️"
        return icon(bool(report.get("passed")))

    table = "| Check | Status | Detail |\n|-------|--------|--------|\n"
    if has_gate_0:
        table += (
            f"| dbt compile | {_status(compile_result)} | {_detail_compile(compile_result)} |\n"
            f"| dbt build --empty | {_status(build_empty_result)} | {_detail_build_empty(build_empty_result)} |\n"
            f"| Schema gate | {_status(schema_gate)} | {_detail_schema_gate(schema_gate)} |\n"
        )
    table += (
        f"| ruff | {icon(ruff_passed)} | {_detail_ruff(ruff)} |\n"
        f"| sqlfluff | {icon(sql_passed)} | {_detail_sqlfluff(sqlfluff)} |\n"
        f"| gitleaks | {icon(gl_passed)} | {_detail_gitleaks(gitleaks)} |\n"
        f"| dbt Scorecard | {icon(sc_passed)} | {_detail_scorecard(scorecard)} |\n"
    )

    parts = [
        f"{DETAILS_COMMENT_MARKER}\n"
        f"## Gate 0 — Static Analysis {overall_icon}\n\n"
        f"{table}\n"
    ]

    for collapsible in [
        _collapsible_compile(compile_result) if has_gate_0 else "",
        _collapsible_build_empty(build_empty_result) if has_gate_0 else "",
        _collapsible_ruff(ruff),
        _collapsible_sqlfluff(sqlfluff),
        _collapsible_gitleaks(gitleaks),
        _collapsible_scorecard(scorecard),
        _collapsible_schema_gate(schema_gate) if has_gate_0 else "",
    ]:
        if collapsible:
            parts.append(collapsible + "\n")

    return "".join(parts)


def _find_comment_by_marker(marker: str, pr_number: str, repo: str) -> str | None:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{pr_number}/comments",
         "--jq", f'.[] | select(.body | contains("{marker}")) | .id'],
        capture_output=True, text=True,
    )
    comment_id = result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
    return comment_id


def _post_or_update_comment(body: str, comment_id: str | None, pr_number: str, repo: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
        tmp.write(body)
        tmp_path = tmp.name
    try:
        if comment_id:
            result = subprocess.run(
                ["gh", "api", "--method", "PATCH",
                 f"repos/{repo}/issues/comments/{comment_id}",
                 "--field", f"body=@{tmp_path}"],
                capture_output=True, text=True,
            )
        else:
            result = subprocess.run(
                ["gh", "pr", "comment", pr_number,
                 "--repo", repo,
                 "--body-file", tmp_path],
                capture_output=True, text=True,
            )
        if result.returncode != 0:
            print(f"Failed to post PR comment: {result.stderr}", file=sys.stderr)
            sys.exit(1)
    finally:
        os.unlink(tmp_path)


def main():
    workspace_id = os.environ.get("EPHEMERAL_WORKSPACE_ID", "")
    workspace_name = os.environ.get("EPHEMERAL_WORKSPACE_NAME", "")
    head_branch = os.environ.get("HEAD_BRANCH", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    repo = os.environ.get("REPO", "")

    ruff = load_report("reports/ruff.json")
    sqlfluff_report = load_report("reports/sqlfluff.json")
    gitleaks = load_report("reports/gitleaks.json")
    scorecard = load_report("reports/scorecard.json")
    compile_result = load_report("reports/dbt_compile.json")
    build_empty_result = load_report("reports/dbt_build_empty.json")
    schema_gate = load_report("reports/schema_gate.json")

    workspace_comment = build_comment(workspace_id, workspace_name, head_branch)
    details_comment = build_details_comment(
        ruff=ruff,
        sqlfluff=sqlfluff_report,
        gitleaks=gitleaks,
        scorecard=scorecard,
        compile_result=compile_result,
        build_empty_result=build_empty_result,
        schema_gate=schema_gate,
    )

    workspace_comment_id = _find_comment_by_marker(COMMENT_MARKER, pr_number, repo)
    _post_or_update_comment(workspace_comment, workspace_comment_id, pr_number, repo)
    print("Workspace PR comment posted.", flush=True)

    details_comment_id = _find_comment_by_marker(DETAILS_COMMENT_MARKER, pr_number, repo)
    _post_or_update_comment(details_comment, details_comment_id, pr_number, repo)
    print("Static analysis details PR comment posted.", flush=True)


if __name__ == "__main__":
    main()
