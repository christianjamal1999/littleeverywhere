"""Do everything, on the server: one step per tick, for every video that has it switched on.

/api/tick is called once a minute by Vercel's cron (see vercel.json) and by any open browser tab, so a
video keeps moving with the tab closed. Each tick collects finished Higgsfield jobs, then for each video
works out the next missing thing and does it. Anything that fails more than a couple of times, or needs
money or a person, stops that video with a plain message instead of trying forever."""
import base64
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from flask import jsonify, request

import services as sv
import store
import sync

studio = sys.modules.get("app") or sys.modules["__main__"]
app = studio.app

WAVE = 8  # scene jobs kept running on Higgsfield at once
PREVIEWS_PER_TICK = 6
REVIEWS_PER_TICK = 6
MAX_TRIES = 3  # of one step, or of one scene's keyframe or clip
MAX_REMAKES = 2  # times the automatic check may send one scene back
MAX_RECOMPOSE = 2
UNHEARD_LIMIT = 0.12  # share of lyric lines missing from the song before it's composed again
STEP_LOCK_SECONDS = 310  # a step still marked pending after this long was cut off by the server
TICK_SECONDS = 240  # stop starting new work after this long, so the request ends inside the limit


def _off(x):
    return x in (False, "false", "0")


def _scenes(p):
    return (p.get("plan") or {}).get("scenes", [])


def _own(p):
    """Scenes that need their own keyframe and clip (not a reused chorus shot)."""
    return [sc for sc in _scenes(p) if not sc.get("reuse")]


# ============================================================== the automatic check of each clip

REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["ok", "people", "problems", "note"],
    "properties": {"ok": {"type": "boolean"}, "people": {"type": "integer"},
                   "problems": {"type": "array", "items": {"type": "string"}}, "note": {"type": "string"}},
}


def _image(data, media_type="image/jpeg"):
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.b64encode(data).decode()}}


def _small(url):
    """A character's reference image, shrunk for the check."""
    src = store.download_to(url, store.tmp_path("ref" + (os.path.splitext(url.split("?")[0])[1] or ".png")))
    out = store.tmp_path("ref_small.jpg")
    sv._run(["-i", src, "-vf", "scale=512:-2", "-frames:v", "1", "-q:v", "5", out])
    sv._discard_download(url, src)
    with open(out, "rb") as f:
        data = f.read()
    os.remove(out)
    return data


def review_clip(s, sc, cast, frames, refs):
    """Claude looks at frames from one clip beside the cast's reference images. Returns the verdict."""
    names = ", ".join(c["name"] for c in cast) or "nobody (an insert shot with no people)"
    content = []
    for c in cast:
        if c["id"] in refs:
            content += [{"type": "text", "text": f"Reference for {c['name']}"
                         + (f" (signature item: {c['icon']})" if c.get("icon") else "") + ":"}, _image(refs[c["id"]])]
    content.append({"type": "text", "text": "Frames from the clip, in order:"})
    content += [_image(f) for f in frames]
    content.append({"type": "text", "text": f"""You are the quality check for one shot of a kids' music video.
The shot should show exactly {len(cast)} {'person' if len(cast) == 1 else 'people'}: {names}.
Check every frame for problems a parent watching would notice:
1. People: count them. Any extra person anywhere (background, doorway, edge of frame), or someone missing?
2. Identity: is each one clearly the character in their reference - same hair color and style, same
   signature item, same outfit or costume? Hair or the signature item changing between frames is a problem.
3. Faces and bodies: eyes, teeth and hands well-formed, no extra or fused limbs, nothing melted or frightening.
4. Sharpness: faces in focus, not smeared or heavily motion-blurred.
5. No garbled text, logos or watermarks.
Be lenient about pose, expression, lighting and small style differences; they are meant to vary.
ok: false only when there is a real problem from the list. people: how many people you count.
problems: each real problem in a few words (empty when ok). note: one short sentence telling the animator
what to fix in the remake (empty when ok)."""})
    msg = sv._claude(s, what=f"Scene check, scene {sc['n']}",
                     output_config={"effort": "low", "format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
                     messages=[{"role": "user", "content": content}])
    return json.loads(next(b.text for b in msg.content if b.type == "text"))


def _review_wave(s, pid, p, todo):
    cast_all = studio._cast_of(p)
    refs = {c["id"]: _small(c["images"][0]) for c in cast_all if c.get("images")}
    grabs = {}
    for sc in todo:
        media = sync.media_scene(p["plan"], sc)
        path = store.download_to(media["video"], store.tmp_path(f"check{sc['n']}.mp4"))
        grabs[sc["n"]] = sync.frame_grabs(path, min(sc["end"] - sc["start"], sv.media_duration(path)))
        sv._discard_download(media["video"], path)
    project = sv.current_spend_project()

    def check(sc):
        sv.set_spend_project(project)
        try:
            return sc["n"], review_clip(s, sc, studio._scene_cast(p, sc), grabs[sc["n"]], refs), None
        except sv.UserError as e:
            return sc["n"], None, str(e)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(check, todo))
    failed = [err for _, verdict, err in results if err]
    if failed and len(failed) == len(results):
        raise sv.UserError(f"The automatic check couldn't run: {failed[0]}")

    def apply(q):
        for n, verdict, err in results:
            sc = studio._scene(q, n)
            key = (sc.get("preview") or {}).get("key")
            if not verdict or not key or sc.get("review"):
                continue
            if verdict["ok"]:
                sc["review"] = {"status": "approved", "by": "ai", "key": key, "note": "", "at": store.now()}
            else:
                remakes = sc.get("remakes", 0)
                sc["review"] = {"status": "declined", "by": "ai", "key": key, "at": store.now(),
                                "note": verdict["note"] or "; ".join(verdict["problems"]),
                                "problems": verdict["problems"], "stuck": remakes >= MAX_REMAKES}
    return studio.update_doc("projects", pid, apply)


# ============================================================== what to do next

class Stop(Exception):
    """The video can't go on by itself. done=True means it's finished."""

    def __init__(self, message, done=False):
        super().__init__(message)
        self.done = done


class Wait(Exception):
    """Nothing to do right now (jobs running, or it's the person's turn)."""


def _cast_step(s, p):
    cast = studio.cast_for(p["brief"].get("cast", []))
    look = sv.look_of(s)
    for c in cast:
        if c.get("job"):
            raise Wait(f"Drawing {c['name']} in the {look} look…")
        if c.get("job_error") and c.get("restyling"):
            raise Stop(f"Couldn't redraw {c['name']}: {c['job_error']}")
        if not c.get("images"):
            raise Stop(f"{c['name']} has no picture yet. Generate one in Characters, then press Do everything.")
        if look != "custom" and c.get("style", "photo") != look:
            return "cast", f"Redrawing {c['name']} in the {look} look", lambda: studio.restyle_character(s, c)
        if c.get("needs_body"):
            raise Wait(f"Drawing {c['name']}…")
    return None


def next_step(s, p):
    """(step name, label, function) for the next thing to do. Raises Wait or Stop."""
    pid, plan, auto = p["id"], p.get("plan"), p.get("auto") or {}
    step = _cast_step(s, p)
    if step:
        return step
    if not plan:
        return "write", "Writing the song and storyboard", lambda: studio.projects_write(pid)
    song = p.get("song")
    if not song:
        return "compose", "Making the music", lambda: studio.song_compose(pid)
    if not song.get("timing"):
        return "timing", "Timing the lyrics to the song", lambda: studio._fit_to_song(s, pid, p)
    if (song.get("source") == "elevenlabs" and sync.unheard_share(song["timing"]) > UNHEARD_LIMIT
            and auto.get("recomposed", 0) < MAX_RECOMPOSE and not any(sc.get("video") for sc in _scenes(p))):
        def recompose():
            studio.update_doc("projects", pid, lambda q: q["auto"].update(recomposed=auto.get("recomposed", 0) + 1))
            return studio.song_compose(pid)
        return "recompose", "Some lyric lines are missing from the song, so making the music again", recompose
    if not plan.get("choreography"):
        return "choreograph", "Choreographing the dance", lambda: studio.projects_choreograph(pid)
    if not plan.get("direction"):
        return "direct", "Directing every shot", lambda: studio.projects_direct(pid)

    locs = plan["direction"]["locations"]
    bad = next((loc for loc in locs if loc.get("error")), None)
    if bad:
        raise Stop(f"The set photo of {bad['name']} failed: {bad['error']}")
    missing = [loc for loc in locs if not loc.get("image") and not loc.get("job")]
    if missing:
        return "sets", "Making the set photos", lambda: [studio.location_plate(pid, loc["id"]) for loc in missing]
    if any(loc.get("job") for loc in locs):
        raise Wait("Making the set photos…")

    scenes, own = _scenes(p), _own(p)
    running = sum(len(sc.get("jobs") or {}) for sc in scenes)
    for kind, what in (("image", "keyframe"), ("video", "animation")):
        for sc in own:
            err = (sc.get("errors") or {}).get(kind)
            if not err:
                continue
            if sv.low_balance(err):
                raise Stop("Your Higgsfield balance ran out. Add funds at console.higgsfield.ai, then press "
                           "Do everything again; it picks up where it stopped.")
            if (sc.get("tries") or {}).get(kind, 0) >= MAX_TRIES:
                raise Stop(f"Scene {sc['n']}'s {what} failed {MAX_TRIES} times: {err}")

    room = WAVE - running
    todo = [sc for sc in own if not sc.get("image") and not (sc.get("jobs") or {}).get("image")]
    if todo and room > 0:
        batch = todo[:room]
        return ("keyframes", f"Painting keyframes ({len(own) - len(todo)}/{len(own)} done)",
                lambda: [studio.submit_image(s, pid, p, sc["n"]) for sc in batch])

    def needs_clip(sc):
        review = sc.get("review") or {}
        redo = review.get("status") == "declined" and not review.get("stuck") and sc.get("video")
        return sc.get("image") and not (sc.get("jobs") or {}).get("video") and (not sc.get("video") or redo)

    todo = [sc for sc in own if needs_clip(sc)]
    if todo and room > 0 and not any(not sc.get("image") for sc in own):
        batch = todo[:room]
        done = sum(bool(sc.get("video")) for sc in own)

        def animate():
            sv.test_higgsfield(s)  # stops here, with a clear message, when the balance is at zero
            for sc in batch:
                err = (sc.get("errors") or {}).get("video")
                # A shot the model's filter refused twice is tried once more without the song slice.
                with_song = not (sv.content_blocked(err) and (sc.get("tries") or {}).get("video", 0) >= 2)
                if (sc.get("review") or {}).get("status") == "declined":
                    studio.update_doc("projects", pid, lambda q, n=sc["n"]: studio._scene(q, n).update(
                        remakes=studio._scene(q, n).get("remakes", 0) + 1))
                studio.submit_video(s, pid, p, sc["n"], with_song)
        return "animation", f"Animating with the song ({done}/{len(own)} done)", animate
    if running:
        raise Wait(f"Generating on Higgsfield ({running} running)…")

    stale = [sc for sc in scenes if sync.media_scene(plan, sc).get("video")
             and (sc.get("preview") or {}).get("key") != studio.preview_key(s, p, sc)]
    failed = next((sc for sc in stale if (sc.get("tries") or {}).get("preview", 0) >= MAX_TRIES), None)
    if failed:
        raise Stop(f"Scene {failed['n']}'s preview couldn't be made after {MAX_TRIES} tries: "
                   f"{(failed.get('errors') or {}).get('preview')}")
    if stale:
        batch = stale[:PREVIEWS_PER_TICK]
        began = time.time()

        def cut():
            for sc in batch:
                if time.time() - began < TICK_SECONDS - sync.PREVIEW_TIMEOUT:  # the rest wait for the next tick
                    studio._try_preview(s, pid, sc["n"])
        return "previews", f"Cutting each scene to the song ({len(scenes) - len(stale)}/{len(scenes)} done)", cut

    # Chorus repeats follow their original: approved when it is.
    by_n = {sc["n"]: sc for sc in scenes}
    follow = [sc for sc in scenes if sc.get("reuse") and not sc.get("review")
              and studio.approved(s, p, by_n[sc["reuse"]])]
    if follow:
        def inherit():
            def apply(q):
                for sc in follow:
                    target = studio._scene(q, sc["n"])
                    target["review"] = {"status": "approved", "by": "reuse", "note": "", "at": store.now(),
                                        "key": (target.get("preview") or {}).get("key")}
            return studio.update_doc("projects", pid, apply)
        return "inherit", "Approving the repeated chorus shots", inherit

    unreviewed = [sc for sc in own if not sc.get("review")]
    if unreviewed and not _off(s.get("auto_review")):
        batch = unreviewed[:REVIEWS_PER_TICK]
        return ("review", f"Checking each scene for mistakes ({len(own) - len(unreviewed)}/{len(own)} checked)",
                lambda: _review_wave(s, pid, p, batch))
    waiting = [sc["n"] for sc in scenes if not studio.approved(s, p, sc)]
    if waiting:
        stuck = [sc["n"] for sc in own if (sc.get("review") or {}).get("stuck")]
        if stuck:
            raise Wait(f"Your turn: scene{'s' if len(stuck) > 1 else ''} {', '.join(map(str, stuck))} still "
                       "had a problem after two remakes. Watch and approve, or decline with a note.")
        raise Wait(f"Your turn: watch and approve {len(waiting)} scene{'s' if len(waiting) > 1 else ''} below.")

    # Thumbnails never hold the video up: they're tried a few times, and the video finishes either way.
    if not p.get("thumbnail") and not p.get("thumb_jobs") and auto.get("thumb_tries", 0) < MAX_TRIES:
        def thumbnails():
            studio.update_doc("projects", pid, lambda q: (q["auto"].update(thumb_tries=auto.get("thumb_tries", 0) + 1),
                                                          q.update(thumb_error=None)))
            return studio.submit_thumbnails(s, pid, p)
        return "thumbnails", "Making three thumbnails", thumbnails
    if not p.get("final") or auto.get("rendered") != _render_stamp(p):
        def render():
            out = studio.projects_render(pid)
            studio.update_doc("projects", pid, lambda q: q["auto"].update(rendered=_render_stamp(q)))
            return out
        return "render", "Rendering the finished video", render
    if not p.get("short") and song and not auto.get("short_failed"):
        return "short", "Cutting the vertical Short", lambda: build_short(s, pid)
    if p.get("thumb_jobs"):
        raise Wait("Finishing the thumbnails…")
    if not p.get("thumbnail"):
        raise Stop(f"Your video is ready. The thumbnails couldn't be made ({p.get('thumb_error') or 'no reason given'}); "
                   "press Make 3 thumbnails in a little while.", done=True)
    raise Stop("Your video is ready: pick a thumbnail and publish.", done=True)


def _render_stamp(p):
    return "|".join((sc.get("preview") or {}).get("key", "") for sc in _scenes(p))


# ============================================================== the Short

def build_short(s, pid):
    def work(p):
        if not (p.get("plan") and p.get("song")):
            raise sv.UserError("The Short needs the song and the animated scenes.")
        run = [sync.media_scene(p["plan"], sc) for sc in sync.short_scenes(p["plan"])]
        if any(not sc.get("video") for sc in run):
            raise sv.UserError("Animate the first chorus before cutting the Short.")
        out = sync.build_short(run, studio._song_file(p), store.tmp_path("short_final.mp4"))
        with open(out, "rb") as f:
            url = store.put_file(f"projects/{pid}/short.mp4", f.read(), "video/mp4")
        os.remove(out)

        def apply(q):
            store.delete_media([q.get("short")])
            q["short"] = url
        return apply
    return studio.run_project_task(pid, "short", "Cutting the vertical Short", work)


@app.post("/api/projects/<pid>/short")
def project_short(pid):
    return jsonify(build_short(studio.settings(), pid))


# ============================================================== running it

def _set_auto(pid, **changes):
    def apply(q):
        q.setdefault("auto", {}).update(changes)
    return studio.update_doc("projects", pid, apply)


def advance(s, p):
    """Do this video's next step. Returns a short description of what happened."""
    pid, auto = p["id"], p.get("auto") or {}
    if studio.task_running(p):
        return "busy"
    # One request at a time per video: an open tab and the server's timer both call this.
    if auto.get("pending") and time.time() - auto.get("pending_at", 0) < STEP_LOCK_SECONDS:
        return "busy"
    sv.set_spend_project(pid)
    try:
        step, label, run = next_step(s, p)
    except Wait as e:
        if auto.get("status") != str(e):
            _set_auto(pid, status=str(e), step=None, fails=0, pending=False)
        return "waiting"
    except Stop as e:
        message, finished = str(e), e.done
        if not finished:
            return _stop(pid, message)

        def apply(q):
            q["auto"] = {**(q.get("auto") or {}), "on": False, "status": message, "done": True, "error": None,
                         "step": None, "pending": False}
            studio.add_event(q, "Your video is ready", ok=True, error=None, kind="auto")
        studio.update_doc("projects", pid, apply)
        return "done"
    same = auto.get("step") == step
    fails = (auto.get("fails", 0) if same else 0) + (1 if same and auto.get("pending") else 0)  # cut off mid-step
    if fails >= MAX_TRIES:
        return _stop(pid, f"{label} didn't finish after {MAX_TRIES} tries. {auto.get('last_error') or ''}".strip())
    _set_auto(pid, status=label + "…", step=step, fails=fails, pending=True, pending_at=time.time(), error=None)
    try:
        run()
    except (sv.UserError, RuntimeError) as e:
        message = str(e)
        if "already running" in message:
            _set_auto(pid, pending=False)
            return "busy"
        if sv.low_balance(message):
            return _stop(pid, message)
        if step == "short":
            _set_auto(pid, short_failed=True, pending=False, last_error=message)
        else:
            _set_auto(pid, fails=fails + 1, pending=False, last_error=message)
        return f"{step} failed"
    _set_auto(pid, fails=0, pending=False, last_error=None)
    return step


def _stop(pid, message):
    def apply(q):
        q["auto"] = {**(q.get("auto") or {}), "on": False, "status": message, "error": message, "step": None,
                     "pending": False}
        studio.add_event(q, "Do everything stopped", ok=False, error=message, kind="auto")
    studio.update_doc("projects", pid, apply)
    return "stopped"


@app.route("/api/tick", methods=["GET", "POST"])
def tick():
    """Collect finished jobs and move every switched-on video one step. Safe to call at any time; open to
    Vercel's cron without the app password because it takes no input and only continues work you started."""
    began = time.time()
    s = studio.settings()
    try:
        studio.activity()
    except Exception as e:  # noqa: BLE001 - polling trouble shouldn't stop the steps below
        app.logger.warning("tick: collecting jobs failed: %s", e)
    done = {}
    for p in store.list_docs("projects"):
        if not (p.get("auto") or {}).get("on") or time.time() - began > TICK_SECONDS:
            continue
        try:
            done[p["id"]] = advance(s, store.load_doc("projects", p["id"]) or p)
        except Exception as e:  # noqa: BLE001 - one video's trouble shouldn't block the others
            app.logger.exception(e)
            done[p["id"]] = f"error: {e}"
    return jsonify(done=done, seconds=round(time.time() - began, 1))


@app.post("/api/projects/<pid>/auto")
def project_auto(pid):
    on = bool((request.get_json(silent=True) or {}).get("on", True))
    s = studio.settings()
    if on:
        sv.need(s, "anthropic_api_key", "elevenlabs_api_key", "hf_key_id", "hf_key_secret")
        sv.test_higgsfield(s)

    def apply(q):
        if on:
            q["auto"] = {"on": True, "status": "Starting…", "started": time.time(),
                         "recomposed": (q.get("auto") or {}).get("recomposed", 0)}
            for sc in _scenes(q):  # a fresh start retries what failed before
                sc["errors"] = {}
                sc.pop("tries", None)
                if (sc.get("review") or {}).get("stuck"):
                    sc["review"]["stuck"], sc["remakes"] = False, 0
            for loc in studio._locations(q):
                loc.pop("error", None)
            q["thumb_error"] = None
        else:
            q["auto"] = {**(q.get("auto") or {}), "on": False, "status": "Stopped."}
    return jsonify(studio.update_doc("projects", pid, apply))


# ============================================================== what it will cost

FALLBACK_USD = {"keyframe": 0.11, "clip": 0.75, "set": 0.08, "thumbnail": 0.10, "claude": 0.6, "song": 0.4}


@app.get("/api/projects/<pid>/quote")
def project_quote(pid):
    """Roughly what finishing this video costs from here, using Higgsfield's own price for one clip."""
    s = studio.settings()
    p = studio.load_or_404("projects", pid)
    scenes = _scenes(p)
    if scenes:
        own = _own(p)
        clips = sum(not sc.get("video") for sc in own)
        keyframes = sum(not sc.get("image") for sc in own)
        reused = len(scenes) - len(own)
    else:
        total = max(8, round(float(p["brief"].get("length") or 150) / 4.5))
        reused = round(total * 0.3) if not _off(s.get("reuse_chorus")) else 0
        clips = keyframes = total - reused
    clip_usd, priced = FALLBACK_USD["clip"], False
    sample = next((c["images"][0] for c in studio.cast_for(p["brief"].get("cast", [])) if c.get("images")), None)
    if sample and clips and sv.video_mode(s) == "seedance_ref":
        try:
            clip_usd = sv.hf_estimate(s, s["seedance_endpoint"], {
                "prompt": "The kids dance", "image_urls": [sv.public_url(s, sample)], "duration": 5,
                "resolution": s.get("video_resolution") or "1080p", "aspect_ratio": s["aspect_ratio"],
                "generate_audio": True}, timeout=15)
            priced = True
        except sv.UserError:
            pass
    plan = p.get("plan") or {}
    usd = (clips * clip_usd + keyframes * FALLBACK_USD["keyframe"]
           + (0 if plan.get("direction") else 3 * FALLBACK_USD["set"] + FALLBACK_USD["claude"])
           + (0 if p.get("song") else FALLBACK_USD["song"])
           + (0 if p.get("thumbnail") else 3 * FALLBACK_USD["thumbnail"])
           + (clips * 0.02 if not _off(s.get("auto_review")) else 0))
    return jsonify(usd=round(usd, 2), clips=clips, keyframes=keyframes, reused=reused,
                   clip_usd=round(clip_usd, 2), priced=priced)
