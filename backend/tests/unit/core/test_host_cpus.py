"""The native mesh job default follows the CPUs a process can really use.

A container limited with ``--cpus`` still sees every host core through
``os.process_cpu_count()``. If the cgroup quota is ignored, a 4-CPU container on
a 12-core host defaults to the cap and oversubscribes; if the default ignores
CPUs entirely, one mesh job runs at a time on any machine and an import uses a
fraction of the CPU it was given. Both regressions turn this file red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import host_cpus
from app.core.host_cpus import cgroup_cpu_limit, native_jobs_for, usable_cpus

CONTAINER_CGROUP = "0::/\n"
SERVICE_CGROUP = "0::/system.slice/printstash.service\n"


@pytest.fixture
def cgroup(tmp_path: Path) -> Path:
    root = tmp_path / "cgroup"
    root.mkdir()
    return root


def _self_cgroup(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "self-cgroup"
    path.write_text(content)
    return path


def _cpu_max(group: Path, line: str) -> None:
    group.mkdir(parents=True, exist_ok=True)
    (group / "cpu.max").write_text(line + "\n")


class TestCgroupCpuLimit:
    def test_reads_the_container_quota(self, cgroup: Path, tmp_path: Path) -> None:
        _cpu_max(cgroup, "400000 100000")

        assert cgroup_cpu_limit(cgroup, _self_cgroup(tmp_path, CONTAINER_CGROUP)) == 4

    def test_an_unlimited_quota_is_no_limit(self, cgroup: Path, tmp_path: Path) -> None:
        _cpu_max(cgroup, "max 100000")

        assert cgroup_cpu_limit(cgroup, _self_cgroup(tmp_path, CONTAINER_CGROUP)) is None

    @pytest.mark.parametrize(
        ("line", "cpus"),
        [
            pytest.param("150000 100000", 2, id="one-and-a-half"),
            pytest.param("50000 100000", 1, id="half-a-cpu"),
        ],
    )
    def test_a_fractional_quota_rounds_up(
        self, cgroup: Path, tmp_path: Path, line: str, cpus: int
    ) -> None:
        _cpu_max(cgroup, line)

        assert cgroup_cpu_limit(cgroup, _self_cgroup(tmp_path, CONTAINER_CGROUP)) == cpus

    def test_a_service_group_quota_below_the_mount_applies(
        self, cgroup: Path, tmp_path: Path
    ) -> None:
        _cpu_max(cgroup, "max 100000")
        _cpu_max(cgroup / "system.slice" / "printstash.service", "200000 100000")

        assert cgroup_cpu_limit(cgroup, _self_cgroup(tmp_path, SERVICE_CGROUP)) == 2

    def test_a_parent_slice_quota_applies(self, cgroup: Path, tmp_path: Path) -> None:
        _cpu_max(cgroup / "system.slice", "300000 100000")
        _cpu_max(cgroup / "system.slice" / "printstash.service", "max 100000")

        assert cgroup_cpu_limit(cgroup, _self_cgroup(tmp_path, SERVICE_CGROUP)) == 3

    def test_the_tightest_quota_on_the_path_wins(
        self, cgroup: Path, tmp_path: Path
    ) -> None:
        _cpu_max(cgroup / "system.slice", "200000 100000")
        _cpu_max(cgroup / "system.slice" / "printstash.service", "600000 100000")

        assert cgroup_cpu_limit(cgroup, _self_cgroup(tmp_path, SERVICE_CGROUP)) == 2

    def test_reads_a_cgroup_v1_quota(self, cgroup: Path, tmp_path: Path) -> None:
        (cgroup / "cpu").mkdir()
        (cgroup / "cpu" / "cpu.cfs_quota_us").write_text("300000\n")
        (cgroup / "cpu" / "cpu.cfs_period_us").write_text("100000\n")

        assert cgroup_cpu_limit(cgroup, _self_cgroup(tmp_path, CONTAINER_CGROUP)) == 3

    def test_a_cgroup_v1_quota_of_minus_one_is_no_limit(
        self, cgroup: Path, tmp_path: Path
    ) -> None:
        (cgroup / "cpu").mkdir()
        (cgroup / "cpu" / "cpu.cfs_quota_us").write_text("-1\n")
        (cgroup / "cpu" / "cpu.cfs_period_us").write_text("100000\n")

        assert cgroup_cpu_limit(cgroup, _self_cgroup(tmp_path, CONTAINER_CGROUP)) is None

    def test_no_cgroup_files_is_no_limit(self, cgroup: Path, tmp_path: Path) -> None:
        assert cgroup_cpu_limit(cgroup, tmp_path / "absent") is None

    def test_ignores_a_cgroup_path_that_escapes_the_mount(
        self, cgroup: Path, tmp_path: Path
    ) -> None:
        _cpu_max(tmp_path / "outside", "100000 100000")

        assert cgroup_cpu_limit(cgroup, _self_cgroup(tmp_path, "0::/../outside\n")) is None


class TestUsableCpus:
    def test_a_quota_below_the_visible_cpus_wins(
        self, cgroup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(host_cpus.os, "process_cpu_count", lambda: 12)
        _cpu_max(cgroup, "400000 100000")

        assert usable_cpus(cgroup, _self_cgroup(tmp_path, CONTAINER_CGROUP)) == 4

    def test_visible_cpus_below_the_quota_win(
        self, cgroup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(host_cpus.os, "process_cpu_count", lambda: 2)
        _cpu_max(cgroup, "800000 100000")

        assert usable_cpus(cgroup, _self_cgroup(tmp_path, CONTAINER_CGROUP)) == 2

    def test_without_a_quota_every_visible_cpu_counts(
        self, cgroup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(host_cpus.os, "process_cpu_count", lambda: 6)

        assert usable_cpus(cgroup, _self_cgroup(tmp_path, CONTAINER_CGROUP)) == 6

    def test_an_unknown_cpu_count_raises(
        self, cgroup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(host_cpus.os, "process_cpu_count", lambda: None)

        with pytest.raises(RuntimeError, match="no CPU count"):
            usable_cpus(cgroup, _self_cgroup(tmp_path, CONTAINER_CGROUP))


class TestNativeJobsFor:
    @pytest.mark.parametrize(
        ("cpus", "jobs"),
        [
            pytest.param(1, 1, id="one-cpu"),
            pytest.param(2, 1, id="two-cpus"),
            pytest.param(3, 1, id="three-cpus"),
            pytest.param(4, 2, id="four-cpus"),
            pytest.param(7, 3, id="seven-cpus"),
            pytest.param(8, 4, id="eight-cpus-reaches-the-cap"),
            pytest.param(64, 4, id="sixty-four-cpus-stays-at-the-cap"),
        ],
    )
    def test_runs_one_job_per_two_cpus_within_bounds(self, cpus: int, jobs: int) -> None:
        assert native_jobs_for(cpus) == jobs

    def test_rejects_a_count_below_one_cpu(self) -> None:
        with pytest.raises(ValueError, match="at least one CPU"):
            native_jobs_for(0)
