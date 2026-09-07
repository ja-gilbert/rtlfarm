"""The database layer: the connection, migration runner, and schema.

Everything in this package is plain ``sqlite3``. There is no ORM; the schema
lives in numbered SQL files under ``migrations/``, and transactions are
controlled only by explicit SQL (``BEGIN IMMEDIATE``, ``COMMIT``,
``ROLLBACK``) on connections opened in autocommit mode.
"""
