# -*- coding: utf-8 -*-
"""
通常実行のエントリポイント（1時間毎に cron-job.org -> GitHub Actions 経由で起動される）。

処理の流れ (フィードごと):
  1. feeds.json からRSS URLを読む(max_per_run / stale_days / fresh_hours が指定されていればそれを使う)
  2. RSSを取得・パース (rss.py)
  3. 前回までの既読ID (state/read_<id>.json) と持ち越しキュー (state/queue_<id>.json) を読み、
     そのどちらにも入っていない記事を「今回の未読」として抽出
     (キューの記事は前回「通知する」と判定済みなので、以降の日付/重複判定にはかけない)
  3.4 exclude_words に該当する記事は、既読化のみして通知しない
      (そのフィードでは扱わない話題。別フィード/別チャンネルに任せる場合に使う)
  3.45 地域名が放送局名など(例「関西テレビ」)の一部としてしか出てこない記事は、
      地域の誤マッチとみなして既読化のみし、通知しない (area_words / area_false_match_words)
  3.5 公開日が stale_days 日より古い記事は、既読化のみして通知しない
      (Googleニュースの検索結果に急に大昔の記事が紛れ込むことがあるため)
  3.6 公開日が fresh_hours 時間より古い記事は「古い記事」として印を付け、
      すでに通知済みの話題の後追い記事であれば通知しない。
      まだ一度も通知していない話題であれば、古くても通知する(見逃し防止)
  4. 持ち越しキューを先頭に結合 (古いものから順に通知するため)
  5. 先頭 max_per_run 件だけDiscordに通知
  6. 通知できた分だけ既読化。通知しきれなかった分はキューに保存(破棄しない)
  7. RSS取得/パース/送信のいずれかで失敗したら Discord にエラー通知し、
     そのフィードの処理はスキップして次のフィードへ進む(1フィードの異常で全体を止めない)

このファイルが更新した state/*.json は、呼び出し元のGitHub Actionsワークフローが
git commit & push する(このスクリプト自体はgit操作を行わない)。
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import dedup
import logger
import notifier
import rss
import state_manager

_CONTEXT = "main"
_JST = timezone(timedelta(hours=9))
# 「あす22日は」のような、配信元が翌日を指して使う相対日付表現。
# Googleニュースへの掲載や本スクリプトの通知が遅れると、実際にはとっくに
# 過ぎた日を指したまま「あす」という表現だけが残り、内容が誤解を招く
# (例: 23日に届いた通知が「あす22日は晴れ」と案内する)。
_RELATIVE_DATE_TITLE_RE = re.compile(r"(?:あす|明日)(\d{1,2})日")
DEFAULT_MAX_NOTIFY_PER_RUN = 50  # 1フィードあたり1回で通知する上限件数のデフォルト値
DEFAULT_STALE_ARTICLE_DAYS = 3   # 記事の公開日がこれより古ければ「既読化のみ」で通知しないデフォルト値(日単位、大昔の記事対策)
DEFAULT_FRESH_HOURS = 3          # 記事の公開日がこれより古ければ「古い記事」として扱うデフォルト値(時間単位)
                                 # 「古い記事」は、すでに通知済みの話題の後追いなら通知せず、
                                 # 初めての話題なら(見逃し防止のため)通知する。詳細は dedup.classify_articles 参照
# いずれも feeds.json 側で "max_per_run" / "stale_days" / "fresh_hours" を指定すればフィードごとに上書きできる
# (例: 話題が広く更新の速い「中東情勢」だけ上限を増やす、期間を短くする、など)

# 同一話題(複数社が同じ出来事を別記事で配信したもの)をまとめるクラスタリングのデフォルト値。
# いずれも feeds.json 側で "dedup_first_n" / "dedup_similarity_threshold" /
# "dedup_batch_cooldown_minutes" を指定すればフィードごとに上書きできる。
# "dedup_first_n" に 0 を指定すると「1波あたりの上限なし」になり、同じ話題の続報も
# (古い後追い記事以外は)すべて通知する。自然災害系のフィード向けの設定。
DEFAULT_DEDUP_FIRST_N = dedup.DEFAULT_FIRST_N
DEFAULT_DEDUP_SIMILARITY_THRESHOLD = dedup.DEFAULT_SIMILARITY_THRESHOLD
DEFAULT_DEDUP_BATCH_COOLDOWN_MINUTES = dedup.DEFAULT_BATCH_COOLDOWN_MINUTES
DEFAULT_DEDUP_SAME_SOURCE_THRESHOLD = dedup.DEFAULT_SAME_SOURCE_THRESHOLD

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
        # 以下は任意項目。指定されている場合のみ型を確認する
        # (指定しなければこのファイル冒頭の DEFAULT_* が使われる)
        for optional_key in (
            "max_per_run",
            "stale_days",
            "fresh_hours",
            "dedup_first_n",
            "dedup_batch_cooldown_minutes",
        ):
            # bool は int のサブクラスなので、true/false の書き間違いを明示的に弾く
            if optional_key in feed and (
                not isinstance(feed[optional_key], int) or isinstance(feed[optional_key], bool)
            ):
                raise RuntimeError(
                    f"feeds.json の '{optional_key}' は整数で指定してください: {feed}"
                )
        # 件数・期間系は 1以上 だけを許可する。0 や負の数(打ち間違い)を通すと、
        #   - max_per_run: 0 → 1件も通知されずキューが増え続ける / 負 → 末尾の記事だけ持ち越され続ける
        #   - stale_days / fresh_hours: 0以下 → 全記事が「古い」扱いになり、
        #     stale_days の場合は通知されないまま全件既読化される
        # という「エラーにならずに黙って通知が止まる」事故になるため、起動時に止める。
        for positive_key in ("max_per_run", "stale_days", "fresh_hours"):
            if positive_key in feed and feed[positive_key] < 1:
                raise RuntimeError(
                    f"feeds.json の '{positive_key}' は1以上で指定してください: {feed}"
                )
        if "dedup_batch_cooldown_minutes" in feed and feed["dedup_batch_cooldown_minutes"] < 0:
            raise RuntimeError(
                f"feeds.json の 'dedup_batch_cooldown_minutes' は0以上で指定してください: {feed}"
            )
        # dedup_first_n は 0 に「1波あたりの上限なし」という意味を持たせているため、
        # 負の数(打ち間違い)は弾いて 0以上 だけを許可する
        if "dedup_first_n" in feed and feed["dedup_first_n"] < 0:
            raise RuntimeError(
                f"feeds.json の 'dedup_first_n' は0以上で指定してください"
                f"(0は「1波あたりの上限なし」の意味): {feed}"
            )
        for threshold_key in ("dedup_similarity_threshold", "dedup_same_source_threshold"):
            threshold = feed.get(threshold_key)
            if threshold is not None and not (
                isinstance(threshold, (int, float)) and 0 <= threshold <= 1
            ):
                raise RuntimeError(
                    f"feeds.json の '{threshold_key}' は0〜1の数値で指定してください: {feed}"
                )
        for words_key in ("area_words", "area_false_match_words"):
            words = feed.get(words_key)
            if words is not None and not (
                isinstance(words, list) and all(isinstance(w, str) for w in words)
            ):
                raise RuntimeError(
                    f"feeds.json の '{words_key}' は文字列の配列で指定してください: {feed}"
                )
        exclude_words = feed.get("exclude_words")
        if exclude_words is not None and not (
            isinstance(exclude_words, list) and all(isinstance(w, str) for w in exclude_words)
        ):
            raise RuntimeError(
                f"feeds.json の 'exclude_words' は文字列の配列で指定してください: {feed}"
            )
        ignore_words = feed.get("dedup_ignore_words")
        if ignore_words is not None and not (
            isinstance(ignore_words, list) and all(isinstance(w, str) for w in ignore_words)
        ):
            raise RuntimeError(
                f"feeds.json の 'dedup_ignore_words' は文字列の配列で指定してください: {feed}"
            )
    return feeds


def is_older_than(pub_date: str, threshold: timedelta, ctx: str) -> bool:
    """
    記事のpubDateが、現在時刻から threshold 分より古いかどうかを判定する。

    Googleニュースの検索結果は、記事の公開日とは無関係に「Google側が最近
    クロール/再インデックスした記事」が急に上位に出てくることがある
    (半年前の記事だったり、逆に同日でも配信から何時間も経った記事だったりする)。
    そのような記事は「新着」として通知する意味が薄いため、既読化だけして
    通知はスキップする。

    pub_date が空文字列だったり、想定外のフォーマットでパースできない場合は
    安全側に倒して古いとは判定しない(=通知する)。取りこぼしを防ぐため。
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

    return published_at < datetime.now(timezone.utc) - threshold


def is_stale(pub_date: str, stale_days: int, ctx: str) -> bool:
    """
    記事のpubDateが stale_days 日より古いかどうかを判定する。
    「半年前の記事が急に出てくる」ような、あからさまに大昔の記事を弾くための
    粗いフィルタ(日単位)。stale_days はフィードごとに feeds.json の
    "stale_days" で上書きできる(指定が無ければ DEFAULT_STALE_ARTICLE_DAYS を使う)。
    """
    return is_older_than(pub_date, timedelta(days=stale_days), ctx)


def is_too_old_for_fresh_notify(pub_date: str, fresh_hours: int, ctx: str) -> bool:
    """
    記事のpubDateが fresh_hours 時間より古いかどうかを判定する。
    is_stale (日単位)よりもっと厳しく、「同日ではあるが配信から何時間も
    経った記事がGoogle側の都合で急に出てくる」ケースを弾くための時間単位フィルタ。
    fresh_hours はフィードごとに feeds.json の "fresh_hours" で上書きできる
    (指定が無ければ DEFAULT_FRESH_HOURS を使う)。
    """
    return is_older_than(pub_date, timedelta(hours=fresh_hours), ctx)


def is_stale_relative_date_title(title: str, pub_date: str, ctx: str) -> bool:
    """
    タイトルに「あす22日は」のような相対日付表現が含まれる記事が、
    通知しようとしている時点ですでに古くなっていないかを判定する。

    「あす(N)日」は本来、配信元がその記事を出した翌日がN日であることを
    示す表現。ところがGoogleニュース側の掲載遅延やRSS取得間隔により、
    実際に通知されるのがN日を過ぎてからになることがあり、その場合
    「あす22日は晴れ」のような予報がすでに終わった日の話として届いてしまい、
    利用者を混乱させる。

    そこで、タイトルの「あす(N)日」が指す実際の日付(公開日の翌日)を求め、
    それが通知しようとしている時点(JST)の今日より前になっていれば、
    古い記事とみなす。タイトルにこの表現が無い記事や、pub_dateが無い/
    パースできない記事、翌日の日付とNが一致しない(表現の意味を誤読している
    可能性がある)場合は、安全側に倒して古いとは判定しない(=通知する)。
    """
    m = _RELATIVE_DATE_TITLE_RE.search(title)
    if not m or not pub_date:
        return False
    try:
        published_at = parsedate_to_datetime(pub_date)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as e:
        logger.warn(ctx, f"pubDateのパースに失敗したため相対日付フィルタをスキップします: {pub_date!r} ({e})")
        return False

    target_day = int(m.group(1))
    published_jst = published_at.astimezone(_JST)
    target_date = (published_jst + timedelta(days=1)).date()
    if target_date.day != target_day:
        # 「あす(N)日」が公開日の翌日と一致しない = この表現を誤読している
        # 可能性があるため、安全側に倒して通知する
        return False

    return datetime.now(_JST).date() > target_date


def is_false_area_match(
    title: str, area_words: list[str], false_match_words: list[str]
) -> bool:
    """
    タイトルに出てくる地域名が、放送局名や社名の一部でしかない(=その記事は
    その地域の話ではない)かどうかを判定する。

    GoogleニュースのRSSは「(大阪 OR 近畿 OR 関西) AND (台風 OR 大雨 ...)」のような
    検索式で記事を拾うが、この地域名の判定は本文や配信元名まで含めて行われる。
    そのため「【台風・大雨解説】…関東各地で記録的な大雨…(関西テレビ) - Yahoo!ニュース」
    のように、関東の話なのに配信元が「関西テレビ」であるだけの記事が混ざってしまう。

    そこで、
      - タイトルに地域名(area_words)が含まれている
      - しかし false_match_words(例「関西テレビ」)を取り除くと地域名が1つも残らない
    という記事だけを「地域の誤マッチ」と判定する。

    タイトルに地域名がもともと1つも無い記事は、地域名が本文側にあって拾われた
    (例「大雨、死者5人・不明6人に」)可能性があり、タイトルだけでは判断できないため
    誤マッチとはみなさない(災害情報の取りこぼしを避けるための安全側の判定)。
    """
    if not area_words or not false_match_words:
        return False
    if not any(w in title for w in area_words):
        return False
    stripped = title
    for w in false_match_words:
        stripped = stripped.replace(w, "")
    return not any(w in stripped for w in area_words)


def process_feed(feed: dict) -> None:
    feed_id = feed["id"]
    feed_name = feed["name"]
    feed_url = feed["url"]
    webhook_env = feed["webhook_env"]
    max_per_run = feed.get("max_per_run", DEFAULT_MAX_NOTIFY_PER_RUN)
    stale_days = feed.get("stale_days", DEFAULT_STALE_ARTICLE_DAYS)
    fresh_hours = feed.get("fresh_hours", DEFAULT_FRESH_HOURS)
    dedup_first_n = feed.get("dedup_first_n", DEFAULT_DEDUP_FIRST_N)
    dedup_similarity_threshold = feed.get(
        "dedup_similarity_threshold", DEFAULT_DEDUP_SIMILARITY_THRESHOLD
    )
    dedup_batch_cooldown_minutes = feed.get(
        "dedup_batch_cooldown_minutes", DEFAULT_DEDUP_BATCH_COOLDOWN_MINUTES
    )
    # 類似度の計算から除外する語。
    # フィード名(=検索キーワード。例「大東市」)は、そのフィードの記事全件に必ず含まれるため
    # 話題を区別する情報を持たない。にも関わらず類似度は押し上げてしまい、無関係な記事同士が
    # 同じ話題と誤判定される原因になるので、常に除外する。
    # feeds.json の "dedup_ignore_words" で、都道府県名など他の共通語も追加できる。
    dedup_ignore_words = [feed_name] + list(feed.get("dedup_ignore_words", []))
    dedup_same_source_threshold = feed.get(
        "dedup_same_source_threshold", DEFAULT_DEDUP_SAME_SOURCE_THRESHOLD
    )
    # タイトルにこれらの語を含む記事は、このフィードでは通知しない(既読化のみ)。
    # 「大東市の一般ニュース」と「大東市の災害情報」のように、同じ記事を拾う
    # フィードが複数ある場合に、チャンネルごとの住み分けをするために使う。
    exclude_words = list(feed.get("exclude_words", []))
    # 地域名(area_words)が、放送局名など(area_false_match_words)の一部としてしか
    # タイトルに出てこない記事は、地域の誤マッチとして通知しない。詳細は is_false_area_match 参照。
    area_words = list(feed.get("area_words", []))
    area_false_match_words = list(feed.get("area_false_match_words", []))
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

    # 2.1 前回持ち越しキューを「未読抽出より先に」読み込む。
    #     キューの記事は前回の実行で既に「通知する」と判定済みで、まだ送れていないだけ。
    #     Googleニュースの検索RSSは同じ記事を何時間も載せ続けるため、これらは今回の
    #     RSSにもそのまま出てくる。ここで除外しておかないと、下の stale / dedup 判定に
    #     もう一度かけられ、「公開から時間が経った後追い記事」とみなされて既読化され、
    #     キューからも消える(=一度も通知されないまま失われる)。
    #     持ち越し記事の再判定は行わない、というのが本来の設計(手順3のコメント参照)。
    queued = state_manager.load_queue(feed_id)
    queued_ids = {q["id"] for q in queued}

    unread_new = [
        a for a in reversed(articles) if a.id not in read_ids and a.id not in queued_ids
    ]

    # 2.5 公開日が stale_days 日より古い記事は「既読化のみ」で通知対象から外す
    #     (Googleニュースの検索結果に急に大昔の記事が紛れ込むことがあるため)
    #     ※ 既読IDは「古い順に並んだリスト」で追記する(順序が崩れると上限超過時に
    #        ランダムなIDが消えて再通知が起きるため。state_manager 参照)
    stale_ids = [a.id for a in unread_new if is_stale(a.pub_date, stale_days, ctx)]
    if stale_ids:
        state_manager.append_read_ids(feed_id, stale_ids)
        read_ids = read_ids | set(stale_ids)
        logger.info(
            ctx,
            f"公開日が{stale_days}日以上前の記事を{len(stale_ids)}件、"
            "通知せず既読化しました",
        )
    unread_new = [a for a in unread_new if a.id not in read_ids]

    # 2.52 タイトルの「あす22日は」等の相対日付表現が指す日付がすでに過ぎている記事は、
    #      通知しても内容が古く誤解を招くだけなので、既読化のみして通知しない。
    #      詳細は is_stale_relative_date_title 参照。
    stale_relative_date_ids = [
        a.id for a in unread_new if is_stale_relative_date_title(a.title, a.pub_date, ctx)
    ]
    if stale_relative_date_ids:
        state_manager.append_read_ids(feed_id, stale_relative_date_ids)
        read_ids = read_ids | set(stale_relative_date_ids)
        logger.info(
            ctx,
            f"「あす(N)日は」等の相対日付表現が指す日付を過ぎた記事を"
            f"{len(stale_relative_date_ids)}件、通知せず既読化しました",
        )
    unread_new = [a for a in unread_new if a.id not in read_ids]

    # 2.55 exclude_words にマッチする記事は、このフィードでは通知せず既読化だけする。
    #      同じ記事を別のフィード(別チャンネル)が拾う前提の住み分け用で、
    #      例えば「大東市」フィードから災害・警報系の記事を外し、
    #      それらは「大東市の災害情報」フィード(自然災害チャンネル)に任せる、という使い方をする。
    #      ここで既読化しておかないと、毎回同じ記事を判定し続けることになる。
    if exclude_words:
        excluded_ids = [
            a.id for a in unread_new if any(w in a.title for w in exclude_words)
        ]
        if excluded_ids:
            state_manager.append_read_ids(feed_id, excluded_ids)
            read_ids = read_ids | set(excluded_ids)
            logger.info(
                ctx,
                f"exclude_words に該当する記事を{len(excluded_ids)}件、通知せず既読化しました",
            )
        unread_new = [a for a in unread_new if a.id not in read_ids]

    # 2.56 地域名が放送局名など(例「関西テレビ」)の一部としてしか出てこない記事は、
    #      その地域の話ではないので通知せず既読化だけする。
    #      GoogleニュースのRSSは配信元名も検索対象にしてしまうため、
    #      「関東の大雨の記事を関西テレビが配信した」ものが (大阪 OR 近畿 OR 関西) の
    #      検索式に引っかかって届いてしまうのを防ぐ。
    if area_words and area_false_match_words:
        false_area_ids = [
            a.id
            for a in unread_new
            if is_false_area_match(a.title, area_words, area_false_match_words)
        ]
        if false_area_ids:
            state_manager.append_read_ids(feed_id, false_area_ids)
            read_ids = read_ids | set(false_area_ids)
            logger.info(
                ctx,
                f"地域名が配信元名などの一部でしかない記事を{len(false_area_ids)}件、"
                "通知せず既読化しました",
            )
        unread_new = [a for a in unread_new if a.id not in read_ids]

    # 2.6 同一話題(複数社が同じ出来事を別記事で配信したもの)をクラスタリングして間引く。
    #     - すでに通知済みの話題 … 最速 dedup_first_n 件まではそのまま通知。それ以降は、
    #       クールダウン(pubDate差・壁時計差の両方)を満たした場合のみ次の波として通知する。
    #       ただし公開から fresh_hours 時間以上経った記事(=後追い報道)は通知しない。
    #       dedup_first_n が0の場合は波の上限なし(後追い記事以外はすべて通知)
    #     - 初めての話題 … 公開から時間が経っていても通知する(見逃し防止)
    old_ids = {
        a.id for a in unread_new if is_too_old_for_fresh_notify(a.pub_date, fresh_hours, ctx)
    }
    unread_new_dicts_raw = [
        {"id": a.id, "title": a.title, "link": a.link, "pub_date": a.pub_date}
        for a in unread_new
    ]
    to_notify_dicts, dedup_skip_ids = dedup.classify_articles(
        feed_id,
        unread_new_dicts_raw,
        first_n=dedup_first_n,
        similarity_threshold=dedup_similarity_threshold,
        batch_cooldown_minutes=dedup_batch_cooldown_minutes,
        old_ids=old_ids,
        ignore_words=dedup_ignore_words,
        same_source_threshold=dedup_same_source_threshold,
    )
    if dedup_skip_ids:
        # 記事の並び(古い順)を保ったまま既読化する
        skipped_in_order = [d["id"] for d in unread_new_dicts_raw if d["id"] in dedup_skip_ids]
        state_manager.append_read_ids(feed_id, skipped_in_order)
        read_ids = read_ids | dedup_skip_ids
        old_skipped = len(dedup_skip_ids & old_ids)
        logger.info(
            ctx,
            f"同一話題の重複記事を{len(dedup_skip_ids)}件、通知せず既読化しました"
            f"(うち公開から{fresh_hours}時間以上経った後追い記事{old_skipped}件)",
        )

    # 3. 前回持ち越しキュー(手順2.1で読み込み済み)を先頭に結合 (古いものを優先して通知するため)
    #    (queue内の記事は、キューに入った時点で既に新鮮度チェック済み・クラスタ判定済みのため、
    #     ここでは再チェックしない)
    # キューにあるがすでに既読扱いになっているものは除外(念のための整合性チェック)。
    # 手順2.1でキュー内のIDを未読抽出から除外しているため、通常ここでは何も落ちない。
    # 落ちるのは過去の不整合データが残っていた場合だけ。
    queued_alive = [q for q in queued if q["id"] not in read_ids]

    # 新規未読のうち、キューに重複して入っているものは除外
    unread_new_dicts = [d for d in to_notify_dicts if d["id"] not in queued_ids]

    pending = queued_alive + unread_new_dicts  # 通知すべき全件(古い順)

    if not pending:
        # 既読化済みのゴミがキューに残っていた場合はここで掃除する
        # (掃除しないと state/queue_<id>.json に永遠に残り続ける)
        if len(queued_alive) != len(queued):
            state_manager.save_queue(feed_id, queued_alive)
            logger.info(
                ctx,
                f"既読化済みの記事{len(queued) - len(queued_alive)}件をキューから削除しました",
            )
        logger.info(ctx, "未読記事なし。通知スキップ")
        return

    logger.info(ctx, f"未読合計: {len(pending)}件 (うち持ち越し{len(queued_alive)}件)")

    # 4. Discord送信 (最大max_per_run件、このフィード専用のWebhookへ)
    #    send_articles は途中で失敗しても例外を投げず、送信できた分だけを返す
    #    (詳細は notifier.send_articles のdocstring参照)。
    #    ここでの try/except は、send_articles 呼び出し自体が想定外の形で
    #    失敗した場合の保険。
    try:
        sent = notifier.send_articles(webhook_env, feed_name, pending, max_per_run)
    except Exception as e:
        msg = logger.error(ctx, f"Discord送信処理そのものが異常終了しました: {e}", exc=e)
        notifier.send_error(ctx, msg)
        # 状態は変更せず次回に持ち越す(pendingをそのままqueueに保存)
        state_manager.save_queue(feed_id, pending)
        return

    if len(sent) < len(pending[:max_per_run]):
        # 送信予定件数に対して実際に送れた件数が少ない = 途中でレート制限等により打ち切られた
        logger.warn(
            ctx,
            f"送信予定{len(pending[:max_per_run])}件のうち{len(sent)}件しか送信できませんでした。"
            "残りは次回に持ち越します(重複通知は起きません)。",
        )

    # 5. 送信できた分だけ既読化し、残りはqueueに保存(破棄しない)
    state_manager.append_read_ids(feed_id, [a["id"] for a in sent])
    sent_ids = {a["id"] for a in sent}

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
        # 1フィードの想定外エラーで全体を落とさない。
        # ここで落ちるとスクリプトが異常終了し、GitHub Actions側の
        # 「state/*.json をコミット&プッシュ」ステップがスキップされてしまう。
        # その結果、処理済みの内容が記録されず、次回以降に未読が溜まり続けて
        # 一気に大量通知される事故につながるため、必ず握りつぶしてログ+Discord通知に留める。
        try:
            process_feed(feed)
        except Exception as e:
            msg = logger.error(
                _CONTEXT, f"フィード処理で想定外のエラー (id={feed.get('id')}): {e}", exc=e
            )
            notifier.send_error(f"feed:{feed.get('id')}", msg)

    logger.info(_CONTEXT, "===== 通常実行 終了 =====")


if __name__ == "__main__":
    main()
