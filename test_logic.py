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
    同じ出来事を複数社が別記事(別guid)で配信した場合に、最速3件(1波目)は
    そのまま通知され、無関係な別の話題は別クラスタとして独立して通知されることを確認する。
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

        # 4件目: 1波目(3件)がまだ埋まった直後で、pubDate差・壁時計差ともに30分未満 → 通知しない
        too_soon = {
            "id": "id_too_soon",
            "title": "スーパーで女性刺され死亡 元夫を逮捕 大阪・大東市 - C新聞",
            "link": "https://example.com/too_soon",
            "pub_date": (base + timedelta(minutes=20)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        }
        to_notify2, skip_ids2 = dedup.classify_articles(
            feed_id, [too_soon], now=base + timedelta(minutes=20)
        )
        assert to_notify2 == [] and skip_ids2 == {"id_too_soon"}
        print("OK: test_dedup_clustering (クールダウン未達の続報は通知しない)")
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_batch_cooldown_opens_next_wave():
    """
    1波目(最速3件)が埋まった後、2波目は
      (a) pubDateが1波目の最新記事から30分以上後
      (b) 壁時計で前回通知から30分以上経過
    の両方を満たした記事だけを、再び最速3件まとめて通知することを確認する。
    """
    feed_id = "test_batch_cooldown"
    path = dedup._clusters_path(feed_id)
    if os.path.exists(path):
        os.remove(path)

    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)

    def article(i, title, minutes):
        return {
            "id": f"b{i}",
            "title": title,
            "link": f"https://example.com/b{i}",
            "pub_date": (base + timedelta(minutes=minutes)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        }

    try:
        titles = [
            "スーパーで従業員刺される「叫びながら近づいて刺した」80歳元夫を現行犯逮捕 大阪府大東市 - MBSニュース",
            "女性従業員は搬送先の病院で死亡 スーパーで従業員が刃物で刺された事件 80歳元夫を現行犯逮捕 大阪府大東市 - TBS NEWS DIG",
            "スーパーで「従業員刺された」と通報 女性けが 搬送時意識あり 80歳男を殺人未遂容疑で逮捕 大阪・大東市 - Yahoo!ニュース",
        ]
        batch1 = [article(i, t, i * 3) for i, t in enumerate(titles)]  # pubDateは0分,3分,6分
        to_notify, skip_ids = dedup.classify_articles(feed_id, batch1, now=base + timedelta(minutes=6))
        assert {a["id"] for a in to_notify} == {"b0", "b1", "b2"}
        assert skip_ids == set()
        print("OK: test_batch_cooldown (1波目=最速3件は即通知)")

        # 4件目: pubDateは1波目最新(6分)から20分後、壁時計も26分経過 → まだ30分ゲート未達
        too_soon = article(3, "スーパーで女性死亡 殺人容疑に切り替え 元夫を再逮捕 大阪・大東市 - C新聞", 26)
        to_notify2, skip_ids2 = dedup.classify_articles(
            feed_id, [too_soon], now=base + timedelta(minutes=26)
        )
        assert to_notify2 == [] and skip_ids2 == {"b3"}
        print("OK: test_batch_cooldown (30分ゲート未達の記事は通知しない)")

        # 5件目: pubDateが1波目最新から34分後、壁時計も34分経過 → 両ゲートを満たし2波目として通知
        ok_followup = article(4, "スーパーで女性死亡 殺人容疑に切り替え 元夫を再逮捕 大阪・大東市 - D新聞", 40)
        to_notify3, skip_ids3 = dedup.classify_articles(
            feed_id, [ok_followup], now=base + timedelta(minutes=40)
        )
        assert {a["id"] for a in to_notify3} == {"b4"}
        assert skip_ids3 == set()
        print("OK: test_batch_cooldown (両ゲートを満たすと2波目として通知される)")
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_similarity_threshold_reangled_followup_boundary():
    """
    デフォルト閾値の境界を、実データで固定しておくテスト。

    題材は実際にあった事例: 同じ事件について「メモ・ノートが見つかった」という
    切り口の違う続報。既存クラスタとの類似度は実測 **約0.19** しかない。

    そのため現在のデフォルト閾値(0.2)では「別の話題」と判定され、続報として
    通知される。これは意図した挙動。閾値を0.12まで下げていた頃はこれを同じ話題
    として間引けていたが、その水準だと「大東市で火災」と「大東市で交通事故」の
    ような**無関係な記事同士**まで0.15〜0.27で誤マッチし、別の出来事が黙って
    間引かれる副作用があったため、0.2に引き上げた。

    切り口の違う続報も間引きたい場合は、feeds.json で
    dedup_similarity_threshold を 0.17 程度まで下げる(誤マッチのリスクは上がる)。

    _similarity の実装を変えて類似度が大きくずれた場合に気づけるよう、
    実測値そのものをここで固定する。
    """
    feed_id = "test_similarity_reangled"
    path = dedup._clusters_path(feed_id)
    if os.path.exists(path):
        os.remove(path)

    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 18, 3, 0, tzinfo=timezone.utc)

    def article(i, title, minutes):
        return {
            "id": f"r{i}",
            "title": title,
            "link": f"https://example.com/r{i}",
            "pub_date": (base + timedelta(minutes=minutes)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        }

    try:
        seed = [
            article(0, "女性従業員は搬送先の病院で死亡 スパで従業員が刃物で刺された事件 - Infoseek", 0),
            article(1, "スパで元妻を包丁で刺したか 男(80)を現行犯逮捕 元妻はその後死亡 - A新聞", 1),
            article(2, "スパで刺された女性店員(66)死亡 80歳元夫を現行犯逮捕 - B新聞", 2),
        ]
        dedup.classify_articles(feed_id, seed, now=base, first_n=3)

        # 「メモ・ノートが見つかった」という新しい切り口の続報(実際にあった事例)。
        reangled = dedup.normalize_title(
            "事件前に「あいつを殺す」とのメモ見つかる スーパーで元妻を刺殺 逮捕の男の自宅で - C新聞"
        )
        clusters = dedup.load_clusters(feed_id)
        best_ratio = max(
            dedup._similarity(reangled, t) for c in clusters for t in c["titles"]
        )
        # 実測値の固定 (_similarity の挙動が変わったら気づけるように)
        assert 0.18 <= best_ratio <= 0.20, f"類似度の実測値が想定から外れた: {best_ratio}"

        # デフォルト閾値(0.2)では別話題 = 続報として通知される
        assert best_ratio < dedup.DEFAULT_SIMILARITY_THRESHOLD, (
            f"デフォルト閾値の想定が変わっている: best_ratio={best_ratio} / "
            f"threshold={dedup.DEFAULT_SIMILARITY_THRESHOLD}"
        )
        # 閾値を下げれば同じ話題として間引ける、という逃げ道が残っていること
        assert best_ratio >= 0.17
        print(
            f"OK: test_similarity_threshold_boundary "
            f"(切り口の違う続報は類似度{best_ratio:.2f}=閾値0.2未満のため別話題として通知される)"
        )
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_ignore_words_removes_feed_keyword():
    """
    フィード名(=検索キーワード)を類似度計算から除外することで、
    「地名が共通なだけの無関係な記事」が同じ話題と誤判定されなくなることを確認する。

    このシステムは「大東市」で検索したRSSを読むため、そのフィードの記事は全件が
    「大東市」を含む。この語は話題を区別する情報を持たないのに類似度だけ押し上げる。
    """
    ignore = ["大東市"]

    # (1) 共通点が地名だけの無関係な記事 → 除外後は類似度0になる
    unrelated = [
        ("大東市で火災 住宅1棟全焼 - A新聞", "大東市で交通事故 2人けが - B新聞"),
        ("大東市で秋祭りが開催されました - A新聞", "大東市の市議会で予算案が可決 - B新聞"),
    ]
    for a, b in unrelated:
        before = dedup._similarity(dedup.normalize_title(a), dedup.normalize_title(b))
        after = dedup._similarity(
            dedup.normalize_title(a, ignore), dedup.normalize_title(b, ignore)
        )
        assert before >= 0.12, f"除外前の類似度が想定より低い: {before}"
        assert after == 0.0, f"地名除外後も類似度が残っている: {after} ({a} / {b})"
    print("OK: test_ignore_words (地名だけが共通の無関係な記事は類似度0になる)")

    # (2) 本当に同じ話題の記事は、除外後も閾値を十分上回ったままであること
    same_topic = [
        (
            "スーパーで従業員刺される 80歳元夫を現行犯逮捕 大阪府大東市 - MBS",
            "スーパーで「従業員刺された」と通報 女性けが 80歳男を殺人未遂容疑で逮捕 大阪・大東市 - Yahoo",
        ),
        (
            "大東市長が新体育館の建設方針を表明 - A新聞",
            "大東市の新体育館、建設方針を市長が表明 - B新聞",
        ),
    ]
    for a, b in same_topic:
        after = dedup._similarity(
            dedup.normalize_title(a, ignore), dedup.normalize_title(b, ignore)
        )
        assert after >= dedup.DEFAULT_SIMILARITY_THRESHOLD, (
            f"同じ話題なのに閾値を下回った: {after} ({a} / {b})"
        )
    print("OK: test_ignore_words (同じ話題の記事は除外後も閾値を上回る)")

    # (3) 「大阪府大東市」「大阪・大東市」のような表記ゆれでも除去されること
    #     (記号を落としてから除去しているため)
    assert "大東市" not in dedup.normalize_title("大阪府大東市で火災 - A新聞", ignore)
    assert "大東市" not in dedup.normalize_title("大阪・大東市で火災 - A新聞", ignore)
    print("OK: test_ignore_words (表記ゆれがあっても除去される)")

    # (4) 除去するとタイトルが空になる場合は、除去前のものを使って比較材料を残すこと
    assert dedup.normalize_title("大東市 - A新聞", ignore) == "大東市"
    print("OK: test_ignore_words (タイトルが空になる場合は除去前に戻す)")


def test_ignore_words_end_to_end_keeps_topics_separate():
    """
    classify_articles 経由で、地名だけが共通の無関係な2記事が
    別々の話題として扱われ、どちらも通知されることを確認する
    (以前は片方が「同じ話題の重複」とみなされて通知されなかった)。
    """
    feed_id = "test_ignore_words_e2e"
    path = dedup._clusters_path(feed_id)
    if os.path.exists(path):
        os.remove(path)

    from datetime import datetime, timezone

    now = datetime(2026, 9, 22, 3, 0, tzinfo=timezone.utc)
    articles = [
        {"id": "w1", "title": "大東市で火災 住宅1棟全焼 - A新聞", "link": "https://example.com/w1", "pub_date": ""},
        {"id": "w2", "title": "大東市で交通事故 2人けが - B新聞", "link": "https://example.com/w2", "pub_date": ""},
    ]
    try:
        # 地名を除外しない場合: 類似度0.273で同じ話題と誤判定される
        to_notify, skip_ids = dedup.classify_articles(feed_id, articles, now=now)
        assert skip_ids == set() or len(to_notify) == 2, (to_notify, skip_ids)
        # first_n=3 の範囲内なので通知自体はされるが、同じクラスタに入ってしまう
        assert len(dedup.load_clusters(feed_id)) == 1, "地名除外なしでは1クラスタに誤統合される"

        os.remove(path)

        # 地名を除外した場合: 別々のクラスタになる
        to_notify, skip_ids = dedup.classify_articles(
            feed_id, articles, now=now, ignore_words=["大東市"]
        )
        assert {a["id"] for a in to_notify} == {"w1", "w2"}, to_notify
        assert skip_ids == set(), skip_ids
        assert len(dedup.load_clusters(feed_id)) == 2, "地名除外後は別クラスタになるべき"
        print("OK: test_ignore_words_end_to_end (無関係な2記事が別の話題として扱われる)")
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_ignore_words_known_limitation():
    """
    既知の限界を記録しておくテスト(挙動が変わったら気づけるように)。

    地名の除外で解決するのは「共通点が検索キーワードだけ」のケース。
    文の言い回しそのものが似ている無関係な記事は、地名を除いても
    類似度が残り、依然として同じ話題と誤判定される。
    bigram類似度だけで区別できる限界であり、気になる場合は feeds.json の
    dedup_ignore_words に共通語を足すか、閾値を上げて対処する。
    """
    ignore = ["大東市"]
    a = "大東市長が記者会見で表明 新体育館の建設へ - A新聞"
    b = "大東市長が記者会見で陳謝 職員の不祥事を受けて - B新聞"
    after = dedup._similarity(dedup.normalize_title(a, ignore), dedup.normalize_title(b, ignore))
    assert after >= dedup.DEFAULT_SIMILARITY_THRESHOLD, (
        f"限界ケースの挙動が変わった(改善した?): {after}"
    )

    # dedup_ignore_words に共通語を足せば区別できるようになること
    ignore2 = ["大東市", "市長", "記者会見"]
    after2 = dedup._similarity(dedup.normalize_title(a, ignore2), dedup.normalize_title(b, ignore2))
    assert after2 < dedup.DEFAULT_SIMILARITY_THRESHOLD, (
        f"共通語を足しても区別できない: {after2}"
    )
    print(
        f"OK: test_ignore_words_known_limitation "
        f"(言い回しが似た無関係記事は地名除外だけでは残る={after:.2f} / 共通語追加で解消={after2:.2f})"
    )


def test_extract_source():
    """タイトル末尾の " - 媒体名" から配信元を取り出せること。"""
    assert dedup.extract_source("大東市で火災 - A新聞") == "A新聞"
    # 見出し中に " - " が複数あっても、最後のものを配信元とみなす
    assert dedup.extract_source("速報 - 大東市で火災 - 地域ニュースサイト号外NET") == "地域ニュースサイト号外NET"
    # 形式に合わない場合は空文字列(=配信元不明)
    assert dedup.extract_source("大東市で火災") == ""
    print("OK: test_extract_source (配信元を抽出できる)")


def test_same_source_threshold_splits_templated_headlines():
    """
    同じ配信元が定型テンプレートで量産する見出しが、別々の話題として
    扱われることを確認する(実データで見つかった誤判定の再現)。

    号外NET / 選挙ドットコム / ｄメニューニュース の防犯情報などは、
    毎回ほぼ同じ書式で別の出来事を配信するため、地名を除外してもなお
    類似度0.31〜0.44に達し、同じ話題と誤判定されていた。
    """
    feed_id = "test_same_source_split"
    path = dedup._clusters_path(feed_id)
    from datetime import datetime, timezone

    now = datetime(2026, 9, 22, 3, 0, tzinfo=timezone.utc)
    ig = ["大東市"]

    cases = [
        (
            "号外NETの別イベント告知",
            "【大東市】9月21日は何の日？ポップタウン住道オペラパークで9月23日には「地域包括フェスティバル」を開催！ - 地域ニュースサイト号外NET",
            "【大東市】ポップタウン住道オペラパークでオリジナル防災グッズを作って遊んでみませんか？ - 地域ニュースサイト号外NET",
        ),
        (
            "選挙ドットコムの別議員の一般質問",
            "大東市9月議会 9/24(木)10：00から一般質問 シニアディスコ - 選挙ドットコム",
            "大東市令和8年9月議会 あずま健太郎一般質問 ９月２４日10:00から - 選挙ドットコム",
        ),
        (
            "ｄメニューニュースの別の防犯情報",
            "（大阪）大東市寺川５丁目付近で声かけ　９月１８日 - ｄメニューニュース",
            "（大阪）大東市北条４丁目付近で盗撮の疑い　９月９日 - ｄメニューニュース",
        ),
    ]
    for label, t1, t2 in cases:
        if os.path.exists(path):
            os.remove(path)
        try:
            ratio = dedup._similarity(
                dedup.normalize_title(t1, ig), dedup.normalize_title(t2, ig)
            )
            # 通常の閾値は超えてしまう(=同一配信元ルールが無いと誤判定される)ことを確認
            assert ratio >= dedup.DEFAULT_SIMILARITY_THRESHOLD, f"{label}: 前提が崩れた {ratio}"
            assert ratio < dedup.DEFAULT_SAME_SOURCE_THRESHOLD, f"{label}: 前提が崩れた {ratio}"

            arts = [
                {"id": "t1", "title": t1, "link": "https://example.com/1", "pub_date": ""},
                {"id": "t2", "title": t2, "link": "https://example.com/2", "pub_date": ""},
            ]
            to_notify, skip_ids = dedup.classify_articles(
                feed_id, arts, now=now, ignore_words=ig
            )
            assert {a["id"] for a in to_notify} == {"t1", "t2"}, (label, to_notify)
            assert skip_ids == set(), (label, skip_ids)
            assert len(dedup.load_clusters(feed_id)) == 2, f"{label}: 別クラスタにならなかった"
            print(f"OK: test_same_source_threshold ({label} 類似度{ratio:.2f} → 別の話題として通知)")
        finally:
            if os.path.exists(path):
                os.remove(path)


def test_same_source_near_duplicate_still_clusters():
    """
    回帰テスト: 同一配信元ルールを入れても、
    「既存記事のpubDateだけ更新されて再配信された」ケース(同じ配信元・ほぼ同一タイトル)は
    従来どおり同じクラスタにまとまり、古い後追いとして間引かれること。

    このケースは過去に「翌日になって古い内容が通知される」不具合として修正済みで、
    同一配信元ルールで壊してはいけない。類似度が0.9以上あるため、
    厳しい閾値(DEFAULT_SAME_SOURCE_THRESHOLD)でも拾える。
    """
    feed_id = "test_same_source_dup"
    path = dedup._clusters_path(feed_id)
    if os.path.exists(path):
        os.remove(path)

    from datetime import datetime, timezone

    now = datetime(2026, 9, 22, 8, 10, tzinfo=timezone.utc)
    ig = ["大東市"]
    base_title = (
        "【大東市】9月21日は何の日？ポップタウン住道オペラパークで9月23日には"
        "「地域包括フェスティバル」を開催！ - 地域ニュースサイト号外NET"
    )
    edited_title = base_title.replace("開催！ -", "開催！(更新) -")
    try:
        ratio = dedup._similarity(
            dedup.normalize_title(base_title, ig), dedup.normalize_title(edited_title, ig)
        )
        assert ratio >= dedup.DEFAULT_SAME_SOURCE_THRESHOLD, (
            f"再配信記事が厳しい閾値を下回った: {ratio}"
        )
        dedup.classify_articles(
            feed_id,
            [{"id": "seed", "title": base_title, "link": "https://example.com/s", "pub_date": ""}],
            now=now,
            ignore_words=ig,
        )
        to_notify, skip_ids = dedup.classify_articles(
            feed_id,
            [{"id": "again", "title": edited_title, "link": "https://example.com/a", "pub_date": ""}],
            now=now,
            old_ids={"again"},
            ignore_words=ig,
        )
        assert to_notify == [] and skip_ids == {"again"}, (to_notify, skip_ids)
        assert len(dedup.load_clusters(feed_id)) == 1, "同一配信元の再配信が別クラスタになった"
        print(f"OK: test_same_source_near_duplicate (再配信記事は類似度{ratio:.2f}で従来どおり間引かれる)")
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_same_source_rule_only_applies_to_single_source_cluster():
    """
    複数の配信元が既に入っているクラスタ(=各社が報じている本物の話題)には、
    同じ配信元の続報でも通常の閾値で合流すること。

    ここで厳しい閾値を適用してしまうと、大きな事件で同じ社の続報が
    毎回「新しい話題」として通知されてしまう。
    """
    feed_id = "test_same_source_multi"
    path = dedup._clusters_path(feed_id)
    if os.path.exists(path):
        os.remove(path)

    from datetime import datetime, timezone

    now = datetime(2026, 9, 22, 3, 0, tzinfo=timezone.utc)
    ig = ["大東市"]
    try:
        # 2社が報じた話題でクラスタを作る
        seed = [
            {"id": "m1", "title": "大東市のスーパーで従業員刺される 80歳男を現行犯逮捕 - TBS NEWS DIG",
             "link": "https://example.com/1", "pub_date": ""},
            {"id": "m2", "title": "スーパーで女性従業員が刺される 大阪府大東市 男を逮捕 - 日テレNEWS NNN",
             "link": "https://example.com/2", "pub_date": ""},
        ]
        dedup.classify_articles(feed_id, seed, now=now, first_n=999, ignore_words=ig)
        clusters = dedup.load_clusters(feed_id)
        assert len(clusters) == 1, f"前提: 2社の記事が1クラスタになるべき {len(clusters)}"

        # そこへ TBS(既にクラスタ内にいる配信元)の続報が来る。
        # 通常の閾値(0.2)は超えるが、厳しい閾値(0.6)は下回る類似度でも合流すること。
        followup = {"id": "m3",
                    "title": "スーパーで従業員刺され死亡 80歳の男を殺人容疑で送検 大阪・大東市 - TBS NEWS DIG",
                    "link": "https://example.com/3", "pub_date": ""}
        ratio = max(
            dedup._similarity(dedup.normalize_title(followup["title"], ig), t)
            for t in clusters[0]["titles"]
        )
        assert dedup.DEFAULT_SIMILARITY_THRESHOLD <= ratio < dedup.DEFAULT_SAME_SOURCE_THRESHOLD, (
            f"前提: 続報の類似度は通常閾値と厳しい閾値の間にあるべき {ratio}"
        )
        dedup.classify_articles(feed_id, [followup], now=now, first_n=999, ignore_words=ig)
        assert len(dedup.load_clusters(feed_id)) == 1, (
            "複数配信元のクラスタに同じ配信元の続報が合流しなかった"
        )
        print("OK: test_same_source_rule (複数配信元のクラスタには通常の閾値で合流する)")
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_old_cluster_state_without_sources_is_loadable():
    """
    sources を持たない旧形式のクラスタstateを読んでもクラッシュせず、
    「配信元不明」として通常の閾値で動作すること(過去にKeyError事故があったため)。
    """
    import json

    feed_id = "test_old_cluster_schema"
    path = dedup._clusters_path(feed_id)
    try:
        os.makedirs(dedup.STATE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"clusters": [{"titles": ["大東市で火災住宅1棟全焼"], "batch_open_count": 1,
                               "batch_ref_pub_date": "", "last_notified_at": ""}]},
                f, ensure_ascii=False,
            )
        clusters = dedup.load_clusters(feed_id)
        assert len(clusters) == 1 and clusters[0]["sources"] == [], clusters
        # 配信元不明のクラスタには通常の閾値が使われること
        assert dedup._required_threshold(clusters[0], "A新聞", 0.2, 0.6) == 0.2
        print("OK: test_old_cluster_state (sources が無い旧stateでも壊れない)")
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


def test_edited_article_resurfacing_as_old_is_skipped():
    """
    実際にあった事例の再現テスト: Googleニュース側で既存記事のpubDateだけ更新されて
    再配信され、タイトルの言い回しがわずかに変わった結果、旧クラスタとマッチせず
    「初めての話題」として誤判定され、翌日になって古い内容が通知されてしまっていた。
    閾値(0.2)でもこうした再配信記事はほぼ同一タイトルのため既存クラスタに正しくマッチし、
    old_ids(fresh_hours超過)の判定で通知されずに間引かれることを確認する。
    """
    feed_id = "test_edited_resurface"
    path = dedup._clusters_path(feed_id)
    if os.path.exists(path):
        os.remove(path)

    from datetime import datetime, timezone

    now = datetime(2026, 9, 22, 8, 10, tzinfo=timezone.utc)

    def article(article_id, title, pub_date=""):
        return {"id": article_id, "title": title, "link": f"https://example.com/{article_id}", "pub_date": pub_date}

    try:
        dedup.classify_articles(
            feed_id,
            [article(
                "seed",
                "【大東市】9月21日は何の日？ポップタウン住道オペラパークで9月23日には"
                "「地域包括フェスティバル」を開催！ - 地域ニュースサイト号外NET",
                "Mon, 21 Sep 2026 22:21:46 GMT",
            )],
            now=now,
        )

        # 編集によりpubDateだけ更新されたほぼ同一記事(=old_ids判定される)
        resurfaced = article(
            "resurfaced",
            "【大東市】9月21日は何の日？ポップタウン住道オペラパークで9月23日には"
            "「地域包括フェスティバル」を開催！(更新) - 地域ニュースサイト号外NET",
            "Tue, 22 Sep 2026 08:10:00 GMT",
        )
        to_notify, skip_ids = dedup.classify_articles(
            feed_id, [resurfaced], now=now, old_ids={"resurfaced"}
        )
        assert to_notify == [] and skip_ids == {"resurfaced"}, (to_notify, skip_ids)
        print("OK: test_edited_article_resurfacing (編集で再配信された古い記事は通知されない)")
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


def test_queued_article_is_not_dropped_on_next_run():
    """
    回帰テスト: キューに持ち越した記事が、次回実行で捨てられないこと。

    かつては process_feed が「未読抽出 → stale/dedup判定 → キュー読み込み」の順に
    処理していたため、以下の事故が起きていた:
      1回目: max_per_run を超えた分がキューに積まれる
      2回目: Googleニュースは同じ記事を何時間も載せ続けるので、キューの記事が
             RSSにもまだ載っている。それが再度 stale/dedup 判定にかけられ、
             「既に通知済みの話題の、公開から時間が経った後追い記事」と判定されて
             既読化される → 直後のキュー整合性チェックで弾かれ、
             一度も通知されないまま消える(しかもキューファイルにゴミが残る)
    現在はキューを未読抽出より先に読み、キュー内のIDを再判定対象から外している。
    """
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    import notifier
    import state_manager

    feed_id = "test_queue_carryover"
    feed = {
        "id": feed_id,
        "name": "テスト",
        "url": "https://example.invalid/rss",
        "webhook_env": "DUMMY_WEBHOOK_ENV",
        "max_per_run": 1,  # 1件しか送れないので必ずキューに持ち越しが発生する
    }
    paths = [
        state_manager._read_path(feed_id),
        state_manager._queue_path(feed_id),
        dedup._clusters_path(feed_id),
    ]

    now = datetime.now(timezone.utc)

    def build_feed(hours_ago):
        """同じ話題の2記事を、指定時間前の配信として返す(RSSは新しい順)。"""
        pub = format_datetime(now - timedelta(hours=hours_ago))
        return [
            rss.Article(id="q2", title="大東市で記者会見 新たな方針を説明 - B新聞", link="https://example.com/q2", pub_date=pub),
            rss.Article(id="q1", title="大東市で記者会見 新たな方針を説明 - A新聞", link="https://example.com/q1", pub_date=pub),
        ]

    sent_ids = []
    original_fetch = rss.fetch_articles
    original_send = notifier.send_articles
    original_error = notifier.send_error

    def fake_send_articles(webhook_env, feed_name, articles, max_count):
        delivered = articles[:max_count]
        sent_ids.extend(a["id"] for a in delivered)
        return delivered

    try:
        for p in paths:
            if os.path.exists(p):
                os.remove(p)
        notifier.send_articles = fake_send_articles
        notifier.send_error = lambda *a, **k: None

        # 1回目: 2件とも未読。max_per_run=1 なので q1 だけ送信し、q2 はキューへ
        rss.fetch_articles = lambda url: build_feed(0.1)
        main.process_feed(feed)
        assert sent_ids == ["q1"], sent_ids
        assert [q["id"] for q in state_manager.load_queue(feed_id)] == ["q2"]

        # 2回目: 同じ記事がRSSに残ったまま、かつ fresh_hours を超えて古くなっている。
        # 持ち越した q2 は「後追い記事」と誤判定されずに通知されること。
        sent_ids.clear()
        rss.fetch_articles = lambda url: build_feed(main.DEFAULT_FRESH_HOURS + 2)
        main.process_feed(feed)
        assert sent_ids == ["q2"], f"持ち越し記事が通知されなかった: {sent_ids}"
        assert state_manager.load_queue(feed_id) == []
        assert set(state_manager.load_read_ids(feed_id)) == {"q1", "q2"}
        print("OK: test_queued_article_is_not_dropped (持ち越し記事は次回に必ず通知される)")

        # 3回目: すべて既読。キューにゴミが残っていないこと
        sent_ids.clear()
        rss.fetch_articles = lambda url: build_feed(main.DEFAULT_FRESH_HOURS + 3)
        main.process_feed(feed)
        assert sent_ids == [], sent_ids
        assert state_manager.load_queue(feed_id) == []
        print("OK: test_queued_article_is_not_dropped (通知済みの記事が再通知されない)")
    finally:
        rss.fetch_articles = original_fetch
        notifier.send_articles = original_send
        notifier.send_error = original_error
        for p in paths:
            if os.path.exists(p):
                os.remove(p)


def test_stale_queue_entries_are_cleaned_up():
    """
    既読化済みの記事がキューに残っていた場合(過去バージョンが残した不整合データ)、
    新規未読が無い実行でもキューから掃除されること。
    掃除しないと state/queue_<id>.json にゴミが永久に残り続ける。
    """
    import notifier
    import state_manager

    feed_id = "test_queue_cleanup"
    feed = {
        "id": feed_id,
        "name": "テスト",
        "url": "https://example.invalid/rss",
        "webhook_env": "DUMMY_WEBHOOK_ENV",
    }
    paths = [
        state_manager._read_path(feed_id),
        state_manager._queue_path(feed_id),
        dedup._clusters_path(feed_id),
    ]
    original_fetch = rss.fetch_articles
    original_error = notifier.send_error
    try:
        for p in paths:
            if os.path.exists(p):
                os.remove(p)
        # 既読なのにキューにも残っている、という不整合状態を作る
        state_manager.append_read_ids(feed_id, ["ghost"])
        state_manager.save_queue(
            feed_id, [{"id": "ghost", "title": "既読なのに残っている記事", "link": "https://example.com/g", "pub_date": ""}]
        )

        rss.fetch_articles = lambda url: []
        notifier.send_error = lambda *a, **k: None
        main.process_feed(feed)

        assert state_manager.load_queue(feed_id) == [], state_manager.load_queue(feed_id)
        print("OK: test_stale_queue_entries_are_cleaned_up (既読化済みのゴミがキューから消える)")
    finally:
        rss.fetch_articles = original_fetch
        notifier.send_error = original_error
        for p in paths:
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    test_parse_success()
    test_parse_no_channel_raises()
    test_parse_no_link_raises()
    test_parse_not_xml_raises()
    test_diff_and_queue_logic()
    test_dedup_clustering()
    test_batch_cooldown_opens_next_wave()
    test_similarity_threshold_reangled_followup_boundary()
    test_ignore_words_removes_feed_keyword()
    test_ignore_words_end_to_end_keeps_topics_separate()
    test_ignore_words_known_limitation()
    test_extract_source()
    test_same_source_threshold_splits_templated_headlines()
    test_same_source_near_duplicate_still_clusters()
    test_same_source_rule_only_applies_to_single_source_cluster()
    test_old_cluster_state_without_sources_is_loadable()
    test_edited_article_resurfacing_as_old_is_skipped()
    test_old_article_only_skipped_when_topic_already_notified()
    test_read_ids_order_is_preserved()
    test_load_queue_skips_malformed_items()
    test_rss_skips_broken_item_but_keeps_rest()
    test_fresh_hours_filter()
    test_queued_article_is_not_dropped_on_next_run()
    test_stale_queue_entries_are_cleaned_up()
    print("\n全テスト成功")
