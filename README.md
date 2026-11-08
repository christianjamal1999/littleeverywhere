# KidSong Studio

Make kids' music videos (the Music Travel Kids format) with a reusable cast.

0. **Ideas**: Claude researches live trends with web search (competitors' latest uploads, Shorts/TikTok trends, upcoming holidays) and turns the brief into 8 video ideas.
1. **Characters**: design recurring kids (Claude can draft them), generate portraits and turnaround sheets on Higgsfield, or upload your own art. The first two images of each character are sent as references with every scene. Each character has a **story bible** (voice, likes, friends, what they're learning) that Claude uses in every song.
2. **Song & storyboard**: Claude writes original lyrics, a music style, timed sections and a shot list that uses your cast, plus the YouTube title, description, tags and thumbnail idea.
3. **Music**: ElevenLabs Music sings the lyrics, or you upload an MP3/WAV (from Suno, etc.) and click *Fit scenes to song*.
4. **Keyframes**: Grok Image 2.0 on Higgsfield paints each scene with the character references.
5. **Animation**: Seedance 2.0 reference-to-video animates each scene, using the keyframe, the character references and that scene's slice of the song. Kling 2.5 Turbo is also available.
6. **Render**: ffmpeg stitches the clips to the song and burns in **sing-along lyrics** (karaoke captions in Fredoka Bold, where each word fills yellow as it's sung). Scenes without video use a slow zoom on the keyframe, so you can render drafts.

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
- Supabase's default upload limit is 50 MB per file; renders are bitrate-capped to fit.
- Optional `APP_PASSWORD` env var adds a password on top of Vercel Authentication.
- Uploads through Vercel are capped at about 4.5 MB per file.

## Models (change under Settings → Advanced)

| Job | Default |
|---|---|
| Songwriting, storyboards, ideas | `claude-opus-5-5` |
| Images | `xai/grok-imagine-image-2.0` |
| Video | `bytedance/seedance-2.0/reference-to-video` or `kling-video/v2.5-turbo/pro/image-to-video` |
| Music | ElevenLabs `music_v2_5` |
