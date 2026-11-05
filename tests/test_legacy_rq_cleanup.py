"""Conservative classification and atomic archival of legacy queue entries."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock

spec = importlib.util.spec_from_file_location(
    "legacy_cleanup", Path(__file__).resolve().parents[1] / "deployment/archive_legacy_rq_entries.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class LegacyCleanupTests(unittest.TestCase):
    def redis(self):
        redis = MagicMock()
        redis.scan_iter.return_value = [b"rq:wip:default"]
        redis.zrangebyscore.return_value = [
            b"site::legacy", b"site::modern:execution-id", b"site::custom:ambiguous",
            b"other::legacy", b"site::", b"site::missing"]
        redis.type.side_effect = lambda key: b"none" if key.endswith(b"missing") else b"hash"
        redis.eval.return_value = 1
        return redis

    def test_dry_run_skips_modern_ambiguous_foreign_and_missing_jobs(self):
        redis = self.redis()
        self.assertEqual({"candidates": 1, "archived": 0},
                         cleanup.archive_candidates(redis, "site", cutoff=100))
        redis.eval.assert_not_called()

    def test_apply_uses_actual_job_key_and_preserves_modern_entries(self):
        redis = self.redis()
        self.assertEqual({"candidates": 1, "archived": 1},
                         cleanup.archive_candidates(redis, "site", apply=True, cutoff=100))
        redis.eval.assert_called_once_with(cleanup.ARCHIVE, 3, b"rq:wip:default",
            b"frappe:v16-upgrade:legacy-started:rq:wip:default", b"rq:job:site::legacy", b"site::legacy", 100)

    def test_registered_worker_refuses_archival(self):
        redis = self.redis()
        redis.eval.return_value = -1
        with self.assertRaisesRegex(RuntimeError, "registered worker"):
            cleanup.archive_candidates(redis, "site", apply=True, cutoff=100)


if __name__ == "__main__":
    unittest.main()
