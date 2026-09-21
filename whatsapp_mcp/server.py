#!/usr/bin/env python3
"""A small, read-only MCP server for the WhatsApp macOS data store.

The WhatsApp desktop application keeps its local chat data in a Core Data
SQLite store. The schema has changed over time, so this server discovers the
relevant tables and columns at startup instead of baking in one app version.

Only SELECT statements are issued. Sending is deliberately not implemented:
writing directly to WhatsApp's local database would bypass its encryption and
delivery machinery and could corrupt the account state.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import quote


SERVER_NAME = "whatsapp-local"
SERVER_VERSION = "0.1.0"
DEFAULT_CHAT_DB = (
    Path.home()
    / "Library"
    / "Group Containers"
    / "group.net.whatsapp.WhatsApp.shared"
    / "ChatStorage.sqlite"
)
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
DEFAULT_BODY_CHARS = 2_000
SUPPORTED_PROTOCOLS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
}


class MCPError(Exception):
    """An expected, user-actionable tool error."""

    def __init__(self, message: str, *, data: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.data = dict(data or {})


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: tuple[str, ...]
    primary_key: str | None


@dataclass(frozen=True)
class Layout:
    chat: TableInfo | None
    message: TableInfo | None
    chat_id: str | None
    chat_name: str | None
    chat_last_date: str | None
    message_chat_ref: str | None
    message_text: str | None
    message_date: str | None
    message_from: str | None
    message_to: str | None
    message_from_me: str | None
    message_type: str | None
    message_status: str | None


def _ident(value: str) -> str:
    """Quote a SQLite identifier after rejecting NUL bytes."""

    if "\x00" in value:
        raise ValueError("NUL is not valid in a SQLite identifier")
    return '"' + value.replace('"', '""') + '"'


def _canonical(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _parse_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise MCPError("limit must be an integer") from exc
    if limit < 1 or limit > MAX_LIMIT:
        raise MCPError(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def _parse_offset(cursor: Any) -> int:
    if cursor in (None, ""):
        return 0
    if not isinstance(cursor, str) or not cursor.startswith("offset:"):
        raise MCPError("cursor must be an offset cursor returned by this server")
    try:
        offset = int(cursor.removeprefix("offset:"))
    except ValueError as exc:
        raise MCPError("cursor is invalid") from exc
    if offset < 0:
        raise MCPError("cursor is invalid")
    return offset


def _truncate(value: Any, max_chars: int = DEFAULT_BODY_CHARS) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return f"<binary {len(value)} bytes>"
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _timestamp_iso(value: Any) -> str | None:
    """Convert common Unix and Cocoa epoch values to an ISO UTC string."""

    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number == 0:
        return None
    if abs(number) > 1_000_000_000_000:
        number /= 1_000
    # Core Data WhatsApp stores often use seconds since 2001-01-01.
    if 100_000_000 < number < 1_000_000_000:
        number += 978_307_200
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _normal_bool(value: Any) -> bool | int | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"yes", "true", "1"}:
        return True
    if text in {"no", "false", "0"}:
        return False
    return str(value)


def _pick_column(
    columns: Sequence[str],
    exact: Sequence[str],
    contains: Sequence[str] = (),
    excludes: Sequence[str] = (),
) -> str | None:
    canonical_columns = {column: _canonical(column) for column in columns}
    exact_canonical = [_canonical(item) for item in exact]
    contains_canonical = [_canonical(item) for item in contains]
    excludes_canonical = [_canonical(item) for item in excludes]
    for wanted in exact_canonical:
        for column, canonical in canonical_columns.items():
            if canonical != wanted:
                continue
            if any(excluded in canonical for excluded in excludes_canonical):
                continue
            return column
    for wanted in contains_canonical:
        for column, canonical in canonical_columns.items():
            if wanted not in canonical:
                continue
            if any(excluded in canonical for excluded in excludes_canonical):
                continue
            return column
    return None


def _as_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return f"<binary {len(value)} bytes>"
    return str(value)


class WhatsAppStore:
    """Read-only access to the WhatsApp chat database."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None):
        configured = db_path or os.environ.get("WHATSAPP_CHAT_DB")
        self.db_path = Path(configured).expanduser() if configured else DEFAULT_CHAT_DB

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        # URI mode=ro makes accidental writes fail even if a future code path
        # is added carelessly. query_only is an additional defense in depth.
        uri = f"file:{quote(str(self.db_path), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=1.5)
        except (sqlite3.Error, OSError) as exc:
            raise MCPError(
                "Cannot open WhatsApp's local database. macOS may be blocking "
                "the MCP runtime from the WhatsApp group container.",
                data={
                    "database_path": str(self.db_path),
                    "database_exists": self.db_path.exists(),
                    "needs_full_disk_access": True,
                    "sqlite_error": str(exc),
                },
            ) from exc
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 1500")
            yield connection
        except sqlite3.Error as exc:
            raise MCPError(
                "WhatsApp's local database could not be read.",
                data={"sqlite_error": str(exc)},
            ) from exc
        finally:
            connection.close()

    def _tables(self, connection: sqlite3.Connection) -> list[TableInfo]:
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type IN ('table', 'view')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        tables: list[TableInfo] = []
        for row in rows:
            name = str(row[0])
            try:
                pragma_rows = connection.execute(
                    f"PRAGMA table_info({_ident(name)})"
                ).fetchall()
            except sqlite3.Error:
                continue
            columns = tuple(str(item[1]) for item in pragma_rows)
            pk_columns = [str(item[1]) for item in pragma_rows if int(item[5] or 0) > 0]
            primary_key = pk_columns[0] if pk_columns else _pick_column(
                columns, ("Z_PK", "ID", "ROWID")
            )
            tables.append(TableInfo(name, columns, primary_key))
        return tables

    @staticmethod
    def _table_score(table: TableInfo, kind: str) -> int:
        name = _canonical(table.name)
        columns = {_canonical(column) for column in table.columns}
        if "FTS" in name or "SEARCH" in name or "TOKEN" in name:
            return -10_000
        score = 0
        if kind == "chat":
            if "CHATSESSION" in name:
                score += 100
            elif "CHAT" in name or "CONVERSATION" in name:
                score += 40
            if {"ZCONTACTJID", "ZJID", "ZREMOTEJID"} & columns:
                score += 25
            if {"ZPARTNERNAME", "ZDISPLAYNAME", "ZTITLE", "ZNAME"} & columns:
                score += 15
            if {"ZLASTMESSAGEDATE", "ZMESSAGEDATE", "ZDATE"} & columns:
                score += 10
        elif kind == "message":
            if "MESSAGE" in name:
                score += 100
            if {"ZTEXT", "ZBODY", "ZCONTENT", "ZMESSAGETEXT"} & columns:
                score += 30
            if {"ZMESSAGEDATE", "ZDATE", "ZTIMESTAMP"} & columns:
                score += 20
        return score

    def _layout(self, connection: sqlite3.Connection) -> Layout:
        tables = self._tables(connection)
        chat = max(tables, key=lambda item: self._table_score(item, "chat"), default=None)
        message = max(
            tables, key=lambda item: self._table_score(item, "message"), default=None
        )
        if chat is not None and self._table_score(chat, "chat") <= 0:
            chat = None
        if message is not None and self._table_score(message, "message") <= 0:
            message = None
        chat_columns = chat.columns if chat else ()
        message_columns = message.columns if message else ()
        return Layout(
            chat=chat,
            message=message,
            chat_id=_pick_column(
                chat_columns,
                (
                    "ZCONTACTJID",
                    "ZJID",
                    "ZREMOTEJID",
                    "ZCHATJID",
                    "ZPARTNERJID",
                    "ZIDENTIFIER",
                ),
                ("JID",),
            ),
            chat_name=_pick_column(
                chat_columns,
                ("ZPARTNERNAME", "ZDISPLAYNAME", "ZTITLE", "ZNAME"),
                ("NAME", "TITLE"),
                ("LASTMESSAGE",),
            ),
            chat_last_date=_pick_column(
                chat_columns,
                ("ZLASTMESSAGEDATE", "ZMESSAGEDATE", "ZDATE", "ZTIMESTAMP"),
                ("DATE", "TIMESTAMP"),
            ),
            message_chat_ref=_pick_column(
                message_columns,
                (
                    "ZCHATSESSION",
                    "ZCHAT",
                    "ZCHATID",
                    "ZCHATSESSIONID",
                    "ZCONVERSATION",
                ),
                ("CHATSESSION", "CONVERSATION"),
            ),
            message_text=_pick_column(
                message_columns,
                ("ZTEXT", "ZBODY", "ZCONTENT", "ZMESSAGETEXT"),
                ("TEXT", "BODY", "CONTENT"),
                ("LINKPREVIEW",),
            ),
            message_date=_pick_column(
                message_columns,
                ("ZMESSAGEDATE", "ZSENTDATE", "ZDATE", "ZTIMESTAMP"),
                ("DATE", "TIMESTAMP"),
            ),
            message_from=_pick_column(
                message_columns,
                ("ZFROMJID", "ZSENDERJID", "ZAUTHOR", "ZPUSHNAME"),
                ("FROMJID", "SENDER", "AUTHOR", "PUSHNAME"),
            ),
            message_to=_pick_column(
                message_columns,
                ("ZTOJID", "ZDESTINATIONJID"),
                ("TOJID", "DESTINATION"),
            ),
            message_from_me=_pick_column(
                message_columns,
                ("ZISFROMME", "ZFROMME", "ISFROMME"),
                ("FROMME",),
            ),
            message_type=_pick_column(
                message_columns,
                ("ZMEDIATYPE", "ZMESSAGETYPE", "ZTYPE"),
                ("MEDIATYPE", "MESSAGETYPE", "TYPE"),
            ),
            message_status=_pick_column(
                message_columns,
                ("ZMESSAGESTATUS", "ZSTATUS"),
                ("MESSAGESTATUS", "STATUS"),
            ),
        )

    @staticmethod
    def _require_layout(layout: Layout, *, need_chat: bool, need_message: bool) -> None:
        missing: list[str] = []
        if need_chat and layout.chat is None:
            missing.append("chat table")
        if need_message and layout.message is None:
            missing.append("message table")
        if missing:
            raise MCPError(
                "Could not recognize WhatsApp's local schema.",
                data={"missing": missing},
            )

    @staticmethod
    def _chat_ref(
        layout: Layout, chat_id: str, connection: sqlite3.Connection
    ) -> tuple[Any, str | None, str | None]:
        assert layout.chat is not None
        table = layout.chat
        if not table.primary_key:
            raise MCPError("WhatsApp's chat table has no usable primary key")
        pk = None
        prefix = table.name + ":"
        if chat_id.startswith(prefix):
            raw_pk = chat_id[len(prefix) :]
            try:
                pk = int(raw_pk)
            except ValueError:
                pk = raw_pk
            row = connection.execute(
                f"SELECT * FROM {_ident(table.name)} WHERE {_ident(table.primary_key)} = ? LIMIT 1",
                (pk,),
            ).fetchone()
        elif layout.chat_id:
            row = connection.execute(
                f"SELECT * FROM {_ident(table.name)} WHERE {_ident(layout.chat_id)} = ? LIMIT 1",
                (chat_id,),
            ).fetchone()
        else:
            row = None
        if row is None:
            raise MCPError("chat_id was not found", data={"chat_id": chat_id})
        if pk is None:
            pk = row[table.primary_key]
        stable_id = str(row[layout.chat_id]) if layout.chat_id and row[layout.chat_id] else f"{table.name}:{pk}"
        display_name = (
            str(row[layout.chat_name])
            if layout.chat_name and row[layout.chat_name] is not None
            else None
        )
        return pk, stable_id, display_name

    @staticmethod
    def _message_projection(layout: Layout, *, prefix: str = "") -> list[str]:
        assert layout.message is not None
        table = layout.message

        def expr(column: str | None, alias: str) -> str:
            if column is None:
                return f"NULL AS {_ident(alias)}"
            return f"{prefix}{_ident(column)} AS {_ident(alias)}"

        return [
            expr(table.primary_key, "message_id"),
            expr(layout.message_date, "timestamp"),
            expr(layout.message_text, "text"),
            expr(layout.message_from, "from"),
            expr(layout.message_to, "to"),
            expr(layout.message_from_me, "from_me"),
            expr(layout.message_type, "type"),
            expr(layout.message_status, "status"),
            expr(layout.message_chat_ref, "chat_ref"),
        ]

    @staticmethod
    def _format_message(row: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in ("message_id", "timestamp", "from", "to", "type", "status", "chat_ref"):
            value = row[key]
            if value is not None:
                output_key = "id" if key == "message_id" else key
                result[output_key] = _as_json_value(value)
        if row["timestamp"] is not None:
            result["timestamp_iso"] = _timestamp_iso(row["timestamp"])
        if row["text"] is not None:
            result["text"] = _truncate(row["text"])
        if row["from_me"] is not None:
            result["from_me"] = _normal_bool(row["from_me"])
        return result

    def find_chats(
        self, *, query: str | None = None, limit: Any = DEFAULT_LIMIT, cursor: Any = None
    ) -> dict[str, Any]:
        limit_int = _parse_limit(limit)
        offset = _parse_offset(cursor)
        with self.connection() as connection:
            layout = self._layout(connection)
            self._require_layout(layout, need_chat=True, need_message=False)
            assert layout.chat is not None
            table = layout.chat
            if not table.primary_key:
                raise MCPError("WhatsApp's chat table has no usable primary key")
            projection = [
                f"{_ident(table.primary_key)} AS {_ident('row_id')}",
                (
                    f"{_ident(layout.chat_id)} AS {_ident('chat_jid')}"
                    if layout.chat_id
                    else "NULL AS \"chat_jid\""
                ),
                (
                    f"{_ident(layout.chat_name)} AS {_ident('chat_name')}"
                    if layout.chat_name
                    else "NULL AS \"chat_name\""
                ),
                (
                    f"{_ident(layout.chat_last_date)} AS {_ident('last_date')}"
                    if layout.chat_last_date
                    else "NULL AS \"last_date\""
                ),
            ]
            where: list[str] = []
            params: list[Any] = []
            normalized_query = str(query).strip() if query is not None else ""
            if normalized_query:
                searchable = [column for column in (layout.chat_id, layout.chat_name) if column]
                if searchable:
                    where.append(
                        "(" + " OR ".join(
                            f"CAST({_ident(column)} AS TEXT) LIKE ? COLLATE NOCASE"
                            for column in searchable
                        ) + ")"
                    )
                    params.extend([f"%{normalized_query}%"] * len(searchable))
            sql = f"SELECT {', '.join(projection)} FROM {_ident(table.name)}"
            if where:
                sql += " WHERE " + " AND ".join(where)
            order = _ident(layout.chat_last_date) if layout.chat_last_date else _ident(table.primary_key)
            sql += f" ORDER BY {order} DESC, {_ident(table.primary_key)} DESC LIMIT ? OFFSET ?"
            params.extend([limit_int + 1, offset])
            rows = connection.execute(sql, params).fetchall()
            has_more = len(rows) > limit_int
            rows = rows[:limit_int]
            chats: list[dict[str, Any]] = []
            for row in rows:
                raw_id = row["chat_jid"]
                raw_pk = row["row_id"]
                stable_id = str(raw_id) if raw_id not in (None, "") else f"{table.name}:{raw_pk}"
                item: dict[str, Any] = {"id": stable_id}
                if row["chat_name"] not in (None, ""):
                    item["name"] = _truncate(row["chat_name"], 300)
                elif raw_id not in (None, ""):
                    item["name"] = str(raw_id)
                if row["last_date"] is not None:
                    item["last_message_timestamp"] = _as_json_value(row["last_date"])
                    item["last_message_timestamp_iso"] = _timestamp_iso(row["last_date"])
                chats.append(item)
            result: dict[str, Any] = {
                "chats": chats,
                "count": len(chats),
                "database": str(self.db_path),
            }
            if has_more:
                result["next_cursor"] = f"offset:{offset + limit_int}"
            return result

    def _message_query_base(
        self,
        connection: sqlite3.Connection,
        layout: Layout,
        *,
        chat_id: str | None,
        search: str | None = None,
    ) -> tuple[str, list[Any], str | None, str | None]:
        self._require_layout(layout, need_chat=chat_id is not None, need_message=True)
        assert layout.message is not None
        message = layout.message
        params: list[Any] = []
        where: list[str] = []
        chat_stable_id = None
        chat_name = None
        join = ""
        if chat_id is not None:
            if layout.chat is None or not layout.message_chat_ref:
                raise MCPError(
                    "This WhatsApp schema does not expose a chat-to-message relation yet.",
                    data={"chat_id": chat_id},
                )
            chat_pk, chat_stable_id, chat_name = self._chat_ref(layout, chat_id, connection)
            where.append(f"m.{_ident(layout.message_chat_ref)} = ?")
            params.append(chat_pk)
        elif layout.message_chat_ref and layout.chat and layout.chat.primary_key:
            join = (
                f" LEFT JOIN {_ident(layout.chat.name)} AS c"
                f" ON m.{_ident(layout.message_chat_ref)} = c.{_ident(layout.chat.primary_key)}"
            )
        if search is not None:
            if not layout.message_text:
                raise MCPError("WhatsApp's message table has no text column")
            where.append(f"CAST(m.{_ident(layout.message_text)} AS TEXT) LIKE ? COLLATE NOCASE")
            params.append(f"%{search}%")
        sql = f"FROM {_ident(message.name)} AS m{join}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql, params, chat_stable_id, chat_name

    def read_messages(
        self,
        *,
        chat_id: str,
        limit: Any = DEFAULT_LIMIT,
        cursor: Any = None,
    ) -> dict[str, Any]:
        if not str(chat_id).strip():
            raise MCPError("chat_id is required")
        limit_int = _parse_limit(limit)
        offset = _parse_offset(cursor)
        with self.connection() as connection:
            layout = self._layout(connection)
            base, params, stable_id, chat_name = self._message_query_base(
                connection, layout, chat_id=str(chat_id)
            )
            assert layout.message is not None
            if not layout.message.primary_key:
                raise MCPError("WhatsApp's message table has no usable primary key")
            date_order = (
                f"m.{_ident(layout.message_date)} DESC"
                if layout.message_date
                else f"m.{_ident(layout.message.primary_key)} DESC"
            )
            sql = (
                f"SELECT {', '.join(self._message_projection(layout, prefix='m.'))} "
                f"{base} ORDER BY {date_order}, m.{_ident(layout.message.primary_key)} DESC LIMIT ? OFFSET ?"
            )
            rows = connection.execute(sql, [*params, limit_int + 1, offset]).fetchall()
            has_more = len(rows) > limit_int
            rows = rows[:limit_int]
            result: dict[str, Any] = {
                "chat_id": stable_id or str(chat_id),
                "messages": [self._format_message(row) for row in rows],
                "count": len(rows),
            }
            if chat_name:
                result["chat_name"] = chat_name
            if has_more:
                result["next_cursor"] = f"offset:{offset + limit_int}"
            return result

    def search_messages(
        self,
        *,
        query: str,
        chat_id: str | None = None,
        limit: Any = DEFAULT_LIMIT,
        cursor: Any = None,
    ) -> dict[str, Any]:
        normalized_query = str(query).strip()
        if not normalized_query:
            raise MCPError("query is required")
        limit_int = _parse_limit(limit)
        offset = _parse_offset(cursor)
        with self.connection() as connection:
            layout = self._layout(connection)
            base, params, selected_chat_id, selected_chat_name = self._message_query_base(
                connection, layout, chat_id=str(chat_id) if chat_id is not None else None, search=normalized_query
            )
            assert layout.message is not None
            if not layout.message.primary_key:
                raise MCPError("WhatsApp's message table has no usable primary key")
            projection = self._message_projection(layout, prefix="m.")
            if chat_id is None and layout.chat and layout.chat.primary_key and layout.chat_id:
                projection.append(f"c.{_ident(layout.chat_id)} AS {_ident('joined_chat_id')}")
                projection.append(
                    (
                        f"c.{_ident(layout.chat_name)} AS {_ident('joined_chat_name')}"
                        if layout.chat_name
                        else "NULL AS \"joined_chat_name\""
                    )
                )
            date_order = (
                f"m.{_ident(layout.message_date)} DESC"
                if layout.message_date
                else f"m.{_ident(layout.message.primary_key)} DESC"
            )
            sql = (
                f"SELECT {', '.join(projection)} {base}"
                f" ORDER BY {date_order}, m.{_ident(layout.message.primary_key)} DESC LIMIT ? OFFSET ?"
            )
            rows = connection.execute(sql, [*params, limit_int + 1, offset]).fetchall()
            has_more = len(rows) > limit_int
            rows = rows[:limit_int]
            matches: list[dict[str, Any]] = []
            for row in rows:
                item = self._format_message(row)
                if selected_chat_id:
                    item["chat_id"] = selected_chat_id
                elif "joined_chat_id" in row.keys() and row["joined_chat_id"] is not None:
                    item["chat_id"] = str(row["joined_chat_id"])
                elif row["chat_ref"] is not None:
                    item["chat_id"] = _as_json_value(row["chat_ref"])
                if selected_chat_name:
                    item["chat_name"] = selected_chat_name
                elif "joined_chat_name" in row.keys() and row["joined_chat_name"]:
                    item["chat_name"] = _truncate(row["joined_chat_name"], 300)
                matches.append(item)
            result: dict[str, Any] = {
                "query": normalized_query,
                "messages": matches,
                "count": len(matches),
            }
            if has_more:
                result["next_cursor"] = f"offset:{offset + limit_int}"
            return result

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "database_path": str(self.db_path),
            "database_exists": self.db_path.exists(),
            "read_only": True,
            "send_supported": False,
        }
        try:
            with self.connection() as connection:
                tables = self._tables(connection)
                layout = self._layout(connection)
                result.update(
                    {
                        "accessible": True,
                        "table_count": len(tables),
                        "tables": [table.name for table in tables],
                        "detected_chat_table": layout.chat.name if layout.chat else None,
                        "detected_message_table": layout.message.name if layout.message else None,
                        "detected_message_text_column": layout.message_text,
                        "detected_message_date_column": layout.message_date,
                    }
                )
        except MCPError as exc:
            result.update({"accessible": False, "error": str(exc), **exc.data})
        return result


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "whatsapp_status",
        "description": (
            "Check local WhatsApp database access and report the detected schema. "
            "Read-only; it never returns message bodies."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "find_chats",
        "description": (
            "Find WhatsApp chats by name or JID. Returns stable chat IDs and compact "
            "metadata with offset pagination."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name or WhatsApp JID substring."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
                "cursor": {"type": "string", "description": "Cursor returned by a previous call."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_messages",
        "description": (
            "Read recent messages for one chat. Results are newest first, bounded by "
            "a small default page size, with message text truncated for token efficiency."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["chat_id"],
            "properties": {
                "chat_id": {"type": "string", "description": "ID returned by find_chats."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
                "cursor": {"type": "string", "description": "Cursor returned by a previous call."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "search_messages",
        "description": (
            "Search WhatsApp message text, optionally within one chat. Returns compact "
            "matches and supports offset pagination."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Text substring to search for."},
                "chat_id": {"type": "string", "description": "Optional ID returned by find_chats."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
                "cursor": {"type": "string", "description": "Cursor returned by a previous call."},
            },
            "additionalProperties": False,
        },
    },
]


def _tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if isinstance(payload, dict):
        result["structuredContent"] = payload
    return result


def _call_tool(store: WhatsAppStore, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if name == "whatsapp_status":
            return _tool_result(store.status())
        if name == "find_chats":
            return _tool_result(
                store.find_chats(
                    query=arguments.get("query"),
                    limit=arguments.get("limit", DEFAULT_LIMIT),
                    cursor=arguments.get("cursor"),
                )
            )
        if name == "read_messages":
            return _tool_result(
                store.read_messages(
                    chat_id=str(arguments.get("chat_id", "")),
                    limit=arguments.get("limit", DEFAULT_LIMIT),
                    cursor=arguments.get("cursor"),
                )
            )
        if name == "search_messages":
            return _tool_result(
                store.search_messages(
                    query=str(arguments.get("query", "")),
                    chat_id=(str(arguments["chat_id"]) if arguments.get("chat_id") is not None else None),
                    limit=arguments.get("limit", DEFAULT_LIMIT),
                    cursor=arguments.get("cursor"),
                )
            )
        raise MCPError(f"Unknown tool: {name}")
    except MCPError as exc:
        payload = {"error": str(exc), **exc.data}
        return _tool_result(payload, is_error=True)
    except Exception as exc:  # Keep protocol errors structured and on stdout-safe JSON.
        return _tool_result(
            {"error": "Unexpected WhatsApp MCP error", "detail": str(exc)},
            is_error=True,
        )


def _respond(request_id: Any, result: Any) -> None:
    response = {"jsonrpc": "2.0", "id": request_id, "result": result}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _respond_error(request_id: Any, code: int, message: str, data: Any = None) -> None:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    response = {"jsonrpc": "2.0", "id": request_id, "error": error}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def serve(store: WhatsAppStore | None = None) -> None:
    store = store or WhatsAppStore()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _respond_error(None, -32700, "Invalid JSON", str(exc))
            continue
        if not isinstance(request, dict):
            _respond_error(None, -32600, "Invalid Request")
            continue
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            _respond_error(request_id, -32602, "params must be an object")
            continue
        # Notifications have no id and must not receive a response.
        is_notification = "id" not in request
        if method == "initialize":
            requested = params.get("protocolVersion")
            protocol = requested if requested in SUPPORTED_PROTOCOLS else "2025-06-18"
            if not is_notification:
                _respond(
                    request_id,
                    {
                        "protocolVersion": protocol,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                        "instructions": (
                            "This server is read-only and accesses the local WhatsApp desktop database. "
                            "Use find_chats before read_messages."
                        ),
                    },
                )
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            if not is_notification:
                _respond(request_id, {})
        elif method == "tools/list":
            if not is_notification:
                _respond(request_id, {"tools": TOOL_DEFINITIONS})
        elif method == "tools/call":
            if not isinstance(params.get("name"), str):
                if not is_notification:
                    _respond_error(request_id, -32602, "tools/call requires a tool name")
                continue
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                if not is_notification:
                    _respond_error(request_id, -32602, "tool arguments must be an object")
                continue
            if not is_notification:
                _respond(request_id, _call_tool(store, params["name"], arguments))
        elif not is_notification:
            _respond_error(request_id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    serve()
