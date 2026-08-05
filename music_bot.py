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
# Prottek server-e ekhon ja bajche tar tracking: {guild_id: {"song": Song, "started_at": time.time(), "seek_offset": int}}
now_playing = {}


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
    def __init__(self, source_url, title, thumbnail=None, webpage_url=None, duration=0):
        self.source_url = source_url
        self.title = title
        self.thumbnail = thumbnail
        self.webpage_url = webpage_url
        self.duration = duration  # seconds


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
        duration=data.get("duration", 0),
    )


def format_time(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def parse_time(text):
    # "1:30" -> 90, "90" -> 90
    parts = text.split(":")
    parts = [int(p) for p in parts]
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


class SeekModal(discord.ui.Modal, title="Gaan-er kono jaygay jete chao?"):
    position = discord.ui.TextInput(
        label="Shomoy (mm:ss ba shudhu seconds)",
        placeholder="jemon: 1:30",
        required=True,
        max_length=10,
    )

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = self.ctx.guild.id
        info = now_playing.get(guild_id)
        if not info or not self.ctx.voice_client:
            await interaction.response.send_message("Ekhon kichu bajche na.", ephemeral=True)
            return
        try:
            seconds = parse_time(self.position.value)
        except ValueError:
            await interaction.response.send_message("Shomoy thik moto dao, jemon `1:30`.", ephemeral=True)
            return

        song = info["song"]
        if song.duration and seconds > song.duration:
            await interaction.response.send_message(
                f"Gaan-er duration {format_time(song.duration)}, eto boro shomoy-e jete parba na.",
                ephemeral=True,
            )
            return
        if seconds < 0:
            seconds = 0

        info["manual_stop"] = True
        self.ctx.voice_client.stop()
        play_song(self.ctx, song, seek_seconds=seconds)
        await interaction.response.send_message(f"Jump kora holo: **{format_time(seconds)}**", ephemeral=True)


def make_progress_bar(elapsed, duration, length=20):
    if not duration:
        return "▬" * length
    filled = int((elapsed / duration) * length)
    filled = max(0, min(length, filled))
    bar = "▬" * filled + "🔵" + "▬" * (length - filled - 1)
    return bar


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

    @discord.ui.button(label="Seek", style=discord.ButtonStyle.secondary, emoji="🎯")
    async def seek_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SeekModal(self.ctx))

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        queue = get_queue(guild_id)
        queue.clear()
        if guild_id in now_playing:
            now_playing[guild_id]["manual_stop"] = True
        vc = self.ctx.voice_client
        if vc:
            vc.stop()
        await interaction.response.send_message("Stop kora holo, queue clear.", ephemeral=True)


def get_elapsed(guild_id):
    import time
    info = now_playing.get(guild_id)
    if not info:
        return 0
    return info["seek_offset"] + (time.time() - info["started_at"])


def play_song(ctx, song, seek_seconds=0):
    """Play a specific song from a specific position (seconds)."""
    import time
    guild_id = ctx.guild.id
    voice_client = ctx.voice_client

    opts = get_ffmpeg_options(guild_id)
    if seek_seconds > 0:
        opts = dict(opts)
        opts["before_options"] = f"-ss {seek_seconds} " + opts["before_options"]

    raw_source = discord.FFmpegPCMAudio(song.source_url, **opts)
    source = discord.PCMVolumeTransformer(raw_source, volume=get_volume(guild_id))

    now_playing[guild_id] = {
        "song": song,
        "started_at": time.time(),
        "seek_offset": seek_seconds,
    }

    def after_play(error):
        if error:
            print(f"Playback error: {error}")
        # Jodi user seek/restart korche tahole eikhane notun kore queue theke pull hobe na
        if now_playing.get(guild_id, {}).get("song") is song and not now_playing[guild_id].get("manual_stop"):
            play_next(ctx)

    voice_client.play(source, after=after_play)

    embed = discord.Embed(
        title="Now Playing",
        description=f"[{song.title}]({song.webpage_url})" if song.webpage_url else song.title,
        color=discord.Color.green(),
    )
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)
    if song.duration:
        embed.add_field(name="Duration", value=format_time(song.duration))
        embed.add_field(name="Shuru hocche", value=format_time(seek_seconds))

    view = MusicControls(ctx)
    asyncio.run_coroutine_threadsafe(
        ctx.send(embed=embed, view=view), bot.loop
    )


def play_next(ctx):
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    voice_client = ctx.voice_client

    if not queue or voice_client is None:
        return

    song = queue.pop(0)
    play_song(ctx, song, seek_seconds=0)


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


@bot.command(name="seek")
async def seek(ctx, position: str):
    guild_id = ctx.guild.id
    info = now_playing.get(guild_id)
    if not info or not ctx.voice_client or not (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        await ctx.send("Ekhon kichu bajche na.")
        return

    try:
        seconds = parse_time(position)
    except ValueError:
        await ctx.send("Shomoy thik moto dao, jemon: `mseek 1:30` ba `mseek 90`")
        return

    song = info["song"]
    if song.duration and seconds > song.duration:
        await ctx.send(f"Gaan-er duration {format_time(song.duration)}, eto boro shomoy-e jete parba na.")
        return
    if seconds < 0:
        seconds = 0

    info["manual_stop"] = True
    ctx.voice_client.stop()
    play_song(ctx, song, seek_seconds=seconds)


@bot.command(name="restart")
async def restart(ctx):
    guild_id = ctx.guild.id
    info = now_playing.get(guild_id)
    if not info or not ctx.voice_client:
        await ctx.send("Ekhon kichu bajche na.")
        return
    song = info["song"]
    info["manual_stop"] = True
    ctx.voice_client.stop()
    play_song(ctx, song, seek_seconds=0)


@bot.command(name="forward")
async def forward(ctx, seconds: int = 10):
    guild_id = ctx.guild.id
    info = now_playing.get(guild_id)
    if not info or not ctx.voice_client:
        await ctx.send("Ekhon kichu bajche na.")
        return
    new_pos = int(get_elapsed(guild_id)) + seconds
    song = info["song"]
    if song.duration and new_pos > song.duration:
        new_pos = song.duration
    info["manual_stop"] = True
    ctx.voice_client.stop()
    play_song(ctx, song, seek_seconds=new_pos)


@bot.command(name="rewind")
async def rewind(ctx, seconds: int = 10):
    guild_id = ctx.guild.id
    info = now_playing.get(guild_id)
    if not info or not ctx.voice_client:
        await ctx.send("Ekhon kichu bajche na.")
        return
    new_pos = int(get_elapsed(guild_id)) - seconds
    if new_pos < 0:
        new_pos = 0
    song = info["song"]
    info["manual_stop"] = True
    ctx.voice_client.stop()
    play_song(ctx, song, seek_seconds=new_pos)


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
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    queue.clear()
    if guild_id in now_playing:
        now_playing[guild_id]["manual_stop"] = True
    if ctx.voice_client:
        ctx.voice_client.stop()
    await ctx.send("Stop kora holo, queue clear.")


@bot.command(name="leave")
async def leave(ctx):
    guild_id = ctx.guild.id
    if guild_id in now_playing:
        now_playing[guild_id]["manual_stop"] = True
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
    guild_id = ctx.guild.id
    info = now_playing.get(guild_id)
    if not info or not ctx.voice_client or not (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        await ctx.send("Ekhon kichu bajche na.")
        return

    song = info["song"]
    elapsed = get_elapsed(guild_id)
    if song.duration:
        elapsed = min(elapsed, song.duration)
    bar = make_progress_bar(elapsed, song.duration)

    embed = discord.Embed(
        title="Now Playing",
        description=f"[{song.title}]({song.webpage_url})" if song.webpage_url else song.title,
        color=discord.Color.green(),
    )
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)
    duration_text = format_time(song.duration) if song.duration else "?"
    embed.add_field(
        name="Progress",
        value=f"{format_time(elapsed)} {bar} {duration_text}",
        inline=False,
    )
    await ctx.send(embed=embed, view=MusicControls(ctx))


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
.guild.id)
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
