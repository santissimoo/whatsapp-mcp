use rusqlite::{Connection, OpenFlags, params_from_iter, types::Value as Sql};
use serde_json::{Value, json};
use std::{
    path::Path,
    time::{Duration, Instant},
};

type Result<T> = std::result::Result<T, String>;

pub fn open(path: &Path) -> Result<Connection> {
    let db = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)
        .map_err(|e| format!("Cannot open WhatsApp database: {e}. Check that WhatsApp Desktop is installed and logged in, the database path is correct, and macOS permits the launching app/helper to read it."))?;
    db.busy_timeout(Duration::from_millis(1500))
        .map_err(|e| e.to_string())?;
    db.execute_batch(
        "PRAGMA query_only=ON; PRAGMA cache_size=-256; PRAGMA mmap_size=0; PRAGMA temp_store=FILE;",
    )
    .map_err(|e| e.to_string())?;
    let start = Instant::now();
    db.progress_handler(1000, Some(move || start.elapsed() > Duration::from_secs(5)))
        .map_err(|e| e.to_string())?;
    Ok(db)
}

fn ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

// Explicit tables, a few known column aliases, and an actionable error on schema changes.
// Do not guess which arbitrary table contains private messages.
struct Table {
    name: &'static str,
    columns: Vec<String>,
}
impl Table {
    fn load(db: &Connection, name: &'static str) -> Result<Self> {
        let mut stmt = db
            .prepare(&format!("PRAGMA table_info({})", ident(name)))
            .map_err(|e| e.to_string())?;
        let columns = stmt
            .query_map([], |r| r.get(1))
            .map_err(|e| e.to_string())?
            .collect::<rusqlite::Result<Vec<String>>>()
            .map_err(|e| e.to_string())?;
        if columns.is_empty() {
            return Err(format!("Unsupported WhatsApp schema: missing {name}."));
        }
        Ok(Self { name, columns })
    }
    fn optional(&self, names: &[&str]) -> String {
        names
            .iter()
            .find_map(|n| self.columns.iter().find(|c| c.eq_ignore_ascii_case(n)))
            .map(|c| ident(c))
            .unwrap_or_else(|| "NULL".into())
    }
    fn required(&self, names: &[&str]) -> Result<String> {
        let col = self.optional(names);
        if col == "NULL" {
            Err(format!(
                "Unsupported WhatsApp schema: {} needs {}.",
                self.name,
                names.join(" or ")
            ))
        } else {
            Ok(col)
        }
    }
}

fn string<'a>(args: &'a Value, name: &str, required: bool) -> Result<Option<&'a str>> {
    match args.get(name) {
        None if !required => Ok(None),
        Some(Value::String(s)) if !s.trim().is_empty() && s.len() <= 4096 => Ok(Some(s)),
        _ => Err(format!(
            "{name} must be a nonempty string of at most 4096 bytes"
        )),
    }
}

fn page(
    args: &Value,
    order: &str,
    pk: &str,
    filters: &mut Vec<String>,
    params: &mut Vec<Sql>,
) -> Result<usize> {
    let limit = match args.get("limit") {
        None => 20,
        Some(v) => v
            .as_u64()
            .filter(|v| (1..=100).contains(v))
            .ok_or("limit must be an integer from 1 to 100")? as usize,
    };
    if let Some(cursor) = string(args, "cursor", false)? {
        let v: Value = serde_json::from_str(cursor)
            .map_err(|_| "Invalid cursor; use next_cursor from the previous page")?;
        let a = v
            .as_array()
            .filter(|a| a.len() == 2)
            .ok_or("Invalid cursor")?;
        let date = a[0]
            .as_f64()
            .filter(|v| v.is_finite())
            .ok_or("Invalid cursor date")?;
        let id = a[1].as_i64().ok_or("Invalid cursor id")?;
        filters.push(format!("({order}, {pk}) < (?, ?)"));
        params.extend([Sql::Real(date), Sql::Integer(id)]);
    }
    Ok(limit)
}

fn like(s: &str) -> Sql {
    Sql::Text(format!(
        "%{}%",
        s.replace('\\', "\\\\")
            .replace('%', "\\%")
            .replace('_', "\\_")
    ))
}

fn where_sql(filters: &[String]) -> String {
    if filters.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", filters.join(" AND "))
    }
}

fn rows(db: &Connection, sql: &str, params: Vec<Sql>, limit: usize, key: &str) -> Result<Value> {
    let mut stmt = db.prepare(sql).map_err(|e| e.to_string())?;
    let names: Vec<String> = stmt
        .column_names()
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
    let mut query = stmt
        .query(params_from_iter(params))
        .map_err(|e| e.to_string())?;
    let mut items = Vec::new();
    let mut cursor = None;
    let mut has_more = false;
    while let Some(row) = query.next().map_err(|e| e.to_string())? {
        if items.len() == limit {
            has_more = true;
            break;
        }
        let mut object = serde_json::Map::new();
        for (i, name) in names.iter().enumerate() {
            if name.starts_with('_') {
                continue;
            }
            let value = match row.get_ref(i).map_err(|e| e.to_string())? {
                rusqlite::types::ValueRef::Null => continue,
                rusqlite::types::ValueRef::Integer(v) if name == "from_me" => json!(v != 0),
                rusqlite::types::ValueRef::Integer(v) => json!(v),
                rusqlite::types::ValueRef::Real(v) => json!(v),
                rusqlite::types::ValueRef::Text(v) => json!(String::from_utf8_lossy(v)),
                rusqlite::types::ValueRef::Blob(_) => continue,
            };
            object.insert(name.clone(), value);
        }
        cursor = Some(
            json!([
                row.get::<_, f64>("_date").map_err(|e| e.to_string())?,
                row.get::<_, i64>("_id").map_err(|e| e.to_string())?
            ])
            .to_string(),
        );
        items.push(Value::Object(object));
    }
    let mut result = json!({key: items, "count": items.len()});
    if has_more {
        result["next_cursor"] = json!(cursor);
    }
    Ok(result)
}

fn chats(db: &Connection, args: &Value) -> Result<Value> {
    let t = Table::load(db, "ZWACHATSESSION")?;
    let pk = t.required(&["Z_PK"])?;
    let jid = t.required(&["ZCONTACTJID", "ZJID", "ZREMOTEJID"])?;
    let name = t.optional(&["ZPARTNERNAME", "ZDISPLAYNAME", "ZTITLE", "ZNAME"]);
    let date = t.optional(&["ZLASTMESSAGEDATE", "ZMESSAGEDATE"]);
    let order = format!("COALESCE({date}, 0)");
    let mut filters = Vec::new();
    let mut params = Vec::new();
    if let Some(q) = string(args, "query", false)? {
        filters.push(format!(
            "({jid} LIKE ? ESCAPE '\\' OR {name} LIKE ? ESCAPE '\\')"
        ));
        params.extend([like(q), like(q)]);
    }
    let limit = page(args, &order, &pk, &mut filters, &mut params)?;
    params.push(Sql::Integer((limit + 1) as i64));
    let sql = format!("SELECT COALESCE(NULLIF({jid}, ''), 'ZWACHATSESSION:' || {pk}) AS id,
        substr(COALESCE({name}, {jid}), 1, 300) AS name, {date} AS last_message_timestamp,
        strftime('%Y-%m-%dT%H:%M:%fZ', {date}+978307200, 'unixepoch') AS last_message_timestamp_iso,
        {order} AS _date, {pk} AS _id FROM ZWACHATSESSION{} ORDER BY {order} DESC, {pk} DESC LIMIT ?", where_sql(&filters));
    rows(db, &sql, params, limit, "chats")
}

fn messages(db: &Connection, args: &Value, search: bool) -> Result<Value> {
    let m = Table::load(db, "ZWAMESSAGE")?;
    let c = Table::load(db, "ZWACHATSESSION")?;
    let mpk = format!("m.{}", m.required(&["Z_PK"])?);
    let cpk = format!("c.{}", c.required(&["Z_PK"])?);
    let relation = format!("m.{}", m.required(&["ZCHATSESSION", "ZCHAT"])?);
    let jid = format!("c.{}", c.required(&["ZCONTACTJID", "ZJID", "ZREMOTEJID"])?);
    let col = |names: &[&str]| {
        let n = m.optional(names);
        if n == "NULL" { n } else { format!("m.{n}") }
    };
    let cname = c.optional(&["ZPARTNERNAME", "ZDISPLAYNAME", "ZTITLE", "ZNAME"]);
    let cname = if cname == "NULL" {
        cname
    } else {
        format!("c.{cname}")
    };
    let date = format!("m.{}", m.required(&["ZMESSAGEDATE", "ZSENTDATE", "ZDATE"])?);
    let text = format!("m.{}", m.required(&["ZTEXT", "ZBODY"])?);
    let order = format!("COALESCE({date}, 0)");
    let mut filters = Vec::new();
    let mut params = Vec::new();
    let chat = string(args, "chat_id", !search)?;
    if let Some(chat) = chat {
        if let Some(pk) = chat.strip_prefix("ZWACHATSESSION:") {
            filters.push(format!("{cpk} = ?"));
            params.push(Sql::Integer(pk.parse().map_err(|_| "Invalid chat_id")?));
        } else {
            filters.push(format!("{jid} = ?"));
            params.push(Sql::Text(chat.to_owned()));
        }
    }
    if search {
        let q = string(args, "query", true)?.unwrap();
        filters.push(format!("{text} LIKE ? ESCAPE '\\'"));
        params.push(like(q));
    }
    let limit = page(args, &order, &mpk, &mut filters, &mut params)?;
    params.push(Sql::Integer((limit + 1) as i64));
    let sql = format!(
        "SELECT {mpk} AS id, {date} AS timestamp,
        strftime('%Y-%m-%dT%H:%M:%fZ', {date}+978307200, 'unixepoch') AS timestamp_iso,
        substr({text}, 1, 2000) AS text, length({text}) > 2000 AS text_truncated,
        substr({}, 1, 300) AS \"from\", substr({}, 1, 300) AS \"to\", {} AS from_me,
        {} AS type, {} AS status,
        COALESCE(NULLIF({jid}, ''), 'ZWACHATSESSION:' || {cpk}) AS chat_id,
        substr({cname}, 1, 300) AS chat_name, {order} AS _date, {mpk} AS _id
        FROM ZWAMESSAGE m LEFT JOIN ZWACHATSESSION c ON {relation} = {cpk}{}
        ORDER BY {order} DESC, {mpk} DESC LIMIT ?",
        col(&["ZFROMJID", "ZSENDERJID"]),
        col(&["ZTOJID"]),
        col(&["ZISFROMME", "ZFROMME"]),
        col(&["ZMESSAGETYPE", "ZMEDIATYPE"]),
        col(&["ZMESSAGESTATUS", "ZSTATUS"]),
        where_sql(&filters)
    );
    let mut result = rows(db, &sql, params, limit, "messages")?;
    if let Some(chat) = chat {
        result["chat_id"] = json!(chat);
    }
    if search {
        result["query"] = args["query"].clone();
    }
    Ok(result)
}

pub fn call(path: &Path, tool: &str, args: &Value) -> Result<Value> {
    if tool == "whatsapp_status" {
        return Ok(match open(path) {
            Ok(db) => {
                let schema =
                    Table::load(&db, "ZWACHATSESSION").and_then(|_| Table::load(&db, "ZWAMESSAGE"));
                json!({"accessible": true, "schema_tables_present": schema.is_ok(), "schema_error": schema.err(), "read_only": true, "send_supported": false, "database_path": path})
            }
            Err(e) => {
                json!({"accessible": false, "read_only": true, "send_supported": false, "database_path": path, "error": e})
            }
        });
    }
    let db = open(path)?;
    match tool {
        "find_chats" => chats(&db, args),
        "read_messages" => messages(&db, args, false),
        "search_messages" => messages(&db, args, true),
        _ => Err(format!("Unknown tool: {tool}")),
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn connection_rejects_writes() {
        let path = std::env::temp_dir().join(format!(
            "whatsapp-mcp-readonly-{}.sqlite",
            std::process::id()
        ));
        rusqlite::Connection::open(&path)
            .unwrap()
            .execute("CREATE TABLE test(id INTEGER)", [])
            .unwrap();
        let db = super::open(&path).unwrap();
        assert!(db.execute("INSERT INTO test VALUES(1)", []).is_err());
        db.execute_batch("PRAGMA query_only=OFF").unwrap();
        assert!(db.execute("INSERT INTO test VALUES(1)", []).is_err());
        drop(db);
        std::fs::remove_file(path).unwrap();
    }
}
