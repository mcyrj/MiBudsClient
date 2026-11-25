import json
import platform
import sys
import urllib.request
import urllib.error
import re
import threading
import time
from dataclasses import dataclass
from http.client import HTTPException
from typing import Callable, Optional, Tuple
from ui.constants import APP_VERSION, GITHUB_URL
from .user_preferences import should_show_update_notification


def start_update_check(on_update_available: Callable[[str], None]) -> threading.Thread:
    """Check once in the background and report an update allowed by preferences."""
    def perform_update_check() -> None:
        # Keep the existing startup delay before checking for updates.
        time.sleep(3)
        has_update, latest_ver = check_for_updates()
        if has_update and latest_ver and should_show_update_notification(latest_ver):
            on_update_available(latest_ver)

    worker = threading.Thread(target=perform_update_check, daemon=True)
    worker.start()
    return worker

def parse_version(version_str: str) -> Tuple[int, ...]:
    """
    Parses a version string like 'v1.2.3-alpha.4' into a comparable tuple.
    Returns (major, minor, patch, pre_release_type, pre_release_num).
    Pre-release types are mapped: alpha=1, beta=2, rc=3, final=4.
    """
    # Clean 'v' prefix and handle basic semantic versioning
    v = version_str.lstrip('v').lower()
    
    # Regex to capture major, minor, patch and optional pre-release info
    # Supports: 1.2.3, 1.2.3-alpha.1, 1.2.3-beta, 1.2.3-rc5, etc.
    match = re.match(r'^(\d+)\.(\d+)\.(\d+)(?:-?([a-zA-Z]+)(?:\.?(\d+))?)?$', v)
    
    if not match:
        # Fallback for non-standard versions: just extract all digit groups
        digits = re.findall(r'\d+', v)
        return tuple(map(int, digits)) + (4, 0)

    major, minor, patch, pre_type, pre_num = match.groups()
    
    pre_map = {
        'alpha': 1,
        'beta': 2,
        'rc': 3,
        None: 4
    }
    
    # Determine pre-release type value
    type_val = 4 # Default to final
    if pre_type:
        for key, val in pre_map.items():
            if key and pre_type.startswith(key):
                type_val = val
                break
    
    num_val = int(pre_num) if pre_num else 0
    
    return (int(major), int(minor), int(patch), type_val, num_val)

@dataclass(frozen=True)
class Release:
    version: str
    release_url: str
    download_url: str | None = None

    @property
    def is_newer(self) -> bool:
        return parse_version(self.version) > parse_version(APP_VERSION)


def fetch_latest_release() -> Release | None:
    """Read the latest stable release. None means GitHub has no stable release."""
    repository = GITHUB_URL.rstrip('/')
    owner, repo = repository.rsplit('/', 2)[-2:]
    request = urllib.request.Request(
        f'https://api.github.com/repos/{owner}/{repo}/releases/latest',
        headers={'User-Agent': 'MiBudsClient-Updater', 'Accept': 'application/vnd.github+json'},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            release = json.load(response)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            return None
        raise
    except HTTPException as exc:
        raise OSError('Could not read the GitHub release response') from exc

    if not isinstance(release, dict):
        raise ValueError('Invalid GitHub release response')
    if release.get('draft') or release.get('prerelease'):
        return None
    version = release.get('tag_name')
    if not isinstance(version, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', version):
        raise ValueError('Invalid stable release version')

    release_url = f'{repository}/releases/tag/{version}'
    download_url = None
    if sys.platform == 'darwin' and platform.machine() == 'arm64':
        package = f'MiBudsClient-{version}-macOS-arm64.zip'
        assets = release.get('assets')
        if isinstance(assets, list) and any(
            isinstance(asset, dict) and asset.get('name') == package
            and asset.get('state') == 'uploaded' for asset in assets
        ):
            download_url = f'{repository}/releases/download/{version}/{package}'
    return Release(version, release_url, download_url)


def check_for_updates() -> Tuple[bool, Optional[str]]:
    """Return a newer version for the automatic startup notification."""
    try:
        release = fetch_latest_release()
        if release and release.is_newer:
            return True, release.version
    except (OSError, ValueError) as exc:
        print(f'Error checking for updates: {exc}')
    return False, None
