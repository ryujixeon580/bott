# -*- coding: utf-8 -*-
import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from dotenv import load_dotenv
import asyncio
from datetime import datetime, timedelta, timezone
import logging
import traceback
from collections import defaultdict
import json
from urllib.parse import quote
from enum import Enum
from functools import partial
import copy
import sys
import re
import math

# --- CONFIGURAÇÃO INICIAL E CONSTANTES ---

load_dotenv()

# --- TRAVA DE SERVIDOR ÚNICO ---
ALLOWED_GUILD_ID = os.getenv("GUILD_ID") 
if ALLOWED_GUILD_ID:
    ALLOWED_GUILD_ID = int(ALLOWED_GUILD_ID)

# Variaveis de Tempo
TIME_ORDER_TIMEOUT = 12     # Horas para cancelar pedido sem pagamento
TIME_THREAD_CLOSE = 3      # Horas para fechar tópico após confirmação
AUTO_SAVE_INTERVAL = 60     # Segundos para o backup automático do banco

# ID de Admin
HARDCODED_ADMIN_ROLE_ID = 0 

class OrderStatus(Enum):
    PENDING = "Pagamento_pendente"
    CONFIRMED = "Confirmado"
    CANCELLED = "Cancelado"

class StockStatus(Enum):
    AVAILABLE = "disponivel"
    RESERVED = "reservado"
    SOLD = "vendido"
    INFINITE = "infinito"
    ARCHIVED = "arquivado"

# Configuração do logger
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s'))
logger = logging.getLogger('discord_bot')
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(handler)

# --- GERENCIADOR DE BANCO DE DADOS EM MEMÓRIA (SINGLE TENANT) ---
DATA_FILE = "data_single.json"

class InMemoryDatabase:
    def __init__(self, filename):
        self.filename = filename
        self._lock = asyncio.Lock()
        self._db_cache = {
            "config": {
                "cargo_adm_id": None, "cargo_cliente_id": None,
                "canal_feedback_id": None, "pix_chave": "", "pix_nome": "LOJA", 
                "pix_cidade": "BRASIL", "quantidade_por_categoria": {}, "cargo_atendimento_id": None, "admin_commission_percent": 5, "attendance_config": {"channel_id": None, "message_id": None}, "admin_stats": {}, "withdraw_requests": [], "payment_logs": [], "finance": {"total_paid": 0.0}, "painel_config": {"financeiro_channel": None, "financeiro_message_id": None, "ranking_channel": None, "ranking_message_id": None}, "cargo_aprovador_saque": None
            },
            "products": [], 
            "orders": [], 
            "active_catalogs": [], "attendance": {}
        }
        self._dirty = False
        self._initialized = False

    def load_from_disk(self):
        if not os.path.exists(self.filename):
            try:
                with open(self.filename, 'w', encoding='utf-8') as f:
                    json.dump(self._db_cache, f, indent=4, ensure_ascii=False)
            except Exception as e:
                logger.critical(f"Erro ao criar arquivo de dados: {e}")
        else:
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if "guilds" in data:
                        logger.warning("Detectado formato antigo (Multi-Server). Iniciando com banco limpo ou migre manualmente.")
                    else:
                        self._db_cache = data
            except Exception as e:
                logger.error(f"Arquivo corrompido: {e}")
        
        self._initialized = True
        logger.info(f"Database Single-Server carregado. Produtos: {len(self._db_cache['products'])}")

    async def save_to_disk(self):
        if not self._dirty: return
        async with self._lock:
            data_to_save = copy.deepcopy(self._db_cache)
            self._dirty = False
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, partial(self._write_json_sync, data_to_save))

    def _write_json_sync(self, data):
        temp_file = f"{self.filename}.tmp"
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            os.replace(temp_file, self.filename)
        except Exception as e:
            logger.error(f"FALHA AO SALVAR: {e}")

    async def force_save(self):
        self._dirty = True 
        await self.save_to_disk()
    
    # --- Config ---
    async def get_config(self):
        async with self._lock:
            cfg = self._db_cache["config"]
            cfg.setdefault("cargo_atendimento_id", None)
            cfg.setdefault("admin_commission_percent", 5)
            cfg.setdefault("attendance_config", {"channel_id": None, "message_id": None})
            cfg.setdefault("admin_stats", {})
            cfg.setdefault("withdraw_requests", [])
            cfg.setdefault("payment_logs", [])
            cfg.setdefault("finance", {"total_paid": 0.0})
            cfg.setdefault("painel_config", {"financeiro_channel": None, "financeiro_message_id": None, "ranking_channel": None, "ranking_message_id": None})
            cfg.setdefault("cargo_aprovador_saque", None)
            return copy.deepcopy(cfg)

    async def update_config(self, new_config):
        async with self._lock:
            self._db_cache["config"].update(new_config)
            self._dirty = True
        await self.force_save()

    # --- Produtos ---
    async def get_products(self, status_filter=None, exclude_status=None, category=None):
        async with self._lock:
            products = self._db_cache["products"]
            modified = False
            if 'quantidade_por_categoria' not in self._db_cache["config"]:
                self._db_cache["config"]["quantidade_por_categoria"] = {}
                modified = True
            for p in products:
                if 'deliverables' not in p:
                    p['deliverables'] = [p['entregavel']] if p.get('entregavel') else []
                    modified = True
                if 'categoria' not in p:
                    p['categoria'] = "Geral"
                    modified = True
                if 'permite_quantidade' not in p:
                    p['permite_quantidade'] = False
                    modified = True
            if modified: self._dirty = True

            filtered = products
            if status_filter:
                filtered = [p for p in filtered if p['status'] == status_filter] if not isinstance(status_filter, list) else [p for p in filtered if p['status'] in status_filter]
            if exclude_status:
                filtered = [p for p in filtered if p['status'] not in exclude_status]
            if category:
                filtered = [p for p in filtered if p.get('categoria') == category]
            return sorted(copy.deepcopy(filtered), key=lambda x: x['id'])

    async def get_product_by_id(self, product_id):
        async with self._lock:
            for p in self._db_cache["products"]:
                if str(p['id']) == str(product_id):
                    return copy.deepcopy(p)
        return None

    async def add_product(self, product_data):
        async with self._lock:
            cat = product_data.get("categoria", "Geral")
            current_count = sum(1 for p in self._db_cache["products"] if p.get("categoria", "Geral") == cat and p.get("status") != StockStatus.ARCHIVED.value)
            if current_count >= 25:
                raise ValueError(f"A categoria '{cat}' atingiu o limite de 25 produtos.")

            existing_ids = [p['id'] for p in self._db_cache['products']]
            new_id = max(existing_ids) + 1 if existing_ids else 1
            product_data['id'] = new_id
            if 'deliverables' not in product_data: product_data['deliverables'] = []
            if 'status' not in product_data: product_data['status'] = StockStatus.AVAILABLE.value
            if 'categoria' not in product_data: product_data['categoria'] = "Geral"
            if 'permite_quantidade' not in product_data: product_data['permite_quantidade'] = False
            self._db_cache['products'].append(product_data)
            self._dirty = True
        await self.force_save()
        return copy.deepcopy(product_data)

    async def delete_product(self, product_id):
        async with self._lock:
            self._db_cache['products'] = [p for p in self._db_cache['products'] if str(p['id']) != str(product_id)]
            self._dirty = True
        await self.force_save()

    async def update_product(self, product_id, update_data):
        async with self._lock:
            updated_prod = None
            for i, p in enumerate(self._db_cache['products']):
                if str(p['id']) == str(product_id):
                    if "categoria" in update_data and p.get("categoria") != update_data["categoria"]:
                        new_cat = update_data["categoria"]
                        current_count = sum(1 for pr in self._db_cache['products'] if pr.get("categoria", "Geral") == new_cat and pr.get("status") != StockStatus.ARCHIVED.value)
                        if current_count >= 25:
                            raise ValueError(f"A categoria '{new_cat}' está cheia.")
                    
                    self._db_cache['products'][i].update(update_data)
                    updated_prod = copy.deepcopy(self._db_cache['products'][i])
                    self._dirty = True
                    break
        if updated_prod: await self.force_save()
        return updated_prod

    # --- Pedidos ---
    async def get_orders(self, status_filter=None, user_id=None):
        async with self._lock:
            orders = self._db_cache["orders"]
            if status_filter:
                 orders = [o for o in orders if o['status'] == status_filter]
            if user_id:
                 orders = [o for o in orders if str(o['user_id']) == str(user_id)]
            return sorted(copy.deepcopy(orders), key=lambda x: x['id'], reverse=True)

    async def get_order_by_id(self, order_id):
        async with self._lock:
            for o in self._db_cache["orders"]:
                if str(o['id']) == str(order_id):
                    return copy.deepcopy(o)
        return None

    async def add_order(self, order_data):
        async with self._lock:
            existing_ids = [o['id'] for o in self._db_cache['orders']]
            new_id = max(existing_ids) + 1 if existing_ids else 1
            order_data['id'] = new_id
            order_data['created_at'] = datetime.now(timezone.utc).isoformat()
            self._db_cache['orders'].append(order_data)
            self._dirty = True
        await self.force_save()
        return order_data

    async def update_order(self, order_id, update_data):
        async with self._lock:
            should_force = False
            for i, o in enumerate(self._db_cache['orders']):
                if str(o['id']) == str(order_id):
                    self._db_cache['orders'][i].update(update_data)
                    self._dirty = True
                    should_force = "status" in update_data
        if should_force: await self.force_save()
        return None
    
    # --- Catálogos ---
    async def add_active_catalog(self, message_id, channel_id, categories):
        async with self._lock:
            self._db_cache["active_catalogs"].append({
                "message_id": message_id, "channel_id": channel_id, "categories": categories
            })
            self._dirty = True
        await self.force_save()

    async def get_active_catalogs(self):
        async with self._lock:
            return copy.deepcopy(self._db_cache["active_catalogs"])

    async def remove_active_catalog(self, message_id):
        async with self._lock:
            self._db_cache["active_catalogs"] = [c for c in self._db_cache["active_catalogs"] if str(c["message_id"]) != str(message_id)]
            self._dirty = True
        await self.force_save()

# Instancia o banco em memória
db = InMemoryDatabase(DATA_FILE)
db.load_from_disk()

# --- UTILS E API HELPERS ---

async def update_catalog_display(bot):
    active_catalogs = await db.get_active_catalogs()
    for cat_data in active_catalogs:
        try:
            channel_id = cat_data.get("channel_id")
            message_id = cat_data.get("message_id")
            categories = cat_data.get("categories", [])
            channel = bot.get_channel(channel_id)
            if not channel: channel = await bot.fetch_channel(channel_id)
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                await db.remove_active_catalog(message_id)
                continue
            products = await db.get_products(exclude_status=[StockStatus.ARCHIVED.value])
            valid_products = []
            for p in products:
                prod_cat = p.get("categoria", "Geral")
                if prod_cat in categories:
                    if p.get('infinito') or (p.get('deliverables') and len(p['deliverables']) > 0):
                        valid_products.append(p)
            view = MultiCategoryCatalogView(valid_products, bot, categories)
            await message.edit(view=view)
        except Exception as e:
            logger.error(f"Erro ao atualizar catalogo msg {message_id}: {e}")

def crc16_ccitt_false(data: str) -> str:
    crc = 0xFFFF
    for byte in data.encode('utf-8'):
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000: crc = (crc << 1) ^ 0x1021
            else: crc <<= 1
    return f"{crc & 0xFFFF:04X}"

def generate_pix_key_dynamic(valor: float, user_name: str, identifier: str, pix_config: dict) -> str:
    chave = pix_config.get('chave') or pix_config.get('pix_chave')
    nome = pix_config.get('nome') or pix_config.get('pix_nome') or 'NOME'
    cidade = pix_config.get('cidade') or pix_config.get('pix_cidade') or 'CIDADE'
    if not chave: return "CHAVE_PIX_NAO_CONFIGURADA"
    txid = f"{identifier}"[:25]
    def format_field(id_str, value):
        value_str = str(value)
        return f"{id_str}{len(value_str):02d}{value_str}"
    payload = "".join([
        format_field("00", "01"),
        format_field("26", format_field("00", "br.gov.bcb.pix") + format_field("01", chave)),
        format_field("52", "0000"),
        format_field("53", "986"),
        format_field("54", f"{valor:.2f}"),
        format_field("58", "BR"),
        format_field("59", nome),
        format_field("60", cidade),
        format_field("62", format_field("05", txid))
    ]) + "6304"
    return payload + crc16_ccitt_false(payload)

def create_success_embed(title, description):
    return discord.Embed(title=f"✅ {title}", description=description, color=discord.Color.green())

def create_error_embed(title, description):
    return discord.Embed(title=f"❌ {title}", description=description, color=discord.Color.red())

async def get_admin_role_id(bot):
    config = await db.get_config()
    return config.get('cargo_adm_id')

async def check_admin_permission(interaction: discord.Interaction, bot) -> bool:
    if ALLOWED_GUILD_ID and interaction.guild_id != ALLOWED_GUILD_ID:
        return False

    if interaction.user.guild_permissions.administrator: return True
    if interaction.user.id == interaction.guild.owner_id: return True
    
    adm_role_id = await get_admin_role_id(bot)
    if adm_role_id and any(r.id == adm_role_id for r in interaction.user.roles): return True
    
    if HARDCODED_ADMIN_ROLE_ID != 0 and any(r.id == HARDCODED_ADMIN_ROLE_ID for r in interaction.user.roles): return True
    
    return False



async def check_staff_permission(interaction: discord.Interaction, bot) -> bool:
    if ALLOWED_GUILD_ID and interaction.guild_id != ALLOWED_GUILD_ID:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    config = await db.get_config()
    adm_role_id = config.get('cargo_adm_id')
    atendimento_role_id = config.get('cargo_atendimento_id')
    role_ids = {r.id for r in interaction.user.roles}
    if adm_role_id and adm_role_id in role_ids:
        return True
    if atendimento_role_id and atendimento_role_id in role_ids:
        return True
    if HARDCODED_ADMIN_ROLE_ID != 0 and HARDCODED_ADMIN_ROLE_ID in role_ids:
        return True
    return False

async def _update_attendance_user(user_id: int, **updates):
    async with db._lock:
        att = db._db_cache.setdefault("attendance", {})
        row = att.setdefault(str(user_id), {
            "status": "offline",
            "clock_in": None,
            "clock_out": None,
            "period_orders": 0,
            "period_value": 0.0,
            "period_commission": 0.0
        })
        row.update(updates)
        db._dirty = True
    await db.force_save()

async def build_attendance_embed(bot):
    async with db._lock:
        attendance = copy.deepcopy(db._db_cache.get("attendance", {}))
    ativos, pausa, offline = [], [], []
    for admin_id, data in attendance.items():
        entrada = "-"
        saida = "-"
        if data.get("clock_in"):
            try:
                entrada = datetime.fromisoformat(data["clock_in"]).astimezone(timezone.utc).strftime("%H:%M")
            except Exception:
                entrada = str(data.get("clock_in"))
        if data.get("clock_out"):
            try:
                saida = datetime.fromisoformat(data["clock_out"]).astimezone(timezone.utc).strftime("%H:%M")
            except Exception:
                saida = str(data.get("clock_out"))
        line = (
            f"<@{admin_id}>\n"
            f"Entrada: `{entrada}` | Saída: `{saida}`\n"
            f"📦 {data.get('period_orders', 0)} | 💰 R$ {data.get('period_value', 0.0):.2f} | 💸 R$ {data.get('period_commission', 0.0):.2f}"
        )
        status = data.get("status", "offline")
        if status == "active":
            ativos.append(line)
        elif status == "paused":
            pausa.append(line)
        else:
            offline.append(line)
    embed = discord.Embed(title="🕒 Painel de Atendimento", description="Status em tempo real da equipe.", color=discord.Color.blue())
    embed.add_field(name="🟢 Ativos", value="\n\n".join(ativos) if ativos else "Nenhum atendente ativo.", inline=False)
    embed.add_field(name="🟡 Em pausa", value="\n\n".join(pausa) if pausa else "Nenhum atendente em pausa.", inline=False)
    embed.add_field(name="🔴 Indisponíveis", value="\n\n".join(offline) if offline else "Nenhum atendente indisponível.", inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    return embed

class AttendanceView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _has_perm(self, interaction):
        return await check_staff_permission(interaction, self.bot)

    @discord.ui.button(label="🟢 Iniciar", style=discord.ButtonStyle.success)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._has_perm(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await _update_attendance_user(interaction.user.id,
            status="active",
            clock_in=datetime.now(timezone.utc).isoformat(),
            clock_out=None,
            period_orders=0,
            period_value=0.0,
            period_commission=0.0
        )
        await interaction.response.send_message("🟢 Turno iniciado.", ephemeral=True)
        await update_attendance_panel(self.bot)

    @discord.ui.button(label="🟡 Pausar", style=discord.ButtonStyle.secondary)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._has_perm(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await _update_attendance_user(interaction.user.id, status="paused")
        await interaction.response.send_message("🟡 Atendimento pausado.", ephemeral=True)
        await update_attendance_panel(self.bot)

    @discord.ui.button(label="🔴 Parar", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._has_perm(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await _update_attendance_user(interaction.user.id,
            status="offline",
            clock_out=datetime.now(timezone.utc).isoformat()
        )
        await interaction.response.send_message("🔴 Turno encerrado.", ephemeral=True)
        await update_attendance_panel(self.bot)

async def update_attendance_panel(bot):
    config = await db.get_config()
    att_cfg = config.get("attendance_config", {})
    channel_id = att_cfg.get("channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return
    embed = await build_attendance_embed(bot)
    view = AttendanceView(bot)
    message_id = att_cfg.get("message_id")
    if message_id:
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(embed=embed, view=view)
            return
        except Exception:
            pass
    msg = await channel.send(embed=embed, view=view)
    await db.update_config({"attendance_config": {"channel_id": channel_id, "message_id": msg.id}})


async def build_finance_embed():
    config = await db.get_config()
    stats = config.get("admin_stats", {})
    total_sales = sum(float(v.get("total_value", 0.0)) for v in stats.values())
    total_balance = sum(float(v.get("balance", 0.0)) for v in stats.values())
    total_paid = float(config.get("finance", {}).get("total_paid", 0.0))
    embed = discord.Embed(title="📊 Painel Financeiro", color=discord.Color.green())
    embed.add_field(name="💰 Total em vendas", value=f"R$ {total_sales:.2f}", inline=True)
    embed.add_field(name="💸 Comissão pendente", value=f"R$ {total_balance:.2f}", inline=True)
    embed.add_field(name="🏦 Total pago", value=f"R$ {total_paid:.2f}", inline=True)
    embed.timestamp = datetime.now(timezone.utc)
    return embed

async def build_ranking_embed():
    config = await db.get_config()
    stats = config.get("admin_stats", {})
    ranking = sorted(stats.items(), key=lambda kv: (float(kv[1].get("total_value", 0.0)), int(kv[1].get("completed", 0))), reverse=True)
    lines = []
    for i, (admin_id, data) in enumerate(ranking[:10], 1):
        lines.append(f"{i}. <@{admin_id}>\n📦 {int(data.get('completed', 0))} pedidos | 💰 R$ {float(data.get('total_value', 0.0)):.2f} | 💸 R$ {float(data.get('balance', 0.0)):.2f}")
    embed = discord.Embed(title="🏆 Ranking de Atendimento", description="\n\n".join(lines) if lines else "Sem dados ainda.", color=discord.Color.gold())
    embed.timestamp = datetime.now(timezone.utc)
    return embed

async def update_status_panels(bot):
    config = await db.get_config()
    painel = config.get("painel_config", {})

    async def _up(channel_id, message_id, embed):
        if not channel_id:
            return message_id
        channel = bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                return message_id
        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(embed=embed)
                return msg.id
            except Exception:
                pass
        msg = await channel.send(embed=embed)
        return msg.id

    financeiro_embed = await build_finance_embed()
    ranking_embed = await build_ranking_embed()

    painel["financeiro_message_id"] = await _up(painel.get("financeiro_channel"), painel.get("financeiro_message_id"), financeiro_embed)
    painel["ranking_message_id"] = await _up(painel.get("ranking_channel"), painel.get("ranking_message_id"), ranking_embed)
    await db.update_config({"painel_config": painel})

# --- MODAIS ---

class GenericFieldModal(discord.ui.Modal):
    def __init__(self, field_name, current_value, save_callback, is_long=False, max_len=None):
        super().__init__(title=f"Editar {field_name}")
        self.save_callback = save_callback
        style = discord.TextStyle.long if is_long else discord.TextStyle.short
        is_optional = "Imagem" in field_name
        self.input = discord.ui.TextInput(label=field_name, default=str(current_value) if current_value else "", style=style, max_length=max_len, required=not is_optional)
        self.add_item(self.input)
    async def on_submit(self, interaction: discord.Interaction):
        await self.save_callback(interaction, self.input.value)

class AddDeliverableModal(discord.ui.Modal, title="Adicionar Entregável"):
    def __init__(self, bot, parent_view):
        super().__init__()
        self.bot = bot
        self.parent_view = parent_view
        self.content = discord.ui.TextInput(label="Conteúdo da Entrega", style=discord.TextStyle.paragraph, placeholder="Cole aqui a chave, link ou texto...", required=True, max_length=1024)
        self.add_item(self.content)
    async def on_submit(self, interaction: discord.Interaction):
        await self.parent_view.add_deliverable_callback(interaction, self.content.value)

# --- VIEW SEM PERMISSÃO ---
class NoPermissionView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        
        container = discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay(content="ryu"),
                accessory=discord.ui.Thumbnail(
                    media="RYUFOTO",
                ),
            ),
            discord.ui.TextDisplay(content="Entra no servidor aqui em baixo e dá só uma olhadinha"),
        )
        
        action_row = discord.ui.ActionRow(
            discord.ui.Button(
                url="https://discord.gg/RYUJI",
                style=discord.ButtonStyle.link,
                label="Entrar no servidor",
                emoji="😉",
            ),
        )
        self.add_item(container)
        self.add_item(action_row)

# --- SISTEMA DE ESTOQUE V2 ---
class StockManagerLayout(discord.ui.LayoutView):
    def __init__(self, bot, products_data):
        super().__init__(timeout=None)
        self.bot = bot
        self.products = products_data 
        self.categories = sorted(list(set(p.get("categoria", "Geral") for p in self.products)))
        if not self.categories: self.categories = ["Geral"]
        self.current_category = self.categories[0]
        self.filtered_products = [p for p in self.products if p.get("categoria", "Geral") == self.current_category]
        self.current_product_id = str(self.filtered_products[0]['id']) if self.filtered_products else None
        self.page = 0
        self.ITEMS_PER_PAGE = 25
        self.selected_deliverable_index = None
        self.update_components()

    async def reload_data(self):
        self.products = await db.get_products(exclude_status=[StockStatus.ARCHIVED.value])

    def get_current_product(self):
        if not self.current_product_id: return None
        for p in self.products:
            if str(p['id']) == str(self.current_product_id): return p
        return None

    def update_components(self):
        self.clear_items()
        if len(self.categories) > 0:
            cat_options = [discord.SelectOption(label=cat, value=cat, default=(cat==self.current_category)) for cat in self.categories[:25]]
            self.add_item(discord.ui.ActionRow(discord.ui.Select(custom_id="filter_category", options=cat_options, placeholder="📂 Filtrar por Categoria")))
        product = self.get_current_product()
        if not product:
            self.add_item(discord.ui.TextDisplay(f"### Nenhum produto na categoria '{self.current_category}'. Crie um novo."))
            action_row_ctrl = discord.ui.ActionRow(discord.ui.Button(style=discord.ButtonStyle.success, label="Novo Produto", custom_id="btn_new", emoji="➕"))
            self.add_item(action_row_ctrl)
            return
        
        deliverables = product.get('deliverables', [])
        container1 = discord.ui.Container()
        container1.add_item(discord.ui.Section(discord.ui.TextDisplay(content=f"### {product['nome']}"), accessory=discord.ui.Button(style=discord.ButtonStyle.primary, label="Editar Nome", emoji="✏️", custom_id="edit_name")))
        container1.add_item(discord.ui.Section(discord.ui.TextDisplay(content=f"📂 Categoria: **{product.get('categoria', 'Geral')}**"), accessory=discord.ui.Button(style=discord.ButtonStyle.primary, label="Mudar Categoria", emoji="🏷️", custom_id="edit_category")))
        container1.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        desc_trunc = (product.get('descricao')[:90] + '...') if len(product.get('descricao', '')) > 90 else product.get('descricao', 'Sem descrição')
        container1.add_item(discord.ui.Section(discord.ui.TextDisplay(content=f"📝 {desc_trunc}"), accessory=discord.ui.Button(style=discord.ButtonStyle.primary, label="Editar Desc.", emoji="✏️", custom_id="edit_desc")))
        container1.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        container1.add_item(discord.ui.Section(discord.ui.TextDisplay(content=f"💰 **R$ {product.get('valor', 0.0):.2f}**"), accessory=discord.ui.Button(style=discord.ButtonStyle.primary, label="Editar Preço", emoji="💲", custom_id="edit_price")))
        container1.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        is_infinite = product.get('infinito', False)
        opt_infinite = discord.SelectOption(label="Infinito ♾️", value="inf", default=is_infinite, description="Estoque não acaba")
        opt_finite = discord.SelectOption(label="Consumível (Finito)", value="fin", default=not is_infinite, description="Estoque reduz ao vender")
        select_stock = discord.ui.Select(custom_id="select_stock_type", options=[opt_infinite, opt_finite], placeholder="Tipo de Estoque")
        container1.add_item(discord.ui.ActionRow(select_stock))
        container1.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        container1.add_item(discord.ui.TextDisplay(content="### 📦 Entregáveis"))
        selected_text = "Nenhum entregável selecionado."
        if self.selected_deliverable_index is not None and 0 <= self.selected_deliverable_index < len(deliverables):
            full_text = deliverables[self.selected_deliverable_index]
            selected_text = f"**Item #{self.selected_deliverable_index + 1}:** {full_text}"
        elif not deliverables: selected_text = "Estoque vazio."
        container1.add_item(discord.ui.TextDisplay(content=selected_text))
        deliv_options = []
        for idx, d in enumerate(deliverables[:25]): 
            preview = d[:50] + "..." if len(d) > 50 else d
            is_sel = (idx == self.selected_deliverable_index)
            deliv_options.append(discord.SelectOption(label=f"Item #{idx+1}", value=str(idx), description=preview, default=is_sel))
        if not deliv_options: deliv_options.append(discord.SelectOption(label="Sem itens", value="-1", default=True))
        select_deliv = discord.ui.Select(custom_id="select_deliverable", options=deliv_options, placeholder="Selecione para ver/remover", disabled=(not deliverables))
        container1.add_item(discord.ui.ActionRow(select_deliv))
        btn_add = discord.ui.Button(style=discord.ButtonStyle.success, label="Add", emoji="➕", custom_id="btn_add_deliv")
        btn_rem = discord.ui.Button(style=discord.ButtonStyle.danger, label="Remover", emoji="🗑️", custom_id="btn_rem_deliv", disabled=(self.selected_deliverable_index is None))
        container1.add_item(discord.ui.ActionRow(btn_add, btn_rem))
        container1.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        img_url = product.get('imagem_url')
        if img_url: container1.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(media=img_url, description="Imagem do Produto")))
        else: container1.add_item(discord.ui.TextDisplay("🖼️ **Imagem:** (Opcional)"))
        btn_img = discord.ui.Button(style=discord.ButtonStyle.primary, label="Editar Imagem", emoji="✏️", custom_id="edit_img")
        container1.add_item(discord.ui.ActionRow(btn_img))
        self.add_item(container1)
        total_products = len(self.filtered_products)
        total_pages = (total_products - 1) // self.ITEMS_PER_PAGE + 1
        if self.page >= total_pages: self.page = max(0, total_pages - 1)
        start_idx = self.page * self.ITEMS_PER_PAGE
        end_idx = start_idx + self.ITEMS_PER_PAGE
        current_page_products = self.filtered_products[start_idx:end_idx]
        if total_pages > 1:
            btn_prev = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Anterior", emoji="⬅️", custom_id="btn_prev_page", disabled=(self.page == 0))
            btn_status = discord.ui.Button(style=discord.ButtonStyle.secondary, label=f"Página {self.page + 1}/{total_pages}", disabled=True)
            btn_next = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Próximo", emoji="➡️", custom_id="btn_next_page", disabled=(self.page == total_pages - 1))
            self.add_item(discord.ui.ActionRow(btn_prev, btn_status, btn_next))
        nav_options = []
        for p in current_page_products:
            label = f"#{p['id']} - {p['nome']}"[:100]
            stock_info = "Infinito" if p.get('infinito') else f"Estoque: {len(p.get('deliverables', []))}"
            desc = f"R$ {p['valor']:.2f} | {stock_info}"
            is_selected = str(p['id']) == str(self.current_product_id)
            nav_options.append(discord.SelectOption(label=label, value=str(p['id']), description=desc, default=is_selected))
        if nav_options:
            select_nav = discord.ui.Select(custom_id="select_nav_product", options=nav_options, placeholder=f"Produtos na categoria '{self.current_category}'...")
            self.add_item(discord.ui.ActionRow(select_nav))
        btn_new = discord.ui.Button(style=discord.ButtonStyle.success, label="Novo Produto", custom_id="btn_new")
        btn_dup = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Duplicar", emoji="📑", custom_id="btn_dup")
        btn_del = discord.ui.Button(style=discord.ButtonStyle.danger, label="Excluir", custom_id="btn_del")
        self.add_item(discord.ui.ActionRow(btn_new, btn_dup, btn_del))

    async def add_deliverable_callback(self, interaction: discord.Interaction, content: str):
        if not self.current_product_id: return
        p = self.get_current_product()
        if not p: return
        await interaction.response.defer()
        if 'deliverables' not in p: p['deliverables'] = []
        p['deliverables'].append(content)
        updates = {'deliverables': p['deliverables']}
        if len(p['deliverables']) > 1:
            updates['infinito'] = False
            p['infinito'] = False
        await db.update_product(self.current_product_id, updates)
        await update_catalog_display(self.bot)
        await self.reload_data()
        self.filtered_products = [p for p in self.products if p.get("categoria", "Geral") == self.current_category]
        self.selected_deliverable_index = len(p['deliverables']) - 1
        self.update_components()
        await interaction.edit_original_response(view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        custom_id = interaction.data.get('custom_id', '')
        if custom_id == "filter_category":
            await interaction.response.defer()
            self.current_category = interaction.data['values'][0]
            self.filtered_products = [p for p in self.products if p.get("categoria", "Geral") == self.current_category]
            self.current_product_id = str(self.filtered_products[0]['id']) if self.filtered_products else None
            self.page = 0
            self.selected_deliverable_index = None
            self.update_components()
            await interaction.edit_original_response(view=self)
            return False
        if custom_id == "select_nav_product":
            await interaction.response.defer()
            self.current_product_id = interaction.data['values'][0]
            self.selected_deliverable_index = None
            self.update_components()
            await interaction.edit_original_response(view=self)
            return False
        if custom_id in ["btn_prev_page", "btn_next_page"]:
            await interaction.response.defer()
            self.page += 1 if "next" in custom_id else -1
            self.update_components()
            await interaction.edit_original_response(view=self)
            return False
        if custom_id == "select_deliverable":
            await interaction.response.defer()
            val = interaction.data['values'][0]
            if val != "-1":
                self.selected_deliverable_index = int(val)
                self.update_components()
            await interaction.edit_original_response(view=self)
            return False
        if custom_id == "btn_add_deliv":
            await interaction.response.send_modal(AddDeliverableModal(self.bot, self))
            return False
        if custom_id == "btn_rem_deliv":
            await interaction.response.defer()
            p = self.get_current_product()
            if p and self.selected_deliverable_index is not None:
                try:
                    p['deliverables'].pop(self.selected_deliverable_index)
                    await db.update_product(self.current_product_id, {'deliverables': p['deliverables']})
                    await update_catalog_display(self.bot)
                    await self.reload_data()
                    self.filtered_products = [p for p in self.products if p.get("categoria", "Geral") == self.current_category]
                    self.selected_deliverable_index = None
                    self.update_components()
                except IndexError: pass
            await interaction.edit_original_response(view=self)
            return False
        if custom_id == "select_stock_type":
            await interaction.response.defer()
            is_inf = (interaction.data['values'][0] == "inf")
            await db.update_product(self.current_product_id, {"infinito": is_inf})
            await update_catalog_display(self.bot)
            await self.reload_data()
            self.filtered_products = [p for p in self.products if p.get("categoria", "Geral") == self.current_category]
            self.update_components()
            await interaction.edit_original_response(view=self)
            return False
        if custom_id == "btn_new":
            await interaction.response.defer()
            try:
                new_prod = {"nome": "Novo Produto", "descricao": "Descrição pendente...", "valor": 0.0, "deliverables": [], "imagem_url": "", "infinito": False, "status": StockStatus.AVAILABLE.value, "categoria": self.current_category, "permite_quantidade": False}
                created = await db.add_product(new_prod)
                await self.reload_data()
                self.filtered_products = [p for p in self.products if p.get("categoria", "Geral") == self.current_category]
                self.current_product_id = str(created['id'])
                self.page = (len(self.filtered_products) - 1) // self.ITEMS_PER_PAGE
                self.selected_deliverable_index = None
                self.update_components()
                await interaction.edit_original_response(view=self)
            except ValueError as e: 
                await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)
            return False
        if custom_id == "btn_dup":
            await interaction.response.defer()
            p = self.get_current_product()
            if p:
                try:
                    new_data = p.copy()
                    if 'id' in new_data: del new_data['id']
                    new_data['nome'] = f"{new_data['nome']} (Cópia)"
                    new_data['deliverables'] = list(p.get('deliverables', []))
                    if len(new_data['deliverables']) > 1: new_data['infinito'] = False
                    created = await db.add_product(new_data)
                    await self.reload_data()
                    self.filtered_products = [p for p in self.products if p.get("categoria", "Geral") == self.current_category]
                    self.current_product_id = str(created['id'])
                    self.page = (len(self.filtered_products) - 1) // self.ITEMS_PER_PAGE
                    self.selected_deliverable_index = None
                    await update_catalog_display(self.bot)
                    self.update_components()
                    await interaction.edit_original_response(view=self)
                except ValueError as e: 
                    await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)
            else:
                await interaction.edit_original_response(view=self)
            return False
        if custom_id == "btn_del":
            await interaction.response.defer()
            if self.current_product_id:
                await db.delete_product(self.current_product_id)
                await self.reload_data()
                self.categories = sorted(list(set(p.get("categoria", "Geral") for p in self.products)))
                if not self.categories: self.categories = ["Geral"]
                if self.current_category not in self.categories: self.current_category = self.categories[0]
                self.filtered_products = [p for p in self.products if p.get("categoria", "Geral") == self.current_category]
                self.current_product_id = str(self.filtered_products[0]['id']) if self.filtered_products else None
                self.selected_deliverable_index = None
                await update_catalog_display(self.bot)
                self.update_components()
            await interaction.edit_original_response(view=self)
            return False
        if custom_id.startswith("edit_"):
            p = self.get_current_product()
            if not p: return False
            field_map = {"edit_name": ("Nome", p['nome'], False, 100), "edit_desc": ("Descrição", p.get('descricao', ''), True, 1000), "edit_price": ("Valor (ex: 10.50)", str(p.get('valor', 0.0)), False, 20), "edit_img": ("URL da Imagem", p.get('imagem_url', ''), False, None), "edit_category": ("Categoria", p.get('categoria', 'Geral'), False, 50)}
            conf = field_map.get(custom_id)
            if conf:
                async def save_handler(itx, value):
                    await itx.response.defer()
                    updates = {}
                    try:
                        if custom_id == "edit_name": updates["nome"] = value
                        elif custom_id == "edit_desc": updates["descricao"] = value
                        elif custom_id == "edit_img": updates["imagem_url"] = value
                        elif custom_id == "edit_price": updates["valor"] = float(value.replace(',', '.'))
                        elif custom_id == "edit_category": updates["categoria"] = value.strip()
                        await db.update_product(self.current_product_id, updates)
                        await self.reload_data()
                        self.categories = sorted(list(set(p.get("categoria", "Geral") for p in self.products)))
                        if custom_id == "edit_category": self.current_category = value.strip()
                        self.filtered_products = [p for p in self.products if p.get("categoria", "Geral") == self.current_category]
                        await update_catalog_display(self.bot)
                        self.update_components()
                        await itx.edit_original_response(view=self)
                    except ValueError as e: 
                        await itx.followup.send(f"❌ Erro: {e}", ephemeral=True)
                await interaction.response.send_modal(GenericFieldModal(conf[0], conf[1], save_handler, conf[2], conf[3]))
            return False
        return True

# --- INTERAÇÕES EFÊMERAS ---

class AdminActionsView(discord.ui.View):
    def __init__(self, bot, order_id):
        super().__init__(timeout=180)
        self.bot = bot
        self.order_id = order_id

    @discord.ui.button(label="📥 Reivindicar Pedido", style=discord.ButtonStyle.primary, emoji="🧾")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_staff_permission(interaction, self.bot):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await db.update_order(self.order_id, {"admin_id": interaction.user.id, "claimed_at": datetime.now(timezone.utc).isoformat()})
        await interaction.response.send_message("✅ Pedido reivindicado com sucesso.", ephemeral=True)

    @discord.ui.button(label="Confirmar Pedido", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_staff_permission(interaction, self.bot):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await interaction.response.defer()
        await confirm_order_logic(self.order_id, self.bot)
        try: await interaction.edit_original_response(content="✅ **Pedido processado com sucesso.**", view=None)
        except discord.NotFound: pass

    @discord.ui.button(label="Cancelar Pedido", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_staff_permission(interaction, self.bot):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await interaction.response.defer()
        await cancel_order_logic(self.order_id, "Cancelado via Menu Administrativo", self.bot)
        try: await interaction.edit_original_response(content="✅ **Pedido cancelado com sucesso.**", view=None)
        except discord.NotFound: pass

class PaymentConfirmationView(discord.ui.View):
    def __init__(self, bot, order_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.order_id = order_id
    @discord.ui.button(label="Já paguei", style=discord.ButtonStyle.success, emoji="💸")
    async def paid_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            await db.update_order(self.order_id, {"reminder_target": "admin"})
            button.disabled = True
            button.label = "Notificação Enviada"
            await interaction.edit_original_response(view=self)
            
            order_data = await db.get_order_by_id(self.order_id)
            thread_id = order_data.get("thread_id")
            if thread_id:
                thread = self.bot.get_channel(thread_id)
                if thread:
                    admin_role_id = await get_admin_role_id(self.bot)
                    role_mention = f"<@&{admin_role_id}>" if admin_role_id else "Administradores"
                    await thread.send(f"🔔 {role_mention}, o cliente {interaction.user.mention} informou que **realizou o pagamento**!")

            await interaction.followup.send("✅ Administradores notificados!", ephemeral=True)
        except Exception:
            await interaction.followup.send("Erro ao notificar.", ephemeral=True)

class PaymentComponentsView(discord.ui.LayoutView):
    def __init__(self, bot, order_data, pix_payload, product_img_url=None, admin_role_id=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.order_data = order_data
        self.pix_payload = pix_payload
        self.product_img = product_img_url
        self.admin_role_id = admin_role_id
        
        role_mention = f"<@&{admin_role_id}>" if admin_role_id else "@Admin"
        
        product_name = order_data['produto_nome']
        order_id = order_data['id']
        price_val = order_data['valor']
        product_desc = order_data.get('produto_descricao', 'Sem descrição disponível.')
        
        self.add_item(discord.ui.TextDisplay(content=f"### 🔔 Atenção! {role_mention}"))
        gallery_items = []
        if self.product_img: gallery_items.append(discord.MediaGalleryItem(media=self.product_img, description="Imagem do Produto"))
        media_gallery = discord.ui.MediaGallery(*gallery_items) if gallery_items else None
        
        price_button = discord.ui.Button(style=discord.ButtonStyle.success, label=f"R$ {price_val:.2f}", custom_id=f"price_display_{order_id}", disabled=False)
        container = discord.ui.Container(discord.ui.Section(discord.ui.TextDisplay(content=f"### 📦 {product_name} - #{order_id}"), accessory=price_button), discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small), discord.ui.TextDisplay(content=f"📝 **Descrição do Produto**\n{product_desc}"),)
        if media_gallery: container.add_item(media_gallery)
        self.add_item(container)
        action_row = discord.ui.ActionRow(discord.ui.Button(style=discord.ButtonStyle.secondary, label="PIX Copia e Cola", emoji="📋", custom_id="btn_pix_copy"), discord.ui.Button(style=discord.ButtonStyle.secondary, label="Gerar QR Code", emoji="📷", custom_id="btn_qr_code"), discord.ui.Button(style=discord.ButtonStyle.primary, emoji="⚙️", custom_id="btn_admin_gear"))
        self.add_item(action_row)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        custom_id = interaction.data.get('custom_id', '')
        if custom_id.startswith("price_display_"): await interaction.response.defer(); return False
        if custom_id == "btn_pix_copy": await self.handle_pix_copy(interaction); return False
        if custom_id == "btn_qr_code": await self.handle_qr_code(interaction); return False
        if custom_id == "btn_admin_gear": await self.handle_admin_gear(interaction); return False
        return True

    async def handle_pix_copy(self, interaction: discord.Interaction):
        view = PaymentConfirmationView(self.bot, self.order_data['id'])
        msg = self.pix_payload
        await interaction.response.send_message(content=msg, view=view, ephemeral=True)

    async def handle_qr_code(self, interaction: discord.Interaction):
        encoded_payload = quote(self.pix_payload)
        qr_url = f"https://quickchart.io/qr?text={encoded_payload}&size=300"
        embed = discord.Embed(title="📷 QR Code PIX", description="Escaneie com o app do seu banco.", color=discord.Color(0xFFFFFF))
        embed.set_image(url=qr_url)
        view = PaymentConfirmationView(self.bot, self.order_data['id'])
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def handle_admin_gear(self, interaction: discord.Interaction):
        if not await check_staff_permission(interaction, self.bot): return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
        view = AdminActionsView(self.bot, self.order_data['id'])
        await interaction.response.send_message(content=f"⚙️ **Gerenciamento do Pedido #{self.order_data['id']}**\nSelecione uma ação:", view=view, ephemeral=True)

# --- CONFIG VIEWS ---
class GeneralSetupLayout(discord.ui.LayoutView):
    def __init__(self, bot_instance, config_data):
        super().__init__()
        self.bot = bot_instance
        self.config = config_data
        self.ID_ADMIN_TEXT = 101
        self.ID_CLIENT_TEXT = 102
        self.ID_FEEDBACK_TEXT = 103
        def fmt(key, prefix):
            val = self.config.get(key)
            return f"<{prefix}{val}>" if val else "`Não Configurado`"
        self.admin_select = discord.ui.RoleSelect(placeholder="Nome do cargo", min_values=1, max_values=1)
        self.admin_select.callback = self.on_admin_select
        self.client_select = discord.ui.RoleSelect(placeholder="Nome do cargo", min_values=1, max_values=1)
        self.client_select.callback = self.on_customer_select
        self.feedback_select = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text], placeholder="Nome do canal", min_values=1, max_values=1)
        self.feedback_select.callback = self.on_feedback_select
        self.container = discord.ui.Container(discord.ui.TextDisplay("# ⚙️ Configuração Geral"), discord.ui.Separator(spacing=discord.SeparatorSpacing.small), discord.ui.TextDisplay(f"## 🛡️ Cargo Admin: {fmt('cargo_adm_id', '@&')}", id=self.ID_ADMIN_TEXT), discord.ui.TextDisplay("Defina o cargo que poderá usar os comandos da Loja"), discord.ui.ActionRow(self.admin_select), discord.ui.Separator(spacing=discord.SeparatorSpacing.small), discord.ui.TextDisplay(f"## 👤 Cargo Cliente: {fmt('cargo_cliente_id', '@&')}", id=self.ID_CLIENT_TEXT), discord.ui.TextDisplay("Defina o cargo que o cliente irá receber apos a compra"), discord.ui.ActionRow(self.client_select), discord.ui.Separator(spacing=discord.SeparatorSpacing.small), discord.ui.TextDisplay(f"## ⭐ Canal Feedback: {fmt('canal_feedback_id', '#')}", id=self.ID_FEEDBACK_TEXT), discord.ui.TextDisplay("Canal para feedback pós-venda"), discord.ui.ActionRow(self.feedback_select), accent_color=discord.Color(10181046))
        self.add_item(self.container)
    async def _update_state(self, interaction: discord.Interaction, key: str, value: int, text_id: int, prefix: str, header: str):
        await interaction.response.defer()
        await db.update_config({key: value})
        text_component = self.find_item(text_id)
        if text_component: text_component.content = f"## {header}: <{prefix}{value}>"
        await interaction.edit_original_response(view=self)
    async def on_admin_select(self, interaction: discord.Interaction): await self._update_state(interaction, 'cargo_adm_id', self.admin_select.values[0].id, self.ID_ADMIN_TEXT, '@&', "🛡️ Cargo Admin")
    async def on_customer_select(self, interaction: discord.Interaction): await self._update_state(interaction, 'cargo_cliente_id', self.client_select.values[0].id, self.ID_CLIENT_TEXT, '@&', "👤 Cargo Cliente")
    async def on_feedback_select(self, interaction: discord.Interaction): await self._update_state(interaction, 'canal_feedback_id', self.feedback_select.values[0].id, self.ID_FEEDBACK_TEXT, '#', "⭐ Canal Feedback")

class PixSetupModal(discord.ui.Modal, title="Configuração PIX"):
    def __init__(self, bot_instance, current_config):
        super().__init__()
        self.bot = bot_instance
        self.add_item(discord.ui.TextInput(label="Chave PIX", default=current_config.get('pix_chave', ''), required=False))
        self.add_item(discord.ui.TextInput(label="Nome do Beneficiário", default=current_config.get('pix_nome', 'Loja'), required=False))
        self.add_item(discord.ui.TextInput(label="Cidade do Beneficiário", default=current_config.get('pix_cidade', 'SAO PAULO'), required=False))
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            new_config = {"pix_chave": self.children[0].value.strip(), "pix_nome": self.children[1].value.strip(), "pix_cidade": self.children[2].value.strip()}
            await db.update_config(new_config)
            await interaction.followup.send(embed=create_success_embed("PIX Atualizado", "Dados bancários configurados com sucesso!"), ephemeral=True)
        except Exception as e:
            logger.error(f"Erro config pix: {e}")
            await interaction.followup.send(embed=create_error_embed("Erro", "Falha ao salvar dados do PIX."), ephemeral=True)

class SetAttendanceRoleModal(discord.ui.Modal, title="Cargo de Atendimento"):
    def __init__(self, bot_instance):
        super().__init__()
        self.bot = bot_instance
        self.add_item(discord.ui.TextInput(label="ID do cargo de atendimento", required=True))
    async def on_submit(self, interaction: discord.Interaction):
        try:
            role_id = int(self.children[0].value.strip())
            await db.update_config({"cargo_atendimento_id": role_id})
            await interaction.response.send_message("✅ Cargo de atendimento configurado.", ephemeral=True)
        except Exception:
            await interaction.response.send_message("❌ ID inválido.", ephemeral=True)

class SetAttendanceChannelModal(discord.ui.Modal, title="Canal do Bate-Ponto"):
    def __init__(self, bot_instance):
        super().__init__()
        self.bot = bot_instance
        self.add_item(discord.ui.TextInput(label="ID do canal de bate-ponto", required=True))
    async def on_submit(self, interaction: discord.Interaction):
        try:
            channel_id = int(self.children[0].value.strip())
            config = await db.get_config()
            att_cfg = config.get("attendance_config", {})
            att_cfg["channel_id"] = channel_id
            await db.update_config({"attendance_config": att_cfg})
            await interaction.response.send_message("✅ Canal de bate-ponto configurado.", ephemeral=True)
            await update_attendance_panel(self.bot)
        except Exception:
            await interaction.response.send_message("❌ ID inválido.", ephemeral=True)



class SetCommissionModal(discord.ui.Modal, title="Comissão ADM"):
    def __init__(self, bot_instance):
        super().__init__()
        self.bot = bot_instance
        self.add_item(discord.ui.TextInput(label="Percentual da comissão", placeholder="Ex: 5", required=True))
    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = float(self.children[0].value.strip().replace(",", "."))
            await db.update_config({"admin_commission_percent": value})
            await interaction.response.send_message(f"✅ Comissão definida em {value:.2f}%.", ephemeral=True)
            await update_status_panels(self.bot)
        except Exception:
            await interaction.response.send_message("❌ Valor inválido.", ephemeral=True)

class SetApproverRoleModal(discord.ui.Modal, title="Cargo aprova saque"):
    def __init__(self, bot_instance):
        super().__init__()
        self.bot = bot_instance
        self.add_item(discord.ui.TextInput(label="ID do cargo aprovador", required=True))
    async def on_submit(self, interaction: discord.Interaction):
        try:
            role_id = int(self.children[0].value.strip())
            await db.update_config({"cargo_aprovador_saque": role_id})
            await interaction.response.send_message("✅ Cargo aprovador configurado.", ephemeral=True)
        except Exception:
            await interaction.response.send_message("❌ ID inválido.", ephemeral=True)

class SetStatusPanelsModal(discord.ui.Modal, title="Painéis de Status"):
    def __init__(self, bot_instance):
        super().__init__()
        self.bot = bot_instance
        self.add_item(discord.ui.TextInput(label="ID canal financeiro", required=False))
        self.add_item(discord.ui.TextInput(label="ID canal ranking", required=False))
    async def on_submit(self, interaction: discord.Interaction):
        try:
            config = await db.get_config()
            painel = config.get("painel_config", {})
            painel["financeiro_channel"] = int(self.children[0].value.strip()) if self.children[0].value.strip() else None
            painel["ranking_channel"] = int(self.children[1].value.strip()) if self.children[1].value.strip() else None
            await db.update_config({"painel_config": painel})
            await interaction.response.send_message("✅ Painéis configurados.", ephemeral=True)
            await update_status_panels(self.bot)
        except Exception:
            await interaction.response.send_message("❌ IDs inválidos.", ephemeral=True)

class ConfigSelectionView(discord.ui.View):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
    @discord.ui.button(label="Configurar Geral (Canais/Cargos)", style=discord.ButtonStyle.primary, emoji="⚙️")
    async def general_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        config_data = await db.get_config()
        view = GeneralSetupLayout(self.bot, config_data)
        await interaction.response.send_message(view=view, ephemeral=True)
    @discord.ui.button(label="Configurar PIX", style=discord.ButtonStyle.success, emoji="💲")
    async def pix_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        config_data = await db.get_config()
        await interaction.response.send_modal(PixSetupModal(self.bot, config_data))

    @discord.ui.button(label="🎧 Cargo Atendimento", style=discord.ButtonStyle.secondary)
    async def attendance_role_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_admin_permission(interaction, self.bot):
            return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
        await interaction.response.send_modal(SetAttendanceRoleModal(self.bot))

    @discord.ui.button(label="🕒 Bate-Ponto", style=discord.ButtonStyle.secondary)
    async def attendance_channel_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_admin_permission(interaction, self.bot):
            return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
        await interaction.response.send_modal(SetAttendanceChannelModal(self.bot))



    @discord.ui.button(label="💸 Comissão ADM", style=discord.ButtonStyle.secondary, row=1)
    async def commission_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_admin_permission(interaction, self.bot):
            return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
        await interaction.response.send_modal(SetCommissionModal(self.bot))

    @discord.ui.button(label="🛡️ Cargo aprova saque", style=discord.ButtonStyle.secondary, row=1)
    async def approver_role_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_admin_permission(interaction, self.bot):
            return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
        await interaction.response.send_modal(SetApproverRoleModal(self.bot))

    @discord.ui.button(label="📊 Painéis de Status", style=discord.ButtonStyle.secondary, row=1)
    async def status_panels_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_admin_permission(interaction, self.bot):
            return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
        await interaction.response.send_modal(SetStatusPanelsModal(self.bot))

# --- CATALOG WIZARD ---
class MultiCategoryCatalogView(discord.ui.View):
    def __init__(self, products: list, bot_instance, categories: list):
        super().__init__(timeout=None)
        self.bot = bot_instance
        self.categories = categories
        self.products = products
        self.setup_select_menus()
    def setup_select_menus(self):
        products_by_cat = defaultdict(list)
        for p in self.products:
            cat = p.get("categoria", "Geral")
            if cat in self.categories: products_by_cat[cat].append(p)
        count = 0
        for cat in self.categories:
            if count >= 5: break 
            prods = products_by_cat.get(cat, [])
            if not prods: continue
            options = []
            for p in sorted(prods, key=lambda x: x['id'])[:25]:
                stock_count = len(p.get('deliverables', []))
                stock_display = "Infinito ♾️" if p.get('infinito') else f"{stock_count} un."
                desc = f"R$ {p['valor']:.2f} | 📦 {stock_display}"
                options.append(discord.SelectOption(label=p['nome'], value=str(p['id']), description=desc, emoji="🛒"))
            if options:
                select = discord.ui.Select(placeholder=f"📂 Escolha um produto de {cat} e efetue a compra.", min_values=1, max_values=1, options=options, custom_id=f"cat_sel_{cat}"[:100])
                select.callback = self.select_callback
                self.add_item(select)
                count += 1
    async def select_callback(self, interaction: discord.Interaction):
        selected_value = interaction.data['values'][0]
        try:
            product_data = await db.get_product_by_id(selected_value)
            if not product_data:
                return await interaction.response.send_message(embed=create_error_embed("Erro", "Produto não encontrado."), ephemeral=True)

            # Robux precisa abrir Modal como resposta inicial da interação.
            # Por isso ele vem antes do defer/edit do catálogo.
            if is_robux_product(product_data):
                return await interaction.response.send_modal(RobuxQuantityModal(self.bot, product_data))

            await interaction.response.defer()
            all_prods = await db.get_products(exclude_status=[StockStatus.ARCHIVED.value])
            new_view = MultiCategoryCatalogView(all_prods, self.bot, self.categories)
            await interaction.edit_original_response(view=new_view)

            if await product_allows_quantity(product_data):
                view = QuantitySelectView(self.bot, product_data)
                await interaction.followup.send(
                    content="🛒 Escolha a quantidade desejada:",
                    view=view,
                    ephemeral=True
                )
            else:
                await handle_purchase(interaction, product_data, self.bot)
        except Exception as e:
            logger.error(f"Erro callback catalogo: {e}")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(embed=create_error_embed("Erro", f"Falha ao processar: {e}"), ephemeral=True)
                else:
                    await interaction.response.send_message(embed=create_error_embed("Erro", f"Falha ao processar: {e}"), ephemeral=True)
            except Exception:
                pass

class CatalogDetailsModal(discord.ui.Modal, title="Detalhes do Catálogo"):
    def __init__(self, bot_instance, selected_categories):
        super().__init__()
        self.bot = bot_instance
        self.selected_cats = selected_categories
        self.add_item(discord.ui.TextInput(label="Título do Embed", default="🛍️ Catálogo de Vendas"))
        self.add_item(discord.ui.TextInput(label="Descrição", style=discord.TextStyle.long, default="Selecione abaixo o produto que deseja adquirir."))
        self.add_item(discord.ui.TextInput(label="URL da Imagem (Opcional)", required=False))
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            sales_channel = interaction.channel
            if not sales_channel: return await interaction.followup.send(embed=create_error_embed("Erro", "Não foi possível identificar o canal."), ephemeral=True)
            cats_found = self.selected_cats
            all_prods = await db.get_products(exclude_status=[StockStatus.ARCHIVED.value])
            valid_prods = []
            final_cats = []
            for p in all_prods:
                p_cat = p.get("categoria", "Geral")
                if p_cat in cats_found:
                    if p.get('infinito') or (p.get('deliverables') and len(p['deliverables']) > 0):
                        valid_prods.append(p)
                        if p_cat not in final_cats: final_cats.append(p_cat)
            if not valid_prods: return await interaction.followup.send(embed=create_error_embed("Vazio", "Nenhum produto com estoque encontrado para essas categorias."), ephemeral=True)
            embed = discord.Embed(title=self.children[0].value, description=self.children[1].value, color=discord.Color.blurple())
            if self.children[2].value: embed.set_image(url=self.children[2].value)
            view = MultiCategoryCatalogView(valid_prods, self.bot, final_cats)
            sent_msg = await sales_channel.send(embed=embed, view=view)
            await db.add_active_catalog(sent_msg.id, sales_channel.id, final_cats)
            await interaction.followup.send(embed=create_success_embed("Sucesso!", f"Catálogo criado com: {', '.join(final_cats)}"), ephemeral=True)
        except Exception as e:
            logger.error(f"Erro modal catálogo: {e}")
            await interaction.followup.send(embed=create_error_embed("Erro", "Falha ao enviar."), ephemeral=True)

class CatalogWizardView(discord.ui.View):
    def __init__(self, bot, categories):
        super().__init__(timeout=120)
        self.bot = bot
        self.categories = categories
        self.selected_values = []
        options = [discord.SelectOption(label=c, value=c) for c in categories[:25]]
        max_v = min(5, len(options))
        select = discord.ui.Select(placeholder="Selecione as Categorias (Multiseleção - Máx 5)", min_values=1, max_values=max_v, options=options, custom_id="wizard_select")
        select.callback = self.select_callback
        self.add_item(select)
        btn = discord.ui.Button(label="Continuar", style=discord.ButtonStyle.success, emoji="➡️")
        btn.callback = self.confirm_btn
        self.add_item(btn)
    async def select_callback(self, interaction: discord.Interaction):
        self.selected_values = interaction.data['values']
        await interaction.response.defer()
    async def confirm_btn(self, interaction: discord.Interaction):
        if not self.selected_values: return await interaction.response.send_message("❌ Selecione pelo menos uma categoria.", ephemeral=True)
        await interaction.response.send_modal(CatalogDetailsModal(self.bot, self.selected_values))

class GoToThreadView(discord.ui.View):
    def __init__(self, thread_url: str):
        super().__init__()
        self.add_item(discord.ui.Button(label="Ir para o Pedido", style=discord.ButtonStyle.link, url=thread_url, emoji="🛒"))


def extract_base_amount(product_name: str) -> int:
    match = re.search(r"x([\d\.,]+)", product_name or "")
    if not match:
        return 1
    raw = match.group(1).replace(".", "").replace(",", "")
    try:
        return int(raw)
    except ValueError:
        return 1


async def product_allows_quantity(product: dict) -> bool:
    if "permite_quantidade" in product:
        return bool(product.get("permite_quantidade"))
    config = await db.get_config()
    category_flags = config.get("quantidade_por_categoria", {})
    return bool(category_flags.get(product.get("categoria", "Geral"), False))


def is_robux_product(product: dict) -> bool:
    categoria = str(product.get("categoria", "")).lower()
    nome = str(product.get("nome", "")).lower()
    return "robux" in categoria or "robux" in nome


def calc_gamepass_robux(robux_liquido: int) -> int:
    # Roblox fica com 30%; o vendedor recebe aproximadamente 70%.
    return math.ceil(robux_liquido / 0.70)


def calc_robux_price_brl(robux_liquido: int) -> float:
    # Tabela combinada na loja. Para valores fora da tabela, usa proporção do pacote de 1.000.
    tabela = {
        100: 4.49,
        500: 23.49,
        1000: 46.49,
        5000: 231.99,
    }
    if robux_liquido in tabela:
        return tabela[robux_liquido]
    preco_por_robux = tabela[1000] / 1000
    return round(robux_liquido * preco_por_robux, 2)


def get_progressive_discount(mult: int) -> int:
    if mult <= 1:
        return 0
    if mult == 2:
        return 3
    if mult == 3:
        return 5
    if mult == 4:
        return 6
    if mult == 5:
        return 8
    return min(8 + (mult - 5), 23)


class ConfirmQuantityView(discord.ui.View):
    def __init__(self, bot, product, multiplier, final_price, subtotal, discount_percent, final_amount):
        super().__init__(timeout=60)
        self.bot = bot
        self.product = product
        self.multiplier = multiplier
        self.final_price = final_price
        self.subtotal = subtotal
        self.discount_percent = discount_percent
        self.final_amount = final_amount

    @discord.ui.button(label="Confirmar compra", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        product_copy = self.product.copy()
        product_copy["valor"] = round(self.final_price, 2)
        product_copy["multiplicador"] = self.multiplier
        product_copy["quantidade_final"] = self.final_amount
        product_copy["produto_base_nome"] = self.product.get("nome", "Produto")

        base_desc = self.product.get("descricao", "")
        product_copy["descricao"] = (
            f"{base_desc}\n\n"
            f"📦 **Quantidade final:** {self.final_amount:,}".replace(",", ".")
        )

        await handle_purchase(interaction, product_copy, self.bot)

    @discord.ui.button(label="Escolher outra quantidade", style=discord.ButtonStyle.secondary, emoji="🔁")
    async def change_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = QuantitySelectView(self.bot, self.product)
        await interaction.response.edit_message(
            content="🛒 Escolha a quantidade desejada:",
            embed=None,
            view=view
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="❌ Compra cancelada.",
            embed=None,
            view=None
        )


class QuantitySelectView(discord.ui.View):
    def __init__(self, bot, product):
        super().__init__(timeout=60)
        self.bot = bot
        self.product = product

        option_values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20]
        special_labels = {5: "5x ⭐", 10: "10x 🔥", 20: "20x 💎"}
        options = []
        for i in option_values:
            discount = get_progressive_discount(i)
            label = special_labels.get(i, f"{i}x")
            desc = "Sem desconto" if i == 1 else f"{discount}% de desconto"
            options.append(discord.SelectOption(label=label, value=str(i), description=desc))

        select = discord.ui.Select(
            placeholder="Escolha a quantidade",
            options=options,
            custom_id=f"quantity_select_{product['id']}"
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        mult = int(interaction.data["values"][0])
        base_price = float(self.product["valor"])
        discount = get_progressive_discount(mult)

        subtotal = base_price * mult
        final_price = subtotal * (1 - discount / 100)
        savings = subtotal - final_price
        base_amount = extract_base_amount(self.product.get("nome", ""))
        final_amount = base_amount * mult

        stock_text = "♾️ Estoque infinito"
        if not self.product.get("infinito", False):
            stock_count = len(self.product.get("deliverables", []))
            stock_text = f"📦 Estoque atual: {stock_count}"

        embed = discord.Embed(
            title="🛒 Confirmar compra",
            description=f"Você está prestes a comprar **{self.product['nome']}**",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Quantidade escolhida", value=f"**{mult}x**", inline=True)
        embed.add_field(name="Você vai receber", value=f"**{final_amount:,}**".replace(",", "."), inline=True)
        embed.add_field(name="Desconto", value="**Sem desconto**" if discount == 0 else f"**{discount}% de desconto**", inline=True)
        embed.add_field(name="Estoque", value=stock_text, inline=True)
        embed.add_field(name="Subtotal", value=f"R$ {subtotal:.2f}", inline=True)
        embed.add_field(name="Você economiza", value=f"R$ {savings:.2f}", inline=True)
        embed.add_field(name="Total final", value=f"**R$ {final_price:.2f}**", inline=False)
        embed.set_footer(text="Confira os valores antes de confirmar a compra.")

        preview_view = ConfirmQuantityView(
            self.bot,
            self.product,
            mult,
            round(final_price, 2),
            round(subtotal, 2),
            discount,
            final_amount
        )

        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=preview_view
        )

def is_valid_gamepass_url(url: str) -> bool:
    url = (url or "").strip()
    if not url:
        return False
    lowered = url.lower()
    if "roblox.com" not in lowered:
        return False
    allowed_parts = [
        "/game-pass/",
        "/gamepasses/",
        "game-pass",
        "gamepass",
        "passes"
    ]
    return any(part in lowered for part in allowed_parts)


class GamePassLinkModal(discord.ui.Modal, title="Enviar GamePass"):
    def __init__(self, bot, product):
        super().__init__()
        self.bot = bot
        self.product = product
        self.add_item(discord.ui.TextInput(
            label="Link da sua GamePass",
            placeholder="Cole aqui o link da GamePass criada no Roblox",
            required=True,
            max_length=300
        ))

    async def on_submit(self, interaction: discord.Interaction):
        gamepass_link = self.children[0].value.strip()

        if not is_valid_gamepass_url(gamepass_link):
            return await interaction.response.send_message(
                "❌ Link inválido. Envie um link de GamePass do Roblox.",
                ephemeral=True
            )

        robux_liquido = self.product.get("robux_liquido")
        robux_gamepass = self.product.get("robux_gamepass")

        extra = (
            f"\n\n🔗 **GamePass enviada pelo cliente:**\n{gamepass_link}"
        )

        product_copy = self.product.copy()
        product_copy["gamepass_link"] = gamepass_link
        product_copy["descricao"] = f"{product_copy.get('descricao', '')}{extra}"
        product_copy["entregavel_custom"] = (
            f"🪙 Robux líquidos: {robux_liquido}\n"
            f"🎟️ Cliente deve criar GamePass de: {robux_gamepass} Robux\n"
            f"🔗 Link da GamePass: {gamepass_link}\n"
            f"📉 Taxa Roblox considerada: 30%"
        )

        await handle_purchase(interaction, product_copy, self.bot)


class ConfirmRobuxView(discord.ui.View):
    def __init__(self, bot, product):
        super().__init__(timeout=60)
        self.bot = bot
        self.product = product

    @discord.ui.button(label="Enviar GamePass", style=discord.ButtonStyle.success, emoji="🔗")
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GamePassLinkModal(self.bot, self.product))

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Compra cancelada.", embed=None, view=None)


class RobuxQuantityModal(discord.ui.Modal, title="Comprar Robux"):
    def __init__(self, bot, product):
        super().__init__()
        self.bot = bot
        self.product = product
        self.add_item(discord.ui.TextInput(
            label="Quantos Robux você quer receber?",
            placeholder="Ex: 1000",
            required=True,
            max_length=6
        ))

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.children[0].value.strip().replace(".", "").replace(",", "")
        if not raw.isdigit():
            return await interaction.response.send_message("❌ Digite apenas números.", ephemeral=True)

        robux_liquido = int(raw)
        if robux_liquido < 100:
            return await interaction.response.send_message("❌ O mínimo é 100 Robux.", ephemeral=True)
        if robux_liquido > 10000:
            return await interaction.response.send_message("❌ O máximo é 10.000 Robux.", ephemeral=True)

        robux_gamepass = calc_gamepass_robux(robux_liquido)
        preco = calc_robux_price_brl(robux_liquido)

        desc_extra = (
            f"🪙 **Robux líquidos:** {robux_liquido:,}\n"
            f"🎟️ **Game Pass:** {robux_gamepass:,} Robux\n"
            f"📉 **Taxa Roblox:** 30%"
        ).replace(",", ".")

        embed = discord.Embed(title="🪙 Confirmar compra de Robux", color=discord.Color.gold())
        embed.add_field(name="Você vai receber", value=f"**{robux_liquido:,} Robux**".replace(",", "."), inline=False)
        embed.add_field(name="Game Pass precisa ter", value=f"**{robux_gamepass:,} Robux**".replace(",", "."), inline=False)
        embed.add_field(name="Preço", value=f"**R$ {preco:.2f}**", inline=False)
        embed.set_footer(text="A taxa de 30% do Roblox já foi considerada. Depois, envie o link da GamePass.")

        product_copy = self.product.copy()
        product_copy["valor"] = preco
        product_copy["robux_custom"] = True
        product_copy["robux_liquido"] = robux_liquido
        product_copy["robux_gamepass"] = robux_gamepass
        product_copy["multiplicador"] = 1
        product_copy["nome"] = f"Robux - Receber {robux_liquido:,}".replace(",", ".")
        base_desc = self.product.get("descricao", "")
        product_copy["descricao"] = f"{base_desc}\n\n{desc_extra}" if base_desc else desc_extra
        product_copy["entregavel_custom"] = (
            f"🪙 Robux líquidos: {robux_liquido:,}\n"
            f"🎟️ Cliente deve criar Game Pass de: {robux_gamepass:,} Robux\n"
            f"📉 Taxa Roblox considerada: 30%"
        ).replace(",", ".")

        await interaction.response.send_message(embed=embed, view=ConfirmRobuxView(self.bot, product_copy), ephemeral=True)


# --- BUSINESS LOGIC ---

async def handle_purchase(interaction: discord.Interaction, product: dict, bot):
    mult = int(product.get("multiplicador", 1))
    user_orders = await db.get_orders(status_filter=OrderStatus.PENDING.value, user_id=interaction.user.id)
    if user_orders:
        existing_order = user_orders[0]
        guild_id_fix = interaction.guild_id
        thread_url = f"https://discord.com/channels/{guild_id_fix}/{existing_order.get('thread_id', '')}"
        embed = discord.Embed(title="⚠️ Você já tem um pedido em aberto!", description="Conclua o anterior antes.", color=discord.Color.orange())
        if not interaction.response.is_done(): await interaction.response.send_message(embed=embed, view=GoToThreadView(thread_url), ephemeral=True)
        else: await interaction.followup.send(embed=embed, view=GoToThreadView(thread_url), ephemeral=True)
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)

    reserved_item = None
    base_product = await db.get_product_by_id(product['id'])
    if not base_product:
        return await interaction.followup.send(embed=create_error_embed("Erro", "Produto não encontrado."), ephemeral=True)

    if product.get("robux_custom"):
        reserved_item = product.get("entregavel_custom") or (
            f"🪙 Robux líquidos: {product.get('robux_liquido')}\n"
            f"🎟️ Game Pass: {product.get('robux_gamepass')} Robux"
        )
    elif not base_product.get('infinito'):
        deliverables = base_product.get('deliverables', [])
        if len(deliverables) < mult:
            await update_catalog_display(bot)
            return await interaction.followup.send(
                embed=create_error_embed("Esgotado", f"Estoque insuficiente para {mult}x."),
                ephemeral=True
            )
        reserved_items = []
        for _ in range(mult):
            if deliverables:
                reserved_items.append(deliverables.pop(0))
        if len(reserved_items) < mult:
            if reserved_items:
                deliverables = reserved_items + deliverables
            await db.update_product(base_product['id'], {'deliverables': deliverables})
            await update_catalog_display(bot)
            return await interaction.followup.send(
                embed=create_error_embed("Esgotado", f"Estoque insuficiente para {mult}x."),
                ephemeral=True
            )
        reserved_item = "\n".join(reserved_items)
        await db.update_product(base_product['id'], {'deliverables': deliverables})
        await update_catalog_display(bot)
    else:
        deliverables = base_product.get('deliverables', [])
        if not deliverables:
            return await interaction.followup.send(
                embed=create_error_embed("Erro", "Produto infinito sem conteúdo configurado."),
                ephemeral=True
            )
        reserved_item = "\n".join([deliverables[0]] * mult)

    try:
        config = await db.get_config()
        if not config.get('pix_chave'):
            if not base_product.get('infinito') and reserved_item:
                p = await db.get_product_by_id(base_product['id'])
                if p:
                    rollback_items = reserved_item.split("\n") if reserved_item else []
                    p['deliverables'] = rollback_items + p.get('deliverables', [])
                    await db.update_product(p['id'], {'deliverables': p['deliverables']})
            return await interaction.followup.send(embed=create_error_embed("Erro", "Chave PIX da loja não configurada."), ephemeral=True)

        order_data = {
            "produto_id": base_product['id'],
            "produto_nome": product['nome'],
            "produto_descricao": product.get('descricao', base_product.get('descricao', '')),
            "user_name": interaction.user.name,
            "user_id": interaction.user.id,
            "valor": product['valor'],
            "status": OrderStatus.PENDING.value,
            "entregavel": reserved_item,
            "multiplicador": mult,
            "robux_custom": bool(product.get("robux_custom")),
            "robux_liquido": product.get("robux_liquido"),
            "robux_gamepass": product.get("robux_gamepass"),
            "gamepass_link": product.get("gamepass_link")
        }
        new_order = await db.add_order(order_data)
        thread = await interaction.channel.create_thread(name=f"Pedido #{new_order['id']} - {interaction.user.name}", type=discord.ChannelType.private_thread)
        await thread.add_user(interaction.user)
        await db.update_order(new_order['id'], {"thread_id": thread.id})
        pix_key = generate_pix_key_dynamic(new_order['valor'], interaction.user.name, str(new_order['id']), config)
        image_url = base_product.get('imagem_url')
        admin_role = await get_admin_role_id(bot)
        view = PaymentComponentsView(bot, new_order, pix_key, image_url, admin_role_id=admin_role)
        await thread.send(view=view)
        jump_view = GoToThreadView(thread.jump_url)
        try:
            await interaction.user.send(content=f"Pedido #{new_order['id']} criado! Clique abaixo.", view=jump_view)
        except discord.Forbidden:
            pass
        await interaction.followup.send(embed=create_success_embed("Sucesso", "Canal de pagamento criado."), view=jump_view, ephemeral=True)
    except Exception as e:
        logger.error(f"Erro processar compra: {e}")
        if not base_product.get('infinito') and reserved_item:
            p = await db.get_product_by_id(base_product['id'])
            if p:
                rollback_items = reserved_item.split("\n") if reserved_item else []
                p['deliverables'] = rollback_items + p.get('deliverables', [])
                await db.update_product(p['id'], {'deliverables': p['deliverables']})
            await update_catalog_display(bot)
        if not interaction.response.is_done():
            await interaction.followup.send(embed=create_error_embed("Erro", "Ocorreu um erro."), ephemeral=True)

async def confirm_order_logic(order_id: int, bot):
    try:
        order = await db.get_order_by_id(order_id)
        if not order or order['status'] != OrderStatus.PENDING.value: return
        await db.update_order(order_id, {"status": OrderStatus.CONFIRMED.value, "confirmed_at": datetime.now(timezone.utc).isoformat()})
        # Comissão e período do atendente que reivindicou
        config = await db.get_config()
        admin_id = order.get("admin_id")
        if admin_id:
            percent = config.get("admin_commission_percent", 5)
            commission = float(order.get("valor", 0)) * (float(percent) / 100.0)
            async with db._lock:
                cfg = db._db_cache.setdefault("config", {})
                stats = cfg.setdefault("admin_stats", {})
                admin = stats.setdefault(str(admin_id), {
                    "total_orders": 0,
                    "total_value": 0.0,
                    "completed": 0,
                    "cancelled": 0,
                    "total_time": 0.0,
                    "balance": 0.0
                })
                admin["total_orders"] += 1
                admin["total_value"] += float(order.get("valor", 0))
                admin["completed"] += 1
                admin["balance"] += commission

                attendance = db._db_cache.setdefault("attendance", {})
                att = attendance.get(str(admin_id))
                if att and att.get("status") == "active":
                    att["period_orders"] = att.get("period_orders", 0) + 1
                    att["period_value"] = float(att.get("period_value", 0.0)) + float(order.get("valor", 0))
                    att["period_commission"] = float(att.get("period_commission", 0.0)) + commission
                db._dirty = True
            await db.force_save()
            try:
                await update_attendance_panel(bot)
            except Exception:
                pass
            try:
                await update_status_panels(bot)
            except Exception:
                pass
        
        guild = None
        if ALLOWED_GUILD_ID:
            guild = bot.get_guild(ALLOWED_GUILD_ID)
        if not guild and bot.guilds:
            guild = bot.guilds[0]
            
        if not guild: return
        
        member = None
        user_to_dm = None
        mention = f"<@{order['user_id']}>"
        try:
            member = await guild.fetch_member(order['user_id'])
            if member:
                mention = member.mention
                user_to_dm = member
                config = await db.get_config()
                customer_role_id = config.get('cargo_cliente_id')
                if customer_role_id:
                    customer_role = guild.get_role(customer_role_id)
                    if customer_role: await member.add_roles(customer_role, reason="Compra confirmada.")
        except discord.NotFound:
            try: user_to_dm = await bot.fetch_user(order['user_id'])
            except: pass
        embed_entrega = discord.Embed(title="📦 Compra Aprovada!", description=f"Obrigado {mention}, Voce ira receber", color=discord.Color.brand_green())
        embed_entrega.add_field(name=f"{order['produto_nome']}", value=f"```\n{order['entregavel']}\n```", inline=False)
        config = await db.get_config()
        feedback_channel_id = config.get('canal_feedback_id')
        feedback_url = f"https://discord.com/channels/{guild.id}/{feedback_channel_id}" if feedback_channel_id else None
        view_entrega = discord.ui.View()
        if feedback_channel_id and feedback_url: view_entrega.add_item(discord.ui.Button(label="⭐ Deixar Feedback", style=discord.ButtonStyle.link, url=feedback_url))
        thread = bot.get_channel(order.get('thread_id'))
        if thread: await thread.send(embed=embed_entrega, view=view_entrega)
        try:
            if user_to_dm: await user_to_dm.send(embed=embed_entrega, view=view_entrega)
        except: pass
        if feedback_channel_id:
            feedback_channel = bot.get_channel(feedback_channel_id)
            if feedback_channel: await feedback_channel.send(embed=discord.Embed(title="Nova Compra Efetuada", description=f"O {mention} comprou **{order['produto_nome']}**. \nQual seu feedback? 🙂", color=2618704))
    except Exception as e: logger.error(f"Erro confirmar pedido {order_id}: {e}")

async def cancel_order_logic(order_id: int, reason: str, bot):
    order = await db.get_order_by_id(order_id)
    if not order or order['status'] != OrderStatus.PENDING.value: return
    await db.update_order(order_id, {"status": OrderStatus.CANCELLED.value})
    product = await db.get_product_by_id(order['produto_id'])
    if product and not product.get('infinito'):
        if order.get('entregavel'):
            product['deliverables'].insert(0, order['entregavel'])
            await db.update_product(product['id'], {'deliverables': product['deliverables']})
            await update_catalog_display(bot)
    thread = None
    catalog_url = None
    if order.get("thread_id"):
        try:
            thread = await bot.fetch_channel(order['thread_id'])
            if thread and thread.parent: catalog_url = thread.parent.jump_url
        except: pass
    try:
        user = await bot.fetch_user(order['user_id'])
        if user:
            embed = discord.Embed(title="😥 Pedido Cancelado", description=f"Seu pedido para o item **{order['produto_nome']}** foi cancelado.\nMas não se preocupe, você pode voltar para nossa loja clicando abaixo", color=discord.Color.orange())
            view = None
            if catalog_url:
                view = discord.ui.View()
                view.add_item(discord.ui.Button(label="Voltar para a loja", style=discord.ButtonStyle.link, url=catalog_url))
            await user.send(embed=embed, view=view)
    except: pass
    if thread:
        try: await thread.delete()
        except: pass


# --- CLASSE PRINCIPAL ---

class MyBot(commands.Bot):
    def __init__(self, config: dict):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.db = db

    async def setup_hook(self):
        logger.info("Executando setup_hook...")
        self.tree.on_error = self.on_app_command_error
        self.bg_save_task = self.loop.create_task(self.auto_save_loop())

        try:
            active_catalogs = await self.db.get_active_catalogs()
            all_prods = await self.db.get_products()
            for cat_info in active_catalogs:
                categories = cat_info.get("categories", [])
                if categories:
                    valid_p = [p for p in all_prods if p.get("categoria", "Geral") in categories]
                    if valid_p: self.add_view(MultiCategoryCatalogView(valid_p, self, categories))
            logger.info("Views persistentes restauradas (Single Server).")
        except Exception as e:
            logger.error(f"Erro restaurando views: {e}")
        
        if ALLOWED_GUILD_ID:
            logger.info(f"Syncing commands para guilda {ALLOWED_GUILD_ID}")
            self.tree.copy_global_to(guild=discord.Object(id=ALLOWED_GUILD_ID))
            await self.tree.sync(guild=discord.Object(id=ALLOWED_GUILD_ID))
        else:
            await self.tree.sync()

    async def on_ready(self):
        logger.info(f'Bot conectado: {self.user}')
        if ALLOWED_GUILD_ID:
            guild = self.get_guild(ALLOWED_GUILD_ID)
            if guild: logger.info(f"Operando em: {guild.name}")
            else: logger.warning(f"AVISO: Guilda {ALLOWED_GUILD_ID} nao encontrada! O bot pode nao responder.")
        
        if not self.manage_orders_lifecycle.is_running(): self.manage_orders_lifecycle.start()
    
    async def auto_save_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(AUTO_SAVE_INTERVAL)
            try: await self.db.save_to_disk()
            except Exception as e: logger.error(f"Erro no Auto-Save: {e}")

    async def close(self):
        logger.info("Desligando bot... Salvando dados...")
        await self.db.force_save()
        await super().close()

    @tasks.loop(minutes=1)
    async def manage_orders_lifecycle(self):
        try:
            now_utc = datetime.now(timezone.utc)
            orders = await self.db.get_orders()
            for order in orders:
                if order['status'] == OrderStatus.PENDING.value:
                    created_at = datetime.fromisoformat(order['created_at'])
                    elapsed_hours = (now_utc - created_at).total_seconds() / 3600
                    if elapsed_hours >= TIME_ORDER_TIMEOUT:
                        await cancel_order_logic(order['id'], "Timeout", self)
                        continue
                elif order['status'] == OrderStatus.CONFIRMED.value:
                    if order.get("thread_closed"): continue
                    confirmed_at_str = order.get("confirmed_at")
                    if not confirmed_at_str: continue
                    confirmed_at = datetime.fromisoformat(confirmed_at_str)
                    hours_since_conf = (now_utc - confirmed_at).total_seconds() / 3600
                    if hours_since_conf >= TIME_THREAD_CLOSE:
                        thread_id = order.get("thread_id")
                        thread = self.get_channel(thread_id)
                        if thread:
                            try:
                                await thread.edit(locked=True, archived=True, reason="Fechamento automático.")
                                await db.update_order(order['id'], {"thread_closed": True})
                            except: pass
        except Exception as e: logger.error(f"Erro lifecycle: {e}")

    @manage_orders_lifecycle.before_loop
    async def before_check(self): await self.wait_until_ready()
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, discord.app_commands.CommandInvokeError):
             if isinstance(error.original, discord.NotFound) and error.original.code == 10062: return
        actual = getattr(error, 'original', error)
        if isinstance(actual, app_commands.errors.MissingRole):
            if not interaction.response.is_done(): await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
            else: await interaction.followup.send(view=NoPermissionView(), ephemeral=True)
        else:
            logger.error(f"Erro AppCommand: {error}")
            try: 
                if not interaction.response.is_done(): await interaction.response.send_message("Erro interno.", ephemeral=True)
            except: pass
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound): return
        logger.error(f"Erro PrefixCommand: {error}")

def load_config():
    required_env_vars = ["DISCORD_TOKEN"]
    env_config = {var.lower(): os.getenv(var) for var in required_env_vars}
    if not all(env_config.values()): raise ValueError("Variáveis .env faltando (DISCORD_TOKEN).")
    return env_config

if __name__ == "__main__":
    try:
        bot_config = load_config()
        bot = MyBot(config=bot_config)

        @bot.tree.command(name="configurar", description="Menu de configuração.")
        async def configurar(interaction: discord.Interaction):
            if not await check_admin_permission(interaction, bot): return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            embed = discord.Embed(title="⚙️ Configuração", description="Selecione abaixo:", color=discord.Color.dark_grey())
            await interaction.followup.send(embed=embed, view=ConfigSelectionView(bot), ephemeral=True)

        @bot.tree.command(name="catalogo", description="Envia o painel de vendas com categorias.")
        async def catalogo(interaction: discord.Interaction):
            if await check_admin_permission(interaction, bot):
                await interaction.response.defer(ephemeral=True)
                all_prods = await db.get_products()
                cats = sorted(list(set(p.get("categoria", "Geral") for p in all_prods)))
                if not cats: return await interaction.followup.send("❌ Nenhuma categoria/produto encontrada.", ephemeral=True)
                view = CatalogWizardView(bot, cats)
                await interaction.followup.send("### 🧙 Criador de Catálogo\nSelecione abaixo as categorias:", view=view, ephemeral=True)
            else: await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)

        @bot.tree.command(name="estoque", description="Gerencia produtos e categorias.")
        async def estoque(interaction: discord.Interaction):
            if await check_admin_permission(interaction, bot):
                await interaction.response.defer(ephemeral=True)
                products = await db.get_products(exclude_status=[StockStatus.ARCHIVED.value])
                view = StockManagerLayout(bot, products)
                await interaction.followup.send(view=view, ephemeral=True)
            else: await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
        
        @bot.tree.command(name="dashboard", description="Vendas e faturamento detalhados.")
        async def dashboard(interaction: discord.Interaction):
            if await check_admin_permission(interaction, bot):
                await interaction.response.defer(ephemeral=True)
                try:
                    orders = await db.get_orders()
                    confirmed = [o for o in orders if o['status'] == OrderStatus.CONFIRMED.value]
                    cancelled = [o for o in orders if o['status'] == OrderStatus.CANCELLED.value]
                    active = [o for o in orders if o['status'] == OrderStatus.PENDING.value]
                    total_sales = sum(o['valor'] for o in confirmed)
                    sales_by_cat = defaultdict(float)
                    for o in confirmed:
                        p = await db.get_product_by_id(o['produto_id'])
                        cat_name = p.get('categoria', 'Desconhecido') if p else 'Excluído'
                        sales_by_cat[cat_name] += o['valor']
                    embed = discord.Embed(title="📊 Dashboard Financeiro", color=discord.Color.dark_purple())
                    embed.add_field(name="💰 Faturamento Total", value=f"R$ {total_sales:.2f}", inline=False)
                    stats = f"✅ Confirmados: {len(confirmed)}\n❌ Cancelados: {len(cancelled)}\n⏳ Ativos: {len(active)}"
                    embed.add_field(name="📦 Pedidos", value=stats, inline=True)
                    cat_text = ""
                    for cat, val in sales_by_cat.items(): cat_text += f"**{cat}**: R$ {val:.2f}\n"
                    if not cat_text: cat_text = "Nenhuma venda."
                    embed.add_field(name="🏷️ Por Categoria", value=cat_text, inline=True)
                    await interaction.followup.send(embed=embed, ephemeral=True)
                except Exception as e: logger.error(f"Dash error: {e}"); await interaction.followup.send("Erro ao gerar dashboard.", ephemeral=True)
            else: await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)


        @bot.tree.command(name="entrartrabalho", description="Inicia seu turno de atendimento.")
        async def entrartrabalho(interaction: discord.Interaction):
            if not await check_staff_permission(interaction, bot):
                return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
            await _update_attendance_user(interaction.user.id,
                status="active",
                clock_in=datetime.now(timezone.utc).isoformat(),
                clock_out=None,
                period_orders=0,
                period_value=0.0,
                period_commission=0.0
            )
            await interaction.response.send_message("🟢 Turno iniciado.", ephemeral=True)
            await update_attendance_panel(bot)

        @bot.tree.command(name="pausartrabalho", description="Pausa seu turno.")
        async def pausartrabalho(interaction: discord.Interaction):
            if not await check_staff_permission(interaction, bot):
                return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
            await _update_attendance_user(interaction.user.id, status="paused")
            await interaction.response.send_message("🟡 Turno pausado.", ephemeral=True)
            await update_attendance_panel(bot)

        @bot.tree.command(name="parartrabalho", description="Encerra seu turno.")
        async def parartrabalho(interaction: discord.Interaction):
            if not await check_staff_permission(interaction, bot):
                return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
            await _update_attendance_user(interaction.user.id, status="offline", clock_out=datetime.now(timezone.utc).isoformat())
            await interaction.response.send_message("🔴 Turno encerrado.", ephemeral=True)
            await update_attendance_panel(bot)

        @bot.tree.command(name="publicarbateponto", description="Publica ou atualiza o painel de bate-ponto.")
        async def publicarbateponto(interaction: discord.Interaction):
            if not await check_admin_permission(interaction, bot):
                return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            await update_attendance_panel(bot)
            await update_status_panels(bot)
            await interaction.followup.send("✅ Painéis publicados/atualizados.", ephemeral=True)


        @bot.tree.command(name="saldoadm", description="Mostra seu saldo de comissão.")
        async def saldoadm(interaction: discord.Interaction):
            if not await check_staff_permission(interaction, bot):
                return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
            config = await db.get_config()
            stats = config.get("admin_stats", {})
            row = stats.get(str(interaction.user.id), {})
            await interaction.response.send_message(f"💸 Seu saldo atual: R$ {float(row.get('balance', 0.0)):.2f}", ephemeral=True)

        @bot.tree.command(name="solicitarsaque", description="Solicita saque do seu saldo.")
        @app_commands.describe(valor="Valor do saque em reais")
        async def solicitarsaque(interaction: discord.Interaction, valor: float):
            if not await check_staff_permission(interaction, bot):
                return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
            config = await db.get_config()
            stats = config.get("admin_stats", {})
            row = stats.get(str(interaction.user.id), {})
            saldo = float(row.get("balance", 0.0))
            if valor <= 0 or valor > saldo:
                return await interaction.response.send_message("❌ Valor inválido ou saldo insuficiente.", ephemeral=True)
            async with db._lock:
                cfg = db._db_cache.setdefault("config", {})
                reqs = cfg.setdefault("withdraw_requests", [])
                reqs.append({"id": len(reqs)+1, "user_id": interaction.user.id, "valor": float(valor), "status": "pending", "created_at": datetime.now(timezone.utc).isoformat()})
                db._dirty = True
            await db.force_save()
            await interaction.response.send_message("✅ Solicitação de saque criada.", ephemeral=True)

        @bot.tree.command(name="aprovarsaque", description="Aprova um saque pendente pelo ID.")
        @app_commands.describe(id="ID da solicitação")
        async def aprovarsaque(interaction: discord.Interaction, id: int):
            config = await db.get_config()
            aprovador = config.get("cargo_aprovador_saque")
            role_ids = {r.id for r in interaction.user.roles}
            if not (interaction.user.guild_permissions.administrator or (aprovador and aprovador in role_ids) or await check_admin_permission(interaction, bot)):
                return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
            async with db._lock:
                cfg = db._db_cache.setdefault("config", {})
                reqs = cfg.setdefault("withdraw_requests", [])
                stats = cfg.setdefault("admin_stats", {})
                target = None
                for req in reqs:
                    if int(req.get("id", 0)) == id and req.get("status") == "pending":
                        target = req
                        break
                if not target:
                    return await interaction.response.send_message("❌ Solicitação não encontrada.", ephemeral=True)
                row = stats.setdefault(str(target["user_id"]), {"total_orders": 0, "total_value": 0.0, "completed": 0, "cancelled": 0, "total_time": 0.0, "balance": 0.0})
                valor_pago = float(target.get("valor", 0.0))
                vendas_resetadas = float(row.get("total_value", 0.0))
                row["balance"] = 0.0
                row["total_value"] = 0.0
                target["status"] = "paid"
                cfg.setdefault("payment_logs", []).append({"user_id": target["user_id"], "valor": valor_pago, "vendas_resetadas": vendas_resetadas, "data": datetime.now(timezone.utc).isoformat(), "aprovado_por": interaction.user.id})
                finance = cfg.setdefault("finance", {"total_paid": 0.0})
                finance["total_paid"] = float(finance.get("total_paid", 0.0)) + valor_pago
                db._dirty = True
            await db.force_save()
            await interaction.response.send_message("✅ Saque aprovado e saldo zerado.", ephemeral=True)
            await update_status_panels(bot)

        @bot.tree.command(name="rankingadms", description="Ranking de atendimento por vendas.")
        async def rankingadms(interaction: discord.Interaction):
            if not await check_admin_permission(interaction, bot):
                return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
            embed = await build_ranking_embed()
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @bot.tree.command(name="atualizarpaineis", description="Atualiza financeiro, ranking e bate-ponto.")
        async def atualizarpaineis(interaction: discord.Interaction):
            if not await check_admin_permission(interaction, bot):
                return await interaction.response.send_message(view=NoPermissionView(), ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            await update_status_panels(bot)
            await update_attendance_panel(bot)
            await interaction.followup.send("✅ Painéis atualizados.", ephemeral=True)

        bot.run(bot_config['discord_token'])
    except Exception as e:
        logger.critical(f"Erro fatal: {e}\n{traceback.format_exc()}")
