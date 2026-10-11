import resources
from resources import SystemResources, auto_job_cap, auto_processes


def box(cpus, usable_gb, total_gb=None):
    return SystemResources(cpu_logical=cpus, ram_usable_gb=usable_gb, ram_total_gb=total_gb or usable_gb)


def test_one_process_per_core_when_alone():
    assert auto_processes(box(2, 2.67)) == 2
    assert auto_processes(box(16, 64)) == resources.MAX_PROCESSES


def test_cores_are_shared_with_running_jobs():
    assert auto_processes(box(2, 2.67), concurrent_jobs=2) == 1
    assert auto_processes(box(8, 32), concurrent_jobs=2) == 4


def test_ram_binds_below_cores():
    per = resources.PROCESS_BASE_MB + resources.MB_PER_WORKER
    assert auto_processes(box(8, 3 * per / 1024)) == 3


def test_never_zero():
    assert auto_processes(box(1, 0.01), concurrent_jobs=4) == 1
    assert auto_job_cap(box(1, 0.01)) == 1


def test_job_cap_follows_the_box():
    assert auto_job_cap(box(2, 2.67, 3.73)) == 2
    assert auto_job_cap(box(8, 30, 32)) == 8
