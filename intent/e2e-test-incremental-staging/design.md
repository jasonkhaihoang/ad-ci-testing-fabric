# E2E Incremental-Staging Design

## Intent
Validate CI pipeline behaviour when an upstream staging model (`stg_salescloud__opportunity`) is modified, triggering full downstream closure across the sales pipeline fact models.

## Modified models

### stg_salescloud__opportunity
Staging model for Salesforce opportunities representing the sales pipeline. Provides a 1:1 mapping with the source Salesforce Opportunity object, applying column renaming and excluding soft-deleted records.

- Grain: one row per opportunity (`opportunity_id`)
- Materialization: view
- Columns: opportunity_id, account_id, owner_id, opportunity_name, stage_name, opportunity_type, lead_source, amount, probability, expected_revenue, created_date, close_date, last_stage_change_date, is_closed, is_won, is_deleted, last_modified_date, system_modified_timestamp, fiscal_quarter, fiscal_year

### fct_pipeline
Sales pipeline fact table — the core analytical table for pipeline reporting. Joins opportunity data with account and user dimensions.

- Grain: one row per opportunity (`opportunity_id`)
- Materialization: table
- Unique key: opportunity_id
- Columns: opportunity_id, account_id, owner_id, opportunity_name, stage_name, opportunity_type, lead_source, amount, probability, expected_revenue, weighted_amount, created_date, close_date, last_stage_change_date, is_closed, is_won, forecast_category, sales_cycle_days, opportunity_age_days, days_in_current_stage, is_orphaned_opportunity, is_zero_value, last_modified_date, system_modified_timestamp, account_name, account_type, industry, billing_city, billing_state, billing_country, owner_name, owner_email, owner_is_active

### fct_pipeline_monthly_product
Monthly product-level aggregates showing product performance metrics aggregated by close month.

- Grain: one row per close month and product (`close_month`, `product_id`)
- Materialization: table
- Unique key: close_month, product_id
- Columns: close_month, product_id, product_code, product_name, total_revenue, won_revenue, lost_revenue, opportunity_count, won_opportunity_count, lost_opportunity_count, total_quantity, line_item_count, avg_deal_size, avg_unit_price, avg_discount, win_rate_pct, earliest_close_date, latest_close_date

### fct_sales_pipeline_by_stage
Sales pipeline summary aggregated by stage, fiscal year, and fiscal quarter.

- Grain: one row per stage, fiscal year, and fiscal quarter (`stage_name`, `fiscal_year`, `fiscal_quarter`)
- Materialization: table
- Columns: stage_name, fiscal_year, fiscal_quarter, opportunity_count, won_count, lost_count, total_amount, weighted_amount, avg_probability
