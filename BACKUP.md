# Encrypted data backups

`/db_export` is available only to ADMIN_ID in a private chat. The export uses
the bot's internal DATABASE_URL and a dedicated read-only REPEATABLE READ
connection. It does not require pg_dump or public PostgreSQL networking.
The worker does not block Telegram polling. Only one export can run at a time,
with at least one minute between attempts.

The versioned `.enc` file contains all public application tables as PostgreSQL
CSV plus a schema manifest and sequence counters. Encryption is Fernet with the
same SHA-256-derived APP_MASTER_KEY used for cached data. Keep that original
key in a password manager, separately from the backup. The key, bot token,
payment credentials, database password and environment variables are NOT
included. Telegram file IDs are included, not copies of remote media files.

This is a data backup, not a pg_dump file: keep this code version to recreate
the schema. Roles, extensions and server settings are not included. Legacy
`.enc` files from older implementations are deliberately rejected: their format
and key derivation have not been verified. Do not rename one into the new format.

Limits: 120 MiB raw CSV, 30 MiB compressed archive, 42 MiB encrypted file.
Large databases require a separate pg_dump backup. Temporary files are removed
on completion or handled errors; an abrupt process termination may leave files
until the container's ephemeral storage is removed. Do not persist its temp dir.

## Verify and restore on an offline destination

Do not post backups, database URLs or keys into chat or commit them to Git.
The following commands run on an operator-controlled machine/container with
the project's Python dependencies installed. Supply secrets via its private
environment, not command-line arguments. APP_MASTER_KEY must be the original
key; DATABASE_URL must reference the NEW destination database.

1. Keep the source running for a preliminary backup and verify it:
   `python db_backup.py inspect <backup.enc>`.
2. Prepare an empty destination database. With the destination bot stopped,
   initialize only the schema: `python -c "import database; database.init_db()"`.
3. Restore: `python db_backup.py restore <backup.enc> --confirm-empty-target`.
   Any existing rows or column/type mismatches abort the import. There is no
   DROP or TRUNCATE. All table imports share one transaction. Sequence setval
   is not transactional in PostgreSQL: on failure an empty target can retain
   advanced counters; no existing table data is overwritten.
4. Verify counts, subscriptions, payment records and connections before cutover.

This is a snapshot, NOT replication. A preliminary export excludes later writes.
For final cutover stop new checkouts and all source database writers before the
final snapshot (use the offline exporter below, not the still-running bot).
Reconcile pending Platega invoices and switch callbacks carefully; provider
retries are finite. Do not run two polling instances with the same BOT_TOKEN.
Retain the old database and an offline backup until verification is complete.

An offline script can use `db_backup.encrypted_backup(DATABASE_URL,
APP_MASTER_KEY)` as a context manager, copying its result to a secure directory
before leaving the context. The built-in context deletes temporary files after
use.

## Restore through Telegram

Use a NEW empty destination database, not the running production database.
Stop the old polling instance before starting the destination with the same
BOT_TOKEN. Set the original APP_MASTER_KEY and ADMIN_ID on the destination,
its new DATABASE_URL, and DB_IMPORT_MODE=1 before its first launch.

In restore mode the bot initializes the schema but does not run cleanup,
reminders, normal message handlers or payment handlers. Platega callbacks get
HTTP 503, not a false success. Do not switch live payments to this endpoint until
restoration is finished; pending payments still need reconciliation.

1. Send /db_import in the administrator's private chat.
2. Send a new-format .enc document (up to 20,000,000 bytes). Telegram's standard
   getFile download limit is lower than its upload limit; larger files require
   the offline restore script.
3. After key/format/integrity validation, press the confirmation button within
   10 minutes. A wrong key, legacy format, nonempty target or schema mismatch
   prevents restoration. /cancel removes the staged file.
4. After success, set DB_IMPORT_MODE=0 and restart the destination. Keep the
   old bot stopped. Check subscriptions and payments before deleting anything.

The restore process keeps getUpdates offset=0, so queued payment/business events
are not acknowledged and can be handled after normal startup. It deduplicates
admin actions in memory while waiting. Telegram returns only a bounded first
batch: a backlog of 100 older updates can prevent new import commands from being
visible. Use offline restore in that case; never discard pending updates just
to reach an import command. Telegram update retention and provider callback
retry windows are finite; do not leave the service in this mode indefinitely.
Temporary staging is lost on restart and must be uploaded again.

Install the updated bot.py AND db_backup.py on the SOURCE service first to
restore `/db_export`. Deploying only to the new, empty project cannot back up the
old database. No production deployment is performed by the local code changes.
