"""config 纯逻辑测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
_WORKER_DIR = str(Path(__file__).resolve().parent.parent)
for _p in (_TESTS_DIR, _WORKER_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from depin_worker.config import (  # noqa: E402
    ConfigError,
    SamplerSettings,
    WorkerConfig,
    config_from_dict,
    load_config,
)


class TestSamplerSettings(unittest.TestCase):
    def test_defaults(self):
        s = SamplerSettings()
        self.assertEqual(s.interval_seconds, 1.0)
        self.assertEqual(s.device_index, 0)
        self.assertTrue(s.include_processes)

    def test_validation(self):
        with self.assertRaises(ConfigError):
            SamplerSettings(interval_seconds=0)
        with self.assertRaises(ConfigError):
            SamplerSettings(interval_seconds=float("nan"))
        with self.assertRaises(ConfigError):
            SamplerSettings(device_index=-1)
        with self.assertRaises(ConfigError):
            SamplerSettings(interval_seconds=True)  # bool 不是合法数字

    def test_valid(self):
        s = SamplerSettings(interval_seconds=0.1, device_index=2, output_path=Path("/tmp/a.jsonl"))
        self.assertEqual(s.interval_seconds, 0.1)
        self.assertEqual(s.device_index, 2)


class TestWorkerConfig(unittest.TestCase):
    def test_roundtrip(self):
        cfg = WorkerConfig(
            sampler=SamplerSettings(interval_seconds=0.5),
            worker_id="worker-1",
        )
        data = cfg.to_dict()
        self.assertEqual(data["sampler"]["output_path"], "nvml_samples.jsonl")
        restored = config_from_dict(json.loads(json.dumps(data)))
        self.assertEqual(restored, cfg)

    def test_heartbeat_placeholder_validation(self):
        with self.assertRaises(ConfigError):
            WorkerConfig(heartbeat_interval_seconds=0)
        WorkerConfig(heartbeat_interval_seconds=30.0)  # 合法，但 stage-1 无消费

    def test_unknown_keys_rejected(self):
        with self.assertRaises(ConfigError):
            config_from_dict({"unexpected": 1})
        with self.assertRaises(ConfigError):
            config_from_dict({"sampler": {"bogus": 2}})

    def test_load_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            path.write_text(
                json.dumps(
                    {
                        "worker_id": "w1",
                        "sampler": {"interval_seconds": 2, "output_path": "x.jsonl"},
                    }
                ),
                encoding="utf-8",
            )
            cfg = load_config(path)
            self.assertEqual(cfg.worker_id, "w1")
            self.assertEqual(cfg.sampler.interval_seconds, 2)
            self.assertEqual(cfg.sampler.output_path, Path("x.jsonl"))

    def test_load_config_bad_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)
            with self.assertRaises(ConfigError):
                load_config(Path(tmp) / "missing.json")


if __name__ == "__main__":
    unittest.main()
