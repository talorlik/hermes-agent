"""G-KERNEL-REAP: an exited kernel child is reaped without tearing its cell down.

Fork-owned. It sits beside ``test_code_kernel.py`` so the fork carries no
edit on that upstream test file.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import pytest

from tools.code_kernel import shutdown_all_kernels


@pytest.fixture(autouse=True)
def _fresh_kernel_registry():
    shutdown_all_kernels()
    yield
    shutdown_all_kernels()


class _DeadProc:
    """A child that has already exited. poll() is the reap."""

    def __init__(self) -> None:
        self.polls = 0
        self.returncode = None
        # teardown calls os.killpg(pid). Zero probes this process group.
        self.pid = 2147483647

    def poll(self):
        self.polls += 1
        self.returncode = 0
        return 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class TestDeadKernelIsReaped(unittest.TestCase):
    def test_reap_once_polls_attached_dead_kernel_and_leaves_it(self):
        from tools.code_kernel import SessionKernel, _KERNELS, _REGISTRY, _reap_once

        kernel = SessionKernel(("dead-owner", "project", "python", "/tmp", ()))
        proc = _DeadProc()
        kernel.proc = proc  # type: ignore[assignment]
        kernel.attached = 1
        tmp = tempfile.mkdtemp(prefix="reap_attached_")
        kernel.tmpdir = tmp
        with _REGISTRY.lock:
            _KERNELS[kernel.key] = kernel
        try:
            _reap_once()
            self.assertGreaterEqual(proc.polls, 1)
            self.assertIs(_KERNELS.get(kernel.key), kernel)
            self.assertEqual(kernel.attached, 1)
            self.assertFalse(kernel.stop_event.is_set())
            self.assertTrue(os.path.isdir(tmp))
        finally:
            with _REGISTRY.lock:
                _KERNELS.pop(kernel.key, None)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reap_once_pops_and_tears_down_unattached_exited_kernel(self):
        from tools.code_kernel import SessionKernel, _KERNELS, _REGISTRY, _reap_once

        kernel = SessionKernel(("dead-idle", "project", "python", "/tmp", ()))
        proc = _DeadProc()
        kernel.proc = proc  # type: ignore[assignment]
        kernel.attached = 0
        tmp = tempfile.mkdtemp(prefix="reap_idle_")
        kernel.tmpdir = tmp
        with _REGISTRY.lock:
            _KERNELS[kernel.key] = kernel
        try:
            _reap_once()
            self.assertGreaterEqual(proc.polls, 1)
            self.assertNotIn(kernel.key, _KERNELS)
            self.assertTrue(kernel.stop_event.is_set())
            self.assertFalse(os.path.exists(tmp))
        finally:
            with _REGISTRY.lock:
                _KERNELS.pop(kernel.key, None)
            shutil.rmtree(tmp, ignore_errors=True)
