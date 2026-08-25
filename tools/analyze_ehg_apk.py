"""One-off analyzer: compare OAuth Basic header + extract JS bundle across the
Hymer and Eriba EHG XAPKs. Working tool, source/ is gitignored."""
from __future__ import annotations

import base64
import re
import sys
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
ANDROID = BASE.parent / "source" / "androidapp"
PACKAGES = ANDROID / "packages"
ANALYSIS = ANDROID / "analysis"

# Known shipped default from custom_components/hymer_connect/const.py
KNOWN_B64 = (
    "ZWhnLXByb2QtbW9iaWxlLWFwcC10ZWNobmljYWwtdXNlcjpaez96Ois3bVFhNXZAb2Vl"
    "NV0lZEVeUSpxeDh9WXIoYWw1eFNUaC05LERdYm48OzhWbzh1PGclc8OcLShOMyV5"
)

APPS = {
    "hymer_2.10.14": PACKAGES / "HYMER Connect_2.10.14_APKPure.xapk",
    "hymer_2.10.16": PACKAGES / "HYMER Connect_2.10.16_APKPure.xapk",
    "eriba_2.10.18": PACKAGES / "ERIBA+Connect_2.10.18_APKPure.xapk",
}

# Needles to locate the client credential in any (binary) entry.
NEEDLES = [
    b"ehg-prod",
    b"technical-user",
    b"ZWhnLXByb2Qt",          # base64 prefix of "ehg-prod..."
    b"client_secret",
    b"grant_type",
]
# ASCII creds look like: <client-id>:<secret>
CRED_RE = re.compile(rb"ehg-[a-z0-9\-]+-user:[^\s\"'<>&]{6,80}")
# base64 blobs that might be a Basic header value
B64_RE = re.compile(rb"[A-Za-z0-9+/]{40,120}={0,2}")


def find_base_apk(xapk: Path) -> bytes:
    """Return the raw bytes of the 'base' split apk inside the xapk."""
    with zipfile.ZipFile(xapk) as z:
        names = z.namelist()
        # base apk is the package apk (largest .apk that is not a config split)
        cand = [n for n in names if n.endswith(".apk") and "config." not in n]
        if not cand:
            cand = [n for n in names if n.endswith(".apk")]
        cand.sort(key=lambda n: z.getinfo(n).file_size, reverse=True)
        return z.read(cand[0]), cand[0]


def scan_apk(app: str, apk_bytes: bytes, outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    apk_path = outdir / "base.apk"
    apk_path.write_bytes(apk_bytes)
    print(f"\n{'='*70}\n{app}: base apk {len(apk_bytes)/1e6:.1f} MB\n{'='*70}")

    creds: set[str] = set()
    b64_creds: set[str] = set()
    hit_files: dict[str, list[str]] = {}
    bundle_bytes = None

    with zipfile.ZipFile(apk_path) as z:
        for info in z.infolist():
            data = z.read(info)
            if info.filename.endswith("index.android.bundle"):
                bundle_bytes = data
                (outdir / "index.android.bundle").write_bytes(data)
            hits = [n.decode() for n in NEEDLES if n in data]
            if hits:
                hit_files.setdefault(info.filename, []).extend(hits)
            for m in CRED_RE.findall(data):
                creds.add(m.decode(errors="replace"))
            # base64 forms that decode to a credential
            for m in B64_RE.findall(data):
                try:
                    dec = base64.b64decode(m + b"===")
                except Exception:
                    continue
                if b"-user:" in dec and b"ehg" in dec:
                    b64_creds.add(m.decode())

    print("Entries with OAuth needles:")
    for fn, hits in sorted(hit_files.items()):
        print(f"  {fn}: {sorted(set(hits))}")
    print("\nASCII credentials (client-id:secret) found:")
    for c in sorted(creds):
        print(f"  {c}")
    print("\nbase64 blobs decoding to a credential:")
    for c in sorted(b64_creds):
        try:
            dec = base64.b64decode(c + "===").decode(errors="replace")
        except Exception:
            dec = "<decode error>"
        same = " (== shipped default)" if c == KNOWN_B64 else ""
        print(f"  {c[:48]}...{same}\n     -> {dec}")
    if bundle_bytes:
        print(f"\nindex.android.bundle: {len(bundle_bytes)/1e6:.1f} MB -> saved")
    else:
        print("\nindex.android.bundle: NOT FOUND")


def main() -> None:
    print("Shipped default decodes to:")
    print("  " + base64.b64decode(KNOWN_B64 + "===").decode(errors="replace"))
    for app, xapk in APPS.items():
        if not xapk.exists():
            print(f"MISSING: {xapk}")
            continue
        apk_bytes, base_name = find_base_apk(xapk)
        print(f"\n[{app}] base split = {base_name}")
        scan_apk(app, apk_bytes, ANALYSIS / app)


if __name__ == "__main__":
    sys.exit(main())
