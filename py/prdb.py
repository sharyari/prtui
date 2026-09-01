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
        mergeable INT,
        ci_url TEXT,
        head_sha TEXT,
        ci_sha TEXT,
        draft INT DEFAULT 0,
        head_ref TEXT,
        base_ref TEXT,
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
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn.cursor()
        conn.commit()
    finally:
        conn.close()


def create_pr_table(cursor):
    cursor.execute(pr_table_creation_query)
    # Migrate existing DBs that predate the mergeable column.
    try:
        cursor.execute("ALTER TABLE PRS ADD COLUMN mergeable INT")
    except Exception:
        pass
    # Migrate existing DBs that predate the ci_url column.
    try:
        cursor.execute("ALTER TABLE PRS ADD COLUMN ci_url TEXT")
    except Exception:
        pass
    # Migrate existing DBs that predate the head_sha/ci_sha columns.
    try:
        cursor.execute("ALTER TABLE PRS ADD COLUMN head_sha TEXT")
        cursor.execute("ALTER TABLE PRS ADD COLUMN ci_sha TEXT")
    except Exception:
        pass
    # Migrate existing DBs that predate the draft column.
    try:
        cursor.execute("ALTER TABLE PRS ADD COLUMN draft INT DEFAULT 0")
    except Exception:
        pass
    # Migrate existing DBs that predate the head_ref/base_ref columns.
    try:
        cursor.execute("ALTER TABLE PRS ADD COLUMN head_ref TEXT")
        cursor.execute("ALTER TABLE PRS ADD COLUMN base_ref TEXT")
    except Exception:
        pass
    # Migrate existing DBs that predate the ci_state column.
    try:
        cursor.execute("ALTER TABLE PRS ADD COLUMN ci_state TEXT")
    except Exception:
        pass

def pr_insert(cursor, pr):
    read_at = pr["updated_at"] if pr["type"] == "mine" else None
    cursor.execute(
        "INSERT INTO PRS (number, repo, type, author, title, updated_at, read_at, approvals, mergeable, ci_url, head_sha, ci_sha, draft, head_ref, base_ref, ci_state)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(repo, number) DO UPDATE SET"
        " type=excluded.type, author=excluded.author,"
        " title=excluded.title, updated_at=excluded.updated_at,"
        " approvals=excluded.approvals, mergeable=excluded.mergeable,"
        " ci_url=COALESCE(excluded.ci_url, ci_url),"
        " head_sha=excluded.head_sha,"
        " ci_sha=COALESCE(excluded.ci_sha, ci_sha),"
        " draft=excluded.draft,"
        " head_ref=excluded.head_ref,"
        " base_ref=excluded.base_ref,"
        " ci_state=COALESCE(excluded.ci_state, ci_state),"
        " read_at=COALESCE(read_at, excluded.read_at)",
        (pr["number"], pr["repo"], pr["type"], pr["author"],
         pr["title"], pr["updated_at"], read_at, pr.get("approvals", ""),
         pr.get("mergeable"), pr.get("ci_url"), pr.get("head_sha"), pr.get("ci_sha"),
         int(bool(pr.get("draft"))), pr.get("head_ref"), pr.get("base_ref"),
         pr.get("ci_state"))
    )

def pr_get_all(cursor, type):
    cursor.execute(
        "SELECT number, repo, type, author, title, updated_at, read_at,"
        " approvals, mergeable, ci_url, head_sha, ci_sha, draft, head_ref, base_ref, ci_state FROM PRS WHERE type=?", (type,)
    )
    return [dict(r) for r in cursor.fetchall()]

def pr_get_ci_url(cursor, repo, number):
    cursor.execute(
        "SELECT ci_url FROM PRS WHERE repo = ? AND number = ?",
        (repo, number)
    )
    row = cursor.fetchone()
    return row["ci_url"] if row else None

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

def pr_get_missing_ci_state(cursor):
    """Return set of (repo, number) for PRs with NULL ci_state."""
    cursor.execute("SELECT repo, number FROM PRS WHERE ci_state IS NULL")
    return {(r["repo"], r["number"]) for r in cursor.fetchall()}

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


