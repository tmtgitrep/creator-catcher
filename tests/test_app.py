import importlib.util
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).parents[1] / "src" / "creator_catcher" / "app.py"
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
        self.assertTrue(result["creators"][0]["url"].endswith("/videos"))

    def test_status_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            original = app.STATUS_FILE
            app.STATUS_FILE = Path(directory) / "status.json"
            try:
                app.update_status(state="checking", headline="Checking test", percent=None)
                self.assertEqual(app.status_snapshot()["state"], "checking")
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

if __name__ == "__main__":
    unittest.main()
