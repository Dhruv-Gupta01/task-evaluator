"""Install the dockerfile-pin CLI that the vendored check-image-digests.sh needs.

    uv run python scripts/install_dockerfile_pin.py [--archive FILE]

Downloads the same version upstream CI uses (v1.5.0, github.com/azu/dockerfile-pin),
checks it against the release sha256 below and puts it in backend/.tools/bin/, which
tb_static_checks adds to PATH. Run it once per machine. On a network that cuts off
GitHub's release CDN, download the archive another way and pass it with --archive;
it is checked against the same sha256.
"""

import argparse
import hashlib
import io
import platform
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "v1.5.0"
SHA256 = {
    "darwin_amd64.tar.gz": "b8e3db0418aae0508af08fb99b9706f6ce73edba34ebc274829d523173e20c94",
    "darwin_arm64.tar.gz": "8e2dfd82c2eedeaf18ed8c9f833a51dce2f7f374906ba45e81515fc620d3890e",
    "linux_amd64.tar.gz": "88845bfbe4918596f00eb77ebeec8a0826026c9b24bcc83053cae48402e95ea6",
    "linux_arm64.tar.gz": "fc039f7aa19ba712cb419667d930e4a5052f33b5b6c9c408b1159c7adf585a36",
    "windows_amd64.zip": "6afcddc663062e1c873e485d2a2d7df83a74ae7a7821bf2a5eb27c27b0264827",
    "windows_arm64.zip": "9bc6a46426fe64cada7dae88629a7de2f849fb5146519817ae479c39fc0506fc",
}
TOOLS_BIN = Path(__file__).resolve().parents[1] / ".tools" / "bin"


def asset_name() -> str:
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}[platform.system()]
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    return f"{system}_{arch}." + ("zip" if system == "windows" else "tar.gz")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, help="an already downloaded release archive for this machine")
    args = ap.parse_args()
    asset = asset_name()
    url = f"https://github.com/azu/dockerfile-pin/releases/download/{VERSION}/dockerfile-pin_{asset}"
    if args.archive:
        data = args.archive.read_bytes()
    else:
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256[asset]:
        print(f"sha256 mismatch for {asset}: got {digest}, expected {SHA256[asset]}")
        return 1
    exe = "dockerfile-pin.exe" if asset.endswith(".zip") else "dockerfile-pin"
    if asset.endswith(".zip"):
        binary = zipfile.ZipFile(io.BytesIO(data)).read(exe)
    else:
        binary = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz").extractfile(exe).read()
    TOOLS_BIN.mkdir(parents=True, exist_ok=True)
    target = TOOLS_BIN / exe
    target.write_bytes(binary)
    target.chmod(0o755)
    print(f"installed dockerfile-pin {VERSION} ({asset}, sha256 ok) to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
