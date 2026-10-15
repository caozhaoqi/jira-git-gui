# -*- coding: utf-8 -*-
"""e2e: SSE cookie_expired 是否真的送达订阅者（隔离后端 + 坏 Cookie）。"""
import json
import sys
import threading
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8799"
events = []


def reader():
    with httpx.Client(timeout=120.0, trust_env=False) as c:
        with c.stream("GET", f"{BASE}/api/events") as r:
            ev, buf = None, []
            for line in r.iter_lines():
                if line == "":
                    if buf:
                        events.append((ev, "\n".join(buf)))
                        ev, buf = None, []
                    continue
                if line.startswith("event:"):
                    ev = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    buf.append(line.split(":", 1)[1].strip())


threading.Thread(target=reader, daemon=True).start()
time.sleep(2.0)

print("--- calling /api/repos?refresh=true", flush=True)
with httpx.Client(timeout=300.0, trust_env=False) as c:
    r = c.get(f"{BASE}/api/repos", params={"refresh": True})
print("status:", r.status_code, flush=True)
try:
    print("resp:", json.dumps(r.json(), ensure_ascii=False)[:400], flush=True)
except Exception as e:
    print("resp(raw):", r.text[:400], e, flush=True)

t0 = time.time()
while time.time() - t0 < 20:
    if any(e[0] == "cookie_expired" for e in events):
        break
    time.sleep(0.3)

print("--- events received:")
for ev, data in events:
    print(f"  [{ev}] {data[:250]}")
print("--- RESULT:", "PASS" if any(e[0] == "cookie_expired" for e in events) else "FAIL", flush=True)
