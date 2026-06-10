# E2E Incremental-Staging Design

## Scope note
This is a CI end-to-end test fixture. Column-level definitions are owned exclusively by the dbt schema.yml files and are intentionally not specified here. Column-level drift is out of scope — do not flag missing_column or unexpected_column findings.

## Intent
Validate CI pipeline behaviour when an upstream staging model is modified, triggering full downstream closure.

## Modified models

The following models are expected to appear in state:modified+ as a result of marking stg_salescloud__opportunity:

- **stg_salescloud__opportunity** — staging model for Salesforce Opportunity data (grain: opportunity_id, materialization: view)
- **fct_pipeline** — fact model for active pipeline opportunities (grain: opportunity_id, materialization: table)
- **fct_pipeline_monthly_product** — fact model for monthly pipeline by product (grain: close_month + product_id, materialization: table)
- **fct_sales_pipeline_by_stage** — fact model for pipeline by sales stage (materialization: table)

All four models are expected. No additional models are expected. No structural (grain, materialization, unique_key) changes are made — this is a marker-only change for CI test purposes.
