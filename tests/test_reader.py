import json
import tempfile
import unittest
from pathlib import Path

import reader

LISTING = json.loads(reader.FIXTURE_PATH.read_text(encoding="utf-8"))
KEYWORDS = reader.split_csv(reader.DEFAULTS["KEYWORDS"])
DAY = 24 * 60 * 60


def make_post(post_id, created_utc):
    return {
        "id": post_id,
        "subreddit": "ExampleVoiceGigs",
        "title": "test post",
        "selftext": "",
        "created_utc": created_utc,
        "permalink": f"/r/ExampleVoiceGigs/comments/{post_id}/test_post/",
    }


class ReaderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = reader.open_db(Path(tmp.name) / "test.db")
        self.addCleanup(self.conn.close)  # cleanups run last-in first-out: close, then delete

    def saved_ids(self):
        return sorted(row[0] for row in self.conn.execute("SELECT id FROM posts"))

    def test_keyword_filter_keeps_exactly_the_three_casting_calls(self):
        # fx0001 via "[paid]", fx0002 via "narrator", fx0003 via "looking for a voice".
        # fx0006 says "paid" and "hiring" without brackets, so it must not match.
        matches = reader.find_casting_calls(LISTING, KEYWORDS)
        self.assertEqual(sorted(post["id"] for post in matches), ["fx0001", "fx0002", "fx0003"])

    def test_second_ingest_of_the_same_posts_saves_nothing(self):
        matches = reader.find_casting_calls(LISTING, KEYWORDS)
        self.assertEqual(len(reader.save_posts(self.conn, matches)), 3)
        self.assertEqual(len(reader.save_posts(self.conn, matches)), 0)
        self.assertEqual(self.saved_ids(), ["fx0001", "fx0002", "fx0003"])

    def test_prune_removes_old_posts_and_keeps_recent_ones(self):
        now = 1_800_000_000
        reader.save_posts(self.conn, [make_post("old", now - 31 * DAY), make_post("recent", now - DAY)])
        self.assertEqual(reader.prune(self.conn, now=now), 1)
        self.assertEqual(self.saved_ids(), ["recent"])


if __name__ == "__main__":
    unittest.main()
