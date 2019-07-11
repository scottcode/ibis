"""Test support for already-defined UDFs in Postgres"""

import pytest

import ibis.expr.datatypes as dt
from ibis.sql.postgres import existing_udf
from ibis.sql.postgres.udf.api import (
    func_to_udf,
    remove_decorators
)


# mark test module as postgresql (for ability to easily exclude,
# e.g. in conda build tests)
# (Temporarily adding `postgis` marker so Azure Windows pipeline will exclude
#     pl/python tests.
#     TODO: update Windows pipeline to exclude postgres_extensions
#     TODO: remove postgis marker below once Windows pipeline updated
pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.postgis,
    pytest.mark.postgres_extensions,
]

# Database setup (tables and UDFs)


@pytest.fixture(scope='session')
def next_serial(con):
    # `test_sequence` SEQUENCE is created in database in the
    # load-data.sh --> datamgr.py#postgres step
    # to avoid parallel attempts to create the same sequence (when testing
    # run in parallel
    serial_proxy = con.con.execute("SELECT nextval('test_sequence') as value;")
    return serial_proxy.fetchone()['value']


@pytest.fixture(scope='session')
def test_schema(con, next_serial):
    schema_name = 'udf_test_{}'.format(next_serial)
    con.con.execute(
        "CREATE SCHEMA IF NOT EXISTS {};".format(schema_name)
    )
    return schema_name


@pytest.fixture(scope='session')
def table_name():
    return 'udf_test_users'


@pytest.fixture(scope='session')
def sql_table_setup(test_schema, table_name):
    return """DROP TABLE IF EXISTS {schema}.{table_name};
CREATE TABLE {schema}.{table_name} (
    user_id integer,
    user_name varchar,
    name_length integer
);
INSERT INTO {schema}.{table_name} VALUES
(1, 'Raj', 3),
(2, 'Judy', 4),
(3, 'Jonathan', 8)
;
""".format(schema=test_schema, table_name=table_name)


@pytest.fixture(scope='session')
def sql_define_py_udf(test_schema):
    return """CREATE OR REPLACE FUNCTION {schema}.pylen(x varchar)
RETURNS integer
LANGUAGE plpythonu
AS
$$
return len(x)
$$;""".format(schema=test_schema)


@pytest.fixture(scope='session')
def sql_define_udf(test_schema):
    return """CREATE OR REPLACE FUNCTION {schema}.custom_len(x varchar)
RETURNS integer
LANGUAGE SQL
AS
$$
SELECT length(x);
$$;""".format(schema=test_schema)


@pytest.fixture(scope='session')
def con_for_udf(
        con,
        test_schema,
        sql_table_setup,
        sql_define_udf,
        sql_define_py_udf
):
    con.con.execute(sql_table_setup)
    con.con.execute(sql_define_udf)
    con.con.execute(sql_define_py_udf)
    try:
        yield con
    finally:
        # teardown
        con.con.execute("DROP SCHEMA IF EXISTS {} CASCADE".format(test_schema))


@pytest.fixture
def table(con_for_udf, table_name, test_schema):
    return con_for_udf.table(table_name, schema=test_schema)

# Tests


def test_sql_length_udf_worked(test_schema, table):
    """Test creating ibis UDF object based on existing UDF in the database"""
    # Create ibis UDF objects referring to UDFs already created in the database
    custom_length_udf = existing_udf(
        'custom_len',
        input_types=[dt.string],
        output_type=dt.int32,
        schema=test_schema
    )
    result_obj = table[
        table,
        custom_length_udf(table['user_name']).name('custom_len')
    ]
    result = result_obj.execute()
    assert result['custom_len'].sum() == result['name_length'].sum()


def test_py_length_udf_worked(test_schema, table):
    # Create ibis UDF objects referring to UDFs already created in the database
    py_length_udf = existing_udf(
        'pylen',
        input_types=[dt.string],
        output_type=dt.int32,
        schema=test_schema
    )
    result_obj = table[
        table,
        py_length_udf(table['user_name']).name('custom_len')
    ]
    result = result_obj.execute()
    assert result['custom_len'].sum() == result['name_length'].sum()


def mult_a_b(a, b):
    """Test function to be defined in-database as a UDF
    and used via ibis UDF"""
    return a * b


def test_func_to_udf_smoke(con_for_udf, test_schema, table):
    """Test creating a UDF in database based on Python function
    and then creating an ibis UDF object based on that"""
    mult_a_b_udf = func_to_udf(
        con_for_udf.con,
        mult_a_b,
        (dt.int32, dt.int32),
        dt.int32,
        schema=test_schema,
        replace=True
    )
    table_filt = table.filter(table['user_id'] == 2)
    expr = table_filt[
        mult_a_b_udf(
            table_filt['user_id'],
            table_filt['name_length']
        ).name('mult_result')
    ]
    result = expr.execute()
    assert result['mult_result'].iloc[0] == 8


def test_client_udf_api(con_for_udf, test_schema, table):
    """Test creating a UDF in database based on Python function
    using an ibis client method."""
    @con_for_udf.udf(
        [dt.int32, dt.int32],
        dt.int32,
        schema=test_schema,
        replace=True)
    def multiply(a, b):
        return a * b

    table_filt = table.filter(table['user_id'] == 2)
    expr = table_filt[
        multiply(
            table_filt['user_id'],
            table_filt['name_length']
        ).name('mult_result')
    ]
    result = expr.execute()
    assert result['mult_result'].iloc[0] == 8


def test_remove_decorators():
    input_ = """\
@mydeco1(1, 3)
@mydeco2
@mydeco3(
    'dummy',
    5,
    None
)
def orig_func(x, y, z):
    return x * y + z
"""
    expected = """\
def orig_func(x, y, z):
    return x * y + z
"""
    assert remove_decorators(input_) == expected


def test_remove_decorators_when_none_exist():
    input_ = """\
def orig_func(x, y, z):
    return x * y + z
"""
    assert remove_decorators(input_) == input_
