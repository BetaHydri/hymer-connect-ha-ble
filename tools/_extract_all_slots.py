"""Extract the FULL slot catalog (every componentId) from the decompiled EHG
Hermes bundle and merge the missing buses into tools/ehg_metadata.json.

Ground truth = the app's own single-line slot records, e.g.
  {'componentId': 100, 'id': 1, 'name': 'TirePressureFrontRight',
   'mode': 'r', 'datatype': 'int', 'unit': 'psi'}

We emit label/mode/datatype/unit only (enum wire values are NOT parsed here --
writable string selects still need a separate enum pass). Existing catalog
buses are preserved untouched (add-only), so no curated data is overwritten.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IDX = REPO / "source/androidapp/_archive_old_app/_hermes_decompiled/index.js"
META = REPO / "tools/ehg_metadata.json"

_CAP1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAP2 = re.compile(r"([a-z0-9])([A-Z])")


def camel_snake(s: str) -> str:
    s = _CAP1.sub(r"\1_\2", s)
    s = _CAP2.sub(r"\1_\2", s)
    return re.sub(r"_+", "_", s).lower().strip("_")


def _field(obj: str, key: str) -> str | None:
    m = re.search(r"'" + key + r"'\s*:\s*(?:'([^']*)'|(-?\d+))", obj)
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def main() -> int:
    txt = IDX.read_text(encoding="utf-8", errors="replace")
    rec = re.compile(r"\{[^{}]*?'componentId'\s*:\s*\d+[^{}]*?\}")
    slots: dict[str, dict[str, dict]] = {}
    for m in rec.finditer(txt):
        obj = m.group(0)
        cid, sid = _field(obj, "componentId"), _field(obj, "id")
        if cid is None or sid is None:
            continue
        cid_i = int(cid)
        if cid_i < 1 or cid_i > 200:  # skip 0xFFFFFFFF "no data" sentinels
            continue
        name = _field(obj, "name") or f"slot{sid}"
        entry: dict = {
            "label": camel_snake(name),
            "mode": (_field(obj, "mode") or "r"),
            "datatype": (_field(obj, "datatype") or ""),
        }
        unit = _field(obj, "unit")
        if unit:
            entry["unit"] = unit
        slots.setdefault(str(cid_i), {})[str(sid)] = entry

    meta = json.loads(META.read_text(encoding="utf-8"))
    existing = meta.setdefault("slots", {})
    added = []
    for bus, sd in slots.items():
        if bus not in existing:
            existing[bus] = sd
            added.append(int(bus))
    meta["slots"] = existing
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"extracted {len(slots)} buses from index.js")
    print(f"catalog buses with slots: {len(existing)} (added {len(added)} new)")
    print(f"added: {sorted(added)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
