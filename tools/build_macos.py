"""Build the Apple Silicon release from this checkout on an Apple Silicon Mac."""

import hashlib
import os
from pathlib import Path
import platform
import plistlib
import runpy
import subprocess
import sys
from urllib.request import urlretrieve

import flet_desktop.version


ROOT = Path(__file__).resolve().parents[1]
FLET_CLIENT_SHA256 = "4a47451b17e42ac9997aaab170446520af80b96b07e02d7e247289972d4094e9"


def run(*command, env=None):
    subprocess.run(command, cwd=ROOT, check=True, env=env)


def main():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("Use an arm64 Python environment on an Apple Silicon Mac.")

    version = runpy.run_path(str(ROOT / "ui/constants.py"))["APP_VERSION"]
    work = ROOT / "build" / "macos-release"
    runtime_dir = work / "desktop-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    desktop = runtime_dir / "flet-macos.tar.gz"
    if not desktop.exists():
        flet_version = flet_desktop.version.version
        url = f"https://github.com/flet-dev/flet/releases/download/v{flet_version}/{desktop.name}"
        pending = work / f"{desktop.name}.download"
        urlretrieve(url, pending)
        pending.replace(desktop)
    with desktop.open("rb") as file:
        if hashlib.file_digest(file, "sha256").hexdigest() != FLET_CLIENT_SHA256:
            raise SystemExit(f"Flet desktop archive checksum mismatch: {desktop}")

    run(
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--onedir",
        "--name", "MiBudsClient", "--target-arch", "arm64",
        "--osx-bundle-identifier", "io.github.mcyrj.mibudsclient",
        "--icon", str(ROOT / "assets/icon.png"), "--workpath", str(work), "--specpath", str(work),
        "--paths", str(ROOT), "--add-data", f"{ROOT / 'assets'}:assets",
        "--add-data", f"{ROOT / 'LICENSE'}:.", "--add-data", f"{ROOT / 'NOTICE.md'}:.",
        "--add-data", f"{desktop}:flet_desktop/app", "--collect-data", "flet",
        "--hidden-import", "flet_desktop", "--hidden-import", "pystray._darwin",
        str(ROOT / "main.py"),
        # The optional Flet CLI hook must see the archive, not the live app cache.
        env={**os.environ, "FLET_VIEW_PATH": str(runtime_dir)},
    )

    app = ROOT / "dist" / "MiBudsClient.app"
    plist = app / "Contents" / "Info.plist"
    info = plistlib.loads(plist.read_bytes())
    info.update({
        "CFBundleShortVersionString": version.removeprefix("v"),
        "CFBundleVersion": version.removeprefix("v"),
        "LSUIElement": True,
        "NSBluetoothAlwaysUsageDescription": "MiBudsClient connects to your paired earbuds to read battery levels and change settings.",
    })
    plist.write_bytes(plistlib.dumps(info))
    run("codesign", "--force", "--sign", "-", str(app))
    run("codesign", "--verify", "--deep", "--strict", str(app))

    archive = app.with_name(f"MiBudsClient-{version}-macOS-arm64.zip")
    archive.unlink(missing_ok=True)
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive))
    print(archive)


if __name__ == "__main__":
    main()
