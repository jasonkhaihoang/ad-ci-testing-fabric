# E2E Incremental-Staging Design

## Summary
This PR makes a marker-only change to `stg_salescloud__opportunity` for CI test purposes. **No columns are added, removed, or renamed in any model.** No grain changes. No materialization changes. No unique_key changes. The only change is a code comment marker in the SQL file.

## Expected state:modified+ closure
Exactly four models are expected:

1. **stg_salescloud__opportunity** — view, grain: opportunity_id, no structural changes
2. **fct_pipeline** — table, unique_key: opportunity_id, no structural changes
3. **fct_pipeline_monthly_product** — table, unique_key: [close_month, product_id], no structural changes
4. **fct_sales_pipeline_by_stage** — table, grain: [stage_name, fiscal_year, fiscal_quarter], no structural changes

## Drift assessment
Since this is a marker-only change with no structural modifications, the correct finding is: **has_drift = false, findings = []**.
