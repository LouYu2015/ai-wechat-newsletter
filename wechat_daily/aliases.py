"""Alias database: default_anon derivation, command scanning, persistence, backup.

Phase 1 implementation.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import (
    ALIASES_FILE, ALIASES_CURSOR_FILE, ANON_SALT_FILE,
    ALIASES_BACKUP_DIR, ALIAS_RESERVATION_DAYS, GROUP_TABLE,
)
from .message_parser import MSG_TEXT, decompress, parse_sender_content
from .wechat_db import get_conn

# ── Word lists ──────────────────────────────────────────────────────────────────
# 40 × 40 = 1600 combinations. Plenty of headroom for 500+ current users and
# growth, with low collision rate during deterministic walking.
ADJECTIVES = [
    "聪明", "勇敢", "温柔", "沉稳", "活泼", "机智", "淡定", "好奇",
    "开朗", "认真", "幽默", "细心", "热情", "冷静", "睿智", "随和",
    "坚定", "乐观", "敏锐", "优雅", "豁达", "低调", "务实", "风趣",
    "严谨", "洒脱", "稳重", "灵巧", "博学", "专注",
    "温暖", "安静", "神秘", "直率", "坦荡", "谨慎", "灵动", "飘逸",
    "天真", "倔强",
]

ANIMALS = [
    "熊猫", "老虎", "狮子", "大象", "长颈鹿", "企鹅", "海豚", "猫头鹰",
    "狐狸", "兔子", "仓鼠", "猎豹", "北极熊", "浣熊", "鸵鸟", "火烈鸟",
    "犀牛", "斑马", "水獭", "貂", "白鹭", "鹦鹉", "松鼠", "羊驼",
    "树懒", "穿山甲", "蜂鸟", "雪豹", "荷兰猪", "剑鱼", "鲸鱼",
    "海狸", "河马", "麋鹿", "考拉", "飞鼠", "鸳鸯", "喜鹊", "海龟",
    "灰熊",
]

RESERVED_ALIASES = {
    # English
    "admin", "bot", "anonymous", "system", "unknown", "hidden",
    "moderator", "operator", "official", "service", "support",
    # Chinese — authority/role impersonation
    "管理员", "群主", "机器人", "助手", "系统", "官方", "小助手",
    "客服", "通知", "公告", "服务通知", "系统消息",
    # Chinese — identity ambiguity
    "匿名", "所有人", "全员", "大家", "某群友",
    # WeChat / Tencent brand
    "微信", "微信官方", "微信团队", "腾讯", "腾讯官方",
}

# Whitelist: CJK, Hiragana, Katakana, Hangul, Basic Latin letters/digits, _ - ·
# Explicitly excludes: Bopomofo (U+3100–312F, U+31A0–31BA), RTL/LTR embedding marks
# (U+200E/F, U+202A–202E, U+2066–2069), zero-width chars (U+200B–200D, U+FEFF),
# combining/annotation characters, and all other format/control categories.
_ALIAS_RE = re.compile(
    r'^['
    r'\u4e00-\u9fff'      # CJK Unified Ideographs
    r'\u3400-\u4dbf'      # CJK Extension A
    r'\uf900-\ufaff'      # CJK Compatibility Ideographs
    r'\u3040-\u309f'      # Hiragana
    r'\u30a0-\u30ff'      # Katakana
    r'\uac00-\ud7af'      # Hangul syllables
    r'a-zA-Z0-9_\-·'
    r']{1,16}$'
)


# ── Default anon derivation ─────────────────────────────────────────────────────

def _load_or_create_salt() -> bytes:
    ANON_SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if ANON_SALT_FILE.exists():
        raw = ANON_SALT_FILE.read_text().strip()
        if len(raw) == 64:
            try:
                return bytes.fromhex(raw)
            except ValueError:
                pass
        print(f"\033[93m[WARNING] anon_salt.txt 格式无效（长度={len(raw)}），将重新生成盐（仅影响新用户）\033[0m")
    salt = secrets.token_bytes(32)
    ANON_SALT_FILE.write_text(salt.hex())
    # Also back up the salt immediately
    ALIASES_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = ALIASES_BACKUP_DIR / "anon_salt.txt"
    if not backup.exists():
        shutil.copy2(ANON_SALT_FILE, backup)
    return salt


def _initial_anon_indices(wxid: str, salt: bytes) -> tuple[int, int]:
    """Hash-derived starting (adj_idx, animal_idx) for *wxid*."""
    h = hashlib.sha256(salt + wxid.encode()).digest()
    return h[0] % len(ADJECTIVES), h[1] % len(ANIMALS)


def compute_default_anon(wxid: str, salt: bytes) -> str:
    """Return the initial (no-collision-check) anon name for *wxid*.

    This is the starting point used by ``AliasDB._allocate_default_anon``;
    actual allocation may walk past collisions and assign a different combo.
    Tests may use this to predict the token of an isolated user (one with
    no namespace collisions).
    """
    adj_i, ani_i = _initial_anon_indices(wxid, salt)
    return f"{ADJECTIVES[adj_i]}的{ANIMALS[ani_i]}"


# ── AliasDB ─────────────────────────────────────────────────────────────────────

class AliasDB:
    """In-memory alias database with persistence to aliases.json."""

    def __init__(
        self,
        users: dict[str, dict],
        reservations: list[dict],
        salt: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._users = users
        self._reservations = reservations
        self._salt = salt
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._command_log: list[dict] = []  # records for this run

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, clock=None) -> "AliasDB":
        salt = _load_or_create_salt()
        if not ALIASES_FILE.exists():
            return cls({}, [], salt, clock)

        try:
            data = json.loads(ALIASES_FILE.read_text(encoding='utf-8'))
            return cls(
                users=data.get('users', {}),
                reservations=data.get('alias_reservations', []),
                salt=salt,
                clock=clock,
            )
        except Exception:
            # Attempt recovery from backup
            return cls._load_from_backup(salt, clock)

    @classmethod
    def _load_from_backup(cls, salt: bytes, clock=None) -> "AliasDB":
        if not ALIASES_BACKUP_DIR.exists():
            return cls({}, [], salt, clock)
        candidates = sorted(ALIASES_BACKUP_DIR.glob("*.json"), reverse=True)
        for f in candidates:
            if f.name == "anon_salt.txt":
                continue
            try:
                data = json.loads(f.read_text(encoding='utf-8'))
                print(f"\033[93m[WARNING] aliases.json 解析失败，已从备份恢复: {f.name}\033[0m")
                return cls(
                    users=data.get('users', {}),
                    reservations=data.get('alias_reservations', []),
                    salt=salt,
                    clock=clock,
                )
            except Exception:
                continue
        return cls({}, [], salt, clock)

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self) -> None:
        """Backup current file, then write updated aliases.json."""
        self._backup()
        self._expire_reservations()
        ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'version': 1,
            'token_format_version': 2,
            'updated_at': self._clock().isoformat(),
            'users': self._users,
            'alias_reservations': self._reservations,
        }
        ALIASES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    def _backup(self) -> None:
        ALIASES_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        today = self._clock().strftime('%Y-%m-%d')
        daily_backup = ALIASES_BACKUP_DIR / f"{today}.json"
        if ALIASES_FILE.exists() and not daily_backup.exists():
            shutil.copy2(ALIASES_FILE, daily_backup)
        # Rotate: delete backups older than 30 days
        cutoff = self._now_ts() - ALIAS_RESERVATION_DAYS * 86400
        for f in ALIASES_BACKUP_DIR.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)

    # ── User lookup ──────────────────────────────────────────────────────────

    def _allocate_default_anon(self, wxid: str) -> str:
        """Pick a unique ``{adj}的{animal}`` for *wxid* by walking from the
        hash-derived initial position, skipping combos taken by another user
        or reserved. Allocation is lazy — only wxids that actually appear in
        a day's messages get registered, so 40×40 = 1600 combos is plenty.
        """
        adj_i, ani_i = _initial_anon_indices(wxid, self._salt)
        n_adj, n_ani = len(ADJECTIVES), len(ANIMALS)
        used = self.all_default_anons()
        for k in range(n_adj * n_ani):
            ai = (adj_i + k // n_ani) % n_adj
            ni = (ani_i + k) % n_ani
            candidate = f"{ADJECTIVES[ai]}的{ANIMALS[ni]}"
            if candidate in used or candidate in RESERVED_ALIASES:
                continue
            return candidate
        raise RuntimeError("alias namespace exhausted; expand ADJECTIVES/ANIMALS")

    def get_or_create_user(self, wxid: str, real_name: str | None = None) -> dict:
        if wxid not in self._users:
            anon = self._allocate_default_anon(wxid)
            self._users[wxid] = {
                'default_anon': anon,
                'real_name_seen': real_name or wxid,
                'public_alias': None,
                'optout': False,
                'last_command_ts': None,
                'last_command': None,
            }
        elif real_name:
            self._users[wxid]['real_name_seen'] = real_name
        return self._users[wxid]

    def token_of(self, wxid: str) -> str:
        """Return the stable token (= default_anon) for *wxid*, allocating one
        on first use. Allocations persist on the next ``save()``."""
        return self.get_or_create_user(wxid)['default_anon']

    def wxid_of_token(self, token: str) -> str | None:
        for wxid, u in self._users.items():
            if u['default_anon'] == token:
                return wxid
        return None

    def public_name_of(self, wxid: str) -> str:
        user = self.get_or_create_user(wxid)
        return user.get('public_alias') or user['default_anon']

    def real_name_seen(self, wxid: str) -> str | None:
        """Last group-side nickname observed for *wxid*, if any."""
        user = self._users.get(wxid)
        if not user:
            return None
        name = user.get('real_name_seen')
        if not name or name == wxid:
            return None
        return name

    def is_optout(self, wxid: str) -> bool:
        user = self._users.get(wxid)
        return bool(user and user.get('optout'))

    def all_default_anons(self) -> set[str]:
        return {u['default_anon'] for u in self._users.values()}

    def optout_anons(self) -> set[str]:
        return {u['default_anon'] for u in self._users.values() if u.get('optout')}

    def optout_wxids(self) -> list[str]:
        """Return list of wxids that have opted out."""
        return [wxid for wxid, u in self._users.items() if u.get('optout')]

    # ── Command processing ────────────────────────────────────────────────────

    def _now_ts(self) -> float:
        return self._clock().timestamp()

    def _expire_reservations(self) -> None:
        cutoff = self._now_ts() - ALIAS_RESERVATION_DAYS * 86400
        self._reservations = [
            r for r in self._reservations if r['released_at'] >= cutoff
        ]

    def _alias_holder(self, alias: str) -> str | None:
        """Return wxid of the current holder of *alias*, or None."""
        for wxid, u in self._users.items():
            if u.get('public_alias') == alias:
                return wxid
        return None

    def _is_reserved(self, alias: str, requester_wxid: str) -> bool:
        """True if alias is in reservation period and requester is NOT the original holder."""
        cutoff = self._now_ts() - ALIAS_RESERVATION_DAYS * 86400
        for r in self._reservations:
            if r['alias'] == alias and r['released_at'] >= cutoff:
                if r['released_by_wxid'] != requester_wxid:
                    return True
        return False

    def _remove_reservation(self, alias: str) -> None:
        self._reservations = [r for r in self._reservations if r['alias'] != alias]

    def _add_reservation(self, alias: str, wxid: str) -> None:
        self._remove_reservation(alias)
        self._reservations.append({
            'alias': alias,
            'released_by_wxid': wxid,
            'released_at': self._now_ts(),
        })

    def _is_default_anon(self, alias: str) -> bool:
        return alias in self.all_default_anons()

    def _validate_alias(self, alias: str) -> str | None:
        """Return error message if alias is invalid, else None."""
        # alias must already be NFC-normalized by caller
        if not _ALIAS_RE.match(alias):
            return "别名格式不合法（仅支持中英文/数字/_ /- /·，长度 1–16）"
        if alias.lower() in RESERVED_ALIASES or alias in RESERVED_ALIASES:
            return f"别名「{alias}」是保留词，不可使用"
        if self._is_default_anon(alias):
            return f"别名「{alias}」与某位群友的默认匿名名冲突，不可使用"
        return None

    def apply_command(self, wxid: str, command: str, ts: int,
                      real_name: str | None = None) -> tuple[bool, str]:
        """Apply a single /alias or /optout command. Returns (success, message).

        Per design §6.5: trailing content after the keyword is ignored.
        """
        user = self.get_or_create_user(wxid, real_name)
        cmd = command.strip()

        # Use re.match so trailing content (e.g. "/optout 请帮我退出") is ignored
        if re.match(r'^/optout(\s|$)', cmd):
            user['optout'] = True
            user['last_command_ts'] = ts
            user['last_command'] = '/optout'
            self._command_log.append({'ts': ts, 'wxid': wxid, 'cmd': '/optout', 'ok': True, 'msg': 'optout 成功'})
            return True, 'optout 成功'

        if re.match(r'^/optin(\s|$)', cmd):
            user['optout'] = False
            user['last_command_ts'] = ts
            user['last_command'] = '/optin'
            self._command_log.append({'ts': ts, 'wxid': wxid, 'cmd': '/optin', 'ok': True, 'msg': 'optin 成功'})
            return True, 'optin 成功'

        if re.match(r'^/alias\s*$', cmd):
            # Clear alias
            old_alias = user.get('public_alias')
            if old_alias:
                self._add_reservation(old_alias, wxid)
            user['public_alias'] = None
            user['last_command_ts'] = ts
            user['last_command'] = cmd
            msg = '已清空别名，恢复默认匿名名'
            self._command_log.append({'ts': ts, 'wxid': wxid, 'cmd': cmd, 'ok': True, 'msg': msg})
            return True, msg

        m = re.match(r'^/alias\s+(\S+)', cmd)
        if m:
            new_alias = unicodedata.normalize('NFC', m.group(1))
            err = self._validate_alias(new_alias)
            if err:
                self._command_log.append({'ts': ts, 'wxid': wxid, 'cmd': cmd, 'ok': False, 'msg': err})
                return False, err

            holder = self._alias_holder(new_alias)
            if holder and holder != wxid:
                msg = f"别名「{new_alias}」已被占用（先到先得）"
                self._command_log.append({'ts': ts, 'wxid': wxid, 'cmd': cmd, 'ok': False, 'msg': msg})
                return False, msg

            if self._is_reserved(new_alias, wxid):
                msg = f"别名「{new_alias}」处于 30 天预留期，暂不可使用"
                self._command_log.append({'ts': ts, 'wxid': wxid, 'cmd': cmd, 'ok': False, 'msg': msg})
                return False, msg

            # Release old alias if changing
            old_alias = user.get('public_alias')
            if old_alias and old_alias != new_alias:
                self._add_reservation(old_alias, wxid)

            # If reclaiming own reserved alias, remove from reservations
            self._remove_reservation(new_alias)

            user['public_alias'] = new_alias
            user['last_command_ts'] = ts
            user['last_command'] = cmd
            msg = f"已设置公开别名为「{new_alias}」"
            self._command_log.append({'ts': ts, 'wxid': wxid, 'cmd': cmd, 'ok': True, 'msg': msg})
            return True, msg

        return False, f"未识别的指令: {cmd}"

    # ── Incremental command scan ──────────────────────────────────────────────

    def scan_commands(self, contact_map=None) -> list[dict]:
        """Scan new /alias /optout /optin commands from the message DB incrementally.

        Updates aliases in-place. Returns list of command log entries.
        """
        cursor_ts = 0
        if ALIASES_CURSOR_FILE.exists():
            try:
                cursor_ts = int(ALIASES_CURSOR_FILE.read_text().strip())
            except Exception:
                pass

        rows: list[tuple] = []
        for rel in ["message/message_0.db", "message/message_1.db"]:
            try:
                conn = get_conn(rel)
            except FileNotFoundError:
                continue
            cur = conn.cursor()
            cur.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{GROUP_TABLE}'"
            )
            if not cur.fetchone():
                continue
            cur.execute(
                f"SELECT create_time, message_content FROM {GROUP_TABLE} "
                f"WHERE local_type = ? AND create_time > ? ORDER BY create_time",
                (MSG_TEXT, cursor_ts),
            )
            rows.extend(cur.fetchall())

        rows.sort(key=lambda x: x[0])

        max_ts = cursor_ts
        for create_time, message_content in rows:
            raw = decompress(message_content)
            wxid, content = parse_sender_content(raw)
            if not wxid:
                continue
            content = content.strip().split('\n')[0].strip()  # first line only
            if not content.startswith('/'):
                max_ts = max(max_ts, create_time)
                continue

            real_name = None
            if contact_map:
                real_name = contact_map.by_wxid(wxid)

            self.apply_command(wxid, content, create_time, real_name)
            max_ts = max(max_ts, create_time)

        if max_ts > cursor_ts:
            ALIASES_CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
            ALIASES_CURSOR_FILE.write_text(str(max_ts))

        return list(self._command_log)

    def command_log(self) -> list[dict]:
        return list(self._command_log)
