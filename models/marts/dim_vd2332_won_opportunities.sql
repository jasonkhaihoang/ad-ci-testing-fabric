{{ config(materialized='table') }}

with won_opps as (
    select
        opportunity_id,
        account_id,
        owner_id,
        opportunity_name,
        stage_name,
        opportunity_type,
        lead_source,
        amount,
        expected_revenue,
        close_date,
        fiscal_quarter,
        fiscal_year
    from {{ ref('stg_salescloud__opportunity') }}
    where is_won = true
)

select * from won_opps
