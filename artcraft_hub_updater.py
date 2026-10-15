#!/usr/bin/env python3
"""
ArtCraft Hub updater.

What this script does:
  1. Creates/verifies artcraft-hub/ and all 13 app subfolders.
  2. NEVER writes to, replaces, or deletes artcraft-hub/index.html (the launcher).
  3. Checks each GitHub repository for its latest release web ZIP.
  4. Compares the release tag with the installed version recorded per app.
  5. If already current, reports UP_TO_DATE without downloading.
  6. If newer (or the installed version is unknown), downloads with curl progress,
     verifies the ZIP, stages the replacement, and safely swaps the app folder.
  7. Keeps the previous app folder if download/verification/extraction fails.
  8. Prints detailed progress and writes a timestamped log file.

The first run after upgrading from the old downloader refreshes installed apps once,
because the old script did not record their installed release versions.

Requirements: Python 3.9+; no third-party packages required.

IMPORTANT:
  - The default GitHub owner is "storytold", based on the earlier setup.
  - Verify/change GITHUB_OWNER below if the repositories belong to a different owner.
  - This script only downloads ZIPs attached to GitHub Releases. It does not guess
    branch ZIPs or clone source repositories.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


# ---------------------------------------------------------------------------
# SETTINGS — edit these if needed
# ---------------------------------------------------------------------------

GITHUB_OWNER = "storytold"

# The script creates this folder in the directory from which you run it.
PROJECT_DIR_NAME = "artcraft-hub"

APP_NAMES = [
    "photocraft",
    "vectorcraft",
    "filmcraft",
    "lightcraft",
    "pdfcraft",
    "effectcraft",
    "designcraft",
    "figcraft",
    "wordcraft",
    "soundcraft",
    "deckcraft",
    "cadcraft",
    "gridcraft",
]

# Number of recent GitHub releases to inspect for each app.
RELEASES_TO_CHECK = 10

# A release asset qualifies if its filename ends in .zip and includes "web".
# Exact "web.zip" is preferred.
ZIP_MUST_CONTAIN = "web"

# Version metadata stored inside each app folder by this updater.
VERSION_METADATA_FILE = ".artcraft-release.json"

# Network timeouts in seconds.
API_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 180

# Retry transient network errors this many times.
MAX_RETRIES = 3

# Optional GitHub token for higher API rate limits.
# Recommended: set GITHUB_TOKEN in your shell; do not paste a token into this file.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")
LOG_FILE_NAME = f"artcraft-download-{RUN_ID}.log"


def configure_logging(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("artcraft_downloader")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


# Logger is initialized in main() after the project folder is created.
LOG: logging.Logger | None = None


def log(level: str, message: str, *args: Any) -> None:
    if LOG is None:
        print(message % args if args else message)
        return
    getattr(LOG, level.lower())(message, *args)


# ---------------------------------------------------------------------------
# HTTP / GITHUB API
# ---------------------------------------------------------------------------

def make_headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "User-Agent": "ArtCraft-Hub-Setup-Script/1.0",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def request_bytes(url: str, timeout: int, accept: str) -> bytes:
    """Fetch a small response such as GitHub API JSON, with visible retries."""
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        log("info", "HTTP request attempt %d/%d: %s", attempt, MAX_RETRIES, url)
        request = urllib.request.Request(url, headers=make_headers(accept))

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", "unknown")
                content_length = response.headers.get("Content-Length", "not provided")
                log(
                    "info",
                    "HTTP response: status=%s, content-length=%s, final-url=%s",
                    status,
                    content_length,
                    response.geturl(),
                )
                data = response.read()
                log("debug", "Read %d bytes from %s", len(data), url)
                return data

        except urllib.error.HTTPError as exc:
            last_error = exc
            body = ""
            try:
                body = exc.read(1000).decode("utf-8", errors="replace")
            except Exception:
                pass

            if exc.code in (400, 401, 403, 404, 410, 422):
                log(
                    "error",
                    "HTTP %s for %s. Response detail: %s",
                    exc.code,
                    url,
                    body[:700] or "(no response body)",
                )
                raise

            log(
                "warning",
                "HTTP %s on attempt %d: %s | detail=%s",
                exc.code,
                attempt,
                url,
                body[:300] or "(no response body)",
            )

        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = exc
            log(
                "warning",
                "Network error on attempt %d/%d for %s: %r",
                attempt,
                MAX_RETRIES,
                url,
                exc,
            )

        if attempt < MAX_RETRIES:
            delay = attempt * 2
            log("info", "Waiting %d seconds before retry...", delay)
            time.sleep(delay)

    assert last_error is not None
    raise last_error


def download_to_file(url: str, destination: Path, timeout: int) -> int:
    """
    Download with macOS curl so the terminal shows a live progress bar.
    Python still handles GitHub API lookups and ZIP extraction.
    """
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("curl was not found. macOS normally includes curl.")

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        log("info", "Download attempt %d/%d using curl: %s", attempt, MAX_RETRIES, url)
        try:
            if destination.exists():
                destination.unlink()

            # curl writes its progress bar directly to the terminal (stderr).
            # --fail prevents an HTTP error page from being saved as a ZIP.
            command = [
                curl,
                "--location",
                "--fail",
                "--show-error",
                "--progress-bar",
                "--connect-timeout", "30",
                "--max-time", str(timeout),
                "--output", str(destination),
                url,
            ]
            result = subprocess.run(command, check=False)

            if result.returncode != 0:
                raise RuntimeError(f"curl exited with status {result.returncode}")

            size = destination.stat().st_size if destination.exists() else 0
            if size <= 0:
                raise IOError("curl reported success but the downloaded file is empty")

            log("info", "Download finished: %s bytes saved to %s", f"{size:,}", destination)
            return size

        except (OSError, RuntimeError, IOError) as exc:
            last_error = exc
            log("warning", "Download error on attempt %d/%d: %s", attempt, MAX_RETRIES, exc)
            try:
                if destination.exists():
                    destination.unlink()
            except OSError:
                log("warning", "Could not remove partial download: %s", destination)

            if attempt < MAX_RETRIES:
                delay = attempt * 2
                log("info", "Retrying download in %d seconds...", delay)
                time.sleep(delay)

    assert last_error is not None
    raise last_error


def get_json(url: str) -> Any:
    log("info", "Requesting GitHub API JSON...")
    raw = request_bytes(url, API_TIMEOUT_SECONDS, "application/vnd.github+json")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        log("error", "GitHub API response was not valid JSON: %r", exc)
        raise

    log("debug", "GitHub API JSON parsed successfully (%s)", type(result).__name__)
    return result


def github_repo_url(app: str) -> str:
    owner = urllib.parse.quote(GITHUB_OWNER, safe="")
    repo = urllib.parse.quote(app, safe="")
    return f"https://api.github.com/repos/{owner}/{repo}"


def list_recent_releases(app: str) -> list[dict[str, Any]]:
    repo_api = github_repo_url(app)
    releases_api = (
        f"{repo_api}/releases?per_page={RELEASES_TO_CHECK}"
    )

    log("info", "Repository assumed for %s: %s/%s", app, GITHUB_OWNER, app)
    log("info", "Checking repository metadata: %s", repo_api)

    # Check repository separately so a 404 can be distinguished from no releases.
    repo_data = get_json(repo_api)
    log(
        "info",
        "Repository found: %s | default branch=%s | visibility=%s",
        repo_data.get("full_name", f"{GITHUB_OWNER}/{app}"),
        repo_data.get("default_branch", "unknown"),
        "private" if repo_data.get("private") else "public",
    )

    log("info", "Checking up to %d recent releases: %s", RELEASES_TO_CHECK, releases_api)
    releases = get_json(releases_api)

    if not isinstance(releases, list):
        raise ValueError(
            "GitHub releases endpoint did not return a list. "
            f"Response type was {type(releases).__name__}."
        )

    log("info", "GitHub returned %d release(s).", len(releases))
    for index, release in enumerate(releases, start=1):
        log(
            "debug",
            "Release %d: tag=%s name=%s draft=%s prerelease=%s",
            index,
            release.get("tag_name"),
            release.get("name"),
            release.get("draft"),
            release.get("prerelease"),
        )
    return releases


# ---------------------------------------------------------------------------
# RELEASE ASSET SELECTION
# ---------------------------------------------------------------------------

def choose_web_zip(
    releases: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """
    Select the newest release that has a web ZIP. Within that release, prefer
    an asset named exactly web.zip; otherwise use its first matching web ZIP.
    Never prefer an older release merely because its asset has a shorter name.
    """
    for release in releases:
        release_name = release.get("name") or release.get("tag_name") or "(unnamed release)"
        assets = release.get("assets", [])
        log("info", "Release %r contains %d asset(s).", release_name, len(assets))
        candidates: list[dict[str, Any]] = []

        for asset in assets:
            name = str(asset.get("name", ""))
            size = asset.get("size", "unknown")
            url = asset.get("browser_download_url", "")
            log("debug", "  Asset: name=%r size=%s URL=%s", name, size, url)

            if name.lower().endswith(".zip") and ZIP_MUST_CONTAIN in name.lower() and url:
                candidates.append(asset)
                log("info", "  Candidate web ZIP found: %s (%s bytes)", name, size)

        if candidates:
            for asset in candidates:
                if str(asset.get("name", "")).lower() == "web.zip":
                    return release, asset
            return release, candidates[0]

    return None, None


# ---------------------------------------------------------------------------
# SAFE EXTRACTION
# ---------------------------------------------------------------------------

def normalized_zip_path(name: str) -> PurePosixPath:
    # ZIP paths use forward slashes even on macOS.
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or any(part in ("..", "") for part in path.parts):
        raise ValueError(f"Unsafe path inside ZIP: {name!r}")
    if path.parts and ":" in path.parts[0]:
        raise ValueError(f"Suspicious drive-like path inside ZIP: {name!r}")
    return path


def find_common_wrapper(files: list[PurePosixPath]) -> str | None:
    """
    If every file is under the same single top-level directory, return its name.
    Example: web/index.html + web/assets/app.js => strip 'web/'.
    If files exist at the ZIP root or have different roots, strip nothing.
    """
    if not files:
        return None

    roots = {path.parts[0] for path in files if path.parts}
    if len(roots) != 1:
        return None

    root = next(iter(roots))
    if any(len(path.parts) < 2 for path in files):
        return None

    return root


def safe_extract(zip_path: Path, destination: Path) -> tuple[int, int]:
    """
    Extract without allowing ZIP path traversal.
    If the archive has one wrapper directory, strip that directory so the app's
    index.html lands directly inside its own app folder.
    Existing files inside that app folder may be overwritten; the root launcher
    is safe because extraction destination is always an app subfolder.
    """
    destination = destination.resolve()
    log("info", "Opening ZIP archive: %s", zip_path)
    log("info", "Extraction destination: %s", destination)

    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()
        log("info", "ZIP contains %d entries.", len(members))

        file_paths: list[PurePosixPath] = []
        member_paths: dict[str, PurePosixPath] = {}

        for member in members:
            relative = normalized_zip_path(member.filename)
            member_paths[member.filename] = relative
            if not member.is_dir():
                file_paths.append(relative)

        wrapper = find_common_wrapper(file_paths)
        if wrapper:
            log("info", "Common top-level folder detected: %r (will strip it).", wrapper)
        else:
            log("info", "No single wrapper folder detected; preserving archive paths.")

        extracted_files = 0
        extracted_dirs = 0
        skipped_wrapper_dirs = 0

        for number, member in enumerate(members, start=1):
            relative = member_paths[member.filename]
            log(
                "info",
                "ZIP entry %d/%d: %s%s",
                number,
                len(members),
                member.filename,
                " [directory]" if member.is_dir() else "",
            )

            if wrapper and relative.parts and relative.parts[0] == wrapper:
                relative = PurePosixPath(*relative.parts[1:])
                if not relative.parts:
                    skipped_wrapper_dirs += 1
                    log("debug", "Skipping wrapper directory entry.")
                    continue

            target = (destination / Path(*relative.parts)).resolve()

            if target != destination and destination not in target.parents:
                raise ValueError(
                    f"Archive path escapes destination after normalization: {member.filename!r}"
                )

            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                extracted_dirs += 1
                log("debug", "Created/verified directory: %s", target)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)

            # Do not create filesystem symlinks from archive metadata. Treat each
            # entry as a regular file and write its bytes to a confined target path.
            log("info", "Writing file: %s", target)
            with archive.open(member, "r") as source, open(target, "wb") as output:
                shutil.copyfileobj(source, output)

            extracted_files += 1
            log("debug", "Finished writing %s (%d bytes).", target, target.stat().st_size)

        log(
            "info",
            "Extraction summary: %d file(s), %d directory entr(ies), %d wrapper entr(ies) skipped.",
            extracted_files,
            extracted_dirs,
            skipped_wrapper_dirs,
        )

    return extracted_files, extracted_dirs


# ---------------------------------------------------------------------------
# PER-APP PROCESSING
# ---------------------------------------------------------------------------

def read_installed_version(app_dir: Path) -> str | None:
    """Read the release tag saved by this updater; legacy installs have no tag."""
    metadata_path = app_dir / VERSION_METADATA_FILE
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        tag = metadata.get("tag")
        return str(tag) if tag else None
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return None


def write_version_metadata(app_dir: Path, app: str, release: dict[str, Any], asset: dict[str, Any]) -> None:
    metadata = {
        "tag": release.get("tag_name", "unknown"),
        "release_name": release.get("name") or "",
        "asset": asset.get("name") or "",
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "repository": f"{GITHUB_OWNER}/{app}",
    }
    (app_dir / VERSION_METADATA_FILE).write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )


def process_app(project_dir: Path, app: str) -> dict[str, Any]:
    result: dict[str, Any] = {"app": app, "status": "FAILED", "files": 0, "error": ""}
    app_dir = project_dir / app
    stage_dir = project_dir / f".{app}.update-staging-{RUN_ID}"
    backup_dir = project_dir / f".{app}.update-backup-{RUN_ID}"

    log("info", "")
    log("info", "#" * 76)
    log("info", "UPDATE APP: %s", app)
    log("info", "#" * 76)

    try:
        app_dir.mkdir(parents=True, exist_ok=True)
        installed_tag = read_installed_version(app_dir)
        has_entry_point = (app_dir / "index.html").is_file()

        log("info", "Installed app folder: %s", app_dir)
        log("info", "Installed release tag: %s", installed_tag or "unknown (legacy install)")
        log("info", "App entry point present: %s", "yes" if has_entry_point else "no")

        log("info", "Checking GitHub for %s/%s...", GITHUB_OWNER, app)
        releases = list_recent_releases(app)
        release, asset = choose_web_zip(releases)

        if not asset or not release:
            result["status"] = "SKIPPED"
            result["error"] = (
                f"No .zip asset with {ZIP_MUST_CONTAIN!r} in its filename "
                f"found in the latest {RELEASES_TO_CHECK} releases."
            )
            log("error", result["error"])
            return result

        latest_tag = str(release.get("tag_name") or "unknown")
        log(
            "info",
            "Latest release: %s | tag=%s | asset=%s | size=%s bytes",
            release.get("name") or "(no release name)",
            latest_tag,
            asset.get("name"),
            asset.get("size", "unknown"),
        )

        if installed_tag == latest_tag and has_entry_point:
            result["status"] = "UP_TO_DATE"
            log("info", "%s is already at %s; no download needed.", app, latest_tag)
            return result

        if installed_tag is None:
            log("info", "Installed version is unknown (legacy install); refreshing once.")
        elif installed_tag != latest_tag:
            log("info", "Update available: %s -> %s", installed_tag, latest_tag)
        else:
            log("warning", "Tag matches but index.html is missing; repairing installation.")

        download_url = asset["browser_download_url"]

        # Stage inside the project folder so renames occur on the same filesystem.
        with tempfile.TemporaryDirectory(prefix=f".artcraft-{app}-update-", dir=project_dir) as temp_name:
            temp_dir = Path(temp_name)
            zip_path = temp_dir / Path(asset.get("name") or f"{app}-web.zip").name
            stage_dir.mkdir(parents=True, exist_ok=False)

            log("info", "Downloading release ZIP to: %s", zip_path)
            log("info", "Download URL: %s", download_url)
            download_to_file(download_url, zip_path, DOWNLOAD_TIMEOUT_SECONDS)

            log("info", "Verifying ZIP integrity...")
            if not zipfile.is_zipfile(zip_path):
                raise zipfile.BadZipFile(
                    "Downloaded file is not a ZIP archive; refusing to replace installed app."
                )
            with zipfile.ZipFile(zip_path) as archive:
                bad_member = archive.testzip()
                if bad_member:
                    raise zipfile.BadZipFile(f"ZIP integrity check failed at entry: {bad_member}")
                log("info", "ZIP integrity check passed (%d entries).", len(archive.infolist()))

            log("info", "Extracting into staging folder: %s", stage_dir)
            extracted_files, _ = safe_extract(zip_path, stage_dir)
            result["files"] = extracted_files

            if not (stage_dir / "index.html").is_file():
                raise ValueError(
                    "Extracted release has no root index.html; refusing to replace installed app."
                )

            write_version_metadata(stage_dir, app, release, asset)

            # Only replace the existing folder after download, ZIP validation, and
            # extraction succeed. Restore the old folder if the swap fails.
            moved_old_app = False
            try:
                if app_dir.exists():
                    if backup_dir.exists():
                        shutil.rmtree(backup_dir)
                    os.replace(app_dir, backup_dir)
                    moved_old_app = True
                os.replace(stage_dir, app_dir)
            except Exception:
                if moved_old_app and backup_dir.exists() and not app_dir.exists():
                    os.replace(backup_dir, app_dir)
                raise

            if backup_dir.exists():
                shutil.rmtree(backup_dir)

        result["status"] = "UPDATED" if installed_tag else "INSTALLED/REFRESHED"
        log("info", "SUCCESS: %s now runs release %s; extracted files=%d",
            app, latest_tag, result["files"])
        return result

    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            message = (
                f"GitHub returned 404 for {GITHUB_OWNER}/{app}. "
                "The repo name/owner may be wrong, or the repo may be private."
            )
        elif exc.code == 403:
            message = (
                "GitHub returned 403. This can mean API rate limiting or missing access "
                "to a private repository. Check the log and consider setting GITHUB_TOKEN."
            )
        elif exc.code == 401:
            message = "GitHub returned 401 (unauthorized). Check repository access/token."
        else:
            message = f"GitHub HTTP error {exc.code}: {exc.reason}"
        result["error"] = message
        log("exception", "APP UPDATE FAILED: %s | %s", app, message)
        return result
    except zipfile.BadZipFile as exc:
        result["error"] = f"Bad ZIP: {exc}"
        log("exception", "APP UPDATE FAILED: %s | %s", app, result["error"])
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        log("exception", "APP UPDATE FAILED: %s | %s", app, result["error"])
        return result
    finally:
        if stage_dir.exists():
            try:
                shutil.rmtree(stage_dir)
            except OSError as exc:
                log("warning", "Could not remove staging folder %s: %s", stage_dir, exc)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    global LOG

    start = time.time()
    starting_dir = Path.cwd().resolve()
    project_dir = starting_dir / PROJECT_DIR_NAME

    # If the user already ran the command from inside artcraft-hub, don't create
    # artcraft-hub/artcraft-hub by accident.
    if starting_dir.name == PROJECT_DIR_NAME:
        project_dir = starting_dir

    project_dir.mkdir(parents=True, exist_ok=True)

    log_path = project_dir / LOG_FILE_NAME
    LOG = configure_logging(log_path)

    log("info", "=" * 88)
    log("info", "ARTCRAFT HUB — VERSION-AWARE UPDATER")
    log("info", "=" * 88)
    log("info", "Start time: %s", datetime.now().astimezone().isoformat(timespec="seconds"))
    log("info", "Current working directory: %s", starting_dir)
    log("info", "Project root: %s", project_dir)
    log("info", "GitHub owner: %s", GITHUB_OWNER)
    log("info", "Apps to process: %d", len(APP_NAMES))
    log("info", "Detailed log file: %s", log_path)
    log("info", "Python version: %s", sys.version.replace("\n", " "))
    log("info", "GitHub token configured: %s", "yes" if GITHUB_TOKEN else "no")
    log("info", "")
    log("warning", "The root launcher index.html will NOT be touched by this script.")
    log("warning", "App ZIPs are extracted only into their own subfolders.")
    log("warning", "When an update is needed, each app folder is replaced by verified release contents.")
    log("info", "")

    # Create every folder first, so the full layout exists even if some downloads fail.
    log("info", "PHASE 1: Creating/verifying all app folders...")
    folder_errors = 0
    for index, app in enumerate(APP_NAMES, start=1):
        app_dir = project_dir / app
        try:
            app_dir.mkdir(parents=True, exist_ok=True)
            log("info", "[%02d/%02d] Folder ready: %s", index, len(APP_NAMES), app_dir)
        except Exception:
            folder_errors += 1
            log("exception", "Could not create folder: %s", app_dir)

    launcher = project_dir / "index.html"
    if launcher.exists():
        log("info", "Launcher exists and will be preserved: %s", launcher)
    else:
        log(
            "warning",
            "No root launcher index.html currently exists at %s.",
            launcher,
        )
        log(
            "warning",
            "This script will not create the launcher; put your custom index.html here.",
        )

    log("info", "")
    log("info", "PHASE 2: Checking versions and updating apps when needed...")
    results: list[dict[str, Any]] = []

    for index, app in enumerate(APP_NAMES, start=1):
        log("info", "")
        log("info", "OVERALL PROGRESS: app %d of %d", index, len(APP_NAMES))
        result = process_app(project_dir, app)
        results.append(result)

    log("info", "")
    log("info", "=" * 88)
    log("info", "FINAL SUMMARY")
    log("info", "=" * 88)

    counts: dict[str, int] = {}
    for result in results:
        status = result["status"]
        counts[status] = counts.get(status, 0) + 1
        log(
            "info",
            "%-24s status=%-22s files=%-6s %s",
            result["app"],
            status,
            result["files"],
            result["error"],
        )

    elapsed = time.time() - start
    log("info", "")
    log("info", "Folder creation errors: %d", folder_errors)
    log("info", "Status counts: %s", counts)
    log("info", "Elapsed time: %.1f seconds", elapsed)
    log("info", "Log file: %s", log_path)
    log("info", "Project folder: %s", project_dir)
    log("info", "Launcher preserved: %s", launcher)
    log("info", "Finished. Review UP_TO_DATE / UPDATED / FAILED entries above.")

    failures = sum(
        1 for result in results
        if result["status"] in ("FAILED", "SKIPPED")
    )
    return 1 if failures or folder_errors else 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except KeyboardInterrupt:
        log("warning", "Interrupted by user (Ctrl+C). Any completed app folders remain on disk.")
        exit_code = 130
    except Exception as fatal:
        log("exception", "FATAL SCRIPT ERROR: %s: %s", type(fatal).__name__, fatal)
        exit_code = 2
    sys.exit(exit_code)
