# vocation-reddit-reader

The Reddit piece of VOcation, my personal voice-acting job tracker.

## Purpose

I'm a solo voice actor; this checks a few subreddits once a day for casting calls so I can apply by hand.

It reads the newest posts from a short list of subreddits, keeps the ones that look like casting calls (keyword match on title and body), and saves them to a SQLite file on my own machine. Nothing is sent anywhere else, and nothing is ever posted back to Reddit.

## What it accesses

| | |
|---|---|
| Endpoint | `GET https://oauth.reddit.com/r/{subreddit}/new?limit=100`, and nothing else |
| Auth | One token per run from `https://www.reddit.com/api/v1/access_token` (script app, password grant) |
| Scope | `read` |
| Fields kept | post id, subreddit, title, URL, created time, body text |
| Fields not kept | author, comments, votes, user profiles |

Request volume: **number of subreddits × 1 request per daily run**, plus 1 token request. With the 7 subreddits in `.env.example` that is 7 listing requests (8 HTTP requests in total) once a day, far below Reddit's free-tier limit of 100 queries per minute.

## Hard rules

These are enforced in [`reader.py`](reader.py), not just promised here.

- **Read-only.** The only data request is `GET /r/{sub}/new`. No posting, commenting, voting, messaging, user-profile lookups, or comment-thread crawling. Subreddit names are checked against `^[A-Za-z0-9_]{2,21}$`, so a config value cannot point the request at any other path.
- **Respect limits.** At most 1 listing request per subreddit per run (duplicate names are dropped), a 2-second pause before every request, `X-Ratelimit-Remaining` / `X-Ratelimit-Reset` honored (sleeps until reset when nothing is left), and on HTTP 429 it waits for `Retry-After` (or 60 s), retries once, then skips that subreddit for the day.
- **Descriptive User-Agent** on every request, in Reddit's required format: `python:vocation-reddit-reader:v0.1 (by /u/mattcavanaugh)`.
- **Data use.** Saved posts are for my personal use only. They are never shared or sold, and never used to train AI or ML models.
- **Retention.** `python reader.py prune` deletes saved posts older than 30 days.

## Setup

Requires Python 3.11 or newer.

1. Create a Reddit app of type **script** at <https://www.reddit.com/prefs/apps>.
2. Install the one dependency:
   ```
   python -m pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`. Adjust `SUBREDDITS` and `KEYWORDS` (both comma-separated) if you like. `.env` is gitignored.
4. Run it once a day with cron, for example at 8:00 AM:
   ```
   0 8 * * * cd /path/to/vocation-reddit-reader && /usr/bin/python3 reader.py run >> reader.log 2>&1
   ```

## Usage

```
python reader.py run            # read /new once per subreddit, save new casting calls
python reader.py run --dry-run  # same, but use fixtures/sample_new.json instead of calling Reddit
python reader.py list           # print saved posts, newest first, one per line
python reader.py prune          # delete saved posts older than 30 days
```

`--dry-run` needs no `.env` and makes no network calls. It feeds the sample file through the same filter and saves to `posts.db`, so running it twice shows dedupe at work (the second run saves 0). The sample posts and subreddit names are fictional.

## Tests

```
python -m unittest -v
```

Covers the keyword filter (exactly 3 of the 6 sample posts match), dedupe (saving the same posts twice adds nothing), and prune (an old post is deleted, a recent one is kept).

## Status: awaiting Reddit API approval

Until approval it only runs with `--dry-run` against the bundled sample file.

## License

MIT. See [LICENSE](LICENSE).
