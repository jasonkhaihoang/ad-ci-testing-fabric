{{ config(materialized='table') }}

with stages as (
    select distinct
        stage_name,
        case
            when stage_name in ('Prospecting', 'Qualification') then 1
            when stage_name in ('Needs Analysis', 'Value Proposition') then 2
            when stage_name in ('Id. Decision Makers', 'Perception Analysis') then 3
            when stage_name in ('Proposal/Price Quote', 'Negotiation/Review') then 4
            when stage_name in ('Closed Won', 'Closed Lost') then 5
            else 0
        end as stage_order,
        case
            when stage_name like 'Closed%' then true
            else false
        end as is_closed_stage,
        case
            when stage_name = 'Closed Won' then true
            else false
        end as is_won_stage
    from {{ ref('stg_salescloud__opportunity') }}
    where stage_name is not null
)

select * from stages
