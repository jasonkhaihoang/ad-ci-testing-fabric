{{ config(materialized='table') }}

with source as (
    select * from {{ ref('stg_salescloud__opportunity') }}
),

final as (
    select distinct stage_name
    from source
    where stage_name is not null
)

select * from final
