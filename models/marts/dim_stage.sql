{{ config(materialized='table') }}

with stages as (
    select distinct
        stage_name
    from {{ ref('stg_salescloud__opportunity') }}
    where stage_name is not null
)

select
    stage_name
from stages
