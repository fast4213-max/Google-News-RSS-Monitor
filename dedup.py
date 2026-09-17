# -*- coding: utf-8 -*-
"""
同一ニュース（複数社が同じ出来事を別記事として配信したもの）をまとめて、
Discordに同じ話題の記事が延々と流れ続けるのを防ぐための「話題クラスタリング」処理。

方針:
  - Googleニュースの検索RSSは、同じ出来事について各社(産経/毎日/Yahoo!ニュース等)が
    それぞれ別記事(別guid)を出すため、そのまま通知すると同じ話題が何件も並んでしまう。
  - タイトルを正規化して類似度を比較し、「同じ話題」とみなせる記事をクラスタにまとめる。
  - 1つの話題について、**「直近 dedup_followup_minutes 分の間」に通知できるのは
    最大 dedup_first_n 件まで**というスライディングウィンドウ方式で間引く。
      - 最初の1件目・2件目(first_n=2)はそのまま通知する。
      - その後 dedup_followup_minutes 分（デフォルト30分）以内に来た同話題の記事は
        既読化だけして通知しない（ここがノイズ削減の本体）。
      - dedup_followup_minutes 分、通知が無いまま経過すると「新しい枠」が開き、
        再びそこから first_n 件まで通知できるようになる（速報→続報→続報…と、
        本当に展開のある事件は各段階で最大2件ずつ拾える）。
    タイトルへのラベル付与は行わない(記事自身のタイトルにすでに【続報】等が
    付いていればそのまま表示され、こちら側で追加の接頭辞は付けない)。
  - 経過時間の判定は記事自身のpubDateではなく、必ず「このプログラムが実際に処理した時刻
    (wall clock)」を基準にする。記事のpubDateはGoogleニュースが古い記事を後から
    再インデックスするとあてにならない(実際の掲載時刻とズレる)ことがあるため、
    これを基準にすると「1回の実行内でたまたまpubDateが数時間ズレている古い重複記事」まで
    続報として通知してしまうバグがあった。wall clock基準にすることで、同じ実行内で
    処理された記事は(pubDateがバラバラでも)確実にまとめて間引かれ、次の定期実行
    (≒1時間後)になって初めて新しい枠として通知される。
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

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")

DEFAULT_FIRST_N = 2                  # 各ウィンドウでそのまま通知する最大件数
DEFAULT_FOLLOWUP_MINUTES = 30        # 通知が途絶えてからこれ以上経つと新しいウィンドウが開く
DEFAULT_SIMILARITY_THRESHOLD = 0.28  # タイトル類似度(bigram Dice係数)がこれ以上なら同じ話題とみなす
DEFAULT_CLUSTER_MAX_AGE_HOURS = 72  # これより古いクラスタは破棄する
MAX_TITLES_PER_CLUSTER = 5          # クラスタ内に保持する正規化タイトルの上限(メモリ節約)
MAX_CLUSTERS_PER_FEED = 300         # フィードあたりのクラスタ保持上限(古い順に間引く)

_LEADING_TAG_RE = re.compile(r"^[\s]*[【\[（(][^】\]）)]{0,20}[】\]）)]\s*")
_STRIP_CHARS_RE = re.compile(r"[\s　、。,.!?！？「」『』\"'\-ー・:：]")


def _clusters_path(feed_id: str) -> str:
    return os.path.join(STATE_DIR, f"clusters_{feed_id}.json")


def load_clusters(feed_id: str) -> list[dict]:
    path = _clusters_path(feed_id)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return []
        return json.loads(content).get("clusters", [])


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


def _parse_iso(value: str, fallback: datetime) -> datetime:
    """wall clockのISO時刻文字列をパースする。壊れていればfallbackを返す。"""
    if value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return fallback


def classify_articles(
    feed_id: str,
    articles: list[dict],
    now: datetime | None = None,
    first_n: int = DEFAULT_FIRST_N,
    followup_minutes: int = DEFAULT_FOLLOWUP_MINUTES,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    cluster_max_age_hours: int = DEFAULT_CLUSTER_MAX_AGE_HOURS,
) -> tuple[list[dict], set[str]]:
    """
    未通知記事(古い順)を「同じ話題」でクラスタリングし、通知すべきものだけを返す。

    articles: [{"id", "title", "link", "pub_date"}, ...] 古い順
    戻り値: (to_notify, skip_ids)
      to_notify: 通知する記事のリスト(古い順、タイトルは一切書き換えない)
      skip_ids : 通知せず既読化だけする記事IDの集合

    「直近 followup_minutes 分の間に通知できるのは最大 first_n 件まで」という
    スライディングウィンドウ方式。followup_minutes 分、通知が無いまま経過すると
    枠がリセットされ、再び first_n 件まで通知できるようになる。

    経過時間の判定は、記事自身のpubDateではなく必ず「このプログラムを実際に
    呼び出した時刻(now、通常は処理開始時のwall clock)」を基準にする。
    そのため同じ1回の呼び出し内で渡された articles は、たとえpubDateが
    バラバラでも「同時刻に処理されたもの」として扱われる。

    クラスタ状態は state/clusters_<feed_id>.json に保存される。
    """
    now = now or datetime.now(timezone.utc)
    clusters = load_clusters(feed_id)

    # 古すぎるクラスタは破棄(無関係な後日の記事が誤って同じ話題に混ざるのを防ぐ)
    fresh_clusters = []
    for c in clusters:
        last_notified = _parse_iso(c.get("last_notified_at", ""), now)
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
            elapsed = now - _parse_iso(best_cluster["last_notified_at"], now)
            if elapsed >= timedelta(minutes=followup_minutes):
                # 通知が途絶えてから followup_minutes 分経過 → 新しい枠を開く
                best_cluster["notified_count"] = 0
            if best_cluster["notified_count"] >= first_n:
                skip_ids.add(a["id"])
                continue
            to_notify.append(dict(a))
            best_cluster["notified_count"] += 1
            best_cluster["last_notified_at"] = now.isoformat()
            best_cluster["titles"].append(norm)
            best_cluster["titles"] = best_cluster["titles"][-MAX_TITLES_PER_CLUSTER:]
        else:
            new_cluster = {
                "titles": [norm],
                "notified_count": 1,
                "last_notified_at": now.isoformat(),
            }
            clusters.append(new_cluster)
            to_notify.append(dict(a))

    if len(clusters) > MAX_CLUSTERS_PER_FEED:
        clusters = clusters[-MAX_CLUSTERS_PER_FEED:]

    save_clusters(feed_id, clusters)
    return to_notify, skip_ids
