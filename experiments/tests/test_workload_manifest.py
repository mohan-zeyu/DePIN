"""workloads 纯逻辑测试：op/shape 清单、精度校验（不导入 torch）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_EXPERIMENTS_DIR = _TESTS_DIR.parent
for _p in (str(_TESTS_DIR), str(_EXPERIMENTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from depin_experiments import workloads  # noqa: E402


class TestDescribeOps(unittest.TestCase):
    def test_structure(self):
        ops = workloads.describe_ops()
        op_names = [op["op"] for op in ops]
        self.assertIn("conv2d", op_names)
        self.assertEqual(op_names.count("conv2d"), 2)
        self.assertIn("linear(gemm+bias)", op_names)
        self.assertIn("cross_entropy(log_softmax+nll)", op_names)
        self.assertIn("max_pool2d", op_names)

    def test_shapes_consistent(self):
        ops = workloads.describe_ops(batch_size=32)
        by_layer = {op["layer"]: op for op in ops}
        conv1 = by_layer["conv1"]
        self.assertEqual(conv1["input_shape"], [32, 3, 32, 32])
        self.assertEqual(conv1["output_shape"], [32, 16, 32, 32])
        pool1 = by_layer["pool1"]
        self.assertEqual(pool1["output_shape"], [32, 16, 16, 16])
        conv2 = by_layer["conv2"]
        self.assertEqual(conv2["weight_shape"], [32, 16, 3, 3])
        self.assertEqual(conv2["input_shape"], [32, 16, 16, 16])
        flatten = by_layer["flatten"]
        self.assertEqual(flatten["output_shape"], [32, 2048])
        fc = by_layer["fc"]
        self.assertEqual(fc["weight_shape"], [10, 2048])
        # fc 输入必须等于 flatten 输出
        self.assertEqual(fc["input_shape"], flatten["output_shape"])

    def test_no_flops_numbers_in_manifest(self):
        # 明确要求：清单只描述 shape，不产生 FLOPs 数字（避免被当成实测）
        ops = workloads.describe_ops()
        for op in ops:
            for key in op:
                self.assertNotIn("flops", key.lower())

    def test_invalid_image_size(self):
        with self.assertRaises(ValueError):
            workloads.describe_ops(image_size=30)


class TestValidatePrecisionList(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(
            workloads.validate_precision_list(["fp32", "fp16"]), ("fp32", "fp16")
        )
        self.assertEqual(workloads.validate_precision_list(("bf16",)), ("bf16",))

    def test_empty(self):
        with self.assertRaises(ValueError):
            workloads.validate_precision_list([])

    def test_invalid(self):
        with self.assertRaises(ValueError):
            workloads.validate_precision_list(["fp8"])

    def test_duplicate(self):
        with self.assertRaises(ValueError):
            workloads.validate_precision_list(["fp32", "fp32"])

    def test_module_import_does_not_pull_torch(self):
        # 本测试自身即证明：导入 workloads 模块不需要 torch
        self.assertNotIn("torch", sys.modules)


class TestWorkloadConstants(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(workloads.INPUT_SHAPE, (3, 32, 32))
        self.assertEqual(workloads.NUM_CLASSES, 10)
        self.assertEqual(workloads.DATA_POOL_SIZE, 1024)


if __name__ == "__main__":
    unittest.main()
