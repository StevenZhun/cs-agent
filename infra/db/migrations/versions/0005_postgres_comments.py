"""Document all existing PostgreSQL tables and columns in the system catalog."""

from collections.abc import Sequence

from alembic import op

from shared.postgres_comments import TABLE_COMMENTS

revision: str = "0005_postgres_comments"
down_revision: str | None = "0004_app_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    for table_name, (table_comment, columns) in TABLE_COMMENTS.items():
        schema, name = table_name.split(".", 1)
        op.execute(f"COMMENT ON TABLE {schema}.{name} IS {_quoted(table_comment)}")
        for column_name, comment in columns.items():
            op.execute(f"COMMENT ON COLUMN {schema}.{name}.{column_name} IS {_quoted(comment)}")


def downgrade() -> None:
    for table_name, (_, columns) in TABLE_COMMENTS.items():
        schema, name = table_name.split(".", 1)
        for column_name in columns:
            op.execute(f"COMMENT ON COLUMN {schema}.{name}.{column_name} IS NULL")
        op.execute(f"COMMENT ON TABLE {schema}.{name} IS NULL")
