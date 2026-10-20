"""CPUs this process can actually use, and the native mesh job default derived from them.

``os.process_cpu_count()`` honours CPU affinity but not a cgroup CPU quota: a
container started with ``--cpus 4`` on a 12-core host reports 12. The kernel
enforces the quota, so the usable count is the smaller of the two. Positive
fractional quotas round up to whole CPUs before deriving the slot default;
this is a scheduling heuristic, not a bound on each worker's CPU use.
"""

from __future__ import annotations

import math
import os
from pathlib import Path, PurePosixPath

# Scale the automatic slot count by CPU availability. Memory admission
# independently limits which native jobs can run together.
CPUS_PER_NATIVE_JOB = 2
# Keep automatic concurrency bounded; explicit overrides remain available.
MAX_DEFAULT_NATIVE_JOBS = 4

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_SELF_CGROUP = Path("/proc/self/cgroup")


def _quota_cpus(cpu_max: str) -> int | None:
    """CPUs granted by a cgroup v2 ``cpu.max`` line; None when unlimited."""
    quota, period = cpu_max.split()
    if quota == "max":
        return None
    return max(1, math.ceil(int(quota) / int(period)))


def _v2_groups(root: Path, self_cgroup: Path) -> list[Path]:
    """The v2 mount root plus the process's own group and its ancestors.

    A container with a cgroup namespace exposes its group at the root; a host
    service sits below the mount, where a parent slice may carry the quota.
    """
    groups = [root]
    try:
        lines = self_cgroup.read_text().splitlines()
    except OSError:
        return groups
    for line in lines:
        if not line.startswith("0::/"):
            continue
        parts = PurePosixPath(line[3:]).parts[1:]
        if any(part in (".", "..") for part in parts):
            continue
        group = root.joinpath(*parts)
        while group != root:
            groups.append(group)
            group = group.parent
    return groups


def _v1_groups(root: Path, self_cgroup: Path) -> list[Path]:
    """CPU-controller mount roots plus the process's v1 group and its ancestors."""
    mounts = {root / "cpu"}
    try:
        mounts.update(path for path in root.iterdir() if "cpu" in path.name.split(","))
    except OSError:
        pass
    groups = sorted(mounts)
    try:
        lines = self_cgroup.read_text().splitlines()
    except OSError:
        return groups
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) != 3 or "cpu" not in fields[1].split(","):
            continue
        membership = PurePosixPath(fields[2])
        if not membership.is_absolute() or ".." in membership.parts:
            continue
        for mount in sorted(mounts):
            group = mount.joinpath(*membership.parts[1:])
            while group != mount:
                groups.append(group)
                group = group.parent
    return groups


def cgroup_cpu_limit(
    root: Path = _CGROUP_ROOT, self_cgroup: Path = _SELF_CGROUP
) -> int | None:
    """The tightest CPU quota on this process's cgroup path; None when none is set."""
    limits: list[int] = []
    for group in _v2_groups(root, self_cgroup):
        try:
            cpus = _quota_cpus((group / "cpu.max").read_text())
        except OSError:
            continue
        if cpus is not None:
            limits.append(cpus)
    for group in _v1_groups(root, self_cgroup):
        try:  # cgroup v1: a quota of -1 means unlimited
            quota = int((group / "cpu.cfs_quota_us").read_text())
            period = int((group / "cpu.cfs_period_us").read_text())
        except OSError:
            continue
        if quota > 0:
            limits.append(max(1, math.ceil(quota / period)))
    return min(limits) if limits else None


def usable_cpus(root: Path = _CGROUP_ROOT, self_cgroup: Path = _SELF_CGROUP) -> int:
    """Smaller of the affinity-visible CPUs and the cgroup quota."""
    visible = os.process_cpu_count()
    if visible is None:
        raise RuntimeError("the operating system reports no CPU count")
    quota = cgroup_cpu_limit(root, self_cgroup)
    return visible if quota is None else min(visible, quota)


def native_jobs_for(cpus: int) -> int:
    """One native job per two CPUs, at least one and at most four."""
    if cpus < 1:
        raise ValueError("a process has at least one CPU")
    return min(MAX_DEFAULT_NATIVE_JOBS, max(1, cpus // CPUS_PER_NATIVE_JOB))


def default_native_jobs() -> int:
    """Default ``max_render_jobs`` for the CPUs this process can use."""
    return native_jobs_for(usable_cpus())
