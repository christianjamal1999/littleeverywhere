"""Publishing: upload a finished video (and its Short) to YouTube with title, description, lyrics, tags,
thumbnail, the made-for-kids and altered-content declarations and an optional scheduled time. Also joins
finished videos into a compilation.

Connecting uses Google's OAuth with your own Google Cloud OAuth client (client ID and secret in Settings).
Only the youtube.upload permission is requested; the refresh token is stored with the other keys."""
import os
import secrets
import sys
from urllib.parse import urlencode

import requests
from flask import jsonify, redirect, request

import services as sv
import store

studio = sys.modules.get("app") or sys.modules["__main__"]
app = studio.app

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
STATE_ID = "youtube_oauth"
MUSIC_CATEGORY = "10"


def _redirect_uri():
    host = request.headers.get("X-Forwarded-Host") or request.host
    scheme = "http" if host.startswith(("127.0.0.1", "localhost")) else "https"
    return f"{scheme}://{host}/api/youtube/callback"


@app.post("/api/youtube/connect")
def youtube_connect():
    """Returns the Google consent URL to send the browser to."""
    s = studio.settings()
    if not (s.get("youtube_client_id") and s.get("youtube_client_secret")):
        raise sv.UserError("Add your Google OAuth client ID and secret in Settings first.")
    state = secrets.token_urlsafe(24)
    store.save_doc("app_settings", {"id": STATE_ID, "created": store.now(), "state": state})
    return jsonify(url=AUTH_URL + "?" + urlencode({
        "client_id": s["youtube_client_id"], "redirect_uri": _redirect_uri(), "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state}),
        redirect_uri=_redirect_uri())


@app.get("/api/youtube/callback")
def youtube_callback():
    """Google sends the browser back here. No app password (a browser redirect can't carry it); the one-time
    state value proves the request started from this app."""
    saved = store.load_doc("app_settings", STATE_ID) or {}
    state = request.args.get("state", "")
    if not saved.get("state") or not secrets.compare_digest(state, saved["state"]):
        return redirect("/?youtube=" + "That sign-in link expired. Press Connect YouTube again.")
    store.delete_doc("app_settings", STATE_ID)
    if request.args.get("error") or not request.args.get("code"):
        return redirect("/?youtube=" + "YouTube wasn't connected: " + request.args.get("error", "no code"))
    s = studio.settings()
    r = requests.post(TOKEN_URL, timeout=30, data={
        "code": request.args["code"], "client_id": s["youtube_client_id"],
        "client_secret": s["youtube_client_secret"], "redirect_uri": _redirect_uri(),
        "grant_type": "authorization_code"})
    token = r.json().get("refresh_token") if r.ok else None
    if not token:
        return redirect("/?youtube=" + "Google didn't return a token: " + r.text[:120])
    studio.save_secret("youtube_refresh_token", token)
    return redirect("/?youtube=connected")


@app.post("/api/youtube/disconnect")
def youtube_disconnect():
    studio.save_secret("youtube_refresh_token", None)
    return jsonify(studio.settings_view())


def _access_token(s):
    if not s.get("youtube_refresh_token"):
        raise sv.UserError("Connect YouTube in Settings first.")
    r = requests.post(TOKEN_URL, timeout=30, data={
        "client_id": s["youtube_client_id"], "client_secret": s["youtube_client_secret"],
        "refresh_token": s["youtube_refresh_token"], "grant_type": "refresh_token"})
    if not r.ok:
        raise sv.UserError("YouTube sign-in expired. Connect YouTube again in Settings.")
    return r.json()["access_token"]


def _error(r, what):
    try:
        detail = r.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        detail = r.text[:200]
    return sv.UserError(f"YouTube refused the {what} ({r.status_code}): {detail}")


def upload_video(token, path, title, description, tags, privacy, publish_at=None):
    status = {"privacyStatus": "private" if publish_at else privacy, "selfDeclaredMadeForKids": True,
              "containsSyntheticMedia": True}
    if publish_at:
        status["publishAt"] = publish_at
    kept, room = [], 480  # YouTube allows 500 characters of tags in all
    for tag in tags:
        if len(tag) + 1 <= room:
            kept.append(tag)
            room -= len(tag) + 1
    meta = {"snippet": {"title": title[:100], "description": description[:4900], "tags": kept,
                        "categoryId": MUSIC_CATEGORY, "defaultLanguage": "en", "defaultAudioLanguage": "en"},
            "status": status}
    auth = {"Authorization": f"Bearer {token}"}
    size = os.path.getsize(path)
    r = requests.post(f"{UPLOAD_URL}/videos?uploadType=resumable&part=snippet,status", json=meta, timeout=60,
                      headers={**auth, "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
    if not r.ok:
        raise _error(r, "upload")
    with open(path, "rb") as f:
        r = requests.put(r.headers["Location"], data=f, timeout=280,
                         headers={**auth, "Content-Type": "video/mp4", "Content-Length": str(size)})
    if not r.ok:
        raise _error(r, "upload")
    return r.json()["id"]


def set_thumbnail(token, video_id, image_url):
    src = store.download_to(image_url, store.tmp_path("thumb_src" + (os.path.splitext(image_url.split("?")[0])[1] or ".png")))
    out = store.tmp_path("thumb.jpg")
    sv._run(["-i", src, "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
             "-frames:v", "1", "-q:v", "3", out])
    sv._discard_download(image_url, src)
    with open(out, "rb") as f:
        r = requests.post(f"{UPLOAD_URL}/thumbnails/set?videoId={video_id}&uploadType=media", data=f.read(),
                          timeout=120, headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"})
    os.remove(out)
    if not r.ok:
        raise _error(r, "thumbnail")


def description_of(plan):
    lyrics = "\n\n".join(sec["lyrics"].strip() for sec in plan["sections"] if sec.get("lyrics", "").strip())
    return f"{plan['youtube_description'].strip()}\n\n🎵 Lyrics\n\n{lyrics}"


@app.post("/api/projects/<pid>/publish")
def project_publish(pid):
    """Body: {privacy: private|unlisted|public, publish_at: ISO time or "", short: bool}."""
    s = studio.settings()
    data = studio.body()
    privacy = data.get("privacy") if data.get("privacy") in ("private", "unlisted", "public") else "private"
    publish_at = str(data.get("publish_at") or "").strip() or None

    def work(p):
        if not p.get("final"):
            raise sv.UserError("Render the video first.")
        plan, notes = p["plan"], []
        token = _access_token(s)
        path = store.download_to(p["final"], store.tmp_path("publish.mp4"))
        try:
            video_id = upload_video(token, path, plan["youtube_title"], description_of(plan), plan["tags"],
                                    privacy, publish_at)
        finally:
            sv._discard_download(p["final"], path)
        if p.get("thumbnail"):
            try:
                set_thumbnail(token, video_id, p["thumbnail"])
            except (sv.UserError, RuntimeError) as e:
                notes.append(f"The thumbnail wasn't set ({e}). Custom thumbnails need a phone-verified channel; "
                             "add it in YouTube Studio.")
        short_id = None
        if data.get("short", True) and p.get("short"):
            path = store.download_to(p["short"], store.tmp_path("publish_short.mp4"))
            try:
                short_id = upload_video(token, path, (plan["title"][:80] + " #Shorts"),
                                        f"{plan['title']} - full video: https://youtu.be/{video_id}\n\n#Shorts #KidsSongs",
                                        plan["tags"], privacy, publish_at)
            except sv.UserError as e:
                notes.append(f"The Short wasn't uploaded: {e}")
            finally:
                sv._discard_download(p["short"], path)
        record = {"id": video_id, "url": f"https://youtu.be/{video_id}", "privacy": privacy,
                  "publish_at": publish_at, "at": store.now(), "note": " ".join(notes) or None,
                  "short_url": f"https://youtube.com/shorts/{short_id}" if short_id else None}

        def apply(q):
            q["youtube"] = record
        return apply
    return jsonify(studio.run_project_task(pid, "publish", "Publishing to YouTube", work))


# ============================================================== compilations

@app.get("/api/compilations")
def compilations_list():
    return jsonify((studio.channel_doc().get("compilations") or [])[::-1])


@app.post("/api/compilations")
def compilations_create():
    """Join finished videos, newest first unless ids are given, into one long video (no re-encoding)."""
    s = studio.settings()
    data = studio.body()
    projects = {p["id"]: p for p in store.list_docs("projects") if p.get("final")}
    ids = [i for i in (data.get("ids") or list(projects)) if i in projects]
    if len(ids) < 2:
        raise sv.UserError("A compilation needs at least two rendered videos.")
    limit = float(s.get("max_video_mb") or 50)
    studio.channel_doc()
    paths = [store.download_to(projects[i]["final"], store.tmp_path(f"comp{n}.mp4")) for n, i in enumerate(ids)]
    try:
        total = sum(os.path.getsize(x) for x in paths) / 1e6
        if total > limit:
            raise sv.UserError(f"These {len(ids)} videos make a {total:.0f} MB compilation, over your {limit:.0f} MB "
                               "storage limit. Raise the upload limit in Supabase and in Settings first.")
        out = sv.join_videos(paths, store.tmp_path("compilation.mp4"))
    finally:
        for i, x in zip(ids, paths):
            sv._discard_download(projects[i]["final"], x)
    with open(out, "rb") as f:
        url = store.put_file("compilations/compilation.mp4", f.read(), "video/mp4")
    os.remove(out)
    titles = [studio._project_title(projects[i]) for i in ids]
    item = {"id": store.new_id("cmp"), "created": store.now(), "url": url, "titles": titles,
            "title": str(data.get("title") or f"{len(ids)} Kids Songs | {titles[0]} + More").strip()}

    def apply(q):
        q["compilations"] = (q.get("compilations") or []) + [item]
    studio.update_doc("app_settings", studio.CHANNEL_ID, apply)
    return jsonify(item)
