import threading
import time
import unittest

from utils.tasks import DuplicateTaskError, TaskCancelled, TaskManager, TaskState


class TaskManagerTests(unittest.TestCase):
    def tearDown(self):
        getattr(self, "mgr", None) and self.mgr.shutdown(timeout=2)

    def test_success_with_progress(self):
        self.mgr = TaskManager(2)
        seen = []
        self.mgr.add_listener(lambda t: seen.append((t.state, t.stage, t.progress)))

        def work(ctx):
            ctx.report("Этап 1", 0.5)
            return 42

        t = self.mgr.submit("job", work)
        self.assertTrue(t.wait(2))
        self.assertEqual((t.state, t.result), (TaskState.SUCCEEDED, 42))
        self.assertIn((TaskState.RUNNING, "Этап 1", 0.5), seen)

    def test_indeterminate_progress_is_none(self):
        self.mgr = TaskManager(1)
        t = self.mgr.submit("job", lambda ctx: ctx.report("Загрузка"))
        t.wait(2)
        self.assertIsNone(t.progress)

    def test_exception_captured_without_secret(self):
        self.mgr = TaskManager(1)

        def boom(ctx):
            raise RuntimeError("fail PWD=abc123")

        t = self.mgr.submit("job", boom)
        t.wait(2)
        self.assertEqual(t.state, TaskState.FAILED)
        self.assertNotIn("abc123", t.error)

    def test_parallel_limit(self):
        self.mgr = TaskManager(2)
        lock = threading.Lock()
        cur = {"n": 0, "max": 0}

        def work(ctx):
            with lock:
                cur["n"] += 1
                cur["max"] = max(cur["max"], cur["n"])
            time.sleep(0.05)
            with lock:
                cur["n"] -= 1

        tasks = [self.mgr.submit(f"t{i}", work) for i in range(8)]
        for t in tasks:
            self.assertTrue(t.wait(5))
        self.assertEqual(cur["max"], 2)
        self.assertTrue(all(t.state is TaskState.SUCCEEDED for t in tasks))

    def test_queued_task_can_be_cancelled(self):
        self.mgr = TaskManager(1)
        gate = threading.Event()
        first = self.mgr.submit("first", lambda ctx: gate.wait(3))
        second = self.mgr.submit("second", lambda ctx: "never")
        self.assertEqual(second.state, TaskState.QUEUED)
        self.assertTrue(self.mgr.cancel(second.id))
        gate.set()
        first.wait(2)
        self.assertEqual(second.state, TaskState.CANCELLED)
        self.assertIsNone(second.result)

    def test_running_task_cooperative_cancel(self):
        self.mgr = TaskManager(1)
        started = threading.Event()

        def work(ctx):
            started.set()
            for _ in range(200):
                ctx.check_cancelled()
                time.sleep(0.01)

        t = self.mgr.submit("loop", work)
        started.wait(2)
        self.assertTrue(self.mgr.cancel(t.id))
        self.assertTrue(t.wait(2))
        self.assertEqual(t.state, TaskState.CANCELLED)

    def test_on_cancel_hook_called_for_running(self):
        self.mgr = TaskManager(1)
        called = threading.Event()
        started = threading.Event()

        def work(ctx):
            started.set()
            ctx.cancel_token.wait(2)
            raise TaskCancelled()

        t = self.mgr.submit("q", work, on_cancel=called.set)
        started.wait(2)
        self.mgr.cancel(t.id)
        t.wait(2)
        self.assertTrue(called.is_set())

    def test_duplicate_key_rejected_until_finished(self):
        self.mgr = TaskManager(1)
        gate = threading.Event()
        t = self.mgr.submit("a", lambda ctx: gate.wait(3), key="report:1")
        with self.assertRaises(DuplicateTaskError):
            self.mgr.submit("a2", lambda ctx: None, key="report:1")
        gate.set()
        t.wait(2)
        t2 = self.mgr.submit("a3", lambda ctx: None, key="report:1")
        self.assertTrue(t2.wait(2))

    def test_recorder_called_for_every_final_state(self):
        recorded = []
        self.mgr = TaskManager(1, recorder=lambda t: recorded.append(t.state))
        gate = threading.Event()
        a = self.mgr.submit("a", lambda ctx: gate.wait(3))
        b = self.mgr.submit("b", lambda ctx: None)
        self.mgr.cancel(b.id)
        gate.set()
        a.wait(2)
        time.sleep(0.05)
        self.assertCountEqual(recorded, [TaskState.SUCCEEDED, TaskState.CANCELLED])

    def test_shutdown_cancels_and_joins(self):
        self.mgr = TaskManager(1)
        started = threading.Event()

        def work(ctx):
            started.set()
            while True:
                ctx.check_cancelled()
                time.sleep(0.01)

        t = self.mgr.submit("loop", work)
        started.wait(2)
        self.mgr.shutdown(timeout=2)
        self.assertEqual(t.state, TaskState.CANCELLED)
        with self.assertRaises(RuntimeError):
            self.mgr.submit("late", lambda ctx: None)


if __name__ == "__main__":
    unittest.main()
