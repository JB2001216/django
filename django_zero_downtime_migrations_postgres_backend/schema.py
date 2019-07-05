import re
import warnings
from contextlib import contextmanager

from django.conf import settings
from django.db.backends.ddl_references import Statement
from django.db.backends.postgresql.schema import (
    DatabaseSchemaEditor as PostgresDatabaseSchemaEditor
)


class TimeoutException(Exception):
    pass


class UnsafeOperationWarning(Warning):
    pass


class UnsafeOperationException(Exception):
    pass


class MultiStatementSQL(list):

    def __init__(self, obj, *args):
        if args:
            obj = [obj] + list(args)
        super().__init__(obj)

    def __str__(self):
        return '\n'.join(s.rstrip(';') + ';' for s in self)

    def __repr__(self):
        return str(self)

    def __mod__(self, other):
        return MultiStatementSQL(s % other for s in self)

    def format(self, *args, **kwargs):
        return MultiStatementSQL(s.format(*args, **kwargs) for s in self)


class PGLock:

    def __init__(self, sql, use_timeouts=False):
        self.sql = sql
        self.use_timeouts = use_timeouts

    def __str__(self):
        return self.sql

    def __repr__(self):
        return str(self)

    def __mod__(self, other):
        return self.__class__(self.sql % other, self.use_timeouts)

    def format(self, *args, **kwargs):
        return self.__class__(self.sql.format(*args, **kwargs), self.use_timeouts)


class PGAccessExclusive(PGLock):

    def __init__(self, sql, use_timeouts=True):
        super().__init__(sql, use_timeouts)


class PGShareUpdateExclusive(PGLock):
    pass


class DatabaseSchemaEditor(PostgresDatabaseSchemaEditor):

    sql_get_lock_timeout = "SELECT setting || unit FROM pg_settings WHERE name = 'lock_timeout'"
    sql_get_statement_timeout = "SELECT setting || unit FROM pg_settings WHERE name = 'statement_timeout'"
    sql_set_lock_timeout = "SET lock_timeout TO '%(lock_timeout)s'"
    sql_set_statement_timeout = "SET statement_timeout TO '%(statement_timeout)s'"

    sql_create_sequence = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_create_sequence, use_timeouts=False)
    sql_delete_sequence = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_delete_sequence, use_timeouts=False)
    sql_create_table = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_create_table, use_timeouts=False)
    sql_delete_table = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_delete_table, use_timeouts=False)

    sql_rename_table = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_rename_table)
    sql_retablespace_table = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_retablespace_table)

    sql_create_column = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_create_column)
    sql_alter_column = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_alter_column)
    sql_delete_column = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_delete_column)
    sql_rename_column = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_rename_column)

    sql_create_check = MultiStatementSQL(
        PGAccessExclusive("ALTER TABLE %(table)s ADD CONSTRAINT %(name)s CHECK (%(check)s) NOT VALID"),
        PGShareUpdateExclusive("ALTER TABLE %(table)s VALIDATE CONSTRAINT %(name)s"),
    )
    sql_delete_check = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_delete_check)

    sql_create_unique = MultiStatementSQL(
        PGShareUpdateExclusive("CREATE UNIQUE INDEX CONCURRENTLY %(name)s ON %(table)s (%(columns)s)"),
        PGAccessExclusive("ALTER TABLE %(table)s ADD CONSTRAINT %(name)s UNIQUE USING INDEX %(name)s"),
    )
    sql_delete_unique = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_delete_unique)

    sql_create_fk = MultiStatementSQL(
        PGAccessExclusive("ALTER TABLE %(table)s ADD CONSTRAINT %(name)s FOREIGN KEY (%(column)s) "
                          "REFERENCES %(to_table)s (%(to_column)s)%(deferrable)s NOT VALID"),
        PGShareUpdateExclusive("ALTER TABLE %(table)s VALIDATE CONSTRAINT %(name)s"),
    )
    sql_delete_fk = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_delete_fk)

    sql_create_pk = MultiStatementSQL(
        PGShareUpdateExclusive("CREATE UNIQUE INDEX CONCURRENTLY %(name)s ON %(table)s (%(columns)s)"),
        PGAccessExclusive("ALTER TABLE %(table)s ADD CONSTRAINT %(name)s PRIMARY KEY USING INDEX %(name)s"),
    )
    sql_delete_pk = PGAccessExclusive(PostgresDatabaseSchemaEditor.sql_delete_pk)

    sql_create_index = PGShareUpdateExclusive(
        "CREATE INDEX CONCURRENTLY %(name)s ON %(table)s%(using)s (%(columns)s)%(extra)s"
    )
    sql_create_varchar_index = PGShareUpdateExclusive(
        "CREATE INDEX CONCURRENTLY %(name)s ON %(table)s (%(columns)s varchar_pattern_ops)%(extra)s"
    )
    sql_create_text_index = PGShareUpdateExclusive(
        "CREATE INDEX CONCURRENTLY %(name)s ON %(table)s (%(columns)s text_pattern_ops)%(extra)s"
    )
    sql_delete_index = PGShareUpdateExclusive("DROP INDEX CONCURRENTLY IF EXISTS %(name)s")

    _sql_table_count = "SELECT reltuples FROM pg_class WHERE oid = '%(table)s'::regclass"
    _sql_check_notnull_constraint = (
        "SELECT conname FROM pg_constraint "
        "WHERE contype = 'c' AND conrelid = '%(table)s'::regclass AND consrc = '(%(columns)s IS NOT NULL)'"
    )
    _sql_column_not_null_compatible = MultiStatementSQL(
        PGAccessExclusive("ALTER TABLE %(table)s ADD CONSTRAINT %(name)s CHECK (%(column)s IS NOT NULL) NOT VALID"),
        PGShareUpdateExclusive("ALTER TABLE %(table)s VALIDATE CONSTRAINT %(name)s"),
    )

    _varchar_type_regexp = re.compile('^varchar\((?P<max_length>\d+)\)$')
    _numeric_type_regexp = re.compile('^numeric\((?P<precision>\d+), *(?P<scale>\d+)\)$')

    def __init__(self, connection, collect_sql=False, atomic=True):
        self.LOCK_TIMEOUT = getattr(settings, "ZERO_DOWNTIME_MIGRATIONS_LOCK_TIMEOUT", 0)
        self.STATEMENT_TIMEOUT = getattr(settings, "ZERO_DOWNTIME_MIGRATIONS_STATEMENT_TIMEOUT", 0)
        self.USE_NOT_NULL = getattr(settings, "ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL", None)
        self.RAISE_FOR_UNSAFE = getattr(settings, "ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE", False)
        super().__init__(connection, collect_sql=collect_sql, atomic=False)

    def execute(self, sql, params=()):
        statements = []
        if isinstance(sql, MultiStatementSQL):
            statements.extend(sql)
        elif isinstance(sql, Statement) and isinstance(sql.template, MultiStatementSQL):
            statements.extend(Statement(s, **sql.parts) for s in sql.template)
        else:
            statements.append(sql)
        for statement in statements:
            if isinstance(statement, PGLock):
                use_timeouts = statement.use_timeouts
                statement = statement.sql
            elif isinstance(statement, Statement) and isinstance(statement.template, PGLock):
                use_timeouts = statement.template.use_timeouts
                statement = Statement(statement.template.sql, **statement.parts)
            else:
                use_timeouts = False

            if use_timeouts:
                with self._respect_operation_timeout():
                    super().execute(statement, params)
            else:
                super().execute(statement, params)

    @contextmanager
    def _respect_operation_timeout(self):
        if self.collect_sql:
            previous_lock_timeout = '0ms'
            previous_statement_timeout = '0ms'
        else:
            with self.connection.cursor() as cursor:
                cursor.execute(self.sql_get_lock_timeout)
                previous_lock_timeout, = cursor.fetchone()
                cursor.execute(self.sql_get_statement_timeout)
                previous_statement_timeout, = cursor.fetchone()
        self.execute(self.sql_set_lock_timeout % {"lock_timeout": self.LOCK_TIMEOUT})
        self.execute(self.sql_set_statement_timeout % {"statement_timeout": self.STATEMENT_TIMEOUT})
        yield
        self.execute(self.sql_set_lock_timeout % {"lock_timeout": previous_lock_timeout})
        self.execute(self.sql_set_statement_timeout % {"statement_timeout": previous_statement_timeout})

    def alter_db_table(self, model, old_db_table, new_db_table):
        if self.RAISE_FOR_UNSAFE:
            raise UnsafeOperationException("ALTER TABLE RENAME is unsafe operation")
        else:
            warnings.warn(UnsafeOperationWarning("ALTER TABLE RENAME is unsafe operation"))
        return super().alter_db_table(model, old_db_table, new_db_table)

    def alter_db_tablespace(self, model, old_db_tablespace, new_db_tablespace):
        if self.RAISE_FOR_UNSAFE:
            raise UnsafeOperationException("ALTER TABLE SET TABLESPACE is unsafe operation")
        else:
            warnings.warn(UnsafeOperationWarning("ALTER TABLE SET TABLESPACE is unsafe operation"))
        return super().alter_db_tablespace(model, old_db_tablespace, new_db_tablespace)

    def _rename_field_sql(self, table, old_field, new_field, new_type):
        if self.RAISE_FOR_UNSAFE:
            raise UnsafeOperationException("ALTER TABLE RENAME COLUMN is unsafe operation")
        else:
            warnings.warn(UnsafeOperationWarning("ALTER TABLE RENAME COLUMN is unsafe operation"))
        return super()._rename_field_sql(table, old_field, new_field, new_type)

    def _get_table_rows_count(self, model):
        sql = self._sql_table_count % {"table": model._meta.db_table}
        with self.connection.cursor() as cursor:
            cursor.execute(sql)
            rows_count, = cursor.fetchone()
        return rows_count

    def _use_check_constraint_for_not_null(self, model):
        if self.USE_NOT_NULL is True:
            return False
        if self.USE_NOT_NULL is False:
            return True
        if isinstance(self.USE_NOT_NULL, int):
            rows_count = self._get_table_rows_count(model)
            if rows_count >= self.USE_NOT_NULL:
                return True
        return False

    def _add_column_not_null(self, model, field):
        if self.RAISE_FOR_UNSAFE and self.USE_NOT_NULL is None:
            raise UnsafeOperationException("ADD COLUMN NOT NULL is unsafe operation")
        if self._use_check_constraint_for_not_null(model):
            self.deferred_sql.append(self._sql_column_not_null_compatible % {
                "column": self.quote_name(field.column),
                "table": self.quote_name(model._meta.db_table),
                "name": self.quote_name("{}_{}_notnull".format(model._meta.db_table, field.column)),
            })
            return ""
        else:
            warnings.warn(UnsafeOperationWarning("ADD COLUMN NOT NULL is unsafe operation"))
            return " NOT NULL"

    def _add_column_primary_key(self, model, field):
        self.deferred_sql.append(self.sql_create_pk % {
            "table": self.quote_name(model._meta.db_table),
            "name": self.quote_name(self._create_index_name(model._meta.db_table, [field.column], suffix="_pk")),
            "columns": self.quote_name(field.column),
        })
        return ""

    def _add_column_unique(self, model, field):
        self.deferred_sql.append(self._create_unique_sql(model, [field.column]))
        return ""

    def column_sql(self, model, field, include_default=False):
        """
        Take a field and return its column definition.
        The field must already have had set_attributes_from_name() called.
        """
        if not include_default:
            return super().column_sql(model, field, include_default)

        # Get the column's type and use that as the basis of the SQL
        db_params = field.db_parameters(connection=self.connection)
        sql = db_params['type']
        params = []
        # Check for fields that aren't actually columns (e.g. M2M)
        if sql is None:
            return None, None
        # Work out nullability
        null = field.null
        # If we were told to include a default value, do so
        include_default = include_default and not self.skip_default(field)
        if include_default:
            default_value = self.effective_default(field)
            if default_value is not None:
                if self.connection.features.requires_literal_defaults:
                    # Some databases can't take defaults as a parameter (oracle)
                    # If this is the case, the individual schema backend should
                    # implement prepare_default
                    sql += " DEFAULT %s" % self.prepare_default(default_value)
                else:
                    sql += " DEFAULT %s"
                    params += [default_value]
        # Oracle treats the empty string ('') as null, so coerce the null
        # option whenever '' is a possible value.
        if (field.empty_strings_allowed and not field.primary_key and
                self.connection.features.interprets_empty_strings_as_nulls):
            null = True
        if null and not self.connection.features.implied_column_null:
            sql += " NULL"
        elif not null:
            sql += self._add_column_not_null(model, field)
        # Primary key/unique outputs
        if field.primary_key:
            sql += self._add_column_primary_key(model, field)
        elif field.unique:
            sql += self._add_column_unique(model, field)
        # Optionally add the tablespace if it's an implicitly indexed column
        tablespace = field.db_tablespace or model._meta.db_tablespace
        if tablespace and self.connection.features.supports_tablespaces and field.unique:
            sql += " %s" % self.connection.ops.tablespace_sql(tablespace, inline=True)
        # Return the sql
        return sql, params

    def _alter_column_set_not_null(self, model, new_field):
        if self.RAISE_FOR_UNSAFE and self.USE_NOT_NULL is None:
            raise UnsafeOperationException("ALTER COLUMN NOT NULL is unsafe operation")
        if self._use_check_constraint_for_not_null(model):
            self.deferred_sql.append(self._sql_column_not_null_compatible % {
                "column": self.quote_name(new_field.column),
                "table": self.quote_name(model._meta.db_table),
                "name": self.quote_name("{}_{}_notnull".format(model._meta.db_table, new_field.column)),
            })
            return None
        else:
            warnings.warn(UnsafeOperationWarning("ALTER COLUMN NOT NULL is unsafe operation"))
            return self.sql_alter_column_not_null % {
                "column": self.quote_name(new_field.column),
            }, []

    def _alter_column_drop_not_null(self, model, new_field):
        with self.connection.cursor() as cursor:
            cursor.execute(self._sql_check_notnull_constraint % {
                "table": self.quote_name(model._meta.db_table),
                "columns": self.quote_name(new_field.column),
            })
            result = cursor.fetchone()
        if result:
            constraint_name, = result
            self.deferred_sql.append(self.sql_delete_check % {
                "table": self.quote_name(model._meta.db_table),
                "name": constraint_name,
            })
        else:
            return self.sql_alter_column_null % {
                "column": self.quote_name(new_field.column),
            }, []

    def _alter_column_null_sql(self, model, old_field, new_field):
        if new_field.null:
            return self._alter_column_drop_not_null(model, new_field)
        else:
            return self._alter_column_set_not_null(model, new_field)

    def _immediate_type_cast(self, old_type, new_type):
        old_type_varchar_match = self._varchar_type_regexp.match(old_type)
        if old_type_varchar_match:
            if new_type == "text":
                return True
            new_type_varchar_match = self._varchar_type_regexp.match(new_type)
            if new_type_varchar_match:
                old_type_max_length = int(old_type_varchar_match.group("max_length"))
                new_type_max_length = int(new_type_varchar_match.group("max_length"))
                if new_type_max_length >= old_type_max_length:
                    return True
                else:
                    return False
        old_type_numeric_match = self._numeric_type_regexp.match(old_type)
        if old_type_numeric_match:
            new_type_numeric_match = self._numeric_type_regexp.match(new_type)
            old_type_precision = int(old_type_numeric_match.group("precision"))
            old_type_scale = int(old_type_numeric_match.group("scale"))
            new_type_precision = int(new_type_numeric_match.group("precision"))
            new_type_scale = int(new_type_numeric_match.group("scale"))
            if new_type_precision >= old_type_precision and new_type_scale == old_type_scale:
                return True
            else:
                return False
        return False

    def _alter_column_type_sql(self, model, old_field, new_field, new_type):
        old_db_params = old_field.db_parameters(connection=self.connection)
        old_type = old_db_params["type"]
        if not self._immediate_type_cast(old_type, new_type):
            if self.RAISE_FOR_UNSAFE:
                raise UnsafeOperationException("ALTER COLUMN TYPE is unsafe operation")
            else:
                warnings.warn(UnsafeOperationWarning("ALTER COLUMN TYPE is unsafe operation"))
        return super()._alter_column_type_sql(model, old_field, new_field, new_type)
