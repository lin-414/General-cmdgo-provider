#!/usr/bin/env python3
"""AccountPool 单元测试（纯本地，不联网）。"""
import os
import sys
import tempfile
import unittest

# 使项目根目录可导入 pool
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pool
import credstore


class AccountPoolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cmdgo-pool-test-")
        self.file = os.path.join(self.tmp, "accounts.json")
        self.logs = []
        self.p = pool.AccountPool("cmdgo", log=lambda m: self.logs.append(m), file=self.file)

    def _add(self, api_key, user=None, key=None):
        return self.p.add({"apiKey": api_key, "userName": user, "keyName": key})

    def test_add_and_size(self):
        self._add("k1", "alice")
        self._add("k2", "bob")
        self.assertEqual(self.p.size, 2)
        self.assertTrue(os.path.exists(self.file))

    def test_round_robin_pick(self):
        self._add("k1", "alice")
        self._add("k2", "bob")
        self._add("k3", "carol")
        ids = [self.p.pick().id for _ in range(3)]
        self.assertEqual(len(set(ids)), 3)

    def test_cooldown_skip_and_failover(self):
        a = self._add("k1", "alice")
        self._add("k2", "bob")
        self.p.report_failure(a, "upstream 401")
        # alice cooling -> next pick should be bob
        self.assertEqual(self.p.pick().id, "bob")

    def test_success_resets_cooldown(self):
        a = self._add("k1", "alice")
        self.p.report_failure(a, "upstream 401")
        self.p.report_failure(a, "upstream 401")
        self.assertGreater(a.failCount, 0)
        self.p.report_success(a)
        self.assertEqual(a.failCount, 0)
        self.assertIsNone(a.cooldownUntil)

    def test_toggle_and_remove(self):
        a = self._add("k1", "alice")
        b = self._add("k2", "bob")
        self.assertTrue(self.p.toggle(b.id, False))
        self.assertEqual(self.p.active_count(), 1)  # only alice active
        self.assertTrue(self.p.remove(a.id))
        self.assertEqual(self.p.size, 1)

    def test_persistence_round_trip(self):
        self._add("k1", "alice")
        self._add("k2", "bob")
        p2 = pool.AccountPool("cmdgo", log=lambda m: None, file=self.file)
        self.assertEqual(p2.size, 2)
        self.assertEqual({a.id for a in p2.list()}, {"alice", "bob"})

    def test_find_by_key(self):
        a = self._add("k1", "alice")
        found = self.p.find_by_key("k1")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, a.id)
        self.assertIsNone(self.p.find_by_key("nope"))

    def test_clear_empties_pool(self):
        self._add("k1", "alice")
        self._add("k2", "bob")
        self.assertEqual(self.p.clear(), 2)
        self.assertEqual(self.p.size, 0)

    def test_describe_all_hides_key(self):
        self._add("k1", "alice")
        rows = self.p.describe_all()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("apiKey", rows[0])
        self.assertIn("displayName", rows[0])

    def test_display_name_prefers_username(self):
        a = self._add("k1", "alice", "work-key")
        self.assertEqual(a.display_name, "alice")

    def test_display_name_falls_back_for_default(self):
        # 模拟 adopt_legacy 产生的 default 账号
        acc = pool.PoolAccount(id="default", apiKey="legacy")
        self.assertEqual(acc.display_name, "默认账号（旧 key）")

    def test_pick_returns_none_when_empty(self):
        self.assertIsNone(self.p.pick())

    def test_usage_stats_accumulate(self):
        a = self._add("k1", "alice")
        self.p.report_success(a, usage={"prompt_tokens": 100, "completion_tokens": 30})
        self.p.report_success(a, usage={"prompt_tokens": 10, "completion_tokens": 5})
        self.p.report_failure(a, "upstream 429")
        self.assertEqual(a.okCount, 2)
        self.assertEqual(a.errCount, 1)
        self.assertEqual(a.tokensIn, 110)
        self.assertEqual(a.tokensOut, 35)
        # 成功同时清掉冷却状态
        self.assertEqual(a.failCount, 1)  # 刚失败过一次
        self.p.report_success(a)
        self.assertEqual(a.failCount, 0)
        self.assertIsNone(a.cooldownUntil)

    def test_usage_stats_persist_and_describe(self):
        a = self._add("k1", "alice")
        self.p.report_success(a, usage={"prompt_tokens": 1234, "completion_tokens": 567})
        p2 = pool.AccountPool("cmdgo", log=lambda m: None, file=self.file)
        b = p2.list()[0]
        self.assertEqual(b.okCount, 1)
        self.assertEqual(b.tokensIn, 1234)
        row = p2.describe_all()[0]
        self.assertEqual(row["okCount"], 1)
        self.assertEqual(row["errCount"], 0)
        self.assertEqual(row["tokensOut"], 567)

    def test_adopt_legacy(self):
        # 模拟旧 token.json 存在
        data_dir = os.path.join(self.tmp, "data")
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "token.json"), "w", encoding="utf-8") as f:
            f.write('{"apiKey": "legacy-key"}')
        # 临时把 pool 的数据目录指向该目录（通过 monkeypatch 模块级 _data_dir）
        alt = pool.AccountPool("cmdgo", log=lambda m: None, file=self.file)
        orig_data_dir = pool._data_dir
        pool._data_dir = lambda: data_dir
        try:
            alt.adopt_legacy()
        finally:
            pool._data_dir = orig_data_dir
        self.assertEqual(alt.size, 1)
        self.assertEqual(alt.list()[0].id, "default")


class CredentialStoreTest(unittest.TestCase):
    """DPAPI 凭据加密：Windows 走真实 DPAPI（同用户可解），其他平台明文回退。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cmdgo-cred-test-")

    def test_roundtrip(self):
        data = b'{"apiKey": "secret-key-xyz"}'
        enc = credstore.protect(data)
        if os.name == "nt":
            self.assertTrue(enc.startswith(credstore.MAGIC))
        self.assertEqual(credstore.unprotect(enc), data)

    def test_plaintext_passthrough(self):
        # 无 MAGIC 前缀 = 旧版明文文件，读取时原样返回
        data = b'{"apiKey": "legacy"}'
        self.assertEqual(credstore.unprotect(data), data)

    def test_write_read_credential(self):
        path = os.path.join(self.tmp, "cred.json")
        credstore.write_credential(path, b'{"apiKey": "abc"}')
        with open(path, "rb") as f:
            raw = f.read()
        if os.name == "nt":
            self.assertTrue(raw.startswith(credstore.MAGIC))
        self.assertEqual(credstore.read_credential(path), b'{"apiKey": "abc"}')


class PoolEncryptionTest(unittest.TestCase):
    """账号清单落盘应加密（Windows），且读取端无感。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cmdgo-pool-enc-test-")
        self.file = os.path.join(self.tmp, "accounts.json")

    def test_manifest_encrypted_at_rest(self):
        p = pool.AccountPool("cmdgo", log=lambda m: None, file=self.file)
        p.add({"apiKey": "k1-secret", "userName": "alice"})
        with open(self.file, "rb") as f:
            raw = f.read()
        if os.name == "nt":
            self.assertTrue(raw.startswith(credstore.MAGIC))
            self.assertNotIn(b"k1-secret", raw)  # 明文 key 不落在磁盘上
        # 读取端走同一套解密逻辑，对调用方透明
        p2 = pool.AccountPool("cmdgo", log=lambda m: None, file=self.file)
        self.assertEqual(p2.size, 1)
        self.assertEqual(p2.list()[0].apiKey, "k1-secret")


try:
    import cryptography  # noqa: F401
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


@unittest.skipUnless(_HAS_CRYPTO, "cryptography not installed")
class PassphraseExportTest(unittest.TestCase):
    """口令加密 + 账号导出/导入回环（跨机器迁移路径）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cmdgo-export-test-")

    def test_passphrase_roundtrip(self):
        data = b'{"accounts": [{"apiKey": "k1-secret"}]}'
        enc = credstore.encrypt_with_passphrase(data, "pass-phrase-1")
        self.assertNotIn(b"k1-secret", enc)  # 密文不含明文 key
        self.assertEqual(credstore.decrypt_with_passphrase(enc, "pass-phrase-1"), data)
        with self.assertRaises(ValueError):
            credstore.decrypt_with_passphrase(enc, "wrong-passphrase")
        with self.assertRaises(ValueError):
            credstore.decrypt_with_passphrase(b"not-an-envelope", "pass-phrase-1")

    def test_pool_export_import_roundtrip(self):
        p1 = pool.AccountPool("cmdgo", log=lambda m: None, file=os.path.join(self.tmp, "a.json"))
        p1.add({"apiKey": "k1", "userName": "alice"})
        p1.add({"apiKey": "k2", "userName": "bob"})
        blob = p1.export_accounts("pw123")
        # 导入到另一个池：k2 已存在跳过，k1 新增
        p2 = pool.AccountPool("cmdgo", log=lambda m: None, file=os.path.join(self.tmp, "b.json"))
        p2.add({"apiKey": "k2", "userName": "bob"})
        added = p2.import_accounts(blob, "pw123")
        self.assertEqual(added, 1)
        self.assertEqual(p2.size, 2)
        self.assertEqual(p2.find_by_key("k1").userName, "alice")
        with self.assertRaises(ValueError):
            p2.import_accounts(blob, "bad-pw")
        # 空池导出应报错
        p3 = pool.AccountPool("cmdgo", log=lambda m: None, file=os.path.join(self.tmp, "c.json"))
        with self.assertRaises(ValueError):
            p3.export_accounts("pw")


if __name__ == "__main__":
    unittest.main(verbosity=2)
