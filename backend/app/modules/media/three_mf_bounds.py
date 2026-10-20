"""Upper bounds on a 3MF package's geometry, computed without parsing its XML.

Native admission needs a claim before the guarded worker reads the package. A
full parse in the supervisor would cost what the worker is about to spend, so
this module streams each ``.model`` part once and only recognizes structure:
``object``, ``component`` and ``item`` start tags, plus every occurrence of the
word ``triangle``. That count can only exceed the triangles an object declares
(comments, metadata and the ``triangles`` container also match), never fall
short of it, so a weighted claim built on it never starves the worker.

Anything this scanner cannot resolve with certainty returns ``None`` and keeps
the whole-pool claim: an unsafe or ambiguous package is the guarded reader's to
refuse, not this estimate's to guess.
"""

from __future__ import annotations

import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PRODUCTION_NS = b"http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
_MODEL_RELATIONSHIP = b"http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
# The guarded reader's own archive envelope; beyond it the reader refuses.
MAX_ENTRIES = 4096
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_DEPTH = 64
# Structural start tags never contain "<", so a part is split at its last "<"
# and every complete tag lands in one window. A tag longer than this is not
# something a producer writes; it keeps the whole-pool claim.
_CHUNK_BYTES = 1024 * 1024
_MAX_CARRY_BYTES = 64 * 1024
_STRUCTURE = re.compile(
    rb"<(?:[A-Za-z_][\w.-]*:)?(model|object|component|item)(?=[\s/>])([^>]*)>"
)
_ATTRIBUTE = re.compile(
    rb"""\s(?:([A-Za-z_][\w.-]*):)?([A-Za-z_][\w.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')"""
)
_RELATIONSHIP = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?Relationship(?=[\s/>])([^>]*)>")


@dataclass(frozen=True)
class ThreeMFBounds:
    """Bounds the guarded reader cannot exceed for one package.

    ``reached_model_bytes`` is the uncompressed size of the root part and of
    every part a reachable object lives in: the reader parses exactly those.
    ``placed_faces`` counts triangles once per placement reachable from a build
    item, through components, capped at ``face_cap + 1``.
    """

    reached_model_bytes: int
    placed_faces: int


@dataclass
class _Object:
    faces: int
    children: list[tuple[str, int]]


class _Unresolvable(Exception):
    """The package's structure cannot be bounded with certainty."""


def _attributes(raw: bytes) -> dict[tuple[bytes | None, bytes], bytes]:
    values: dict[tuple[bytes | None, bytes], bytes] = {}
    for match in _ATTRIBUTE.finditer(raw):
        key = (match.group(1), match.group(2))
        if key in values:
            raise _Unresolvable("duplicate attribute")
        values[key] = match.group(3) if match.group(3) is not None else match.group(4)
    return values


def _object_id(value: bytes | None) -> int:
    if value is None or not value.isdigit() or not 1 <= int(value) <= 2**31 - 1:
        raise _Unresolvable("object id")
    return int(value)


def _target(
    part: str, attrs: dict[tuple[bytes | None, bytes], bytes], production: set[bytes]
) -> str:
    # The reader follows only the production namespace's path; any other
    # "path" attribute leaves the reference inside its own part.
    paths = [
        value
        for (prefix, name), value in attrs.items()
        if name == b"path" and prefix in production
    ]
    if not paths:
        return part
    (path,) = paths
    target = path.decode("utf-8", "strict").lstrip("/")
    if "\\" in target or ".." in PurePosixPath(target).parts:
        raise _Unresolvable("unsafe path")
    return target


class _Budget:
    def __init__(self, wall_seconds: float, cpu_seconds: float) -> None:
        self.deadline = time.monotonic() + wall_seconds
        self.cpu_deadline = time.thread_time() + cpu_seconds

    def check(self) -> None:
        if time.monotonic() >= self.deadline or time.thread_time() >= self.cpu_deadline:
            raise _Unresolvable("preflight budget")


def _scan_part(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    budget: _Budget,
) -> tuple[dict[int, _Object], list[tuple[str, int]]]:
    """Objects and build items of one part, read in bounded windows."""
    name = info.filename
    objects: dict[int, _Object] = {}
    items: list[tuple[str, int]] = []
    production: set[bytes] = set()
    current: _Object | None = None
    seen_model = False

    def scan(window: bytes) -> None:
        nonlocal current, seen_model
        position = 0
        for match in _STRUCTURE.finditer(window):
            stray = window.count(b"triangle", position, match.start())
            if stray:
                if current is None:
                    raise _Unresolvable("triangles outside an object")
                current.faces += stray
            position = match.end()
            tag, raw = match.group(1), match.group(2)
            attrs = _attributes(raw)
            if tag == b"model":
                if seen_model:
                    raise _Unresolvable("second model element")
                seen_model = True
                for (attr_prefix, attr_name), value in attrs.items():
                    if attr_prefix == b"xmlns" and value == PRODUCTION_NS:
                        production.add(attr_name)
                continue
            if not seen_model or any(
                attr_prefix == b"xmlns"
                or (attr_prefix is None and attr_name == b"xmlns")
                for attr_prefix, attr_name in attrs
            ):
                raise _Unresolvable("namespace outside the model element")
            if tag == b"object":
                object_id = _object_id(attrs.get((None, b"id")))
                if object_id in objects:
                    raise _Unresolvable("duplicate object")
                current = objects[object_id] = _Object(0, [])
            elif tag == b"component":
                if current is None:
                    raise _Unresolvable("component outside an object")
                current.children.append(
                    (
                        _target(name, attrs, production),
                        _object_id(attrs.get((None, b"objectid"))),
                    )
                )
            else:
                items.append(
                    (
                        _target(name, attrs, production),
                        _object_id(attrs.get((None, b"objectid"))),
                    )
                )
        stray = window.count(b"triangle", position)
        if stray:
            if current is None:
                raise _Unresolvable("triangles outside an object")
            current.faces += stray

    carry = b""
    total = 0
    with archive.open(info) as stream:
        while chunk := stream.read(_CHUNK_BYTES):
            total += len(chunk)
            budget.check()
            window = carry + chunk
            cut = window.rfind(b"<")
            if cut == -1:
                cut = len(window)
            # A window that is one unfinished tag stays whole in the carry.
            scan(window[:cut])
            carry = window[cut:]
            if len(carry) > _MAX_CARRY_BYTES:
                raise _Unresolvable("unterminated tag")
    scan(carry)
    budget.check()
    if total != info.file_size or not seen_model:
        raise _Unresolvable("incomplete part")
    return objects, items


def _root_part(
    archive: zipfile.ZipFile, names: dict[str, zipfile.ZipInfo], models: list[str]
) -> str:
    """The part the guarded reader treats as the package root."""
    relationship = names.get("_rels/.rels")
    if relationship is None:
        return next(
            (name for name in models if name.lower() == "3d/3dmodel.model"), models[0]
        )
    if relationship.file_size > 1024 * 1024:
        raise _Unresolvable("relationship size")
    targets = []
    for match in _RELATIONSHIP.finditer(archive.read(relationship)):
        attrs = _attributes(match.group(1))
        if attrs.get((None, b"Type")) == _MODEL_RELATIONSHIP:
            target = attrs.get((None, b"Target"))
            if target is None:
                raise _Unresolvable("relationship without a target")
            targets.append(target.decode("utf-8", "strict").lstrip("/"))
    if len(targets) != 1 or targets[0] not in models:
        raise _Unresolvable("root relationship")
    return targets[0]


def bounds(
    path: Path,
    *,
    face_cap: int,
    max_model_bytes: int,
    wall_seconds: float,
    cpu_seconds: float,
) -> ThreeMFBounds | None:
    """Upper bounds for *path*, or ``None`` when they cannot be certain in budget."""
    budget = _Budget(wall_seconds, cpu_seconds)
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = {entry.filename: entry for entry in entries}
            if (
                len(entries) > MAX_ENTRIES
                or len(names) != len(entries)
                or sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES
            ):
                return None
            models = sorted(name for name in names if name.lower().endswith(".model"))
            model_bytes = sum(names[name].file_size for name in models)
            if (
                not models
                or model_bytes > max_model_bytes
                or any(
                    names[name].file_size
                    > max(names[name].compress_size, 1) * MAX_COMPRESSION_RATIO
                    for name in models
                )
            ):
                return None
            root = _root_part(archive, names, models)
            parts = {name: _scan_part(archive, names[name], budget) for name in models}
    except (
        _Unresolvable,
        OSError,
        ValueError,
        EOFError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
    ):
        # Encrypted, truncated or unsupported entries are the reader's to refuse.
        return None

    cap = face_cap + 1
    placed: dict[tuple[str, int], int] = {}

    def faces(key: tuple[str, int], ancestors: tuple[tuple[str, int], ...]) -> int:
        if key in ancestors or len(ancestors) >= MAX_DEPTH:
            raise _Unresolvable("cyclic or deep component graph")
        if key in placed:
            return placed[key]
        part, object_id = key
        if part not in parts or object_id not in parts[part][0]:
            raise _Unresolvable("missing object")
        obj = parts[part][0][object_id]
        total = obj.faces
        for child in obj.children:
            total = min(cap, total + faces(child, (*ancestors, key)))
        placed[key] = total
        return total

    try:
        total = 0
        for item in parts[root][1]:
            total = min(cap, total + faces(item, ()))
    except _Unresolvable:
        return None
    reached = {root} | {part for part, _ in placed}
    return ThreeMFBounds(sum(names[part].file_size for part in reached), total)
