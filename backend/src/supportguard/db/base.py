from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Engine, MetaData, event, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


@event.listens_for(Engine, "connect")
def _attach_sqlite_control_schema(dbapi_connection: object, _record: object) -> None:
    """Give SQLite tests an isolated namespace for the PostgreSQL control schema."""
    driver = getattr(dbapi_connection, "driver_connection", dbapi_connection)
    if not type(driver).__module__.startswith(("sqlite3", "aiosqlite")):
        return
    connection: Any = dbapi_connection
    cursor = connection.cursor()
    try:
        cursor.execute("ATTACH DATABASE ':memory:' AS supportguard_control")
    finally:
        cursor.close()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
