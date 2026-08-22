CI verification design stub — VD-4731 (dbt-quality single-source-of-truth fix)

Comment-only bump to `stg_salescloud__opportunity` to trigger the gate ladder
and the `dbt Quality` workflow. No structural change to the model.

## Models

### stg_salescloud__opportunity

- Materialization: view
- Columns: opportunity_id, account_id, owner_id, opportunity_name, stage_name,
  opportunity_type, lead_source, amount, probability, expected_revenue,
  created_date, close_date, last_stage_change_date, is_closed, is_won,
  is_deleted, last_modified_date, system_modified_timestamp, fiscal_quarter,
  fiscal_year
