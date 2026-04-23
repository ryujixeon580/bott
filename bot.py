import discord
from discord.ext import commands
from datetime import datetime
import json, os

intents = discord.Intents.all()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DB_FILE = "database.json"

def load_db():
    if not os.path.exists(DB_FILE):
        return {"config": {}}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

async def update_dashboard():
    data = load_db()
    config = data.get("config", {})
    dash = config.get("dashboard", {})
    stats = config.get("admin_stats", {})

    total_sales = sum(a.get("total_value", 0) for a in stats.values())
    total_balance = sum(a.get("balance", 0) for a in stats.values())
    total_paid = config.get("finance", {}).get("total_paid", 0)

    embed = discord.Embed(title="📊 Financeiro", color=0x00ff00)
    embed.add_field(name="💰 Vendido", value=f"R$ {total_sales:.2f}")
    embed.add_field(name="💸 Saldo ADM", value=f"R$ {total_balance:.2f}")
    embed.add_field(name="🏦 Pago", value=f"R$ {total_paid:.2f}")

    async def send_or_edit(channel_id, msg_id):
        if not channel_id: return None
        ch = bot.get_channel(channel_id)
        if not ch: return None
        try:
            if msg_id:
                m = await ch.fetch_message(msg_id)
                await m.edit(embed=embed)
                return msg_id
        except: pass
        m = await ch.send(embed=embed)
        return m.id

    dash["finance_message_id"] = await send_or_edit(
        dash.get("finance_channel"), dash.get("finance_message_id")
    )

    config["dashboard"] = dash
    data["config"] = config
    save_db(data)

def add_admin_sale(admin_id, valor):
    data = load_db()
    config = data.setdefault("config", {})
    stats = config.setdefault("admin_stats", {})

    percent = config.get("admin_commission_percent", 5)

    admin = stats.setdefault(str(admin_id), {
        "total_orders": 0,
        "total_value": 0,
        "completed": 0,
        "balance": 0
    })

    admin["total_orders"] += 1
    admin["total_value"] += valor
    admin["completed"] += 1

    commission = valor * (percent/100)
    admin["balance"] += commission

    save_db(data)

@bot.command()
async def setcomissao(ctx, percent: int):
    data = load_db()
    data.setdefault("config", {})["admin_commission_percent"] = percent
    save_db(data)
    await ctx.send(f"Comissão definida para {percent}%")

@bot.command()
async def sacar(ctx, valor: float):
    data = load_db()
    stats = data.get("config", {}).get("admin_stats", {})
    admin = stats.get(str(ctx.author.id))

    if not admin or admin["balance"] < valor:
        return await ctx.send("Saldo insuficiente.")

    req = {
        "id": len(data["config"].setdefault("withdraw_requests", [])) + 1,
        "user_id": ctx.author.id,
        "valor": valor,
        "status": "pending"
    }

    data["config"]["withdraw_requests"].append(req)
    save_db(data)
    await ctx.send("Pedido enviado.")

@bot.command()
async def aprovar(ctx, wid:int):
    data = load_db()
    config = data.get("config", {})
    role = config.get("cargo_aprovador_saque")

    if role not in [r.id for r in ctx.author.roles]:
        return await ctx.send("Sem permissão")

    for w in config.get("withdraw_requests", []):
        if w["id"]==wid and w["status"]=="pending":
            admin = config["admin_stats"][str(w["user_id"])]

            vendas = admin["total_value"]
            admin["balance"]=0
            admin["total_value"]=0

            w["status"]="paid"

            config.setdefault("payment_logs", []).append({
                "user_id": w["user_id"],
                "valor": w["valor"],
                "vendas_resetadas": vendas,
                "data": datetime.now().strftime("%Y-%m-%d %H:%M")
            })

            config.setdefault("finance", {}).setdefault("total_paid",0)
            config["finance"]["total_paid"] += w["valor"]

            save_db(data)
            await ctx.send("Aprovado!")
            await update_dashboard()
            return

    await ctx.send("ID inválido")

import os
bot.run(os.getenv("DISCORD_TOKEN"))
