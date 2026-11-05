"""lms_kernel.frontend and lms_viz in a real ipykernel, called exactly as runtime
contract 4.1 says: empty code, silent, user_expressions."""

import importlib.util
import json
import queue

import pytest
from jupyter_client.manager import start_new_kernel

HELPER = "__import__('lms_kernel.frontend').frontend.{}"


def _require(*modules):
    """Skip unless the optional packages are installed (torch/numpy are not in [dev];
    the kernel runs on this same interpreter)."""
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if missing:
        pytest.skip(f"optional package not installed: {', '.join(missing)}")


class Kernel:
    def __init__(self):
        self.km, self.kc = start_new_kernel(kernel_name="python3")

    def stop(self):
        self.kc.stop_channels()
        self.km.shutdown_kernel(now=True)

    def run(self, code, timeout=120):
        """Run a normal cell; return (execute_reply content, iopub messages for it)."""
        mid = self.kc.execute(code)
        return self._collect(mid, timeout)

    def _collect(self, mid, timeout):
        reply = self.kc.get_shell_msg(timeout=timeout)
        while reply["parent_header"].get("msg_id") != mid:
            reply = self.kc.get_shell_msg(timeout=timeout)
        msgs = []
        while True:
            m = self.kc.get_iopub_msg(timeout=timeout)
            if m["parent_header"].get("msg_id") != mid:
                continue
            msgs.append(m)
            if m["msg_type"] == "status" and m["content"]["execution_state"] == "idle":
                break
        return reply["content"], msgs

    def helper(self, call, timeout=120):
        """Contract 4.1 call; returns (user_expressions['r'], reply content, iopub msgs)."""
        mid = self.kc.execute("", silent=True, store_history=False,
                              user_expressions={"r": HELPER.format(call)})
        reply, msgs = self._collect(mid, timeout)
        return reply["user_expressions"]["r"], reply, msgs

    def expr(self, expression):
        mid = self.kc.execute("", silent=True, store_history=False, user_expressions={"r": expression})
        reply, _ = self._collect(mid, 60)
        return reply["user_expressions"]["r"]["data"]["text/plain"]


@pytest.fixture(scope="module")
def kernel():
    k = Kernel()
    yield k
    k.stop()


@pytest.fixture(scope="module")
def kernel_no_torch():
    k = Kernel()
    # Behaves like torch is not installed: find_spec() -> None, import -> ModuleNotFoundError.
    k.helper("__class__")  # warm up
    k.expr("__import__('sys').modules.__setitem__('torch', None)")
    yield k
    k.stop()


def _json(r):
    assert r["status"] == "ok", r
    data = r["data"]["application/json"]
    json.dumps(data, allow_nan=False)  # no NaN / Infinity anywhere
    assert data["version"] == 1
    return data


# --- 4.1 calling convention ---------------------------------------------------------------

def test_helper_call_leaves_counter_and_namespace_untouched(kernel):
    kernel.run("a_value = 1")
    count_before = kernel.run("pass")[0]["execution_count"]
    ns_before = kernel.expr("sorted(get_ipython().user_ns)")
    for call in ("info()", "env(['numpy'])", "variables()"):
        r, reply, msgs = kernel.helper(call)
        _json(r)
        assert reply["status"] == "ok"
        # silent: no outputs, no execute_input
        assert {m["msg_type"] for m in msgs} == {"status"}
    assert kernel.expr("sorted(get_ipython().user_ns)") == ns_before
    assert kernel.run("pass")[0]["execution_count"] == count_before + 1


def test_helper_exception_comes_back_as_error(kernel):
    r, reply, _ = kernel.helper("module_tree('does_not_exist')")
    assert reply["status"] == "ok"  # the execution itself succeeds
    assert r["status"] == "error" and r["ename"] == "KeyError"
    assert {"evalue", "traceback"} <= set(r)
    kernel.run("not_a_module = 3")
    r, _, _ = kernel.helper("module_tree('not_a_module')")
    assert r["status"] == "error" and r["ename"] == "TypeError"


# --- 4.2 / 4.3 ------------------------------------------------------------------------------

def test_info(kernel):
    data = _json(kernel.helper("info()")[0])
    assert data == {"version": 1, "lms_kernel": "0.4.0", "contract": "0.10"}


def test_env_with_torch(kernel):
    _require('torch', 'numpy')
    data = _json(kernel.helper('env(["torch>=2.2", "numpy", "no-such-package-xyz", "torch<1", "bad req ~~"])')[0])
    assert data["python"].count(".") == 2 and data["platform"]
    pk = data["packages"]
    assert pk[0]["name"] == "torch" and pk[0]["installed"] and pk[0]["ok"] is True
    assert pk[1]["ok"] is True and pk[1]["installed"]
    assert pk[2] == {"requirement": "no-such-package-xyz", "name": "no-such-package-xyz",
                     "installed": None, "ok": False}
    assert pk[3]["ok"] is False and pk[3]["installed"]
    assert pk[4]["ok"] is False and pk[4]["error"]
    t = data["torch"]
    assert t["version"].startswith("2.")
    assert set(t["cuda"]) == {"available", "version", "devices"}
    if not t["cuda"]["available"]:
        assert t["cuda"] == {"available": False, "version": None, "devices": []}
    assert isinstance(t["mps"], bool)


def test_env_without_torch(kernel_no_torch):
    data = _json(kernel_no_torch.helper('env(["packaging"])')[0])  # always installed
    assert data["torch"] is None
    assert data["packages"][0]["ok"] is True


# --- 4.4 variables / module_tree ----------------------------------------------------------

SETUP = r'''
import torch, numpy as np, collections
from torch import nn

class Explodes:
    """Any call of __repr__/__len__/__str__ would be a contract violation."""
    def __repr__(self): raise AssertionError("repr called")
    def __str__(self): raise AssertionError("str called")
    def __len__(self): raise AssertionError("len called")

class SneakyList(list):
    def __len__(self): raise AssertionError("len called")

q = torch.zeros(2, 4, 8)
emb = nn.Embedding(10, 4)
model = nn.Sequential(nn.Linear(4, 8), nn.ReLU(), nn.Linear(8, 2))
model[2].weight.requires_grad_(False)
arr = np.ones((3, 5), dtype=np.float32)
lr = 0.001
n = 42
flag = True
nothing = None
name = "attention"
long_text = "x" * 500
huge = 10 ** 5000
items = [1, 2, 3]
mapping = {"a": 1}
dq = collections.deque([1, 2])
boom = Explodes()
sneaky = SneakyList([1, 2])
scalar = np.float32(0.5)
def fn(): pass
_hidden = 1
'''


@pytest.fixture(scope="module")
def populated(kernel):
    _require('torch', 'numpy')
    reply, _ = kernel.run(SETUP)
    assert reply["status"] == "ok", reply
    data = _json(kernel.helper("variables()")[0])
    return {v["name"]: v for v in data["variables"]}, data


def test_variables_schema_and_exclusions(populated):
    v, data = populated
    assert data["truncated"] is False
    names = list(v)
    assert names == sorted(names)
    for excluded in ("torch", "np", "nn", "collections", "In", "Out", "exit", "quit",
                     "get_ipython", "open", "_hidden"):
        assert excluded not in v
    assert all(len(x["summary"]) <= 120 for x in v.values())


def test_variables_tensor_ndarray_module(populated):
    v, _ = populated
    assert v["q"] == {"name": "q", "type": "torch.Tensor", "summary": "Tensor(2, 4, 8)",
                      "shape": [2, 4, 8], "dtype": "torch.float32", "device": "cpu", "nbytes": 256}
    assert v["arr"] == {"name": "arr", "type": "numpy.ndarray", "summary": "ndarray(3, 5)",
                        "shape": [3, 5], "dtype": "float32", "device": "cpu", "nbytes": 60}
    m = v["model"]
    assert m["type"] == "torch.nn.modules.container.Sequential" and m["summary"] == "Sequential"
    assert m["params"] == 4 * 8 + 8 + 8 * 2 + 2 and m["trainable"] == m["params"] - 16
    assert v["emb"]["params"] == 40


def test_variables_summaries_never_call_student_code(populated):
    v, _ = populated
    assert v["lr"] == {"name": "lr", "type": "float", "summary": "0.001"}
    assert v["n"]["summary"] == "42" and v["flag"]["summary"] == "True"
    assert v["nothing"]["summary"] == "None" and v["name"]["summary"] == "'attention'"
    assert v["long_text"]["summary"] == "str[500]"
    assert v["huge"]["summary"] == "int"
    assert v["items"]["summary"] == "list[3]" and v["mapping"]["summary"] == "dict[1]"
    assert v["dq"]["summary"] == "deque[2]"
    assert v["boom"] == {"name": "boom", "type": "__main__.Explodes", "summary": "Explodes"}
    assert v["sneaky"]["summary"] == "SneakyList[2]"
    assert v["scalar"]["summary"] == "np.float32(0.5)"
    assert v["fn"]["type"] == "function"


def test_variables_truncates_at_200(kernel):
    kernel.run("for _i in range(250): globals()[f'zz_{_i:03d}'] = _i")
    data = _json(kernel.helper("variables()")[0])
    assert len(data["variables"]) == 200 and data["truncated"] is True
    kernel.run("for _i in range(250): del globals()[f'zz_{_i:03d}']")


def test_variables_without_torch(kernel_no_torch):
    _require('numpy')
    kernel_no_torch.run("import numpy as np\narr = np.zeros(4)\nx = 1")
    data = _json(kernel_no_torch.helper("variables()")[0])
    v = {x["name"]: x for x in data["variables"]}
    assert v["arr"]["shape"] == [4] and v["x"]["summary"] == "1"


def test_module_tree(kernel, populated):
    data = _json(kernel.helper("module_tree('model')")[0])
    assert data["name"] == "model" and data["truncated"] is False
    assert data["type"] == "torch.nn.modules.container.Sequential"
    assert [c["name"] for c in data["children"]] == ["0", "1", "2"]
    first = data["children"][0]
    assert first == {"name": "0", "type": "torch.nn.modules.linear.Linear", "params": 40,
                     "trainable": 40, "children": []}
    assert data["children"][1]["params"] == 0


def test_module_tree_limits(kernel, populated):
    kernel.run(
        "deep = nn.Linear(1, 1)\n"
        "for _ in range(8): deep = nn.Sequential(deep)\n"
        "wide = nn.Sequential(*[nn.ReLU() for _ in range(150)])\n"
    )
    deep = _json(kernel.helper("module_tree('deep')")[0])
    node, depth = deep, 0
    while node["children"]:
        node, depth = node["children"][0], depth + 1
    assert depth == 6 and node.get("truncated") is True
    wide = _json(kernel.helper("module_tree('wide')")[0])
    assert len(wide["children"]) == 100 and wide["truncated"] is True


def test_module_tree_without_torch(kernel_no_torch):
    kernel_no_torch.run("thing = 1")
    r, _, _ = kernel_no_torch.helper("module_tree('thing')")
    assert r["status"] == "error" and r["ename"] == "TypeError"


# --- 4.5 lms_viz ------------------------------------------------------------------------------

def _displays(msgs, kind="display_data"):
    return [m["content"] for m in msgs if m["msg_type"] == kind]


def test_viz_heatmap_nan_and_fallback(kernel):
    reply, msgs = kernel.run(
        "import lms_viz\n"
        "lms_viz.heatmap([[1, float('nan')], [float('inf'), 2]], x_labels=['a', 'b'],"
        " color='diverging', title='attn')"
    )
    assert reply["status"] == "ok"
    shown = _displays(msgs)
    assert len(shown) == 1 and not _displays(msgs, "execute_result")  # last line: no double output
    bundle = shown[0]["data"]
    data = bundle["application/vnd.lms.heatmap+json"]
    assert data == {"version": 1, "data": [[1, None], [None, 2]], "color": "diverging",
                    "x_labels": ["a", "b"], "title": "attn"}
    assert "히트맵 2×2" in bundle["text/plain"]
    json.dumps(data, allow_nan=False)


def test_viz_heatmap_crops_large_tensor(kernel):
    _require('torch')
    _, msgs = kernel.run("import torch, lms_viz\nlms_viz.heatmap(torch.rand(300, 260))")
    data = _displays(msgs)[0]["data"]["application/vnd.lms.heatmap+json"]
    assert len(data["data"]) == 256 and len(data["data"][0]) == 256 and data["truncated"] is True


def test_viz_line_live_updates(kernel):
    _, msgs = kernel.run(
        "import lms_viz\n"
        "curve = lms_viz.line(title='loss', x_title='step')\n"
        "for i in range(50):\n"
        "    curve.append(i, train=1 / (i + 1))\n"
        "    if i % 10 == 0: curve.append(i, val=float('nan') if i == 0 else 0.5)\n"
    )
    first = _displays(msgs)
    assert len(first) == 1
    display_id = first[0]["transient"]["display_id"]
    assert first[0]["data"]["application/vnd.lms.line+json"]["series"] == []
    updates = _displays(msgs, "update_display_data")
    assert updates and all(u["transient"]["display_id"] == display_id for u in updates)
    final = updates[-1]["data"]["application/vnd.lms.line+json"]  # sent at cell end at the latest
    series = {s["name"]: s for s in final["series"]}
    assert len(series["train"]["x"]) == 50 and series["train"]["y"][0] == 1.0
    assert series["val"]["x"] == [0, 10, 20, 30, 40] and series["val"]["y"][0] is None
    assert final["title"] == "loss" and final["x_title"] == "step"


def test_viz_line_limits(kernel):
    _, msgs = kernel.run(
        "import lms_viz\n"
        "lms_viz.line({f's{i}': list(range(20000 if i == 0 else 5)) for i in range(25)})"
    )
    data = _displays(msgs)[0]["data"]["application/vnd.lms.line+json"]
    assert len(data["series"]) == 20 and data["truncated"] is True
    s0 = data["series"][0]
    assert len(s0["x"]) == 10_000 and s0["x"][0] == 0 and s0["x"][-1] == 19_999


def test_viz_scatter(kernel):
    _require('numpy')
    _, msgs = kernel.run(
        "import numpy as np, lms_viz\n"
        "pts = np.arange(6000, dtype=float)\n"
        "pts[1] = np.inf\n"
        "lms_viz.scatter(pts, pts * 2, group=['a', 'b'] * 3000, title='emb')"
    )
    data = _displays(msgs)[0]["data"]["application/vnd.lms.scatter+json"]
    assert len(data["x"]) == len(data["y"]) == len(data["group"]) == 5000
    assert data["truncated"] is True and data["x"][0] == 0
    json.dumps(data, allow_nan=False)
    small = kernel.run("lms_viz.scatter([1, 2], [3, float('nan')], label=['p', 'q'])")[1]
    d2 = _displays(small)[0]["data"]["application/vnd.lms.scatter+json"]
    assert d2 == {"version": 1, "x": [1, 2], "y": [3, None], "label": ["p", "q"]}


def test_variables_shows_injected_name_once_student_overwrites_it():
    """Contract 4.4 (v0.4): user_ns_hidden names are excluded only while they are still the
    same object (IPython %who rule)."""
    k = Kernel()
    try:
        names = lambda: [v["name"] for v in _json(k.helper("variables()")[0])["variables"]]
        assert names() == []  # fresh kernel: In, Out, exit, quit, get_ipython, open all hidden
        k.run("open = 1\nquit = 'q'")
        v = {x["name"]: x for x in _json(k.helper("variables()")[0])["variables"]}
        assert v["open"] == {"name": "open", "type": "int", "summary": "1"}
        assert v["quit"]["summary"] == "'q'"
        assert "exit" not in v and "In" not in v
    finally:
        k.stop()


# --- 4.6 resources ------------------------------------------------------------------------

import os
import subprocess
import sys
import time

from lms_kernel import frontend


def test_resources_in_kernel(kernel):
    t0 = time.monotonic()
    data = _json(kernel.helper("resources()")[0])
    assert time.monotonic() - t0 < 2
    assert set(data) == {"version", "cpu", "memory", "disk", "gpus", "gpu_error", "mps"}
    assert data["version"] == 1 and data["cpu"]["count"] >= 1
    assert data["memory"]["total"] > 0
    disk = data["disk"]
    assert disk["path"] and disk["total"] >= disk["free"] > 0
    assert isinstance(data["gpus"], list)


def test_resources_does_not_import_torch():
    code = ("import sys, lms_kernel.frontend as f; f.resources(); "
            "assert 'torch' not in sys.modules and 'numpy' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30)


def _fake_smi(tmp_path, monkeypatch, body):
    smi = tmp_path / "nvidia-smi"
    smi.write_text("#!/bin/sh\n" + body)
    smi.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def test_gpus_from_nvidia_smi(tmp_path, monkeypatch):
    _fake_smi(tmp_path, monkeypatch,
              'echo "0, Tesla T4, 15360, 2048, 35, 61"\n'
              'echo "1, NVIDIA GeForce RTX 4090, 24564, 0, [N/A], [N/A]"\n')
    gpus, error = frontend._gpus()
    assert error is None
    assert gpus == [
        {"index": 0, "name": "Tesla T4", "memory_total": 15360 * 1024 * 1024,
         "memory_used": 2048 * 1024 * 1024, "utilization": 35, "temperature": 61},
        {"index": 1, "name": "NVIDIA GeForce RTX 4090", "memory_total": 24564 * 1024 * 1024,
         "memory_used": 0, "utilization": None, "temperature": None},
    ]


def test_nvidia_smi_failure_and_absence(tmp_path, monkeypatch):
    _fake_smi(tmp_path, monkeypatch, 'echo "NVIDIA-SMI has failed because it could not communicate" >&2\nexit 9\n')
    gpus, error = frontend._gpus()
    assert gpus == [] and "exit 9" in error and "could not communicate" in error
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert frontend._gpus() == ([], None)


def test_nvidia_smi_timeout(tmp_path, monkeypatch):
    _fake_smi(tmp_path, monkeypatch, "sleep 5\n")
    monkeypatch.setattr(frontend, "NVIDIA_SMI_TIMEOUT", 0.3)
    gpus, error = frontend._gpus()
    assert gpus == [] and "timed out" in error


def test_cpu_memory_without_psutil(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    cpu, memory = frontend._cpu_memory()
    assert cpu == {"count": os.cpu_count(), "percent": None}
    if os.path.exists("/proc/meminfo"):
        assert memory["total"] > memory["available"] > 0
        assert memory["used"] == memory["total"] - memory["available"]
