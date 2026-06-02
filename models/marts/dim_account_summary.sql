{{ config(materialized='table') }}

with accounts as (
    select * from {{ ref('stg_salescloud__account') }}
)

select
    account_id,
    account_name,
    account_type,
    industry,
    billing_city,
    billing_state,
    billing_country
from accounts
