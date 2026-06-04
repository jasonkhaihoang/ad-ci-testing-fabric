{{ config(materialized='table') }}

with opportunities as (
    select * from {{ ref('stg_salescloud__opportunity') }}
),

final as (
    select
        -- Primary key
        opportunity_id,

        -- Foreign keys
        account_id,
        owner_id,

        -- Opportunity attributes
        opportunity_name,
        stage_name,
        opportunity_type,
        lead_source,

        -- Financials
        amount,
        probability,
        expected_revenue,

        -- Dates
        created_date,
        close_date,
        last_stage_change_date,

        -- Status flags
        is_closed,
        is_won,

        -- Time dimensions
        fiscal_quarter,
        fiscal_year,

        -- Audit
        last_modified_date

    from opportunities
    where not is_deleted
)

select * from final
