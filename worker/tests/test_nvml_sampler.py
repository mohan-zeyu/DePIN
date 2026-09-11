"""nvml_sampler 纯逻辑测试：fake NVML + 短间隔真实线程，不依赖 GPU。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
_WORKER_DIR = str(Path(__file__).resolve().parent.parent)
for _p in (_TESTS_DIR, _WORKER_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from depin_worker.nvml_sampler import (  # noqa: E402
    NvmlSampler,
    NvmlSamplerError,
    load_header,
    load_samples,
    sample_is_idle,
)
import fakes  # noqa: E402
from fakes import FakeDeviceSpec, FakeNvml  # noqa: E402


def _mutable_nvml(util=(0, 0), processes=None):
    """带锁的可变 fake：测试线程中途改字段，验证采样器实时反映。"""
    spec = FakeDeviceSpec(util=util, processes=list(processes or []))
    nvml = FakeNvml(devices=[spec])
    lock = threading.Lock()
    return nvml, spec, lock


class TestValidation(unittest.TestCase):
    def test_bad_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in (0, -1, float("nan"), float("inf"), "1"):
                with self.assertRaises(ValueError):
                    NvmlSampler(Path(tmp) / "x.jsonl", interval_seconds=bad)

    def test_bad_device_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                NvmlSampler(Path(tmp) / "x.jsonl", device_index=-1)

    def test_double_start_rejected(self):
        nvml, _, _ = _mutable_nvml()
        with tempfile.TemporaryDirectory() as tmp:
            sampler = NvmlSampler(Path(tmp) / "x.jsonl", interval_seconds=0.05, nvml_module=nvml)
            with sampler:
                with self.assertRaises(NvmlSamplerError):
                    sampler.start()
            # stop 后允许重新 start
            with sampler:
                pass


class TestSampling(unittest.TestCase):
    def test_produces_header_and_samples(self):
        nvml, _, _ = _mutable_nvml(util=(73, 12), processes=[{"pid": 1, "used_memory_bytes": 10}])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            sampler = NvmlSampler(path, interval_seconds=0.02, nvml_module=nvml)
            with sampler:
                time.sleep(0.25)
            stats = sampler.stats
            header = load_header(path)
            samples = load_samples(path)

            self.assertEqual(header["record"], "header")
            self.assertEqual(header["device"]["name"], "NVIDIA GeForce RTX 4060 Laptop GPU")
            self.assertEqual(header["interval_seconds"], 0.02)
            self.assertGreaterEqual(len(samples), 3)
            self.assertGreaterEqual(stats["samples_written"], 3)
            # seq 严格递增
            seqs = [s["seq"] for s in samples]
            self.assertEqual(seqs, sorted(seqs))
            self.assertEqual(len(set(seqs)), len(seqs))
            for sample in samples:
                self.assertEqual(sample["record"], "sample")
                self.assertEqual(sample["device"]["gpu_util_percent"], 73)
                self.assertEqual(sample["device"]["mem_util_percent"], 12)
                self.assertEqual(sample["device"]["memory_total_bytes"], 8589934592)
                self.assertEqual(sample["processes"], [{"pid": 1, "used_memory_bytes": 10, "type": "compute"}])
            # ts 单调不减
            tss = [s["ts"] for s in samples]
            self.assertEqual(tss, sorted(tss))

    def test_unsupported_fields_recorded_as_null(self):
        nvml = fakes.make_wsl_like_nvml()  # power/clocks/per-process 不可用
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            with NvmlSampler(path, interval_seconds=0.02, nvml_module=nvml) as sampler:
                time.sleep(0.15)
            samples = load_samples(path)
            self.assertGreaterEqual(len(samples), 2)
            for sample in samples:
                self.assertIsNone(sample["device"]["power_draw_w"])
                self.assertIsNone(sample["device"]["clocks_sm_mhz"])
                self.assertIsNone(sample["processes"])
                errors = sample["field_errors"]
                self.assertIn("power_draw_w", errors)
                self.assertIn("NVMLError_NotSupported", errors["power_draw_w"])
            stats = sampler.stats
            self.assertGreaterEqual(stats["field_error_counts"]["power_draw_w"], 1)

    def test_no_process_field_when_disabled(self):
        nvml = fakes.make_full_nvml()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            with NvmlSampler(path, interval_seconds=0.02, nvml_module=nvml, include_processes=False):
                time.sleep(0.12)
            for sample in load_samples(path):
                # include_processes=False 时该键应为 null 且无相关错误
                self.assertIsNone(sample.get("processes"))
                self.assertNotIn("processes", sample.get("field_errors") or {})

    def test_live_changes_visible(self):
        nvml, spec, _ = _mutable_nvml(util=(0, 0), processes=[])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            sampler = NvmlSampler(path, interval_seconds=0.02, nvml_module=nvml)
            with sampler:
                time.sleep(0.1)
                spec.util = (99, 88)
                spec.processes = [{"pid": 777, "used_memory_bytes": 5}]
                time.sleep(0.15)
            samples = load_samples(path)
            utils = [s["device"]["gpu_util_percent"] for s in samples]
            self.assertIn(0, utils)
            self.assertIn(99, utils)
            pids = [p["pid"] for s in samples for p in (s.get("processes") or [])]
            self.assertIn(777, pids)

    def test_stop_is_prompt_and_idempotent(self):
        nvml, _, _ = _mutable_nvml()
        with tempfile.TemporaryDirectory() as tmp:
            sampler = NvmlSampler(Path(tmp) / "s.jsonl", interval_seconds=5.0, nvml_module=nvml)
            sampler.start()
            time.sleep(0.05)
            t0 = time.monotonic()
            sampler.stop(timeout=5.0)
            elapsed = time.monotonic() - t0
            # 5 秒周期下 stop 必须立刻打断（用 event.wait 而不是 sleep）
            self.assertLess(elapsed, 1.5)
            sampler.stop()  # 幂等
            self.assertTrue(nvml.shutdown_called)

    def test_init_failure_raises_clearly(self):
        nvml = fakes.make_full_nvml()
        nvml.init_error = fakes.NVMLError_Uninitialized("driver not loaded")
        with tempfile.TemporaryDirectory() as tmp:
            sampler = NvmlSampler(Path(tmp) / "s.jsonl", interval_seconds=0.05, nvml_module=nvml)
            with self.assertRaises(NvmlSamplerError) as ctx:
                sampler.start()
            self.assertIn("NVML 初始化失败", str(ctx.exception))

    def test_import_failure_raises_clearly(self):
        class BrokenLoader:
            def find_module(self, name, path=None):
                return self if name == "pynvml" else None

            def load_module(self, name):
                raise ImportError("no pynvml here")

        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pynvml":
                raise ImportError("no pynvml here")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = fake_import
        try:
            with tempfile.TemporaryDirectory() as tmp:
                sampler = NvmlSampler(Path(tmp) / "s.jsonl", interval_seconds=0.05)
                with self.assertRaises(NvmlSamplerError) as ctx:
                    sampler.start()
                self.assertIn("pynvml", str(ctx.exception))
        finally:
            builtins.__import__ = real_import


class TestSampleIsIdle(unittest.TestCase):
    def test_process_based(self):
        self.assertTrue(sample_is_idle({"processes": []}))
        self.assertFalse(sample_is_idle({"processes": [{"pid": 1}]}))

    def test_util_fallback(self):
        self.assertTrue(sample_is_idle({"device": {"gpu_util_percent": 0}}))
        self.assertFalse(sample_is_idle({"device": {"gpu_util_percent": 17}}))

    def test_unknowable(self):
        self.assertIsNone(sample_is_idle({"processes": None, "device": {"gpu_util_percent": None}}))
        self.assertIsNone(sample_is_idle(None))
        self.assertIsNone(sample_is_idle({"device": {}}))


class TestJsonlIntegrity(unittest.TestCase):
    def test_every_line_is_json(self):
        nvml, spec, _ = _mutable_nvml(util=(5, 5), processes=[])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            with NvmlSampler(path, interval_seconds=0.02, nvml_module=nvml):
                time.sleep(0.15)
            lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
            self.assertGreaterEqual(len(lines), 2)
            for line in lines:
                obj = json.loads(line)  # 每行都必须是合法 JSON
                self.assertIn("record", obj)


if __name__ == "__main__":
    unittest.main()
