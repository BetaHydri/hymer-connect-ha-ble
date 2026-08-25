"""Fix the stale 'in our map?' column in docs/ehg-app-metadata.md.

Only flips rows currently marked '**NO**' whose bus is actually mapped in the
sensor_maps (count>0) to 'yes (mapped/total)'. Leaves existing 'yes (..)' /
'**YES** (..)' rows and genuine NO (count==0) untouched. Data-driven + surgical.

  python tools/_fix_metadata_doc.py          # dry-run: list rows that would flip
  python tools/_fix_metadata_doc.py --apply
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
META = REPO / "tools" / "ehg_metadata.json"
MAPS = REPO / "custom_components" / "hymer_connect" / "sensor_maps"
DOC = REPO / "docs" / "ehg-app-metadata.md"


def mapped_counts() -> dict[int, int]:
    counts: dict[int, set[int]] = {}
    for fn in ("base.json", "lights.json", "hymer.json", "eriba.json"):
        p = MAPS / fn
        if not p.exists():
            continue
        doc = json.loads(p.read_text(encoding="utf-8"))
        for sect in ("sensors", "switches", "lights"):
            for k in (doc.get(sect) or {}):
                m = re.match(r"^(\d+),(\d+)", k)
                if m:
                    counts.setdefault(int(m.group(1)), set()).add(int(m.group(2)))
        clim = doc.get("climate") or {}
        for sub in clim.values():
            if isinstance(sub, dict):
                for v in sub.values():
                    cb = v.get("control_bus") if isinstance(v, dict) else None
                    if cb:
                        counts.setdefault(int(cb), set()).add(-1)
    return {b: len(s) for b, s in counts.items()}


def main() -> int:
    apply = "--apply" in sys.argv
    counts = mapped_counts()
    meta = json.loads(META.read_text(encoding="utf-8"))
    totals = {int(b): len(sl) for b, sl in meta.get("slots", {}).items()}

    lines = DOC.read_text(encoding="utf-8").splitlines()
    flips = []
    out = []
    row_re = re.compile(r"^\| (\d+) \| (.*) \| ([^|]*)\|\s*$")
    for ln in lines:
        m = row_re.match(ln)
        if m and "**NO**" in ln:
            bus = int(m.group(1))
            cnt = counts.get(bus, 0)
            if cnt > 0:
                tot = totals.get(bus, cnt)
                shown = min(cnt, tot) if tot else cnt
                new = ln.replace("**NO**", f"yes ({shown}/{tot})" if tot else "yes")
                flips.append(f"  bus {bus:>3}: NO -> yes ({shown}/{tot})")
                out.append(new)
                continue
        out.append(ln)

    print(f"{len(flips)} stale NO rows would flip to yes:")
    print("\n".join(flips))
    if apply and flips:
        DOC.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\nUPDATED {DOC.relative_to(REPO)}")
    elif not apply:
        print("\n(dry-run; pass --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
