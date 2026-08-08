from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretStoreError(RuntimeError):
    pass


class SecretStore:
    """Encrypted service-owned configuration for the administrative adapter."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.db_path = directory / "state.db"
        self.key_path = directory / "key"
        self._key = self._load_key()
        self._fernet = Fernet(self._key)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS secrets ("
                "name TEXT PRIMARY KEY, value BLOB NOT NULL, updated_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                "name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            db.commit()
        self.db_path.chmod(0o600)

    def _load_key(self) -> bytes:
        if self.key_path.exists():
            value = self.key_path.read_bytes().strip()
            try:
                Fernet(value)
            except (TypeError, ValueError) as exc:
                raise SecretStoreError("clave local inválida") from exc
            self.key_path.chmod(0o600)
            return value
        value = Fernet.generate_key()
        self.key_path.write_bytes(value)
        self.key_path.chmod(0o600)
        return value

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def set_secret(self, name: str, value: str) -> None:
        encrypted = self._fernet.encrypt(value.encode())
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO secrets(name,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (name, encrypted, self._now()),
            )
            db.commit()

    def get_secret(self, name: str) -> str | None:
        with sqlite3.connect(self.db_path) as db:
            row = db.execute("SELECT value FROM secrets WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        try:
            return self._fernet.decrypt(bytes(row[0])).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise SecretStoreError("no se pudo descifrar un secreto local") from exc

    def secret_status(self, name: str) -> dict[str, object]:
        with sqlite3.connect(self.db_path) as db:
            row = db.execute("SELECT updated_at,value FROM secrets WHERE name=?", (name,)).fetchone()
        return {
            "configured": bool(row and bytes(row[1]) and self._fernet.decrypt(bytes(row[1]))),
            "updated_at": row[0] if row else None,
        }

    def delete_secret(self, name: str) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute("DELETE FROM secrets WHERE name=?", (name,))
            db.commit()

    def set_setting(self, name: str, value: str) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO settings(name,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (name, value, self._now()),
            )
            db.commit()

    def get_setting(self, name: str) -> str | None:
        with sqlite3.connect(self.db_path) as db:
            row = db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return str(row[0]) if row else None

    def delete_setting(self, name: str) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute("DELETE FROM settings WHERE name=?", (name,))
            db.commit()
