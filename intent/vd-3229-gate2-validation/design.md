# Design

## stg_salescloud__opportunity
- materialization: view
- columns: account_id, amount, close_date, created_date, expected_revenue, fiscal_quarter, fiscal_year, is_closed, is_deleted, is_won, last_modified_date, last_stage_change_date, lead_source, opportunity_id, opportunity_name, opportunity_type, owner_id, probability, stage_name, system_modified_timestamp

## fct_pipeline
- materialization: table
- columns: account_id, account_name, account_type, amount, billing_city, billing_country, billing_state, close_date, created_date, days_in_current_stage, expected_revenue, forecast_category, industry, is_closed, is_orphaned_opportunity, is_won, is_zero_value, last_modified_date, last_stage_change_date, lead_source, opportunity_age_days, opportunity_id, opportunity_name, opportunity_type, owner_email, owner_id, owner_is_active, owner_name, probability, sales_cycle_days, stage_name, system_modified_timestamp, weighted_amount

## fct_pipeline_monthly_product
- materialization: table
- columns: avg_deal_size, avg_discount, avg_unit_price, close_month, earliest_close_date, latest_close_date, line_item_count, lost_opportunity_count, lost_revenue, opportunity_count, product_code, product_id, product_name, total_quantity, total_revenue, win_rate_pct, won_opportunity_count, won_revenue

## fct_sales_pipeline_by_stage
- materialization: table
- columns: avg_probability, fiscal_quarter, fiscal_year, lost_count, opportunity_count, stage_name, total_amount, weighted_amount, won_count
