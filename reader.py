"""vocation-reddit-reader: a once-a-day, read-only casting-call reader.

I'm a solo voice actor. This script checks a few subreddits once a day for
casting calls and saves the matches to a local SQLite file so I can read them
and apply by hand.

Hard rules (enforced in this file):
- Read-only. Besides the OAuth token request, the only Reddit endpoint called
  is GET https://oauth.reddit.com/r/{sub}/new. No posting, commenting, voting,
  messaging, user-profile lookups, or comment-thread crawling.
- Respect limits. At most one listing request per subreddit per run, a 2-second
  pause between requests, X-Ratelimit headers honored, and on HTTP 429 wait for
  Retry-After (or 60 s), retry once, then skip that subreddit.
- Descriptive User-Agent: "python:vocation-reddit-reader:v0.1 (by /u/mattcavanaugh)".
- Data is for my personal use only. It stays in posts.db on my machine, is never
  shared or sold, and is never used to train AI or ML models. `prune` deletes
  saved posts older than 30 days.

Usage:
    python reader.py run            # fetch /new once per subreddit, save matches
    python reader.py run --dry-run  # same, but read fixtures/sample_new.json
    python reader.py list           # print saved posts, newest first
    python reader.py prune          # delete saved posts older than 30 days
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
DB_PATH = HERE / "posts.db"
FIXTURE_PATH = HERE / "fixtures" / "sample_new.json"

USER_AGENT = "python:vocation-reddit-reader:v0.1 (by /u/mattcavanaugh)"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
LISTING_URL = "https://oauth.reddit.com/r/{sub}/new"
PAUSE_SECONDS = 2
RETENTION_DAYS = 30
SUBREDDIT_NAME = re.compile(r"^[A-Za-z0-9_]{2,21}$")  # rejects paths like "x/comments/y"

# Used when .env is missing a value (so --dry-run works with no .env at all).
DEFAULTS = {
    "REDDIT_CLIENT_ID": "",
    "REDDIT_CLIENT_SECRET": "",
    "REDDIT_USERNAME": "",
    "REDDIT_PASSWORD": "",
    "SUBREDDITS": "VoiceActing,voiceover,castingcalls,gameDevClassifieds,INAT,audiodrama,forhire",
    "KEYWORDS": "casting,looking for a voice,VA needed,[paid],[hiring],narrator,"
    "voice actor,voice over,audition",
}
CREDENTIALS = ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD"]


# ---------- config ----------

def read_env_file(path):
    """Read KEY=VALUE lines from a .env file. Blank lines and # comments are skipped."""
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return values


def split_csv(text):
    return [item.strip() for item in text.split(",") if item.strip()]


def load_config(path=ENV_PATH):
    config = {**DEFAULTS, **read_env_file(path)}
    # One request per subreddit per run: drop duplicates (Reddit names are case-insensitive).
    subreddits = list(dict.fromkeys(s.lower() for s in split_csv(config["SUBREDDITS"])))
    bad = [s for s in subreddits if not SUBREDDIT_NAME.match(s)]
    if bad:
        sys.exit(f"Not a plain subreddit name (no 'r/', slashes or spaces): {', '.join(bad)}")
    config["SUBREDDITS"] = subreddits
    config["KEYWORDS"] = split_csv(config["KEYWORDS"])
    return config


# ---------- filtering ----------

def matches_keywords(post, keywords):
    """Case-insensitive substring match of any keyword against title + body.

    Bracket tags such as "[paid]" match literally, so a post that just says
    "paid" does not count.
    """
    # ponytail: plain substring match, so "casting" also hits "podcasting".
    # A false positive only costs one extra post to read; tune KEYWORDS if it gets noisy.
    text = f"{post.get('title', '')}\n{post.get('selftext', '')}".lower()
    return any(keyword.lower() in text for keyword in keywords)


def find_casting_calls(listing, keywords):
    """Return the posts in a Reddit listing that look like casting calls."""
    posts = [child["data"] for child in listing["data"]["children"]]
    return [post for post in posts if matches_keywords(post, keywords)]


# ---------- storage ----------

def open_db(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            subreddit TEXT,
            title TEXT,
            url TEXT,
            created_utc INTEGER,
            selftext TEXT,
            saved_at TEXT
        )"""
    )
    return conn


def save_posts(conn, posts):
    """Save posts, skipping ids already in the db. Returns only the newly saved posts."""
    saved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = []
    for post in posts:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO posts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                post["id"],
                post["subreddit"],
                post["title"],
                "https://www.reddit.com" + post["permalink"],
                int(post["created_utc"]),
                post.get("selftext", ""),
                saved_at,
            ),
        )
        if cursor.rowcount:
            new.append(post)
    conn.commit()
    return new


def prune(conn, now=None):
    """Delete saved posts created more than RETENTION_DAYS ago. Returns the count."""
    now = time.time() if now is None else now
    cutoff = now - RETENTION_DAYS * 24 * 60 * 60
    deleted = conn.execute("DELETE FROM posts WHERE created_utc < ?", (cutoff,)).rowcount
    conn.commit()
    return deleted


# ---------- Reddit API (read-only) ----------

def get_token(config):
    """Script-app password grant with scope 'read'. Returns a bearer token."""
    response = requests.post(
        TOKEN_URL,
        auth=(config["REDDIT_CLIENT_ID"], config["REDDIT_CLIENT_SECRET"]),
        data={
            "grant_type": "password",
            "username": config["REDDIT_USERNAME"],
            "password": config["REDDIT_PASSWORD"],
            "scope": "read",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if "access_token" not in body:
        sys.exit(f"Reddit login failed: {body.get('error', body)}")
    return body["access_token"]


def wait_for_rate_limit(headers):
    """If Reddit says no requests are left in this window, sleep until it resets."""
    if float(headers.get("X-Ratelimit-Remaining", 1)) < 1:
        reset = float(headers.get("X-Ratelimit-Reset", 60))
        print(f"  rate limit reached, sleeping {reset:.0f}s")
        time.sleep(reset)


def fetch_new(sub, token):
    """GET /r/{sub}/new: the only listing this script reads."""
    headers = {"Authorization": f"bearer {token}", "User-Agent": USER_AGENT}
    for attempt in (1, 2):
        response = requests.get(
            LISTING_URL.format(sub=sub), headers=headers, params={"limit": 100}, timeout=30
        )
        wait_for_rate_limit(response.headers)
        if response.status_code == 429 and attempt == 1:
            retry_after = float(response.headers.get("Retry-After", 60))
            print(f"  429 from Reddit, retrying once in {retry_after:.0f}s")
            time.sleep(retry_after)
            continue
        response.raise_for_status()  # a second 429 ends up here and skips this subreddit
        return response.json()


# ---------- commands ----------

def run(config, conn, dry_run=False):
    if dry_run:
        print(f"[dry run] reading {FIXTURE_PATH.name} instead of calling Reddit")
        token = None
    else:
        missing = [key for key in CREDENTIALS if not config[key]]
        if missing:
            sys.exit(f"Missing in .env: {', '.join(missing)}")
        token = get_token(config)

    total_new = 0
    for sub in config["SUBREDDITS"]:
        if dry_run:
            listing = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        else:
            time.sleep(PAUSE_SECONDS)  # 2 s between every request, including after the token call
            try:
                listing = fetch_new(sub, token)
            except requests.RequestException as error:
                print(f"r/{sub}: skipped ({error})")
                continue
        matches = find_casting_calls(listing, config["KEYWORDS"])
        new = save_posts(conn, matches)
        print(f"r/{sub}: {len(matches)} casting call(s), {len(new)} new")
        for post in new:
            print(f"  + [{post['id']}] {post['title']}")
        total_new += len(new)
    print(f"Saved {total_new} new post(s).")


def list_posts(conn):
    rows = conn.execute(
        "SELECT created_utc, subreddit, title, url FROM posts ORDER BY created_utc DESC"
    ).fetchall()
    for created_utc, subreddit, title, url in rows:
        when = datetime.fromtimestamp(created_utc, timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"{when}  r/{subreddit}  {title}  {url}")
    print(f"{len(rows)} saved post(s).")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only daily casting-call reader.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="read /new once per subreddit, save casting calls")
    run_parser.add_argument(
        "--dry-run", action="store_true", help="use fixtures/sample_new.json instead of Reddit"
    )
    commands.add_parser("list", help="print saved posts, newest first")
    commands.add_parser("prune", help=f"delete saved posts older than {RETENTION_DAYS} days")
    args = parser.parse_args(argv)

    conn = open_db()
    try:
        if args.command == "run":
            run(load_config(), conn, dry_run=args.dry_run)
        elif args.command == "list":
            list_posts(conn)
        elif args.command == "prune":
            print(f"Deleted {prune(conn)} post(s) older than {RETENTION_DAYS} days.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
