"""Deploy this folder to production on Vercel. Asks for a Vercel token, which is never saved or printed.

    .venv/bin/python deploy.py            upload everything and go live
    .venv/bin/python deploy.py --dry-run  only list what would be uploaded
"""
import getpass
import hashlib
import os
import sys
import time

import requests

API = "https://api.vercel.com"
TEAM = {"teamId": "team_7zaKVbwEyOW61BnVn5PrZFOA"}
PROJECT = "kidsong-studio"
ROOT = os.path.dirname(os.path.abspath(__file__))
FILES = ["app.py", "services.py", "video_fixes.py", "store.py", "vault.py", "direction.py", "sync.py", "pipeline.py", "youtube.py", "requirements.txt", "vercel.json", "api/index.py",
         "public/app.js", "public/index.html", "public/style.css", "fonts/Fredoka-Bold.ttf", "fonts/OFL.txt"]


def read(name):
    with open(os.path.join(ROOT, name), "rb") as f:
        return f.read()


def main():
    listing = []
    for name in FILES:
        data = read(name)
        listing.append({"file": name, "sha": hashlib.sha1(data).hexdigest(), "size": len(data)})
        print(f"{name}  {len(data):,} bytes  {listing[-1]['sha']}")
    if "--dry-run" in sys.argv:
        return

    auth = {"Authorization": "Bearer " + getpass.getpass("Paste your Vercel token (it won't show): ").strip()}
    for entry in listing:
        r = requests.post(f"{API}/v2/files", params=TEAM, data=read(entry["file"]), timeout=120,
                          headers={**auth, "x-vercel-digest": entry["sha"], "Content-Type": "application/octet-stream"})
        r.raise_for_status()
    r = requests.post(f"{API}/v13/deployments", params={**TEAM, "forceNew": "1", "skipAutoDetectionConfirmation": "1"},
                      headers=auth, timeout=120, json={"name": PROJECT, "project": PROJECT, "target": "production",
                                                       "projectSettings": {"framework": None}, "files": listing})
    if r.status_code >= 400:
        sys.exit(f"Vercel refused the deployment ({r.status_code}): {r.text[:400]}")
    deployment = r.json()["id"]
    print("Building", deployment)
    while True:
        time.sleep(5)
        r = requests.get(f"{API}/v13/deployments/{deployment}", params=TEAM, headers=auth, timeout=60)
        r.raise_for_status()
        state = r.json()["readyState"]
        if state == "READY":
            print("Live:", deployment)
            return
        if state in ("ERROR", "CANCELED"):
            sys.exit(f"The build ended as {state}. Nothing changed on the live site.")


if __name__ == "__main__":
    main()
