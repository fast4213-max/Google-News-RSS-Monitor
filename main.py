# -*- coding: utf-8 -*-
"""
通常実行のエントリポイント（1時間毎に cron-job.org -> GitHub Actions 経由で起動される）。

処理の流れ (フィードごと):
  1. feeds.json からRSS URLを読む(max_per_run / stale_days が指定されていればそれを使う)
  2. RSSを取得・パース (rss.py)
  3. 前回までの既読ID (state/read_<id>.json) と比較し、未読記事を抽出
  3.5 公開日が stale_days 日より古い記事は、既読化のみして通知しない
      (Googleニュースの検索結果に急に大昔の記事が紛れ込むことがあるため)
  4. 前回持ち越しのキュー (state/queue_<id>.json) を先頭に結合
  5. 先頭 max_per_run 件だけDiscordに通知
  6. 通知できた分だけ既読化。通知しきれなかった分はキューに保存(破棄しない)
  7. RSS取得/パース/送信のいずれかで失敗したら Discord にエラー通知し、
     そのフィードの処理はスキップして次のフィードへ進む(1フィードの異常で全体を止めない)

このファイルが更新した state/*.json は、呼び出し元のGitHub Actionsワークフローが
git commit & push する(このスクリプト自体はgit操作を行わない)。
"""

import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import logger
import notifier
import rss
import state_manager

_CONTEXT = "main"
DEFAULT_MAX_NOTIFY_PER_RUN = 10  # 1フィードあたり1回で通知する上限件数のデフォルト値
DEFAULT_STALE_ARTICLE_DAYS = 3   # 記事の公開日がこれより古ければ「既読化のみ」で通知しないデフォルト値
# どちらも feeds.json 側で "max_per_run" / "stale_days" を指定すればフィードごとに上書きできる
# (例: 話題が広く更新の速い「中東情勢」だけ上限を増やす、期間を短くする、など)

FEEDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feeds.json")


def load_feeds() -> list[dict]:
    with open(FEEDS_PATH, "r", encoding="utf-8") as f:
        feeds = json.load(f)
    if not isinstance(feeds, list) or not feeds:
        raise RuntimeError("feeds.json が空、または配列ではありません")
    for feed in feeds:
        if "id" not in feed or "name" not in feed or "url" not in feed or "webhook_env" not in feed:
            raise RuntimeError(
                f"feeds.json の要素に id/name/url/webhook_env が揃っていません: {feed}"
            )
        # max_per_run / stale_days は任意項目。指定されている場合のみ型を確認する
        # (指定しなければ DEFAULT_MAX_NOTIFY_PER_RUN / DEFAULT_STALE_ARTICLE_DAYS が使われる)
        for optional_key in ("max_per_run", "stale_days"):
            if optional_key in feed and not isinstance(feed[optional_key], int):
                raise RuntimeError(
                    f"feeds.json の '{optional_key}' は整数で指定してください: {feed}"
                )
    return feeds


def is_stale(pub_date: str, stale_days: int, ctx: str) -> bool:
    """
    記事のpubDateが stale_days 日より古いかどうかを判定する。

    Googleニュースの検索結果は、稀に「半年前に書かれた記事」等が
    何かのきっかけで急に(今クロール/再インデックスされて)出現することがある。
    そのような記事は「新着」として通知する意味が薄いため、既読化だけして
    通知はスキップする。

    stale_days はフィードごとに feeds.json の "stale_days" で上書きできる
    (指定が無ければ DEFAULT_STALE_ARTICLE_DAYS を使う)。

    pub_date が空文字列だったり、想定外のフォーマットでパースできない場合は
    安全側に倒して stale とは判定しない(=通知する)。取りこぼしを防ぐため。
    """
    if not pub_date:
        return False
    try:
        published_at = parsedate_to_datetime(pub_date)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as e:
        logger.warn(ctx, f"pubDateのパースに失敗したため日付フィルタをスキップします: {pub_date!r} ({e})")
        return False

    threshold = datetime.now(timezone.utc) - timedelta(days=stale_days)
    return published_at < threshold


def process_feed(feed: dict) -> None:
    feed_id = feed["id"]
    feed_name = feed["name"]
    feed_url = feed["url"]
    webhook_env = feed["webhook_env"]
    max_per_run = feed.get("max_per_run", DEFAULT_MAX_NOTIFY_PER_RUN)
    stale_days = feed.get("stale_days", DEFAULT_STALE_ARTICLE_DAYS)
    ctx = f"feed:{feed_id}"

    # 1. RSS取得・パース
    try:
        articles = rss.fetch_articles(feed_url)
    except (rss.RssFetchError, rss.RssParseError) as e:
        msg = logger.error(ctx, f"RSS取得/解析に失敗しました: {e}", exc=e)
        notifier.send_error(ctx, msg, webhook_env=webhook_env)
        return
    except Exception as e:
        msg = logger.error(ctx, f"想定外のエラー(RSS処理): {e}", exc=e)
        notifier.send_error(ctx, msg, webhook_env=webhook_env)
        return

    logger.info(ctx, f"RSS取得成功: {len(articles)}件")

    # 2. 既読IDと比較して未読を抽出 (RSSは新しい順に並んでいる前提なので反転して古い順にする)
    read_ids = state_manager.load_read_ids(feed_id)
    unread_new = [a for a in reversed(articles) if a.id not in read_ids]

    # 2.5 公開日が stale_days 日より古い記事は「既読化のみ」で通知対象から外す
    #     (Googleニュースの検索結果に急に大昔の記事が紛れ込むことがあるため)
    stale_ids = set()
    fresh_unread = []
    for a in unread_new:
        if is_stale(a.pub_date, stale_days, ctx):
            stale_ids.add(a.id)
        else:
            fresh_unread.append(a)

    if stale_ids:
        read_ids = read_ids | stale_ids
        state_manager.save_read_ids(feed_id, read_ids)
        logger.info(
            ctx,
            f"公開日が{stale_days}日以上前の記事を{len(stale_ids)}件、"
            "通知せず既読化しました",
        )
    unread_new = fresh_unread

    # 3. 前回持ち越しキューを先頭に結合 (古いものを優先して通知するため)
    #    (queue内の記事は、キューに入った時点で既に新鮮度チェック済みのため、ここでは再チェックしない)
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

    # 4. Discord送信 (最大max_per_run件、このフィード専用のWebhookへ)
    try:
        sent = notifier.send_articles(webhook_env, feed_name, pending, max_per_run)
    except Exception as e:
        msg = logger.error(ctx, f"Discord送信中にエラー: {e}", exc=e)
        notifier.send_error(ctx, msg, webhook_env=webhook_env)
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
