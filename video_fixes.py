"""Overrides for services.py and app.py: an intro built from the real logo, and room for it in the file size.
Loaded by api/index.py. Kept apart because the deploy tool can't reliably carry a file as large as services.py."""
import math
import os

from flask import jsonify

import app as studio
import services as sv
import store

# Supabase's default upload limit is 50 MB, and that has to hold the intro and outro too. ffmpeg overshot
# its bitrate cap by 24% on a real video (short, busy segments), so allow for that and aim under the limit.
TARGET_VIDEO_MB = 46
BUMPERS_MB = 12
BITRATE_OVERSHOOT = 1.25
AUDIO_KBPS = 192


def video_kbps(plan):
    seconds = max(1.0, plan["scenes"][-1]["end"])
    room = TARGET_VIDEO_MB - BUMPERS_MB
    return min(sv.MAX_VIDEO_KBPS, int((room * 8000 / seconds - AUDIO_KBPS) / BITRATE_OVERSHOOT))


_join_videos = sv.join_videos


def join_videos(paths, out_path):
    """Join, then stop with a clear message if the finished video won't fit in storage."""
    _join_videos(paths, out_path)
    size_mb = os.path.getsize(out_path) / 1e6
    if size_mb >= sv.UPLOAD_LIMIT_MB:
        raise sv.UserError(f"The finished video is {size_mb:.0f} MB, over the {sv.UPLOAD_LIMIT_MB} MB storage limit. "
                           "Raise the upload limit in Supabase (Storage settings) and render again.")
    return out_path


# ============================================================== the intro

LOGO_SHARE = 0.7  # of the frame's shorter side
LOGO_HEADROOM = 1.25  # drawn larger than needed so the bounce's overshoot never scales it up
SPARKLE = "m 0 -34 b 4 -9 9 -4 34 0 b 9 4 4 9 0 34 b -4 9 -9 4 -34 0 b -9 -4 -4 -9 0 -34"
NOTE = ("m -22 26 b -22 14 -6 10 4 16 l 4 -34 l 10 -34 b 12 -20 28 -18 26 -2 b 22 -12 16 -14 10 -14 "
        "l 10 26 b 10 38 -22 38 -22 26")
# ASS colors are blue-green-red.
BURST_COLORS = ["&H22C9FF&", "&H1E8CFF&", "&HB469FF&", "&HD0A020&", "&H4CC44C&"]
BURST_COUNT = 22
BURST_START, BURST_END = 0.35, 2.7
BURST_SECONDS = 1.0


def _clear_outside(rgba, size):
    """Make whatever surrounds the logo transparent, found by spreading in from the corners.
    The logo is round but the image is square, so its corners would otherwise show as a box."""
    stack, seen = [], bytearray(size * size)
    for corner in (0, size - 1, size * (size - 1), size * size - 1):
        stack.append((corner, rgba[corner * 4:corner * 4 + 3]))
        seen[corner] = 1
    while stack:
        at, base = stack.pop()
        i = at * 4
        if abs(rgba[i] - base[0]) + abs(rgba[i + 1] - base[1]) + abs(rgba[i + 2] - base[2]) > 60:
            continue
        rgba[i + 3] = 0
        x, y = at % size, at // size
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < size and 0 <= ny < size and not seen[ny * size + nx]:
                seen[ny * size + nx] = 1
                stack.append((ny * size + nx, base))


def _logo_png(icon_url, size):
    src = store.download_to(icon_url, store.tmp_path("icon.png"))
    raw, out = store.tmp_path("icon.rgba"), store.tmp_path("logo.png")
    sv._run(["-i", src, "-vf", f"scale={size}:{size}", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba", raw])
    sv._discard_download(icon_url, src)
    with open(raw, "rb") as f:
        rgba = bytearray(f.read())
    _clear_outside(rgba, size)
    with open(raw, "wb") as f:
        f.write(rgba)
    sv._run(["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{size}x{size}", "-i", raw, "-frames:v", "1", out])
    os.remove(raw)
    return out


def _burst_file(w, h, logo):
    """Sparkles and music notes flying outward from just outside the logo, so they never cross it."""
    lines = sv._ass_header(w, h)
    near, far = logo * 0.58, logo * 0.58 + min(w, h) * 0.2
    for i in range(BURST_COUNT):
        angle = i * 2.39996  # the golden angle spreads them evenly without a visible pattern
        start = BURST_START + (BURST_END - BURST_START) * i / BURST_COUNT
        cx, cy = math.cos(angle), math.sin(angle)
        path = NOTE if i % 3 == 0 else SPARKLE
        size = 100 if path is NOTE else 45 + (i * 37) % 70
        tags = (f"\\an7\\bord0\\shad0\\1c{BURST_COLORS[i % len(BURST_COLORS)]}\\fscx{size}\\fscy{size}"
                f"\\move({w / 2 + cx * near:.0f},{h / 2 + cy * near:.0f},{w / 2 + cx * far:.0f},{h / 2 + cy * far:.0f})"
                "\\fad(120,350)")
        lines.append(f"Dialogue: 0,{sv._ass_time(start)},{sv._ass_time(start + BURST_SECONDS)},Sing,,0,0,0,,"
                     f"{{{tags}\\p1}}{path}{{\\p0}}")
    path = store.tmp_path("burst.ass")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _render_intro(icon_url, aspect, out_path, jingle=None):
    w, h = sv.frame_size(aspect)
    logo = int(min(w, h) * LOGO_SHARE)
    png = _logo_png(icon_url, int(logo * LOGO_HEADROOM))
    ass = _burst_file(w, h, logo)
    seconds = sv.INTRO_SECONDS
    fade = seconds - 0.4
    pop = f"{logo}*max(0.01,1-exp(-7*t)*cos(14*t))"
    graph = (f"[0:v]format=rgba,rotate='0.05*sin(9*t)*exp(-1.1*t)':ow='hypot(iw,ih)':oh=ow:c=none,"
             f"scale=w='{pop}*hypot(1,1)':h='{pop}*hypot(1,1)':eval=frame[logo];"
             f"[1:v][logo]overlay=x='(W-w)/2':y='(H-h)/2':eval=frame:shortest=1,setsar=1,"
             f"ass=filename='{ass}':fontsdir='{sv.fonts_dir()}',fade=t=out:st={fade}:d=0.4[v]")
    args = ["-loop", "1", "-framerate", "30", "-t", f"{seconds}", "-i", png,
            "-f", "lavfi", "-i", f"color=c={sv.INTRO_BACKGROUND}:s={w}x{h}:r=30:d={seconds}"]
    # Joining doesn't re-encode, so the tracks have to be in the main video's order: picture first, then sound.
    output = ["-filter_complex", graph, "-map", "[v]"]
    if jingle:
        args += ["-i", jingle]
        output += ["-map", "2:a", "-af", f"afade=t=out:st={fade}:d=0.4"]
    sv._run(args + output + ["-t", f"{seconds}"] + sv.BUMPER_ENCODE + [out_path])
    os.remove(png)
    os.remove(ass)
    return out_path


def _channel_intro(s, song):
    """Path to this format's intro, built once per icon. None if there's no icon yet."""
    ch = studio.channel_doc()
    if not ch.get("icon"):
        return None
    aspect = s["aspect_ratio"]
    url = (ch.get("intros") or {}).get(aspect)
    if not url:
        jingle = store.tmp_path("jingle.mp3")
        with open(jingle, "wb") as f:
            f.write(sv.compose_jingle(s, sv.INTRO_SECONDS + 0.5))
        path = _render_intro(ch["icon"], aspect, store.tmp_path("intro_final.mp4"), jingle)
        os.remove(jingle)
        with open(path, "rb") as f:
            url = store.put_file("channel/intro.mp4", f.read(), "video/mp4")
        os.remove(path)

        def apply(q):
            q.setdefault("intros", {})[aspect] = url
        studio.update_doc("app_settings", studio.CHANNEL_ID, apply)
    return store.download_to(url, store.tmp_path("intro_ready.mp4"))


def channel_animate():
    """Build the silent preview shown in Settings. Free and instant: no AI redraws the logo."""
    ch = studio.channel_doc()
    if not ch.get("icon"):
        raise sv.UserError("Generate the channel icon first.")
    path = _render_intro(ch["icon"], studio.settings()["aspect_ratio"], store.tmp_path("preview.mp4"))
    with open(path, "rb") as f:
        url = store.put_file("channel/clip.mp4", f.read(), "video/mp4")
    os.remove(path)

    def apply(q):
        store.delete_media([q.get("clip")] + list((q.get("intros") or {}).values()))
        q["clip"], q["clip_job"], q["clip_error"], q["intros"] = url, None, None, {}
    return jsonify(studio.update_doc("app_settings", studio.CHANNEL_ID, apply))


sv.video_kbps = video_kbps
sv.join_videos = join_videos
studio._channel_intro = _channel_intro
studio.app.view_functions["channel_animate"] = channel_animate
