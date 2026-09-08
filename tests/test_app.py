import importlib.util
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).parents[1] / "src" / "creator_catcher" / "app.py"
PROJECT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("creator_catcher_app", SOURCE)
app = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(app)

class ConfigurationTests(unittest.TestCase):
    def test_normalizes_creator_url(self):
        self.assertEqual(app.normalize_channel_url("youtube.com/@durandian"), "https://www.youtube.com/@durandian/videos")

    def test_preserves_supported_tabs(self):
        self.assertEqual(app.normalize_channel_url("https://youtube.com/@example/shorts"), "https://www.youtube.com/@example/shorts")

    def test_rejects_non_youtube_hosts(self):
        with self.assertRaises(ValueError):
            app.normalize_channel_url("https://example.com/channel")

    def test_validates_complete_config(self):
        candidate = app.default_config()
        candidate["creators"] = [{"name": "Durandian", "url": "https://youtube.com/@durandian", "enabled": True}]
        result = app.validate_config(candidate)
        self.assertEqual(result["max_height"], 1080)
        self.assertEqual(result["move_to_dir"], "")
        self.assertTrue(result["creators"][0]["url"].endswith("/videos"))

    def test_validates_move_destination(self):
        candidate = app.default_config()
        candidate["move_to_dir"] = "/mnt/plexmediaserver/Media/FromYouTube"
        result = app.validate_config(candidate)
        self.assertEqual(result["move_to_dir"], candidate["move_to_dir"])

    def test_rejects_relative_or_overlapping_move_destination(self):
        candidate = app.default_config()
        candidate["move_to_dir"] = "Media/FromYouTube"
        with self.assertRaisesRegex(ValueError, "absolute mounted path"):
            app.validate_config(candidate)
        candidate["move_to_dir"] = str(Path(candidate["download_dir"]) / "network")
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            app.validate_config(candidate)

    def test_status_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            original = app.STATUS_FILE
            app.STATUS_FILE = Path(directory) / "status.json"
            try:
                app.update_status(state="checking", headline="Checking test", percent=None)
                self.assertEqual(app.status_snapshot()["state"], "checking")
                self.assertEqual(app.status_snapshot()["errors"], [])
            finally:
                app.STATUS_FILE = original

    def test_download_progress_updates_status(self):
        with tempfile.TemporaryDirectory() as directory:
            original = app.STATUS_FILE
            app.STATUS_FILE = Path(directory) / "status.json"
            try:
                count = app.parse_progress_line(
                    "CC_PROGRESS\t42.5%\t425 MiB\t1 GiB\t10 MiB/s\t00:58\tTest video\n",
                    0,
                )
                self.assertEqual(count, 0)
                self.assertEqual(app.status_snapshot()["percent"], 42.5)
                count = app.parse_progress_line("CC_COMPLETE\tTest video\n", count)
                self.assertEqual(count, 1)
                self.assertEqual(app.status_snapshot()["state"], "downloaded")
            finally:
                app.STATUS_FILE = original

    def test_moves_pending_video_and_preserves_creator_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "downloads"
            destination_root = root / "network"
            source = source_root / "Durandian" / "video.mp4"
            source.parent.mkdir(parents=True)
            destination_root.mkdir()
            source.write_bytes(b"video data")
            original_status = app.STATUS_FILE
            app.STATUS_FILE = root / "status.json"
            try:
                moved, errors = app.move_pending_videos(
                    {"download_dir": str(source_root), "move_to_dir": str(destination_root)}
                )
            finally:
                app.STATUS_FILE = original_status
            self.assertEqual((moved, errors), (1, []))
            self.assertFalse(source.exists())
            self.assertEqual(
                (destination_root / "Durandian" / "Durandian - video.mp4").read_bytes(),
                b"video data",
            )

    def test_move_does_not_overwrite_existing_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "downloads"
            destination_root = root / "network"
            source = source_root / "Creator" / "video.mp4"
            target = destination_root / "Creator" / "Creator - video.mp4"
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            source.write_bytes(b"new")
            target.write_bytes(b"existing")
            original_status = app.STATUS_FILE
            app.STATUS_FILE = root / "status.json"
            try:
                moved, errors = app.move_pending_videos(
                    {"download_dir": str(source_root), "move_to_dir": str(destination_root)}
                )
            finally:
                app.STATUS_FILE = original_status
            self.assertEqual(moved, 0)
            self.assertEqual(len(errors), 1)
            self.assertEqual(source.read_bytes(), b"new")
            self.assertEqual(target.read_bytes(), b"existing")

    def test_unavailable_destination_leaves_video_local(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "downloads"
            source = source_root / "Creator" / "video.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"video")
            moved, errors = app.move_pending_videos(
                {"download_dir": str(source_root), "move_to_dir": str(root / "missing-share")}
            )
            self.assertEqual(moved, 0)
            self.assertEqual(len(errors), 1)
            self.assertIn("unavailable", errors[0])
            self.assertTrue(source.exists())

    def test_plex_filename_preserves_youtube_id_and_length(self):
        filename = "20260908 - " + ("A very long title " * 30) + " [abc123XYZ_-].mp4"
        result = app.plex_filename("Durandian", filename)
        self.assertTrue(result.startswith("Durandian - 20260908 - "))
        self.assertTrue(result.endswith(" [abc123XYZ_-].mp4"))
        self.assertLessEqual(len(result.encode("utf-8")), 240)
        self.assertEqual(app.plex_filename("Durandian", result), result)

    def test_scanner_embeds_metadata_and_uses_plex_filename(self):
        original = app.yt_dlp_executable
        app.yt_dlp_executable = lambda: "/yt-dlp"
        try:
            command = app.scanner_command(
                app.default_config(),
                {"url": "https://www.youtube.com/@durandian/videos"},
            )
        finally:
            app.yt_dlp_executable = original
        self.assertIn("--embed-metadata", command)
        self.assertIn("--embed-thumbnail", command)
        self.assertIn("--embed-chapters", command)
        output = command[command.index("--output") + 1]
        self.assertIn("%(channel)s/%(channel)s - ", output)
        self.assertIn("%(upload_date>%Y-%m-%d)s", output)

    def test_records_and_exposes_creator_download_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            original = app.STATS_FILE
            app.STATS_FILE = Path(directory) / "stats.json"
            config = app.default_config()
            config["creators"] = [
                {
                    "name": "Durandian",
                    "url": "https://www.youtube.com/@durandian/videos",
                    "enabled": True,
                }
            ]
            try:
                app.begin_scan_stats(config)
                app.record_creator_downloads(config["creators"][0]["url"], 2)
                payload = app.config_for_api(config)
                app.begin_scan_stats(config)
                reset_payload = app.config_for_api(config)
            finally:
                app.STATS_FILE = original
            self.assertEqual(payload["creators"][0]["download_count"], 2)
            self.assertEqual(payload["creators"][0]["last_scan_download_count"], 2)
            self.assertEqual(reset_payload["creators"][0]["download_count"], 2)
            self.assertEqual(reset_payload["creators"][0]["last_scan_download_count"], 0)

    def test_error_record_is_structured_and_bounded(self):
        record = app.error_record("download", "ERROR: " + ("x" * 3000), "Durandian")
        self.assertEqual(
            {key: record[key] for key in ("stage", "creator")},
            {"stage": "download", "creator": "Durandian"},
        )
        self.assertEqual(len(record["message"]), 2000)


class PackagingTests(unittest.TestCase):
    def test_manual_and_scheduled_scans_can_write_to_mounts(self):
        expected = "ReadWritePaths=/var/lib/creator-catcher -/mnt -/media -/srv"
        for unit in ("creator-catcher-web.service", "creator-catcher-scan.service"):
            contents = (PROJECT / "packaging" / "systemd" / unit).read_text(encoding="utf-8")
            self.assertIn(expected, contents)

    def test_ui_loads_version_and_stops_finished_progress(self):
        static = PROJECT / "src" / "creator_catcher" / "static"
        markup = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="app-version"', markup)
        self.assertIn('await api("/health")', script)
        self.assertIn('["complete","error"].includes(status.state)', script)
        self.assertIn("status.percent ?? 100", script)

if __name__ == "__main__":
    unittest.main()
