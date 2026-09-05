#!/usr/bin/env python3
"""
cmdgo-provider 多账号池 — 移植自上游 dsh-cmdgo-provider/src/pool.ts。

每个 OAuth 登录产生一个 API key；把多个 key 组成账号池即可摊薄套餐额度。
与上游不同点：本项目不依赖 dsh-credentials 凭据存储，key 与账号元数据直接
存在同一份 ``accounts.json``（位于 %APPDATA%\\cmdgo-provider\\）。

调度：对「已启用且冷却过期」的账号做轮询；某账号请求失败会进入指数退避冷却
（上限 15 分钟），适配器可在同一次请求内转接下一个候选账号。

@module cmdgo.pool
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from typing import Optional

import credstore

# 首次失败的冷却时长；连续失败按指数翻倍。
COOLDOWN_BASE_MS = 30_000
# 指数冷却上限。
COOLDOWN_MAX_MS = 15 * 60_000
# 距上次失败超过该时长视为已恢复：failCount 清零、冷却从基础值重新上升。
# 否则昨天连败的账号今天第一次失败就直接顶格冷却 15 分钟。
FAIL_DECAY_MS = 30 * 60_000


def _data_dir() -> str:
    if os.name == "nt":
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        root = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(root, "cmdgo-provider")
    os.makedirs(path, exist_ok=True)
    return path


POOL_FILE = os.path.join(_data_dir(), "accounts.json")

# 供外部复用（cmdgo_provider 的 token.json 也在同一数据目录），避免两处重复实现。
data_dir = _data_dir


class PoolAccount:
    """池中的一个账号。id 稳定、唯一；key 直接保存在本记录里（本项目做法）。"""

    def __init__(self, *, id: str, apiKey: str, userName: Optional[str] = None,
                 keyName: Optional[str] = None, addedAt: Optional[int] = None,
                 enabled: bool = True, failCount: int = 0,
                 cooldownUntil: Optional[int] = None, lastError: Optional[str] = None,
                 lastUsedAt: Optional[int] = None, lastFailAt: Optional[int] = None,
                 okCount: int = 0, errCount: int = 0,
                 tokensIn: int = 0, tokensOut: int = 0) -> None:
        self.id = id
        self.apiKey = apiKey
        self.userName = userName
        self.keyName = keyName
        self.addedAt = addedAt if addedAt is not None else int(time.time() * 1000)
        self.enabled = enabled
        self.failCount = failCount
        self.cooldownUntil = cooldownUntil
        self.lastError = lastError
        self.lastUsedAt = lastUsedAt
        self.lastFailAt = lastFailAt
        # 累计用量统计（跨会话持久化）：成功/失败请求数与 token 数。
        self.okCount = okCount
        self.errCount = errCount
        self.tokensIn = tokensIn
        self.tokensOut = tokensOut

    def to_dict(self) -> dict:
        """序列化为 manifest 行（不对外暴露 API key 明文的查看接口除外，这里完整落地）。"""
        d = {
            "id": self.id,
            "apiKey": self.apiKey,
            "addedAt": self.addedAt,
            "enabled": self.enabled,
            "failCount": self.failCount,
            "okCount": self.okCount,
            "errCount": self.errCount,
            "tokensIn": self.tokensIn,
            "tokensOut": self.tokensOut,
        }
        if self.userName is not None:
            d["userName"] = self.userName
        if self.keyName is not None:
            d["keyName"] = self.keyName
        if self.cooldownUntil is not None:
            d["cooldownUntil"] = self.cooldownUntil
        if self.lastError is not None:
            d["lastError"] = self.lastError
        if self.lastUsedAt is not None:
            d["lastUsedAt"] = self.lastUsedAt
        if self.lastFailAt is not None:
            d["lastFailAt"] = self.lastFailAt
        return d

    @property
    def display_name(self) -> str:
        """给账号一个可读的显示名：优先用户名，其次密钥名，其次 id。

        收编进来的旧账号通常没有 userName/keyName，此时回退到 id；但对于
        ``default`` 这类由 adopt_legacy 产生的账号，给出更友好的标签。
        """
        if self.userName:
            return self.userName
        if self.keyName:
            return self.keyName
        if self.id == "default":
            return "默认账号（旧 key）"
        return self.id

    @classmethod
    def from_dict(cls, d: dict) -> "PoolAccount":
        return cls(
            id=d.get("id", ""),
            apiKey=d.get("apiKey", ""),
            userName=d.get("userName"),
            keyName=d.get("keyName"),
            addedAt=d.get("addedAt"),
            enabled=d.get("enabled", True),
            failCount=d.get("failCount", 0),
            cooldownUntil=d.get("cooldownUntil"),
            lastError=d.get("lastError"),
            lastUsedAt=d.get("lastUsedAt"),
            lastFailAt=d.get("lastFailAt"),
            okCount=d.get("okCount", 0),
            errCount=d.get("errCount", 0),
            tokensIn=d.get("tokensIn", 0),
            tokensOut=d.get("tokensOut", 0),
        )


def _slug(value: Optional[str], fallback: str) -> str:
    cleaned = (value or "").lower()
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in cleaned)
    cleaned = "-".join([p for p in cleaned.split("-") if p])
    return cleaned[:16] if cleaned else fallback


class AccountPool:
    """账号池。所有变更都会写入 manifest（原子写：临时文件 + rename）。"""

    def __init__(self, base_ref: str = "cmdgo", log=print, file: Optional[str] = None) -> None:
        self._base_ref = base_ref  # 保留：兼容旧单 key 的收编
        self._log = log
        self._file = file  # 可覆盖（测试隔离）；默认 POOL_FILE
        self._accounts: list[PoolAccount] = []
        self._loaded = False
        self._cursor = 0
        self._lock = threading.RLock()

    # ---- 文件读写 ----
    @property
    def file(self) -> str:
        return self._file or POOL_FILE

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            parsed = json.loads(credstore.read_credential(self.file))
            acc = parsed.get("accounts")
            if isinstance(acc, list):
                self._accounts = [
                    PoolAccount.from_dict(a) for a in acc
                    if isinstance(a, dict) and isinstance(a.get("id"), str) and isinstance(a.get("apiKey"), str)
                ]
        except FileNotFoundError:
            self._accounts = []
        except Exception as e:
            # 坏文件 / 其他用户的 DPAPI 密文：不静默清空覆盖，先报出来。
            self._accounts = []
            self._log(f"[cmdgo] 账号清单读取失败（保持空池，不会覆盖原文件）：{e}")

    def _persist(self) -> None:
        payload = json.dumps({"version": 1, "accounts": [a.to_dict() for a in self._accounts]},
                             ensure_ascii=False, indent=2)
        try:
            credstore.write_credential(self.file, payload.encode("utf-8"))
        except Exception as e:
            self._log(f"[cmdgo] 账号清单写入失败（不影响本次会话）：{e}")

    # ---- 查询 ----
    def list(self) -> list[PoolAccount]:
        with self._lock:
            self._ensure_loaded()
            return list(self._accounts)

    @property
    def size(self) -> int:
        with self._lock:
            self._ensure_loaded()
            return len(self._accounts)

    def active_count(self, now: Optional[int] = None) -> int:
        with self._lock:
            self._ensure_loaded()
            now = now if now is not None else int(time.time() * 1000)
            return sum(1 for a in self._accounts if a.enabled and (a.cooldownUntil or 0) <= now)

    def _find(self, account_id: str) -> Optional[PoolAccount]:
        for a in self._accounts:
            if a.id == account_id:
                return a
        return None

    def find_by_key(self, apiKey: str) -> Optional[PoolAccount]:
        with self._lock:
            self._ensure_loaded()
            for a in self._accounts:
                if a.apiKey == apiKey:
                    return a
            return None

    # ---- 收编旧单 key ----
    def adopt_legacy(self) -> None:
        """把旧单 key（若还存在）收编为账号池 #1，升级无缝。

        本项目旧 key 存于 token.json；若池为空且该文件有 key，则以 fixed key 收编。
        guarded by 池为空 + 文件存在且有值。
        """
        with self._lock:
            self._ensure_loaded()
            if self._accounts:
                return
            try:
                parsed = json.loads(credstore.read_credential(os.path.join(_data_dir(), "token.json")))
                legacy = parsed.get("apiKey")
                if isinstance(legacy, str) and legacy:
                    self._accounts.append(PoolAccount(id="default", apiKey=legacy, addedAt=int(time.time() * 1000)))
                    self._persist()
                    self._log("[cmdgo] 已将既有凭据收编为账号池 #1（default）")
            except FileNotFoundError:
                return
            except Exception:
                return

    # ---- 增删 ----
    def add(self, info: dict) -> PoolAccount:
        """新增一个账号入池；返回创建的记录。"""
        with self._lock:
            self._ensure_loaded()
            api_key = info.get("apiKey", "")
            user = info.get("userName")
            key_name = info.get("keyName")
            ident = _slug(user or key_name, "acct")
            taken = {a.id for a in self._accounts}
            if ident in taken or ident == "default":
                ident = f"{ident}-{random.randbytes(2).hex()}"
            while ident in taken:
                ident = f"{ident}{random.randbytes(1).hex()}"
            account = PoolAccount(id=ident, apiKey=api_key, userName=user,
                                  keyName=key_name, addedAt=int(time.time() * 1000))
            self._accounts.append(account)
            self._persist()
            self._log(f"[cmdgo] 账号入池：{ident}（{user or '?'} · {key_name or '?'}），池大小 {len(self._accounts)}")
            return account

    # ---- 轮询调度 ----
    def pick(self, now: Optional[int] = None) -> Optional[PoolAccount]:
        """轮询选一个可用账号；若无可用则返回冷却最早到期的启用账号。"""
        with self._lock:
            now = now if now is not None else int(time.time() * 1000)
            usable = [a for a in self._accounts if a.enabled and (a.cooldownUntil or 0) <= now]
            if usable:
                picked = usable[self._cursor % len(usable)]
                self._cursor = (self._cursor + 1) % len(usable)
                picked.lastUsedAt = now
                return picked
            enabled = [a for a in self._accounts if a.enabled]
            if not enabled:
                return None
            return min(enabled, key=lambda a: (a.cooldownUntil or 0))

    # ---- 记账 ----
    def report_failure(self, account: PoolAccount, message: str, now: Optional[int] = None) -> None:
        with self._lock:
            now = now if now is not None else int(time.time() * 1000)
            # 距上次失败超过 FAIL_DECAY_MS 视为已恢复，冷却从基础值重新计。
            if account.lastFailAt and now - account.lastFailAt > FAIL_DECAY_MS:
                account.failCount = 0
            account.failCount += 1
            account.errCount += 1
            ms = min(COOLDOWN_MAX_MS, COOLDOWN_BASE_MS * 2 ** (account.failCount - 1))
            account.cooldownUntil = now + ms
            account.lastError = (message or "")[:200]
            account.lastFailAt = now
            self._persist()
            self._log(f"[cmdgo] 账号 {account.id} 请求失败（第 {account.failCount} 次），"
                      f"冷却 {round(ms / 1000)}s：{account.lastError}")

    def report_success(self, account: PoolAccount, usage: Optional[dict] = None) -> None:
        with self._lock:
            account.okCount += 1
            if isinstance(usage, dict):
                account.tokensIn += int(usage.get("prompt_tokens", 0) or 0)
                account.tokensOut += int(usage.get("completion_tokens", 0) or 0)
            if account.failCount == 0 and account.cooldownUntil is None and account.lastError is None:
                self._persist()
                return
            account.failCount = 0
            account.cooldownUntil = None
            account.lastError = None
            account.lastFailAt = None
            self._persist()

    # ---- 管理 ----
    def toggle(self, account_id: str, enabled: bool) -> bool:
        with self._lock:
            self._ensure_loaded()
            account = self._find(account_id)
            if account is None or account.enabled == enabled:
                return False
            account.enabled = enabled
            if not enabled:
                account.cooldownUntil = None
            self._persist()
            return True

    def remove(self, account_id: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            for i, a in enumerate(self._accounts):
                if a.id == account_id:
                    self._accounts.pop(i)
                    if self._cursor >= max(1, len(self._accounts)):
                        self._cursor = 0
                    self._persist()
                    return True
            return False

    def clear(self) -> int:
        with self._lock:
            self._ensure_loaded()
            count = len(self._accounts)
            for a in list(self._accounts):
                self.remove(a.id)
            return count

    # ---- 导出/导入（跨机器迁移：口令加密信封，见 credstore.py） ----
    def export_accounts(self, passphrase: str) -> bytes:
        """把池内账号凭据导出为口令加密信封（只含 apiKey/用户名/密钥名，不含统计）。"""
        with self._lock:
            self._ensure_loaded()
            if not self._accounts:
                raise ValueError("账号池为空，没有可导出的账号")
            rows = [{"apiKey": a.apiKey, "userName": a.userName, "keyName": a.keyName}
                    for a in self._accounts]
        payload = json.dumps({"accounts": rows}, ensure_ascii=False).encode("utf-8")
        return credstore.encrypt_with_passphrase(payload, passphrase)

    def import_accounts(self, payload: bytes, passphrase: str) -> int:
        """从导出信封导入账号；已在池中的 key 跳过。返回新导入数量。"""
        data = credstore.decrypt_with_passphrase(payload, passphrase)
        rows = json.loads(data).get("accounts")
        if not isinstance(rows, list) or not rows:
            raise ValueError("导出文件里没有账号")
        added = 0
        with self._lock:
            self._ensure_loaded()
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("apiKey"), str) or not row["apiKey"]:
                    continue
                if self.find_by_key(row["apiKey"]) is not None:
                    continue
                self.add({"apiKey": row["apiKey"], "userName": row.get("userName"),
                          "keyName": row.get("keyName")})
                added += 1
        return added

    # ---- 状态快照（绝不带明文 key 给查看接口） ----
    def describe_all(self) -> list[dict]:
        now = int(time.time() * 1000)
        out = []
        for a in self._accounts:
            row = {
                "id": a.id,
                "userName": a.userName,
                "keyName": a.keyName,
                "displayName": a.display_name,
                "addedAt": a.addedAt,
                "enabled": a.enabled,
                "failCount": a.failCount,
                "cooling": bool(a.cooldownUntil and a.cooldownUntil > now),
                "lastError": a.lastError,
                "lastUsedAt": a.lastUsedAt,
                "okCount": a.okCount,
                "errCount": a.errCount,
                "tokensIn": a.tokensIn,
                "tokensOut": a.tokensOut,
            }
            out.append(row)
        return out
