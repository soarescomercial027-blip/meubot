"""INFINITY CRAFT — bot do Discord com painel web de controle (arquivo único).

Variáveis de ambiente:
  DISCORD_TOKEN     (obrigatória) token do bot
  PANEL_PASSWORD    (obrigatória) senha do painel
  FLASK_SECRET_KEY  (recomendada) mantém o login ativo entre reinicializações
  MINECRAFT_IP      (opcional)    endereço mostrado pelo comando !ip
  PANEL_PORT        (opcional)    porta do painel, padrão 5000
  PANEL_HTTPS       (opcional)    use 1 se o painel estiver atrás de HTTPS

Permissões úteis do bot no servidor: Ver canais, Enviar mensagens, Inserir links,
Anexar arquivos, Adicionar reações, Gerenciar mensagens, Gerenciar cargos, Gerenciar canais,
Expulsar, Banir e Silenciar membros.
"""
import asyncio
import copy
import io
import json
import logging
import os
import random
import re
import secrets
import struct
import threading
import time
import unicodedata
from collections import defaultdict, deque
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import discord
from discord.ext import commands
from flask import (
    Flask, abort, flash, redirect, render_template_string, request,
    send_from_directory, session, url_for
)

# =========================================================
# CONFIGURAÇÃO
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config_bot.json"
IMAGENS_DIR = BASE_DIR / "imagens"
IMAGENS_DIR.mkdir(exist_ok=True)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "").strip()
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "").strip()
MINECRAFT_IP = os.getenv("MINECRAFT_IP", "IP_DO_SEU_SERVIDOR").strip()

try:
    PANEL_PORT = int(os.getenv("PANEL_PORT", "5000"))
except ValueError:
    PANEL_PORT = 5000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("infinity-craft")

if not DISCORD_TOKEN:
    raise SystemExit("ERRO: configure a variável DISCORD_TOKEN com o token do bot.")
if not PANEL_PASSWORD:
    raise SystemExit("ERRO: configure PANEL_PASSWORD com uma senha forte para o painel.")
if not FLASK_SECRET_KEY:
    FLASK_SECRET_KEY = secrets.token_hex(32)
    log.warning("FLASK_SECRET_KEY não configurada: o login do painel expira ao reiniciar.")

COR_ROXO = discord.Color.from_rgb(155, 89, 182)  # cor de todos os embeds

DEFAULT_CONFIG = {
    # Boas-vindas
    "boas_vindas_ativo": False,
    "boas_vindas_embed": True,
    "boas_vindas_titulo": "🟣 - SEJA MUITO BEM-VINDO(A)!",
    "boas_vindas_msg": "Bem-vindo(a) ao INFINITY CRAFT, {nome}!",
    "boas_vindas_foto_perfil": True,
    "boas_vindas_imagem_arquivo": "",
    "boas_vindas_imagem_url": "",
    "canal_boas_vindas_id": "",
    "canal_boas_vindas": "chat-geral",  # usado só se o ID acima não estiver definido
    # Despedida
    "saida_ativa": False,
    "saida_msg": "**{nome}** saiu do servidor. Até logo! 👋",
    "saida_canal_id": "",
    # Cargo automático
    "cargo_auto_ativo": False,
    "cargo_auto_id": "",
    # Logs
    "logs_ativo": False,
    "logs_canal_id": "",
    # Moderação
    "moderacao_ativa": True,
    "antilink_ativo": False,
    "antispam_ativo": False,
    "palavras_bloqueadas": ["palavrao1", "hack"],
    # Tickets
    "ticket_ativo": False,
    "ticket_categoria_id": "",
    "ticket_staff_id": "",
    "ticket_logs_canal_id": "",
    "ticket_titulo": "🎫 Central de atendimento",
    "ticket_descricao": (
        "Precisa de ajuda? Escolha o motivo no menu abaixo e abra um ticket.\n"
        "Nossa equipe vai atender você o quanto antes!"
    ),
    "ticket_categorias": ["🛠️ Suporte", "🚨 Denúncia", "❓ Dúvida", "💬 Outro"],
    "ticket_imagem_arquivo": "",
    "ticket_contador": 0,
    # Cargo por reação (painéis criados pelo painel web)
    "cargos_reacao": [],
    # Comandos
    "comandos_custom_ativos": True,
}
CHAVES_LIGA_DESLIGA = {
    "boas_vindas_ativo", "saida_ativa", "cargo_auto_ativo", "logs_ativo",
    "moderacao_ativa", "antilink_ativo", "antispam_ativo", "comandos_custom_ativos",
    "ticket_ativo",
}

config_lock = threading.RLock()


def salvar_config(dados):
    """Grava em arquivo temporário e troca, para nunca deixar o JSON pela metade."""
    temporario = CONFIG_FILE.with_suffix(".tmp")
    with temporario.open("w", encoding="utf-8") as arquivo:
        json.dump(dados, arquivo, ensure_ascii=False, indent=2)
    temporario.replace(CONFIG_FILE)


def carregar_config():
    dados = copy.deepcopy(DEFAULT_CONFIG)
    if not CONFIG_FILE.exists():
        salvar_config(dados)
        return dados
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as arquivo:
            existentes = json.load(arquivo)
    except (OSError, json.JSONDecodeError):
        log.exception("Não foi possível ler config_bot.json; usando a configuração padrão.")
        return dados
    if isinstance(existentes, dict):
        dados.update(existentes)
    if not isinstance(dados["palavras_bloqueadas"], list):
        dados["palavras_bloqueadas"] = list(DEFAULT_CONFIG["palavras_bloqueadas"])
    dados["palavras_bloqueadas"] = [str(p) for p in dados["palavras_bloqueadas"]]
    for chave in ("ticket_categorias", "cargos_reacao"):
        if not isinstance(dados[chave], list):
            dados[chave] = copy.deepcopy(DEFAULT_CONFIG[chave])
    return dados


config_painel = carregar_config()


def ler_config():
    with config_lock:
        return copy.deepcopy(config_painel)


def atualizar_config(**mudancas):
    with config_lock:
        config_painel.update(mudancas)
        salvar_config(config_painel)


def alternar_config(chave):
    with config_lock:
        config_painel[chave] = not bool(config_painel.get(chave))
        salvar_config(config_painel)


# =========================================================
# IMAGENS (upload feito pelo painel)
# =========================================================
LIMITE_IMAGEM = 8 * 1024 * 1024  # 8 MB


def detectar_tipo_imagem(dados):
    """Descobre o tipo pelo conteúdo do arquivo, não pelo nome."""
    if dados.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if dados[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if dados[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if dados[:4] == b"RIFF" and dados[8:12] == b"WEBP":
        return ".webp"
    return None


SLOTS_IMAGEM = {"boas_vindas", "ticket"}


def caminho_imagem(cfg, slot="boas_vindas"):
    nome = Path(str(cfg.get(f"{slot}_imagem_arquivo") or "")).name
    if not nome:
        return None
    caminho = IMAGENS_DIR / nome
    return caminho if caminho.is_file() else None


def apagar_imagem(slot):
    for antigo in IMAGENS_DIR.glob(f"{slot}.*"):
        try:
            antigo.unlink()
        except OSError:
            log.exception("Não foi possível apagar %s", antigo)


def guardar_imagem(slot, dados, extensao):
    apagar_imagem(slot)
    (IMAGENS_DIR / f"{slot}{extensao}").write_bytes(dados)


def url_valida(url):
    try:
        partes = urlparse(url)
    except ValueError:
        return False
    return partes.scheme in {"http", "https"} and bool(partes.netloc) and len(url) <= 500


# =========================================================
# BOT DO DISCORD
# =========================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    case_insensitive=True,
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
)

REGEX_LINK = re.compile(r"(https?://|www\.|discord\.gg/|discord(?:app)?\.com/invite/)\S+", re.IGNORECASE)
SPAM_LIMITE = 5      # mensagens...
SPAM_JANELA = 6      # ...em tantos segundos
SPAM_SILENCIO_MIN = 5
_historico_msgs = defaultdict(lambda: deque(maxlen=SPAM_LIMITE))


def normalizar(texto):
    """Minúsculas e sem acentos: 'AÇÃO' -> 'acao'."""
    texto = unicodedata.normalize("NFKD", texto.casefold())
    return "".join(c for c in texto if not unicodedata.combining(c))


@lru_cache(maxsize=8)
def _regex_bloqueio(palavras):
    termos = [re.escape(normalizar(p.strip())) for p in palavras if p.strip()]
    if not termos:
        return None
    # Só bloqueia a palavra inteira: "hack" não pega "hackathon".
    return re.compile(r"(?<!\w)(?:" + "|".join(termos) + r")(?!\w)")


def contem_palavra_bloqueada(conteudo):
    with config_lock:
        palavras = tuple(config_painel.get("palavras_bloqueadas", []))
    regex = _regex_bloqueio(palavras)
    return bool(regex and regex.search(normalizar(conteudo)))


def eh_moderador(autor):
    if not isinstance(autor, discord.Member):
        return False
    perms = autor.guild_permissions
    return perms.administrator or perms.manage_messages


def aplicar_variaveis(texto, membro):
    """Troca {usuario}, {nome}, {servidor} e {membros} pelos valores reais."""
    return (
        texto.replace("{usuario}", membro.mention)
        .replace("{nome}", membro.display_name)
        .replace("{servidor}", membro.guild.name)
        .replace("{membros}", str(membro.guild.member_count))
    )


def achar_canal(guild, canal_id):
    try:
        canal = guild.get_channel(int(canal_id or 0))
    except (TypeError, ValueError):
        return None
    return canal if isinstance(canal, discord.TextChannel) else None


def achar_canal_boas_vindas(guild, cfg):
    canal = achar_canal(guild, cfg.get("canal_boas_vindas_id"))
    if canal is not None:
        return canal
    return discord.utils.get(guild.text_channels, name=str(cfg.get("canal_boas_vindas", "")))


async def registrar_log(guild, titulo, descricao):
    cfg = ler_config()
    if not cfg["logs_ativo"]:
        return
    canal = achar_canal(guild, cfg["logs_canal_id"])
    if canal is None:
        return
    embed = discord.Embed(
        title=titulo, description=descricao[:4000], color=COR_ROXO,
        timestamp=discord.utils.utcnow(),
    )
    try:
        await canal.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as erro_http:
        log.warning("Não foi possível enviar o log: %s", erro_http)


# ---------- boas-vindas ----------
def montar_boas_vindas(membro, cfg):
    """Cartão roxo com foto de perfil do membro (canto) e a imagem escolhida (embaixo)."""
    embed = discord.Embed(
        title=aplicar_variaveis(cfg["boas_vindas_titulo"], membro)[:256] or None,
        description=aplicar_variaveis(cfg["boas_vindas_msg"], membro)[:4000],
        color=COR_ROXO,
    )
    if cfg["boas_vindas_foto_perfil"]:
        embed.set_thumbnail(url=membro.display_avatar.url)

    arquivo = None
    caminho = caminho_imagem(cfg)
    if caminho is not None:
        arquivo = discord.File(caminho, filename="boas_vindas" + caminho.suffix)
        embed.set_image(url="attachment://" + arquivo.filename)
    elif cfg["boas_vindas_imagem_url"]:
        embed.set_image(url=cfg["boas_vindas_imagem_url"])

    embed.set_footer(text=f"{membro.guild.name} • Membro nº {membro.guild.member_count}")
    return embed, arquivo


async def enviar_boas_vindas(canal, membro, cfg):
    mencoes = discord.AllowedMentions(users=[membro])
    if not cfg["boas_vindas_embed"]:
        texto = aplicar_variaveis(cfg["boas_vindas_msg"], membro)
        await canal.send(f"👋 {membro.mention} {texto}", allowed_mentions=mencoes)
        return
    embed, arquivo = montar_boas_vindas(membro, cfg)
    dados = {"content": membro.mention, "embed": embed, "allowed_mentions": mencoes}
    if arquivo is not None:
        dados["file"] = arquivo
    await canal.send(**dados)


# ---------- moderação automática ----------
async def apagar_com_aviso(message, motivo):
    try:
        await message.delete()
    except discord.NotFound:
        return True  # já foi apagada por outra pessoa
    except discord.Forbidden:
        log.warning("Sem permissão para apagar mensagens no canal %s.", message.channel.id)
        return False
    except discord.HTTPException:
        log.exception("Falha ao aplicar moderação.")
        return False

    try:
        await message.channel.send(
            f"⚠️ {message.author.mention}, sua mensagem foi removida: {motivo}.",
            delete_after=6,
        )
    except discord.HTTPException:
        pass
    await registrar_log(
        message.guild, "🛡️ Mensagem removida",
        f"**Autor:** {message.author.mention}\n**Canal:** {message.channel.mention}\n"
        f"**Motivo:** {motivo}\n\n{message.content[:500]}",
    )
    return True


def detectar_spam(message):
    fila = _historico_msgs[(message.guild.id, message.author.id)]
    agora = time.monotonic()
    fila.append(agora)
    if len(fila) == SPAM_LIMITE and agora - fila[0] <= SPAM_JANELA:
        fila.clear()
        return True
    return False


async def tratar_spam(message):
    try:
        await message.delete()
    except discord.HTTPException:
        pass
    silenciado = False
    try:
        await message.author.timeout(timedelta(minutes=SPAM_SILENCIO_MIN), reason="Anti-spam")
        silenciado = True
    except discord.HTTPException:
        pass
    complemento = f" e silenciado por {SPAM_SILENCIO_MIN} minutos" if silenciado else ""
    try:
        await message.channel.send(
            f"🚫 {message.author.mention}, sem spam por aqui{complemento}.", delete_after=8
        )
    except discord.HTTPException:
        pass
    await registrar_log(
        message.guild, "🚫 Spam detectado",
        f"**Autor:** {message.author.mention}\n**Canal:** {message.channel.mention}\n"
        f"**Silenciado:** {'sim' if silenciado else 'não (sem permissão)'}",
    )


async def moderar(message):
    """Aplica filtro de palavras, anti-link e anti-spam. Retorna True se tratou a mensagem."""
    if message.author.bot or message.guild is None or eh_moderador(message.author):
        return False
    cfg = ler_config()

    if cfg["moderacao_ativa"] and contem_palavra_bloqueada(message.content):
        return await apagar_com_aviso(message, "contém uma palavra proibida")
    if cfg["antilink_ativo"] and REGEX_LINK.search(message.content):
        return await apagar_com_aviso(message, "links não são permitidos aqui")
    if cfg["antispam_ativo"] and detectar_spam(message):
        await tratar_spam(message)
        return True
    return False


# ---------- eventos ----------
@bot.event
async def on_ready():
    log.info("Bot conectado como %s (ID: %s)", bot.user, bot.user.id)


@bot.event
async def on_member_join(membro):
    cfg = ler_config()
    guild = membro.guild

    if cfg["cargo_auto_ativo"] and not membro.bot:
        try:
            cargo = guild.get_role(int(cfg["cargo_auto_id"] or 0))
        except (TypeError, ValueError):
            cargo = None
        if cargo is not None:
            try:
                await membro.add_roles(cargo, reason="Cargo automático")
            except discord.HTTPException as erro_http:
                log.warning("Não foi possível dar o cargo automático: %s", erro_http)

    if cfg["boas_vindas_ativo"]:
        canal = achar_canal_boas_vindas(guild, cfg)
        if canal is None:
            log.warning("Canal de boas-vindas não encontrado em %s.", guild.name)
        else:
            try:
                await enviar_boas_vindas(canal, membro, cfg)
            except discord.HTTPException as erro_http:
                log.warning("Falha ao enviar boas-vindas em #%s: %s", canal.name, erro_http)

    await registrar_log(
        guild, "📥 Membro entrou",
        f"{membro.mention} ({membro})\nConta criada {discord.utils.format_dt(membro.created_at, 'R')}",
    )


@bot.event
async def on_member_remove(membro):
    cfg = ler_config()
    guild = membro.guild

    if cfg["saida_ativa"]:
        canal = achar_canal(guild, cfg["saida_canal_id"]) or achar_canal_boas_vindas(guild, cfg)
        if canal is not None:
            embed = discord.Embed(
                description=aplicar_variaveis(cfg["saida_msg"], membro)[:4000], color=COR_ROXO
            )
            embed.set_thumbnail(url=membro.display_avatar.url)
            embed.set_footer(text=f"{guild.name} • Agora com {guild.member_count} membros")
            try:
                await canal.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as erro_http:
                log.warning("Falha ao enviar despedida em #%s: %s", canal.name, erro_http)

    await registrar_log(guild, "📤 Membro saiu", f"{membro.mention} ({membro})")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if await moderar(message):
        return
    await bot.process_commands(message)


@bot.event
async def on_message_edit(antes, depois):
    # Impede burlar o filtro editando a mensagem depois de enviada.
    if antes.content != depois.content:
        await moderar(depois)


# ---------- comandos gerais ----------
@bot.command()
async def ip(ctx):
    with config_lock:
        ativo = bool(config_painel.get("comandos_custom_ativos", True))
    if ativo:
        await ctx.send(f"🎮 IP do servidor Minecraft: `{MINECRAFT_IP}`")


@bot.command()
async def ping(ctx):
    await ctx.reply(f"🏓 Pong! `{round(bot.latency * 1000)} ms`", mention_author=False)


@bot.command()
async def ajuda(ctx):
    embed = discord.Embed(title="🤖 Comandos do INFINITY CRAFT", color=COR_ROXO)
    embed.add_field(
        name="Geral",
        value=(
            "`!ip` — endereço do Minecraft\n"
            "`!status` — jogadores online no servidor\n"
            "`!ping` — latência do bot\n"
            "`!avatar [@membro]` — foto de perfil\n"
            "`!userinfo [@membro]` — informações do membro\n"
            "`!serverinfo` — informações do servidor\n"
            "`!enquete pergunta | opção 1 | opção 2` — cria uma votação"
        ),
        inline=False,
    )
    embed.add_field(
        name="Moderação",
        value=(
            "`!aviso <mensagem>` — envia um aviso em cartão\n"
            "`!limpar <1-100>` — apaga mensagens do canal\n"
            "`!silenciar @membro <minutos> [motivo]`\n"
            "`!dessilenciar @membro`\n"
            "`!expulsar @membro [motivo]`\n"
            "`!banir @membro [motivo]`\n"
            "`!sorteio <minutos> <prêmio>` — sorteio com 🎉\n"
            "`!ticketpainel` — painel de tickets (admin)"
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command()
async def avatar(ctx, membro: discord.Member = None):
    membro = membro or ctx.author
    embed = discord.Embed(title=f"Foto de {membro.display_name}", color=COR_ROXO)
    embed.set_image(url=membro.display_avatar.replace(size=1024).url)
    await ctx.send(embed=embed)


@bot.command()
@commands.guild_only()
async def userinfo(ctx, membro: discord.Member = None):
    membro = membro or ctx.author
    cargos = ", ".join(r.mention for r in reversed(membro.roles[1:]))[:1000] or "Nenhum"
    embed = discord.Embed(title=str(membro), color=COR_ROXO)
    embed.set_thumbnail(url=membro.display_avatar.url)
    embed.add_field(name="ID", value=str(membro.id))
    embed.add_field(name="Conta criada", value=discord.utils.format_dt(membro.created_at, "D"))
    if membro.joined_at:
        embed.add_field(name="Entrou no servidor", value=discord.utils.format_dt(membro.joined_at, "D"))
    embed.add_field(name="Cargos", value=cargos, inline=False)
    await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


@bot.command()
@commands.guild_only()
async def serverinfo(ctx):
    guild = ctx.guild
    embed = discord.Embed(title=guild.name, color=COR_ROXO)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Dono", value=guild.owner.mention if guild.owner else "—")
    embed.add_field(name="Membros", value=str(guild.member_count))
    embed.add_field(name="Canais de texto", value=str(len(guild.text_channels)))
    embed.add_field(name="Criado em", value=discord.utils.format_dt(guild.created_at, "D"))
    embed.add_field(name="Impulsos", value=str(guild.premium_subscription_count))
    await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


# ---------- comandos de moderação ----------
def hierarquia_ok(ctx, alvo):
    """O autor e o bot só podem punir quem está abaixo deles na lista de cargos."""
    if alvo == ctx.guild.owner or alvo == ctx.author:
        return False
    if ctx.author != ctx.guild.owner and alvo.top_role >= ctx.author.top_role:
        return False
    return alvo.top_role < ctx.guild.me.top_role


def motivo_auditoria(ctx, motivo):
    return f"{ctx.author} | {motivo}"[:400]


@bot.command()
@commands.guild_only()
@commands.has_guild_permissions(manage_messages=True)
async def aviso(ctx, *, mensagem: str):
    embed = discord.Embed(title="📢 AVISO IMPORTANTE", description=mensagem[:4000], color=COR_ROXO)
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass
    await ctx.send(embed=embed)


@bot.command()
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def limpar(ctx, quantidade: int):
    if not 1 <= quantidade <= 100:
        await ctx.reply("⚠️ Escolha um número de 1 a 100.", mention_author=False)
        return
    apagadas = await ctx.channel.purge(limit=quantidade + 1)  # +1 apaga o próprio comando
    await ctx.send(f"🧹 {max(len(apagadas) - 1, 0)} mensagem(ns) apagada(s).", delete_after=5)
    await registrar_log(
        ctx.guild, "🧹 Limpeza de mensagens",
        f"**Moderador:** {ctx.author.mention}\n**Canal:** {ctx.channel.mention}\n**Quantidade:** {len(apagadas) - 1}",
    )


@bot.command()
@commands.guild_only()
@commands.has_guild_permissions(moderate_members=True)
@commands.bot_has_guild_permissions(moderate_members=True)
async def silenciar(ctx, membro: discord.Member, minutos: int, *, motivo: str = "Sem motivo informado"):
    if not hierarquia_ok(ctx, membro):
        await ctx.reply("⛔ Não posso (ou você não pode) punir esse membro.", mention_author=False)
        return
    if not 1 <= minutos <= 40320:
        await ctx.reply("⚠️ Use de 1 a 40320 minutos (28 dias).", mention_author=False)
        return
    await membro.timeout(timedelta(minutes=minutos), reason=motivo_auditoria(ctx, motivo))
    await ctx.send(f"🔇 {membro.mention} foi silenciado por {minutos} minuto(s).")
    await registrar_log(
        ctx.guild, "🔇 Membro silenciado",
        f"**Membro:** {membro.mention}\n**Moderador:** {ctx.author.mention}\n**Tempo:** {minutos} min\n**Motivo:** {motivo}",
    )


@bot.command()
@commands.guild_only()
@commands.has_guild_permissions(moderate_members=True)
@commands.bot_has_guild_permissions(moderate_members=True)
async def dessilenciar(ctx, membro: discord.Member):
    await membro.timeout(None, reason=motivo_auditoria(ctx, "Silêncio removido"))
    await ctx.send(f"🔊 {membro.mention} pode falar de novo.")
    await registrar_log(
        ctx.guild, "🔊 Silêncio removido",
        f"**Membro:** {membro.mention}\n**Moderador:** {ctx.author.mention}",
    )


@bot.command()
@commands.guild_only()
@commands.has_guild_permissions(kick_members=True)
@commands.bot_has_guild_permissions(kick_members=True)
async def expulsar(ctx, membro: discord.Member, *, motivo: str = "Sem motivo informado"):
    if not hierarquia_ok(ctx, membro):
        await ctx.reply("⛔ Não posso (ou você não pode) punir esse membro.", mention_author=False)
        return
    await membro.kick(reason=motivo_auditoria(ctx, motivo))
    await ctx.send(f"👢 **{membro}** foi expulso.")
    await registrar_log(
        ctx.guild, "👢 Membro expulso",
        f"**Membro:** {membro} ({membro.id})\n**Moderador:** {ctx.author.mention}\n**Motivo:** {motivo}",
    )


@bot.command()
@commands.guild_only()
@commands.has_guild_permissions(ban_members=True)
@commands.bot_has_guild_permissions(ban_members=True)
async def banir(ctx, membro: discord.Member, *, motivo: str = "Sem motivo informado"):
    if not hierarquia_ok(ctx, membro):
        await ctx.reply("⛔ Não posso (ou você não pode) punir esse membro.", mention_author=False)
        return
    await membro.ban(reason=motivo_auditoria(ctx, motivo), delete_message_days=0)
    await ctx.send(f"🔨 **{membro}** foi banido.")
    await registrar_log(
        ctx.guild, "🔨 Membro banido",
        f"**Membro:** {membro} ({membro.id})\n**Moderador:** {ctx.author.mention}\n**Motivo:** {motivo}",
    )


# ---------- cargo por reação ----------
def chave_emoji(emoji):
    """Identifica o emoji: o ID nos personalizados, o próprio caractere nos padrão."""
    if getattr(emoji, "id", None):
        return str(emoji.id)
    return (emoji.name or "").replace("\ufe0f", "")


def cargo_da_reacao(mensagem_id, emoji):
    chave = chave_emoji(emoji)
    with config_lock:
        for painel in config_painel.get("cargos_reacao", []):
            if painel.get("mensagem_id") == str(mensagem_id):
                for par in painel.get("pares", []):
                    if par.get("chave") == chave:
                        return int(par["cargo_id"])
    return None


@bot.event
async def on_raw_reaction_add(payload):
    membro = payload.member
    if payload.guild_id is None or membro is None or membro.bot:
        return
    cargo_id = cargo_da_reacao(payload.message_id, payload.emoji)
    cargo = membro.guild.get_role(cargo_id) if cargo_id else None
    if cargo is None:
        return
    try:
        await membro.add_roles(cargo, reason="Cargo por reação")
    except discord.HTTPException as erro_http:
        log.warning("Não foi possível dar o cargo %s: %s", cargo.name, erro_http)


@bot.event
async def on_raw_reaction_remove(payload):
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if guild is None:
        return
    cargo_id = cargo_da_reacao(payload.message_id, payload.emoji)
    cargo = guild.get_role(cargo_id) if cargo_id else None
    membro = guild.get_member(payload.user_id)
    if cargo is None or membro is None or membro.bot:
        return
    try:
        await membro.remove_roles(cargo, reason="Cargo por reação")
    except discord.HTTPException as erro_http:
        log.warning("Não foi possível tirar o cargo %s: %s", cargo.name, erro_http)


@bot.event
async def on_raw_message_delete(payload):
    """Se alguém apagar a mensagem de um painel de cargos, tira o painel da configuração."""
    with config_lock:
        restantes = [p for p in config_painel.get("cargos_reacao", [])
                     if p.get("mensagem_id") != str(payload.message_id)]
        if len(restantes) != len(config_painel.get("cargos_reacao", [])):
            atualizar_config(cargos_reacao=restantes)


# ---------- tickets ----------
def separar_emoji(linha):
    """'🛠️ Suporte' -> ('🛠️', 'Suporte'). Sem emoji no começo -> (None, linha)."""
    linha = linha.strip()
    partes = linha.split(maxsplit=1)
    if len(partes) == 2 and not partes[0][0].isalnum():
        return partes[0], partes[1][:90]
    return None, linha[:90]


def dono_do_ticket(canal):
    topico = getattr(canal, "topic", None) or ""
    if topico.startswith("ticket:"):
        try:
            return int(topico[7:].split("|")[0])
        except ValueError:
            return None
    return None


def categoria_do_ticket(canal):
    topico = getattr(canal, "topic", None) or ""
    return topico.split("|", 1)[1] if "|" in topico else "Suporte"


def eh_staff_ticket(membro, cfg):
    perms = membro.guild_permissions
    if perms.administrator:
        return True
    try:
        cargo_id = int(cfg["ticket_staff_id"] or 0)
    except (TypeError, ValueError):
        cargo_id = 0
    if cargo_id:
        return any(r.id == cargo_id for r in membro.roles)
    return perms.manage_channels


def proximo_ticket():
    with config_lock:
        config_painel["ticket_contador"] = int(config_painel.get("ticket_contador", 0)) + 1
        salvar_config(config_painel)
        return config_painel["ticket_contador"]


async def gerar_transcricao(canal):
    """Texto com todas as mensagens do ticket (até 2000)."""
    linhas = []
    async for msg in canal.history(limit=2000, oldest_first=True):
        hora = msg.created_at.astimezone().strftime("%d/%m/%Y %H:%M")
        texto = msg.content or ""
        if msg.attachments:
            texto += " " + " ".join(a.url for a in msg.attachments)
        if not texto.strip() and msg.embeds:
            texto = "[cartão]"
        linhas.append(f"[{hora}] {msg.author}: {texto.strip()}")
    return "\n".join(linhas).encode("utf-8")


class SelecaoTicket(discord.ui.Select):
    def __init__(self, categorias=None):
        if categorias is None:
            categorias = ler_config()["ticket_categorias"]
        opcoes = []
        for linha in categorias[:25]:
            emoji, nome = separar_emoji(linha)
            opcoes.append(discord.SelectOption(label=nome, value=nome, emoji=emoji))
        if not opcoes:
            opcoes.append(discord.SelectOption(label="Suporte", value="Suporte", emoji="🛠️"))
        super().__init__(
            custom_id="ic:ticket:abrir",
            placeholder="🎫 Escolha o motivo para abrir seu ticket",
            options=opcoes,
        )

    async def callback(self, interaction):
        await abrir_ticket(interaction, self.values[0])
        try:  # volta o menu ao estado inicial
            await interaction.message.edit(view=PainelTicketView())
        except discord.HTTPException:
            pass


class PainelTicketView(discord.ui.View):
    def __init__(self, categorias=None):
        super().__init__(timeout=None)
        self.add_item(SelecaoTicket(categorias))


class ConfirmarFechamentoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)

    @discord.ui.button(label="Sim, fechar", emoji="🔒", style=discord.ButtonStyle.danger)
    async def sim(self, interaction, button):
        await interaction.response.edit_message(content="🔒 Fechando o ticket...", view=None)
        await fechar_ticket(interaction.channel, interaction.user)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def nao(self, interaction, button):
        await interaction.response.edit_message(content="👍 Ok, o ticket continua aberto.", view=None)


class ControleTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Assumir", emoji="🙋", style=discord.ButtonStyle.success, custom_id="ic:ticket:assumir")
    async def assumir(self, interaction, button):
        if not eh_staff_ticket(interaction.user, ler_config()):
            await interaction.response.send_message("⛔ Só a equipe pode assumir tickets.", ephemeral=True)
            return
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(color=COR_ROXO)
        if len(embed.fields) >= 3:
            embed.set_field_at(2, name="Responsável", value=interaction.user.mention)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.channel.send(
            f"🙋 {interaction.user.mention} assumiu este ticket e vai ajudar você.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Fechar ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ic:ticket:fechar")
    async def fechar(self, interaction, button):
        dono = dono_do_ticket(interaction.channel)
        if dono is None:
            await interaction.response.send_message("⚠️ Este canal não é um ticket.", ephemeral=True)
            return
        if interaction.user.id != dono and not eh_staff_ticket(interaction.user, ler_config()):
            await interaction.response.send_message("⛔ Só quem abriu ou a equipe pode fechar.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Tem certeza que quer fechar este ticket? A conversa será salva e o canal apagado.",
            view=ConfirmarFechamentoView(), ephemeral=True,
        )


async def abrir_ticket(interaction, nome_categoria):
    await interaction.response.defer(ephemeral=True)
    cfg = ler_config()
    guild, usuario = interaction.guild, interaction.user
    if guild is None or not isinstance(usuario, discord.Member):
        return
    if not cfg["ticket_ativo"]:
        await interaction.followup.send("⛔ O sistema de tickets está desligado no momento.", ephemeral=True)
        return
    for canal in guild.text_channels:
        if dono_do_ticket(canal) == usuario.id:
            await interaction.followup.send(f"⚠️ Você já tem um ticket aberto: {canal.mention}", ephemeral=True)
            return

    try:
        categoria = guild.get_channel(int(cfg["ticket_categoria_id"] or 0))
    except (TypeError, ValueError):
        categoria = None
    if not isinstance(categoria, discord.CategoryChannel):
        categoria = None
    try:
        staff = guild.get_role(int(cfg["ticket_staff_id"] or 0))
    except (TypeError, ValueError):
        staff = None

    acesso = dict(view_channel=True, send_messages=True, read_message_history=True,
                  attach_files=True, embed_links=True)
    sobrescritas = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        usuario: discord.PermissionOverwrite(**acesso),
        guild.me: discord.PermissionOverwrite(manage_channels=True, manage_messages=True, **acesso),
    }
    if staff is not None:
        sobrescritas[staff] = discord.PermissionOverwrite(manage_messages=True, **acesso)

    numero = proximo_ticket()
    sufixo = re.sub(r"[^a-z0-9]+", "-", normalizar(usuario.name)).strip("-")[:20]
    nome_canal = f"ticket-{numero:04d}" + (f"-{sufixo}" if sufixo else "")
    try:
        canal = await guild.create_text_channel(
            nome_canal, category=categoria, overwrites=sobrescritas,
            topic=f"ticket:{usuario.id}|{nome_categoria}", reason=f"Ticket de {usuario}",
        )
    except discord.HTTPException as erro_http:
        log.warning("Falha ao criar o ticket: %s", erro_http)
        await interaction.followup.send(
            "❌ Não consegui criar o ticket. Avise a equipe (o bot precisa da permissão Gerenciar canais).",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title=f"🎫 Ticket #{numero:04d} • {nome_categoria}",
        description=(
            f"Olá, {usuario.mention}! 👋\n"
            "Conte com detalhes o que você precisa e aguarde: nossa equipe vai atender você em breve.\n\n"
            "Use os botões abaixo para **assumir** (equipe) ou **fechar** o ticket."
        ),
        color=COR_ROXO,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url=usuario.display_avatar.url)
    embed.add_field(name="Aberto por", value=usuario.mention)
    embed.add_field(name="Categoria", value=nome_categoria)
    embed.add_field(name="Responsável", value="Ninguém ainda")
    embed.set_footer(text=guild.name)
    aviso = usuario.mention + (f" {staff.mention}" if staff else "")
    await canal.send(
        content=aviso, embed=embed, view=ControleTicketView(),
        allowed_mentions=discord.AllowedMentions(users=[usuario], roles=[staff] if staff else False),
    )
    await interaction.followup.send(f"✅ Seu ticket foi criado: {canal.mention}", ephemeral=True)
    await registrar_log(
        guild, "🎫 Ticket aberto",
        f"**Ticket:** {canal.mention}\n**Aberto por:** {usuario.mention}\n**Categoria:** {nome_categoria}",
    )


async def fechar_ticket(canal, quem):
    cfg = ler_config()
    guild = canal.guild
    dono_id = dono_do_ticket(canal)
    categoria = categoria_do_ticket(canal)
    texto = await gerar_transcricao(canal)

    def arquivo():
        return discord.File(io.BytesIO(texto), filename=f"{canal.name}.txt")

    resumo = discord.Embed(
        title="📄 Ticket fechado",
        description=(f"**Ticket:** {canal.name}\n**Categoria:** {categoria}\n"
                     f"**Aberto por:** <@{dono_id}>\n**Fechado por:** {quem.mention}"),
        color=COR_ROXO,
        timestamp=discord.utils.utcnow(),
    )
    canal_logs = achar_canal(guild, cfg["ticket_logs_canal_id"])
    if canal_logs is not None:
        try:
            await canal_logs.send(embed=resumo, file=arquivo(), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as erro_http:
            log.warning("Não foi possível enviar a transcrição: %s", erro_http)

    dono = guild.get_member(dono_id) if dono_id else None
    if dono is not None:
        try:
            aviso_dm = discord.Embed(
                title="🔒 Seu ticket foi fechado",
                description=f"Servidor: **{guild.name}**\nCategoria: **{categoria}**\nSegue a conversa em anexo.",
                color=COR_ROXO,
            )
            await dono.send(embed=aviso_dm, file=arquivo())
        except discord.HTTPException:
            pass  # DMs fechadas

    try:
        await canal.send(embed=discord.Embed(
            description=f"🔒 Ticket fechado por {quem.mention}. Este canal será apagado em 5 segundos.",
            color=COR_ROXO,
        ), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass
    await registrar_log(guild, "🔒 Ticket fechado", f"**Ticket:** {canal.name}\n**Fechado por:** {quem.mention}")
    await asyncio.sleep(5)
    try:
        await canal.delete(reason=f"Ticket fechado por {quem}")
    except discord.HTTPException:
        log.exception("Não foi possível apagar o canal do ticket.")


async def enviar_painel_ticket(canal, cfg):
    embed = discord.Embed(
        title=cfg["ticket_titulo"][:256] or None,
        description=cfg["ticket_descricao"][:4000],
        color=COR_ROXO,
    )
    if canal.guild.icon:
        embed.set_thumbnail(url=canal.guild.icon.url)
    embed.set_footer(text=f"{canal.guild.name} • Atendimento")
    dados = {"embed": embed, "view": PainelTicketView(cfg["ticket_categorias"])}
    caminho = caminho_imagem(cfg, "ticket")
    if caminho is not None:
        arquivo = discord.File(caminho, filename="ticket" + caminho.suffix)
        embed.set_image(url="attachment://" + arquivo.filename)
        dados["file"] = arquivo
    await canal.send(**dados)


@bot.command()
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def ticketpainel(ctx):
    """Envia o painel de tickets no canal atual."""
    await enviar_painel_ticket(ctx.channel, ler_config())
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass


async def configurar_views():
    # Registra os botões/menus dos tickets para continuarem funcionando depois de reiniciar o bot.
    bot.add_view(PainelTicketView())
    bot.add_view(ControleTicketView())


bot.setup_hook = configurar_views


# ---------- status do servidor de Minecraft (Java) ----------
def _varint(numero):
    saida = b""
    while True:
        byte = numero & 0x7F
        numero >>= 7
        saida += bytes([byte | (0x80 if numero else 0)])
        if not numero:
            return saida


async def _ler_varint(leitor):
    resultado = 0
    for posicao in range(5):
        byte = (await leitor.readexactly(1))[0]
        resultado |= (byte & 0x7F) << (7 * posicao)
        if not byte & 0x80:
            return resultado
    raise ValueError("VarInt grande demais")


async def _consultar(host, porta):
    inicio = time.monotonic()
    leitor, escritor = await asyncio.open_connection(host, porta)
    try:
        host_bytes = host.encode("utf-8")
        handshake = b"\x00" + _varint(765) + _varint(len(host_bytes)) + host_bytes + struct.pack(">H", porta) + _varint(1)
        escritor.write(_varint(len(handshake)) + handshake + b"\x01\x00")
        await escritor.drain()
        await _ler_varint(leitor)          # tamanho do pacote
        await _ler_varint(leitor)          # id do pacote (0)
        tamanho = await _ler_varint(leitor)
        dados = await leitor.readexactly(tamanho)
        return json.loads(dados.decode("utf-8")), round((time.monotonic() - inicio) * 1000)
    finally:
        escritor.close()


async def consultar_minecraft(endereco, timeout=6):
    host, _, porta = endereco.partition(":")
    porta = int(porta) if porta.isdigit() else 25565
    return await asyncio.wait_for(_consultar(host, porta), timeout)


def texto_motd(descricao):
    if isinstance(descricao, str):
        texto = descricao
    elif isinstance(descricao, dict):
        texto = str(descricao.get("text", "")) + "".join(texto_motd(e) for e in descricao.get("extra", []))
    else:
        texto = ""
    return re.sub(r"§.", "", texto)


@bot.command()
async def status(ctx):
    with config_lock:
        ativo = bool(config_painel.get("comandos_custom_ativos", True))
    if not ativo:
        return
    if MINECRAFT_IP.startswith("IP_DO_SEU"):
        await ctx.reply("⚠️ O IP do Minecraft ainda não foi configurado (variável MINECRAFT_IP).", mention_author=False)
        return
    async with ctx.typing():
        try:
            dados, latencia = await consultar_minecraft(MINECRAFT_IP)
        except (OSError, asyncio.TimeoutError, ValueError, asyncio.IncompleteReadError):
            embed = discord.Embed(title="🔴 Servidor offline", color=COR_ROXO,
                                  description=f"Não consegui falar com `{MINECRAFT_IP}` agora.")
            await ctx.send(embed=embed)
            return
    jogadores = dados.get("players", {})
    nomes = ", ".join(j.get("name", "?") for j in jogadores.get("sample", []))[:900]
    embed = discord.Embed(title="🟢 Servidor online", color=COR_ROXO,
                          description=texto_motd(dados.get("description", "")).strip()[:500] or None)
    embed.add_field(name="Jogadores", value=f"{jogadores.get('online', 0)}/{jogadores.get('max', 0)}")
    embed.add_field(name="Versão", value=str(dados.get("version", {}).get("name", "?"))[:50])
    embed.add_field(name="Resposta", value=f"{latencia} ms")
    embed.add_field(name="IP", value=f"`{MINECRAFT_IP}`", inline=False)
    if nomes:
        embed.add_field(name="Quem está online", value=nomes, inline=False)
    await ctx.send(embed=embed)


# ---------- enquete e sorteio ----------
NUMEROS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


@bot.command()
@commands.guild_only()
async def enquete(ctx, *, texto: str):
    """!enquete pergunta | opção 1 | opção 2 (sem opções vira Sim/Não)."""
    partes = [p.strip() for p in texto.split("|") if p.strip()]
    pergunta, opcoes = partes[0], partes[1:10]
    emojis = NUMEROS if opcoes else ["✅", "❌"]
    opcoes = opcoes or ["Sim", "Não"]
    linhas = [f"{emojis[i]}  {opcao}" for i, opcao in enumerate(opcoes)]
    embed = discord.Embed(title=f"📊 {pergunta[:250]}", description="\n".join(linhas), color=COR_ROXO)
    embed.set_footer(text=f"Enquete de {ctx.author.display_name}")
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass
    mensagem = await ctx.send(embed=embed)
    for emoji in emojis[:len(opcoes)]:
        await mensagem.add_reaction(emoji)


@bot.command()
@commands.guild_only()
@commands.has_guild_permissions(manage_messages=True)
async def sorteio(ctx, minutos: int, *, premio: str):
    """!sorteio <minutos> <prêmio> — se o bot reiniciar durante o sorteio, ele se perde."""
    if not 1 <= minutos <= 1440:
        await ctx.reply("⚠️ Use de 1 a 1440 minutos (24 horas).", mention_author=False)
        return
    fim = discord.utils.utcnow() + timedelta(minutes=minutos)
    embed = discord.Embed(
        title="🎉 SORTEIO",
        description=f"**Prêmio:** {premio[:500]}\n\nReaja com 🎉 para participar!\nTermina {discord.utils.format_dt(fim, 'R')}",
        color=COR_ROXO,
    )
    embed.set_footer(text=f"Sorteio de {ctx.author.display_name}")
    mensagem = await ctx.send(embed=embed)
    await mensagem.add_reaction("🎉")
    await asyncio.sleep(minutos * 60)

    try:
        mensagem = await ctx.channel.fetch_message(mensagem.id)
    except discord.NotFound:
        return  # a mensagem foi apagada
    reacao = discord.utils.get(mensagem.reactions, emoji="🎉")
    participantes = [u async for u in reacao.users() if not u.bot] if reacao else []
    if participantes:
        ganhador = random.choice(participantes)
        resultado = f"🏆 Ganhador: {ganhador.mention}\n**Prêmio:** {premio[:500]}"
        await ctx.send(f"🎉 Parabéns {ganhador.mention}! Você ganhou **{premio[:200]}**!")
    else:
        resultado = "Ninguém participou do sorteio. 😢"
    embed = discord.Embed(title="🎉 SORTEIO ENCERRADO", description=resultado, color=COR_ROXO)
    await mensagem.edit(embed=embed)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.NoPrivateMessage):
        texto = "⚠️ Esse comando só funciona dentro de um servidor."
    elif isinstance(error, commands.MissingPermissions):
        texto = "⛔ Você não tem permissão para usar esse comando."
    elif isinstance(error, commands.BotMissingPermissions):
        texto = "⛔ Eu não tenho permissão para fazer isso. Ajuste as permissões do bot no servidor."
    elif isinstance(error, commands.MissingRequiredArgument):
        texto = f"⚠️ Falta um argumento. Use: `!{ctx.command.name} {ctx.command.signature}`"
    elif isinstance(error, commands.BadArgument):
        texto = "⚠️ Não entendi um dos argumentos (membro não encontrado ou número inválido)."
    else:
        causa = getattr(error, "original", error)
        if isinstance(causa, discord.Forbidden):
            texto = "⛔ O Discord não me deixou fazer isso (confira a posição do cargo do bot)."
        else:
            log.error("Erro em comando", exc_info=(type(error), error, error.__traceback__))
            texto = "❌ Ocorreu um erro ao executar o comando."
    try:
        await ctx.reply(texto, mention_author=False)
    except discord.HTTPException:
        pass


# =========================================================
# PAINEL WEB
# =========================================================
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("PANEL_HTTPS", "").strip() == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
)

LOGIN_HTML = r"""
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login | INFINITY CRAFT</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#1e1f22;color:#f2f3f5;font-family:"Segoe UI",Arial,sans-serif}
.card{width:min(420px,92vw);background:#2b2d31;padding:28px;border-radius:12px;box-shadow:0 12px 36px #0005}
h1{color:#b07fd0;margin-top:0}
.input{width:100%;padding:12px;margin:12px 0;background:#1e1f22;color:#fff;border:1px solid #484b52;border-radius:6px;font:inherit}
button{width:100%;padding:12px;background:#8e44ad;color:#fff;border:0;border-radius:6px;font-weight:bold;cursor:pointer}
button:hover{filter:brightness(1.1)}
:focus-visible{outline:2px solid #b07fd0;outline-offset:2px}
.error{color:#ff8c8c}
</style>
</head>
<body>
<main class="card">
  <h1>🎛️ INFINITY CRAFT</h1>
  <p>Entre para acessar o painel do bot.</p>
  {% for categoria, msg in get_flashed_messages(with_categories=true) %}
    <p class="error" role="alert">{{ msg }}</p>
  {% endfor %}
  <form method="post">
    <input class="input" type="password" name="senha" placeholder="Senha do painel" aria-label="Senha do painel" required autofocus>
    <button type="submit">Entrar</button>
  </form>
</main>
</body>
</html>
"""

PAINEL_HTML = r"""
{% macro interruptor(chave, ativo, rotulo_on, rotulo_off, nome='Status') %}
<div class="row">
  <span>{{ nome }}: <span class="badge {{ 'on' if ativo else 'off' }}">{{ rotulo_on if ativo else rotulo_off }}</span></span>
  <form action="{{ url_for('alternar', chave=chave) }}" method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <button class="btn {{ 'red' if ativo else 'green' }}" type="submit">{{ 'Desligar' if ativo else 'Ligar' }}</button>
  </form>
</div>
{% endmacro %}

{% macro seletor_canal(nome, atual, vazio, obrigatorio=False) %}
<select class="input" id="{{ nome }}" name="{{ nome }}" {{ 'required' if obrigatorio }}>
  <option value="" {{ 'selected' if not atual }}>{{ vazio }}</option>
  {% for s in servidores %}
  <optgroup label="{{ s.nome }}">
    {% for c in s.canais %}
    <option value="{{ c.id }}" {{ 'selected' if c.id == atual|string }}>#{{ c.nome }}</option>
    {% endfor %}
  </optgroup>
  {% endfor %}
</select>
{% endmacro %}
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>INFINITY CRAFT | Painel</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#1e1f22;color:#f2f3f5;font-family:"Segoe UI",Arial,sans-serif;padding-bottom:40px}
header{padding:24px 16px;background:#242629;border-bottom:1px solid #3a3d42}
h1{margin:0;color:#b07fd0}
h2{margin-top:0}
main{max-width:900px;margin:24px auto;padding:0 16px}
.card{background:#2b2d31;border:1px solid #3a3d42;border-radius:10px;padding:20px;margin-bottom:18px;box-shadow:0 4px 12px #0002}
.input{width:100%;padding:11px;background:#1e1f22;color:#f2f3f5;border:1px solid #484b52;border-radius:6px;margin:8px 0 16px;font:inherit}
textarea{resize:vertical}
.btn{display:inline-block;padding:11px 15px;border:0;border-radius:6px;background:#8e44ad;color:#fff;font:inherit;font-weight:bold;cursor:pointer;text-decoration:none}
.btn:hover{filter:brightness(1.1)}
.green{background:#248046}.gray{background:#4e5058}.red{background:#b83b43}
.row{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.row form{margin:0}
.badge{padding:4px 9px;border-radius:5px;font-size:12px;font-weight:bold}
.on{background:#248046}.off{background:#b83b43}
.notice{background:#203b2b;border:1px solid #248046;padding:12px;border-radius:6px;margin-bottom:12px}
.error{background:#49272a;border:1px solid #b83b43;padding:12px;border-radius:6px;margin-bottom:12px}
.small{color:#b5bac1;font-size:13px}
.divisor{border:0;border-top:1px solid #3a3d42;margin:18px 0}
.check{display:flex;gap:8px;align-items:center;margin:6px 0 14px}
.check input{width:auto;margin:0}
.previa{max-width:100%;max-height:220px;border-radius:8px;border:1px solid #484b52;display:block;margin:8px 0 12px}
.acoes{display:flex;gap:10px;flex-wrap:wrap}
.par{display:grid;grid-template-columns:100px 1fr;gap:10px}
.item{padding:10px 0;border-top:1px solid #3a3d42}
h3{margin:0 0 6px}
:focus-visible{outline:2px solid #b07fd0;outline-offset:2px}
@media(max-width:600px){.card{padding:15px}.btn{width:100%;text-align:center}}
</style>
</head>
<body>
<header>
  <div class="row" style="max-width:900px;margin:auto">
    <div><h1>🎛️ INFINITY CRAFT</h1><p style="margin:4px 0 0">Controle do bot do Discord</p></div>
    <form action="{{ url_for('logout') }}" method="post">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="btn gray" type="submit">Sair</button>
    </form>
  </div>
</header>

<main>
{% for categoria, msg in get_flashed_messages(with_categories=true) %}
  <div class="{{ 'error' if categoria == 'erro' else 'notice' }}" role="alert">{{ msg }}</div>
{% endfor %}

<section class="card">
  <div class="row">
    <h2>📡 Estado do bot</h2>
    <span class="badge {{ 'on' if bot_online else 'off' }}">{{ 'ONLINE' if bot_online else 'OFFLINE' }}</span>
  </div>
  {% if bot_online %}
    <p>Conectado como <strong>{{ bot_nome }}</strong></p>
  {% else %}
    <p>O bot ainda está conectando ou está offline. Atualize a página em alguns segundos.</p>
  {% endif %}
  <p class="small">Servidores encontrados: {{ servidores|length }}</p>
</section>

<section class="card">
  <h2>🚪 Boas-vindas</h2>
  {{ interruptor('boas_vindas_ativo', config.boas_vindas_ativo, 'LIGADO', 'DESLIGADO') }}
  <hr class="divisor">
  <form action="{{ url_for('salvar_boas_vindas') }}" method="post" enctype="multipart/form-data">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">

    <label for="canal_boas_vindas_id">Canal de boas-vindas</label>
    {{ seletor_canal('canal_boas_vindas_id', config.canal_boas_vindas_id, 'Selecione um canal', True) }}

    <label class="check"><input type="checkbox" name="usar_embed" {{ 'checked' if config.boas_vindas_embed }}> Enviar em cartão roxo (embed)</label>

    <label for="boas_vindas_titulo">Título do cartão</label>
    <input class="input" id="boas_vindas_titulo" name="boas_vindas_titulo" maxlength="256" value="{{ config.boas_vindas_titulo }}">

    <label for="boas_vindas_msg">Mensagem</label>
    <textarea class="input" id="boas_vindas_msg" name="boas_vindas_msg" rows="4" maxlength="1500" required>{{ config.boas_vindas_msg }}</textarea>
    <p class="small">Variáveis: <code>{usuario}</code> (menção), <code>{nome}</code>, <code>{servidor}</code> e <code>{membros}</code> (total de membros). A menção ao novo membro sempre vai fora do cartão, para ele ser avisado.</p>

    <label class="check"><input type="checkbox" name="foto_perfil" {{ 'checked' if config.boas_vindas_foto_perfil }}> Mostrar a foto de perfil do novo membro no canto do cartão</label>

    <label for="imagem_arquivo">Imagem grande do cartão (PNG, JPG, GIF ou WEBP, até 8 MB)</label>
    {% if imagem_atual %}
      <img class="previa" src="{{ url_for('imagem_painel', slot='boas_vindas', v=imagem_versao) }}" alt="Imagem atual do cartão de boas-vindas">
      <label class="check"><input type="checkbox" name="remover_imagem"> Remover a imagem atual</label>
    {% endif %}
    <input class="input" type="file" id="imagem_arquivo" name="imagem_arquivo" accept="image/png,image/jpeg,image/gif,image/webp">

    <label for="imagem_url">Ou use o link de uma imagem (só vale se não houver imagem enviada)</label>
    <input class="input" id="imagem_url" name="imagem_url" maxlength="500" placeholder="https://..." value="{{ config.boas_vindas_imagem_url }}">

    <div class="acoes"><button class="btn" type="submit">Salvar boas-vindas</button></div>
  </form>
  <form action="{{ url_for('testar_boas_vindas') }}" method="post" style="margin-top:10px">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <button class="btn gray" type="submit">👀 Enviar teste no canal salvo</button>
    <span class="small">No teste, a foto de perfil mostrada é a do próprio bot.</span>
  </form>
</section>

<section class="card">
  <h2>👋 Despedida</h2>
  {{ interruptor('saida_ativa', config.saida_ativa, 'LIGADA', 'DESLIGADA') }}
  <hr class="divisor">
  <form action="{{ url_for('salvar_saida') }}" method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label for="saida_canal_id">Canal da despedida</label>
    {{ seletor_canal('saida_canal_id', config.saida_canal_id, 'Mesmo canal das boas-vindas') }}
    <label for="saida_msg">Mensagem (use <code>{nome}</code>, <code>{servidor}</code>, <code>{membros}</code>)</label>
    <textarea class="input" id="saida_msg" name="saida_msg" rows="3" maxlength="1500" required>{{ config.saida_msg }}</textarea>
    <button class="btn" type="submit">Salvar despedida</button>
  </form>
</section>

<section class="card">
  <h2>🎖️ Cargo automático</h2>
  {{ interruptor('cargo_auto_ativo', config.cargo_auto_ativo, 'LIGADO', 'DESLIGADO') }}
  <hr class="divisor">
  <form action="{{ url_for('salvar_cargo') }}" method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label for="cargo_id">Cargo dado a quem entra (bots não recebem)</label>
    <select class="input" id="cargo_id" name="cargo_id" required>
      <option value="" {{ 'selected' if not config.cargo_auto_id }}>Selecione um cargo</option>
      {% for s in servidores %}
      <optgroup label="{{ s.nome }}">
        {% for r in s.cargos %}
        <option value="{{ r.id }}" {{ 'selected' if r.id == config.cargo_auto_id|string }}>{{ r.nome }}</option>
        {% endfor %}
      </optgroup>
      {% endfor %}
    </select>
    <p class="small">Só aparecem cargos abaixo do cargo do bot. Se a lista vier vazia, arraste o cargo do bot para cima nas configurações do servidor.</p>
    <button class="btn" type="submit">Salvar cargo</button>
  </form>
</section>

<section class="card">
  <h2>🎫 Sistema de tickets</h2>
  {{ interruptor('ticket_ativo', config.ticket_ativo, 'LIGADO', 'DESLIGADO') }}
  <hr class="divisor">
  <form action="{{ url_for('salvar_ticket') }}" method="post" enctype="multipart/form-data">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">

    <label for="ticket_categoria_id">Categoria do Discord onde os tickets são criados</label>
    <select class="input" id="ticket_categoria_id" name="ticket_categoria_id">
      <option value="" {{ 'selected' if not config.ticket_categoria_id }}>Sem categoria (fora de pastas)</option>
      {% for s in servidores %}
      <optgroup label="{{ s.nome }}">
        {% for c in s.categorias %}
        <option value="{{ c.id }}" {{ 'selected' if c.id == config.ticket_categoria_id|string }}>{{ c.nome }}</option>
        {% endfor %}
      </optgroup>
      {% endfor %}
    </select>

    <label for="ticket_staff_id">Cargo da equipe (vê e responde todos os tickets)</label>
    <select class="input" id="ticket_staff_id" name="ticket_staff_id">
      <option value="" {{ 'selected' if not config.ticket_staff_id }}>Nenhum (só quem tem Gerenciar canais)</option>
      {% for s in servidores %}
      <optgroup label="{{ s.nome }}">
        {% for r in s.cargos_todos %}
        <option value="{{ r.id }}" {{ 'selected' if r.id == config.ticket_staff_id|string }}>{{ r.nome }}</option>
        {% endfor %}
      </optgroup>
      {% endfor %}
    </select>

    <label for="ticket_logs_canal_id">Canal das transcrições (conversa salva ao fechar)</label>
    {{ seletor_canal('ticket_logs_canal_id', config.ticket_logs_canal_id, 'Não enviar transcrições') }}

    <label for="ticket_titulo">Título do painel</label>
    <input class="input" id="ticket_titulo" name="ticket_titulo" maxlength="256" value="{{ config.ticket_titulo }}" required>

    <label for="ticket_descricao">Texto do painel</label>
    <textarea class="input" id="ticket_descricao" name="ticket_descricao" rows="4" maxlength="2000" required>{{ config.ticket_descricao }}</textarea>

    <label for="ticket_categorias">Motivos do menu (um por linha, com emoji no começo)</label>
    <textarea class="input" id="ticket_categorias" name="ticket_categorias" rows="5" required>{{ config.ticket_categorias | join('\n') }}</textarea>

    <label for="ticket_imagem">Imagem grande do painel (opcional)</label>
    {% if imagem_ticket_atual %}
      <img class="previa" src="{{ url_for('imagem_painel', slot='ticket', v=imagem_ticket_versao) }}" alt="Imagem atual do painel de tickets">
      <label class="check"><input type="checkbox" name="remover_imagem_ticket"> Remover a imagem atual</label>
    {% endif %}
    <input class="input" type="file" id="ticket_imagem" name="ticket_imagem" accept="image/png,image/jpeg,image/gif,image/webp">

    <button class="btn" type="submit">Salvar tickets</button>
  </form>
  <hr class="divisor">
  <form action="{{ url_for('enviar_painel_ticket_web') }}" method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label for="ticket_painel_canal_id">Enviar o painel de tickets neste canal</label>
    {{ seletor_canal('ticket_painel_canal_id', '', 'Selecione um canal', True) }}
    <p class="small">Salve as configurações antes de enviar. Se mudar o painel depois, envie de novo. Quem clicar abre um canal privado só com a pessoa e a equipe, e ao fechar a conversa vai para as transcrições.</p>
    <button class="btn green" type="submit">🎫 Enviar painel de tickets</button>
  </form>
</section>

<section class="card">
  <h2>🎭 Cargo por reação</h2>
  {% if servidores %}
  <form action="{{ url_for('criar_cargos_reacao') }}" method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label for="rr_servidor">Servidor</label>
    <select class="input" id="rr_servidor" name="guild_id" required>
      {% for s in servidores %}<option value="{{ s.id }}">{{ s.nome }}</option>{% endfor %}
    </select>
    <label for="rr_canal">Canal onde o painel será enviado</label>
    <select class="input" id="rr_canal" name="canal_id" required></select>
    <label for="rr_titulo">Título</label>
    <input class="input" id="rr_titulo" name="titulo" maxlength="256" value="Escolha seus cargos">
    <label for="rr_descricao">Texto (opcional)</label>
    <textarea class="input" id="rr_descricao" name="descricao" rows="2" maxlength="1500" placeholder="Ex.: Reaja abaixo para receber os avisos que você quer."></textarea>
    <p class="small">Escolha até 8 pares. Use emojis normais ou emojis personalizados do próprio servidor. Só aparecem cargos abaixo do cargo do bot.</p>
    {% for i in range(8) %}
    <div class="par">
      <input class="input" name="emoji_{{ i }}" maxlength="64" placeholder="😀" aria-label="Emoji {{ i + 1 }}">
      <select class="input cargo-rr" name="cargo_{{ i }}" aria-label="Cargo {{ i + 1 }}"></select>
    </div>
    {% endfor %}
    <button class="btn green" type="submit">🎭 Criar painel de cargos</button>
  </form>
  {% else %}
  <p>O bot não está em nenhum servidor acessível ou ainda está conectando.</p>
  {% endif %}
  {% if paineis_reacao %}
  <hr class="divisor">
  <h3>Painéis ativos</h3>
  {% for p in paineis_reacao %}
  <div class="row item">
    <div>
      <strong>{{ p.titulo }}</strong> em #{{ p.canal_nome }}<br>
      <span class="small">{% for par in p.pares %}{{ par.emoji }} {{ par.cargo_nome }}{% if not loop.last %} · {% endif %}{% endfor %}</span>
    </div>
    <form action="{{ url_for('remover_cargos_reacao') }}" method="post">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <input type="hidden" name="mensagem_id" value="{{ p.mensagem_id }}">
      <button class="btn red" type="submit">Remover</button>
    </form>
  </div>
  {% endfor %}
  {% endif %}
</section>

<section class="card">
  <h2>🛡️ Moderação automática</h2>
  {{ interruptor('moderacao_ativa', config.moderacao_ativa, 'LIGADO', 'DESLIGADO', 'Filtro de palavras') }}
  <hr class="divisor">
  {{ interruptor('antilink_ativo', config.antilink_ativo, 'LIGADO', 'DESLIGADO', 'Anti-link') }}
  <hr class="divisor">
  {{ interruptor('antispam_ativo', config.antispam_ativo, 'LIGADO', 'DESLIGADO', 'Anti-spam') }}
  <p class="small">Anti-spam: 5 mensagens em 6 segundos apaga a última e silencia a pessoa por 5 minutos. Moderadores (Administrador ou Gerenciar mensagens) nunca são afetados.</p>
  <hr class="divisor">
  <form action="{{ url_for('salvar_palavras') }}" method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label for="palavras">Palavras bloqueadas (uma por linha)</label>
    <textarea class="input" id="palavras" name="palavras" rows="6">{{ config.palavras_bloqueadas | join('\n') }}</textarea>
    <p class="small">Só a palavra inteira é bloqueada, sem diferenciar maiúsculas ou acentos. As mudanças valem na hora.</p>
    <button class="btn" type="submit">Salvar palavras</button>
  </form>
</section>

<section class="card">
  <h2>📜 Registro de atividades (logs)</h2>
  {{ interruptor('logs_ativo', config.logs_ativo, 'LIGADO', 'DESLIGADO') }}
  <hr class="divisor">
  <form action="{{ url_for('salvar_logs') }}" method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label for="logs_canal_id">Canal dos logs (deixe privado para a equipe)</label>
    {{ seletor_canal('logs_canal_id', config.logs_canal_id, 'Selecione um canal', True) }}
    <p class="small">Registra entradas, saídas, mensagens removidas pela moderação, spam e punições feitas pelos comandos.</p>
    <button class="btn" type="submit">Salvar canal de logs</button>
  </form>
</section>

<section class="card">
  <h2>📢 Enviar mensagem para um canal</h2>
  {% if servidores %}
  <form action="{{ url_for('enviar_mensagem') }}" method="post" enctype="multipart/form-data">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">

    <label for="servidor">Servidor</label>
    <select class="input" id="servidor" name="guild_id" required>
      {% for s in servidores %}<option value="{{ s.id }}">{{ s.nome }}</option>{% endfor %}
    </select>

    <label for="canal">Canal de texto</label>
    <select class="input" id="canal" name="canal_id" required>
      <option value="">Carregando canais...</option>
    </select>

    <label for="formato_mensagem">Formato</label>
    <select class="input" id="formato_mensagem" name="formato_mensagem">
      <option value="normal">Mensagem normal</option>
      <option value="embed">Cartão roxo (embed)</option>
    </select>

    <div id="campo_titulo" hidden>
      <label for="titulo_mensagem">Título do cartão</label>
      <input class="input" id="titulo_mensagem" name="titulo_mensagem" maxlength="256" placeholder="Ex.: 🟣 - SEJA MUITO BEM-VINDO(A)!">
      <label class="check"><input type="checkbox" name="foto_bot"> Mostrar a foto de perfil do bot no canto do cartão</label>
    </div>

    <label for="mensagem">Texto da mensagem</label>
    <textarea class="input" id="mensagem" name="texto_mensagem" rows="7" maxlength="4000" placeholder="Escreva a mensagem aqui. Markdown e emojis funcionam." required></textarea>
    <p class="small">Mensagem normal: até 2000 caracteres. Cartão: até 4000. Menções a pessoas, cargos e @everyone não notificam ninguém.</p>

    <label for="imagem_mensagem">Imagem (opcional)</label>
    <input class="input" type="file" id="imagem_mensagem" name="imagem_mensagem" accept="image/png,image/jpeg,image/gif,image/webp">

    <div class="acoes">
      <button class="btn green" type="submit">🚀 Enviar mensagem</button>
      <a class="btn gray" href="{{ url_for('index') }}">🔄 Recarregar canais</a>
    </div>
  </form>
  {% else %}
  <p>O bot não está em nenhum servidor acessível ou ainda está conectando.</p>
  {% endif %}
</section>

<section class="card">
  <h2>🎮 Comando !ip</h2>
  {{ interruptor('comandos_custom_ativos', config.comandos_custom_ativos, 'LIGADO', 'DESLIGADO') }}
  <p class="small">O endereço enviado pelo comando é definido pela variável de ambiente MINECRAFT_IP.</p>
</section>
</main>

<script>
const servidores = {{ servidores | tojson }};
const servidorSelect = document.getElementById("servidor");
const canalSelect = document.getElementById("canal");
const formatoSelect = document.getElementById("formato_mensagem");
const campoTitulo = document.getElementById("campo_titulo");
const tituloMensagem = document.getElementById("titulo_mensagem");

function atualizarFormato() {
  const usarEmbed = formatoSelect.value === "embed";
  campoTitulo.hidden = !usarEmbed;
  tituloMensagem.required = usarEmbed;
}

function carregarCanais() {
  const servidor = servidores.find(s => s.id === servidorSelect.value);
  canalSelect.innerHTML = "";
  if (!servidor || !servidor.canais.length) {
    canalSelect.add(new Option("Nenhum canal com permissão de envio", ""));
    return;
  }
  canalSelect.add(new Option("Selecione um canal", ""));
  servidor.canais.forEach(c => canalSelect.add(new Option(c.categoria + " / #" + c.nome, c.id)));
}

if (servidorSelect) {
  formatoSelect.addEventListener("change", atualizarFormato);
  servidorSelect.addEventListener("change", carregarCanais);
  atualizarFormato();
  carregarCanais();
}

const rrServidor = document.getElementById("rr_servidor");
const rrCanal = document.getElementById("rr_canal");

function carregarReacao() {
  const servidor = servidores.find(s => s.id === rrServidor.value);
  rrCanal.innerHTML = "";
  rrCanal.add(new Option("Selecione um canal", ""));
  (servidor ? servidor.canais : []).forEach(c => rrCanal.add(new Option(c.categoria + " / #" + c.nome, c.id)));
  document.querySelectorAll(".cargo-rr").forEach(sel => {
    sel.innerHTML = "";
    sel.add(new Option("— sem cargo —", ""));
    (servidor ? servidor.cargos : []).forEach(r => sel.add(new Option(r.nome, r.id)));
  });
}

if (rrServidor) {
  rrServidor.addEventListener("change", carregarReacao);
  carregarReacao();
}
</script>
</body>
</html>
"""

ROTAS_PUBLICAS = {"login", "static"}
MAX_TENTATIVAS_LOGIN = 5
JANELA_LOGIN_SEGUNDOS = 300
_tentativas_login = {}
_tentativas_lock = threading.Lock()


def iguais(a, b):
    # compare_digest com str quebra se houver acentos; com bytes funciona sempre.
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def login_bloqueado(ip_cliente):
    agora = time.time()
    with _tentativas_lock:
        recentes = [t for t in _tentativas_login.get(ip_cliente, []) if agora - t < JANELA_LOGIN_SEGUNDOS]
        if recentes:
            _tentativas_login[ip_cliente] = recentes
        else:
            _tentativas_login.pop(ip_cliente, None)
        return len(recentes) >= MAX_TENTATIVAS_LOGIN


def registrar_falha_login(ip_cliente):
    with _tentativas_lock:
        _tentativas_login.setdefault(ip_cliente, []).append(time.time())


def voltar():
    return redirect(url_for("index"))


def erro(mensagem):
    flash(mensagem, "erro")
    return voltar()


def sucesso(mensagem):
    flash(mensagem, "ok")
    return voltar()


def listar_servidores():
    """Servidores, canais de texto e cargos que o bot consegue usar."""
    lista = []
    for guild in bot.guilds:
        canais, cargos, cargos_todos, categorias = [], [], [], []
        me = guild.me
        if me is not None:
            for canal in guild.text_channels:
                perms = canal.permissions_for(me)
                if perms.view_channel and perms.send_messages:
                    canais.append({
                        "id": str(canal.id),
                        "nome": canal.name,
                        "categoria": canal.category.name if canal.category else "Sem categoria",
                    })
            for cargo in sorted(guild.roles, key=lambda r: r.position, reverse=True):
                if not cargo.is_default() and not cargo.managed and cargo < me.top_role:
                    cargos.append({"id": str(cargo.id), "nome": cargo.name})
            for cargo in sorted(guild.roles, key=lambda r: r.position, reverse=True):
                if not cargo.is_default():
                    cargos_todos.append({"id": str(cargo.id), "nome": cargo.name})
            for categoria in guild.categories:
                if categoria.permissions_for(me).manage_channels:
                    categorias.append({"id": str(categoria.id), "nome": categoria.name})
        canais.sort(key=lambda c: (c["categoria"].casefold(), c["nome"].casefold()))
        lista.append({"id": str(guild.id), "nome": guild.name, "canais": canais, "cargos": cargos,
                      "cargos_todos": cargos_todos, "categorias": categorias})
    return lista


def validar_canal_form(campo, obrigatorio=False):
    """Devolve o ID do canal enviado, '' se vazio e permitido, ou None se inválido."""
    valor = request.form.get(campo, "").strip()
    if not valor:
        return None if obrigatorio else ""
    if valor.isdigit() and isinstance(bot.get_channel(int(valor)), discord.TextChannel):
        return valor
    return None


def ler_imagem_enviada(campo):
    """Lê e valida uma imagem enviada. Retorna (bytes, extensão) ou None se não enviou."""
    arquivo = request.files.get(campo)
    if arquivo is None or not arquivo.filename:
        return None
    dados = arquivo.read(LIMITE_IMAGEM + 1)
    if len(dados) > LIMITE_IMAGEM:
        raise ValueError("A imagem passou de 8 MB.")
    extensao = detectar_tipo_imagem(dados)
    if extensao is None:
        raise ValueError("Envie uma imagem PNG, JPG, GIF ou WEBP.")
    return dados, extensao


def enviar_no_discord(corrotina, timeout=20):
    """Roda uma corrotina no loop do bot a partir da thread do Flask."""
    futuro = asyncio.run_coroutine_threadsafe(corrotina, bot.loop)
    try:
        return futuro.result(timeout=timeout)
    except Exception:
        futuro.cancel()
        raise


@app.context_processor
def injetar_csrf():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["csrf_token"]}


@app.before_request
def proteger_rotas():
    if request.endpoint in ROTAS_PUBLICAS or request.endpoint is None:
        return None
    if not session.get("autenticado"):
        return redirect(url_for("login"))
    if request.method == "POST":
        enviado = request.form.get("csrf_token", "")
        salvo = session.get("csrf_token", "")
        if not (enviado and salvo and iguais(enviado, salvo)):
            abort(400, description="Token de segurança inválido. Atualize a página e tente novamente.")
    return None


@app.after_request
def cabecalhos_seguranca(resposta):
    resposta.headers["X-Frame-Options"] = "DENY"
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["Referrer-Policy"] = "no-referrer"
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@app.errorhandler(413)
def arquivo_grande(_):
    flash("O envio passou do limite de tamanho (10 MB).", "erro")
    return redirect(url_for("index" if session.get("autenticado") else "login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("autenticado"):
        return voltar()
    if request.method == "POST":
        ip_cliente = request.remote_addr or "desconhecido"
        if login_bloqueado(ip_cliente):
            flash("Muitas tentativas. Aguarde alguns minutos e tente de novo.", "erro")
        elif iguais(request.form.get("senha", ""), PANEL_PASSWORD):
            session.clear()
            session.permanent = True
            session["autenticado"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            with _tentativas_lock:
                _tentativas_login.pop(ip_cliente, None)
            return voltar()
        else:
            registrar_falha_login(ip_cliente)
            log.warning("Tentativa de login com senha incorreta (IP %s).", ip_cliente)
            flash("Senha incorreta.", "erro")
    return render_template_string(LOGIN_HTML)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    cfg = ler_config()
    caminho = caminho_imagem(cfg)
    caminho_ticket = caminho_imagem(cfg, "ticket")
    return render_template_string(
        PAINEL_HTML,
        config=cfg,
        servidores=listar_servidores(),
        bot_online=not bot.is_closed() and bot.is_ready(),
        bot_nome=str(bot.user) if bot.user else "",
        imagem_atual=caminho is not None,
        imagem_versao=int(caminho.stat().st_mtime) if caminho else 0,
        imagem_ticket_atual=caminho_ticket is not None,
        imagem_ticket_versao=int(caminho_ticket.stat().st_mtime) if caminho_ticket else 0,
        paineis_reacao=descrever_paineis_reacao(cfg),
    )


@app.route("/imagem/<string:slot>")
def imagem_painel(slot):
    if slot not in SLOTS_IMAGEM:
        abort(404)
    caminho = caminho_imagem(ler_config(), slot)
    if caminho is None:
        abort(404)
    return send_from_directory(IMAGENS_DIR, caminho.name)


@app.route("/alternar/<string:chave>", methods=["POST"])
def alternar(chave):
    if chave not in CHAVES_LIGA_DESLIGA:
        abort(404)
    alternar_config(chave)
    return sucesso("Configuração atualizada.")


@app.route("/salvar-boas-vindas", methods=["POST"])
def salvar_boas_vindas():
    titulo = request.form.get("boas_vindas_titulo", "").strip()
    mensagem = request.form.get("boas_vindas_msg", "").strip()
    imagem_url = request.form.get("imagem_url", "").strip()
    canal_id = validar_canal_form("canal_boas_vindas_id", obrigatorio=True)

    if not mensagem:
        return erro("Escreva a mensagem de boas-vindas.")
    if len(mensagem) > 1500 or len(titulo) > 256:
        return erro("Mensagem (até 1500) ou título (até 256) grande demais.")
    if canal_id is None:
        return erro("Canal não encontrado. Atualize a página e escolha de novo.")
    if imagem_url and not url_valida(imagem_url):
        return erro("O link da imagem precisa começar com http:// ou https://.")
    try:
        nova_imagem = ler_imagem_enviada("imagem_arquivo")
    except ValueError as problema:
        return erro(str(problema))

    mudancas = {
        "boas_vindas_titulo": titulo,
        "boas_vindas_msg": mensagem,
        "canal_boas_vindas_id": canal_id,
        "boas_vindas_embed": request.form.get("usar_embed") == "on",
        "boas_vindas_foto_perfil": request.form.get("foto_perfil") == "on",
        "boas_vindas_imagem_url": imagem_url,
    }
    if nova_imagem:
        dados, extensao = nova_imagem
        guardar_imagem("boas_vindas", dados, extensao)
        mudancas["boas_vindas_imagem_arquivo"] = f"boas_vindas{extensao}"
    elif request.form.get("remover_imagem") == "on":
        apagar_imagem("boas_vindas")
        mudancas["boas_vindas_imagem_arquivo"] = ""

    atualizar_config(**mudancas)
    return sucesso("Boas-vindas salvas.")


@app.route("/testar-boas-vindas", methods=["POST"])
def testar_boas_vindas():
    if not bot.is_ready():
        return erro("O bot ainda está conectando. Tente de novo em alguns segundos.")
    cfg = ler_config()
    try:
        canal = bot.get_channel(int(cfg["canal_boas_vindas_id"] or 0))
    except (TypeError, ValueError):
        canal = None
    if not isinstance(canal, discord.TextChannel):
        return erro("Salve um canal de boas-vindas antes de testar.")
    me = canal.guild.me
    if me is None or not canal.permissions_for(me).send_messages:
        return erro("O bot não tem permissão para enviar mensagens nesse canal.")

    try:
        enviar_no_discord(enviar_boas_vindas(canal, me, cfg))
    except Exception:
        log.exception("Falha ao enviar o teste de boas-vindas.")
        return erro("Falha no teste. Confira o terminal e as permissões do bot (Inserir links e Anexar arquivos).")
    return sucesso(f"Teste enviado em #{canal.name}.")


@app.route("/salvar-saida", methods=["POST"])
def salvar_saida():
    mensagem = request.form.get("saida_msg", "").strip()
    canal_id = validar_canal_form("saida_canal_id")
    if not mensagem or len(mensagem) > 1500:
        return erro("Escreva a mensagem de despedida (até 1500 caracteres).")
    if canal_id is None:
        return erro("Canal não encontrado. Atualize a página e escolha de novo.")
    atualizar_config(saida_msg=mensagem, saida_canal_id=canal_id)
    return sucesso("Despedida salva.")


@app.route("/salvar-cargo", methods=["POST"])
def salvar_cargo():
    cargo_id = request.form.get("cargo_id", "").strip()
    if not cargo_id.isdigit():
        return erro("Escolha um cargo.")
    for guild in bot.guilds:
        cargo = guild.get_role(int(cargo_id))
        if cargo is not None and guild.me is not None and cargo < guild.me.top_role and not cargo.managed:
            atualizar_config(cargo_auto_id=cargo_id)
            return sucesso(f"Cargo automático: {cargo.name}.")
    return erro("Cargo não encontrado ou acima do cargo do bot.")


@app.route("/salvar-logs", methods=["POST"])
def salvar_logs():
    canal_id = validar_canal_form("logs_canal_id", obrigatorio=True)
    if canal_id is None:
        return erro("Escolha um canal de logs válido.")
    atualizar_config(logs_canal_id=canal_id)
    return sucesso("Canal de logs salvo.")


def descrever_paineis_reacao(cfg):
    """Dados prontos para mostrar os painéis de cargo por reação no painel web."""
    lista = []
    for painel in cfg.get("cargos_reacao", []):
        try:
            canal = bot.get_channel(int(painel.get("canal_id") or 0))
            guild = bot.get_guild(int(painel.get("guild_id") or 0))
        except (TypeError, ValueError):
            canal = guild = None
        pares = []
        for par in painel.get("pares", []):
            cargo = guild.get_role(int(par["cargo_id"])) if guild else None
            pares.append({"emoji": par.get("emoji", ""), "cargo_nome": cargo.name if cargo else "cargo apagado"})
        lista.append({
            "mensagem_id": painel.get("mensagem_id", ""),
            "titulo": painel.get("titulo", ""),
            "canal_nome": canal.name if canal else "canal indisponível",
            "pares": pares,
        })
    return lista


@app.route("/salvar-ticket", methods=["POST"])
def salvar_ticket():
    titulo = request.form.get("ticket_titulo", "").strip()
    descricao = request.form.get("ticket_descricao", "").strip()
    categorias, vistas = [], set()
    for linha in request.form.get("ticket_categorias", "").splitlines():
        linha = " ".join(linha.split())
        if linha and normalizar(linha) not in vistas:
            vistas.add(normalizar(linha))
            categorias.append(linha)

    if not titulo or len(titulo) > 256:
        return erro("Escreva um título de até 256 caracteres para o painel de tickets.")
    if not descricao or len(descricao) > 2000:
        return erro("Escreva uma descrição de até 2000 caracteres para o painel de tickets.")
    if not 1 <= len(categorias) <= 25 or any(len(c) > 90 for c in categorias):
        return erro("Informe de 1 a 25 categorias, uma por linha, com até 90 caracteres.")

    categoria_id = request.form.get("ticket_categoria_id", "").strip()
    if categoria_id and not (categoria_id.isdigit()
                             and isinstance(bot.get_channel(int(categoria_id)), discord.CategoryChannel)):
        return erro("Categoria do Discord não encontrada. Atualize a página.")
    staff_id = request.form.get("ticket_staff_id", "").strip()
    if staff_id and not (staff_id.isdigit() and any(g.get_role(int(staff_id)) for g in bot.guilds)):
        return erro("Cargo da equipe não encontrado. Atualize a página.")
    logs_id = validar_canal_form("ticket_logs_canal_id")
    if logs_id is None:
        return erro("Canal das transcrições não encontrado. Atualize a página.")
    try:
        nova_imagem = ler_imagem_enviada("ticket_imagem")
    except ValueError as problema:
        return erro(str(problema))

    mudancas = {
        "ticket_titulo": titulo, "ticket_descricao": descricao, "ticket_categorias": categorias,
        "ticket_categoria_id": categoria_id, "ticket_staff_id": staff_id, "ticket_logs_canal_id": logs_id,
    }
    if nova_imagem:
        guardar_imagem("ticket", *nova_imagem)
        mudancas["ticket_imagem_arquivo"] = f"ticket{nova_imagem[1]}"
    elif request.form.get("remover_imagem_ticket") == "on":
        apagar_imagem("ticket")
        mudancas["ticket_imagem_arquivo"] = ""

    atualizar_config(**mudancas)
    return sucesso("Configuração dos tickets salva.")


@app.route("/enviar-painel-ticket", methods=["POST"])
def enviar_painel_ticket_web():
    if not bot.is_ready():
        return erro("O bot ainda está conectando. Tente de novo em alguns segundos.")
    canal_id = validar_canal_form("ticket_painel_canal_id", obrigatorio=True)
    if canal_id is None:
        return erro("Escolha um canal válido para o painel.")
    canal = bot.get_channel(int(canal_id))
    perms = canal.permissions_for(canal.guild.me)
    if not (perms.send_messages and perms.embed_links and perms.attach_files):
        return erro("O bot precisa de Enviar mensagens, Inserir links e Anexar arquivos nesse canal.")
    try:
        enviar_no_discord(enviar_painel_ticket(canal, ler_config()))
    except Exception:
        log.exception("Falha ao enviar o painel de tickets.")
        return erro("Falha ao enviar. Confira o terminal, as permissões do bot e os emojis das categorias.")
    if not ler_config()["ticket_ativo"]:
        flash("Lembrete: o sistema de tickets está desligado. Ligue-o para os membros conseguirem abrir tickets.", "erro")
    return sucesso(f"Painel de tickets enviado em #{canal.name}.")


@app.route("/criar-cargos-reacao", methods=["POST"])
def criar_cargos_reacao():
    if not bot.is_ready():
        return erro("O bot ainda está conectando. Tente de novo em alguns segundos.")
    titulo = request.form.get("titulo", "").strip() or "Escolha seus cargos"
    descricao = request.form.get("descricao", "").strip()
    if len(titulo) > 256 or len(descricao) > 1500:
        return erro("Título (até 256) ou texto (até 1500) grande demais.")
    try:
        guild = bot.get_guild(int(request.form.get("guild_id", "")))
        canal = bot.get_channel(int(request.form.get("canal_id", "")))
    except ValueError:
        return erro("Selecione um servidor e um canal válidos.")
    if guild is None or not isinstance(canal, discord.TextChannel) or canal.guild.id != guild.id:
        return erro("Servidor ou canal não encontrado. Atualize a página.")
    perms = canal.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links
            and perms.add_reactions and perms.read_message_history):
        return erro("O bot precisa de Enviar mensagens, Inserir links, Adicionar reações e Ver histórico nesse canal.")

    pares, vistas = [], set()
    for i in range(8):
        texto_emoji = request.form.get(f"emoji_{i}", "").strip()
        cargo_id = request.form.get(f"cargo_{i}", "").strip()
        if not texto_emoji and not cargo_id:
            continue
        if not texto_emoji or not cargo_id.isdigit():
            return erro(f"Linha {i + 1}: preencha o emoji e o cargo juntos.")
        emoji = discord.PartialEmoji.from_str(texto_emoji)
        if emoji.id is None and texto_emoji.isascii():
            return erro(f"Linha {i + 1}: use um emoji (ex.: 😀) ou um emoji personalizado do servidor.")
        chave = chave_emoji(emoji)
        if chave in vistas:
            return erro(f"Linha {i + 1}: esse emoji já foi usado neste painel.")
        cargo = guild.get_role(int(cargo_id))
        if cargo is None or cargo.managed or cargo.is_default() or cargo >= guild.me.top_role:
            return erro(f"Linha {i + 1}: cargo inválido ou acima do cargo do bot.")
        vistas.add(chave)
        pares.append({"emoji": str(emoji), "chave": chave, "cargo": cargo})
    if not pares:
        return erro("Adicione pelo menos um par de emoji e cargo.")

    async def criar():
        linhas = [f"{p['emoji']}  ›  {p['cargo'].mention}" for p in pares]
        corpo = (descricao + "\n\n" if descricao else "") + "\n".join(linhas)
        embed = discord.Embed(title=titulo, description=corpo, color=COR_ROXO)
        embed.set_footer(text="Reaja para receber o cargo • tire a reação para remover")
        mensagem = await canal.send(embed=embed)
        try:
            for p in pares:
                await mensagem.add_reaction(discord.PartialEmoji.from_str(p["emoji"]))
        except discord.HTTPException:
            await mensagem.delete()
            raise
        return mensagem.id

    try:
        mensagem_id = enviar_no_discord(criar(), timeout=40)
    except Exception:
        log.exception("Falha ao criar o painel de cargos por reação.")
        return erro("Falha ao criar o painel. Confira os emojis (os personalizados precisam ser deste servidor) e as permissões.")

    entrada = {
        "guild_id": str(guild.id), "canal_id": str(canal.id), "mensagem_id": str(mensagem_id), "titulo": titulo,
        "pares": [{"emoji": p["emoji"], "chave": p["chave"], "cargo_id": str(p["cargo"].id)} for p in pares],
    }
    with config_lock:
        atualizar_config(cargos_reacao=config_painel["cargos_reacao"] + [entrada])
    return sucesso(f"Painel de cargos criado em #{canal.name}.")


@app.route("/remover-cargos-reacao", methods=["POST"])
def remover_cargos_reacao():
    mensagem_id = request.form.get("mensagem_id", "").strip()
    with config_lock:
        alvo = next((p for p in config_painel["cargos_reacao"] if p.get("mensagem_id") == mensagem_id), None)
        if alvo is None:
            return erro("Painel não encontrado. Atualize a página.")
        atualizar_config(cargos_reacao=[p for p in config_painel["cargos_reacao"] if p is not alvo])

    async def apagar():
        canal = bot.get_channel(int(alvo["canal_id"]))
        if canal is not None:
            await (await canal.fetch_message(int(mensagem_id))).delete()

    try:
        enviar_no_discord(apagar())
    except Exception:
        pass  # mensagem já apagada ou sem permissão: o painel já foi removido da configuração
    return sucesso("Painel de cargos removido.")


@app.route("/salvar-palavras", methods=["POST"])
def salvar_palavras():
    palavras, vistas = [], set()
    for linha in request.form.get("palavras", "").splitlines():
        palavra = linha.strip()
        chave = normalizar(palavra)
        if palavra and chave not in vistas:
            vistas.add(chave)
            palavras.append(palavra)

    if len(palavras) > 500 or any(len(p) > 50 for p in palavras):
        return erro("Use no máximo 500 palavras, com até 50 caracteres cada.")

    atualizar_config(palavras_bloqueadas=palavras)
    return sucesso(f"{len(palavras)} palavra(s) bloqueada(s) salva(s).")


@app.route("/enviar", methods=["POST"])
def enviar_mensagem():
    texto = request.form.get("texto_mensagem", "").strip()
    titulo = request.form.get("titulo_mensagem", "").strip()
    formato = request.form.get("formato_mensagem", "normal").strip().lower()
    foto_bot = request.form.get("foto_bot") == "on"

    if not bot.is_ready():
        return erro("O bot ainda está conectando. Tente de novo em alguns segundos.")
    if not texto:
        return erro("Digite uma mensagem antes de enviar.")
    if formato not in {"normal", "embed"}:
        return erro("Selecione um formato de mensagem válido.")
    limite = 4000 if formato == "embed" else 2000
    if len(texto) > limite:
        return erro(f"A mensagem passou do limite de {limite} caracteres.")
    if formato == "embed" and (not titulo or len(titulo) > 256):
        return erro("Para o cartão, preencha um título de até 256 caracteres.")
    try:
        imagem = ler_imagem_enviada("imagem_mensagem")
    except ValueError as problema:
        return erro(str(problema))

    try:
        guild = bot.get_guild(int(request.form.get("guild_id", "")))
        canal = bot.get_channel(int(request.form.get("canal_id", "")))
    except ValueError:
        return erro("Selecione um servidor e um canal válidos.")

    if guild is None or not isinstance(canal, discord.TextChannel) or canal.guild.id != guild.id:
        return erro("Servidor ou canal não encontrado. Atualize a página.")
    if guild.me is None:
        return erro("Não foi possível verificar as permissões do bot.")
    perms = canal.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages):
        return erro("O bot não tem permissão para ver ou enviar mensagens nesse canal.")

    async def enviar():
        dados = {"allowed_mentions": discord.AllowedMentions.none()}
        arquivo = None
        if imagem:
            arquivo = discord.File(io.BytesIO(imagem[0]), filename="imagem" + imagem[1])
            dados["file"] = arquivo
        if formato == "embed":
            embed = discord.Embed(title=titulo, description=texto, color=COR_ROXO)
            if foto_bot and bot.user:
                embed.set_thumbnail(url=bot.user.display_avatar.url)
            if arquivo:
                embed.set_image(url="attachment://" + arquivo.filename)
            dados["embed"] = embed
        else:
            dados["content"] = texto
        await canal.send(**dados)

    try:
        enviar_no_discord(enviar())
    except Exception:
        log.exception("Falha ao enviar mensagem pelo painel.")
        return erro("Falha ao enviar. Confira o terminal e as permissões do bot.")

    log.info("Painel enviou mensagem (%s, %d caracteres) em #%s.", formato, len(texto), canal.name)
    return sucesso(f"Mensagem enviada em #{canal.name}.")


def rodar_site():
    # Só acessível neste computador. Não use host="0.0.0.0" sem HTTPS e proteção de rede.
    app.run(host="127.0.0.1", port=PANEL_PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    threading.Thread(target=rodar_site, name="flask-panel", daemon=True).start()
    bot.run(DISCORD_TOKEN, log_handler=None)
