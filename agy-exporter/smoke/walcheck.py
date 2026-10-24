"""Does snapshot_sqlite_to_memory() see un-checkpointed WAL frames behind a read-only mount?

A writer process stands in for Antigravity mid-session: WAL mode, autocheckpoint off,
100 committed rows living only in -wal. Then the directory and its files are made
read-only (what a :ro bind mount presents) and the exporter's snapshot function reads it.
"""
import importlib.util, os, sqlite3, subprocess, sys, tempfile
from pathlib import Path

d = Path(tempfile.mkdtemp()); db = d / "conv.db"
writer = subprocess.Popen([sys.executable, "-I", "-c", f"""
import sqlite3, sys, time
c = sqlite3.connect({str(db)!r}, isolation_level=None)
c.execute('pragma journal_mode=wal'); c.execute('pragma wal_autocheckpoint=0')
c.execute('create table steps(idx integer)')
for i in range(100): c.execute('insert into steps values (?)', (i,))
print('ready', flush=True); time.sleep(120)
"""], stdout=subprocess.PIPE, text=True)
assert writer.stdout.readline().strip() == "ready"
try:
    main_only = sqlite3.connect(f"file:{db}?immutable=1", uri=True).execute("select count(*) from steps").fetchone()[0]
except sqlite3.OperationalError as e:
    main_only = f"none ({e})"

spec = importlib.util.spec_from_file_location("exporter", sys.argv[1]); m = importlib.util.module_from_spec(spec)
sys.modules["exporter"] = m; spec.loader.exec_module(m)
files = [db, Path(f"{db}-wal"), Path(f"{db}-shm")]
try:
    for p in files: p.chmod(0o444)
    d.chmod(0o555)
    try:
        conn = m.snapshot_sqlite_to_memory(db, Path(tempfile.mkdtemp()))
        seen = conn.execute("select count(*) from steps").fetchone()[0]
        print(f"rows committed by writer: 100   in main file alone: {main_only}   exporter sees: {seen}")
    except Exception as e:
        print(f"rows committed by writer: 100   in main file alone: {main_only}   exporter RAISED {type(e).__name__}: {e}")
finally:
    d.chmod(0o755)
    for p in files:
        if p.exists(): p.chmod(0o644)
    writer.kill()
print("sqlite", sqlite3.sqlite_version)
