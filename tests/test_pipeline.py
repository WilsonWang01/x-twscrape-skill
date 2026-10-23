"""离线验证，不连接 X，不使用真实 Cookie。"""

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "x-twscrape" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import collect_x_tweets_90d as collector
import download_x_attachments as downloader
import x_watch


class PipelineTest(unittest.TestCase):
    def test_explicit_handles_and_utc_window(self):
        users = collector.selected_users(["@Example", "example", "another_user"])
        self.assertEqual([row["handle"] for row in users], ["Example", "another_user"])
        for handles in (None, ["https://x.com/example"]):
            with self.assertRaises(collector.CollectorError):
                collector.selected_users(handles)
        _, _, start, end = collector.build_window(argparse.Namespace(lookback_minutes=20))
        self.assertEqual(end - start, timedelta(minutes=20))
        self.assertTrue(collector.in_window(start, start, end))
        self.assertFalse(collector.in_window(end, start, end))

    def test_archive_dedupe_and_no_automatic_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            store = collector.OutputStore(Path(directory), repair=False)
            record = dict(collector="twscrape", source={"handle": "example"},
                          handle="example", mode="timeline", query=None, tweet={"id": 123})
            self.assertTrue(store.record_tweet(**record))
            again = collector.OutputStore(Path(directory), repair=False)
            self.assertFalse(again.record_tweet(**record))
            self.assertEqual(len(store.tweets_path.read_text().splitlines()), 1)
            damaged = store.tweets_path.read_text() + '{"partial"'
            store.tweets_path.write_text(damaged)
            with self.assertRaises(json.JSONDecodeError):
                collector.OutputStore(Path(directory), repair=False)
            self.assertEqual(store.tweets_path.read_text(), damaged)

    def test_database_and_output_locks_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db, output = root / "accounts.db", root / "archive"
            with x_watch.pipeline_lock(db, output):
                with self.assertRaises(RuntimeError):
                    with x_watch.pipeline_lock(db, root / "other"):
                        pass
                with self.assertRaises(RuntimeError):
                    with x_watch.pipeline_lock(root / "other.db", output):
                        pass
            with x_watch.pipeline_lock(db, output):
                pass

    def test_status_reads_only_allowlisted_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "accounts.db"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("CREATE TABLE accounts (username, active, locks, stats, cookies, password)")
                connection.execute("INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?)",
                                   ("test", 1, "{}", "{}", "PRIVATE_SENTINEL", "PRIVATE_SENTINEL"))
                connection.commit()
            with patch("sys.stdout", new_callable=io.StringIO) as stream:
                x_watch.status(argparse.Namespace(db=db, output=root))
            self.assertNotIn("PRIVATE_SENTINEL", stream.getvalue())
            self.assertEqual(set(x_watch.read_accounts(db)[0]), {"username", "active", "locks", "stats"})

    def test_attachment_candidates_download_and_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tweets = root / "tweets.jsonl"
            tweets.write_text(json.dumps({"handle": "example", "tweet": {
                "id": 123,
                "media": {"photos": [{"url": "https://pbs.twimg.com/media/fake.jpg?name=small"}],
                          "videos": [{"url": "https://example.invalid/fake.mp4"}]},
                "links": [{"expanded_url": "https://example.invalid/report.pdf?download=1"},
                          {"expanded_url": "https://example.invalid/article"}]
            }}) + "\n")
            candidates = downloader.iter_attachments(tweets, include_videos=False)
            self.assertEqual([item.kind for item in candidates], ["image", "pdf"])
            self.assertIn("name=orig", candidates[0].url)
            args = ["download_x_attachments.py", "--tweets", str(tweets), "--output-dir", str(root / "attachments"),
                    "--sleep-min", "0", "--sleep-max", "0", "--skip-existing-manifest"]
            def fake_fetch(url, **kwargs):
                return ((b"%PDF-1.7\nexample", "application/pdf", url) if ".pdf" in url
                        else (b"fake-image", "image/jpeg", url))
            with patch.object(sys, "argv", args), patch.object(downloader, "fetch_with_alternates", side_effect=fake_fetch) as fetch:
                self.assertEqual(downloader.main(), 0)
                self.assertEqual(fetch.call_count, 2)
            manifest = root / "attachments" / "attachment_manifest.jsonl"
            rows = [json.loads(line) for line in manifest.read_text().splitlines()]
            self.assertTrue(all(Path(row["local_path"]).is_file() for row in rows))
            with patch.object(sys, "argv", args), patch.object(downloader, "fetch_with_alternates") as fetch:
                self.assertEqual(downloader.main(), 0)
                fetch.assert_not_called()
            self.assertEqual(len(manifest.read_text().splitlines()), 2)

    def test_success_partial_and_scan_limit_exit_codes(self):
        class FakePool:
            async def stats(self):
                return {"active": 1}
        class FakeAPI:
            pool = FakePool()
            def __init__(self, db):
                pass
            async def user_by_login(self, handle):
                if handle == "broken":
                    raise RuntimeError("simulated lookup error")
                return {"id": 1, "username": handle}
            async def user_tweets(self, user_id, limit):
                for number in (1, 2):
                    yield types.SimpleNamespace(dict=lambda number=number: {
                        "id": number, "date": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), "rawContent": "sample"
                    })
        module = types.ModuleType("twscrape")
        module.API = FakeAPI
        for handles, cap, expected in ((["example"], -1, 0), (["broken"], -1, 1), (["example"], 1, 1)):
            with self.subTest(handles=handles, cap=cap), tempfile.TemporaryDirectory() as directory:
                args = ["collect", "--handles", *handles, "--lookback-minutes", "20", "--force",
                        "--output-dir", directory, "--max-scan-per-mode", str(cap)]
                with patch.object(sys, "argv", args), patch.dict(sys.modules, {"twscrape": module}), \
                     patch.object(collector, "patch_twscrape_xclid"), patch.object(collector, "load_dotenv"):
                    self.assertEqual(asyncio.run(collector.main_async()), expected)
                if handles == ["example"] and cap == -1:
                    rows = (Path(directory) / "tweets.jsonl").read_text().splitlines()
                    self.assertEqual(len(rows), 2)

    def test_wrapper_downloads_partial_archive_but_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db, output = root / "accounts.db", root / "archive"
            db.touch()
            output.mkdir()
            (output / "tweets.jsonl").touch()
            args = argparse.Namespace(db=db, output=output, modes=["timeline"], replies_every=6,
                                      handles=["example"], days=None, lookback_minutes=20,
                                      query_suffix="", attachments=True)
            with patch.object(x_watch.subprocess, "run", side_effect=[types.SimpleNamespace(returncode=1),
                                                                      types.SimpleNamespace(returncode=0)]) as run:
                self.assertEqual(x_watch.run_once(args, 6), 1)
                self.assertEqual(run.call_count, 2)
                command = run.call_args_list[0].args[0]
                self.assertEqual(command[command.index("--source") + 1], "twscrape")
                self.assertIn("replies", command)
            state = json.loads((output / "last_run.json").read_text())
            self.assertFalse(state["ok"])
            self.assertEqual(state["scrape_exit_code"], 1)

    def test_paid_sources_are_rejected_before_network(self):
        for source in ("auto", "xapi"):
            with patch.object(sys, "argv", ["collect", "--source", source, "--handles", "example"]), \
                 patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    collector.parse_args()
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
