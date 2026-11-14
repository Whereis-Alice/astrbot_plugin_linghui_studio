"""A cancellable child request must return its own route metrics to the caller."""

import ast
import asyncio
from contextvars import ContextVar
from pathlib import Path
import types
from typing import Any, Dict, List
import unittest
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]


class TaskRouteMetricsTest(unittest.IsolatedAsyncioTestCase):
    def plugin(self, generate):
        source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "LinghuiStudioPlugin")
        method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_call_generation_api_task")
        namespace = {"asyncio": asyncio, "Any": Any, "Dict": Dict, "List": List}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(ROOT / "main.py"), "exec"), namespace)
        context = ContextVar("test_route_metrics", default=None)
        shared = {}

        def set_metrics(metrics):
            shared.clear()
            shared.update(metrics)
            context.set(dict(metrics))

        def get_metrics():
            current = context.get()
            return dict(current if isinstance(current, dict) else shared)

        async def call_api(images, prompt, model, *args, **kwargs):
            set_metrics({})
            return await generate(prompt, set_metrics)

        plugin = types.SimpleNamespace(
            api_mgr=types.SimpleNamespace(call_api=call_api, get_last_metrics=get_metrics, _set_last_metrics=set_metrics),
            img_mgr=types.SimpleNamespace(proxy=None),
            task_mgr=types.SimpleNamespace(attach_runtime_task=AsyncMock(), detach_runtime_task=AsyncMock()),
        )
        plugin.generate = types.MethodType(namespace["_call_generation_api_task"], plugin)
        return plugin

    async def test_concurrent_child_routes_replace_stale_parent_metrics_without_crossing(self):
        barrier = asyncio.Event()
        entered = []

        async def generate(prompt, publish):
            entered.append(prompt)
            if len(entered) == 2:
                barrier.set()
            await barrier.wait()
            publish({"channel_id": prompt, "attempt_chain": [{"channel_id": prompt, "success": True}]})
            await asyncio.sleep(0)
            return prompt.encode()

        plugin = self.plugin(generate)

        async def request(channel):
            plugin.api_mgr._set_last_metrics({"channel_id": "previous-" + channel})
            result = await plugin.generate(channel, [], channel, "")
            return result, plugin.api_mgr.get_last_metrics()

        results = await asyncio.gather(request("miku"), request("orb"))
        for channel, (result, metrics) in zip(("miku", "orb"), results):
            self.assertEqual(result, channel.encode())
            self.assertEqual(metrics["channel_id"], channel)
            self.assertEqual(metrics["attempt_chain"][0]["channel_id"], channel)
        self.assertEqual(plugin.task_mgr.detach_runtime_task.await_count, 2)

    async def test_exception_and_cancellation_publish_child_metrics_and_detach(self):
        for cancellation in (False, True):
            with self.subTest(cancellation=cancellation):
                started = asyncio.Event()

                async def generate(prompt, publish):
                    publish({"channel_id": "orb", "error_category": "timeout"})
                    started.set()
                    if cancellation:
                        await asyncio.Event().wait()
                    raise ValueError("fixture failure")

                plugin = self.plugin(generate)

                async def request():
                    plugin.api_mgr._set_last_metrics({"channel_id": "stale"})
                    try:
                        await plugin.generate("task", [], "portrait", "")
                    finally:
                        self.assertEqual(plugin.api_mgr.get_last_metrics()["channel_id"], "orb")

                task = asyncio.create_task(request())
                await started.wait()
                if cancellation:
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancellation else ValueError):
                    await task
                plugin.task_mgr.detach_runtime_task.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
