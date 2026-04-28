{{ config(materialized='table') }}

with opportunities as (
    select * from {{ ref('stg_salescloud__opportunity') }}
),

accounts as (
    select * from {{ ref('dim_account') }}
),

users as (
    select * from {{ ref('dim_user') }}
),

opportunity_metrics as (
    select
        -- Primary key (grain: one row per opportunity)
        opp.opportunity_id,

        -- Foreign keys
        opp.account_id,
        opp.owner_id,

        -- Opportunity attributes
        opp.opportunity_name,
        opp.stage_name,
        opp.opportunity_type,
        opp.lead_source,

        -- Amounts and probability
        opp.amount,
        opp.probability,
        opp.expected_revenue,

        -- Calculated: Weighted pipeline value for forecasting
        case
            when opp.amount is not null and opp.probability is not null
            then opp.amount * (opp.probability / 100.0)
            else null
        end as weighted_amount,

        -- Dates
        opp.created_date,
        opp.close_date,
        opp.last_stage_change_date,

        -- Status flags
        opp.is_closed,
        opp.is_won,

        -- Calculated: Forecast category based on probability and status
        case
            when opp.is_closed = true and opp.is_won = true then 'Closed Won'
            when opp.is_closed = true and opp.is_won = false then 'Closed Lost'
            when opp.probability >= 75 then 'Best Case'
            when opp.probability >= 50 then 'Commit'
            when opp.probability >= 25 then 'Pipeline'
            else 'Upside'
        end as forecast_category,

        -- Calculated: Sales cycle duration (for closed opportunities)
        case
            when opp.is_closed = true and opp.close_date is not null
            then datediff(day, opp.created_date, opp.close_date)
            else null
        end as sales_cycle_days,

        -- Calculated: Age of opportunity
        datediff(day, opp.created_date, current_date()) as opportunity_age_days,

        -- Calculated: Days in current stage (for open opportunities)
        case
            when opp.is_closed = false and opp.last_stage_change_date is not null
            then datediff(day, opp.last_stage_change_date, current_date())
            else null
        end as days_in_current_stage,

        -- Data quality flags
        case when opp.account_id is null then true else false end as is_orphaned_opportunity,
        case when opp.amount = 0 or opp.amount is null then true else false end as is_zero_value,

        -- Audit
        opp.last_modified_date,
        opp.system_modified_timestamp

    from opportunities as opp
),

final as (
    select
        opp.*,

        -- Denormalized account attributes (for convenience)
        acct.account_name,
        acct.account_type,
        acct.industry,
        acct.billing_city,
        acct.billing_state,
        acct.billing_country,

        -- Denormalized user attributes (for convenience)
        usr.user_name as owner_name,
        usr.email as owner_email,
        usr.is_active as owner_is_active

    from opportunity_metrics as opp

    -- LEFT JOIN to preserve orphaned opportunities for data quality visibility
    left join accounts as acct
        on opp.account_id = acct.account_id

    -- LEFT JOIN to preserve unassigned opportunities
    left join users as usr
        on opp.owner_id = usr.user_id
)

select * from final
