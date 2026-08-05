import asyncio
import os
import discord
from discord.ext import commands
import yt_dlp as youtube_dl

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DEFAULT_PREFIX = "m"
# -----------------------------

if not BOT_TOKEN:
    raise RuntimeError(
        "DISCORD_BOT_TOKEN environment variable set kora nei. "
        "Railway-e Variables tab-e giye add koro."
    )

intents = discord.Intents.default()
intents.message_content = True

# Prottek server-er jonno alada prefix
prefixes = {}


def get_prefix(bot, message):
    if message.guild is None:
        return DEFAULT_PREFIX
    return prefixes.get(message.guild.id, DEFAULT_PREFIX)


bot = commands.Bot(command_prefix=get_prefix, intents=intents)

ytdl_format_options = {
    "format": "bestaudio[abr>0]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    },
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

# Prottek server-er jonno alada queue
queues = {}
# Prottek server-er jonno alada volume level (default 0.5 = 50%)
volumes = {}
# Prottek server-er jonno alada bass boost level (default 8)
bass_levels = {}


def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = []
    return queues[guild_id]


def get_volume(guild_id):
    if guild_id not in volumes:
        volumes[guild_id] = 0.5
    return volumes[guild_id]


def get_bass(guild_id):
    if guild_id not in bass_levels:
        bass_levels[guild_id] = 8
    return bass_levels[guild_id]


def get_ffmpeg_options(guild_id):
    bass = get_bass(guild_id)
    return {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": f"-vn -b:a 320k -af bass=g={bass}",
    }


class Song:
    def __init__(self, source_url, title, thumbnail=None, webpage_url=None):
        self.source_url = source_url
        self.title = title
        self.thumbnail = thumbnail
        self.webpage_url = webpage_url


async def search_song(query):
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(
        None, lambda: ytdl.extract_info(query, download=False)
    )
    if "entries" in data:
        data = data["entries"][0]
    return Song(
        data["url"],
        data.get("title", "Unknown"),
        thumbnail=data.get("thumbnail"),
        webpage_url=data.get("webpage_url"),
    )


class MusicControls(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.ctx.voice_client
        if vc and vc.is_playing():
            vc.pause()
            button.label = "Resume"
            button.style = discord.ButtonStyle.success
            await interaction.response.edit_message(view=self)
        elif vc and vc.is_paused():
            vc.resume()
            button.label = "Pause"
            button.style = discord.ButtonStyle.secondary
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.send_message("Ekhon kichu bajche na.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.ctx.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message("Skip kora holo.", ephemeral=True)
        else:
            await interaction.response.send_message("Ekhon kichu bajche na.", ephemeral=True)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        import random
        queue = get_queue(self.ctx.guild.id)
        if not queue:
            await interaction.response.send_message("Queue khali.", ephemeral=True)
            return
        random.shuffle(queue)
        await interaction.response.send_message("Queue shuffle kora holo.", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue = get_queue(self.ctx.guild.id)
        queue.clear()
        vc = self.ctx.voice_client
        if vc:
            vc.stop()
        await interaction.response.send_message("Stop kora holo, queue clear.", ephemeral=True)


def play_next(ctx):
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    voice_client = ctx.voice_client

    if not queue or voice_client is None:
        return

    song = queue.pop(0)
    raw_source = discord.FFmpegPCMAudio(song.source_url, **get_ffmpeg_options(guild_id))
    source = discord.PCMVolumeTransformer(raw_source, volume=get_volume(guild_id))

    def after_play(error):
        if error:
            print(f"Playback error: {error}")
        play_next(ctx)

    voice_client.play(source, after=after_play)

    embed = discord.Embed(
        title="Now Playing",
        description=f"[{song.title}]({song.webpage_url})" if song.webpage_url else song.title,
        color=discord.Color.green(),
    )
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)

    view = MusicControls(ctx)
    asyncio.run_coroutine_threadsafe(
        ctx.send(embed=embed, view=view), bot.loop
    )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.command(name="setprefix")
@commands.has_permissions(administrator=True)
async def setprefix(ctx, new_prefix: str):
    if len(new_prefix) > 5:
        await ctx.send("Prefix beshi boro hoye gelo, choto kichu dao (max 5 character).")
        return
    prefixes[ctx.guild.id] = new_prefix
    await ctx.send(f"Prefix change kora holo: **{new_prefix}**")


@setprefix.error
async def setprefix_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Eta korar jonno tomar Admin permission lagbe.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Notun prefix dite hobe, jemon: `!setprefix ?`")


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

    embed = discord.Embed(
        title="Queue-te jog holo",
        description=f"[{song.title}]({song.webpage_url})" if song.webpage_url else song.title,
        color=discord.Color.blurple(),
    )
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)
    await ctx.send(embed=embed)

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


@bot.command(name="bass")
async def bass(ctx, level: int = None):
    guild_id = ctx.guild.id
    if level is None:
        await ctx.send(f"Ekhon bass boost: **{get_bass(guild_id)}**")
        return

    if level < 0 or level > 20:
        await ctx.send("Bass level 0 theke 20-er moddhe dao (default 8).")
        return

    bass_levels[guild_id] = level
    await ctx.send(f"Bass boost set kora holo: **{level}** — notun gaan theke effect ashbe.")


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
