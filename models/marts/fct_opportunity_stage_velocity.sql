{{ config(materialized='table') }}
-- VD-2358 validation: stage velocity metrics fact table

with opportunities as (
    select * from {{ ref('stg_salescloud__opportunity') }}
),

stage_metrics as (
    select
        stage_name,
        count(opportunity_id) as total_opportunities,
        count(
            case when is_closed = true and is_won = true
            then 1 end
        ) as won_count,
        count(
            case when is_closed = true and is_won = false
            then 1 end
        ) as lost_count,
        count(
            case when is_closed = false then 1 end
        ) as open_count,
        sum(
            case when is_closed = true and is_won = true
            then amount else 0 end
        ) as won_revenue,
        sum(
            case when is_closed = false then amount else 0 end
        ) as open_pipeline,
        avg(
            case
                when is_closed = true
                    and days_in_current_stage is not null
                then days_in_current_stage
            end
        ) as avg_days_in_stage,
        case
            when count(
                case when is_closed = true then 1 end
            ) > 0
            then count(
                case when is_closed = true and is_won = true
                then 1 end
            ) * 1.0 / count(
                case when is_closed = true then 1 end
            )
        end as win_rate
    from opportunities
    group by stage_name
)

select * from stage_metrics
