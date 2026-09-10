import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import bot, extract_mention_question, process_question


class BotCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_question_commands_and_legacy_ask(self):
        self.assertIsNone(bot.get_command("ask"))
        self.assertIsNotNone(bot.get_command("연예질물"))
        self.assertIsNotNone(bot.get_command("연예질문"))
        self.assertIsNotNone(bot.tree.get_command("ask"))
        self.assertIsNotNone(bot.get_command("health"))
        self.assertIsNotNone(bot.get_command("stats"))

    def test_only_own_mention_is_removed(self):
        user = SimpleNamespace(id=12345)
        message = SimpleNamespace(
            content="<@12345> 장원영 <@67890> 최근 활동 알려줘",
        )
        with patch.object(bot._connection, "user", user):
            self.assertEqual(
                extract_mention_question(message),
                "장원영 <@67890> 최근 활동 알려줘",
            )

    async def test_three_entry_points_share_backend_response_format(self):
        senders = [AsyncMock(), AsyncMock(), AsyncMock()]
        backend_response = {
            "answer": "장원영의 최근 활동입니다.",
            "sources": [{"title": "기사", "url": "https://example.com/1"}],
            "domain": "ent_culture",
        }
        questions = [
            "요즘 어떤 아이돌이 유명해?",
            "요즘 어떤 아이돌이 유명해?",
            "요즘 어떤 아이돌이 유명해?",
        ]
        with patch("bot.request_api", new=AsyncMock(return_value=backend_response)) as request:
            for question, sender in zip(questions, senders):
                await process_question(question, sender)

        self.assertEqual(request.await_count, 3)
        for sender in senders:
            output = sender.await_args.args[0]
            self.assertIn("**답변**", output)
            self.assertIn("**출처**", output)
            self.assertIn("`분야: ent_culture`", output)

    async def test_empty_question_is_not_sent_to_backend(self):
        send = AsyncMock()
        with patch("bot.request_api", new=AsyncMock()) as request:
            await process_question("  ", send)
        request.assert_not_awaited()
        send.assert_awaited_once_with("질문을 입력해주세요.")


if __name__ == "__main__":
    unittest.main()
