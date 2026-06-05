{{ config(materialized='table') }}

with opps as (
    select * from {{ ref('stg_salescloud__opportunity') }}
),

final as (
    select
        opportunity_id,
        opportunity_name,
        stage_name,
        amount,
        probability,
        is_closed,
        is_won,
        close_date,
        fiscal_quarter,
        fiscal_year,

        case
            when is_closed = true and is_won = true then 'Closed Won'
            when is_closed = true and is_won = false then 'Closed Lost'
            when probability >= 75 then 'Best Case'
            when probability >= 50 then 'Commit'
            when probability >= 25 then 'Pipeline'
            else 'Upside'
        end as forecast_category

    from opps
)

select * from final
-- VD-2375 validation: unit test references stg_salescloud__opportunity as unmodified view fixture
