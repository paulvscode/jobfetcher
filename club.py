"""Private client file: the research and action plan for one gym (club.json).

club.json stays on your computer (it is in .gitignore). The repository only holds
club.json.enc, encrypted with the password in CLUB_PASSWORD, so the GitHub
workflow can read it while the public repository cannot.

Usage:
  python club.py seal     encrypt club.json into club.json.enc (run after each edit)
  python club.py open     decrypt club.json.enc into club.json (on another computer)

The dashboard section built from it is also encrypted on the public site: visitors
see a password box and the browser decrypts the section locally.
"""
import base64
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLUB_JSON = os.path.join(HERE, "club.json")
CLUB_ENC = os.path.join(HERE, "club.json.enc")
ITERATIONS = 310_000  # PBKDF2-SHA256, as recommended by OWASP


def load_env(path=os.path.join(HERE, ".env")):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def password():
    load_env()
    return os.getenv("CLUB_PASSWORD") or None


def encrypt(data: bytes, pwd: str) -> dict:
    """AES-256-GCM with a PBKDF2 key; the browser decrypts it with WebCrypto."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt, ITERATIONS, 32)
    ct = AESGCM(key).encrypt(iv, data, None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "it": ITERATIONS, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def decrypt(blob: dict, pwd: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = base64.b64decode
    key = hashlib.pbkdf2_hmac("sha256", pwd.encode(), d(blob["salt"]), blob["it"], 32)
    return AESGCM(key).decrypt(d(blob["iv"]), d(blob["ct"]), None)


def load_club():
    """The client file, or None when it is missing or cannot be decrypted."""
    if os.path.exists(CLUB_JSON):
        with open(CLUB_JSON, encoding="utf-8") as f:
            return json.load(f)
    pwd = password()
    if os.path.exists(CLUB_ENC) and pwd:
        with open(CLUB_ENC, encoding="utf-8") as f:
            try:
                return json.loads(decrypt(json.load(f), pwd))
            except Exception:
                print("club.json.enc: wrong CLUB_PASSWORD, client section skipped")
    return None


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    pwd = password()
    if cmd not in ("seal", "open"):
        sys.exit(__doc__)
    if not pwd:
        sys.exit("Set CLUB_PASSWORD in .env first.")
    if cmd == "seal":
        with open(CLUB_JSON, "rb") as f:
            raw = f.read()
        json.loads(raw)  # refuse to seal a broken file
        with open(CLUB_ENC, "w", encoding="utf-8") as f:
            json.dump(encrypt(raw, pwd), f)
        print(f"Sealed {CLUB_JSON} -> {CLUB_ENC}")
    else:
        with open(CLUB_ENC, encoding="utf-8") as f:
            raw = decrypt(json.load(f), pwd)
        with open(CLUB_JSON, "wb") as f:
            f.write(raw)
        print(f"Opened {CLUB_ENC} -> {CLUB_JSON}")


if __name__ == "__main__":
    main()
