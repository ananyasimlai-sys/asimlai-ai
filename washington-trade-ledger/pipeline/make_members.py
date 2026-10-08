#!/usr/bin/env python3
"""One-off helper: write members.json (a snapshot of every current member of Congress plus
former members who appear in the House trade data). build.py layers the live roster on top.

    python3 -I make_members.py CURRENT.json HISTORICAL.json KADOA_FILERS.json OUT.json
"""
import json, re, sys
cur, hist, filers, out = sys.argv[1:5]
def rec(p):
    t = p["terms"][-1]; nm = p["name"]
    return {"name": nm.get("official_full") or (nm["first"] + " " + nm["last"]), "party": t.get("party"), "state": t.get("state"),
            "district": t.get("district"), "type": t.get("type"), "end": t.get("end")}
members = {}
need = set()
for f in json.load(open(filers, encoding="utf-8")):
    m = re.search(r"/([A-Z]\d{6})\.jpg", f.get("photo_url") or "")
    if m: need.add(m.group(1))
for p in json.load(open(hist, encoding="utf-8")):
    b = p["id"]["bioguide"]
    if b in need or p["terms"][-1].get("end", "") >= "2025-01-01": members[b] = rec(p)
for p in json.load(open(cur, encoding="utf-8")):
    members[p["id"]["bioguide"]] = rec(p)
missing = sorted(need - set(members))
json.dump(members, open(out, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
print(len(members), "members;", "filers without a roster entry:", missing)
