{{ config(materialized='view') }}
-- main: account model v2 (VD-2117 advance)

with source as (
    select * from {{ source('salescloud', 'account') }}
),

renamed as (
    select
        -- Primary key
        id as account_id,

        -- Account attributes
        name as account_name,
        type as account_type,
        industry,

        -- Contact info
        billingcity as billing_city,
        billingstate as billing_state,
        billingcountry as billing_country,

        -- Foreign keys
        ownerid as owner_id,

        -- Flags
        isdeleted as is_deleted,

        -- Audit fields
        createddate as created_date,
        lastmodifieddate as last_modified_date,
        annualrevenue as annual_revenue

    from source
    where isdeleted = false  -- Exclude soft-deleted records
)

select * from renamed

-- VD-2079 AC2 MAIN branch change
