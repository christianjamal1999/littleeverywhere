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


REUSE_DURATION_SLACK = 0.2  # seconds a repeated chorus shot may differ in length from the first one
REUSE_WORD_SLACK = 0.15  # and each word may land this far from where it did the first time


def mark_reuse(plan, lines, enabled=True):
    """A chorus shot whose words fall at the same moments as the same shot of an earlier chorus reuses
    that shot's keyframe and animation (scene["reuse"] = its number): the chorus is paid for once and the
    lips still match, because the song repeats on the same beat grid."""
    timed = [(w, t[0]) for ln in lines if ln["heard"] for w, t in zip(ln["words"], ln["times"])]
    firsts = []
    for sc in plan["scenes"]:
        sc.pop("reuse", None)
        words = [(_plain(w), at - sc["start"]) for w, at in timed if sc["start"] <= at < sc["end"]]
        if not enabled or not words or not str(sc.get("section", "")).lower().startswith("chorus"):
            continue
        dur, cast = sc["end"] - sc["start"], sorted(sc.get("cast", []))
        for n, fwords, fdur, fcast in firsts:
            if (fcast == cast and abs(fdur - dur) <= REUSE_DURATION_SLACK and len(fwords) == len(words)
                    and all(a[0] == b[0] and abs(a[1] - b[1]) <= REUSE_WORD_SLACK for a, b in zip(fwords, words))):
                sc["reuse"] = n
                break
        else:
            firsts.append((sc["n"], words, dur, cast))


def media_scene(plan, sc):
    """The scene as it should be rendered: a reused chorus shot shows its original's keyframe and clip."""
    if not sc.get("reuse"):
        return sc
    src = next((x for x in plan["scenes"] if x["n"] == sc["reuse"]), None)
    return {**sc, "image": (src or {}).get("image"), "video": (src or {}).get("video")}


def unheard_share(lines):
    return sum(not ln["heard"] for ln in lines) / max(1, len(lines))


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


def card_of(sc):
    return str((sc.get("direction") or {}).get("card") or "").strip()[:40]


def preview_key(sc, tkey, captions, size, kbps):
    return "|".join(str(x) for x in (sc.get("video") or sc.get("image") or "none", *frames(sc), tkey, captions,
                                     size, kbps, card_of(sc), 3))


CARD_POP = r"{\fad(150,200)\t(0,160,\fscx112\fscy112)\t(160,320,\fscx100\fscy100)}"


def write_captions(lines, w, h, path, scenes=()):
    """The sing-along lines (if any) plus each scene's learning card, a big word popped up top by code."""
    out = sv._ass_header(w, h)
    size = round(h * 0.13)
    at = out.index("[Events]") - 1
    out.insert(at, f"Style: Card,Fredoka,{size},&H00FFFFFF,&H00FFFFFF,&H00B4469A,&H64000000,-1,0,0,0,"
                   f"100,100,2,0,1,{max(5, size // 8)},{max(3, size // 12)},8,{w // 12},{w // 12},{round(h * 0.07)},1")
    out += sv._timed_caption_events([ln for ln in lines if ln["heard"]], sv.POP)
    for sc in scenes:
        if card_of(sc):
            start, end = sc["start"] + 0.3, max(sc["start"] + 1.2, sc["end"] - 0.2)
            out.append(f"Dialogue: 1,{sv._ass_time(start)},{sv._ass_time(end)},Card,,0,0,0,,{CARD_POP}"
                       + sv._ass_text(card_of(sc)))
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


# ============================================================== a vertical Short from the first chorus

SHORT_MAX_SECONDS = 58


def short_scenes(plan):
    """The first chorus (the hook), as the run of scenes to put in the Short."""
    scenes, run = plan["scenes"], []
    for sc in scenes:
        if str(sc.get("section", "")).lower().startswith("chorus"):
            run.append(sc)
        elif run:
            break
    if not run:
        run = [sc for sc in scenes if sc.get("sung")][:6] or scenes[:6]
    while len(run) > 1 and run[-1]["end"] - run[0]["start"] > SHORT_MAX_SECONDS:
        run.pop()
    return run


def build_short(scenes, song_path, out_path):
    """9:16: the middle of each widescreen clip, large, over a blurred fill of the same clip, with the song."""
    parts = []
    for sc in scenes:
        f0, f1 = frames(sc)
        n = max(1, f1 - f0)
        src = store.download_to(sc["video"], store.tmp_path("shortclip.mp4"))
        seg = store.tmp_path(f"short{sc['n']}.mp4")
        graph = (f"[0:v]fps={FPS},tpad=stop_mode=clone:stop_duration={n / FPS:.3f},split[a][b];"
                 "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2,eq=brightness=-0.06[bg];"
                 "[b]crop=ih*1.2:ih,scale=1080:-2[fg];[bg][fg]overlay=0:(H-h)/2,setsar=1[v]")
        sv._run(["-i", src, "-filter_complex", graph, "-map", "[v]", "-frames:v", str(n), "-an", "-c:v", "libx264",
                 "-preset", "fast", "-bf", "0", "-crf", "20", "-maxrate", "6000k", "-bufsize", "6000k",
                 "-pix_fmt", "yuv420p", "-r", str(FPS), seg])
        sv._discard_download(sc["video"], src)
        parts.append(seg)
    start, dur = frames(scenes[0])[0] / FPS, (frames(scenes[-1])[1] - frames(scenes[0])[0]) / FPS
    listing = store.tmp_path("short.txt")
    with open(listing, "w") as f:
        f.writelines(f"file '{p}'\n" for p in parts)
    sv._run(["-f", "concat", "-safe", "0", "-i", listing, "-ss", f"{start:.4f}", "-t", f"{dur:.4f}", "-i", song_path,
             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-af", f"afade=t=out:st={max(0, dur - 0.6):.2f}:d=0.6"]
            + PREVIEW_AUDIO + ["-shortest", "-movflags", "+faststart", out_path])
    for path in parts + [listing]:
        os.remove(path)
    return out_path


def frame_grabs(video_path, seconds, count=3, width=768):
    """A few JPEG frames spread across a clip, for the automatic check."""
    out = []
    for i in range(count):
        at = seconds * (i + 0.5) / count
        path = store.tmp_path(f"grab{i}.jpg")
        sv._run(["-ss", f"{at:.2f}", "-i", video_path, "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", path])
        with open(path, "rb") as f:
            out.append(f.read())
        os.remove(path)
    return out


# ============================================================== how far along a video is

def progress(p):
    """0-100 for the whole job of making this video, weighted by how long each part takes."""
    plan, song = p.get("plan") or {}, p.get("song") or {}
    scenes = plan.get("scenes", [])
    own = [sc for sc in scenes if not sc.get("reuse")]

    def share(items, test):
        return sum(bool(test(x)) for x in items) / len(items) if items else 0.0

    locs = (plan.get("direction") or {}).get("locations", [])
    done = (5 * bool(plan) + 5 * bool(song) + 2 * bool(song.get("timing")) + 4 * bool(plan.get("choreography"))
            + 6 * bool(plan.get("direction")) + 3 * share(locs, lambda x: x.get("image"))
            + 15 * share(own, lambda x: x.get("image")) + 40 * share(own, lambda x: x.get("video"))
            + 5 * share(scenes, lambda x: x.get("preview"))
            + 8 * share(scenes, lambda x: (x.get("review") or {}).get("status") == "approved")
            + 2 * bool(p.get("thumbnail")) + 4 * bool(p.get("final")) + 1 * bool(p.get("short")))
    return int(min(100, round(done)))
