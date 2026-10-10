import os
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema
from payflow.app import create_app
from payflow.config import Settings


@pytest.fixture
def database_url(tmp_path):
    postgres = os.getenv("PAYFLOW_TEST_DATABASE_URL")
    if not postgres:
        yield "sqlite:///" + str(tmp_path / "test.db")
        return
    url = make_url(postgres).set(drivername="postgresql+psycopg")
    admin = create_engine(url)
    schema = "payflow_test_" + uuid.uuid4().hex
    with admin.begin() as conn:
        conn.execute(CreateSchema(schema))
    try:
        yield url.update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    finally:
        with admin.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        admin.dispose()


@pytest.fixture
def client(database_url):
    app = create_app(Settings(database_url=database_url, auto_worker=False))
    with TestClient(app) as http:
        yield http


@pytest.fixture
def service(client):
    return client.app.state.service


@pytest.fixture
def order_data():
    return {"product": "Developer Pro", "customer": "Ganesh", "amount": 149900, "currency": "inr"}
