"""
╔══════════════════════════════════════════════════════════════╗
║         🎮 Roblox FFlag Tracker — Discord Bot                ║
║  Busca, limpia y explica las Fast Flags de Roblox            ║
║  Repositorio: MaximumADHD/Roblox-FFlag-Tracker              ║
╚══════════════════════════════════════════════════════════════╝
"""

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import asyncio
import json
import io
import os
from datetime import datetime, timedelta
from typing import Optional

# ══════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════
DISCORD_TOKEN        = os.getenv("DISCORD_TOKEN", "")
GITHUB_TOKEN         = os.getenv("GITHUB_TOKEN", "")

GUILD_ID             = int(os.getenv("GUILD_ID", "0"))
ALLOWED_CHANNEL_ID   = int(os.getenv("ALLOWED_CHANNEL_ID", "0"))

REPO_OWNER   = "MaximumADHD"
REPO_NAME    = "Roblox-FFlag-Tracker"
REPO_BRANCH  = "main"
REPO_TREE    = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/git/trees/{REPO_BRANCH}?recursive=1"
RAW_BASE     = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{REPO_BRANCH}/"

CACHE_TTL          = timedelta(hours=6)
RESULTS_PER_PAGE   = 10
MAX_CONCURRENT     = 15   # Descargas simultáneas máximas

# Colores
C_BLUE    = 0x5865F2
C_GREEN   = 0x57F287
C_RED     = 0xED4245
C_YELLOW  = 0xFEE75C
C_CYAN    = 0x00B4D8

# ══════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ══════════════════════════════════════════════════════════════════
flags_db:      dict[str, str]     = {}
flags_sources: dict[str, str]     = {}
flags_updated: Optional[datetime] = None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ══════════════════════════════════════════════════════════════════
# VERIFICACIÓN DE CANAL
# ══════════════════════════════════════════════════════════════════
async def check_channel(interaction: discord.Interaction) -> bool:
    if ALLOWED_CHANNEL_ID and interaction.channel_id != ALLOWED_CHANNEL_ID:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="❌ Canal no permitido",
                description=f"Este bot solo funciona en <#{ALLOWED_CHANNEL_ID}>.",
                color=C_RED,
            ),
            ephemeral=True,
        )
        return False
    return True

# ══════════════════════════════════════════════════════════════════
# CARGA DE FLAGS — CONCURRENTE Y CON PROGRESO
# ══════════════════════════════════════════════════════════════════
def _headers() -> dict:
    h = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"token {GITHUB_TOKEN}"
    return h

async def _download_one(sess: aiohttp.ClientSession, item: dict, sem: asyncio.Semaphore) -> tuple[str, str, dict]:
    """Descarga un archivo JSON y devuelve (plataforma, path, data_dict)."""
    path     = item["path"]
    platform = os.path.splitext(os.path.basename(path))[0]
    url      = RAW_BASE + path

    async with sem:
        try:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 200:
                    text = await r.text()
                    data = json.loads(text)
                    if isinstance(data, dict):
                        return platform, path, data
        except Exception as e:
            print(f"[Flags] ⚠️ Error en {path}: {e}")
    return platform, path, {}

async def refresh_flags(force: bool = False) -> bool:
    global flags_db, flags_sources, flags_updated

    if not force and flags_updated and (datetime.now() - flags_updated) < CACHE_TTL:
        return True

    print("[Flags] Actualizando caché desde GitHub...")
    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            # 1) Obtener árbol
            async with sess.get(REPO_TREE, headers=_headers()) as r:
                if r.status == 403:
                    print("[Flags] ⚠️ Rate limit de GitHub alcanzado.")
                    print("[Flags]    Verifica que tu GITHUB_TOKEN esté activo y no expirado.")
                    return False
                if r.status == 401:
                    print("[Flags] ❌ Token de GitHub inválido (401 Unauthorized).")
                    print("[Flags]    Revisa que el token sea 'classic' con scope 'public_repo'.")
                    return False
                if r.status != 200:
                    print(f"[Flags] Error en GitHub API: {r.status}")
                    return False
                tree = await r.json()

            json_files = [
                f for f in tree.get("tree", [])
                if f["type"] == "blob" and f["path"].endswith(".json")
            ]
            print(f"[Flags] 📁 {len(json_files):,} archivos JSON encontrados en el árbol")

            # 2) Descargar todo en paralelo controlado
            sem = asyncio.Semaphore(MAX_CONCURRENT)
            tasks = [_download_one(sess, item, sem) for item in json_files]
            results = await asyncio.gather(*tasks)

            new_db: dict[str, str] = {}
            new_src: dict[str, str] = {}

            for i, (platform, path, data) in enumerate(results, 1):
                for k, v in data.items():
                    if k not in new_db:
                        new_db[k]  = str(v)
                        new_src[k] = platform
                if i % 500 == 0:
                    print(f"[Flags] ⏳ Procesados {i}/{len(json_files)} archivos...")

        flags_db      = new_db
        flags_sources = new_src
        flags_updated = datetime.now()
        print(f"[Flags] ✅ {len(new_db):,} flags únicas de {len(json_files):,} archivos")
        return True

    except Exception as e:
        print(f"[Flags] Error crítico: {e}")
        return False

# ══════════════════════════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════════════════════════
def search_flags(query: str) -> dict[str, str]:
    q = query.lower()
    exact   = {k: v for k, v in flags_db.items() if k.lower() == q}
    starts  = {k: v for k, v in flags_db.items() if k.lower().startswith(q) and k.lower() != q}
    contain = {k: v for k, v in flags_db.items() if q in k.lower() and not k.lower().startswith(q)}
    return {**exact, **starts, **contain}

def validate_flags(user_flags: dict) -> tuple[dict, list]:
    valid   = {k: v for k, v in user_flags.items() if k in flags_db}
    invalid = [k for k in user_flags if k not in flags_db]
    return valid, invalid

def parse_user_file(text: str) -> dict:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        if isinstance(data, list):
            return {str(k): "true" for k in data if isinstance(k, str)}
    except json.JSONDecodeError:
        pass

    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith(("#", "//")):
            continue
        for sep in ("=", ":"):
            if sep in line:
                left, right = line.split(sep, 1)
                k = left.strip().strip("\"'")
                v = right.strip().strip("\"'")
                if k:
                    result[k] = v
                break
    return result

# ══════════════════════════════════════════════════════════════════
# VISTA — PAGINACIÓN
# ══════════════════════════════════════════════════════════════════
class FlagSearchView(discord.ui.View):
    def __init__(self, results: dict[str, str], query: str):
        super().__init__(timeout=180)
        self.items = list(results.items())
        self.query = query
        self.page  = 0
        self.pages = max(1, (len(self.items) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
        self._sync()

    def _sync(self):
        self.btn_prev.disabled = (self.page == 0)
        self.btn_next.disabled = (self.page >= self.pages - 1)
        self.btn_page.label    = f"📄 {self.page + 1}/{self.pages}"

    def build_embed(self) -> discord.Embed:
        start = self.page * RESULTS_PER_PAGE
        chunk = self.items[start : start + RESULTS_PER_PAGE]

        embed = discord.Embed(
            title=f"🔍 Búsqueda: `{self.query}`",
            color=C_BLUE,
            description=f"**{len(self.items):,}** flags encontradas",
        )

        lines = []
        for k, v in chunk:
            src = flags_sources.get(k, "?")
            dv  = v if len(v) <= 35 else v[:32] + "..."
            lines.append(f"**`{k}`**\nValor: `{dv}` · `{src}`")

        field_val = "\n\n".join(lines) or "Sin resultados"
        if len(field_val) > 1020:
            field_val = field_val[:1017] + "…"

        embed.add_field(
            name=f"Flags — Página {self.page + 1} de {self.pages}",
            value=field_val,
            inline=False,
        )
        embed.set_footer(text="Fuente: MaximumADHD/Roblox-FFlag-Tracker  •  Expira en 3 min")
        return embed

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def btn_prev(self, interaction: discord.Interaction, _btn):
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="📄 1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def btn_page(self, interaction: discord.Interaction, _btn):
        await interaction.response.defer()

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def btn_next(self, interaction: discord.Interaction, _btn):
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

# ══════════════════════════════════════════════════════════════════
# EVENTOS
# ══════════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    print(f"\n[Bot] ✅ Conectado como {bot.user}  (ID: {bot.user.id})")
    print(f"[Bot] Servidor objetivo: {GUILD_ID}")
    print(f"[Bot] Canal permitido: {ALLOWED_CHANNEL_ID}")
    print("[Bot] Cargando flags...")
    await refresh_flags()

    auto_refresh.start()
    print("[Bot] ✅ Auto-refresh iniciado")

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            synced = await bot.tree.sync(guild=guild)
            print(f"[Bot] ✅ {len(synced)} comandos sincronizados en GUILD {GUILD_ID}\n")
        else:
            synced = await bot.tree.sync()
            print(f"[Bot] ✅ {len(synced)} comandos sincronizados globalmente\n")
    except Exception as e:
        print(f"[Bot] ❌ Error al sincronizar comandos: {e}\n")

@tasks.loop(hours=6)
async def auto_refresh():
    print("[Auto] Refrescando caché de flags...")
    await refresh_flags(force=True)

@auto_refresh.before_loop
async def _before():
    await bot.wait_until_ready()

# ══════════════════════════════════════════════════════════════════
# COMANDOS
# ══════════════════════════════════════════════════════════════════
@bot.tree.command(name="buscar", description="🔍 Busca flags de Roblox por nombre", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.describe(nombre="Parte del nombre de la flag (ej: FPS, Render, Shadow...)")
async def cmd_buscar(interaction: discord.Interaction, nombre: str):
    if not await check_channel(interaction):
        return
    await interaction.response.defer(thinking=True)

    if not flags_db:
        if not await refresh_flags():
            await interaction.followup.send(embed=discord.Embed(title="❌ Sin conexión", description="No se pudo conectar al repositorio.", color=C_RED))
            return

    results = search_flags(nombre)
    if not results:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Sin resultados",
                description=f"No hay flags que contengan `{nombre}`.\n\n💡 **Sugerencias:** `FPS`, `Render`, `Shadow`, `Network`, `Graphics`, `Physics`",
                color=C_RED,
            )
        )
        return

    view = FlagSearchView(results, nombre)
    await interaction.followup.send(embed=view.build_embed(), view=view)

@bot.tree.command(name="limpiar", description="🧹 Sube tu archivo de flags y elimina las inválidas", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.describe(archivo="Tu archivo .json o .txt", formato="Formato de salida")
@app_commands.choices(formato=[
    app_commands.Choice(name="JSON (.json)", value="json"),
    app_commands.Choice(name="Texto (.txt)", value="txt"),
    app_commands.Choice(name="Ambos", value="both"),
])
async def cmd_limpiar(interaction: discord.Interaction, archivo: discord.Attachment, formato: str = "json"):
    if not await check_channel(interaction):
        return
    await interaction.response.defer(thinking=True)

    if not (archivo.filename.endswith(".json") or archivo.filename.endswith(".txt")):
        await interaction.followup.send(embed=discord.Embed(title="❌ Formato no soportado", description="Solo `.json` o `.txt`.", color=C_RED))
        return
    if archivo.size > 2_097_152:
        await interaction.followup.send(embed=discord.Embed(title="❌ Archivo muy grande", description="Máximo **2 MB**.", color=C_RED))
        return

    try:
        raw = await archivo.read()
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    except Exception as e:
        await interaction.followup.send(f"❌ Error al leer: `{e}`")
        return

    user_flags = parse_user_file(text)
    if not user_flags:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Archivo vacío",
                description="Formatos:\n```json\n{\"Flag\": \"valor\"}\n```\n```\nFlag = valor\n```",
                color=C_RED,
            )
        )
        return

    if not flags_db:
        await refresh_flags()

    valid, invalid = validate_flags(user_flags)
    total = len(user_flags)
    pct = len(valid) / total * 100 if total else 0

    out_files = []
    if valid:
        if formato in ("json", "both"):
            out_files.append(discord.File(io.BytesIO(json.dumps(valid, indent=2).encode()), "flags_validas.json"))
        if formato in ("txt", "both"):
            out_files.append(discord.File(io.BytesIO("\n".join(f"{k} = {v}" for k, v in valid.items()).encode()), "flags_validas.txt"))

    if invalid:
        header = f"# Flags eliminadas — no existen en el repositorio\n# Total: {len(invalid)}\n\n"
        out_files.append(discord.File(io.BytesIO((header + "\n".join(invalid)).encode()), "flags_invalidas.txt"))

    color = C_GREEN if valid else C_RED
    embed = discord.Embed(title="🧹 Limpieza completada", color=color)
    embed.add_field(name="📥 Total", value=f"**{total}**", inline=True)
    embed.add_field(name="✅ Válidas", value=f"**{len(valid)}** ({pct:.1f}%)", inline=True)
    embed.add_field(name="🗑️ Removidas", value=f"**{len(invalid)}**", inline=True)

    if not valid:
        embed.description = "⚠️ Ninguna flag existe en el repositorio oficial."
    elif not invalid:
        embed.description = "✨ ¡Perfecto! Todas son válidas."
    else:
        embed.description = f"Se eliminaron **{len(invalid)}** flags no oficiales."

    if invalid:
        preview = "\n".join(f"• `{f}`" for f in invalid[:12])
        if len(invalid) > 12:
            preview += f"\n• … y **{len(invalid) - 12}** más"
        embed.add_field(name="🚫 Eliminadas:", value=preview, inline=False)

    embed.set_footer(text=f"BD: {len(flags_db):,} flags")
    await interaction.followup.send(embed=embed, files=out_files)

@bot.tree.command(name="estado", description="📊 Estado del bot y la BD", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
async def cmd_estado(interaction: discord.Interaction):
    if not await check_channel(interaction):
        return

    embed = discord.Embed(title="📊 Estado del Bot", color=C_BLUE)
    if flags_db:
        platforms = sorted(set(flags_sources.values()))
        embed.add_field(name="✅ Flags", value=f"**{len(flags_db):,}**", inline=True)
        embed.add_field(name="📁 Plataformas", value=f"**{len(platforms)}**", inline=True)
        if flags_updated:
            age = datetime.now() - flags_updated
            total_mins = int(age.total_seconds() // 60)
            h, m = divmod(total_mins, 60)
            embed.add_field(name="🕐 Caché", value=f"Hace **{h}h {m}m**", inline=True)
        plat_lines = "\n".join(f"• `{p}`" for p in platforms[:25])
        if len(platforms) > 25:
            plat_lines += f"\n• … y {len(platforms)-25} más"
        embed.add_field(name="Plataformas:", value=plat_lines, inline=False)
    else:
        embed.add_field(name="⚠️", value="BD no cargada. Usa `/actualizar`.", inline=False)

    embed.set_footer(text="github.com/MaximumADHD/Roblox-FFlag-Tracker")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="actualizar", description="🔄 Fuerza actualización del caché", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
async def cmd_actualizar(interaction: discord.Interaction):
    if not await check_channel(interaction):
        return
    await interaction.response.defer(thinking=True)

    prev = len(flags_db)
    ok = await refresh_flags(force=True)

    if ok:
        diff = len(flags_db) - prev
        sign = "+" if diff > 0 else ""
        embed = discord.Embed(
            title="✅ Caché actualizado",
            description=f"**{len(flags_db):,}** flags cargadas\nCambio: `{sign}{diff}`",
            color=C_GREEN,
        )
    else:
        embed = discord.Embed(
            title="❌ Error",
            description="No se pudo conectar.\n• Verifica `GITHUB_TOKEN`\n• Rate limit de GitHub",
            color=C_RED,
        )
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════════════════════════════
# INICIO
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN no está configurado.")

    if not GUILD_ID:
        print("⚠️ GUILD_ID no configurado — sincronización global (hasta 1h).")
    if not ALLOWED_CHANNEL_ID:
        print("⚠️ ALLOWED_CHANNEL_ID no configurado — funciona en cualquier canal.")
    if not GITHUB_TOKEN:
        print("⚠️ GITHUB_TOKEN no configurado — límite 60 req/h.")

    bot.run(DISCORD_TOKEN, log_handler=None)
