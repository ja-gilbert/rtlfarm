"""The database layer: the connection, migration runner, and schema.

Plain ``sqlite3``, no ORM: the schema lives in numbered SQL files under
``migrations/``, and transactions are only ever explicit SQL on connections
opened in autocommit mode.
"""
