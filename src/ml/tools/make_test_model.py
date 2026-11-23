"""Тестовая модель прогноза для проверки Docker-Lambda: «будет как сейчас»
(последнее значение на все горизонты). Модель Алихана заменяет её, сохранив
вход и выход (см. src/ml/README.md).

    pip install onnx && python3 src/ml/tools/make_test_model.py
"""
import json
import os

import onnx
from onnx import TensorProto, helper

WINDOW, FEATURES, HORIZONS = 120, ["cpu", "mem", "disk", "errors"], [15, 30, 60]
F, H = len(FEATURES), len(HORIZONS)
out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models")

x = helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, WINDOW, F])
y = helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, H, F])
nodes = [
    helper.make_node("Constant", [], ["idx"], value=helper.make_tensor("idx", TensorProto.INT64, [1], [WINDOW - 1])),
    helper.make_node("Gather", ["x", "idx"], ["last"], axis=1),                     # [1, 1, F]
    helper.make_node("Constant", [], ["reps"], value=helper.make_tensor("reps", TensorProto.INT64, [3], [1, H, 1])),
    helper.make_node("Tile", ["last", "reps"], ["y"]),                              # [1, H, F]
]
graph = helper.make_graph(nodes, "naive_forecast", [x], [y])
model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)], producer_name="kezekshi")
model.ir_version = 8
onnx.checker.check_model(model)
onnx.save(model, os.path.join(out_dir, "forecast.onnx"))
with open(os.path.join(out_dir, "forecast.json"), "w") as f:
    json.dump({"version": "test-naive-1", "window": WINDOW, "features": FEATURES, "horizons": HORIZONS,
               "mean": [0.0] * F, "std": [1.0] * F}, f, indent=2)
print("saved forecast.onnx and forecast.json to", os.path.abspath(out_dir))
