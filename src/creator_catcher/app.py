#!/usr/bin/env python3
"""Creator Catcher server and scheduled YouTube channel scanner."""

from __future__ import annotations

import argparse
import fcntl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit


VERSION = "0.2.0"
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
STATE_DIR = Path(os.environ.get("CREATOR_CATCHER_STATE_DIR", "/var/lib/creator-catcher"))
CONFIG_FILE = STATE_DIR / "config.json"
STATUS_FILE = STATE_DIR / "status.json"
ARCHIVE_FILE = STATE_DIR / "download-archive.txt"
LOCK_FILE = STATE_DIR / "scan.lock"
CONFIG_LOCK = threading.RLock()
VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}


def default_config() -> dict:
    return {
        "download_dir": str(STATE_DIR / "downloads"),
        "move_to_dir": "",
        "lookback_days": 2,
        "max_per_creator": 20,
        "max_height": 1080,
        "creators": [],
    }


def atomic_json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_config() -> dict:
    with CONFIG_LOCK:
        if not CONFIG_FILE.exists():
            config = default_config()
            atomic_json_write(CONFIG_FILE, config)
            return config
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read configuration: {exc}") from exc
        config = default_config()
        config.update(loaded)
        return validate_config(config)


def normalize_channel_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Enter a YouTube creator URL.")
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        raise ValueError("Use a youtube.com creator or channel URL.")
    path = parsed.path.rstrip("/")
    if not path or path == "/":
        raise ValueError("The URL must identify a creator or channel.")
    if not path.endswith(("/videos", "/shorts", "/streams")):
        path += "/videos"
    return urlunsplit(("https", "www.youtube.com", path, "", ""))


def validate_config(candidate: dict) -> dict:
    if not isinstance(candidate, dict):
        raise ValueError("Configuration must be a JSON object.")
    download_dir = Path(str(candidate.get("download_dir", ""))).expanduser()
    if not download_dir.is_absolute():
        raise ValueError("Download directory must be an absolute path.")
    move_to_value = str(candidate.get("move_to_dir", "")).strip()
    move_to_dir = Path(move_to_value).expanduser() if move_to_value else None
    if move_to_dir is not None:
        if not move_to_dir.is_absolute():
            raise ValueError("Move-to directory must be an absolute mounted path.")
        download_resolved = download_dir.resolve(strict=False)
        move_to_resolved = move_to_dir.resolve(strict=False)
        if (
            download_resolved == move_to_resolved
            or download_resolved in move_to_resolved.parents
            or move_to_resolved in download_resolved.parents
        ):
            raise ValueError("Download and move-to directories must not overlap.")
    lookback = int(candidate.get("lookback_days", 2))
    maximum = int(candidate.get("max_per_creator", 20))
    height = int(candidate.get("max_height", 1080))
    if not 1 <= lookback <= 30:
        raise ValueError("Lookback must be between 1 and 30 days.")
    if not 1 <= maximum <= 100:
        raise ValueError("Maximum videos must be between 1 and 100.")
    if height not in {480, 720, 1080, 1440, 2160}:
        raise ValueError("Resolution must be 480, 720, 1080, 1440, or 2160.")

    creators = candidate.get("creators", [])
    if not isinstance(creators, list) or len(creators) > 500:
        raise ValueError("Creators must be a list containing at most 500 entries.")
    normalized = []
    seen = set()
    for creator in creators:
        if not isinstance(creator, dict):
            raise ValueError("Each creator must be an object.")
        name = str(creator.get("name", "")).strip()
        if not name or len(name) > 120:
            raise ValueError("Each creator needs a name of at most 120 characters.")
        url = normalize_channel_url(str(creator.get("url", "")))
        if url in seen:
            raise ValueError(f"Duplicate creator URL: {url}")
        seen.add(url)
        normalized.append({"name": name, "url": url, "enabled": bool(creator.get("enabled", True))})

    return {
        "download_dir": str(download_dir),
        "move_to_dir": str(move_to_dir) if move_to_dir is not None else "",
        "lookback_days": lookback,
        "max_per_creator": maximum,
        "max_height": height,
        "creators": normalized,
    }


def save_config(candidate: dict) -> dict:
    config = validate_config(candidate)
    with CONFIG_LOCK:
        atomic_json_write(CONFIG_FILE, config)
    return config


def status_snapshot() -> dict:
    if not STATUS_FILE.exists():
        return {
            "state": "idle",
            "headline": "Ready",
            "detail": "Press Scan now to check for new videos.",
            "percent": None,
            "download_count": 0,
        }
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "error", "headline": "Status unavailable", "detail": "", "percent": None}


def update_status(**fields: object) -> dict:
    status = status_snapshot()
    status.update(fields)
    status["updated_at"] = int(time.time())
    atomic_json_write(STATUS_FILE, status)
    return status


def yt_dlp_executable() -> str:
    configured = os.environ.get("CREATOR_CATCHER_YTDLP", "/usr/lib/creator-catcher/vendor/yt-dlp")
    if Path(configured).is_file():
        return configured
    found = shutil.which("yt-dlp")
    if found:
        return found
    raise RuntimeError("yt-dlp is not installed.")


def scanner_command(config: dict, creator: dict) -> list[str]:
    height = config["max_height"]
    return [
        yt_dlp_executable(),
        "--ignore-errors",
        "--no-overwrites",
        "--no-color",
        "--download-archive",
        str(ARCHIVE_FILE),
        "--dateafter",
        f"now-{config['lookback_days']}days",
        "--playlist-end",
        str(config["max_per_creator"]),
        "--match-filters",
        "!is_live",
        "--format",
        f"bv*[height<={height}]+ba/b[height<={height}]/b",
        "--merge-output-format",
        "mp4",
        "--paths",
        config["download_dir"],
        "--output",
        "%(channel)s/%(upload_date)s - %(title).180B [%(id)s].%(ext)s",
        "--newline",
        "--progress",
        "--progress-delta",
        "0.5",
        "--progress-template",
        "download:CC_PROGRESS\t%(progress._percent_str)s\t%(progress._downloaded_bytes_str)s\t%(progress._total_bytes_str)s\t%(progress._speed_str)s\t%(progress._eta_str)s\t%(info.title)s",
        "--print",
        "before_dl:CC_DOWNLOAD\t%(title)s",
        "--print",
        "after_move:CC_COMPLETE\t%(title)s",
        creator["url"],
    ]


def parse_progress_line(line: str, download_count: int) -> int:
    fields = line.rstrip("\n").split("\t", 6)
    if fields[0] == "CC_DOWNLOAD" and len(fields) >= 2:
        update_status(
            state="downloading",
            headline=f"Downloading {fields[1]}",
            detail="Preparing download…",
            percent=0,
            download_count=download_count,
        )
    elif fields[0] == "CC_PROGRESS" and len(fields) >= 7:
        try:
            percent = max(0.0, min(100.0, float(fields[1].strip().rstrip("%"))))
        except ValueError:
            percent = 0.0
        details = []
        downloaded, total, speed, eta = fields[2:6]
        if downloaded not in {"NA", "N/A"} and total not in {"NA", "N/A"}:
            details.append(f"{downloaded} of {total}")
        if speed not in {"NA", "N/A"}:
            details.append(speed)
        if eta not in {"NA", "N/A"}:
            details.append(f"ETA {eta}")
        update_status(
            state="downloading",
            headline=f"Downloading {fields[6]}",
            detail=" • ".join(details),
            percent=percent,
            download_count=download_count,
        )
    elif fields[0] == "CC_COMPLETE" and len(fields) >= 2:
        download_count += 1
        noun = "video" if download_count == 1 else "videos"
        update_status(
            state="downloaded",
            headline=f"Saved {fields[1]}",
            detail=f"{download_count} {noun} downloaded in this scan.",
            percent=100,
            download_count=download_count,
        )
    return download_count


def move_pending_videos(config: dict) -> tuple[int, list[str]]:
    """Move completed local videos into the configured destination.

    Files retain their path relative to the download directory, which keeps
    creator folders intact. A temporary file and atomic rename prevent Plex
    from seeing a partially copied video on a network filesystem.
    """
    destination_value = config.get("move_to_dir", "")
    if not destination_value:
        return 0, []

    source_root = Path(config["download_dir"])
    destination_root = Path(destination_value)
    if not destination_root.is_dir():
        return 0, [f"move destination is unavailable: {destination_root}"]
    videos = sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in VIDEO_EXTENSIONS
    )
    moved = 0
    errors: list[str] = []
    for position, source in enumerate(videos, 1):
        relative = source.relative_to(source_root)
        target = destination_root / relative
        temporary = target.with_name(f".{target.name}.creator-catcher-part")
        update_status(
            state="moving",
            headline=f"Moving {source.name}",
            detail=f"Sending to {destination_root}…",
            percent=round((position - 1) * 100 / len(videos), 1),
            move_position=position,
            move_total=len(videos),
            moved_count=moved,
        )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(f"destination already exists: {target}")
            temporary.unlink(missing_ok=True)
            total_bytes = source.stat().st_size
            copied_bytes = 0
            with source.open("rb") as source_file, temporary.open("wb") as target_file:
                while chunk := source_file.read(4 * 1024 * 1024):
                    target_file.write(chunk)
                    copied_bytes += len(chunk)
                    update_status(
                        state="moving",
                        headline=f"Moving {source.name}",
                        detail=f"{copied_bytes / 1048576:.1f} of {total_bytes / 1048576:.1f} MiB",
                        percent=round(copied_bytes * 100 / total_bytes, 1) if total_bytes else 100,
                        move_position=position,
                        move_total=len(videos),
                        moved_count=moved,
                    )
                target_file.flush()
                os.fsync(target_file.fileno())
            try:
                shutil.copystat(source, temporary)
            except OSError:
                pass
            os.replace(temporary, target)
            source.unlink()
            moved += 1
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            errors.append(f"{relative}: {exc}")

    for directory in sorted(source_root.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass
    return moved, errors


def scan() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A scan is already running.", flush=True)
            return 0

        try:
            config = load_config()
            Path(config["download_dir"]).mkdir(parents=True, exist_ok=True)
            enabled = [creator for creator in config["creators"] if creator["enabled"]]
            if not enabled:
                update_status(
                    state="idle",
                    headline="No enabled creators",
                    detail="Add or enable a creator first.",
                    percent=None,
                    download_count=0,
                )
                return 0

            failures = 0
            download_count = 0
            for position, creator in enumerate(enabled, 1):
                update_status(
                    state="checking",
                    headline=f"Checking {creator['name']}",
                    detail="Looking for eligible new uploads…",
                    percent=None,
                    creator_position=position,
                    creator_total=len(enabled),
                    download_count=download_count,
                )
                process = subprocess.Popen(
                    scanner_command(config, creator),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert process.stdout is not None
                recent_warning = ""
                for line in process.stdout:
                    print(line, end="", flush=True)
                    download_count = parse_progress_line(line, download_count)
                    if line.startswith(("ERROR:", "WARNING:")):
                        recent_warning = line.strip()
                if process.wait() != 0:
                    failures += 1
                    update_status(
                        state="error",
                        headline=f"Could not scan {creator['name']}",
                        detail=recent_warning or "yt-dlp returned an error.",
                        percent=None,
                        download_count=download_count,
                    )

            moved_count, move_errors = move_pending_videos(config)
            noun = "video" if download_count == 1 else "videos"
            moved_noun = "video" if moved_count == 1 else "videos"
            if failures or move_errors:
                problem_count = failures + len(move_errors)
                error_detail = move_errors[0] if move_errors else f"{failures} creator checks failed."
                update_status(
                    state="error",
                    headline="Scan finished with errors",
                    detail=(
                        f"Downloaded {download_count} new {noun}; moved {moved_count} {moved_noun}. "
                        f"{problem_count} problem(s): {error_detail}"
                    ),
                    percent=None,
                    download_count=download_count,
                    moved_count=moved_count,
                )
                return 1
            update_status(
                state="complete",
                headline="Scan complete",
                detail=f"Downloaded {download_count} new {noun}; moved {moved_count} {moved_noun}.",
                percent=100,
                download_count=download_count,
                moved_count=moved_count,
            )
            return 0
        except Exception as exc:
            update_status(state="error", headline="Scan failed", detail=str(exc), percent=None)
            print(f"Creator Catcher: {exc}", file=sys.stderr)
            return 1


class RequestHandler(BaseHTTPRequestHandler):
    server_version = f"CreatorCatcher/{VERSION}"

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"{self.address_string()} - {format_string % args}", flush=True)

    def send_bytes(self, payload: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_bytes(json.dumps(value).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self.send_bytes((STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/app.css":
            self.send_bytes((STATIC_DIR / "app.css").read_bytes(), "text/css; charset=utf-8")
        elif path == "/app.js":
            self.send_bytes((STATIC_DIR / "app.js").read_bytes(), "text/javascript; charset=utf-8")
        elif path == "/api/config":
            self.send_json(load_config())
        elif path == "/api/status":
            self.send_json(status_snapshot())
        elif path == "/health":
            self.send_json({"ok": True, "version": VERSION})
        else:
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def read_json(self) -> object:
        if self.headers.get("X-Creator-Catcher") != "1":
            raise PermissionError("Missing request verification header.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid request length.") from exc
        if not 0 < length <= 1_000_000:
            raise ValueError("Request body is empty or too large.")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            body = self.read_json()
            if path == "/api/config":
                self.send_json(save_config(body))
            elif path == "/api/scan":
                if body != {}:
                    raise ValueError("Scan request must be an empty object.")
                subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "scan"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self.send_json({"started": True}, HTTPStatus.ACCEPTED)
            else:
                self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def serve() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    load_config()
    host = os.environ.get("CREATOR_CATCHER_HOST", "127.0.0.1")
    port = int(os.environ.get("CREATOR_CATCHER_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), RequestHandler)
    print(f"Creator Catcher {VERSION} listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def validate_installation() -> int:
    checks = {
        "state_directory": str(STATE_DIR),
        "yt_dlp": yt_dlp_executable(),
        "ffmpeg": shutil.which("ffmpeg"),
        "configuration": load_config(),
    }
    print(json.dumps(checks, indent=2))
    return 0 if checks["ffmpeg"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("command", choices=("serve", "scan", "validate"), nargs="?", default="serve")
    return parser.parse_args()


def main() -> int:
    command = parse_args().command
    if command == "scan":
        return scan()
    if command == "validate":
        return validate_installation()
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
