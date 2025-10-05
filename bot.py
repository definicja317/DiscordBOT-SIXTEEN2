# --== bot.py ==--
import os
import asyncio
from discord.errors import HTTPException, LoginFailure, GatewayNotFound
from aiohttp import ClientConnectorError
import logging
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ===== Strefa czasu „Europe/Warsaw” z bezpiecznym fallbackiem =====
def get_warsaw_tz():
    try:
        from zoneinfo import ZoneInfo
        try:
            return ZoneInfo("Europe/Warsaw")
        except Exception:
            try:
                import tzdata  # noqa: F401
                from zoneinfo import ZoneInfo as ZI2
                return ZI2("Europe/Warsaw")
            except Exception:
                pass
    except Exception:
        pass
    try:
        return datetime.now().astimezone().tzinfo
    except Exception:
        return None

WARSAW = get_warsaw_tz()

import discord
from discord.ext import commands
from discord import app_commands

# === NOWOŚĆ: prosty serwer HTTP dla Render/UptimeRobot ===
from aiohttp import web

async def _health(request):
    return web.Response(text="OK")

async def _setup_http():
    """Start a tiny HTTP health server.
    - Tries $PORT if provided, otherwise 10000, then falls back to an ephemeral port (0).
    - Binds to 127.0.0.1 for local dev to avoid firewall prompts & conflicts; override with HOST=0.0.0.0 if needed.
    - Never crashes the bot if binding fails.
    """
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)

    host_env = os.getenv("HOST")
    host = host_env or ("0.0.0.0" if os.getenv("PORT") else "127.0.0.1")
    ports_to_try: list[int] = []
    if os.getenv("PORT"):
        try:
            ports_to_try.append(int(os.getenv("PORT")))
        except Exception:
            pass
    ports_to_try += [10000, 0]  # 0 -> let OS choose a free port

    runner = web.AppRunner(app)
    await runner.setup()

    last_err = None
    for port in ports_to_try:
        try:
            site = web.TCPSite(runner, host=host, port=port)
            await site.start()
            actual_port = port
            try:
                # when port=0, get the real chosen port
                if getattr(site, "_server", None) and site._server.sockets:
                    actual_port = site._server.sockets[0].getsockname()[1]
            except Exception:
                pass
            logging.getLogger("http").info(f"HTTP health server running on {host}:{actual_port}")
            return
        except OSError as e:
            last_err = e
            logging.getLogger("http").warning(f"Port {port} unavailable ({e}); trying next...")
        except Exception as e:
            last_err = e
            logging.getLogger("http").exception("Health server start failed; continuing without it.")
            break

    # If we got here, we couldn't bind at all — continue without HTTP server
    logging.getLogger("http").warning(f"Health server NOT started. Reason: {last_err}")

# ===== Konfiguracja =====
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

# <<< TUTAJ WSTAW SWOJE ID ROLI >>>
REQUIRED_ROLE_ID = 1422343548216410151  # ← WSTAW ID roli, która ma dostęp do komend

# <<< USTAWIONE PRZEZ CIEBIE >>>
CAYO_IMAGE_URL   = "https://cdn.discordapp.com/attachments/1224129510535069766/1414204332747915274/image.png?ex=68e2f92b&is=68e1a7ab&hm=fd48d69d175f8c13eb6070b5ddcb5a40f2352df55288a8df2f48c85b577f4c28&"
ZANCUDO_IMAGE_URL= "https://cdn.discordapp.com/attachments/1224129510535069766/1414194392214011974/image.png?ex=68e2efe9&is=68e19e69&hm=c4734258cbcd6c77efde3327245c888cd68a2477eea198da96a97e024f664b56&"
LOGO_URL         = "https://cdn.discordapp.com/icons/1422343547780337819/683dd456c5dc7e124326d4d810992d07.webp?size=1024"
CAPT_CHANNEL_ID  = 1422343549386752000  # oznaczany kanał w ogłoszeniu CAPT

intents = discord.Intents.default()
intents.message_content = False
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("discord")

# Rejestry aktywnych ogłoszeń
ACTIVE_CAPTS: dict[tuple[int, int], "CaptView"] = {}
ACTIVE_AIRDROPS: dict[tuple[int, int], "AirdropView"] = {}
ACTIVE_EVENTS: dict[tuple[int, int], "EventView"] = {}
SQUADS: dict[int, dict] = {}  # {msg_id: {...}}

# ===== Check roli (z ID w kodzie) =====
def role_required_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.CheckFailure("Tej komendy można użyć tylko na serwerze.")
        m: discord.Member = interaction.user
        if m.guild_permissions.administrator or m == interaction.guild.owner:
            return True
        if REQUIRED_ROLE_ID == 0:
            raise app_commands.CheckFailure("Ustaw REQUIRED_ROLE_ID w kodzie bota.")
        if any(r.id == REQUIRED_ROLE_ID for r in m.roles):
            return True
        raise app_commands.CheckFailure("Nie masz wymaganej roli.")
    return app_commands.check(predicate)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = "Wystąpił błąd."
    if isinstance(error, app_commands.CheckFailure):
        msg = str(error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

# ===== Pomocnicze =====
def fmt_users(user_ids: list[int], guild: discord.Guild, limit: int = 25) -> str:
    if not user_ids:
        return "-"
    lines = []
    for uid in user_ids[:limit]:
        m = guild.get_member(uid)
        lines.append(f"• {m.mention} | {m.display_name}" if m else f"• <@{uid}>")
    if len(user_ids) > limit:
        lines.append(f"… (+{len(user_ids)-limit})")
    return "\n".join(lines)

def format_numbered_users(user_ids: list[int], guild: discord.Guild) -> list[str]:
    lines = []
    for i, uid in enumerate(user_ids, start=1):
        m = guild.get_member(uid)
        lines.append(f"{i}. {m.mention} | {m.display_name}" if m else f"{i}. <@{uid}>")
    return lines

def chunk_lines(lines: list[str], max_chars: int = 1800) -> list[str]:
    chunks, cur, cur_len = [], [], 0
    for line in lines:
        ln = len(line) + 1
        if cur_len + ln > max_chars and cur:
            chunks.append("\n".join(cur)); cur, cur_len = [line], ln
        else:
            cur.append(line); cur_len += ln
    if cur:
        chunks.append("\n".join(cur))
    return chunks

def _thumb_url(guild: discord.Guild) -> str | None:
    url = (LOGO_URL or "").strip()
    if url.lower().startswith("http"):
        return url
    try:
        if guild and guild.icon:
            return guild.icon.url
    except Exception:
        pass
    return None

def _channel_mention(guild: discord.Guild) -> str | None:
    if not CAPT_CHANNEL_ID:
        return None
    ch = guild.get_channel(CAPT_CHANNEL_ID)
    if isinstance(ch, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread)):
        return ch.mention
    return f"<#{CAPT_CHANNEL_ID}>"

def make_simple_ping_embed(title: str,
                           voice: discord.VoiceChannel,
                           starts_at: datetime,
                           guild: discord.Guild,
                           image_url: str) -> discord.Embed:
    ts = int(starts_at.timestamp())
    desc = (
        f"Zapraszamy na 🎧 {voice.mention}!\n\n"
        f"**Start:** <t:{ts}:t> • <t:{ts}:R>"
    )
    emb = discord.Embed(title=title, description=desc, color=0xFFFFFF)
    thumb = _thumb_url(guild)
    if thumb:
        emb.set_thumbnail(url=thumb)
    if image_url:
        emb.set_image(url=image_url)
    return emb

# ---------- EMBEDY CAPT ----------
def make_main_embed(starts_at: datetime, users: list[int], guild: discord.Guild,
                    author: discord.Member, image_url: str) -> discord.Embed:
    ts = int(starts_at.timestamp())
    chan = _channel_mention(guild)
    desc = (
        "Kliknij przycisk, aby się zapisać!\n\n"
        "**Czas rozpoczęcia:**\n"
        f"<t:{ts}:t>  •  <t:{ts}:R>\n"
    )
    if chan:
        desc += f"**Kanał:** {chan}\n"

    emb = discord.Embed(title="CAPTURES!", description=desc, color=0xFFFFFF)
    emb.add_field(name=f"Zapisani ({len(users)}):", value=fmt_users(users, guild), inline=False)

    thumb = _thumb_url(guild)
    if thumb: emb.set_thumbnail(url=thumb)
    if image_url: emb.set_image(url=image_url)

    emb.set_footer(text=f"Wystawione przez {author.display_name}")
    return emb

def make_pick_embed(selected_ids: list[int], total_count: int, guild: discord.Guild,
                    picker: discord.Member) -> discord.Embed:
    lines = []
    for i, uid in enumerate(selected_ids, start=1):
        m = guild.get_member(uid)
        lines.append(f"{i}. {m.mention} | {m.display_name}" if m else f"{i}. <@{uid}>")
    now_pl = datetime.now(tz=WARSAW) if WARSAW else datetime.now()
    desc = f"Wybrano {len(selected_ids)}/{total_count} osób:\n\n**Wybrani gracze:**\n" + ("\n".join(lines) if lines else "-")
    emb = discord.Embed(title="Lista osób na captures!", description=desc, color=0xFFFFFF)
    thumb = _thumb_url(guild)
    if thumb: emb.set_thumbnail(url=thumb)
    emb.set_footer(text=f"Wystawione przez {picker.display_name} • {now_pl.strftime('%d.%m.%Y %H:%M')}")
    return emb

# ---------- AIRDROP EMBEDY ----------
def make_airdrop_embed(starts_at: datetime, users: list[int], guild: discord.Guild,
                       author: discord.Member, info_text: str,
                       voice: discord.VoiceChannel | None, max_slots: int, queue_len: int) -> discord.Embed:
    ts = int(starts_at.timestamp())
    desc_parts: list[str] = []
    if info_text:
        desc_parts.append(f"{info_text}\n")
    desc_parts.append("**Kanał głosowy:**")
    desc_parts.append(voice.mention if voice else "-")
    desc_parts.append("")
    desc_parts.append("**Czas rozpoczęcia:**")
    desc_parts.append(f"Rozpoczęcie AirDrop o <t:{ts}:t> ( <t:{ts}:R> )")
    if max_slots and max_slots > 0:
        desc_parts.append("")
        desc_parts.append(f"**Kolejka:** {queue_len}")
    desc = "\n".join(desc_parts)

    field_name = f"Zapisani ({len(users)}/{max_slots})" if (max_slots and max_slots > 0) else f"Zapisani ({len(users)})"
    emb = discord.Embed(title="AirDrop!", description=desc, color=0xFFFFFF)
    emb.add_field(name=field_name, value="-", inline=False)
    thumb = _thumb_url(guild)
    if thumb:
        emb.set_thumbnail(url=thumb)
    emb.set_footer(text=f"Wystawione przez {author.display_name}")
    return emb

def make_airdrop_picked_embed(picked_ids: list[int], guild: discord.Guild, picker: discord.Member | None) -> discord.Embed:
    lines = []
    for i, uid in enumerate(picked_ids, start=1):
        m = guild.get_member(uid)
        lines.append(f"{i}. {m.mention} | {m.display_name}" if m else f"{i}. <@{uid}>")
    now_pl = datetime.now(tz=WARSAW) if WARSAW else datetime.now()
    desc = "**Wytypowani na AirDrop:**\n" + ("\n".join(lines) if lines else "-")
    emb = discord.Embed(title="Wytypowani na AirDrop!", description=desc, color=0xFFFFFF)
    thumb = _thumb_url(guild)
    if thumb: emb.set_thumbnail(url=thumb)
    footer_by = picker.display_name if isinstance(picker, discord.Member) else (picker or "Bot")
    emb.set_footer(text=f"Wytypował: {footer_by} • {now_pl.strftime('%d.%m.%Y %H:%M')}")
    return emb

# ---------- CAPT: PICK okno ----------
class PickView(discord.ui.View):
    def __init__(self, capt: "CaptView", option_rows: list[tuple[int, str, str | None]], total_count: int, picker: discord.Member):
        super().__init__(timeout=300)
        self.capt = capt
        self.total_count = total_count
        self.picker = picker
        options = []
        for idx, (uid, nick_label, user_desc) in enumerate(option_rows, start=1):
            label = f"{idx}. {nick_label}"[:100]
            desc  = (user_desc or f"ID {uid}")[:100]
            options.append(discord.SelectOption(label=label, value=str(uid), description=desc))
        max_vals = min(25, len(options)) or 1
        self.select = discord.ui.Select(placeholder="Wybierz graczy (max 25)", min_values=0, max_values=max_vals, options=options)
        self.add_item(self.select)

        async def _on_select(inter: discord.Interaction):
            chosen_ids = [int(v) for v in self.select.values]
            if not chosen_ids:
                txt = "Nic nie zaznaczono. Wybierz graczy, potem **Publikuj listę**."
            else:
                lines = []
                for i, uid in enumerate(chosen_ids, start=1):
                    m = capt.guild.get_member(uid)
                    lines.append(f"{i}. {m.display_name if m else f'ID {uid}'}")
                txt = f"Zaznaczono {len(chosen_ids)}/{self.total_count}:\n" + "\n".join(lines)
            await inter.response.edit_message(content=txt, view=self)
        self.select.callback = _on_select

    @discord.ui.button(label="Publikuj listę", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Publikuję listę…", ephemeral=True)
        chosen = [int(v) for v in self.select.values]
        if not chosen:
            return await interaction.followup.send("Nie wybrałeś żadnych osób.", ephemeral=True)
        self.capt.picked_list = list(dict.fromkeys(chosen))
        emb = make_pick_embed(chosen, len(self.capt.users), self.capt.guild, self.picker)
        msg = await interaction.channel.send(embed=emb)
        self.capt.pick_message = msg
        await interaction.followup.send("Opublikowano listę i zapisano wybór.", ephemeral=True)

    @discord.ui.button(label="Anuluj", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Anulowano wybieranie.", view=None)
        self.stop()

# ---------- CAPT: ogłoszenie ----------
class CaptView(discord.ui.View):
    def __init__(self, starts_at: datetime, guild: discord.Guild, author: discord.Member, image_url: str):
        try:
            remain = int((starts_at - datetime.now(tz=WARSAW)).total_seconds())
        except Exception:
            remain = 0
        timeout_seconds = max(60, remain + 3600)
        super().__init__(timeout=timeout_seconds)
        self.starts_at = starts_at
        self.users: list[int] = []
        self.picked_list: list[int] = []  # LISTA CAPTURES
        self.guild = guild
        self.author = author
        self.image_url = image_url
        self.message: discord.Message | None = None
        self.pick_message: discord.Message | None = None
        self._lock = asyncio.Lock()

    async def refresh_announce(self):
        if not self.message:
            return
        emb = make_main_embed(self.starts_at, self.users, self.guild, self.author, self.image_url)
        await self.message.edit(embed=emb, view=self)

    async def refresh_pick_embed(self, channel: discord.abc.Messageable, picker: discord.Member):
        if not self.picked_list:
            if self.pick_message:
                try:
                    emb = make_pick_embed([], len(self.users), self.guild, picker)
                    await self.pick_message.edit(embed=emb)
                except Exception:
                    self.pick_message = None
            return
        emb = make_pick_embed(self.picked_list, len(self.users), self.guild, picker)
        if self.pick_message:
            try:
                await self.pick_message.edit(embed=emb)
                return
            except Exception:
                self.pick_message = None
        try:
            msg = await channel.send(embed=emb)
            self.pick_message = msg
        except Exception:
            pass

    @discord.ui.button(label="Dołącz", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, _: discord.ui.Button):
        async with self._lock:
            uid = interaction.user.id
            if uid not in self.users:
                self.users.append(uid)
        await interaction.response.send_message("Dołączono.", ephemeral=True)
        await self.refresh_announce()

    @discord.ui.button(label="Opuść", style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button):
        async with self._lock:
            uid = interaction.user.id
            changed = False
            if uid in self.users:
                self.users.remove(uid); changed = True
            if uid in self.picked_list:
                self.picked_list.remove(uid); changed = True
        await interaction.response.send_message("Zaktualizowano.", ephemeral=True)
        if changed:
            await self.refresh_announce()
            await self.refresh_pick_embed(interaction.channel, interaction.user)

    @discord.ui.button(label="PICK", style=discord.ButtonStyle.primary)
    async def pick(self, interaction: discord.Interaction, _: discord.ui.Button):
        mem: discord.Member = interaction.user
        if not (mem.guild_permissions.administrator or mem == self.author or any(r.id == REQUIRED_ROLE_ID for r in mem.roles)):
            await interaction.response.send_message("Tylko wystawiający / admin / rola uprawniona może wybierać osoby.", ephemeral=True)
            return
        if not self.users:
            await interaction.response.send_message("Nikt się jeszcze nie zapisał.", ephemeral=True)
            return
        displayed_ids = self.users[:25]
        option_rows: list[tuple[int, str, str | None]] = []
        for uid in displayed_ids:
            m = self.guild.get_member(uid)
            if m is None:
                try:
                    m = await self.guild.fetch_member(uid)
                except Exception:
                    m = None
            nick_label = m.display_name if m else f"Użytkownik {uid}"
            user_desc = f"{m.name}" if m else None
            option_rows.append((uid, nick_label, user_desc))
        view = PickView(self, option_rows, len(self.users), mem)
        await interaction.response.send_message(
            "Wybierz graczy (pokazane NICKI). Potem kliknij **Publikuj listę**.",
            view=view, ephemeral=True
        )

# ===== [CAPT] Panel – LISTA CAPTURES =====
class CaptAddInstantView(discord.ui.View):
    def __init__(self, capt: "CaptView"):
        super().__init__(timeout=240)
        self.capt = capt
        self.user_select = discord.ui.UserSelect(
            placeholder="Wybierz osoby do LISTY CAPTURES (działa natychmiast)",
            min_values=1, max_values=25
        )
        self.add_item(self.user_select)
        async def _on_pick(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True, thinking=False)
            added = 0
            names = []
            for u in self.user_select.values:
                if u.id not in self.capt.picked_list:
                    self.capt.picked_list.append(u.id); added += 1
                names.append(f"- {getattr(u, 'display_name', getattr(u, 'name', ''))}")
            await self.capt.refresh_pick_embed(inter.channel, inter.user)
            await inter.followup.send(f"✅ Dodano do **listy CAPTURES**: **{added}**.\n" + ("\n".join(names) if names else ""), ephemeral=True)
        self.user_select.callback = _on_pick
    @discord.ui.button(label="Zamknij", style=discord.ButtonStyle.secondary)
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Zamknięto.", view=None)

class CaptRemoveInstantView(discord.ui.View):
    def __init__(self, capt: "CaptView"):
        super().__init__(timeout=240)
        self.capt = capt
        if not capt.picked_list:
            self.add_item(discord.ui.Button(label="Lista CAPTURES jest pusta", style=discord.ButtonStyle.secondary, disabled=True))
            return
        options = []
        for uid in capt.picked_list[:25]:
            m = capt.guild.get_member(uid)
            label = (m.display_name if m else f"User {uid}")[:100]
            desc  = (f"@{m.name}" if m else f"ID {uid}")[:100]
            options.append(discord.SelectOption(label=label, value=str(uid), description=desc))
        self.sel = discord.ui.Select(
            placeholder="Zaznacz osoby do usunięcia z LISTY CAPTURES (instant)",
            min_values=1, max_values=len(options), options=options
        )
        self.add_item(self.sel)
        async def _on_remove(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True, thinking=False)
            chosen = [int(v) for v in self.sel.values]
            removed_names = []
            before = set(self.capt.picked_list)
            self.capt.picked_list = [uid for uid in self.capt.picked_list if uid not in chosen]
            for uid in chosen:
                m = self.capt.guild.get_member(uid)
                removed_names.append(f"- {m.display_name if m else f'ID {uid}'}")
            removed = len(before) - len(set(self.capt.picked_list))
            await self.capt.refresh_pick_embed(inter.channel, inter.user)
            await inter.followup.send(f"✅ Usunięto z **listy CAPTURES**: **{removed}**.\n" + ("\n".join(removed_names) if removed_names else ""), ephemeral=True)
        self.sel.callback = _on_remove
    @discord.ui.button(label="Zamknij", style=discord.ButtonStyle.secondary)
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Zamknięto.", view=None)

class PanelView(discord.ui.View):
    def __init__(self, capt: "CaptView", opener: discord.Member):
        super().__init__(timeout=600)
        self.capt = capt
        self.opener = opener
    @discord.ui.button(label="Dodaj do listy CAPTURES", style=discord.ButtonStyle.success)
    async def add_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Wybierz osoby do **listy CAPTURES**:", view=CaptAddInstantView(self.capt), ephemeral=True)
    @discord.ui.button(label="Usuń z listy CAPTURES", style=discord.ButtonStyle.danger)
    async def rem_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Zaznacz osoby do **usunięcia z listy CAPTURES**:", view=CaptRemoveInstantView(self.capt), ephemeral=True)
    @discord.ui.button(label="Pokaż listę zapisanych", style=discord.ButtonStyle.secondary)
    async def show_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.capt.users:
            return await interaction.response.send_message("Brak zapisanych.", ephemeral=True)
        lines = format_numbered_users(self.capt.users, self.capt.guild)
        parts = chunk_lines(lines, 1800)
        await interaction.response.send_message(f"**Lista zapisanych (część 1):**\n{parts[0]}", ephemeral=True)
        for i, p in enumerate(parts[1:], start=2):
            await interaction.followup.send(f"**Lista zapisanych (część {i}):**\n{p}", ephemeral=True)

# ---------- AIRDROP: PICK (max 20) ----------
class AirdropPickView(discord.ui.View):
    def __init__(self, adr: "AirdropView", option_rows: list[tuple[int, str, str | None]], picker: discord.Member):
        super().__init__(timeout=300)
        self.adr = adr
        self.picker = picker
        options = []
        for idx, (uid, nick, desc) in enumerate(option_rows, start=1):
            options.append(discord.SelectOption(label=(f"{idx}. {nick}")[:100], value=str(uid), description=(desc or f"ID {uid}")[:100]))
        max_vals = min(20, len(options)) or 1
        self.sel = discord.ui.Select(placeholder="Wybierz osoby (max 20)", min_values=0, max_values=max_vals, options=options)
        self.add_item(self.sel)
        async def _on_select(inter: discord.Interaction):
            chosen_ids = [int(v) for v in self.sel.values]
            if not chosen_ids:
                txt = "Nic nie zaznaczono. Kliknij **Publikuj Wytypowanych**."
            else:
                lines = []
                for i, uid in enumerate(chosen_ids, start=1):
                    m = adr.guild.get_member(uid)
                    lines.append(f"{i}. {m.display_name if m else f'User {uid}'}")
                txt = f"Zaznaczono {len(chosen_ids)}:\n" + "\n".join(lines)
            await inter.response.edit_message(content=txt, view=self)
        self.sel.callback = _on_select
    @discord.ui.button(label="Publikuj Wytypowanych", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button):
        chosen = [int(v) for v in self.sel.values]
        if not chosen:
            return await interaction.response.edit_message(content="Nie wybrałeś żadnych osób.", view=self)
        self.adr.picked_list = list(dict.fromkeys(self.adr.picked_list + chosen))
        await self.adr.refresh_picked_embed(interaction.channel, interaction.user)
        await interaction.response.edit_message(content="Opublikowano/odświeżono listę **Wytypowani na AirDrop!**", view=self)
    @discord.ui.button(label="Anuluj", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Anulowano wybór.", view=None)
        self.stop()

# ---------- AIRDROP: ogłoszenie ----------
class AirdropView(discord.ui.View):
    def __init__(self, starts_at: datetime, guild: discord.Guild, author: discord.Member,
                 info_text: str, voice: discord.VoiceChannel | None, max_slots: int):
        try:
            remain = int((starts_at - datetime.now(tz=WARSAW)).total_seconds())
        except Exception:
            remain = 0
        timeout_seconds = max(60, remain + 3600)
        super().__init__(timeout=timeout_seconds)
        self.starts_at = starts_at
        self.guild = guild
        self.author = author
        self.info_text = info_text
        self.voice = voice
        self.max_slots = max(0, int(max_slots or 0))  # 0 = bez limitu
        self.users: list[int] = []   # zapisani
        self.queue: list[int] = []   # kolejka (gdy limit)
        self.picked_list: list[int] = []  # WYTYPOWANI (drugi embed)
        self.message: discord.Message | None = None
        self.picked_message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self.queue: list[int] = []
        self.queue: list[int] = []  # kolejka chętnych (gdy limit miejsc)


    async def refresh_embed(self):
        if not self.message:
            return
        is_full = (self.max_slots > 0 and len(self.users) >= self.max_slots)
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label == "Dołącz":
                item.disabled = is_full
            if isinstance(item, discord.ui.Button) and item.label == "Dołącz do kolejki":
                item.disabled = (self.max_slots <= 0)
        emb = make_airdrop_embed(self.starts_at, self.users, self.guild, self.author, self.info_text, self.voice, self.max_slots, len(self.queue))
        await self.message.edit(embed=emb, view=self)

    async def refresh_picked_embed(self, channel: discord.abc.Messageable, picker: discord.Member | None):
        emb = make_airdrop_picked_embed(self.picked_list, self.guild, picker or self.author)
        if self.picked_message:
            try:
                await self.picked_message.edit(embed=emb)
                return
            except Exception:
                self.picked_message = None
        try:
            self.picked_message = await channel.send(embed=emb)
        except Exception:
            pass

    @discord.ui.button(label="Dołącz", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, _: discord.ui.Button):
        async with self._lock:
            uid = interaction.user.id
            if uid in self.users:
                added = False
            else:
                if self.max_slots > 0 and len(self.users) >= self.max_slots:
                    await interaction.response.send_message(f"Limit miejsc osiągnięty ({self.max_slots}). Użyj **Dołącz do kolejki**.", ephemeral=True)
                    return
                self.users.append(uid)
                if uid in self.queue:
                    self.queue.remove(uid)
        await interaction.response.send_message("Dołączono.", ephemeral=True)
        await self.refresh_embed()

    @discord.ui.button(label="Opuść", style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button):
        async with self._lock:
            uid = interaction.user.id
            changed = False
            if uid in self.users:
                self.users.remove(uid); changed = True
            if uid in self.queue:
                self.queue.remove(uid); changed = True
            if uid in self.picked_list:
                self.picked_list.remove(uid); changed = True
        await interaction.response.send_message("Zaktualizowano.", ephemeral=True)
        if changed:
            await self.refresh_embed()
            await self.refresh_picked_embed(interaction.channel, interaction.user)

    @discord.ui.button(label="Dołącz do kolejki", style=discord.ButtonStyle.primary)
    async def join_queue(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.max_slots <= 0:
            await interaction.response.send_message("Kolejka działa tylko przy ustawionym limicie miejsc.", ephemeral=True)
            return
        async with self._lock:
            uid = interaction.user.id
            if uid in self.users:
                await interaction.response.send_message("Jesteś już zapisany. Nie trzeba do kolejki.", ephemeral=True); return
            if len(self.users) < self.max_slots:
                await interaction.response.send_message("Są wolne miejsca - kliknij **Dołącz**.", ephemeral=True); return
            if uid in self.queue:
                await interaction.response.send_message("Jesteś już w kolejce.", ephemeral=True); return
            self.queue.append(uid)
        await interaction.response.send_message("Dodano do kolejki.", ephemeral=True)
        await self.refresh_embed()

    @discord.ui.button(label="PICK", style=discord.ButtonStyle.secondary)
    async def pick_from_signups(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.users:
            await interaction.response.send_message("Brak zapisanych.", ephemeral=True)
            return
        option_rows = []
        for uid in self.users[:20]:
            m = self.guild.get_member(uid)
            nick = m.display_name if m else f"User {uid}"
            desc = f"@{m.name}" if m else f"ID {uid}"
            option_rows.append((uid, nick, desc))
        view = AirdropPickView(self, option_rows, interaction.user)
        await interaction.response.send_message(
            "Wybierz osoby z **zapisanych** (**max 20**) do wytypowania. Następnie kliknij **Publikuj Wytypowanych**.",
            view=view, ephemeral=True
        )

# ===== [PATCH] AIRDROP – instant dod/usuń WYTYPOWANYCH =====
class AirdropAddAnyView(discord.ui.View):
    def __init__(self, adr: "AirdropView"):
        super().__init__(timeout=240)
        self.adr = adr
        self.user_select = discord.ui.UserSelect(
            placeholder="Wybierz osoby do WYTYPOWANYCH (działa natychmiast)",
            min_values=1, max_values=25
        )
        self.add_item(self.user_select)
        async def _on_pick(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True, thinking=False)
            chosen = list(self.user_select.values)
            added = 0
            names = []
            for u in chosen:
                if u.id not in self.adr.picked_list:
                    self.adr.picked_list.append(u.id); added += 1
                names.append(f"- {getattr(u,'display_name', getattr(u,'name',''))}")
            await self.adr.refresh_picked_embed(inter.channel, inter.user)
            await inter.followup.send(f"✅ Dodano do WYTYPOWANYCH: **{added}**.\n" + ("\n".join(names) if names else ""), ephemeral=True)
        self.user_select.callback = _on_pick
    @discord.ui.button(label="Zamknij", style=discord.ButtonStyle.secondary)
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Zamknięto.", view=None)

class AirdropRemovePickedView(discord.ui.View):
    def __init__(self, adr: "AirdropView"):
        super().__init__(timeout=240)
        self.adr = adr
        if not adr.picked_list:
            self.add_item(discord.ui.Button(label="Lista WYTYPOWANYCH pusta", style=discord.ButtonStyle.secondary, disabled=True))
            return
        options = []
        for uid in adr.picked_list[:25]:
            m = adr.guild.get_member(uid)
            options.append(discord.SelectOption(label=(m.display_name if m else f"User {uid}")[:100],
                                               value=str(uid),
                                               description=(f"@{m.name}" if m else f"ID {uid}")[:100]))
        self.sel = discord.ui.Select(
            placeholder="Zaznacz osoby do usunięcia (działa natychmiast)",
            min_values=1, max_values=len(options), options=options
        )
        self.add_item(self.sel)
        async def _on_remove(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True, thinking=False)
            chosen = [int(v) for v in self.sel.values]
            removed_names = []
            for uid in chosen:
                m = self.adr.guild.get_member(uid)
                removed_names.append(f"- {m.display_name if m else f'ID {uid}'}")
            before = set(self.adr.picked_list)
            self.adr.picked_list = [uid for uid in self.adr.picked_list if uid not in chosen]
            removed = len(before) - len(set(self.adr.picked_list))
            await self.adr.refresh_picked_embed(inter.channel, inter.user)
            await inter.followup.send(f"✅ Usunięto z WYTYPOWANYCH: **{removed}**.\n" + ("\n".join(removed_names) if removed_names else ""), ephemeral=True)
        self.sel.callback = _on_remove
    @discord.ui.button(label="Zamknij", style=discord.ButtonStyle.secondary)
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Zamknięto.", view=None)

class AirdropWinnersModal(discord.ui.Modal, title="Losuj z kolejki"):
    def __init__(self, adr: "AirdropView"):
        super().__init__(timeout=180)
        self.adr = adr
        self.count = discord.ui.TextInput(label="Ile osób wylosować?", placeholder="np. 5", required=True, max_length=3)
        self.add_item(self.count)
    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(str(self.count.value).strip()); assert n > 0
        except Exception:
            return await interaction.response.send_message("Podaj poprawną liczbę > 0.", ephemeral=True)
        pool = list(self.adr.queue)
        if not pool:
            return await interaction.response.send_message("Kolejka jest pusta.", ephemeral=True)
        winners = random.sample(pool, k=min(n, len(pool)))
        for uid in winners:
            if uid not in self.adr.picked_list:
                self.adr.picked_list.append(uid)
        await self.adr.refresh_picked_embed(interaction.channel, interaction.user)
        await interaction.response.send_message("Wylosowano i dodano do **Wytypowanych** (z kolejki).", ephemeral=True)

class AirdropPanelView(discord.ui.View):
    def __init__(self, adr: AirdropView, opener: discord.Member):
        super().__init__(timeout=600)
        self.adr = adr
        self.opener = opener
    @discord.ui.button(label="Dodaj do WYTYPOWANYCH", style=discord.ButtonStyle.success)
    async def add_any(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Wybierz osoby z **całego serwera** do dodania:",
                                                view=AirdropAddAnyView(self.adr), ephemeral=True)
    @discord.ui.button(label="Usuń z WYTYPOWANYCH", style=discord.ButtonStyle.danger)
    async def rem_picked(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Zaznacz osoby do **usunięcia** z WYTYPOWANYCH:",
                                                view=AirdropRemovePickedView(self.adr), ephemeral=True)
    @discord.ui.button(label="Pokaż listę zapisanych", style=discord.ButtonStyle.secondary, row=1)
    async def show_signed(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.adr.users:
            return await interaction.response.send_message("Brak zapisanych.", ephemeral=True)
        lines = format_numbered_users(self.adr.users, interaction.guild)
        parts = chunk_lines(lines, max_chars=1800)
        await interaction.response.send_message(f"**Lista zapisanych (część 1):**\n{parts[0]}", ephemeral=True)
        for idx, p in enumerate(parts[1:], start=2):
            await interaction.followup.send(f"**Lista zapisanych (część {idx}):**\n{p}", ephemeral=True)
    @discord.ui.button(label="Losuj z kolejki", style=discord.ButtonStyle.primary)
    async def winners_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(AirdropWinnersModal(self.adr))

# ===== Komendy podstawowe =====
@bot.tree.command(name="ping", description="Sprawdź opóźnienie bota.")
@role_required_check()
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency*1000)}ms", ephemeral=True)

@bot.tree.command(name="say", description="Bot powtórzy twoją wiadomość.")
@role_required_check()
async def say(interaction: discord.Interaction, text: str):
    await interaction.response.send_message(text)

# ===== CREATE CAPT =====
@bot.tree.command(name="create-capt", description="Utwórz CAPT z odliczaniem, zdjęciem i pingiem @everyone.")
@role_required_check()
@app_commands.describe(start_time="Godzina startu 24h, np. 15:40 (czas Polski).",
                       image_url="Link do dużego zdjęcia (pokaże się w embeddzie).")
async def create_capt(interaction: discord.Interaction, start_time: str, image_url: str):
    try:
        hh, mm = (int(x) for x in start_time.strip().split(":"))
        assert 0 <= hh <= 23 and 0 <= mm <= 59
    except Exception:
        return await interaction.response.send_message("Podaj godzinę **HH:MM** (np. 15:40).", ephemeral=True)
    try:
        now_pl = datetime.now(tz=WARSAW)
        today_start = datetime(now_pl.year, now_pl.month, now_pl.day, hh, mm, tzinfo=WARSAW)
        starts_at = today_start if today_start > now_pl else today_start + timedelta(days=1)
    except Exception:
        now_local = datetime.now()
        today_start = datetime(now_local.year, now_local.month, now_local.day, hh, mm)
        starts_at = today_start if today_start > now_local else today_start + timedelta(days=1)
    author = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
    view = CaptView(starts_at, interaction.guild, author, image_url)
    embed = make_main_embed(starts_at, [], interaction.guild, author, image_url)
    allowed = discord.AllowedMentions(everyone=True)
    await interaction.response.send_message(content="@everyone", embed=embed, view=view, allowed_mentions=allowed)
    msg = await interaction.original_response()
    view.message = msg
    ACTIVE_EVENTS[(interaction.guild.id, interaction.channel.id)] = view
    ACTIVE_CAPTS[(interaction.guild.id, interaction.channel.id)] = view
    async def ticker():
        try:
            while True:
                await asyncio.sleep(15)
                reached = False
                try:
                    reached = datetime.now(tz=WARSAW) >= starts_at
                except Exception:
                    pass
                if reached:
                    final = make_main_embed(starts_at, view.users, interaction.guild, author, image_url)
                    final.description += "\n**CAPT rozpoczął się.**"
                    await msg.edit(embed=final, view=view)
                    break
        except asyncio.CancelledError:
            pass
    interaction.client.loop.create_task(ticker())

# ===== PANEL CAPT =====
@bot.tree.command(name="panel-capt", description="Otwórz panel CAPT w tym kanale.")
@role_required_check()
async def panel_capt(interaction: discord.Interaction):
    key = (interaction.guild.id, interaction.channel.id)
    capt = ACTIVE_CAPTS.get(key)
    if not capt or not capt.message:
        return await interaction.response.send_message("Brak aktywnego ogłoszenia w tym kanale.", ephemeral=True)
    mem: discord.Member = interaction.user
    if not (mem.guild_permissions.administrator or mem == capt.author or any(r.id == REQUIRED_ROLE_ID for r in mem.roles)):
        return await interaction.response.send_message("Panel dostępny dla wystawiającego, administratora lub roli uprawnionej.", ephemeral=True)
    view = PanelView(capt, mem)
    await interaction.response.send_message(
        f"Panel CAPT – zapisanych: **{len(capt.users)}**, WYBRANI: **{len(capt.picked_list)}**.",
        view=view, ephemeral=True
    )

# ===== AIRDROP =====
@bot.tree.command(name="airdrop", description="Utwórz AirDrop (opis, głosowy, timer, licznik; opcjonalny limit i kolejka).")
@role_required_check()
@app_commands.describe(
    info_text="Tekst w opisie (np. zasady/uwagi).",
    voice="Kanał głosowy do AirDropa.",
    start_time="Godzina startu 24h, np. 20:00 (czas Polski).",
    max_slots="Maks. liczba zapisanych (0 = bez limitu)."
)
async def airdrop(interaction: discord.Interaction, info_text: str, voice: discord.VoiceChannel, start_time: str, max_slots: int = 0):
    try:
        hh, mm = (int(x) for x in start_time.strip().split(":"))
        assert 0 <= hh <= 23 and 0 <= mm <= 59
    except Exception:
        return await interaction.response.send_message("Podaj godzinę **HH:MM** (np. 20:00).", ephemeral=True)
    try:
        now_pl = datetime.now(tz=WARSAW)
        today_start = datetime(now_pl.year, now_pl.month, now_pl.day, hh, mm, tzinfo=WARSAW)
        starts_at = today_start if today_start > now_pl else today_start + timedelta(days=1)
    except Exception:
        now_local = datetime.now()
        today_start = datetime(now_local.year, now_local.month, now_local.day, hh, mm)
        starts_at = today_start if today_start > now_local else today_start + timedelta(days=1)
    author = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
    view = AirdropView(starts_at, interaction.guild, author, info_text, voice, max_slots)
    embed = make_airdrop_embed(starts_at, [], interaction.guild, author, info_text, voice, max_slots, queue_len=0)
    allowed = discord.AllowedMentions(everyone=True)
    await interaction.response.send_message(content="@everyone", embed=embed, view=view, allowed_mentions=allowed)
    msg = await interaction.original_response()
    view.message = msg
    ACTIVE_AIRDROPS[(interaction.guild.id, interaction.channel.id)] = view
    async def ticker():
        try:
            while True:
                await asyncio.sleep(15)
                reached = False
                try:
                    reached = datetime.now(tz=WARSAW) >= starts_at
                except Exception:
                    pass
                if reached:
                    final = make_airdrop_embed(starts_at, view.users, interaction.guild, author, info_text, voice, max_slots, len(view.queue))
                    final.description += "\n**AirDrop rozpoczął się.**"
                    await msg.edit(embed=final, view=view)
                    break
        except asyncio.CancelledError:
            pass
    interaction.client.loop.create_task(ticker())

# ===== PANEL AIRDROP =====
@bot.tree.command(name="panel-airdrop", description="Otwórz panel AIRDROP w tym kanale (zarządza WYTYPOWANYMI).")
@role_required_check()
async def panel_airdrop(interaction: discord.Interaction):
    key = (interaction.guild.id, interaction.channel.id)
    adr = ACTIVE_AIRDROPS.get(key)
    if not adr or not adr.message:
        return await interaction.response.send_message("Brak aktywnego airdropa w tym kanale.", ephemeral=True)
    mem: discord.Member = interaction.user
    if not (mem.guild_permissions.administrator or mem == adr.author or any(r.id == REQUIRED_ROLE_ID for r in mem.roles)):
        return await interaction.response.send_message("Panel dostępny dla wystawiającego, administratora lub roli uprawnionej.", ephemeral=True)
    view = AirdropPanelView(adr, mem)
    lim = f" / {adr.max_slots}" if adr.max_slots else ""
    queue_info = f", kolejka: **{len(adr.queue)}**" if adr.max_slots else ""
    await interaction.response.send_message(
        f"Panel AIRDROP – zapisanych: **{len(adr.users)}{lim}**{queue_info}. WYTYPOWANI: **{len(adr.picked_list)}**.",
        view=view, ephemeral=True
    )


# ===== EVENT: helpers, views, command =====
class EventSignupModal(discord.ui.Modal, title="Zapis na event"):
    def __init__(self, ev_view: "EventView"):
        super().__init__(timeout=180)
        self.ev_view = ev_view
        self.info = discord.ui.TextInput(
            label="Imię Nazwisko | UID",
            placeholder="np. Jan Kowalski | 123456 (lub dowolny opis)",
            max_length=100,
            required=True,
        )
        self.add_item(self.info)

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.info.value).strip()
        if not text:
            return await interaction.response.send_message("Wpisz dowolny opis (np. Imię Nazwisko | UID).", ephemeral=True)
        async with self.ev_view._lock:
            uid = interaction.user.id
            if self.ev_view.max_slots > 0 and len(self.ev_view.signups) >= self.ev_view.max_slots and uid not in self.ev_view.signups:
                return await interaction.response.send_message(f"Limit zapisów osiągnięty ({self.ev_view.max_slots}).", ephemeral=True)
            self.ev_view.signups[uid] = text
        await interaction.response.send_message("✅ Zapisano na event.", ephemeral=True)
        await self.ev_view.refresh_embed()

def make_event_embed(ev_view: "EventView") -> discord.Embed:
    ts_start = int(ev_view.starts_at.timestamp())
    ts_tp = int(ev_view.teleport_at.timestamp())
    desc = (
        f"**Start:** <t:{ts_start}:t> • <t:{ts_start}:R>\n"
        f"**Teleportacja:** <t:{ts_tp}:t> • <t:{ts_tp}:R>\n"
        f"**Kanał głosowy:** {ev_view.voice.mention if ev_view.voice else '-'}\n\n"
        "Kliknij **Zapisz się** aby dołączyć. Wpisz: `Imię Nazwisko | UID`.\n"
    )
    count = len(ev_view.signups)
    field_name = f"Zapisani ({count}/{ev_view.max_slots})" if ev_view.max_slots > 0 else f"Zapisani ({count})"
    emb = discord.Embed(title=ev_view.name, description=desc, color=0xFFFFFF)
    if _thumb_url(ev_view.guild): emb.set_thumbnail(url=_thumb_url(ev_view.guild))
    emb.add_field(name=field_name, value="-", inline=False)  # nie pokazujemy listy
    return emb

def make_event_picked_embed(ev_view: "EventView", picker: discord.Member | None) -> discord.Embed:
    lines = []
    for i, uid in enumerate(ev_view.picked_ids, start=1):
        m = ev_view.guild.get_member(uid)
        user_label = f"{m.mention} | {ev_view.signups.get(uid, '')}" if m else f"<@{uid}> | {ev_view.signups.get(uid, '')}"
        lines.append(f"{i}. {user_label}")
    now_pl = datetime.now(tz=WARSAW) if WARSAW else datetime.now()
    title = f"Wytypowani na {ev_view.name}!"
    desc = "**Wytypowani na Event:**\n" + ("\n".join(lines) if lines else "-")
    emb = discord.Embed(title=title, description=desc, color=0xFFFFFF)
    if _thumb_url(ev_view.guild): emb.set_thumbnail(url=_thumb_url(ev_view.guild))
    footer_by = picker.display_name if isinstance(picker, discord.Member) else (picker or "Bot")
    emb.set_footer(text=f"Wytypował: {footer_by} • {now_pl.strftime('%d.%m.%Y %H:%M')}")
    return emb

class EventPickView(discord.ui.View):
    def __init__(self, ev_view: "EventView", picker: discord.Member):
        super().__init__(timeout=300)
        self.ev_view = ev_view
        self.picker = picker
        options = []
        signed = list(ev_view.signups.items())
        # pokaż max 25 na raz (Discord limit)
        for idx, (uid, info) in enumerate(signed[:25], start=1):
            m = ev_view.guild.get_member(uid)
            label = (f"{idx}. {(m.display_name if m else 'Użytkownik')}")[:100]
            desc = (info or f"ID {uid}")[:100]
            options.append(discord.SelectOption(label=label, value=str(uid), description=desc))
        max_vals = min(ev_view.max_slots if ev_view.max_slots > 0 else 25, len(options)) or 1
        self.sel = discord.ui.Select(placeholder="Wybierz osoby do wystawienia", min_values=0, max_values=max_vals, options=options)
        self.add_item(self.sel)

        async def _on_select(inter: discord.Interaction):
            chosen_ids = [int(v) for v in self.sel.values]
            if not chosen_ids:
                txt = "Nic nie zaznaczono. Kliknij **Publikuj wytypowanych**."
            else:
                lines = []
                for i, uid in enumerate(chosen_ids, start=1):
                    m = ev_view.guild.get_member(uid)
                    lines.append(f"{i}. {m.display_name if m else f'ID {uid}'}")
                txt = f"Zaznaczono {len(chosen_ids)}:\n" + "\n".join(lines)
            await inter.response.edit_message(content=txt, view=self)
        self.sel.callback = _on_select

    @discord.ui.button(label="Publikuj wytypowanych", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button):
        chosen = [int(v) for v in self.sel.values]
        if not chosen:
            return await interaction.response.edit_message(content="Nie wybrałeś żadnych osób.", view=self)
        # scalaj z poprzednimi wyborami (bez duplikatów)
        self.ev_view.picked_ids = list(dict.fromkeys(self.ev_view.picked_ids + chosen))
        await self.ev_view.refresh_picked_embed(interaction.channel, interaction.user)
        await interaction.response.edit_message(content="Opublikowano/odświeżono listę wytypowanych.", view=self)

    @discord.ui.button(label="Anuluj", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Anulowano.", view=None)
        self.stop()

class EventView(discord.ui.View):
    def __init__(self, name: str, starts_at: datetime, teleport_at: datetime,
                 voice: discord.VoiceChannel, guild: discord.Guild, author: discord.Member, max_slots: int = 0):
        try:
            remain = int((starts_at - datetime.now(tz=WARSAW)).total_seconds())
        except Exception:
            remain = 0
        timeout_seconds = max(60, remain + 3600)
        super().__init__(timeout=timeout_seconds)
        self.name = name
        self.starts_at = starts_at
        self.teleport_at = teleport_at
        self.voice = voice
        self.guild = guild
        self.author = author
        self.max_slots = max(0, int(max_slots or 0))
        self.signups: dict[int, str] = {}  # user_id -> "Imię Nazwisko | UID"
        self.picked_ids: list[int] = []
        self.message: discord.Message | None = None
        self.picked_message: discord.Message | None = None
        self._lock = asyncio.Lock()

    async def refresh_embed(self):
        if self.message:
            await self.message.edit(embed=make_event_embed(self), view=self)

    async def refresh_picked_embed(self, channel: discord.abc.Messageable, picker: discord.Member | None):
        emb = make_event_picked_embed(self, picker)
        if self.picked_message:
            try:
                await self.picked_message.edit(embed=emb)
                return
            except Exception:
                self.picked_message = None
        try:
            self.picked_message = await channel.send(embed=emb)
        except Exception:
            pass

    @discord.ui.button(label="Zapisz się", style=discord.ButtonStyle.success)
    async def signup(self, interaction: discord.Interaction, _: discord.ui.Button):
        async with self._lock:
            if self.max_slots > 0 and len(self.signups) >= self.max_slots and interaction.user.id not in self.signups:
                return await interaction.response.send_message(f"Limit zapisów osiągnięty ({self.max_slots}).", ephemeral=True)
        await interaction.response.send_modal(EventSignupModal(self))

    @discord.ui.button(label="Opuść", style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button):
        async with self._lock:
            uid = interaction.user.id
            changed = False
            if uid in self.signups:
                del self.signups[uid]; changed = True
            if uid in self.picked_ids:
                self.picked_ids.remove(uid); changed = True
            if uid in self.queue:
                self.queue.remove(uid); changed = True
        await interaction.response.send_message("Zaktualizowano.", ephemeral=True)
        if changed:
            await self.refresh_embed()
            await self.refresh_picked_embed(interaction.channel, interaction.user)

    
    @discord.ui.button(label="Dołącz do chętnych", style=discord.ButtonStyle.secondary)
    async def join_queue(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.max_slots <= 0:
            return await interaction.response.send_message("Kolejka działa tylko przy ustawionym limicie miejsc.", ephemeral=True)
        async with self._lock:
            uid = interaction.user.id
            if uid in self.signups:
                return await interaction.response.send_message("Już jesteś zapisany - kolejka niepotrzebna.", ephemeral=True)
            if len(self.signups) < self.max_slots:
                return await interaction.response.send_message("Są wolne miejsca - użyj **Zapisz się**.", ephemeral=True)
            if uid in self.queue:
                return await interaction.response.send_message("Już jesteś w kolejce chętnych.", ephemeral=True)
            self.queue.append(uid)
        await interaction.response.send_message("Dodano do kolejki chętnych.", ephemeral=True)
        await self.refresh_embed()
    @discord.ui.button(label="Wystaw na event (ADMIN)", style=discord.ButtonStyle.primary)
    async def publish_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        m: discord.Member = interaction.user
        is_admin = (m.guild_permissions.administrator or m == self.author or any(r.id == REQUIRED_ROLE_ID for r in m.roles))
        if not is_admin:
            return await interaction.response.send_message("Tylko wystawiający / admin / rola uprawniona może wystawiać.", ephemeral=True)
        if not self.signups:
            return await interaction.response.send_message("Brak zapisanych.", ephemeral=True)
        view = EventPickView(self, m)
        await interaction.response.send_message("Wybierz osoby do **wystawienia** (możesz uruchomić kilka razy aby dodać więcej).",
                                                view=view, ephemeral=True)

@bot.tree.command(name="create-event", description="Tworzy ogłoszenie eventu z zapisami i wystawianiem.")
@role_required_check()
@app_commands.describe(
    name="Nazwa eventu (np. MCL)",
    voice="Kanał głosowy do zbiórki",
    start_time="Godzina startu 24h, np. 18:30 (PL)",
    teleport_time="Godzina teleportacji 24h, np. 18:15 (PL)",
    max_slots="Maksymalna liczba zapisów (0 = bez limitu)"
)
async def create_event(interaction: discord.Interaction, name: str, voice: discord.VoiceChannel,
                       start_time: str, teleport_time: str, max_slots: int = 0):
    # parsowanie czasu
    try:
        starts_at = _parse_pl_time(start_time)
    except Exception:
        return await interaction.response.send_message("Podaj **start** w formacie HH:MM (np. 18:30).", ephemeral=True)
    try:
        teleport_at = _parse_pl_time(teleport_time)
    except Exception:
        return await interaction.response.send_message("Podaj **teleport** w formacie HH:MM (np. 18:15).", ephemeral=True)

    author = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
    view = EventView(name, starts_at, teleport_at, voice, interaction.guild, author, max_slots)
    embed = make_event_embed(view)

    await interaction.response.send_message(embed=embed, view=view)
    msg = await interaction.original_response()
    view.message = msg
    ACTIVE_EVENTS[(interaction.guild.id, interaction.channel.id)] = view

# ===== EVENT PANEL =====
class EventAddPickedView(discord.ui.View):
    def __init__(self, ev: "EventView"):
        super().__init__(timeout=240)
        self.ev = ev
        if not ev.signups:
            self.add_item(discord.ui.Button(label="Brak zapisanych", style=discord.ButtonStyle.secondary, disabled=True))
            return
        options = []
        for uid, info in list(ev.signups.items())[:25]:
            m = ev.guild.get_member(uid)
            label = (m.display_name if m else f"User {uid}")[:100]
            desc = (info or f"ID {uid}")[:100]
            options.append(discord.SelectOption(label=label, value=str(uid), description=desc))
        self.sel = discord.ui.Select(placeholder="Wybierz osoby do WYTYPOWANYCH", min_values=1, max_values=len(options), options=options)
        self.add_item(self.sel)
        async def _on_pick(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True, thinking=False)
            chosen = [int(v) for v in self.sel.values]
            added = 0
            for uid in chosen:
                if uid not in self.ev.picked_ids:
                    self.ev.picked_ids.append(uid); added += 1
            await self.ev.refresh_picked_embed(inter.channel, inter.user)
            await inter.followup.send(f"✅ Dodano do WYTYPOWANYCH: **{added}**.", ephemeral=True)
        self.sel.callback = _on_pick

class EventRemovePickedView(discord.ui.View):
    def __init__(self, ev: "EventView"):
        super().__init__(timeout=240)
        self.ev = ev
        if not ev.picked_ids:
            self.add_item(discord.ui.Button(label="Lista WYTYPOWANYCH pusta", style=discord.ButtonStyle.secondary, disabled=True))
            return
        options = []
        for uid in ev.picked_ids[:25]:
            m = ev.guild.get_member(uid)
            label = (m.display_name if m else f"User {uid}")[:100]
            desc = (ev.signups.get(uid, f"ID {uid}"))[:100]
            options.append(discord.SelectOption(label=label, value=str(uid), description=desc))
        self.sel = discord.ui.Select(placeholder="Wybierz osoby do usunięcia", min_values=1, max_values=len(options), options=options)
        self.add_item(self.sel)
        async def _on_rem(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True, thinking=False)
            chosen = [int(v) for v in self.sel.values]
            before = set(self.ev.picked_ids)
            self.ev.picked_ids = [u for u in self.ev.picked_ids if u not in chosen]
            removed = len(before) - len(set(self.ev.picked_ids))
            await self.ev.refresh_picked_embed(inter.channel, inter.user)
            await inter.followup.send(f"✅ Usunięto z WYTYPOWANYCH: **{removed}**.", ephemeral=True)
        self.sel.callback = _on_rem

class EventChangeTimesModal(discord.ui.Modal, title="Zmień godziny eventu"):
    def __init__(self, ev: "EventView"):
        super().__init__(timeout=180)
        self.ev = ev
        self.start_in = discord.ui.TextInput(label="Start (HH:MM)", default=self.ev.starts_at.strftime("%H:%M"))
        self.tp_in = discord.ui.TextInput(label="Teleport (HH:MM)", default=self.ev.teleport_at.strftime("%H:%M"))
        self.add_item(self.start_in); self.add_item(self.tp_in)
    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_start = _parse_pl_time(str(self.start_in.value))
            new_tp = _parse_pl_time(str(self.tp_in.value))
        except Exception:
            return await interaction.response.send_message("Błędny format. Użyj HH:MM.", ephemeral=True)
        self.ev.starts_at = new_start
        self.ev.teleport_at = new_tp
        await self.ev.refresh_embed()
        await interaction.response.send_message("✅ Zmieniono godziny.", ephemeral=True)

class EventPanelView(discord.ui.View):
    def __init__(self, ev: "EventView", opener: discord.Member):
        super().__init__(timeout=600)
        self.ev = ev
        self.opener = opener
    @discord.ui.button(label="➕ Wpisz na WYTYPOWANYCH", style=discord.ButtonStyle.success)
    async def add_picked(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Wybierz osoby do WYTYPOWANYCH:", view=EventAddPickedView(self.ev), ephemeral=True)
    @discord.ui.button(label="➖ Wypisz z WYTYPOWANYCH", style=discord.ButtonStyle.danger)
    async def rem_picked(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Zaznacz osoby do usunięcia:", view=EventRemovePickedView(self.ev), ephemeral=True)
    @discord.ui.button(label="⏱️ Zmień godziny", style=discord.ButtonStyle.primary)
    async def chg_times(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(EventChangeTimesModal(self.ev))
    @discord.ui.button(label="👀 Pokaż kolejkę chętnych", style=discord.ButtonStyle.secondary)
    async def show_queue(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.ev.queue:
            return await interaction.response.send_message("Kolejka jest pusta.", ephemeral=True)
        lines = []
        for i, uid in enumerate(self.ev.queue, start=1):
            m = interaction.guild.get_member(uid)
            lines.append(f"{i}. {m.mention if m else f'<@{uid}>'}")
        await interaction.response.send_message("**Kolejka chętnych:**\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(name="panel-event", description="Panel administracyjny eventu w tym kanale.")
@role_required_check()
async def panel_event(interaction: discord.Interaction):
    key = (interaction.guild.id, interaction.channel.id)
    ev = ACTIVE_EVENTS.get(key)
    if not ev or not ev.message:
        return await interaction.response.send_message("Brak aktywnego eventu w tym kanale.", ephemeral=True)
    m: discord.Member = interaction.user
    queue_len = len(getattr(ev, "queue", []))
    queue_info = f" • kolejka: **{queue_len}**" if ev.max_slots else ""
    lim = f"/{ev.max_slots}" if ev.max_slots else ""
    if not (m.guild_permissions.administrator or m == ev.author or any(r.id == REQUIRED_ROLE_ID for r in m.roles)):
        return await interaction.response.send_message("Panel tylko dla wystawiającego/admina/roli uprawnionej.", ephemeral=True)
    queue_info = f" • kolejka: **{len(getattr(ev, 'queue', []))}**" if ev.max_slots else ""
    lim = f"/{ev.max_slots}" if ev.max_slots else ""
    await interaction.response.send_message(
        f"Panel **{ev.name}** - zapisanych: **{len(ev.signups)}{lim}**{queue_info}. WYTYPOWANI: **{len(ev.picked_ids)}**.",
        view=EventPanelView(ev, m), ephemeral=True
    )
# ====== PINGI: Cayo i Zancudo ======
def _parse_pl_time(start_time: str) -> datetime:
    try:
        hh, mm = (int(x) for x in start_time.strip().split(":"))
        assert 0 <= hh <= 23 and 0 <= mm <= 59
    except Exception:
        raise ValueError("HH:MM")
    try:
        now_pl = datetime.now(tz=WARSAW)
        today_start = datetime(now_pl.year, now_pl.month, now_pl.day, hh, mm, tzinfo=WARSAW)
        return today_start if today_start > now_pl else today_start + timedelta(days=1)
    except Exception:
        now_local = datetime.now()
        today_start = datetime(now_local.year, now_local.month, now_local.day, hh, mm)
        return today_start if today_start > now_local else today_start + timedelta(days=1)

@bot.tree.command(name="ping-cayo", description="Ping na Cayo Perico: wybierz kanał i godzinę (PL).")
@role_required_check()
@app_commands.describe(voice="Kanał głosowy do wbicia.", start_time="Godzina startu 24h, np. 19:30 (czas Polski).")
async def ping_cayo(interaction: discord.Interaction, voice: discord.VoiceChannel, start_time: str):
    try:
        starts_at = _parse_pl_time(start_time)
    except ValueError:
        return await interaction.response.send_message("Podaj godzinę **HH:MM** (np. 19:30).", ephemeral=True)
    embed = make_simple_ping_embed("Atak na CAYO PERICO!", voice, starts_at, interaction.guild, CAYO_IMAGE_URL)
    allowed = discord.AllowedMentions(everyone=True)
    await interaction.response.send_message(content="@everyone", embed=embed, allowed_mentions=allowed)

@bot.tree.command(name="ping-zancudo", description="Ping na Fort Zancudo: wybierz kanał i godzinę (PL).")
@role_required_check()
@app_commands.describe(voice="Kanał głosowy do wbicia.", start_time="Godzina startu 24h, np. 19:30 (czas Polski).")
async def ping_zancudo(interaction: discord.Interaction, voice: discord.VoiceChannel, start_time: str):
    try:
        starts_at = _parse_pl_time(start_time)
    except ValueError:
        return await interaction.response.send_message("Podaj godzinę **HH:MM** (np. 19:30).", ephemeral=True)
    embed = make_simple_ping_embed("Atak na FORT ZANCUDO!", voice, starts_at, interaction.guild, ZANCUDO_IMAGE_URL)
    allowed = discord.AllowedMentions(everyone=True)
    await interaction.response.send_message(content="@everyone", embed=embed, allowed_mentions=allowed)

# ====== STATUS / PRESENCE ======
def _presence_from_choice(choice: str) -> discord.Status:
    return {
        "online": discord.Status.online,
        "dnd": discord.Status.do_not_disturb,
        "idle": discord.Status.idle,
        "invisible": discord.Status.invisible,
    }.get(choice, discord.Status.online)

def _is_valid_stream(url: str) -> bool:
    if not url: return False
    u = url.lower()
    return ("twitch.tv/" in u) or ("youtube.com/" in u) or ("youtu.be/" in u)

@bot.tree.command(name="set-status", description="Ustaw status bota (gra/słucham/oglądam/rywalizuję/stream) i widoczność.")
@role_required_check()
@app_commands.describe(text="Tekst statusu (np. nazwa gry/streamu)", stream_url="URL streamu (TYLKO gdy tryb = stream)")
@app_commands.choices(
    activity_type=[
        app_commands.Choice(name="Gra (Playing)", value="playing"),
        app_commands.Choice(name="Słucham (Listening)", value="listening"),
        app_commands.Choice(name="Oglądam (Watching)", value="watching"),
        app_commands.Choice(name="Rywalizuję (Competing)", value="competing"),
        app_commands.Choice(name="Stream (Streaming)", value="streaming"),
    ],
    presence=[
        app_commands.Choice(name="Online", value="online"),
        app_commands.Choice(name="Nie przeszkadzać (DND)", value="dnd"),
        app_commands.Choice(name="Zaraz wracam (Idle)", value="idle"),
        app_commands.Choice(name="Niewidoczny", value="invisible"),
    ],
)
async def set_status(interaction: discord.Interaction,
                     activity_type: app_commands.Choice[str],
                     text: str,
                     presence: app_commands.Choice[str] = None,
                     stream_url: str = ""):
    await interaction.response.send_message("Zmieniam status…", ephemeral=True)
    kind = activity_type.value
    status = _presence_from_choice(presence.value) if presence else discord.Status.online
    if kind == "streaming":
        if not _is_valid_stream(stream_url):
            return await interaction.followup.send("Podaj prawidłowy link do streama (Twitch/YouTube).", ephemeral=True)
        activity = discord.Streaming(name=text, url=stream_url)
    elif kind == "listening":
        activity = discord.Activity(type=discord.ActivityType.listening, name=text)
    elif kind == "watching":
        activity = discord.Activity(type=discord.ActivityType.watching, name=text)
    elif kind == "competing":
        activity = discord.Activity(type=discord.ActivityType.competing, name=text)
    else:
        activity = discord.Game(name=text)
    await bot.change_presence(status=status, activity=activity)
    what = "stream" if kind == "streaming" else kind
    await interaction.followup.send(
        f"✅ Ustawiono status: **{what}** → „**{text}**” • Widoczność: **{(presence.value if presence else 'online')}**",
        ephemeral=True
    )

@bot.tree.command(name="clear-status", description="Wyczyść aktywność bota (pozostawia widoczność).")
@role_required_check()
async def clear_status(interaction: discord.Interaction):
    await bot.change_presence(activity=None)
    await interaction.response.send_message("✅ Wyczyszczono aktywność bota.", ephemeral=True)

@bot.tree.command(name="set-visibility", description="Ustaw tylko widoczność bota (online/dnd/idle/invisible).")
@role_required_check()
@app_commands.choices(
    presence=[
        app_commands.Choice(name="Online", value="online"),
        app_commands.Choice(name="Nie przeszkadzać (DND)", value="dnd"),
        app_commands.Choice(name="Zaraz wracam (Idle)", value="idle"),
        app_commands.Choice(name="Niewidoczny", value="invisible"),
    ]
)
async def set_visibility(interaction: discord.Interaction, presence: app_commands.Choice[str]):
    await bot.change_presence(status=_presence_from_choice(presence.value))
    await interaction.response.send_message(f"✅ Ustawiono widoczność: **{presence.name}**.", ephemeral=True)

# ======== SQUAD – helpers / komenda ========
def create_squad_embed(guild: discord.Guild, author_name: str,
                       members_list: str = "Brak członków składu.",
                       title: str = "Main Squad"):
    member_lines = [line for line in members_list.split('\n') if line.strip()]
    count = len(member_lines)
    embed = discord.Embed(title=title, description=f"Oto aktualny skład:\n\n{members_list}", color=0xFFFFFF)
    if LOGO_URL: embed.set_thumbnail(url=LOGO_URL)
    embed.add_field(name="Liczba członków:", value=f"**{count}**", inline=False)
    embed.set_footer(text=f"Aktywowane przez {author_name}")
    return embed

class SquadModal(discord.ui.Modal, title='Edytuj Skład'):
    def __init__(self, message_id: int, current_content: str):
        super().__init__(timeout=180)
        self.message_id = message_id
        self.list_input = discord.ui.TextInput(
            label='Lista członków (np. 1- @nick, 2- @nick...)',
            style=discord.TextStyle.paragraph, default=current_content,
            required=True, max_length=4000
        )
        self.add_item(self.list_input)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("Aktualizuję skład…", ephemeral=True)
        new_members_list = self.list_input.value
        squad_data = SQUADS.get(self.message_id)
        if not squad_data:
            return await interaction.followup.send("Błąd: Nie znaleziono danych tego składu.", ephemeral=True)
        message = squad_data.get("message")
        author_name = squad_data.get("author_name", "Bot")
        title = message.embeds[0].title if (message and message.embeds) else "Main Squad"
        SQUADS[self.message_id]["members_list"] = new_members_list
        new_embed = create_squad_embed(interaction.guild, author_name, new_members_list, title)
        if message:
            view = SquadView(self.message_id, squad_data.get("role_id"))
            role_id = squad_data.get("role_id")
            content = f"<@&{role_id}> **Zaktualizowano Skład!**" if role_id else ""
            await message.edit(content=content, embed=new_embed, view=view)
            await interaction.followup.send("✅ Skład zaktualizowany!", ephemeral=True)
        else:
            await interaction.followup.send("Błąd: Nie można odświeżyć wiadomości składu.", ephemeral=True)

class SquadView(discord.ui.View):
    def __init__(self, message_id: int, role_id: int):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.role_id = role_id
    @discord.ui.button(label="Zarządzaj składem (ADMIN)", style=discord.ButtonStyle.blurple)
    async def manage_squad_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        m: discord.Member = interaction.user
        is_admin = (m.guild_permissions.administrator or m == m.guild.owner
                    or (REQUIRED_ROLE_ID and any(r.id == REQUIRED_ROLE_ID for r in m.roles)))
        if not is_admin:
            return await interaction.response.send_message("⛔ Brak uprawnień do zarządzania składem!", ephemeral=True)
        squad_data = SQUADS.get(self.message_id)
        if not squad_data:
            return await interaction.response.send_message("Błąd: Nie znaleziono danych tego składu.", ephemeral=True)
        current_content = squad_data.get("members_list", "1- @...")
        await interaction.response.send_modal(SquadModal(self.message_id, current_content))

@bot.tree.command(name="create-squad", description="Tworzy ogłoszenie o składzie z możliwością edycji.")
@role_required_check()
async def create_squad(interaction: discord.Interaction, rola: discord.Role, tytul: str = "Main Squad"):
    try:
        m: discord.Member = interaction.user
        is_admin = (m.guild_permissions.administrator or m == interaction.guild.owner
                    or (REQUIRED_ROLE_ID and any(r.id == REQUIRED_ROLE_ID for r in m.roles)))
        if not is_admin:
            return await interaction.response.send_message("⛔ Brak uprawnień!", ephemeral=True)
        await interaction.response.send_message("Tworzę ogłoszenie o składzie…", ephemeral=True)
        author_name = m.display_name
        role_id = rola.id
        initial_members = "1- [Wpisz osobę]\n2- [Wpisz osobę]\n3- [Wpisz osobę]"
        embed = create_squad_embed(interaction.guild, author_name, initial_members, tytul)
        view = SquadView(0, role_id)
        allowed = discord.AllowedMentions(roles=[rola], users=False, everyone=False)
        sent = await interaction.channel.send(content=rola.mention, embed=embed, view=view, allowed_mentions=allowed)
        SQUADS[sent.id] = {
            "role_id": role_id, "members_list": initial_members,
            "message": sent, "channel_id": sent.channel.id, "author_name": author_name,
        }
        view.message_id = sent.id
        await sent.edit(view=view)
        await interaction.followup.send(f"✅ Ogłoszenie o składzie **{tytul}** dla roli {rola.mention} wysłane!", ephemeral=True)
    except Exception as e:
        logging.exception("create-squad failed")
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ Błąd w /create-squad: `{e}`", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Błąd w /create-squad: `{e}`", ephemeral=True)

# ===== Start / sync =====
@bot.event
async def on_ready():
    log.info(f"Zalogowano jako {bot.user} (id: {bot.user.id})")
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info(f"Zsynchronizowano {len(synced)} komend na guild {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            log.info(f"Zsynchronizowano globalnie {len(synced)} komend")
    except Exception as e:
        log.exception("Błąd syncu: %s", e)

async def main():
    # Start HTTP health server for Render/UptimeRobot pings
    try:
        await _setup_http()
    except Exception:
        logging.getLogger("http").exception("HTTP health server init failed; continuing without it.")

    # Prepare command sync
    async def sync_commands():
        try:
            if GUILD_ID:
                guild_obj = discord.Object(id=int(GUILD_ID))
                bot.tree.copy_global_to(guild=guild_obj)
                await bot.tree.sync(guild=guild_obj)
            else:
                await bot.tree.sync()
        except Exception:
            logging.getLogger("discord").exception("Slash command sync failed.")

    # Robust login with backoff to survive Cloudflare 1015 rate limit on the shared IP
    backoff = 30
    while True:
        try:
            async with bot:
                await sync_commands()
                await bot.start(TOKEN)
            break  # clean exit
        except LoginFailure as e:
            logging.getLogger("discord").error("Login failure: wrong token or permissions: %s", e)
            raise
        except HTTPException as e:
            # discord.py raises HTTPException for 4xx/5xx REST calls including /users/@me during login
            if getattr(e, "status", None) == 429 or "Access denied" in str(e):
                logging.getLogger("discord").warning("Hit HTTP 429/Cloudflare 1015 during login. Sleeping %ss.", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 900)  # up to 15 minutes
                continue
            logging.getLogger("discord").exception("HTTPException during start; retrying in %ss", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 900)
        except (ClientConnectorError, OSError, GatewayNotFound) as e:
            logging.getLogger("discord").warning("Network/Gateway error: %s; retrying in %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 900)
        except Exception:
            logging.getLogger("discord").exception("Unexpected error in bot.start(); retrying in %ss", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 900)

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Brak DISCORD_TOKEN w .env")
    asyncio.run(main())
