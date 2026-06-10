# E2E Incremental-Staging Design

## Intent
Validate CI pipeline behaviour when an upstream staging model is modified, triggering full downstream closure.

## Modified models

### stg_salescloud__opportunity
Staging model for Salesforce Opportunity data.
- Grain: one row per opportunity (`opportunity_id`)
- Materialization: view
- Source: salescloud opportunity raw table

### fct_pipeline
Fact model aggregating active pipeline opportunities.
- Grain: one row per opportunity (`opportunity_id`)
- Materialization: table
- Unique key: `opportunity_id`
- Downstream of: `stg_salescloud__opportunity`

### fct_pipeline_monthly_product
Fact model for monthly pipeline aggregated by product.
- Grain: one row per close month and product (`close_month`, `product_id`)
- Materialization: table
- Unique key: `close_month`, `product_id`
- Downstream of: `fct_pipeline`

### fct_sales_pipeline_by_stage
Fact model for pipeline aggregated by sales stage.
- Grain: one row per opportunity and stage
- Materialization: table
- Downstream of: `stg_salescloud__opportunity`
