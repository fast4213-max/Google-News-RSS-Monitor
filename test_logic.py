# -*- coding: utf-8 -*-
"""
ネットワークに依存しない部分のロジック検証用スクリプト（動作確認用、本番では使わない）。
- RSSパース (rss.parse_items)
- 既読差分抽出とキュー持ち越しのロジック (main.process_feed 相当を簡易再現)
"""
import os

import dedup
import rss

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Google ニュース</title>
<item>
<title>大東市でイベント開催 - テスト新聞A</title>
<link>https://example.com/article1</link>
<guid>https://example.com/article1</guid>
<pubDate>Wed, 17 Sep 2026 03:00:00 GMT</pubDate>
</item>
<item>
<title>大東市役所のお知らせ - テスト新聞B</title>
<link>https://example.com/article2</link>
<guid>https://example.com/article2</guid>
<pubDate>Wed, 17 Sep 2026 02:00:00 GMT</pubDate>
</item>
<item>
<title>大東市の天気 - テスト新聞C</title>
<link>https://example.com/article3</link>
<guid>https://example.com/article3</guid>
<pubDate>Wed, 17 Sep 2026 01:00:00 GMT</pubDate>
</item>
</channel>
</rss>"""

BROKEN_RSS_NO_CHANNEL = """<?xml version="1.0"?><rss version="2.0"></rss>"""
BROKEN_RSS_NO_LINK = """<?xml version="1.0"?>
<rss version="2.0"><channel><item><title>タイトルのみ</title></item></channel></rss>"""
NOT_XML = b"<html>not an rss feed</html>"


def test_parse_success():
    articles = rss.parse_items(SAMPLE_RSS.encode("utf-8"), "test")
    assert len(articles) == 3, f"期待値3件、実際{len(articles)}件"
    assert articles[0].title == "大東市でイベント開催 - テスト新聞A"
    assert articles[0].id == "https://example.com/article1"
    print("OK: test_parse_success")


def test_parse_no_channel_raises():
    try:
        rss.parse_items(BROKEN_RSS_NO_CHANNEL.encode("utf-8"), "test")
        raise AssertionError("RssParseErrorが発生すべきなのに発生しなかった")
    except rss.RssParseError:
        print("OK: test_parse_no_channel_raises")


def test_parse_no_link_raises():
    try:
        rss.parse_items(BROKEN_RSS_NO_LINK.encode("utf-8"), "test")
        raise AssertionError("RssParseErrorが発生すべきなのに発生しなかった")
    except rss.RssParseError:
        print("OK: test_parse_no_link_raises")


def test_parse_not_xml_raises():
    try:
        rss.parse_items(NOT_XML, "test")
        raise AssertionError("RssParseErrorが発生すべきなのに発生しなかった")
    except rss.RssParseError:
        print("OK: test_parse_not_xml_raises")


def test_diff_and_queue_logic():
    """main.process_feed の差分抽出ロジックを簡易再現して検証する。"""
    articles = rss.parse_items(SAMPLE_RSS.encode("utf-8"), "test")

    # ケース1: 何も既読でない場合、古い順(article3, article2, article1)で並ぶこと
    read_ids = set()
    unread_new = [a for a in reversed(articles) if a.id not in read_ids]
    assert [a.id for a in unread_new] == [
        "https://example.com/article3",
        "https://example.com/article2",
        "https://example.com/article1",
    ], "古い順ソートが期待通りでない"
    print("OK: test_diff_and_queue_logic (古い順ソート)")

    # ケース2: article3 だけ既読なら、残り2件が未読になること
    read_ids = {"https://example.com/article3"}
    unread_new = [a for a in reversed(articles) if a.id not in read_ids]
    assert len(unread_new) == 2
    print("OK: test_diff_and_queue_logic (差分抽出)")

    # ケース3: 10件超のとき、10件だけ送信し残りをqueueに回すロジック
    many = [{"id": f"id{i}", "title": f"t{i}", "link": f"l{i}"} for i in range(15)]
    MAX = 10
    to_send = many[:MAX]
    remaining = many[MAX:]
    assert len(to_send) == 10 and len(remaining) == 5
    print("OK: test_diff_and_queue_logic (10件制限)")


def test_dedup_clustering():
    """
    同じ出来事を複数社が別記事(別guid)で配信した場合に、
    最速2件だけ通知され、以降はこのプログラムの実行時刻(wall clock)基準で
    dedup_followup_minutes 経つまでは間引かれ、経てば「続報: 」付きで通知されることを確認する。
    """
    feed_id = "test_dedup_logic"
    path = dedup._clusters_path(feed_id)
    if os.path.exists(path):
        os.remove(path)

    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
    titles = [
        "スーパーで従業員刺される「叫びながら近づいて刺した」80歳元夫を現行犯逮捕 大阪府大東市 - MBSニュース",
        "【速報】女性従業員は搬送先の病院で死亡 スーパーで従業員が刃物で刺された事件"
        "「叫びながら近づいて刺した」80歳元夫を現行犯逮捕 大阪府大東市 - TBS NEWS DIG",
        "【速報】スーパーで「従業員刺された」と通報 女性けが 搬送時意識あり "
        "80歳男を殺人未遂容疑で逮捕 大阪・大東市 - Yahoo!ニュース",
        "大東市で桜まつり開催 来月10日から - 地元新聞",  # 無関係な別の話題
    ]
    # 4件とも同じ回のRSS取得で一度に届いた想定(pubDateはバラバラだが、
    # classify_articlesの呼び出しは1回=同じwall clock "now"で処理される)。
    articles = [
        {
            "id": f"id{i}",
            "title": t,
            "link": f"https://example.com/{i}",
            "pub_date": (base + timedelta(minutes=i * 3)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        }
        for i, t in enumerate(titles)
    ]

    try:
        to_notify, skip_ids = dedup.classify_articles(feed_id, articles, now=base)
        notified_ids = {a["id"] for a in to_notify}
        assert notified_ids == {"id0", "id1", "id3"}, f"通知される想定と違う: {notified_ids}"
        assert skip_ids == {"id2"}, f"間引かれる想定と違う: {skip_ids}"
        print("OK: test_dedup_clustering (同一話題の最速2件のみ通知、pubDateがバラバラでも同一実行内はまとめて間引かれる)")

        # 10分後(followup_minutes=30未満)は、実際の記事pubDateに関わらずまだ間引かれること
        too_soon = {
            "id": "id_too_soon",
            "title": titles[0],
            "link": "https://example.com/too_soon",
            "pub_date": (base + timedelta(minutes=10)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        }
        to_notify_soon, skip_ids_soon = dedup.classify_articles(
            feed_id, [too_soon], now=base + timedelta(minutes=10)
        )
        assert to_notify_soon == [] and skip_ids_soon == {"id_too_soon"}
        print("OK: test_dedup_clustering (30分未満はまだ間引かれる)")

        # 実行時刻(wall clock)で40分後(followup_minutes=30以上)なら「続報: 」付きで通知されること
        followup = {
            "id": "id_followup",
            "title": titles[0],
            "link": "https://example.com/followup",
            "pub_date": (base + timedelta(minutes=40)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        }
        to_notify_followup, skip_ids_followup = dedup.classify_articles(
            feed_id, [followup], now=base + timedelta(minutes=40)
        )
        assert len(to_notify_followup) == 1
        assert to_notify_followup[0]["title"].startswith(dedup.FOLLOWUP_LABEL)
        assert skip_ids_followup == set()
        print("OK: test_dedup_clustering (30分経過後は続報として通知される)")
    finally:
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    test_parse_success()
    test_parse_no_channel_raises()
    test_parse_no_link_raises()
    test_parse_not_xml_raises()
    test_diff_and_queue_logic()
    test_dedup_clustering()
    print("\n全テスト成功")
