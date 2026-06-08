-- ci-test marker
{{ config(materialized='table') }}

with opportunities as (
    select * from {{ ref('stg_salescloud__opportunity') }}
),

pipeline_by_stage as (
    select
        stage_name,
        fiscal_year,
        fiscal_quarter,
        count(*) as opportunity_count,
        count(case when is_won = true then 1 end) as won_count,
        count(
            case
                when is_closed = true and is_won = false
                    then 1
            end
        ) as lost_count,
        sum(amount) as total_amount,
        sum(
            case
                when amount is not null and probability is not null
                    then amount * (probability / 100.0)
            end
        ) as weighted_amount,
        avg(probability) as avg_probability
    from opportunities
    group by stage_name, fiscal_year, fiscal_quarter
)

select *, 'ci-diff-test' as _ci_marker from pipeline_by_stage
