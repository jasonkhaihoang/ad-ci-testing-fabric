# Sales Pipeline dbt Project — Design

CI verification design doc for the `e2e-test-vd3472` intent — documents every
model in the `sales-pipeline` dbt project so `ci/design-drift` can compare it
against the full `state:modified` set produced by a greenfield run (no prod
manifest to defer against, so every model in the project is "modified").

## Staging models

### stg_salescloud__account
View. Cleaned Salesforce Account object.
Columns: account_id, account_name, account_type, billing_city, billing_country,
billing_state, created_date, industry, is_deleted, last_modified_date, owner_id.

### stg_salescloud__user
View. Cleaned Salesforce User object.
Columns: created_date, email, is_active, job_title, last_modified_date,
profile_id, user_id, user_name, user_role_id, username.

### stg_salescloud__opportunity
View. Cleaned Salesforce Opportunity object.
Columns: account_id, amount, close_date, created_date, expected_revenue,
fiscal_quarter, fiscal_year, is_closed, is_deleted, is_won, last_modified_date,
last_stage_change_date, lead_source, opportunity_id, opportunity_name,
opportunity_type, owner_id, probability, stage_name, system_modified_timestamp.

### stg_salescloud__opportunitylineitem
View. Cleaned Salesforce OpportunityLineItem object.
Columns: created_date, description, discount, line_item_id, opportunity_id,
pricebook_entry_id, product_code, product_id, product_name, quantity,
service_date, sort_order, total_price, unit_price.

## Dimension models

### dim_account
Table. Account dimension built from stg_salescloud__account.
Columns: account_id, account_name, account_type, billing_city, billing_country,
billing_state, created_date, industry, last_modified_date, owner_id.

### dim_user
Table. User dimension built from stg_salescloud__user.
Columns: created_date, email, is_active, job_title, last_modified_date,
profile_id, user_id, user_name, user_role_id, username.

## Fact models

### fct_pipeline
Table. Opportunity-grain sales pipeline fact, denormalized with account and
owner attributes.
Columns: account_id, account_name, account_type, amount, billing_city,
billing_country, billing_state, close_date, created_date,
days_in_current_stage, expected_revenue, forecast_category, industry,
is_closed, is_orphaned_opportunity, is_won, is_zero_value, last_modified_date,
last_stage_change_date, lead_source, opportunity_age_days, opportunity_id,
opportunity_name, opportunity_type, owner_email, owner_id, owner_is_active,
owner_name, probability, sales_cycle_days, stage_name,
system_modified_timestamp, weighted_amount.

### fct_pipeline_monthly_product
Table. Monthly product-grain rollup of pipeline/won/lost revenue built from
opportunity line items.
Columns: avg_deal_size, avg_discount, avg_unit_price, close_month,
earliest_close_date, latest_close_date, line_item_count,
lost_opportunity_count, lost_revenue, opportunity_count, product_code,
product_id, product_name, total_quantity, total_revenue, win_rate_pct,
won_opportunity_count, won_revenue.

### fct_sales_pipeline_by_stage
Table. Stage/fiscal-quarter grain rollup of pipeline amounts and counts.
Columns: avg_probability, fiscal_quarter, fiscal_year, lost_count,
opportunity_count, stage_name, total_amount, weighted_amount, won_count.
