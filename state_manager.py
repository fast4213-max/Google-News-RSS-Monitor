# -*- coding: utf-8 -*-
"""
既読管理 / 通知待ちキュー のJSON永続化。

保存場所:
  state/read_<feed_id>.json   … 通知済み(既読)の記事ID一覧
  state/queue_<feed_id>.json  … 未読だが今回まだ通知しきれなかった記事(次回に持ち越し)

方針:
  - フィードごとにファイルを分割 → フィードが増えても1ファイルが肥大化しない。
  - read.json は「上限件数」を持たせて無限に増え続けないようにする(古いものから削除)。
  - ここではファイルの読み書きのみを行い、実際の git commit は main.py 側 (git_sync.py) が担当する。
    (このスクリプト自身はGitHub Actions上で実行され、リポジトリのファイルを直接書き換える)
"""

import json
import os

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


def load_read_ids(feed_id: str) -> set[str]:
    """既読済み記事IDの集合を読み込む。"""
    data = _load_json(_read_path(feed_id), {"ids": []})
    return set(data.get("ids", []))


def save_read_ids(feed_id: str, ids: set[str]) -> None:
    """既読済み記事IDを保存する。上限を超えた分は古い順(リストの先頭)から間引く。"""
    id_list = list(ids)
    if len(id_list) > MAX_READ_IDS_PER_FEED:
        id_list = id_list[-MAX_READ_IDS_PER_FEED:]
    _save_json(_read_path(feed_id), {"ids": id_list})


def load_queue(feed_id: str) -> list[dict]:
    """次回に持ち越された未通知記事のリストを読み込む。"""
    data = _load_json(_queue_path(feed_id), {"items": []})
    return data.get("items", [])


def save_queue(feed_id: str, items: list[dict]) -> None:
    """次回に持ち越す未通知記事のリストを保存する。"""
    _save_json(_queue_path(feed_id), {"items": items})
