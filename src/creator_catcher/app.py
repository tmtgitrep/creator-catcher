#!/usr/bin/env python3
"""Creator Catcher server and scheduled YouTube channel scanner."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time as datetime_time, timedelta
import fcntl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit


VERSION = "0.7.0"
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
STATE_DIR = Path(os.environ.get("CREATOR_CATCHER_STATE_DIR", "/var/lib/creator-catcher"))
CONFIG_FILE = STATE_DIR / "config.json"
STATUS_FILE = STATE_DIR / "status.json"
STATS_FILE = STATE_DIR / "stats.json"
ARCHIVE_FILE = STATE_DIR / "download-archive.txt"
LOCK_FILE = STATE_DIR / "scan.lock"
SCHEDULE_FILE = STATE_DIR / "schedule.json"
VIDEO_LOG_FILE = STATE_DIR / "video-log.json"
CONFIG_LOCK = threading.RLock()
STATS_LOCK = threading.RLock()
VIDEO_LOG_LOCK = threading.RLock()
VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
MAX_ERRORS = 100
MAX_VIDEO_LOG_ENTRIES = 500


def default_config() -> dict:
    return {
        "download_dir": str(STATE_DIR / "downloads"),
        "move_to_dir": "",
        "lookback_days": 2,
        "max_per_creator": 20,
        "max_height": 1080,
        "automatic_scans_enabled": True,
        "automatic_scan_interval_days": 1,
        "automatic_scan_time": "04:00",
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


def normalize_video_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Paste a YouTube video link.")
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    host = parsed.netloc.lower().split(":", 1)[0]
    video_id = ""
    path_parts = [part for part in parsed.path.split("/") if part]
    if host in {"youtu.be", "www.youtu.be"} and path_parts:
        video_id = path_parts[0]
    elif host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }:
        if parsed.path.rstrip("/") == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif len(path_parts) == 2 and path_parts[0] in {"embed", "live", "shorts"}:
            video_id = path_parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Enter a valid YouTube watch, short, live, or youtu.be video link.")
    return f"https://www.youtube.com/watch?v={video_id}"


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
    automatic_scans_enabled = bool(candidate.get("automatic_scans_enabled", True))
    automatic_scan_interval_days = int(candidate.get("automatic_scan_interval_days", 1))
    automatic_scan_time = str(candidate.get("automatic_scan_time", "04:00")).strip()
    if not 1 <= lookback <= 30:
        raise ValueError("Lookback must be between 1 and 30 days.")
    if not 1 <= maximum <= 100:
        raise ValueError("Maximum videos must be between 1 and 100.")
    if height not in {480, 720, 1080, 1440, 2160}:
        raise ValueError("Resolution must be 480, 720, 1080, 1440, or 2160.")
    if not 1 <= automatic_scan_interval_days <= 30:
        raise ValueError("Automatic scan frequency must be between 1 and 30 days.")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", automatic_scan_time):
        raise ValueError("Automatic scan time must use 24-hour HH:MM format.")

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
        "automatic_scans_enabled": automatic_scans_enabled,
        "automatic_scan_interval_days": automatic_scan_interval_days,
        "automatic_scan_time": automatic_scan_time,
        "creators": normalized,
    }


def save_config(candidate: dict) -> dict:
    config = validate_config(candidate)
    with CONFIG_LOCK:
        atomic_json_write(CONFIG_FILE, config)
    return config


def load_stats() -> dict:
    with STATS_LOCK:
        if not STATS_FILE.exists():
            return {"creators": {}}
        try:
            loaded = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read download statistics: {exc}") from exc
        creators = loaded.get("creators", {}) if isinstance(loaded, dict) else {}
        if not isinstance(creators, dict):
            raise RuntimeError("Cannot read download statistics: creators must be an object.")
        normalized = {}
        for url, values in creators.items():
            if not isinstance(values, dict):
                continue
            normalized[str(url)] = {
                "downloaded": max(0, int(values.get("downloaded", 0))),
                "last_scan": max(0, int(values.get("last_scan", 0))),
            }
        return {"creators": normalized}


def save_stats(stats: dict) -> None:
    with STATS_LOCK:
        atomic_json_write(STATS_FILE, stats)


def begin_scan_stats(config: dict) -> None:
    with STATS_LOCK:
        stats = load_stats()
        for creator in config["creators"]:
            entry = stats["creators"].setdefault(creator["url"], {"downloaded": 0, "last_scan": 0})
            entry["last_scan"] = 0
        save_stats(stats)


def record_creator_downloads(url: str, count: int) -> None:
    count = max(0, int(count))
    with STATS_LOCK:
        stats = load_stats()
        entry = stats["creators"].setdefault(url, {"downloaded": 0, "last_scan": 0})
        entry["downloaded"] += count
        entry["last_scan"] += count
        save_stats(stats)


def load_video_log() -> list[dict]:
    with VIDEO_LOG_LOCK:
        if not VIDEO_LOG_FILE.exists():
            return []
        try:
            loaded = json.loads(VIDEO_LOG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = loaded.get("entries", []) if isinstance(loaded, dict) else []
        return [entry for entry in entries if isinstance(entry, dict)][
            :MAX_VIDEO_LOG_ENTRIES
        ]


def save_video_log(entries: list[dict]) -> None:
    atomic_json_write(VIDEO_LOG_FILE, {"entries": entries[:MAX_VIDEO_LOG_ENTRIES]})


def update_video_log(video_id: str, **fields: object) -> dict:
    """Update one video record without allowing log I/O to stop a scan."""
    with VIDEO_LOG_LOCK:
        entries = load_video_log()
        entry = next((item for item in entries if item.get("video_id") == video_id), None)
        if entry is None:
            entry = {
                "video_id": video_id,
                "creator": "Unknown creator",
                "title": video_id,
                "download_status": "pending",
                "transfer_status": "pending",
            }
            entries.insert(0, entry)
        else:
            entries.remove(entry)
            entries.insert(0, entry)
        entry.update(fields)
        try:
            save_video_log(entries)
        except OSError as exc:
            print(f"Creator Catcher: cannot update video log: {exc}", file=sys.stderr)
        return entry


def record_video_attempt(video_id: str, creator: str, title: str) -> None:
    update_video_log(
        video_id,
        creator=creator or "Unknown creator",
        title=title or video_id,
        attempted_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        download_status="pending",
        download_error="",
        transfer_status="pending",
        transfer_error="",
    )


def record_video_download(video_id: str, filepath: str, move_enabled: bool) -> str:
    entry = update_video_log(
        video_id,
        source_path=filepath,
        downloaded_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        download_status="success",
        download_error="",
        transfer_status="pending" if move_enabled else "not_requested",
        transfer_error="",
    )
    return str(entry.get("title", video_id))


def video_id_from_path(path: Path) -> str:
    match = re.search(r"\[([A-Za-z0-9_-]{11})\](?=\.[^.]+$)", path.name)
    return match.group(1) if match else ""


def record_video_transfer(source: Path, success: bool, error: str = "") -> None:
    video_id = video_id_from_path(source)
    if not video_id:
        return
    fields: dict[str, object] = {
        "transfer_status": "success" if success else "failed",
        "transfer_error": "" if success else error[:2000],
    }
    if success:
        fields["transferred_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if not any(entry.get("video_id") == video_id for entry in load_video_log()):
        try:
            downloaded_at = datetime.fromtimestamp(source.stat().st_mtime).astimezone()
        except OSError:
            downloaded_at = datetime.now().astimezone()
        fields.update(
            creator=source.parent.name or "Unknown creator",
            title=source.stem,
            source_path=str(source),
            downloaded_at=downloaded_at.isoformat(timespec="seconds"),
            download_status="success",
            download_error="",
        )
    update_video_log(video_id, **fields)


def mark_video_download_failures(video_ids: set[str], error: str) -> None:
    for video_id in video_ids:
        entry = next(
            (item for item in load_video_log() if item.get("video_id") == video_id), None
        )
        if entry and entry.get("download_status") == "pending":
            update_video_log(
                video_id,
                download_status="failed",
                download_error=(error or "Download did not complete.")[:2000],
                transfer_status="not_requested",
            )


def config_for_api(config: dict | None = None) -> dict:
    config = config or load_config()
    stats = load_stats()["creators"]
    return {
        **config,
        "creators": [
            {
                **creator,
                "download_count": stats.get(creator["url"], {}).get("downloaded", 0),
                "last_scan_download_count": stats.get(creator["url"], {}).get("last_scan", 0),
            }
            for creator in config["creators"]
        ],
    }


def default_status() -> dict:
    return {
        "state": "idle",
        "headline": "Ready",
        "detail": "Press Scan now to check for new videos.",
        "percent": None,
        "download_count": 0,
        "errors": [],
    }


def status_snapshot() -> dict:
    if not STATUS_FILE.exists():
        return default_status()
    try:
        status = default_status()
        status.update(json.loads(STATUS_FILE.read_text(encoding="utf-8")))
        if not isinstance(status["errors"], list):
            status["errors"] = []
        return status
    except (OSError, json.JSONDecodeError):
        return {
            **default_status(),
            "state": "error",
            "headline": "Status unavailable",
            "detail": "",
        }


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


def history_date_after(years: int, today: date | None = None) -> str:
    if not 1 <= years <= 10:
        raise ValueError("History range must be between 1 and 10 years.")
    today = today or date.today()
    try:
        cutoff = today.replace(year=today.year - years)
    except ValueError:
        cutoff = today.replace(year=today.year - years, day=28)
    return cutoff.strftime("%Y%m%d")


def scanner_command(config: dict, creator: dict, single_video: bool = False) -> list[str]:
    height = config["max_height"]
    command = [
        yt_dlp_executable(),
        "--ignore-errors",
        "--no-overwrites",
        "--no-color",
        "--download-archive",
        str(ARCHIVE_FILE),
    ]
    date_after = config.get("date_after", f"now-{config['lookback_days']}days")
    if date_after:
        command.extend(("--dateafter", date_after))
    if config.get("max_per_creator") is not None:
        command.extend(("--playlist-end", str(config["max_per_creator"])))
    if single_video:
        command.append("--no-playlist")
    command.extend([
        "--match-filters",
        "!is_live",
        "--format",
        f"bv*[height<={height}]+ba/b[height<={height}]/b",
        "--merge-output-format",
        "mp4",
        "--embed-metadata",
        "--embed-thumbnail",
        "--embed-chapters",
        "--parse-metadata",
        "%(channel|)s:%(meta_artist)s",
        "--paths",
        config["download_dir"],
        "--output",
        "%(channel)s/%(channel)s - %(upload_date>%Y-%m-%d)s - %(title).130B [%(id)s].%(ext)s",
        "--newline",
        "--progress",
        "--progress-delta",
        "0.5",
        "--progress-template",
        "download:CC_PROGRESS\t%(progress._percent_str)s\t%(progress._downloaded_bytes_str)s\t%(progress._total_bytes_str)s\t%(progress._speed_str)s\t%(progress._eta_str)s\t%(info.title)s",
        "--print",
        "before_dl:CC_DOWNLOAD\t%(id)s\t%(channel)s\t%(title)s",
        "--print",
        "after_move:CC_COMPLETE\t%(id)s\t%(filepath)s",
        creator["url"],
    ])
    return command


def parse_progress_line(
    line: str,
    download_count: int,
    attempted_video_ids: set[str] | None = None,
    move_enabled: bool = False,
) -> int:
    attempted_video_ids = attempted_video_ids if attempted_video_ids is not None else set()
    if line.startswith("CC_DOWNLOAD\t"):
        fields = line.rstrip("\n").split("\t", 3)
        if len(fields) < 4:
            return download_count
        video_id, creator, title = fields[1:4]
        attempted_video_ids.add(video_id)
        record_video_attempt(video_id, creator, title)
        update_status(
            state="downloading",
            headline=f"Downloading {title}",
            detail="Preparing download…",
            percent=0,
            download_count=download_count,
        )
    elif line.startswith("CC_PROGRESS\t"):
        fields = line.rstrip("\n").split("\t", 6)
        if len(fields) < 7:
            return download_count
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
    elif line.startswith("CC_COMPLETE\t"):
        fields = line.rstrip("\n").split("\t", 2)
        if len(fields) < 3:
            return download_count
        title = record_video_download(fields[1], fields[2], move_enabled)
        download_count += 1
        noun = "video" if download_count == 1 else "videos"
        update_status(
            state="downloaded",
            headline=f"Saved {title}",
            detail=f"{download_count} {noun} downloaded in this scan.",
            percent=100,
            download_count=download_count,
        )
    return download_count


def truncate_utf8(value: str, maximum_bytes: int) -> str:
    return value.encode("utf-8")[:maximum_bytes].decode("utf-8", errors="ignore").rstrip(" .-")


def plex_filename(creator: str, filename: str) -> str:
    """Prefix a video filename with its creator while preserving its ID."""
    path = Path(filename)
    prefix = f"{creator} - "
    if path.stem.casefold().startswith(prefix.casefold()):
        return filename

    identifier_match = re.search(r"( \[[^\[\]]+\])$", path.stem)
    identifier = identifier_match.group(1) if identifier_match else ""
    original_stem = path.stem[: -len(identifier)] if identifier else path.stem
    maximum_stem_bytes = 240 - len(path.suffix.encode("utf-8")) - len(identifier.encode("utf-8"))
    stem = truncate_utf8(prefix + original_stem, maximum_stem_bytes)
    return f"{stem}{identifier}{path.suffix}"


def plex_relative_path(source: Path, source_root: Path) -> Path:
    relative = source.relative_to(source_root)
    if len(relative.parts) < 2:
        return relative
    creator = relative.parts[0]
    return Path(*relative.parts[:-1], plex_filename(creator, relative.name))


def error_record(stage: str, message: str, creator: str = "") -> dict:
    cleaned = re.sub(r"^(?:ERROR|WARNING):\s*", "", message.strip())
    return {"stage": stage, "creator": creator, "message": cleaned[:2000]}


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
    videos = sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in VIDEO_EXTENSIONS
    )
    destination_root = Path(destination_value)
    if not destination_root.is_dir():
        error = f"move destination is unavailable: {destination_root}"
        for source in videos:
            record_video_transfer(source, False, error)
        return 0, [error]
    moved = 0
    errors: list[str] = []
    for position, source in enumerate(videos, 1):
        source_relative = source.relative_to(source_root)
        destination_relative = plex_relative_path(source, source_root)
        target = destination_root / destination_relative
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
            record_video_transfer(source, True)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            record_video_transfer(source, False, str(exc))
            errors.append(f"{source_relative}: {exc}")

    for directory in sorted(source_root.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass
    return moved, errors


def load_schedule_state() -> dict:
    if not SCHEDULE_FILE.exists():
        return {}
    try:
        loaded = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def next_automatic_scan(
    config: dict,
    now: datetime | None = None,
    last_started_at: datetime | None = None,
) -> datetime | None:
    """Return the next due time using the server's local clock."""
    if not config["automatic_scans_enabled"]:
        return None
    now = now or datetime.now()
    scheduled_time = datetime_time.fromisoformat(config["automatic_scan_time"])
    if last_started_at is None:
        raw_last_started = load_schedule_state().get("last_started_at")
        if isinstance(raw_last_started, str):
            try:
                last_started_at = datetime.fromisoformat(raw_last_started)
            except ValueError:
                last_started_at = None
    if last_started_at is None:
        return datetime.combine(now.date(), scheduled_time)
    next_date = last_started_at.date() + timedelta(
        days=config["automatic_scan_interval_days"]
    )
    return datetime.combine(next_date, scheduled_time)


def automatic_scan_is_due(
    config: dict,
    now: datetime | None = None,
    last_started_at: datetime | None = None,
) -> bool:
    now = now or datetime.now()
    next_scan = next_automatic_scan(config, now, last_started_at)
    return next_scan is not None and now >= next_scan


def scheduled_scan() -> int:
    config = load_config()
    now = datetime.now()
    if not automatic_scan_is_due(config, now):
        print("Automatic scan is not due.", flush=True)
        return 0
    atomic_json_write(SCHEDULE_FILE, {"last_started_at": now.isoformat(timespec="seconds")})
    return scan()


def scan(
    creator_url: str | None = None,
    history_years: int | None = None,
    video_url: str | None = None,
) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A scan is already running.", flush=True)
            return 0

        scan_errors: list[dict] = []
        try:
            config = load_config()
            Path(config["download_dir"]).mkdir(parents=True, exist_ok=True)
            history_mode = creator_url is not None or history_years is not None
            video_mode = video_url is not None
            if history_mode and video_mode:
                raise ValueError("Choose either a creator history or one video link.")
            if video_mode:
                normalized_video_url = normalize_video_url(video_url)
                creators = [{"name": "Video link", "url": normalized_video_url}]
                scan_config = {**config, "date_after": "", "max_per_creator": None}
                scan_kind = "video"
            elif history_mode:
                if creator_url is None or history_years is None:
                    raise ValueError("History scans require a creator and year range.")
                normalized_url = normalize_channel_url(creator_url)
                selected = [creator for creator in config["creators"] if creator["url"] == normalized_url]
                if not selected:
                    raise ValueError("The history creator must be selected from the saved creator list.")
                creators = selected
                scan_config = {
                    **config,
                    "date_after": history_date_after(history_years),
                    "max_per_creator": None,
                }
                scan_kind = "history"
            else:
                creators = [creator for creator in config["creators"] if creator["enabled"]]
                scan_config = config
                scan_kind = "regular"
            if not video_mode:
                begin_scan_stats(config)
            update_status(
                errors=[],
                download_count=0,
                moved_count=0,
                scan_kind=scan_kind,
            )
            if not creators:
                update_status(
                    state="idle",
                    headline="No enabled creators",
                    detail="Add or enable a creator first.",
                    percent=None,
                    download_count=0,
                    errors=[],
                )
                return 0

            failures = 0
            download_count = 0
            for position, creator in enumerate(creators, 1):
                creator_start_count = download_count
                if video_mode:
                    headline = "Checking video link"
                    detail = "Looking for the requested video…"
                elif history_mode:
                    headline = f"Checking history for {creator['name']}"
                    detail = f"Looking back {history_years} year{'s' if history_years != 1 else ''}…"
                else:
                    headline = f"Checking {creator['name']}"
                    detail = "Looking for eligible new uploads…"
                update_status(
                    state="checking",
                    headline=headline,
                    detail=detail,
                    percent=None,
                    creator_position=position,
                    creator_total=len(creators),
                    download_count=download_count,
                )
                process = subprocess.Popen(
                    scanner_command(scan_config, creator, single_video=video_mode),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert process.stdout is not None
                recent_warning = ""
                creator_errors = []
                attempted_video_ids: set[str] = set()
                for line in process.stdout:
                    print(line, end="", flush=True)
                    download_count = parse_progress_line(
                        line,
                        download_count,
                        attempted_video_ids,
                        move_enabled=bool(config.get("move_to_dir")),
                    )
                    if line.startswith("ERROR:"):
                        creator_errors.append(line.strip())
                    if line.startswith(("ERROR:", "WARNING:")):
                        recent_warning = line.strip()
                return_code = process.wait()
                mark_video_download_failures(
                    attempted_video_ids,
                    creator_errors[-1] if creator_errors else recent_warning,
                )
                creator_downloads = download_count - creator_start_count
                if not video_mode:
                    record_creator_downloads(creator["url"], creator_downloads)
                if return_code != 0 or creator_errors:
                    failures += 1
                    messages = creator_errors or [recent_warning or "yt-dlp returned an error."]
                    for message in messages:
                        if len(scan_errors) < MAX_ERRORS:
                            scan_errors.append(error_record("download", message, creator["name"]))
                    update_status(
                        state="checking",
                        headline=f"Could not scan {creator['name']}",
                        detail=recent_warning or "yt-dlp returned an error.",
                        percent=None,
                        download_count=download_count,
                        errors=scan_errors,
                    )

            moved_count, move_errors = move_pending_videos(config)
            for message in move_errors:
                if len(scan_errors) < MAX_ERRORS:
                    scan_errors.append(error_record("move", message))
            noun = "video" if download_count == 1 else "videos"
            moved_noun = "video" if moved_count == 1 else "videos"
            if failures or move_errors:
                problem_count = failures + len(move_errors)
                error_detail = move_errors[0] if move_errors else f"{failures} creator checks failed."
                update_status(
                    state="error",
                    headline=(
                        "Video download finished with errors"
                        if video_mode
                        else "History scan finished with errors"
                        if history_mode
                        else "Scan finished with errors"
                    ),
                    detail=(
                        f"Downloaded {download_count} new {noun}; moved {moved_count} {moved_noun}. "
                        f"{problem_count} problem(s): {error_detail}"
                    ),
                    percent=100,
                    download_count=download_count,
                    moved_count=moved_count,
                    errors=scan_errors,
                )
                return 1
            if video_mode and download_count == 0:
                completion_detail = "No new video was downloaded; it may already be in the archive."
            else:
                completion_detail = (
                    f"Downloaded {download_count} new {noun}; moved {moved_count} {moved_noun}."
                )
            update_status(
                state="complete",
                headline=(
                    "Video download complete"
                    if video_mode
                    else "History scan complete"
                    if history_mode
                    else "Scan complete"
                ),
                detail=completion_detail,
                percent=100,
                download_count=download_count,
                moved_count=moved_count,
                errors=[],
            )
            return 0
        except Exception as exc:
            if len(scan_errors) < MAX_ERRORS:
                scan_errors.append(error_record("scanner", str(exc)))
            update_status(
                state="error",
                headline="Scan failed",
                detail=str(exc),
                percent=None,
                errors=scan_errors,
            )
            print(f"Creator Catcher: {exc}", file=sys.stderr)
            return 1


def validate_history_request(body: object, config: dict | None = None) -> tuple[dict, int]:
    if not isinstance(body, dict) or set(body) != {"creator_url", "years"}:
        raise ValueError("History request must contain creator_url and years.")
    if isinstance(body["years"], bool):
        raise ValueError("History range must be a whole number of years.")
    try:
        years = int(body["years"])
    except (TypeError, ValueError) as exc:
        raise ValueError("History range must be a whole number of years.") from exc
    history_date_after(years)
    creator_url = normalize_channel_url(str(body["creator_url"]))
    config = config or load_config()
    creator = next((item for item in config["creators"] if item["url"] == creator_url), None)
    if creator is None:
        raise ValueError("Select a creator from the saved creator list.")
    return creator, years


def validate_video_request(body: object) -> str:
    if not isinstance(body, dict) or set(body) != {"video_url"}:
        raise ValueError("Video request must contain video_url.")
    return normalize_video_url(str(body["video_url"]))


def spawn_scan_process(arguments: list[str]) -> None:
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "scan", *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    threading.Thread(target=process.wait, daemon=True).start()


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
            self.send_json(config_for_api())
        elif path == "/api/status":
            self.send_json(status_snapshot())
        elif path == "/api/video-log":
            self.send_json({"entries": load_video_log()})
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
                self.send_json(config_for_api(save_config(body)))
            elif path == "/api/scan":
                if body != {}:
                    raise ValueError("Scan request must be an empty object.")
                update_status(
                    state="starting",
                    headline="Starting scan…",
                    detail="Preparing to check enabled creators.",
                    percent=None,
                    errors=[],
                )
                spawn_scan_process([])
                self.send_json({"started": True}, HTTPStatus.ACCEPTED)
            elif path == "/api/history":
                creator, years = validate_history_request(body)
                update_status(
                    state="starting",
                    headline=f"Starting history scan for {creator['name']}…",
                    detail=f"Preparing to look back {years} year{'s' if years != 1 else ''}.",
                    percent=None,
                    errors=[],
                    scan_kind="history",
                )
                spawn_scan_process(
                    ["--creator-url", creator["url"], "--history-years", str(years)]
                )
                self.send_json({"started": True}, HTTPStatus.ACCEPTED)
            elif path == "/api/video":
                video_url = validate_video_request(body)
                update_status(
                    state="starting",
                    headline="Starting video download…",
                    detail="Checking the download archive and preparing the video.",
                    percent=None,
                    errors=[],
                    scan_kind="video",
                )
                spawn_scan_process(["--video-url", video_url])
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
    parser.add_argument(
        "command",
        choices=("serve", "scan", "scheduled-scan", "validate"),
        nargs="?",
        default="serve",
    )
    parser.add_argument("--creator-url")
    parser.add_argument("--history-years", type=int)
    parser.add_argument("--video-url")
    args = parser.parse_args()
    scan_options_used = any(
        value is not None for value in (args.creator_url, args.history_years, args.video_url)
    )
    if args.command != "scan" and scan_options_used:
        parser.error("History and video options can only be used with the scan command.")
    if (args.creator_url is None) != (args.history_years is None):
        parser.error("--creator-url and --history-years must be used together.")
    if args.video_url is not None and args.creator_url is not None:
        parser.error("--video-url cannot be combined with creator history options.")
    return args


def main() -> int:
    args = parse_args()
    if args.command == "scan":
        return scan(args.creator_url, args.history_years, args.video_url)
    if args.command == "scheduled-scan":
        return scheduled_scan()
    if args.command == "validate":
        return validate_installation()
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
