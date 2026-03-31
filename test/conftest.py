# type: ignore

import pytest
from prompt_toolkit.completion import Completion

import mycli.sqlexecute
from test.utils import CHARSET, DATABASE, HOST, PASSWORD, PORT, SSH_HOST, SSH_PORT, SSH_USER, USER, create_db, db_connection


def _completion_eq(self, other):
    """Compare Completion objects by text and start_position only, ignoring display_meta."""
    if not isinstance(other, Completion):
        return NotImplemented
    return self.text == other.text and self.start_position == other.start_position


Completion.__eq__ = _completion_eq
Completion.__hash__ = lambda self: hash((self.text, self.start_position))


@pytest.fixture(scope="function")
def connection():
    create_db(DATABASE)
    connection = db_connection(DATABASE)
    yield connection

    connection.close()


@pytest.fixture
def cursor(connection):
    with connection.cursor() as cur:
        return cur


@pytest.fixture
def executor(connection):
    return mycli.sqlexecute.SQLExecute(
        database=DATABASE,
        user=USER,
        host=HOST,
        password=PASSWORD,
        port=PORT,
        socket=None,
        charset=CHARSET,
        local_infile=False,
        ssl=None,
        ssh_user=SSH_USER,
        ssh_host=SSH_HOST,
        ssh_port=SSH_PORT,
        ssh_password=None,
        ssh_key_filename=None,
    )
