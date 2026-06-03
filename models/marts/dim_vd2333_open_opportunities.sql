{{ config(materialized='table') }}

with open_opps as (
    select
        opportunity_id,
        account_id,
        owner_id,
        opportunity_name,
        stage_name,
        opportunity_type,
        lead_source,
        amount,
        probability,
        expected_revenue,
        close_date,
        created_date,
        fiscal_quarter,
        fiscal_year
    from {{ ref('stg_salescloud__opportunity') }}
    where is_closed = false
)

select * from open_opps
