# src/utils/fastjson.py
"""
A tiny, drop-in JSON helper that prefers a fast backend and exposes the
same surface as Python's json module:
    - dumps(obj, **kwargs) -> str
    - dump(obj, fp_or_path, **kwargs) -> None
    - loads(s, **kwargs) -> Any
    - load(fp_or_path, **kwargs) -> Any

Notes
-----
* Backend order: orjson -> ujson -> stdlib json
* `dump`/`load` accept either an open file-like object or a str/Path path.
* Writes are atomic when given a path (tmp + replace).
* Defaults: UTF-8, ensure_ascii=False, no indentation (compact).
"""

from __future__ import annotations

import io
import os
import json as _stdlib_json
from pathlib import Path
from typing import Any, Union, IO

# ---- Backend selection ------------------------------------------------------

_backend = "stdlib"

try:
    import orjson as _orjson  # type: ignore
    _backend = "orjson"
except Exception:  # pragma: no cover
    try:
        import ujson as _ujson  # type: ignore
        _backend = "ujson"
    except Exception:  # pragma: no cover
        _ujson = None  # type: ignore
        _orjson = None  # type: ignore

# ---- Internal helpers -------------------------------------------------------

_PathLike = Union[str, Path]
_FileLike = IO[str]
_FileOrPath = Union[_PathLike, _FileLike]


def _is_path(x: Any) -> bool:
    return isinstance(x, (str, Path))


def _open_for_read(fp_or_path: _FileOrPath) -> _FileLike:
    if _is_path(fp_or_path):
        return open(fp_or_path, "r", encoding="utf-8")
    if isinstance(fp_or_path, io.TextIOBase):
        return fp_or_path
    raise TypeError("load(fp): fp must be a path or a text file object")


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _ensure_str(s_or_bytes: Union[str, bytes]) -> str:
    return s_or_bytes.decode("utf-8") if isinstance(s_or_bytes, (bytes, bytearray)) else str(s_or_bytes)

# ---- Public API: dumps / loads ----------------------------------------------


def dumps(obj: Any, **kwargs: Any) -> str:
    """
    Serialize `obj` to a JSON string.

    Common kwargs honored across backends:
        - indent (int | None)
        - ensure_ascii (bool)
        - separators (tuple)  # stdlib only; ignored by fast backends
        - default (callable)  # used by stdlib; orjson uses OPT_NON_STR keys via option not exposed here
    """
    indent = kwargs.pop("indent", None)
    ensure_ascii = kwargs.pop("ensure_ascii", False)
    default = kwargs.pop("default", None)

    if _backend == "orjson":
        # orjson dumps -> bytes; options for ascii/indent
        opt = 0
        if not ensure_ascii:
            opt |= _orjson.OPT_NON_STR_KEYS if hasattr(_orjson, "OPT_NON_STR_KEYS") else 0  # noop if absent
        if indent:
            opt |= getattr(_orjson, "OPT_INDENT_2", 0)
        try:
            # orjson doesn't accept default= the same way; fallback to stdlib if default is supplied
            if default is not None:
                raise TypeError
            return _ensure_str(_orjson.dumps(obj, option=opt))
        except Exception:
            # fallback to stdlib when custom default or unsupported types appear
            return _stdlib_json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent, default=default)

    if _backend == "ujson":
        # ujson supports ensure_ascii, indent
        return _ujson.dumps(obj, ensure_ascii=ensure_ascii, indent=indent if indent else 0)

    # stdlib
    return _stdlib_json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent, default=default)


def loads(s: Union[str, bytes, bytearray], **kwargs: Any) -> Any:
    text = _ensure_str(s)
    if _backend == "orjson":
        return _orjson.loads(text)
    if _backend == "ujson":
        return _ujson.loads(text)
    return _stdlib_json.loads(text, **kwargs)

# ---- Public API: dump / load (path-or-file, atomic writes) -------------------


def dump(obj: Any, fp_or_path: _FileOrPath, **kwargs: Any) -> None:
    """
    Serialize `obj` to `fp_or_path`. Accepts an open text file or a filesystem path.
    When a path is given, writes atomically: write to .tmp then replace.
    """
    if _is_path(fp_or_path):
        path = Path(fp_or_path)  # type: ignore[arg-type]
        data = dumps(obj, **kwargs)
        _atomic_write(path, data)
        return

    if not isinstance(fp_or_path, io.TextIOBase):
        raise TypeError("dump(obj, fp): fp must be a path or a text file object")

    # open file-like: write directly
    fp_or_path.write(dumps(obj, **kwargs))


def load(fp_or_path: _FileOrPath, **kwargs: Any) -> Any:
    """
    Parse JSON content from `fp_or_path`. Accepts an open text file or a filesystem path.
    """
    if _is_path(fp_or_path):
        with _open_for_read(fp_or_path) as f:
            return loads(f.read(), **kwargs)
    if not isinstance(fp_or_path, io.TextIOBase):
        raise TypeError("load(fp): fp must be a path or a text file object")
    return loads(fp_or_path.read(), **kwargs)

# ---- Convenience aliases (optional) -----------------------------------------

# For codebases that use these names:
write_json = dump
read_json = load

# Expose which backend we ended up using (may help with diagnostics)
BACKEND = _backend