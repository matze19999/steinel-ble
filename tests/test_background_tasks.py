"""Background jobs must neither block bootstrap nor survive entry shutdown."""

import asyncio
import types
from unittest.mock import Mock

import pytest
from test_import_setup import integration as integration


@pytest.mark.parametrize("product", [7973, 7801])
def test_background_jobs_are_entry_owned_and_cancelled_on_shutdown(
    integration, tmp_path, product
):
    async def run():
        jobs = []

        def background(hass, coro, name):
            task = asyncio.create_task(coro, name=name)
            jobs.append(task)
            return task

        hass = types.SimpleNamespace(
            data={},
            config=types.SimpleNamespace(
                path=lambda *parts: str(tmp_path.joinpath(*parts))
            ),
            async_create_task=Mock(side_effect=AssertionError("Would block bootstrap")),
        )
        entry = types.SimpleNamespace(
            entry_id="test",
            options={"disconnect_when_idle": True},
            data={
                "address": "AA:BB:CC:DD:EE:FF",
                "product_id": product,
                "sensor_properties": {"12": ["motion"]},
            },
            async_create_background_task=background,
        )
        coordinator = integration.coordinator.SteinelCoordinator(hass, entry)
        started = set()

        async def forever(name):
            started.add(name)
            await asyncio.Event().wait()

        for method in (
            "_async_reconnect_monitor",
            "_async_sensor_poll_loop",
            "_async_l810_motion_poll_loop",
            "_async_idle_disconnect",
            "_async_expire_advertisement",
            "_async_persist_light_states",
        ):
            setattr(coordinator, method, lambda name=method: forever(name))
        coordinator._persist_light_states = Mock()
        coordinator._start_reconnect_monitor()
        coordinator._start_sensor_polling()
        coordinator._arm_idle_disconnect()
        coordinator._advertisement_received(
            types.SimpleNamespace(name="test", manufacturer_data={}), None
        )
        coordinator._schedule_state_persist()
        await asyncio.sleep(0)
        assert len(started) == (6 if product == 7801 else 5)
        assert all(not task.done() for task in jobs)
        hass.async_create_task.assert_not_called()
        await coordinator.async_shutdown()
        assert all(task.cancelled() for task in jobs)

    asyncio.run(run())
