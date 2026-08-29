import unittest
from unittest.mock import AsyncMock, patch

import promo
import services.queue as queue
from db.session import engine
from utils.logging_config import configure_logging


class RuntimeConfigTests(unittest.TestCase):
    def test_uncached_ad_delay_is_five_seconds(self):
        self.assertEqual(promo.AD_DISPLAY_SECONDS, 5)

    def test_uncached_placeholder_requests_a_five_second_deadline(self):
        class LoopStub:
            def time(self):
                return 100.0

        queue._placeholder_ready_at.clear()
        with patch.object(queue.asyncio, "get_running_loop", return_value=LoopStub()):
            queue.hold_placeholder("123", 456)

        self.assertEqual(queue._placeholder_ready_at[("123", 456)], 105.0)

    def test_sql_echo_is_disabled(self):
        self.assertFalse(engine.echo)

    @patch("utils.logging_config.logging.basicConfig")
    def test_bot_log_uses_bounded_rotation(self, basic_config):
        configure_logging()
        handlers = basic_config.call_args.kwargs["handlers"]
        try:
            rotating = next(handler for handler in handlers if hasattr(handler, "maxBytes"))
            self.assertEqual(rotating.maxBytes, 45_000_000)
            self.assertEqual(rotating.backupCount, 1)
            self.assertEqual(rotating.encoding.lower().replace("-", ""), "utf8")
        finally:
            for handler in handlers:
                handler.close()


class CachedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_delivery_without_placeholder_requests_no_sleep(self):
        class BotStub:
            async def send_video(self, **kwargs):
                return None

        with patch.object(queue.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            result = await queue._deliver_video(
                BotStub(),
                "123",
                None,
                file_id="cached-file-id",
                video_path=None,
            )

        self.assertEqual(result, "cached-file-id")
        sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
