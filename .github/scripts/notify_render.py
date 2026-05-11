"""Pure renderer module for PR comment markdown.

Owns all formatting logic. No I/O, no subprocess calls.

Public surface:
    ReportBundle                     — named slots for all gate reports
    render_workspace_comment(...)    — ephemeral workspace notification
    render_details_comment(bundle)   — gate 0 static analysis + gate sections
    render_ruff(report)              — (passed, markdown)
    render_sqlfluff(report)          — (passed, markdown)
    render_gitleaks(report)          — (passed, markdown)
    render_scorecard(report)         — (passed, markdown)
    render_gate_0(compile, build_empty, schema_gate) — (passed, markdown)
    render_gate_3(summary)           — (passed, markdown)
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


FABRIC_WORKSPACE_URL = "https://app.fabric.microsoft.com/groups/{workspace_id}/list?experience=fabric-developer"
COMMENT_MARKER = "<!-- ephemeral-workspace-ready -->"
DETAILS_COMMENT_MARKER = "<!-- static-analysis-details -->"

_VIOLATION_CAP = 20
_GATE4_FAILING_CAP = 10


@dataclass
class ReportBundle:
    ruff: Any = field(default_factory=list)
    sqlfluff: Any = field(default_factory=dict)
    gitleaks: Any = field(default_factory=dict)
    scorecard: Any = field(default_factory=dict)
    compile_result: Any = None
    build_empty_result: Any = None
    schema_gate: Any = None
    shortcut_seeding: Any = None
    gate_2: Any = None
    gate_3: Any = None
    gate_4: Any = None


def _icon(passed: bool) -> str:
    return "✅" if passed else "❌"


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


# ─── per-tool renderers ───────────────────────────────────────────────────────

def render_ruff(report) -> tuple[bool, str]:
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


def render_sqlfluff(report) -> tuple[bool, str]:
    if report is None:
        return True, "#### SQLFluff\n\n✅ No violations\n"
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


def render_gitleaks(report) -> tuple[bool, str]:
    if report is None:
        return True, "#### Gitleaks\n\n✅ No secrets found\n"
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


def render_scorecard(report) -> tuple[bool, str]:
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
    section_passed = bool(report.get("passed", computed_passed))
    table = "| Check | Status | Result |\n|-------|--------|--------|\n"
    for check, passed, result in checks:
        table += f"| {check} | {_icon(passed)} | {result} |\n"
    section = f"#### dbt Scorecard\n\n_{model_count} model(s) analysed_\n\n{table}"
    violations_list = report.get("naming_violations", [])
    if violations_list:
        section += _format_naming_violations_table(violations_list)
    return section_passed, section


def render_gate_0(compile_result: dict, build_empty_result: dict, schema_gate: dict) -> tuple[bool, str]:
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
        return f"| {label} | {_icon(bool(report.get('passed')))} |\n"

    if not schema_gate:
        sg_cell = "⚠️ Unavailable"
        violations = []
        evaluated = 0
    elif sg_ok:
        violations = schema_gate.get("violations", [])
        evaluated = schema_gate.get("models_evaluated", 0)
        sg_cell = f"✅ {evaluated} model(s) evaluated" if evaluated else "✅ No changed models in scope"
    else:
        violations = schema_gate.get("violations", [])
        evaluated = schema_gate.get("models_evaluated", 0)
        sg_cell = f"❌ {len(violations)} violation(s)"

    table = (
        "| Check | Result |\n|-------|--------|\n"
        + _item(compile_result, "dbt compile")
        + _item(build_empty_result, "dbt build --empty")
        + f"| Schema gate | {sg_cell} |\n"
    )

    overall_icon = _icon(gate_passed)
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


def render_gate_3(summary: dict) -> tuple[bool, str]:
    counts = summary.get("counts") or {"pass": 0, "fail": 0, "error": 0, "skip": 0}
    p = counts.get("pass", 0)
    f = counts.get("fail", 0)
    e = counts.get("error", 0)
    s = counts.get("skip", 0)
    overall = summary.get("overall_status", "pass")
    passed = overall == "pass"

    summary_line = f"{p} passed / {f} failed / {e} errored / {s} skipped"
    head = f"## Gate 3 — Unit Tests {_icon(passed)}\n\n{summary_line}\n"

    failures = summary.get("failures") or []
    if not failures:
        return passed, head + "\n"

    def _first_line(msg: str) -> str:
        lines = (msg or "").splitlines()
        return lines[0][:200] if lines else ""

    rows = "\n".join(
        f"| `{x.get('name', '')}` | {x.get('status', '')} | {_first_line(x.get('message') or '')} |"
        for x in failures
    )
    tail = ""
    if summary.get("truncated") or (f + e) > len(failures):
        tail = f"\n\n_Showing top {len(failures)} of {f + e} failing tests._"

    section = (
        head
        + "\n<details>\n<summary>Failing tests</summary>\n\n"
        + "| Test | Status | Message |\n|------|--------|---------|\n"
        + rows
        + tail
        + "\n\n</details>\n"
    )
    return passed, section


# ─── section renderers (return str, no passed flag) ──────────────────────────

def _render_shortcut_seeding(report: dict | None) -> str:
    _ZERO_STATE_COPY = {
        "greenfield": "🔗 No shortcuts derived — greenfield mode (no prod manifest available)",
        "no-modified-models": "🔗 No shortcuts derived — PR has no modified dbt models",
        "no-upstreams": "🔗 No shortcuts derived — modified models have no prod upstreams",
    }
    if not report:
        return ""
    derived = report.get("derived") or []
    zero_state = report.get("zero_state")
    if derived:
        aliases = "\n".join(entry.get("alias", "") for entry in derived)
        created = report.get("created")
        already = report.get("already_existed")
        counts = ""
        if created is not None or already is not None:
            counts = f" ({created or 0} created, {already or 0} already existed)"
        return (
            f"### 🔗 Seeded {len(derived)} shortcuts from prod{counts}\n\n"
            f"<details>\n<summary>Shortcut aliases</summary>\n\n"
            f"{aliases}\n\n"
            f"</details>\n"
        )
    copy = _ZERO_STATE_COPY.get(zero_state)
    if copy:
        return f"### {copy}\n"
    return ""


def render_gate_2(result: dict | None) -> str:
    if not result:
        return ""
    overall = result.get("overall_status", "")
    passed = overall == "pass"
    models = result.get("models") or []
    head_sha = result.get("head_sha", "")
    passed_count = sum(1 for m in models if m.get("status") in ("success", "pass"))
    failed_count = len(models) - passed_count
    summary_line = f"{passed_count} passed / {failed_count} failed"
    if head_sha:
        summary_line += f" · `{head_sha[:7]}`"
    head = f"## Gate 2 — Write {_icon(passed)}\n\n{summary_line}\n"
    failures = [m for m in models if m.get("status") not in ("success", "pass")]
    if not failures:
        return head + "\n"
    rows = "\n".join(
        f"| `{m.get('name', '')}` | {m.get('status', '')} | {(m.get('error_message') or '')[:200]} |"
        for m in failures[:10]
    )
    tail = f"\n\n_Showing top {len(failures[:10])} of {failed_count} failing models._" if failed_count > 10 else ""
    return (
        head
        + "\n<details>\n<summary>Failing models</summary>\n\n"
        + "| Model | Status | Error |\n|-------|--------|-------|\n"
        + rows
        + tail
        + "\n\n</details>\n"
    )


def render_gate_4(result: dict | None) -> str:
    if not result:
        return ""
    overall = result.get("overall_status", "")
    passed = overall == "pass"
    tests = result.get("tests") or []
    store_failures_config_ok = result.get("store_failures_config_ok", True)
    passing = [t for t in tests if t.get("status") in ("pass",)]
    failing = [t for t in tests if t.get("status") in ("fail", "error")]
    skipped = [t for t in tests if t.get("status") == "skip"]
    summary_line = f"{len(passing)} passed / {len(failing)} failed / {len(skipped)} skipped"
    head = f"## Gate 4 — Data Tests {_icon(passed)}\n\n{summary_line}\n"
    advisory = ""
    if not store_failures_config_ok:
        advisory = (
            "\n> ⚠️ **Advisory:** `dbt_project.yml` is missing `tests: +store_failures: true` "
            "and/or `+store_failures_as: table`. Failure drill-down tables will not be available. "
            "Gate signal is unaffected.\n"
        )
    if not failing:
        return head + advisory + "\n"
    sorted_failing = sorted(failing, key=lambda t: t.get("name", ""))
    shown = sorted_failing[:_GATE4_FAILING_CAP]
    remainder = len(sorted_failing) - len(shown)
    rows = []
    for t in shown:
        name = t.get("name", "")
        model = t.get("model", "")
        status = t.get("status", "")
        failures_count = t.get("failures_count", "")
        msg = (t.get("message") or "")[:200]
        store_table = t.get("store_failures_table") or ""
        if store_failures_config_ok and store_table:
            name_cell = f"`{name}` → `{store_table}`"
        else:
            name_cell = f"`{name}`"
        rows.append(f"| {name_cell} | `{model}` | {status} | {failures_count} | {msg} |")
    tail = f"\n\n_…and {remainder} more_" if remainder else ""
    return (
        head
        + advisory
        + "\n<details>\n<summary>Failing tests</summary>\n\n"
        + "| Test | Model | Status | Failures | Message |\n"
        + "|------|-------|--------|----------|---------|\n"
        + "\n".join(rows)
        + tail
        + "\n\n</details>\n"
    )


# ─── detail-column helpers (used in summary table) ───────────────────────────

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


def _detail_schema_gate(report: dict | None) -> str:
    if not report:
        return "⚠️ Unavailable"
    evaluated = report.get("models_evaluated", 0)
    violations = report.get("violations", [])
    if report.get("passed"):
        return f"{evaluated} model(s) evaluated — all pass" if evaluated else "0 changed models"
    return f"{len(violations)} violation(s) across {evaluated} model(s)"


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
    rows = "\n".join(f"| {c} | {_icon(p)} | {v} |" for c, p, v in checks)
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


# ─── public composers ────────────────────────────────────────────────────────

def render_workspace_comment(
    workspace_id: str,
    workspace_name: str,
    head_branch: str,
    greenfield_fallback: bool = False,
) -> str:
    ws_url = FABRIC_WORKSPACE_URL.format(workspace_id=workspace_id)
    greenfield_notice = ""
    if greenfield_fallback:
        greenfield_notice = (
            "\n> ⚠️ **No prod manifest available** — Slim CI is falling back to full build. "
            "Once a CD workflow publishes a `prod-manifest` artifact from `main`, "
            "Slim CI will use it automatically.\n"
        )
    return f"""{COMMENT_MARKER}
## Ephemeral Workspace Ready

**Workspace:** [{workspace_name}]({ws_url})
**Branch:** `{head_branch}`
{greenfield_notice}
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


GATE_0_MARKER = "<!-- ci-gate-0 -->"
GATE_1_MARKER = "<!-- ci-gate-1 -->"
GATE_2_MARKER = "<!-- ci-gate-2 -->"
GATE_3_MARKER = "<!-- ci-gate-3 -->"
GATE_4_MARKER = "<!-- ci-gate-4 -->"


def render_gate_0_comment(
    compile_result,
    build_empty_result,
    schema_gate,
    *,
    ruff=None,
    sqlfluff=None,
    gitleaks=None,
    scorecard=None,
    shortcut_seeding=None,
) -> str:
    has_gate_0 = bool(compile_result or build_empty_result or schema_gate)
    has_tools = any(x is not None for x in [ruff, sqlfluff, gitleaks, scorecard, shortcut_seeding])

    if not has_gate_0 and not has_tools:
        return f"{GATE_0_MARKER}\n## Gate 0 — Static Analysis ⚠️\n\n_No data available._\n"

    _, section = render_gate_0(
        compile_result or {},
        build_empty_result or {},
        schema_gate or {},
    )

    parts = [f"{GATE_0_MARKER}\n{section}"]

    for value, renderer in [
        (ruff, render_ruff),
        (sqlfluff, render_sqlfluff),
        (gitleaks, render_gitleaks),
        (scorecard, render_scorecard),
    ]:
        if value is not None:
            _, tool_section = renderer(value)
            parts.append(tool_section)

    shortcut_section = _render_shortcut_seeding(shortcut_seeding)
    if shortcut_section:
        parts.append(shortcut_section)

    return "\n".join(parts)


def render_gate_1_comment(greenfield: bool) -> str:
    if greenfield:
        detail = "Running **greenfield** build (no prod manifest available — full build)."
    else:
        detail = "Running **incremental** (slim CI) build using prod manifest."
    return f"{GATE_1_MARKER}\n## Gate 1 — Manifest ✅\n\n{detail}\n"


def render_gate_2_comment(result) -> str:
    if not result:
        return f"{GATE_2_MARKER}\n## Gate 2 — Write ⚠️\n\n_No data available._\n"
    section = render_gate_2(result)
    return f"{GATE_2_MARKER}\n{section}"


def render_gate_3_comment(result) -> str:
    if not result:
        return f"{GATE_3_MARKER}\n## Gate 3 — Unit Tests ⚠️\n\n_No data available._\n"
    _, section = render_gate_3(result)
    return f"{GATE_3_MARKER}\n{section}"


def render_gate_4_comment(result) -> str:
    if not result:
        return f"{GATE_4_MARKER}\n## Gate 4 — Data Tests ⚠️\n\n_No data available._\n"
    section = render_gate_4(result)
    return f"{GATE_4_MARKER}\n{section}"


def render_details_comment(bundle: ReportBundle) -> str:
    ruff = bundle.ruff if bundle.ruff is not None else []
    sqlfluff = bundle.sqlfluff if bundle.sqlfluff is not None else {}
    gitleaks = bundle.gitleaks if bundle.gitleaks is not None else {}
    scorecard = bundle.scorecard if bundle.scorecard is not None else {}

    ruff_passed, _ = render_ruff(ruff)
    sql_passed, _ = render_sqlfluff(sqlfluff)
    gl_passed, _ = render_gitleaks(gitleaks)
    sc_passed, _ = render_scorecard(scorecard)

    has_gate_0 = bool(bundle.compile_result or bundle.build_empty_result or bundle.schema_gate)
    gate_0_passed = True
    if has_gate_0:
        gate_0_passed, _ = render_gate_0(
            bundle.compile_result or {},
            bundle.build_empty_result or {},
            bundle.schema_gate or {},
        )

    overall_passed = ruff_passed and sql_passed and gl_passed and sc_passed and gate_0_passed
    overall_icon = _icon(overall_passed)

    def _status(report) -> str:
        if not report:
            return "⚠️"
        return _icon(bool(report.get("passed")))

    table = "| Check | Status | Detail |\n|-------|--------|--------|\n"
    if has_gate_0:
        table += (
            f"| dbt compile | {_status(bundle.compile_result)} | {_detail_compile(bundle.compile_result)} |\n"
            f"| dbt build --empty | {_status(bundle.build_empty_result)} | {_detail_build_empty(bundle.build_empty_result)} |\n"
            f"| Schema gate | {_status(bundle.schema_gate)} | {_detail_schema_gate(bundle.schema_gate)} |\n"
        )
    table += (
        f"| ruff | {_icon(ruff_passed)} | {_detail_ruff(ruff)} |\n"
        f"| sqlfluff | {_icon(sql_passed)} | {_detail_sqlfluff(sqlfluff)} |\n"
        f"| gitleaks | {_icon(gl_passed)} | {_detail_gitleaks(gitleaks)} |\n"
        f"| dbt Scorecard | {_icon(sc_passed)} | {_detail_scorecard(scorecard)} |\n"
    )

    parts = [
        f"{DETAILS_COMMENT_MARKER}\n"
        f"## Gate 0 — Static Analysis {overall_icon}\n\n"
        f"{table}\n"
    ]

    for collapsible in [
        _collapsible_compile(bundle.compile_result) if has_gate_0 else "",
        _collapsible_build_empty(bundle.build_empty_result) if has_gate_0 else "",
        _collapsible_ruff(ruff),
        _collapsible_sqlfluff(sqlfluff),
        _collapsible_gitleaks(gitleaks),
        _collapsible_scorecard(scorecard),
        _collapsible_schema_gate(bundle.schema_gate) if has_gate_0 else "",
    ]:
        if collapsible:
            parts.append(collapsible + "\n")

    shortcut_section = _render_shortcut_seeding(bundle.shortcut_seeding)
    if shortcut_section:
        parts.append(shortcut_section + "\n")

    gate_2_section = render_gate_2(bundle.gate_2)
    if gate_2_section:
        parts.append(gate_2_section + "\n")

    if bundle.gate_3 is not None:
        _, gate_3_section = render_gate_3(bundle.gate_3)
        parts.append(gate_3_section + "\n")

    gate_4_section = render_gate_4(bundle.gate_4)
    if gate_4_section:
        parts.append(gate_4_section + "\n")

    return "".join(parts)
