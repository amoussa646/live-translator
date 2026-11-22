"""Stream an audio file to the local engine like a live microphone and report latency.

usage: .venv/bin/python scripts/test_stream.py FILE [--source en] [--target ru] [--fast]
  --fast  send as fast as the engine accepts (file mode) instead of real time
"""
import argparse
import asyncio
import json
import subprocess
import time

import websockets

ap = argparse.ArgumentParser()
ap.add_argument("file")
ap.add_argument("--source", default="en")
ap.add_argument("--target", default="ru")
ap.add_argument("--fast", action="store_true")
ap.add_argument("--url", default="ws://127.0.0.1:8787/ws")
args = ap.parse_args()

pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", args.file, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                     check=True, capture_output=True).stdout
dur = len(pcm) / 32000


async def main():
    url = f"{args.url}?source={args.source}&target={args.target}"
    async with websockets.connect(url, max_size=None) as ws:
        t0 = time.monotonic()
        sent_at = {}

        async def sender():
            step = 3200  # 100 ms
            for i in range(0, len(pcm), step):
                await ws.send(pcm[i:i + step])
                if not args.fast:
                    await asyncio.sleep(0.1)
            await ws.send(b"")
            sent_at["end"] = time.monotonic() - t0

        task = asyncio.create_task(sender())
        sentences = {}
        async for raw in ws:
            m = json.loads(raw)
            now = time.monotonic() - t0
            if m["type"] == "sentence":
                sentences[m["id"]] = (now, m)
                print(f"[{now:6.2f}s] {m['lang']}  {m['text']}")
            elif m["type"] == "translation":
                ts, s = sentences[m["id"]]
                lag = now - s["end"] if not args.fast else None
                extra = f" | audio->translation {lag:.2f}s" if lag is not None else ""
                print(f"[{now:6.2f}s] {m['target']}  {m['text']}   (mt {m.get('ms')} ms{extra})")
            elif m["type"] == "error":
                print("ERROR", m)
            elif m["type"] == "done":
                break
        await task
        total = time.monotonic() - t0
        print(f"\naudio {dur:.1f}s | wall {total:.1f}s | speed {dur / total:.2f}x real time | sentences {len(sentences)}")


asyncio.run(main())
