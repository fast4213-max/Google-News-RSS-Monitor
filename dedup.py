# -*- coding: utf-8 -*-
"""
同一ニュース（複数社が同じ出来事を別記事として配信したもの）をまとめて、
Discordに同じ話題の記事が延々と流れ続けるのを防ぐための「話題クラスタリング」処理。

方針(バッチ+クールダウン方式):
  - Googleニュースの検索RSSは、同じ出来事について各社(産経/毎日/Yahoo!ニュース等)が
    それぞれ別記事(別guid)を出すため、そのまま通知すると同じ話題が何件も並んでしまう。
  - タイトルを正規化して類似度を比較し、「同じ話題」とみなせる記事をクラスタにまとめる。
  - 1つの話題は「波(バッチ)」単位で通知する。
      - 1波目: 話題が初めて検知された時点で、最速 first_n 件（デフォルト3件）を
        そのまま通知する。配信元は問わない(タイトル末尾に配信元表記が無くてもよい)。
      - 2波目以降: 直前の波が first_n 件で埋まったあと、次の記事は
        以下の**両方**を満たした場合にだけ「次の波」として通知する。
          (a) その記事のpubDateが、直前の波の記事群のうち最も新しいpubDateから
              batch_cooldown_minutes 分(デフォルト30分)以上後である
          (b) 壁時計で、直前の波を通知した時刻から batch_cooldown_minutes 分
              (デフォルト30分)以上経過している
        満たさない記事は通知せず既読化だけする(=同じクラスタの次の記事が
        条件を満たした時点で改めて波が開く。取りこぼした記事自体は再通知されない)。
      - pubDateが空/パース不能などで判定できない場合は、見逃し防止のため
        安全側(=通知する側)に倒す。
    以前は「配信元が同じでpubDateが前回より新しければ即続報」という配信元ベースの
    判定だったが、配信元表記が無い/変わるタイトルを取りこぼすリスクがあったため、
    時間ベースの波状(バッチ)通知に統一した。
  - 「古い記事(old_ids)」の扱い:
    配信から fresh_hours 時間以上経った記事は、Googleニュースが後から
    掘り起こしてきただけのことが多い(既存記事がpubDateだけ更新されて
    再配信されるケースを含む)。
      - すでに通知済みの話題(既存クラスタ)にマッチする古い記事 → 捨てる
      - どのクラスタにもマッチしない古い記事(=初めての話題) → 通知する(見逃し防止)
    そのため類似度閾値(dedup_similarity_threshold)は、言い回しが多少変わった
    再配信記事なら既存クラスタにマッチする程度には緩く設定してある
    (上記の「古い記事は捨てる」判定に乗せるため)。
    ただし下げすぎると、地名などの共通語を含むだけの無関係な記事同士まで
    同じ話題とみなされ、別の出来事が黙って間引かれてしまう。
  - クラスタ情報は state/clusters_<feed_id>.json に永続化する。
    dedup_cluster_max_age_hours より古いクラスタは自然に破棄され、
    無関係な後日の記事が誤って同じクラスタに混ざるのを防ぐ。

注意:
  - あくまで「タイトルの見た目の類似度」による簡易判定であり完全ではない。
    閾値(dedup_similarity_threshold)はfeeds.jsonでフィードごとに調整できる。
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")

DEFAULT_FIRST_N = 3                     # 1つの波(バッチ)で無条件に通知する件数
DEFAULT_SIMILARITY_THRESHOLD = 0.2      # タイトル類似度(bigram Dice係数)がこれ以上なら同じ話題とみなす
                                        # 地名などの共通語を含むだけの無関係な記事同士が0.15前後まで上がるため、
                                        # それを誤って同じ話題とみなさない水準に置いている。
                                        # 詳細なトレードオフは test_logic.py の
                                        # test_similarity_threshold_reangled_followup_boundary を参照
DEFAULT_BATCH_COOLDOWN_MINUTES = 30     # 次の波を開くまでのクールダウン(分)。pubDate差・壁時計差の両方に使う
DEFAULT_CLUSTER_MAX_AGE_HOURS = 72      # これより古いクラスタは破棄する
MAX_TITLES_PER_CLUSTER = 5              # クラスタ内に保持する正規化タイトルの上限(メモリ節約)
MAX_CLUSTERS_PER_FEED = 300             # フィードあたりのクラスタ保持上限(古い順に間引く)

_LEADING_TAG_RE = re.compile(r"^[\s]*[【\[（(][^】\]）)]{0,20}[】\]）)]\s*")
_STRIP_CHARS_RE = re.compile(r"[\s　、。,.!?！？「」『』\"'\-ー・:：]")


def _clusters_path(feed_id: str) -> str:
    return os.path.join(STATE_DIR, f"clusters_{feed_id}.json")


def load_clusters(feed_id: str) -> list[dict]:
    """
    クラスタ状態を読み込む。

    形式が想定と違う要素(旧形式や手で壊してしまったデータ)は、
    落とすのではなく「使える形に整える」か、整えられなければ捨てる。
    state ファイルの不備で本番処理全体がクラッシュするのを防ぐため
    (実際に、フィールド名を変更した際に旧形式のデータでKeyErrorになり、
    run-notify が数時間落ち続けた事故があった)。
    """
    path = _clusters_path(feed_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return []
        raw_clusters = json.loads(content).get("clusters", [])
    except (OSError, ValueError):
        return []

    clusters = []
    for c in raw_clusters:
        if not isinstance(c, dict):
            continue
        titles = [t for t in c.get("titles", []) if isinstance(t, str) and t]
        if not titles:
            continue
        batch_open_count = c.get("batch_open_count")
        clusters.append(
            {
                "titles": titles[-MAX_TITLES_PER_CLUSTER:],
                "batch_open_count": batch_open_count if isinstance(batch_open_count, int) else 0,
                "batch_ref_pub_date": (
                    c.get("batch_ref_pub_date") if isinstance(c.get("batch_ref_pub_date"), str) else ""
                ),
                "last_notified_at": c.get("last_notified_at") or "",
            }
        )
    return clusters


def save_clusters(feed_id: str, clusters: list[dict]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_clusters_path(feed_id), "w", encoding="utf-8") as f:
        json.dump({"clusters": clusters}, f, ensure_ascii=False, indent=2)
        f.write("\n")


def normalize_title(title: str) -> str:
    """
    タイトルから、話題比較の邪魔になる部分を取り除く。
      - 先頭の【速報】【現場報告】(1面)等のタグ
      - 末尾の " - 配信元メディア名"
      - 空白・句読点・記号
    """
    t = title
    # 末尾の "- メディア名" を除去 (最後の " - " 以降を落とす)
    if " - " in t:
        t = t.rsplit(" - ", 1)[0]
    # 先頭のタグ【】[]（）を繰り返し除去 (「【速報】【大東市】...」のような複数タグ対策)
    while True:
        new_t = _LEADING_TAG_RE.sub("", t)
        if new_t == t:
            break
        t = new_t
    t = _STRIP_CHARS_RE.sub("", t)
    return t


def _bigrams(s: str) -> set:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _similarity(a: str, b: str) -> float:
    """
    2文字bigramのDice係数で類似度を測る。
    日本語の見出しは単語の順序や言い回しが配信元ごとにバラバラなことが多く、
    語順に敏感な difflib.SequenceMatcher よりも、語順に頑健なbigram一致率の方が
    「同じ出来事を報じた見出し」を安定して拾える。
    """
    set_a, set_b = _bigrams(a), _bigrams(b)
    if not set_a or not set_b:
        return 0.0
    return 2 * len(set_a & set_b) / (len(set_a) + len(set_b))


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _parse_iso(value: str) -> datetime:
    """
    wall clockのISO時刻文字列をパースする(クラスタの失効判定・クールダウン判定専用)。
    欠けている/壊れている場合は _EPOCH (大昔) を返す(=クールダウン済み扱い)。
    """
    if value:
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return _EPOCH
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return _EPOCH


def _parse_pub_date(value: str) -> datetime | None:
    """記事のpubDate(RFC822形式)をパースする。失敗したらNoneを返す。"""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def classify_articles(
    feed_id: str,
    articles: list[dict],
    now: datetime | None = None,
    first_n: int = DEFAULT_FIRST_N,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    batch_cooldown_minutes: int = DEFAULT_BATCH_COOLDOWN_MINUTES,
    cluster_max_age_hours: int = DEFAULT_CLUSTER_MAX_AGE_HOURS,
    old_ids: set[str] | None = None,
) -> tuple[list[dict], set[str]]:
    """
    未通知記事(古い順)を「同じ話題」でクラスタリングし、通知すべきものだけを返す
    (バッチ+クールダウン方式)。

    articles: [{"id", "title", "link", "pub_date"}, ...] 古い順
    old_ids : 「配信から時間が経っている記事」のID集合(main.py の fresh_hours 判定結果)
    戻り値: (to_notify, skip_ids)
      to_notify: 通知する記事のリスト(古い順、タイトルは一切書き換えない)
      skip_ids : 通知せず既読化だけする記事IDの集合

    通知するかどうかの判定:
      - 既存クラスタにマッチした(=すでに通知済みの話題)
          - old_ids に入っている(古い記事) → 通知しない
          - 現在の波がまだ first_n 件に達していない → そのまま通知(波に追加)
          - 波が first_n 件で埋まっている → 次の両方を満たす場合だけ次の波として通知:
              (a) pubDateが直前の波の最新pubDateから batch_cooldown_minutes 分以上後
              (b) 壁時計で直前の通知から batch_cooldown_minutes 分以上経過
            (pubDateが判定不能な場合は安全側に倒して満たしたものとみなす)
      - どのクラスタにもマッチしない(=初めての話題)
          → 古い記事であっても通知する(見逃しを防ぐため)。新しい波(1波目)を開始する。

    クラスタ状態は state/clusters_<feed_id>.json に保存される。
    """
    old_ids = old_ids or set()
    now = now or datetime.now(timezone.utc)
    cooldown = timedelta(minutes=batch_cooldown_minutes)
    clusters = load_clusters(feed_id)

    # 古すぎるクラスタは破棄(無関係な後日の記事が誤って同じ話題に混ざるのを防ぐ)
    fresh_clusters = []
    for c in clusters:
        last_notified = _parse_iso(c.get("last_notified_at", ""))
        if now - last_notified <= timedelta(hours=cluster_max_age_hours):
            fresh_clusters.append(c)
    clusters = fresh_clusters

    to_notify: list[dict] = []
    skip_ids: set[str] = set()

    for a in articles:
        norm = normalize_title(a["title"])

        best_cluster = None
        best_ratio = 0.0
        for c in clusters:
            ratio = max((_similarity(norm, t) for t in c["titles"]), default=0.0)
            if ratio > best_ratio:
                best_ratio = ratio
                best_cluster = c

        if best_cluster is not None and best_ratio >= similarity_threshold:
            # すでに通知済みの話題。配信から時間が経った記事は後追い報道とみなして捨てる
            if a["id"] in old_ids:
                skip_ids.add(a["id"])
                continue

            starting_new_batch = False
            if best_cluster["batch_open_count"] < first_n:
                notify = True
            else:
                article_pub = _parse_pub_date(a.get("pub_date", ""))
                ref_pub = _parse_pub_date(best_cluster.get("batch_ref_pub_date", ""))
                # pubDateが判定できない場合は見逃し防止のため満たしたものとみなす
                pub_gate_ok = (
                    article_pub is None
                    or ref_pub is None
                    or article_pub >= ref_pub + cooldown
                )
                last_notified = _parse_iso(best_cluster.get("last_notified_at", ""))
                cooldown_gate_ok = now >= last_notified + cooldown
                notify = pub_gate_ok and cooldown_gate_ok
                starting_new_batch = notify

            if not notify:
                skip_ids.add(a["id"])
                continue

            if starting_new_batch:
                best_cluster["batch_open_count"] = 0
                best_cluster["batch_ref_pub_date"] = ""

            to_notify.append(dict(a))
            best_cluster["batch_open_count"] += 1
            best_cluster["last_notified_at"] = now.isoformat()
            best_cluster["titles"].append(norm)
            best_cluster["titles"] = best_cluster["titles"][-MAX_TITLES_PER_CLUSTER:]

            article_pub = _parse_pub_date(a.get("pub_date", ""))
            if article_pub is not None:
                cur_ref = _parse_pub_date(best_cluster.get("batch_ref_pub_date", ""))
                if cur_ref is None or article_pub > cur_ref:
                    best_cluster["batch_ref_pub_date"] = a.get("pub_date", "")
        else:
            new_cluster = {
                "titles": [norm],
                "batch_open_count": 1,
                "batch_ref_pub_date": a.get("pub_date", ""),
                "last_notified_at": now.isoformat(),
            }
            clusters.append(new_cluster)
            to_notify.append(dict(a))

    if len(clusters) > MAX_CLUSTERS_PER_FEED:
        clusters = clusters[-MAX_CLUSTERS_PER_FEED:]

    save_clusters(feed_id, clusters)
    return to_notify, skip_ids
