#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║         🎮 Roblox FFlag Tracker — Discord Bot                ║
║  Busca, limpia y explica las Fast Flags de Roblox            ║
║  Repositorio: MaximumADHD/Roblox-FFlag-Tracker              ║
╚══════════════════════════════════════════════════════════════╝
"""

# ── Cargar .env automáticamente si existe ──────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import json
import io
import os
import re
from anthropic import Anthropic
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional

# ══════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════
DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN", "")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")   # opcional — sube el rate limit de 60 → 5000 req/h

REPO_OWNER   = "MaximumADHD"
REPO_NAME    = "Roblox-FFlag-Tracker"
REPO_BRANCH  = "main"
REPO_TREE    = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/git/trees/{REPO_BRANCH}?recursive=1"
RAW_BASE     = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{REPO_BRANCH}/"

CACHE_TTL          = timedelta(hours=6)   # Refresca cada 6 horas
MAX_CONV_TURNS     = 8                    # Turnos de conversación guardados por usuario
RESULTS_PER_PAGE   = 10                   # Flags por página en /buscar

# Colores para embeds
C_BLUE    = 0x5865F2
C_GREEN   = 0x57F287
C_RED     = 0xED4245
C_YELLOW  = 0xFEE75C
C_CYAN    = 0x00B4D8

# ══════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ══════════════════════════════════════════════════════════════════
flags_db:      dict[str, str]     = {}   # flag → valor
flags_sources: dict[str, str]     = {}   # flag → plataforma
flags_updated: Optional[datetime] = None

conversations: dict[str, list] = defaultdict(list)   # uid → historial

ai = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ══════════════════════════════════════════════════════════════════
# CARGA DE FLAGS DESDE GITHUB
# ══════════════════════════════════════════════════════════════════
def _headers() -> dict:
    h = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"token {GITHUB_TOKEN}"
    return h

async def refresh_flags(force: bool = False) -> bool:
    global flags_db, flags_sources, flags_updated

    if not force and flags_updated and (datetime.now() - flags_updated) < CACHE_TTL:
        return True

    print("[Flags] Actualizando caché desde GitHub...")
    try:
        async with aiohttp.ClientSession() as sess:
            # 1) Obtener árbol de archivos
            async with sess.get(REPO_TREE, headers=_headers()) as r:
                if r.status == 403:
                    print("[Flags] Rate limit de GitHub alcanzado — configura GITHUB_TOKEN")
                    return False
                if r.status != 200:
                    print(f"[Flags] Error en GitHub API: {r.status}")
                    return False
                tree = await r.json()

            json_files = [
                f for f in tree.get("tree", [])
                if f["type"] == "blob" and f["path"].endswith(".json")
            ]
            print(f"[Flags] {len(json_files)} archivos JSON encontrados")

            new_db: dict[str, str] = {}
            new_src: dict[str, str] = {}

            # 2) Descargar cada archivo
            for item in json_files:
                path     = item["path"]
                platform = os.path.splitext(os.path.basename(path))[0]
                url      = RAW_BASE + path
                try:
                    async with sess.get(url) as r:
                        if r.status == 200:
                            data = json.loads(await r.text())
                            if isinstance(data, dict):
                                for k, v in data.items():
                                    if k not in new_db:   # primer archivo gana
                                        new_db[k]  = str(v)
                                        new_src[k] = platform
                except Exception as e:
                    print(f"[Flags] Error en {path}: {e}")

        flags_db      = new_db
        flags_sources = new_src
        flags_updated = datetime.now()
        print(f"[Flags] ✅ {len(new_db):,} flags únicas de {len(json_files)} archivos")
        return True

    except Exception as e:
        print(f"[Flags] Error crítico: {e}")
        return False

# ══════════════════════════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════════════════════════
def search_flags(query: str) -> dict[str, str]:
    """Busca flags por nombre. Devuelve ordenado: exacto → empieza con → contiene."""
    q = query.lower()
    exact   = {k: v for k, v in flags_db.items() if k.lower() == q}
    starts  = {k: v for k, v in flags_db.items() if k.lower().startswith(q) and k.lower() != q}
    contain = {k: v for k, v in flags_db.items() if q in k.lower() and not k.lower().startswith(q)}
    return {**exact, **starts, **contain}


def validate_flags(user_flags: dict) -> tuple[dict, list]:
    """Separa flags válidas de inválidas respecto al repositorio."""
    valid   = {k: v for k, v in user_flags.items() if k in flags_db}
    invalid = [k for k in user_flags if k not in flags_db]
    return valid, invalid


def parse_user_file(text: str) -> dict:
    """
    Parsea un archivo de flags. Soporta:
    - JSON: {"FlagName": "valor"}
    - Texto: FlagName = valor  /  FlagName: valor
    """
    # Intento JSON
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        if isinstance(data, list):
            return {str(k): "true" for k in data if isinstance(k, str)}
    except json.JSONDecodeError:
        pass

    # Línea por línea
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


def find_mentioned_flags(text: str) -> dict[str, dict]:
    """Detecta flags mencionadas en un texto, exactas o case-insensitive."""
    tokens   = re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", text)
    lower_map = {k.lower(): k for k in flags_db}
    found: dict[str, dict] = {}
    for t in tokens:
        key = None
        if t in flags_db:
            key = t
        elif t.lower() in lower_map:
            key = lower_map[t.lower()]
        if key and key not in found:
            found[key] = {
                "value": flags_db[key],
                "platform": flags_sources.get(key, "?"),
            }
    return found

# ══════════════════════════════════════════════════════════════════
# VISTA — PAGINACIÓN DE RESULTADOS
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
        # Truncar si supera límite de Discord (1024 chars por campo)
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
# EVENTOS DEL BOT
# ══════════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    print(f"\n[Bot] ✅ Conectado como {bot.user}  (ID: {bot.user.id})")
    print("[Bot] Cargando flags...")
    await refresh_flags()
    try:
        synced = await bot.tree.sync()
        print(f"[Bot] ✅ {len(synced)} comandos sincronizados\n")
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
# COMANDO 1 — /buscar
# ══════════════════════════════════════════════════════════════════
@bot.tree.command(name="buscar", description="🔍 Busca flags de Roblox por nombre o palabra clave")
@app_commands.describe(nombre="Parte del nombre de la flag (ej: FPS, Render, Shadow, Network...)")
async def cmd_buscar(interaction: discord.Interaction, nombre: str):
    await interaction.response.defer(thinking=True)

    if not flags_db:
        if not await refresh_flags():
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Sin conexión", description="No se pudo conectar al repositorio.", color=C_RED)
            )
            return

    results = search_flags(nombre)

    if not results:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Sin resultados",
                description=(
                    f"No hay flags que contengan `{nombre}`.\n\n"
                    "💡 **Sugerencias:** `FPS`, `Render`, `Shadow`, `Network`, `Graphics`, `Physics`"
                ),
                color=C_RED,
            )
        )
        return

    view  = FlagSearchView(results, nombre)
    await interaction.followup.send(embed=view.build_embed(), view=view)

# ══════════════════════════════════════════════════════════════════
# COMANDO 2 — /limpiar
# ══════════════════════════════════════════════════════════════════
@bot.tree.command(
    name="limpiar",
    description="🧹 Sube tu archivo de flags — elimina las inválidas y descarga el JSON limpio"
)
@app_commands.describe(
    archivo="Tu archivo .json o .txt con flags de Roblox",
    formato="Formato del archivo de salida (JSON por defecto)",
)
@app_commands.choices(formato=[
    app_commands.Choice(name="JSON   (.json)",    value="json"),
    app_commands.Choice(name="Texto  (.txt)",     value="txt"),
    app_commands.Choice(name="Ambos  (.json + .txt)", value="both"),
])
async def cmd_limpiar(
    interaction: discord.Interaction,
    archivo: discord.Attachment,
    formato: str = "json",
):
    await interaction.response.defer(thinking=True)

    # ── Validaciones básicas ────────────────────────────────────
    if not (archivo.filename.endswith(".json") or archivo.filename.endswith(".txt")):
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Formato no soportado",
                description="Solo acepto archivos **`.json`** o **`.txt`**.",
                color=C_RED,
            )
        )
        return

    if archivo.size > 2_097_152:  # 2 MB
        await interaction.followup.send(
            embed=discord.Embed(title="❌ Archivo muy grande", description="Máximo **2 MB**.", color=C_RED)
        )
        return

    # ── Leer contenido ─────────────────────────────────────────
    try:
        raw  = await archivo.read()
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    except Exception as e:
        await interaction.followup.send(f"❌ No se pudo leer el archivo: `{e}`")
        return

    # ── Parsear ────────────────────────────────────────────────
    user_flags = parse_user_file(text)

    if not user_flags:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Archivo vacío o sin formato reconocido",
                description=(
                    "**Formatos soportados:**\n"
                    "```json\n{\"FlagName\": \"valor\"}\n```"
                    "```\nFlagName = valor\n```"
                ),
                color=C_RED,
            )
        )
        return

    # ── Cargar BD si es necesario ──────────────────────────────
    if not flags_db:
        await refresh_flags()

    # ── Validar ────────────────────────────────────────────────
    valid, invalid = validate_flags(user_flags)
    total = len(user_flags)
    pct   = len(valid) / total * 100 if total else 0

    # ── Generar archivos de salida ─────────────────────────────
    out_files: list[discord.File] = []

    if valid:
        if formato in ("json", "both"):
            out_files.append(discord.File(
                io.BytesIO(json.dumps(valid, indent=2).encode()),
                filename="flags_validas.json",
            ))
        if formato in ("txt", "both"):
            txt_content = "\n".join(f"{k} = {v}" for k, v in valid.items())
            out_files.append(discord.File(
                io.BytesIO(txt_content.encode()),
                filename="flags_validas.txt",
            ))

    if invalid:
        header = (
            "# Flags eliminadas — no existen en el repositorio MaximumADHD/Roblox-FFlag-Tracker\n"
            f"# Total: {len(invalid)}\n\n"
        )
        inv_content = header + "\n".join(invalid)
        out_files.append(discord.File(
            io.BytesIO(inv_content.encode()),
            filename="flags_invalidas.txt",
        ))

    # ── Embed resumen ──────────────────────────────────────────
    color = C_GREEN if valid else C_RED
    embed = discord.Embed(title="🧹 Limpieza completada", color=color)

    embed.add_field(name="📥 Total enviadas",  value=f"**{total}**",                    inline=True)
    embed.add_field(name="✅ Flags válidas",   value=f"**{len(valid)}** ({pct:.1f}%)",  inline=True)
    embed.add_field(name="🗑️ Flags removidas", value=f"**{len(invalid)}**",             inline=True)

    if not valid:
        embed.description = "⚠️ **Ninguna flag** de tu archivo existe en el repositorio oficial de Roblox."
    elif not invalid:
        embed.description = "✨ **¡Perfecto!** Todas tus flags son válidas."
    else:
        embed.description = (
            f"Se eliminaron **{len(invalid)}** flags no encontradas en el repositorio oficial.\n"
            "Descarga los archivos adjuntos 👇"
        )

    if invalid:
        preview = "\n".join(f"• `{f}`" for f in invalid[:12])
        if len(invalid) > 12:
            preview += f"\n• … y **{len(invalid) - 12}** más (ver `flags_invalidas.txt`)"
        embed.add_field(name="🚫 Flags eliminadas:", value=preview, inline=False)

    embed.set_footer(text=f"BD: {len(flags_db):,} flags  •  MaximumADHD/Roblox-FFlag-Tracker")
    await interaction.followup.send(embed=embed, files=out_files)

# ══════════════════════════════════════════════════════════════════
# COMANDO 3 — /preguntar
# ══════════════════════════════════════════════════════════════════
_SYSTEM_PROMPT = """Eres un experto técnico especializado en las Fast Flags (FFlags) de Roblox.

== TIPOS DE FLAGS ==
• FFlag / DFFlag   → Booleano (true / false)
• FInt  / DFInt    → Entero
• FString          → Texto
• FLog  / DFLog    → Nivel de logging
• F = estático  |  D (Dynamic) = puede cambiar en runtime sin reiniciar cliente

== FORMATO DE RESPUESTA PARA CADA FLAG ==
Cuando el usuario pregunte por una flag específica, usa SIEMPRE esta estructura:

## 🏷️ [NombreDeLaFlag]
**¿Qué hace?** — Descripción técnica precisa del efecto en el cliente/juego
**Tipo:** [Booleano / Entero / String / Log]
**Valor default (repositorio):** `[valor actual en el repo oficial]`
**Valor máximo recomendado:** `[número / true / false — o "N/A" si no aplica]`
**Categoría:** [Gráficos 🎨 / Rendimiento ⚡ / Red 🌐 / Físicas 🏗️ / Audio 🔊 / UI 🖥️ / Debug 🐛]
**Impacto:** [Bajo / Medio / Alto]
**⚠️ Riesgos:** [Si puede causar baneos, crashes, bugs o inestabilidad — o "Ninguno conocido"]

== REGLAS ==
- Si preguntan por múltiples flags, responde CADA UNA por separado con la estructura anterior.
- Si NO conoces con certeza una flag, dilo: "No tengo información fiable sobre esta flag."
- NUNCA inventes valores, comportamientos o efectos que no conoces.
- Si el repositorio te da el valor default, úsalo como referencia exacta.
- Para preguntas generales sobre FFlags (qué son, cómo funcionan, etc.), responde con precisión técnica.
- Siempre menciona si una flag es peligrosa de modificar.

Responde SIEMPRE en español. Usa markdown. Sé preciso y técnico pero entendible."""


@bot.tree.command(
    name="preguntar",
    description="💬 Pregunta a la IA qué hacen las flags, valores default, máximos y efectos"
)
@app_commands.describe(
    pregunta="¿Qué quieres saber? Puedes escribir nombres de flags específicas",
    nueva="Inicia una nueva conversación (borra tu historial anterior)",
)
async def cmd_preguntar(
    interaction: discord.Interaction,
    pregunta: str,
    nueva: bool = False,
):
    await interaction.response.defer(thinking=True)

    if not ai:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ IA no configurada",
                description=(
                    "El comando `/preguntar` necesita `ANTHROPIC_API_KEY` en el `.env`.\n"
                    "Ver el `README.md` para configurarlo."
                ),
                color=C_RED,
            )
        )
        return

    if not flags_db:
        await refresh_flags()

    uid = str(interaction.user.id)
    if nueva:
        conversations[uid] = []

    # ── Detectar flags mencionadas y construir contexto ─────────
    mentioned = find_mentioned_flags(pregunta)

    if mentioned:
        ctx_lines = ["**Flags encontradas en el repositorio oficial:**"]
        for name, info in mentioned.items():
            ctx_lines.append(f"- `{name}` = `{info['value']}` (plataforma: `{info['platform']}`)")
        context_block = "\n".join(ctx_lines)
        user_content  = f"{context_block}\n\n**Pregunta del usuario:** {pregunta}"
    else:
        user_content = pregunta

    # ── Construir historial + nuevo mensaje ─────────────────────
    history  = conversations[uid]
    messages = history + [{"role": "user", "content": user_content}]

    try:
        resp   = ai.messages.create(
            model      = "claude-sonnet-4-20250514",
            max_tokens = 1500,
            system     = _SYSTEM_PROMPT,
            messages   = messages,
        )
        answer = resp.content[0].text

        # Actualizar historial (guardamos la pregunta original, sin contexto extra)
        conversations[uid].append({"role": "user",      "content": pregunta})
        conversations[uid].append({"role": "assistant", "content": answer})

        # Limitar a MAX_CONV_TURNS intercambios
        max_msgs = MAX_CONV_TURNS * 2
        if len(conversations[uid]) > max_msgs:
            conversations[uid] = conversations[uid][-max_msgs:]

        # ── Embed de respuesta ──────────────────────────────────
        embed = discord.Embed(title="💬 Respuesta sobre FFlags", color=C_CYAN)
        embed.set_author(
            name=f"{interaction.user.display_name} preguntó:",
            icon_url=interaction.user.display_avatar.url,
        )

        # Discord permite hasta ~4000 chars en description
        if len(answer) <= 4000:
            embed.description = answer
        else:
            embed.description = answer[:3990] + "\n\n*…[respuesta cortada por límite de Discord]*"

        # Mostrar flags que se analizaron
        if mentioned:
            flags_preview = "\n".join(
                f"`{k}` = `{v['value']}` · `{v['platform']}`"
                for k, v in list(mentioned.items())[:5]
            )
            embed.add_field(name="📌 Flags en el repositorio:", value=flags_preview, inline=False)

        turn = len(conversations[uid]) // 2
        embed.set_footer(
            text=f"Turno {turn}/{MAX_CONV_TURNS} · Escribe /preguntar nueva:True para reiniciar · {len(flags_db):,} flags en BD"
        )

        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Error de IA",
                description=f"```{str(e)[:800]}```",
                color=C_RED,
            )
        )

# ══════════════════════════════════════════════════════════════════
# COMANDO /estado
# ══════════════════════════════════════════════════════════════════
@bot.tree.command(name="estado", description="📊 Estado del bot y la base de datos de flags")
async def cmd_estado(interaction: discord.Interaction):
    embed = discord.Embed(title="📊 Estado del Bot", color=C_BLUE)

    if flags_db:
        platforms = sorted(set(flags_sources.values()))
        embed.add_field(name="✅ Flags cargadas",  value=f"**{len(flags_db):,}**", inline=True)
        embed.add_field(name="📁 Plataformas",     value=f"**{len(platforms)}**",  inline=True)

        if flags_updated:
            age    = datetime.now() - flags_updated
            total_mins = int(age.total_seconds() // 60)
            h, m   = divmod(total_mins, 60)
            embed.add_field(name="🕐 Caché",  value=f"Hace **{h}h {m}m**", inline=True)

        plat_lines = "\n".join(f"• `{p}`" for p in platforms[:25])
        if len(platforms) > 25:
            plat_lines += f"\n• … y {len(platforms)-25} más"
        embed.add_field(name="Plataformas indexadas:", value=plat_lines, inline=False)
    else:
        embed.add_field(name="⚠️ Estado", value="Base de datos no cargada aún. Usa `/actualizar`.", inline=False)

    embed.set_footer(text="github.com/MaximumADHD/Roblox-FFlag-Tracker")
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════
# COMANDO /actualizar
# ══════════════════════════════════════════════════════════════════
@bot.tree.command(name="actualizar", description="🔄 Fuerza la actualización del caché de flags desde GitHub")
async def cmd_actualizar(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    prev = len(flags_db)
    ok   = await refresh_flags(force=True)

    if ok:
        diff = len(flags_db) - prev
        sign = "+" if diff > 0 else ""
        embed = discord.Embed(
            title="✅ Caché actualizado",
            description=(
                f"**{len(flags_db):,}** flags cargadas\n"
                f"Cambio respecto al caché anterior: `{sign}{diff}`"
            ),
            color=C_GREEN,
        )
    else:
        embed = discord.Embed(
            title="❌ Error al actualizar",
            description=(
                "No se pudo conectar al repositorio.\n"
                "• Verifica tu `GITHUB_TOKEN` si tienes uno configurado.\n"
                "• GitHub podría tener un rate limit activo."
            ),
            color=C_RED,
        )

    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════════════════════════════
# INICIO
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN no está configurado en el .env o variables de entorno.")

    if not ANTHROPIC_KEY:
        print("⚠️  ANTHROPIC_API_KEY no configurado — el comando /preguntar estará deshabilitado.")

    auto_refresh.start()
    bot.run(DISCORD_TOKEN, log_handler=None)
