import asyncio
import os
import discord
from discord.ext import commands
import yt_dlp as youtube_dl

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
COMMAND_PREFIX = "!"
# -----------------------------

if not BOT_TOKEN:
    raise RuntimeError(
        "DISCORD_BOT_TOKEN environment variable set kora nei. "
        "Railway-e Variables tab-e giye add koro."
    )

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

ytdl_format_options = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

ffmpeg_options = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

# Prottek server-er jonno alada queue
queues = {}
# Prottek server-er jonno alada volume level (default 0.5 = 50%)
volumes = {}


def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = []
    return queues[guild_id]


def get_volume(guild_id):
    if guild_id not in volumes:
        volumes[guild_id] = 0.5
    return volumes[guild_id]


class Song:
    def __init__(self, source_url, title):
        self.source_url = source_url
        self.title = title


async def search_song(query):
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(
        None, lambda: ytdl.extract_info(query, download=False)
    )
    if "entries" in data:
        data = data["entries"][0]
    return Song(data["url"], data.get("title", "Unknown"))


def play_next(ctx):
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    voice_client = ctx.voice_client

    if not queue or voice_client is None:
        return

    song = queue.pop(0)
    raw_source = discord.FFmpegPCMAudio(song.source_url, **ffmpeg_options)
    source = discord.PCMVolumeTransformer(raw_source, volume=get_volume(guild_id))

    def after_play(error):
        if error:
            print(f"Playback error: {error}")
        play_next(ctx)

    voice_client.play(source, after=after_play)
    asyncio.run_coroutine_threadsafe(
        ctx.send(f"Ekhon bajche: **{song.title}**"), bot.loop
    )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.command(name="join")
async def join(ctx):
    if ctx.author.voice is None:
        await ctx.send("Age ekta voice channel e join koro.")
        return
    channel = ctx.author.voice.channel
    if ctx.voice_client is None:
        await channel.connect()
    else:
        await ctx.voice_client.move_to(channel)
    await ctx.send(f"Joined **{channel.name}**")


@bot.command(name="play")
async def play(ctx, *, query: str):
    if ctx.author.voice is None:
        await ctx.send("Age voice channel e join koro.")
        return

    if ctx.voice_client is None:
        await ctx.author.voice.channel.connect()

    await ctx.send(f"Khujchi: **{query}** ...")
    try:
        song = await search_song(query)
    except Exception as e:
        await ctx.send(f"Song khunje pawa jaini: {e}")
        return

    queue = get_queue(ctx.guild.id)
    queue.append(song)
    await ctx.send(f"Queue-te jog holo: **{song.title}**")

    if not ctx.voice_client.is_playing():
        play_next(ctx)


@bot.command(name="skip")
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("Skip kora holo.")
    else:
        await ctx.send("Ekhon kichu bajche na.")


@bot.command(name="pause")
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("Pause kora holo.")


@bot.command(name="resume")
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("Abar chalu kora holo.")


@bot.command(name="stop")
async def stop(ctx):
    queue = get_queue(ctx.guild.id)
    queue.clear()
    if ctx.voice_client:
        ctx.voice_client.stop()
    await ctx.send("Stop kora holo, queue clear.")


@bot.command(name="leave")
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Voice channel theke ber hoye gelam.")


@bot.command(name="volume")
async def volume(ctx, level: int = None):
    guild_id = ctx.guild.id
    if level is None:
        current = int(get_volume(guild_id) * 100)
        await ctx.send(f"Ekhon volume: **{current}%**")
        return

    if level < 0 or level > 100:
        await ctx.send("Volume 0 theke 100-er moddhe dite hobe.")
        return

    volumes[guild_id] = level / 100

    if ctx.voice_client and ctx.voice_client.source:
        ctx.voice_client.source.volume = level / 100

    await ctx.send(f"Volume set kora holo: **{level}%**")


@bot.command(name="nowplaying")
async def nowplaying(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        await ctx.send("Ekhon gaan bajche.")
    else:
        await ctx.send("Ekhon kichu bajche na.")


@bot.command(name="shuffle")
async def shuffle(ctx):
    import random
    queue = get_queue(ctx.guild.id)
    if not queue:
        await ctx.send("Queue khali, shuffle korar kichu nei.")
        return
    random.shuffle(queue)
    await ctx.send("Queue shuffle kora holo.")


@bot.command(name="queue")
async def show_queue(ctx):
    queue = get_queue(ctx.guild.id)
    if not queue:
        await ctx.send("Queue khali.")
        return
    msg = "\n".join(f"{i+1}. {s.title}" for i, s in enumerate(queue))
    await ctx.send(f"**Queue:**\n{msg}")


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
