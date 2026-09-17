# type: ignore

import pytest

import mycli.sqlexecute
from test.utils import CHARACTER_SET, DATABASE, HOST, PASSWORD, PORT, USER, create_db, db_connection


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
        character_set=CHARACTER_SET,
        local_infile=False,
        ssl=None,
    )


@pytest.fixture(autouse=True)
def isolate_special_command_registry():
    """Keep one test's registered special commands out of the next test's registry.

    The registries are module-level, and constructing a client registers into
    them -- including a command per favorite query, read from the developer's
    own ~/.myclirc when a test builds MyCli() with no config path. Without this,
    what a test sees depends on which tests ran before it.
    """
    from mycli.packages.special import main as special_main
    from mycli.packages.special.favoritequeries import FavoriteQueries

    commands = special_main.COMMANDS.copy()
    case_sensitive = special_main.CASE_SENSITIVE_COMMANDS.copy()
    case_insensitive = special_main.CASE_INSENSITIVE_COMMANDS.copy()
    favorites = getattr(FavoriteQueries, 'instance', None)
    try:
        yield
    finally:
        special_main.COMMANDS.clear()
        special_main.COMMANDS.update(commands)
        special_main.CASE_SENSITIVE_COMMANDS.clear()
        special_main.CASE_SENSITIVE_COMMANDS.update(case_sensitive)
        special_main.CASE_INSENSITIVE_COMMANDS.clear()
        special_main.CASE_INSENSITIVE_COMMANDS.update(case_insensitive)
        if favorites is None:
            if hasattr(FavoriteQueries, 'instance'):
                del FavoriteQueries.instance
        else:
            FavoriteQueries.instance = favorites
