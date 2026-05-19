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
    render_gate_0(compile, schema_gate) — (passed, markdown)
    render_gate_3(summary)           — (passed, markdown)
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

_RUNNER_PREFIX_RE = re.compile(r"^/home/runner/work/[^/]+/[^/]+/")


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
    schema_gate: Any = None
    shortcut_seeding: Any = None
    gate_2: Any = None
    gate_3: Any = None
    gate_4: Any = None


def _strip_runner_prefix(path: str) -> str:
    return _RUNNER_PREFIX_RE.sub("", path)


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
    sorted_issues = sorted(issues, key=lambda x: x.get("filename", ""))
    lines = [
        f"- `{_strip_runner_prefix(item.get('filename', 'unknown'))}` — `{item.get('code', 'unknown')}` {item.get('message', '')}"
        for item in sorted_issues[:_VIOLATION_CAP]
    ]
    remainder = count - len(lines)
    detail = "\n".join(lines)
    if remainder:
        detail += f"\n\n_…and {remainder} more_"
    section = (
        f"#### Ruff\n\n"
        f"❌ {count} issue(s)\n\n"
        f"<details>\n<summary>Violations</summary>\n\n"
        f"{detail}\n\n"
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


def render_gate_0(compile_result: dict, schema_gate: dict, *, extra_rows: str = "") -> tuple[bool, str]:
    def _check_ok(report: dict) -> bool | None:
        if not report:
            return None
        return bool(report.get("passed"))

    compile_ok = _check_ok(compile_result)
    sg_ok = _check_ok(schema_gate)
    gate_passed = all(v is not False for v in [compile_ok, sg_ok])

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

    def _cell(report: dict, detail_fn) -> str:
        if not report:
            return "⚠️ Unavailable"
        return f"{_icon(bool(report.get('passed')))} {detail_fn(report)}"

    table = (
        "| Check | Result |\n|-------|--------|\n"
        + f"| dbt compile | {_cell(compile_result, _detail_compile)} |\n"
        + f"| Schema gate | {sg_cell} |\n"
        + extra_rows
    )

    overall_icon = _icon(gate_passed)
    section = f"## Gate 0 — Static Analysis {overall_icon}\n\n{table}"

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
    head_sha = result.get("head_sha", "")

    clone = result.get("clone") or {}
    build = result.get("build") or {}
    clone_models = clone.get("models") or []
    build_models = build.get("models") or []
    clone_status = clone.get("status", "fail")
    build_status = build.get("status", "fail")

    clone_icon = _icon(clone_status == "pass")
    build_icon = _icon(build_status == "pass")

    parts = [
        f"Clone: {len(clone_models)} {clone_icon}",
        f"Build: {len(build_models)} {build_icon}",
    ]
    if head_sha:
        parts.append(f"`{head_sha[:7]}`")
    summary_line = " · ".join(parts)

    head = f"## Gate 2 — Write {_icon(passed)}\n\n{summary_line}\n"

    sections = [head]
    for step_label, step_status, step_models in [
        ("Clone", clone_status, clone_models),
        ("Build", build_status, build_models),
    ]:
        failures = [m for m in step_models if m.get("status") not in ("success", "pass")]
        if step_status == "fail" and failures:
            rows = "\n".join(
                f"| `{m.get('name', '')}` | {m.get('status', '')} | {(m.get('error_message') or '')[:200]} |"
                for m in failures[:10]
            )
            tail = (
                f"\n\n_Showing top {min(10, len(failures))} of {len(failures)} failing models._"
                if len(failures) > 10
                else ""
            )
            sections.append(
                f"\n<details>\n<summary>{step_label} — failing models</summary>\n\n"
                "| Model | Status | Error |\n|-------|--------|-------|\n"
                + rows
                + tail
                + "\n\n</details>\n"
            )

    return "".join(sections)


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


def render_gate_5(result: dict | None) -> str:
    if not result:
        return ""
    overall = result.get("overall_status", "")
    passed = overall == "pass"
    artifacts = result.get("artifacts") or []

    brand_new = [a for a in artifacts if a.get("baseline") is None]
    existing = [a for a in artifacts if a.get("baseline") is not None]

    head = f"## Gate 5 — Data-Diff vs Prod {_icon(passed)}\n\n"

    if not artifacts:
        return head + "_No artifacts in diff scope._\n"

    n_diff = sum(
        1 for a in existing
        if _has_schema_diff(a.get("schema_delta") or {})
        or (a.get("row_count_delta") or {}).get("delta", 0) != 0
        or (
            (a.get("value_delta") or {}).get("rows_with_diffs", 0) > 0
            and not (a.get("value_delta") or {}).get("skipped_no_unique_key", False)
        )
    )
    summary_parts = []
    if brand_new:
        summary_parts.append(f"{len(brand_new)} brand-new")
    if existing:
        summary_parts.append(f"{len(existing)} compared")
    if n_diff:
        summary_parts.append(f"{n_diff} with diff")
    head += " · ".join(summary_parts) + "\n"

    sections = [head]

    if brand_new:
        rows = "\n".join(
            f"| `{a['name']}` | `{a.get('materialized', '')}` |"
            for a in brand_new
        )
        sections.append(
            "\n<details>\n<summary>Brand-new artifacts (auto-pass)</summary>\n\n"
            "| Artifact | Materialization |\n"
            "|----------|-----------------|\n"
            + rows
            + "\n\n</details>\n"
        )

    if existing:
        rows = "\n".join(
            f"| `{a['name']}` | `{a.get('materialized', '')}` "
            f"| {_render_schema_delta_cell(a.get('schema_delta') or {})} "
            f"| {(a.get('row_count_delta') or {}).get('prod', 0)} → {(a.get('row_count_delta') or {}).get('pr', 0)} ({(a.get('row_count_delta') or {}).get('delta', 0):+}) "
            f"| {_render_value_delta_cell(a.get('value_delta'))} |"
            for a in existing
        )
        sections.append(
            "\n| Artifact | Materialization | Schema delta | Row-count delta | Value delta |\n"
            "|----------|-----------------|--------------|-----------------|-------------|\n"
            + rows
            + "\n"
        )

        schema_details = []
        for a in existing:
            sd = a.get("schema_delta") or {}
            if not _has_schema_diff(sd):
                continue
            lines = []
            for col in sd.get("added") or []:
                lines.append(f"- `{col}` added")
            for col in sd.get("removed") or []:
                lines.append(f"- `{col}` removed")
            for item in sd.get("renamed") or []:
                if isinstance(item, dict):
                    lines.append(f"- `{item.get('from', '?')}` renamed → `{item.get('to', '?')}`")
                else:
                    lines.append(f"- `{item}` renamed")
            for item in sd.get("type_changed") or []:
                lines.append(
                    f"- `{item['column']}` type: `{item['prod_dtype']}` → `{item['ci_dtype']}`"
                )
            for item in sd.get("nullability_flipped") or []:
                lines.append(
                    f"- `{item['column']}` nullability: `{item['prod_nullable']}` → `{item['ci_nullable']}`"
                )
            if lines:
                schema_details.append(f"**`{a['name']}`**\n" + "\n".join(lines))

        if schema_details:
            sections.append(
                "\n<details>\n<summary>Schema delta details</summary>\n\n"
                + "\n\n".join(schema_details)
                + "\n\n</details>\n"
            )

    return "\n".join(sections)


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


def _has_schema_diff(schema_delta: dict) -> bool:
    return any(
        schema_delta.get(k)
        for k in ("added", "removed", "renamed", "type_changed", "nullability_flipped")
    )


def _render_schema_delta_cell(schema_delta: dict) -> str:
    parts = []
    added = schema_delta.get("added") or []
    removed = schema_delta.get("removed") or []
    renamed = schema_delta.get("renamed") or []
    type_changed = schema_delta.get("type_changed") or []
    nullability_flipped = schema_delta.get("nullability_flipped") or []
    if added:
        parts.append(f"+{len(added)} col(s)")
    if removed:
        parts.append(f"-{len(removed)} col(s)")
    if renamed:
        parts.append(f"{len(renamed)} rename(s)")
    if type_changed:
        parts.append(f"{len(type_changed)} type change(s)")
    if nullability_flipped:
        parts.append(f"{len(nullability_flipped)} nullability change(s)")
    return ", ".join(parts) if parts else "—"


def _render_value_delta_cell(value_delta: dict | None) -> str:
    if value_delta is None:
        return "N/A"
    if value_delta.get("skipped_no_unique_key"):
        return "⚠️ skipped (no `unique_key`)"
    rows = value_delta.get("rows_with_diffs", 0)
    if rows == 0:
        return "—"
    return f"{rows} row(s) differ"


# ─── public composers ────────────────────────────────────────────────────────

_PROVISION_STEP_LABELS: list[tuple[str, str]] = [
    ("provision", "Provision workspace/lakehouse"),
    ("create-environment", "Create Fabric Environment"),
    ("publish-environment", "Publish Fabric Environment"),
    ("set-workspace-default", "Set workspace default environment"),
    ("upload-prod-state", "Upload prod-state to OneLake"),
    ("derive-shortcuts", "Derive shortcuts"),
    ("seed-shortcuts", "Seed shortcuts"),
    ("generate-notebook", "Generate and deploy notebook"),
]


def _step_outcome_icon(outcome: str) -> str:
    return {
        "success": "✅",
        "failure": "❌",
        "skipped": "⏭ skipped",
        "cancelled": "🚫 cancelled",
    }.get(outcome, "⏳")


def _render_provision_steps_table(provision_steps: dict[str, str]) -> str:
    rows = "\n".join(
        f"| {label} | {_step_outcome_icon(provision_steps.get(key, ''))} |"
        for key, label in _PROVISION_STEP_LABELS
    )
    return f"| Step | Result |\n|---|---|\n{rows}"


def render_workspace_comment(
    workspace_id: str,
    workspace_name: str,
    head_branch: str,
    greenfield_fallback: bool = False,
    shortcut_seeding: dict | None = None,
    *,
    provision_failed: bool = False,
    provision_steps: dict[str, str] | None = None,
) -> str:
    ws_url = FABRIC_WORKSPACE_URL.format(workspace_id=workspace_id)
    if provision_failed:
        table = _render_provision_steps_table(provision_steps or {})
        return (
            f"{COMMENT_MARKER}\n"
            f"## ⚠️ Provision Failed\n\n"
            f"**Workspace:** [{workspace_name}]({ws_url})  "
            f"**Branch:** `{head_branch}`\n\n"
            f"{table}\n\n"
            f"> Gates 2–5 will not run. Fix the failing step and push again."
        )
    greenfield_notice = ""
    if greenfield_fallback:
        greenfield_notice = (
            "\n> ⚠️ **No prod manifest available** — Slim CI is falling back to full build. "
            "Once a CD workflow publishes a `prod-manifest` artifact from `main`, "
            "Slim CI will use it automatically.\n"
        )
    seeding_section = _render_shortcut_seeding(shortcut_seeding)
    if seeding_section:
        seeding_section = "\n" + seeding_section
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
{seeding_section}"""


GATE_0_MARKER = "<!-- ci-static-check -->"
GATE_1_MARKER = "<!-- ci-state-modified+ -->"
GATE_2_MARKER = "<!-- ci-run -->"
GATE_3_MARKER = "<!-- ci-unit-tests -->"
GATE_4_MARKER = "<!-- ci-data-tests -->"
GATE_5_MARKER = "<!-- ci-data-diff -->"
PREFLIGHT_MARKER = "<!-- ci-preflight -->"


def render_gate_0_comment(
    compile_result,
    schema_gate,
    *,
    ruff=None,
    sqlfluff=None,
    gitleaks=None,
    scorecard=None,
    shortcut_seeding=None,
) -> str:
    has_gate_0 = bool(compile_result or schema_gate)
    has_tools = any(x is not None for x in [ruff, sqlfluff, gitleaks, scorecard, shortcut_seeding])

    if not has_gate_0 and not has_tools:
        return f"{GATE_0_MARKER}\n## Gate 0 — Static Analysis ⚠️\n\n_No data available._\n"

    # Compute tool results first so summary rows can be embedded in the Gate 0 table
    tool_table_rows = ""
    tool_parts = []
    tool_passed_flags = []
    for value, renderer, detail_fn, label in [
        (ruff,      render_ruff,      _detail_ruff,      "Ruff"),
        (sqlfluff,  render_sqlfluff,  _detail_sqlfluff,  "SQLFluff"),
        (gitleaks,  render_gitleaks,  _detail_gitleaks,  "Gitleaks"),
        (scorecard, render_scorecard, _detail_scorecard, "dbt Scorecard"),
    ]:
        if value is not None:
            tool_passed, tool_section = renderer(value)
            tool_passed_flags.append(tool_passed)
            tool_table_rows += f"| {label} | {_icon(tool_passed)} {detail_fn(value)} |\n"
            if not tool_passed:
                tool_parts.append(tool_section)

    gate_0_passed, section = render_gate_0(
        compile_result or {},
        schema_gate or {},
        extra_rows=tool_table_rows,
    )

    overall_passed = gate_0_passed and all(tool_passed_flags)
    if overall_passed != gate_0_passed:
        section = section.replace(
            f"## Gate 0 — Static Analysis {_icon(gate_0_passed)}",
            f"## Gate 0 — Static Analysis {_icon(overall_passed)}",
        )

    parts = [f"{GATE_0_MARKER}\n{section}"] + tool_parts

    shortcut_section = _render_shortcut_seeding(shortcut_seeding)
    if shortcut_section:
        parts.append(shortcut_section)

    return "\n".join(parts)


def render_gate_1_comment(
    closure: list[dict],
    *,
    greenfield: bool,
    passed: bool,
    platform_error: dict | None = None,
) -> str:
    """Render the ci/state-modified+ PR comment.

    When `platform_error` is set (VD-1596 Phase 2 artifact-mode failure), the
    body distinguishes a platform error from greenfield by carrying the
    Mode / Category / Reason triple plus a remediation footer that tells the
    operator to re-run the job or push a new commit. `platform_error` keys:
    `mode`, `category`, `reason`.
    """
    icon = _icon(passed)
    heading = f"## Gate 1 — Compile-time Logic {icon}"

    if platform_error:
        mode = platform_error.get("mode", "artifact")
        category = platform_error.get("category", "")
        reason = platform_error.get("reason", "")
        return (
            f"{GATE_1_MARKER}\n"
            f"{heading}\n\n"
            f"> ❌ **Platform error** — fetching prod state failed.\n\n"
            f"- Mode: {mode}\n"
            f"- Category: {category}\n"
            f"- Reason: {reason}\n\n"
            "Gates 2–5 did not run. Re-run this job once the issue is resolved, "
            "or push a new commit.\n"
        )

    if not passed and not closure:
        return (
            f"{GATE_1_MARKER}\n"
            f"{heading}\n\n"
            "Gate failed before the model closure was resolved — see CI logs.\n"
        )

    if greenfield:
        mode_line = "> ⚠️ **Greenfield** — full graph selected (no prod baseline available)\n"
    else:
        n = len(closure)
        mode_line = f"> **Incremental** — {n} model(s) in `state:modified+` scope\n"

    if not closure:
        return f"{GATE_1_MARKER}\n{heading}\n\n{mode_line}\n_No modified models in scope._\n"

    rows = "\n".join(
        f"| `{item['name']}` | `{item['materialization']}` |"
        for item in closure
    )
    table = "| Model | Materialization |\n|-------|----------------|\n" + rows + "\n"
    note = "_Project-owned models only — dbt package models (e.g. Elementary) are excluded._\n"
    return f"{GATE_1_MARKER}\n{heading}\n\n{mode_line}\n{table}\n{note}"


def render_gate_2_comment(result, *, run_url: str = "") -> str:
    if not result:
        link = f" [View CI run]({run_url})" if run_url else ""
        return (
            f"{GATE_2_MARKER}\n"
            f"## Gate 2 — Write ❌\n\n"
            f"Notebook run failed before writing results — "
            f"check CI logs for HTTP error, timeout, or notebook crash.{link}\n"
        )
    section = render_gate_2(result)
    return f"{GATE_2_MARKER}\n{section}"


def render_gate_3_comment(result) -> str:
    if not result:
        return f"{GATE_3_MARKER}\n## Gate 3 — Unit Tests ⚠️\n\n_No data available._\n"
    _, section = render_gate_3(result)
    return f"{GATE_3_MARKER}\n{section}"


def render_gate_4_comment(result) -> str:
    if not result:
        return (
            f"{GATE_4_MARKER}\n"
            "## Gate 4 — Data Tests ⏭️ Skipped\n\n"
            "Gate 2 (Isolated Build) did not succeed — data tests require built rows to run against.\n\n"
            "Fix the Gate 2 failure and re-push to trigger Gate 4.\n"
        )
    section = render_gate_4(result)
    return f"{GATE_4_MARKER}\n{section}"


def render_gate_5_comment(result) -> str:
    if not result:
        return (
            f"{GATE_5_MARKER}\n"
            "## Gate 5 — Data-Diff vs Prod ⏭️ Skipped\n\n"
            "Gate 2 (Isolated Build) did not succeed — data-diff requires built rows to compare against.\n\n"
            "Fix the Gate 2 failure and re-push to trigger Gate 5.\n"
        )
    section = render_gate_5(result)
    return f"{GATE_5_MARKER}\n{section}"


def _preflight_row_icon(status: str) -> str:
    return {
        "pass": "✅",
        "ok": "✅",
        "fail": "❌",
        "skipped": "⏭️",
        "behind": "⚠️",
    }.get(status, "⚠️")


def render_preflight_comment(result: dict | None) -> str:
    """Render the <!-- ci-preflight --> upserted PR comment section.

    Rows always shown: auto-rebase (informational), intent, ci-config.
    Violation row added only when auto_merge_disabled check failed.
    """
    if not result:
        return f"{PREFLIGHT_MARKER}\n## `ci/preflight` ⚠️\n\n_No data available._\n"

    overall = result.get("overall_status", "fail")
    heading_icon = "✅" if overall == "pass" else "❌"

    rows = []

    ar = result.get("auto_rebase", {})
    ar_icon = _preflight_row_icon(ar.get("status", "ok"))
    rows.append(f"| auto-rebase | {ar_icon} | {ar.get('message', '')} |")

    intent = result.get("intent", {})
    intent_icon = _preflight_row_icon(intent.get("status", "fail"))
    rows.append(f"| intent | {intent_icon} | {intent.get('message', '')} |")

    ci = result.get("ci_config", {})
    ci_status = ci.get("status", "fail")
    ci_icon = _preflight_row_icon(ci_status)

    if ci_status == "skipped":
        ci_detail = f"⏭ skipped — {ci.get('message', 'previous step failed')}"
    else:
        ci_detail = ci.get("message", "")
        if ci.get("line_number"):
            ci_detail = f"{ci_detail} (line {ci['line_number']})"
        missing = ci.get("missing_keys") or []
        if missing:
            formatted = ", ".join(f"`{k}`" for k in missing)
            ci_detail = f"{ci_detail} — missing: {formatted}"

    rows.append(f"| ci-config | {ci_icon} | {ci_detail} |")

    # Violation row: only shown when auto-merge is enabled (failure case)
    am = result.get("auto_merge_disabled", {})
    if am and not am.get("passed", True):
        rows.append(f"| auto-merge | ❌ | {am.get('message', '')} |")

    table = (
        "| Step | Status | Detail |\n"
        "|------|--------|--------|\n"
        + "\n".join(rows)
    )

    return (
        f"{PREFLIGHT_MARKER}\n"
        f"## `ci/preflight` {heading_icon}\n\n"
        f"{table}\n"
    )


def render_details_comment(bundle: ReportBundle) -> str:
    ruff = bundle.ruff if bundle.ruff is not None else []
    sqlfluff = bundle.sqlfluff if bundle.sqlfluff is not None else {}
    gitleaks = bundle.gitleaks if bundle.gitleaks is not None else {}
    scorecard = bundle.scorecard if bundle.scorecard is not None else {}

    ruff_passed, _ = render_ruff(ruff)
    sql_passed, _ = render_sqlfluff(sqlfluff)
    gl_passed, _ = render_gitleaks(gitleaks)
    sc_passed, _ = render_scorecard(scorecard)

    has_gate_0 = bool(bundle.compile_result or bundle.schema_gate)
    gate_0_passed = True
    if has_gate_0:
        gate_0_passed, _ = render_gate_0(
            bundle.compile_result or {},
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
