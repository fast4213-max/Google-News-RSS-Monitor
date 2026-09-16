# -*- coding: utf-8 -*-
"""
RSS取得・パース処理。

方針:
  - 外部ライブラリの feedparser は使わず、標準ライブラリ (urllib + xml.etree) で完結させる。
    → 依存が減り、GitHub Actions 上での構造崩れ（パース失敗）の原因を切り分けやすくするため。
  - Google News RSS は Bot 判定で弾かれることがあるため、ブラウザ相当の User-Agent を付ける。
  - 1記事ずつ dict {"id": guid, "title": str, "link": str, "pub_date": str} にして返す。
    id (guid) が既読管理のキーになる。guid が無い場合は link を代わりに使う。
"""

import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 20


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


def fetch_raw(url: str) -> bytes:
    """RSSのXML本文をバイト列で取得する。失敗したら RssFetchError。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            status = res.status
            if status != 200:
                raise RssFetchError(f"HTTPステータス異常: {status} url={url}")
            return res.read()
    except RssFetchError:
        raise
    except Exception as e:
        raise RssFetchError(f"RSS取得に失敗しました url={url} : {e}") from e


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
    for idx, item in enumerate(items):
        title_el = item.find("title")
        link_el = item.find("link")
        guid_el = item.find("guid")
        pubdate_el = item.find("pubDate")

        if title_el is None or link_el is None:
            # title/link が無いitemは構造異常とみなす
            raise RssParseError(
                f"item[{idx}] に title または link がありません（RSS構造が変わった可能性）url={url}"
            )

        title = (title_el.text or "").strip()
        link = (link_el.text or "").strip()
        guid = (guid_el.text or "").strip() if guid_el is not None and guid_el.text else link
        pub_date = (pubdate_el.text or "").strip() if pubdate_el is not None and pubdate_el.text else ""

        if not title or not link:
            raise RssParseError(
                f"item[{idx}] の title または link が空です（RSS構造が変わった可能性）url={url}"
            )

        articles.append(Article(id=guid, title=title, link=link, pub_date=pub_date))

    return articles


def fetch_articles(url: str) -> list[Article]:
    """取得+パースをまとめて行うエントリポイント。"""
    raw = fetch_raw(url)
    return parse_items(raw, url)
