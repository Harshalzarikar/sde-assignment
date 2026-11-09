"""Local debugging: print one workflow's decisions in a time range, plus the managers' saved state.

  python devtools/show_decisions.py state/public_02.db ks_cancel 2027-01-07T12 2027-01-08
"""
import json
import sqlite3
import sys

db_path, wid = sys.argv[1], sys.argv[2]
start = sys.argv[3] if len(sys.argv) > 3 else ""
end = sys.argv[4] if len(sys.argv) > 4 else "9999"
db = sqlite3.connect(db_path)
for ts, kind, summary in db.execute("SELECT ts, kind, summary FROM decisions WHERE workflow_id = ? AND ts >= ? "
                                    "AND ts < ? ORDER BY id", (wid, start, end)):
    print(ts[:16], f"{kind:<22}", summary[:140])
for key, value in db.execute("SELECT key, value FROM meta WHERE key LIKE ?", (f"%:{wid}",)):
    print(f"\n{key}:", json.dumps(json.loads(value), indent=1)[:1500])
