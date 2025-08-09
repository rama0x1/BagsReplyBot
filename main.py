#!/usr/bin/env python3
import os
import time
import re
import requests
import sqlite3
import logging
from datetime import datetime

# --- Config from environment ---
TW_BEARER = os.environ.get("TWITTER_BEARER_TOKEN", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
LAUNCH_ACCOUNT = os.environ.get("LAUNCH_ACCOUNT", "LaunchOnBags").lstrip("@")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 30))

if not TW_BEARER or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("ERROR: set TWITTER_BEARER_TOKEN, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.")
    exit(1)

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --- DB ---
DB_PATH = os.environ.get("DB_PATH", "bot_state.sqlite3")

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tracked_tweets (
    tweet_id TEXT PRIMARY KEY,
    beneficiary_username TEXT,
    beneficiary_id TEXT,
    contract TEXT,
    created_at TEXT,
    notified INTEGER DEFAULT 0
);
"""

# --- Regex for extraction ---
PATTERN = re.compile(r"royalties\s+shared\s+with\s+@([A-Za-z0-9_]{1,15}).*?([1-9A-HJ-NP-Za-km-z]{32,44})", re.I | re.S)

HEADERS = {"Authorization": f"Bearer {TW_BEARER}"}

# --- Helpers ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(CREATE_TABLE_SQL)
    conn.commit()
    conn.close()

def db_insert_tracked(tweet_id, beneficiary_username, beneficiary_id, contract):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO tracked_tweets (tweet_id, beneficiary_username, beneficiary_id, contract, created_at) VALUES (?, ?, ?, ?, ?);",
                    (tweet_id, beneficiary_username, beneficiary_id, contract, datetime.utcnow().isoformat()))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

def db_get_unnotified():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT tweet_id, beneficiary_username, beneficiary_id, contract FROM tracked_tweets WHERE notified=0;")
    rows = cur.fetchall()
    conn.close()
    return rows

def db_mark_notified(tweet_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE tracked_tweets SET notified=1 WHERE tweet_id=?;", (tweet_id,))
    conn.commit()
    conn.close()

# --- Twitter API helpers ---
def tw_get(path, params=None):
    url = f"https://api.twitter.com/2{path}"
    r = requests.get(url, headers=HEADERS, params=params, timeout=15)
    if r.status_code == 200:
        return r.json()
    else:
        logging.warning("Twitter API %s returned %s: %s", path, r.status_code, r.text)
        return None

def get_user_by_username(username):
    j = tw_get(f"/users/by/username/{username}", params={"user.fields":"public_metrics,description"})
    if not j or "data" not in j:
        return None
    return j["data"]

def get_user_tweets(user_id, max_results=5):
    params = {"max_results": max_results, "tweet.fields":"conversation_id,created_at,referenced_tweets"}
    j = tw_get(f"/users/{user_id}/tweets", params=params)
    if not j:
        return []
    return j.get("data", [])

def get_tweet_retweeters(tweet_id):
    j = tw_get(f"/tweets/{tweet_id}/retweeted_by", params={"user.fields":"id,username"})
    if not j:
        return []
    return j.get("data", [])

def search_recent(query, max_results=10):
    params = {"query": query, "max_results": max_results, "tweet.fields":"author_id,created_at"}
    j = tw_get("/tweets/search/recent", params=params)
    if not j:
        return []
    return j.get("data", [])

# --- Telegram ---
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode":"HTML"}
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            logging.warning("Telegram send failed %s %s", r.status_code, r.text)
            return False
        return True
    except Exception as e:
        logging.exception("Telegram API error: %s", e)
        return False

# --- Detect pattern in tweet text ---
def extract_beneficiary_and_contract(text):
    m = PATTERN.search(text or "")
    if not m:
        return None, None
    return m.group(1), m.group(2)

# --- Checks ---
def check_retweet(tweet_id, beneficiary_id):
    users = get_tweet_retweeters(tweet_id)
    for u in users:
        if str(u.get("id")) == str(beneficiary_id):
            return True
    return False

def check_reply(tweet_id, beneficiary_username):
    query = f"conversation_id:{tweet_id} from:{beneficiary_username}"
    res = search_recent(query)
    return len(res) > 0

def check_quote_via_user_tweets(beneficiary_id, tweet_id):
    tweets = get_user_tweets(beneficiary_id, max_results=20)
    for t in tweets:
        refs = t.get("referenced_tweets") or []
        for r in refs:
            if str(r.get("id")) == str(tweet_id):
                return True
    return False

# --- Main loop ---
def main():
    init_db()

    # Cache launch account ID once
    launch_user = get_user_by_username(LAUNCH_ACCOUNT)
    if not launch_user:
        logging.error("Cannot find launch account @%s", LAUNCH_ACCOUNT)
        return
    launch_id = launch_user["id"]
    logging.info("Launch account @%s has ID %s", LAUNCH_ACCOUNT, launch_id)

    beneficiary_cache = {}  # username -> (id, followers, bio)

    while True:
        try:
            tweets = get_user_tweets(launch_id, max_results=5)
            if tweets:
                for t in reversed(tweets):
                    tid = t["id"]
                    text = t.get("text","")
                    beneficiary_username, contract = extract_beneficiary_and_contract(text)
                    if beneficiary_username and contract:
                        if beneficiary_username not in beneficiary_cache:
                            u = get_user_by_username(beneficiary_username)
                            if u:
                                beneficiary_cache[beneficiary_username] = (
                                    u["id"],
                                    u.get("public_metrics", {}).get("followers_count", "?"),
                                    u.get("description", "")
                                )
                            else:
                                logging.warning("Could not resolve beneficiary @%s", beneficiary_username)
                                continue
                        beneficiary_id = beneficiary_cache[beneficiary_username][0]
                        db_insert_tracked(tid, beneficiary_username, beneficiary_id, contract)
                        logging.info("Tracking tweet %s -> beneficiary @%s contract %s", tid, beneficiary_username, contract)

            rows = db_get_unnotified()
            for tweet_id, beneficiary_username, beneficiary_id, contract in rows:
                action = None
                if check_retweet(tweet_id, beneficiary_id):
                    action = "retweet"
                elif check_reply(tweet_id, beneficiary_username):
                    action = "reply"
                elif check_quote_via_user_tweets(beneficiary_id, tweet_id):
                    action = "quote"
                if action:
                    _, followers, bio = beneficiary_cache.get(beneficiary_username, ("?", "?", ""))
                    msg = (f"🚨 <b>Beneficiary action detected</b>\n\n"
                           f"User: @{beneficiary_username}\n"
                           f"Followers: {followers}\n"
                           f"Bio: {bio}\n"
                           f"Contract: <code>{contract}</code>\n"
                           f"Action: {action}\n"
                           f"Tweet: https://twitter.com/{LAUNCH_ACCOUNT}/status/{tweet_id}")
                    send_telegram(msg)
                    db_mark_notified(tweet_id)
                    logging.info("Notified for tweet %s action %s", tweet_id, action)

            time.sleep(POLL_INTERVAL)

        except Exception as e:
            logging.exception("Main loop error: %s", e)
            time.sleep(10)

if __name__ == "__main__":
    main()
