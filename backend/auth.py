"""Authentication, sessions, roles, and invites for NexusSOP.

Implements:
- Password hashing with pbkdf2_hmac (SHA-256, 200k iterations)
- Session cookies with HttpOnly, SameSite=Lax, 14-day expiry
- Role-based access control (admin, engineer)
- Invite codes for engineer account creation
"""

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import HTTPException, Request, Response, Cookie
from pydantic import BaseModel


# Configuration
SESSION_COOKIE_NAME = "nexus_sop_session"
SESSION_EXPIRY_DAYS = 14
PBKDF2_ITERATIONS = 200_000
COOKIE_SECURE = os.environ.get("SOP_COOKIE_SECURE", "0") == "1"
DEFAULT_ADMIN_USER = os.environ.get("SOP_ADMIN_USER", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("SOP_ADMIN_PASSWORD", "admin")


class User(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    must_change_password: bool


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    display_name: str
    password: str
    role: str


class AcceptInviteRequest(BaseModel):
    username: str
    display_name: str
    password: str


def hash_password(password: str, salt: Optional[bytes] = None) -> tuple[bytes, bytes]:
    """Hash a password using pbkdf2_hmac (SHA-256, 200k iterations).

    Returns (salt, hash) as bytes.
    """
    if salt is None:
        salt = secrets.token_bytes(32)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return salt, password_hash


def verify_password(password: str, salt: bytes, password_hash: bytes) -> bool:
    """Verify a password against a stored hash."""
    _, computed_hash = hash_password(password, salt)
    return secrets.compare_digest(computed_hash, password_hash)


def create_session_token() -> str:
    """Generate a cryptographically random session token."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """Hash a session token for storage (SHA-256)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def now() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_auth_tables(connection: sqlite3.Connection) -> None:
    """Initialize authentication-related database tables."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL,
            password_hash BLOB NOT NULL,
            password_salt BLOB NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'engineer')),
            disabled INTEGER NOT NULL DEFAULT 0,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS auth_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            created_by INTEGER NOT NULL,
            room_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            used_by INTEGER,
            used_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(created_by) REFERENCES users(id),
            FOREIGN KEY(room_id) REFERENCES rooms(id),
            FOREIGN KEY(used_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS room_members (
            room_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TEXT NOT NULL,
            PRIMARY KEY (room_id, user_id),
            FOREIGN KEY(room_id) REFERENCES rooms(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )


def ensure_default_admin(connection: sqlite3.Connection) -> bool:
    """Create default admin user if users table is empty.

    Returns True if a new admin was created (first boot).
    """
    user_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count == 0:
        salt, password_hash = hash_password(DEFAULT_ADMIN_PASSWORD)
        connection.execute(
            "INSERT INTO users (username, display_name, password_hash, password_salt, role, must_change_password, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (DEFAULT_ADMIN_USER, "Administrator", password_hash, salt, "admin", now()),
        )
        return True
    return False


def ensure_default_room(connection: sqlite3.Connection) -> None:
    """Create the default 'Shared floor' room if it doesn't exist."""
    room = connection.execute(
        "SELECT id FROM rooms WHERE name = 'Shared floor'"
    ).fetchone()
    if not room:
        connection.execute(
            "INSERT INTO rooms (name, created_at) VALUES ('Shared floor', ?)",
            (now(),),
        )
        room_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Add all existing users to the room
        connection.execute(
            "INSERT INTO room_members (room_id, user_id, joined_at) "
            "SELECT ?, id, ? FROM users",
            (room_id, now()),
        )


def get_user_by_username(
    connection: sqlite3.Connection, username: str
) -> Optional[sqlite3.Row]:
    """Fetch a user by username (case-insensitive)."""
    return connection.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
        (username,),
    ).fetchone()


def get_user_by_id(connection: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    """Fetch a user by ID."""
    return connection.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()


def get_session_user(
    connection: sqlite3.Connection, session_token: str
) -> Optional[sqlite3.Row]:
    """Validate a session token and return the associated user."""
    token_hash = hash_session_token(session_token)
    session = connection.execute(
        """SELECT user_id, expires_at FROM auth_sessions
           WHERE token_hash = ?""",
        (token_hash,),
    ).fetchone()
    if not session:
        return None

    # Check expiry
    expires_at = datetime.fromisoformat(session["expires_at"])
    if datetime.now(timezone.utc) > expires_at:
        # Delete expired session
        connection.execute(
            "DELETE FROM auth_sessions WHERE token_hash = ?",
            (token_hash,),
        )
        return None

    user = get_user_by_id(connection, session["user_id"])
    if user and user["disabled"]:
        return None

    return user


def create_session(
    connection: sqlite3.Connection, user_id: int
) -> tuple[str, str]:
    """Create a new session for a user.

    Returns (session_token, expires_at).
    """
    token = create_session_token()
    token_hash = hash_session_token(token)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_EXPIRY_DAYS)).isoformat(
        timespec="seconds"
    )
    connection.execute(
        "INSERT INTO auth_sessions (user_id, token_hash, expires_at, created_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, token_hash, expires_at, now()),
    )
    return token, expires_at


def delete_session(connection: sqlite3.Connection, session_token: str) -> None:
    """Delete a session by token."""
    token_hash = hash_session_token(session_token)
    connection.execute(
        "DELETE FROM auth_sessions WHERE token_hash = ?",
        (token_hash,),
    )


def create_invite(
    connection: sqlite3.Connection, created_by: int, room_id: int
) -> tuple[str, str]:
    """Create a single-use invite code.

    Returns (code, expires_at).
    """
    code = secrets.token_urlsafe(16)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds")
    connection.execute(
        "INSERT INTO invites (code, created_by, room_id, expires_at, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (code, created_by, room_id, expires_at, now()),
    )
    return code, expires_at


def get_invite(connection: sqlite3.Connection, code: str) -> Optional[sqlite3.Row]:
    """Fetch an invite by code."""
    return connection.execute(
        "SELECT * FROM invites WHERE code = ?",
        (code,),
    ).fetchone()


def is_invite_valid(connection: sqlite3.Connection, code: str) -> bool:
    """Check if an invite code is valid (not expired, not used)."""
    invite = get_invite(connection, code)
    if not invite:
        return False

    # Check expiry
    expires_at = datetime.fromisoformat(invite["expires_at"])
    if datetime.now(timezone.utc) > expires_at:
        return False

    # Check if already used
    if invite["used_by"] is not None:
        return False

    return True


def accept_invite(
    connection: sqlite3.Connection,
    code: str,
    username: str,
    display_name: str,
    password: str,
) -> int:
    """Accept an invite and create a new engineer account.

    Returns the new user ID.
    """
    invite = get_invite(connection, code)
    if not invite:
        raise HTTPException(404, "Invite not found")

    if not is_invite_valid(connection, code):
        raise HTTPException(410, "Invite expired or already used")

    # Create user
    salt, password_hash = hash_password(password)
    connection.execute(
        "INSERT INTO users (username, display_name, password_hash, password_salt, role, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (username, display_name, password_hash, salt, "engineer", now()),
    )
    user_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Mark invite as used
    connection.execute(
        "UPDATE invites SET used_by = ?, used_at = ? WHERE code = ?",
        (user_id, now(), code),
    )

    # Add user to the room
    connection.execute(
        "INSERT INTO room_members (room_id, user_id, joined_at) VALUES (?, ?, ?)",
        (invite["room_id"], user_id, now()),
    )

    return user_id


def change_password(
    connection: sqlite3.Connection,
    user_id: int,
    current_password: str,
    new_password: str,
) -> None:
    """Change a user's password."""
    user = get_user_by_id(connection, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    if not verify_password(current_password, user["password_salt"], user["password_hash"]):
        raise HTTPException(401, "Current password is incorrect")

    salt, password_hash = hash_password(new_password)
    connection.execute(
        "UPDATE users SET password_hash = ?, password_salt = ?, must_change_password = 0 "
        "WHERE id = ?",
        (password_hash, salt, user_id),
    )


def is_user_in_room(connection: sqlite3.Connection, user_id: int, room_id: int) -> bool:
    """Check if a user is a member of a room."""
    member = connection.execute(
        "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
        (room_id, user_id),
    ).fetchone()
    return member is not None


def get_room_members(connection: sqlite3.Connection, room_id: int) -> list[sqlite3.Row]:
    """Get all members of a room with their user details."""
    return connection.execute(
        """SELECT u.id, u.username, u.display_name, u.role
           FROM room_members rm
           JOIN users u ON rm.user_id = u.id
           WHERE rm.room_id = ?
           ORDER BY u.username""",
        (room_id,),
    ).fetchall()
