{% macro get_fixture_sql(rows, column_name_to_data_types) %}
-- Fixture for {{ model.name }}
{% set default_row = {} %}

{%- if not column_name_to_data_types -%}

  {%- if target.type == 'duckdb' -%}
    {#-- DuckDB: infer column types from fixture row values.
         Avoids adapter.get_columns_in_relation() which requires the relation to
         exist in the database — impossible in an in-memory DuckDB session where
         staging models are never materialised. --#}
    {%- set column_name_to_data_types = {} -%}
    {%- set column_name_to_quoted = {} -%}
    {%- if rows | length > 0 -%}
      {%- for col_name in rows[0].keys() -%}
        {%- set col_lower = col_name | lower -%}
        {%- set inferred = namespace(type='varchar') -%}
        {%- for row in rows if inferred.type == 'varchar' -%}
          {%- set val = row[col_name] -%}
          {%- if val is not none -%}
            {%- if val is sameas true or val is sameas false -%}
              {%- set inferred.type = 'boolean' -%}
            {%- elif val is float -%}
              {%- set inferred.type = 'float' -%}
            {%- elif val is number -%}
              {%- set inferred.type = 'integer' -%}
            {%- elif val is string -%}
              {%- set v = val | trim -%}
              {%- if v | length == 10 and v[4:5] == '-' and v[7:8] == '-' -%}
                {%- set inferred.type = 'date' -%}
              {%- elif v | length >= 19 and v[4:5] == '-' and v[7:8] == '-'
                    and (v[10:11] == ' ' or v[10:11] == 'T') -%}
                {%- set inferred.type = 'timestamp' -%}
              {%- endif -%}
            {%- endif -%}
          {%- endif -%}
        {%- endfor -%}
        {#-- All-null column: fall back to the model's schema-yml data_type --#}
        {%- if inferred.type == 'varchar' -%}
          {%- if col_lower in model.columns and model.columns[col_lower].data_type -%}
            {%- set inferred.type = model.columns[col_lower].data_type -%}
          {%- endif -%}
        {%- endif -%}
        {%- do column_name_to_data_types.update({col_lower: inferred.type}) -%}
        {%- do column_name_to_quoted.update({col_lower: '"' ~ col_lower ~ '"'}) -%}
      {%- endfor -%}
    {%- else -%}
      {#-- Empty rows: build schema entirely from the model's schema-yml definition --#}
      {%- for col_name, col_def in model.columns.items() -%}
        {%- set col_lower = col_name | lower -%}
        {%- set dt = col_def.data_type if col_def.data_type else 'varchar' -%}
        {%- do column_name_to_data_types.update({col_lower: dt}) -%}
        {%- do column_name_to_quoted.update({col_lower: '"' ~ col_lower ~ '"'}) -%}
      {%- endfor -%}
    {%- endif -%}

  {%- else -%}
    {#-- All other adapters: standard dbt behaviour — live schema introspection. --#}
    {%- set this_or_defer_relation = defer_relation if (defer_relation and not load_relation(this)) else this -%}
    {%- set columns_in_relation = adapter.get_columns_in_relation(this_or_defer_relation) -%}
    {%- set column_name_to_data_types = {} -%}
    {%- set column_name_to_quoted = {} -%}
    {%- for column in columns_in_relation -%}
      {%- do column_name_to_data_types.update({column.name|lower: column.data_type}) -%}
      {%- do column_name_to_quoted.update({column.name|lower: column.quoted}) -%}
    {%- endfor -%}
  {%- endif -%}

{%- else -%}
  {#-- column_name_to_data_types provided by caller: derive quoted names. --#}
  {%- set column_name_to_quoted = {} -%}
  {%- for col_name in column_name_to_data_types.keys() -%}
    {%- do column_name_to_quoted.update({col_name: '"' ~ col_name ~ '"'}) -%}
  {%- endfor -%}
{%- endif -%}

{%- if not column_name_to_data_types -%}
  {{ exceptions.raise_compiler_error("Not able to get columns for unit test '" ~ model.name ~ "' from relation " ~ this ~ " because the relation doesn't exist") }}
{%- endif -%}

{%- for column_name, column_type in column_name_to_data_types.items() -%}
  {%- do default_row.update({column_name: (safe_cast("null", column_type) | trim )}) -%}
{%- endfor -%}

{{ validate_fixture_rows(rows, row_number) }}

{%- for row in rows -%}
{%-   set formatted_row = format_row(row, column_name_to_data_types) -%}
{%-   set default_row_copy = default_row.copy() -%}
{%-   do default_row_copy.update(formatted_row) -%}
select
{%-   for column_name, column_value in default_row_copy.items() %} {{ column_value }} as {{ column_name_to_quoted[column_name] }}{% if not loop.last -%}, {%- endif %}
{%-   endfor %}
{%-   if not loop.last %}
union all
{%    endif %}
{%- endfor -%}

{%- if (rows | length) == 0 -%}
  select
  {%- for column_name, column_value in default_row.items() %} {{ column_value }} as {{ column_name_to_quoted[column_name] }}{% if not loop.last -%},{%- endif %}
  {%- endfor %}
  limit 0
{%- endif -%}
{% endmacro %}
