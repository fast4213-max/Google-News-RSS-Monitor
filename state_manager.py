# -*- coding: utf-8 -*-
"""
既読管理 / 通知待ちキュー のJSON永続化。

保存場所:
  state/read_<feed_id>.json   … 通知済み(既読)の記事ID一覧
  state/queue_<feed_id>.json  … 未読だが今回まだ通知しきれなかった記事(次回に持ち越し)

方針:
  - フィードごとにファイルを分割 → フィードが増えても1ファイルが肥大化しない。
  - read.json は「上限件数」を持たせて無限に増え続けないようにする(古いものから削除)。
  - 既読IDは必ず「既読になった順(古い順)のリスト」として保存する。
    set をそのまま json.dump すると並び順が実行のたびに変わってしまい、
      (1) 中身が数件しか変わっていないのに state/*.json の差分が毎回全行になる
      (2) 上限を超えたときに「古い順に間引く」つもりが実際にはランダムな
          IDが捨てられ、捨てられた記事が「未読」に戻って再通知される
    という問題が起きるため。追記は append_read_ids() を使う。
  - ここではファイルの読み書きのみを行い、実際の git commit は
    呼び出し元のGitHub Actionsワークフローが担当する。
"""

import json
import os
from collections.abc import Iterable

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
MAX_READ_IDS_PER_FEED = 2000  # 既読リストの上限。超えたら古い方から間引く


def _read_path(feed_id: str) -> str:
    return os.path.join(STATE_DIR, f"read_{feed_id}.json")


def _queue_path(feed_id: str) -> str:
    return os.path.join(STATE_DIR, f"queue_{feed_id}.json")


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return default
        return json.loads(content)


def _save_json(path: str, data) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_read_id_list(feed_id: str) -> list[str]:
    """既読済み記事IDを「既読になった順(古い順)」のリストで読み込む。"""
    data = _load_json(_read_path(feed_id), {"ids": []})
    ids = data.get("ids", [])
    return [i for i in ids if isinstance(i, str)]


def load_read_ids(feed_id: str) -> set[str]:
    """既読済み記事IDの集合を読み込む(「既読かどうか」の判定用)。"""
    return set(load_read_id_list(feed_id))


def append_read_ids(feed_id: str, new_ids: Iterable[str]) -> None:
    """
    既読済み記事IDを追記する。

    既存の並び順(古い順)は保ったまま、まだ入っていないIDだけを末尾に足す。
    上限(MAX_READ_IDS_PER_FEED)を超えた分は、本当に古い方(リストの先頭)から捨てる。

    new_ids は「古い順に並んだ」リストを渡すこと(set を渡すと追記分の順序が
    実行のたびに変わってしまうため)。
    """
    id_list = load_read_id_list(feed_id)
    known = set(id_list)
    for article_id in new_ids:
        if article_id not in known:
            id_list.append(article_id)
            known.add(article_id)

    if len(id_list) > MAX_READ_IDS_PER_FEED:
        id_list = id_list[-MAX_READ_IDS_PER_FEED:]
    _save_json(_read_path(feed_id), {"ids": id_list})


def load_queue(feed_id: str) -> list[dict]:
    """
    次回に持ち越された未通知記事のリストを読み込む。

    id/title/link を欠いた要素(手で壊してしまった場合や、将来のスキーマ変更で
    形式が変わった場合)は読み飛ばす。ここで例外を投げると main.py 全体が
    落ちてstateのコミットが止まり、以後ずっと未読が溜まり続ける事故になるため
    (実際にクラスタstateで同種の事故が起きた)、クラスタ読み込みと同じ方針で防御的にする。
    """
    data = _load_json(_queue_path(feed_id), {"items": []})
    raw_items = data.get("items", [])
    if not isinstance(raw_items, list):
        return []
    items = []
    for item in raw_items:
        if (
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("title"), str)
            and isinstance(item.get("link"), str)
        ):
            items.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "link": item["link"],
                    "pub_date": item.get("pub_date") if isinstance(item.get("pub_date"), str) else "",
                }
            )
    return items


def save_queue(feed_id: str, items: list[dict]) -> None:
    """次回に持ち越す未通知記事のリストを保存する。"""
    _save_json(_queue_path(feed_id), {"items": items})
