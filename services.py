"""Clients for Claude (songwriting + storyboards), Higgsfield (images + video),
ElevenLabs (music) and ffmpeg (slicing + final render)."""
import base64
import json
import math
import os
import re
import subprocess
import threading
import time

import anthropic
import imageio_ffmpeg
import requests

import store


def _ffmpeg_exe():
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    if os.access(exe, os.X_OK):
        return exe
    # Serverless bundles can drop the execute bit; run a copy from /tmp instead.
    import shutil
    copy = "/tmp/ffmpeg-bin"
    if not os.path.exists(copy):
        shutil.copy(exe, copy)
        os.chmod(copy, 0o755)
    return copy


FFMPEG = _ffmpeg_exe()
FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
HF_BASE = "https://api.higgsfield.ai"


class UserError(Exception):
    """An error worth showing to the user as-is (missing key, bad input, API refusal)."""


def need(settings, *keys):
    labels = {
        "anthropic_api_key": "Anthropic API key",
        "hf_key_id": "Higgsfield key ID",
        "hf_key_secret": "Higgsfield key secret",
        "elevenlabs_api_key": "ElevenLabs API key",
    }
    missing = [labels.get(k, k) for k in keys if not settings.get(k)]
    if missing:
        raise UserError("Add your " + " and ".join(missing) + " in Settings first.")


# ============================================================== look and rules for characters

HOUSE_STYLE = ("Hyperrealistic photograph, shot on a professional cinema camera with an 85mm lens: real children "
               "with natural skin texture and pores, individual hair strands, wet catchlights in the eyes, real "
               "fabric weave and stitching, bright natural sunlight, true-to-life colors, shallow depth of field, "
               "clean uncluttered composition. Never illustrated, animated, CGI or doll-like")

CHARACTER_GUIDELINES = """\
ALLOWED
- Original children aged 3-6 with natural child proportions, or the AI version of a real child whose parent or guardian gave consent in the New Character form.
- Photographic detail: real skin texture, individual hair strands, real fabric weave and stitching.
- Anatomically correct faces: two matching eyes with round pupils and natural catchlights, natural eyebrows, a child's small nose, two matching ears, and a child's small even teeth in the correct number.
- Genuine big smiles and laughter.
- Modest everyday children's clothes that fully cover the body, one signature outfit per character; colorful and cultural prints shown respectfully.
- Simple accessories: hair clips, bows, tiny stud earrings. Wheelchairs and hearing aids shown naturally.
- Playing, dancing, singing, toy instruments, friendly side-by-side poses.
NOT ALLOWED
- Cartoon or toy look: oversized eyes, plastic skin, outlines, exaggerated proportions.
- Adult styling on children: makeup, glossy lips, long false-looking lashes, glamour or posed-model looks.
- Swimwear, underwear, sleepwear, bare midriffs or revealing clothes, even at the beach or pool. Children stay in their full outfits.
- The likeness of any real child without that consent, of any other real person, or another channel's characters.
- Anything scary, violent or unsafe.
- Text, logos, watermarks, extra fingers or distorted faces, fused or too many teeth, mismatched eyes or ears."""


ADULT_GUIDELINES = """\
ADULTS IN THE CAST
- A real adult (a parent, teacher or host) may appear only as the AI version of someone who gave consent in the New
  Character form: themselves, or with their written permission.
- They look like real adults with natural skin, modest everyday clothes that fully cover the body, friendly and warm,
  and they stay the same person with the same face, hair and outfit in every shot."""


def is_adult(c):
    return c.get("kind") == "adult"


STYLE_3D = ("Frame from a big-budget 3D animated family film: stylized, appealing toddler characters with slightly "
            "large expressive eyes, round soft faces, smooth softly shaded skin with a warm rosy glow, sculpted "
            "hair with visible strands, and richly detailed fabric textures; physically based rendering, soft "
            "warm sunlight with gentle rim light, glowing saturated candy colors, shallow depth of field with "
            "creamy bokeh, crisp focus on the eyes. Always this same 3D animated look; never a photograph of "
            "real children, never flat 2D cartoon, never anime")

STYLE_LIFELIKE = ("Still from a premium family film where the children look almost real: lifelike toddlers with true "
                  "human proportions (a normal-sized head on a real child's body, natural-sized eyes, real little "
                  "hands), real skin with soft natural texture and rosy cheeks, fine individual hair strands and "
                  "flyaways, real clothes with visible weave, stitching, buttons and creases. Gently idealized, like "
                  "the most adorable real kids on a perfect day. Bright warm sunlight with a soft rim light, vivid "
                  "saturated colors, shallow depth of field with creamy bokeh, tack-sharp eyes with wet catchlights. "
                  "Never a cartoon: no oversized heads, no giant eyes, no bobblehead or toy bodies, no smooth "
                  "plastic or clay skin, no painted or illustrated look")

GUIDELINES_LIFELIKE = """\
ALLOWED
- Original lifelike children aged 3-6 with true child proportions, or the lifelike version of a real child whose parent or guardian gave consent in the New Character form.
- One signature item per character (a hat, hair clip, headband, scarf) that they wear in every video, and one signature outfit; a holiday costume may replace the outfit but always keeps the signature item, hair and face.
- Real detail: skin texture, individual hair strands, real fabric weave and stitching.
- Anatomically correct faces: two matching natural-sized eyes with round pupils and catchlights, natural eyebrows, a child's small nose, two matching ears, small even baby teeth in the correct number.
- Genuine big smiles and laughter.
- Modest everyday children's clothes that fully cover the body; colorful and cultural prints shown respectfully.
- Wheelchairs and hearing aids shown naturally. Playing, dancing, singing, toy instruments, friendly animals.
NOT ALLOWED
- Cartoon or toy look: oversized heads or eyes, plastic skin, outlines, exaggerated proportions.
- The likeness of any real child without that consent, of any other real person, or another channel's or film's characters.
- Adult styling on children: makeup, glossy lips, glamour or posed-model looks.
- Swimwear, underwear, sleepwear, bare midriffs or revealing clothes, even at the beach or pool.
- Anything scary, violent or unsafe.
- Text, logos, watermarks, extra fingers, distorted faces, fused or too many teeth, mismatched eyes or ears."""

GUIDELINES_3D = """\
ALLOWED
- Original stylized 3D animated children aged 3-6 with cute toddler proportions, or the 3D animated version of a real child whose parent or guardian gave consent in the New Character form.
- One signature item per character (a hat, hair clip, headband, scarf) that they wear in every video, and one signature outfit; a holiday costume may replace the outfit but always keeps the signature item, hair and face.
- Correct, appealing faces: two matching eyes with catchlights, natural eyebrows, a small nose, two matching ears, small even baby teeth.
- Genuine big smiles and laughter, exaggerated fun expressions.
- Modest everyday children's clothes that fully cover the body; colorful and cultural prints shown respectfully.
- Wheelchairs and hearing aids shown naturally. Playing, dancing, singing, toy instruments, friendly animals.
NOT ALLOWED
- Photorealistic real-looking children, or the likeness of any real child without that consent, of any other real person, or another channel's or film's characters.
- Adult styling on children: makeup, glossy lips, glamour or posed-model looks.
- Swimwear, underwear, sleepwear, bare midriffs or revealing clothes, even at the beach or pool.
- Anything scary, violent or unsafe.
- Text, logos, watermarks, extra fingers, distorted faces, fused or too many teeth, mismatched eyes or ears."""

LOOKS = {"lifelike": "Lifelike: almost-real children with film lighting, like the top kids' music channels",
         "cartoon": "3D cartoon: big heads and big eyes", "photo": "Plain photograph",
         "custom": "Custom (the two boxes below)"}
STYLES = {"lifelike": (STYLE_LIFELIKE, GUIDELINES_LIFELIKE), "cartoon": (STYLE_3D, GUIDELINES_3D),
          "photo": (HOUSE_STYLE, CHARACTER_GUIDELINES)}


def look_of(settings):
    look = settings.get("look")
    return look if look in LOOKS else "lifelike"  # also what the earlier "3d" setting becomes


def house_style(settings):
    look = look_of(settings)
    if look == "custom":
        return settings.get("house_style") or HOUSE_STYLE
    return STYLES[look][0]


def guidelines(settings):
    look = look_of(settings)
    if look == "custom":
        return settings.get("character_guidelines") or CHARACTER_GUIDELINES
    return STYLES[look][1]


def style_prefix(settings, cast=()):
    """What every generated image of the cast must follow."""
    rules = guidelines(settings)
    if any(is_adult(c) for c in cast):
        rules += "\n" + ADULT_GUIDELINES
    return f"{house_style(settings)}.\n{rules}\nSCENE: "


# ============================================================== spend tracking
# Every paid call leaves one event in usage_events; the Dashboard adds them up. Logging must never
# break the work it describes, so failures here are only printed.

# USD per million tokens: input, output, cache read (platform.claude.com/docs/en/about-claude/pricing).
CLAUDE_PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches
MUSIC_USD_PER_MINUTE = 0.15  # elevenlabs.io/pricing/api, same on every plan
TRANSCRIBE_USD_PER_HOUR = 0.22  # Scribe v2, same page
SPEECH_USD_PER_1K_CHARACTERS = 0.04  # Flash, same page

_spend = threading.local()


def set_spend_project(project_id):
    """The video this request's spending belongs to."""
    _spend.project = project_id


def current_spend_project():
    return getattr(_spend, "project", None)


def log_spend(service, what, usd, detail="", project=None, event_id=None):
    try:
        store.save_doc("usage_events", {
            "id": event_id or store.new_id("use"), "created": store.now(), "ts": time.time(),
            "service": service, "what": what, "usd": usd, "detail": detail,
            "project": project or getattr(_spend, "project", None)})
    except Exception as e:  # noqa: BLE001
        print("Couldn't log spending:", e)


def _log_claude(settings, what, msg):
    u = msg.usage
    model = getattr(msg, "model", "") or ""
    price = CLAUDE_PRICES.get(model) or CLAUDE_PRICES.get(settings.get("claude_model"))
    tokens_in = u.input_tokens or 0
    tokens_out = u.output_tokens or 0
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
    searches = getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
    usd = None
    if price:
        usd = (tokens_in * price[0] + tokens_out * price[1] + cache_read * price[2]
               + cache_write * price[0] * 1.25) / 1e6 + searches * WEB_SEARCH_USD
    detail = f"{model or settings.get('claude_model')}: {tokens_in + cache_read + cache_write:,} in, {tokens_out:,} out"
    if searches:
        detail += f", {searches} web searches"
    log_spend("Anthropic", what, usd, detail)


# ============================================================== Claude

CHANNEL_PLAYBOOK = """\
You write for a YouTube kids' music channel aimed at ages 2-6 and their parents.
What wins in this niche right now (from studying channels like Music Travel Kids, whose
AI-animated relaunch took songs from zero to millions of views within weeks):
- A recurring cast of 3-5 relatable, diverse kid characters who appear in every video.
- Dance-along / move-along songs with explicit actions ("wiggle like a monkey", "stomp, stomp").
- Seasonal and holiday tie-ins published 2-4 weeks early (Halloween, winter, spring, summer).
- Social-emotional lessons (sharing, kindness, feelings), plus classic early-learning topics
  (colors, counting, ABCs, animals, shapes) and nature/outdoor/travel settings.
- Bright, saturated, sunny visuals; big expressive faces; lots of animals.
- Songs of 2-3.5 minutes, simple repetitive chorus, call-and-response, a clear hook in the
  first 5 seconds, and a chorus that repeats at least 3 times.
Always write 100% original lyrics and melodies. Never imitate or quote existing songs,
nursery-rhyme parodies of copyrighted works, or other channels' characters.
Keep everything wholesome, safe, inclusive and age-appropriate.
"""

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "youtube_title", "youtube_description", "tags", "music_style",
                 "bpm", "sections", "scenes", "thumbnail_prompt", "thumbnail_text"],
    "properties": {
        "title": {"type": "string"},
        "youtube_title": {"type": "string"},
        "youtube_description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "music_style": {"type": "string"},
        "bpm": {"type": "integer"},
        "sections": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "duration_seconds", "lyrics", "styles"],
            "properties": {
                "name": {"type": "string"},
                "duration_seconds": {"type": "number"},
                "lyrics": {"type": "string"},
                "styles": {"type": "array", "items": {"type": "string"}},
            }}},
        "scenes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["section", "start", "end", "lyric", "visual", "action", "camera", "cast", "singers"],
            "properties": {
                "section": {"type": "string"},
                "start": {"type": "number"},
                "end": {"type": "number"},
                "lyric": {"type": "string"},
                "visual": {"type": "string"},
                "action": {"type": "string"},
                "camera": {"type": "string"},
                "cast": {"type": "array", "items": {"type": "string"}},
                "singers": {"type": "array", "items": {"type": "string"}},
            }}},
        "thumbnail_prompt": {"type": "string"},
        "thumbnail_text": {"type": "string"},
    },
}

IDEAS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["ideas"],
    "properties": {"ideas": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["topic", "lesson", "setting", "mood", "hook", "why"],
        "properties": {k: {"type": "string"} for k in
                       ["topic", "lesson", "setting", "mood", "hook", "why"]},
    }}},
}

LOOK_FIELDS = ["name", "age", "look", "face", "icon", "outfit", "personality", "signature", "mannerisms"]
ICON_GUIDE = """\
icon: the ONE signature item that makes this character recognizable at a glance in every video, worn on or near
the head so it shows in close-ups: e.g. "a crown of big yellow sunflowers", "a green-and-white trucker cap with
a pine tree on the front", "a round pink daisy clip above the left ear". Bold, simple, one or two colors, and
different from every other cast member's item."""
FACE_GUIDE = """\
face: an exact, literal description that makes this one child recognizable, covering in order: face shape;
eyes (color, shape, size, spacing); eyebrows (color, thickness, arch); nose (bridge, tip, nostrils); ears
(size, shape, how far they stick out, any earrings); mouth and lips; teeth when smiling (gaps, missing baby
teeth, size); skin tone and any freckles or marks and exactly where they sit; hair (color, texture, length,
part, style). Plain visual facts only, no comparisons to anyone.
{ICON}
mannerisms: 3-4 quick physical habits that make this child move like a real kid (e.g. "bounces on her toes
when excited, tilts her head left when she sings, scrunches her nose when she giggles").""".replace("{ICON}", ICON_GUIDE)
BIBLE_FIELDS = ["voice", "likes", "friends", "growth"]
CHARACTER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": LOOK_FIELDS + BIBLE_FIELDS,
    "properties": {k: {"type": "string"} for k in LOOK_FIELDS + BIBLE_FIELDS},
}
BIBLE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": BIBLE_FIELDS,
    "properties": {k: {"type": "string"} for k in BIBLE_FIELDS},
}
BIBLE_GUIDE = """\
voice: how they talk and sing - pace, favorite words, sounds they make (e.g. "says 'Woo-hoo!', sings high and giggly").
likes: 3-5 favorite things (animals, foods, colors, activities) that can show up in songs.
friends: how they relate to each other cast member by name (who they team up with, playful rivalries).
growth: one small, relatable thing they're learning (sharing, being brave in the dark, waiting their turn)."""


def _claude(settings, what="Claude request", **request):
    """One streamed Claude call with refusal fallbacks, adaptive thinking and friendly errors."""
    need(settings, "anthropic_api_key")
    client = anthropic.Anthropic(api_key=settings["anthropic_api_key"])
    try:
        with client.beta.messages.stream(
            model=settings.get("claude_model") or "claude-opus-5-5",
            max_tokens=32000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            **request,
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise UserError("Anthropic rejected the API key. Check it in Settings.")
    except anthropic.RateLimitError:
        raise UserError("Anthropic rate limit hit. Wait a minute and try again.")
    except anthropic.APIStatusError as e:
        raise UserError(f"Anthropic API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise UserError("Couldn't reach the Anthropic API. Check your connection.")
    _log_claude(settings, what, msg)
    if msg.stop_reason == "refusal":
        raise UserError("Claude declined this request. Try rephrasing it.")
    if msg.stop_reason == "max_tokens":
        raise UserError("Claude ran out of room. Try asking for less at once.")
    return msg


def claude_json(settings, system, prompt, schema, what="Claude request"):
    msg = _claude(
        settings,
        what=what,
        output_config={
            "effort": settings.get("claude_effort") or "high",
            "format": {"type": "json_schema", "schema": schema},
        },
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in msg.content if b.type == "text")
    return json.loads(text)


def wearing(c):
    icon = f"always wearing their signature {c['icon'].rstrip('. ')}; " if c.get("icon") else ""
    return f"{icon}wearing {c.get('costume') or c.get('outfit', '')}"


def character_line(c):
    who = "an adult, " if is_adult(c) else ""
    return f"{c['name']} ({who}age {c.get('age') or '?'}): {c.get('look', '')}; {wearing(c)}"


def dressed(cast, plan):
    """The cast as they appear in this video: a holiday costume from the creative director replaces the outfit."""
    costumes = {x["name"]: x["costume"] for x in ((plan or {}).get("direction") or {}).get("costumes", [])
                if x.get("costume")}
    return [{**c, "costume": costumes[c["name"]]} if c["name"] in costumes else c for c in cast]


def find_icon(settings, c, others):
    """A signature item for a character that doesn't have one yet."""
    taken = "\n".join(f"- {o['name']}: {o['icon']}" for o in others if o.get("icon")) or "- none yet"
    prompt = f"""Give this recurring cast member a signature item.
{c['name']}: {c.get('look', '')} Outfit: {c.get('outfit', '')} Personality: {c.get('personality', '')}
Items already taken by the rest of the cast:
{taken}
{ICON_GUIDE}
If their look or outfit already includes such an item (hair bows, a cap, a headband), use that."""
    schema = {"type": "object", "additionalProperties": False, "required": ["icon"],
              "properties": {"icon": {"type": "string"}}}
    return claude_json(settings, CHANNEL_PLAYBOOK, prompt, schema, "Signature item")["icon"].strip()


def hair_of(c):
    """The sentences of a character's description about their hair, which video models drift on most."""
    text = f"{c.get('face', '')} {c.get('look', '')}"
    found = [x.strip() for x in re.split(r"(?<=[.;])\s+", text) if re.search(r"\bhair|braid|bang|fringe|curl|ponytail|bun\b", x, re.I)]
    return " ".join(dict.fromkeys(found))


def head_count(cast):
    """Exactly who is in the shot. Video models invent extra people to fill space or match extra voices."""
    if not cast:
        return "There are no people in this shot."
    names = " and ".join(c["name"] for c in cast)
    n = len(cast)
    return (f"Exactly {n} {'person' if n == 1 else 'people'} in the whole shot: {names}. Nobody else is in it, nobody "
            f"enters the frame, and no one appears in the background, in mirrors, doorways or windows.")


def without_absent(text, cast, everyone):
    """Drop sentences that name cast members who aren't in this shot, so models don't add them."""
    absent = [c["name"] for c in everyone if c["id"] not in {x["id"] for x in cast}]
    if not absent or not text:
        return text
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in absent) + r")\b", re.I)
    parts = re.split(r"(?<=[.;!?])\s+", text)
    return " ".join(x for x in parts if not pattern.search(x))


def identity_lock(cast):
    """The per-character facial facts that must match in every image."""
    lines = [f"{c['name']}: {c.get('face') or c.get('look', '')}; {wearing(c)}" for c in cast]
    if not lines:
        return ""
    return ("IDENTITY LOCK, the most important instruction: each person must be unmistakably the same real person as "
            "in their reference images, pixel-faithful in eye color and shape, eyebrows, nose, ears, lips, teeth, "
            "skin tone, freckles, hair and outfit. Do not beautify, age or restyle them.\n" + "\n".join(lines) + "\n"
            + head_count(cast) + "\n")


def character_bible(c):
    parts = [f"personality: {c.get('personality', '')}", f"signature: {c.get('signature', '')}"]
    parts += [f"{k}: {c[k]}" for k in BIBLE_FIELDS if c.get(k)]
    return " | ".join(parts)


def write_song_plan(settings, brief, cast):
    length = int(brief.get("length") or 150)
    roster = "\n".join(
        f"- id={c['id']} | {character_line(c)} | {character_bible(c)}" for c in cast
    ) or "- (no recurring characters selected; invent unnamed background kids only)"
    prompt = f"""Create a complete original kids' song and shot-by-shot storyboard.

Channel: {settings.get('channel_name') or 'our channel'}
Topic: {brief.get('topic')}
Learning goal / lesson: {brief.get('lesson') or 'fun + movement'}
Setting: {brief.get('setting') or 'your choice, outdoors and colorful'}
Mood / genre: {brief.get('mood') or 'upbeat, bouncy pop'}
Target length: about {length} seconds
Extra notes: {brief.get('notes') or 'none'}

Recurring cast available (use their exact ids in scene.cast; feature everyone across the video):
{roster}

Keep the cast in character, using their story bible: work in signature moves and catchphrases
(as call-and-response where it fits), use their likes as props or verses, show their friendships
on screen, and where it suits the topic give one character a small moment tied to their growth.

Requirements:
- sections: in song order (Intro, Verse 1, Chorus, Verse 2, Chorus, Bridge, Chorus, Outro...).
  duration_seconds per section between 6 and 40, summing to the target length.
  lyrics: the sung lines, one per line, simple words a 3-year-old can follow; empty for an
  instrumental intro. styles: 2-4 short music-production descriptors for that section.
- music_style: one line describing genre, instruments, vocals (e.g. "cheerful kids pop, ukulele,
  hand claps, bright female lead vocal with kids choir on chorus").
- scenes: contiguous shots covering 0 to the full length with no gaps (scene.start of each =
  previous scene.end). Each scene 4-5 seconds, never longer (animation is billed in 5-second
  clips, so longer scenes waste money); use more scenes instead. lyric = the line(s) sung during it.
  visual: one vivid sentence describing the shot (place, lighting, animals, props) - name the
  characters but don't re-describe their appearance. action: what the characters physically do
  and it must be specific, quick and on the beat, moving the way real kids do: named moves with body parts,
  like "two fast knee bounces, claps above her head, spins once and points at the camera", never just "dances".
  Use each character's mannerisms. camera: a simple camera move. cast: ids of characters in shot.
  singers: ids of the characters singing the words in that shot (empty for instrumental shots).
  Every chorus is cut into the same shots in the same order with the same lyric split, cast and singers,
  because the chorus is animated once and reused each time it comes back.
  About half the shots are a single character alone, which is what a close-up needs. A shot with no cast
  at all (an animal, hands clapping, feet stomping, a prop) is a welcome cutaway once or twice per verse;
  give it no singers.
  Every shot with words is a medium close-up of its 1-2 singers facing the camera with their mouths clearly
  visible, because lip-sync only works on faces that are large and turned toward the viewer.
  Most shots should show just 1-3 characters, which animate far more cleanly. Use the full cast only
  for wide chorus shots, where everyone dances on their own spot without crossing paths.
- youtube_title: under 70 characters, emoji ok, includes a search keyword like "Kids Songs".
  youtube_description: 3 short paragraphs plus hashtags. tags: 12-20 search tags.
- thumbnail_prompt: one bright, high-contrast 16:9 scene with the cast's big happy (or funny)
  facial expressions. thumbnail_text: 1-3 punchy words to overlay.
"""
    plan = claude_json(settings, CHANNEL_PLAYBOOK, prompt, PLAN_SCHEMA, "Song and storyboard")
    valid = {c["id"] for c in cast}
    for i, s in enumerate(plan["scenes"]):
        s["n"] = i + 1
        s["cast"] = [cid for cid in s["cast"] if cid in valid]
        s["singers"] = [cid for cid in s.get("singers", []) if cid in s["cast"]]
    return plan


MAX_SEARCH_CONTINUATIONS = 3


def research_trends(settings, focus):
    """Live web research on what's working in kids' music right now.
    Returns {"brief": markdown text, "sources": [{"title", "url"}]}."""
    model = settings.get("claude_model") or "claude-opus-5-5"
    # Dynamic-filtering web search needs an Opus/Sonnet 4.6+ model; others get the basic tool.
    search_type = ("web_search_20260209" if model.startswith(("claude-opus", "claude-sonnet"))
                   else "web_search_20250305")
    prompt = f"""Research what's working in YouTube kids' music (ages 2-6) right now. Today is
{__import__('datetime').date.today().isoformat()}.
Look for: the newest uploads and their view counts from channels like Music Travel Kids
(@musictravelkids), Cocomelon, Super Simple Songs, Bebefinn, Pinkfong, Little Angel and Bounce
Patrol; kids' songs and dances trending on YouTube Shorts and TikTok; holidays and seasonal
moments in the next 8 weeks; and any YouTube policy news affecting AI-made or made-for-kids videos.
Creator's focus: {focus or 'none'}.

Write a tight brief (under 300 words) as markdown bullets under three headings:
**Trending now**, **Gaps to own**, **Timing**. Name specific videos, view counts and dates where you
found them. If a search turns up nothing useful, say so rather than guessing."""
    messages = [{"role": "user", "content": prompt}]
    assistant, text, sources = [], [], {}
    for _ in range(MAX_SEARCH_CONTINUATIONS + 1):
        msg = _claude(
            settings,
            what="Trend research",
            output_config={"effort": "medium"},
            system=CHANNEL_PLAYBOOK,
            tools=[{"type": search_type, "name": "web_search", "max_uses": 8}],
            messages=messages + ([{"role": "assistant", "content": assistant}] if assistant else []),
        )
        for block in msg.content:
            if block.type == "text":
                text.append(block.text)
            elif block.type == "web_search_tool_result" and isinstance(block.content, list):
                for r in block.content:
                    sources.setdefault(r.url, {"title": r.title, "url": r.url})
        if msg.stop_reason != "pause_turn":
            break
        # Server-side search loop hit its iteration cap: resend the turn so far and it resumes.
        assistant += [b.model_dump(exclude_none=True) for b in msg.content]
    brief = "".join(text).strip()
    if not brief:
        raise UserError("The trend research came back empty. Try again in a minute.")
    return {"brief": brief, "sources": list(sources.values())[:12]}


def suggest_ideas(settings, focus, research_brief=""):
    research = (f"\nLive research from the web, gathered just now (use it; prefer ideas it "
                f"supports):\n{research_brief}\n" if research_brief else "")
    prompt = f"""Give 8 fresh video ideas for the channel "{settings.get('channel_name')}".
Focus or constraints from the creator: {focus or 'none - mix evergreen and seasonal'}.
Today is {__import__('datetime').date.today().isoformat()}; include 2-3 seasonal ideas for the
next 4-8 weeks. hook = the chorus hook line. why = one sentence on why it should perform.
{research}"""
    return claude_json(settings, CHANNEL_PLAYBOOK, prompt, IDEAS_SCHEMA, "Video ideas")["ideas"]


BRIEF_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["topic", "lesson", "setting", "mood", "notes", "why"],
    "properties": {k: {"type": "string"} for k in ["topic", "lesson", "setting", "mood", "notes", "why"]},
}
# What earns the views on the channels this one is modelled on, most reliable first.
WINNING_FORMATS = [
    "a dance-along where every line names a move to copy (wiggle, stomp, freeze, spin)",
    "a dance-along built around animals and how each one moves or sounds",
    "a feelings or kindness song (sharing, taking turns, saying sorry, being brave, big feelings)",
    "a good-habits song (brushing teeth, tidying up, washing hands, bedtime, trying new foods)",
    "a counting, colors, shapes or ABC song where the learning is acted out with the whole body",
    "a farm, zoo, ocean or jungle animal song with sounds to shout back",
    "a vehicles or things-that-go song with actions (trains, diggers, rockets, buses)",
    "a seasonal or holiday dance song for a holiday 3 to 6 weeks from today",
]


def suggest_brief(settings, done, cast, avoid=()):
    """One ready-to-make video idea the channel hasn't done. done: titles and topics already made."""
    import datetime
    import random
    made = "\n".join(f"- {t}" for t in list(done) + list(avoid)) or "- nothing yet"
    names = ", ".join(f"{c['name']} ({c.get('personality', '')[:60]})" for c in cast) or "no recurring cast yet"
    prompt = f"""Pick the next video for the channel "{settings.get('channel_name')}" and fill in its brief.
Today is {datetime.date.today().isoformat()}.
Format to use this time: {random.choice(WINNING_FORMATS)}.
Already made or already suggested (choose a clearly different subject, hook and setting from all of these):
{made}
Cast: {names}

topic: the song's subject as a short working title, like "Stomp Like a Dinosaur".
lesson: what kids learn or practice, in a few words.
setting: where it happens; bright, colorful and different from the videos already made.
mood: the genre and energy, like "bouncy dance pop with claps, 115 BPM".
notes: for the songwriter: the chorus hook line, 3-4 named actions kids copy, and which cast member leads.
why: one sentence on why this should perform, for the creator."""
    return claude_json(settings, CHANNEL_PLAYBOOK, prompt, BRIEF_SCHEMA, "Video idea")


# ============================================================== connection tests

def test_anthropic(settings):
    need(settings, "anthropic_api_key")
    model = settings.get("claude_model") or "claude-opus-5-5"
    try:
        info = anthropic.Anthropic(api_key=settings["anthropic_api_key"]).models.retrieve(model)
    except anthropic.AuthenticationError:
        raise UserError("Anthropic rejected this key.")
    except anthropic.PermissionDeniedError:
        raise UserError("This key isn't allowed to use the Anthropic API.")
    except anthropic.NotFoundError:
        raise UserError(f"The key works, but {model} isn't available to it. Pick another model.")
    except anthropic.APIConnectionError:
        raise UserError("Couldn't reach the Anthropic API.")
    return f"Key works. {info.display_name} is available."


HF_PROBE_URL = f"{HF_BASE}/requests/00000000-0000-0000-0000-000000000000/status"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _hf_probe(headers):
    """Response to a cheap authenticated call (a request ID that doesn't exist)."""
    try:
        return requests.get(HF_PROBE_URL, headers=headers, timeout=30)
    except requests.RequestException:
        raise UserError("Couldn't reach Higgsfield.")


def _describe_credential(label, value):
    """Shape of a pasted credential, never its content."""
    issues = []
    if value != value.strip():
        issues.append("has spaces or line breaks at the ends")
    if any(c in value for c in "\"'`"):
        issues.append("contains quote marks")
    if " " in value.strip():
        issues.append("contains spaces")
    return f"{label}: {len(value.strip())} characters" + (f" ({', '.join(issues)})" if issues else "")


def test_higgsfield(settings):
    # There's no balance endpoint; asking for a request that doesn't exist checks the key cheaply.
    r = _hf_probe(_hf_headers(settings))
    # Per Higgsfield's docs: 401 = bad credentials, 403 = insufficient balance, 404 = fine here.
    if r.status_code in (402, 403):
        raise UserError("Your key is valid, but your Higgsfield balance is too low to generate "
                        "anything. Add funds at console.higgsfield.ai, then test again.")
    if r.status_code >= 500:
        raise UserError(f"Higgsfield is having trouble right now ({r.status_code}).")
    if r.status_code != 401:
        return "Key works. Check your balance at console.higgsfield.ai."

    # Rejected: work out why, so the message says what to fix.
    key_id, secret = _hf_credentials(settings)
    # Higgsfield key IDs are UUIDs (its API validates them as such).
    if not UUID_RE.match(key_id):
        if UUID_RE.match(secret):
            raise UserError("Your key ID and secret are in each other's fields (the key ID is the "
                            "one shaped like 1b4e28ba-2fa1-11d2-883f-a1b2c3d4e5f6). Swap them, "
                            "save, and test again.")
        raise UserError(
            f"That key ID isn't a Higgsfield API key ID. Real ones are 36 characters, shaped like "
            f"1b4e28ba-2fa1-11d2-883f-a1b2c3d4e5f6; yours is {len(key_id)} characters. API "
            "credentials come from console.higgsfield.ai (a separate pay-as-you-go account), not "
            "the higgsfield.ai app or subscription. Create a key there and copy its key ID and "
            "secret, which is shown only once.")
    accepted = lambda resp: resp.status_code not in (401, 422)  # 422 = malformed credentials
    if accepted(_hf_probe({"hf-api-key": key_id, "hf-secret": secret})):
        raise UserError("Higgsfield accepts this key only through its older login style. Tell "
                        "Claude so the app can switch to it.")
    try:
        detail = r.json().get("detail") or r.text[:120]
    except ValueError:
        detail = r.text[:120]
    raw_id = settings.get("hf_key_id") or ""
    raw_secret = settings.get("hf_key_secret") or ""
    raise UserError(
        f"Higgsfield rejected this key ID / secret (it said: \"{detail}\"). What the app has: "
        f"{_describe_credential('key ID', raw_id)}; {_describe_credential('secret', raw_secret)}. "
        "The key ID is the right shape, so the secret doesn't match it, or the key was deleted. "
        "In console.higgsfield.ai, create a new key and copy both values right away (the secret "
        "is shown only once).")


def test_elevenlabs(settings):
    need(settings, "elevenlabs_api_key")
    try:
        r = requests.get("https://api.elevenlabs.io/v1/user/subscription",
                         headers={"xi-api-key": settings["elevenlabs_api_key"]}, timeout=30)
    except requests.RequestException:
        raise UserError("Couldn't reach ElevenLabs.")
    if r.status_code == 401 and "missing_permissions" in r.text:
        return "Key works (it can't read account details, which is fine for music)."
    if r.status_code in (401, 403):
        raise UserError("ElevenLabs rejected this key.")
    if r.status_code >= 400:
        raise UserError(f"ElevenLabs error {r.status_code}.")
    sub = r.json()
    used, limit = sub.get("character_count"), sub.get("character_limit")
    credits = f" {used:,} of {limit:,} credits used this period." if used is not None and limit else ""
    return f"Key works. Plan: {sub.get('tier', 'unknown')}.{credits}"


def test_renderer():
    p = subprocess.run([FFMPEG, "-hide_banner", "-version"], capture_output=True, text=True)
    version = (p.stdout.split("\n")[0].split(" ")[2:3] or ["?"])[0]
    storage = {"supabase": "Supabase", "ephemeral": "temporary (add the Supabase env vars!)",
               "local": "this computer"}[store.storage_kind()]
    if store.USE_SUPABASE:
        try:
            store.check_supabase()
            storage += " (connected)"
        except (RuntimeError, requests.RequestException) as e:
            raise UserError(f"Can't reach Supabase with the configured key: {e}")
    captions = "sing-along captions supported" if captions_supported() else \
        "sing-along captions NOT supported by this ffmpeg build"
    return f"ffmpeg {version}, {captions}. Storage: {storage}."


def _cast_summary(existing):
    return "\n".join(f"- {c['name']}: {c.get('personality', '')}" for c in existing) or "- none yet"


def suggest_character(settings, concept, existing):
    prompt = f"""Design one recurring kid character for the channel's cast.
Concept from the creator: {concept or 'surprise me - complement the existing cast'}
Existing cast (make this one visually and personality-wise distinct):
{_cast_summary(existing)}
look: hair, skin tone, face, build - concrete visual details an image model can follow.
{FACE_GUIDE}
outfit: a signature outfit that stays the same in every video. age: e.g. "5".
signature: a catchphrase or signature dance move.
{BIBLE_GUIDE}

Every look and outfit must follow these rules:
{guidelines(settings)}"""
    return claude_json(settings, CHANNEL_PLAYBOOK, prompt, CHARACTER_SCHEMA, "Character design")


def describe_photo(settings, photos, name, age, kind, existing):
    """Turn photos of a consenting real person into a character sheet. Describes what's visible only.
    photos: [(bytes, content_type)], all of the same person."""
    adult = kind == "adult"
    if adult:
        consent = ("This person has consented (or is the creator themselves) to becoming a recurring adult "
                   "character, such as a parent, teacher or host, on their family channel.")
    else:
        consent = ("A parent or guardian has consented to turning their child into a character for their "
                   "family channel.")
    prompt = f"""{consent} The {len(photos)} photo{'s are all' if len(photos) > 1 else ' is'} of the same
person. Write a character sheet from what you can see, so an image model can recreate exactly this person.
Do not try to work out who they are. Their name is {name}{f" and they are {age}" if age else ""}; use that
name and age exactly.
look: hair, skin tone, build, height for their age. Keep to what's visible.
{FACE_GUIDE}
Be exhaustive about the face: it's the only way the AI version stays recognizably this person. Where the
photos disagree (lighting, angle), describe what's true across them.
outfit: a signature outfit for every video; base it on what they wear here if it's suitable.
personality, signature and the story fields: warm, simple suggestions the creator can edit, not claims
about the person.
{BIBLE_GUIDE}
Existing cast, for friendships:
{_cast_summary(existing)}

The character must follow these rules:
{guidelines(settings)}
{ADULT_GUIDELINES if adult else ""}"""
    images = [{"type": "image", "source": {"type": "base64", "media_type": ctype,
                                           "data": base64.b64encode(data).decode()}} for data, ctype in photos]
    msg = _claude(
        settings, what="Character from photo",
        output_config={"effort": settings.get("claude_effort") or "high",
                       "format": {"type": "json_schema", "schema": CHARACTER_SCHEMA}},
        system=CHANNEL_PLAYBOOK,
        messages=[{"role": "user", "content": images + [{"type": "text", "text": prompt}]}],
    )
    return json.loads(next(b.text for b in msg.content if b.type == "text"))


def flesh_out_bible(settings, c, others):
    """Fill in a character's story bible from what's already known about them."""
    known = "\n".join(f"{k}: {c[k]}" for k in LOOK_FIELDS + BIBLE_FIELDS if c.get(k))
    prompt = f"""Write the story bible for this recurring cast member. Keep anything already
written below and build on it; fill in what's missing.

{known}

Rest of the cast:
{_cast_summary(others)}

{BIBLE_GUIDE}
Keep each field to one or two short sentences."""
    return claude_json(settings, CHANNEL_PLAYBOOK, prompt, BIBLE_SCHEMA, "Story bible")


# ============================================================== Higgsfield

HF_LOW_BALANCE = ("Higgsfield says your balance is too low. The API is pay-as-you-go: add funds "
                  "at console.higgsfield.ai, then try again.")


def _hf_credentials(settings):
    # Strip whitespace and quote marks that tag along when copying from a terminal or .env file.
    key_id = (settings.get("hf_key_id") or "").strip().strip("\"'`").strip()
    secret = (settings.get("hf_key_secret") or "").strip().strip("\"'`").strip()
    # Higgsfield's console also offers the combined "KEY_ID:KEY_SECRET" string; accept it pasted
    # into either field. The part before the colon is the (UUID) key ID.
    if ":" in key_id and (not secret or key_id.endswith(":" + secret)):
        key_id, secret = key_id.split(":", 1)
    elif ":" in secret and UUID_RE.match(secret.split(":", 1)[0].strip()):
        key_id, secret = (part.strip() for part in secret.split(":", 1))
    return key_id, secret


def _hf_headers(settings):
    key_id, secret = _hf_credentials(settings)
    need({"hf_key_id": key_id, "hf_key_secret": secret}, "hf_key_id", "hf_key_secret")
    return {"Authorization": f"Key {key_id}:{secret}", "Content-Type": "application/json"}


def _hf_check(r):
    # Per Higgsfield's docs: 401 = bad credentials, 403 = insufficient balance.
    if r.status_code == 401:
        raise UserError("Higgsfield rejected the key ID / secret. Check them in Settings.")
    if r.status_code in (402, 403):
        raise UserError(HF_LOW_BALANCE)
    if r.status_code >= 400:
        raise UserError(f"Higgsfield error {r.status_code}: {r.text[:400]}")
    return r.json()


def hf_upload(settings, data, content_type):
    """Upload bytes to Higgsfield temporary storage; returns a public URL."""
    meta = _hf_check(requests.post(f"{HF_BASE}/files/generate-upload-url",
                                   headers=_hf_headers(settings),
                                   json={"content_type": content_type}, timeout=60))
    r = requests.put(meta["upload_url"], data=data, headers=meta.get("upload_headers") or
                     {"Content-Type": content_type}, timeout=300)
    r.raise_for_status()
    return meta["public_url"]


_public_cache = {}


def public_url(settings, url):
    """Higgsfield needs public https URLs. Blob URLs already are; local files get uploaded."""
    if url.startswith("http"):
        return url
    if url not in _public_cache:
        ext = url.rsplit(".", 1)[-1].lower()
        ctype = {"png": "image/png", "webp": "image/webp", "wav": "audio/wav"}.get(ext, "image/jpeg")
        _public_cache[url] = hf_upload(settings, store.fetch_bytes(url), ctype)
    return _public_cache[url]


def hf_submit(settings, endpoint, body):
    try:
        r = requests.post(f"{HF_BASE}/{endpoint.strip('/')}", headers=_hf_headers(settings),
                          json=body, timeout=120)
    except requests.RequestException:
        raise UserError("Couldn't reach Higgsfield. Check your connection and try again.")
    data = _hf_check(r)
    try:
        usd = hf_estimate(settings, endpoint, body, timeout=10)
    except UserError:
        usd = None
    return {"request_id": data["request_id"], "status_url": data.get("status_url") or
            f"{HF_BASE}/requests/{data['request_id']}/status", "status": data.get("status", "queued"),
            "submitted": time.time(), "usd": usd, "endpoint": endpoint}


def hf_poll(settings, job):
    """Returns (status, output_url, error)."""
    try:
        r = requests.get(job["status_url"], headers=_hf_headers(settings), timeout=60)
    except requests.RequestException:
        raise UserError("Couldn't reach Higgsfield to check progress. Will retry.")
    data = _hf_check(r)
    status = data.get("status")
    if status == "completed":
        url = ((data.get("images") or [{}])[0].get("url")
               or (data.get("video") or {}).get("url")
               or (data.get("audio") or {}).get("url"))
        return status, url, None
    if status in ("failed", "nsfw", "canceled"):
        err = data.get("error") or {"nsfw": "Blocked by content moderation - adjust the prompt.",
                                    "canceled": "Canceled."}.get(status, "Generation failed.")
        return status, None, str(err)
    return status, None, None


def low_balance(error):
    return bool(error) and any(x in str(error).lower() for x in ("balance", "top up", "insufficient"))


def content_blocked(error):
    return bool(error) and any(x in str(error).lower() for x in ("copyright", "moderation", "blocked", "nsfw", "policy"))


def image_job(settings, prompt, ref_urls, aspect_ratio, best=False):
    """best: the highest quality, for the few images every later scene is matched against."""
    body = {"prompt": prompt, "aspect_ratio": aspect_ratio,
            "resolution": "2k" if best else settings.get("image_resolution") or "1k",
            "quality": "medium" if best else settings.get("image_quality") or "medium"}
    refs = [public_url(settings, u) for u in ref_urls[:10]]
    if refs:
        body["image_urls"] = refs
    return hf_submit(settings, settings["image_endpoint"], body)


# Higgsfield's image model accepts up to 10 references on paper, but in practice generations
# fail once a request carries more than about 4 full-size portraits. Bigger casts are sent as
# one lineup image instead: every character side by side, in a known order.
MAX_SEPARATE_REFS = 4
LINEUP_TILE = (480, 640)
LINEUP_CACHE_SECONDS = 3600  # Higgsfield's upload storage is temporary
_lineup_cache = {}


def cast_lineup(settings, cast):
    """One image showing each character's main reference, left to right (rows of up to 4).
    Uploaded to Higgsfield's temporary storage; returns its public URL."""
    urls = tuple(c["images"][0] for c in cast)
    cached = _lineup_cache.get(urls)
    if cached and time.time() - cached[1] < LINEUP_CACHE_SECONDS:
        return cached[0]
    w, h = LINEUP_TILE
    n = len(urls)
    cols = n if n <= 4 else -(-n // 2)
    inputs, chains, layout = [], [], []
    for i, u in enumerate(urls):
        ext = os.path.splitext(u.split("?")[0])[1] or ".png"
        inputs += ["-i", store.download_to(u, store.tmp_path(f"lineup{i}{ext}"))]
        chains.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                      f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:white,setsar=1[t{i}]")
        layout.append(f"{(i % cols) * w}_{(i // cols) * h}")
    grid = "".join(f"[t{i}]" for i in range(n)) + f"xstack=inputs={n}:layout={'|'.join(layout)}:fill=white"
    out = store.tmp_path("lineup.jpg")
    _run(inputs + ["-filter_complex", ";".join(chains) + ";" + grid, "-frames:v", "1", "-q:v", "3", out])
    with open(out, "rb") as f:
        url = hf_upload(settings, f.read(), "image/jpeg")
    for path in [out] + inputs[1::2]:
        os.remove(path)
    _lineup_cache[urls] = (url, time.time())
    return url


def cast_refs(settings, cast, per_character=2, max_refs=MAX_SEPARATE_REFS):
    """Reference image URLs for a cast, plus prompt lines saying who is in which image."""
    cast = [c for c in cast if c.get("images")]
    if per_character > 1 and sum(min(per_character, len(c["images"])) for c in cast) > max_refs:
        per_character = 1  # one sharp face each holds a likeness better than a small face in a lineup
    if sum(min(per_character, len(c["images"])) for c in cast) <= max_refs:
        refs, legend = [], []
        for c in cast:
            imgs = c["images"][:per_character]
            start = len(refs) + 1
            refs += imgs
            nums = ", ".join(str(i) for i in range(start, start + len(imgs)))
            legend.append(f"{c['name']} = reference image {nums}")
        return refs, legend
    order = "left to right" if len(cast) <= 4 else "left to right, top row then bottom row"
    who = ", ".join(c["name"] for c in cast)
    return [cast_lineup(settings, cast)], [
        f"reference image 1 is a lineup of the {len(cast)} characters, {order}: {who}"]


def character_prompt(settings, c, shot="headshot", photos=0, restyle=False):
    """headshot: the master reference for the face. body and sheet are matched to reference image 1.
    photos: how many photos of the real person come first in the references, for their AI version.
    restyle: reference image 1 is this character in another visual style; redraw them in the current one."""
    who = "person" if is_adult(c) else "child"
    look = look_of(settings)
    animated = look in ("lifelike", "cartoon")
    kind = {"lifelike": "Lifelike film portrait", "cartoon": "3D animated character portrait"}.get(look, "Photo")
    version = "lifelike film version" if look == "lifelike" else "3D animated film version"
    base = f"{style_prefix(settings, [c])}{identity_lock([c])}{kind} of {character_line(c)}. "
    if photos:
        refs = "Reference image 1 is a photo" if photos == 1 else f"Reference images 1-{photos} are photos"
        if animated:
            base += (f"{refs} of this real {who}. Make the {version} of this very same {who}: "
                     "instantly recognizable to their family, keeping the face shape, eye color and shape, "
                     "eyebrows, nose, ears, smile, teeth, skin tone, freckles, marks and hair exactly, at the "
                     "same age, in the visual style described above. ")
        else:
            base += (f"{refs} of this real {who}. Make a new photo of the very same {who}: an exact likeness "
                     "that their family would recognize instantly, keeping the face shape, eyes, eyebrows, nose, "
                     "ears, mouth, teeth, skin tone, freckles, marks and hair exactly as in the photos, at the "
                     "same age. Do not beautify, slim, smooth or restyle them. Only the clothes, background and "
                     "lighting are new. ")
    if restyle:
        base += (f"Reference image 1 shows this same {who} in a different visual style. Redraw them in the style "
                 "described above, keeping the face shape, eye color, eyebrows, nose, ears, smile, teeth, skin "
                 "tone, freckles, hair color, hairstyle and outfit recognizably the same. ")
    if shot == "sheet":
        return (base + "Turnaround sheet on a clean white background: front, three-quarter and side views plus "
                "three expressions (happy, surprised, laughing), both ears visible in the side view, identical "
                "face, outfit and proportions in every view. Match reference image 1 exactly. No text.")
    if shot == "body":
        return (base + "Full-body view, friendly natural pose, softly blurred sunny outdoor background, centered, "
                f"the exact same {who} as reference image 1, signature item clearly visible. No text.")
    return (base + "Head and shoulders portrait, facing the camera straight on, eyes open and looking into the lens, "
            "a natural open-mouth smile showing the teeth, both ears visible, hair clear of the face, the "
            "signature item clearly visible, even soft daylight, plain softly blurred background, tack-sharp "
            "focus on the face. No text.")


def _direction_text(scene, look, cast=(), everyone=()):
    """The creative director's breakdown of this shot, for the keyframe."""
    d = scene["direction"]
    parts = [f"FIRST FRAME: {without_absent(d['keyframe'], cast, everyone)}",
             f"Shot: {d['shot_size']}, {d['angle']}, {d['lens']}.",
             f"Blocking: {without_absent(d['blocking'], cast, everyone)}", f"Lighting: {d['lighting']}"]
    if look:
        parts.append(f"Look: {look['style']} Palette: {look['palette']} Wardrobe: {look['wardrobe']}")
    if d.get("continuity"):
        parts.append(f"Continuity: {d['continuity']}")
    return " ".join(parts) + " "


def scene_image_prompt(settings, scene, cast, look=None, location=None, everyone=()):
    """The keyframe's prompt and references. With the creative director's notes, the shot follows them,
    and the location's set photo (if it has one) is the last reference so every shot there matches it."""
    plate = (location or {}).get("image")
    refs, legend = cast_refs(settings, cast, max_refs=MAX_SEPARATE_REFS - 1 if plate else MAX_SEPARATE_REFS)
    if plate:
        refs = refs + [plate]
        legend.append(f"reference image {len(refs)} is the empty set of {location['name']}: put the scene in "
                      "exactly this place, with the same landmarks, materials, colors and light")
    who = "; ".join(legend)
    framing = ("Medium close-up with the singers' faces large and sharp, facing the camera, mouths clearly visible"
               if scene.get("singers") else "Cinematic shot with every face clearly visible and in focus")
    if scene.get("direction"):
        shot = _direction_text(scene, look, cast, everyone)
        if location:
            shot += f"SET: {location['name']}: {location['description']} "
    else:
        shot = f"{scene['visual']} Action: {scene['action']}. "
    prompt = (f"{style_prefix(settings, cast)}{identity_lock(cast)}{shot}"
              + (f"References: {who}. " if who else "")
              + f"{framing}, no text, no watermark.")
    return prompt, refs


THUMBNAIL_TAKES = [
    ("One face", "One cast member's face fills a third of the frame in the foreground with a huge exaggerated "
                 "expression (wide eyes, open mouth), the scene's key object or animal right beside them."),
    ("Funny moment", "Two cast members in a funny, high-emotion moment about the song's topic (a playful tug, a "
                     "shared surprise, a proud grin), faces large and close together."),
    ("Dance party", "The whole cast mid-dance in a row, full of energy, with the title in big glossy 3D bubble "
                    "letters across the top."),
]


def thumbnail_prompt(settings, plan, cast, legend, take):
    name, framing = THUMBNAIL_TAKES[take % len(THUMBNAIL_TAKES)]
    title = (f"Big glossy 3D bubble-letter title reading \"{plan['thumbnail_text']}\", spelled exactly. "
             if name == "Dance party" else "No text. ")
    return (f"{style_prefix(settings, cast)}{identity_lock(cast)}YouTube thumbnail for a kids' music video. "
            f"{plan['thumbnail_prompt']} {framing} " + (f"Characters: {'; '.join(legend)}. " if legend else "")
            + title + "Hyper-saturated colors, bright rim light, extremely high contrast, simple readable "
            "composition that works at phone size.")


def icon_job(settings):
    name = settings.get("channel_name") or "our channel"
    prompt = (f"Round YouTube channel icon for a kids' music channel called \"{name}\". A bold, friendly, "
              "professional logo: a big cheerful music note with a smiling sun and a small globe, bright "
              f"saturated colors, clean shapes on a simple background, with the words \"{name}\" in big rounded "
              "letters spelled exactly. Very readable at small size. No people, no children, no watermark.")
    return image_job({**settings, "image_resolution": "2k", "image_quality": "medium"}, prompt, [], "1:1")


INTRO_BACKGROUND = "0xFFF6E0"


def intro_canvas(icon_url):
    """The square icon centered on a wide frame, ready to animate. Returns JPEG bytes."""
    src = store.download_to(icon_url, store.tmp_path("icon.png"))
    out = store.tmp_path("canvas.jpg")
    _run(["-i", src, "-vf", f"scale=-2:760,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:{INTRO_BACKGROUND}",
          "-frames:v", "1", "-q:v", "2", out])
    _discard_download(icon_url, src)
    with open(out, "rb") as f:
        data = f.read()
    os.remove(out)
    return data


def intro_clip_job(settings, canvas_url):
    return hf_submit(settings, settings["kling_endpoint"], {
        "prompt": ("The logo pops onto the screen with a playful bounce, then gently wiggles as sparkles and "
                   "little music notes burst around it. The logo stays centered, sharp and readable. "
                   "The background stays clean. No new text."),
        "image_url": public_url(settings, canvas_url), "duration": 5, "cfg_scale": 0.75,
        "negative_prompt": "new text, changed letters, people, children, distorted logo, scene cuts",
    })


KEEP_IDENTITY = ("Animate exactly what is in this image. Every person stays the same person with the same eyes, "
                 "eyebrows, nose, ears, teeth, hair and outfit, and every animal stays the same animal from the first "
                 "frame to the last: nothing morphs or transforms, and no new people or characters appear. Stay in this same "
                 "shot with no cuts; the camera makes only the one smooth move described, never a big zoom.")
LIVELY = ("Live-action footage at real-time speed, never slow motion. The children have the quick, slightly messy "
          "energy of real kids: fast head bobs and shoulder bounces, knees bending, weight shifting, arms swinging, "
          "natural blinking, eyes darting and changing expressions.")
ANTI_MORPH = ("morphing, transforming, changing faces, changing hair, extra people, a new child entering, new characters, "
              "scene cuts, fast camera moves, motion blur, slow motion, stiff "
              "bodies, frozen faces, mannequin, floating, text, watermark, distorted faces, extra teeth, extra limbs, scary")


def _sung_direction(scene, cast):
    """Who sings what in this scene, so mouths move only when and where the song has words."""
    words = (scene["sung"] if "sung" in scene else scene.get("lyric") or "").strip()
    if not words:
        return "This part of the song has no words: nobody sings, and mouths stay smiling or closed."
    singers = [c["name"] for c in cast if c["id"] in (scene.get("singers") or [])]
    who = " and ".join(singers) or "The children"
    verb = "sings" if len(singers) == 1 else "sing"
    return (f'{who} {verb} these exact words, with lips, teeth and jaw forming each syllable clearly and on time: '
            f'"{words}". Anyone else keeps their mouth closed or smiling.')


def _shot_motion(scene, cast=(), everyone=()):
    """What happens in the shot: the creative director's beats and the choreography when there are some,
    otherwise the storyboard's action and camera. Cast members who aren't in the shot are never mentioned."""
    d, ch = scene.get("direction"), scene.get("choreo")
    if d:
        d = {**d, **{k: without_absent(d[k], cast, everyone) for k in ("beats", "blocking")}}
    if ch:
        ch = {**ch, **{k: without_absent(ch[k], cast, everyone) for k in ("counts", "formation")}}
    if not d:
        dance = f" Choreography by the count: {ch['counts']}" if ch else ""
        return f"{scene['action']}.{dance} Camera: {scene['camera']}."
    text = (f"Beat by beat: {d['beats']} Camera: {d['camera_move']} ({d['shot_size']}, {d['angle']}). "
            f"Blocking: {d['blocking'].rstrip('. ')}.")
    if ch:
        text += f" Choreography by the count: {ch['counts'].rstrip('. ')}. Formation: {ch['formation'].rstrip('. ')}."
    return text


def _clip_length(dur, choices):
    """The shortest clip the model offers that covers the scene."""
    return next((c for c in choices if dur <= c + 0.5), choices[-1])


VIDEO_MODES = ("seedance_ref", "kling_i2v", "wan_i2v", "ltx_fast")


def video_mode(settings):
    mode = settings.get("video_mode")
    return mode if mode in VIDEO_MODES else "seedance_ref"  # the old "lipsync" mix is retired


def video_prompt(scene, cast, everyone=(), bpm=None, note=""):
    habits = " ".join(f"{c['name']} {c['mannerisms']}." for c in cast if c.get("mannerisms"))
    hair = " ".join(f"{c['name']}'s hair stays exactly like this in every frame: {hair_of(c)}"
                    for c in cast if hair_of(c))
    hair += " " + " ".join(f"{c['name']} keeps wearing {c['icon'].rstrip('. ')} for the whole shot."
                           for c in cast if c.get("icon"))
    tempo = f"Everyone moves on the beat of a {bpm} BPM song. " if bpm else ""
    motion = (f"{KEEP_IDENTITY} {head_count(cast)} {hair} {LIVELY} {tempo}{_shot_motion(scene, cast, everyone)} "
              f"{habits} {_sung_direction(scene, cast)} Smooth, steady camera; every face sharp and in focus. "
              "No text or subtitles.")
    if len(cast) >= 3:
        # Video models mix up characters that cross paths, so a group stays put.
        motion += (" Everyone dances on their own spot and stays in the same left-to-right order: "
                   "nobody walks, rolls or crosses in front of anyone else.")
    if note:
        motion += f" Fix from the last attempt: {note}"
    return motion


def video_job(settings, scene, cast, audio_url=None, bpm=None, everyone=(), note=""):
    dur = max(1.0, scene["end"] - scene["start"])
    mode = video_mode(settings)
    motion = video_prompt(scene, cast, everyone, bpm, note)
    resolution = settings.get("video_resolution") or "1080p"
    if mode == "kling_i2v":
        return hf_submit(settings, settings["kling_endpoint"], {
            "prompt": motion, "image_url": public_url(settings, scene["image"]),
            "duration": _clip_length(dur, (5, 10)), "negative_prompt": ANTI_MORPH, "cfg_scale": 0.75,
        })
    if mode == "ltx_fast":
        return hf_submit(settings, settings["ltx_endpoint"], {
            "prompt": motion, "image_url": public_url(settings, scene["image"]),
            "duration": _clip_length(dur, (6, 8, 10)), "resolution": resolution,
            "aspect_ratio": settings.get("aspect_ratio") or "16:9", "generate_audio": False,
        })
    if mode == "wan_i2v":
        body = {"prompt": motion, "image_url": public_url(settings, scene["image"]),
                "duration": _clip_length(dur, (5, 10, 15)), "resolution": resolution}
        if audio_url:
            body["audio_url"] = audio_url
            body["prompt"] += " Only the singers named above sing, in sync with the audio."
        return hf_submit(settings, settings["wan_endpoint"], body)
    # Seedance 2.0: the keyframe opens the shot, each character's face reference keeps them on-model, and
    # the scene's exact slice of the song drives the lip-sync and the dancing.
    refs, legend = cast_refs(settings, cast, per_character=1, max_refs=MAX_SEPARATE_REFS - 1)
    legend = "; ".join(re.sub(r"reference image (\d+)", lambda m: f"reference image {int(m.group(1)) + 1}", x)
                       for x in legend)
    body = {
        "prompt": (f"Use reference image 1 as the opening frame. {('References: ' + legend + '. ') if legend else ''}"
                   + motion + (" Only the singers named above sing, in sync with the reference audio."
                               if audio_url else "")),
        "image_urls": [public_url(settings, u) for u in ([scene["image"]] + refs)[:9]],
        "duration": int(min(15, max(4, math.ceil(dur)))),  # never shorter than the scene
        "resolution": resolution,
        "aspect_ratio": settings.get("aspect_ratio") or "16:9",
        "generate_audio": bool(audio_url),
    }
    if audio_url:
        body["audio_urls"] = [audio_url]
    try:
        return hf_submit(settings, settings["seedance_endpoint"], {**body, "bitrate_mode": "high"})
    except UserError as e:
        if "bitrate" not in str(e).lower():
            raise
        return hf_submit(settings, settings["seedance_endpoint"], body)  # this account can't ask for high bitrate


# ============================================================== price check

def hf_estimate(settings, endpoint, body, timeout=30):
    """Higgsfield's quoted price in USD for one request with these parameters (not charged)."""
    try:
        r = requests.post(f"{HF_BASE}/estimate/{endpoint.strip('/')}", headers=_hf_headers(settings),
                          json=body, timeout=timeout)
    except requests.RequestException:
        raise UserError("Couldn't reach Higgsfield.")
    usd = _find_usd(_hf_check(r))
    if usd is None:
        raise UserError(f"Unexpected price reply: {r.text[:200]}")
    return usd


def _find_usd(data):
    """The USD amount in an estimate reply, wherever it sits."""
    if isinstance(data, dict):
        for k, v in data.items():
            if "usd" in k.lower() and not isinstance(v, (dict, list)):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        data = list(data.values())
    if isinstance(data, list):
        for v in data:
            found = _find_usd(v) if isinstance(v, (dict, list)) else None
            if found is not None:
                return found
    return None


def price_options(settings, sample_image):
    """Each option: what it is, the settings it needs, and a request to price.
    Keyframes are priced per image; animation per 5-second clip."""
    img = {"prompt": "Kids dancing in a sunny meadow", "aspect_ratio": "16:9", "image_urls": [sample_image]}
    keyframes = [
        {"label": f"Grok Image 2.0, {res.upper()}, {q} quality",
         "settings": {"image_resolution": res, "image_quality": q},
         "endpoint": settings["image_endpoint"], "body": {**img, "resolution": res, "quality": q}}
        for res, q in (("1k", "low"), ("1k", "medium"), ("2k", "medium"))]
    clip = {"prompt": "The kids dance and wave", "duration": 5}
    wide = {"resolution": "1080p", "aspect_ratio": "16:9"}
    animation = [
        {"label": "LTX 2.5 Fast, 1080p, 6 seconds", "settings": {"video_mode": "ltx_fast", "video_resolution": "1080p"},
         "endpoint": settings["ltx_endpoint"],
         "body": {**clip, **wide, "duration": 6, "image_url": sample_image, "generate_audio": False}},
        {"label": "Kling 2.5 Turbo", "settings": {"video_mode": "kling_i2v"},
         "endpoint": settings["kling_endpoint"], "body": {**clip, "image_url": sample_image}},
        {"label": "Wan 2.6, 1080p (moves to the song)", "settings": {"video_mode": "wan_i2v", "video_resolution": "1080p"},
         "endpoint": settings["wan_endpoint"], "body": {**clip, "image_url": sample_image, "resolution": "1080p"}},
        {"label": "Seedance 2.0, 1080p (moves to the song)", "settings": {"video_mode": "seedance_ref", "video_resolution": "1080p"},
         "endpoint": settings["seedance_endpoint"], "body": {**clip, **wide, "image_urls": [sample_image]}},
    ]
    return {"keyframes": keyframes, "animation": animation}


def price_check(settings, sample_image):
    out = {}
    for group, options in price_options(settings, sample_image).items():
        rows = []
        for o in options:
            try:
                usd, error = hf_estimate(settings, o["endpoint"], o["body"]), None
            except UserError as e:
                if "rejected the key" in str(e) or "balance" in str(e):
                    raise
                usd, error = None, str(e)[:160]
            rows.append({"label": o["label"], "settings": o["settings"], "usd": usd, "error": error})
        out[group] = rows
    return out


# ============================================================== ElevenLabs music

def compose_song(settings, plan):
    need(settings, "elevenlabs_api_key")
    base_styles = [s.strip() for s in re.split(r"[,;]", plan.get("music_style", "")) if s.strip()]
    chunks = []
    for sec in plan["sections"]:
        text = f"[{sec['name']}]" + ("\n" + sec["lyrics"].strip() if sec["lyrics"].strip() else "")
        ms = int(min(120000, max(3000, float(sec["duration_seconds"]) * 1000)))
        chunks.append({"text": text, "duration_ms": ms,
                       "positive_styles": (base_styles + sec.get("styles", []))[:8] or ["kids pop"]})
    r = requests.post(
        "https://api.elevenlabs.io/v1/music?output_format=mp3_44100_128",
        headers={"xi-api-key": settings["elevenlabs_api_key"], "Content-Type": "application/json"},
        json={"composition_plan": {"chunks": chunks}, "model_id": settings.get("music_model") or "music_v2_5"},
        timeout=290,
    )
    if r.status_code in (401, 403):
        raise UserError("ElevenLabs rejected the API key (or your plan lacks Music access).")
    if r.status_code >= 400:
        raise UserError(f"ElevenLabs error {r.status_code}: {r.text[:400]}")
    minutes = sum(c["duration_ms"] for c in chunks) / 60000
    log_spend("ElevenLabs", "Song", minutes * MUSIC_USD_PER_MINUTE, f"{minutes:.1f} minutes of music")
    return r.content


def compose_jingle(settings, seconds):
    """A very short instrumental sting for the intro."""
    need(settings, "elevenlabs_api_key")
    plan = {"chunks": [{"text": "[Instrumental]", "duration_ms": max(3000, int(seconds * 1000)), "positive_styles": [
        "cheerful kids TV jingle", "ukulele", "glockenspiel", "hand claps", "instrumental, no vocals"]}]}
    r = requests.post(
        "https://api.elevenlabs.io/v1/music?output_format=mp3_44100_128",
        headers={"xi-api-key": settings["elevenlabs_api_key"], "Content-Type": "application/json"},
        json={"composition_plan": plan, "model_id": settings.get("music_model") or "music_v2_5"}, timeout=120)
    if r.status_code >= 400:
        raise UserError(f"ElevenLabs couldn't make the intro jingle ({r.status_code}): {r.text[:200]}")
    log_spend("ElevenLabs", "Intro jingle", seconds / 60 * MUSIC_USD_PER_MINUTE, f"{seconds:.0f} seconds of music")
    return r.content


def speak(settings, text):
    """Narration audio (mp3 bytes) for a line of text."""
    need(settings, "elevenlabs_api_key")
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{settings['narration_voice']}?output_format=mp3_44100_128",
        headers={"xi-api-key": settings["elevenlabs_api_key"], "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_flash_v2_5"}, timeout=60)
    if r.status_code >= 400:
        raise UserError(f"ElevenLabs couldn't read the outro aloud ({r.status_code}): {r.text[:200]}")
    log_spend("ElevenLabs", "Outro narration", len(text) / 1000 * SPEECH_USD_PER_1K_CHARACTERS, f"{len(text)} characters")
    return r.content


def transcribe_song(settings, song_path):
    """When each sung word happens: a list of {text, start, end} in seconds."""
    need(settings, "elevenlabs_api_key")
    try:
        with open(song_path, "rb") as f:
            r = requests.post("https://api.elevenlabs.io/v1/speech-to-text",
                              headers={"xi-api-key": settings["elevenlabs_api_key"]},
                              data={"model_id": "scribe_v2", "timestamps_granularity": "word"},
                              files={"file": ("song.mp3", f, "audio/mpeg")}, timeout=120)
    except requests.RequestException:
        raise UserError("Couldn't reach ElevenLabs to listen to the song.")
    if r.status_code in (401, 403):
        raise UserError("This ElevenLabs key isn't allowed to use speech-to-text.")
    if r.status_code >= 400:
        raise UserError(f"ElevenLabs speech-to-text error {r.status_code}: {r.text[:200]}")
    words = [{"text": w["text"], "start": w["start"], "end": w["end"]}
             for w in r.json().get("words", []) if w.get("type") == "word"]
    if words:
        log_spend("ElevenLabs", "Song timing", words[-1]["end"] / 3600 * TRANSCRIBE_USD_PER_HOUR,
                  "Listened to the song for caption timing")
    return words


# ============================================================== ffmpeg

FFMPEG_STEP_TIMEOUT = 120  # one scene segment never legitimately takes this long


def _run(args):
    try:
        p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] + args,
                           capture_output=True, text=True, timeout=FFMPEG_STEP_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg got stuck on one of the clips (a damaged image or video?). "
                           "Regenerate the scene and render again.")
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + p.stderr[-600:])


def media_duration(path):
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
    if not m:
        raise UserError("Couldn't read the audio file's length.")
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def audio_slice_wav(song_path, start, end):
    """Cut a 2-15 s WAV from the song (Higgsfield's audio reference limits)."""
    length = min(15.0, max(2.0, end - start))
    out = store.tmp_path("slice.wav")
    _run(["-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", song_path,
          "-ac", "1", "-ar", "24000", out])
    return out


def frame_size(aspect):
    return {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:3": (1440, 1080)}.get(aspect, (1920, 1080))


# Supabase's default upload limit is 50 MB. ffmpeg overshoots a bitrate cap on short, busy segments,
# so aim well under it.


_captions_supported = None


def captions_supported():
    """Whether this ffmpeg build can burn ASS subtitles (needs libass)."""
    global _captions_supported
    if _captions_supported is None:
        p = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True)
        _captions_supported = bool(re.search(r"^\s*\S+\s+ass\s", p.stdout, re.M))
    return _captions_supported


CAPTION_FONT_FILE = "Fredoka-Bold.ttf"


def fonts_dir():
    """Folder holding the caption font: the bundled copy, else a one-time download to /tmp."""
    if os.path.exists(os.path.join(FONTS_DIR, CAPTION_FONT_FILE)):
        return FONTS_DIR
    cache = "/tmp/kidsong-fonts"
    path = os.path.join(cache, CAPTION_FONT_FILE)
    if not os.path.exists(path):
        # An old user agent makes the Google Fonts CSS API hand back plain TTF links.
        css = requests.get("https://fonts.googleapis.com/css2?family=Fredoka:wght@700",
                           headers={"User-Agent": "Mozilla/4.0"}, timeout=30).text
        url = re.search(r"https://[^)\s]+\.ttf", css)
        if not url:
            raise RuntimeError("Couldn't find the caption font on Google Fonts.")
        os.makedirs(cache, exist_ok=True)
        with open(path, "wb") as f:
            f.write(requests.get(url.group(0), timeout=60).content)
    return cache


def _ass_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _lyric_lines(text):
    """Split a scene's lyric into caption lines of at most 8 words."""
    lines = []
    for raw in re.split(r"\n|\s/\s", text or ""):
        words = raw.split()
        while len(words) > 8:
            cut = (len(words) + 1) // 2
            lines.append(words[:cut])
            words = words[cut:]
        if words:
            lines.append(words)
    return lines


CAPTION_LEAD_SECONDS = 0.25  # a line appears just before its first word is sung
CAPTION_HOLD_SECONDS = 0.4  # and lingers briefly after its last word


def _ass_text(word):
    return word.replace("\\", "").replace("{", "(").replace("}", ")")


def _timed_caption_events(timed, pop):
    """One caption per lyric line, shown only while it's sung, so instrumental breaks stay clear."""
    events = []
    for i, line in enumerate(timed):
        first, last = line["times"][0][0], line["times"][-1][1]
        prev_end = timed[i - 1]["times"][-1][1] if i else 0.0
        next_start = timed[i + 1]["times"][0][0] if i + 1 < len(timed) else last + CAPTION_HOLD_SECONDS
        show = max(prev_end, first - CAPTION_LEAD_SECONDS, 0.0)
        hide = max(last, min(last + CAPTION_HOLD_SECONDS, next_start - CAPTION_LEAD_SECONDS))
        parts = [f"{{\\k{round((first - show) * 100)}}}"]
        for j, word in enumerate(line["words"]):
            until = line["times"][j + 1][0] if j + 1 < len(line["words"]) else last
            parts.append(f"{{\\kf{max(1, round((until - line['times'][j][0]) * 100))}}}{_ass_text(word)}")
        events.append(f"Dialogue: 0,{_ass_time(show)},{_ass_time(hide)},Sing,,0,0,0,,{pop}{parts[0]}" + " ".join(parts[1:]))
    return events


def _ass_header(w, h):
    size = round(h * 0.075)
    # ASS colours are &HAABBGGRR. Primary = sung (yellow), secondary = not yet sung (white).
    return [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Sing,Fredoka,{size},&H003CC9FF,&H00FFFFFF,&H003A2126,&H64000000,-1,0,0,0,"
        f"100,100,1,0,1,{max(3, size // 9)},{max(2, size // 14)},2,{w // 12},{w // 12},"
        f"{round(h * 0.07)},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]


POP = r"{\fad(120,120)\t(0,140,\fscx108\fscy108)\t(140,280,\fscx100\fscy100)}"


# Intro and outro are encoded and stored once, then joined to each video without re-encoding,
# so their format has to match the main video's: same size, frame rate, codec and audio.
BUMPER_ENCODE = ["-c:v", "libx264", "-preset", "superfast", "-bf", "0", "-crf", "22", "-maxrate", "2500k",
                 "-bufsize", "5000k", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]
INTRO_SECONDS = 3.5
MIN_SLIDE_SECONDS = 1.8
SLIDE_PADDING_SECONDS = 0.35
BED_VOLUME = 0.14


def build_intro(settings, clip_url, aspect, out_path):
    """The animated icon with a short jingle."""
    w, h = frame_size(aspect)
    clip = store.download_to(clip_url, store.tmp_path("intro.mp4"))
    jingle = store.tmp_path("jingle.mp3")
    with open(jingle, "wb") as f:
        f.write(compose_jingle(settings, INTRO_SECONDS + 0.5))
    fade = INTRO_SECONDS - 0.4
    _run(["-i", clip, "-i", jingle, "-t", f"{INTRO_SECONDS}", "-map", "0:v", "-map", "1:a",
          "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:"
                 f"{INTRO_BACKGROUND},setsar=1,fps=30,fade=t=in:st=0:d=0.2,fade=t=out:st={fade}:d=0.4",
          "-af", f"afade=t=out:st={fade}:d=0.4"] + BUMPER_ENCODE + [out_path])
    _discard_download(clip_url, clip)
    os.remove(jingle)
    return out_path


def _slide(settings, image_url, caption, line, song_path, song_from, w, h):
    """One outro card: the picture on a blurred copy of itself, a title, a narrated line and quiet music.
    Returns (path, seconds)."""
    voice = store.tmp_path("line.mp3")
    with open(voice, "wb") as f:
        f.write(speak(settings, line))
    seconds = max(MIN_SLIDE_SECONDS, media_duration(voice) + SLIDE_PADDING_SECONDS)
    ass = store.tmp_path("title.ass")
    with open(ass, "w", encoding="utf-8") as f:
        f.write("\n".join(_ass_header(w, h) + [
            f"Dialogue: 0,0:00:00.00,{_ass_time(seconds)},Sing,,0,0,0,,{POP}{_ass_text(caption)}"]) + "\n")
    picture = store.download_to(image_url, store.tmp_path("slide.png"))
    out = store.tmp_path("slide.mp4")
    zw, zh = w * 5 // 4, h * 5 // 4
    frames = int(seconds * 30) + 1
    video = (f"[0:v]split=2[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},gblur=sigma=30[bg];"
             f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,"
             f"scale={zw}:{zh},zoompan=z='min(zoom+0.0006,1.1)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
             f":d={frames}:s={w}x{h}:fps=30,setsar=1,ass=filename='{ass}':fontsdir='{fonts_dir()}'[v]")
    audio = (f"[1:a]aresample=44100,apad[voice];[2:a]aresample=44100,volume={BED_VOLUME}[bed];"
             "[voice][bed]amix=inputs=2:duration=first:normalize=0[mix]")
    _run(["-loop", "1", "-i", picture, "-i", voice, "-ss", f"{song_from:.2f}", "-stream_loop", "-1", "-i", song_path,
          "-filter_complex", f"{video};{audio}", "-map", "[v]", "-map", "[mix]", "-t", f"{seconds:.3f}"]
         + BUMPER_ENCODE + [out])
    _discard_download(image_url, picture)
    os.remove(voice)
    os.remove(ass)
    return out, seconds


def build_outro(settings, cast, closing_image, song_path, aspect, out_path):
    """Meet each character, then ask viewers to like and subscribe, with the song quietly underneath."""
    w, h = frame_size(aspect)
    slides, song_from = [], 0.0
    for c in cast:
        first_name = c["name"].split()[0]
        path, seconds = _slide(settings, c["images"][0], f"This is {c['name']}!", f"This is {first_name}!",
                               song_path, song_from, w, h)
        slides.append(path)
        song_from += seconds
    channel = settings.get("channel_name") or "our channel"
    path, _ = _slide(settings, closing_image, "Like and subscribe!",
                     f"Thanks for singing with us! Like and subscribe to {channel} for more songs!",
                     song_path, song_from, w, h)
    slides.append(path)
    join_videos(slides, out_path)
    for p in slides:
        os.remove(p)
    return out_path


def join_videos(paths, out_path):
    """Join finished videos end to end without re-encoding."""
    listing = store.tmp_path("join.txt")
    with open(listing, "w") as f:
        f.writelines(f"file '{p}'\n" for p in paths)
    _run(["-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", "-movflags", "+faststart", out_path])
    os.remove(listing)
    return out_path


def _discard_download(url, path):
    """Free the temp copy right away: a full video's 1080p clips would fill the server's /tmp."""
    if not store.local_media_path(url):
        os.remove(path)
