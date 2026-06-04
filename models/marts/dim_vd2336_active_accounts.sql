{{ config(materialized='table') }}

-- VD-2336 validation: new table-materialized mart added alongside a view-materialized stg bump.
-- Validates AC-3: table models are still cloned during gate 2 (not skipped like views).

with accounts as (
    select * from {{ ref('stg_salescloud__account') }}
),

final as (
    select
        account_id,
        account_name,
        account_type,
        industry,
        billing_city,
        billing_state,
        billing_country,
        created_date
    from accounts
)

select * from final
