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
    同じ出来事を複数社が別記事(別guid)で配信した場合に、最速3件はそのまま通知され、
    4件目以降は「同じ配信元が前回より新しいpubDateで改めて報じた場合」だけ
    続報として通知されることを確認する。初見の配信元は4件目以降だと通知されない。
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
        assert notified_ids == {"id0", "id1", "id2", "id3"}, f"通知される想定と違う: {notified_ids}"
        assert skip_ids == set(), f"間引かれる想定と違う: {skip_ids}"
        print("OK: test_dedup_clustering (最速3件[id0,1,2]はそのまま通知、別話題[id3]も独立して通知)")

        # 4件目: 最速3件に含まれない初見の配信元(C新聞) → 通知しない
        unknown_source = {
            "id": "id_unknown_source",
            "title": "スーパーで女性刺され死亡 元夫を逮捕 大阪・大東市 - C新聞",
            "link": "https://example.com/unknown_source",
            "pub_date": (base + timedelta(minutes=30)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        }
        to_notify2, skip_ids2 = dedup.classify_articles(
            feed_id, [unknown_source], now=base + timedelta(minutes=30)
        )
        assert to_notify2 == [] and skip_ids2 == {"id_unknown_source"}
        print("OK: test_dedup_clustering (最速3件に無い初見の配信元は4件目以降だと通知しない)")

        # 5件目: 最速3件に含まれるMBSニュースが、前回(id0)より新しいpubDateで改めて報道
        # → 続報として通知される(壁時計の待機時間は不要、pubDateが新しければ即通知)
        mbs_followup = {
            "id": "id_mbs_followup",
            "title": "スーパーで女性死亡 殺人容疑に切り替え 元夫を再逮捕 大阪・大東市 - MBSニュース",
            "link": "https://example.com/mbs_followup",
            "pub_date": (base + timedelta(minutes=5)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        }
        to_notify3, skip_ids3 = dedup.classify_articles(
            feed_id, [mbs_followup], now=base + timedelta(minutes=5)
        )
        assert {a["id"] for a in to_notify3} == {"id_mbs_followup"}
        assert skip_ids3 == set()
        print("OK: test_dedup_clustering (最速3件に含まれる配信元の新しいpubDateでの続報は即通知)")

        # 6件目: MBSニュースが同じか前回より古い/同時刻のpubDateで再度出現 → 続報とはみなさない
        mbs_duplicate = {
            "id": "id_mbs_duplicate",
            "title": "スーパーで女性死亡 殺人容疑に切り替え 元夫を再逮捕 大阪・大東市 - MBSニュース",
            "link": "https://example.com/mbs_duplicate",
            "pub_date": (base + timedelta(minutes=5)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        }
        to_notify4, skip_ids4 = dedup.classify_articles(
            feed_id, [mbs_duplicate], now=base + timedelta(minutes=5)
        )
        assert to_notify4 == [] and skip_ids4 == {"id_mbs_duplicate"}
        print("OK: test_dedup_clustering (同じ配信元でもpubDateが前回以下なら続報とみなさない)")
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_old_article_only_skipped_when_topic_already_notified():
    """
    公開から時間が経った記事(old_ids)の扱い:
      - すでに通知済みの話題の後追い記事 → 通知しない
      - まだ一度も通知していない話題 → 古くても通知する(見逃し防止)
    """
    feed_id = "test_old_article_logic"
    path = dedup._clusters_path(feed_id)
    if os.path.exists(path):
        os.remove(path)

    from datetime import datetime, timezone

    now = datetime(2026, 9, 18, 5, 10, tzinfo=timezone.utc)

    def article(article_id, title):
        return {"id": article_id, "title": title, "link": f"https://example.com/{article_id}", "pub_date": ""}

    try:
        # 最初に「刺傷事件」の話題を通知させてクラスタを作る
        dedup.classify_articles(
            feed_id,
            [article("seed", "スーパーで女性従業員刺される 80代男を現行犯逮捕 大阪・大東市 - A新聞")],
            now=now,
        )

        # 同じ話題の「古い」後追い記事と、まったく別の話題の「古い」記事を同時に渡す
        followup_old = article(
            "old_followup", "スーパーで女性刺され死亡 殺人容疑で元夫を逮捕 大阪・大東市 - B新聞"
        )
        new_topic_old = article("old_new_topic", "大東市で新図書館がオープン 蔵書10万冊 - C新聞")
        to_notify, skip_ids = dedup.classify_articles(
            feed_id,
            [followup_old, new_topic_old],
            now=now,
            old_ids={"old_followup", "old_new_topic"},
        )

        notified_ids = {a["id"] for a in to_notify}
        assert notified_ids == {"old_new_topic"}, f"通知される想定と違う: {notified_ids}"
        assert skip_ids == {"old_followup"}, f"間引かれる想定と違う: {skip_ids}"
        print("OK: test_old_article (古い後追いは間引き、古くても初めての話題は通知)")
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_read_ids_order_is_preserved():
    """
    既読IDが「古い順」で保存され、上限を超えたら本当に古い方から消えることを確認する。
    (setのまま保存すると順序が毎回変わり、上限超過時にランダムなIDが消えて
    その記事が未読に戻り再通知されるバグがあった)
    """
    import json

    import state_manager

    feed_id = "test_read_order"
    path = state_manager._read_path(feed_id)
    if os.path.exists(path):
        os.remove(path)

    original_max = state_manager.MAX_READ_IDS_PER_FEED
    try:
        state_manager.append_read_ids(feed_id, ["a", "b", "c"])
        state_manager.append_read_ids(feed_id, ["c", "d"])  # 既存の"c"は重複させない
        assert state_manager.load_read_id_list(feed_id) == ["a", "b", "c", "d"]
        print("OK: test_read_ids_order (追記順=古い順が保たれる)")

        # 上限を超えたら、古い方(先頭)から捨てられること
        state_manager.MAX_READ_IDS_PER_FEED = 3
        state_manager.append_read_ids(feed_id, ["e"])
        assert state_manager.load_read_id_list(feed_id) == ["c", "d", "e"]
        print("OK: test_read_ids_order (上限超過時は古い方から捨てられる)")

        # ファイル上も順序が保たれていること(差分が毎回全行にならない)
        with open(path, "r", encoding="utf-8") as f:
            assert json.load(f)["ids"] == ["c", "d", "e"]
    finally:
        state_manager.MAX_READ_IDS_PER_FEED = original_max
        if os.path.exists(path):
            os.remove(path)


def test_load_queue_skips_malformed_items():
    """
    queue内に id/title/link を欠いた壊れた要素があっても、
    main.py 側で KeyError にならず、正常な要素だけ読み込まれることを確認する。
    (クラスタstateで実際に起きたKeyError事故と同種の防御)
    """
    import json

    import state_manager

    feed_id = "test_queue_malformed"
    path = state_manager._queue_path(feed_id)
    try:
        os.makedirs(state_manager.STATE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "items": [
                        {"id": "ok1", "title": "正常な記事", "link": "https://example.com/1", "pub_date": ""},
                        {"title": "idが無い記事", "link": "https://example.com/2"},
                        {"id": "ok2", "title": "pub_dateが無い記事", "link": "https://example.com/3"},
                        "壊れた文字列要素",
                    ]
                },
                f,
                ensure_ascii=False,
            )
        items = state_manager.load_queue(feed_id)
        assert {i["id"] for i in items} == {"ok1", "ok2"}, items
        assert all("pub_date" in i for i in items)
        print("OK: test_load_queue_skips_malformed_items (壊れた要素は読み飛ばしクラッシュしない)")
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_rss_skips_broken_item_but_keeps_rest():
    """title/linkが欠けたitemが1件あっても、残りの記事は処理されること。"""
    rss_with_one_broken = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>正常な記事 - テスト新聞</title><link>https://example.com/ok</link></item>
<item><title>リンクが無い記事</title></item>
</channel></rss>"""
    articles = rss.parse_items(rss_with_one_broken.encode("utf-8"), "test")
    assert len(articles) == 1 and articles[0].title == "正常な記事 - テスト新聞"
    print("OK: test_rss_skips_broken_item (1件壊れていても残りは処理される)")


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
    test_old_article_only_skipped_when_topic_already_notified()
    test_read_ids_order_is_preserved()
    test_load_queue_skips_malformed_items()
    test_rss_skips_broken_item_but_keeps_rest()
    test_fresh_hours_filter()
    print("\n全テスト成功")
