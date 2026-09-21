# WhatsApp Local MCP

This is a small, read-only MCP server for the WhatsApp macOS desktop database. It is designed for agents that need to search or read conversations without screenshots, chat-list scrolling, or repeated UI navigation.

Tools:

- `whatsapp_status` — verify local database access and show the detected schema, without returning message bodies.
- `find_chats` — find chats by display name or WhatsApp JID.
- `read_messages` — read a bounded page of recent messages for a chat.
- `search_messages` — search message text, optionally within one chat.

The server discovers WhatsApp's Core Data table and column names at runtime because WhatsApp changes its local schema between desktop releases. Every database connection is opened with SQLite read-only mode and `PRAGMA query_only=ON`.

Sending is intentionally not exposed yet. Writing directly to the local database would bypass WhatsApp's delivery and encryption machinery and could corrupt the account state. A separate, guarded send transport can be added once a safe supported route is selected.

## Local setup

The default database path is:

```text
~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite
```

Override it with `WHATSAPP_CHAT_DB` when testing or when WhatsApp changes its storage location.

Run the server directly:

```sh
/opt/homebrew/bin/python3 /Users/santiagopineda/Projects/whatsapp-mcp/whatsapp_mcp/server.py
```

On macOS, the WhatsApp group container is protected by privacy controls. The runtime that launches this process needs access to it. Prefer granting access to a dedicated signed launcher; adding the Python runtime to Full Disk Access is broader and should be treated as a last resort.

## Codex MCP configuration

Codex uses a local stdio server entry in `~/.codex/config.toml`:

```toml
[mcp_servers.whatsapp_local]
command = "/opt/homebrew/bin/python3"
args = ["/Users/santiagopineda/Projects/whatsapp-mcp/whatsapp_mcp/server.py"]
env = { WHATSAPP_CHAT_DB = "/Users/santiagopineda/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite" }
```

The same server can be registered with Executor's local MCP catalog once the runtime has access to the database.
