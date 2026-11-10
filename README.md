# KidSong Studio

Make kids' music videos (the Music Travel Kids format) with a reusable cast.

0. **Ideas**: Claude researches live trends with web search (competitors' latest uploads, Shorts/TikTok trends, upcoming holidays) and turns the brief into 8 video ideas.
1. **Characters**: design recurring kids (Claude can draft them), generate portraits and turnaround sheets on Higgsfield, or upload your own art. **From photos of a real person** (a child with their parent or guardian's consent, or an adult such as a parent, teacher or host with theirs): upload 1-4 photos and Claude writes an exhaustive face sheet, then Higgsfield makes a likeness-locked AI version. The photos aren't stored. The first two images of each character are sent as references with every scene. Each character has a **story bible** (voice, likes, friends, what they're learning) that Claude uses in every song.
2. **Song & storyboard**: Claude writes original lyrics, a music style, timed sections and a shot list that uses your cast, plus the YouTube title, description, tags and thumbnail idea.
3. **Music**: ElevenLabs Music sings the lyrics, or you upload an MP3/WAV (from Suno, etc.). The lyrics are then **timed by code**: ElevenLabs forced alignment pins the exact written words to the audio (it can't change them), the speech-to-text transcript only confirms which lines are really sung, and every scene is re-cut to start just before its own words. Lines nobody can hear get no captions and no singing.
4. **Choreographer**: Claude designs the dance: a library of named moves kids can copy, the same routine every chorus, beat-by-beat counts for each shot, and one left-to-right formation so the video model never swaps faces.
5. **Creative director**: Claude designs the world (a look bible, a wardrobe lock and 2-5 sets described in detail with landmarks in fixed places), then breaks every scene down: shot size, angle, lens, one camera move, what happens second by second, blocking, the exact first frame and continuity. Each set gets an empty **set photo** that's sent as a reference with every keyframe shot there, so places look the same from shot to shot.
6. **Keyframes**: Grok Image 2.0 on Higgsfield paints each scene from the director's first frame, with the character references and the set photo.
7. **Animation**: Seedance 2.0 at 1080p (high bitrate) animates each scene from its keyframe, one face reference per character and that scene's exact slice of the song, which drives the lip-sync. Prompts lock the head count and each character's hair, and never mention cast members who aren't in the shot.
8. **Review**: each clip becomes a preview cut frame-exact to its place in the song, with that slice of the song and the sing-along captions. Approve or decline it (say what's wrong and the remake is told to fix it).
9. **Render**: the approved previews are joined exactly as they are (no re-encoding) under the whole song, so the finished video is what you approved. Captions are karaoke-style in Fredoka Bold. *Render a draft* works any time.

**▶ Do everything** runs the whole pipeline on the server (`pipeline.py`): one step per call to `/api/tick`, which Vercel's cron hits every minute and any open tab nudges, so you can close the browser. It redraws the cast in the current look, writes, composes (again if lyric lines are missing from the song), choreographs, directs, paints, animates in waves of 8, has Claude check frames of every clip (extra people, hair, signature items, warped faces, blur) and remakes failures up to twice, makes three thumbnails, renders, and cuts a vertical Short. It stops with a plain message when the Higgsfield balance runs out or something fails three times. A progress bar shows on every screen.

**Look**: Settings → *Look of every video* (lifelike by default, 3D cartoon, plain photograph, or custom). Each character has a **signature item** worn in every video. **Chorus reuse** animates each chorus shot once. **Publishing**: connect YouTube in Settings (your own Google OAuth client) to upload the video and its Short with metadata, thumbnail, made-for-kids and altered-content flags, optionally scheduled. *Make a compilation* joins rendered videos.

**Settings** has a *Test connection* button for each key and a *Check server* button (ffmpeg, caption support, storage).

The caption font is Fredoka Bold (SIL Open Font License, `fonts/OFL.txt`). If `fonts/` isn't deployed, the server downloads it from Google Fonts once and caches it in `/tmp`.

## Run locally

```bash
./run.sh
```

Then open http://127.0.0.1:5178 and add keys in **Settings**. Keys are stored in your browser only.

## Deploy on Vercel

- `api/index.py` serves the Flask app; `public/` is static. Functions get 300 s and 2 GB.
- Storage is **Supabase** (project `kidsong-studio`): `characters` and `projects` tables hold JSON documents, and the public `media` bucket holds images, songs and videos. Set `SUPABASE_URL` and `SUPABASE_SECRET_KEY` (an `sb_secret_…` key from Supabase → Project Settings → API Keys) in Vercel. Without them, nothing persists. Set the same two variables locally to use Supabase from your Mac; otherwise the app stores everything in `data/`.
- Supabase's free plan caps files at 50 MB, which forces about 1.8 Mbps for a 2.5-minute 1080p video. On Supabase Pro, raise the upload limit (Storage settings) and set *Storage limit per video* in Settings to match (200 MB gives about 8 Mbps).
- Optional `APP_PASSWORD` env var adds a password on top of Vercel Authentication.
- Uploads through Vercel are capped at about 4.5 MB per file.

## Models (change under Settings → Advanced)

| Job | Default |
|---|---|
| Songwriting, storyboards, ideas | `claude-opus-5-5` |
| Images | `xai/grok-imagine-image-2.0` |
| Video | `bytedance/seedance-2.0/reference-to-video` or `kling-video/v2.5-turbo/pro/image-to-video` |
| Music | ElevenLabs `music_v2_5` |
