# --== bot.py (full with MCL & ZoneWars) ==--
import os
import asyncio
import logging
from datetime import datetime, timedelta
import re
from dotenv import load_dotenv

# ===== Timezone Europe/Warsaw with safe fallback =====
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
    # fallback: system local tzinfo
    try:
        return datetime.now().astimezone().tzinfo
    except Exception:
        return None

WARSAW = get_warsaw_tz()


# ===== Discord =====
import discord
from discord.ext import commands
from discord import app_commands

# ===== Tiny HTTP health server (optional) =====
from aiohttp import web

async def _health(_request):
    return web.Response(text="OK")

async def _setup_http():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)

    host = os.getenv("HOST") or "127.0.0.1"
    ports_to_try = []
    if os.getenv("PORT"):
        try:
            ports_to_try.append(int(os.getenv("PORT")))
        except Exception:
            pass
    ports_to_try += [10000, 0]  # 0 -> random free port

    runner = web.AppRunner(app)
    await runner.setup()

    for port in ports_to_try:
        try:
            site = web.TCPSite(runner, host=host, port=port)
            await site.start()
            try:
                if getattr(site, "_server", None) and site._server.sockets:
                    port = site._server.sockets[0].getsockname()[1]
            except Exception:
                pass
            logging.getLogger("http").info(f"HTTP health server on {host}:{port}")
            return
        except Exception as e:
            logging.getLogger("http").warning(f"Health server port {port} failed: {e}")
    logging.getLogger("http").warning("Health server NOT started.")

# ===== Config =====
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional but recommended for fast guild sync

# Role required to use commands (besides admins/owner)
REQUIRED_ROLE_ID = int(os.getenv("REQUIRED_ROLE_ID", "0"))

# Images / channel used by pings
CAYO_IMAGE_URL    = os.getenv("CAYO_IMAGE_URL", "https://cdn.discordapp.com/attachments/1224129510535069766/1414204332747915274/image.png?ex=68e644eb&is=68e4f36b&hm=85fb17e716b33129fe78f48823089127f4dfbf5d3336428125dd7ec9576b2838&")
ZANCUDO_IMAGE_URL = os.getenv("ZANCUDO_IMAGE_URL", "https://cdn.discordapp.com/attachments/1224129510535069766/1414194392214011974/image.png?ex=68e63ba9&is=68e4ea29&hm=50dc577e382a4f9c3c5f40c2c7debad58ae5e62eb4975441ee4df85c76ea53b3&")
LOGO_URL          = os.getenv("LOGO_URL", "https://cdn.discordapp.com/icons/1422343547780337819/683dd456c5dc7e124326d4d810992d07.webp?size=1024")
CAPT_CHANNEL_ID   = int(os.getenv("CAPT_CHANNEL_ID", "0"))

intents = discord.Intents.default()
intents.message_content = False
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ===== Active registries =====
ACTIVE_CAPTS = {}     # (guild_id, channel_id) -> CaptView
ACTIVE_AIRDROPS = {}  # (guild_id, channel_id) -> AirdropView

# ===== Permissions check =====
def role_required_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.CheckFailure("Tej komendy można użyć tylko na serwerze.")
        m: discord.Member = interaction.user
        if m.guild_permissions.administrator or m == interaction.guild.owner:
            return True
        if REQUIRED_ROLE_ID and any(r.id == REQUIRED_ROLE_ID for r in m.roles):
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

# ===== Helpers =====

# Extra helpers used by ping-* commands
def _parse_hhmm_to_dt(raw_time: str):
    """Parse times like '19:00', '19.00', '19 00', or '1900' -> datetime in the nearest future.
    Tries Europe/Warsaw tz if available (WARSAW), otherwise naive local time.
    """
    raw = str(raw_time or "").strip()
    parts = re.findall(r"\d+", raw)
    if len(parts) >= 2:
        hh, mm = int(parts[0]), int(parts[1])
    elif len(parts) == 1 and len(parts[0]) in (3, 4):
        hh = int(parts[0][:-2])
        mm = int(parts[0][-2:])
    else:
        raise ValueError("bad time format")
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("time out of range")
    from datetime import datetime, timedelta
    try:
        now_pl = datetime.now(tz=WARSAW)
        t = datetime(now_pl.year, now_pl.month, now_pl.day, hh, mm, tzinfo=WARSAW)
        return t if t > now_pl else t + timedelta(days=1)
    except Exception:
        now_local = datetime.now()
        t = datetime(now_local.year, now_local.month, now_local.day, hh, mm)
        return t if t > now_local else t + timedelta(days=1)

def _rel_pl(dtobj):
    """Return Discord relative timestamp for an aware datetime."""
    try:
        ts = int(dtobj.timestamp())
    except Exception:
        from datetime import datetime
        ts = int(datetime.now().timestamp())
    return f"<t:{ts}:R>"
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


def fmt_users(
    user_ids: list[int],
    guild: discord.Guild,
    input_map: dict[int, str] | None = None,
    extra_labels: dict[int, str] | None = None,
    start_index: int = 1,
    limit: int = 25,
) -> str:
    """{LP}. @mention | [tekst z zapisu] | [etykieta]"""
    input_map = input_map or {}
    extra_labels = extra_labels or {}

    if not user_ids:
        return ""

    lines: list[str] = []
    for idx, uid in enumerate(user_ids[:limit], start=start_index):
        m = guild.get_member(uid)
        mention = (m.mention if m else f"<@{uid}>")
        parts = [mention]

        signup_text = input_map.get(uid)
        if signup_text:
            parts.append(signup_text)

        label = extra_labels.get(uid)
        if label:
            parts.append(label)

        lines.append(f"{idx}. " + " | ".join(parts))

    if len(user_ids) > limit:
        lines.append(f"(+{len(user_ids) - limit})")

    return "\n".join(lines)

def format_numbered_users(user_ids, guild: discord.Guild):
    lines = []
    for i, uid in enumerate(user_ids, start=1):
        m = guild.get_member(uid)
        lines.append(f"{i}. {m.mention} | {m.display_name}" if m else f"{i}. <@{uid}>")
    return lines

def chunk_lines(lines, max_chars: int = 1800):
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

# ===================== CAPT =====================
def make_main_embed(starts_at: datetime, users, guild: discord.Guild,
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
    emb.add_field(name=f"Zapisani ({len(users)}):", value="-", inline=False)

    thumb = _thumb_url(guild)
    if thumb: emb.set_thumbnail(url=thumb)
    if image_url: emb.set_image(url=image_url)

    emb.set_footer(text=f"Wystawione przez {author.display_name}")
    return emb

def make_pick_embed(selected_ids, total_count: int, guild: discord.Guild,
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

class PickView(discord.ui.View):
    def __init__(self, capt: "CaptView", option_rows, total_count: int, picker: discord.Member):
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
            try:
                await inter.response.edit_message(content=txt, view=self)
            except Exception:
                try:
                    await inter.response.defer(ephemeral=True, thinking=False)
                except Exception:
                    pass
                await inter.followup.send(txt, ephemeral=True)
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


class CaptPagedPickView(discord.ui.View):
    """Paginowany PICK dla CAPT: przeglądaj wszystkie zapisane (po 25) i wybierz max 25."""
    PAGE_SIZE = 25
    MAX_PICK = 25

    def __init__(self, capt: "CaptView", picker: discord.Member):
        super().__init__(timeout=300)
        self.capt = capt
        self.picker = picker
        # Przygotuj pełną listę opcji
        self.option_rows = []
        for uid in self.capt.users:
            m = self.capt.guild.get_member(uid)
            nick_label = m.display_name if m else f"Użytkownik {uid}"
            user_desc = f"@{m.name}" if m else f"ID {uid}"
            self.option_rows.append((uid, nick_label, user_desc))

        self.page = 0
        self.page_selections: dict[int, set[int]] = {}  # page -> set(user_id)
        self._build_select_for_page()

    def _build_select_for_page(self):
        # Usuń istniejący select (jeśli jest)
        for child in list(self.children):
            if isinstance(child, discord.ui.Select):
                self.remove_item(child)
        start = self.page * self.PAGE_SIZE
        end = start + self.PAGE_SIZE
        slice_rows = self.option_rows[start:end]
        options = []
        for idx, (uid, label, desc) in enumerate(slice_rows, start=1):
            options.append(discord.SelectOption(label=f"{idx}. {label}"[:100], value=str(uid), description=(desc or f"ID {uid}")[:100]))
        selected_on_page = self.page_selections.get(self.page, set())
        # oblicz ile jeszcze można wybrać
        current_total = sum(len(s) for s in self.page_selections.values())
        remaining = max(0, self.MAX_PICK - current_total)
        max_vals = min(len(options), remaining) if remaining > 0 else 0
        if max_vals == 0 and options:
            # Nie można już nic dodać — ale pokaż wybór jako disabled
            sel = discord.ui.Select(placeholder=f"Wybrano {current_total}/{self.MAX_PICK}. Limit osiągnięty.", min_values=0, max_values=0, options=options, disabled=True)
        else:
            sel = discord.ui.Select(placeholder=f"Wybierz graczy (strona {self.page+1}/{(len(self.option_rows)-1)//self.PAGE_SIZE+1})", min_values=0, max_values=max_vals or 1, options=options)
        async def _on_select(inter: discord.Interaction):
            # Aktualizuj wybór dla bieżącej strony
            try:
                chosen = {int(v) for v in (sel.values or [])}
                self.page_selections[self.page] = chosen
                current_total = sum(len(s) for s in self.page_selections.values())
                names = []
                for p, s in self.page_selections.items():
                    for uid in s:
                        m = self.capt.guild.get_member(uid)
                        names.append(m.display_name if m else f"ID {uid}")
                txt = f"Zaznaczono {current_total}/{self.MAX_PICK}:\n" + (", ".join(names) if names else "-")
                # przerenderuj select (może zmienić się remaining)
                self._build_select_for_page()
                await inter.response.edit_message(content=txt, view=self)
            except Exception:
                try:
                    await inter.response.defer(ephemeral=True, thinking=False)
                except Exception:
                    pass
        sel.callback = _on_select
        self.add_item(sel)

    @discord.ui.button(label="◀︎", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            self._build_select_for_page()
        await interaction.response.edit_message(content=None, view=self)

    @discord.ui.button(label="▶︎", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        max_page = (len(self.option_rows) - 1) // self.PAGE_SIZE if self.option_rows else 0
        if self.page < max_page:
            self.page += 1
            self._build_select_for_page()
        await interaction.response.edit_message(content=None, view=self)

    @discord.ui.button(label="Wyczyść wybór", style=discord.ButtonStyle.danger)
    async def clear_sel(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page_selections.clear()
        self._build_select_for_page()
        await interaction.response.edit_message(content="Wyczyszczono wybór.", view=self)

    @discord.ui.button(label="Publikuj listę", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button):
        chosen = []
        for s in self.page_selections.values():
            chosen.extend(list(s))
        # ogranicz do 25 na wszelki wypadek (stabilność)
        chosen = list(dict.fromkeys(chosen))[:self.MAX_PICK]
        if not chosen:
            return await interaction.response.edit_message(content="Nie wybrano żadnych osób.", view=self)
        self.capt.picked_list = list(chosen)
        emb = make_pick_embed(chosen, len(self.capt.users), self.capt.guild, self.picker)
        msg = await interaction.channel.send(embed=emb)
        self.capt.pick_message = msg
        await interaction.response.edit_message(content="Opublikowano listę CAPT.", view=None)
class CaptView(discord.ui.View):
    def __init__(self, starts_at: datetime, guild: discord.Guild, author: discord.Member, image_url: str):
        try:
            remain = int((starts_at - datetime.now(tz=WARSAW)).total_seconds())
        except Exception:
            remain = 0
        timeout_seconds = max(60, remain + 3600)
        super().__init__(timeout=timeout_seconds)
        self.starts_at = starts_at
        self.users = []
        self.picked_list = []  # LISTA CAPTURES
        self.guild = guild
        self.author = author
        self.event_name = "CAPT"
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
        if not (mem.guild_permissions.administrator or mem == self.author or (REQUIRED_ROLE_ID and any(r.id == REQUIRED_ROLE_ID for r in mem.roles))):
            await interaction.response.send_message("Tylko wystawiający / admin / rola uprawniona może wybierać osoby.", ephemeral=True)
            return
        if not self.users:
            await interaction.response.send_message("Nikt się jeszcze nie zapisał.", ephemeral=True)
            return
        view = CaptPagedPickView(self, mem)
        await interaction.response.send_message(
            "Wybierz graczy z pełnej listy zapisanych (przełączaj strony ◀︎ ▶︎). Maksymalnie 25, potem **Publikuj listę**.",
            view=view, ephemeral=True
        )
    @discord.ui.button(label="PICK", style=discord.ButtonStyle.primary)
    async def pick(self, interaction: discord.Interaction, _: discord.ui.Button):
        mem: discord.Member = interaction.user
        if not (mem.guild_permissions.administrator or mem == self.author or (REQUIRED_ROLE_ID and any(r.id == REQUIRED_ROLE_ID for r in mem.roles))):
            await interaction.response.send_message("Tylko wystawiający / admin / uprawniona rola może wybierać osoby.", ephemeral=True)
            return
        if not self.users:
            await interaction.response.send_message("Nikt się jeszcze nie zapisał.", ephemeral=True)
            return
        view = CaptPagedPickView(self, mem)
        await interaction.response.send_message(
            "Wybierz graczy z pełnej listy zapisanych (paginacja ◀︎ ▶︎). Maksymalnie 25, następnie **Publikuj listę**.",
            view=view, ephemeral=True
        )

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

# ===================== AIRDROP =====================
def make_airdrop_embed(starts_at: datetime, users, guild: discord.Guild,
                       author: discord.Member, info_text: str,
                       voice: discord.VoiceChannel | None, max_slots: int, queue_len: int) -> discord.Embed:
    ts = int(starts_at.timestamp())
    desc_parts = []
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

def make_airdrop_picked_embed(picked_ids, guild: discord.Guild, picker: discord.Member | None) -> discord.Embed:
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

class AirdropPickView(discord.ui.View):
    def __init__(self, adr: "AirdropView", option_rows, picker: discord.Member):
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
            try:
                await inter.response.edit_message(content=txt, view=self)
            except Exception:
                try:
                    await inter.response.defer(ephemeral=True, thinking=False)
                except Exception:
                    pass
                await inter.followup.send(txt, ephemeral=True)
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

class AirdropView(discord.ui.View):
    def __init__(self, starts_at: datetime, guild: discord.Guild, author: discord.Member,
                 info_text: str, voice: discord.VoiceChannel | None, max_slots: int = 0):
        try:
            remain = int((starts_at - datetime.now(tz=WARSAW)).total_seconds())
        except Exception:
            remain = 0
        timeout_seconds = max(60, remain + 3600)
        super().__init__(timeout=timeout_seconds)
        self.starts_at = starts_at
        self.guild = guild
        self.author = author
        self.event_name = "AirDrop"
        self.info_text = info_text
        self.voice = voice
        self.max_slots = 0  # unlimited signups  # 0 = bez limitu
        self.users = []    # zapisani
        self.queue = []    # kolejka (gdy limit)
        self.picked_list = []  # WYTYPOWANI (drugi embed)
        self.message: discord.Message | None = None
        self.picked_message: discord.Message | None = None
        self._lock = asyncio.Lock()

    async def refresh_embed(self):
        if not self.message:
            return
        is_full = (self.max_slots > 0 and len(self.users) >= self.max_slots)
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label == "Dołącz":
                item.disabled = is_full
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
                pass
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

    @discord.ui.button(label="PICK", style=discord.ButtonStyle.secondary)
    async def pick_from_signups(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.users:
            await interaction.response.send_message("Brak zapisanych.", ephemeral=True)
            return
        option_rows = []
        for uid in self.users:
            m = self.guild.get_member(uid)
            nick = m.display_name if m else f"User {uid}"
            desc = f"@{m.name}" if m else f"ID {uid}"
            option_rows.append((uid, nick, desc))
        view = AirdropPickView(self, option_rows, interaction.user)
        await interaction.response.send_message(
            "Wybierz osoby z **zapisanych** (**max 20**) do wytypowania. Następnie kliknij **Publikuj Wytypowanych**.",
            view=view, ephemeral=True
        )

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

# ===================== MCL (custom flow) =====================
MCL_MAX_PICK = 20  # ile osób można wytypować

def mcl_make_embed(title_text: str,
                   voice: discord.VoiceChannel,
                   start_at: datetime,
                   tp_at: datetime,
                   guild: discord.Guild,
                   signed_count: int) -> discord.Embed:
    ts_start = int(start_at.timestamp())
    ts_tp = int(tp_at.timestamp())
    desc = (
        f"**Start:** <t:{ts_start}:t> • <t:{ts_start}:R>\n"
        f"**Teleportacja:** <t:{ts_tp}:t> • <t:{ts_tp}:R>\n"
        f"**Kanał głosowy:** {voice.mention}\n\n"
        "Kliknij **Zapisz się** aby dołączyć. Wpisz: **Imię Nazwisko | UID**.\n\n"
        f"**Zapisani ({signed_count}):**\n-"
    )
    emb = discord.Embed(title=title_text, description=desc, color=0xFFFFFF)
    thumb = _thumb_url(guild)
    if thumb:
        emb.set_thumbnail(url=thumb)
    return emb

def mcl_make_selected_embed(picker: discord.Member,
                            guild: discord.Guild,
                            selected_ids: list[int],
                            input_map: dict[int, str],
                            extra_labels: dict[int, str],
                            event_name: str = "MCL") -> discord.Embed:
    now_pl = datetime.now(tz=WARSAW) if WARSAW else datetime.now()
    lines = []
    for i, uid in enumerate(selected_ids, start=1):
        m = guild.get_member(uid)
        nick = f"{m.mention}" if m else f"<@{uid}>"
        extra = input_map.get(uid, "").strip()
        role = extra_labels.get(uid, "").strip()
        row = f"{i}. {nick} | {extra}" if extra else f"{i}. {nick}"
        if role:
            row += f" {role}"
        lines.append(row)
    desc = f"**Wytypowani na {event_name}!**\n" + ("\n".join(lines) if lines else "-")
    emb = discord.Embed(title=f"Wytypowani na {event_name}!", description=desc, color=0xFFFFFF)
    thumb = _thumb_url(guild)
    if thumb:
        emb.set_thumbnail(url=thumb)
    emb.set_footer(text=f"Wytypował: {picker.display_name} • {now_pl.strftime('%d.%m.%Y %H:%M')}")
    return emb

class MclSignupModal(discord.ui.Modal):
    def __init__(self, view: "MclView"):
        super().__init__(title=f"Zapis na {view.event_name}")
        self.view = view
        self.name_uid = discord.ui.TextInput(
            label="Imię Nazwisko | UID",
            placeholder="np. Jan Kowalski | 12345",
            min_length=3,
            max_length=80,
            required=True
        )
        self.add_item(self.name_uid)

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.name_uid.value).strip()
        await self.view.add_or_update_signup(interaction.user, text)
        try:
            await interaction.response.send_message("✅ Zapis przyjęty.", ephemeral=True)
        except Exception:
            pass

class MclAssignLabelModal(discord.ui.Modal, title="Nadaj/edytuj etykietę"):
    def __init__(self, view: "MclSelectedView", target_user_id: int):
        super().__init__(timeout=None)
        self.view = view
        self.target_user_id = target_user_id
        existing = view.extra_labels.get(target_user_id, "")
        self.label_input = discord.ui.TextInput(
            label="Etykieta (np. caller, flank) – puste aby usunąć",
            placeholder="np. caller",
            default=existing,
            max_length=24,
            required=False
        )
        self.add_item(self.label_input)

    async def on_submit(self, interaction: discord.Interaction):
        label = str(self.label_input.value or "").strip()
        if label:
            self.view.extra_labels[self.target_user_id] = label
        else:
            self.view.extra_labels.pop(self.target_user_id, None)
        await self.view.refresh_selected_embed(interaction.channel, interaction.user)
        try:
            await interaction.response.send_message("✅ Zaktualizowano etykietę.", ephemeral=True)
        except Exception:
            pass




class MclAssignLabelModal(discord.ui.Modal, title="Nadaj/edytuj etykietę"):
    def __init__(self, sel_view: "MclSelectedView", uid: int):
        super().__init__()
        self.sel_view = sel_view
        self.uid = uid
        # Discord requires text input label <= 45 chars
        self.label_input = discord.ui.TextInput(
            label="Etykieta przy nicku",           # <=45
            placeholder="np. caller / support / lider",
            max_length=30,
            required=False
        )
        self.add_item(self.label_input)

    async def on_submit(self, interaction: discord.Interaction):
        text = (self.label_input.value or "").strip()
        # Store/remove label
        if text:
            self.sel_view.extra_labels[self.uid] = text[:30]
        else:
            self.sel_view.extra_labels.pop(self.uid, None)
        # Update the published embed
        try:
            await self.sel_view.refresh_selected_embed(interaction.channel, interaction.user)
            await interaction.response.send_message("Zapisano etykietę.", ephemeral=True)
        except Exception:
            # in case the message is already acknowledged
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("Zapisano etykietę.", ephemeral=True)
                else:
                    await interaction.response.send_message("Zapisano etykietę.", ephemeral=True)
            except Exception:
                pass
class MclAssignLabelPicker(discord.ui.View):
    def __init__(self, selected_view: "MclSelectedView"):
        super().__init__(timeout=300)
        self.sel_view = selected_view
        options = []
        for uid in selected_view.selected_ids[:25]:
            m = selected_view.guild.get_member(uid)
            label = (m.display_name if m else f"User {uid}")[:100]
            signup_text = (selected_view.input_map.get(uid, "")[:40])
            current = selected_view.extra_labels.get(uid, "")
            desc = (signup_text + (" | " + current if current else "")) or f"ID {uid}"
            options.append(discord.SelectOption(label=label, value=str(uid), description=desc))
        if not options:
            self.add_item(discord.ui.Button(label="Lista wytypowanych jest pusta", style=discord.ButtonStyle.secondary, disabled=True))
            return
        self.select = discord.ui.Select(placeholder="Wybierz gracza do etykiety", min_values=1, max_values=1, options=options)
        self.add_item(self.select)

        async def _on_select(inter: discord.Interaction):
            uid = int(self.select.values[0])
            await inter.response.send_modal(MclAssignLabelModal(self.sel_view, uid))
        self.select.callback = _on_select

        self.select.callback = _on_select

    @discord.ui.button(label="Zamknij", style=discord.ButtonStyle.secondary)
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Zamknięto.", view=None)

class MclSelectedView(discord.ui.View):
    def __init__(self, parent: "MclView", picker: discord.Member):
        super().__init__(timeout=600)
        self.parent = parent
        self.picker = picker
        self.guild = parent.guild
        self.selected_ids: list[int] = list(parent.selected_ids)  # snapshot
        self.input_map: dict[int, str] = parent.input_map
        self.extra_labels: dict[int, str] = parent.extra_labels
        self.message: discord.Message | None = None

    async def refresh_selected_embed(self, channel: discord.abc.Messageable, picker: discord.Member | None):
        emb = mcl_make_selected_embed(picker or self.picker, self.guild, self.selected_ids, self.input_map, self.extra_labels, self.parent.event_name)
        if self.message:
            try:
                await self.message.edit(embed=emb, view=self)
                return
            except Exception:
                self.message = None
        try:
            self.message = await channel.send(embed=emb, view=self)
        except Exception:
            pass

    @discord.ui.button(label="NadajRole", style=discord.ButtonStyle.primary)
    async def assign_labels(self, interaction: discord.Interaction, _: discord.ui.Button):
        mem: discord.Member = interaction.user
        if not (mem.guild_permissions.administrator or (REQUIRED_ROLE_ID and any(r.id == REQUIRED_ROLE_ID for r in mem.roles))):
            return await interaction.response.send_message("Brak uprawnień.", ephemeral=True)
        await interaction.response.send_message("Wybierz gracza do nadania/edycji etykiety:", view=MclAssignLabelPicker(self), ephemeral=True)
    @discord.ui.button(label="PANEL", style=discord.ButtonStyle.secondary)
    async def manage_panel(self, interaction: discord.Interaction, _: discord.ui.Button):
        # Otwórz panel zarządzania (ephemeral)
        try:
            await interaction.response.send_message("Panel zarządzania składem:", ephemeral=True, view=MclManagePanel(self))
        except Exception:
            try:
                await interaction.followup.send("Panel zarządzania składem:", ephemeral=True, view=MclManagePanel(self))
            except Exception:
                pass



class MclPickView(discord.ui.View):
    def __init__(self, mclview: "MclView", opener: discord.Member):
        super().__init__(timeout=180)
        self.mcl = mclview
        self.opener = opener

        # build options from signups
        options = []
        for uid in self.mcl.signups[:25]:
            m = self.mcl.guild.get_member(uid)
            label = (m.display_name if m else f"User {uid}")[:100]
            options.append(discord.SelectOption(label=label, value=str(uid)))
        placeholder = f"Wybierz graczy (max {self.mcl.max_pick})"
        self.select = discord.ui.Select(placeholder=placeholder, min_values=0, max_values = min(self.mcl.max_pick, len(options)), options=options)
        self.add_item(self.select)
        self.select.callback = self._on_select  # wire once

    async def _on_select(self, inter: discord.Interaction):
        # Always acknowledge the interaction to avoid 'This action failed'
        try:
            chosen = [int(v) for v in (self.select.values or [])]
            names = []
            for uid in chosen:
                m = self.mcl.guild.get_member(uid)
                names.append(m.display_name if m else str(uid))
            txt = "Wybrane osoby: " + (", ".join(names) if names else "brak")
            try:
                await inter.response.edit_message(content=txt, view=self)
            except Exception:
                try:
                    await inter.response.defer(ephemeral=True, thinking=False)
                except Exception:
                    pass
                await inter.followup.send(txt, ephemeral=True)
        except Exception:
            # last resort so discord doesn't show the red error
            try:
                if inter.response.is_done():
                    await inter.followup.send("Zaznaczanie nie powiodło się, spróbuj ponownie.", ephemeral=True)
                else:
                    await inter.response.send_message("Zaznaczanie nie powiodło się, spróbuj ponownie.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Publikuj listę", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button):
        values = self.select.values or []
        chosen = [int(v) for v in values]
        if not chosen:
            return await interaction.response.edit_message(content="Nie wybrano żadnych osób.", view=self)

        self.mcl.selected_ids = list(dict.fromkeys(chosen))
        sel_view = MclSelectedView(self.mcl, interaction.user)
        await sel_view.refresh_selected_embed(interaction.channel, interaction.user)

        await interaction.response.edit_message(content=f"Opublikowano listę **Wytypowani na {self.mcl.event_name}!** (dodano przycisk **NadajRole**).", view=None)

    @discord.ui.button(label="Anuluj", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Anulowano.", view=None)
        self.stop()




class ZoneWarsPickView(MclPickView):
    """Oddzielny widok picków dla ZoneWars."""
    def __init__(self, mcl_view: "MclView", opener: discord.Member):
        super().__init__(mcl_view, opener)

class MclManagePanel(discord.ui.View):
    """Panel z przyciskami: Dodaj z zapisanych / Usuń z wytypowanych"""
    def __init__(self, sel_view: "MclSelectedView"):
        super().__init__(timeout=300)
        self.sel_view = sel_view

    @discord.ui.button(label="Dodaj osoby", style=discord.ButtonStyle.success)
    async def add_from_signups(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = MclPanelAddView(self.sel_view)
        await interaction.response.edit_message(content="Wybierz osoby do dodania:", view=view)

    @discord.ui.button(label="Usuń osoby", style=discord.ButtonStyle.danger)
    async def remove_from_selected(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = MclPanelRemoveView(self.sel_view)
        await interaction.response.edit_message(content="Wybierz osoby do usunięcia:", view=view)

    @discord.ui.button(label="Zamknij", style=discord.ButtonStyle.secondary)
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Zamknięto panel.", view=None)


class _BasePanelSelect(discord.ui.View):
    def __init__(self, sel_view: "MclSelectedView"):
        super().__init__(timeout=300)
        self.sel_view = sel_view
        self.select = None  # type: ignore

    def _build_select(self, placeholder: str, options: list[discord.SelectOption], max_values: int = 20):
        if not options:
            self.add_item(discord.ui.Button(label="Brak pozycji", style=discord.ButtonStyle.secondary, disabled=True))
            self.add_item(discord.ui.Button(label="Wróć", style=discord.ButtonStyle.secondary))
            return
        self.select = discord.ui.Select(placeholder=placeholder, min_values=0, max_values=min(max_values, len(options)), options=options)
        self.add_item(self.select)



class MclPanelAddView(_BasePanelSelect):
    """Dodawanie do wytypowanych: działa natychmiast po kliknięciu w select (bez potwierdzania)."""
    def __init__(self, sel_view: "MclSelectedView"):
        super().__init__(sel_view)
        # lista z zapisanych, których jeszcze nie ma w selected
        available = [uid for uid in sel_view.parent.signups if uid not in sel_view.selected_ids]
        options: list[discord.SelectOption] = []
        for uid in available[:25]:
            m = sel_view.guild.get_member(uid)
            label = (m.display_name if m else f"User {uid}")[:100]
            desc = (sel_view.input_map.get(uid, "")[:96]) or f"ID {uid}"
            options.append(discord.SelectOption(label=label, value=str(uid), description=desc))
        if not options:
            self.add_item(discord.ui.Button(label="Brak osób do dodania", style=discord.ButtonStyle.secondary, disabled=True))
        else:
            self.select = discord.ui.Select(placeholder="Dodaj z zapisanych…", min_values=1, max_values=1, options=options)
            self.add_item(self.select)

            async def _on_pick(inter: discord.Interaction):
                try:
                    uid = int(self.select.values[0])
                    if uid not in self.sel_view.selected_ids:
                        self.sel_view.selected_ids.append(uid)
                        # synchronizuj z parentem
                        self.sel_view.parent.selected_ids = list(self.sel_view.selected_ids)
                        try:
                            await self.sel_view.refresh_selected_embed(inter.channel, inter.user)
                        except Exception:
                            pass
                    # przebuduj opcje (żeby zniknęła dodana osoba)
                    new_view = MclPanelAddView(self.sel_view)
                    content = f"Dodano: <@{uid}>. Wybierz kolejną osobę albo wróć."
                    try:
                        await inter.response.edit_message(content=content, view=new_view)
                    except Exception:
                        try:
                            await inter.response.defer(ephemeral=True, thinking=False)
                        except Exception:
                            pass
                        await inter.followup.send(content, ephemeral=True, view=new_view)
                except Exception:
                    try:
                        await inter.response.send_message("Nie udało się dodać. Spróbuj ponownie.", ephemeral=True)
                    except Exception:
                        pass

            self.select.callback = _on_pick

    @discord.ui.button(label="Wróć", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Panel zarządzania składem:", view=MclManagePanel(self.sel_view))

    @discord.ui.button(label="Zamknij", style=discord.ButtonStyle.secondary)
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Zamknięto panel.", view=None)


class MclPanelRemoveView(_BasePanelSelect):
    """Usuwanie z wytypowanych: działa natychmiast po kliknięciu w select (bez potwierdzania)."""
    def __init__(self, sel_view: "MclSelectedView"):
        super().__init__(sel_view)
        options: list[discord.SelectOption] = []
        for uid in sel_view.selected_ids[:25]:
            m = sel_view.guild.get_member(uid)
            label = (m.display_name if m else f"User {uid}")[:100]
            desc = (sel_view.input_map.get(uid, "")[:96]) or f"ID {uid}"
            options.append(discord.SelectOption(label=label, value=str(uid), description=desc))
        if not options:
            self.add_item(discord.ui.Button(label="Brak osób do usunięcia", style=discord.ButtonStyle.secondary, disabled=True))
        else:
            self.select = discord.ui.Select(placeholder="Usuń z wytypowanych…", min_values=1, max_values=1, options=options)
            self.add_item(self.select)

            async def _on_pick(inter: discord.Interaction):
                try:
                    uid = int(self.select.values[0])
                    if uid in self.sel_view.selected_ids:
                        self.sel_view.selected_ids = [x for x in self.sel_view.selected_ids if x != uid]
                        # synchronizuj z parentem
                        self.sel_view.parent.selected_ids = list(self.sel_view.selected_ids)
                        try:
                            await self.sel_view.refresh_selected_embed(inter.channel, inter.user)
                        except Exception:
                            pass
                    new_view = MclPanelRemoveView(self.sel_view)
                    content = f"Usunięto: <@{uid}>. Wybierz kolejną osobę albo wróć."
                    try:
                        await inter.response.edit_message(content=content, view=new_view)
                    except Exception:
                        try:
                            await inter.response.defer(ephemeral=True, thinking=False)
                        except Exception:
                            pass
                        await inter.followup.send(content, ephemeral=True, view=new_view)
                except Exception:
                    try:
                        await inter.response.send_message("Nie udało się usunąć. Spróbuj ponownie.", ephemeral=True)
                    except Exception:
                        pass

            self.select.callback = _on_pick

    @discord.ui.button(label="Wróć", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Panel zarządzania składem:", view=MclManagePanel(self.sel_view))

    @discord.ui.button(label="Zamknij", style=discord.ButtonStyle.secondary)
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Zamknięto panel.", view=None)

class MclView(discord.ui.View):
    def __init__(self, title_text: str, voice: discord.VoiceChannel, start_at: datetime, tp_at: datetime, guild: discord.Guild, author: discord.Member, event_name: str = "MCL", max_pick: int = 20):
        remain = int((tp_at - datetime.now(tz=WARSAW)).total_seconds()) if WARSAW else 0
        super().__init__(timeout=max(60, remain + 3600))
        self.title_text = title_text
        self.voice = voice
        self.start_at = start_at
        self.tp_at = tp_at
        self.guild = guild
        self.author = author
        self.event_name = event_name
        self.message: discord.Message | None = None
        self.max_pick = int(max(1, max_pick))
        # signups and data
        self.signups: list[int] = []
        self.input_map: dict[int, str] = {}  # user_id -> "Imię Nazwisko | UID"
        # selected data
        self.selected_ids: list[int] = []
        self.extra_labels: dict[int, str] = {}  # user_id -> label text
        self._lock = asyncio.Lock()

    async def add_or_update_signup(self, member: discord.Member | discord.User, text: str):
        uid = member.id
        async with self._lock:
            if uid not in self.signups:
                self.signups.append(uid)
            self.input_map[uid] = text
        await self.refresh_main()

    async def remove_signup(self, member: discord.Member | discord.User):
        uid = member.id
        async with self._lock:
            changed = False
            if uid in self.signups:
                self.signups.remove(uid); changed = True
            if uid in self.selected_ids:
                self.selected_ids.remove(uid); changed = True
            self.input_map.pop(uid, None)
            self.extra_labels.pop(uid, None)
        if changed:
            await self.refresh_main()

    async def refresh_main(self):
        if not self.message:
            return
        emb = mcl_make_embed(self.title_text, self.voice, self.start_at, self.tp_at, self.guild, len(self.signups))
        emb.set_footer(text=f"Wystawione przez {self.author.display_name}")
        await self.message.edit(embed=emb, view=self)

    @discord.ui.button(label="Zapisz się", style=discord.ButtonStyle.success)
    async def join_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(MclSignupModal(self))

    @discord.ui.button(label="Opuść", style=discord.ButtonStyle.danger)
    async def leave_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.remove_signup(interaction.user)
        await interaction.response.send_message("Zaktualizowano.", ephemeral=True)

    @discord.ui.button(label="Wystaw na event (ADMIN)", style=discord.ButtonStyle.primary)
    async def admin_pick_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        mem: discord.Member = interaction.user
        if not (mem.guild_permissions.administrator or (REQUIRED_ROLE_ID and any(r.id == REQUIRED_ROLE_ID for r in mem.roles))):
            return await interaction.response.send_message("Panel tylko dla adminów / wymaganej roli.", ephemeral=True)
        if not self.signups:
            return await interaction.response.send_message("Brak zapisanych.", ephemeral=True)
        view = ZoneWarsPickView(self, mem) if self.event_name == "ZoneWars" else MclPickView(self, mem)
        await interaction.response.send_message(
            f"Wybierz osoby do **Wytypowani na {self.event_name}!** (max {self.max_pick}). Potem kliknij **Publikuj listę**.",
            view=view, ephemeral=True
        )

# ===================== SLASH COMMANDS =====================
@bot.tree.command(name="create-mcl", description="Stwórz ogłoszenie MCL: opis, kanał, start, teleportacja.")
@role_required_check()
@app_commands.describe(
    opis="Nagłówek/tytuł embeda (np. MCL).",
    voice="Kanał głosowy do wbicia.",
    start_time="Godzina startu 24h, np. 19:00.",
    tp_time="Godzina teleportacji 24h, np. 20:00."
)
async def create_mcl(interaction: discord.Interaction, opis: str, voice: discord.VoiceChannel, start_time: str, tp_time: str):
    # Parse times -> nearest future
    def _p(hhmm: str) -> datetime:
        """Parse times like '19:00', '19.00', '19 00', or '1900' -> nearest future."""
        raw = hhmm.strip()
        parts = re.findall(r"\d+", raw)
        if len(parts) >= 2:
            hh, mm = int(parts[0]), int(parts[1])
        elif len(parts) == 1 and len(parts[0]) in (3, 4):
            hh = int(parts[0][:-2])
            mm = int(parts[0][-2:])
        else:
            raise ValueError("bad time")
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("range")
        try:
            now_pl = datetime.now(tz=WARSAW)
            t = datetime(now_pl.year, now_pl.month, now_pl.day, hh, mm, tzinfo=WARSAW)
            return t if t > now_pl else t + timedelta(days=1)
        except Exception:
            now_local = datetime.now()
            t = datetime(now_local.year, now_local.month, now_local.day, hh, mm)
            return t if t > now_local else t + timedelta(days=1)

    try:
        start_at = _p(start_time)
        tp_at = _p(tp_time)
    except Exception:
        return await interaction.response.send_message("Podaj godziny w formacie **HH:MM** (np. 19:00).", ephemeral=True)

    author = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
    view = MclView(opis, voice, start_at, tp_at, interaction.guild, author, event_name="MCL", max_pick=20)
    embed = mcl_make_embed(opis, voice, start_at, tp_at, interaction.guild, 0)
    embed.set_footer(text=f"Wystawione przez {author.display_name}")
    allowed = discord.AllowedMentions(everyone=True)
    await interaction.response.send_message(content="@everyone", embed=embed, view=view, allowed_mentions=allowed)
    msg = await interaction.original_response()
    view.message = msg



class AirdropPanelView(discord.ui.View):
    def __init__(self, adr: AirdropView, invoker: discord.Member):
        super().__init__(timeout=300)
        self.adr = adr
        self.invoker = invoker

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        mem: discord.Member = interaction.user
        if mem.guild_permissions.administrator or mem == self.adr.author:
            return True
        REQUIRED = globals().get("REQUIRED_ROLE_ID", 0)
        if REQUIRED and any(r.id == REQUIRED for r in mem.roles):
            return True
        await interaction.response.send_message("Brak uprawnień do panelu AirDrop.", ephemeral=True)
        return False

    @discord.ui.button(label="Publikuj listę", style=discord.ButtonStyle.success)
    async def publish(self, it: discord.Interaction, _: discord.ui.Button):
        await it.response.defer(ephemeral=True, thinking=False)
        await self.adr.refresh_picked_embed(it.channel, it.user)

    @discord.ui.button(label="Wyczyść wytypowanych", style=discord.ButtonStyle.danger)
    async def clear_picked(self, it: discord.Interaction, _: discord.ui.Button):
        self.adr.picked_list.clear()
        await it.response.send_message("Wyczyszczono listę WYTYPOWANYCH.", ephemeral=True)
        await self.adr.refresh_embed()
        await self.adr.refresh_picked_embed(it.channel, it.user)

    @discord.ui.button(label="Pokaż wytypowanych", style=discord.ButtonStyle.secondary)
    async def show_picked(self, it: discord.Interaction, _: discord.ui.Button):
        if not self.adr.picked_list:
            await it.response.send_message("Brak WYTYPOWANYCH.", ephemeral=True)
            return
        lines = []
        for i, uid in enumerate(self.adr.picked_list, 1):
            lines.append(f"{i}. <@{uid}>")
        emb = discord.Embed(title=f"Wytypowani ({len(self.adr.picked_list)})", description="\\n".join(lines), color=0xFFFFFF)
        await it.response.send_message(embed=emb, ephemeral=True)


    @discord.ui.button(label="Dodaj osobę", style=discord.ButtonStyle.primary)
    async def add_person(self, it: discord.Interaction, _: discord.ui.Button):
        # Ephemeral mini-panel z wyborem użytkownika z całego serwera
        await it.response.send_message(
            "Wybierz osobę z serwera i kliknij **Dodaj**.",
            view=AddPickedView(self.adr), ephemeral=True
        )

    @discord.ui.button(label="Usuń osobę", style=discord.ButtonStyle.danger)
    async def remove_person(self, it: discord.Interaction, _: discord.ui.Button):
        # Ephemeral mini-panel z wyborem użytkownika do usunięcia
        await it.response.send_message(
            "Wybierz osobę do **usunięcia** z listy wytypowanych i kliknij **Usuń**.",
            view=RemovePickedView(self.adr), ephemeral=True
        )



class AddPickedView(discord.ui.View):
    def __init__(self, adr: "AirdropView"):
        super().__init__(timeout=180)
        self.adr = adr
        self.user_select = discord.ui.UserSelect(placeholder="Wybierz osobę z serwera", min_values=1, max_values=1)
        self.add_item(self.user_select)

    @discord.ui.button(label="Dodaj", style=discord.ButtonStyle.success)
    async def do_add(self, it: discord.Interaction, _: discord.ui.Button):
        await it.response.defer(ephemeral=True, thinking=False)
        if not self.user_select.values:
            return
        user = self.user_select.values[0]
        if user.id not in self.adr.picked_list:
            self.adr.picked_list.append(user.id)
        await self.adr.refresh_embed()
        await self.adr.refresh_picked_embed(it.channel, it.user)
        await it.followup.send(f"✅ Dodano do WYTYPOWANYCH: {user.mention}", ephemeral=True)

class RemovePickedView(discord.ui.View):
    def __init__(self, adr: "AirdropView"):
        super().__init__(timeout=180)
        self.adr = adr
        self.user_select = discord.ui.UserSelect(placeholder="Wybierz osobę do usunięcia", min_values=1, max_values=1)
        self.add_item(self.user_select)

    @discord.ui.button(label="Usuń", style=discord.ButtonStyle.danger)
    async def do_remove(self, it: discord.Interaction, _: discord.ui.Button):
        await it.response.defer(ephemeral=True, thinking=False)
        if not self.user_select.values:
            return
        user = self.user_select.values[0]
        if user.id in self.adr.picked_list:
            self.adr.picked_list.remove(user.id)
            await self.adr.refresh_embed()
            await self.adr.refresh_picked_embed(it.channel, it.user)
            await it.followup.send(f"🗑️ Usunięto z WYTYPOWANYCH: {user.mention}", ephemeral=True)
        else:
            await it.followup.send("Ta osoba nie jest na liście WYTYPOWANYCH.", ephemeral=True)




@bot.tree.command(name="create-zonewars", description="Stwórz ogłoszenie ZoneWars: opis, kanał, start, teleportacja.")
@role_required_check()
@app_commands.describe(
    opis="Nagłówek/tytuł embeda (np. ZoneWars).",
    voice="Kanał głosowy do wbicia.",
    start_time="Godzina startu 24h, np. 19:00.",
    tp_time="Godzina teleportacji 24h, np. 20:00."
)
async def create_zonewars(interaction: discord.Interaction, opis: str, voice: discord.VoiceChannel, start_time: str, tp_time: str):
    # Parse times -> nearest future
    def _p(hhmm: str) -> datetime:
        """Parse times like '19:00', '19.00', '19 00', or '1900' -> nearest future."""
        raw = hhmm.strip()
        parts = re.findall(r"\d+", raw)
        if len(parts) >= 2:
            hh, mm = int(parts[0]), int(parts[1])
        elif len(parts) == 1 and len(parts[0]) in (3, 4):
            hh = int(parts[0][:-2])
            mm = int(parts[0][-2:])
        else:
            raise ValueError("bad time")
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("range")
        try:
            now_pl = datetime.now(tz=WARSAW)
            t = datetime(now_pl.year, now_pl.month, now_pl.day, hh, mm, tzinfo=WARSAW)
            return t if t > now_pl else t + timedelta(days=1)
        except Exception:
            now_local = datetime.now()
            t = datetime(now_local.year, now_local.month, now_local.day, hh, mm)
            return t if t > now_local else t + timedelta(days=1)

    try:
        start_at = _p(start_time)
        tp_at = _p(tp_time)
    except Exception:
        return await interaction.response.send_message("Podaj godziny w formacie **HH:MM** (np. 19:00).", ephemeral=True)

    author = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
    view = MclView(opis, voice, start_at, tp_at, interaction.guild, author, event_name="ZoneWars", max_pick=25)
    embed = mcl_make_embed(opis, voice, start_at, tp_at, interaction.guild, 0)
    embed.set_footer(text=f"Wystawione przez {author.display_name}")
    allowed = discord.AllowedMentions(everyone=True)
    await interaction.response.send_message(content="@everyone", embed=embed, view=view, allowed_mentions=allowed)
    msg = await interaction.original_response()
    view.message = msg


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
                try:
                    if datetime.now(tz=WARSAW) >= starts_at:
                        final = make_main_embed(starts_at, view.users, interaction.guild, author, image_url)
                        final.description += "\n**CAPT rozpoczął się.**"
                        await msg.edit(embed=final, view=view)
                        break
                except Exception:
                    break
        except asyncio.CancelledError:
            pass
    interaction.client.loop.create_task(ticker())

@bot.tree.command(name="panel-capt", description="Otwórz panel CAPT w tym kanale.")
@role_required_check()
async def panel_capt(interaction: discord.Interaction):
    key = (interaction.guild.id, interaction.channel.id)
    capt = ACTIVE_CAPTS.get(key)
    if not capt or not capt.message:
        return await interaction.response.send_message("Brak aktywnego ogłoszenia w tym kanale.", ephemeral=True)
    mem: discord.Member = interaction.user
    if not (mem.guild_permissions.administrator or mem == capt.author or (REQUIRED_ROLE_ID and any(r.id == REQUIRED_ROLE_ID for r in mem.roles))):
        return await interaction.response.send_message("Panel dostępny dla wystawiającego, administratora lub roli uprawnionej.", ephemeral=True)
    view = PanelView(capt, mem)
    await interaction.response.send_message(
        f"Panel CAPT – zapisanych: **{len(capt.users)}**, WYBRANI: **{len(capt.picked_list)}**.",
        view=view, ephemeral=True
    )

@bot.tree.command(name="airdrop", description="Utwórz AirDrop (opis, głosowy, timer; nielimitowane zapisy, PICK max 20).")
@role_required_check()
@app_commands.describe(
    info_text="Tekst w opisie (np. zasady/uwagi).",
    voice="Kanał głosowy do AirDropa.",
    start_time="Godzina startu 24h, np. 20:00 (czas Polski).",)
async def airdrop(interaction: discord.Interaction, info_text: str, voice: discord.VoiceChannel, start_time: str):
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
    view = AirdropView(starts_at, interaction.guild, author, info_text, voice, 0)
    embed = make_airdrop_embed(starts_at, [], interaction.guild, author, info_text, voice, 0, queue_len=0)
    allowed = discord.AllowedMentions(everyone=True)
    await interaction.response.send_message(content="@everyone", embed=embed, view=view, allowed_mentions=allowed)
    msg = await interaction.original_response()
    view.message = msg
    ACTIVE_AIRDROPS[(interaction.guild.id, interaction.channel.id)] = view

    async def ticker():
        try:
            while True:
                await asyncio.sleep(15)
                try:
                    if datetime.now(tz=WARSAW) >= starts_at:
                        final = make_airdrop_embed(starts_at, view.users, interaction.guild, author, info_text, voice, 0, queue_len=0)
                        final.description += "\n**AirDrop rozpoczął się.**"
                        await msg.edit(embed=final, view=view)
                        break
                except Exception:
                    break
        except asyncio.CancelledError:
            pass
    interaction.client.loop.create_task(ticker())

@bot.tree.command(name="panel-airdrop", description="Otwórz panel AIRDROP w tym kanale (zarządza WYTYPOWANYMI).")
@role_required_check()
async def panel_airdrop(interaction: discord.Interaction):
    key = (interaction.guild.id, interaction.channel.id)
    adr = ACTIVE_AIRDROPS.get(key)
    if not adr or not adr.message:
        return await interaction.response.send_message("Brak aktywnego airdropa w tym kanale.", ephemeral=True)
    mem: discord.Member = interaction.user
    if not (mem.guild_permissions.administrator or (REQUIRED_ROLE_ID and any(r.id == REQUIRED_ROLE_ID for r in mem.roles))):
        return await interaction.response.send_message("Panel dostępny tylko dla administracji lub posiadaczy wymaganej roli.", ephemeral=True)

    await interaction.response.send_message(
        f"Panel AIRDROP – zapisanych: **{len(adr.users)}**. WYTYPOWANI: **{len(adr.picked_list)}**.",
        view=AirdropPanelView(adr, mem), ephemeral=True
    )

# ===== Pings =====
@bot.tree.command(name="purge-commands", description="(ADMIN) Usuń globalne komendy bota i wgraj aktualne tylko na tę gildię.")
@role_required_check()
async def purge_commands(interaction: discord.Interaction):
    """
    Czyści stare globalne slash-komendy tej aplikacji i wgrywa aktualny zestaw na obecną gildię.
    Używaj rozważnie. Wymaga uprawnień (admin/właściciel/rola).
    """
    try:
        # 1) Clear commands for THIS GUILD and sync the ones from this file
        guild = discord.Object(id=interaction.guild.id)
        bot.tree.clear_commands(guild=guild)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)

        # 2) Clear GLOBAL commands (removes stale global entries)
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()

        await interaction.response.send_message("✅ Wyczyszczono stare komendy i wgrano aktualne na tę gildię.", ephemeral=True)
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ Błąd purge: {e}", ephemeral=True)
        except Exception:
            pass

# ===== Lifecycle =====
@bot.event
async def on_ready():
    log.info(f"Zalogowano jako {bot.user} (id={bot.user.id})")
    # Start health server
    try:
        asyncio.create_task(_setup_http())
    except Exception:
        pass

    # Guild-preferred sync
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info(f"/ Synced {len(synced)} komend do gildii {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            log.info(f"/ Synced {len(synced)} komend globalnie")
    except Exception as e:
        log.exception(f"Sync komend nie powiódł się: {e}")



@bot.tree.command(name="ping-cayo", description="Ping o Cayo (z licznikiem i przyciskiem Będę)")
@app_commands.describe(voice_channel="Kanał głosowy", start="Godzina startu HH:MM")
async def ping_cayo(interaction: discord.Interaction, voice_channel: discord.VoiceChannel, start: str):
    LOGO = os.getenv("LOGO_URL", "")
    IMAGE = os.getenv("CAYO_IMAGE_URL", "")
    class PV(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.users: list[int] = []
        @discord.ui.button(label="Będę", style=discord.ButtonStyle.success)
        async def bede(self, it: discord.Interaction, btn: discord.ui.Button):
            if it.user.id not in self.users:
                self.users.append(it.user.id)
            start_dt = _parse_hhmm_to_dt(start)
            emb = discord.Embed(title="Atak na CAYO PERICO!", color=0xFFFFFF)
            emb.add_field(name="Zapraszamy na", value=f"{voice_channel.mention}", inline=False)
            emb.add_field(name="Start", value=f"**{start}** · {_rel_pl(start_dt)}", inline=False)
            if LOGO: emb.set_thumbnail(url=LOGO)
            if IMAGE: emb.set_image(url=IMAGE)
            emb.set_footer(text=f"Zapisani: {len(self.users)}")
            await it.message.edit(embed=emb, view=self)
            await it.response.send_message("✅ Zapisano!", ephemeral=True)
    
        @discord.ui.button(label="ListaBędę", style=discord.ButtonStyle.secondary)
        async def lista_bede(self, it: discord.Interaction, btn: discord.ui.Button):
            if not self.users:
                await it.response.send_message("📭 Nikt się jeszcze nie zapisał.", ephemeral=True)
                return
            mentions = [f"<@{uid}>" for uid in self.users[:100]]
            left = len(self.users) - len(mentions)
            lines = "\n".join(f"{i+1}. {m}" for i, m in enumerate(mentions))
            if left > 0:
                lines += f"\n… i jeszcze {left} więcej"
            emb = discord.Embed(title=f"Lista zapisanych ({len(self.users)})", description=lines, color=0xFFFFFF)
            await it.response.send_message(embed=emb, ephemeral=True)

    view = PV()
    start_dt = _parse_hhmm_to_dt(start)
    embed = discord.Embed(title="Atak na CAYO PERICO!", color=0xFFFFFF)
    embed.add_field(name="Zapraszamy na", value=f"{voice_channel.mention}", inline=False)
    embed.add_field(name="Start", value=f"**{start}** · {_rel_pl(start_dt)}", inline=False)
    LOGO = os.getenv("LOGO_URL", ""); IMAGE = os.getenv("CAYO_IMAGE_URL", "")
    if LOGO: embed.set_thumbnail(url=LOGO)
    if IMAGE: embed.set_image(url=IMAGE)
    embed.set_footer(text="Zapisani: 0")
    await interaction.response.send_message(content="@everyone", embed=embed, view=view)


@bot.tree.command(name="ping-zancudo", description="Ping o Zancudo (z licznikiem i przyciskiem Będę)")
@app_commands.describe(voice_channel="Kanał głosowy", start="Godzina startu HH:MM")
async def ping_zancudo(interaction: discord.Interaction, voice_channel: discord.VoiceChannel, start: str):
    LOGO = os.getenv("LOGO_URL", "")
    IMAGE = os.getenv("ZANCUDO_IMAGE_URL", "")
    class PV(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.users: list[int] = []
        @discord.ui.button(label="Będę", style=discord.ButtonStyle.success)
        async def bede(self, it: discord.Interaction, btn: discord.ui.Button):
            if it.user.id not in self.users:
                self.users.append(it.user.id)
            start_dt = _parse_hhmm_to_dt(start)
            emb = discord.Embed(title="Atak na FORT ZANCUDO!", color=0xFFFFFF)
            emb.add_field(name="Zapraszamy na", value=f"{voice_channel.mention}", inline=False)
            emb.add_field(name="Start", value=f"**{start}** · {_rel_pl(start_dt)}", inline=False)
            if LOGO: emb.set_thumbnail(url=LOGO)
            if IMAGE: emb.set_image(url=IMAGE)
            emb.set_footer(text=f"Zapisani: {len(self.users)}")
            await it.message.edit(embed=emb, view=self)
            await it.response.send_message("✅ Zapisano!", ephemeral=True)
    
        @discord.ui.button(label="ListaBędę", style=discord.ButtonStyle.secondary)
        async def lista_bede(self, it: discord.Interaction, btn: discord.ui.Button):
            if not self.users:
                await it.response.send_message("📭 Nikt się jeszcze nie zapisał.", ephemeral=True)
                return
            mentions = [f"<@{uid}>" for uid in self.users[:100]]
            left = len(self.users) - len(mentions)
            lines = "\n".join(f"{i+1}. {m}" for i, m in enumerate(mentions))
            if left > 0:
                lines += f"\n… i jeszcze {left} więcej"
            emb = discord.Embed(title=f"Lista zapisanych ({len(self.users)})", description=lines, color=0xFFFFFF)
            await it.response.send_message(embed=emb, ephemeral=True)

    view = PV()
    start_dt = _parse_hhmm_to_dt(start)
    embed = discord.Embed(title="Atak na FORT ZANCUDO!", color=0xFFFFFF)
    embed.add_field(name="Zapraszamy na", value=f"{voice_channel.mention}", inline=False)
    embed.add_field(name="Start", value=f"**{start}** · {_rel_pl(start_dt)}", inline=False)
    LOGO = os.getenv("LOGO_URL", ""); IMAGE = os.getenv("ZANCUDO_IMAGE_URL", "")
    if LOGO: embed.set_thumbnail(url=LOGO)
    if IMAGE: embed.set_image(url=IMAGE)
    embed.set_footer(text="Zapisani: 0")
    await interaction.response.send_message(content="@everyone", embed=embed, view=view)


@bot.tree.command(name="ping-magazyny", description="Ping o magazynach (z licznikiem i przyciskiem Będę)")
@app_commands.describe(voice_channel="Kanał głosowy", start="Godzina startu HH:MM")
async def ping_magazyny(interaction: discord.Interaction, voice_channel: discord.VoiceChannel, start: str):
    LOGO = os.getenv("LOGO_URL", "")
    class PV(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.users: list[int] = []
        @discord.ui.button(label="Będę", style=discord.ButtonStyle.success)
        async def bede(self, it: discord.Interaction, btn: discord.ui.Button):
            if it.user.id not in self.users:
                self.users.append(it.user.id)
            start_dt = _parse_hhmm_to_dt(start)
            emb = discord.Embed(title="Ping o MAGAZYNACH!", color=0xFFFFFF)
            emb.add_field(name="Zapraszamy na", value=f"{voice_channel.mention}", inline=False)
            emb.add_field(name="Start", value=f"**{start}** · {_rel_pl(start_dt)}", inline=False)
            if LOGO: emb.set_thumbnail(url=LOGO)
            emb.set_footer(text=f"Zapisani: {len(self.users)}")
            await it.message.edit(embed=emb, view=self)
            await it.response.send_message("✅ Zapisano!", ephemeral=True)
    
        @discord.ui.button(label="ListaBędę", style=discord.ButtonStyle.secondary)
        async def lista_bede(self, it: discord.Interaction, btn: discord.ui.Button):
            if not self.users:
                await it.response.send_message("📭 Nikt się jeszcze nie zapisał.", ephemeral=True)
                return
            mentions = [f"<@{uid}>" for uid in self.users[:100]]
            left = len(self.users) - len(mentions)
            lines = "\n".join(f"{i+1}. {m}" for i, m in enumerate(mentions))
            if left > 0:
                lines += f"\n… i jeszcze {left} więcej"
            emb = discord.Embed(title=f"Lista zapisanych ({len(self.users)})", description=lines, color=0xFFFFFF)
            await it.response.send_message(embed=emb, ephemeral=True)

    view = PV()
    start_dt = _parse_hhmm_to_dt(start)
    embed = discord.Embed(title="Ping o MAGAZYNACH!", color=0xFFFFFF)
    embed.add_field(name="Zapraszamy na", value=f"{voice_channel.mention}", inline=False)
    embed.add_field(name="Start", value=f"**{start}** · {_rel_pl(start_dt)}", inline=False)
    LOGO = os.getenv("LOGO_URL", "")
    if LOGO: embed.set_thumbnail(url=LOGO)
    embed.set_footer(text="Zapisani: 0")
    await interaction.response.send_message(content="@everyone", embed=embed, view=view)


@bot.tree.command(name="ping-dilerzy", description="Ping o dilerach (z licznikiem i przyciskiem Będę)")
@app_commands.describe(voice_channel="Kanał głosowy", start="Godzina startu HH:MM")
async def ping_dilerzy(interaction: discord.Interaction, voice_channel: discord.VoiceChannel, start: str):
    LOGO = os.getenv("LOGO_URL", "")
    class PV(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.users: list[int] = []
        @discord.ui.button(label="Będę", style=discord.ButtonStyle.success)
        async def bede(self, it: discord.Interaction, btn: discord.ui.Button):
            if it.user.id not in self.users:
                self.users.append(it.user.id)
            start_dt = _parse_hhmm_to_dt(start)
            emb = discord.Embed(title="Ping o DILERACH!", color=0xFFFFFF)
            emb.add_field(name="Zapraszamy na", value=f"{voice_channel.mention}", inline=False)
            emb.add_field(name="Start", value=f"**{start}** · {_rel_pl(start_dt)}", inline=False)
            if LOGO: emb.set_thumbnail(url=LOGO)
            emb.set_footer(text=f"Zapisani: {len(self.users)}")
            await it.message.edit(embed=emb, view=self)
            await it.response.send_message("✅ Zapisano!", ephemeral=True)
    
        @discord.ui.button(label="ListaBędę", style=discord.ButtonStyle.secondary)
        async def lista_bede(self, it: discord.Interaction, btn: discord.ui.Button):
            if not self.users:
                await it.response.send_message("📭 Nikt się jeszcze nie zapisał.", ephemeral=True)
                return
            mentions = [f"<@{uid}>" for uid in self.users[:100]]
            left = len(self.users) - len(mentions)
            lines = "\n".join(f"{i+1}. {m}" for i, m in enumerate(mentions))
            if left > 0:
                lines += f"\n… i jeszcze {left} więcej"
            emb = discord.Embed(title=f"Lista zapisanych ({len(self.users)})", description=lines, color=0xFFFFFF)
            await it.response.send_message(embed=emb, ephemeral=True)

    view = PV()
    start_dt = _parse_hhmm_to_dt(start)
    embed = discord.Embed(title="Ping o DILERACH!", color=0xFFFFFF)
    embed.add_field(name="Zapraszamy na", value=f"{voice_channel.mention}", inline=False)
    embed.add_field(name="Start", value=f"**{start}** · {_rel_pl(start_dt)}", inline=False)
    LOGO = os.getenv("LOGO_URL", "")
    if LOGO: embed.set_thumbnail(url=LOGO)
    embed.set_footer(text="Zapisani: 0")
    await interaction.response.send_message(content="@everyone", embed=embed, view=view)

def _check_env():
    if not TOKEN:
        raise RuntimeError("Brak DISCORD_TOKEN w .env")



# ===== Render/UptimeRobot web server =====
def _create_web_app():
    app = web.Application()
    async def root(_):
        return web.Response(text="OK", content_type="text/plain")
    async def health(_):
        return web.Response(text="HEALTHY", content_type="text/plain")
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    return app


if __name__ == "__main__":
    import os, asyncio, signal

    def _check_env():
        token = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
        if not token:
            raise RuntimeError("Brak DISCORD_TOKEN w .env")
        return token

    async def _main():
        token = _check_env()
        # Start web server for Render/UptimeRobot
        app = _create_web_app()
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.getenv("PORT", "10000"))
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

        # Start discord bot
        # Use start() instead of run() to keep control in this coroutine
        bot_task = asyncio.create_task(bot.start(token))

        # Handle shutdown signals (Render sends SIGTERM on deploys)
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass  # windows

        await stop.wait()
        await bot.close()
        await runner.cleanup()

    asyncio.run(_main())
