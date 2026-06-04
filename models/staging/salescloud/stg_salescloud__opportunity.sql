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
        systemmodstamp as system_modified_timestamp,

        -- Derived: fiscal quarter from close date
        case
            when month(closedate) in (1, 2, 3) then 'Q1'
            when month(closedate) in (4, 5, 6) then 'Q2'
            when month(closedate) in (7, 8, 9) then 'Q3'
            when month(closedate) in (10, 11, 12) then 'Q4'
        end as fiscal_quarter,

        -- Derived: fiscal year of close date
        year(closedate) as fiscal_year

    from source
    where isdeleted = false
)

select * from renamed

-- VD-2229 validation bump
-- VD-2370 validation bump
