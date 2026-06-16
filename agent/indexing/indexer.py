import os
import sqlite3
from pathlib import Path

from agent.indexing.models import Reference, Symbol
from agent.indexing.parser import parse_workspace

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER NOT NULL,
    column INTEGER NOT NULL,
    scope TEXT,
    signature TEXT
);

CREATE TABLE IF NOT EXISTS symbol_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    line INTEGER NOT NULL,
    column INTEGER NOT NULL,
    is_definition INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    mtime REAL NOT NULL
);
"""


class Indexer:
    def __init__(self, workspace: str, db_path: str | None = None):
        self.workspace = str(Path(workspace).resolve())
        self.db_path = os.path.expanduser(db_path or "~/.coding-agent/code_index.db")
        db_dir = Path(self.db_path).parent
        if not db_dir.exists():
            db_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.executescript(SCHEMA)

    def build(self) -> None:
        symbols, refs = parse_workspace(self.workspace)
        with self._connection() as conn:
            conn.execute("DELETE FROM symbols WHERE workspace = ?", (self.workspace,))
            conn.execute("DELETE FROM symbol_references WHERE workspace = ?", (self.workspace,))
            conn.execute("DELETE FROM files WHERE path LIKE ?", (f"{self.workspace}%",))

            for symbol in symbols:
                conn.execute(
                    """
                    INSERT INTO symbols
                    (workspace, path, name, kind, line, column, scope, signature)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.workspace,
                        symbol.path,
                        symbol.name,
                        symbol.kind,
                        symbol.line,
                        symbol.column,
                        symbol.scope,
                        symbol.signature,
                    ),
                )

            for ref in refs:
                conn.execute(
                    """
                    INSERT INTO symbol_references
                    (workspace, path, name, line, column, is_definition)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.workspace,
                        ref.path,
                        ref.name,
                        ref.line,
                        ref.column,
                        int(ref.is_definition),
                    ),
                )

            for py_file in Path(self.workspace).rglob("*.py"):
                if "__pycache__" in py_file.parts:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO files (path, mtime) VALUES (?, ?)",
                    (str(py_file), py_file.stat().st_mtime),
                )

    def is_stale(self) -> bool:
        with self._connection() as conn:
            stored = {
                row["path"]: row["mtime"]
                for row in conn.execute(
                    "SELECT path, mtime FROM files WHERE path LIKE ?",
                    (f"{self.workspace}%",),
                )
            }

        current: dict[str, float] = {}
        for py_file in Path(self.workspace).rglob("*.py"):
            if "__pycache__" in py_file.parts:
                continue
            current[str(py_file)] = py_file.stat().st_mtime

        return stored != current

    def search_symbols(self, query: str, kind: str | None = None) -> list[Symbol]:
        with self._connection() as conn:
            sql = "SELECT * FROM symbols WHERE workspace = ? AND name LIKE ?"
            params: list = [self.workspace, f"%{query}%"]
            if kind:
                sql += " AND kind = ?"
                params.append(kind)
            rows = conn.execute(sql, params).fetchall()

        return [
            Symbol(
                path=row["path"],
                name=row["name"],
                kind=row["kind"],
                line=row["line"],
                column=row["column"],
                scope=row["scope"],
                signature=row["signature"],
            )
            for row in rows
        ]

    def find_definition(self, name: str) -> list[Symbol]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM symbols WHERE workspace = ? AND name = ?",
                (self.workspace, name),
            ).fetchall()
        return [
            Symbol(
                path=row["path"],
                name=row["name"],
                kind=row["kind"],
                line=row["line"],
                column=row["column"],
                scope=row["scope"],
                signature=row["signature"],
            )
            for row in rows
        ]

    def find_references(self, name: str) -> list[Reference]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM symbol_references "
                "WHERE workspace = ? AND name = ? ORDER BY path, line",
                (self.workspace, name),
            ).fetchall()
        return [
            Reference(
                path=row["path"],
                name=row["name"],
                line=row["line"],
                column=row["column"],
                is_definition=bool(row["is_definition"]),
            )
            for row in rows
        ]
