"""Everything that has to line up with the song, done by code: when each lyric word is sung, where each
scene starts and ends, and the per-scene previews (clip + song + sing-along captions) that the final
video is assembled from without re-encoding, so the finished video is exactly what was approved.

Timing: the written lyrics are pinned to the audio with ElevenLabs forced alignment (it is given the exact
text, so captions can only ever show the real lyrics). The speech-to-text transcript is used only to check
which lines can actually be heard; a line nobody can hear gets no caption and no lip-sync."""
import difflib
import hashlib
import json
import os
import re

import requests

import services as sv
import store

FPS = 30
LEAD_SECONDS = 0.25  # a scene starts just before its first sung word
HEARD_WINDOW = 1.5  # a transcript word this close to where a lyric word was aligned confirms the line is sung
MIN_SCENE_SECONDS = 1.5
BITRATE_OVERSHOOT = 1.2
ALIGN_USD_PER_HOUR = sv.TRANSCRIBE_USD_PER_HOUR
PREVIEW_AUDIO = ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]


def _plain(word):
    return re.sub(r"[^a-z0-9]", "", word.lower())


def lyric_lines(plan):
    return [words for sec in plan["sections"] for words in sv._lyric_lines(sec.get("lyrics"))]


# ============================================================== when each word is sung

def force_align(settings, song_path, text):
    """Word timings for this exact text in this song: [{text, start, end}]."""
    sv.need(settings, "elevenlabs_api_key")
    try:
        with open(song_path, "rb") as f:
            r = requests.post("https://api.elevenlabs.io/v1/forced-alignment",
                              headers={"xi-api-key": settings["elevenlabs_api_key"]},
                              files={"file": ("song.mp3", f, "audio/mpeg")}, data={"text": text}, timeout=180)
    except requests.RequestException:
        raise sv.UserError("Couldn't reach ElevenLabs to time the lyrics.")
    if r.status_code in (401, 403):
        raise sv.UserError("This ElevenLabs key isn't allowed to use forced alignment.")
    if r.status_code >= 400:
        raise sv.UserError(f"ElevenLabs forced alignment error {r.status_code}: {r.text[:200]}")
    words = [{"text": w["text"], "start": float(w["start"]), "end": float(w["end"])}
             for w in r.json().get("words", []) if _plain(str(w.get("text", "")))]
    if words:
        sv.log_spend("ElevenLabs", "Lyric timing", words[-1]["end"] / 3600 * ALIGN_USD_PER_HOUR,
                     "Pinned the written lyrics to the song")
    return words


def _fill_gaps(times, total_end):
    """Words without a timing share the time between their timed neighbours evenly."""
    n, i = len(times), 0
    while i < n:
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        lo = times[i - 1][1] if i else max(0.0, (times[j][0] if j < n else total_end) - 0.3 * (j - i))
        hi = times[j][0] if j < n else min(total_end, lo + 0.3 * (j - i))
        step = max(0.0, hi - lo) / (j - i)
        for k in range(i, j):
            times[k] = (lo + step * (k - i), lo + step * (k - i + 1))
        i = j
    return times


def song_timing(plan, aligned, heard, duration):
    """Caption lines [{words, times, heard}] from the written lyrics, the aligned words and the transcript."""
    lines = lyric_lines(plan)
    written = [w for line in lines for w in line]
    if not written:
        return []
    times = [None] * len(written)
    matcher = difflib.SequenceMatcher(None, [_plain(w) for w in written], [_plain(w["text"]) for w in aligned],
                                      autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            word = aligned[block.b + k]
            times[block.a + k] = (word["start"], word["end"])
    if not any(times):
        raise sv.UserError("Couldn't find the lyrics in this song. Do the written lyrics match what's sung?")
    times = _fill_gaps(times, duration)
    out, at = [], 0
    for words in lines:
        t = times[at:at + len(words)]
        at += len(words)
        wanted = {_plain(w) for w in words}
        lo, hi = t[0][0] - HEARD_WINDOW, t[-1][1] + HEARD_WINDOW
        out.append({"words": words, "times": [list(x) for x in t],
                    "heard": any(_plain(h["text"]) in wanted and lo <= h["start"] <= hi for h in heard)})
    return out


def timing_key(lines):
    return hashlib.sha1(json.dumps(lines, sort_keys=True).encode()).hexdigest()[:12]


# ============================================================== scenes cut to the song

def _snap(t):
    return round(t * FPS) / FPS


def fit_scenes(plan, lines, duration, cast_ids=None):
    """Move scene boundaries so every scene starts just before its own lyric lines are sung and ends where
    the next scene's begin. Fills scene["sung"] with the words really sung in it. Returns the numbers of
    scenes whose start or end moved by more than a frame."""
    scenes = plan["scenes"]
    sung_lines = [ln for ln in lines if ln["heard"]]
    key = lambda words: " ".join(_plain(w) for w in words)  # noqa: E731
    scene_lines = [(i, key(words)) for i, sc in enumerate(scenes) for words in sv._lyric_lines(sc.get("lyric"))]
    matcher = difflib.SequenceMatcher(None, [k for _, k in scene_lines], [key(ln["words"]) for ln in lines],
                                      autojunk=False)
    ranges = {}
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            i, ln = scene_lines[block.a + k][0], lines[block.b + k]
            if ln["heard"]:
                lo, hi = ranges.get(i, (ln["times"][0][0], ln["times"][-1][1]))
                ranges[i] = (min(lo, ln["times"][0][0]), max(hi, ln["times"][-1][1]))
    # Anchors: scenes with sung lines start just before them, in order. An anchor out of order is ignored.
    starts, last = {0: 0.0}, 0.0
    for i in range(1, len(scenes)):
        if i in ranges:
            t = ranges[i][0] - LEAD_SECONDS
            prev = max((ranges[j][1] for j in ranges if j < i), default=0.0)
            t = max(t, min(prev + 0.05, ranges[i][0]))
            if t >= last + MIN_SCENE_SECONDS:
                starts[i] = last = t
    anchors = sorted(starts) + [len(scenes)]
    times = {len(scenes): duration}
    for a, b in zip(anchors, anchors[1:]):
        ta, tb = starts[a], times.get(b, starts.get(b))
        times[a] = ta
        free = b - a - 1  # scenes with no sung words between two anchors
        if not free:
            continue
        # The anchor scene keeps its sung words; the scenes after it share what's left before the next anchor.
        # An anchor with no words (the very first scene) takes an equal share like the rest.
        begin = ranges[a][1] + 0.05 if a in ranges else ta + (tb - ta) / (free + 1)
        if (tb - begin) / free < MIN_SCENE_SECONDS:
            begin = ta + (tb - ta) / (free + 1)
        for k in range(free):
            times[a + 1 + k] = begin + (tb - begin) * k / free
    moved = []
    for i, sc in enumerate(scenes):
        start, end = _snap(times[i]), _snap(times[i + 1])
        if abs(start - sc["start"]) > 1 / FPS or abs(end - sc["end"]) > 1 / FPS:
            moved.append(sc["n"])
        sc["start"], sc["end"] = start, end
        words = [w for ln in sung_lines for w, t in zip(ln["words"], ln["times"]) if start <= t[0] < end]
        sc["sung"] = " ".join(words)
        if words and not sc.get("singers"):
            sc["singers"] = [c for c in sc.get("cast", []) if not cast_ids or c in cast_ids]
        if not words:
            sc["singers"] = []
    return moved


# ============================================================== previews and the final video

def frames(sc):
    return round(sc["start"] * FPS), round(sc["end"] * FPS)


def video_kbps(settings, duration, bumpers):
    """Bitrate that keeps the finished video under the storage limit, capped at a visually lossless rate."""
    limit = float(settings.get("max_video_mb") or 50)
    room = limit * 0.9 - (12 if bumpers else 0)
    # x264 overshoots its cap by up to ~20% on short, busy clips, so aim below it.
    return int(max(1500, min(12000, (room * 8000 / max(1.0, duration) - 192) / BITRATE_OVERSHOOT)))


def preview_key(sc, tkey, captions, size, kbps):
    return "|".join(str(x) for x in (sc.get("video") or sc.get("image") or "none", *frames(sc), tkey, captions,
                                     size, kbps, 2))


def write_captions(lines, w, h, path):
    out = sv._ass_header(w, h) + sv._timed_caption_events([ln for ln in lines if ln["heard"]], sv.POP)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return path


def build_preview(sc, song_path, ass, aspect, kbps, out_path):
    """One scene, frame-exact on the song's timeline, with its slice of the song and burned-in captions.
    Every preview is encoded the same way so the final video joins them without re-encoding."""
    w, h = sv.frame_size(aspect)
    f0, f1 = frames(sc)
    n = max(1, f1 - f0)
    start, dur = f0 / FPS, n / FPS
    fit = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={FPS},"
           f"tpad=stop_mode=clone:stop_duration={dur:.3f}")  # hold the last frame rather than loop back
    sub = (f",setpts=PTS+{start:.4f}/TB,ass=filename='{ass}':fontsdir='{sv.fonts_dir()}',setpts=PTS-STARTPTS"
           if ass else "")
    if sc.get("video"):
        src = store.download_to(sc["video"], store.tmp_path("clip.mp4"))
        inputs, vf = ["-i", src], fit + sub
    elif sc.get("image"):
        src = store.download_to(sc["image"], store.tmp_path("still.png"))
        zw, zh = w * 3 // 2, h * 3 // 2
        inputs = ["-loop", "1", "-i", src]
        vf = (f"scale={zw}:{zh}:force_original_aspect_ratio=increase,crop={zw}:{zh},"
              f"zoompan=z='min(zoom+0.0007,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={n + 1}:s={w}x{h}:fps={FPS},setsar=1" + sub)
    else:
        src = None
        inputs, vf = ["-f", "lavfi", "-i", f"color=c=0xFFE9A8:s={w}x{h}:r={FPS}"], "null" + sub
    audio = (["-ss", f"{start:.4f}", "-t", f"{dur:.4f}", "-i", song_path] if song_path
             else ["-f", "lavfi", "-t", f"{dur:.4f}", "-i", "anullsrc=r=44100:cl=stereo"])
    sv._run(inputs + audio + [
        "-map", "0:v", "-map", "1:a", "-vf", vf, "-frames:v", str(n), "-af", f"apad,atrim=0:{dur:.4f}",
        "-c:v", "libx264", "-preset", "fast", "-bf", "0", "-crf", "17", "-maxrate", f"{kbps}k",
        "-bufsize", f"{kbps}k", "-pix_fmt", "yuv420p", "-r", str(FPS)] + PREVIEW_AUDIO
        + ["-movflags", "+faststart", out_path])
    if src and sc.get("video"):
        sv._discard_download(sc["video"], src)
    elif src:
        sv._discard_download(sc["image"], src)
    return out_path


def assemble(preview_paths, song_path, out_path):
    """Join the approved previews exactly as they are (no re-encode) and lay the whole song under them,
    so there are no seams in the music."""
    listing = store.tmp_path("previews.txt")
    with open(listing, "w") as f:
        f.writelines(f"file '{p}'\n" for p in preview_paths)
    if song_path:
        sv._run(["-f", "concat", "-safe", "0", "-i", listing, "-i", song_path, "-map", "0:v", "-map", "1:a",
                 "-c:v", "copy"] + PREVIEW_AUDIO + ["-shortest", "-movflags", "+faststart", out_path])
    else:
        sv._run(["-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", "-movflags", "+faststart", out_path])
    os.remove(listing)
    return out_path
