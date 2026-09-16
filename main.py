# -*- coding: utf-8 -*-
"""
通常実行のエントリポイント（1時間毎に cron-job.org -> GitHub Actions 経由で起動される）。

処理の流れ (フィードごと):
  1. feeds.json からRSS URLを読む
  2. RSSを取得・パース (rss.py)
  3. 前回までの既読ID (state/read_<id>.json) と比較し、未読記事を抽出
  4. 前回持ち越しのキュー (state/queue_<id>.json) を先頭に結合
  5. 先頭 MAX_NOTIFY_PER_RUN 件だけDiscordに通知
  6. 通知できた分だけ既読化。通知しきれなかった分はキューに保存(破棄しない)
  7. RSS取得/パース/送信のいずれかで失敗したら Discord にエラー通知し、
     そのフィードの処理はスキップして次のフィードへ進む(1フィードの異常で全体を止めない)

このファイルが更新した state/*.json は、呼び出し元のGitHub Actionsワークフローが
git commit & push する(このスクリプト自体はgit操作を行わない)。
"""

import json
import os

import logger
import notifier
import rss
import state_manager

_CONTEXT = "main"
MAX_NOTIFY_PER_RUN = 10  # Discordのレート制限を避けるための1フィードあたり1回の上限

FEEDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feeds.json")


def load_feeds() -> list[dict]:
    with open(FEEDS_PATH, "r", encoding="utf-8") as f:
        feeds = json.load(f)
    if not isinstance(feeds, list) or not feeds:
        raise RuntimeError("feeds.json が空、または配列ではありません")
    for feed in feeds:
        if "id" not in feed or "name" not in feed or "url" not in feed:
            raise RuntimeError(f"feeds.json の要素に id/name/url が揃っていません: {feed}")
    return feeds


def process_feed(feed: dict) -> None:
    feed_id = feed["id"]
    feed_name = feed["name"]
    feed_url = feed["url"]
    ctx = f"feed:{feed_id}"

    # 1. RSS取得・パース
    try:
        articles = rss.fetch_articles(feed_url)
    except (rss.RssFetchError, rss.RssParseError) as e:
        msg = logger.error(ctx, f"RSS取得/解析に失敗しました: {e}", exc=e)
        notifier.send_error(ctx, msg)
        return
    except Exception as e:
        msg = logger.error(ctx, f"想定外のエラー(RSS処理): {e}", exc=e)
        notifier.send_error(ctx, msg)
        return

    logger.info(ctx, f"RSS取得成功: {len(articles)}件")

    # 2. 既読IDと比較して未読を抽出 (RSSは新しい順に並んでいる前提なので反転して古い順にする)
    read_ids = state_manager.load_read_ids(feed_id)
    unread_new = [a for a in reversed(articles) if a.id not in read_ids]

    # 3. 前回持ち越しキューを先頭に結合 (古いものを優先して通知するため)
    queued = state_manager.load_queue(feed_id)
    queued_ids = {q["id"] for q in queued}
    # キューにあるがすでに既読扱いになっているものは除外(念のための整合性チェック)
    queued = [q for q in queued if q["id"] not in read_ids]

    # 新規未読のうち、キューに重複して入っているものは除外
    unread_new_dicts = [
        {"id": a.id, "title": a.title, "link": a.link, "pub_date": a.pub_date}
        for a in unread_new
        if a.id not in queued_ids
    ]

    pending = queued + unread_new_dicts  # 通知すべき全件(古い順)

    if not pending:
        logger.info(ctx, "未読記事なし。通知スキップ")
        return

    logger.info(ctx, f"未読合計: {len(pending)}件 (うち持ち越し{len(queued)}件)")

    # 4. Discord送信 (最大MAX_NOTIFY_PER_RUN件)
    try:
        sent = notifier.send_articles(feed_name, pending, MAX_NOTIFY_PER_RUN)
    except Exception as e:
        msg = logger.error(ctx, f"Discord送信中にエラー: {e}", exc=e)
        notifier.send_error(ctx, msg)
        # 送信に失敗した場合、状態は変更せず次回に持ち越す(pendingをそのままqueueに保存)
        state_manager.save_queue(feed_id, pending)
        return

    # 5. 送信できた分だけ既読化し、残りはqueueに保存(破棄しない)
    sent_ids = {a["id"] for a in sent}
    new_read_ids = read_ids | sent_ids
    state_manager.save_read_ids(feed_id, new_read_ids)

    remaining = [p for p in pending if p["id"] not in sent_ids]
    state_manager.save_queue(feed_id, remaining)

    logger.info(
        ctx,
        f"通知完了: {len(sent)}件送信 / {len(remaining)}件を次回に持ち越し",
    )


def main() -> None:
    logger.info(_CONTEXT, "===== 通常実行 開始 =====")
    try:
        feeds = load_feeds()
    except Exception as e:
        msg = logger.error(_CONTEXT, f"feeds.json の読み込みに失敗しました: {e}", exc=e)
        notifier.send_error("feeds-load", msg)
        return

    for feed in feeds:
        process_feed(feed)

    logger.info(_CONTEXT, "===== 通常実行 終了 =====")


if __name__ == "__main__":
    main()
