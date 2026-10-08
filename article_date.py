# -*- coding: utf-8 -*-
"""
元記事の「本当の公開日」を取得する。

背景:
  Googleニュースの pubDate は、元記事が再インデックスされるとその時刻に更新されることがある
  (実例: 6/26配信の記事が pubDate=10/8 で流れてきた)。pubDate だけで古さを判定すると
  こうした記事をすり抜けるため、通知直前に元記事のページを開いて公開日を確認する。

方針:
  - Googleニュースのリンク(news.google.com/rss/articles/...)は batchexecute で元URLに解決する。
  - 公開日は meta / JSON-LD の datePublished を優先し、無ければ「M/D HH:MM 配信」形式の表記を読む。
  - 取得に失敗した場合は None を返す。呼び出し側は「不明なら通知する」(見逃し防止)側に倒すこと。
"""

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import logger

_CONTEXT = "article_date"
_JST = timezone(timedelta(hours=9))
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_TIMEOUT = 10
_BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

_META_DATE_RE = re.compile(
    r'(?:datePublished|article:published_time|pubdate)["\']?\s*[:=,]?\s*'
    r'(?:content=)?["\']?(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?)',
    re.I,
)
# 「06/26 12:32 配信」のように年が無い表記(ABC朝日放送など)
_MD_HAISHIN_RE = re.compile(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*配信")


def _get(url: str, data: bytes | None = None, headers: dict | None = None) -> str:
    h = {"User-Agent": _UA, **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as res:
        return res.read().decode("utf-8", "ignore")


def resolve_google_news_url(link: str) -> str | None:
    """news.google.com/rss/articles/<id> を元記事のURLに解決する。失敗時は None。"""
    if "news.google.com/rss/articles/" not in link:
        return link
    gid = link.split("/articles/", 1)[1].split("?", 1)[0]
    page = _get(f"https://news.google.com/rss/articles/{gid}?hl=ja&gl=JP&ceid=JP:ja")
    ts = re.search(r'data-n-a-ts="(\d+)"', page)
    sg = re.search(r'data-n-a-sg="([^"]+)"', page)
    if not ts or not sg:
        return None
    inner = json.dumps(
        ["garturlreq",
         [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
          "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
         gid, int(ts.group(1)), sg.group(1)]
    )
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
    res = _get(
        _BATCH_URL,
        data=body.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    m = re.search(r'garturlres\\",\\"(https?://[^"\\]+)', res)
    return m.group(1) if m else None


def extract_published_at(html: str, now: datetime | None = None) -> datetime | None:
    """HTMLから公開日時を読み取る(tz付きdatetime)。読めなければ None。"""
    now = now or datetime.now(timezone.utc)
    m = _META_DATE_RE.search(html)
    if m:
        s = m.group(1).replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=_JST)
        except ValueError:
            pass
    m = _MD_HAISHIN_RE.search(html)
    if m:
        mon, day, hh, mm = (int(x) for x in m.groups())
        now_jst = now.astimezone(_JST)
        try:
            dt = datetime(now_jst.year, mon, day, hh, mm, tzinfo=_JST)
            if dt > now_jst + timedelta(days=1):  # 年またぎ(12月の記事を1月に見ている等)
                dt = dt.replace(year=now_jst.year - 1)
            return dt
        except ValueError:
            return None
    return None


def fetch_published_at(link: str, ctx: str) -> datetime | None:
    """リンク先の本当の公開日時を返す。何か失敗したら None(=不明)。"""
    try:
        url = resolve_google_news_url(link)
        if not url:
            return None
        return extract_published_at(_get(url))
    except Exception as e:
        logger.warn(ctx, f"元記事の公開日取得に失敗(通知は続行します): {e}")
        return None
