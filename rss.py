# -*- coding: utf-8 -*-
"""
RSS取得・パース処理。

方針:
  - 外部ライブラリの feedparser は使わず、標準ライブラリ (urllib + xml.etree) で完結させる。
    → 依存が減り、GitHub Actions 上での構造崩れ（パース失敗）の原因を切り分けやすくするため。
  - Google News RSS は Bot 判定で弾かれることがあるため、ブラウザ相当の User-Agent を付ける。
  - 1記事ずつ dict {"id": guid, "title": str, "link": str, "pub_date": str} にして返す。
    id (guid) が既読管理のキーになる。guid が無い場合は link を代わりに使う。
  - Google News 側の一時的な不調 (503 Service Unavailable / 502 / タイムアウト等) は
    数十秒で復旧することがほとんどなので、その場で数秒待って自動的に再試行する。
    再試行しても駄目だった場合にだけ RssFetchError を投げてDiscordにエラー通知する
    (一瞬のブリップのたびにシステム通知チャンネルへエラーが飛ぶのを防ぐため)。
"""

import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import logger

_CONTEXT = "rss"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 20
# 一時的な失敗に対する再試行設定。
# 待ち時間の合計は 2+4=6秒 程度に抑えてある。GitHub Actions の実行時間を
# 無駄に伸ばさず、かつGoogle側の一瞬の不調は吸収できる長さ。
RETRY_BACKOFF_SECONDS = (2, 4)  # 要素数 = 再試行回数 (初回を含めると最大3回試行する)
# 再試行する価値があるHTTPステータス。
#   5xx: Google側の一時的な障害
#   408: リクエストタイムアウト
#   429: レート制限(少し待てば通ることがある)
# 404 や 400 などは待っても結果が変わらないため、即座にエラーにする。
RETRYABLE_STATUS = {408, 429}


@dataclass
class Article:
    id: str
    title: str
    link: str
    pub_date: str


class RssFetchError(Exception):
    """RSS取得(HTTP)段階のエラー。ネットワーク/HTTPステータス起因。"""


class RssParseError(Exception):
    """RSS解析(XML)段階のエラー。構造が想定と異なる場合。"""


def _is_retryable(e: Exception) -> bool:
    """
    その例外が「少し待てば直るかもしれない一時的な失敗」かどうかを判定する。

    Google News RSS は 503 Service Unavailable を時々返すが、これは
    こちらの不具合ではなくGoogle側の一時的な不調で、数十秒〜次回実行時には
    復旧していることがほとんど。こうしたものだけ再試行の対象にする。
    """
    if isinstance(e, urllib.error.HTTPError):
        return e.code in RETRYABLE_STATUS or 500 <= e.code < 600
    # URLError(DNS失敗・接続拒否など)とタイムアウトはネットワーク起因の一時的失敗とみなす
    return isinstance(e, (urllib.error.URLError, TimeoutError, OSError))


def _fetch_once(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
        status = res.status
        if status != 200:
            raise RssFetchError(f"HTTPステータス異常: {status} url={url}")
        return res.read()


def fetch_raw(url: str) -> bytes:
    """
    RSSのXML本文をバイト列で取得する。

    一時的な失敗(5xx・タイムアウト・接続エラーなど)は RETRY_BACKOFF_SECONDS に従って
    待ってから自動的に再試行する。再試行しても駄目な場合、および再試行しても
    無意味な失敗(404など)は RssFetchError を投げる。
    """
    attempts = len(RETRY_BACKOFF_SECONDS) + 1
    last_error: Exception | None = None

    for attempt in range(attempts):
        try:
            return _fetch_once(url)
        except RssFetchError:
            raise
        except Exception as e:
            last_error = e
            is_last_attempt = attempt == attempts - 1
            if is_last_attempt or not _is_retryable(e):
                break
            wait = RETRY_BACKOFF_SECONDS[attempt]
            logger.warn(
                _CONTEXT,
                f"RSS取得に一時的に失敗しました({attempt + 1}/{attempts}回目): {e} "
                f"→ {wait}秒待って再試行します url={url}",
            )
            time.sleep(wait)

    raise RssFetchError(
        f"RSS取得に失敗しました({attempts}回試行) url={url} : {last_error}"
    ) from last_error


def parse_items(raw_xml: bytes, url: str) -> list[Article]:
    """
    RSS 2.0 の <channel><item> を読み取って Article のリストにする。
    Google News RSS の構造 (title / link / guid / pubDate) を前提にしているため、
    構造が変わった場合はここで RssParseError を投げて呼び出し元がDiscordにエラー通知する。
    """
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as e:
        raise RssParseError(f"XMLとして解析できませんでした url={url} : {e}") from e

    channel = root.find("channel")
    if channel is None:
        raise RssParseError(f"<channel> 要素が見つかりません（RSS構造が変わった可能性）url={url}")

    items = channel.findall("item")
    if not items:
        # 0件は「エラー」ではなく「今は該当記事なし」の可能性があるため例外にはしない。
        return []

    articles: list[Article] = []
    broken = 0
    for item in items:
        title_el = item.find("title")
        link_el = item.find("link")
        guid_el = item.find("guid")
        pubdate_el = item.find("pubDate")

        title = (title_el.text or "").strip() if title_el is not None and title_el.text else ""
        link = (link_el.text or "").strip() if link_el is not None and link_el.text else ""
        guid = (guid_el.text or "").strip() if guid_el is not None and guid_el.text else link
        pub_date = (pubdate_el.text or "").strip() if pubdate_el is not None and pubdate_el.text else ""

        if not title or not link:
            # title/link が欠けているitemは1件だけスキップする。
            # ここで例外にすると「1件壊れているだけでそのフィード全体が処理されず、
            # 未読が溜まって次回以降に大量通知される」ことになるため、
            # 全件壊れている場合(=RSSの構造自体が変わった可能性)のみエラーにする。
            broken += 1
            continue

        articles.append(Article(id=guid, title=title, link=link, pub_date=pub_date))

    if broken and not articles:
        raise RssParseError(
            f"全{len(items)}件の item に title または link がありません"
            f"（RSS構造が変わった可能性）url={url}"
        )
    if broken:
        logger.warn(_CONTEXT, f"title/linkが欠けている item を{broken}件スキップしました url={url}")

    return articles


def fetch_articles(url: str) -> list[Article]:
    """取得+パースをまとめて行うエントリポイント。"""
    raw = fetch_raw(url)
    return parse_items(raw, url)
