#!/usr/bin/env python3
"""
珠宝订单协作系统 — Demo v2.0
新增：多用户认证、真实 AI 问答、报价卡片、客户画像、演示数据

启动: python server.py  →  http://localhost:8899
"""

import sqlite3, json, re, os, hashlib, secrets, csv, shutil
from datetime import datetime, date, timedelta
from contextlib import contextmanager, asynccontextmanager
from io import StringIO
from typing import Optional
import random
import urllib.request
import urllib.error
import ssl

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

# ─── 配置 ───
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DEMO_MODE = os.environ.get("PUBLIC_DEMO_MODE", "").lower() == "true"
PUBLIC_DEMO_USER = "demo_viewer"
SEED_DB_PATH = os.path.join(APP_DIR, "jewelry_public_demo.db" if PUBLIC_DEMO_MODE else "jewelry.db")

# Vercel 的部署目录不可作为可持续写入的数据盘。为演示环境把随代码
# 发布的初始数据库复制到可写的临时空间；函数重启后会回到初始演示数据。
if os.environ.get("DATABASE_PATH"):
    DB_PATH = os.environ["DATABASE_PATH"]
elif os.environ.get("VERCEL"):
    DB_PATH = os.path.join("/tmp", "jewelry-crm-demo.db")
    if not os.path.exists(DB_PATH):
        shutil.copy2(SEED_DB_PATH, DB_PATH)
else:
    DB_PATH = SEED_DB_PATH
DEMO_MODE = True  # demo 模式启动时有丰富数据

# ─── AI 提供者配置 ───
AI_CONFIG = {
    "provider": os.environ.get("AI_PROVIDER", "openai_compatible"),
    "api_key": os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", ""),
    "model": os.environ.get("AI_MODEL", "openrouter/auto"),
    "base_url": os.environ.get("AI_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"),
}

DEFAULT_PROFIT_RATE = 0.10
STATUS_FLOW = ["draft","pending_quote","quoting","quoted","reviewed",
               "sent_to_client","confirmed","in_production","delivered"]
STATUS_LABELS = {"draft":"草稿","pending_quote":"待报价","quoting":"报价中",
    "quoted":"已报价","reviewed":"已审核","sent_to_client":"已发客户",
    "confirmed":"已确认","in_production":"制作中","delivered":"已交付"}
STATUS_DESCRIPTIONS = {
    "draft": "内部草稿，需求还没整理完整。",
    "pending_quote": "助理已录入需求，等待师傅核算材料和工费。",
    "quoting": "师傅正在报价或补充成本明细。",
    "quoted": "师傅已提交成本，等待老板审核利润和对客报价。",
    "reviewed": "老板已审核，可以复制报价卡到微信发给客户。",
    "sent_to_client": "已通过微信等外部渠道发给客户，系统只记录这个动作。",
    "confirmed": "客户已确认方案或价格，准备进入制作排期。",
    "in_production": "已经开始制作或采购材料。",
    "delivered": "订单已交付完成，可进入复购维护。",
}
ROLE_PERMISSIONS = {
    "boss": {"ai": True, "ai_config": True, "dashboard_finance": True, "dashboard_charts": True, "cost": True, "final_price": True, "customers": True, "customer_sensitive": True, "users": True, "settings": True, "orders_create": True, "orders_edit": True, "status": True, "quote": True, "payment": True, "price_edit": True, "after_sale": True, "notifications": True},
    "assistant": {"ai": True, "ai_config": False, "dashboard_finance": False, "dashboard_charts": False, "cost": False, "final_price": True, "customers": False, "customer_sensitive": False, "users": False, "settings": False, "orders_create": True, "orders_edit": True, "status": False, "quote": False, "payment": False, "price_edit": False, "after_sale": True, "notifications": True},
    "master": {"ai": False, "ai_config": False, "dashboard_finance": False, "dashboard_charts": False, "cost": False, "final_price": False, "customers": False, "customer_sensitive": False, "users": False, "settings": False, "orders_create": False, "orders_edit": False, "status": False, "quote": True, "payment": False, "price_edit": False, "after_sale": False, "notifications": True},
    "demo_viewer": {"ai": False, "ai_config": False, "dashboard_finance": False, "dashboard_charts": False, "cost": False, "final_price": False, "customers": False, "customer_sensitive": False, "users": False, "settings": False, "orders_create": False, "orders_edit": False, "status": False, "quote": False, "payment": False, "price_edit": False, "after_sale": False, "notifications": False},
}

# ─── 数据库 ───
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn; conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()

def init_db():
    with get_db() as db:
        # 用户表
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            role TEXT NOT NULL DEFAULT 'assistant',
            custom_fields TEXT DEFAULT '{}',
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now','localtime')))""")

        # 订单表
        db.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT UNIQUE NOT NULL,
            customer_name TEXT DEFAULT '', product_type TEXT DEFAULT '',
            metal_type TEXT DEFAULT '', main_stone TEXT DEFAULT '',
            side_stones TEXT DEFAULT '', material_notes TEXT DEFAULT '',
            special_notes TEXT DEFAULT '', cost_total REAL DEFAULT 0,
            profit_rate REAL DEFAULT 0.10, profit REAL DEFAULT 0,
            final_price REAL DEFAULT 0, status TEXT DEFAULT 'draft',
            created_by TEXT DEFAULT 'assistant',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')))""")

        # 报价明细表
        db.execute("""CREATE TABLE IF NOT EXISTS quote_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL, item_name TEXT NOT NULL,
            amount REAL DEFAULT 0, filled_by TEXT DEFAULT 'master',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE)""")
        ensure_columns(db, "quote_items", {
            "quantity": "REAL DEFAULT 1",
            "unit": "TEXT DEFAULT ''",
            "unit_price": "REAL DEFAULT 0",
        })

        # 系统配置表
        db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT DEFAULT '')""")

        # 通知日志表
        db.execute("""CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, title TEXT, message TEXT,
            channel TEXT DEFAULT 'in_app', sent_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (user_id) REFERENCES users(id))""")

        db.execute("""CREATE TABLE IF NOT EXISTS customer_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            age TEXT DEFAULT '',
            gender TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            wechat TEXT DEFAULT '',
            city TEXT DEFAULT '',
            address TEXT DEFAULT '',
            birthday TEXT DEFAULT '',
            preferences TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now','localtime')))""")
        ensure_columns(db, "customer_profiles", {
            "wechat": "TEXT DEFAULT ''",
            "birthday": "TEXT DEFAULT ''",
        })

        db.execute("""CREATE TABLE IF NOT EXISTS status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            from_status TEXT DEFAULT '',
            to_status TEXT NOT NULL,
            changed_by TEXT DEFAULT '',
            changed_at TEXT DEFAULT (datetime('now','localtime')),
            note TEXT DEFAULT '',
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE)""")
        ensure_columns(db, "status_history", {
            "event_type": "TEXT DEFAULT 'status'",
            "is_sensitive": "INTEGER DEFAULT 0",
        })
        db.execute("""UPDATE status_history SET is_sensitive=1, event_type='financial'
            WHERE note LIKE '利润调整：%' OR note LIKE '订单总价调整：%' OR note LIKE '%固定利润%'""")

        db.execute("""CREATE TABLE IF NOT EXISTS after_sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            customer_name TEXT DEFAULT '',
            service_type TEXT DEFAULT '',
            content TEXT DEFAULT '',
            handled_by TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE)""")

        db.execute("""CREATE TABLE IF NOT EXISTS payment_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            category TEXT DEFAULT 'deposit',
            amount REAL DEFAULT 0,
            paid_at TEXT DEFAULT (date('now','localtime')),
            submitted_by TEXT DEFAULT '',
            submitted_at TEXT DEFAULT (datetime('now','localtime')),
            note TEXT DEFAULT '',
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE)""")

        db.execute("""CREATE TABLE IF NOT EXISTS quote_change_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            requested_by TEXT DEFAULT 'master',
            items_json TEXT DEFAULT '[]',
            reason TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            reviewed_by TEXT DEFAULT '',
            reviewed_at TEXT DEFAULT '',
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE)""")

        ensure_columns(db, "orders", {
            "budget": "REAL DEFAULT 0",
            "ring_size": "TEXT DEFAULT ''",
            "due_date": "TEXT DEFAULT ''",
            "customer_source": "TEXT DEFAULT ''",
            "occasion": "TEXT DEFAULT ''",
            "design_brief": "TEXT DEFAULT ''",
            "image_url": "TEXT DEFAULT ''",
            "follow_up_note": "TEXT DEFAULT ''",
            "material_specs": "TEXT DEFAULT '{}'",
            "deposit_amount": "REAL DEFAULT 0",
            "paid_amount": "REAL DEFAULT 0",
            "paid_at": "TEXT DEFAULT ''",
            "payment_note": "TEXT DEFAULT ''",
            "profit_mode": "TEXT DEFAULT 'percent'",
            "profit_fixed": "REAL DEFAULT 0",
        })

def ensure_columns(db, table, columns):
    existing = {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

ROLE_DEFAULTS = {
    "boss": {"name":"老板","fields":["id","order_number","customer_name","product_type","metal_type","main_stone","side_stones","material_notes","special_notes","budget","ring_size","due_date","customer_source","occasion","design_brief","image_url","follow_up_note","material_specs","deposit_amount","paid_amount","paid_at","payment_note","paid_total","balance_amount","is_paid","payment_status","payment_records","cost_total","profit_mode","profit_rate","profit_fixed","profit","final_price","status","created_by","created_at","updated_at","quote_items","status_history","after_sales","quote_change_requests"]},
    "assistant": {"name":"助理","fields":["id","order_number","customer_name","product_type","metal_type","main_stone","side_stones","material_notes","special_notes","budget","ring_size","due_date","customer_source","occasion","design_brief","image_url","follow_up_note","material_specs","final_price","status","created_at","status_history"]},
    "master": {"name":"师傅","fields":["id","order_number","image_url","material_specs","status","created_at","quote_items","status_history","quote_change_requests"]},
    "demo_viewer": {"name":"演示访客","fields":["id","order_number","customer_name","product_type","metal_type","main_stone","side_stones","material_notes","special_notes","due_date","customer_source","occasion","design_brief","image_url","follow_up_note","material_specs","status","created_at","updated_at","status_history"]},
}

# ─── 权限工具 ───
def hash_password(pw): return hashlib.sha256(pw.encode()).hexdigest()
def verify_pw(pw, h): return hash_password(pw) == h

def get_user_fields(role, custom_fields_json="{}"):
    """获取某角色能看到哪些字段"""
    if role in ROLE_DEFAULTS:
        return ROLE_DEFAULTS[role]["fields"]
    # 自定义角色
    try:
        cf = json.loads(custom_fields_json)
        return cf.get("fields", ["id","order_number","customer_name","status","created_at"])
    except:
        return ["id","order_number","customer_name","status","created_at"]

def get_payment_summary(db, order_id, final_price):
    rows = [dict(r) for r in db.execute("SELECT * FROM payment_records WHERE order_id=? ORDER BY paid_at ASC, submitted_at ASC, id ASC",(order_id,)).fetchall()]
    deposit = sum(float(r["amount"] or 0) for r in rows if r["category"] == "deposit")
    paid_total = sum(float(r["amount"] or 0) for r in rows)
    balance = max(round(float(final_price or 0) - paid_total, 2), 0)
    is_paid = float(final_price or 0) > 0 and balance <= 0.01
    paid_at = ""
    if is_paid and rows:
        paid_at = max((r["paid_at"] or "") for r in rows)
    return {
        "payment_records": rows,
        "deposit_amount": round(deposit, 2),
        "paid_amount": round(max(paid_total - deposit, 0), 2),
        "paid_total": round(paid_total, 2),
        "balance_amount": balance,
        "is_paid": is_paid,
        "payment_status": "paid" if is_paid else "unpaid",
        "paid_at": paid_at,
    }

def sync_order_payment_summary(db, order_id):
    row = db.execute("SELECT final_price FROM orders WHERE id=?",(order_id,)).fetchone()
    if not row:
        return
    summary = get_payment_summary(db, order_id, row["final_price"] or 0)
    db.execute("""UPDATE orders SET deposit_amount=?,paid_amount=?,paid_at=?,updated_at=datetime('now','localtime') WHERE id=?""",
               (summary["deposit_amount"], summary["paid_amount"], summary["paid_at"], order_id))

def order_dict(row, role_or_fields="boss"):
    if not row: return None
    d = dict(row)
    if isinstance(role_or_fields, str):
        # 查用户
        with get_db() as db:
            u = db.execute("SELECT * FROM users WHERE username=?",(role_or_fields,)).fetchone()
        if u:
            fields = get_user_fields(u["role"], u["custom_fields"])
        else:
            fields = ROLE_DEFAULTS.get(role_or_fields, ROLE_DEFAULTS["boss"])["fields"]
    else:
        fields = role_or_fields
    allowed = set(fields)
    result = {k:d[k] for k in allowed if k in d}
    payment_fields = {"paid_total","balance_amount","is_paid","payment_status","payment_records","deposit_amount","paid_amount","paid_at"}
    if allowed & payment_fields and "id" in d:
        with get_db() as db:
            summary = get_payment_summary(db, d["id"], d.get("final_price") or 0)
        for key, value in summary.items():
            if key in allowed:
                result[key] = value
    if PUBLIC_DEMO_MODE and "customer_name" in result:
        result["customer_name"] = f"演示客户 {int(d['id']):02d}"
    return result

def get_user_permissions(role, custom_fields_json="{}"):
    perms = dict(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["assistant"]))
    try:
        custom = json.loads(custom_fields_json or "{}")
        perms.update(custom.get("permissions", {}))
    except:
        pass
    return perms

def get_period_start(period):
    today = date.today()
    if period == "today":
        return today.isoformat()
    if period == "week":
        return (today - timedelta(days=today.weekday())).isoformat()
    if period == "year":
        return date(today.year, 1, 1).isoformat()
    return today.replace(day=1).isoformat()

def build_chart_series(db, date_field, trend_days=180):
    trend_days = max(30, min(int(trend_days or 180), 1095))
    end = date.today()
    start = end - timedelta(days=trend_days - 1)
    if trend_days <= 62:
        granularity = "day"
    elif trend_days <= 120:
        granularity = "week"
    else:
        granularity = "month"

    def bucket_key(value):
        if not value:
            return ""
        d = datetime.strptime(value[:10], "%Y-%m-%d").date()
        if granularity == "day":
            return d.isoformat()
        if granularity == "week":
            monday = d - timedelta(days=d.weekday())
            return monday.isoformat()
        return d.strftime("%Y-%m")

    if granularity == "month":
        buckets = []
        cursor = date(start.year, start.month, 1)
        end_month = date(end.year, end.month, 1)
        while cursor <= end_month:
            buckets.append(cursor.strftime("%Y-%m"))
            year = cursor.year + (1 if cursor.month == 12 else 0)
            month = 1 if cursor.month == 12 else cursor.month + 1
            cursor = date(year, month, 1)
        date_start = start.strftime("%Y-%m-01")
    elif granularity == "week":
        first = start - timedelta(days=start.weekday())
        buckets = []
        cursor = first
        while cursor <= end:
            buckets.append(cursor.isoformat())
            cursor += timedelta(days=7)
        date_start = first.isoformat()
    else:
        buckets = [(start + timedelta(days=i)).isoformat() for i in range(trend_days)]
        date_start = start.isoformat()

    order_rows = db.execute(
        f"""SELECT {date_field} as dt FROM orders
            WHERE {date_field}>=? AND COALESCE({date_field},'')!=''""",
        (date_start,)).fetchall()
    finance_rows = db.execute(
        f"""SELECT {date_field} as dt, final_price, profit
            FROM orders
            WHERE status IN ('confirmed','in_production','delivered') AND {date_field}>=? AND COALESCE({date_field},'')!=''
            """,
        (date_start,)).fetchall()
    orders = {b: 0 for b in buckets}
    finance = {b: {"revenue": 0, "profit": 0} for b in buckets}
    for r in order_rows:
        b = bucket_key(r["dt"])
        if b in orders:
            orders[b] += 1
    for r in finance_rows:
        b = bucket_key(r["dt"])
        if b in finance:
            finance[b]["revenue"] += float(r["final_price"] or 0)
            finance[b]["profit"] += float(r["profit"] or 0)
    return {
        "granularity": granularity,
        "orders": [{"label": b, "value": int(orders.get(b, 0))} for b in buckets],
        "finance": [{"label": b, "revenue": round(float(finance.get(b, {}).get("revenue", 0)), 2), "profit": round(float(finance.get(b, {}).get("profit", 0)), 2)} for b in buckets],
    }

def recalc_order(db, order_id):
    items = db.execute("SELECT amount FROM quote_items WHERE order_id=?",(order_id,)).fetchall()
    cost = sum(i["amount"] for i in items)
    row = db.execute("SELECT profit_mode,profit_rate,profit_fixed FROM orders WHERE id=?",(order_id,)).fetchone()
    mode = (row["profit_mode"] if row and row["profit_mode"] else "percent")
    rate = float(row["profit_rate"] if row and row["profit_rate"] is not None else DEFAULT_PROFIT_RATE)
    fixed = float(row["profit_fixed"] if row and row["profit_fixed"] is not None else 0)
    profit = round(fixed if mode == "fixed" else cost * rate, 2)
    final = cost + profit
    db.execute("UPDATE orders SET cost_total=?,profit=?,final_price=?,updated_at=datetime('now','localtime') WHERE id=?",
               (cost, profit, final, order_id))
    try:
        sync_order_payment_summary(db, order_id)
    except sqlite3.OperationalError:
        pass
    return cost, profit, final

def get_setting(key, default=""):
    with get_db() as db:
        r = db.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
    return r["value"] if r else default

def set_setting(key, value):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",(key,value))

def get_setting_from_conn(db, key, default=""):
    r = db.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
    return r["value"] if r else default

def send_wecom_webhook(webhook_url, title, message):
    webhook_url = (webhook_url or "").strip()
    if not webhook_url.startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="):
        raise ValueError("企业微信机器人地址格式不正确")
    content = f"**{title}**\n\n{message}\n\n> 来自珠宝协作 CRM Demo"
    payload = json.dumps({
        "msgtype": "markdown",
        "markdown": {"content": content}
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    context = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=8, context=context) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    result = json.loads(raw or "{}")
    if result.get("errcode") != 0:
        raise RuntimeError(result.get("errmsg") or "企业微信发送失败")
    return result

def send_configured_wecom(title, message):
    with get_db() as db:
        webhook_url = get_setting_from_conn(db, "wecom_webhook_url", "")
        enabled = get_setting_from_conn(db, "wecom_enabled", "1")
    if not webhook_url or enabled == "0":
        return None
    return send_wecom_webhook(webhook_url, title, message)

def add_notification(user_id, title, message, channel="in_app"):
    user_id = user_id or None
    webhook_url = ""
    enabled = "1"
    with get_db() as db:
        db.execute("INSERT INTO notifications(user_id,title,message,channel) VALUES(?,?,?,?)",
                   (user_id, title, message, channel))
        webhook_url = get_setting_from_conn(db, "wecom_webhook_url", "")
        enabled = get_setting_from_conn(db, "wecom_enabled", "1")
    if webhook_url and enabled != "0":
        try:
            send_wecom_webhook(webhook_url, title, message)
        except Exception as e:
            print(f"企业微信通知发送失败：{e}")

def add_status_history(db, order_id, from_status, to_status, changed_by="", note="", force=False, event_type="status", is_sensitive=False):
    exists = db.execute("SELECT id FROM status_history WHERE order_id=? AND from_status=? AND to_status=? ORDER BY id DESC LIMIT 1",
                        (order_id, from_status or "", to_status)).fetchone()
    if force or not exists:
        db.execute("""INSERT INTO status_history(order_id,from_status,to_status,changed_by,note,event_type,is_sensitive)
            VALUES(?,?,?,?,?,?,?)""",
            (order_id, from_status or "", to_status, changed_by, note, event_type, 1 if is_sensitive else 0))

def can_view_sensitive_history(fields):
    return any(f in fields for f in ("cost_total","profit","profit_rate","profit_fixed","payment_records","paid_total","balance_amount"))

FIELD_LABELS = {
    "customer_name": "客户姓名", "product_type": "产品类型", "metal_type": "金属材质",
    "main_stone": "主石", "side_stones": "辅石", "material_notes": "材料备注",
    "special_notes": "特殊要求", "budget": "客户预算", "ring_size": "尺寸",
    "due_date": "交付日期", "customer_source": "客户来源", "occasion": "用途",
    "design_brief": "设计方向", "image_url": "参考图", "follow_up_note": "跟进建议",
    "material_specs": "材料清单", "deposit_amount": "定金", "paid_amount": "已付款",
    "paid_at": "付款日期", "payment_note": "付款备注", "final_price": "订单总价",
}

def summarize_order_updates(old_order, updates):
    public_changes, sensitive_changes = [], []
    hidden_keys = {"profit_mode","profit_rate","profit_fixed","profit","cost_total","status"}
    sensitive_keys = {"deposit_amount","paid_amount","paid_at","payment_note","final_price"}
    for key, new_value in updates.items():
        if key in hidden_keys:
            continue
        old_value = old_order[key] if key in old_order.keys() else ""
        old_text = "空" if old_value in (None, "") else str(old_value)
        new_text = "空" if new_value in (None, "") else str(new_value)
        if old_text == new_text:
            continue
        label = FIELD_LABELS.get(key, key)
        target = sensitive_changes if key in sensitive_keys else public_changes
        target.append(f"{label}：{old_text} → {new_text}")
    return public_changes, sensitive_changes

def reset_status_timeline(db, order_id, created_at, current_status, completed_at=""):
    """为演示订单生成从创建到当前状态的完整、递进时间线。"""
    db.execute("DELETE FROM status_history WHERE order_id=?", (order_id,))
    target_index = STATUS_FLOW.index(current_status) if current_status in STATUS_FLOW else 0
    steps = STATUS_FLOW[1:target_index + 1] if target_index >= 1 else [current_status]
    actors = {
        "pending_quote": ("assistant", "小助理录入需求并提交报价清单"),
        "quoting": ("master", "老师傅开始核算材料和工费"),
        "quoted": ("master", "老师傅提交成本与单价"),
        "reviewed": ("boss", "老板审核利润和对客报价"),
        "sent_to_client": ("boss", "报价已复制并通过微信发给客户"),
        "confirmed": ("boss", "客户确认方案并支付定金"),
        "in_production": ("master", "进入制作或采购排期"),
        "delivered": ("boss", "客户付清尾款并完成交付"),
    }
    base = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S")
    offsets = [2, 7, 28, 50, 72, 120, 168, 240]
    previous = ""
    for i, status in enumerate(steps):
        actor, note = actors.get(status, ("系统", "状态更新"))
        if status == "delivered" and completed_at:
            if len(completed_at) <= 10:
                changed_at = datetime.strptime(completed_at[:10] + " 18:00:00", "%Y-%m-%d %H:%M:%S")
            else:
                changed_at = datetime.strptime(completed_at[:19], "%Y-%m-%d %H:%M:%S")
        else:
            changed_at = base + timedelta(hours=offsets[min(i, len(offsets)-1)])
        db.execute("""INSERT INTO status_history(order_id,from_status,to_status,changed_by,changed_at,note)
            VALUES(?,?,?,?,?,?)""",
            (order_id, previous, status, actor, changed_at.strftime("%Y-%m-%d %H:%M:%S"), note))
        previous = status

def enrich_order_details(db, d, fields, viewer_username=""):
    if not d or "id" not in d:
        return d
    order_id = d["id"]
    if "quote_items" in fields:
        d["quote_items"] = [dict(it) for it in db.execute("SELECT * FROM quote_items WHERE order_id=?",(order_id,)).fetchall()]
    if "status_history" in fields:
        if can_view_sensitive_history(fields):
            rows = db.execute("SELECT * FROM status_history WHERE order_id=? ORDER BY changed_at ASC,id ASC",(order_id,)).fetchall()
        else:
            rows = db.execute("""SELECT * FROM status_history
                WHERE order_id=? AND (COALESCE(is_sensitive,0)=0 OR changed_by=?)
                ORDER BY changed_at ASC,id ASC""",(order_id, viewer_username)).fetchall()
        d["status_history"] = [dict(it) for it in rows]
    if "after_sales" in fields:
        d["after_sales"] = [dict(it) for it in db.execute("SELECT * FROM after_sales WHERE order_id=? ORDER BY created_at DESC",(order_id,)).fetchall()]
    if "quote_change_requests" in fields:
        d["quote_change_requests"] = [dict(it) for it in db.execute("SELECT * FROM quote_change_requests WHERE order_id=? ORDER BY created_at DESC",(order_id,)).fetchall()]
    if "payment_records" in fields:
        summary = get_payment_summary(db, order_id, d.get("final_price") or 0)
        d.update({k:v for k,v in summary.items() if k in fields})
    return d

# ─── Seed 演示数据 ───
def demo_password(env_name, local_default):
    password = os.environ.get(env_name)
    if os.environ.get("VERCEL") and not password:
        raise RuntimeError(f"请配置 Vercel 环境变量：{env_name}")
    return password or local_default

def ensure_default_users(db):
    if PUBLIC_DEMO_MODE:
        password = os.environ.get("DEMO_VIEWER_PASSWORD")
        if os.environ.get("VERCEL") and not password:
            raise RuntimeError("请配置 Vercel 环境变量：DEMO_VIEWER_PASSWORD")
        db.execute("UPDATE users SET is_active=0")
        db.execute("""INSERT INTO users(username,password_hash,display_name,role,custom_fields,is_active)
            VALUES(?,?,?,?,?,1)
            ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,
            display_name=excluded.display_name, role=excluded.role, custom_fields=excluded.custom_fields, is_active=1""",
            (PUBLIC_DEMO_USER, hash_password(password or "local-demo-only"), "演示访客", "demo_viewer", "{}"))
        return
    users = [
        ("boss",demo_password("DEMO_BOSS_PASSWORD", "boss123"),"张老板","boss","{}"),
        ("assistant",demo_password("DEMO_ASSISTANT_PASSWORD", "assist123"),"小助理","assistant","{}"),
        ("master",demo_password("DEMO_MASTER_PASSWORD", "master123"),"老师傅","master","{}"),
    ]
    for username, password, display_name, role, custom_fields in users:
        db.execute("""INSERT INTO users(username,password_hash,display_name,role,custom_fields,is_active)
            VALUES(?,?,?,?,?,1)
            ON CONFLICT(username) DO UPDATE SET
                password_hash=excluded.password_hash,
                display_name=excluded.display_name,
                role=excluded.role,
                custom_fields=excluded.custom_fields,
                is_active=1""",
            (username, hash_password(password), display_name, role, custom_fields))
    # 演示站仅保留对外展示的三个角色，停用旧的辅助演示账户。
    db.execute("UPDATE users SET is_active=0 WHERE username IN ('assist','custom')")

def seed_demo():
    with get_db() as db:
        ensure_default_users(db)
        if db.execute("SELECT COUNT(*) as c FROM orders").fetchone()["c"] > 0:
            for k,v in [("profit_rate","0.10"),("currency","CNY"),("shop_name","珠宝定制工作室")]:
                db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))
            return

        # 演示订单（覆盖过去 60 天）
        customers = ["张太太","李小姐","王先生","陈女士","赵太太","周小姐","钱先生","孙太太"]
        products = ["戒指","项链","手镯","耳环","吊坠","手链"]
        metals = ["18K白金","18K黄金","18K玫瑰金","PT950铂金","14K金"]
        stones = ["祖母绿 1.2克拉","钻石 0.5克拉","蓝宝石 2.0克拉","红宝石 0.8克拉","海蓝宝 1.5克拉","翡翠","钻石 1.0克拉","无"]
        statuses = ["pending_quote","quoted","reviewed","sent_to_client","confirmed","in_production","delivered"]
        weights = [1,2,2,1,3,2,4]  # delivered 多一些

        orders_data = []
        today = date.today()
        for i in range(18):
            days_ago = random.randint(0, 55)
            order_date = (today - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
            customer = random.choice(customers)
            product = random.choice(products)
            metal = random.choice(metals)
            stone = random.choice(stones)
            status = random.choices(statuses, weights=weights, k=1)[0]
            rate = random.choice([0.10,0.12,0.15])
            on = f"J{today.strftime('%Y%m%d')}-{i+1:03d}"

            # 人为修改日期以模拟历史
            db.execute(
                "INSERT INTO orders(order_number,customer_name,product_type,metal_type,main_stone,status,profit_rate,created_at,updated_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (on, customer, product, metal, stone, status, rate, order_date, order_date,
                 random.choice(["assistant","小助理"])))

            # 给非待报价的订单生成报价
            if status not in ("draft","pending_quote","quoting"):
                items = [
                    (f"{stone.split()[0] if stone!='无' else product}裸石", random.randint(2000,15000)),
                    (f"{metal}托", random.randint(1000,5000)),
                    ("碎钻配镶", random.randint(300,2000)),
                    ("工费", random.randint(300,1200)),
                ]
                for it in items:
                    db.execute("INSERT INTO quote_items(order_id,item_name,amount) VALUES(?,?,?)",
                               (i+1, it[0], it[1]))
                recalc_order(db, i+1)

        # 确保至少有几个不同状态的活跃订单
        # 1个 pending_quote
        db.execute("UPDATE orders SET status='pending_quote' WHERE id=1")
        # 1个 quoted
        db.execute("UPDATE orders SET status='quoted' WHERE id=2")
        # 1个 reviewed
        db.execute("UPDATE orders SET status='reviewed' WHERE id=3")

        # 默认系统配置
        for k,v in [("profit_rate","0.10"),("currency","CNY"),("shop_name","珠宝定制工作室")]:
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))

def upsert_quote_items(db, order_id, items):
    db.execute("DELETE FROM quote_items WHERE order_id=?",(order_id,))
    for item in items:
        if isinstance(item, tuple):
            name, amount = item
            qty, unit, unit_price = 1, "", amount
        else:
            name = item.get("item_name", "")
            qty = float(item.get("quantity") or 1)
            unit = item.get("unit", "")
            unit_price = float(item.get("unit_price") or 0)
            amount = float(item.get("amount") or qty * unit_price)
        db.execute("INSERT INTO quote_items(order_id,item_name,quantity,unit,unit_price,amount) VALUES(?,?,?,?,?,?)",
                   (order_id,name,qty,unit,unit_price,amount))
    recalc_order(db, order_id)

def normalize_quote_items(items):
    normalized = []
    for it in items:
        qty = float(it.get("quantity") or 1)
        unit = it.get("unit", "")
        unit_price = float(it.get("unit_price") or 0)
        amount = float(it.get("amount") or qty * unit_price)
        normalized.append({
            "item_name": it.get("item_name", ""),
            "quantity": qty,
            "unit": unit,
            "unit_price": unit_price,
            "amount": amount,
        })
    return normalized

def fetch_quote_items(db, order_id):
    return [dict(r) for r in db.execute(
        "SELECT item_name,quantity,unit,unit_price,amount FROM quote_items WHERE order_id=? ORDER BY id ASC",
        (order_id,)).fetchall()]

def format_money(value):
    return f"¥{float(value or 0):,.0f}"

def quote_item_snapshot(item):
    return f"{item.get('quantity') or 1:g}{item.get('unit') or ''} × {format_money(item.get('unit_price'))} = {format_money(item.get('amount'))}"

def summarize_quote_diff(old_items, new_items):
    old_map = {it.get("item_name") or f"未命名{idx}": it for idx, it in enumerate(old_items)}
    new_map = {it.get("item_name") or f"未命名{idx}": it for idx, it in enumerate(new_items)}
    names = sorted(set(old_map) | set(new_map))
    changes = []
    for name in names:
        old = old_map.get(name)
        new = new_map.get(name)
        if old and not new:
            changes.append(f"{name}：删除（原 {quote_item_snapshot(old)}）")
        elif new and not old:
            changes.append(f"{name}：新增 {quote_item_snapshot(new)}")
        else:
            fields = []
            for key, label in (("quantity","数量"),("unit","单位"),("unit_price","单价"),("amount","金额")):
                old_val = old.get(key)
                new_val = new.get(key)
                old_cmp = float(old_val or 0) if key != "unit" else (old_val or "")
                new_cmp = float(new_val or 0) if key != "unit" else (new_val or "")
                if old_cmp != new_cmp:
                    fmt_old = format_money(old_val) if key in ("unit_price","amount") else f"{old_val:g}" if isinstance(old_val, (int, float)) else str(old_val or "")
                    fmt_new = format_money(new_val) if key in ("unit_price","amount") else f"{new_val:g}" if isinstance(new_val, (int, float)) else str(new_val or "")
                    fields.append(f"{label} {fmt_old} → {fmt_new}")
            if fields:
                changes.append(f"{name}：" + "，".join(fields))
    old_total = sum(float(it.get("amount") or 0) for it in old_items)
    new_total = sum(float(it.get("amount") or 0) for it in new_items)
    if old_total != new_total:
        changes.append(f"成本合计：{format_money(old_total)} → {format_money(new_total)}")
    return changes

def replace_quote_items(db, order_id, items):
    normalized = normalize_quote_items(items)
    db.execute("DELETE FROM quote_items WHERE order_id=?",(order_id,))
    for it in normalized:
        db.execute("INSERT INTO quote_items(order_id,item_name,quantity,unit,unit_price,amount) VALUES(?,?,?,?,?,?)",
                   (order_id,it["item_name"],it["quantity"],it["unit"],it["unit_price"],it["amount"]))
    return recalc_order(db, order_id), normalized

def seed_customer_profiles(db):
    profiles = [
        ("张明轩","42","男","13816686688","wx_zhangmx","上海","浦东新区世纪大道","1984-05-18","祖母绿、复古低调、日常佩戴","高价值复购客户，适合保养服务和纪念日提醒"),
        ("李婉晴","29","女","13657111122","li_wq520","杭州","西湖区文三路","1997-09-02","显钻、细戒臂、求婚急单","对交付时间敏感，沟通要明确节点"),
        ("王宇航","36","男","13962557733","wang_yh88","苏州","工业园区星湖街","1990-01-11","长辈礼物、寓意好、包装体面","适合推荐礼盒和祝福卡"),
        ("陈思雨","31","女","13522292299","chen_siyu","上海","徐汇区衡山路","1995-11-26","珍珠、小钻、温柔日常","价格敏感，适合强调售后和日常佩戴价值"),
        ("赵琳","38","女","13700008866","zhaolin_jewel","南京","鼓楼区中山北路","1988-03-08","翡翠、手镯、收藏级成色","偏高客单，适合节日前重点维护"),
        ("周雅婷","33","女","13688881234","zhou_yt","宁波","鄞州区钱湖北路","1993-07-15","蓝宝石、简洁通勤","喜欢先看图再确认预算"),
        ("钱嘉豪","40","男","13900006666","qianjh","上海","静安区南京西路","1986-12-03","男士戒指、低调、有重量感","注重材质说明和证书"),
        ("孙若兰","45","女","13899990001","sun_ruolan","杭州","滨江区江南大道","1981-10-22","黄金手镯、翡翠吊坠","老客户，适合节日复购提醒"),
    ]
    for p in profiles:
        db.execute("""INSERT OR REPLACE INTO customer_profiles(name,age,gender,phone,wechat,city,address,birthday,preferences,notes)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", p)

def ensure_demo_story():
    """把随机演示数据改成一条能讲清楚业务价值的销售故事。"""
    today = date.today()
    customers = [
        ("李婉晴","29","女","13657111122","li_wq520","杭州","西湖区文三路","1997-09-02","显钻、细戒臂、求婚急单"),
        ("张明轩","42","男","13816686688","wx_zhangmx","上海","浦东新区世纪大道","1984-05-18","祖母绿、复古低调、日常佩戴"),
        ("王宇航","36","男","13962557733","wang_yh88","苏州","工业园区星湖街","1990-01-11","长辈礼物、寓意好、包装体面"),
        ("陈思雨","31","女","13522292299","chen_siyu","上海","徐汇区衡山路","1995-11-26","珍珠、小钻、温柔日常"),
        ("赵琳","38","女","13700008866","zhaolin_jewel","南京","鼓楼区中山北路","1988-03-08","翡翠、手镯、收藏级成色"),
        ("周雅婷","33","女","13688881234","zhou_yt","宁波","鄞州区钱湖北路","1993-07-15","蓝宝石、简洁通勤"),
        ("钱嘉豪","40","男","13900006666","qianjh","上海","静安区南京西路","1986-12-03","男士戒指、低调、有重量感"),
        ("孙若兰","45","女","13899990001","sun_ruolan","杭州","滨江区江南大道","1981-10-22","黄金手镯、翡翠吊坠"),
        ("林嘉怡","27","女","13712345678","linjiayi","广州","天河区珠江新城","1999-04-06","彩宝、耳饰、轻奢款"),
        ("高博文","34","男","13987654321","gaobowen","深圳","南山区科技园","1992-08-19","求婚钻戒、预算明确、重证书"),
    ]
    order_templates = [
        ("李婉晴","婚戒","PT950铂金","钻石 1.0克拉 D色 VS1","小钻 12颗","pending_quote",40000,0.15,"小红书私信","求婚","细戒臂、六爪、显钻、不要太高托",[]),
        ("张明轩","戒指","18K白金","祖母绿 1.2克拉","梯钻 2颗","quoting",38000,0.16,"老客户复购","结婚纪念日","复古、低调、有质感，适合日常佩戴",[]),
        ("王宇航","吊坠","18K黄金","翡翠冰种平安扣","无","quoted",26000,0.14,"朋友转介绍","生日礼物","稳重、福气、适合长辈",[( "翡翠平安扣",1,"件",15200),( "18K黄金扣头和链",1,"件",3600),( "抛光与装配",1,"件",900)]),
        ("陈思雨","耳环","18K玫瑰金","Akoya 珍珠 8mm","小钻 8颗","reviewed",12000,0.12,"微信老客群","日常佩戴","温柔、轻盈、不要太夸张",[( "Akoya 珍珠",2,"颗",2600),( "18K玫瑰金耳托",1,"对",1800),( "小钻配镶",8,"颗",112.5),( "工费",1,"件",650)]),
        ("赵琳","手镯","18K黄金","冰种翡翠手镯","无","sent_to_client",68000,0.18,"线下到店","收藏佩戴","颜色干净、种水好、适合日常和宴会佩戴",[( "冰种翡翠手镯",1,"件",48500),( "证书与包装",1,"套",680),( "工费与质检",1,"件",1200)]),
        ("周雅婷","项链","18K白金","蓝宝石 1.8克拉","小钻 16颗","confirmed",42000,0.15,"小红书私信","生日礼物","简洁通勤，主石颜色要正",[( "蓝宝石",1.8,"克拉",14200),( "18K白金链托",1,"件",5200),( "小钻配镶",16,"颗",160),( "工费",1,"件",1200)]),
        ("钱嘉豪","男戒","PT950铂金","黑钻 0.8克拉","无","in_production",30000,0.13,"线下到店","自用","低调、有重量感、戒面不要太亮",[( "黑钻",0.8,"克拉",12800),( "PT950铂金戒托",1,"件",7600),( "雕蜡与工费",1,"件",1800)]),
        ("孙若兰","手镯","18K黄金","古法金素圈","无","delivered",28000,0.12,"老客户复购","节日礼物","克重足，佩戴舒适，包装体面",[( "古法金手镯",32,"克",620),( "工费",1,"件",1600)]),
        ("林嘉怡","耳环","18K玫瑰金","红宝石 0.6克拉","小钻 10颗","pending_quote",18000,0.15,"微信老客群","生日礼物","小巧但有存在感",[]),
        ("高博文","婚戒","18K白金","钻石 1.5克拉 F色 VS2","小钻 18颗","quoted",76000,0.16,"朋友转介绍","求婚","经典六爪，证书齐全，预算可上浮",[( "GIA 钻石",1.5,"克拉",43800),( "18K白金戒托",1,"件",6200),( "小钻",18,"颗",180),( "镶嵌工费",1,"件",1800)]),
        ("李婉晴","对戒","18K白金","无","无","sent_to_client",16000,0.12,"小红书私信","婚礼","简洁耐看，适合日常佩戴",[( "18K白金对戒",1,"对",9800),( "刻字服务",1,"次",300),( "工费",1,"件",800)]),
        ("张明轩","胸针","18K黄金","祖母绿 0.8克拉","珍珠 3颗","confirmed",32000,0.15,"老客户复购","纪念日","复古胸针，可搭西装",[( "祖母绿",0.8,"克拉",11800),( "珍珠",3,"颗",900),( "18K黄金胸针托",1,"件",5200),( "工费",1,"件",1600)]),
        ("王宇航","手链","18K黄金","无","无","delivered",22000,0.12,"朋友转介绍","母亲节","结实、寓意好、包装完整",[( "18K黄金手链",24,"克",610),( "工费",1,"件",1200)]),
        ("陈思雨","项链","18K白金","海蓝宝 1.2克拉","小钻 6颗","in_production",21000,0.13,"微信老客群","日常佩戴","清透、轻盈、适合通勤",[( "海蓝宝",1.2,"克拉",6200),( "18K白金链托",1,"件",3600),( "小钻",6,"颗",120),( "工费",1,"件",1000)]),
        ("赵琳","吊坠","18K黄金","翡翠叶子","小钻 5颗","delivered",36000,0.16,"线下到店","收藏佩戴","叶子寓意好，颜色要阳绿",[( "翡翠叶子",1,"件",23800),( "18K黄金扣头",1,"件",2800),( "小钻",5,"颗",180),( "工费",1,"件",1200)]),
        ("周雅婷","戒指","18K白金","蓝宝石 0.9克拉","梯钻 2颗","reviewed",26000,0.14,"小红书私信","自用","通勤款，戒臂简洁",[( "蓝宝石",0.9,"克拉",9800),( "梯钻",2,"颗",1600),( "18K白金戒托",1,"件",4200),( "工费",1,"件",1200)]),
        ("钱嘉豪","袖扣","18K白金","黑玛瑙","无","pending_quote",12000,0.12,"线下到店","商务礼物","低调，适合西装",[]),
        ("孙若兰","吊坠","18K黄金","翡翠佛公","无","confirmed",45000,0.15,"老客户复购","生日礼物","厚装，笑脸好，证书齐全",[( "翡翠佛公",1,"件",28500),( "18K黄金扣头",1,"件",3200),( "证书与包装",1,"套",600),( "工费",1,"件",1300)]),
        ("林嘉怡","手链","18K玫瑰金","彩宝组合","小钻 12颗","quoted",24000,0.15,"微信老客群","生日礼物","颜色活泼，可叠戴",[( "彩宝组合",8,"颗",980),( "18K玫瑰金链",1,"件",4200),( "小钻",12,"颗",120),( "工费",1,"件",1200)]),
        ("高博文","项链","PT950铂金","钻石 0.8克拉","无","delivered",39000,0.14,"朋友转介绍","纪念日","简洁单钻，日常佩戴",[( "钻石",0.8,"克拉",22800),( "PT950铂金链托",1,"件",5200),( "工费",1,"件",1200)]),
    ]
    with get_db() as db:
        while db.execute("SELECT COUNT(*) as c FROM orders").fetchone()["c"] < 20:
            idx = db.execute("SELECT COUNT(*) as c FROM orders").fetchone()["c"] + 1
            on = f"J{today.strftime('%Y%m%d')}-{idx:03d}"
            db.execute("INSERT OR IGNORE INTO orders(order_number,status,created_by) VALUES(?,?,?)",(on,"draft","assistant"))
        order_ids = [r["id"] for r in db.execute("SELECT id FROM orders ORDER BY id LIMIT 20").fetchall()]
        db.execute("DELETE FROM customer_profiles WHERE name IN ('我刚拿到','刚拿到','客户资料') OR name LIKE '%的' OR name LIKE '%电'")
        for i, order_id in enumerate(order_ids):
            tpl = order_templates[i]
            name, product, metal, main_stone, side_stones, status, budget, rate, source, occasion, brief, items = tpl
            created_at = (datetime.now() - timedelta(days=45 - i * 2, hours=(i % 5) + 1)).strftime("%Y-%m-%d %H:%M:%S")
            due_date = (date.today() + timedelta(days=max(5, 28 - i))).isoformat() if status not in ("delivered",) else (date.today() - timedelta(days=2 + i % 6)).isoformat()
            materials = [{"name": main_stone.split()[0], "quantity": 1, "unit": "件"}] if main_stone != "无" else [{"name": metal, "quantity": 1, "unit": "件"}]
            if side_stones and side_stones != "无":
                materials.append({"name": side_stones.split()[0], "quantity": 1, "unit": "批"})
            db.execute("""UPDATE orders SET customer_name=?,product_type=?,metal_type=?,main_stone=?,side_stones=?,
                material_notes=?,special_notes=?,budget=?,ring_size=?,due_date=?,customer_source=?,occasion=?,
                design_brief=?,image_url=?,follow_up_note=?,status=?,profit_rate=?,material_specs=?,
                created_at=?,updated_at=? WHERE id=?""",
                (name,product,metal,main_stone,side_stones,
                 f"{name}需要{brief}，材料需按清单报价。", "演示数据：状态、报价、收款和客户资料均可下钻。",
                 budget, "按客户尺寸确认" if product in ("戒指","婚戒","男戒","对戒") else "", due_date, source, occasion,
                 brief, random.choice(["/assets/ring-emerald.svg","/assets/ring-vintage.svg","/assets/pendant-jade.svg","/assets/earrings-pearl.svg"]),
                 "按当前节点继续推进，必要时用 AI 生成微信跟进话术。", status, rate,
                 json.dumps({"materials":materials}, ensure_ascii=False), created_at, created_at, order_id))
            quote_items = [{"item_name":n,"quantity":q,"unit":u,"unit_price":p} for n,q,u,p in items]
            db.execute("DELETE FROM quote_change_requests WHERE order_id=?",(order_id,))
            if quote_items:
                upsert_quote_items(db, order_id, quote_items)
            else:
                db.execute("DELETE FROM quote_items WHERE order_id=?",(order_id,))
                recalc_order(db, order_id)
            db.execute("DELETE FROM payment_records WHERE order_id=?",(order_id,))
            final = db.execute("SELECT final_price FROM orders WHERE id=?",(order_id,)).fetchone()["final_price"] or 0
            start = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
            payment_plan = []
            if status in ("confirmed","in_production"):
                payment_plan.append(("deposit", round(final * 0.3, 2), (start + timedelta(days=6)).strftime("%Y-%m-%d"), "客户确认后支付定金"))
                if status == "in_production":
                    payment_plan.append(("partial", round(final * 0.2, 2), (start + timedelta(days=12)).strftime("%Y-%m-%d"), "制作前补付部分款项"))
            if status == "delivered":
                payment_plan = [
                    ("deposit", round(final * 0.3, 2), (start + timedelta(days=5)).strftime("%Y-%m-%d"), "客户确认后支付定金"),
                    ("partial", round(final * 0.2, 2), (start + timedelta(days=12)).strftime("%Y-%m-%d"), "制作中补付款"),
                    ("balance", round(final * 0.5, 2), (start + timedelta(days=22)).strftime("%Y-%m-%d"), "交付时付清尾款"),
                ]
            for category, amount, paid_at, note in payment_plan:
                db.execute("""INSERT INTO payment_records(order_id,category,amount,paid_at,submitted_by,submitted_at,note)
                    VALUES(?,?,?,?,?,?,?)""",(order_id,category,amount,paid_at,"boss",paid_at+" 10:30:00",note))
            sync_order_payment_summary(db, order_id)
            paid_at = db.execute("SELECT paid_at FROM orders WHERE id=?",(order_id,)).fetchone()["paid_at"] or ""
            reset_status_timeline(db, order_id, created_at, status, paid_at)
        used_names = sorted({tpl[0] for tpl in order_templates})
        profile_map = {p[0]:p for p in customers}
        db.execute(f"DELETE FROM customer_profiles WHERE name NOT IN ({','.join('?' for _ in used_names)})", used_names)
        for name in used_names:
            p = profile_map[name]
            total = db.execute("SELECT COUNT(*) as c, COALESCE(SUM(final_price),0) as s FROM orders WHERE customer_name=?",(name,)).fetchone()
            notes = f"由演示订单自动生成：共 {total['c']} 单，累计报价 ¥{total['s']:,.0f}。"
            db.execute("""INSERT OR REPLACE INTO customer_profiles(name,age,gender,phone,wechat,city,address,birthday,preferences,notes)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",(p[0],p[1],p[2],p[3],p[4],p[5],p[6],p[7],p[8],notes))
        for k,v in [("shop_name","璟禾珠宝定制"),("ai_provider","openai_compatible"),("ai_base_url","https://openrouter.ai/api/v1/chat/completions"),("ai_model","openrouter/auto")]:
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))
        for delivered in db.execute("SELECT id,customer_name FROM orders WHERE status='delivered' LIMIT 3").fetchall():
            exists = db.execute("SELECT id FROM after_sales WHERE order_id=?",(delivered["id"],)).fetchone()
            if not exists:
                db.execute("""INSERT INTO after_sales(order_id,customer_name,service_type,content,handled_by)
                    VALUES(?,?,?,?,?)""",(delivered["id"],delivered["customer_name"],"保养","交付后客户回店清洗保养一次，反馈佩戴舒适。","boss"))
    return
    demo = [
        {
            "id": 1, "customer_name": "李婉晴", "product_type": "婚戒", "metal_type": "PT950铂金",
            "main_stone": "钻石 1.0克拉 D色 VS1", "side_stones": "小钻 12颗",
            "material_notes": "客户发来参考图，喜欢细戒臂和六爪镶，预算希望控制在 4 万内。",
            "special_notes": "急单，求婚日期已定，需先确认裸石和戒托工期。",
            "budget": 40000, "ring_size": "港码 12", "due_date": (today + timedelta(days=12)).isoformat(),
            "customer_source": "小红书私信", "occasion": "求婚", "design_brief": "细戒臂、六爪、显钻、不要太高托",
            "image_url": "/assets/ring-emerald.svg", "follow_up_note": "先让师傅确认工期，今晚 8 点前给客户初步方案。",
            "status": "pending_quote", "profit_rate": 0.15,
            "materials":[{"name":"钻石","quantity":1.0,"unit":"克拉"},{"name":"碎钻","quantity":12,"unit":"颗"},{"name":"PT950铂金","quantity":1,"unit":"件"}],
            "items": [],
        },
        {
            "id": 2, "customer_name": "张明轩", "product_type": "戒指", "metal_type": "18K白金",
            "main_stone": "祖母绿 1.2克拉", "side_stones": "梯钻 2颗",
            "material_notes": "老客户，想做周年纪念款，强调祖母绿颜色要浓，戒臂不能太粗。",
            "special_notes": "客户能接受 3.8 万左右报价，适合重点维护。",
            "budget": 38000, "ring_size": "港码 13", "due_date": (today + timedelta(days=20)).isoformat(),
            "customer_source": "老客户复购", "occasion": "结婚纪念日", "design_brief": "复古、低调、有质感，适合日常佩戴",
            "image_url": "/assets/ring-vintage.svg", "follow_up_note": "待老板审核报价，建议附赠一次免费保养提升成交感。",
            "status": "quoted", "profit_rate": 0.16,
            "materials":[{"name":"祖母绿宝石","quantity":1.2,"unit":"克拉"},{"name":"梯钻","quantity":2,"unit":"颗"},{"name":"18K白金","quantity":1,"unit":"件"}],
            "items": [{"item_name":"祖母绿宝石","quantity":1.2,"unit":"克拉","unit_price":18166.67}, {"item_name":"18K白金戒托","quantity":1,"unit":"件","unit_price":4200}, {"item_name":"梯钻","quantity":2,"unit":"颗","unit_price":1800}, {"item_name":"镶嵌工费","quantity":1,"unit":"件","unit_price":1800}],
        },
        {
            "id": 3, "customer_name": "王宇航", "product_type": "吊坠", "metal_type": "18K黄金",
            "main_stone": "翡翠冰种平安扣", "side_stones": "无",
            "material_notes": "准备送母亲生日礼物，客户比较在意寓意和包装。",
            "special_notes": "已审核，下一步可生成微信报价卡发客户。",
            "budget": 26000, "ring_size": "", "due_date": (today + timedelta(days=9)).isoformat(),
            "customer_source": "朋友转介绍", "occasion": "生日礼物", "design_brief": "稳重、福气、适合长辈，链子要结实",
            "image_url": "/assets/pendant-jade.svg", "follow_up_note": "发报价时突出寓意和品质保证，客户今晚会和家人商量。",
            "status": "reviewed", "profit_rate": 0.14,
            "materials":[{"name":"翡翠","quantity":1,"unit":"件"},{"name":"18K黄金","quantity":1,"unit":"件"}],
            "items": [{"item_name":"翡翠平安扣","quantity":1,"unit":"件","unit_price":15200}, {"item_name":"18K黄金扣头和链","quantity":1,"unit":"件","unit_price":3600}, {"item_name":"抛光与装配","quantity":1,"unit":"件","unit_price":900}, {"item_name":"礼盒包装","quantity":1,"unit":"件","unit_price":180}],
        },
        {
            "id": 4, "customer_name": "陈思雨", "product_type": "耳环", "metal_type": "18K玫瑰金",
            "main_stone": "珍珠 8-8.5mm", "side_stones": "小钻 8颗",
            "material_notes": "客户已收到报价，觉得款式满意，但还在犹豫价格。",
            "special_notes": "适合 AI 生成跟进话术，强调可日常佩戴和售后清洗。",
            "budget": 12000, "ring_size": "", "due_date": (today + timedelta(days=18)).isoformat(),
            "customer_source": "微信老客群", "occasion": "日常佩戴", "design_brief": "温柔、轻盈、不要太夸张",
            "image_url": "/assets/earrings-pearl.svg", "follow_up_note": "已发客户未确认，明天上午适合温和跟进。",
            "status": "sent_to_client", "profit_rate": 0.12,
            "materials":[{"name":"珍珠","quantity":2,"unit":"颗"},{"name":"小钻","quantity":8,"unit":"颗"},{"name":"18K玫瑰金","quantity":1,"unit":"对"}],
            "items": [{"item_name":"Akoya 珍珠","quantity":2,"unit":"颗","unit_price":2600}, {"item_name":"18K玫瑰金耳托","quantity":1,"unit":"对","unit_price":1800}, {"item_name":"小钻配镶","quantity":8,"unit":"颗","unit_price":112.5}, {"item_name":"工费","quantity":1,"unit":"件","unit_price":650}],
        },
        {
            "id": 5, "customer_name": "赵琳", "product_type": "手镯", "metal_type": "18K黄金",
            "main_stone": "冰种翡翠手镯", "side_stones": "无",
            "material_notes": "客户到店挑选，偏收藏级成色，希望证书和包装完整。",
            "special_notes": "已付清尾款并交付，后续适合做节日复购维护。",
            "budget": 68000, "ring_size": "圈口 56", "due_date": (today - timedelta(days=5)).isoformat(),
            "customer_source": "线下到店", "occasion": "收藏佩戴", "design_brief": "颜色干净、种水好、适合日常和宴会佩戴",
            "image_url": "/assets/pendant-jade.svg", "follow_up_note": "一个月后提醒免费保养，节日前推荐翡翠吊坠。",
            "status": "delivered", "profit_rate": 0.18,
            "materials":[{"name":"翡翠","quantity":1,"unit":"件"},{"name":"18K黄金","quantity":1,"unit":"件"}],
            "items": [{"item_name":"冰种翡翠手镯","quantity":1,"unit":"件","unit_price":48500}, {"item_name":"证书与包装","quantity":1,"unit":"套","unit_price":680}, {"item_name":"工费与质检","quantity":1,"unit":"件","unit_price":1200}],
        },
    ]
    with get_db() as db:
        name_map = {"张太太":"张明轩","李小姐":"李婉晴","王先生":"王宇航","陈女士":"陈思雨","赵太太":"赵琳","周小姐":"周雅婷","钱先生":"钱嘉豪","孙太太":"孙若兰"}
        for old_name, new_name in name_map.items():
            db.execute("UPDATE orders SET customer_name=? WHERE customer_name=?",(new_name, old_name))
        db.execute("DELETE FROM customer_profiles WHERE name IN ('我刚拿到','刚拿到','客户资料') OR name LIKE '%的' OR name LIKE '%电'")
        for order in demo:
            exists = db.execute("SELECT id FROM orders WHERE id=?",(order["id"],)).fetchone()
            if not exists:
                continue
            created_at = (datetime.now() - timedelta(days={1:2, 2:8, 3:12, 4:15, 5:28}.get(order["id"], 20),
                                                    hours={1:3, 2:6, 3:4, 4:2, 5:5}.get(order["id"], 1))).strftime("%Y-%m-%d %H:%M:%S")
            fields = ["customer_name","product_type","metal_type","main_stone","side_stones","material_notes",
                      "special_notes","budget","ring_size","due_date","customer_source","occasion","design_brief",
                      "image_url","follow_up_note","status","profit_rate"]
            db.execute(
                f"UPDATE orders SET {', '.join(f'{f}=?' for f in fields)}, material_specs=?, created_at=?, updated_at=datetime('now','localtime') WHERE id=?",
                [order[f] for f in fields] + [json.dumps({"materials":order["materials"]}, ensure_ascii=False), created_at, order["id"]]
            )
            if order["items"]:
                upsert_quote_items(db, order["id"], order["items"])
            else:
                db.execute("DELETE FROM quote_items WHERE order_id=?",(order["id"],))
                recalc_order(db, order["id"])
            row = db.execute("SELECT final_price FROM orders WHERE id=?",(order["id"],)).fetchone()
            final_price = float(row["final_price"] or 0) if row else 0
            deposit = final_price * 0.3 if order["status"] in ("confirmed","in_production","delivered") else 0
            paid = max(final_price - deposit, 0) if order["status"] == "delivered" else 0
            paid_at = (datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S") + timedelta(days=24, hours=3)).strftime("%Y-%m-%d %H:%M:%S") if order["status"] == "delivered" else ""
            payment_note = "客户已付定金，尾款待交付时支付。" if deposit and not paid else ("定金与尾款均已收齐，完成交付。" if paid_at else "")
            db.execute("""UPDATE orders SET deposit_amount=?,paid_amount=?,paid_at=?,payment_note=? WHERE id=?""",
                       (round(deposit, 2), round(paid, 2), paid_at, payment_note, order["id"]))
            reset_status_timeline(db, order["id"], created_at, order["status"], paid_at)
        for k,v in [
            ("shop_name","璟禾珠宝定制"),
            ("ai_provider","openai_compatible"),
            ("ai_base_url","https://openrouter.ai/api/v1/chat/completions"),
            ("ai_model","openrouter/auto"),
        ]:
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))
        seed_customer_profiles(db)
        delivered = db.execute("SELECT id,customer_name FROM orders WHERE status='delivered' ORDER BY id LIMIT 1").fetchone()
        if delivered:
            exists = db.execute("SELECT id FROM after_sales WHERE order_id=?",(delivered["id"],)).fetchone()
            if not exists:
                db.execute("""INSERT INTO after_sales(order_id,customer_name,service_type,content,handled_by)
                    VALUES(?,?,?,?,?)""",(delivered["id"],delivered["customer_name"],"保养","交付后客户回店清洗保养一次，反馈佩戴舒适。","张老板"))

# ─── Pydantic ───
class LoginRequest(BaseModel): username:str; password:str
class UserCreate(BaseModel): username:str; password:str; display_name:str; role:str; custom_fields:Optional[str]="{}"
class OrderCreate(BaseModel):
    customer_name:str=""; product_type:str=""; metal_type:str=""; main_stone:str=""; side_stones:str=""
    material_notes:str=""; special_notes:str=""; budget:float=0; ring_size:str=""; due_date:str=""
    customer_source:str=""; occasion:str=""; design_brief:str=""; image_url:str=""; follow_up_note:str=""
    deposit_amount:float=0; paid_amount:float=0; paid_at:str=""; payment_note:str=""
    material_specs:dict={}
class OrderUpdate(BaseModel):
    customer_name:Optional[str]=None; product_type:Optional[str]=None; metal_type:Optional[str]=None
    main_stone:Optional[str]=None; side_stones:Optional[str]=None; material_notes:Optional[str]=None
    special_notes:Optional[str]=None; budget:Optional[float]=None; ring_size:Optional[str]=None
    due_date:Optional[str]=None; customer_source:Optional[str]=None; occasion:Optional[str]=None
    design_brief:Optional[str]=None; image_url:Optional[str]=None; follow_up_note:Optional[str]=None
    deposit_amount:Optional[float]=None; paid_amount:Optional[float]=None; paid_at:Optional[str]=None; payment_note:Optional[str]=None
    material_specs:Optional[dict]=None
    profit_mode:Optional[str]=None; profit_rate:Optional[float]=None; profit_fixed:Optional[float]=None
    final_price:Optional[float]=None; status:Optional[str]=None
class StatusUpdate(BaseModel): status:str
class PaymentCreate(BaseModel):
    category:str="deposit"; amount:float; paid_at:str=""; note:str=""
class QuoteItemInput(BaseModel): item_name:str; amount:float
class QuoteSubmit(BaseModel): items:list; total:Optional[float]=None
class QuoteChangeReview(BaseModel): action:str="approve"
class AskQuery(BaseModel): question:str; username:str="boss"
class SettingsUpdate(BaseModel): key:str; value:str
class CustomerProfileUpdate(BaseModel):
    age:Optional[str]=None; gender:Optional[str]=None; phone:Optional[str]=None; wechat:Optional[str]=None; city:Optional[str]=None
    address:Optional[str]=None; birthday:Optional[str]=None; preferences:Optional[str]=None; notes:Optional[str]=None
class AfterSaleCreate(BaseModel):
    service_type:str="调整"; content:str; handled_by:str="老板"

@asynccontextmanager
async def lifespan(app):
    init_db()
    seed_demo()
    ensure_demo_story()
    yield

app = FastAPI(title="珠宝协作 v2.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=os.path.join(os.path.dirname(__file__),"assets")), name="assets")

@app.middleware("http")
async def public_demo_guard(request: Request, call_next):
    """公开演示站只允许演示账号读取经过脱敏的数据。"""
    if not PUBLIC_DEMO_MODE or not request.url.path.startswith("/api"):
        return await call_next(request)
    path, method = request.url.path, request.method
    if path == "/api/health":
        return await call_next(request)
    if os.environ.get("DEMO_ACCESS_ENABLED", "true").lower() != "true":
        return JSONResponse({"detail": "演示访问目前已暂停"}, status_code=403)
    if path == "/api/auth/login":
        return await call_next(request)
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token or get_setting(f"token_{token}") != PUBLIC_DEMO_USER:
        return JSONResponse({"detail": "请先登录演示账号"}, status_code=401)
    allowed = (
        path == "/api/auth/verify" or path == "/api/status-flow" or path == "/api/stats"
        or path == "/api/orders" or re.fullmatch(r"/api/orders/\\d+", path)
    )
    if method != "GET" or not allowed:
        return JSONResponse({"detail": "演示账号仅支持查看"}, status_code=403)
    return await call_next(request)

@app.get("/api/health")
def health(): return {"status":"ok","time":datetime.now().isoformat()}

# ─── 认证 API ───
@app.post("/api/auth/login")
def login(data:LoginRequest):
    with get_db() as db:
        u = db.execute("SELECT * FROM users WHERE username=? AND is_active=1",(data.username,)).fetchone()
    if not u or not verify_pw(data.password, u["password_hash"]):
        raise HTTPException(401,"用户名或密码错误")
    token = secrets.token_hex(32)
    # 简单 token 存内存（demo 级别，生产环境用 JWT）
    set_setting(f"token_{token}", data.username)
    return {"token":token,"user":{"username":u["username"],"display_name":u["display_name"],
            "role":u["role"],"custom_fields":u["custom_fields"]}}

@app.post("/api/auth/verify")
def verify_token(request:Request):
    auth = request.headers.get("Authorization","").replace("Bearer ","")
    username = get_setting(f"token_{auth}")
    if not username: raise HTTPException(401,"Token 无效")
    with get_db() as db:
        u = db.execute("SELECT * FROM users WHERE username=?",(username,)).fetchone()
    if not u: raise HTTPException(401,"用户不存在")
    return {"username":u["username"],"display_name":u["display_name"],"role":u["role"]}

# ─── 用户管理（老板专属）───
@app.get("/api/users")
def list_users():
    with get_db() as db:
        rows = db.execute("SELECT id,username,display_name,role,custom_fields,is_active,created_at FROM users").fetchall()
    users = []
    for r in rows:
        d = dict(r)
        d["permissions"] = get_user_permissions(d["role"], d["custom_fields"])
        users.append(d)
    return users

@app.post("/api/users")
def create_user(data:UserCreate):
    with get_db() as db:
        if db.execute("SELECT id FROM users WHERE username=?",(data.username,)).fetchone():
            raise HTTPException(400,"用户名已存在")
        db.execute("INSERT INTO users(username,password_hash,display_name,role,custom_fields) VALUES(?,?,?,?,?)",
                   (data.username,hash_password(data.password),data.display_name,data.role,
                    data.custom_fields or '{"fields":["id","order_number","customer_name","status","created_at"]}'))
    add_notification(0, "新用户", f"{data.display_name}({data.role}) 已创建")
    return {"ok":True,"username":data.username}

@app.put("/api/users/{user_id}")
def update_user(user_id:int, data:dict):
    with get_db() as db:
        for k,v in data.items():
            if k in ("display_name","role","custom_fields","is_active","password"):
                if k=="password":
                    db.execute("UPDATE users SET password_hash=? WHERE id=?",(hash_password(v),user_id))
                else:
                    db.execute(f"UPDATE users SET {k}=? WHERE id=?",(str(v),user_id))
    return {"ok":True}

@app.delete("/api/users/{user_id}")
def delete_user(user_id:int):
    with get_db() as db:
        db.execute("UPDATE users SET is_active=0 WHERE id=?",(user_id,))
    return {"ok":True}

# ─── 订单 API（根据用户角色过滤字段）───
def get_user_role(username):
    if PUBLIC_DEMO_MODE:
        return ("demo_viewer", "{}")
    with get_db() as db:
        u = db.execute("SELECT * FROM users WHERE username=?",(username,)).fetchone()
    return (u["role"], u["custom_fields"]) if u else ("boss","{}")

@app.get("/api/status-flow")
def status_flow():
    return [{"key":s,"label":STATUS_LABELS[s],"description":STATUS_DESCRIPTIONS[s]} for s in STATUS_FLOW]

@app.get("/api/orders")
def list_orders(username:str="boss", status:str="", period:str="", metric:str="", amount_min:float=0, amount_max:float=0, date_from:str="", date_to:str="", source:str="", basis:str="created", payment_status:str=""):
    role, custom = get_user_role(username)
    fields = get_user_fields(role, custom)
    start = date_from or (get_period_start(period) if period else "")
    date_field = "paid_at" if basis == "completed" else "created_at"
    with get_db() as db:
        if role == "master":
            master_statuses = ["pending_quote","quoting","quoted","reviewed"]
            sql = f"SELECT * FROM orders WHERE status IN ({','.join('?' for _ in master_statuses)})"
            params = master_statuses
            if status and status in master_statuses:
                sql += " AND status=?"
                params.append(status)
            sql += " ORDER BY CASE status WHEN 'pending_quote' THEN 1 WHEN 'quoting' THEN 2 WHEN 'quoted' THEN 3 WHEN 'reviewed' THEN 4 END, created_at DESC LIMIT 100"
            rows = db.execute(sql, params).fetchall()
        elif status:
            sql = "SELECT * FROM orders WHERE status=?"
            params = [status]
            if start:
                sql += f" AND {date_field}>=?"
                params.append(start)
            if date_to:
                sql += f" AND {date_field}<=?"
                params.append(date_to + " 23:59:59")
            if source:
                sql += " AND customer_source=?"
                params.append(source)
            sql += f" ORDER BY {date_field} DESC, created_at DESC LIMIT 100"
            rows = db.execute(sql, params).fetchall()
        elif metric in ("revenue","profit"):
            sql = "SELECT * FROM orders WHERE status IN ('confirmed','in_production','delivered')"
            params = []
            if start:
                sql += f" AND {date_field}>=?"
                params.append(start)
            if date_to:
                sql += f" AND {date_field}<=?"
                params.append(date_to + " 23:59:59")
            if source:
                sql += " AND customer_source=?"
                params.append(source)
            sql += f" ORDER BY {date_field} DESC, updated_at DESC LIMIT 100"
            rows = db.execute(sql, params).fetchall()
        elif start:
            sql = f"SELECT * FROM orders WHERE {date_field}>=?"
            params = [start]
            if date_to:
                sql += f" AND {date_field}<=?"
                params.append(date_to + " 23:59:59")
            if source:
                sql += " AND customer_source=?"
                params.append(source)
            sql += f" ORDER BY {date_field} DESC, created_at DESC LIMIT 100"
            rows = db.execute(sql, params).fetchall()
        else:
            if source:
                rows = db.execute("SELECT * FROM orders WHERE customer_source=? ORDER BY created_at DESC LIMIT 100",(source,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 100").fetchall()
    orders = []
    for r in rows:
        if amount_min and (r["final_price"] or 0) < amount_min:
            continue
        if amount_max and (r["final_price"] or 0) > amount_max:
            continue
        d = order_dict(r, fields)
        if payment_status == "paid" and not d.get("is_paid"):
            continue
        if payment_status == "unpaid" and d.get("is_paid"):
            continue
        if any(f in fields for f in ("quote_items","status_history","after_sales","quote_change_requests")):
            with get_db() as db2:
                d = enrich_order_details(db2, d, fields, username)
        orders.append(d)
    return {"orders":orders,"count":len(orders)}

@app.get("/api/orders/{order_id}")
def get_order(order_id:int, username:str="boss"):
    role, custom = get_user_role(username)
    fields = get_user_fields(role, custom)
    with get_db() as db:
        r = db.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    if not r: raise HTTPException(404,"订单不存在")
    d = order_dict(r, fields)
    if any(f in fields for f in ("quote_items","status_history","after_sales","quote_change_requests")):
        with get_db() as db:
            d = enrich_order_details(db, d, fields, username)
    return d

@app.post("/api/orders")
def create_order(data:OrderCreate, username:str="assistant"):
    today = date.today().strftime("%Y%m%d")
    with get_db() as db:
        last = db.execute("SELECT order_number FROM orders WHERE order_number LIKE ? ORDER BY id DESC LIMIT 1",
                          (f"J{today}-%",)).fetchone()
        on = f"J{today}-001" if not last else f"J{today}-{int(last['order_number'].split('-')[1])+1:03d}"
        db.execute("""INSERT INTO orders(order_number,customer_name,product_type,metal_type,main_stone,side_stones,
                   material_notes,special_notes,budget,ring_size,due_date,customer_source,occasion,design_brief,
                   image_url,follow_up_note,deposit_amount,paid_amount,paid_at,payment_note,material_specs,status,created_by)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (on,data.customer_name,data.product_type,data.metal_type,data.main_stone,data.side_stones,
                    data.material_notes,data.special_notes,data.budget,data.ring_size,data.due_date,data.customer_source,
                    data.occasion,data.design_brief,data.image_url,data.follow_up_note,data.deposit_amount,data.paid_amount,
                    data.paid_at,data.payment_note,json.dumps(data.material_specs,ensure_ascii=False),"pending_quote",username))
        r = db.execute("SELECT * FROM orders WHERE order_number=?",(on,)).fetchone()
        add_status_history(db, r["id"], "", "pending_quote", username, "创建订单")
    add_notification(0, "新订单", f"{data.customer_name}的{data.product_type}已创建，订单号 {on}")
    return order_dict(r, username)

@app.put("/api/orders/{order_id}")
def update_order(order_id:int, data:OrderUpdate, username:str="boss"):
    role, custom = get_user_role(username)
    perms = get_user_permissions(role, custom)
    with get_db() as db:
        old_order = db.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
        if not old_order: raise HTTPException(404)
        updates = {k:v for k,v in data.dict(exclude_none=True).items()}
        price_keys = {"final_price","profit_mode","profit_rate","profit_fixed"}
        if price_keys & set(updates) and not perms.get("price_edit"):
            raise HTTPException(403, "当前账号不能修改订单利润或总价")
        legacy_payment_keys = {"deposit_amount","paid_amount","paid_at","payment_note"}
        if legacy_payment_keys & set(updates) and not perms.get("payment"):
            raise HTTPException(403, "当前账号不能维护收款信息")
        profit_note = ""
        if "profit_mode" in updates:
            if updates["profit_mode"] not in ("percent","fixed"):
                raise HTTPException(400, "未知利润模式")
        if "profit_rate" in updates:
            updates["profit_rate"] = max(float(updates["profit_rate"] or 0), 0)
        if "profit_fixed" in updates:
            updates["profit_fixed"] = max(float(updates["profit_fixed"] or 0), 0)
        if {"profit_mode","profit_rate","profit_fixed"} & set(updates):
            next_mode = updates.get("profit_mode", old_order["profit_mode"] or "percent")
            next_rate = float(updates.get("profit_rate", old_order["profit_rate"] or DEFAULT_PROFIT_RATE) or 0)
            next_fixed = float(updates.get("profit_fixed", old_order["profit_fixed"] or 0) or 0)
            old_mode = old_order["profit_mode"] or "percent"
            old_desc = f"百分比利润 {float(old_order['profit_rate'] or 0) * 100:.1f}%" if old_mode != "fixed" else f"固定利润 ¥{float(old_order['profit_fixed'] or 0):,.0f}"
            new_desc = f"百分比利润 {next_rate * 100:.1f}%" if next_mode != "fixed" else f"固定利润 ¥{next_fixed:,.0f}"
            profit_note = f"利润调整：{old_desc} → {new_desc}"
        if "final_price" in updates:
            final = float(updates["final_price"] or 0)
            cost_total = float(old_order["cost_total"] or 0)
            updates["profit"] = round(final - cost_total, 2)
            updates["profit_mode"] = "fixed"
            updates["profit_fixed"] = updates["profit"]
            updates["profit_rate"] = round((final - cost_total) / cost_total, 4) if cost_total else 0
            profit_note = f"订单总价调整：¥{float(old_order['final_price'] or 0):,.0f} → ¥{final:,.0f}，系统转为固定利润 ¥{updates['profit_fixed']:,.0f}"
        public_changes, sensitive_changes = summarize_order_updates(old_order, updates)
        if "material_specs" in updates:
            updates["material_specs"] = json.dumps(updates["material_specs"], ensure_ascii=False)
        if updates:
            db.execute(f"UPDATE orders SET {', '.join(f'{k}=?' for k in updates)}, updated_at=datetime('now','localtime') WHERE id=?",
                       list(updates.values())+[order_id])
        if ({"profit_mode","profit_rate","profit_fixed"} & set(updates)) and "final_price" not in data.dict(exclude_none=True):
            recalc_order(db, order_id)
        if "final_price" in updates:
            sync_order_payment_summary(db, order_id)
        if profit_note:
            r2 = db.execute("SELECT status FROM orders WHERE id=?",(order_id,)).fetchone()
            add_status_history(db, order_id, r2["status"], r2["status"], username, profit_note, force=True, event_type="financial", is_sensitive=True)
        if public_changes:
            r2 = db.execute("SELECT status FROM orders WHERE id=?",(order_id,)).fetchone()
            add_status_history(db, order_id, r2["status"], r2["status"], username, "资料修改：" + "；".join(public_changes), force=True, event_type="data", is_sensitive=False)
        if sensitive_changes:
            r2 = db.execute("SELECT status FROM orders WHERE id=?",(order_id,)).fetchone()
            add_status_history(db, order_id, r2["status"], r2["status"], username, "财务资料修改：" + "；".join(sensitive_changes), force=True, event_type="financial", is_sensitive=True)
        r = db.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    return order_dict(r, username)

@app.post("/api/orders/{order_id}/payments")
def create_payment(order_id:int, data:PaymentCreate, username:str="boss"):
    role, custom = get_user_role(username)
    perms = get_user_permissions(role, custom)
    if not perms.get("payment"):
        raise HTTPException(403, "当前账号不能维护收款信息")
    if data.amount <= 0:
        raise HTTPException(400, "收款金额必须大于 0")
    if data.category not in ("deposit","partial","balance","adjustment"):
        raise HTTPException(400, "未知收款类别")
    paid_at = data.paid_at or date.today().isoformat()
    with get_db() as db:
        order = db.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
        if not order: raise HTTPException(404, "订单不存在")
        db.execute("""INSERT INTO payment_records(order_id,category,amount,paid_at,submitted_by,note)
            VALUES(?,?,?,?,?,?)""",(order_id,data.category,float(data.amount),paid_at,username,data.note))
        sync_order_payment_summary(db, order_id)
        r = db.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
        add_status_history(db, order_id, r["status"], r["status"], username,
            f"新增收款：{data.category} ¥{float(data.amount):,.0f}，支付日期 {paid_at}", force=True, event_type="payment", is_sensitive=True)
    add_notification(0, "新增收款", f"订单 {r['order_number']} 新增收款 ¥{data.amount:,.0f}")
    return order_dict(r, username)

@app.delete("/api/orders/{order_id}/payments/{payment_id}")
def delete_payment(order_id:int, payment_id:int, username:str="boss"):
    role, custom = get_user_role(username)
    perms = get_user_permissions(role, custom)
    if not perms.get("payment"):
        raise HTTPException(403, "当前账号不能维护收款信息")
    with get_db() as db:
        payment = db.execute("SELECT * FROM payment_records WHERE id=? AND order_id=?",(payment_id, order_id)).fetchone()
        db.execute("DELETE FROM payment_records WHERE id=? AND order_id=?",(payment_id, order_id))
        sync_order_payment_summary(db, order_id)
        r = db.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
        if payment:
            add_status_history(db, order_id, r["status"], r["status"], username,
                f"删除收款：{payment['category']} ¥{float(payment['amount'] or 0):,.0f}，支付日期 {payment['paid_at']}", force=True, event_type="payment", is_sensitive=True)
    return order_dict(r, username)

@app.put("/api/orders/{order_id}/status")
def update_status(order_id:int, data:StatusUpdate, username:str="boss"):
    if data.status not in STATUS_FLOW:
        raise HTTPException(400, "未知订单状态")
    with get_db() as db:
        old = db.execute("SELECT status FROM orders WHERE id=?",(order_id,)).fetchone()
        if not old: raise HTTPException(404, "订单不存在")
        db.execute("UPDATE orders SET status=?,updated_at=datetime('now','localtime') WHERE id=?",(data.status,order_id))
        add_status_history(db, order_id, old["status"], data.status, username)
        r = db.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    add_notification(0, "状态变更", f"订单 {r['order_number']}：{STATUS_LABELS.get(old['status'],old['status'])} → {STATUS_LABELS.get(data.status,data.status)}")
    return order_dict(r, username)

@app.delete("/api/orders/{order_id}")
def delete_order(order_id:int):
    with get_db() as db: db.execute("DELETE FROM orders WHERE id=?",(order_id,))
    return {"ok":True}

@app.post("/api/orders/{order_id}/quote")
def submit_quote(order_id:int, data:QuoteSubmit, username:str="master"):
    pending_notice = None
    protected_result = None
    with get_db() as db:
        old = db.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
        if not old: raise HTTPException(404, "订单不存在")
        previous_items = fetch_quote_items(db, order_id)
        protected_status = old["status"] in ("reviewed","sent_to_client","confirmed","in_production","delivered")
        if protected_status:
            normalized = normalize_quote_items(data.items)
            db.execute("""INSERT INTO quote_change_requests(order_id,requested_by,items_json,reason)
                VALUES(?,?,?,?)""",(order_id,username,json.dumps(normalized,ensure_ascii=False),"老板已审核后申请修改成本"))
            diff = summarize_quote_diff(previous_items, normalized)
            note = "提交成本修改申请，等待老板审批"
            if diff:
                note += "：" + "；".join(diff)
            add_status_history(db, order_id, old["status"], old["status"], username, note, force=True, event_type="quote", is_sensitive=True)
            db.execute("INSERT INTO notifications(user_id,title,message,channel) VALUES(?,?,?,?)",
                       (None, "成本修改申请", f"订单 {old['order_number']} 老师傅申请修改成本", "in_app"))
            pending_notice = ("成本修改申请", f"订单 {old['order_number']} 老师傅申请修改成本，请老板审批是否退回待报价")
            r = db.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
            protected_result = order_dict(r, username)
        else:
            (cost, profit, final), normalized = replace_quote_items(db, order_id, data.items)
            db.execute("UPDATE orders SET status='quoted',updated_at=datetime('now','localtime') WHERE id=?",(order_id,))
            add_status_history(db, order_id, old["status"] if old else "", "quoted", username, "提交报价")
            diff = summarize_quote_diff(previous_items, normalized)
            detail = "；".join(diff) if diff else f"{len(normalized)} 项，成本合计 {format_money(cost)}"
            add_status_history(db, order_id, "quoted", "quoted", username,
                f"成本明细更新：{detail}", force=True, event_type="quote", is_sensitive=True)
            r = db.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
            quote_items = [dict(it) for it in db.execute("SELECT * FROM quote_items WHERE order_id=?",(order_id,)).fetchall()]
    if pending_notice:
        try:
            send_configured_wecom(*pending_notice)
        except Exception as e:
            print(f"企业微信通知发送失败：{e}")
        return protected_result
    add_notification(0, "报价完成", f"订单 {r['order_number']} 成本¥{cost:,.0f} → 报价¥{final:,.0f}")
    d = order_dict(r, username)
    d["quote_items"] = quote_items
    return d

@app.post("/api/quote-change-requests/{request_id}/review")
def review_quote_change_request(request_id:int, data:QuoteChangeReview, username:str="boss"):
    role, custom = get_user_role(username)
    if role != "boss":
        raise HTTPException(403, "只有老板可以审批成本修改申请")
    with get_db() as db:
        req = db.execute("SELECT * FROM quote_change_requests WHERE id=?",(request_id,)).fetchone()
        if not req: raise HTTPException(404, "申请不存在")
        order = db.execute("SELECT * FROM orders WHERE id=?",(req["order_id"],)).fetchone()
        if data.action == "approve":
            items = json.loads(req["items_json"] or "[]")
            previous_items = fetch_quote_items(db, req["order_id"])
            (cost, profit, final), normalized = replace_quote_items(db, req["order_id"], items)
            db.execute("UPDATE orders SET status='pending_quote',updated_at=datetime('now','localtime') WHERE id=?",(req["order_id"],))
            db.execute("UPDATE quote_change_requests SET status='approved',reviewed_by=?,reviewed_at=datetime('now','localtime') WHERE id=?",(username,request_id))
            diff = summarize_quote_diff(previous_items, normalized)
            note = "批准成本修改申请，退回待报价"
            if diff:
                note += "：" + "；".join(diff)
            add_status_history(db, req["order_id"], order["status"], "pending_quote", username, note, event_type="quote", is_sensitive=True)
        else:
            db.execute("UPDATE quote_change_requests SET status='rejected',reviewed_by=?,reviewed_at=datetime('now','localtime') WHERE id=?",(username,request_id))
            add_status_history(db, req["order_id"], order["status"], order["status"], username, "拒绝成本修改申请", force=True, event_type="quote", is_sensitive=True)
        r = db.execute("SELECT * FROM orders WHERE id=?",(req["order_id"],)).fetchone()
    return order_dict(r, username)

@app.post("/api/orders/{order_id}/review")
def review_order(order_id:int):
    with get_db() as db:
        db.execute("UPDATE orders SET status='reviewed',updated_at=datetime('now','localtime') WHERE id=?",(order_id,))
        add_status_history(db, order_id, "quoted", "reviewed", "boss", "审核通过")
        r = db.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    add_notification(0, "审核通过", f"订单 {r['order_number']} 已审核，报价¥{r['final_price']:,.0f}")
    return order_dict(r, "boss")

@app.post("/api/orders/{order_id}/send-to-client")
def send_to_client(order_id:int):
    with get_db() as db:
        db.execute("UPDATE orders SET status='sent_to_client',updated_at=datetime('now','localtime') WHERE id=?",(order_id,))
        add_status_history(db, order_id, "reviewed", "sent_to_client", "boss", "标记已发客户")
        r = db.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    return order_dict(r, "boss")

@app.post("/api/orders/{order_id}/after-sales")
def create_after_sale(order_id:int, data:AfterSaleCreate, username:str="boss"):
    with get_db() as db:
        order = db.execute("SELECT customer_name FROM orders WHERE id=?",(order_id,)).fetchone()
        if not order: raise HTTPException(404, "订单不存在")
        db.execute("""INSERT INTO after_sales(order_id,customer_name,service_type,content,handled_by)
            VALUES(?,?,?,?,?)""",(order_id,order["customer_name"],data.service_type,data.content,data.handled_by or username))
        rows = [dict(r) for r in db.execute("SELECT * FROM after_sales WHERE order_id=? ORDER BY created_at DESC",(order_id,)).fetchall()]
        current = db.execute("SELECT status FROM orders WHERE id=?",(order_id,)).fetchone()
        add_status_history(db, order_id, current["status"], current["status"], username,
            f"售后记录：{data.service_type} - {data.content}", force=True, event_type="after_sale", is_sensitive=False)
    add_notification(0, "售后记录", f"{order['customer_name']} 的订单新增售后：{data.service_type}")
    return {"ok":True,"after_sales":rows}

# ─── 统计 API ───
@app.get("/api/stats")
def get_stats(username:str="boss", period:str="month", basis:str="created", trend_days:int=180):
    role, custom = get_user_role(username)
    start = get_period_start(period)
    date_field = "paid_at" if basis == "completed" else "created_at"
    with get_db() as db:
        if role == "master":
            allowed_statuses = ("pending_quote","quoting","quoted","reviewed")
            placeholders = ",".join("?" for _ in allowed_statuses)
            bs = {r["status"]:r["c"] for r in db.execute(
                f"SELECT status,COUNT(*) as c FROM orders WHERE status IN ({placeholders}) GROUP BY status",
                allowed_statuses).fetchall()}
            rows = db.execute(f"""SELECT * FROM orders WHERE status IN ({placeholders})
                ORDER BY CASE status WHEN 'pending_quote' THEN 1 WHEN 'quoting' THEN 2 WHEN 'quoted' THEN 3 WHEN 'reviewed' THEN 4 END,
                created_at DESC LIMIT 8""", allowed_statuses).fetchall()
            fields = get_user_fields(role, custom)
            workbench = [order_dict(r, fields) for r in rows]
            return {
                "total_orders": sum(bs.values()),
                "period_orders": sum(bs.values()),
                "month_orders": sum(bs.values()),
                "month_revenue": 0,
                "month_profit": 0,
                "pending_quote": bs.get("pending_quote",0) + bs.get("quoting",0),
                "need_review": bs.get("quoted",0),
                "reviewed_quote": bs.get("reviewed",0),
                "pending_client_confirm": 0,
                "pending_revenue": 0,
                "weekly_new": 0,
                "date_basis": basis,
                "date_basis_label": "订单创建日期",
                "workbench": workbench,
                "by_status": {s:bs.get(s,0) for s in STATUS_FLOW},
                "status_labels": STATUS_LABELS,
            }
        total = db.execute("SELECT COUNT(*) as c FROM orders").fetchone()["c"]
        bs = {r["status"]:r["c"] for r in db.execute(f"SELECT status,COUNT(*) as c FROM orders WHERE {date_field}>=? GROUP BY status",(start,)).fetchall()}
        mo = db.execute(f"SELECT COUNT(*) as c FROM orders WHERE {date_field}>=?",(start,)).fetchone()["c"]
        mr = db.execute(f"SELECT COALESCE(SUM(final_price),0) as s FROM orders WHERE status IN ('confirmed','in_production','delivered') AND {date_field}>=?",(start,)).fetchone()["s"]
        mp = db.execute(f"SELECT COALESCE(SUM(profit),0) as s FROM orders WHERE status IN ('confirmed','in_production','delivered') AND {date_field}>=?",(start,)).fetchone()["s"]

        # 本月待收款（已发客户但未确认）
        pending_revenue = db.execute("SELECT COALESCE(SUM(final_price),0) as s FROM orders WHERE status='sent_to_client'").fetchone()["s"]

        # 本周新增
        wk = (date.today()-timedelta(days=date.today().weekday())).isoformat()
        weekly = db.execute(f"SELECT COUNT(*) as c FROM orders WHERE {date_field}>=?",(wk,)).fetchone()["c"]
        workbench = [dict(r) for r in db.execute("""SELECT id,order_number,customer_name,product_type,main_stone,status,
            final_price,due_date,follow_up_note,image_url FROM orders
            WHERE status IN ('pending_quote','quoted','reviewed','sent_to_client')
            ORDER BY CASE status WHEN 'quoted' THEN 1 WHEN 'pending_quote' THEN 2 WHEN 'sent_to_client' THEN 3 WHEN 'reviewed' THEN 4 END,
            due_date ASC LIMIT 6""").fetchall()]

        if PUBLIC_DEMO_MODE:
            fields = get_user_fields(role, custom)
            workbench = [order_dict(row, fields) for row in workbench]

        perms = get_user_permissions(role, custom)
        if not perms.get("dashboard_finance"):
            mr = 0
            mp = 0
            pending_revenue = 0
        chart_data = build_chart_series(db, date_field, trend_days) if perms.get("dashboard_charts") else {"orders": [], "finance": [], "granularity": "day"}
        return {
            "total_orders":total,"period_orders":mo,"month_orders":mo,"month_revenue":mr,"month_profit":mp,
            "pending_quote":bs.get("pending_quote",0),"need_review":bs.get("quoted",0),
            "pending_client_confirm":bs.get("sent_to_client",0),
            "pending_revenue":pending_revenue,
            "weekly_new":weekly,
            "date_basis": basis,
            "date_basis_label": "付款日期" if basis == "completed" else "订单创建日期",
            "workbench": workbench,
            "chart_data": chart_data,
            "by_status":{s:bs.get(s,0) for s in STATUS_FLOW},
            "status_labels":STATUS_LABELS,
        }

# ─── 客户画像 API ───
@app.get("/api/customers/{name}")
def customer_profile(name:str, username:str="boss"):
    role, custom = get_user_role(username)
    perms = get_user_permissions(role, custom)
    with get_db() as db:
        rows = db.execute("SELECT * FROM orders WHERE customer_name LIKE ? ORDER BY created_at DESC",(f"%{name}%",)).fetchall()
        profile = db.execute("SELECT * FROM customer_profiles WHERE name=?",(name,)).fetchone()
        if not rows and not profile: raise HTTPException(404,"未找到该客户")

        total_spent = sum(o["final_price"] or 0 for o in rows if o["status"] not in ("draft",))
        total_orders = len(rows)
        avg_order = total_spent / total_orders if total_orders > 0 else 0
        products = {}
        for o in rows:
            products[o["product_type"]] = products.get(o["product_type"],0) + 1
        first_order = rows[-1]["created_at"] if rows else ""
        last_order = rows[0]["created_at"] if rows else ""

        profile_dict = dict(profile) if profile else {"name":name,"age":"","gender":"","phone":"","wechat":"","city":"","address":"","birthday":"","preferences":"","notes":""}
        if not perms.get("customer_sensitive"):
            profile_dict["phone"] = "仅老板可见"
            profile_dict["wechat"] = "仅老板可见"
            profile_dict["address"] = "仅老板可见"
        return {
            "name":name,"total_orders":total_orders,"total_spent":total_spent,
            "avg_order":round(avg_order,0),"products":products,
            "first_order":first_order,"last_order":last_order,
            "orders":[order_dict(r) for r in rows],
            "profile":profile_dict,
            "is_vip":total_spent > 50000,
            "is_regular":total_orders >= 3,
        }

@app.put("/api/customers/{name}")
def update_customer_profile(name:str, data:CustomerProfileUpdate, username:str="boss"):
    role, custom = get_user_role(username)
    perms = get_user_permissions(role, custom)
    if not perms.get("customer_sensitive"):
        raise HTTPException(403, "当前账号不能维护客户敏感资料")
    updates = data.dict(exclude_none=True)
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO customer_profiles(name) VALUES(?)",(name,))
        if updates:
            db.execute(f"UPDATE customer_profiles SET {', '.join(f'{k}=?' for k in updates)}, updated_at=datetime('now','localtime') WHERE name=?",
                       list(updates.values())+[name])
        row = db.execute("SELECT * FROM customer_profiles WHERE name=?",(name,)).fetchone()
    return dict(row)

def extract_customer_name_from_text(question, known_names=None):
    known_names = known_names or []
    patterns = [
        r"(?:拿到|客户|给|把)\s*([一-龥]{2,4})(?:的)?(?:电话|手机号|手机|联系方式|微信|微信号|生日|城市|住在|居住地|喜欢|偏好|喜好)",
        r"([一-龥]{2,4})(?:的)(?:电话|手机号|手机|联系方式|微信|微信号|生日|城市|住在|居住地|喜欢|偏好|喜好)",
    ]
    for pattern in patterns:
        m = re.search(pattern, question)
        if m:
            return m.group(1).rstrip("的电手微")
    blocked = {"我刚拿到","刚拿到","客户资料"}
    found = next((n for n in known_names if n in question and n not in blocked), "")
    if found:
        return found
    return ""

def try_ai_update_customer(question, username="boss"):
    role, custom = get_user_role(username)
    if role != "boss":
        return None
    with get_db() as db:
        known_names = [r["name"] for r in db.execute("SELECT name FROM customer_profiles").fetchall()]
    name = extract_customer_name_from_text(question, known_names)
    if not name: return None
    field_patterns = [
        ("phone", r'(?:电话|手机号|手机|联系方式)(?:是|为|:|：)?\s*([0-9*\-]{6,20})'),
        ("wechat", r'(?:微信|微信号)(?:是|为|:|：)?\s*([A-Za-z0-9_\-]{3,30})'),
        ("city", r'(?:城市|住在|居住在)(?:是|为|:|：)?\s*([一-龥]{2,12})'),
        ("birthday", r'(?:生日)(?:是|为|:|：)?\s*([0-9]{4}[-/.年][0-9]{1,2}[-/.月][0-9]{1,2}日?)'),
        ("preferences", r'(?:喜欢|偏好|喜好)(?:是|为|:|：)?\s*([^，。；;]+)'),
    ]
    updates = {}
    for field, pattern in field_patterns:
        m = re.search(pattern, question)
        if m:
            updates[field] = m.group(1).strip()
    if not updates:
        return None
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO customer_profiles(name) VALUES(?)",(name,))
        db.execute(f"UPDATE customer_profiles SET {', '.join(f'{k}=?' for k in updates)}, updated_at=datetime('now','localtime') WHERE name=?",
                   list(updates.values())+[name])
        row = db.execute("SELECT * FROM customer_profiles WHERE name=?",(name,)).fetchone()
    return {"answer":f"已更新 {name} 的客户资料：{', '.join(updates.keys())}。","type":"customer_update","model":"rule","profile":dict(row)}

@app.get("/api/customers")
def list_customers():
    """所有客户的消费排行"""
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT customer_name, COUNT(*) as order_count, SUM(final_price) as total_spent, MAX(created_at) as last_order FROM orders WHERE status NOT IN ('draft','') GROUP BY customer_name ORDER BY total_spent DESC"
        ).fetchall()]
        existing = {r["customer_name"] for r in rows}
        for p in db.execute("SELECT name FROM customer_profiles ORDER BY updated_at DESC").fetchall():
            if p["name"] not in existing:
                rows.append({"customer_name":p["name"],"order_count":0,"total_spent":0,"last_order":""})
    return [{"name":r["customer_name"],"order_count":r["order_count"],
             "total_spent":r["total_spent"] or 0,"last_order":r["last_order"],
             "is_vip":(r["total_spent"] or 0) > 50000} for r in rows]

# ─── 报价卡片生成 ───
@app.get("/api/orders/{order_id}/quote-card")
def get_quote_card(order_id:int):
    """生成可复制到微信的报价卡片文本"""
    with get_db() as db:
        r = db.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
        if not r: raise HTTPException(404)
        shop = get_setting("shop_name","珠宝定制工作室")

    final = r["final_price"] or 0
    metal = r["metal_type"] or ""
    product = r["product_type"] or "珠宝"
    stone = r["main_stone"] or ""
    due = r["due_date"] or "确认后排期"
    brief = r["design_brief"] or "按沟通方案定制"

    card = f"""━━━━━━━━━━━━━━━━━━
  ✨ {shop} ✨
━━━━━━━━━━━━━━━━━━
  为您精心定制

  {metal}{stone}{product}
  {brief}

  💰 专属报价

      ¥ {final:,.0f}

━━━━━━━━━━━━━━━━━━
  ✓ 精选优质材料
  ✓ 精工细作
  ✓ 品质保证
  ✓ 预计交付：{due}

  报价有效期：7 天
  如有疑问请联系我们
━━━━━━━━━━━━━━━━━━
"""
    return {"card_text":card,"order_number":r["order_number"],"final_price":final}

# ─── AI 问答（真实模型 + 回退规则引擎）───
AI_SYSTEM_PROMPT = """你是珠宝店老板的业务助手。你会收到系统整理好的真实订单、客户和经营摘要。

回答格式：
- 用简洁、像店铺运营助理一样的中文回答
- 先给结论，再列关键订单或客户
- 涉及报价、利润、客户历史时，只能使用上下文里的数字
- 如果用户要微信话术，直接给可复制的话术
- 不要说自己能查询数据库，也不要编造上下文之外的数据"""

def load_ai_config():
    cfg = dict(AI_CONFIG)
    cfg["provider"] = get_setting("ai_provider", cfg["provider"]) or "openai_compatible"
    cfg["base_url"] = get_setting("ai_base_url", cfg["base_url"]) or "https://openrouter.ai/api/v1/chat/completions"
    cfg["model"] = get_setting("ai_model", cfg["model"]) or "openrouter/auto"
    cfg["api_key"] = get_setting("ai_api_key", "") or get_setting("anthropic_api_key", "") or cfg["api_key"]
    return cfg

def normalize_chat_url(base_url):
    url = (base_url or "").strip().rstrip("/")
    if not url:
        url = "https://openrouter.ai/api/v1/chat/completions"
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"

def read_http_error(e):
    try:
        body = e.read().decode("utf-8")
        data = json.loads(body)
        return data.get("error", {}).get("message") or data.get("message") or body[:220]
    except Exception:
        return str(e)

def build_ai_context(username="boss"):
    role, custom = get_user_role(username)
    fields = get_user_fields(role, custom)
    can_see_money = role == "boss" or "profit" in fields or "cost_total" in fields
    with get_db() as db:
        stats = get_stats(username)
        orders = db.execute("""SELECT * FROM orders
            WHERE status NOT IN ('delivered','draft')
            ORDER BY CASE status WHEN 'quoted' THEN 1 WHEN 'pending_quote' THEN 2 WHEN 'sent_to_client' THEN 3
            WHEN 'reviewed' THEN 4 WHEN 'confirmed' THEN 5 WHEN 'in_production' THEN 6 END, due_date ASC LIMIT 12""").fetchall()
        customers = db.execute("""SELECT customer_name, COUNT(*) as order_count, SUM(final_price) as total_spent,
            MAX(created_at) as last_order FROM orders WHERE status NOT IN ('draft','')
            GROUP BY customer_name ORDER BY total_spent DESC LIMIT 8""").fetchall()

    lines = [
        f"当前用户：{username}，角色：{role}",
        f"今日日期：{date.today().isoformat()}",
        f"经营摘要：待报价 {stats['pending_quote']} 单，待老板审核 {stats['need_review']} 单，已发客户待确认 {stats['pending_client_confirm']} 单，总订单 {stats['total_orders']} 单。",
    ]
    if can_see_money:
        lines.append(f"老板可见金额：本月营收 ¥{stats['month_revenue']:,.0f}，本月利润 ¥{stats['month_profit']:,.0f}，待收款 ¥{stats['pending_revenue']:,.0f}。")
    else:
        lines.append("该角色不能查看成本、利润和经营金额。")

    lines.append("\n活跃订单：")
    for o in orders:
        parts = [
            f"{o['order_number']} {STATUS_LABELS.get(o['status'], o['status'])}",
            f"客户:{o['customer_name'] if role != 'master' else '隐藏'}",
            f"产品:{o['metal_type']}{o['main_stone']}{o['product_type']}",
            f"预算:{'¥'+format(o['budget'] or 0, ',.0f') if role != 'master' else '隐藏'}",
            f"交付:{o['due_date'] or '未定'}",
            f"来源:{o['customer_source'] or '未填'}",
            f"需求:{o['design_brief'] or o['material_notes'] or '未填'}",
            f"跟进:{o['follow_up_note'] or '无'}",
        ]
        if can_see_money:
            parts.append(f"成本:¥{(o['cost_total'] or 0):,.0f}")
            parts.append(f"报价:¥{(o['final_price'] or 0):,.0f}")
            parts.append(f"利润:¥{(o['profit'] or 0):,.0f}")
        lines.append("；".join(parts))

    lines.append("\n客户排行：")
    for c in customers:
        amount = f"¥{(c['total_spent'] or 0):,.0f}" if can_see_money else "金额隐藏"
        lines.append(f"{c['customer_name']}：{c['order_count']} 单，累计 {amount}，最近 {c['last_order'][:10] if c['last_order'] else '-'}")
    return "\n".join(lines)

def rule_based_answer(question:str) -> dict:
    """规则引擎（AI 不可用时的回退方案）"""
    q = question.strip()
    scores = {"pending_summary":0,"pending_quote":0,"need_review":0,"unfinished":0,
              "profit":0,"customer":0,"today":0,"overview":0,"customers_top":0,"followup":0}
    for kw in ["还没做","还没有做","未处理","待处理","待办","需要我做","需要我处理","需要你整理","帮我整理","全部事项","哪些还没有","哪些还没","还有什么","哪些工作","卡在谁","谁卡在","没完成","处理的事项","处理一下","还没","还没有","有哪些订单","哪些订单","还有哪些","帮我看看","我需要处理","需要我处理的事","需要你帮我"]:
        if kw in q: scores["pending_summary"]+=1
    for kw in ["等师傅","师傅报价","还没报价","待报价","等报价"]:
        if kw in q: scores["pending_quote"]+=1
    for kw in ["等我","老板审核","还没审核","我还没看","待审核","需要审核"]:
        if kw in q: scores["need_review"]+=1
    for kw in ["没完成","未完成","进行中","还没完成","还没交付"]:
        if kw in q: scores["unfinished"]+=1
    for kw in ["利润","赚了多少","赚了","盈利","收入","营收"]:
        if kw in q: scores["profit"]+=1
    for kw in ["客户排行","最好的客户","哪些客户","客户消费","客户价值","大客户","VIP"]:
        if kw in q: scores["customers_top"]+=1
    for kw in ["跟进","话术","发给","怎么说","微信", "催确认"]:
        if kw in q: scores["followup"]+=2
    for kw in ["订过","做过"]:
        if kw in q: scores["customer"]+=1
    for kw in ["今天","今日"]:
        if kw in q: scores["today"]+=1
    for kw in ["总体","所有订单","全部","看板","概览","情况","状态"]:
        if kw in q: scores["overview"]+=1
    # pending_summary 加权：含"处理"/"待办"等强烈意图词时给额外分
    for kw in ["处理","整理","待办"]:
        if kw in q: scores["pending_summary"] += 2
    best = max(scores, key=scores.get)
    if scores[best]==0:
        return {"answer":"🤔 我能回答：\n📋 待办 | 💰 利润 | 👤 客户 | 📈 看板 | 🏆 客户排行","type":"help","model":"rule"}

    with get_db() as db:
        if best=="profit":
            ms = date.today().replace(day=1).isoformat()
            md = db.execute("SELECT COALESCE(SUM(profit),0) as p, COUNT(*) as c FROM orders WHERE status IN ('confirmed','in_production','delivered') AND updated_at>=?",(ms,)).fetchone()
            ad = db.execute("SELECT COALESCE(SUM(profit),0) as p, COUNT(*) as c FROM orders WHERE status IN ('confirmed','in_production','delivered')").fetchone()
            return {"answer":f"💰 **利润概览**\n\n📅 本月：¥{md['p']:,.0f}（{md['c']} 单）\n📊 累计：¥{ad['p']:,.0f}（{ad['c']} 单）\n📈 待收款：¥{db.execute('SELECT COALESCE(SUM(final_price),0) as s FROM orders WHERE status=\"sent_to_client\"',()).fetchone()['s']:,.0f}","type":"stats","model":"rule"}

        if best=="pending_summary":
            pending = db.execute("SELECT * FROM orders WHERE status IN ('pending_quote','quoting') ORDER BY created_at ASC").fetchall()
            quoted = db.execute("SELECT * FROM orders WHERE status='quoted'").fetchall()
            a = "📋 **待处理事项**\n\n"
            if pending:
                a += f"🔴 等师傅报价（{len(pending)}单）：\n"
                for o in pending:
                    a += f"  • {o['order_number']} {o['customer_name']}的{o['metal_type']}{o['product_type']}"
                    if o['main_stone']: a += f"（{o['main_stone']}）"
                    a += f" — {o['created_at'][:10]}\n"
            else: a += "🔴 等报价：无\n"
            if quoted:
                a += f"\n🟡 等老板审核（{len(quoted)}单）：\n"
                for o in quoted:
                    a += f"  • {o['order_number']} {o['customer_name']}的{o['product_type']} — ¥{(o['final_price'] or 0):,.0f}\n"
            else: a += "\n🟡 等审核：无\n"
            if not pending and not quoted: a += "\n✅ 全部处理完毕！"
            return {"answer":a,"type":"summary","model":"rule"}

        if best=="pending_quote":
            rows = db.execute("SELECT * FROM orders WHERE status IN ('pending_quote','quoting') ORDER BY created_at ASC").fetchall()
            if not rows: return {"answer":"✅ 没有在等报价的订单。","type":"list","model":"rule"}
            a = f"🔴 **等师傅报价**（{len(rows)}单）：\n\n"
            for o in rows:
                a += f"  • {o['order_number']} {o['customer_name']}的{o['metal_type']}{o['product_type']}"
                if o['main_stone']: a += f"（{o['main_stone']}）"
                a += f" — {o['created_at'][:10]}\n"
            return {"answer":a,"type":"list","model":"rule"}

        if best=="need_review":
            rows = db.execute("SELECT * FROM orders WHERE status='quoted' ORDER BY created_at ASC").fetchall()
            if not rows: return {"answer":"✅ 没有需要审核的报价。","type":"list","model":"rule"}
            a = f"🟡 **等你审核**（{len(rows)}单）：\n\n"
            for o in rows:
                a += f"  • {o['order_number']} {o['customer_name']}的{o['product_type']} — 成本¥{(o['cost_total'] or 0):,.0f}，报价¥{(o['final_price'] or 0):,.0f}\n"
            return {"answer":a,"type":"list","model":"rule"}

        if best=="customers_top":
            rows = db.execute("SELECT customer_name, COUNT(*) as oc, SUM(final_price) as ts FROM orders WHERE status NOT IN ('draft','') GROUP BY customer_name ORDER BY ts DESC LIMIT 10").fetchall()
            a = "🏆 **客户消费排行**\n\n"
            for i,r in enumerate(rows,1):
                a += f"{i}. {r['customer_name']} — ¥{r['ts']:,.0f}（{r['oc']}单）\n"
            return {"answer":a,"type":"list","model":"rule"}

        if best=="followup":
            known = [r["customer_name"] for r in db.execute("SELECT DISTINCT customer_name FROM orders WHERE customer_name!=''").fetchall()]
            named_customer = next((name for name in known if name in question), "")
            nm = re.search(r'([一-龥]{1,3}(?:太太|小姐|先生|女士|总))', question)
            row = None
            if named_customer:
                row = db.execute("SELECT * FROM orders WHERE customer_name=? ORDER BY updated_at DESC LIMIT 1",(named_customer,)).fetchone()
            elif nm:
                row = db.execute("SELECT * FROM orders WHERE customer_name LIKE ? ORDER BY updated_at DESC LIMIT 1",(f"%{nm.group(1)}%",)).fetchone()
            if not row:
                row = db.execute("SELECT * FROM orders WHERE status='sent_to_client' ORDER BY due_date ASC LIMIT 1").fetchone()
            if not row:
                return {"answer":"目前没有需要跟进客户确认的订单。","type":"followup","model":"rule"}
            price = f"¥{(row['final_price'] or 0):,.0f}" if row["final_price"] else "报价"
            answer = f"""💬 **可发给 {row['customer_name']} 的微信话术**

{row['customer_name']}您好，我这边把{row['metal_type']}{row['main_stone']}{row['product_type']}的方案又确认了一遍。

这版重点是：{row['design_brief'] or row['material_notes'] or '按您之前沟通的方向定制'}。目前报价是 {price}，预计交付时间是 {row['due_date'] or '确认后排期'}。

如果您觉得方向没问题，我这边就帮您锁定材料和制作档期。后续清洗保养我们也会一起跟进，您不用担心。"""
            return {"answer":answer,"type":"followup","model":"rule"}

        if best=="unfinished":
            rows = db.execute("SELECT * FROM orders WHERE status NOT IN ('delivered','draft') ORDER BY CASE status WHEN 'pending_quote' THEN 1 WHEN 'quoting' THEN 2 WHEN 'quoted' THEN 3 WHEN 'reviewed' THEN 4 WHEN 'sent_to_client' THEN 5 WHEN 'confirmed' THEN 6 WHEN 'in_production' THEN 7 END").fetchall()
            if not rows: return {"answer":"✅ 所有订单已完成！","type":"list","model":"rule"}
            groups = {}
            for o in rows:
                groups.setdefault(STATUS_LABELS.get(o["status"],o["status"]),[]).append(o)
            a = "📋 **未完成订单**\n\n"
            for sn,ords in groups.items():
                a += f"**{sn}**（{len(ords)}单）：\n"
                for o in ords:
                    a += f"  • {o['order_number']} {o['customer_name']}的{o['product_type']} — {o['created_at'][:10]}\n"
                a += "\n"
            return {"answer":a,"type":"summary","model":"rule"}

        if best=="today":
            td = date.today().isoformat()
            nt = db.execute("SELECT COUNT(*) as c FROM orders WHERE date(created_at)=?",(td,)).fetchone()["c"]
            ut = db.execute("SELECT COUNT(*) as c FROM orders WHERE date(updated_at)=?",(td,)).fetchone()["c"]
            return {"answer":f"📅 **今日概览**（{td}）\n\n🆕 新建：{nt} 单\n✏️  更新：{ut} 单\n📦 总订单：{db.execute('SELECT COUNT(*) as c FROM orders',()).fetchone()['c']} 单","type":"today","model":"rule"}

        if best=="overview":
            rows = db.execute("SELECT status,COUNT(*) as c FROM orders GROUP BY status ORDER BY CASE status WHEN 'pending_quote' THEN 1 WHEN 'quoting' THEN 2 WHEN 'quoted' THEN 3 WHEN 'reviewed' THEN 4 WHEN 'sent_to_client' THEN 5 WHEN 'confirmed' THEN 6 WHEN 'in_production' THEN 7 WHEN 'delivered' THEN 8 END").fetchall()
            a = "📊 **状态总览**\n\n"
            emoji = {"pending_quote":"🔴","quoting":"🟠","quoted":"🟡","reviewed":"🟢","sent_to_client":"📤","confirmed":"✅","in_production":"🔧","delivered":"🎉"}
            for r in rows:
                a += f"{emoji.get(r['status'],'📌')} {STATUS_LABELS.get(r['status'],r['status'])}：{r['c']} 单\n"
            return {"answer":a+f"\n📦 总计：{sum(r['c'] for r in rows)} 单","type":"overview","model":"rule"}

        if best=="customer":
            nm = re.search(r'([一-龥]{2,4}(?:太太|小姐|先生|女士|总))', question)
            if nm:
                rows = db.execute("SELECT * FROM orders WHERE customer_name LIKE ? ORDER BY created_at DESC",(f"%{nm.group(1)}%",)).fetchall()
                if not rows: return {"answer":f"📭 没有 {nm.group(1)} 的订单。","type":"customer","model":"rule"}
                a = f"👤 **{nm.group(1)}**（{len(rows)}单）\n\n"
                for o in rows:
                    a += f"  • {o['created_at'][:10]} {o['product_type']} ¥{(o['final_price'] or 0):,.0f} {STATUS_LABELS.get(o['status'],'')}\n"
                return {"answer":a,"type":"customer","model":"rule"}
            return {"answer":'🤔 要查哪位客户？例如"张明轩以前订过什么？"',"type":"clarify","model":"rule"}

    return {"answer":"🤔 抱歉，不太理解。","type":"unknown","model":"rule"}

def try_ai_profile_update(question, username="boss"):
    role, custom = get_user_role(username)
    if role != "boss":
        return None
    with get_db() as db:
        names = [r["name"] for r in db.execute("SELECT name FROM customer_profiles").fetchall()]
    name = extract_customer_name_from_text(question, names)
    if not name:
        return None
    patterns = {
        "phone": r"(?:电话|手机号|手机|联系方式)(?:是|为|:|：)?\s*([0-9\-\s]{7,20})",
        "wechat": r"(?:微信|微信号)(?:是|为|:|：)?\s*([A-Za-z0-9_\-]{3,30})",
        "birthday": r"(?:生日)(?:是|为|:|：)?\s*([0-9]{4}[-/年][0-9]{1,2}[-/月][0-9]{1,2}日?)",
        "city": r"(?:城市|住在|居住地)(?:是|为|:|：)?\s*([一-龥]{2,12})",
        "preferences": r"(?:喜欢|偏好|喜好)(?:是|为|:|：)?\s*([一-龥A-Za-z0-9，,、\s]{2,60})",
    }
    updates = {}
    for field, pattern in patterns.items():
        m = re.search(pattern, question)
        if m:
            value = m.group(1).strip().replace("年","-").replace("月","-").replace("日","").replace("/","-")
            updates[field] = value
    if not updates:
        return None
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO customer_profiles(name) VALUES(?)",(name,))
        db.execute(f"UPDATE customer_profiles SET {', '.join(f'{k}=?' for k in updates)}, updated_at=datetime('now','localtime') WHERE name=?",
                   list(updates.values())+[name])
    lines = [f"{k}：{v}" for k,v in updates.items()]
    return {"answer":f"已更新 {name} 的客户资料：\n" + "\n".join(lines), "type":"profile_update", "model":"rule-action"}

def call_openai_compatible(cfg, question, context):
    payload = {
        "model": cfg["model"],
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": f"业务上下文：\n{context}\n\n用户问题：{question}"},
        ],
    }
    req = urllib.request.Request(
        normalize_chat_url(cfg["base_url"]),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type":"application/json","Authorization":f"Bearer {cfg['api_key']}"},
        method="POST",
    )
    try:
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=30, context=context) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {read_http_error(e)}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络连接失败: {e.reason}")
    return data["choices"][0]["message"]["content"]

def call_ai_model(question:str, username:str="boss") -> dict:
    """调用真实 AI 模型"""
    action = try_ai_profile_update(question, username)
    if action:
        return action
    cfg = load_ai_config()
    if not cfg["api_key"]:
        return rule_based_answer(question)
    context = build_ai_context(username)

    try:
        if cfg["provider"] == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=cfg["api_key"])
            message = client.messages.create(
                model=cfg["model"],
                max_tokens=800,
                system=AI_SYSTEM_PROMPT,
                messages=[{"role":"user","content":f"业务上下文：\n{context}\n\n用户问题：{question}"}]
            )
            answer = message.content[0].text
            return {"answer":answer,"type":"ai","model":cfg["model"]}
        if cfg["provider"] in ("openai_compatible","deepseek","openai"):
            answer = call_openai_compatible(cfg, question, context)
            return {"answer":answer,"type":"ai","model":cfg["model"]}
        else:
            return rule_based_answer(question)
    except Exception as e:
        # 回退到规则引擎
        result = rule_based_answer(question)
        result["note"] = f"AI 不可用：{str(e)[:220]}。已使用内置引擎。"
        return result

@app.post("/api/ask")
def ask_question(data:AskQuery):
    update = try_ai_update_customer(data.question, data.username)
    if update:
        return update
    return call_ai_model(data.question, data.username)

@app.get("/api/ai/test")
def test_ai(username:str="boss"):
    result = call_ai_model("请用一句话确认你能看到当前演示订单，并指出最应该优先处理的一单。", username)
    return result

# ─── 通知 API ───
@app.get("/api/notifications")
def get_notifications(limit:int=20):
    with get_db() as db:
        rows = db.execute("SELECT * FROM notifications ORDER BY sent_at DESC LIMIT ?",(limit,)).fetchall()
    return [dict(r) for r in rows]

# ─── 系统配置（开发者/老板）───
@app.get("/api/settings")
def list_settings():
    with get_db() as db:
        rows = db.execute("SELECT * FROM settings").fetchall()
    return {r["key"]:r["value"] for r in rows}

@app.put("/api/settings")
def update_setting(data:SettingsUpdate):
    set_setting(data.key, data.value)
    if data.key in ("ai_api_key","anthropic_api_key"):
        AI_CONFIG["api_key"] = data.value
    if data.key == "ai_provider":
        AI_CONFIG["provider"] = data.value
    if data.key == "ai_model":
        AI_CONFIG["model"] = data.value
    if data.key == "ai_base_url":
        AI_CONFIG["base_url"] = data.value
    return {"ok":True}

@app.post("/api/wecom/test")
def test_wecom(username:str="boss"):
    role, custom = get_user_role(username)
    perms = get_user_permissions(role, custom)
    if role != "boss" and not perms.get("settings"):
        raise HTTPException(403, "只有管理员可以测试企业微信通知")
    try:
        result = send_configured_wecom("企业微信通知测试", "这是一条来自珠宝协作 CRM Demo 的测试消息。")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except urllib.error.URLError as e:
        raise HTTPException(400, f"网络连接失败：{e.reason}")
    except Exception as e:
        raise HTTPException(400, str(e))
    if result is None:
        raise HTTPException(400, "请先在设置中填写企业微信群机器人 Webhook")
    return {"ok": True, "result": result}

# ─── 静态文件 ───
@app.get("/")
def serve_index():
    return FileResponse(
        os.path.join(os.path.dirname(__file__),"index.html"),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control":"no-store, max-age=0"}
    )

if __name__ == "__main__":
    # 环境变量中读取 API key
    AI_CONFIG["api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    print(f"💎 珠宝协作 v2.0 → http://localhost:8899")
    print(f"   手机同 Wi‑Fi 访问: http://你的Mac局域网IP:8899")
    print(f"   AI: {'Claude' if AI_CONFIG['api_key'] else '规则引擎（无 API Key）'}")
    # 云端平台会通过 PORT 环境变量指定监听端口；本地仍默认使用 8899。
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8899")), log_level="warning")
