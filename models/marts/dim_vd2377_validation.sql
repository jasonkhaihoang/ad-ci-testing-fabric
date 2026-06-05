{{ config(materialized='table') }}

-- VD-2377 validation model: exercises AC-33 (Gate 2 clones dim_user TABLE).

with opportunities as (
    select * from {{ ref('stg_salescloud__opportunity') }}
),

users as (
    select * from {{ ref('dim_user') }}
),

final as (
    select
        opp.opportunity_id,
        opp.owner_id,
        usr.user_name as owner_name,
        usr.job_title as owner_job_title,
        opp.amount,
        opp.stage_name,
        opp.is_won
    from opportunities as opp
    left join users as usr
        on opp.owner_id = usr.user_id
)

select * from final
