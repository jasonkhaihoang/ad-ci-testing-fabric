{{ config(materialized='table') }}

with accounts as (
    select * from {{ ref('stg_salescloud__account') }}
),

banded as (
    select
        account_id,
        account_name,
        account_type,
        industry,
        annual_revenue,

        case
            when annual_revenue is null      then 'Unknown'
            when annual_revenue < 1000000    then 'SMB (<1M)'
            when annual_revenue < 10000000   then 'Mid-Market (1–10M)'
            when annual_revenue < 100000000  then 'Enterprise (10–100M)'
            else 'Large Enterprise (>100M)'
        end as revenue_band

    from accounts
)

select * from banded
