# E2E Incremental-Staging Design

## Intent
Validate CI pipeline behaviour when `stg_salescloud__opportunity` is modified, triggering full downstream closure.

## Modified models

### stg_salescloud__opportunity
Staging model: 1:1 with Salesforce Opportunity object, column rename + soft-delete exclusion.
- Grain: opportunity_id (unique, not null)
- Materialization: view
- Key columns: opportunity_id, account_id, owner_id, stage_name, amount, close_date, is_closed, is_won, fiscal_year, fiscal_quarter

### fct_pipeline
Core sales pipeline fact. Joins staging opportunity with account and user dimensions.
- Grain: opportunity_id (unique, not null)
- Materialization: table
- Unique key: opportunity_id
- Key columns: opportunity_id, account_id, owner_id, stage_name, amount, weighted_amount, forecast_category, is_closed, is_won, close_date, account_name, owner_name, sales_cycle_days

### fct_pipeline_monthly_product
Monthly pipeline aggregated by product and close month.
- Grain: (close_month, product_id) — combination unique
- Materialization: table
- Unique key: close_month, product_id
- Key columns: close_month, product_id, product_name, total_revenue, won_revenue, opportunity_count, win_rate_pct

### fct_sales_pipeline_by_stage
Pipeline summary aggregated by sales stage, fiscal year, and fiscal quarter.
- Grain: (stage_name, fiscal_year, fiscal_quarter)
- Materialization: table
- Key columns: stage_name, fiscal_year, fiscal_quarter, opportunity_count, won_count, lost_count, total_amount, weighted_amount, avg_probability
