"""Save / restore jobs.db between GitHub Actions runs, compressed and encrypted.

Usage:  python snapshot.py pack      jobs.db  -> data.snapshot  (gzip + AES-256-GCM)
        python snapshot.py unpack    data.snapshot -> jobs.db

The weekly "Collect" workflow packs the database and stores it as a GitHub artifact;
the "Publish" workflow downloads the latest one and rebuilds the site in a few minutes.
Artifacts of a public repository can be downloaded by anyone, and the database holds
sole traders' names, so the file is always encrypted (DATA_PASSWORD, or CLUB_PASSWORD).
"""
import gzip
import hashlib
import os
import sys

from club import load_env

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(HERE, "jobs.db"))
SNAP = os.path.join(HERE, "data.snapshot")
MAGIC, ITERATIONS = b"HJDB1", 310_000


def key(pwd, salt):
    return hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt, ITERATIONS, 32)


def main():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    load_env()
    pwd = os.getenv("DATA_PASSWORD") or os.getenv("CLUB_PASSWORD")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("pack", "unpack"):
        sys.exit(__doc__)
    if not pwd:
        sys.exit("No DATA_PASSWORD or CLUB_PASSWORD: refusing to store the database unencrypted.")
    if cmd == "pack":
        with open(DB_PATH, "rb") as f:
            raw = gzip.compress(f.read(), 6)
        salt, iv = os.urandom(16), os.urandom(12)
        with open(SNAP, "wb") as f:
            f.write(MAGIC + salt + iv + AESGCM(key(pwd, salt)).encrypt(iv, raw, None))
        print(f"Packed {DB_PATH} -> {SNAP} ({os.path.getsize(SNAP) / 1e6:.1f} MB)")
    else:
        with open(SNAP, "rb") as f:
            blob = f.read()
        if not blob.startswith(MAGIC):
            sys.exit("data.snapshot: unknown format")
        salt, iv, ct = blob[5:21], blob[21:33], blob[33:]
        try:
            raw = AESGCM(key(pwd, salt)).decrypt(iv, ct, None)
        except Exception:
            sys.exit("data.snapshot: wrong password")
        with open(DB_PATH, "wb") as f:
            f.write(gzip.decompress(raw))
        print(f"Restored {DB_PATH} ({os.path.getsize(DB_PATH) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
