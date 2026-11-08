"""The two Claude passes between the storyboard and the keyframes: a choreographer who sets the dance,
and a creative director who designs the world and breaks every scene down shot by shot.

Both write into the project's plan: plan["choreography"] and plan["direction"] for the whole video,
scene["choreo"] and scene["direction"] for each shot. Keyframe and animation prompts read them."""
from concurrent.futures import ThreadPoolExecutor

import services as sv

SHOTS_PER_CALL = 10  # scenes the director breaks down per Claude call; the calls run side by side
MAX_PARALLEL_CALLS = 4


def _obj(**props):
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


STR = {"type": "string"}
STRS = {"type": "array", "items": STR}

CHOREO_SCHEMA = _obj(
    style=STR,
    formation=STR,
    moves={"type": "array", "items": _obj(name=STR, counts=STR, steps=STR, cue=STR)},
    sections={"type": "array", "items": _obj(section=STR, routine=STR)},
    scenes={"type": "array", "items": _obj(n={"type": "integer"}, moves=STRS, counts=STR, formation=STR,
                                           energy=STR)},
)

WORLD_SCHEMA = _obj(
    look=_obj(style=STR, palette=STR, lighting=STR, lens=STR, wardrobe=STR),
    locations={"type": "array", "items": _obj(id=STR, name=STR, description=STR, layout=STR, lighting=STR,
                                              palette=STR, props=STR, plate_prompt=STR)},
    flow=STR,
    scene_locations={"type": "array", "items": _obj(n={"type": "integer"}, location=STR)},
)

SHOTS_SCHEMA = _obj(
    scenes={"type": "array", "items": _obj(n={"type": "integer"}, shot_size=STR, angle=STR, lens=STR,
                                           camera_move=STR, beats=STR, blocking=STR, lighting=STR,
                                           keyframe=STR, continuity=STR)},
)


def _names(cast):
    return {c["id"]: c["name"] for c in cast}


def _beats(sc, bpm):
    return round((sc["end"] - sc["start"]) * (bpm or 110) / 60)


def _scene_lines(plan, cast, scenes=None, with_choreo=False):
    names = _names(cast)
    lines = []
    for sc in scenes or plan["scenes"]:
        words = (sc["sung"] if "sung" in sc else sc.get("lyric") or "").strip()
        who = ", ".join(names.get(i, "?") for i in sc.get("cast", [])) or "no main cast"
        singers = ", ".join(names.get(i, "?") for i in sc.get("singers", [])) or "nobody"
        line = (f"- scene {sc['n']} [{sc['section']}] {sc['start']:.1f}-{sc['end']:.1f}s "
                f"({sc['end'] - sc['start']:.1f}s, {_beats(sc, plan.get('bpm'))} beats) | in shot: {who} | "
                f"singing: {singers} | words: \"{words or '(instrumental)'}\" | storyboard: {sc.get('visual', '')} "
                f"Action: {sc.get('action', '')} Camera: {sc.get('camera', '')}")
        if with_choreo and sc.get("choreo"):
            ch = sc["choreo"]
            line += f" | CHOREOGRAPHY: {ch['counts']} Formation: {ch['formation']}"
        lines.append(line)
    return "\n".join(lines)


def _cast_sheet(cast):
    return "\n".join(
        f"- {c['name']} ({'adult' if c.get('kind') == 'adult' else 'child'}, age {c.get('age') or '?'}): "
        f"outfit: {c.get('outfit', '')} | mannerisms: {c.get('mannerisms', '')} | personality: "
        f"{c.get('personality', '')} | signature: {c.get('signature', '')}" for c in cast) or "- no recurring cast"


def _song_summary(plan, brief):
    return (f"Title: {plan.get('title')}\nTopic: {brief.get('topic')}\nLesson: {brief.get('lesson') or '-'}\n"
            f"Setting from the brief: {brief.get('setting') or '-'}\nMood: {brief.get('mood') or '-'}\n"
            f"Music: {plan.get('music_style')} at {plan.get('bpm')} BPM\n\nLyrics:\n"
            + "\n".join(f"[{s['name']}]\n{s['lyrics']}" for s in plan["sections"]))


# ============================================================== choreographer

CHOREOGRAPHER = sv.CHANNEL_PLAYBOOK + """
You are the channel's choreographer. You design dance routines that toddlers and preschoolers copy at home,
and that AI video models can animate cleanly."""


def choreograph(settings, plan, cast, brief):
    """Adds plan["choreography"] and scene["choreo"]. Returns the plan."""
    prompt = f"""Choreograph this song, shot by shot.

{_song_summary(plan, brief)}

Cast:
{_cast_sheet(cast)}

Shots:
{_scene_lines(plan, cast)}

Write:
- style: two sentences on the dance style and energy for this song.
- formation: the one left-to-right order the cast stands in for the whole video (e.g. "Mia, Leo, Ava from
  camera left to right"). Keeping everyone in the same spot every time is what keeps the video model from
  swapping their faces, so only change it when a shot truly needs it, and say so in that scene.
- moves: the song's move library, 4-8 named moves. name: short and fun ("Bunny Hop Clap"). counts: how many
  beats it takes. steps: exactly what each body part does on each count, so anyone could copy it from text.
  cue: the lyric that triggers it. Moves must be ones a 2-6 year old can do: big, simple, on the spot,
  mostly from the waist up so they read in a medium close-up. Act out the lyrics literally ("stomp" = stomp).
- sections: the routine for each song section in order. Every chorus uses the identical routine, because
  kids learn it by repetition.
- scenes: one entry for every shot above (same n). moves: the move names used. counts: the beat-by-beat
  plan that fits the shot's beat count, like "1-2: two knee bounces; 3-4: clap above the head twice; 5-8:
  Bunny Hop Clap". Name who does what when the cast doesn't move together. Singers keep their faces to the
  camera and don't spin while they sing, so their lips stay readable. Use each child's mannerisms between
  moves. formation: who stands where in this shot. energy: low, medium or high.
Nobody crosses in front of anyone else, nobody leaves their spot, no lifts, no floor work, nothing unsafe."""
    out = sv.claude_json(settings, CHOREOGRAPHER, prompt, CHOREO_SCHEMA, "Choreography")
    by_n = {s["n"]: s for s in out.pop("scenes")}
    for sc in plan["scenes"]:
        ch = by_n.get(sc["n"])
        if ch:
            sc["choreo"] = {k: ch[k] for k in ("moves", "counts", "formation", "energy")}
    plan["choreography"] = out
    return plan


# ============================================================== creative director

DIRECTOR = sv.CHANNEL_PLAYBOOK + """
You are the channel's creative director and director of photography. You design one coherent world for each
video and break it down shot by shot so precisely that an image model and a video model reproduce the same
places, the same light and the same children in every shot."""

LOCATION_GUIDE = """\
- id: a short slug like "meadow". name: a friendly name.
- description: the set in great detail, 120-200 words: ground and surfaces, materials, plants, buildings or
  natural features, the exact colors, and 3-5 landmark objects with where each one sits in frame (left,
  center, right, foreground, background) so every shot of this place shows the same things in the same
  spots.
- layout: where the camera usually sits and what's behind the cast from the front, the left and the right.
- lighting: time of day, sun direction and height, shadow length, sky, color temperature. It stays the same
  for the whole video unless the flow says the light changes.
- palette: 4-6 named colors that define this place.
- props: recurring props that live here and where they sit.
- plate_prompt: a prompt for an empty establishing photo of this set with nobody in it, wide, at the
  usual camera position, matching the description, lighting and palette exactly."""


def design_world(settings, plan, cast, brief):
    """The look bible and the locations, and which location each scene happens in."""
    prompt = f"""Design the world for this music video.

{_song_summary(plan, brief)}

Cast:
{_cast_sheet(cast)}

House photographic style: {settings.get('house_style') or sv.HOUSE_STYLE}

Shots:
{_scene_lines(plan, cast, with_choreo=True)}

Write:
- look: the visual bible every shot follows. style: the photographic look in one or two sentences.
  palette: the video's overall color palette. lighting: the overall lighting approach. lens: the lenses
  and depth of field. wardrobe: the rule that every cast member wears exactly their signature outfit from
  the cast list in every shot, with no costumes, hats, coats or changes, plus any prop someone carries
  throughout.
- locations: 2-5 sets, few enough that each one is seen several times and stays recognizable. Prefer
  bright, sunny, safe outdoor or cozy indoor places that suit the lyrics.
{LOCATION_GUIDE}
- flow: two or three sentences on how the video moves through the locations and how the light changes.
- scene_locations: the location id of every shot (same n as above). Keep runs of shots in one place; only
  move when the song moves (a new verse, the bridge)."""
    world = sv.claude_json(settings, DIRECTOR, prompt, WORLD_SCHEMA, "Creative direction: world")
    ids = [loc["id"] for loc in world["locations"]]
    where = {s["n"]: s["location"] for s in world.pop("scene_locations")}
    if not ids:
        raise sv.UserError("The creative director didn't design any locations. Try again.")
    return world, {sc["n"]: where[sc["n"]] if where.get(sc["n"]) in ids else ids[0] for sc in plan["scenes"]}


def _shot_prompt(plan, cast, world, where, scenes):
    locations = {loc["id"]: loc for loc in world["locations"]}
    look = world["look"]
    used = sorted({where[sc["n"]] for sc in scenes})
    sets = "\n".join(f"- {i} ({locations[i]['name']}): {locations[i]['description']} Layout: "
                     f"{locations[i]['layout']} Lighting: {locations[i]['lighting']}" for i in used)
    shots = _scene_lines(plan, cast, scenes, with_choreo=True)
    shots = "\n".join(f"{line} | LOCATION: {where[sc['n']]}" for line, sc in zip(shots.split("\n"), scenes))
    first, last = scenes[0]["n"], scenes[-1]["n"]
    before = next((s for s in plan["scenes"] if s["n"] == first - 1), None)
    return f"""Break down shots {first}-{last} of this music video ({len(plan['scenes'])} shots in all).

Look: {look['style']} Palette: {look['palette']} Lighting: {look['lighting']} Lens: {look['lens']}
Wardrobe: {look['wardrobe']}
How the video flows: {world['flow']}

Sets used in these shots:
{sets}

Cast:
{_cast_sheet(cast)}

Shots to break down:
{shots}
{f"The shot just before these is scene {before['n']}: {before.get('visual', '')}" if before else "These open the video."}

For every shot write:
- shot_size: extreme wide, wide, medium wide, medium, medium close-up or close-up. Any shot with sung words
  is a medium close-up or close-up of its singers, faces large, turned to the camera, mouths clearly
  visible, because lip-sync only works that way. Vary sizes from shot to shot and open each new set with
  a wider shot.
- angle: eye level at the children's height, low angle, high angle, over the shoulder, and so on.
- lens: focal length and depth of field.
- camera_move: one smooth move for the whole shot (slow push-in, gentle pull-back, slow pan left, small arc
  around the singer, crane up, gentle handheld sway on the beat, or locked off). No cuts, whip pans,
  spins or fast zooms; the video model has to keep everyone's face the same.
- beats: what the camera and the cast do second by second over the shot's length, 2-3 beats, like
  "0-2s: close-up on Mia's face as she sings 'hop, hop', bouncing on her toes; 2-4s: camera eases back to
  a medium shot as she claps twice above her head; 4-5s: she points at the lens and giggles". Follow the
  choreography counts and the words.
- blocking: exactly where each person stands in frame (camera left, center, right; foreground or
  background), in the choreography's formation, and which way they face.
- lighting: the light in this shot, consistent with the set and the time of day.
- keyframe: the exact first frame for the image model, 60-100 words: the framing, where each person is and
  their pose and expression at second 0, and which of the set's landmarks are visible and where. Name the
  cast but don't describe their faces or outfits; references handle that.
- continuity: what must match the neighbouring shots: props in hands, positions, light, weather.
Never change anyone's outfit, hair or age, never add new named characters, keep everything safe."""


def break_down_shots(settings, plan, cast, world, where):
    chunks = [plan["scenes"][i:i + SHOTS_PER_CALL] for i in range(0, len(plan["scenes"]), SHOTS_PER_CALL)]
    project = sv.current_spend_project()

    def run(chunk):
        sv.set_spend_project(project)  # each thread keeps its own; spending still counts toward this video
        what = f"Creative direction: shots {chunk[0]['n']}-{chunk[-1]['n']}"
        return sv.claude_json(settings, DIRECTOR, _shot_prompt(plan, cast, world, where, chunk), SHOTS_SCHEMA, what)

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_CALLS, len(chunks))) as pool:
        results = list(pool.map(run, chunks))
    return {s["n"]: s for r in results for s in r["scenes"]}


def direct(settings, plan, cast, brief):
    """Adds plan["direction"] and scene["direction"]. Returns the plan."""
    world, where = design_world(settings, plan, cast, brief)
    shots = break_down_shots(settings, plan, cast, world, where)
    for sc in plan["scenes"]:
        shot = shots.get(sc["n"])
        if shot:
            shot.pop("n", None)
            sc["direction"] = {"location": where[sc["n"]], **shot}
    plan["direction"] = world
    return plan


def location(plan, loc_id):
    return next((loc for loc in (plan.get("direction") or {}).get("locations", []) if loc["id"] == loc_id), None)


def plate_prompt(settings, plan, loc):
    look = plan["direction"]["look"]
    return (f"{settings['house_style']}. Wide establishing photograph of an empty set, with no people, no "
            f"children and no animals in it: {loc['plate_prompt']} {loc['description']} Lighting: "
            f"{loc['lighting']}. Palette: {loc['palette']}. Look: {look['style']} No text, no watermark.")
