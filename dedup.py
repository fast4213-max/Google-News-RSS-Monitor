# -*- coding: utf-8 -*-
"""
同一ニュース（複数社が同じ出来事を別記事として配信したもの）をまとめて、
Discordに同じ話題の記事が延々と流れ続けるのを防ぐための「話題クラスタリング」処理。

方針:
  - Googleニュースの検索RSSは、同じ出来事について各社(産経/毎日/Yahoo!ニュース等)が
    それぞれ別記事(別guid)を出すため、そのまま通知すると同じ話題が何件も並んでしまう。
  - タイトルを正規化して類似度を比較し、「同じ話題」とみなせる記事をクラスタにまとめる。
  - 1つの話題について、**最初の dedup_first_n 件（デフォルト3件、最速で報じた3社分）は
    そのまま通知する**。このとき各記事の「配信元(タイトル末尾の " - 媒体名")」と
    「pubDate」をクラスタに記録しておく。
  - 4件目以降(続報)は、**同じ配信元が同じ話題について、前回より新しいpubDateで
    改めて記事を出した場合にだけ**続報として通知する。
      - 例: 最速3件が「MBSニュース・TBS NEWS DIG・Yahoo!ニュース」だったとして、
        その後MBSニュースが同じ話題を新しいpubDateで更新した記事を出せば通知する。
      - 同じ配信元が同じ内容を重複して出すことは基本的に無いため、
        「同じ配信元 かつ pubDateが前回より新しい」は「本当に内容が更新された
        (=続報)」とほぼ確実にみなせる、という考え方。壁時計での待機時間は設けない
        (以前は「前回通知から90分経過で新しい枠が開く」方式だったが、これだと
        本当の続報が来ても最大90分待たされる問題があったため廃止した)。
      - 逆に、最速3件に含まれていない**初見の配信元**が4件目以降に出てきた場合は、
        「本当に内容が更新された記事なのか、単なる他社の後追い(内容は同じ)なのか」を
        区別する手段が無いため、安全側に倒して通知しない。
  - 「古い記事(old_ids)」の扱い:
    配信から fresh_hours 時間以上経った記事は、Googleニュースが後から
    掘り起こしてきただけのことが多い。
      - すでに通知済みの話題(既存クラスタ)にマッチする古い記事 → 捨てる
        (これにより、まだ notified_count が first_n に達していない状態でも、
        古い記事が「最速枠」を不正に埋めてしまうのを防ぐ)
      - どのクラスタにもマッチしない古い記事(=初めての話題) → 通知する(見逃し防止)
  - クラスタ情報は state/clusters_<feed_id>.json に永続化する。
    dedup_cluster_max_age_hours より古いクラスタは自然に破棄され、
    無関係な後日の記事が誤って同じクラスタに混ざるのを防ぐ。

注意:
  - あくまで「タイトルの見た目の類似度」による簡易判定であり完全ではない。
    閾値(dedup_similarity_threshold)はfeeds.jsonでフィードごとに調整できる。
  - 配信元名は、タイトル末尾の " - 媒体名" 表記から抜き出している。この表記が
    無いタイトルは配信元が特定できず、続報判定の対象にはならない(=4件目以降は
    通知されない)。
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")

DEFAULT_FIRST_N = 3                  # 最速で無条件に通知する件数
DEFAULT_SIMILARITY_THRESHOLD = 0.28  # タイトル類似度(bigram Dice係数)がこれ以上なら同じ話題とみなす
DEFAULT_CLUSTER_MAX_AGE_HOURS = 72   # これより古いクラスタは破棄する
MAX_TITLES_PER_CLUSTER = 5           # クラスタ内に保持する正規化タイトルの上限(メモリ節約)
MAX_SOURCES_PER_CLUSTER = 20         # クラスタ内に保持する配信元の上限(メモリ節約)
MAX_CLUSTERS_PER_FEED = 300          # フィードあたりのクラスタ保持上限(古い順に間引く)

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
        notified_count = c.get("notified_count")
        raw_sources = c.get("sources", {})
        sources = (
            {k: v for k, v in raw_sources.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(raw_sources, dict)
            else {}
        )
        clusters.append(
            {
                "titles": titles[-MAX_TITLES_PER_CLUSTER:],
                "notified_count": notified_count if isinstance(notified_count, int) else 0,
                "last_notified_at": c.get("last_notified_at") or "",
                "sources": sources,
            }
        )
    return clusters


def save_clusters(feed_id: str, clusters: list[dict]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_clusters_path(feed_id), "w", encoding="utf-8") as f:
        json.dump({"clusters": clusters}, f, ensure_ascii=False, indent=2)
        f.write("\n")


def extract_source(title: str) -> str:
    """
    タイトル末尾の " - 媒体名" から配信元名を取り出す。
    無ければ空文字を返す(=配信元不明。続報判定の対象にはならない)。
    """
    if " - " in title:
        return title.rsplit(" - ", 1)[1].strip()
    return ""


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
    wall clockのISO時刻文字列をパースする(クラスタの失効判定専用)。
    欠けている/壊れている場合は _EPOCH (大昔) を返す。
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
    cluster_max_age_hours: int = DEFAULT_CLUSTER_MAX_AGE_HOURS,
    old_ids: set[str] | None = None,
) -> tuple[list[dict], set[str]]:
    """
    未通知記事(古い順)を「同じ話題」でクラスタリングし、通知すべきものだけを返す。

    articles: [{"id", "title", "link", "pub_date"}, ...] 古い順
    old_ids : 「配信から時間が経っている記事」のID集合(main.py の fresh_hours 判定結果)
    戻り値: (to_notify, skip_ids)
      to_notify: 通知する記事のリスト(古い順、タイトルは一切書き換えない)
      skip_ids : 通知せず既読化だけする記事IDの集合

    通知するかどうかの判定:
      - 既存クラスタにマッチした(=すでに通知済みの話題)
          - old_ids に入っている(古い記事) → 通知しない
            (「初回3件」の枠を古い後追い記事が不正に埋めるのを防ぐため、
            first_n未満でもここで弾く)
          - まだ first_n 件に達していない → そのまま通知(最速枠)
          - first_n 件に達している(続報の可能性) →
            「同じ配信元が過去にこの話題で通知されており、かつ今回のpubDateが
            その配信元の前回pubDateより新しい」場合にだけ続報として通知する。
            配信元が不明、または初見の配信元の場合は通知しない。
      - どのクラスタにもマッチしない(=初めての話題)
          → 古い記事であっても通知する(見逃しを防ぐため)

    クラスタ状態は state/clusters_<feed_id>.json に保存される。
    """
    old_ids = old_ids or set()
    now = now or datetime.now(timezone.utc)
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
        source = extract_source(a["title"])

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

            notify = False
            if best_cluster["notified_count"] < first_n:
                # まだ最速枠が埋まっていない
                notify = True
            elif source:
                # 続報判定: 同じ配信元が、前回より新しいpubDateで改めて記事を出したか
                prev_pub_str = best_cluster["sources"].get(source)
                article_pub = _parse_pub_date(a.get("pub_date", ""))
                prev_pub = _parse_pub_date(prev_pub_str) if prev_pub_str else None
                if prev_pub is not None and article_pub is not None and article_pub > prev_pub:
                    notify = True

            if not notify:
                skip_ids.add(a["id"])
                continue

            to_notify.append(dict(a))
            best_cluster["notified_count"] += 1
            best_cluster["last_notified_at"] = now.isoformat()
            best_cluster["titles"].append(norm)
            best_cluster["titles"] = best_cluster["titles"][-MAX_TITLES_PER_CLUSTER:]
            if source:
                best_cluster["sources"][source] = a.get("pub_date", "")
                if len(best_cluster["sources"]) > MAX_SOURCES_PER_CLUSTER:
                    # 古い配信元から間引く(dictは挿入順を保持するのでpopitem(last=False)相当)
                    oldest_source = next(iter(best_cluster["sources"]))
                    del best_cluster["sources"][oldest_source]
        else:
            new_cluster = {
                "titles": [norm],
                "notified_count": 1,
                "last_notified_at": now.isoformat(),
                "sources": {source: a.get("pub_date", "")} if source else {},
            }
            clusters.append(new_cluster)
            to_notify.append(dict(a))

    if len(clusters) > MAX_CLUSTERS_PER_FEED:
        clusters = clusters[-MAX_CLUSTERS_PER_FEED:]

    save_clusters(feed_id, clusters)
    return to_notify, skip_ids
