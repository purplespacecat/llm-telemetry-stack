#!/usr/bin/env python3
"""Snapshot the Antigravity data agy-exporter reads into a standalone tree.

Copies only what the exporter opens, per surface: conversations/*.db,
conversation_summaries.db (both via sqlite3 backup, so a live WAL is folded in
consistently) and brain/<cid>/.system_generated/logs/transcript.jsonl.

usage: snapshot.py <src ~/.gemini> <dest dir>
"""
import shutil
import sqlite3
import sys
from pathlib import Path

src_root, dst_root = Path(sys.argv[1]), Path(sys.argv[2])


def backup_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dest = sqlite3.connect(dst)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()


for surface in ("antigravity-cli", "antigravity"):
    s, d = src_root / surface, dst_root / surface
    dbs = sorted((s / "conversations").glob("*.db"))
    for db in dbs:
        backup_db(db, d / "conversations" / db.name)
    summaries = s / "conversation_summaries.db"
    if summaries.exists():
        backup_db(summaries, d / "conversation_summaries.db")
    transcripts = sorted((s / "brain").glob("*/.system_generated/logs/transcript.jsonl"))
    for t in transcripts:
        out = d / t.relative_to(s)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(t, out)
    print(f"{surface}: {len(dbs)} conversations, summaries={'yes' if summaries.exists() else 'no'}, "
          f"{len(transcripts)} transcripts")
