# -*- coding: utf-8 -*-
"""
Discord Webhook 通知処理。

方針:
  - 1メッセージ = 1記事。タイトル自体をリンク化した embed で送信する
    (通常のメッセージ本文では Markdown の [タイトル](URL) 記法が効かないため、
    タイトルをクリック可能にするには embed の title+url を使う必要がある)。
  - Discord Webhook のレート制限(概ね 5リクエスト/2秒 程度)を避けるため、
    送信の合間に短いスリープを入れる。
  - エラー通知は通常通知と見分けやすいよう先頭に絵文字を付ける。
  - Webhook URL はフィードごとに別チャンネルへ送れるよう、呼び出し元から
    「環境変数名」を受け取ってその都度読み出す方式にしている(コードに直書きしない)。
  - エラー・システム通知は、原因のフィードに関わらず**常に**共通のシステム通知用
    Webhook (DISCORD_WEBHOOK_URL_SYSTEM) に送る。各フィード専用チャンネル(本チャンネル)
    には記事の通知だけが届くようにし、チャンネルを後から増やしてもエラー監視は
    システム通知チャンネル1箇所を見ればよい状態を保つ。
"""

import json
import os
import time
import urllib.request
import urllib.error

import logger

_CONTEXT = "discord-send"
SEND_INTERVAL_SECONDS = 1.2  # レート制限回避のための送信間隔
SYSTEM_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL_SYSTEM"  # フィード特定不能な異常時の送り先
RATE_LIMIT_MAX_RETRIES = 3  # 429(レート制限)発生時に待って再試行する最大回数
RATE_LIMIT_FALLBACK_WAIT = 5.0  # Discordが待機時間を教えてくれなかった場合の保険の待ち時間(秒)

# Discord Webhookはcloudflareの背後にあり、urllibのデフォルトUser-Agent
# ("Python-urllib/3.12") だとbot判定で403 (Cloudflareエラー1010) になることがあるため、
# ブラウザ相当のUser-Agentを明示的に付ける。rss.pyと同じ値で統一。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

EMBED_TITLE_MAX_LENGTH = 256  # Discord embed titleの上限文字数(超えると400エラーになる)


def _resolve_webhook_url(webhook_env: str) -> str:
    """
    環境変数名(webhook_env)からWebhook URLの実体を取り出す。
    feeds.json 側では実URLを書かず「環境変数の名前」だけを持たせる設計。
    """
    url = os.environ.get(webhook_env, "").strip()
    if not url:
        raise RuntimeError(
            f"環境変数 {webhook_env} が設定されていません。"
            "GitHub Secrets に登録されているか、feeds.json の webhook_env の綴りが"
            "Secrets の名前と一致しているか確認してください。"
        )
    return url


def _post(webhook_env: str, body: dict) -> None:
    """
    Discord Webhookへ1回分のメッセージをPOSTする。
    body はDiscord Webhook APIのペイロード全体(例: {"content": ...} や {"embeds": [...]}）。

    429 (Too Many Requests、レート制限)が返ってきた場合は、Discordが返す
    Retry-After ヘッダー(あと何秒待てば良いか)に従って待機し、
    RATE_LIMIT_MAX_RETRIES 回まで自動的に再試行する。
    Retry-After が無い場合は RATE_LIMIT_FALLBACK_WAIT 秒待って再試行する。
    それでも失敗する場合は例外を送出し、呼び出し元(send_articles)に委ねる。
    """
    url = _resolve_webhook_url(webhook_env)
    payload = json.dumps(body).encode("utf-8")

    for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 2):  # 通常送信1回 + 再試行分
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                # Discord Webhook は成功時 204 No Content
                if res.status not in (200, 204):
                    raise RuntimeError(f"Discord応答が異常です status={res.status}")
            return  # 成功
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt <= RATE_LIMIT_MAX_RETRIES:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait_seconds = float(retry_after) if retry_after else RATE_LIMIT_FALLBACK_WAIT
                except ValueError:
                    wait_seconds = RATE_LIMIT_FALLBACK_WAIT
                logger.warn(
                    _CONTEXT,
                    f"レート制限(429)を検知。{wait_seconds}秒待って再試行します "
                    f"({attempt}/{RATE_LIMIT_MAX_RETRIES}回目)",
                )
                time.sleep(wait_seconds)
                continue
            body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Discord送信でHTTPエラー status={e.code} body={body}") from e
        except Exception as e:
            raise RuntimeError(f"Discord送信に失敗しました: {e}") from e

    # ここに到達するのは429が RATE_LIMIT_MAX_RETRIES 回を超えて続いた場合
    raise RuntimeError(
        f"レート制限(429)が{RATE_LIMIT_MAX_RETRIES}回の再試行後も解消しませんでした"
    )


def send_article(webhook_env: str, feed_name: str, title: str, link: str) -> None:
    """
    1記事を、指定のWebhook(=そのフィード専用チャンネル)に通知する。
    タイトル自体をクリック可能なリンクにするため、embed の title+url を使う
    (通常のcontentにMarkdownリンクを書いても文字列のまま表示され、リンク化されないため)。

    embed には description 等を付けないため、Googleニュース等の埋め込み
    プレビュー(サムネイル付きカード)は表示されず、タイトルだけのシンプルな
    通知になる。embed自体がカード状に表示され左端に色付きの線が入るため、
    テキストの区切り線を別途入れなくても記事ごとの境目は分かりやすい。

    フィード名の見出し(【○○】)は表示しない。チャンネル自体がフィードごとに
    分かれているため、メッセージ内で改めてフィード名を出す必要がないため。
    feed_name はログ出力にのみ使用する。
    """
    safe_title = title if len(title) <= EMBED_TITLE_MAX_LENGTH else title[:EMBED_TITLE_MAX_LENGTH - 1] + "…"
    body = {"embeds": [{"title": safe_title, "url": link}]}
    _post(webhook_env, body)
    logger.info(_CONTEXT, f"通知送信OK ({webhook_env}): {title}")


def send_articles(webhook_env: str, feed_name: str, articles: list, max_count: int) -> list:
    """
    複数記事をまとめて、指定のWebhook(フィード専用チャンネル)に送信する。
    Discordのレート制限を避けるため間隔を空けて送信し、429時は _post 内で
    自動的に待機・再試行する(それでも失敗する場合のみここに例外が届く)。

    max_count を超える分は送信せずそのまま返す(呼び出し元がqueueに保存する)。

    重要: 送信中に例外が起きて途中で止まっても、例外を外に投げない。
    それまでに送信できた分は「成功」として戻り値に含め、失敗した記事以降は
    送信せずに残す。これにより、呼び出し元(main.py)は「送れた分だけ既読化し、
    残りは次回に持ち越す」という扱いができ、レート制限で一部だけ失敗した際に
    既に送信済みの記事が重複通知されるのを防ぐ。

    戻り値: 実際に送信できた記事のリスト(先頭から連続する成功分)
    """
    to_send = articles[:max_count]
    sent = []
    for i, article in enumerate(to_send):
        try:
            send_article(webhook_env, feed_name, article["title"], article["link"])
        except Exception as e:
            logger.error(
                _CONTEXT,
                f"{i + 1}/{len(to_send)}件目の送信に失敗したため、ここで送信を打ち切ります"
                f"（{len(sent)}件は送信済み・既読化されます）: {e}",
            )
            break
        sent.append(article)
        if i < len(to_send) - 1:
            time.sleep(SEND_INTERVAL_SECONDS)
    return sent


def send_error(context: str, message: str) -> None:
    """
    処理エラーをDiscordに通知する。
    タイトル通知と見分けやすいよう絵文字と context を先頭に出す。

    どのフィードで起きたエラーであっても、常に共通のシステム通知用Webhook
    (DISCORD_WEBHOOK_URL_SYSTEM) に送る。各フィード専用チャンネル(本チャンネル)には
    記事の通知だけを流し、エラー監視はシステム通知チャンネル1箇所に一元化するため。

    Discord送信自体が失敗した場合はActionsログにのみ残す(無限ループ防止のため再送しない)。
    """
    content = f"⚠️ **RSS通知エラー** [{context}]\n```\n{message[:1800]}\n```"
    try:
        _post(SYSTEM_WEBHOOK_ENV, {"content": content})
    except Exception as e:
        logger.error(
            _CONTEXT,
            f"エラー通知そのものの送信にも失敗しました (webhook_env={SYSTEM_WEBHOOK_ENV}): {e}",
        )
