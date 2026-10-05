"""Employer credentials encrypted for the current Windows user with DPAPI."""
import ctypes
from contextlib import closing
from ctypes import wintypes
import os
import secrets
import sqlite3
from urllib.parse import urlsplit


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Automatic account passwords require Windows DPAPI on this computer.")
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(destination)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        kernel.LocalFree(destination.data)


def account_password(database, url: str, email: str, *, create: bool = True) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or not email.strip():
        raise ValueError("Account creation requires HTTPS and a saved email address.")
    origin = f"https://{parts.netloc.lower()}"
    username = email.strip().casefold()
    with closing(sqlite3.connect(database, timeout=10)) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS employer_credentials (origin TEXT NOT NULL, username TEXT NOT NULL, encrypted_password BLOB NOT NULL, PRIMARY KEY(origin, username))")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT encrypted_password FROM employer_credentials WHERE origin=? AND username=?", (origin, username)).fetchone()
        if row:
            return protect(bytes(row[0]), decrypt=True).decode("utf-8")
        if not create:
            raise ValueError("No saved password for this site and email.")
        # Guarantee common character classes without predictable positions.
        characters = [secrets.choice(pool) for pool in ("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz", "0123456789", "!@#$%")]
        characters += [secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%") for _ in range(20)]
        secrets.SystemRandom().shuffle(characters)
        password = "".join(characters)
        connection.execute("INSERT INTO employer_credentials VALUES (?, ?, ?)", (origin, username, protect(password.encode("utf-8"))))
        return password


def saved_accounts(database) -> list[dict[str, str]]:
    with closing(sqlite3.connect(database)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='employer_credentials'").fetchone():
            return []
        return [{"site": row[0], "email": row[1]} for row in connection.execute("SELECT origin, username FROM employer_credentials ORDER BY origin, username")]
