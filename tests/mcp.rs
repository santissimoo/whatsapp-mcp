use rusqlite::Connection;
use serde_json::{Value, json};
use std::{
    fs,
    io::Write,
    path::PathBuf,
    process::{Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
};

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture {
    dir: PathBuf,
    db: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let dir = std::env::temp_dir().join(format!(
            "whatsapp-mcp-test-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&dir).unwrap();
        let db = dir.join("ChatStorage.sqlite");
        Connection::open(&db).unwrap().execute_batch("
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZLASTMESSAGEDATE REAL);
            CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMESSAGEDATE REAL, ZTEXT TEXT, ZFROMJID TEXT, ZISFROMME INTEGER);
            CREATE INDEX message_chat_date ON ZWAMESSAGE(ZCHATSESSION, ZMESSAGEDATE, Z_PK);
            INSERT INTO ZWACHATSESSION VALUES (1, 'alice@example.test', 'Alice', 800000001), (2, 'team@example.test', 'Team', 800000000);
            INSERT INTO ZWAMESSAGE VALUES (1,1,800000000,'first','alice@example.test',0), (2,1,800000001,'second','self',1), (3,1,800000001,'100%_ready','self',1), (4,2,800000000,'team note','bob@example.test',0);
        ").unwrap();
        Self { dir, db }
    }
    fn run(&self, requests: &[Value]) -> Vec<Value> {
        let mut child = Command::new(env!("CARGO_BIN_EXE_whatsapp-mcp"))
            .env("WHATSAPP_CHAT_DB", &self.db)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut input = child.stdin.take().unwrap();
        for r in requests {
            writeln!(input, "{r}").unwrap();
        }
        drop(input);
        let result = child.wait_with_output().unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        String::from_utf8(result.stdout)
            .unwrap()
            .lines()
            .map(|s| serde_json::from_str(s).unwrap())
            .collect()
    }
    fn tool(&self, name: &str, args: Value) -> Value {
        self.run(&[json!({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":name,"arguments":args}})])[0]["result"].clone()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.dir).unwrap();
    }
}

#[test]
fn protocol_and_errors() {
    let f = Fixture::new();
    let r = f.run(&[
        json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}),
        json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
        json!({"jsonrpc":"2.0","id":2,"method":"tools/list"}),
        json!({"jsonrpc":"2.0","id":3,"method":"unknown"}),
        json!({"jsonrpc":"2.0","method":"ping","params":3}),
        json!({"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"send_message"}}),
    ]);
    assert_eq!(r.len(), 4);
    assert_eq!(r[0]["result"]["protocolVersion"], "2025-06-18");
    assert_eq!(r[1]["result"]["tools"].as_array().unwrap().len(), 4);
    assert_eq!(r[2]["error"]["code"], -32601);
    assert_eq!(r[3]["error"]["code"], -32602);
}

#[test]
fn chats_messages_and_literal_search() {
    let f = Fixture::new();
    let chats = f.tool("find_chats", json!({"query":"ALICE"}));
    assert_eq!(
        chats["structuredContent"]["chats"][0]["id"],
        "alice@example.test"
    );
    let m = f.tool("read_messages", json!({"chat_id":"alice@example.test"}));
    assert_eq!(m["isError"], false);
    let msgs = m["structuredContent"]["messages"].as_array().unwrap();
    assert_eq!(
        msgs.iter()
            .map(|v| v["id"].as_i64().unwrap())
            .collect::<Vec<_>>(),
        vec![3, 2, 1]
    );
    assert_eq!(msgs[0]["from_me"], true);
    assert!(
        msgs[0]["timestamp_iso"]
            .as_str()
            .unwrap()
            .starts_with("2026-")
    );
    assert_eq!(
        f.tool("search_messages", json!({"query":"%_"}))["structuredContent"]["count"],
        1
    );
    assert_eq!(
        f.tool("search_messages", json!({"query":"' OR 1=1 --"}))["structuredContent"]["count"],
        0
    );
    let result = f.tool("search_messages", json!({"query":"note"}));
    assert_eq!(
        result["structuredContent"]["messages"][0]["chat_name"],
        "Team"
    );
}

#[test]
fn keyset_pagination_survives_new_messages_and_equal_timestamps() {
    let f = Fixture::new();
    let first = f.tool(
        "read_messages",
        json!({"chat_id":"alice@example.test","limit":1}),
    );
    let cursor = first["structuredContent"]["next_cursor"].clone();
    Connection::open(&f.db).unwrap().execute("INSERT INTO ZWAMESSAGE (Z_PK,ZCHATSESSION,ZMESSAGEDATE,ZTEXT) VALUES (5,1,800000002,'new')", []).unwrap();
    let second = f.tool(
        "read_messages",
        json!({"chat_id":"alice@example.test","limit":2,"cursor":cursor}),
    );
    let messages = second["structuredContent"]["messages"].as_array().unwrap();
    assert_eq!(
        messages
            .iter()
            .map(|v| v["id"].as_i64().unwrap())
            .collect::<Vec<_>>(),
        vec![2, 1]
    );
    assert!(second["structuredContent"].get("next_cursor").is_none());
}

#[test]
fn validation_truncation_and_missing_database() {
    let mut f = Fixture::new();
    for args in [
        json!({"limit":0}),
        json!({"limit":101}),
        json!({"limit":"5"}),
        json!({"cursor":"offset:0"}),
    ] {
        assert_eq!(f.tool("find_chats", args)["isError"], true);
    }
    let text = "🦀".repeat(3000);
    Connection::open(&f.db)
        .unwrap()
        .execute("UPDATE ZWAMESSAGE SET ZTEXT=? WHERE Z_PK=3", [&text])
        .unwrap();
    let m = f.tool(
        "read_messages",
        json!({"chat_id":"alice@example.test","limit":1}),
    );
    assert_eq!(
        m["structuredContent"]["messages"][0]["text"]
            .as_str()
            .unwrap()
            .chars()
            .count(),
        2000
    );
    assert_eq!(m["structuredContent"]["messages"][0]["text_truncated"], 1);
    f.db = f.dir.join("nonexistent.sqlite");
    assert_eq!(
        f.tool("whatsapp_status", json!({}))["structuredContent"]["accessible"],
        false
    );
    assert!(!f.db.exists());
}

#[test]
fn reads_live_wal_without_changing_database() {
    let f = Fixture::new();
    let db = Connection::open(&f.db).unwrap();
    db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; INSERT INTO ZWAMESSAGE(Z_PK,ZCHATSESSION,ZMESSAGEDATE,ZTEXT) VALUES(5,1,800000002,'in WAL');").unwrap();
    let before = fs::read(&f.db).unwrap();
    let wal_path = f.dir.join("ChatStorage.sqlite-wal");
    let wal_before = fs::read(&wal_path).unwrap();
    let r = f.tool(
        "read_messages",
        json!({"chat_id":"alice@example.test","limit":1}),
    );
    assert_eq!(r["structuredContent"]["messages"][0]["text"], "in WAL");
    assert_eq!(fs::read(&f.db).unwrap(), before);
    assert_eq!(fs::read(&wal_path).unwrap(), wal_before);
}

#[test]
fn unknown_schema_fails_clearly() {
    let f = Fixture::new();
    Connection::open(&f.db)
        .unwrap()
        .execute("DROP TABLE ZWAMESSAGE", [])
        .unwrap();
    let r = f.tool("read_messages", json!({"chat_id":"alice@example.test"}));
    assert_eq!(r["isError"], true);
    assert!(
        r["structuredContent"]["error"]
            .as_str()
            .unwrap()
            .contains("missing ZWAMESSAGE")
    );
}
