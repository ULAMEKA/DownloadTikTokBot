import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from routers.inline_router import (
    INLINE_EMPTY_QUERY_HINT,
    INLINE_UNSUPPORTED_HINT,
    handle_inline_query,
)


class InlineHintTests(unittest.IsolatedAsyncioTestCase):
    async def _answer_for(self, text: str):
        query = SimpleNamespace(query=text, answer=AsyncMock())
        await handle_inline_query(query)
        query.answer.assert_awaited_once()
        return query.answer.await_args

    async def test_empty_query_shows_link_hint(self):
        call = await self._answer_for("   ")

        self.assertEqual(call.args[0], [])
        self.assertEqual(call.kwargs["button"].text, INLINE_EMPTY_QUERY_HINT)

    async def test_unsupported_query_shows_download_error_hint(self):
        call = await self._answer_for("просто текст")

        self.assertEqual(call.args[0], [])
        self.assertEqual(call.kwargs["button"].text, INLINE_UNSUPPORTED_HINT)

    async def test_partially_typed_link_keeps_input_hint(self):
        call = await self._answer_for("https://vm.tik")

        self.assertEqual(call.kwargs["button"].text, INLINE_EMPTY_QUERY_HINT)

    async def test_profile_link_is_reported_as_unsupported(self):
        call = await self._answer_for("https://www.tiktok.com/@user")

        self.assertEqual(call.args[0], [])
        self.assertEqual(call.kwargs["button"].text, INLINE_UNSUPPORTED_HINT)

    async def test_video_link_shows_send_menu_without_hint(self):
        call = await self._answer_for("https://www.tiktok.com/@user/video/1234567890")

        self.assertNotIn("button", call.kwargs)
        self.assertEqual(call.kwargs["results"][0].id, "download")


if __name__ == "__main__":
    unittest.main()
