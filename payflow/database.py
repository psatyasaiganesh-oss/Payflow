from contextlib import contextmanager
from sqlalchemy import (
    CheckConstraint, Column, Float, Integer, MetaData, String, Table, Text,
    create_engine, event, select,
)

metadata = MetaData()
orders = Table("orders", metadata,
    Column("id", String(64), primary_key=True),
    Column("idempotency_key", String(200), nullable=False, unique=True),
    Column("request_hash", String(64), nullable=False),
    Column("product", String(100), nullable=False),
    Column("customer", String(120), nullable=False),
    Column("amount", Integer, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("status", String(32), nullable=False, default="pending"),
    Column("payment_id", String(80), unique=True),
    Column("refunded", Integer, nullable=False, default=0),
    Column("fulfillment_count", Integer, nullable=False, default=0),
    Column("last_error", String(240)),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint("amount >= 50"),
    CheckConstraint("refunded >= 0 AND refunded <= amount"),
    CheckConstraint("fulfillment_count >= 0 AND fulfillment_count <= 1"),
)
payments = Table("mock_payments", metadata,
    Column("id", String(80), primary_key=True),
    Column("order_id", String(64), nullable=False, unique=True),
    Column("amount", Integer, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("status", String(32), nullable=False),
    Column("refunded", Integer, nullable=False, default=0),
)
refunds = Table("refund_requests", metadata,
    Column("idempotency_key", String(200), primary_key=True),
    Column("order_id", String(64), nullable=False),
    Column("amount", Integer, nullable=False),
    Column("provider_id", String(80)),
    Column("created_at", Float, nullable=False),
)
inbox = Table("events", metadata,
    Column("id", String(100), primary_key=True),
    Column("payment_id", String(80), nullable=False),
    Column("type", String(100), nullable=False),
    Column("payload", Text, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("status", String(32), nullable=False, default="pending", index=True),
    Column("deliveries", Integer, nullable=False, default=1),
    Column("attempts", Integer, nullable=False, default=0),
    Column("available_at", Float, nullable=False),
    Column("lease_until", Float),
    Column("lease_token", String(64)),
    Column("last_error", String(240)),
    Column("created_at", Float, nullable=False),
    Column("processed_at", Float),
)
ledger = Table("ledger", metadata,
    Column("id", String(64), primary_key=True),
    Column("effect_key", String(140), nullable=False, unique=True),
    Column("order_id", String(64), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("amount", Integer, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("created_at", Float, nullable=False),
)
audit = Table("audit", metadata,
    Column("id", String(64), primary_key=True),
    Column("order_id", String(64)),
    Column("kind", String(64), nullable=False),
    Column("message", String(300), nullable=False),
    Column("created_at", Float, nullable=False),
)
control = Table("control", metadata,
    Column("id", Integer, primary_key=True),
    Column("worker_paused", Integer, nullable=False, default=0),
    Column("fail_next", Integer, nullable=False, default=0),
)
scenario_runs = Table("scenario_runs", metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(64), nullable=False),
    Column("passed", Integer, nullable=False),
    Column("details", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)


class Database:
    def __init__(self, url: str):
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        args = {"check_same_thread": False, "timeout": 20} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=args, pool_pre_ping=True)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def configure(connection, _):
                connection.isolation_level = None
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=20000")
            @event.listens_for(self.engine, "begin")
            def begin(connection):
                # SQLite has no SELECT FOR UPDATE; serialize short transactions.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            if conn.execute(select(control)).first() is None:
                conn.execute(control.insert().values(id=1, worker_paused=0, fail_next=0))

    @contextmanager
    def transaction(self):
        with self.engine.begin() as conn:
            yield conn

    def close(self):
        self.engine.dispose()
