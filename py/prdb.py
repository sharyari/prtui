import sqlite3
from contextlib import contextmanager
from pathlib import Path
import config

DB_PATH = Path(config.read_config().get('db-path', str(config.CONFIG_PATH.parent / 'prtui.db')))

pr_table_creation_query = """
    CREATE TABLE IF NOT EXISTS PRS (
        number INT,
        repo CHAR(25),
        type CHAR(25),
        author CHAR(25),
        title CHAR(100),
        updated_at CHAR(30),
        read_at CHAR(30),
        approvals TEXT,
        PRIMARY KEY(repo, number)
    );
"""

comments_table_creation_query = """
    CREATE TABLE IF NOT EXISTS COMMENTS (
        id INT PRIMARY KEY,
        pr_number INT,
        pr_repo CHAR(25),
        user CHAR(25),
        path CHAR(100),
        diff_hunk TEXT,
        created_at CHAR(30),
        updated_at CHAR(30),
        in_reply_to_id INT,
        comment TEXT,
        type CHAR(20) DEFAULT 'comment',
        FOREIGN KEY (pr_repo, pr_number) REFERENCES PRS(repo, number) ON DELETE CASCADE
    )
"""

def db_exists():
    return DB_PATH.exists()


@contextmanager
def connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn.cursor()
        conn.commit()
    finally:
        conn.close()


def create_pr_table(cursor):
    cursor.execute(pr_table_creation_query)

def pr_insert(cursor, pr):
    cursor.execute(
        "INSERT INTO PRS (number, repo, type, author, title, updated_at, approvals)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(repo, number) DO UPDATE SET"
        " type=excluded.type, author=excluded.author,"
        " title=excluded.title, updated_at=excluded.updated_at,"
        " approvals=excluded.approvals",
        (pr["number"], pr["repo"], pr["type"], pr["author"],
         pr["title"], pr["updated_at"], pr.get("approvals", ""))
    )

def pr_get_all(cursor, type):
    cursor.execute(
        "SELECT number, repo, type, author, title, updated_at, read_at,"
        " approvals FROM PRS WHERE type=?", (type,)
    )
    return [dict(r) for r in cursor.fetchall()]

def pr_mark_read(cursor, repo, number):
    cursor.execute(
        "UPDATE PRS SET read_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
        " WHERE repo = ? AND number = ?",
        (repo, number)
    )

def create_comments_table(cursor):
    cursor.execute(comments_table_creation_query)

def comment_insert(cursor, comment):
    cursor.execute(
        "REPLACE INTO COMMENTS VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (comment["id"], comment["pr_number"], comment["pr_repo"],
         comment["user"], comment["path"], comment["diff_hunk"],
         comment["created_at"], comment["updated_at"],
         comment.get("in_reply_to_id"), comment["body"],
         comment.get("type", "comment"))
    )

def pr_get_updated_at(cursor):
    """Return {(repo, number): updated_at} for all PRs."""
    cursor.execute("SELECT repo, number, updated_at FROM PRS")
    return {(r["repo"], r["number"]): r["updated_at"] for r in cursor.fetchall()}

def pr_delete(cursor, repo, number):
    """Delete a PR and its comments."""
    cursor.execute("DELETE FROM COMMENTS WHERE pr_repo = ? AND pr_number = ?",
                   (repo, number))
    cursor.execute("DELETE FROM PRS WHERE repo = ? AND number = ?",
                   (repo, number))

def get_comments(cursor, pr_number, pr_repo):
    cursor.execute(
        "SELECT id, pr_number, pr_repo, user, path, diff_hunk,"
        " created_at, updated_at, in_reply_to_id, comment, type"
        " FROM COMMENTS WHERE pr_number = ? AND pr_repo = ?"
        " ORDER BY created_at DESC",
        (pr_number, pr_repo)
    )
    return [dict(r) for r in cursor.fetchall()]

def get_latest_comment(cursor, pr_number, pr_repo, user, *, type=None, not_type=None):
    """Return the most recent comment row by user, optionally filtered by type."""
    sql = ("SELECT id, pr_number, pr_repo, user, path, diff_hunk,"
           " created_at, updated_at, in_reply_to_id, comment, type"
           " FROM COMMENTS"
           " WHERE pr_number = ? AND pr_repo = ? AND user = ?")
    params = [pr_number, pr_repo, user]
    if type:
        sql += " AND type = ?"
        params.append(type)
    elif not_type:
        sql += " AND type != ?"
        params.append(not_type)
    sql += " ORDER BY created_at DESC LIMIT 1"
    cursor.execute(sql, params)
    row = cursor.fetchone()
    return dict(row) if row else None
