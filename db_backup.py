"""Encrypted application-data snapshots; no external PostgreSQL client required.

Restore only into an empty database initialized by this version of database.py.
This is not a pg_dump archive and does not include roles or server configuration.
"""

import base64
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

MAGIC = b"DIALOGDEL-DATA-1\n"
MAX_ARCHIVE = 30 * 1024 * 1024
MAX_RAW = 120 * 1024 * 1024
MAX_ENCRYPTED = 42 * 1024 * 1024


class BackupError(Exception):
    pass


def cipher(master_key):
    if not master_key or not master_key.strip():
        raise BackupError("APP_MASTER_KEY is missing")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(master_key.strip().encode()).digest()))


def identifier(name):
    # The application schema uses only simple lower-case identifiers.
    if not isinstance(name, str) or not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise BackupError("Unsupported database identifier")
    return '"' + name + '"'


def qualified(name):
    return '"public".' + identifier(name)


def schema(cursor):
    cursor.execute("""
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
               a.attgenerated, a.attidentity
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid
        WHERE n.nspname='public' AND c.relkind IN ('r', 'p')
          AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY c.relname, a.attname
    """)
    tables = {}
    for table, column, data_type, generated, identity in cursor.fetchall():
        identifier(table)
        identifier(column)
        if generated or identity:
            raise BackupError("Generated columns require pg_dump instead")
        tables.setdefault(table, []).append([column, data_type])
    if "users" not in tables:
        raise BackupError("This is not an initialized bot database")
    cursor.execute("""
        SELECT child.relname, parent.relname
        FROM pg_constraint con
        JOIN pg_class child ON child.oid=con.conrelid
        JOIN pg_namespace n ON n.oid=child.relnamespace
        JOIN pg_class parent ON parent.oid=con.confrelid
        JOIN pg_namespace pn ON pn.oid=parent.relnamespace
        WHERE con.contype='f' AND n.nspname='public' AND pn.nspname='public'
    """)
    parents = {table: set() for table in tables}
    for child, parent in cursor.fetchall():
        parents[child].add(parent)
    ordered = []
    while parents:
        ready = sorted(table for table, deps in parents.items() if not deps.difference(ordered))
        if not ready:
            raise BackupError("Cyclic foreign keys require pg_dump instead")
        for table in ready:
            ordered.append(table)
            del parents[table]
    return [{"name": table, "columns": tables[table]} for table in ordered]


class LimitedWriter:
    def __init__(self, output, budget):
        self.output, self.budget = output, budget

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.budget[0] += len(data)
        if self.budget[0] > MAX_RAW:
            raise BackupError("Database exceeds the bot backup size limit")
        return self.output.write(data)


def export_archive(connection, path):
    connection.set_session(isolation_level="REPEATABLE READ", readonly=True, autocommit=False)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '120s'")
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            cursor.execute("SET LOCAL DateStyle = 'ISO, YMD'")
            cursor.execute("SET LOCAL client_encoding = 'UTF8'")
            tables = schema(cursor)
            manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                        "tables": tables, "sequences": []}
            budget = [0]
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for table in tables:
                    name = table["name"]
                    columns = ", ".join(identifier(c[0]) for c in table["columns"])
                    with archive.open(name + ".csv", "w") as output:
                        cursor.copy_expert(
                            f"COPY {qualified(name)} ({columns}) TO STDOUT WITH (FORMAT CSV, ENCODING 'UTF8')",
                            LimitedWriter(output, budget),
                        )
                    if archive.fp.tell() > MAX_ARCHIVE:
                        raise BackupError("Compressed backup exceeds the bot size limit")
                cursor.execute("""SELECT c.relname FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND c.relkind='S' ORDER BY c.relname""")
                sequences = [row[0] for row in cursor.fetchall()]
                for name in sequences:
                    cursor.execute(f"SELECT last_value, is_called FROM {qualified(name)}")
                    value, called = cursor.fetchone()
                    manifest["sequences"].append([name, value, called])
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    finally:
        connection.rollback()


@contextmanager
def encrypted_backup(database_url, master_key, connect=None):
    encryption = cipher(master_key)
    if not database_url:
        raise BackupError("DATABASE_URL is missing")
    if connect is None:
        import psycopg2
        connect = psycopg2.connect
    with tempfile.TemporaryDirectory(prefix="dialogdel-backup-") as directory:
        plain = Path(directory) / "data.zip"
        connection = connect(database_url, sslmode="require", connect_timeout=10)
        try:
            export_archive(connection, plain)
        finally:
            connection.close()
        if plain.stat().st_size > MAX_ARCHIVE:
            raise BackupError("Compressed backup exceeds the bot size limit")
        token = encryption.encrypt(plain.read_bytes())
        output = Path(directory) / ("dialogdelbot-backup-v1-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".enc")
        output.write_bytes(MAGIC + token)
        plain.unlink()
        if output.stat().st_size > MAX_ENCRYPTED:
            raise BackupError("Encrypted backup exceeds the bot size limit")
        yield str(output)


def open_archive(path, master_key):
    if Path(path).stat().st_size > MAX_ENCRYPTED:
        raise BackupError("Backup is too large")
    payload = Path(path).read_bytes()
    if not payload.startswith(MAGIC):
        raise BackupError("Unknown backup format; legacy .enc files are not supported")
    try:
        plain = cipher(master_key).decrypt(payload[len(MAGIC):])
    except InvalidToken:
        raise BackupError("Wrong APP_MASTER_KEY or corrupted backup") from None
    archive = zipfile.ZipFile(io.BytesIO(plain))
    try:
        entries = archive.infolist()
        if len(entries) > 1000 or sum(item.file_size for item in entries) > MAX_RAW + 1024 * 1024:
            raise BackupError("Unpacked backup is too large")
        if archive.getinfo("manifest.json").file_size > 1024 * 1024:
            raise BackupError("Manifest is too large")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest["format"] != 1:
            raise BackupError("Unsupported backup version")
        expected = {"manifest.json"} | {t["name"] + ".csv" for t in manifest["tables"]}
        if len(expected) != len(manifest["tables"]) + 1 or sorted(expected) != sorted(i.filename for i in entries):
            raise BackupError("Invalid backup contents")
        if archive.testzip() is not None:
            raise BackupError("Corrupted backup archive")
        return archive, manifest
    except Exception:
        archive.close()
        raise


def restore_empty(connection, archive, manifest):
    """No DROP/TRUNCATE; refuse any nonempty destination. Caller must stop bot."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute("SET LOCAL statement_timeout = '120s'")
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            cursor.execute("SET LOCAL DateStyle = 'ISO, YMD'")
            cursor.execute("SET LOCAL client_encoding = 'UTF8'")
            tables = schema(cursor)
            if tables != manifest["tables"]:
                raise BackupError("Schema mismatch; use the same bot version on both sides")
            cursor.execute("LOCK TABLE " + ", ".join(qualified(t["name"]) for t in tables) + " IN ACCESS EXCLUSIVE MODE")
            for table in tables:
                cursor.execute(f"SELECT EXISTS (SELECT 1 FROM {qualified(table['name'])})")
                if cursor.fetchone()[0]:
                    raise BackupError("Destination is not empty; restore refused")
            for table in tables:
                columns = ", ".join(identifier(c[0]) for c in table["columns"])
                with archive.open(table["name"] + ".csv") as source:
                    cursor.copy_expert(
                        f"COPY {qualified(table['name'])} ({columns}) FROM STDIN WITH (FORMAT CSV, ENCODING 'UTF8')", source,
                    )
            for name, value, called in manifest["sequences"]:
                cursor.execute("SELECT setval(%s::regclass, %s, %s)", (qualified(name), value, called))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "restore"))
    parser.add_argument("file")
    parser.add_argument("--confirm-empty-target", action="store_true")
    args = parser.parse_args()
    try:
        archive, manifest = open_archive(args.file, os.environ.get("APP_MASTER_KEY", ""))
        with archive:
            if args.action == "inspect":
                print("Verified backup:", manifest["created_at"], "tables:", len(manifest["tables"]))
                return
            if not args.confirm_empty_target:
                raise BackupError("Stop the destination bot, initialize its empty schema, then pass --confirm-empty-target")
            import psycopg2
            connection = psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require", connect_timeout=10)
            try:
                restore_empty(connection, archive, manifest)
            finally:
                connection.close()
            print("Data restored. Keep the original APP_MASTER_KEY on the destination bot.")
    except Exception as exc:
        # Driver exception text may contain credentials or private message data.
        print(str(exc) if isinstance(exc, BackupError) else "Backup operation failed: " + type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
