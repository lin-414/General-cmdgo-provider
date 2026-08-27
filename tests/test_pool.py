#!/usr/bin/env python3
"""AccountPool 单元测试（纯本地，不联网）。"""
import os
import sys
import tempfile
import unittest

# 使项目根目录可导入 pool
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pool


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
