import asyncio
import datetime
import os
import random
import re
import time
import discord
import aiohttp
import io
from PIL import Image, ImageDraw, ImageFont
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
bot_start_time = datetime.datetime.utcnow()

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
radio_format_options.pop("format", None)
radio_format_options["noplaylist"] = False
radio_format_options["playlistend"] = 5
radio_format_options["extract_flat"] = "in_playlist"
ytdl_radio = youtube_dl.YoutubeDL(radio_format_options)

# Dedicated instance for the `search` command (top-5 results, no auto-download)
search_format_options = dict(ytdl_format_options)
search_format_options["default_search"] = "ytsearch5"
search_format_options.pop("extractor_args", None)
ytdl_search = youtube_dl.YoutubeDL(search_format_options)

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
# Per-guild logs channel id
logs_channels = {}
# Tracks when each user joined a voice channel: {(guild_id, user_id): datetime}
voice_join_times = {}
# Per-guild DJ role id (None = no restriction)
dj_roles = {}
# Per-guild 24/7 mode toggle
stay_247 = {}
# Per-guild welcome/leave message config: {"channel_id":, "join_message":, "leave_message":}
welcome_config = {}
# Per-guild leveling: {(guild_id, user_id): {"xp": int, "level": int}}
level_data = {}
# Anti-spam cooldown for chat XP: {(guild_id, user_id): timestamp}
last_xp_time = {}
# Per-guild channel for level-up announcements (None = same channel as activity)
level_channels = {}
# Per-guild allowed bot-command channels (empty set = allowed everywhere)
bot_channels = {}
# Per-guild command aliases: {alias: real_command_name}
command_aliases = {}
# Per-guild active vote-skip votes: {guild_id: set(user_id)}
voteskip_votes = {}


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


# Per-guild active special filter (nightcore/vaporwave/8d/karaoke/treble), None = off
active_filters = {}
FILTER_PRESETS = {
    "nightcore": "asetrate=48000*1.25,aresample=48000",
    "vaporwave": "asetrate=48000*0.8,aresample=48000",
    "8d": "apulsator=hz=0.08",
    "karaoke": "pan=stereo|c0=c0-c1|c1=c1-c0",
    "treble": "treble=g=5",
}


def get_ffmpeg_options(guild_id):
    bass = get_bass(guild_id)
    audio_filter = f"bass=g={bass}"
    extra = FILTER_PRESETS.get(active_filters.get(guild_id))
    if extra:
        audio_filter += f",{extra}"
    return {
        # -nostdin avoids a known ffmpeg hang where it waits on stdin input
        "before_options": "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
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
        entry_id = entry.get("id")
        if not entry_id or entry_id == video_id:
            continue  # skip the same song
        try:
            full = ytdl.extract_info(f"https://www.youtube.com/watch?v={entry_id}", download=False)
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

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"SeekModal error: {error}")
        message = "⚠️ Something went wrong seeking that song — please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            pass

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

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        # Without this, any exception here just times out silently and Discord
        # shows the generic "This interaction failed" with no explanation.
        print(f"MusicControls error on {item}: {error}")
        message = "⚠️ Something went wrong with that button — please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            pass

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
            try:
                vc.stop()
            except discord.ClientException:
                pass
        play_song(self.ctx, prev_song, seek_seconds=0)
        await interaction.response.send_message("Playing previous song.", ephemeral=True)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, row=0)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.ctx.voice_client
        try:
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
        except discord.ClientException:
            # Happens if the song ended in the split-second between the click and the response
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.ctx.voice_client
        try:
            if vc and vc.is_playing():
                vc.stop()
                await interaction.response.send_message("Skipped.", ephemeral=True)
            else:
                await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        except discord.ClientException:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
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
            try:
                vc.stop()
            except discord.ClientException:
                pass
        await interaction.response.send_message("Stopped and cleared the queue.", ephemeral=True)

    @discord.ui.button(label="Disconnect", style=discord.ButtonStyle.danger, row=2)
    async def disconnect_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        get_queue(guild_id).clear()
        if guild_id in now_playing:
            now_playing[guild_id]["manual_stop"] = True
        vc = self.ctx.voice_client
        if vc:
            try:
                await vc.disconnect()
            except discord.ClientException:
                pass
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
    voteskip_votes.pop(guild_id, None)

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
    asyncio.run_coroutine_threadsafe(
        send_log(ctx.guild, f"🎵 **{ctx.author}** is now playing **{song.title}** in **{voice_client.channel.name}**"),
        bot.loop,
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

    # Resolve custom command aliases
    if message.guild:
        prefix = get_prefix(bot, message)
        if isinstance(prefix, str) and message.content.startswith(prefix):
            rest = message.content[len(prefix):]
            parts = rest.split(" ", 1)
            cmd_word = parts[0].lower()
            guild_aliases = command_aliases.get(message.guild.id, {})
            if cmd_word in guild_aliases:
                real = guild_aliases[cmd_word]
                message.content = prefix + real + (" " + parts[1] if len(parts) > 1 else "")

    # Chat XP (with cooldown to prevent spam-farming)
    if message.guild:
        key = (message.guild.id, message.author.id)
        now_ts = datetime.datetime.utcnow().timestamp()
        if now_ts - last_xp_time.get(key, 0) >= 60:
            last_xp_time[key] = now_ts
            await grant_xp_and_announce(
                message.guild, message.author, random.randint(5, 15), fallback_channel=message.channel
            )

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
    elif isinstance(error, commands.CheckFailure):
        pass  # already messaged by the failing check (e.g. global_music_check)
    elif isinstance(error, commands.CommandInvokeError):
        print(f"Unhandled command error in '{ctx.command}': {error.original}")
        try:
            await ctx.send("⚠️ Something went wrong running that command. It's been logged.")
        except Exception:
            pass
    else:
        print(f"Unhandled command error: {error}")


async def send_log(guild, description, color=discord.Color.blurple()):
    channel_id = logs_channels.get(guild.id)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not channel:
        return
    embed = discord.Embed(description=description, color=color, timestamp=discord.utils.utcnow())
    try:
        await channel.send(embed=embed)
    except Exception:
        pass


# ---------- LEVELING SYSTEM ----------
def xp_for_level(level):
    # Fast early levels, slows down as level increases (classic XP curve)
    return 5 * (level ** 2) + 50 * level + 100


def add_xp(guild_id, user_id, amount):
    key = (guild_id, user_id)
    data = level_data.setdefault(key, {"xp": 0, "level": 0})
    data["xp"] += amount
    leveled_up = False
    while data["xp"] >= xp_for_level(data["level"]):
        data["xp"] -= xp_for_level(data["level"])
        data["level"] += 1
        leveled_up = True
    return leveled_up, data["level"]


async def grant_xp_and_announce(guild, member, amount, fallback_channel=None):
    leveled_up, new_level = add_xp(guild.id, member.id, amount)
    if not leveled_up:
        return
    channel_id = level_channels.get(guild.id)
    channel = guild.get_channel(channel_id) if channel_id else fallback_channel
    if not channel:
        channel = guild.system_channel
    if not channel:
        return
    embed = discord.Embed(
        title="🎉 Level Up!",
        description=f"{member.mention} just reached **Level {new_level}**!",
        color=discord.Color.gold(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"{member} • Keep chatting and hanging out in voice to level up!")
    try:
        await channel.send(embed=embed)
    except Exception:
        pass


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    guild = member.guild
    key = (guild.id, member.id)
    # User joined a voice channel
    if before.channel is None and after.channel is not None:
        voice_join_times[key] = datetime.datetime.utcnow()
        await send_log(
            guild,
            f"🔊 **{member}** joined voice channel **{after.channel.name}**",
            color=discord.Color.green(),
        )
    # User left a voice channel entirely
    elif before.channel is not None and after.channel is None:
        joined_at = voice_join_times.pop(key, None)
        duration_text = ""
        seconds = 0
        if joined_at:
            seconds = (datetime.datetime.utcnow() - joined_at).total_seconds()
            duration_text = f" (stayed {format_time(seconds)})"
            xp_gain = int((seconds / 60) * 4)  # 4 XP per minute in voice
            if xp_gain > 0:
                await grant_xp_and_announce(guild, member, xp_gain, fallback_channel=None)
        await send_log(
            guild,
            f"🔇 **{member}** left voice channel **{before.channel.name}**{duration_text}",
            color=discord.Color.red(),
        )
    # User switched voice channels
    elif before.channel is not None and after.channel is not None and before.channel != after.channel:
        await send_log(
            guild,
            f"🔀 **{member}** moved from **{before.channel.name}** to **{after.channel.name}**",
            color=discord.Color.gold(),
        )


MAX_IMAGE_BYTES = 15 * 1024 * 1024  # 15 MB cap so a huge/bad link can't stall or blow up memory


async def fetch_image_bytes(url):
    """Download a URL and return image bytes only if it's actually an image.
    Returns (bytes_or_none, error_message_or_none)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None, f"That link returned an error (HTTP {resp.status})."
                content_type = resp.headers.get("Content-Type", "")
                if "image" not in content_type:
                    return None, (
                        "That doesn't look like a direct image link (I got a webpage, not an image). "
                        "Right-click the image itself and choose **Copy Image Address** — "
                        "Tenor page links and pin.it share links won't work."
                    )
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_IMAGE_BYTES:
                    return None, "That image is too large (max 15 MB). Please use a smaller file."
                chunks = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_IMAGE_BYTES:
                        return None, "That image is too large (max 15 MB). Please use a smaller file."
                return bytes(chunks), None
    except Exception:
        return None, "I couldn't reach that link at all — please double check it."


async def generate_welcome_banner(member, background_url=None, title="WELCOME"):
    width, height = 900, 300

    # Fetch the background bytes (may be a static image or an animated GIF)
    bg_bytes = None
    if background_url:
        try:
            bg_bytes, _ = await fetch_image_bytes(background_url)
        except Exception:
            bg_bytes = None

    src_img = None
    is_animated = False
    if bg_bytes:
        try:
            src_img = Image.open(io.BytesIO(bg_bytes))
            is_animated = getattr(src_img, "is_animated", False) and getattr(src_img, "n_frames", 1) > 1
        except Exception:
            src_img = None

    # Fetch avatar once — reused on every frame if the background is animated.
    # Small badge in the top-right corner instead of a big centered circle,
    # so the background (especially an animated gif) stays clean and visible.
    AVATAR_SIZE = 84
    avatar, mask, ring = None, None, None
    avatar_x = width - AVATAR_SIZE - 24
    avatar_y = 24
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(str(member.display_avatar.replace(size=256).url), timeout=10) as resp:
                avatar_bytes = await resp.read()
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((AVATAR_SIZE, AVATAR_SIZE))
        mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255)
        ring_size = AVATAR_SIZE + 8
        ring = Image.new("RGBA", (ring_size, ring_size), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse((0, 0, ring_size, ring_size), fill=(255, 255, 255, 255))
    except Exception:
        avatar = None

    def compose_frame(base_rgb):
        frame = base_rgb.convert("RGB").resize((width, height)).convert("RGBA")
        if avatar is not None:
            frame.paste(ring, (avatar_x - 4, avatar_y - 4), ring)
            frame.paste(avatar, (avatar_x, avatar_y), mask)
        return frame.convert("RGB")

    if is_animated:
        # Render every frame of the source GIF with the avatar/text composited on top,
        # so the welcome banner keeps its animation instead of freezing on frame 1.
        MAX_FRAMES = 60  # cap to keep file size / render time reasonable
        frames = []
        durations = []
        n_frames = min(src_img.n_frames, MAX_FRAMES)
        for i in range(n_frames):
            src_img.seek(i)
            frames.append(compose_frame(src_img.convert("RGB")))
            durations.append(src_img.info.get("duration", 100) or 100)
        buffer = io.BytesIO()
        frames[0].save(
            buffer,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0,
            disposal=2,
        )
        buffer.seek(0)
        return discord.File(buffer, filename="welcome.gif")

    # Static background image, or none provided/fetch failed -> default gradient
    if src_img is not None:
        bg = src_img.convert("RGB")
    else:
        bg = Image.new("RGB", (width, height))
        top = (30, 30, 60)
        bottom = (90, 40, 120)
        for y in range(height):
            ratio = y / height
            r = int(top[0] + (bottom[0] - top[0]) * ratio)
            g = int(top[1] + (bottom[1] - top[1]) * ratio)
            b = int(top[2] + (bottom[2] - top[2]) * ratio)
            ImageDraw.Draw(bg).line([(0, y), (width, y)], fill=(r, g, b))

    final_frame = compose_frame(bg)
    buffer = io.BytesIO()
    final_frame.save(buffer, format="PNG")
    buffer.seek(0)
    return discord.File(buffer, filename="welcome.png")


DEFAULT_JOIN_MESSAGE = "👋 Welcome {mention} to **{server}**! We're now **{membercount}** members strong."
DEFAULT_LEAVE_MESSAGE = "😢 **{user}** has left **{server}**. We're now **{membercount}** members."


@bot.event
async def on_member_join(member):
    config = welcome_config.get(member.guild.id)
    if not config or not config.get("channel_id"):
        return
    channel = member.guild.get_channel(config["channel_id"])
    if not channel:
        return
    template = config.get("join_message", DEFAULT_JOIN_MESSAGE)
    text = template.format(
        mention=member.mention,
        user=str(member),
        server=member.guild.name,
        membercount=member.guild.member_count,
    )
    try:
        banner = await generate_welcome_banner(member, background_url=config.get("background_url"), title="WELCOME")
        await channel.send(content=text, file=banner)
    except Exception:
        try:
            await channel.send(text)
        except Exception:
            pass


@bot.event
async def on_member_remove(member):
    config = welcome_config.get(member.guild.id)
    if not config:
        return
    channel_id = config.get("leave_channel_id") or config.get("channel_id")
    if not channel_id:
        return
    channel = member.guild.get_channel(channel_id)
    if not channel:
        return
    template = config.get("leave_message", DEFAULT_LEAVE_MESSAGE)
    text = template.format(
        mention=member.mention,
        user=str(member),
        server=member.guild.name,
        membercount=member.guild.member_count,
    )
    try:
        await channel.send(text)
    except Exception:
        pass


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
    await send_log(ctx.guild, f"🤖 Bot joined **{channel.name}** (requested by {ctx.author})", color=discord.Color.blue())


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


@bot.command(name="leave", aliases=["disconnect"])
async def leave(ctx):
    guild_id = ctx.guild.id
    if guild_id in now_playing:
        now_playing[guild_id]["manual_stop"] = True
    if ctx.voice_client:
        channel_name = ctx.voice_client.channel.name
        await ctx.voice_client.disconnect()
        await ctx.send("Left the voice channel.")
        await send_log(ctx.guild, f"🤖 Bot left **{channel_name}** (requested by {ctx.author})", color=discord.Color.blue())


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


@bot.command(name="filter")
async def filter_cmd(ctx, name: str = None):
    guild_id = ctx.guild.id
    valid = list(FILTER_PRESETS.keys()) + ["bassboost", "clear"]
    if name is None:
        current = active_filters.get(guild_id, "none")
        await ctx.send(f"Current filter: **{current}**\nAvailable: {', '.join(valid)}")
        return
    name = name.lower()
    if name == "clear":
        active_filters.pop(guild_id, None)
        bass_levels[guild_id] = 3
        await ctx.send("Filters cleared — applies from the next song.")
        return
    if name == "bassboost":
        bass_levels[guild_id] = 12
        active_filters.pop(guild_id, None)
        await ctx.send("🔊 Bass boost filter applied — applies from the next song.")
        return
    if name not in FILTER_PRESETS:
        await ctx.send(f"Unknown filter. Available: {', '.join(valid)}")
        return
    active_filters[guild_id] = name
    await ctx.send(f"🎚 Filter **{name}** applied — applies from the next song (use `mrestart` to hear it now).")


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


@bot.command(name="nowplaying", aliases=["np"])
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


@bot.command(name="skipto")
async def skipto(ctx, number: int):
    queue = get_queue(ctx.guild.id)
    if number < 1 or number > len(queue):
        await ctx.send("Invalid queue position.")
        return
    del queue[: number - 1]
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    else:
        play_next(ctx)
    await ctx.send(f"⏭️ Skipping to song #{number} in the queue.")


@bot.command(name="remove")
async def remove_song(ctx, number: int):
    queue = get_queue(ctx.guild.id)
    if number < 1 or number > len(queue):
        await ctx.send("Invalid queue position.")
        return
    removed = queue.pop(number - 1)
    await ctx.send(f"🗑 Removed **{removed.title}** from the queue.")


@bot.command(name="clearqueue")
async def clearqueue(ctx):
    queue = get_queue(ctx.guild.id)
    queue.clear()
    await ctx.send("🧹 Queue cleared (current song keeps playing).")


@bot.command(name="search")
async def search_cmd(ctx, *, query: str):
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: ytdl_search.extract_info(query, download=False))
    except Exception as e:
        await ctx.send(f"Search failed: {e}")
        return
    entries = data.get("entries") or []
    if not entries:
        await ctx.send("No results found.")
        return
    lines = "\n".join(f"{i+1}. {e.get('title', 'Unknown')}" for i, e in enumerate(entries[:5]))
    await ctx.send(f"🔎 **Results for '{query}':**\n{lines}\n\nUse `mplay <song name>` to play one.")


# ---------- PLAYLISTS (per-user, in-memory) ----------
playlists = {}  # user_id -> {playlist_name: [{"title":, "url":}]}
playlist_active = {}  # user_id -> currently active playlist name


@bot.group(name="playlist", invoke_without_command=True)
async def playlist(ctx):
    user_playlists = playlists.get(ctx.author.id, {})
    if not user_playlists:
        await ctx.send("You have no playlists yet. Create one with `mplaylist create <name>`.")
        return
    lines = "\n".join(f"• {n} ({len(songs)} songs)" for n, songs in user_playlists.items())
    await ctx.send(f"**Your Playlists:**\n{lines}")


@playlist.command(name="create")
async def playlist_create(ctx, *, name: str):
    user_playlists = playlists.setdefault(ctx.author.id, {})
    if name in user_playlists:
        await ctx.send("You already have a playlist with that name.")
        return
    user_playlists[name] = []
    playlist_active[ctx.author.id] = name
    await ctx.send(f"📂 Created playlist **{name}** and set it as active.")


@playlist.command(name="add")
async def playlist_add(ctx, *, query: str):
    name = playlist_active.get(ctx.author.id)
    if not name:
        await ctx.send("No active playlist. Create one first: `mplaylist create <name>`")
        return
    try:
        song = await search_song(query)
    except Exception as e:
        await ctx.send(f"Couldn't find that song: {e}")
        return
    playlists[ctx.author.id][name].append({"title": song.title, "url": song.webpage_url or query})
    await ctx.send(f"➕ Added **{song.title}** to **{name}**.")


@playlist.command(name="remove")
async def playlist_remove(ctx, *, query: str):
    name = playlist_active.get(ctx.author.id)
    if not name:
        await ctx.send("No active playlist.")
        return
    songs = playlists[ctx.author.id][name]
    for s in songs:
        if query.lower() in s["title"].lower():
            songs.remove(s)
            await ctx.send(f"➖ Removed **{s['title']}** from **{name}**.")
            return
    await ctx.send("Song not found in the active playlist.")


@playlist.command(name="play")
async def playlist_play(ctx, *, name: str):
    user_playlists = playlists.get(ctx.author.id, {})
    if name not in user_playlists or not user_playlists[name]:
        await ctx.send("That playlist doesn't exist or is empty.")
        return
    if ctx.author.voice is None:
        await ctx.send("You need to join a voice channel first.")
        return
    if ctx.voice_client is None:
        await ctx.author.voice.channel.connect()
    queue = get_queue(ctx.guild.id)
    added = 0
    for entry in user_playlists[name]:
        try:
            song = await search_song(entry["url"])
            queue.append(song)
            added += 1
        except Exception:
            continue
    await ctx.send(f"▶️ Queued {added} song(s) from **{name}**.")
    if not ctx.voice_client.is_playing():
        play_next(ctx)


@playlist.command(name="delete")
async def playlist_delete(ctx, *, name: str):
    user_playlists = playlists.get(ctx.author.id, {})
    if name in user_playlists:
        del user_playlists[name]
        if playlist_active.get(ctx.author.id) == name:
            playlist_active.pop(ctx.author.id, None)
        await ctx.send(f"🗑 Deleted playlist **{name}**.")
    else:
        await ctx.send("That playlist doesn't exist.")


@playlist.command(name="list")
async def playlist_list(ctx, *, name: str = None):
    user_playlists = playlists.get(ctx.author.id, {})
    if name is None:
        if not user_playlists:
            await ctx.send("You have no playlists.")
            return
        lines = "\n".join(f"• {n} ({len(s)} songs)" for n, s in user_playlists.items())
        await ctx.send(f"**Your Playlists:**\n{lines}")
        return
    if name not in user_playlists:
        await ctx.send("That playlist doesn't exist.")
        return
    songs = user_playlists[name]
    if not songs:
        await ctx.send(f"**{name}** is empty.")
        return
    lines = "\n".join(f"{i+1}. {s['title']}" for i, s in enumerate(songs))
    await ctx.send(f"**{name}:**\n{lines}")


# ---------- LOGS & ADMIN CONFIG ----------
@bot.command(name="setuplogs")
@commands.has_permissions(manage_guild=True)
async def setuplogs(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    logs_channels[ctx.guild.id] = channel.id
    await ctx.send(f"✅ Logs will now be sent to {channel.mention}.")
    await send_log(ctx.guild, "📋 Logging channel set up.", color=discord.Color.blue())


@bot.command(name="removelogs")
@commands.has_permissions(manage_guild=True)
async def removelogs(ctx):
    logs_channels.pop(ctx.guild.id, None)
    await ctx.send("Logging disabled.")


@bot.group(name="dj", invoke_without_command=True)
async def dj(ctx):
    role_id = dj_roles.get(ctx.guild.id)
    if role_id:
        role = ctx.guild.get_role(role_id)
        await ctx.send(f"Current DJ role: {role.mention if role else '(role deleted)'}")
    else:
        await ctx.send("No DJ role set. Everyone can use music commands.")


@dj.command(name="role")
@commands.has_permissions(manage_guild=True)
async def dj_role(ctx, role: discord.Role = None):
    if role is None:
        dj_roles.pop(ctx.guild.id, None)
        await ctx.send("DJ role restriction removed — everyone can use music commands again.")
        return
    dj_roles[ctx.guild.id] = role.id
    await ctx.send(f"DJ role set to {role.mention}. Only members with this role can use music commands.")


def is_dj(ctx):
    role_id = dj_roles.get(ctx.guild.id)
    if not role_id:
        return True
    if ctx.author.guild_permissions.manage_guild:
        return True
    return any(r.id == role_id for r in ctx.author.roles)


@bot.command(name="247")
@commands.has_permissions(manage_guild=True)
async def stay_in_vc(ctx, mode: str = None):
    guild_id = ctx.guild.id
    if mode is None:
        state = "on" if stay_247.get(guild_id) else "off"
        await ctx.send(f"24/7 mode is currently: **{state}**")
        return
    mode = mode.lower()
    if mode not in ("on", "off"):
        await ctx.send("Usage: `m247 on` or `m247 off`")
        return
    stay_247[guild_id] = (mode == "on")
    await ctx.send(f"24/7 mode turned **{mode}**.")


@bot.command(name="botchannel")
@commands.has_permissions(manage_guild=True)
async def botchannel(ctx, action: str = None, channel: discord.TextChannel = None):
    guild_id = ctx.guild.id
    allowed = bot_channels.setdefault(guild_id, set())
    if action is None or action == "list":
        if not allowed:
            await ctx.send("No restricted channels set — commands work everywhere.")
        else:
            mentions = ", ".join(f"<#{cid}>" for cid in allowed)
            await ctx.send(f"Bot commands are restricted to: {mentions}")
        return
    if action == "add":
        if channel is None:
            await ctx.send("Usage: `mbotchannel add #channel`")
            return
        allowed.add(channel.id)
        await ctx.send(f"Bot commands are now allowed in {channel.mention}.")
    elif action == "remove":
        if channel is None:
            await ctx.send("Usage: `mbotchannel remove #channel`")
            return
        allowed.discard(channel.id)
        await ctx.send(f"Removed {channel.mention} from allowed channels.")
    elif action == "clear":
        allowed.clear()
        await ctx.send("Cleared channel restrictions — commands work everywhere now.")
    else:
        await ctx.send("Usage: `mbotchannel add/remove/list/clear [#channel]`")


def channel_allowed(ctx):
    allowed = bot_channels.get(ctx.guild.id)
    if not allowed:
        return True
    return ctx.channel.id in allowed


@bot.group(name="aliases", invoke_without_command=True)
async def aliases(ctx):
    guild_aliases = command_aliases.get(ctx.guild.id, {})
    if not guild_aliases:
        await ctx.send("No custom aliases set.")
        return
    lines = "\n".join(f"`{a}` → `{c}`" for a, c in guild_aliases.items())
    await ctx.send(f"**Custom Aliases:**\n{lines}")


@aliases.command(name="set")
@commands.has_permissions(manage_guild=True)
async def aliases_set(ctx, alias: str, real_command: str):
    if bot.get_command(real_command) is None:
        await ctx.send(f"`{real_command}` is not a real command.")
        return
    command_aliases.setdefault(ctx.guild.id, {})[alias.lower()] = real_command.lower()
    await ctx.send(f"Alias set: `{alias}` → `{real_command}`")


@aliases.command(name="remove")
@commands.has_permissions(manage_guild=True)
async def aliases_remove(ctx, alias: str):
    guild_aliases = command_aliases.get(ctx.guild.id, {})
    if alias.lower() in guild_aliases:
        del guild_aliases[alias.lower()]
        await ctx.send(f"Removed alias `{alias}`.")
    else:
        await ctx.send("That alias doesn't exist.")


@aliases.command(name="list")
async def aliases_list(ctx):
    await aliases(ctx)


@aliases.command(name="clear")
@commands.has_permissions(manage_guild=True)
async def aliases_clear(ctx):
    command_aliases.pop(ctx.guild.id, None)
    await ctx.send("All aliases cleared.")


@bot.group(name="settings", invoke_without_command=True)
async def settings(ctx):
    await settings_view(ctx)


@settings.command(name="view")
async def settings_view(ctx):
    guild_id = ctx.guild.id
    prefix = get_prefix(bot, ctx.message)
    dj_role_obj = ctx.guild.get_role(dj_roles.get(guild_id)) if dj_roles.get(guild_id) else None
    logs_channel_obj = ctx.guild.get_channel(logs_channels.get(guild_id)) if logs_channels.get(guild_id) else None
    embed = discord.Embed(title="⚙️ Server Settings", color=discord.Color.blurple())
    embed.add_field(name="Prefix", value=f"`{prefix}`", inline=True)
    embed.add_field(name="Volume", value=f"{int(get_volume(guild_id) * 100)}%", inline=True)
    embed.add_field(name="Bass Boost", value=str(get_bass(guild_id)), inline=True)
    embed.add_field(name="Loop Mode", value=get_loop(guild_id), inline=True)
    embed.add_field(name="Autoplay", value="on" if get_autoplay(guild_id) else "off", inline=True)
    embed.add_field(name="24/7 Mode", value="on" if stay_247.get(guild_id) else "off", inline=True)
    embed.add_field(name="DJ Role", value=dj_role_obj.mention if dj_role_obj else "None", inline=True)
    embed.add_field(name="Logs Channel", value=logs_channel_obj.mention if logs_channel_obj else "None", inline=True)
    allowed = bot_channels.get(guild_id)
    embed.add_field(
        name="Bot Channels",
        value=", ".join(f"<#{c}>" for c in allowed) if allowed else "Everywhere",
        inline=False,
    )
    await ctx.send(embed=embed)


@settings.command(name="prefix")
@commands.has_permissions(administrator=True)
async def settings_prefix(ctx, new_prefix: str):
    await setprefix(ctx, new_prefix)


@settings.command(name="default_volume")
@commands.has_permissions(manage_guild=True)
async def settings_default_volume(ctx, level: int):
    await volume(ctx, level)


@settings.command(name="autoplay")
@commands.has_permissions(manage_guild=True)
async def settings_autoplay(ctx, mode: str):
    await autoplay(ctx, mode)


@bot.command(name="voteskip")
async def voteskip(ctx):
    guild_id = ctx.guild.id
    vc = ctx.voice_client
    if not vc or not vc.is_playing():
        await ctx.send("Nothing is playing right now.")
        return
    if ctx.author.voice is None or ctx.author.voice.channel != vc.channel:
        await ctx.send("You need to be in the same voice channel to vote.")
        return
    votes = voteskip_votes.setdefault(guild_id, set())
    votes.add(ctx.author.id)
    members_in_vc = [m for m in vc.channel.members if not m.bot]
    needed = max(1, len(members_in_vc) // 2 + 1)
    if len(votes) >= needed:
        vc.stop()
        votes.clear()
        await ctx.send("✅ Vote passed — skipping!")
    else:
        await ctx.send(f"🗳 Vote to skip: {len(votes)}/{needed}")


MUSIC_COMMANDS = {
    "play", "skip", "pause", "resume", "stop", "queue", "shuffle", "volume",
    "bass", "loop", "autoplay", "seek", "forward", "rewind", "restart",
    "previous", "nowplaying", "join", "leave", "voteskip",
}


@bot.check
async def global_music_check(ctx):
    if ctx.guild is None:
        return True  # ignore DMs — DJ role / channel checks need guild context
    if ctx.command and ctx.command.name in MUSIC_COMMANDS:
        if not channel_allowed(ctx):
            allowed = bot_channels.get(ctx.guild.id)
            mentions = ", ".join(f"<#{c}>" for c in allowed)
            await ctx.send(f"Please use music commands in: {mentions}")
            return False
        if not is_dj(ctx):
            await ctx.send("You need the DJ role to use music commands.")
            return False
    return True


@bot.group(name="welcome", invoke_without_command=True)
@commands.has_permissions(manage_guild=True)
async def welcome(ctx):
    config = welcome_config.get(ctx.guild.id, {})
    channel = ctx.guild.get_channel(config["channel_id"]) if config.get("channel_id") else None
    leave_channel = ctx.guild.get_channel(config["leave_channel_id"]) if config.get("leave_channel_id") else channel
    await ctx.send(
        f"**Welcome (join) channel:** {channel.mention if channel else 'Not set'}\n"
        f"**Leave channel:** {leave_channel.mention if leave_channel else 'Not set'}\n"
        f"**Join message:** {config.get('join_message', DEFAULT_JOIN_MESSAGE)}\n"
        f"**Leave message:** {config.get('leave_message', DEFAULT_LEAVE_MESSAGE)}\n\n"
        f"Use `mwelcome channel #channel`, `mwelcome leavechannel #channel`, "
        f"`mwelcome message <text>`, `mwelcome leavemessage <text>`, "
        f"`mwelcome background <image url>`, `mwelcome test`\n"
        f"Placeholders: `{{mention}}` `{{user}}` `{{server}}` `{{membercount}}`"
    )


@welcome.command(name="channel")
@commands.has_permissions(manage_guild=True)
async def welcome_channel(ctx, channel: discord.TextChannel):
    guild_id = ctx.guild.id
    config = welcome_config.setdefault(guild_id, {})
    config["channel_id"] = channel.id
    try:
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(f"✅ Welcome channel set to {channel.mention} and locked so only the bot can post there.")
    except discord.Forbidden:
        await ctx.send(
            f"✅ Welcome channel set to {channel.mention}, but I couldn't lock it "
            "(need **Manage Channels** permission) — lock it manually if you want it bot-only."
        )


@welcome.command(name="message")
@commands.has_permissions(manage_guild=True)
async def welcome_message(ctx, *, text: str):
    welcome_config.setdefault(ctx.guild.id, {})["join_message"] = text
    await ctx.send("✅ Join message updated.")


@welcome.command(name="leavemessage")
@commands.has_permissions(manage_guild=True)
async def welcome_leavemessage(ctx, *, text: str):
    welcome_config.setdefault(ctx.guild.id, {})["leave_message"] = text
    await ctx.send("✅ Leave message updated.")


@welcome.command(name="test")
async def welcome_test(ctx):
    config = welcome_config.get(ctx.guild.id, {})
    template = config.get("join_message", DEFAULT_JOIN_MESSAGE)
    text = template.format(
        mention=ctx.author.mention,
        user=str(ctx.author),
        server=ctx.guild.name,
        membercount=ctx.guild.member_count,
    )
    banner = await generate_welcome_banner(ctx.author, background_url=config.get("background_url"), title="WELCOME")
    await ctx.send(content=f"**Preview:**\n{text}", file=banner)


@welcome.command(name="leavechannel")
@commands.has_permissions(manage_guild=True)
async def welcome_leavechannel(ctx, channel: discord.TextChannel):
    guild_id = ctx.guild.id
    welcome_config.setdefault(guild_id, {})["leave_channel_id"] = channel.id
    await ctx.send(f"✅ Leave messages will now be sent in {channel.mention} (separate from the welcome channel).")


@welcome.command(name="background")
@commands.has_permissions(manage_guild=True)
async def welcome_background(ctx, url: str = None):
    guild_id = ctx.guild.id
    config = welcome_config.setdefault(guild_id, {})
    if url is None:
        config.pop("background_url", None)
        await ctx.send("Background reset to the default gradient.")
        return
    # Validate the URL actually points to a real image before saving it
    async with ctx.typing():
        img_bytes, error = await fetch_image_bytes(url)
    if error:
        await ctx.send(f"❌ Couldn't use that image: {error}")
        return
    config["background_url"] = url
    await ctx.send("✅ Welcome banner background image updated. Use `mwelcome test` to preview it.")


@bot.command(name="level", aliases=["rank"])
async def level_cmd(ctx, member: discord.Member = None):
    member = member or ctx.author
    data = level_data.get((ctx.guild.id, member.id), {"xp": 0, "level": 0})
    needed = xp_for_level(data["level"])
    bar = make_progress_bar(data["xp"], needed, length=15)
    embed = discord.Embed(title=f"📊 {member}'s Level", color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=str(data["level"]), inline=True)
    embed.add_field(name="XP", value=f"{data['xp']}/{needed}", inline=True)
    embed.add_field(name="Progress", value=bar, inline=False)
    await ctx.send(embed=embed)


@bot.command(name="leaderboard", aliases=["lb"])
async def leaderboard(ctx):
    guild_id = ctx.guild.id
    entries = [(uid, d) for (gid, uid), d in level_data.items() if gid == guild_id]
    if not entries:
        await ctx.send("No activity recorded yet.")
        return
    entries.sort(key=lambda e: (e[1]["level"], e[1]["xp"]), reverse=True)
    lines = []
    for i, (uid, d) in enumerate(entries[:10]):
        member = ctx.guild.get_member(uid)
        name = str(member) if member else f"User {uid}"
        lines.append(f"**{i+1}.** {name} — Level {d['level']} ({d['xp']} XP)")
    embed = discord.Embed(title="🏆 Leaderboard", description="\n".join(lines), color=discord.Color.gold())
    await ctx.send(embed=embed)


@bot.command(name="setlevelchannel")
@commands.has_permissions(manage_guild=True)
async def setlevelchannel(ctx, channel: discord.TextChannel = None):
    guild_id = ctx.guild.id
    if channel is None:
        level_channels.pop(guild_id, None)
        await ctx.send("Level-up announcements will now be sent wherever the XP was earned.")
        return
    level_channels[guild_id] = channel.id
    await ctx.send(f"✅ Level-up announcements will be sent in {channel.mention}.")


@bot.command(name="ping")
async def ping(ctx):
    latency_ms = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! Latency: **{latency_ms}ms**")


@bot.command(name="lyrics")
async def lyrics(ctx, *, query: str = None):
    guild_id = ctx.guild.id
    if query is None:
        info = now_playing.get(guild_id)
        if not info:
            await ctx.send("Nothing is playing — provide a song name: `mlyrics <song>`")
            return
        query = info["song"].title
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://some-random-api.com/lyrics", params={"title": query}, timeout=10
            ) as resp:
                if resp.status != 200:
                    await ctx.send("Couldn't find lyrics for that song.")
                    return
                data = await resp.json()
    except Exception:
        await ctx.send("Lyrics service is unavailable right now.")
        return
    lyrics_text = data.get("lyrics", "")
    title = data.get("title", query)
    artist = data.get("author", "Unknown")
    if not lyrics_text:
        await ctx.send("Couldn't find lyrics for that song.")
        return
    if len(lyrics_text) > 4000:
        lyrics_text = lyrics_text[:4000] + "..."
    embed = discord.Embed(title=f"🎤 {title} — {artist}", description=lyrics_text, color=discord.Color.blurple())
    await ctx.send(embed=embed)


@bot.command(name="info", aliases=["botinfo"])
async def info_cmd(ctx):
    uptime = datetime.datetime.utcnow() - bot_start_time
    embed = discord.Embed(
        title=f"🎶 {bot.user.name}",
        description="A feature-rich music & moderation bot.",
        color=discord.Color.blurple(),
    )
    if bot.user.avatar:
        embed.set_thumbnail(url=bot.user.avatar.url)
    embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Uptime", value=format_time(uptime.total_seconds()), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Prefix", value=f"`{get_prefix(bot, ctx.message)}`", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="invite")
async def invite(ctx):
    url = (
        f"https://discord.com/api/oauth2/authorize?client_id={bot.user.id}"
        "&permissions=277062483008&scope=bot"
    )
    await ctx.send(f"🔗 Invite me to your server: {url}")


@bot.command(name="stats")
async def stats(ctx):
    total_voice = sum(1 for g in bot.guilds if g.voice_client)
    embed = discord.Embed(title="📊 Bot Stats", color=discord.Color.blurple())
    embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Active Voice Connections", value=str(total_voice), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="uptime")
async def uptime(ctx):
    uptime_delta = datetime.datetime.utcnow() - bot_start_time
    await ctx.send(f"⏱️ Uptime: **{format_time(uptime_delta.total_seconds())}**")


@bot.command(name="avatar")
async def avatar(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"{member}'s Avatar", color=discord.Color.blurple())
    embed.set_image(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="userinfo")
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"👤 {member}", color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=str(member.id), inline=True)
    embed.add_field(
        name="Joined Server",
        value=member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "Unknown",
        inline=True,
    )
    embed.add_field(name="Account Created", value=member.created_at.strftime("%Y-%m-%d"), inline=True)
    roles = ", ".join(r.mention for r in member.roles if r.name != "@everyone")
    embed.add_field(name="Roles", value=roles or "None", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="serverinfo")
async def serverinfo(ctx):
    guild = ctx.guild
    embed = discord.Embed(title=f"🏠 {guild.name}", color=discord.Color.blurple())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner", value=str(guild.owner), inline=True)
    embed.add_field(name="Members", value=str(guild.member_count), inline=True)
    embed.add_field(name="Created", value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="Text Channels", value=str(len(guild.text_channels)), inline=True)
    embed.add_field(name="Voice Channels", value=str(len(guild.voice_channels)), inline=True)
    embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="restrict")
@commands.has_permissions(manage_guild=True)
async def restrict(ctx, action: str = None, channel: discord.TextChannel = None):
    await botchannel(ctx, action, channel)


@bot.command(name="language")
async def language(ctx):
    await ctx.send("🌐 This bot currently only supports **English**. More languages may be added in the future.")


@bot.command(name="setup")
@commands.has_permissions(manage_guild=True)
async def setup_cmd(ctx):
    await ctx.send(
        "⚙️ **Quick setup:**\n"
        f"1. `{get_prefix(bot, ctx.message)}setuplogs #channel` — set a logs channel\n"
        f"2. `{get_prefix(bot, ctx.message)}dj role @role` — restrict music to a DJ role (optional)\n"
        f"3. `{get_prefix(bot, ctx.message)}botchannel add #channel` — restrict commands to a channel (optional)\n"
        f"4. `{get_prefix(bot, ctx.message)}settings view` — review your settings anytime"
    )


@bot.command(name="reset")
@commands.has_permissions(administrator=True)
async def reset_cmd(ctx):
    guild_id = ctx.guild.id
    for store in (prefixes, dj_roles, logs_channels, stay_247, bass_levels, volumes,
                  loop_modes, autoplay_flags, active_filters, command_aliases):
        store.pop(guild_id, None)
    bot_channels.pop(guild_id, None)
    await ctx.send("♻️ All server settings have been reset to default.")


@bot.command(name="reload")
@commands.has_permissions(administrator=True)
async def reload_cmd(ctx):
    await ctx.send("🔄 Settings refreshed. (Live commands don't require a restart.)")


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


BOT_NAME = "TORMENTA MUSIC 2"

HELP_CATEGORIES = {
    "music": {
        "label": "Music",
        "desc": "Music playback commands",
        "emoji": "🎶",
        "commands": lambda p: (
            f"`{p}play <song/url>` — Search & play/queue a song\n"
            f"`{p}search <song>` — Search top 5 results\n"
            f"`{p}queue` — Show the current queue\n"
            f"`{p}nowplaying` (`{p}np`) — Show current song\n"
            f"`{p}skip` — Skip current song\n"
            f"`{p}skipto <number>` — Skip to a queue position\n"
            f"`{p}previous` — Go back to the last song\n"
            f"`{p}stop` — Stop and clear queue\n"
            f"`{p}pause` / `{p}resume` — Pause or resume\n"
            f"`{p}restart` — Restart current song\n"
            f"`{p}loop [off/one/queue]` — Set loop mode\n"
            f"`{p}shuffle` — Shuffle the queue\n"
            f"`{p}remove <number>` — Remove a song from queue\n"
            f"`{p}clearqueue` — Clear the queue\n"
            f"`{p}volume [0-100]` — View/set volume\n"
            f"`{p}seek <mm:ss>` — Jump to a position\n"
            f"`{p}forward [secs]` / `{p}rewind [secs]` — Skip forward/back\n"
            f"`{p}lyrics [song]` — Get lyrics\n"
            f"`{p}autoplay [on/off]` — Auto-continue with related songs"
        ),
    },
    "filters": {
        "label": "Filters",
        "desc": "Audio filter commands",
        "emoji": "🎚",
        "commands": lambda p: (
            f"`{p}filter bassboost` — Boost the bass\n"
            f"`{p}filter nightcore` — Speed up + pitch up\n"
            f"`{p}filter vaporwave` — Slow down + pitch down\n"
            f"`{p}filter 8d` — Rotating 8D audio effect\n"
            f"`{p}filter karaoke` — Reduce vocals\n"
            f"`{p}filter treble` — Boost the treble\n"
            f"`{p}filter clear` — Remove all filters\n"
            f"`{p}bass [0-20]` — View/set bass boost level"
        ),
    },
    "playlists": {
        "label": "Playlists",
        "desc": "Playlist management commands",
        "emoji": "📂",
        "commands": lambda p: (
            f"`{p}playlist create <name>` — Create a playlist\n"
            f"`{p}playlist add <song>` — Add a song to your active playlist\n"
            f"`{p}playlist remove <song>` — Remove a song from it\n"
            f"`{p}playlist play <name>` — Queue an entire playlist\n"
            f"`{p}playlist delete <name>` — Delete a playlist\n"
            f"`{p}playlist list [name]` — List your playlists or a playlist's songs"
        ),
    },
    "voice": {
        "label": "Voice",
        "desc": "Voice channel commands",
        "emoji": "🔊",
        "commands": lambda p: (
            f"`{p}join` — Join your voice channel\n"
            f"`{p}leave` / `{p}disconnect` — Leave voice channel"
        ),
    },
    "moderation": {
        "label": "Moderation",
        "desc": "Server moderation commands",
        "emoji": "🛡",
        "commands": lambda p: (
            f"`{p}ban @user [reason]` — Ban a member\n"
            f"`{p}unban <username/ID>` — Unban a member\n"
            f"`{p}kick @user [reason]` — Kick a member\n"
            f"`{p}timeout @user <minutes> [reason]` — Timeout a member\n"
            f"`{p}untimeout @user` — Remove a timeout\n"
            f"`{p}clear <amount>` — Delete recent messages\n"
            f"`{p}sticky <message>` / `{p}stickyoff` — Sticky message"
        ),
    },
    "config": {
        "label": "Config",
        "desc": "Configuration commands",
        "emoji": "🔧",
        "commands": lambda p: (
            f"`{p}setprefix <new>` — Change prefix (Admin)\n"
            f"`{p}dj role <@role>` — Set a DJ role\n"
            f"`{p}247 on/off` — Stay in voice 24/7\n"
            f"`{p}botchannel add/remove/list/clear` — Restrict commands to channels\n"
            f"`{p}restrict ...` — Alias for botchannel\n"
            f"`{p}aliases set/remove/list/clear` — Custom command aliases\n"
            f"`{p}settings view` — Show all server settings\n"
            f"`{p}setuplogs [#channel]` / `{p}removelogs` — Voice activity logs\n"
            f"`{p}setup` — Quick setup guide\n"
            f"`{p}reset` — Reset all settings (Admin)\n"
            f"`{p}reload` — Refresh settings"
        ),
    },
    "general": {
        "label": "General",
        "desc": "General utility commands",
        "emoji": "🌐",
        "commands": lambda p: (
            f"`{p}help` — Show this menu\n"
            f"`{p}ping` — Check bot latency\n"
            f"`{p}info` — Bot info\n"
            f"`{p}invite` — Get the bot's invite link\n"
            f"`{p}stats` — Bot statistics\n"
            f"`{p}uptime` — Bot uptime\n"
            f"`{p}language` — Supported languages\n"
            f"`{p}voteskip` — Vote to skip the current song\n"
            f"`{p}avatar [@user]` — Show a user's avatar\n"
            f"`{p}userinfo [@user]` — Show user info\n"
            f"`{p}serverinfo` — Show server info"
        ),
    },
    "welcome": {
        "label": "Welcome & Levels",
        "desc": "Welcome/leave messages and leveling",
        "emoji": "🎉",
        "commands": lambda p: (
            f"`{p}welcome channel #channel` — Set + lock the welcome channel (Admin)\n"
            f"`{p}welcome leavechannel #channel` — Set a separate leave channel (Admin)\n"
            f"`{p}welcome message <text>` — Set the join message (Admin)\n"
            f"`{p}welcome leavemessage <text>` — Set the leave message (Admin)\n"
            f"`{p}welcome background <image url>` — Set the welcome banner background (Admin)\n"
            f"`{p}welcome test` — Preview the join message + banner\n"
            f"`{p}level [@user]` (`{p}rank`) — Show a level & XP progress\n"
            f"`{p}leaderboard` (`{p}lb`) — Top members by XP\n"
            f"`{p}setlevelchannel #channel` — Set level-up announcement channel (Admin)"
        ),
    },
}


def build_welcome_embed(prefix):
    embed = discord.Embed(
        title=f"Welcome! Let's Get Started with {BOT_NAME}",
        description=(
            f"**About {BOT_NAME}**\n"
            "A simple, high-quality music bot built for great sound and easy use.\n\n"
            "**Supported Platforms**\nYouTube • SoundCloud\n\n"
            "**Features**\nCustom aliases • Audio filters • Playlist management • "
            "Voice activity logs • Moderation tools • And much more!\n\n"
            f"**Quick Start**\n"
            f"1. Join a voice channel\n"
            f"2. Type `{prefix}play [song name]`\n"
            f"3. Explore commands below!\n\n"
            "📋 **Browse Commands by Category**"
        ),
        color=discord.Color.dark_teal(),
    )
    if bot.user and bot.user.avatar:
        embed.set_thumbnail(url=bot.user.avatar.url)
    embed.set_footer(text=f"Prefix: {prefix} • Use the menu below to explore")
    return embed


def build_category_embed(key, prefix):
    data = HELP_CATEGORIES[key]
    embed = discord.Embed(
        title=f"{data['emoji']} {data['label']} Commands",
        description=data["commands"](prefix),
        color=discord.Color.dark_teal(),
    )
    embed.set_footer(text=f"{BOT_NAME} • Prefix: {prefix}")
    return embed


def build_all_commands_embed(prefix):
    embed = discord.Embed(
        title=f"📖 {BOT_NAME} — All Commands",
        color=discord.Color.dark_teal(),
    )
    for data in HELP_CATEGORIES.values():
        embed.add_field(
            name=f"{data['emoji']} {data['label']}",
            value=data["commands"](prefix),
            inline=False,
        )
    embed.set_footer(text=f"Prefix: {prefix}")
    return embed


class HelpBackView(discord.ui.View):
    def __init__(self, prefix):
        super().__init__(timeout=180)
        self.prefix = prefix

    @discord.ui.button(label="Back to Help Menu", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_welcome_embed(self.prefix)
        await interaction.response.edit_message(embed=embed, view=HelpSelectView(self.prefix))


class HelpCategorySelect(discord.ui.Select):
    def __init__(self, prefix):
        self.prefix = prefix
        options = [
            discord.SelectOption(label=data["label"], description=data["desc"], value=key, emoji=data["emoji"])
            for key, data in HELP_CATEGORIES.items()
        ]
        super().__init__(placeholder="Choose a category to explore...", options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        embed = build_category_embed(key, self.prefix)
        await interaction.response.edit_message(embed=embed, view=HelpBackView(self.prefix))


class HelpSelectView(discord.ui.View):
    def __init__(self, prefix):
        super().__init__(timeout=180)
        self.prefix = prefix
        self.add_item(HelpCategorySelect(prefix))

    @discord.ui.button(label="View All Commands", style=discord.ButtonStyle.success, row=1)
    async def view_all_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_all_commands_embed(self.prefix)
        await interaction.response.edit_message(embed=embed, view=HelpBackView(self.prefix))


@bot.command(name="help")
async def custom_help(ctx):
    prefix = get_prefix(bot, ctx.message)
    embed = build_welcome_embed(prefix)
    await ctx.send(embed=embed, view=HelpSelectView(prefix))


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
