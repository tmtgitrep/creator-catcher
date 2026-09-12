import importlib.util
from datetime import date, datetime
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

    def test_normalizes_single_video_urls_and_drops_playlist_parameters(self):
        expected = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertEqual(
            app.normalize_video_url(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=2"
            ),
            expected,
        )
        self.assertEqual(app.normalize_video_url("https://youtu.be/dQw4w9WgXcQ?t=30"), expected)
        self.assertEqual(app.normalize_video_url("https://youtube.com/shorts/dQw4w9WgXcQ"), expected)

    def test_rejects_non_video_youtube_urls(self):
        for value in ("https://youtube.com/@creator", "https://example.com/watch?v=dQw4w9WgXcQ"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "valid YouTube"):
                app.normalize_video_url(value)

    def test_validates_complete_config(self):
        candidate = app.default_config()
        candidate["creators"] = [{"name": "Durandian", "url": "https://youtube.com/@durandian", "enabled": True}]
        result = app.validate_config(candidate)
        self.assertEqual(result["max_height"], 1080)
        self.assertEqual(result["move_to_dir"], "")
        self.assertTrue(result["automatic_scans_enabled"])
        self.assertEqual(result["automatic_scan_interval_days"], 1)
        self.assertEqual(result["automatic_scan_time"], "04:00")
        self.assertTrue(result["creators"][0]["url"].endswith("/videos"))

    def test_validates_automatic_scan_schedule(self):
        candidate = app.default_config()
        candidate.update(
            automatic_scans_enabled=False,
            automatic_scan_interval_days=7,
            automatic_scan_time="21:35",
        )
        result = app.validate_config(candidate)
        self.assertFalse(result["automatic_scans_enabled"])
        self.assertEqual(result["automatic_scan_interval_days"], 7)
        self.assertEqual(result["automatic_scan_time"], "21:35")
        candidate["automatic_scan_interval_days"] = 31
        with self.assertRaisesRegex(ValueError, "between 1 and 30 days"):
            app.validate_config(candidate)
        candidate["automatic_scan_interval_days"] = 1
        candidate["automatic_scan_time"] = "4:00 PM"
        with self.assertRaisesRegex(ValueError, "24-hour HH:MM"):
            app.validate_config(candidate)

    def test_automatic_scan_due_uses_interval_and_local_time(self):
        config = app.default_config()
        config["automatic_scan_interval_days"] = 3
        config["automatic_scan_time"] = "04:30"
        last_started = datetime(2026, 9, 9, 4, 31)
        self.assertFalse(
            app.automatic_scan_is_due(config, datetime(2026, 9, 12, 4, 29), last_started)
        )
        self.assertTrue(
            app.automatic_scan_is_due(config, datetime(2026, 9, 12, 4, 30), last_started)
        )
        config["automatic_scans_enabled"] = False
        self.assertIsNone(app.next_automatic_scan(config, datetime(2026, 9, 12, 5, 0)))

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

    def test_single_video_command_ignores_age_limits_and_playlists(self):
        config = app.default_config()
        config.update(date_after="", max_per_creator=None)
        original = app.yt_dlp_executable
        app.yt_dlp_executable = lambda: "/yt-dlp"
        try:
            command = app.scanner_command(
                config,
                {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
                single_video=True,
            )
        finally:
            app.yt_dlp_executable = original
        self.assertIn("--no-playlist", command)
        self.assertNotIn("--dateafter", command)
        self.assertNotIn("--playlist-end", command)
        self.assertEqual(command[-1], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_video_request_accepts_only_one_video_url(self):
        self.assertEqual(
            app.validate_video_request({"video_url": "https://youtu.be/dQw4w9WgXcQ"}),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        with self.assertRaisesRegex(ValueError, "must contain video_url"):
            app.validate_video_request({"video_url": "https://youtu.be/dQw4w9WgXcQ", "extra": 1})

    def test_history_scan_uses_calendar_cutoff_without_playlist_limit(self):
        self.assertEqual(app.history_date_after(3, date(2026, 9, 8)), "20230908")
        self.assertEqual(app.history_date_after(1, date(2024, 2, 29)), "20230228")
        config = app.default_config()
        config["date_after"] = "20230908"
        config["max_per_creator"] = None
        original = app.yt_dlp_executable
        app.yt_dlp_executable = lambda: "/yt-dlp"
        try:
            command = app.scanner_command(config, {"url": "https://www.youtube.com/@durandian/videos"})
        finally:
            app.yt_dlp_executable = original
        self.assertEqual(command[command.index("--dateafter") + 1], "20230908")
        self.assertNotIn("--playlist-end", command)

    def test_history_request_requires_saved_creator_and_valid_years(self):
        config = app.default_config()
        config["creators"] = [
            {
                "name": "Durandian",
                "url": "https://www.youtube.com/@durandian/videos",
                "enabled": False,
            }
        ]
        creator, years = app.validate_history_request(
            {"creator_url": "youtube.com/@durandian", "years": 3}, config
        )
        self.assertEqual(creator["name"], "Durandian")
        self.assertEqual(years, 3)
        with self.assertRaisesRegex(ValueError, "between 1 and 10"):
            app.validate_history_request({"creator_url": creator["url"], "years": 11}, config)
        with self.assertRaisesRegex(ValueError, "saved creator"):
            app.validate_history_request(
                {"creator_url": "youtube.com/@someoneelse", "years": 3}, config
            )

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

    def test_ui_has_expandable_history_download(self):
        static = PROJECT / "src" / "creator_catcher" / "static"
        markup = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('class="card history-card collapsible-card"', markup)
        self.assertIn('id="history-creator"', markup)
        self.assertIn('id="history-years"', markup)
        self.assertIn('api("/api/history"', script)

    def test_ui_has_expandable_download_and_schedule_settings(self):
        static = PROJECT / "src" / "creator_catcher" / "static"
        markup = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('class="card settings-card collapsible-card"', markup)
        self.assertIn('id="automatic-enabled"', markup)
        self.assertIn('id="automatic-interval"', markup)
        self.assertIn('id="automatic-time"', markup)
        self.assertIn("config.automatic_scan_interval_days", script)

    def test_ui_has_expandable_single_video_download(self):
        static = PROJECT / "src" / "creator_catcher" / "static"
        markup = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('class="card video-card collapsible-card"', markup)
        self.assertIn('id="video-url"', markup)
        self.assertIn('id="video-download"', markup)
        self.assertIn('api("/api/video"', script)

    def test_ui_has_collapsible_creators(self):
        markup = (
            PROJECT / "src" / "creator_catcher" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn('class="card creators-card collapsible-card"', markup)
        self.assertIn('<summary><span class="section-title">Creators</span></summary>', markup)

    def test_timer_checks_app_managed_schedule(self):
        timer = (PROJECT / "packaging/systemd/creator-catcher-scan.timer").read_text(
            encoding="utf-8"
        )
        service = (PROJECT / "packaging/systemd/creator-catcher-scan.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("OnUnitActiveSec=5m", timer)
        self.assertIn("ExecStart=/usr/bin/creator-catcher scheduled-scan", service)

if __name__ == "__main__":
    unittest.main()
