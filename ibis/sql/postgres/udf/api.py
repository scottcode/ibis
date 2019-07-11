import ast
import inspect
from textwrap import dedent
from decimal import Decimal
import collections
import itertools

import sqlalchemy as sa

import ibis.expr.rules as rlz
from ibis.expr import datatypes
from ibis.expr.signature import Argument as Arg
from ibis.sql.postgres.compiler import PostgresUDFNode, add_operation


_udf_name_cache = collections.defaultdict(itertools.count)


# type mapping based on: https://www.postgresql.org/docs/10/plpython-data.html
sql_default_type = 'VARCHAR'

pytype_sql = {
    bool: 'BOOLEAN',
    int: "INTEGER",
    float: 'DOUBLE',
    Decimal: 'NUMERIC',
    bytes: 'BYTEA',
    str: sql_default_type,
}

pytype_ibistype = {
    bool: datatypes.Boolean(),
    int: datatypes.Int32(),
    float: datatypes.Float(),
    Decimal: datatypes.Float(),
    bytes: datatypes.Binary(),
    str: datatypes.String(),
}

ibistype_pytype = {v: k for k, v in pytype_ibistype.items()}
ibistype_sqltype = {
    ibistype: pytype_sql[pytype]
    for ibistype, pytype in ibistype_pytype.items()
}


def create_udf_node(name, fields):
    """Create a new UDF node type.

    Parameters
    ----------
    name : str
        Then name of the UDF node
    fields : OrderedDict
        Mapping of class member name to definition

    Returns
    -------
    result : type
        A new PostgresUDFNode subclass
    """
    definition = next(_udf_name_cache[name])
    external_name = '{}_{:d}'.format(name, definition)
    return type(external_name, (PostgresUDFNode,), fields)


def existing_udf(name,
                 input_types,
                 output_type,
                 schema=None,
                 parameters=None):
    """Create an ibis function that refers to an existing Postgres UDF already
    defined in database

    Parameters
    ----------
    name: str
    input_types : List[DataType]
    output_type : DataType
    schema: str - optionally specify the schema that the UDF is defined in
    parameters: List[str] - give names to the arguments of the UDF

    Returns
    -------
    wrapper : Callable
        The wrapped function
    """
    if parameters is None:
        parameters = ['v{}'.format(i) for i in range(len(input_types))]
    elif len(input_types) != len(parameters):
        raise ValueError(
            (
                "Length mismatch in arguments to existing_udf: "
                "len(input_types)={}, len(parameters)={}"
            ).format(len(input_types), len(parameters))
        )

    udf_node_fields = collections.OrderedDict([
        (name, Arg(rlz.value(type_)))
        for name, type_ in zip(parameters, input_types)
    ] + [
        (
            'output_type',
            lambda self, output_type=output_type: rlz.shape_like(
                self.args, dtype=output_type
            )
        )
    ])
    udf_node_fields['resolve_name'] = lambda self: name

    udf_node = create_udf_node(name, udf_node_fields)

    def _translate_udf(t, expr):
        func_obj = sa.func
        if schema is not None:
            func_obj = getattr(func_obj, schema)
        func_obj = getattr(func_obj, name)

        sa_args = [t.translate(arg) for arg in expr.op().args]

        return func_obj(*sa_args)

    add_operation(udf_node, _translate_udf)

    def wrapped(*args, **kwargs):
        node = udf_node(*args, **kwargs)
        return node.to_expr()

    return wrapped


class LineNums(ast.NodeVisitor):
    """NodeVisitor for abstract syntax tree that notes the line numbers
    of all decorator lines and (separately) all other node types"""
    def __init__(self):
        self.first_non_decorator_line = None
        self.decorator_lines = []

    def visit_FunctionDef(self, node):
        self.decorator_lines.extend(
            n.lineno for n in node.decorator_list
        )
        for func_field, func_node in ast.iter_fields(node):
            if (
                    func_field != 'decorator_list'
                    and isinstance(func_node, ast.AST)
            ):
                self.generic_visit(func_node)

    def generic_visit(self, node):
        if hasattr(node, 'lineno'):
            if self.first_non_decorator_line is None:
                self.first_non_decorator_line = node.lineno
            else:
                self.first_non_decorator_line = min(
                    self.first_non_decorator_line,
                    node.lineno
                )
        ast.NodeVisitor.generic_visit(self, node)


def remove_decorators(funcdef_source):
    """Given a string of source code defining a function, strip out all
    decorator lines and return the resulting string"""
    func_ast = ast.parse(funcdef_source)
    visitor = LineNums()
    visitor.visit(func_ast)
    lines = funcdef_source.splitlines(keepends=True)
    first_nondecorator = visitor.first_non_decorator_line - 1
    return ''.join(lines[first_nondecorator:])


def func_to_udf(conn,
                python_func,
                in_types=None,
                out_type=None,
                schema=None,
                replace=False,
                name=None):
    """Defines a UDF in the database

    Parameters
    ----------
    conn: sqlalchemy engine
    python_func: python function
    in_types: List[DataType]; if left None, will try to infer datatypes from
    function signature
    out_type : DataType
    schema: str - optionally specify the schema in which to define the UDF
    replace: bool - replace UDF in database if already exists
    name: str - name for the UDF to be defined in database

    Returns
    -------
    wrapper : Callable
        The ibis UDF object as a wrapped function
    """
    if name is None:
        internal_name = python_func.__name__
    else:
        internal_name = name
    signature = inspect.signature(python_func)
    parameter_names = signature.parameters.keys()
    if in_types is None:
        raise NotImplementedError('inferring in_types not implemented')
    if out_type is None:
        raise NotImplementedError('inferring out_type not implemented')
    replace_text = ' OR REPLACE ' if replace else ''
    schema_fragment = (schema + '.') if schema else ''
    template = """CREATE {replace} FUNCTION
{schema_fragment}{name}({signature})
RETURNS {return_type}
LANGUAGE plpythonu
AS $$
{func_definition}
return {internal_name}({args})
$$;
"""

    postgres_signature = ', '.join(
        '{name} {type}'.format(
            name=name,
            type=ibistype_sqltype[type_],
        )
        for name, type_ in zip(parameter_names, in_types)
    )
    return_type = ibistype_sqltype[out_type]
    # If function definition is indented extra,
    # Postgres UDF will fail with indentation error.
    # Also, need to remove decorators, because they
    # won't be defined in the UDF body.
    func_definition = remove_decorators(
        dedent(
            inspect.getsource(python_func)
        )
    )
    formatted_sql = template.format(
        replace=replace_text,
        schema_fragment=schema_fragment,
        name=internal_name,
        signature=postgres_signature,
        return_type=return_type,
        func_definition=func_definition,
        # for internal_name, need to make sure this works if passing
        # name parameter
        internal_name=python_func.__name__,
        args=', '.join(parameter_names)
    )
    conn.execute(formatted_sql)
    return existing_udf(
        name=internal_name,
        input_types=in_types,
        output_type=out_type,
        schema=schema,
        parameters=parameter_names
    )
