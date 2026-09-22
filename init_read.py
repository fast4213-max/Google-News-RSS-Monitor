# -*- coding: utf-8 -*-
"""
初回セットアップ用スクリプト。

RSSに現在載っている記事を「全部既読」にするだけで、Discordへの通知は一切送らない。
初めてこのシステムを導入するとき、いきなり過去記事が大量通知されるのを防ぐために使う。

使い方: cron-job.org のジョブで event_type=init-read を送るか、
       手動で GitHub Actions の workflow_dispatch(あれば) / ローカルで
       `python init_read.py` を実行する。

注意: これを実行した「後」に main.py を動かすと、実行時点以降に増えた記事だけが
      通知対象になる。
"""

import json
import os

import logger
import notifier
import rss
import state_manager

_CONTEXT = "init-read"
FEEDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feeds.json")


def load_feeds() -> list[dict]:
    with open(FEEDS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    logger.info(_CONTEXT, "===== 初回既読化 開始（通知は送信されません） =====")
    feeds = load_feeds()

    for feed in feeds:
        feed_id = feed["id"]
        feed_url = feed["url"]
        try:
            articles = rss.fetch_articles(feed_url)
        except Exception as e:
            msg = logger.error(_CONTEXT, f"[{feed_id}] RSS取得失敗のためスキップ: {e}", exc=e)
            notifier.send_error(f"{_CONTEXT}:{feed_id}", msg)
            continue

        # 既存の既読IDは残したまま、今RSSに載っている分を追記する
        # (万一再実行しても安全。RSSは新しい順なので、古い順になるよう反転して渡す)
        ids = [a.id for a in reversed(articles)]
        state_manager.append_read_ids(feed_id, ids)

        # 持ち越しキューが残っていた場合は初期化時にクリアする
        state_manager.save_queue(feed_id, [])

        logger.info(_CONTEXT, f"[{feed_id}] {len(set(ids))}件を既読化しました")

    logger.info(_CONTEXT, "===== 初回既読化 完了 =====")


if __name__ == "__main__":
    main()
