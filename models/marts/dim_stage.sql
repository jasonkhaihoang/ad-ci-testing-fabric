{{ config(materialized='table') }}

with opportunities as (
    select * from {{ ref('stg_salescloud__opportunity') }}
),

stages as (
    select distinct
        stage_name,

        -- Derived: stage ordering approximation from typical Salesforce pipeline progression
        case stage_name
            when 'Prospecting'         then 1
            when 'Qualification'       then 2
            when 'Needs Analysis'      then 3
            when 'Value Proposition'   then 4
            when 'Id. Decision Makers' then 5
            when 'Perception Analysis' then 6
            when 'Proposal/Price Quote' then 7
            when 'Negotiation/Review'  then 8
            when 'Closed Won'          then 9
            when 'Closed Lost'         then 10
            else 99
        end as stage_order,

        -- Derived: whether the stage represents a closed state
        case
            when stage_name in ('Closed Won', 'Closed Lost') then true
            else false
        end as is_closed_stage,

        -- Derived: whether the stage represents a won outcome
        case
            when stage_name = 'Closed Won' then true
            else false
        end as is_won_stage

    from opportunities
    where stage_name is not null
)

select * from stages
