"""Alembic environment for the shared PostgreSQL instance."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from services.bff.infrastructure.models import Base
from services.content import models as content_models  # noqa: F401
from services.content.rag import models as rag_models  # noqa: F401
from services.customer_data.infrastructure import models as business_models  # noqa: F401
from services.quality import models as quality_models  # noqa: F401
from shared.web.settings import ServiceSettings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic 与应用服务读取同一个根目录 `.env`，不再保留第二套数据库地址。
database_url = ServiceSettings().database_url
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata


def include_name(name: str | None, type_: str, _parent_names: object) -> bool:
    """项目迁移管理 app/business/content；LangGraph 框架自行管理 checkpoint。"""

    return type_ != "schema" or name in {"app", "business", "content", "quality"}


def configure_context(connection=None, *, url: str | None = None) -> None:
    """Keep schema-aware migration options identical online and offline."""

    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        compare_type=True,
        compare_server_default=True,
        version_table="alembic_version",
        version_table_schema="app",
        literal_binds=url is not None,
        dialect_opts={"paramstyle": "named"} if url is not None else None,
    )


def run_migrations_offline() -> None:
    configure_context(url=config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        # Make the exported SQL executable against an empty PostgreSQL database.
        schema_comments = {
            "langgraph": "LangGraph checkpointer framework-owned tables",
            "app": "Identity, conversation, messages, runs, feedback and wrap-up facts",
            "business": "Customer profiles and service records owned by the adapter service",
            "content": "Authoritative intent, FAQ, SOP and RAG content versions",
            "quality": "Synchronous guard and asynchronous quality inspection results",
        }
        for schema, comment in schema_comments.items():
            context.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            context.execute(f"COMMENT ON SCHEMA {schema} IS '{comment}'")
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS app"))
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS business"))
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS content"))
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS quality"))
        connection.commit()
        configure_context(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
