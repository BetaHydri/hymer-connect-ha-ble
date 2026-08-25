"""Extract EHG component/device identifiers from the Hermes bundles and diff
across app versions/brands to spot newly supported devices or mappings.

Reads the bundles dumped by analyze_ehg_apk.py under
source/androidapp/analysis/<app>/index.android.bundle plus the old app bundle in
_archive_old_app/. source/ is gitignored (RE working data)."""
from __future__ import annotations

import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
ANDROID = BASE.parent / "source" / "androidapp"
ANALYSIS = ANDROID / "analysis"
ARCHIVE = ANDROID / "_archive_old_app"

BUNDLES = {
    "OLD": ARCHIVE / "com.ehg.hymerconnect" / "assets" / "index.android.bundle",
    "hymer_2.10.14": ANALYSIS / "hymer_2.10.14" / "index.android.bundle",
    "hymer_2.10.16": ANALYSIS / "hymer_2.10.16" / "index.android.bundle",
    "eriba_2.10.18": ANALYSIS / "eriba_2.10.18" / "index.android.bundle",
}

# Printable ASCII runs (identifier-ish); Hermes stores string literals inline.
STR_RE = re.compile(rb"[A-Za-z][A-Za-z0-9_]{3,63}")

# Component/appliance identifiers the EHG app classifies on (see memory:
# dan-simms-metadata-architecture). CamelCase device component names + vendor
# tokens. We keep this generous and filter noise afterwards.
DEVICE_RE = re.compile(
    r"^("
    r"LightCircuit\w*|LightGroup\w*|Light\w*Module|"
    r"Truma\w*|Alde\w*|Dometic\w*|Thetford\w*|Airxcel\w*|Aventa\w*|Saphir\w*|"
    r"Combi\w*|TelecoTelair\w*|Telair\w*|Teleco\w*|"
    r"Votronic\w*|Schaudt\w*|EBL\w*|NordElettronica\w*|CBE\w*|"
    r"Toptron\w*|Hegotec\w*|LIM\w*|ZipDee\w*|Zipdee\w*|"
    r"\w*Fridge\w*|\w*Heater\w*|\w*Boiler\w*|\w*Inverter\w*|\w*Charger\w*|"
    r"\w*Awning\w*|\w*Battery\w*|\w*Solar\w*|\w*Compressor\w*|"
    r"\w*AirCondition\w*|\w*Aircon\w*"
    r")$"
)

# Vendor/keyword tokens to also surface as free-text hits (case-insensitive).
KEYWORDS = [
    "truma", "alde", "dometic", "thetford", "airxcel", "aventa", "saphir",
    "votronic", "schaudt", "nordelettronica", "teleco", "telair", "toptron",
    "hegotec", "zipdee", "webasto", "eberspacher", "victron", "mppt",
    "lithium", "agm", "gasfilter", "duocontrol",
]


def strings(path: Path) -> set[str]:
    data = path.read_bytes()
    return {m.group().decode("latin1") for m in STR_RE.finditer(data)}


def components(strs: set[str]) -> set[str]:
    return {s for s in strs if DEVICE_RE.match(s)}


def main() -> None:
    sets: dict[str, set[str]] = {}
    comps: dict[str, set[str]] = {}
    for app, p in BUNDLES.items():
        if not p.exists():
            print(f"MISSING bundle: {app} -> {p}")
            continue
        s = strings(p)
        sets[app] = s
        comps[app] = components(s)
        print(f"{app:16} strings={len(s):>6}  device-like={len(comps[app]):>4}")

    def show(title: str, items: set[str]) -> None:
        print(f"\n### {title} ({len(items)})")
        for x in sorted(items, key=str.lower):
            print(f"  {x}")

    if "OLD" in comps and "hymer_2.10.16" in comps:
        new = comps["hymer_2.10.16"] - comps["OLD"]
        gone = comps["OLD"] - comps["hymer_2.10.16"]
        show("NEW device-like identifiers in hymer_2.10.16 vs OLD", new)
        show("REMOVED device-like identifiers (OLD -> 2.10.16)", gone)

    if "hymer_2.10.16" in comps and "eriba_2.10.18" in comps:
        only_eriba = comps["eriba_2.10.18"] - comps["hymer_2.10.16"]
        only_hymer = comps["hymer_2.10.16"] - comps["eriba_2.10.18"]
        show("ONLY in Eriba 2.10.18 (not Hymer 2.10.16)", only_eriba)
        show("ONLY in Hymer 2.10.16 (not Eriba 2.10.18)", only_hymer)

    # keyword sweep on the newest Hymer bundle for anything device-ish we missed
    if "hymer_2.10.16" in sets:
        kw_hits = {
            s for s in sets["hymer_2.10.16"]
            if any(k in s.lower() for k in KEYWORDS)
        }
        show("Keyword sweep (Hymer 2.10.16) - vendor/appliance tokens", kw_hits)


if __name__ == "__main__":
    main()
