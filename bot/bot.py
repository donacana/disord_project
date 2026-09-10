import logging
import re
from typing import Any, Awaitable, Callable

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from config import (
    API_BASE_URL,
    API_TIMEOUT_SECONDS,
    DISCORD_TOKEN,
    validate_config,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)

SendMessage = Callable[..., Awaitable[Any]]


def split_message(text: str, limit: int = 1900) -> list[str]:
    """디스코드 메시지 길이 제한에 맞게 답변을 나눈다."""
    if len(text) <= limit:
        return [text]

    messages: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            messages.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at == -1:
            split_at = limit

        messages.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    return messages


def format_sources(sources: list[dict[str, Any]]) -> str:
    if not sources:
        return ""

    lines = ["", "**출처**"]
    for index, source in enumerate(sources, start=1):
        title = source.get("title") or "제목 없음"
        url = source.get("url") or ""
        collected_at = source.get("collected_at")

        if url:
            lines.append(f"{index}. [{title}](<{url}>)")
        else:
            lines.append(f"{index}. {title}")
        if collected_at:
            lines.append(f"   수집 시각: {collected_at}")

    return "\n".join(lines)


async def request_api(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    async with httpx.AsyncClient(
        base_url=API_BASE_URL,
        timeout=API_TIMEOUT_SECONDS,
    ) as client:
        response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()


async def send_long_message(send: SendMessage, message: str) -> None:
    for part in split_message(message):
        await send(
            part,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def extract_mention_question(message: discord.Message) -> str:
    """Remove only this bot's mention; preserve mentions of other users."""
    if bot.user is None:
        return message.content.strip()
    return re.sub(rf"<@!?{bot.user.id}>", "", message.content).strip()


async def process_question(
    question: str,
    send: SendMessage,
    typing=None,
) -> None:
    """Call Backend POST /ask and preserve the existing Discord output."""
    question = question.strip()
    if not question:
        await send("질문을 입력해주세요.")
        return

    async def request_and_send() -> None:
        try:
            data = await request_api(
                "POST",
                "/ask",
                json={"question": question, "top_k": 5},
            )
        except httpx.TimeoutException:
            await send("답변 생성 시간이 초과되었습니다. 잠시 후 다시 시도해주세요.")
            return
        except httpx.ConnectError:
            await send(
                "백엔드 서버에 연결할 수 없습니다. "
                "FastAPI가 실행 중인지 확인해주세요."
            )
            return
        except httpx.HTTPStatusError as error:
            logger.exception("API HTTP error")
            await send(f"API 요청에 실패했습니다. 상태 코드: {error.response.status_code}")
            return
        except (httpx.RequestError, ValueError):
            logger.exception("API request error")
            await send("답변을 불러오는 중 오류가 발생했습니다.")
            return

        answer = data.get("answer") or "답변이 없습니다."
        domain = data.get("domain") or "unknown"
        sources = data.get("sources") or []
        result = (
            f"**답변**\n{answer}"
            f"{format_sources(sources)}"
            f"\n\n`분야: {domain}`"
        )
        await send_long_message(send, result)

    if typing is None:
        await request_and_send()
    else:
        async with typing():
            await request_and_send()


async def sync_application_commands() -> None:
    synced = await bot.tree.sync()
    logger.info("Discord slash commands synced: %d", len(synced))


@bot.event
async def setup_hook() -> None:
    await sync_application_commands()


@bot.event
async def on_ready() -> None:
    if bot.user is None:
        return

    logger.info("Discord bot connected: %s (%s)", bot.user, bot.user.id)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    if bot.user is not None and bot.user in message.mentions:
        question = extract_mention_question(message)
        await process_question(question, message.channel.send, message.channel.typing)
        return

    await bot.process_commands(message)


@bot.tree.command(name="ask", description="연예·문화 질문을 보냅니다")
@app_commands.describe(question="질문 내용")
async def ask_slash(interaction: discord.Interaction, question: str) -> None:
    await interaction.response.defer()
    await process_question(question, interaction.followup.send)


@bot.command(name="연예질물", aliases=["연예질문"])
async def entertainment_question_command(
    ctx: commands.Context,
    *,
    question: str,
) -> None:
    await process_question(question, ctx.send, ctx.typing)


@bot.command(name="health")
async def health_command(ctx: commands.Context) -> None:
    """FastAPI와 데이터베이스 상태를 확인한다."""
    try:
        data = await request_api("GET", "/health")
    except httpx.RequestError:
        await ctx.send("❌ 백엔드 서버에 연결할 수 없습니다.")
        return
    except httpx.HTTPStatusError as error:
        await ctx.send(f"❌ 상태 확인 실패: HTTP {error.response.status_code}")
        return
    except ValueError:
        await ctx.send("❌ 서버 응답 형식이 올바르지 않습니다.")
        return

    await ctx.send(
        "✅ 서버 상태\n"
        f"- API: `{data.get('api', 'unknown')}`\n"
        f"- Database: `{data.get('database', 'unknown')}`"
    )


@bot.command(name="stats")
async def stats_command(ctx: commands.Context) -> None:
    """수집된 기사와 임베딩 통계를 확인한다."""
    try:
        data = await request_api("GET", "/stats")
    except httpx.RequestError:
        await ctx.send("❌ 백엔드 서버에 연결할 수 없습니다.")
        return
    except httpx.HTTPStatusError as error:
        await ctx.send(f"❌ 통계 조회 실패: HTTP {error.response.status_code}")
        return
    except ValueError:
        await ctx.send("❌ 서버 응답 형식이 올바르지 않습니다.")
        return

    await ctx.send(
        "**데이터 현황**\n"
        f"- 기사: `{data.get('total_articles', 0)}`건\n"
        f"- 임베딩: `{data.get('total_embeddings', 0)}`건\n"
        f"- 수집처: `{data.get('source_count', 0)}`개\n"
        f"- 마지막 수집: `{data.get('last_collected_at') or '없음'}`"
    )


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    await ctx.send(
        "**사용 가능한 명령어**\n"
        "- `@봇 질문 내용` : 기사 기반 질문\n"
        "- `/ask 질문 내용` : 기사 기반 질문\n"
        "- `!연예질물 질문 내용` : 기사 기반 질문\n"
        "- `!health` : 서버 상태 확인\n"
        "- `!stats` : 수집 데이터 현황 확인\n"
        "- `!help` : 명령어 안내"
    )


@entertainment_question_command.error
async def entertainment_question_command_error(
    ctx: commands.Context,
    error: commands.CommandError,
) -> None:
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            "질문을 함께 입력해주세요.\n"
            "예: `!연예질물 최근 연예계 주요 이슈를 알려줘`"
        )
        return

    logger.exception("Discord command error", exc_info=error)
    await ctx.send("명령어 처리 중 오류가 발생했습니다.")


def main() -> None:
    validate_config()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
