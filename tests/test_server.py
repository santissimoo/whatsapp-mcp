from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from whatsapp_mcp.server import MCPError, WhatsAppStore


class WhatsAppStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "ChatStorage.sqlite"
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT NOT NULL,
                ZPARTNERNAME TEXT,
                ZLASTMESSAGEDATE REAL
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMESSAGEDATE REAL,
                ZTEXT TEXT,
                ZFROMJID TEXT,
                ZTOJID TEXT,
                ZISFROMME INTEGER,
                ZMEDIATYPE TEXT
            );
            INSERT INTO ZWACHATSESSION VALUES (1, '+15551230000', 'Sanjay', 800000000);
            INSERT INTO ZWACHATSESSION VALUES (2, '12345-67890@g.us', 'Team', 799000000);
            INSERT INTO ZWAMESSAGE VALUES (1, 1, 800000000, 'first', '+15551230000', 'me', 0, NULL);
            INSERT INTO ZWAMESSAGE VALUES (2, 1, 800000100, 'second', 'me', '+15551230000', 1, NULL);
            INSERT INTO ZWAMESSAGE VALUES (3, 2, 799000000, 'team note', '+15550001111', '12345-67890@g.us', 0, NULL);
            """
        )
        connection.close()
        self.store = WhatsAppStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_find_chats_returns_stable_ids(self) -> None:
        result = self.store.find_chats(query="sanjay")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["chats"][0]["id"], "+15551230000")
        self.assertEqual(result["chats"][0]["name"], "Sanjay")

    def test_read_messages_filters_by_chat(self) -> None:
        result = self.store.read_messages(chat_id="+15551230000")
        self.assertEqual([item["text"] for item in result["messages"]], ["second", "first"])
        self.assertTrue(result["messages"][0]["from_me"])

    def test_search_messages_can_join_chat_metadata(self) -> None:
        result = self.store.search_messages(query="note")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["messages"][0]["chat_id"], "12345-67890@g.us")
        self.assertEqual(result["messages"][0]["chat_name"], "Team")

    def test_database_is_read_only(self) -> None:
        with self.assertRaises(MCPError):
            with self.store.connection() as connection:
                connection.execute("CREATE TABLE should_not_exist (id INTEGER)")


if __name__ == "__main__":
    unittest.main()
