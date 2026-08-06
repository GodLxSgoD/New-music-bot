import asyncio
import datetime
import os
import re
import discord
from discord.ext import commands
import yt_dlp as youtube_dl

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DEFAULT_PREFIX = "m"
# -----------------------------

if not BOT_TOKEN:
    raise RuntimeError(
        "DISCORD_BOT_TOKEN environment variable is not set. "
        "Go to Railway's Variables tab and add it."
    )

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Per-server custom prefix
prefixes = {}


def get_prefix(bot, message):
    if message.guild is None:
        return DEFAULT_PREFIX
    return prefixes.get(message.guild.id, DEFAULT_PREFIX)


bot = commands.Bot(command_prefix=get_prefix, intents=intents)
bot.remove_command("help")

ytdl_format_options = {
    "format": "bestaudio[abr>0]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web", "ios", "tv"],
        }
    },
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

# SoundCloud fallback, when YouTube blocks
sc_format_options = dict(ytdl_format_options)
sc_format_options["default_search"] = "scsearch"
sc_format_options.pop("extractor_args", None)
ytdl_sc = youtube_dl.YoutubeDL(sc_format_options)

# Used for Autoplay: fetches a YouTube "Mix"/radio playlist based on the last played video
radio_format_options = dict(ytdl_format_options)
radio_format_options["noplaylist"] = False
radio_format_options["playlistend"] = 5
ytdl_radio = youtube_dl.YoutubeDL(radio_format_options)

# Per-server song queue
queues = {}
# Per-server volume level (default 0.5 = 50%)
volumes = {}
# Per-server bass boost level (default 3)
bass_levels = {}
# Tracks what's currently playing per server
now_playing = {}
# Per-server play history (for Previous button)
history = {}
# Per-server loop mode: "off", "one", "queue"
loop_modes = {}
# Per-server autoplay toggle: True/False
autoplay_flags = {}
# Per-channel sticky message: {channel_id: {"content": str, "message": discord.Message}}
sticky_messages = {}


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
        bass_levels[guild_id] = 3
    return bass_levels[guild_id]


def get_ffmpeg_options(guild_id):
    bass = get_bass(guild_id)
    audio_filter = f"bass=g={bass},dynaudnorm=f=150:g=15"
    return {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": f"-vn -b:a 320k -af {audio_filter}",
    }


def get_history(guild_id):
    if guild_id not in history:
        history[guild_id] = []
    return history[guild_id]


def get_loop(guild_id):
    if guild_id not in loop_modes:
        loop_modes[guild_id] = "off"
    return loop_modes[guild_id]


def get_autoplay(guild_id):
    return autoplay_flags.get(guild_id, False)


class Song:
    def __init__(self, source_url, title, thumbnail=None, webpage_url=None, duration=0, source_name="YouTube"):
        self.source_url = source_url
        self.title = title
        self.thumbnail = thumbnail
        self.webpage_url = webpage_url
        self.duration = duration  # seconds
        self.source_name = source_name


async def search_song(query):
    loop = asyncio.get_event_loop()
    source_name = "YouTube"
    try:
        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(query, download=False)
        )
    except Exception:
        source_name = "SoundCloud"
        data = await loop.run_in_executor(
            None, lambda: ytdl_sc.extract_info(query, download=False)
        )
    if "entries" in data:
        data = data["entries"][0]
    return Song(
        data["url"],
        data.get("title", "Unknown"),
        thumbnail=data.get("thumbnail"),
        webpage_url=data.get("webpage_url"),
        duration=data.get("duration", 0),
        source_name=source_name,
    )


def fetch_autoplay_song(last_song):
    """Blocking call (safe to run on a background thread) that finds a related
    YouTube song to the last one played, using YouTube's Mix/Radio playlist."""
    if not last_song.webpage_url or "watch?v=" not in last_song.webpage_url:
        return None
    match = re.search(r"watch\?v=([\w-]+)", last_song.webpage_url)
    if not match:
        return None
    video_id = match.group(1)
    radio_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    try:
        data = ytdl_radio.extract_info(radio_url, download=False)
    except Exception:
        return None

    entries = data.get("entries") or []
    for entry in entries:
        if not entry:
            continue
        if entry.get("id") == video_id:
            continue  # skip the same song
        try:
            full = ytdl.extract_info(entry["url"], download=False)
        except Exception:
            continue
        return Song(
            full["url"],
            full.get("title", "Unknown"),
            thumbnail=full.get("thumbnail"),
            webpage_url=full.get("webpage_url"),
            duration=full.get("duration", 0),
            source_name="YouTube (Autoplay)",
        )
    return None


def format_time(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def parse_time(text):
    parts = text.split(":")
    parts = [int(p) for p in parts]
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


class SeekModal(discord.ui.Modal, title="Jump to a position in the song"):
    position = discord.ui.TextInput(
        label="Time (mm:ss or just seconds)",
        placeholder="e.g. 1:30",
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
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        try:
            seconds = parse_time(self.position.value)
        except ValueError:
            await interaction.response.send_message("Please enter a valid time, e.g. `1:30`.", ephemeral=True)
            return

        song = info["song"]
        if song.duration and seconds > song.duration:
            await interaction.response.send_message(
                f"The song is only {format_time(song.duration)} long, you can't seek past that.",
                ephemeral=True,
            )
            return
        if seconds < 0:
            seconds = 0

        info["manual_stop"] = True
        self.ctx.voice_client.stop()
        play_song(self.ctx, song, seek_seconds=seconds)
        await interaction.response.send_message(f"Jumped to: **{format_time(seconds)}**", ephemeral=True)


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

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=0)
    async def previous_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        hist = get_history(guild_id)
        if not hist:
            await interaction.response.send_message("No previous song.", ephemeral=True)
            return
        prev_song = hist.pop()
        current = now_playing.get(guild_id)
        if current:
            get_queue(guild_id).insert(0, current["song"])
            now_playing[guild_id]["manual_stop"] = True
        vc = self.ctx.voice_client
        if vc:
            vc.stop()
        play_song(self.ctx, prev_song, seek_seconds=0)
        await interaction.response.send_message("Playing previous song.", ephemeral=True)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, row=0)
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
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.ctx.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message("Skipped.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        import random
        queue = get_queue(self.ctx.guild.id)
        if not queue:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return
        random.shuffle(queue)
        await interaction.response.send_message("Queue shuffled.", ephemeral=True)

    @discord.ui.button(label="Loop: off", style=discord.ButtonStyle.secondary, row=1, emoji="🔁")
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        order = ["off", "queue", "one"]
        current = get_loop(guild_id)
        next_mode = order[(order.index(current) + 1) % len(order)]
        loop_modes[guild_id] = next_mode
        button.label = f"Loop: {next_mode}"
        button.style = discord.ButtonStyle.success if next_mode != "off" else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Seek", style=discord.ButtonStyle.secondary, row=1, emoji="🎯")
    async def seek_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SeekModal(self.ctx))

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, row=2)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        queue = get_queue(guild_id)
        queue.clear()
        if guild_id in now_playing:
            now_playing[guild_id]["manual_stop"] = True
        vc = self.ctx.voice_client
        if vc:
            vc.stop()
        await interaction.response.send_message("Stopped and cleared the queue.", ephemeral=True)

    @discord.ui.button(label="Disconnect", style=discord.ButtonStyle.danger, row=2)
    async def disconnect_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        get_queue(guild_id).clear()
        if guild_id in now_playing:
            now_playing[guild_id]["manual_stop"] = True
        vc = self.ctx.voice_client
        if vc:
            await vc.disconnect()
        await interaction.response.send_message("Disconnected.", ephemeral=True)

    @discord.ui.button(label="Autoplay: off", style=discord.ButtonStyle.secondary, row=2, emoji="♾️")
    async def autoplay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        new_state = not get_autoplay(guild_id)
        autoplay_flags[guild_id] = new_state
        button.label = f"Autoplay: {'on' if new_state else 'off'}"
        button.style = discord.ButtonStyle.success if new_state else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)


def get_elapsed(guild_id):
    import time
    info = now_playing.get(guild_id)
    if not info:
        return 0
    return info["seek_offset"] + (time.time() - info["started_at"])


def build_now_playing_embed(guild_id, song, elapsed=0):
    embed = discord.Embed(
        title="🎵 Now Playing",
        description=f"[{song.title}]({song.webpage_url})" if song.webpage_url else song.title,
        color=discord.Color.green(),
    )
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)

    duration_text = format_time(song.duration) if song.duration else "?"
    bar = make_progress_bar(elapsed, song.duration)
    embed.add_field(name="Progress", value=f"{format_time(elapsed)} {bar} {duration_text}", inline=False)

    queue_count = len(get_queue(guild_id))
    volume_pct = int(get_volume(guild_id) * 100)
    loop_mode = get_loop(guild_id)

    embed.add_field(name="Queue", value=str(queue_count), inline=True)
    embed.add_field(name="Volume", value=f"{volume_pct}%", inline=True)
    embed.add_field(name="Loop", value=loop_mode, inline=True)
    embed.add_field(name="Source", value=song.source_name, inline=True)
    return embed


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
        if now_playing.get(guild_id, {}).get("song") is song and not now_playing[guild_id].get("manual_stop"):
            mode = get_loop(guild_id)
            if mode == "one":
                play_song(ctx, song, seek_seconds=0)
                return
            if mode == "queue":
                get_queue(guild_id).append(song)
            get_history(guild_id).append(song)

            if not get_queue(guild_id) and get_autoplay(guild_id):
                next_song = fetch_autoplay_song(song)
                if next_song:
                    play_song(ctx, next_song, seek_seconds=0)
                    return
            play_next(ctx)

    voice_client.play(source, after=after_play)

    embed = build_now_playing_embed(guild_id, song, elapsed=seek_seconds)
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


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    channel_id = message.channel.id
    if channel_id in sticky_messages:
        old = sticky_messages[channel_id]
        try:
            await old["message"].delete()
        except Exception:
            pass
        try:
            new_msg = await message.channel.send(f"📌 {old['content']}")
            sticky_messages[channel_id]["message"] = new_msg
        except Exception:
            pass

    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to do that.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("Couldn't find that member.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        print(f"Unhandled command error: {error}")


@bot.command(name="setprefix")
@commands.has_permissions(administrator=True)
async def setprefix(ctx, new_prefix: str):
    if len(new_prefix) > 5:
        await ctx.send("That prefix is too long, please use something shorter (max 5 characters).")
        return
    prefixes[ctx.guild.id] = new_prefix
    await ctx.send(f"Prefix changed to: **{new_prefix}**")


@setprefix.error
async def setprefix_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need Administrator permission to do that.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Please provide a new prefix, e.g. `msetprefix ?`")


@bot.command(name="seek")
async def seek(ctx, position: str):
    guild_id = ctx.guild.id
    info = now_playing.get(guild_id)
    if not info or not ctx.voice_client or not (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        await ctx.send("Nothing is playing right now.")
        return

    try:
        seconds = parse_time(position)
    except ValueError:
        await ctx.send("Please enter a valid time, e.g.: `mseek 1:30` or `mseek 90`")
        return

    song = info["song"]
    if song.duration and seconds > song.duration:
        await ctx.send(f"The song is only {format_time(song.duration)} long, you can't seek past that.")
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
        await ctx.send("Nothing is playing right now.")
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
        await ctx.send("Nothing is playing right now.")
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
        await ctx.send("Nothing is playing right now.")
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
        await ctx.send("You need to join a voice channel first.")
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
        await ctx.send("You need to join a voice channel first.")
        return

    if ctx.voice_client is None:
        await ctx.author.voice.channel.connect()

    await ctx.send(f"Searching: **{query}** ...")
    try:
        song = await search_song(query)
    except Exception as e:
        await ctx.send(f"Couldn't find that song: {e}")
        return

    queue = get_queue(ctx.guild.id)
    queue.append(song)

    embed = discord.Embed(
        title="Added to Queue",
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
        await ctx.send("Skipped.")
    else:
        await ctx.send("Nothing is playing right now.")


@bot.command(name="pause")
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("Paused.")


@bot.command(name="resume")
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("Resumed.")


@bot.command(name="stop")
async def stop(ctx):
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    queue.clear()
    if guild_id in now_playing:
        now_playing[guild_id]["manual_stop"] = True
    if ctx.voice_client:
        ctx.voice_client.stop()
    await ctx.send("Stopped and cleared the queue.")


@bot.command(name="leave")
async def leave(ctx):
    guild_id = ctx.guild.id
    if guild_id in now_playing:
        now_playing[guild_id]["manual_stop"] = True
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Left the voice channel.")


@bot.command(name="bass")
async def bass(ctx, level: int = None):
    guild_id = ctx.guild.id
    if level is None:
        await ctx.send(f"Current bass boost: **{get_bass(guild_id)}**")
        return

    if level < 0 or level > 20:
        await ctx.send("Bass level must be between 0 and 20 (default 3).")
        return

    bass_levels[guild_id] = level
    await ctx.send(f"Bass boost set to: **{level}** — this will apply from the next song.")


@bot.command(name="volume")
async def volume(ctx, level: int = None):
    guild_id = ctx.guild.id
    if level is None:
        current = int(get_volume(guild_id) * 100)
        await ctx.send(f"Current volume: **{current}%**")
        return

    if level < 0 or level > 100:
        await ctx.send("Volume must be between 0 and 100.")
        return

    volumes[guild_id] = level / 100

    if ctx.voice_client and ctx.voice_client.source:
        ctx.voice_client.source.volume = level / 100

    await ctx.send(f"Volume set to: **{level}%**")


@bot.command(name="nowplaying")
async def nowplaying(ctx):
    guild_id = ctx.guild.id
    info = now_playing.get(guild_id)
    if not info or not ctx.voice_client or not (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        await ctx.send("Nothing is playing right now.")
        return

    song = info["song"]
    elapsed = get_elapsed(guild_id)
    if song.duration:
        elapsed = min(elapsed, song.duration)

    embed = build_now_playing_embed(guild_id, song, elapsed=elapsed)
    await ctx.send(embed=embed, view=MusicControls(ctx))


@bot.command(name="loop")
async def loop(ctx, mode: str = None):
    guild_id = ctx.guild.id
    if mode is None:
        await ctx.send(f"Current loop mode: **{get_loop(guild_id)}**")
        return
    mode = mode.lower()
    if mode not in ("off", "one", "queue"):
        await ctx.send("Loop mode must be one of: `off`, `one`, `queue`")
        return
    loop_modes[guild_id] = mode
    await ctx.send(f"Loop mode set to: **{mode}**")


@bot.command(name="autoplay")
async def autoplay(ctx, mode: str = None):
    guild_id = ctx.guild.id
    if mode is None:
        state = "on" if get_autoplay(guild_id) else "off"
        await ctx.send(f"Autoplay is currently: **{state}**")
        return
    mode = mode.lower()
    if mode not in ("on", "off"):
        await ctx.send("Usage: `mautoplay on` or `mautoplay off`")
        return
    autoplay_flags[guild_id] = (mode == "on")
    await ctx.send(f"Autoplay turned **{mode}**.")


@bot.command(name="previous")
async def previous(ctx):
    guild_id = ctx.guild.id
    hist = get_history(guild_id)
    if not hist:
        await ctx.send("No previous song.")
        return
    prev_song = hist.pop()
    current = now_playing.get(guild_id)
    if current:
        get_queue(guild_id).insert(0, current["song"])
        now_playing[guild_id]["manual_stop"] = True
    if ctx.voice_client:
        ctx.voice_client.stop()
    play_song(ctx, prev_song, seek_seconds=0)


@bot.command(name="shuffle")
async def shuffle(ctx):
    import random
    queue = get_queue(ctx.guild.id)
    if not queue:
        await ctx.send("The queue is empty, nothing to shuffle.")
        return
    random.shuffle(queue)
    await ctx.send("Queue shuffled.")


@bot.command(name="queue")
async def show_queue(ctx):
    queue = get_queue(ctx.guild.id)
    if not queue:
        await ctx.send("The queue is empty.")
        return
    msg = "\n".join(f"{i+1}. {s.title}" for i, s in enumerate(queue))
    await ctx.send(f"**Queue:**\n{msg}")


@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    await member.ban(reason=reason)
    await ctx.send(f"🔨 Banned **{member}**. Reason: {reason}")


@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
async def unban(ctx, *, user: str):
    banned = [entry async for entry in ctx.guild.bans()]
    target = None
    if user.isdigit():
        target = discord.utils.find(lambda b: b.user.id == int(user), banned)
    else:
        target = discord.utils.find(lambda b: str(b.user) == user or b.user.name == user, banned)

    if target is None:
        await ctx.send("Couldn't find that user in the ban list.")
        return

    await ctx.guild.unban(target.user)
    await ctx.send(f"✅ Unbanned **{target.user}**.")


@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    await member.kick(reason=reason)
    await ctx.send(f"👢 Kicked **{member}**. Reason: {reason}")


@bot.command(name="timeout")
@commands.has_permissions(moderate_members=True)
async def timeout(ctx, member: discord.Member, minutes: int, *, reason="No reason provided"):
    duration = datetime.timedelta(minutes=minutes)
    await member.timeout(duration, reason=reason)
    await ctx.send(f"🔇 Timed out **{member}** for **{minutes} minute(s)**. Reason: {reason}")


@bot.command(name="untimeout")
@commands.has_permissions(moderate_members=True)
async def untimeout(ctx, member: discord.Member):
    await member.timeout(None)
    await ctx.send(f"🔊 Removed timeout from **{member}**.")


@bot.command(name="clear")
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int = 5):
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"🧹 Deleted {len(deleted) - 1} messages.")
    await asyncio.sleep(3)
    await msg.delete()


@bot.command(name="sticky")
@commands.has_permissions(manage_messages=True)
async def sticky(ctx, *, content: str):
    channel_id = ctx.channel.id
    old = sticky_messages.get(channel_id)
    if old and old.get("message"):
        try:
            await old["message"].delete()
        except Exception:
            pass
    msg = await ctx.send(f"📌 {content}")
    sticky_messages[channel_id] = {"content": content, "message": msg}


@bot.command(name="stickyoff")
@commands.has_permissions(manage_messages=True)
async def stickyoff(ctx):
    channel_id = ctx.channel.id
    old = sticky_messages.pop(channel_id, None)
    if old and old.get("message"):
        try:
            await old["message"].delete()
        except Exception:
            pass
    await ctx.send("Sticky message removed.")


@bot.command(name="help")
async def custom_help(ctx):
    prefix = get_prefix(bot, ctx.message)

    embed = discord.Embed(
        title="🎶 Music Bot — Command List",
        description=f"Prefix: **`{prefix}`**\nExample: `{prefix}play believer`",
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="▶️ Playback",
        value=(
            f"`{prefix}play <song>` — Search & play/queue a song\n"
            f"`{prefix}pause` / `{prefix}resume` — Pause or resume\n"
            f"`{prefix}skip` — Skip current song\n"
            f"`{prefix}previous` — Go back to the last song\n"
            f"`{prefix}stop` — Stop and clear queue\n"
            f"`{prefix}restart` — Restart current song"
        ),
        inline=False,
    )

    embed.add_field(
        name="🎯 Navigation",
        value=(
            f"`{prefix}seek <mm:ss>` — Jump to a position\n"
            f"`{prefix}forward [secs]` — Skip forward (default 10s)\n"
            f"`{prefix}rewind [secs]` — Skip backward (default 10s)"
        ),
        inline=False,
    )

    embed.add_field(
        name="📜 Queue",
        value=(
            f"`{prefix}queue` — Show the current queue\n"
            f"`{prefix}shuffle` — Shuffle the queue\n"
            f"`{prefix}loop [off/one/queue]` — Set loop mode\n"
            f"`{prefix}autoplay [on/off]` — Auto-continue with related songs"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔊 Audio",
        value=(
            f"`{prefix}volume [0-100]` — View/set volume\n"
            f"`{prefix}bass [0-20]` — View/set bass boost"
        ),
        inline=False,
    )

    embed.add_field(
        name="⚙️ Voice & Settings",
        value=(
            f"`{prefix}join` — Join your voice channel\n"
            f"`{prefix}leave` — Leave voice channel\n"
            f"`{prefix}nowplaying` — Show current song details\n"
            f"`{prefix}setprefix <new>` — Change prefix (Admin only)"
        ),
        inline=False,
    )

    embed.add_field(
        name="🛡️ Moderation",
        value=(
            f"`{prefix}ban @user [reason]` — Ban a member\n"
            f"`{prefix}unban <username or ID>` — Unban a member\n"
            f"`{prefix}kick @user [reason]` — Kick a member\n"
            f"`{prefix}timeout @user <minutes> [reason]` — Timeout a member\n"
            f"`{prefix}untimeout @user` — Remove a timeout\n"
            f"`{prefix}clear <amount>` — Delete recent messages\n"
            f"`{prefix}sticky <message>` — Pin a repeating sticky message\n"
            f"`{prefix}stickyoff` — Remove the sticky message"
        ),
        inline=False,
    )

    embed.set_footer(text="Tip: Use the buttons on the Now Playing card for quick controls too!")
    await ctx.send(embed=embed)


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
