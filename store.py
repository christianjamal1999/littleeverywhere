"""Storage for characters, projects and media.

Two backends behind one interface:
- Local disk (data/) when running on your machine.
- Supabase when SUPABASE_URL and SUPABASE_SECRET_KEY are set (deployed on Vercel):
  characters/projects live in Postgres tables as JSON documents, media lives in the public
  "media" Storage bucket. Row-level security is on with no policies, so only this server's
  secret key can read or write rows.
"""
import json
import os
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, unquote

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
ON_VERCEL = bool(os.environ.get("VERCEL"))
# Vercel's code directory is read-only; without Supabase, fall back to (ephemeral) /tmp.
DATA = "/tmp/kidsong-data" if ON_VERCEL else os.path.join(ROOT, "data")
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
USE_SUPABASE = bool(SB_URL and SB_KEY)
BUCKET = "media"
PUBLIC_PREFIX = f"{SB_URL}/storage/v1/object/public/{BUCKET}/"
TMP = "/tmp/kidsong" if ON_VERCEL else os.path.join(DATA, "tmp")
os.makedirs(TMP, exist_ok=True)


def storage_kind():
    return "supabase" if USE_SUPABASE else "ephemeral" if ON_VERCEL else "local"


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def now():
    return int(time.time())


def tmp_path(name):
    return os.path.join(TMP, f"{uuid.uuid4().hex[:8]}_{name}")


# ---------------------------------------------------------------- Supabase

def _sb(method, path, **kw):
    headers = kw.pop("headers", {})
    # Same pair supabase-js sends: works for sb_secret_ keys and legacy service_role JWTs.
    headers.update({"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"})
    r = requests.request(method, SB_URL + path, headers=headers, timeout=300, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"Supabase {method} {path.split('?')[0]} failed "
                           f"({r.status_code}): {r.text[:300]}")
    return r.json() if r.content else None


def check_supabase():
    """Cheap round trip that proves the URL and secret key work."""
    _sb("GET", "/rest/v1/characters?select=id&limit=1")


# ---------------------------------------------------------------- media files

def put_file(pathname, data, content_type):
    """Store bytes, return a URL the browser can load (public https on Supabase)."""
    base, ext = os.path.splitext(pathname)
    rel = f"{base}-{uuid.uuid4().hex[:6]}{ext}"
    if USE_SUPABASE:
        _sb("POST", f"/storage/v1/object/{BUCKET}/{quote(rel)}", data=data,
            headers={"Content-Type": content_type, "x-upsert": "false",
                     "cache-control": "31536000"})  # paths are unique, so cache forever
        return PUBLIC_PREFIX + quote(rel)
    full = os.path.join(DATA, "media", rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    return "/media/" + rel


def local_media_path(url):
    """Filesystem path for a local /media/ URL, else None."""
    if url and url.startswith("/media/"):
        return os.path.join(DATA, "media", url[len("/media/"):])
    return None


def fetch_bytes(url):
    p = local_media_path(url)
    if p:
        with open(p, "rb") as f:
            return f.read()
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    return r.content


def download_to(url, dest):
    p = local_media_path(url)
    if p:
        return p
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dest


def delete_media(urls):
    urls = [u for u in urls if u]
    if USE_SUPABASE:
        paths = [unquote(u[len(PUBLIC_PREFIX):]) for u in urls if u.startswith(PUBLIC_PREFIX)]
        if paths:
            _sb("DELETE", f"/storage/v1/object/{BUCKET}", json={"prefixes": paths})
        return
    for u in urls:
        p = local_media_path(u)
        if p and os.path.exists(p):
            os.remove(p)


# ---------------------------------------------------------------- documents

def _doc_dir(kind):
    d = os.path.join(DATA, "db", kind)
    os.makedirs(d, exist_ok=True)
    return d


def save_doc(kind, doc):
    doc["updated"] = now()
    if USE_SUPABASE:
        _sb("POST", f"/rest/v1/{kind}", json={
            "id": doc["id"], "data": doc,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        return doc
    path = os.path.join(_doc_dir(kind), doc["id"] + ".json")
    with open(path + ".tmp", "w") as f:
        f.write(json.dumps(doc, indent=1))
    os.replace(path + ".tmp", path)
    return doc


def load_doc(kind, doc_id):
    if USE_SUPABASE:
        rows = _sb("GET", f"/rest/v1/{kind}?select=data&id=eq.{quote(doc_id)}")
        return rows[0]["data"] if rows else None
    path = os.path.join(_doc_dir(kind), doc_id + ".json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def list_docs(kind):
    if USE_SUPABASE:
        return [r["data"] for r in _sb("GET", f"/rest/v1/{kind}?select=data&order=created_at.desc")]
    docs = []
    for name in os.listdir(_doc_dir(kind)):
        if name.endswith(".json"):
            with open(os.path.join(_doc_dir(kind), name)) as f:
                docs.append(json.load(f))
    return sorted(docs, key=lambda d: d.get("created", 0), reverse=True)


def delete_doc(kind, doc_id):
    if USE_SUPABASE:
        _sb("DELETE", f"/rest/v1/{kind}?id=eq.{quote(doc_id)}")
        return
    path = os.path.join(_doc_dir(kind), doc_id + ".json")
    if os.path.exists(path):
        os.remove(path)
