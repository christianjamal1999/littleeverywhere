"""KidSong Studio - make kids' music videos with Claude + Higgsfield.

Runs locally (python app.py) or on Vercel. Settings, including API keys, are saved server-side
(encrypted, see vault.py) so they work from any device; keys are never sent back to the browser.
"""
import hmac
import os
import time

from flask import Flask, abort, g, jsonify, request, send_from_directory

import direction as dr
import services as sv
import store
import sync
import vault

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
MAX_COMPLETIONS_PER_POLL = 4
# A task still marked running after this long was cut off (Vercel stops requests at 300 s).
TASK_TIMEOUT = 330 if store.ON_VERCEL else 3600
PROGRESS_SAVE_SECONDS = 3
MAX_EVENTS_PER_DOC = 10

DEFAULTS = {
    "anthropic_api_key": "",
    "claude_model": "claude-opus-5-5",
    "claude_effort": "high",
    "hf_key_id": "",
    "hf_key_secret": "",
    "elevenlabs_api_key": "",
    "music_model": "music_v2_5",
    "channel_name": "Wander Tunes",
    "look": "lifelike",
    "reuse_chorus": True,
    "auto_review": True,
    "youtube_client_id": "",
    "youtube_client_secret": "",
    "youtube_refresh_token": "",
    "house_style": sv.HOUSE_STYLE,
    "character_guidelines": sv.CHARACTER_GUIDELINES,
    "aspect_ratio": "16:9",
    "image_endpoint": "xai/grok-imagine-image-2.0",
    "video_mode": "seedance_ref",
    "seedance_endpoint": "bytedance/seedance-2.0/reference-to-video",
    "kling_endpoint": "kling-video/v2.5-turbo/pro/image-to-video",
    "wan_endpoint": "wan/v2.6/image-to-video",
    "ltx_endpoint": "lightricks/ltx-2.5/image-to-video/fast",
    "image_resolution": "1k",
    "image_quality": "medium",
    "video_resolution": "1080p",
    "max_video_mb": 50,
    "sing_along_audio": True,
    "sing_along_captions": True,
    "add_intro": True,
    "add_outro": True,
    "narration_voice": "cgSgspJ2msm6clMCkdW9",
}

SECRET_KEYS = ("anthropic_api_key", "hf_key_id", "hf_key_secret", "elevenlabs_api_key",
               "youtube_client_id", "youtube_client_secret", "youtube_refresh_token")
OPEN_PATHS = ("/api/config", "/api/youtube/callback", "/api/tick")  # no app password: see each route
SETTINGS_ID = "default"
CHANNEL_ID = "channel"  # the channel's icon and animated intro, kept beside the settings

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024


# ------------------------------------------------------------------ plumbing

def saved_settings():
    """(plain settings, decrypted API keys, keys_readable) from storage, once per request."""
    if "saved" not in g:
        doc = store.load_doc("app_settings", SETTINGS_ID) or {}
        secrets, readable = vault.unseal(doc.get("secrets"))
        g.saved = (doc.get("settings", {}), secrets, readable)
    return g.saved


def settings(overrides=None):
    plain, secrets, _ = saved_settings()
    s = dict(DEFAULTS)
    s.update({k: v for k, v in {**plain, **secrets, **(overrides or {})}.items()
              if k in DEFAULTS and v not in (None, "")})
    s["video_mode"], s["look"] = sv.video_mode(s), sv.look_of(s)
    return s


@app.before_request
def gate():
    if not APP_PASSWORD or not request.path.startswith("/api/") or request.path in OPEN_PATHS:
        return None
    given = request.headers.get("X-App-Password", "")
    if not hmac.compare_digest(given.encode(), APP_PASSWORD.encode()):
        return jsonify(error="Enter the app password in Settings.", auth=True), 401
    return None


@app.before_request
def spend_context():
    # Spending during this request counts toward the video it's about, if any.
    sv.set_spend_project((request.view_args or {}).get("pid"))


@app.errorhandler(sv.UserError)
def user_error(e):
    return jsonify(error=str(e)), 400


@app.errorhandler(Exception)
def any_error(e):
    code = getattr(e, "code", 500)
    if not isinstance(code, int):
        code = 500
    app.logger.exception(e)
    return jsonify(error=str(getattr(e, "description", None) or e)), code


def load_or_404(kind, doc_id):
    doc = store.load_doc(kind, doc_id)
    if not doc:
        abort(404, f"{kind[:-1].capitalize()} not found")
    return doc


def cast_for(ids):
    chars = {c["id"]: c for c in store.list_docs("characters")}
    return [chars[i] for i in ids if i in chars]


def body():
    return request.get_json(silent=True) or {}


def save_upload(prefix):
    f = request.files.get("file")
    if not f:
        raise sv.UserError("No file received.")
    name = os.path.basename(f.filename or "upload").replace(" ", "_")
    return store.put_file(f"{prefix}/{name}", f.read(), f.mimetype or "application/octet-stream")


def collect_output(s, job, kind_prefix, what, project=None):
    """Advance one Higgsfield job. Returns (finished, url_or_None, error_or_None).
    Only successful jobs are charged, so that's when the spending is logged."""
    status, url, err = sv.hf_poll(s, job)
    job["status"] = status
    if url:
        # Keyed by request, so two polls finishing the same job can't count it twice.
        took = f", took {time.time() - job['submitted']:.0f} s" if job.get("submitted") else ""
        sv.log_spend("Higgsfield", what, job.get("usd"), (job.get("endpoint") or "") + took,
                     project, "use_" + job["request_id"])
        ext = os.path.splitext(url.split("?")[0])[1] or ".bin"
        ctype = {".mp4": "video/mp4", ".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
        return True, store.put_file(f"{kind_prefix}{ext}", store.fetch_bytes(url), ctype), None
    return bool(err), None, err


# ------------------------------------------------------------------ background work
# Long jobs (writing, music, rendering) and Higgsfield jobs run while the user is elsewhere,
# so results are applied to a freshly loaded document, never to the copy a job started with.
# Each finished job leaves an event on its document; /api/activity reports new ones as notices.

def update_doc(kind, doc_id, fn):
    """Load the latest version, apply fn, save. Returns the saved document."""
    doc = load_or_404(kind, doc_id)
    fn(doc)
    return store.save_doc(kind, doc)


def add_event(doc, label, ok=True, error=None, kind=""):
    doc["events"] = (doc.get("events") or [])[-(MAX_EVENTS_PER_DOC - 1):] + [
        {"ts": time.time(), "label": label, "ok": ok, "error": error, "kind": kind}]


def task_running(p):
    task = p.get("task")
    return bool(task) and time.time() - task["started"] < TASK_TIMEOUT


def run_project_task(pid, kind, label, work):
    """Mark the project busy, run work(snapshot) -> apply(latest), then record the outcome."""
    def start(p):
        if task_running(p):
            raise sv.UserError(f"{p['task']['label']} is already running for this video.")
        p["task"] = {"kind": kind, "label": label, "started": time.time()}

    snapshot = update_doc("projects", pid, start)
    try:
        apply = work(snapshot)
    except Exception as e:
        error = str(e)
        update_doc("projects", pid, lambda p: (p.pop("task", None),
                                               add_event(p, label, False, error, kind)))
        raise

    def finish(p):
        apply(p)
        p.pop("task", None)
        add_event(p, label, True, None, kind)
    return update_doc("projects", pid, finish)


def advance_character(s, c):
    job = c.get("job")
    done, url, err = collect_output(s, job, f"characters/{c['id']}/gen", f"Portrait of {c['name']}")
    if not done:
        return

    def apply(q):
        if (q.get("job") or {}).get("request_id") != job["request_id"]:
            store.delete_media([url])  # superseded by a newer request
            return
        q["job"], q["job_error"] = None, err
        if url and q.pop("restyling", None):
            # The old look's images step aside (kept, in case you switch back), and the body is redone to match.
            if q.get("old_images"):
                store.delete_media(q["images"])  # an in-between redraw; the originals stay
            else:
                q["old_images"] = q["images"]
            q["images"], q["needs_body"] = [url], True
        elif url:
            q["images"].append(url)
        if url:
            q["style"] = job.get("style") or q.get("style")
        add_event(q, f"Portrait of {q['name']}", ok=bool(url), error=err, kind="portrait")
    latest = update_doc("characters", c["id"], apply)
    # A new character's face comes first; its full-body reference is then matched to that face.
    if url and latest.get("needs_body") and not latest.get("job"):
        body_job = sv.image_job(s, sv.character_prompt(s, latest, "body"), latest["images"][:1], "3:4", best=True)
        body_job["style"] = sv.look_of(s)

        def queue_body(q):
            q["job"], q["needs_body"] = body_job, False
        update_doc("characters", c["id"], queue_body)


def _locations(p):
    return ((p.get("plan") or {}).get("direction") or {}).get("locations", [])


def advance_project(s, p, budget):
    """Collect finished Higgsfield jobs (up to budget[0]) and apply them to the latest project."""
    pid, results = p["id"], []
    pending = [("thumb", None, job) for job in [p.get("thumb_job")] + (p.get("thumb_jobs") or [])] + [
        ("plate", loc["id"], loc.get("job")) for loc in _locations(p)] + [
        (kind, sc["n"], job) for sc in (p.get("plan") or {}).get("scenes", [])
        for kind, job in (sc.get("jobs") or {}).items()]
    for kind, n, job in pending:
        if not job or budget[0] <= 0:
            continue
        if kind == "plate":
            prefix, what = f"projects/{pid}/set-{n}", f"Set photo, {n}"
        else:
            prefix = f"projects/{pid}/thumb" if kind == "thumb" else f"projects/{pid}/scene{n}-{kind}"
            what = "Thumbnail" if kind == "thumb" else f"{'Keyframe' if kind == 'image' else 'Animation'}, scene {n}"
        done, url, err = collect_output(s, job, prefix, what, pid)
        if done:
            results.append((kind, n, job["request_id"], url, err))
            budget[0] -= 1
    if not results:
        return
    stale = []

    def apply(q):
        scenes = {sc["n"]: sc for sc in (q.get("plan") or {}).get("scenes", [])}
        for kind, n, rid, url, err in results:
            if kind == "thumb":
                jobs = [j for j in [q.get("thumb_job")] + (q.get("thumb_jobs") or []) if j]
                if rid not in [j["request_id"] for j in jobs]:
                    stale.append(url)
                    continue
                q["thumb_job"] = None
                q["thumb_jobs"] = [j for j in (q.get("thumb_jobs") or []) if j["request_id"] != rid]
                if url:
                    q["thumb_options"] = ((q.get("thumb_options") or []) + [url])[-6:]
                    q["thumbnail"] = q.get("thumbnail") if q.get("thumbnail") in q["thumb_options"] else url
                else:
                    q["thumb_error"] = err
                if not q["thumb_jobs"]:
                    add_event(q, "Thumbnails", ok=bool(q.get("thumbnail")), error=None if q.get("thumbnail") else err,
                              kind="thumb")
                continue
            if kind == "plate":
                loc = dr.location(q.get("plan") or {}, n)
                if not loc or (loc.get("job") or {}).get("request_id") != rid:
                    stale.append(url)
                    continue
                loc["job"], loc["error"] = None, err
                if url:
                    stale.append(loc.get("image"))
                    loc["image"] = url
                add_event(q, f"Set photo of {loc['name']}", ok=bool(url), error=err, kind="plate")
                continue
            sc = scenes.get(n)
            if not sc or ((sc.get("jobs") or {}).get(kind) or {}).get("request_id") != rid:
                stale.append(url)
                continue
            del sc["jobs"][kind]
            if url:
                stale.append(sc.get(kind))
                sc[kind] = url
                if kind == "image" and sc.get("video"):
                    stale.append(sc.pop("video"))  # a new keyframe invalidates the old clip
                if kind == "video":
                    sc.pop("review", None)  # a new clip needs a new look
            else:
                sc.setdefault("errors", {})[kind] = err
                sc.setdefault("tries", {})[kind] = (sc.get("tries") or {}).get(kind, 0) + 1
            what = "Keyframe" if kind == "image" else "Animation"
            add_event(q, f"{what} for scene {n}", ok=bool(url), error=err, kind=kind)
    update_doc("projects", pid, apply)
    store.delete_media(stale)
    plan = (store.load_doc("projects", pid) or {}).get("plan") or {}
    for kind, n, rid, url, err in results:
        if kind == "video" and url:  # the shot itself, and every chorus repeat that reuses it
            for sc in plan.get("scenes", []):
                if sc["n"] == n or sc.get("reuse") == n:
                    _try_preview(s, pid, sc["n"])


def channel_doc():
    return store.load_doc("app_settings", CHANNEL_ID) or store.save_doc(
        "app_settings", {"id": CHANNEL_ID, "created": store.now()})


CHANNEL_JOBS = {"icon": "Channel icon", "clip": "Channel intro animation"}


def advance_channel(s, ch):
    for kind, label in CHANNEL_JOBS.items():
        job = ch.get(f"{kind}_job")
        if not job:
            continue
        done, url, err = collect_output(s, job, f"channel/{kind}", label)
        if not done:
            continue

        def apply(q):
            q[f"{kind}_job"], q[f"{kind}_error"] = None, err
            if url:
                store.delete_media([q.get(kind)] + list((q.get("intros") or {}).values()))
                q[kind], q["intros"] = url, {}
            add_event(q, label, ok=bool(url), error=err, kind=kind)
        update_doc("app_settings", CHANNEL_ID, apply)


# ------------------------------------------------------------------ static

@app.get("/")
def index():
    return send_from_directory(PUBLIC, "index.html")


@app.get("/<path:name>")
def public_files(name):
    if name.startswith("media/"):
        return send_from_directory(os.path.join(store.DATA, "media"), name[len("media/"):])
    return send_from_directory(PUBLIC, name)


@app.get("/api/config")
def config():
    return jsonify(defaults={k: v for k, v in DEFAULTS.items() if k not in SECRET_KEYS},
                   storage=store.storage_kind(), password_required=bool(APP_PASSWORD), looks=sv.LOOKS)


TESTS = {
    "anthropic": sv.test_anthropic,
    "higgsfield": sv.test_higgsfield,
    "elevenlabs": sv.test_elevenlabs,
    "renderer": lambda s: sv.test_renderer(),
}


@app.route("/api/test/<service>", methods=["GET", "POST"])
def test_connection(service):
    if service not in TESTS:
        abort(404, "Unknown service")
    # Test what's typed in the form (not yet saved) on top of the saved settings.
    try:
        return jsonify(ok=True, message=TESTS[service](settings(body().get("override"))))
    except sv.UserError as e:
        return jsonify(ok=False, message=str(e))


def _hint(value):
    return ("…" + value[-4:]) if len(value) > 8 else "saved"


def settings_view():
    _, secrets, readable = saved_settings()
    s = settings()
    return {
        "settings": {k: s[k] for k in DEFAULTS if k not in SECRET_KEYS},
        "keys": {k: {"set": bool(secrets.get(k)), "hint": _hint(secrets[k]) if secrets.get(k) else ""}
                 for k in SECRET_KEYS},
        "keys_unreadable": not readable,
        "encrypted": vault._fernet() is not None,
    }


@app.get("/api/settings")
def settings_get():
    return jsonify(settings_view())


@app.put("/api/settings")
def settings_put():
    """Body: {settings: {...}, keys: {name: "new value" | null to remove}}. Omitted keys are kept."""
    data = body()
    plain, secrets, _ = saved_settings()
    plain = {**plain, **{k: v for k, v in (data.get("settings") or {}).items()
                         if k in DEFAULTS and k not in SECRET_KEYS}}
    secrets = dict(secrets)
    for k, v in (data.get("keys") or {}).items():
        if k not in SECRET_KEYS:
            continue
        if v:
            secrets[k] = str(v).strip()
        else:
            secrets.pop(k, None)
    _save_settings(plain, secrets)
    return jsonify(settings_view())


def _save_settings(plain, secrets):
    store.save_doc("app_settings", {"id": SETTINGS_ID, "created": store.now(),
                                    "settings": plain, "secrets": vault.seal(secrets)})
    g.pop("saved", None)


def save_secret(name, value):
    plain, secrets, _ = saved_settings()
    secrets = dict(secrets)
    if value:
        secrets[name] = value
    else:
        secrets.pop(name, None)
    _save_settings(plain, secrets)


@app.post("/api/prices")
def prices():
    """Higgsfield's quoted prices for each keyframe and animation option on this account."""
    s = settings()
    sv.need(s, "hf_key_id", "hf_key_secret")
    sample = next((c["images"][0] for c in store.list_docs("characters") if c.get("images")),
                  "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png")
    return jsonify(sv.price_check(s, sv.public_url(s, sample)))


# ------------------------------------------------------------------ channel icon and intro

@app.get("/api/channel")
def channel_get():
    return jsonify(channel_doc())


@app.post("/api/channel/icon")
def channel_icon():
    s = settings()
    channel_doc()
    job = sv.icon_job(s)

    def apply(q):
        q["icon_job"], q["icon_error"] = job, None
    return jsonify(update_doc("app_settings", CHANNEL_ID, apply))


@app.post("/api/channel/animate")
def channel_animate():
    s = settings()
    icon = channel_doc().get("icon")
    if not icon:
        raise sv.UserError("Generate the channel icon first.")
    canvas = store.put_file("channel/canvas.jpg", sv.intro_canvas(icon), "image/jpeg")
    job = sv.intro_clip_job(s, canvas)

    def apply(q):
        q["clip_job"], q["clip_error"] = job, None
    return jsonify(update_doc("app_settings", CHANNEL_ID, apply))


# ------------------------------------------------------------------ characters

CHAR_FIELDS = tuple(sv.LOOK_FIELDS + sv.BIBLE_FIELDS)


@app.get("/api/characters")
def characters_list():
    return jsonify(store.list_docs("characters"))


@app.post("/api/characters")
def characters_create():
    data = body()
    c = {"id": store.new_id("chr"), "created": store.now(), "images": [], "job": None}
    c.update({k: str(data.get(k, "")).strip() for k in CHAR_FIELDS})
    if not c["name"]:
        raise sv.UserError("Give the character a name.")
    return jsonify(store.save_doc("characters", c))


@app.put("/api/characters/<cid>")
def characters_update(cid):
    c = load_or_404("characters", cid)
    data = body()
    c.update({k: str(data[k]).strip() for k in CHAR_FIELDS if k in data})
    if data.get("kind") in KINDS:
        c["kind"] = data["kind"]
    if "images" in data:  # reorder / remove; only URLs already on the character
        keep = [u for u in data["images"] if u in c["images"]]
        store.delete_media([u for u in c["images"] if u not in keep])
        c["images"] = keep
    return jsonify(store.save_doc("characters", c))


@app.delete("/api/characters/<cid>")
def characters_delete(cid):
    c = load_or_404("characters", cid)
    store.delete_media(c.get("images", []))
    store.delete_doc("characters", cid)
    return jsonify(ok=True)


@app.delete("/api/characters")
def characters_delete_all():
    """Remove the whole cast. Videos already made keep their keyframes and clips."""
    cast = store.list_docs("characters")
    for c in cast:
        store.delete_media(c.get("images", []))
        store.delete_doc("characters", c["id"])
    return jsonify(removed=len(cast))


def restyle_character(s, c):
    """Redraw a character in the current look from their existing portrait. Returns the saved character."""
    if not c.get("images"):
        raise sv.UserError(f"{c['name']} has no image to restyle. Generate a portrait first.")
    if not c.get("icon"):
        others = [o for o in store.list_docs("characters") if o["id"] != c["id"]]
        c = update_doc("characters", c["id"], lambda q: q.update(icon=sv.find_icon(s, c, others)))
    # Redraw from the earliest pictures (the original, closest to the real child), not from a redraw of a redraw.
    source = (c.get("old_images") or c["images"])[:1]
    job = sv.image_job(s, sv.character_prompt(s, c, restyle=True), source, "3:4", best=True)
    job["style"] = sv.look_of(s)

    def apply(q):
        q["job"], q["job_error"], q["restyling"] = job, None, True
    return update_doc("characters", c["id"], apply)


@app.post("/api/characters/<cid>/restyle")
def characters_restyle(cid):
    return jsonify(restyle_character(settings(), load_or_404("characters", cid)))


def _new_character(fields, job, extra=None):
    job["style"] = sv.look_of(settings())
    c = {"id": store.new_id("chr"), "created": store.now(), "images": [], "job": job, "needs_body": True,
         **{k: str(fields.get(k, "")).strip() for k in CHAR_FIELDS}, **(extra or {})}
    return store.save_doc("characters", c)


@app.post("/api/characters/suggest")
def characters_suggest():
    avoid = [str(x)[:40] for x in (body().get("avoid") or [])][:12]
    return jsonify(sv.surprise_character(settings(), store.list_docs("characters"), avoid))


@app.post("/api/characters/describe")
def characters_describe():
    """New character from words: Claude writes the sheet, then its master portrait is generated."""
    s = settings()
    data = body()
    name, concept = str(data.get("name", "")).strip(), str(data.get("concept", "")).strip()
    if not name:
        raise sv.UserError("Give the character a name.")
    sv.need(s, "hf_key_id", "hf_key_secret")
    fields = sv.suggest_character(s, f"Named {name}. {concept}", store.list_docs("characters"))
    fields["name"] = name
    job = sv.image_job(s, sv.character_prompt(s, fields), [], "3:4", best=True)
    return jsonify(_new_character(fields, job))


PHOTO_TYPES = ("image/jpeg", "image/png", "image/webp")
MAX_PHOTOS = 4
KINDS = ("child", "adult")


@app.post("/api/characters/from-photo")
def characters_from_photo():
    """New character: the AI version of a real person, with their consent (a child's parent or guardian's).
    Up to four photos of the same person; more angles give a closer likeness. The photos are only used for
    this one generation and are never saved in our storage."""
    s = settings()
    name, age = request.form.get("name", "").strip(), request.form.get("age", "").strip()
    kind = request.form.get("kind", "child")
    if kind not in KINDS:
        raise sv.UserError("Say whether the photos show a child or an adult.")
    if request.form.get("consent") != "yes":
        raise sv.UserError("Confirm you're the child's parent or guardian, or have their permission."
                           if kind == "child" else "Confirm this is you, or that you have this person's permission.")
    if not name:
        raise sv.UserError("Give the character a name.")
    files = request.files.getlist("file")
    if not files:
        raise sv.UserError("Upload at least one photo.")
    if len(files) > MAX_PHOTOS:
        raise sv.UserError(f"Upload at most {MAX_PHOTOS} photos.")
    if any(f.mimetype not in PHOTO_TYPES for f in files):
        raise sv.UserError("Upload JPG, PNG or WEBP photos.")
    sv.need(s, "hf_key_id", "hf_key_secret")
    photos = [(f.read(), f.mimetype) for f in files]
    fields = sv.describe_photo(s, photos, name, age, kind, store.list_docs("characters"))
    fields.update(name=name, age=age or fields.get("age", ""))
    refs = [sv.hf_upload(s, data, ctype) for data, ctype in photos]
    job = sv.image_job(s, sv.character_prompt(s, {**fields, "kind": kind}, photos=len(refs)), refs, "3:4",
                       best=True)
    consent = {"given": True, "at": store.now(), "by": "guardian" if kind == "child" else "self or permission"}
    return jsonify(_new_character(fields, job, {"source": "photo", "kind": kind, "consent": consent}))


@app.post("/api/characters/<cid>/bible")
def characters_bible(cid):
    s = settings()
    c = load_or_404("characters", cid)
    others = [o for o in store.list_docs("characters") if o["id"] != cid]
    c.update({k: v.strip() for k, v in sv.flesh_out_bible(s, c, others).items()})
    return jsonify(store.save_doc("characters", c))


@app.post("/api/characters/<cid>/images")
def characters_add_image(cid):
    c = load_or_404("characters", cid)
    c["images"].append(save_upload(f"characters/{cid}"))
    return jsonify(store.save_doc("characters", c))


@app.post("/api/characters/<cid>/generate")
def characters_generate(cid):
    s = settings()
    c = load_or_404("characters", cid)
    sheet = bool(body().get("sheet")) and bool(c["images"])
    shot = "sheet" if sheet else "body" if c["images"] else "headshot"
    job = sv.image_job(s, sv.character_prompt(s, c, shot), c["images"][:1], "16:9" if sheet else "3:4", best=True)
    if shot == "headshot":
        job["style"] = sv.look_of(s)

    def apply(q):
        q["job"], q["job_error"] = job, None
    return jsonify(update_doc("characters", cid, apply))


# ------------------------------------------------------------------ activity

def _project_title(p):
    return (p.get("plan") or {}).get("title") or p["brief"].get("topic") or "Untitled video"


@app.post("/api/activity")
def activity():
    """Advance every pending Higgsfield job, then report what's running and what finished since
    `since` (unix seconds). The browser calls this every few seconds from any screen."""
    s = settings()
    since = float(body().get("since") or 0)
    can_poll = bool(s.get("hf_key_id") and s.get("hf_key_secret"))
    budget = [MAX_COMPLETIONS_PER_POLL]
    running, events, warnings, auto_on = [], [], [], []

    ch = store.load_doc("app_settings", CHANNEL_ID) or {}
    if can_poll and (ch.get("icon_job") or ch.get("clip_job")):
        try:
            advance_channel(s, ch)
            ch = store.load_doc("app_settings", CHANNEL_ID) or ch
        except sv.UserError as e:
            warnings.append(str(e))
    for kind, label in CHANNEL_JOBS.items():
        if ch.get(f"{kind}_job"):
            running.append({"key": f"channel:{kind}", "label": label, "tab": "settings",
                            "started": ch[f"{kind}_job"].get("submitted")})
    events += [{**e, "tab": "settings", "key": f"channel:{e['kind']}"}
               for e in ch.get("events", []) if e["ts"] > since]

    for c in store.list_docs("characters"):
        if c.get("job") and can_poll and budget[0] > 0:
            try:
                advance_character(s, c)
                c = store.load_doc("characters", c["id"]) or c
            except sv.UserError as e:
                warnings.append(str(e))
        if c.get("job"):
            running.append({"key": f"chr:{c['id']}:portrait", "label": f"Portrait of {c['name']}",
                            "started": c["job"].get("submitted"), "tab": "characters"})
        events += [{**e, "tab": "characters", "key": f"chr:{c['id']}:{e['kind']}"}
                   for e in c.get("events", []) if e["ts"] > since]

    for p in store.list_docs("projects"):
        task = p.get("task")
        if task and not task_running(p):
            label = task["label"]
            p = update_doc("projects", p["id"], lambda q: (q.pop("task", None), add_event(
                q, label, False, "It stopped before finishing (it may have hit the server's "
                "5-minute limit). Try again.", task["kind"])))
        has_jobs = (p.get("thumb_job") or p.get("thumb_jobs") or any(loc.get("job") for loc in _locations(p))
                    or any(sc.get("jobs") for sc in (p.get("plan") or {}).get("scenes", [])))
        if has_jobs and can_poll and budget[0] > 0:
            try:
                advance_project(s, p, budget)
                p = store.load_doc("projects", p["id"]) or p
            except sv.UserError as e:
                warnings.append(str(e))
        title = _project_title(p)
        base = {"project": p["id"], "title": title}
        if p.get("task"):
            running.append({**base, "key": f"prj:{p['id']}:{p['task']['kind']}",
                            "label": p["task"]["label"], "started": p["task"]["started"],
                            "done": p["task"].get("done"), "total": p["task"].get("total")})
        thumbs = [j for j in [p.get("thumb_job")] + (p.get("thumb_jobs") or []) if j]
        if thumbs:
            running.append({**base, "key": f"prj:{p['id']}:thumb", "label": "Thumbnails",
                            "started": thumbs[0].get("submitted")})
        if (p.get("auto") or {}).get("on"):
            auto_on.append({"id": p["id"], "title": title, "status": p["auto"].get("status") or "",
                            "progress": sync.progress(p)})
        for loc in _locations(p):
            if loc.get("job"):
                running.append({**base, "key": f"prj:{p['id']}:plate:{loc['id']}",
                                "label": f"Set photo of {loc['name']}", "started": loc["job"].get("submitted")})
        for sc in (p.get("plan") or {}).get("scenes", []):
            for kind, job in (sc.get("jobs") or {}).items():
                what = "Keyframe" if kind == "image" else "Animation"
                running.append({**base, "key": f"prj:{p['id']}:{kind}:{sc['n']}",
                                "label": f"{what} for scene {sc['n']}", "started": job.get("submitted")})
        events += [{**e, **base, "key": f"prj:{p['id']}:{e['kind']}"}
                   for e in p.get("events", []) if e["ts"] > since]

    events.sort(key=lambda e: e["ts"])
    return jsonify(running=running, events=events[-20:], now=time.time(), auto=auto_on,
                   warning=warnings[0] if warnings else None)


# ------------------------------------------------------------------ dashboard

SPEND_SERVICES = ("Anthropic", "ElevenLabs", "Higgsfield")
SPEND_RECENT = 50


@app.get("/api/spend")
def spend():
    """Spending per API key and per video, optionally for the last ?days=N days."""
    days = request.args.get("days", type=int)
    cutoff = time.time() - days * 86400 if days else 0
    events = sorted((e for e in store.list_docs("usage_events") if e.get("ts", 0) >= cutoff),
                    key=lambda e: e.get("ts", 0), reverse=True)
    titles = {p["id"]: _project_title(p) for p in store.list_docs("projects")}
    services = {name: {"usd": 0.0, "calls": 0, "unpriced": 0} for name in SPEND_SERVICES}
    videos = {}
    for e in events:
        row = services.setdefault(e["service"], {"usd": 0.0, "calls": 0, "unpriced": 0})
        row["calls"] += 1
        row["usd"] += e.get("usd") or 0
        row["unpriced"] += e.get("usd") is None
        if e.get("project"):
            v = videos.setdefault(e["project"], {"id": e["project"], "usd": 0.0, "by_service": {},
                                                 "title": titles.get(e["project"], "Deleted video")})
            v["usd"] += e.get("usd") or 0
            v["by_service"][e["service"]] = v["by_service"].get(e["service"], 0) + (e.get("usd") or 0)
    for e in events[:SPEND_RECENT]:
        e["title"] = titles.get(e.get("project"), "")
    return jsonify(total=sum(r["usd"] for r in services.values()), services=services,
                   videos=sorted(videos.values(), key=lambda v: v["usd"], reverse=True),
                   recent=events[:SPEND_RECENT], tracking_since=events[-1]["ts"] if events else None)


# ------------------------------------------------------------------ ideas

@app.post("/api/research")
def research():
    return jsonify(sv.research_trends(settings(), body().get("focus", "")))


@app.post("/api/brief/suggest")
def brief_suggest():
    """A whole brief for a video the channel hasn't made yet. Body: {avoid: [topics already suggested]}."""
    done = []
    for p in store.list_docs("projects"):
        done.append(" / ".join(x for x in ((p.get("plan") or {}).get("title"), p["brief"].get("topic")) if x))
    avoid = [str(x)[:120] for x in (body().get("avoid") or [])][:12]
    return jsonify(sv.suggest_brief(settings(), done, store.list_docs("characters"), avoid))


@app.post("/api/ideas")
def ideas():
    data = body()
    return jsonify(sv.suggest_ideas(settings(), data.get("focus", ""), data.get("research", "")))


# ------------------------------------------------------------------ projects

@app.get("/api/projects")
def projects_list():
    out = []
    for p in store.list_docs("projects"):
        plan = p.get("plan") or {}
        thumb = p.get("thumbnail") or next(
            (sc.get("image") for sc in plan.get("scenes", []) if sc.get("image")), None)
        busy = (bool(task_running(p)) + bool(p.get("thumb_job")) + len(p.get("thumb_jobs") or []) + sum(bool(loc.get("job")) for loc in _locations(p))
                + sum(len(sc.get("jobs") or {}) for sc in plan.get("scenes", [])))
        out.append({"id": p["id"], "title": plan.get("title") or p["brief"].get("topic"),
                    "created": p["created"], "thumb": thumb, "final": bool(p.get("final")),
                    "scenes": len(plan.get("scenes", [])), "busy": busy,
                    "auto": (p.get("auto") or {}).get("status") if (p.get("auto") or {}).get("on") else None,
                    "progress": sync.progress(p),
                    "youtube": (p.get("youtube") or {}).get("url")})
    return jsonify(out)


@app.post("/api/projects")
def projects_create():
    brief = body().get("brief") or {}
    if not str(brief.get("topic", "")).strip():
        raise sv.UserError("Give the video a topic.")
    p = {"id": store.new_id("prj"), "created": store.now(), "brief": brief, "plan": None,
         "song": None, "thumbnail": None, "thumb_job": None, "final": None}
    return jsonify(store.save_doc("projects", p))


@app.get("/api/projects/<pid>")
def projects_get(pid):
    p = load_or_404("projects", pid)
    return jsonify({**p, "progress": sync.progress(p)})


@app.put("/api/projects/<pid>")
def projects_update(pid):
    p = load_or_404("projects", pid)
    data = body()
    if "brief" in data:
        p["brief"] = data["brief"]
    if "plan" in data and p.get("plan"):
        # Only text fields are editable from the client; media/job state stays server-owned.
        incoming = data["plan"]
        for k in ("title", "youtube_title", "youtube_description", "tags", "music_style",
                  "thumbnail_prompt", "thumbnail_text"):
            if k in incoming:
                p["plan"][k] = incoming[k]
        for sec, new in zip(p["plan"]["sections"], incoming.get("sections", [])):
            sec["lyrics"] = new.get("lyrics", sec["lyrics"])
        by_n = {sc["n"]: sc for sc in incoming.get("scenes", [])}
        for sc in p["plan"]["scenes"]:
            new = by_n.get(sc["n"], {})
            for k in ("lyric", "visual", "action", "camera", "cast"):
                if k in new:
                    sc[k] = new[k]
            for group, keys in (("direction", ("keyframe", "beats")), ("choreo", ("counts",))):
                if sc.get(group) and isinstance(new.get(group), dict):
                    sc[group].update({k: str(new[group][k]) for k in keys if k in new[group]})
        for loc in (incoming.get("direction") or {}).get("locations", []):
            target = dr.location(p["plan"], loc.get("id"))
            if target and "description" in loc:
                target["description"] = str(loc["description"])
    return jsonify(store.save_doc("projects", p))


@app.delete("/api/projects/<pid>")
def projects_delete(pid):
    p = load_or_404("projects", pid)
    urls = [p.get("thumbnail"), p.get("final"), p.get("short"), (p.get("song") or {}).get("url")]
    urls += p.get("thumb_options") or []
    for sc in (p.get("plan") or {}).get("scenes", []):
        urls += [sc.get("image"), sc.get("video"), (sc.get("preview") or {}).get("url")]
    urls += [loc.get("image") for loc in ((p.get("plan") or {}).get("direction") or {}).get("locations", [])]
    store.delete_media(urls)
    store.delete_doc("projects", pid)
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/write")
def projects_write(pid):
    s = settings()

    def work(p):
        plan = sv.write_song_plan(s, p["brief"], cast_for(p["brief"].get("cast", [])))

        def apply(q):
            q["plan"], q["final"] = plan, None
        return apply
    return jsonify(run_project_task(pid, "write", "Writing song & storyboard", work))


def _cast_of(p):
    return sv.dressed(cast_for(p["brief"].get("cast", [])), p.get("plan"))


def _scene_cast(p, sc):
    return sv.dressed(cast_for(sc["cast"]), p.get("plan"))


@app.post("/api/projects/<pid>/choreograph")
def projects_choreograph(pid):
    s = settings()

    def work(p):
        if not p.get("plan"):
            raise sv.UserError("Write the song first.")
        plan = dr.choreograph(s, p["plan"], _cast_of(p), p["brief"])
        moves = {sc["n"]: sc.get("choreo") for sc in plan["scenes"]}

        def apply(q):
            q["plan"]["choreography"] = plan["choreography"]
            for sc in q["plan"]["scenes"]:
                if moves.get(sc["n"]):
                    sc["choreo"] = moves[sc["n"]]
        return apply
    return jsonify(run_project_task(pid, "choreograph", "Choreographing the dance", work))


@app.post("/api/projects/<pid>/direct")
def projects_direct(pid):
    s = settings()

    def work(p):
        if not p.get("plan"):
            raise sv.UserError("Write the song first.")
        plan = dr.direct(s, p["plan"], _cast_of(p), p["brief"])
        shots = {sc["n"]: sc.get("direction") for sc in plan["scenes"]}

        def apply(q):
            # A set that comes back unchanged keeps its photo (or the one being made); the rest are dropped.
            old = {loc["id"]: loc for loc in (q["plan"].get("direction") or {}).get("locations", [])}
            world = plan["direction"]
            for loc in world["locations"]:
                prev = old.pop(loc["id"], None)
                if prev and prev.get("description") == loc["description"]:
                    loc.update({k: prev[k] for k in ("image", "job") if prev.get(k)})
                elif prev:
                    store.delete_media([prev.get("image")])
            store.delete_media([loc.get("image") for loc in old.values()])
            q["plan"]["direction"] = world
            for sc in q["plan"]["scenes"]:
                if shots.get(sc["n"]):
                    sc["direction"] = shots[sc["n"]]
        return apply
    return jsonify(run_project_task(pid, "direct", "Creative direction", work))


@app.post("/api/projects/<pid>/locations/<lid>/plate")
def location_plate(pid, lid):
    """The empty set photo of one location, used as a reference in every keyframe shot there."""
    s = settings()
    p = load_or_404("projects", pid)
    loc = dr.location(p.get("plan") or {}, lid) or abort(404, "Location not found")
    job = sv.image_job(s, dr.plate_prompt(s, p["plan"], loc), [], s["aspect_ratio"], best=True)

    def apply(q):
        target = dr.location(q.get("plan") or {}, lid) or abort(404, "Location not found")
        target["job"] = job
        target.pop("error", None)
    return jsonify(update_doc("projects", pid, apply))


def _song_record(pid, data, source, ext):
    path = store.tmp_path("song" + ext)
    with open(path, "wb") as f:
        f.write(data)
    dur = sv.media_duration(path)
    os.remove(path)
    return {"url": store.put_file(f"projects/{pid}/song{ext}", data,
                                  "audio/mpeg" if ext == ".mp3" else "audio/wav"),
            "duration": dur, "source": source}


def _replace_song(song):
    def apply(q):
        store.delete_media([(q.get("song") or {}).get("url")])
        q["song"] = song
    return apply


@app.post("/api/projects/<pid>/song/compose")
def song_compose(pid):
    s = settings()

    def work(p):
        if not p.get("plan"):
            raise sv.UserError("Write the song first.")
        return _replace_song(_song_record(pid, sv.compose_song(s, p["plan"]), "elevenlabs", ".mp3"))
    return jsonify(_after_new_song(s, pid, run_project_task(pid, "compose", "Composing music", work)))


@app.post("/api/projects/<pid>/song/upload")
def song_upload(pid):
    load_or_404("projects", pid)
    f = request.files.get("file")
    if not f:
        raise sv.UserError("No file received.")
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in (".mp3", ".wav"):
        raise sv.UserError("Upload an .mp3 or .wav file.")
    p = update_doc("projects", pid, _replace_song(_song_record(pid, f.read(), "upload", ext)))
    return jsonify(_after_new_song(settings(), pid, p))


@app.post("/api/projects/<pid>/retime")
def projects_retime(pid):
    p = load_or_404("projects", pid)
    if not (p.get("plan") and p.get("song")):
        raise sv.UserError("Add a song first.")
    return jsonify(_fit_to_song(settings(), pid, p))


def _song_file(p):
    """The project's song on local disk, downloaded once per request."""
    url = p["song"]["url"]
    songs = g.setdefault("songs", {})
    if url not in songs or not os.path.exists(songs[url]):
        songs[url] = store.download_to(url, store.tmp_path("song" + (os.path.splitext(url)[1] or ".mp3")))
    return songs[url]


def _fit_to_song(s, pid, p):
    """Time every lyric word in the song (by code, from the exact written lyrics), then cut the scenes so
    each one covers the words it shows. Scenes that move lose their clip, preview and approval."""
    song = p["song"]
    path = _song_file(p)
    heard = song.get("words")
    if heard is None:
        try:
            heard = sv.transcribe_song(s, path)
        except sv.UserError as e:
            app.logger.warning("Couldn't check which lines are heard: %s", e)
    text = "\n".join(" ".join(words) for words in sync.lyric_lines(p["plan"]))
    lines = sync.song_timing(p["plan"], sync.force_align(s, path, text), heard or [], song["duration"])
    if heard is None:  # nothing to check against, so every line is captioned
        for ln in lines:
            ln["heard"] = True
    cast_ids = p["brief"].get("cast", [])

    def apply(q):
        if (q.get("song") or {}).get("url") != song["url"]:
            return
        q["song"].update(words=heard, timing=lines, timing_key=sync.timing_key(lines))
        moved = set(sync.fit_scenes(q["plan"], lines, song["duration"], cast_ids))
        sync.mark_reuse(q["plan"], lines, s.get("reuse_chorus") not in (False, "false", "0"))
        stale = []
        for sc in q["plan"]["scenes"]:
            if (sc["n"] in moved or sc.get("reuse")) and (sc.get("video") or sc.get("preview")):
                stale += [sc.pop("video", None), (sc.pop("preview", None) or {}).get("url")]
                sc.pop("review", None)
        store.delete_media(stale)
    return update_doc("projects", pid, apply)


def _after_new_song(s, pid, p):
    """Time a new song right away; if that fails, say why on the project instead of failing the upload."""
    try:
        return _fit_to_song(s, pid, p)
    except sv.UserError as e:
        error = str(e)
        return update_doc("projects", pid, lambda q: add_event(q, "Timing the lyrics", False, error, "timing"))


def _scene(p, n):
    sc = next((x for x in (p.get("plan") or {}).get("scenes", []) if x["n"] == n), None)
    if not sc:
        abort(404, "Scene not found")
    sc.setdefault("jobs", {})
    sc.setdefault("errors", {})
    return sc


def _set_scene_job(pid, n, kind, job):
    def apply(q):
        sc = _scene(q, n)
        sc["jobs"][kind] = job
        sc["errors"].pop(kind, None)
    return update_doc("projects", pid, apply)


def submit_image(s, pid, p, n):
    sc = _scene(p, n)
    if sc.get("reuse"):
        raise sv.UserError(f"Scene {n} reuses scene {sc['reuse']}'s shot. Redo that one instead.")
    look = (p["plan"].get("direction") or {}).get("look")
    where = dr.location(p["plan"], (sc.get("direction") or {}).get("location"))
    prompt, refs = sv.scene_image_prompt(s, sc, _scene_cast(p, sc), look, where, _cast_of(p))
    return _set_scene_job(pid, n, "image", sv.image_job(s, prompt, refs, s["aspect_ratio"]))


@app.post("/api/projects/<pid>/scenes/<int:n>/image")
def scene_image(pid, n):
    return jsonify(submit_image(settings(), pid, load_or_404("projects", pid), n))


def submit_video(s, pid, p, n, with_song=True):
    """with_song=False animates without the song slice: no lip-sync, for a shot a model's filter refuses."""
    sc = _scene(p, n)
    if sc.get("reuse"):
        raise sv.UserError(f"Scene {n} reuses scene {sc['reuse']}'s shot. Re-animate that one instead.")
    if not sc.get("image"):
        raise sv.UserError(f"Scene {n} needs a keyframe image first.")
    audio = None
    if with_song and p.get("song") and sv.video_mode(s) in ("seedance_ref", "wan_i2v"):
        # The scene's exact slice of the song: the model lip-syncs and dances to it.
        clip = sv.audio_slice_wav(_song_file(p), sc["start"], sc["end"])
        with open(clip, "rb") as f:
            audio = sv.hf_upload(s, f.read(), "audio/wav")
        os.remove(clip)
    review = sc.get("review") or {}
    note = review.get("note", "") if review.get("status") == "declined" else ""
    job = sv.video_job(s, sc, _scene_cast(p, sc), audio, p["plan"].get("bpm"), _cast_of(p), note)
    return _set_scene_job(pid, n, "video", job)


@app.post("/api/projects/<pid>/scenes/<int:n>/video")
def scene_video(pid, n):
    return jsonify(submit_video(settings(), pid, load_or_404("projects", pid), n))


# ------------------------------------------------------------------ scene previews and review
# Each finished clip becomes a preview: the clip cut frame-exact to its place in the song, with that
# slice of the song and the sing-along captions. You approve or decline each one; the final video is
# the approved previews joined as they are, so it can only contain what you approved.

def _wants_captions(s):
    return s.get("sing_along_captions") not in (False, "false", "0") and sv.captions_supported()


def _bumpers(s):
    return any(s.get(k) not in (False, "false", "0") for k in ("add_intro", "add_outro"))


def _preview_spec(s, p):
    """(key function, captions file maker) for this project's previews with the current settings."""
    song = p.get("song") or {}
    captions = bool(_wants_captions(s) and song.get("timing"))
    tkey = song.get("timing_key", "") if captions else ""
    duration = song.get("duration") or p["plan"]["scenes"][-1]["end"]
    kbps = sync.video_kbps(s, duration, _bumpers(s))
    return captions, tkey, kbps


def preview_key(s, p, sc):
    captions, tkey, kbps = _preview_spec(s, p)
    return sync.preview_key(sync.media_scene(p["plan"], sc), tkey, captions, s["aspect_ratio"], kbps)


def _build_preview(s, pid, p, sc):
    """Build and store one scene's preview; returns the project as saved."""
    captions, tkey, kbps = _preview_spec(s, p)
    sc = sync.media_scene(p["plan"], sc)
    key = sync.preview_key(sc, tkey, captions, s["aspect_ratio"], kbps)
    ass = None
    if (captions or sync.card_of(sc)) and sv.captions_supported():
        w, h = sv.frame_size(s["aspect_ratio"])
        ass = sync.write_captions(p["song"]["timing"] if captions else [], w, h, store.tmp_path("lyrics.ass"),
                                  p["plan"]["scenes"])
    out = sync.build_preview(sc, _song_file(p) if p.get("song") else None, ass, s["aspect_ratio"], kbps,
                             store.tmp_path(f"preview{sc['n']}.mp4"))
    with open(out, "rb") as f:
        url = store.put_file(f"projects/{pid}/scene{sc['n']}-preview.mp4", f.read(), "video/mp4")
    os.remove(out)
    if ass:
        os.remove(ass)

    def apply(q):
        target = _scene(q, sc["n"])
        if preview_key(s, q, target) != key:  # the scene changed while this was being built
            store.delete_media([url])
            return
        store.delete_media([(target.get("preview") or {}).get("url")])
        target["preview"] = {"url": url, "key": key}
        target["errors"].pop("preview", None)
        if (target.get("review") or {}).get("key") != key:
            target.pop("review", None)
    return update_doc("projects", pid, apply)


def _try_preview(s, pid, n):
    p = load_or_404("projects", pid)
    sc = _scene(p, n)
    try:
        return _build_preview(s, pid, p, sc)
    except (sv.UserError, RuntimeError) as e:
        error = str(e)

        def apply(q):
            _scene(q, n)["errors"]["preview"] = error
        return update_doc("projects", pid, apply)


@app.post("/api/projects/<pid>/scenes/<int:n>/preview")
def scene_preview(pid, n):
    return jsonify(_try_preview(settings(), pid, n))


def approved(s, p, sc):
    review, preview = sc.get("review") or {}, sc.get("preview") or {}
    return review.get("status") == "approved" and review.get("key") == preview.get("key") == preview_key(s, p, sc)


@app.post("/api/projects/<pid>/scenes/<int:n>/review")
def scene_review(pid, n):
    s = settings()
    data = body()
    decision, note = data.get("decision"), str(data.get("note", "")).strip()[:500]
    if decision not in ("approve", "decline", "clear"):
        raise sv.UserError("Approve or decline the scene.")

    def apply(q):
        sc = _scene(q, n)
        if decision == "clear":
            sc.pop("review", None)
            return
        key = (sc.get("preview") or {}).get("key")
        if decision == "approve" and key != preview_key(s, q, sc):
            raise sv.UserError(f"Scene {n}'s preview is out of date. Wait for the new one, then approve it.")
        sc["review"] = {"status": "approved" if decision == "approve" else "declined", "key": key,
                        "note": note, "at": store.now()}
        if decision == "decline":  # a person's decline is always remade, and a chorus repeat sends back its original
            sc["remakes"] = 0
            if sc.get("reuse"):
                src = _scene(q, sc["reuse"])
                src["review"], src["remakes"] = {**sc["review"], "key": (src.get("preview") or {}).get("key")}, 0
    return jsonify(update_doc("projects", pid, apply))


def submit_thumbnails(s, pid, p):
    plan = p.get("plan") or abort(400, "Write the song first.")
    cast = _cast_of(p)
    refs, legend = sv.cast_refs(s, cast, per_character=1)
    jobs = [sv.image_job(s, sv.thumbnail_prompt(s, plan, cast, legend, take), refs, "16:9", best=True)
            for take in range(len(sv.THUMBNAIL_TAKES))]

    def apply(q):
        q["thumb_jobs"], q["thumb_error"] = (q.get("thumb_jobs") or []) + jobs, None
    return update_doc("projects", pid, apply)


@app.post("/api/projects/<pid>/thumbnail")
def projects_thumbnail(pid):
    return jsonify(submit_thumbnails(settings(), pid, load_or_404("projects", pid)))


@app.post("/api/projects/<pid>/thumbnail/pick")
def projects_thumbnail_pick(pid):
    url = body().get("url")

    def apply(q):
        if url not in (q.get("thumb_options") or []):
            raise sv.UserError("That thumbnail isn't one of this video's options.")
        q["thumbnail"] = url
    return jsonify(update_doc("projects", pid, apply))


def _channel_intro(s, song):
    """Path to this format's intro, built once per animation. None if there's no animation yet."""
    ch = channel_doc()
    if not ch.get("clip"):
        return None
    aspect = s["aspect_ratio"]
    url = (ch.get("intros") or {}).get(aspect)
    if not url:
        path = sv.build_intro(s, ch["clip"], aspect, store.tmp_path("intro_final.mp4"))
        with open(path, "rb") as f:
            url = store.put_file("channel/intro.mp4", f.read(), "video/mp4")
        os.remove(path)

        def apply(q):
            q.setdefault("intros", {})[aspect] = url
        update_doc("app_settings", CHANNEL_ID, apply)
    return store.download_to(url, store.tmp_path("intro_ready.mp4"))


def _outro(s, pid, p, song):
    """Path to this video's outro, rebuilt only when its cast, channel or format changes."""
    cast = [c for c in cast_for(p["brief"].get("cast", [])) if c.get("images")]
    plan = p["plan"]
    closing = p.get("thumbnail") or next((sc["image"] for sc in reversed(plan["scenes"]) if sc.get("image")), None)
    if not closing and cast:
        closing = cast[0]["images"][0]
    if not closing:
        return None
    key = "|".join([s["aspect_ratio"], s["channel_name"], closing] + [f"{c['name']}:{c['images'][0]}" for c in cast])
    saved = p.get("outro") or {}
    url = saved.get("url") if saved.get("key") == key else None
    if not url:
        path = sv.build_outro(s, cast, closing, song, s["aspect_ratio"], store.tmp_path("outro_final.mp4"))
        with open(path, "rb") as f:
            url = store.put_file(f"projects/{pid}/outro.mp4", f.read(), "video/mp4")
        os.remove(path)

        def apply(q):
            store.delete_media([(q.get("outro") or {}).get("url")])
            q["outro"] = {"key": key, "url": url}
        update_doc("projects", pid, apply)
    return store.download_to(url, store.tmp_path("outro_ready.mp4"))


RENDER_BUILD_SECONDS = 200  # building missing previews stops here so the request ends in time


@app.post("/api/projects/<pid>/render")
def projects_render(pid):
    s = settings()
    draft = bool(body().get("draft"))

    def work(p):
        if not p.get("plan"):
            raise sv.UserError("Write the song first.")
        scenes = p["plan"]["scenes"]
        if not draft:
            waiting = [sc["n"] for sc in scenes if not approved(s, p, sc)]
            if waiting:
                raise sv.UserError(f"Approve every scene first. Still to approve: {', '.join(map(str, waiting))}.")
        began, paths, notes = time.time(), [], []
        if _wants_captions(s) is False and s.get("sing_along_captions") not in (False, "false", "0"):
            notes.append("This server's ffmpeg can't draw captions, so the video has no sing-along lyrics.")
        for i, sc in enumerate(scenes):
            if (sc.get("preview") or {}).get("key") != preview_key(s, p, sc):  # only drafts get here
                if time.time() - began > RENDER_BUILD_SECONDS:
                    raise sv.UserError(f"Made previews for scenes up to {sc['n'] - 1}. Press Render again to finish.")
                p = _build_preview(s, pid, p, sc)
                sc = _scene(p, sc["n"])
            paths.append((sc["preview"]["url"], store.download_to(sc["preview"]["url"], store.tmp_path(f"part{sc['n']}.mp4"))))
            _progress(pid, i + 1, len(scenes))
        song = _song_file(p) if p.get("song") else None
        out = sync.assemble([path for _, path in paths], song, store.tmp_path("final.mp4"))
        for url, path in paths:
            sv._discard_download(url, path)
        intro = outro = None
        if song:
            try:
                if s.get("add_intro") not in (False, "false", "0"):
                    intro = _channel_intro(s, song)
                if s.get("add_outro") not in (False, "false", "0"):
                    outro = _outro(s, pid, p, song)
            except (sv.UserError, RuntimeError) as e:
                notes.append(f"Rendered without the intro or outro: {e}")
        if intro or outro:
            joined = sv.join_videos([x for x in (intro, out, outro) if x], store.tmp_path("full.mp4"))
            os.remove(out)
            out = joined
            for extra in (intro, outro):
                if extra:
                    os.remove(extra)
        size_mb, limit = os.path.getsize(out) / 1e6, float(s.get("max_video_mb") or 50)
        if size_mb > limit:
            os.remove(out)
            raise sv.UserError(f"The finished video is {size_mb:.0f} MB, over your {limit:.0f} MB storage limit. "
                               "Raise the limit in Supabase and in Settings, then render again.")
        if draft:
            notes.append("Draft: not every scene was approved.")
        with open(out, "rb") as f:
            url = store.put_file(f"projects/{pid}/final.mp4", f.read(), "video/mp4")
        os.remove(out)
        note = " ".join(notes) or None

        def apply(q):
            store.delete_media([q.get("final")])
            q["final"], q["render_note"] = url, note
        return apply
    return jsonify(run_project_task(pid, "render", "Rendering video", work))


def _progress(pid, done, total, last=[0.0]):  # noqa: B006 - a deliberate per-process throttle
    if time.time() - last[0] < PROGRESS_SAVE_SECONDS and done < total:
        return
    last[0] = time.time()

    def apply(q):
        if q.get("task"):
            q["task"].update(done=done, total=total)
    update_doc("projects", pid, apply)


import pipeline  # noqa: E402,F401  (adds the server-side "do everything" routes)
import youtube  # noqa: E402,F401

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5178"))
    print(f"KidSong Studio running at http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
