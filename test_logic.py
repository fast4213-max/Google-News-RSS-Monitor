# -*- coding: utf-8 -*-
"""
ネットワークに依存しない部分のロジック検証用スクリプト（動作確認用、本番では使わない）。
- RSSパース (rss.parse_items)
- 既読差分抽出とキュー持ち越しのロジック (main.process_feed 相当を簡易再現)
"""
import os

import dedup
import main
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
    最初のウィンドウで最速2件だけ通知され、dedup_followup_minutes以内の追加分は
    間引かれ、経過後は新しいウィンドウとしてまた最大2件通知されることを確認する。
    タイトルへのラベル付与は行わない。
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

        # 実行時刻(wall clock)で40分後(followup_minutes=30以上)は新しいウィンドウが開き、
        # タイトルを書き換えずに再び最大2件まで通知されること
        followups = [
            {
                "id": "id_followup_a",
                "title": titles[0],
                "link": "https://example.com/followup_a",
                "pub_date": (base + timedelta(minutes=40)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
            },
            {
                "id": "id_followup_b",
                "title": titles[1],
                "link": "https://example.com/followup_b",
                "pub_date": (base + timedelta(minutes=41)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
            },
            {
                "id": "id_followup_c",
                "title": titles[2],
                "link": "https://example.com/followup_c",
                "pub_date": (base + timedelta(minutes=42)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
            },
        ]
        to_notify_followup, skip_ids_followup = dedup.classify_articles(
            feed_id, followups, now=base + timedelta(minutes=40)
        )
        notified_followup_ids = {a["id"] for a in to_notify_followup}
        assert notified_followup_ids == {"id_followup_a", "id_followup_b"}, notified_followup_ids
        assert skip_ids_followup == {"id_followup_c"}
        assert to_notify_followup[0]["title"] == titles[0]
        assert to_notify_followup[1]["title"] == titles[1]
        print("OK: test_dedup_clustering (30分経過後は新しいウィンドウでまた最大2件、ラベル無し)")
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_fresh_hours_filter():
    """
    fresh_hours(時間単位フィルタ)が、stale_days(日単位)より厳しく効くことを確認する。
    実際に「19:37配信の記事が翌日05:10に急に出現した」事例を再現する。
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)

    # 5時間前の記事: fresh_hours=6ならまだ新しい、fresh_hours=3なら古すぎる
    pub_5h_ago = (now - timedelta(hours=5)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert main.is_too_old_for_fresh_notify(pub_5h_ago, 6, "test") is False
    assert main.is_too_old_for_fresh_notify(pub_5h_ago, 3, "test") is True
    print("OK: test_fresh_hours_filter (5時間前の記事はfresh_hoursの値次第で判定が変わる)")

    # 9.5時間前の記事(実際にあった事例): デフォルトのfresh_hours=6では「古すぎる」判定になること
    pub_9_5h_ago = (now - timedelta(hours=9, minutes=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert main.is_too_old_for_fresh_notify(pub_9_5h_ago, main.DEFAULT_FRESH_HOURS, "test") is True
    print("OK: test_fresh_hours_filter (9.5時間前の記事はデフォルト設定で古すぎる判定になる)")

    # pubDateが空/パース不能な場合は安全側に倒して「古すぎる」と判定しないこと
    assert main.is_too_old_for_fresh_notify("", 6, "test") is False
    assert main.is_too_old_for_fresh_notify("不正な日付", 6, "test") is False
    print("OK: test_fresh_hours_filter (pubDateが無い/壊れている場合は通知する側に倒す)")


if __name__ == "__main__":
    test_parse_success()
    test_parse_no_channel_raises()
    test_parse_no_link_raises()
    test_parse_not_xml_raises()
    test_diff_and_queue_logic()
    test_dedup_clustering()
    test_fresh_hours_filter()
    print("\n全テスト成功")
