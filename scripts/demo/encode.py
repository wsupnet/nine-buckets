#!/usr/bin/env python3
"""Turn record.mjs output (JPEG frames + timestamps) into the demo MP4 and GIF.

Runs inside the recorder image:  python3 encode.py /out
Writes /out/networth-demo.mp4 and /out/networth-demo.gif. The recording runs
slower than real use (cursor travel, pauses for captions), so the MP4 is played
back at SPEED (default 1.4x) and the GIF at a further GIF_SPEED (default 1.25x).
"""
import json
import os
import subprocess
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "/out"
meta = json.load(open(os.path.join(OUT, "frames.json")))
frames = meta["frames"]

# Screencast frames arrive only when the page repaints, so each frame is held
# until the next one's timestamp (concat demuxer durations).
lines = []
for i, f in enumerate(frames):
    # The last frame (a static end card) holds until recording stopped.
    nxt = frames[i + 1]["t"] if i + 1 < len(frames) else min(max(meta["end"], f["t"] + 1.0), f["t"] + 8.0)
    lines.append(f"file 'frames/{f['file']}'\nduration {max(nxt - f['t'], 0.001):.4f}")
lines.append(f"file 'frames/{frames[-1]['file']}'")
open(os.path.join(OUT, "concat.txt"), "w").write("\n".join(lines) + "\n")

mp4 = os.path.join(OUT, "networth-demo.mp4")
subprocess.run([
    "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", os.path.join(OUT, "concat.txt"),
    "-vf", f"setpts=PTS/{os.environ.get('SPEED', '1.4')},fps=30,scale=1920:1200:flags=lanczos,format=yuv420p",
    "-c:v", "libx264", "-preset", "slow", "-crf", "22", "-movflags", "+faststart", mp4,
], check=True)

# README preview: the full walkthrough, sped up a little more and downscaled so it stays light.
gif = os.path.join(OUT, "networth-demo.gif")
speed = float(os.environ.get("GIF_SPEED", "1.25"))
width = int(os.environ.get("GIF_WIDTH", "880"))
fps = int(os.environ.get("GIF_FPS", "10"))
subprocess.run([
    "ffmpeg", "-y", "-loglevel", "error", "-i", mp4,
    "-vf", f"setpts=PTS/{speed},fps={fps},scale={width}:-1:flags=lanczos,split[a][b];"
           f"[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
    gif,
], check=True)

for p in (mp4, gif):
    print(f"{p}: {os.path.getsize(p) / 1e6:.1f} MB")
