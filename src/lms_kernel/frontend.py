"""Helpers the LMS frontend calls inside the kernel (runtime contract 4.1-4.4).

Called as ``user_expressions`` of an empty, silent execute_request, e.g.
``__import__('lms_kernel.frontend').frontend.variables()``. Every function returns an
object with ``_repr_json_()``; the result arrives in
``execute_reply.user_expressions.r.data["application/json"]``.

Rules (contract 4.1):
- nothing is added to the student's namespace;
- student objects are never evaluated: no tensor element reads, no GPU sync, no
  ``__repr__``/``__len__`` of arbitrary objects. Only metadata is read;
- results carry ``"version": 1`` and contain no NaN/Infinity.

torch and numpy are optional. ``variables()``/``module_tree()`` only look at them when
the student already imported them (``sys.modules``); ``env()`` imports torch only if it
is installed; ``resources()`` never imports it.
"""

from __future__ import annotations

import collections
import importlib.metadata
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import types
from typing import Any, Optional

from . import __version__

CONTRACT_VERSION = "0.10"
SCHEMA_VERSION = 1

MAX_VARIABLES = 200
MAX_SUMMARY = 120
TREE_MAX_DEPTH = 6
TREE_MAX_CHILDREN = 100

# Exact types whose repr is cheap and ours (not the student's) to call.
REPR_TYPES = (bool, int, float, complex, str, type(None))
CONTAINER_TYPES = (list, tuple, dict, set, frozenset, collections.deque)


class Result:
    """A helper result: IPython formats it as application/json via _repr_json_."""

    def __init__(self, data: dict):
        self.data = data

    def _repr_json_(self):
        return self.data

    def __repr__(self):
        return f"<lms_kernel result: {', '.join(self.data)}>"


# --- 4.2 info ---------------------------------------------------------------------------

def info() -> Result:
    return Result({"version": SCHEMA_VERSION, "lms_kernel": __version__, "contract": CONTRACT_VERSION})


# --- 4.3 env ----------------------------------------------------------------------------

def _package(requirement: str) -> dict:
    from packaging.requirements import InvalidRequirement, Requirement

    try:
        req = Requirement(requirement)
    except InvalidRequirement as e:
        return {"requirement": requirement, "name": None, "installed": None, "ok": False,
                "error": str(e)}
    try:
        installed: Optional[str] = importlib.metadata.version(req.name)
    except importlib.metadata.PackageNotFoundError:
        installed = None
    ok = installed is not None and req.specifier.contains(installed, prereleases=True)
    return {"requirement": requirement, "name": req.name, "installed": installed, "ok": ok}


def _torch_env() -> Optional[dict]:
    if importlib.util.find_spec("torch") is None:
        return None
    no_cuda = {"available": False, "version": None, "devices": []}
    try:
        import torch
    except Exception as e:  # installed but broken (e.g. missing CUDA libraries)
        try:
            version = importlib.metadata.version("torch")
        except importlib.metadata.PackageNotFoundError:
            version = None
        return {"version": version, "cuda": no_cuda, "mps": False,
                "error": f"{type(e).__name__}: {e}"[:500]}

    cuda = no_cuda
    if torch.cuda.is_available():
        devices = []
        for i in range(torch.cuda.device_count()):
            props = torch.cuda.get_device_properties(i)
            devices.append({"name": props.name, "memory_total": int(props.total_memory)})
        cuda = {"available": True, "version": torch.version.cuda, "devices": devices}
    mps_backend = getattr(torch.backends, "mps", None)
    mps = bool(mps_backend and mps_backend.is_available())
    return {"version": torch.__version__, "cuda": cuda, "mps": mps}


def env(requirements=()) -> Result:
    if isinstance(requirements, str):
        requirements = [requirements]
    return Result({
        "version": SCHEMA_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": [_package(str(r)) for r in requirements],
        "torch": _torch_env(),
    })


# --- 4.6 resources ----------------------------------------------------------------------

NVIDIA_SMI_TIMEOUT = 5.0
NVIDIA_SMI_QUERY = "index,name,memory.total,memory.used,utilization.gpu,temperature.gpu"
MIB = 1024 * 1024


def _num(text: str, scale: int = 1) -> Optional[int]:
    """nvidia-smi field -> int (None for "[N/A]", "[Not Supported]", ...)."""
    try:
        return int(float(text.strip()) * scale)
    except ValueError:
        return None


def _gpus() -> tuple[list, Optional[str]]:
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return [], None
    try:
        out = subprocess.run(
            [exe, f"--query-gpu={NVIDIA_SMI_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return [], f"nvidia-smi timed out after {NVIDIA_SMI_TIMEOUT:.0f}s"
    except OSError as e:
        return [], f"nvidia-smi: {e}"[:500]
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip().splitlines()
        return [], (f"nvidia-smi exit {out.returncode}: " + (detail[0] if detail else ""))[:500]
    gpus = []
    for line in out.stdout.splitlines():
        fields = [f.strip() for f in line.split(",")]
        if len(fields) != 6:
            continue
        gpus.append({
            "index": _num(fields[0]),
            "name": fields[1],
            "memory_total": _num(fields[2], MIB),
            "memory_used": _num(fields[3], MIB),
            "utilization": _num(fields[4]),
            "temperature": _num(fields[5]),
        })
    return gpus, None


def _meminfo() -> dict:
    values = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    values[key] = int(parts[0]) * 1024  # kB
    except OSError:
        pass
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    used = total - available if total is not None and available is not None else None
    return {"total": total, "available": available, "used": used}


def _cpu_memory() -> tuple[dict, dict]:
    try:
        import psutil
    except Exception:
        psutil = None
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            return (
                {"count": os.cpu_count(), "percent": float(psutil.cpu_percent(interval=0.1))},
                {"total": int(vm.total), "available": int(vm.available), "used": int(vm.used)},
            )
        except Exception:
            pass
    return {"count": os.cpu_count(), "percent": None}, _meminfo()


def _disk() -> dict:
    path = None
    try:
        path = os.getcwd()
        du = shutil.disk_usage(path)
        return {"path": path, "total": du.total, "used": du.used, "free": du.free}
    except OSError:
        return {"path": path, "total": None, "used": None, "free": None}


def resources() -> Result:
    cpu, memory = _cpu_memory()
    gpus, gpu_error = _gpus()
    return Result({
        "version": SCHEMA_VERSION,
        "cpu": cpu,
        "memory": memory,
        "disk": _disk(),
        "gpus": gpus,
        "gpu_error": gpu_error,
        "mps": platform.system() == "Darwin" and platform.machine() == "arm64",
    })


# --- 4.4 variables / module_tree --------------------------------------------------------

def _shell():
    try:
        from IPython import get_ipython

        return get_ipython()
    except Exception:
        return None


def _user_ns() -> dict:
    shell = _shell()
    if shell is not None:
        return shell.user_ns
    return sys.modules["__main__"].__dict__


def _hidden_ns() -> dict:
    """Names IPython/ipykernel put into user_ns (In, Out, exit, quit, get_ipython, open, ...)."""
    shell = _shell()
    return getattr(shell, "user_ns_hidden", None) or {}


_MISSING = object()


def _injected(name: str, value, hidden: dict) -> bool:
    # Same rule as IPython's %who: hidden only while it is still the very same object.
    return hidden.get(name, _MISSING) is value


def _type_name(cls: type) -> str:
    module = getattr(cls, "__module__", None) or ""
    name = getattr(cls, "__qualname__", None) or cls.__name__
    return name if module == "builtins" else f"{module}.{name}"


def _clip(text: str) -> str:
    return text if len(text) <= MAX_SUMMARY else text[: MAX_SUMMARY - 1] + "…"


def _summary(value: Any) -> str:
    cls = type(value)
    if cls is str:
        return _clip(repr(value)) if len(value) <= MAX_SUMMARY else f"str[{len(value)}]"
    if cls is int and value.bit_length() > 400:  # repr would be huge (or raise past 4300 digits)
        return "int"
    if cls in REPR_TYPES:
        return _clip(repr(value))
    np = sys.modules.get("numpy")
    if np is not None and issubclass(cls, np.generic) and not issubclass(cls, (np.str_, np.bytes_, np.void)):
        return _clip(repr(value))  # numpy scalar: numpy's own repr
    for base in CONTAINER_TYPES:
        if issubclass(cls, base):
            # The base type's C-level __len__, never a subclass override.
            return f"{cls.__name__}[{base.__len__(value)}]"
    return _clip(cls.__name__)


def _torch_module():
    return sys.modules.get("torch")


def _module_params(module) -> tuple[Optional[int], Optional[int]]:
    try:
        total = trainable = 0
        for p in module.parameters():
            n = p.numel()
            total += n
            if p.requires_grad:
                trainable += n
        return total, trainable
    except Exception:  # e.g. uninitialized lazy parameters
        return None, None


def _describe(name: str, value: Any) -> dict:
    cls = type(value)
    item = {"name": name, "type": _type_name(cls), "summary": _summary(value)}
    try:
        torch = _torch_module()
        np = sys.modules.get("numpy")
        if torch is not None and issubclass(cls, torch.Tensor):
            shape = list(value.shape)
            item.update(
                summary=_clip(f"Tensor({', '.join(map(str, shape))})"),
                shape=shape,
                dtype=str(value.dtype),
                device=str(value.device),
                nbytes=value.element_size() * value.nelement(),
            )
        elif np is not None and issubclass(cls, np.ndarray):
            shape = list(value.shape)
            item.update(
                summary=_clip(f"ndarray({', '.join(map(str, shape))})"),
                shape=shape,
                dtype=str(value.dtype),
                device="cpu",
                nbytes=int(value.nbytes),
            )
        elif torch is not None and issubclass(cls, torch.nn.Module):
            params, trainable = _module_params(value)
            item.update(params=params, trainable=trainable)
    except Exception:
        pass  # odd subclass or proxy: keep the common fields only
    return item


def variables() -> Result:
    ns = _user_ns()
    hidden = _hidden_ns()
    names = sorted(
        n for n, v in list(ns.items())
        if isinstance(n, str)
        and not n.startswith("_")
        and not _injected(n, v, hidden)
        and not issubclass(type(v), types.ModuleType)
    )
    truncated = len(names) > MAX_VARIABLES
    return Result({
        "version": SCHEMA_VERSION,
        "variables": [_describe(n, ns[n]) for n in names[:MAX_VARIABLES]],
        "truncated": truncated,
    })


def _tree(name: str, module, depth: int) -> dict:
    params, trainable = _module_params(module)
    node = {"name": name, "type": _type_name(type(module)), "params": params,
            "trainable": trainable, "children": []}
    children = list(module.named_children())
    if children and depth >= TREE_MAX_DEPTH:
        node["truncated"] = True
        return node
    if len(children) > TREE_MAX_CHILDREN:
        node["truncated"] = True
    node["children"] = [_tree(n, m, depth + 1) for n, m in children[:TREE_MAX_CHILDREN]]
    return node


def module_tree(name: str) -> Result:
    ns = _user_ns()
    if name not in ns:
        raise KeyError(name)
    value = ns[name]
    torch = _torch_module()
    if torch is None or not issubclass(type(value), torch.nn.Module):
        raise TypeError(f"{name} is not a torch.nn.Module ({_type_name(type(value))})")
    root = _tree(name, value, 0)
    root.setdefault("truncated", False)
    return Result({"version": SCHEMA_VERSION, **root})
