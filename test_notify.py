# -*- coding: utf-8 -*-
"""
動作テスト用スクリプト。

各フィードの「一番上（最新）の記事」を1件だけDiscordに通知する。
既読状態やキューは一切変更しない(state/*.jsonに触れない)ため、
本番の通知フローに影響を与えずに「RSS取得→Discord送信」の疎通確認ができる。

使い方: cron-job.org のジョブで event_type=test-notify を送るか、
       ローカルで `python test_notify.py` を実行する。
"""

import json
import os

import logger
import notifier
import rss

_CONTEXT = "test-notify"
FEEDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feeds.json")


def load_feeds() -> list[dict]:
    with open(FEEDS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    logger.info(_CONTEXT, "===== テスト通知 開始（先頭1件のみ・既読状態は変更しません） =====")
    feeds = load_feeds()

    for feed in feeds:
        feed_id = feed["id"]
        feed_name = feed["name"]
        feed_url = feed["url"]
        webhook_env = feed["webhook_env"]

        try:
            articles = rss.fetch_articles(feed_url)
        except Exception as e:
            msg = logger.error(_CONTEXT, f"[{feed_id}] RSS取得失敗: {e}", exc=e)
            notifier.send_error(f"{_CONTEXT}:{feed_id}", msg, webhook_env=webhook_env)
            continue

        if not articles:
            logger.info(_CONTEXT, f"[{feed_id}] 記事が0件のため通知スキップ")
            continue

        top = articles[0]
        try:
            notifier.send_article(webhook_env, feed_name, f"[TEST] {top.title}", top.link)
        except Exception as e:
            msg = logger.error(_CONTEXT, f"[{feed_id}] Discord送信失敗: {e}", exc=e)
            notifier.send_error(f"{_CONTEXT}:{feed_id}", msg, webhook_env=webhook_env)

    logger.info(_CONTEXT, "===== テスト通知 終了 =====")


if __name__ == "__main__":
    main()
