{{ config(materialized='view') }}

with source as (
    select * from {{ source('salescloud', 'opportunity') }}
),

renamed as (
    select
        -- Primary key
        id as opportunity_id,

        -- Foreign keys
        accountid as account_id,
        ownerid as owner_id,

        -- Opportunity attributes
        name as opportunity_name,
        stagename as stage_name,
        type as opportunity_type,
        leadsource as lead_source,

        -- Amounts and metrics
        amount,
        probability,
        expectedrevenue as expected_revenue,

        -- Dates
        createddate as created_date,
        closedate as close_date,
        laststagechangedate as last_stage_change_date,

        -- Flags
        isclosed as is_closed,
        iswon as is_won,
        isdeleted as is_deleted,

        -- Audit fields
        lastmodifieddate as last_modified_date,
        systemmodstamp as system_modified_timestamp

    from source
    where isdeleted = false  -- Exclude soft-deleted records
)

select * from renamed
-- modified: 2026-05-16
