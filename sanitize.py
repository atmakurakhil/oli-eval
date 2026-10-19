#!/usr/bin/env python3
"""Copy raw agent transcripts into transcripts/, dropping lines the scorer never reads
(harness init, hooks, thinking-token counters, rate-limit events) so the public copy
carries no machine setup. Usage: sanitize.py SRC_DIR DEST_DIR"""
import json, pathlib, shutil, sys

DROP = {("system", "init"), ("system", "hook_started"), ("system", "hook_response"),
        ("system", "thinking_tokens"), ("rate_limit_event", None)}

src, dst = map(pathlib.Path, sys.argv[1:3])
for f in src.rglob("*"):
    if not f.is_file() or f.suffix == ".err":
        continue
    out = dst / f.relative_to(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    if f.suffix != ".jsonl":
        shutil.copy2(f, out); continue
    keep = []
    for line in f.read_text().splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if (d.get("type"), d.get("subtype")) not in DROP:
            keep.append(line)
    out.write_text("\n".join(keep) + "\n")
