import os
import asyncio
import logging
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiohttp import web  # <— mini-serwer healthcheck

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

# ===== Konfiguracja =====
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
REQUIRED_ROLE_ID = int(os.getenv("REQUIRED_ROLE_ID") or 0)

# <<< USTAW TO >>>
CAPT_CHANNEL_ID = 1422343549386752001  # np. 123456789012345678 – kanał do wzmianki w ogłoszeniu CAPT
LOGO_URL = "https://cdn.discordapp.com/icons/1422343547780337819/683dd456c5dc7e124326d4d810992d07.webp?size=1024"  # miniatura (logo)

# <<< PINGI: obrazki >>>
CAYO_IMAGE_URL = "https://cdn.discordapp.com/attachments/1224129510535069766/1414204332747915274/image.png?ex=68e0feeb&is=68dfad6b&hm=6e94ea1a242b13813caddee86833f464d476daebba49b6c441d8f08e81b7baad&"      # <- PODMIEŃ
ZANCUDO_IMAGE_URL = "https://cdn.discordapp.com/attachments/1224129510535069766/1414194392214011974/image.png?ex=68e0f5a9&is=68dfa429&hm=ee9c6801fd8e897ea8eff6771bce692e72d7604dfd702c467cf0fc32db063919&" # <- PODMIEŃ

# <<< SQUAD ADMINI – UZUPEŁNIJ SWOIMI ID >>>
ADMIN_USER_IDS: list[int] = [333601079264542722, 921771573113937920, 1007732573063098378, 348583392658456596, 588272459556323329, 1114687688319180820, 1184620388425138183]  # np. [1184620388425138183]

intents = discord.Intents.default()
intents.message_content = False
intents.members = True  # włącz też w Dev Portal -> Privileged Gateway Intents
bot = commands.Bot(command_prefix="!", intents=intents)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("discord")

# Rejestry aktywnych ogłoszeń
ACTIVE_CAPTS: dict[tuple[int, int], "CaptView"] = {}
ACTIVE_AIRDROPS: dict[tuple[int, int], "AirdropView"] = {}
SQUADS: dict[int, dict] = {}  # {msg_id: {"role_id": int, "members_list": str, "message": Message, "channel_id": int, "author_name": str}}

# ===== Check roli =====
def role_required_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.CheckFailure("Tej komendy można użyć tylko na serwerze.")
        m: discord.Member = interaction.user
        if m.guild_permissions.administrator or m == interaction.guild.owner:
            return True
        if REQUIRED_ROLE_ID == 0:
            raise app_commands.CheckFailure("Brak konfiguracji REQUIRED_ROLE_ID w .env.")
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
        return "—"
    lines = []
    for uid in user_ids[:limit]:
        m = guild.get_member(uid)
        lines.append(f"• {m.mention} | {m.display_name}" if m else f"• <@{uid}>")
    if len(user_ids) > limit:
        lines.append(f"… (+{len(user_ids)-limit})")
    return "\n".join(lines)

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
    desc = f"Wybrano {len(selected_ids)}/{total_count} osób:\n\n**Wybrani gracze:**\n" + ("\n".join(lines) if lines else "—")
    emb = discord.Embed(title="Lista osób na captures!", description=desc, color=0xFFFFFF)
    thumb = _thumb_url(guild)
    if thumb: emb.set_thumbnail(url=thumb)
    emb.set_footer(text=f"Wystawione przez {picker.display_name} • {now_pl.strftime('%d.%m.%Y %H:%M')}")
    return emb

# ---------- EMBED AIRDROP ----------
def make_airdrop_embed(starts_at: datetime, users: list[int], guild: discord.Guild,
                       author: discord.Member, info_text: str,
                       voice: discord.VoiceChannel | None, max_slots: int, queue_len: int) -> discord.Embed:
    ts = int(starts_at.timestamp())
    desc_parts = []
    if info_text:
        desc_parts.append(f"{info_text}\n")
    desc_parts.append("**Kanał głosowy:**")
    desc_parts.append(voice.mention if voice else "—")
    desc_parts.append("")
    desc_parts.append("**Czas rozpoczęcia:**")
    desc_parts.append(f"Rozpoczęcie AirDrop o <t:{ts}:t> ( <t:{ts}:R> )")
    if max_slots and max_slots > 0:
        desc_parts.append("")
        desc_parts.append(f"**Kolejka:** {queue_len}")

    desc = "\n".join(desc_parts)
    field_name = f"Zapisani ({len(users)}/{max_slots})" if max_slots and max_slots > 0 else f"Zapisani ({len(users)})"
    emb = discord.Embed(title="AirDrop!", description=desc, color=0xFFFFFF)
    emb.add_field(name=field_name, value=fmt_users(users, guild), inline=False)
    thumb = _thumb_url(guild)
    if thumb: emb.set_thumbnail(url=thumb)
    emb.set_footer(text=f"Wystawione przez {author.display_name}")
    return emb

def make_airdrop_winners_embed(winners: list[int], guild: discord.Guild, picker: discord.Member, from_queue: bool) -> discord.Embed:
    lines = []
    for i, uid in enumerate(winners, start=1):
        m = guild.get_member(uid)
        lines.append(f"{i}. {m.mention} | {m.display_name}" if m else f"{i}. <@{uid}>")
    now_pl = datetime.now(tz=WARSAW) if WARSAW else datetime.now()
    header = "**Wylosowani z kolejki:**" if from_queue else "**Wylosowani (z zapisanych):**"
    desc = header + "\n" + ("\n".join(lines) if lines else "—")
    emb = discord.Embed(title="Wyniki losowania AirDrop", description=desc, color=0xFFFFFF)
    thumb = _thumb_url(guild)
    if thumb: emb.set_thumbnail(url=thumb)
    emb.set_footer(text=f"Losował {picker.display_name} • {now_pl.strftime('%d.%m.%Y %H:%M')}")
    return emb

# ---------- CAPT: PICK okno ----------
class PickView(discord.ui.View):
    def __init__(self, capt: "CaptView", option_rows: list[tuple[int, str, str | None]], total_count: int, picker: discord.Member):
        super().__init__(timeout=300)
        self.capt = capt
        self.total_count = total_count
        self.picker = picker
        self.nick_by_id = {uid: nick for uid, nick, _ in option_rows}
        self.user_by_id = {uid: user for uid, _, user in option_rows}
        max_vals = min(25, len(option_rows)) or 1
        options = []
        for idx, (uid, nick_label, user_desc) in enumerate(option_rows, start=1):
            label = f"{idx}. {nick_label}"[:100]
            desc  = (user_desc or f"ID {uid}")[:100]
            options.append(discord.SelectOption(label=label, value=str(uid), description=desc))
        self.select = discord.ui.Select(placeholder="Wybierz graczy (max 25)", min_values=0, max_values=max_vals, options=options)
        self.add_item(self.select)

        async def _on_select(inter: discord.Interaction):
            chosen_ids = [int(v) for v in self.select.values]
            if not chosen_ids:
                txt = "Nic nie zaznaczono. Wybierz graczy, potem **Publikuj listę**."
            else:
                lines = []
                for i, uid in enumerate(chosen_ids, start=1):
                    nick = self.nick_by_id.get(uid, f"ID {uid}")
                    user = self.user_by_id.get(uid)
                    suffix = f" (@{user})" if user else ""
                    lines.append(f"{i}. {nick}{suffix}")
                txt = f"Zaznaczono {len(chosen_ids)}/{self.total_count}:\n" + "\n".join(lines)
            await inter.response.edit_message(content=txt, view=self)
        self.select.callback = _on_select

    @discord.ui.button(label="Publikuj listę", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        chosen = [int(v) for v in self.select.values]
        if not chosen:
            await interaction.followup.send("Nie wybrałeś żadnych osób.", ephemeral=True)
            return
        self.capt.picked_list = list(dict.fromkeys(chosen))
        emb = make_pick_embed(chosen, len(self.capt.users), self.capt.guild, self.picker)
        msg = await interaction.channel.send(embed=emb)
        self.capt.pick_message = msg
        await interaction.followup.send("Opublikowano listę i zapisano wybór.", ephemeral=True)

    @discord.ui.button(label="Anuluj", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Anulowano wybieranie.", ephemeral=True)
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
        self.picked_list: list[int] = []
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
        await interaction.response.defer(ephemeral=True)
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
        await interaction.response.defer(ephemeral=True)
        if changed:
            await self.refresh_announce()
            await self.refresh_pick_embed(interaction.channel, interaction.user)

    @discord.ui.button(label="PICK", style=discord.ButtonStyle.primary)
    async def pick(self, interaction: discord.Interaction, _: discord.ui.Button):
        mem: discord.Member = interaction.user
        if not (mem.guild_permissions.administrator or mem == self.author):
            await interaction.response.send_message("Tylko wystawiający lub administrator może wybierać osoby.", ephemeral=True)
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
            "Wybierz graczy (pokazane NICKI). Zaznaczeni pojawią się poniżej. Potem kliknij **Publikuj listę**.",
            view=view,
            ephemeral=True
        )

# ---------- AIRDROP: ogłoszenie (z KOLEJKĄ) ----------
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
        self.users: list[int] = []
        self.queue: list[int] = []
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        if self.max_slots <= 0:
            for item in list(self.children):
                if isinstance(item, discord.ui.Button) and item.label == "Dołącz do kolejki":
                    self.remove_item(item)

    async def refresh_embed(self):
        if not self.message:
            return
        is_full = (self.max_slots > 0 and len(self.users) >= self.max_slots)
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label == "Dołącz":
                item.disabled = is_full
            if isinstance(item, discord.ui.Button) and item.label == "Dołącz do kolejki":
                item.disabled = (self.max_slots <= 0)
        emb = make_airdrop_embed(
            self.starts_at, self.users, self.guild, self.author,
            self.info_text, self.voice, self.max_slots, len(self.queue)
        )
        await self.message.edit(embed=emb, view=self)

    @discord.ui.button(label="Dołącz", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, _: discord.ui.Button):
        async with self._lock:
            uid = interaction.user.id
            if uid in self.users:
                added = False
            else:
                if self.max_slots > 0 and len(self.users) >= self.max_slots:
                    await interaction.response.send_message(
                        f"Limit miejsc osiągnięty ({self.max_slots}). Użyj **Dołącz do kolejki**.",
                        ephemeral=True
                    )
                    return
                self.users.append(uid)
                if uid in self.queue:
                    self.queue.remove(uid)
                added = True
        await interaction.response.defer(ephemeral=True)
        await self.refresh_embed()
        if added:
            try:
                await interaction.followup.send("Dołączono do AirDrop.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Opuść", style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button):
        async with self._lock:
            uid = interaction.user.id
            changed = False
            if uid in self.users:
                self.users.remove(uid); changed = True
            if uid in self.queue:
                self.queue.remove(uid); changed = True
        await interaction.response.defer(ephemeral=True)
        if changed:
            await self.refresh_embed()

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
                await interaction.response.send_message("Są wolne miejsca — kliknij **Dołącz**.", ephemeral=True); return
            if uid in self.queue:
                await interaction.response.send_message("Jesteś już w kolejce.", ephemeral=True); return
            self.queue.append(uid)
        await interaction.response.defer(ephemeral=True)
        await self.refresh_embed()
        try:
            await interaction.followup.send("Dodano do kolejki.", ephemeral=True)
        except Exception:
            pass

# ---------- PANELE CAPT (ZAPISANI + WYBRANI) ----------
class CaptAddSignupsView(discord.ui.View):
    def __init__(self, capt: CaptView):
        super().__init__(timeout=240)
        self.capt = capt
        self.sel = discord.ui.UserSelect(placeholder="Dodaj do ZAPISANYCH (max 25)", min_values=1, max_values=25)
        self.add_item(self.sel)
        async def _cb(inter: discord.Interaction):
            added = 0
            for user in self.sel.values:
                uid = user.id
                if uid not in self.capt.users:
                    self.capt.users.append(uid); added += 1
            await self.capt.refresh_announce()
            await self.capt.refresh_pick_embed(inter.channel, inter.user)
            await inter.response.edit_message(content=f"Dodano: {added}. Zapisanych: {len(self.capt.users)}", view=self)
        self.sel.callback = _cb

class CaptRemoveSignupsView(discord.ui.View):
    def __init__(self, capt: CaptView):
        super().__init__(timeout=240)
        self.capt = capt
        if not capt.users:
            self.add_item(discord.ui.Button(label="Brak zapisanych", style=discord.ButtonStyle.secondary, disabled=True)); return
        options = []
        for uid in capt.users[:25]:
            m = capt.guild.get_member(uid)
            label = (m.display_name if m else f"User {uid}")[:100]
            desc  = (f"@{m.name}" if m else f"ID {uid}")[:100]
            options.append(discord.SelectOption(label=label, value=str(uid), description=desc, default=True))
        self.sel = discord.ui.Select(placeholder="Odznacz, aby usunąć (ZAPISANI) – max 25",
                                     min_values=0, max_values=len(options), options=options)
        self.add_item(self.sel)
        async def _cb(inter: discord.Interaction):
            keep_ids = {int(v) for v in self.sel.values}
            before = list(self.capt.users)
            self.capt.users = [uid for uid in before if uid in keep_ids]
            removed = len(before) - len(self.capt.users)
            self.capt.picked_list = [uid for uid in self.capt.picked_list if uid in self.capt.users]
            await self.capt.refresh_announce()
            await self.capt.refresh_pick_embed(inter.channel, inter.user)
            await inter.response.edit_message(content=f"Usunięto: {removed}. Zapisanych: {len(self.capt.users)}", view=self)
        self.sel.callback = _cb

class AddToPickedView(discord.ui.View):
    def __init__(self, capt: CaptView):
        super().__init__(timeout=240)
        self.capt = capt
        self.sel = discord.ui.UserSelect(placeholder="Dodaj do WYBRANI (max 25)", min_values=1, max_values=25)
        self.add_item(self.sel)
        async def _cb(inter: discord.Interaction):
            added = 0
            for user in self.sel.values:
                uid = user.id
                if uid not in self.capt.picked_list:
                    self.capt.picked_list.append(uid); added += 1
            await self.capt.refresh_pick_embed(inter.channel, inter.user)
            await inter.response.edit_message(content=f"Dodano do WYBRANI: {added}. Na liście: {len(self.capt.picked_list)}", view=self)
        self.sel.callback = _cb

class RemoveFromPickedView(discord.ui.View):
    def __init__(self, capt: CaptView):
        super().__init__(timeout=240)
        self.capt = capt
        if not capt.picked_list:
            self.add_item(discord.ui.Button(label="Lista WYBRANI jest pusta", style=discord.ButtonStyle.secondary, disabled=True)); return
        options = []
        for uid in capt.picked_list[:25]:
            m = capt.guild.get_member(uid)
            label = (m.display_name if m else f"User {uid}")[:100]
            desc  = (f"@{m.name}" if m else f"ID {uid}")[:100]
            options.append(discord.SelectOption(label=label, value=str(uid), description=desc, default=True))
        self.sel = discord.ui.Select(placeholder="Odznacz, aby usunąć (WYBRANI) – max 25",
                                     min_values=0, max_values=len(options), options=options)
        self.add_item(self.sel)
        async def _cb(inter: discord.Interaction):
            keep_ids = {int(v) for v in self.sel.values}
            before = list(self.capt.picked_list)
            self.capt.picked_list = [uid for uid in before if uid in keep_ids]
            removed = len(before) - len(self.capt.picked_list)
            await self.capt.refresh_pick_embed(inter.channel, inter.user)
            await inter.response.edit_message(content=f"Usunięto z WYBRANI: {removed}. Na liście: {len(self.capt.picked_list)}", view=self)
        self.sel.callback = _cb

class RandomPickModal(discord.ui.Modal, title="Losuj do listy (WYBRANI)"):
    def __init__(self, capt: CaptView):
        super().__init__(timeout=120)
        self.capt = capt
        self.count = discord.ui.TextInput(label="Ile osób dodać?", placeholder="np. 10", required=True, max_length=3)
        self.add_item(self.count)
    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(str(self.count.value).strip()); assert n > 0
        except Exception:
            await interaction.response.send_message("Podaj poprawną liczbę > 0.", ephemeral=True); return
        pool = [uid for uid in self.capt.users if uid not in self.capt.picked_list]
        if not pool:
            await interaction.response.send_message("Brak dostępnych osób do losowania.", ephemeral=True); return
        chosen = random.sample(pool, k=min(n, len(pool)))
        for uid in chosen:
            if uid not in self.capt.picked_list:
                self.capt.picked_list.append(uid)
        await self.capt.refresh_pick_embed(interaction.channel, interaction.user)
        await interaction.response.send_message(f"Wylosowano {len(chosen)}. Na liście WYBRANI: {len(self.capt.picked_list)}.", ephemeral=True)

class PanelView(discord.ui.View):
    def __init__(self, capt: CaptView, opener: discord.Member):
        super().__init__(timeout=600)
        self.capt = capt
        self.opener = opener
    @discord.ui.button(label="Dodaj do ZAPISANYCH", style=discord.ButtonStyle.success)
    async def add_signed_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = CaptAddSignupsView(self.capt)
        await interaction.response.send_message("Wybierz osoby do **dodania** (ZAPISANI):", view=view, ephemeral=True)
    @discord.ui.button(label="Usuń z ZAPISANYCH (odznacz)", style=discord.ButtonStyle.danger)
    async def del_signed_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = CaptRemoveSignupsView(self.capt)
        await interaction.response.send_message("Odznacz osoby do **usunięcia** (ZAPISANI):", view=view, ephemeral=True)
    @discord.ui.button(label="Dodaj do WYBRANI", style=discord.ButtonStyle.secondary)
    async def add_pick_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = AddToPickedView(self.capt)
        await interaction.response.send_message("Wybierz osoby do **dodania** (WYBRANI):", view=view, ephemeral=True)
    @discord.ui.button(label="Usuń z WYBRANI (odznacz)", style=discord.ButtonStyle.secondary)
    async def del_pick_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = RemoveFromPickedView(self.capt)
        await interaction.response.send_message("Odznacz osoby do **usunięcia** (WYBRANI):", view=view, ephemeral=True)
    @discord.ui.button(label="Losuj do WYBRANI", style=discord.ButtonStyle.primary)
    async def rnd_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        modal = RandomPickModal(self.capt)
        await interaction.response.send_modal(modal)

# ---------- PANELE AIRDROP ----------
class AirdropAddSignupsView(discord.ui.View):
    def __init__(self, adr: AirdropView):
        super().__init__(timeout=240)
        self.adr = adr
        self.sel = discord.ui.UserSelect(placeholder="Dodaj do zapisanych (max 25)", min_values=1, max_values=25)
        self.add_item(self.sel)
        async def _cb(inter: discord.Interaction):
            added = 0; skipped_full = 0
            for user in self.sel.values:
                uid = user.id
                if uid in self.adr.users: continue
                if self.adr.max_slots > 0 and len(self.adr.users) >= self.adr.max_slots:
                    skipped_full += 1; continue
                self.adr.users.append(uid)
                if uid in self.adr.queue: self.adr.queue.remove(uid)
                added += 1
            await self.adr.refresh_embed()
            note = (f" (pełno miejsc, pominięto: {skipped_full})" if skipped_full else "")
            queue_part = (f" • Kolejka: {len(self.adr.queue)}" if self.adr.max_slots else "")
            await inter.response.edit_message(
                content=f"Dodano: {added}. Zapisanych: {len(self.adr.users)}"
                        + (f" / {self.adr.max_slots}" if self.adr.max_slots else "")
                        + queue_part + note,
                view=self
            )
        self.sel.callback = _cb

class AirdropRemoveSignupsView(discord.ui.View):
    def __init__(self, adr: AirdropView):
        super().__init__(timeout=240)
        self.adr = adr
        if not adr.users:
            self.add_item(discord.ui.Button(label="Brak zapisanych", style=discord.ButtonStyle.secondary, disabled=True)); return
        options = []
        for uid in adr.users[:25]:
            m = adr.guild.get_member(uid)
            label = (m.display_name if m else f"User {uid}")[:100]
            desc  = (f"@{m.name}" if m else f"ID {uid}")[:100]
            options.append(discord.SelectOption(label=label, value=str(uid), description=desc, default=True))
        self.sel = discord.ui.Select(placeholder="Odznacz, aby usunąć (max 25)",
                                     min_values=0, max_values=len(options), options=options)
        self.add_item(self.sel)
        async def _cb(inter: discord.Interaction):
            keep_ids = {int(v) for v in self.sel.values}
            before = list(self.adr.users)
            self.adr.users = [uid for uid in before if uid in keep_ids]
            removed = len(before) - len(self.adr.users)
            await self.adr.refresh_embed()
            queue_part = (f" • Kolejka: {len(self.adr.queue)}" if self.adr.max_slots else "")
            await inter.response.edit_message(
                content=(
                    f"Usunięto: {removed}. Zapisanych: {len(self.adr.users)}"
                    + (f" / {self.adr.max_slots}" if self.adr.max_slots else "")
                    + queue_part
                ),
                view=self
            )
        self.sel.callback = _cb

class AirdropWinnersModal(discord.ui.Modal):
    def __init__(self, adr: AirdropView):
        super().__init__(title=("Losuj zwycięzców (z kolejki)" if adr.max_slots > 0 else "Losuj zwycięzców (z zapisanych)"))
        self.adr = adr
        self.count = discord.ui.TextInput(label="Ile osób wylosować?", placeholder="np. 5", required=True, max_length=3)
        self.add_item(self.count)
    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(str(self.count.value).strip()); assert n > 0
        except Exception:
            await interaction.response.send_message("Podaj poprawną liczbę > 0.", ephemeral=True); return
        from_queue = self.adr.max_slots > 0
        pool = list(self.adr.queue) if from_queue else list(self.adr.users)
        if not pool:
            await interaction.response.send_message("Brak osób do losowania w wybranej puli.", ephemeral=True); return
        winners = random.sample(pool, k=min(n, len(pool)))
        emb = make_airdrop_winners_embed(winners, self.adr.guild, interaction.user, from_queue=from_queue)
        await interaction.response.send_message("Wyniki losowania opublikowane poniżej.", ephemeral=True)
        await interaction.channel.send(embed=emb)

class AirdropPanelView(discord.ui.View):
    def __init__(self, adr: AirdropView, opener: discord.Member):
        super().__init__(timeout=600)
        self.adr = adr; self.opener = opener
        try:
            if self.adr.max_slots > 0: self.winners_btn.label = "Losuj z kolejki"
            else: self.winners_btn.label = "Losuj z zapisanych"
        except Exception: pass
    @discord.ui.button(label="Dodaj do zapisanych", style=discord.ButtonStyle.success)
    async def add_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = AirdropAddSignupsView(self.adr)
        await interaction.response.send_message("Wybierz osoby do **dodania** na listę zapisanych:", view=view, ephemeral=True)
    @discord.ui.button(label="Usuń z zapisanych (odznacz)", style=discord.ButtonStyle.danger)
    async def del_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = AirdropRemoveSignupsView(self.adr)
        await interaction.response.send_message("Odznacz osoby do **usunięcia** z listy zapisanych:", view=view, ephemeral=True)
    @discord.ui.button(label="Losuj", style=discord.ButtonStyle.primary)
    async def winners_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        modal = AirdropWinnersModal(self.adr)
        await interaction.response.send_modal(modal)

# ===== Komendy podstawowe =====
@bot.tree.command(name="ping", description="Sprawdź opóźnienie bota.")
@role_required_check()
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency*1000)}ms", ephemeral=True)

@bot.tree.command(name="say", description="Bot powtórzy twoją wiadomość.")
@role_required_check()
async def say(interaction: discord.Interaction, text: str):
    await interaction.response.send_message(text)

# ===== CREATE CAPT (z @everyone) =====
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
        await interaction.response.send_message("Brak aktywnego ogłoszenia w tym kanale.", ephemeral=True); return
    mem: discord.Member = interaction.user
    if not (mem.guild_permissions.administrator or mem == capt.author):
        await interaction.response.send_message("Panel dostępny tylko dla wystawiającego lub administratora.", ephemeral=True); return
    view = PanelView(capt, mem)
    await interaction.response.send_message(
        f"Panel CAPT – Zapisani: **{len(capt.users)}**, WYBRANI: **{len(capt.picked_list)}**.",
        view=view, ephemeral=True
    )

# ===== AIRDROP (zawsze @everyone) =====
@bot.tree.command(name="airdrop", description="Utwórz AirDrop (opis, głosowy, timer, zapisy; opcjonalny limit miejsc i kolejka).")
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
@bot.tree.command(name="panel-airdrop", description="Otwórz panel AIRDROP w tym kanale.")
@role_required_check()
async def panel_airdrop(interaction: discord.Interaction):
    key = (interaction.guild.id, interaction.channel.id)
    adr = ACTIVE_AIRDROPS.get(key)
    if not adr or not adr.message:
        await interaction.response.send_message("Brak aktywnego airdropa w tym kanale.", ephemeral=True); return
    mem: discord.Member = interaction.user
    if not (mem.guild_permissions.administrator or mem == adr.author):
        await interaction.response.send_message("Panel dostępny tylko dla wystawiającego lub administratora.", ephemeral=True); return
    view = AirdropPanelView(adr, mem)
    lim = f" / {adr.max_slots}" if adr.max_slots else ""
    queue_info = f", kolejka: **{len(adr.queue)}**" if adr.max_slots else ""
    await interaction.response.send_message(
        f"Panel AIRDROP – zapisanych: **{len(adr.users)}{lim}**{queue_info}.",
        view=view, ephemeral=True
    )

# ===== PINGI: Cayo i Zancudo =====
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
    await interaction.response.defer(ephemeral=True)
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
        await interaction.response.defer(ephemeral=True)
        new_members_list = self.list_input.value
        squad_data = SQUADS.get(self.message_id)
        if not squad_data:
            return await interaction.followup.send("Błąd: Nie znaleziono danych tego składu.", ephemeral=True)
        squad_data["members_list"] = new_members_list
        message = squad_data.get("message")
        author_name = squad_data.get("author_name", "Bot")
        title = message.embeds[0].title if (message and message.embeds) else "Main Squad"
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
                    or (ADMIN_USER_IDS and m.id in ADMIN_USER_IDS)
                    or (REQUIRED_ROLE_ID and any(r.id == REQUIRED_ROLE_ID for r in m.roles)))
        if not is_admin:
            return await interaction.response.send_message("⛔ Brak uprawnień do zarządzania składem!", ephemeral=True)
        squad_data = SQUADS.get(self.message_id)
        if not squad_data:
            return await interaction.response.send_message("Błąd: Nie znaleziono danych tego składu.", ephemeral=True)
        current_content = squad_data.get("members_list", "1- @...")
        await interaction.response.send_modal(SquadModal(self.message_id, current_content))

@bot.tree.command(name="create-squad", description="Tworzy ogłoszenie o składzie z możliwością edycji.")
async def create_squad(interaction: discord.Interaction, rola: discord.Role, tytul: str = "Main Squad"):
    try:
        m: discord.Member = interaction.user
        is_admin = (m.guild_permissions.administrator or m == interaction.guild.owner
                    or (ADMIN_USER_IDS and m.id in ADMIN_USER_IDS)
                    or (REQUIRED_ROLE_ID and any(r.id == REQUIRED_ROLE_ID for r in m.roles)))
        if not is_admin:
            return await interaction.response.send_message("⛔ Brak uprawnień!", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
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

# ====== Healthcheck HTTP (Render + UptimeRobot) ======
async def _health_ok(request):
    user = request.app.get('bot_user')
    txt = "ok" if user else "starting"
    return web.Response(text=txt)

async def start_health_server(bot_instance):
    app = web.Application()
    app['bot_user'] = None
    app.router.add_get('/', _health_ok)
    app.router.add_get('/health', _health_ok)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))  # Render przydziela port w $PORT
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info(f"[health] HTTP listening on 0.0.0.0:{port}")
    async def _bind_user():
        while True:
            try:
                app['bot_user'] = bot_instance.user
            except Exception:
                app['bot_user'] = None
            await asyncio.sleep(10)
    asyncio.create_task(_bind_user())
    return runner

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
    if not TOKEN:
        raise RuntimeError("Brak DISCORD_TOKEN w .env")
    # Start HTTP health server (Render/UptimeRobot)
    health_runner = await start_health_server(bot)
    # Uruchom bota
    try:
        async with bot:
            await bot.start(TOKEN)
    finally:
        try:
            await health_runner.cleanup()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(main())
