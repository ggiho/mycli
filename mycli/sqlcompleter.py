from __future__ import annotations

from collections import Counter
from enum import IntEnum
import functools
import logging
import re
from typing import Any, Collection, Generator, Iterable, Literal

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.completion.base import Document
from pygments.lexers._mysql_builtins import MYSQL_DATATYPES, MYSQL_FUNCTIONS, MYSQL_KEYWORDS
import rapidfuzz

from mycli.packages.completion_engine import extract_cte_columns, suggest_type
from mycli.packages.filepaths import complete_path, parse_path, suggest_path
from mycli.packages.parseutils import last_word
from mycli.packages.special import llm
from mycli.packages.special.favoritequeries import FavoriteQueries
from mycli.packages.special.main import COMMANDS as SPECIAL_COMMANDS

# Pre-compiled regex patterns for hot path operations
_CASE_CHANGE_RE = re.compile("(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_DIGIT_START_RE = re.compile(r'^[\d\.]')
_NAME_PATTERN_RE = re.compile(r"^[_a-zA-Z][_a-zA-Z0-9\$]*$")

_logger = logging.getLogger(__name__)

# SQL snippet templates: trigger -> (expansion, description)
# ${1}, ${2} etc. are placeholder markers (cursor stops)
SNIPPET_TEMPLATES: dict[str, tuple[str, str]] = {
    "sel": ("SELECT ${1:columns} FROM ${2:table} WHERE ${3:condition}", "SELECT ... FROM ... WHERE"),
    "selc": ("SELECT COUNT(*) FROM ${1:table} WHERE ${2:condition}", "SELECT COUNT(*)"),
    "seld": ("SELECT DISTINCT ${1:columns} FROM ${2:table}", "SELECT DISTINCT"),
    "selj": ("SELECT ${1:columns} FROM ${2:table1} t1\nJOIN ${3:table2} t2 ON t1.${4:id} = t2.${5:id}", "SELECT with JOIN"),
    "ins": ("INSERT INTO ${1:table} (${2:columns}) VALUES (${3:values})", "INSERT INTO"),
    "upd": ("UPDATE ${1:table} SET ${2:column} = ${3:value} WHERE ${4:condition}", "UPDATE ... SET"),
    "del": ("DELETE FROM ${1:table} WHERE ${2:condition}", "DELETE FROM"),
    "crt": ("CREATE TABLE ${1:table_name} (\n  ${2:id} INT PRIMARY KEY AUTO_INCREMENT,\n  ${3:column} VARCHAR(255)\n)", "CREATE TABLE"),
    "alt": ("ALTER TABLE ${1:table} ADD COLUMN ${2:column} ${3:type}", "ALTER TABLE ADD"),
    "idx": ("CREATE INDEX ${1:idx_name} ON ${2:table} (${3:columns})", "CREATE INDEX"),
    "grp": ("SELECT ${1:column}, COUNT(*) AS cnt FROM ${2:table} GROUP BY ${1:column} ORDER BY cnt DESC", "GROUP BY with COUNT"),
    "cte": ("WITH ${1:cte_name} AS (\n  SELECT ${2:columns} FROM ${3:table}\n)\nSELECT * FROM ${1:cte_name}", "WITH CTE"),
    "exist": ("SELECT * FROM ${1:table} WHERE EXISTS (\n  SELECT 1 FROM ${2:other} WHERE ${3:condition}\n)", "EXISTS subquery"),
    "case": ("CASE\n  WHEN ${1:condition} THEN ${2:result}\n  ELSE ${3:default}\nEND", "CASE expression"),
}

# Pre-strip placeholders for display in completion text
_SNIPPET_DISPLAY_RE = re.compile(r'\$\{(\d+)(?::([^}]*))?\}')


def _snippet_preview(template: str) -> str:
    """Convert template placeholders to readable preview text."""
    return _SNIPPET_DISPLAY_RE.sub(lambda m: m.group(2) or '...', template)


# Common JSON path patterns for autocompletion
JSON_PATH_SUGGESTIONS = [
    ("'$'", "JSON root"),
    ("'$.'", "object property accessor"),
    ("'$[*]'", "all array elements"),
    ("'$[0]'", "first array element"),
    ("'$.key'", "property access example"),
    ("'$**.key'", "recursive descent"),
]


class Fuzziness(IntEnum):
    PERFECT = 0
    REGEX = 1
    UNDER_WORDS = 2
    CAMEL_CASE = 3
    RAPIDFUZZ = 4


class SQLCompleter(Completer):
    favorite_keywords = [
        'SELECT',
        'FROM',
        'WHERE',
        'UPDATE',
        'DELETE FROM',
        'GROUP BY',
        'ORDER BY',
        'JOIN',
        'INSERT INTO',
        'LIKE',
        'LIMIT',
    ]
    keywords_raw = [
        x.upper()
        for x in favorite_keywords
        + list(MYSQL_DATATYPES)
        + list(MYSQL_KEYWORDS)
        + ['ALTER TABLE', 'CHANGE MASTER TO', 'CHARACTER SET', 'FOREIGN KEY']
    ]
    keywords_d = dict.fromkeys(keywords_raw)
    for x in SPECIAL_COMMANDS:
        if x.upper() in keywords_d:
            del keywords_d[x.upper()]
    keywords = list(keywords_d)

    tidb_keywords = [
        "SELECT",
        "FROM",
        "WHERE",
        "DELETE FROM",
        "UPDATE",
        "GROUP BY",
        "JOIN",
        "INSERT INTO",
        "LIKE",
        "LIMIT",
        "ACCOUNT",
        "ACTION",
        "ADD",
        "ADDDATE",
        "ADMIN",
        "ADVISE",
        "AFTER",
        "AGAINST",
        "AGO",
        "ALGORITHM",
        "ALL",
        "ALTER",
        "ALWAYS",
        "ANALYZE",
        "AND",
        "ANY",
        "APPROX_COUNT_DISTINCT",
        "APPROX_PERCENTILE",
        "AS",
        "ASC",
        "ASCII",
        "ATTRIBUTES",
        "AUTO_ID_CACHE",
        "AUTO_INCREMENT",
        "AUTO_RANDOM",
        "AUTO_RANDOM_BASE",
        "AVG",
        "AVG_ROW_LENGTH",
        "BACKEND",
        "BACKUP",
        "BACKUPS",
        "BATCH",
        "BEGIN",
        "BERNOULLI",
        "BETWEEN",
        "BIGINT",
        "BINARY",
        "BINDING",
        "BINDINGS",
        "BINDING_CACHE",
        "BINLOG",
        "BIT",
        "BIT_AND",
        "BIT_OR",
        "BIT_XOR",
        "BLOB",
        "BLOCK",
        "BOOL",
        "BOOLEAN",
        "BOTH",
        "BOUND",
        "BRIEF",
        "BTREE",
        "BUCKETS",
        "BUILTINS",
        "BY",
        "BYTE",
        "CACHE",
        "CALL",
        "CANCEL",
        "CAPTURE",
        "CARDINALITY",
        "CASCADE",
        "CASCADED",
        "CASE",
        "CAST",
        "CAUSAL",
        "CHAIN",
        "CHANGE",
        "CHAR",
        "CHARACTER",
        "CHARSET",
        "CHECK",
        "CHECKPOINT",
        "CHECKSUM",
        "CIPHER",
        "CLEANUP",
        "CLIENT",
        "CLIENT_ERRORS_SUMMARY",
        "CLUSTERED",
        "CMSKETCH",
        "COALESCE",
        "COLLATE",
        "COLLATION",
        "COLUMN",
        "COLUMNS",
        "COLUMN_FORMAT",
        "COLUMN_STATS_USAGE",
        "COMMENT",
        "COMMIT",
        "COMMITTED",
        "COMPACT",
        "COMPRESSED",
        "COMPRESSION",
        "CONCURRENCY",
        "CONFIG",
        "CONNECTION",
        "CONSISTENCY",
        "CONSISTENT",
        "CONSTRAINT",
        "CONSTRAINTS",
        "CONTEXT",
        "CONVERT",
        "COPY",
        "CORRELATION",
        "CPU",
        "CREATE",
        "CROSS",
        "CSV_BACKSLASH_ESCAPE",
        "CSV_DELIMITER",
        "CSV_HEADER",
        "CSV_NOT_NULL",
        "CSV_NULL",
        "CSV_SEPARATOR",
        "CSV_TRIM_LAST_SEPARATORS",
        "CUME_DIST",
        "CURRENT",
        "CURRENT_DATE",
        "CURRENT_ROLE",
        "CURRENT_TIME",
        "CURRENT_TIMESTAMP",
        "CURRENT_USER",
        "CURTIME",
        "CYCLE",
        "DATA",
        "DATABASE",
        "DATABASES",
        "DATE",
        "DATETIME",
        "DATE_ADD",
        "DATE_SUB",
        "DAY",
        "DAY_HOUR",
        "DAY_MICROSECOND",
        "DAY_MINUTE",
        "DAY_SECOND",
        "DDL",
        "DEALLOCATE",
        "DECIMAL",
        "DEFAULT",
        "DEFINER",
        "DELAYED",
        "DELAY_KEY_WRITE",
        "DENSE_RANK",
        "DEPENDENCY",
        "DEPTH",
        "DESC",
        "DESCRIBE",
        "DIRECTORY",
        "DISABLE",
        "DISABLED",
        "DISCARD",
        "DISK",
        "DISTINCT",
        "DISTINCTROW",
        "DIV",
        "DO",
        "DOT",
        "DOUBLE",
        "DRAINER",
        "DROP",
        "DRY",
        "DUAL",
        "DUMP",
        "DUPLICATE",
        "DYNAMIC",
        "ELSE",
        "ENABLE",
        "ENABLED",
        "ENCLOSED",
        "ENCRYPTION",
        "END",
        "ENFORCED",
        "ENGINE",
        "ENGINES",
        "ENUM",
        "ERROR",
        "ERRORS",
        "ESCAPE",
        "ESCAPED",
        "EVENT",
        "EVENTS",
        "EVOLVE",
        "EXACT",
        "EXCEPT",
        "EXCHANGE",
        "EXCLUSIVE",
        "EXECUTE",
        "EXISTS",
        "EXPANSION",
        "EXPIRE",
        "EXPLAIN",
        "EXPR_PUSHDOWN_BLACKLIST",
        "EXTENDED",
        "EXTRACT",
        "FALSE",
        "FAST",
        "FAULTS",
        "FETCH",
        "FIELDS",
        "FILE",
        "FIRST",
        "FIRST_VALUE",
        "FIXED",
        "FLASHBACK",
        "FLOAT",
        "FLUSH",
        "FOLLOWER",
        "FOLLOWERS",
        "FOLLOWER_CONSTRAINTS",
        "FOLLOWING",
        "FOR",
        "FORCE",
        "FOREIGN",
        "FORMAT",
        "FULL",
        "FULLTEXT",
        "FUNCTION",
        "GENERAL",
        "GENERATED",
        "GET_FORMAT",
        "GLOBAL",
        "GRANT",
        "GRANTS",
        "GROUPS",
        "GROUP_CONCAT",
        "HASH",
        "HAVING",
        "HELP",
        "HIGH_PRIORITY",
        "HISTOGRAM",
        "HISTOGRAMS_IN_FLIGHT",
        "HISTORY",
        "HOSTS",
        "HOUR",
        "HOUR_MICROSECOND",
        "HOUR_MINUTE",
        "HOUR_SECOND",
        "IDENTIFIED",
        "IF",
        "IGNORE",
        "IMPORT",
        "IMPORTS",
        "IN",
        "INCREMENT",
        "INCREMENTAL",
        "INDEX",
        "INDEXES",
        "INFILE",
        "INNER",
        "INPLACE",
        "INSERT_METHOD",
        "INSTANCE",
        "INSTANT",
        "INT",
        "INT1",
        "INT2",
        "INT3",
        "INT4",
        "INT8",
        "INTEGER",
        "INTERNAL",
        "INTERSECT",
        "INTERVAL",
        "INTO",
        "INVISIBLE",
        "INVOKER",
        "IO",
        "IPC",
        "IS",
        "ISOLATION",
        "ISSUER",
        "JOB",
        "JOBS",
        "JSON",
        "JSON_ARRAYAGG",
        "JSON_OBJECTAGG",
        "KEY",
        "KEYS",
        "KEY_BLOCK_SIZE",
        "KILL",
        "LABELS",
        "LAG",
        "LANGUAGE",
        "LAST",
        "LASTVAL",
        "LAST_BACKUP",
        "LAST_VALUE",
        "LEAD",
        "LEADER",
        "LEADER_CONSTRAINTS",
        "LEADING",
        "LEARNER",
        "LEARNERS",
        "LEARNER_CONSTRAINTS",
        "LEFT",
        "LESS",
        "LEVEL",
        "LINEAR",
        "LINES",
        "LIST",
        "LOAD",
        "LOCAL",
        "LOCALTIME",
        "LOCALTIMESTAMP",
        "LOCATION",
        "LOCK",
        "LOCKED",
        "LOGS",
        "LONG",
        "LONGBLOB",
        "LONGTEXT",
        "LOW_PRIORITY",
        "MASTER",
        "MATCH",
        "MAX",
        "MAXVALUE",
        "MAX_CONNECTIONS_PER_HOUR",
        "MAX_IDXNUM",
        "MAX_MINUTES",
        "MAX_QUERIES_PER_HOUR",
        "MAX_ROWS",
        "MAX_UPDATES_PER_HOUR",
        "MAX_USER_CONNECTIONS",
        "MB",
        "MEDIUMBLOB",
        "MEDIUMINT",
        "MEDIUMTEXT",
        "MEMORY",
        "MERGE",
        "MICROSECOND",
        "MIN",
        "MINUTE",
        "MINUTE_MICROSECOND",
        "MINUTE_SECOND",
        "MINVALUE",
        "MIN_ROWS",
        "MOD",
        "MODE",
        "MODIFY",
        "MONTH",
        "NAMES",
        "NATIONAL",
        "NATURAL",
        "NCHAR",
        "NEVER",
        "NEXT",
        "NEXTVAL",
        "NEXT_ROW_ID",
        "NO",
        "NOCACHE",
        "NOCYCLE",
        "NODEGROUP",
        "NODE_ID",
        "NODE_STATE",
        "NOMAXVALUE",
        "NOMINVALUE",
        "NONCLUSTERED",
        "NONE",
        "NORMAL",
        "NOT",
        "NOW",
        "NOWAIT",
        "NO_WRITE_TO_BINLOG",
        "NTH_VALUE",
        "NTILE",
        "NULL",
        "NULLS",
        "NUMERIC",
        "NVARCHAR",
        "OF",
        "OFF",
        "OFFSET",
        "ON",
        "ONLINE",
        "ONLY",
        "ON_DUPLICATE",
        "OPEN",
        "OPTIMISTIC",
        "OPTIMIZE",
        "OPTION",
        "OPTIONAL",
        "OPTIONALLY",
        "OPT_RULE_BLACKLIST",
        "OR",
        "ORDER",
        "OUTER",
        "OUTFILE",
        "OVER",
        "PACK_KEYS",
        "PAGE",
        "PARSER",
        "PARTIAL",
        "PARTITION",
        "PARTITIONING",
        "PARTITIONS",
        "PASSWORD",
        "PERCENT",
        "PERCENT_RANK",
        "PER_DB",
        "PER_TABLE",
        "PESSIMISTIC",
        "PLACEMENT",
        "PLAN",
        "PLAN_CACHE",
        "PLUGINS",
        "POLICY",
        "POSITION",
        "PRECEDING",
        "PRECISION",
        "PREDICATE",
        "PREPARE",
        "PRESERVE",
        "PRE_SPLIT_REGIONS",
        "PRIMARY",
        "PRIMARY_REGION",
        "PRIVILEGES",
        "PROCEDURE",
        "PROCESS",
        "PROCESSLIST",
        "PROFILE",
        "PROFILES",
        "PROXY",
        "PUMP",
        "PURGE",
        "QUARTER",
        "QUERIES",
        "QUERY",
        "QUICK",
        "RANGE",
        "RANK",
        "RATE_LIMIT",
        "READ",
        "REAL",
        "REBUILD",
        "RECENT",
        "RECOVER",
        "RECURSIVE",
        "REDUNDANT",
        "REFERENCES",
        "REGEXP",
        "REGION",
        "REGIONS",
        "RELEASE",
        "RELOAD",
        "REMOVE",
        "RENAME",
        "REORGANIZE",
        "REPAIR",
        "REPEAT",
        "REPEATABLE",
        "REPLACE",
        "REPLAYER",
        "REPLICA",
        "REPLICAS",
        "REPLICATION",
        "REQUIRE",
        "REQUIRED",
        "RESET",
        "RESPECT",
        "RESTART",
        "RESTORE",
        "RESTORES",
        "RESTRICT",
        "RESUME",
        "REVERSE",
        "REVOKE",
        "RIGHT",
        "RLIKE",
        "ROLE",
        "ROLLBACK",
        "ROUTINE",
        "ROW",
        "ROWS",
        "ROW_COUNT",
        "ROW_FORMAT",
        "ROW_NUMBER",
        "RTREE",
        "RUN",
        "RUNNING",
        "S3",
        "SAMPLERATE",
        "SAMPLES",
        "SAN",
        "SAVEPOINT",
        "SCHEDULE",
        "SECOND",
        "SECONDARY_ENGINE",
        "SECONDARY_LOAD",
        "SECONDARY_UNLOAD",
        "SECOND_MICROSECOND",
        "SECURITY",
        "SEND_CREDENTIALS_TO_TIKV",
        "SEPARATOR",
        "SEQUENCE",
        "SERIAL",
        "SERIALIZABLE",
        "SESSION",
        "SESSION_STATES",
        "SET",
        "SETVAL",
        "SHARD_ROW_ID_BITS",
        "SHARE",
        "SHARED",
        "SHOW",
        "SHUTDOWN",
        "SIGNED",
        "SIMPLE",
        "SKIP",
        "SKIP_SCHEMA_FILES",
        "SLAVE",
        "SLOW",
        "SMALLINT",
        "SNAPSHOT",
        "SOME",
        "SOURCE",
        "SPATIAL",
        "SPLIT",
        "SQL",
        "SQL_BIG_RESULT",
        "SQL_BUFFER_RESULT",
        "SQL_CACHE",
        "SQL_CALC_FOUND_ROWS",
        "SQL_NO_CACHE",
        "SQL_SMALL_RESULT",
        "SQL_TSI_DAY",
        "SQL_TSI_HOUR",
        "SQL_TSI_MINUTE",
        "SQL_TSI_MONTH",
        "SQL_TSI_QUARTER",
        "SQL_TSI_SECOND",
        "SQL_TSI_WEEK",
        "SQL_TSI_YEAR",
        "SSL",
        "STALENESS",
        "START",
        "STARTING",
        "STATISTICS",
        "STATS",
        "STATS_AUTO_RECALC",
        "STATS_BUCKETS",
        "STATS_COL_CHOICE",
        "STATS_COL_LIST",
        "STATS_EXTENDED",
        "STATS_HEALTHY",
        "STATS_HISTOGRAMS",
        "STATS_META",
        "STATS_OPTIONS",
        "STATS_PERSISTENT",
        "STATS_SAMPLE_PAGES",
        "STATS_SAMPLE_RATE",
        "STATS_TOPN",
        "STATUS",
        "STD",
        "STDDEV",
        "STDDEV_POP",
        "STDDEV_SAMP",
        "STOP",
        "STORAGE",
        "STORED",
        "STRAIGHT_JOIN",
        "STRICT",
        "STRICT_FORMAT",
        "STRONG",
        "SUBDATE",
        "SUBJECT",
        "SUBPARTITION",
        "SUBPARTITIONS",
        "SUBSTRING",
        "SUM",
        "SUPER",
        "SWAPS",
        "SWITCHES",
        "SYSTEM",
        "SYSTEM_TIME",
        "TABLE",
        "TABLES",
        "TABLESAMPLE",
        "TABLESPACE",
        "TABLE_CHECKSUM",
        "TARGET",
        "TELEMETRY",
        "TELEMETRY_ID",
        "TEMPORARY",
        "TEMPTABLE",
        "TERMINATED",
        "TEXT",
        "THAN",
        "THEN",
        "TIDB",
        "TIFLASH",
        "TIKV_IMPORTER",
        "TIME",
        "TIMESTAMP",
        "TIMESTAMPADD",
        "TIMESTAMPDIFF",
        "TINYBLOB",
        "TINYINT",
        "TINYTEXT",
        "TLS",
        "TO",
        "TOKUDB_DEFAULT",
        "TOKUDB_FAST",
        "TOKUDB_LZMA",
        "TOKUDB_QUICKLZ",
        "TOKUDB_SMALL",
        "TOKUDB_SNAPPY",
        "TOKUDB_UNCOMPRESSED",
        "TOKUDB_ZLIB",
        "TOP",
        "TOPN",
        "TRACE",
        "TRADITIONAL",
        "TRAILING",
        "TRANSACTION",
        "TRIGGER",
        "TRIGGERS",
        "TRIM",
        "TRUE",
        "TRUE_CARD_COST",
        "TRUNCATE",
        "TYPE",
        "UNBOUNDED",
        "UNCOMMITTED",
        "UNDEFINED",
        "UNICODE",
        "UNION",
        "UNIQUE",
        "UNKNOWN",
        "UNLOCK",
        "UNSIGNED",
        "USAGE",
        "USE",
        "USER",
        "USING",
        "UTC_DATE",
        "UTC_TIME",
        "UTC_TIMESTAMP",
        "VALIDATION",
        "VALUE",
        "VALUES",
        "VARBINARY",
        "VARCHAR",
        "VARCHARACTER",
        "VARIABLES",
        "VARIANCE",
        "VARYING",
        "VAR_POP",
        "VAR_SAMP",
        "VERBOSE",
        "VIEW",
        "VIRTUAL",
        "VISIBLE",
        "VOTER",
        "VOTERS",
        "VOTER_CONSTRAINTS",
        "WAIT",
        "WARNINGS",
        "WEEK",
        "WEIGHT_STRING",
        "WHEN",
        "WIDTH",
        "WINDOW",
        "WITH",
        "WITHOUT",
        "WRITE",
        "X509",
        "XOR",
        "YEAR",
        "YEAR_MONTH",
        "ZEROFILL",
    ]

    functions = [x.upper() for x in MYSQL_FUNCTIONS]

    # https://docs.pingcap.com/tidb/dev/tidb-functions
    tidb_functions = [
        "TIDB_BOUNDED_STALENESS",
        "TIDB_DECODE_KEY",
        "TIDB_DECODE_PLAN",
        "TIDB_IS_DDL_OWNER",
        "TIDB_PARSE_TSO",
        "TIDB_VERSION",
        "TIDB_DECODE_SQL_DIGESTS",
        "VITESS_HASH",
        "TIDB_SHARD",
    ]

    show_items: list[Completion] = []

    change_items = [
        "MASTER_BIND",
        "MASTER_HOST",
        "MASTER_USER",
        "MASTER_PASSWORD",
        "MASTER_PORT",
        "MASTER_CONNECT_RETRY",
        "MASTER_HEARTBEAT_PERIOD",
        "MASTER_LOG_FILE",
        "MASTER_LOG_POS",
        "RELAY_LOG_FILE",
        "RELAY_LOG_POS",
        "MASTER_SSL",
        "MASTER_SSL_CA",
        "MASTER_SSL_CAPATH",
        "MASTER_SSL_CERT",
        "MASTER_SSL_KEY",
        "MASTER_SSL_CIPHER",
        "MASTER_SSL_VERIFY_SERVER_CERT",
        "IGNORE_SERVER_IDS",
    ]

    users: list[str] = []

    def __init__(
        self,
        smart_completion: bool = True,
        supported_formats: tuple = (),
        keyword_casing: str = "auto",
    ) -> None:
        super().__init__()
        self.smart_completion = smart_completion
        self.reserved_words = set()
        for x in self.keywords:
            self.reserved_words.update(x.split())
        self.name_pattern = _NAME_PATTERN_RE

        self.special_commands: list[str] = []
        self.table_formats = supported_formats
        if keyword_casing not in ("upper", "lower", "auto"):
            keyword_casing = "auto"
        self.keyword_casing = keyword_casing
        self._match_cache: dict[tuple, list[tuple[str, int]]] = {}
        self.reset_completions()

    def escape_name(self, name: str) -> str:
        if name and ((not self.name_pattern.match(name)) or (name.upper() in self.reserved_words) or (name.upper() in self.functions)):
            name = f'`{name}`'

        return name

    def unescape_name(self, name: str) -> str:
        """Unquote a string."""
        if name and name[0] == '"' and name[-1] == '"':
            name = name[1:-1]

        return name

    def escaped_names(self, names: Collection[str]) -> list[str]:
        return [self.escape_name(name) for name in names]

    def extend_special_commands(self, special_commands: list[str]) -> None:
        # Special commands are not part of all_completions since they can only
        # be at the beginning of a line.
        self.special_commands.extend(special_commands)

    def extend_database_names(self, databases: list[str]) -> None:
        self.databases.extend([self.escape_name(db) for db in databases])

    def extend_keywords(self, keywords: list[str], replace: bool = False) -> None:
        if replace:
            self.keywords = keywords
        else:
            self.keywords.extend(keywords)
        self.all_completions.update(keywords)

    def extend_show_items(self, show_items: Iterable[tuple]) -> None:
        for show_item in show_items:
            self.show_items.extend(show_item)
            self.all_completions.update(show_item)

    def extend_change_items(self, change_items: Iterable[tuple]) -> None:
        for change_item in change_items:
            self.change_items.extend(change_item)
            self.all_completions.update(change_item)

    def extend_users(self, users: Iterable[tuple]) -> None:
        for user in users:
            self.users.extend(user)
            self.all_completions.update(user)

    def extend_schemata(self, schema: str | None) -> None:
        if schema is None:
            return
        metadata = self.dbmetadata["tables"]
        metadata[schema] = {}

        # dbmetadata.values() are the 'tables' and 'functions' dicts
        for metadata in self.dbmetadata.values():
            metadata[schema] = {}
        self.all_completions.update(schema)

    def extend_relations(self, data: list[tuple[str, ...]], kind: Literal['tables', 'views']) -> None:
        """Extend metadata for tables or views

        :param data: list of (rel_name, ...) tuples
        :param kind: either 'tables' or 'views'
        :return:
        """
        data_ll = [self.escaped_names(d) for d in data]

        # dbmetadata['tables'][$schema_name][$table_name] should be a list of
        # column names. Default to an asterisk
        metadata = self.dbmetadata[kind]
        for relname in data_ll:
            try:
                metadata[self.dbname][relname[0]] = ["*"]
            except KeyError:
                _logger.error("%r %r listed in unrecognized schema %r", kind, relname[0], self.dbname)
            self.all_completions.add(relname[0])

    def extend_columns(self, column_data: list[tuple[str, ...]], kind: Literal['tables', 'views']) -> None:
        """Extend column metadata

        :param column_data: list of (rel_name, column_name[, column_type]) tuples
        :param kind: either 'tables' or 'views'
        :return:
        """
        metadata = self.dbmetadata[kind]
        for item in column_data:
            # Extract raw type before escaping (escape_name would mangle type strings)
            col_type = item[2] if len(item) > 2 else None
            escaped = self.escaped_names(item[:2])
            relname = escaped[0]
            column = escaped[1]
            if relname not in metadata[self.dbname]:
                _logger.error("relname '%s' was not found in db '%s'", relname, self.dbname)
                # this could happen back when the completer populated via two calls:
                # SHOW TABLES then SELECT table_name, column_name from information_schema.columns
                # it's a slight race, but much more likely on Vitess picking random shards for each.
                # see discussion in https://github.com/dbcli/mycli/pull/1182 (tl;dr - let's keep it)
                continue
            metadata[self.dbname][relname].append(column)
            self.all_completions.add(column)
            if col_type:
                self.column_types[column] = col_type

    def extend_relations_with_schema(self, data: list[tuple[str, str]], kind: Literal['tables', 'views']) -> None:
        """Extend metadata for tables/views with explicit schema names.

        :param data: list of (schema_name, rel_name) tuples
        :param kind: either 'tables' or 'views'
        """
        metadata = self.dbmetadata[kind]
        for schema, relname in data:
            escaped_relname = self.escape_name(relname)
            if schema not in metadata:
                metadata[schema] = {}
            if escaped_relname not in metadata[schema]:
                metadata[schema][escaped_relname] = ['*']
            self.all_completions.add(escaped_relname)

    def extend_columns_with_schema(self, column_data: list[tuple[str, str, str]], kind: Literal['tables', 'views']) -> None:
        """Extend column metadata with explicit schema names.

        :param column_data: list of (schema_name, rel_name, column_name) tuples
        :param kind: either 'tables' or 'views'
        """
        metadata = self.dbmetadata[kind]
        for schema, relname, column in column_data:
            escaped_relname = self.escape_name(relname)
            escaped_column = self.escape_name(column)
            if schema not in metadata:
                metadata[schema] = {}
            if escaped_relname not in metadata[schema]:
                metadata[schema][escaped_relname] = []
            elif metadata[schema][escaped_relname] == ['*']:
                metadata[schema][escaped_relname] = []
            metadata[schema][escaped_relname].append(escaped_column)
            self.all_completions.add(escaped_column)

    def extend_enum_values(self, enum_data: Iterable[tuple[str, str, list[str]]]) -> None:
        metadata = self.dbmetadata["enum_values"]
        if self.dbname not in metadata:
            metadata[self.dbname] = {}

        for relname, column, values in enum_data:
            relname_escaped = self.escape_name(relname)
            column_escaped = self.escape_name(column)
            table_meta = metadata[self.dbname].setdefault(relname_escaped, {})
            table_meta[column_escaped] = values

    def extend_functions(self, func_data: list[str] | Generator[tuple[str, str]], builtin: bool = False) -> None:
        # if 'builtin' is set this is extending the list of builtin functions
        if builtin:
            if isinstance(func_data, list):
                self.functions.extend(func_data)
            return

        # 'func_data' is a generator object. It can throw an exception while
        # being consumed. This could happen if the user has launched the app
        # without specifying a database name. This exception must be handled to
        # prevent crashing.
        try:
            func_data_ll = [self.escaped_names(d) for d in func_data]
        except Exception as e:
            _logger.debug("escaped_names failed: %r", e)
            func_data_ll = []

        # dbmetadata['functions'][$schema_name][$function_name] should return
        # function metadata.
        metadata = self.dbmetadata["functions"]

        for func in func_data_ll:
            metadata[self.dbname][func[0]] = None
            self.all_completions.add(func[0])

    def extend_foreign_keys(self, fk_data: Iterable[tuple[str, str, str, str]]) -> None:
        for table, column, ref_table, ref_column in fk_data:
            self.foreign_keys.setdefault(table, []).append((column, ref_table, ref_column))

    def extend_procedures(self, procedure_data: Generator[tuple[str, str]]) -> None:
        metadata = self.dbmetadata["procedures"]
        if self.dbname not in metadata:
            metadata[self.dbname] = {}

        for elt in procedure_data:
            metadata[self.dbname][elt[0]] = None

    def set_dbname(self, dbname: str | None) -> None:
        self.dbname = dbname or ''

    def invalidate_match_cache(self) -> None:
        self._match_cache.clear()

    def reset_completions(self) -> None:
        self.databases: list[str] = []
        self.users: list[str] = []
        self.show_items: list[Completion] = []
        self.dbname = ""
        self.dbmetadata: dict[str, Any] = {
            "tables": {},
            "views": {},
            "functions": {},
            "procedures": {},
            "enum_values": {},
        }
        self.column_types: dict[str, str] = {}
        self.all_completions = set(self.keywords + self.functions)
        # FK: {table_name: [(column, ref_table, ref_column), ...]}
        self.foreign_keys: dict[str, list[tuple[str, str, str]]] = {}
        self._lazy_conn_params: dict | None = None
        self._loaded_schemas: set[str] = set()
        self.invalidate_match_cache()

    @staticmethod
    def _compute_matches(
        text: str,
        last: str,
        collection: Collection,
        start_only: bool,
        fuzzy: bool,
    ) -> list[tuple[str, int]]:
        """Core match computation without casing. Returns list of (item, fuzziness)."""
        case_change_pat = _CASE_CHANGE_RE
        completions: list[tuple[str, int]] = []

        if fuzzy:
            regex = ".{0,3}?".join(map(re.escape, text))
            pat = re.compile(f'({regex})')
            under_words_text = [x for x in text.split('_') if x]
            case_words_text = case_change_pat.split(last)

            for item in collection:
                r = pat.search(item.lower())
                if r:
                    completions.append((item, Fuzziness.REGEX))
                    continue

                under_words_item = [x for x in item.lower().split('_') if x]
                occurrences = 0
                for elt_word in under_words_text:
                    for elt_item in under_words_item:
                        if elt_item.startswith(elt_word):
                            occurrences += 1
                            break
                if occurrences >= len(under_words_text):
                    completions.append((item, Fuzziness.UNDER_WORDS))
                    continue

                case_words_item = case_change_pat.split(item)
                occurrences = 0
                for elt_word in case_words_text:
                    for elt_item in case_words_item:
                        if elt_item.startswith(elt_word):
                            occurrences += 1
                            break
                if occurrences >= len(case_words_text):
                    completions.append((item, Fuzziness.CAMEL_CASE))
                    continue

            if len(text) >= 4:
                rapidfuzz_matches = rapidfuzz.process.extract(
                    text,
                    collection,
                    scorer=rapidfuzz.fuzz.WRatio,
                    processor=rapidfuzz.utils.default_process,
                    limit=20,
                    score_cutoff=75,
                )
                for elt in rapidfuzz_matches:
                    item, _score, _type = elt
                    if len(item) < len(text) / 1.5:
                        continue
                    if item in completions:
                        continue
                    completions.append((item, Fuzziness.RAPIDFUZZ))
        else:
            match_end_limit = len(text) if start_only else None
            for item in collection:
                match_point = item.lower().find(text, 0, match_end_limit)
                if match_point >= 0:
                    completions.append((item, Fuzziness.PERFECT))

        return completions

    def find_matches(
        self,
        orig_text: str,
        collection: Collection,
        start_only: bool = False,
        fuzzy: bool = True,
        casing: str | None = None,
    ) -> Generator[tuple[str, int], None, None]:
        """Find completion matches with caching for repeated lookups."""
        last = last_word(orig_text, include="most_punctuations")
        text = last.lower()

        if _DIGIT_START_RE.match(text):
            return iter(())

        cache_key = (text, id(collection), len(collection), start_only, fuzzy)
        cached = self._match_cache.get(cache_key)
        if cached is None:
            cached = self._compute_matches(text, last, collection, start_only, fuzzy)
            if len(self._match_cache) > 2048:
                self._match_cache.clear()
            self._match_cache[cache_key] = cached

        if casing == "auto":
            casing = "lower" if last and (last[0].islower() or last[-1].islower()) else "upper"

        def apply_case(tup: tuple[str, int]) -> tuple[str, int]:
            kw, fuzziness = tup
            if casing == "upper":
                return (kw.upper(), fuzziness)
            return (kw.lower(), fuzziness)

        return (x if casing is None else apply_case(x) for x in cached)

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent | None,
        smart_completion: bool | None = None,
    ) -> Iterable[Completion]:
        word_before_cursor = document.get_word_before_cursor(WORD=True)
        last_for_len = last_word(word_before_cursor, include="most_punctuations")
        text_for_len = last_for_len.lower()

        if smart_completion is None:
            smart_completion = self.smart_completion

        # If smart_completion is off then match any word that starts with
        # 'word_before_cursor'.
        if not smart_completion:
            matches = self.find_matches(word_before_cursor, self.all_completions, start_only=True, fuzzy=False)
            return (Completion(x[0], -len(text_for_len)) for x in matches)

        # completions: list of (text, fuzziness, rank, meta_label)
        completions: list[tuple[str, int, int, str]] = []
        suggestions = suggest_type(document.text, document.text_before_cursor)
        rigid_sort = False
        _cte_cols: dict[str, list[str]] | None = None  # lazy-init per call

        rank = 0
        for suggestion in suggestions:
            _logger.debug("Suggestion type: %r", suggestion["type"])
            rank += 1

            if suggestion["type"] == "column":
                tables = suggestion["tables"]
                _logger.debug("Completion column scope: %r", tables)
                scoped_cols = self.populate_scoped_cols(tables)

                # Supplement with CTE column names for any table sources that are CTEs
                if tables and _cte_cols is None:
                    _cte_cols = extract_cte_columns(document.text)
                if _cte_cols:
                    for _schema, tbl_name, _alias in tables:
                        cte_columns = _cte_cols.get(tbl_name, [])
                        scoped_cols.extend(c for c in cte_columns if c != '*')

                if suggestion.get("drop_unique"):
                    scoped_cols = [col for (col, count) in Counter(scoped_cols).items() if count > 1 and col != "*"]
                elif not tables:
                    scoped_cols = sorted(set(scoped_cols), key=lambda s: s.strip('`'))

                cols = self.find_matches(word_before_cursor, scoped_cols)
                completions.extend([(*x, rank, "column") for x in cols])

            elif suggestion["type"] == "function":
                funcs = self.populate_schema_objects(suggestion["schema"], "functions")
                user_funcs = self.find_matches(word_before_cursor, funcs)
                completions.extend([(*x, rank, "func") for x in user_funcs])

                if not suggestion["schema"]:
                    predefined_funcs = self.find_matches(
                        word_before_cursor, self.functions, start_only=True, fuzzy=False, casing=self.keyword_casing
                    )
                    completions.extend([(*x, rank, "func") for x in predefined_funcs])

            elif suggestion["type"] == "procedure":
                procs = self.populate_schema_objects(suggestion["schema"], "procedures")
                procs_m = self.find_matches(word_before_cursor, procs)
                completions.extend([(*x, rank, "proc") for x in procs_m])

            elif suggestion["type"] == "table":
                tables = self.populate_schema_objects(suggestion["schema"], "tables")
                tables_m = self.find_matches(word_before_cursor, tables)
                completions.extend([(*x, rank, "table") for x in tables_m])

            elif suggestion["type"] == "view":
                views = self.populate_schema_objects(suggestion["schema"], "views")
                views_m = self.find_matches(word_before_cursor, views)
                completions.extend([(*x, rank, "view") for x in views_m])

            elif suggestion["type"] == "cte":
                cte_names = suggestion["cte_names"]
                cte_m = self.find_matches(word_before_cursor, cte_names)
                completions.extend([(*x, rank, "cte") for x in cte_m])

            elif suggestion["type"] == "keyword_list":
                keywords = suggestion["keywords"]
                kw_m = self.find_matches(word_before_cursor, keywords, casing=self.keyword_casing)
                completions.extend([(*x, rank, "keyword") for x in kw_m])

            elif suggestion["type"] == "alias":
                aliases = suggestion["aliases"]
                aliases_m = self.find_matches(word_before_cursor, aliases)
                completions.extend([(*x, rank, "alias") for x in aliases_m])

            elif suggestion["type"] == "database":
                dbs_m = self.find_matches(word_before_cursor, self.databases)
                completions.extend([(*x, rank, "db") for x in dbs_m])

            elif suggestion["type"] == "keyword":
                if text_for_len:
                    for trigger, (template, desc) in SNIPPET_TEMPLATES.items():
                        if trigger.startswith(text_for_len):
                            expanded = _snippet_preview(template)
                            completions.append((expanded, Fuzziness.PERFECT, 0, f"snippet: {desc}"))
                keywords_m = self.find_matches(word_before_cursor, self.keywords, casing=self.keyword_casing)
                completions.extend([(*x, rank, "keyword") for x in keywords_m])

            elif suggestion["type"] == "show":
                show_items_m = self.find_matches(
                    word_before_cursor, self.show_items, start_only=False, fuzzy=True, casing=self.keyword_casing
                )
                completions.extend([(*x, rank, "show") for x in show_items_m])

            elif suggestion["type"] == "change":
                change_items_m = self.find_matches(word_before_cursor, self.change_items, start_only=False, fuzzy=True)
                completions.extend([(*x, rank, "change") for x in change_items_m])

            elif suggestion["type"] == "user":
                users_m = self.find_matches(word_before_cursor, self.users, start_only=False, fuzzy=True)
                completions.extend([(*x, rank, "user") for x in users_m])

            elif suggestion["type"] == "special":
                special_m = self.find_matches(word_before_cursor, self.special_commands, start_only=True, fuzzy=False)
                completions.extend([(*x, 0, "special") for x in special_m])

            elif suggestion["type"] == "favoritequery":
                if hasattr(FavoriteQueries, 'instance') and hasattr(FavoriteQueries.instance, 'list'):
                    queries_m = self.find_matches(word_before_cursor, FavoriteQueries.instance.list(), start_only=False, fuzzy=True)
                    completions.extend([(*x, rank, "fav") for x in queries_m])

            elif suggestion["type"] == "table_format":
                formats_m = self.find_matches(word_before_cursor, self.table_formats)
                completions.extend([(*x, rank, "format") for x in formats_m])

            elif suggestion["type"] == "file_name":
                file_names_m = self.find_files(word_before_cursor)
                completions.extend([(*x, rank, "file") for x in file_names_m])
                rigid_sort = True
            elif suggestion["type"] == "llm":
                if not word_before_cursor:
                    tokens = document.text.split()[1:]
                else:
                    tokens = document.text.split()[1:-1]
                possible_entries = llm.get_completions(tokens)
                subcommands_m = self.find_matches(
                    word_before_cursor,
                    possible_entries,
                    start_only=False,
                    fuzzy=True,
                )
                completions.extend([(*x, rank, "llm") for x in subcommands_m])
            elif suggestion["type"] == "enum_value":
                enum_values = self.populate_enum_values(
                    suggestion["tables"],
                    suggestion["column"],
                    suggestion.get("parent"),
                )
                if enum_values:
                    quoted_values = [self._quote_sql_string(value) for value in enum_values]
                    completions = [(*x, rank, "enum") for x in self.find_matches(word_before_cursor, quoted_values)]
                    break

            elif suggestion["type"] == "join_condition":
                join_tables = suggestion["tables"]
                join_hints = self._suggest_join_conditions(join_tables)
                for condition_text in join_hints:
                    if not text_for_len or condition_text.lower().startswith(text_for_len):
                        completions.append((condition_text, Fuzziness.PERFECT, rank, "join"))

            elif suggestion["type"] == "smart_alias":
                alias_table = suggestion["table"]
                alias_suggestions = self.suggest_alias(alias_table)
                for alias in alias_suggestions:
                    if not text_for_len or alias.startswith(text_for_len):
                        completions.append((alias, Fuzziness.PERFECT, rank, f"alias for {alias_table}"))

            elif suggestion["type"] == "insert_values":
                table = suggestion["table"]
                pos = suggestion["position"]
                ordered_cols = self._get_ordered_columns(table)
                if ordered_cols and pos < len(ordered_cols):
                    remaining = ordered_cols[pos:]
                    for i, col in enumerate(remaining):
                        ct = self.column_types.get(col) or self.column_types.get(col.strip('`')) or ''
                        meta = f"#{pos+i+1}" + (f" {ct}" if ct else "")
                        if not text_for_len or col.lower().startswith(text_for_len):
                            completions.append((col, Fuzziness.PERFECT, rank, meta))

            elif suggestion["type"] == "json_path":
                json_paths = self.suggest_json_paths(word_before_cursor)
                completions.extend([(*x, rank, "json") for x in json_paths])

        def completion_sort_key(item: tuple[str, int, int, str], text_for_len: str):
            candidate, fuzziness, rank, _meta = item
            if not text_for_len:
                return (0, rank, 0)
            elif candidate.lower().startswith(text_for_len):
                return (0, 0, -1000 + len(candidate))
            return (fuzziness, rank, 0)

        if rigid_sort:
            # Preserve first-seen meta for each unique text
            uniq_completions: dict[str, str] = {}
            for item in completions:
                if item[0] not in uniq_completions:
                    uniq_completions[item[0]] = item[3]
        else:
            sorted_completions = sorted(completions, key=lambda item: completion_sort_key(item, text_for_len.lower()))
            uniq_completions: dict[str, str] = {}
            for item in sorted_completions:
                if item[0] not in uniq_completions:
                    uniq_completions[item[0]] = item[3]

        def make_completion(text: str, meta_label: str) -> Completion:
            if meta_label.startswith('#'):
                display = meta_label
            else:
                col_type = self.column_types.get(text) or self.column_types.get(text.strip('`'))
                display = col_type if col_type else meta_label
            return Completion(text, -len(text_for_len), display_meta=display)

        return (make_completion(text, meta) for text, meta in uniq_completions.items())

    def find_files(self, word: str) -> Generator[tuple[str, int], None, None]:
        """Yield matching directory or file names.

        :param word:
        :return: iterable

        """
        # todo position is ignored, but may need to be used
        # todo fuzzy matches for filenames
        base_path, last_path, position = parse_path(word)
        paths = suggest_path(word)
        for name in paths:
            suggestion = complete_path(name, last_path)
            if suggestion:
                yield (suggestion, Fuzziness.PERFECT)

    def suggest_json_paths(self, word_before_cursor: str) -> Generator[tuple[str, int], None, None]:
        """Suggest common JSON path patterns for MySQL JSON functions.

        :param word_before_cursor: text typed so far
        :return: generator of (path, fuzziness) tuples
        """
        # Get the partial text after any quotes that might have been started
        partial = word_before_cursor.lstrip("'\"")

        for path, _meta in JSON_PATH_SUGGESTIONS:
            # Strip quotes for matching
            path_content = path.strip("'")
            if not partial or path_content.startswith(partial) or partial.startswith("$"):
                yield (path, Fuzziness.PERFECT)

    def populate_scoped_cols(self, scoped_tbls: list[tuple[str | None, str, str | None]]) -> list[str]:
        """Find all columns in a set of scoped_tables
        :param scoped_tbls: list of (schema, table, alias) tuples
        :return: list of column names
        """
        columns = []
        meta = self.dbmetadata

        # if scoped tables is empty, this is just after a SELECT so we
        # show all columns for all tables in the schema.
        if len(scoped_tbls) == 0 and self.dbname:
            for table in meta["tables"][self.dbname]:
                columns.extend(meta["tables"][self.dbname][table])
            return columns or ['*']

        # query includes tables, so use those to populate columns
        for tbl in scoped_tbls:
            # A fully qualified schema.relname reference or default_schema
            # DO NOT escape schema names.
            schema = tbl[0] or self.dbname
            relname = tbl[1]
            escaped_relname = self.escape_name(tbl[1])

            # We don't know if schema.relname is a table or view. Since
            # tables and views cannot share the same name, we can check one
            # at a time
            try:
                columns.extend(meta["tables"][schema][relname])

                # Table exists, so don't bother checking for a view
                continue
            except KeyError:
                try:
                    columns.extend(meta["tables"][schema][escaped_relname])
                    # Table exists, so don't bother checking for a view
                    continue
                except KeyError:
                    pass

            try:
                columns.extend(meta["views"][schema][relname])
            except KeyError:
                pass

        return columns

    def populate_enum_values(
        self,
        scoped_tbls: list[tuple[str | None, str, str | None]],
        column: str,
        parent: str | None = None,
    ) -> list[str]:
        values: list[str] = []
        meta = self.dbmetadata["enum_values"]
        column_key = self._escape_identifier(column)
        parent_key = self._strip_backticks(parent) if parent else None

        for schema, relname, alias in scoped_tbls:
            if parent_key and not self._matches_parent(parent_key, schema, relname, alias):
                continue

            schema = schema or self.dbname
            table_meta = meta.get(schema, {})
            escaped_relname = self.escape_name(relname)

            for rel_key in {relname, escaped_relname}:
                columns = table_meta.get(rel_key)
                if columns and column_key in columns:
                    values.extend(columns[column_key])

        return list(dict.fromkeys(values))

    def _escape_identifier(self, name: str) -> str:
        return self.escape_name(self._strip_backticks(name))

    @staticmethod
    def _strip_backticks(name: str | None) -> str:
        if name and name[0] == "`" and name[-1] == "`":
            return name[1:-1]
        return name or ""

    @staticmethod
    def _matches_parent(parent: str, schema: str | None, relname: str, alias: str | None) -> bool:
        if alias and parent == alias:
            return True
        if parent == relname:
            return True
        if schema and parent == f"{schema}.{relname}":
            return True
        return False

    @staticmethod
    def _quote_sql_string(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _suggest_join_conditions(self, tables: list[tuple[str | None, str, str | None]]) -> list[str]:
        """Suggest JOIN ON conditions based on FK metadata and name heuristics."""
        if len(tables) < 2:
            return []
        suggestions = []
        table_info = [(schema, table, alias or table) for schema, table, alias in tables]

        # FK-based suggestions: look for FK relationships between any two tables
        for i, (_, t1, a1) in enumerate(table_info):
            fks = self.foreign_keys.get(t1, [])
            for col, ref_table, ref_col in fks:
                for _, t2, a2 in table_info:
                    if t2 == ref_table:
                        suggestions.append(f"{a1}.{col} = {a2}.{ref_col}")
            # Also check reverse direction
            for j, (_, t2, a2) in enumerate(table_info):
                if i == j:
                    continue
                for col2, ref_table2, ref_col2 in self.foreign_keys.get(t2, []):
                    if ref_table2 == t1:
                        cond = f"{a2}.{col2} = {a1}.{ref_col2}"
                        if cond not in suggestions:
                            suggestions.append(cond)

        # Name heuristic: match columns with same name across tables
        if not suggestions:
            for i, (_, t1, a1) in enumerate(table_info):
                cols1 = set(self._get_ordered_columns(t1))
                for j, (_, t2, a2) in enumerate(table_info):
                    if j <= i:
                        continue
                    cols2 = set(self._get_ordered_columns(t2))
                    common = cols1 & cols2
                    for col in sorted(common):
                        if col != '*':
                            suggestions.append(f"{a1}.{col} = {a2}.{col}")

        return suggestions

    @staticmethod
    def suggest_alias(table_name: str) -> list[str]:
        """Generate smart alias suggestions from a table name.

        Strategies: first letter, underscore initials, common abbreviations.
        """
        name = table_name.strip('`')
        if not name:
            return []
        aliases = []
        # First letter
        aliases.append(name[0].lower())
        # Underscore-separated initials: user_accounts -> ua
        parts = name.split('_')
        if len(parts) > 1:
            initials = ''.join(p[0] for p in parts if p).lower()
            if initials and initials not in aliases:
                aliases.append(initials)
        # camelCase initials: UserAccounts -> ua
        import re
        camel_parts = re.findall(r'[A-Z][a-z]*', name)
        if len(camel_parts) > 1:
            camel_initials = ''.join(p[0] for p in camel_parts).lower()
            if camel_initials not in aliases:
                aliases.append(camel_initials)
        # Short prefix (first 3 chars) for single-word names
        if len(parts) == 1 and len(name) > 3:
            prefix = name[:3].lower()
            if prefix not in aliases:
                aliases.append(prefix)
        return aliases

    def _get_ordered_columns(self, table: str) -> list[str]:
        """Return columns for a table in their original metadata order."""
        meta = self.dbmetadata
        escaped = self.escape_name(table)
        for kind in ("tables", "views"):
            schema_meta = meta[kind].get(self.dbname, {})
            cols = schema_meta.get(table) or schema_meta.get(escaped)
            if cols and cols != ['*']:
                return [c for c in cols if c != '*']
        return []

    def enable_lazy_schema_loading(self, **conn_params) -> None:
        """Enable on-demand cross-schema metadata loading."""
        self._lazy_conn_params = conn_params

    def _lazy_load_schema(self, schema: str) -> None:
        """Fetch tables and columns for a schema on demand, if not already loaded."""
        if schema in self._loaded_schemas:
            return
        conn_params = getattr(self, '_lazy_conn_params', None)
        if not conn_params:
            return
        self._loaded_schemas.add(schema)
        try:
            import pymysql
            conn = pymysql.connect(
                database=schema,
                user=conn_params.get('user', ''),
                password=conn_params.get('password', '') or '',
                host=conn_params.get('host', 'localhost'),
                port=conn_params.get('port') or 0,
                charset=conn_params.get('charset', '') or '',
                use_unicode=True,
                autocommit=True,
                connect_timeout=5,
                program_name="mycli-lazy",
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT TABLE_NAME, COLUMN_NAME "
                        "FROM information_schema.columns "
                        "WHERE TABLE_SCHEMA = %s "
                        "ORDER BY TABLE_NAME, ORDINAL_POSITION",
                        (schema,),
                    )
                    rows = list(cur)
            finally:
                conn.close()

            if not rows:
                return

            seen: set[str] = set()
            tables_data: list[tuple[str, str]] = []
            column_data: list[tuple[str, str, str]] = []
            for table, column in rows:
                if table not in seen:
                    seen.add(table)
                    tables_data.append((schema, table))
                column_data.append((schema, table, column))

            self.extend_relations_with_schema(tables_data, kind="tables")
            self.extend_columns_with_schema(column_data, kind="tables")
            self.invalidate_match_cache()
            _logger.debug("Lazy-loaded schema %r: %d tables, %d columns", schema, len(tables_data), len(column_data))
        except Exception as e:
            _logger.debug("Lazy schema load failed for %r: %r", schema, e)

    def populate_schema_objects(self, schema: str | None, obj_type: str) -> list[str]:
        """Returns list of tables or functions for a (optional) schema"""
        metadata = self.dbmetadata[obj_type]
        schema = schema or self.dbname

        try:
            objects = metadata[schema].keys()
        except KeyError:
            # Schema not yet loaded -- attempt lazy loading
            if schema and schema != self.dbname and obj_type == "tables":
                self._lazy_load_schema(schema)
                try:
                    objects = metadata[schema].keys()
                except KeyError:
                    objects = []
            else:
                objects = []

        return objects
