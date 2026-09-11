"""gpu_probe 纯逻辑测试：路径发现、WSL 判断、JSON schema、字段可用性记录。

全部用 fake NVML / 注入 runner，不依赖真 GPU、pynvml 或 nvidia-smi。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
_WORKER_DIR = str(Path(__file__).resolve().parent.parent)
for _p in (_TESTS_DIR, _WORKER_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from depin_worker import gpu_probe  # noqa: E402
from fakes import make_full_nvml, make_wsl_like_nvml  # noqa: E402


class TestFindNvidiaSmi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        self.smi = self.tmpdir / "nvidia-smi"
        self.smi.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(self.smi, 0o755)

    def test_found_via_injected_path(self):
        found = gpu_probe.find_nvidia_smi(env={"PATH": ""}, extra_candidates=[str(self.smi)])
        self.assertEqual(found, str(self.smi))

    def test_found_via_env_path(self):
        env = {"PATH": str(self.tmpdir)}
        self.assertEqual(gpu_probe.find_nvidia_smi(env=env), str(self.smi))

    def test_env_path_beats_known_locations(self):
        # PATH 命中优先于固定位置兜底
        env = {"PATH": str(self.tmpdir)}
        found = gpu_probe.find_nvidia_smi(env=env, extra_candidates=["/nonexistent/nvidia-smi"])
        self.assertEqual(found, str(self.smi))

    def test_missing_everywhere_returns_none(self):
        env = {"PATH": "/nonexistent"}
        found = gpu_probe.find_nvidia_smi(env=env, extra_candidates=["/also/nonexistent"])
        self.assertIsNone(found)

    def test_non_executable_file_is_rejected(self):
        plain = self.tmpdir / "not-exec"
        plain.write_text("data")
        os.chmod(plain, 0o644)
        env = {"PATH": str(self.tmpdir)}
        # PATH 里唯一的 nvidia-smi 是可执行的这个；not-exec 不叫 nvidia-smi，不影响
        self.assertEqual(gpu_probe.find_nvidia_smi(env=env), str(self.smi))
        self.assertIsNone(
            gpu_probe.find_nvidia_smi(env={"PATH": ""}, extra_candidates=[str(plain)])
        )

    def test_expands_user_prefix(self):
        with tempfile.TemporaryDirectory() as home:
            bin_dir = Path(home) / "bin"
            bin_dir.mkdir()
            fake = bin_dir / "nvidia-smi"
            fake.write_text("#!/bin/sh\n")
            os.chmod(fake, 0o755)
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                found = gpu_probe.find_nvidia_smi(
                    env={"PATH": ""},
                    extra_candidates=["~/bin/nvidia-smi"],
                )
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
        self.assertEqual(found, str(fake))


class TestWslDetection(unittest.TestCase):
    def test_proc_version_microsoft(self):
        is_wsl, evidence = gpu_probe.evaluate_wsl_signals(
            "Linux version 6.6.87.2-microsoft-standard-WSL2 (gcc ... ) #1 SMP",
            {},
            [],
        )
        self.assertTrue(is_wsl)
        self.assertTrue(any("/proc/version" in item for item in evidence))

    def test_proc_version_plain_linux(self):
        is_wsl, evidence = gpu_probe.evaluate_wsl_signals(
            "Linux version 6.8.0-generic (gcc ...)",
            {},
            [],
        )
        self.assertFalse(is_wsl)
        self.assertEqual(evidence, [])

    def test_wsl_distro_env_var(self):
        is_wsl, _ = gpu_probe.evaluate_wsl_signals(None, {"WSL_DISTRO_NAME": "Ubuntu"}, [])
        self.assertTrue(is_wsl)

    def test_wsl_lib_dir(self):
        is_wsl, evidence = gpu_probe.evaluate_wsl_signals(None, {}, ["/usr/lib/wsl/lib"])
        self.assertTrue(is_wsl)
        self.assertTrue(any("/usr/lib/wsl/lib" in item for item in evidence))

    def test_mount_c_windows(self):
        is_wsl, _ = gpu_probe.evaluate_wsl_signals(None, {}, ["/mnt/c/Windows"])
        self.assertTrue(is_wsl)

    def test_no_signals(self):
        is_wsl, evidence = gpu_probe.evaluate_wsl_signals(None, {}, [])
        self.assertFalse(is_wsl)
        self.assertEqual(evidence, [])

    def test_real_machine_probe_does_not_crash(self):
        # 在测试机（macOS）上调用真实 is_wsl 也不应崩溃
        is_wsl, evidence = gpu_probe.is_wsl()
        self.assertIsInstance(is_wsl, bool)
        self.assertIsInstance(evidence, list)


class TestProbeJsonSchema(unittest.TestCase):
    REQUIRED_TOP = {
        "schema_version",
        "probed_at",
        "python_version",
        "platform",
        "wsl",
        "nvidia_smi",
        "nvml",
        "nvidia_smi_probe",
    }
    REQUIRED_DEVICE_FIELDS = {
        "index",
        "name",
        "uuid",
        "memory_total_bytes",
        "compute_capability",
        "temperature_c",
        "power_draw_w",
        "utilization",
        "per_process",
        "unavailable_fields",
    }

    def _check_common_schema(self, report):
        self.assertTrue(self.REQUIRED_TOP.issubset(report.keys()))
        self.assertIn("is_wsl", report["wsl"])
        self.assertIn("evidence", report["wsl"])
        self.assertIn("found", report["nvidia_smi"])
        for key in ("library_available", "init", "device_count", "devices"):
            self.assertIn(key, report["nvml"])

    def test_full_nvml_report(self):
        nvml = make_full_nvml()
        report = gpu_probe.probe_gpu(nvml_module=nvml, env={"PATH": ""})
        self._check_common_schema(report)
        self.assertTrue(report["nvml"]["library_available"])
        self.assertEqual(report["nvml"]["init"]["status"], "ok")
        self.assertEqual(report["nvml"]["device_count"], 1)
        device = report["nvml"]["devices"][0]
        for field in self.REQUIRED_DEVICE_FIELDS:
            self.assertIn(field, device, f"缺少设备字段 {field}")
        self.assertEqual(device["name"], {"status": "ok", "value": "NVIDIA GeForce RTX 4060 Laptop GPU"})
        self.assertEqual(device["uuid"]["value"], nvml.devices[0].uuid)
        self.assertEqual(device["compute_capability"], {"status": "ok", "value": "8.9"})
        self.assertEqual(device["memory_total_bytes"], {"status": "ok", "value": 8589934592})
        self.assertEqual(
            device["per_process"],
            {"status": "ok", "value": [{"pid": 4242, "used_memory_bytes": 512 * 1024 * 1024}]},
        )
        self.assertEqual(device["unavailable_fields"], [])
        # shutdown 被调用
        self.assertTrue(nvml.shutdown_called)

    def test_wsl_like_report_records_unavailable_fields(self):
        nvml = make_wsl_like_nvml()
        report = gpu_probe.probe_gpu(nvml_module=nvml, env={"PATH": ""})
        device = report["nvml"]["devices"][0]
        self.assertEqual(device["temperature_c"], {"status": "ok", "value": 51.0})
        for field in ("power_draw_w", "power_limit_w", "clocks_sm_mhz", "per_process"):
            entry = device[field]
            self.assertEqual(entry["status"], "unavailable", f"{field} 应记录不可用")
            self.assertIn("NVMLError_NotSupported", entry["error"])
            self.assertNotIn("value", entry)
        self.assertIn("per_process", device["unavailable_fields"])
        self.assertIn("power_draw_w", device["unavailable_fields"])

    def test_bytes_name_is_decoded(self):
        nvml = make_full_nvml()
        nvml.name_returns_bytes = True
        report = gpu_probe.probe_gpu(nvml_module=nvml, env={"PATH": ""})
        self.assertEqual(report["nvml"]["devices"][0]["name"]["status"], "ok")
        self.assertIsInstance(report["nvml"]["devices"][0]["name"]["value"], str)

    def test_nvml_init_failure_recorded_not_raised(self):
        from fakes import NVMLError_NotSupported

        nvml = make_full_nvml()
        nvml.init_error = NVMLError_NotSupported("driver not loaded")
        report = gpu_probe.probe_gpu(nvml_module=nvml, env={"PATH": ""})
        self.assertEqual(report["nvml"]["init"]["status"], "unavailable")
        self.assertIn("driver not loaded", report["nvml"]["init"]["error"])
        self.assertEqual(report["nvml"]["devices"], [])

    def test_two_devices(self):
        from fakes import FakeDeviceSpec, FakeNvml

        nvml = FakeNvml(
            devices=[
                FakeDeviceSpec(uuid="GPU-aaa"),
                FakeDeviceSpec(uuid="GPU-bbb"),
            ]
        )
        report = gpu_probe.probe_gpu(nvml_module=nvml, env={"PATH": ""})
        self.assertEqual(report["nvml"]["device_count"], 2)
        self.assertEqual([d["uuid"]["value"] for d in report["nvml"]["devices"]], ["GPU-aaa", "GPU-bbb"])


class TestSmiCrossCheck(unittest.TestCase):
    def test_runner_injection_and_parse(self):
        def fake_runner(path, args):
            assert "--query-gpu" in args[0]
            return (
                0,
                "NVIDIA GeForce RTX 4060 Laptop GPU, GPU-abc-123, 616.56, 8192 MiB, 8.9\n",
                "",
            )

        report = gpu_probe.probe_gpu(
            nvml_module=make_full_nvml(),
            nvidia_smi="/fake/nvidia-smi",
            smi_runner=fake_runner,
            env={"PATH": ""},
        )
        probe = report["nvidia_smi_probe"]
        self.assertEqual(probe["status"], "ok")
        self.assertEqual(probe["command"][0], "/fake/nvidia-smi")
        parsed = probe["parsed"][0]
        self.assertEqual(parsed["name"], "NVIDIA GeForce RTX 4060 Laptop GPU")
        self.assertEqual(parsed["uuid"], "GPU-abc-123")
        self.assertEqual(parsed["driver_version"], "616.56")
        self.assertEqual(parsed["memory.total"], "8192 MiB")
        self.assertEqual(parsed["memory.total_bytes"], 8192 * 1024 * 1024)
        self.assertEqual(parsed["compute_cap"], "8.9")

    def test_smi_nonzero_recorded(self):
        def fake_runner(path, args):
            return (6, "", "Unable to determine the device handle")

        report = gpu_probe.probe_gpu(
            nvml_module=make_full_nvml(),
            nvidia_smi="/fake/nvidia-smi",
            smi_runner=fake_runner,
            env={"PATH": ""},
        )
        self.assertEqual(report["nvidia_smi_probe"]["status"], "unavailable")
        self.assertIn("Unable to determine", report["nvidia_smi_probe"]["stderr"])

    def test_no_smi_skipped(self):
        report = gpu_probe.probe_gpu(
            nvml_module=make_full_nvml(),
            nvidia_smi=None,
            smi_runner=lambda *a: self.fail("不应被调用"),
            env={"PATH": ""},
        )
        self.assertEqual(report["nvidia_smi_probe"]["status"], "skipped")
        self.assertFalse(report["nvidia_smi"]["found"])


class TestParsers(unittest.TestCase):
    def test_parse_query_gpu_csv(self):
        rows = gpu_probe.parse_query_gpu_csv(
            "A, B, 1 MiB\n\nC, D, 2 GiB\nno-commas-line\n"
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {"col0": "A", "col1": "B", "col2": "1 MiB"})

    def test_parse_memory_to_bytes(self):
        mib = gpu_probe.parse_memory_to_bytes
        self.assertEqual(mib("8192 MiB"), 8192 * 1024 * 1024)
        self.assertEqual(mib("8192MIB"), 8192 * 1024 * 1024)
        self.assertEqual(mib("0.5 GiB"), 512 * 1024 * 1024)
        self.assertEqual(mib("8192"), 8192 * 1024 * 1024)  # 无单位按 MiB
        self.assertIsNone(mib("N/A"))
        self.assertIsNone(mib("[N/A]"))
        self.assertIsNone(mib(""))
        self.assertIsNone(mib("abc"))


class TestSummarizeProbe(unittest.TestCase):
    def test_summary(self):
        report = gpu_probe.probe_gpu(nvml_module=make_full_nvml(), env={"PATH": ""})
        summary = gpu_probe.summarize_probe(report)
        self.assertTrue(summary["gpu_present"])
        self.assertEqual(summary["name"], "NVIDIA GeForce RTX 4060 Laptop GPU")
        self.assertEqual(summary["driver_version"], "616.56")
        self.assertEqual(summary["compute_capability"], "8.9")

    def test_summary_without_gpu(self):
        report = {
            "wsl": {"is_wsl": False, "evidence": []},
            "nvidia_smi": {"found": False},
            "nvml": {"devices": []},
        }
        summary = gpu_probe.summarize_probe(report)
        self.assertFalse(summary["gpu_present"])
        self.assertIsNone(summary["name"])


if __name__ == "__main__":
    unittest.main()
