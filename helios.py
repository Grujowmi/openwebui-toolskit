"""
title: Hélios
id: helios
author: ANTOINE Pierre
author_url: https://github.com/Grujowmi
description: Admin-only dashboard of token usage and estimated cost per model and per user. Never reads message content.
version: 0.4.0
required_open_webui_version: 0.13.0
license: AGPL-3.0-or-later
"""

# Hélios, usage dashboard for Open WebUI.
# Copyright (c) 2026 ANTOINE Pierre
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along
# with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# Step 4: periodic mail report to administrators only.
# Step 3: dashboard (cost per model and per user, period filter, daily chart, CSV export).
# Step 2: own schema and table, collection of token counts, retention purge.
# Only reads chat_message.id, user_id, model_id, done, role, usage, created_at,
# updated_at and model.base_model_id. Never reads message content.

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from open_webui.internal.db import get_async_db_context
from open_webui.models.functions import Functions

PLUGIN_VERSION = "0.4.0"
DEFAULT_PREFIX = "/usage"
MIN_INTERVAL_MINUTES = 15
LOOP_TICK_SECONDS = 300
MAIL_CATCHUP_HOURS = 12
SMTP_PASSWORD_ENV = "HELIOS_SMTP_PASSWORD"
UI_LANGUAGES = ("auto", "en", "fr")
ROUTE_MARK = "__helios_route__"

log = logging.getLogger("helios")
log.setLevel(logging.INFO)

# Module state. A code update re-executes the module and resets this dict;
# whatever must survive (background task, mounted prefixes) lives on app.state.
_STATE = {
    "started": False,
    "started_at": None,
    "app": None,
    "function_id": None,
    "valves": None,
    "prefix": None,
    "loop_iterations": 0,
    "last_tick": None,
    "last_check": None,  # "active" | "inactive" | "not_found" | "no_id"
    "last_error": None,
}


# ---------------------------------------------------------------- valves

def _parse_price_map(raw) -> Tuple[dict, Optional[list]]:
    """Validate the price map. Returns (map, warning) where warning is [code, arg] or None."""
    try:
        data = json.loads(raw or "{}")
    except Exception as e:
        return {}, ["price_map_json", e.__class__.__name__]
    if not isinstance(data, dict):
        return {}, ["price_map_object", ""]
    clean = {}
    for key, val in data.items():
        if not isinstance(val, dict):
            return {}, ["price_map_entry", str(key)]
        try:
            inp = float(val.get("input", 0))
            out = float(val.get("output", 0))
        except (TypeError, ValueError):
            return {}, ["price_map_number", str(key)]
        if inp < 0 or out < 0:
            return {}, ["price_map_negative", str(key)]
        clean[str(key)] = {"input": inp, "output": out}
    return clean, None


def _smtp_password_source(v: dict) -> str:
    if os.environ.get(SMTP_PASSWORD_ENV):
        return "env"
    if str(v.get("smtp_password") or ""):
        return "valve"
    return "absent"


def _check_valves(v: dict) -> list:
    """Configuration warnings as [code, arg] pairs; the page translates them."""
    w = []
    try:
        ZoneInfo(str(v.get("timezone") or "UTC"))
    except Exception:
        w.append(["timezone", str(v.get("timezone"))])
    _, err = _parse_price_map(v.get("price_map"))
    if err:
        w.append(err)
    if v.get("ui_language") not in UI_LANGUAGES:
        w.append(["ui_language", str(v.get("ui_language"))])
    if v.get("mail_language") not in ("en", "fr"):
        w.append(["mail_language", str(v.get("mail_language"))])
    if v.get("smtp_security") not in ("starttls", "ssl", "none"):
        w.append(["smtp_security", str(v.get("smtp_security"))])
    if v.get("mail_frequency") not in ("daily", "weekly", "monthly"):
        w.append(["mail_frequency", str(v.get("mail_frequency"))])
    try:
        if not 0 <= int(v.get("mail_hour")) <= 23:
            w.append(["mail_hour", str(v.get("mail_hour"))])
    except (TypeError, ValueError):
        w.append(["mail_hour", str(v.get("mail_hour"))])
    if v.get("mail_enabled"):
        for k in ("smtp_host", "mail_from", "mail_recipients"):
            if not str(v.get(k) or "").strip():
                w.append(["mail_missing", k])
    if v.get("smtp_security") == "none" and _smtp_password_source(v) != "absent":
        w.append(["smtp_cleartext", ""])
    if not v.get("smtp_verify_cert", True):
        w.append(["smtp_no_verify", ""])
    return w


def _redacted_valves(v: dict) -> dict:
    out = dict(v)
    out["smtp_password"] = "(set)" if out.get("smtp_password") else ""
    out["pseudonym_salt"] = "(set)" if out.get("pseudonym_salt") else ""
    return out


async def _current_valves() -> dict:
    """Re-read valves from the database when possible (the loop has no event), else last known."""
    fid = _STATE["function_id"]
    getter = getattr(Functions, "get_function_valves_by_id", None)
    if fid and getter is not None:
        try:
            res = getter(fid)
            if asyncio.iscoroutine(res):
                res = await res
            if isinstance(res, dict):
                merged = {**Event.Valves().model_dump(), **res}
                _STATE["valves"] = Event.Valves(**merged).model_dump()
        except Exception as e:
            log.debug(f"helios: could not re-read valves: {e}")
    return _STATE["valves"] or Event.Valves().model_dump()


# ---------------------------------------------------------------- database

# Everything Hélios stores lives in its own schema: Open WebUI migrations never
# touch it, and "DROP SCHEMA helios CASCADE" removes it entirely.
_DDL = [
    "CREATE SCHEMA IF NOT EXISTS helios",
    """CREATE TABLE IF NOT EXISTS helios.usage (
        message_id    text PRIMARY KEY,
        ts            bigint NOT NULL,
        user_id       text,
        model_id      text,
        base_model_id text,
        input_tokens  bigint,
        output_tokens bigint,
        measured      boolean NOT NULL,
        collected_at  bigint NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS usage_ts_idx ON helios.usage (ts)",
    "CREATE INDEX IF NOT EXISTS usage_user_ts_idx ON helios.usage (user_id, ts)",
    "CREATE INDEX IF NOT EXISTS usage_model_ts_idx ON helios.usage (model_id, ts)",
    """CREATE TABLE IF NOT EXISTS helios.state (
        key        text PRIMARY KEY,
        value      text NOT NULL,
        updated_at bigint NOT NULL
    )""",
]


def _ts_sql(col: str) -> str:
    """Normalize a timestamp column to Unix seconds (accepts s, ms, µs, ns)."""
    return (
        f"(CASE WHEN {col} > 100000000000000000 THEN {col} / 1000000000 "
        f"WHEN {col} > 100000000000000 THEN {col} / 1000000 "
        f"WHEN {col} > 100000000000 THEN {col} / 1000 ELSE {col} END)"
    )


def _tok_sql(key: str, fallback: str) -> str:
    """Same rule as Open WebUI's _token_columns: normalized key, else OpenAI-style key.
    Non-integer values are ignored instead of failing the whole pass."""
    val = (
        f"COALESCE(json_extract_path_text(m.usage, '{key}'), "
        f"json_extract_path_text(m.usage, '{fallback}'))"
    )
    return f"(CASE WHEN {val} ~ '^[0-9]+$' THEN ({val})::bigint END)"


# Assistant replies that are done, finished at least :ready_before, created since :since.
# A row first stored without a measure is upgraded if a measure shows up later.
_COLLECT_SQL = f"""
INSERT INTO helios.usage AS u
    (message_id, ts, user_id, model_id, base_model_id,
     input_tokens, output_tokens, measured, collected_at)
SELECT s.id, s.ts, s.user_id, s.model_id, s.base_model_id,
       s.i, s.o, (s.i IS NOT NULL OR s.o IS NOT NULL), :now
FROM (
    SELECT m.id,
           {_ts_sql("m.created_at")} AS ts,
           {_ts_sql("COALESCE(m.updated_at, m.created_at)")} AS fin,
           m.user_id, m.model_id, mo.base_model_id,
           {_tok_sql("input_tokens", "prompt_tokens")} AS i,
           {_tok_sql("output_tokens", "completion_tokens")} AS o
    FROM chat_message m
    LEFT JOIN model mo ON mo.id = m.model_id
    WHERE m.role = 'assistant' AND m.done IS TRUE AND m.created_at IS NOT NULL
) s
WHERE s.fin <= :ready_before AND s.ts >= :since
ON CONFLICT (message_id) DO UPDATE SET
    input_tokens  = EXCLUDED.input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    measured      = TRUE,
    collected_at  = EXCLUDED.collected_at
WHERE u.measured = FALSE AND EXCLUDED.measured = TRUE
RETURNING (xmax = 0) AS inserted
"""

_STATE_SET_SQL = """
INSERT INTO helios.state (key, value, updated_at) VALUES (:k, :v, :t)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
"""


async def _ensure_schema(db):
    if _STATE.get("schema_ok"):
        return
    dialect = db.get_bind().dialect.name
    if dialect != "postgresql":
        raise RuntimeError(f"Hélios requires PostgreSQL (found {dialect})")
    for stmt in _DDL:
        await db.execute(text(stmt))
    await db.commit()
    _STATE["schema_ok"] = True
    log.info("helios: schema ready")


async def _state_read(db) -> dict:
    res = await db.execute(text("SELECT key, value FROM helios.state"))
    return {k: v for k, v in res.fetchall()}


async def _state_write(db, key: str, value: str):
    await db.execute(text(_STATE_SET_SQL), {"k": key, "v": value, "t": int(time.time())})


def _lock() -> asyncio.Lock:
    if _STATE.get("lock") is None:
        _STATE["lock"] = asyncio.Lock()
    return _STATE["lock"]


def _int(v: dict, key: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(v.get(key)))
    except (TypeError, ValueError):
        return default


async def _collect(v: dict, trigger: str) -> dict:
    """One collection pass plus retention purge. Serialized by a lock."""
    async with _lock():
        started = time.time()
        now = int(started)
        async with get_async_db_context() as db:
            await _ensure_schema(db)
            st = await _state_read(db)
            backfill = "backfill_done" not in st
            since = 0 if backfill else now - _int(v, "collect_window_days", 3, 1) * 86400
            ready_before = now - _int(v, "collect_min_age_minutes", 10) * 60
            res = await db.execute(
                text(_COLLECT_SQL), {"now": now, "ready_before": ready_before, "since": since}
            )
            rows = res.fetchall()
            inserted = sum(1 for r in rows if r[0])
            purged = 0
            months = _int(v, "retention_months", 13)
            if months > 0:
                cutoff = now - int(months * 30.4375 * 86400)
                pres = await db.execute(text("DELETE FROM helios.usage WHERE ts < :c"), {"c": cutoff})
                purged = pres.rowcount or 0
            result = {
                "at": now,
                "trigger": trigger,
                "backfill": backfill,
                "inserted": inserted,
                "upgraded": len(rows) - inserted,
                "purged": purged,
                "duration_ms": int((time.time() - started) * 1000),
            }
            await _state_write(db, "last_collect", json.dumps(result))
            if backfill:
                await _state_write(db, "backfill_done", str(now))
            await db.commit()
    _STATE["last_collect"] = result
    _STATE["last_collect_at"] = now
    log.info(
        f"helios: collect ({trigger}) inserted={inserted} upgraded={result['upgraded']} "
        f"purged={purged} backfill={backfill}"
    )
    return result


async def _table_stats() -> dict:
    async with get_async_db_context() as db:
        await _ensure_schema(db)
        tot = (
            await db.execute(
                text(
                    "SELECT count(*), count(*) FILTER (WHERE NOT measured), min(ts), max(ts), "
                    "COALESCE(sum(input_tokens), 0), COALESCE(sum(output_tokens), 0) FROM helios.usage"
                )
            )
        ).fetchone()
        per_model = (
            await db.execute(
                text(
                    "SELECT COALESCE(model_id, '?'), count(*), count(*) FILTER (WHERE NOT measured) "
                    "FROM helios.usage GROUP BY 1 ORDER BY 2 DESC"
                )
            )
        ).fetchall()
        st = await _state_read(db)
    last = last_mail = None
    try:
        last = json.loads(st["last_collect"]) if "last_collect" in st else None
        last_mail = json.loads(st["last_mail"]) if "last_mail" in st else None
    except Exception:
        pass
    return {
        "rows": tot[0],
        "unmeasured": tot[1],
        "first_ts": tot[2],
        "last_ts": tot[3],
        "input_tokens": int(tot[4]),
        "output_tokens": int(tot[5]),
        "per_model": [{"model": m, "rows": n, "unmeasured": u} for m, n, u in per_model],
        "last_collect": last,
        "last_mail": last_mail,
        "backfill_done": st.get("backfill_done"),
    }


# ---------------------------------------------------------------- report

MAX_RANGE_DAYS = 400


def _tz(v: dict) -> Tuple[str, ZoneInfo]:
    name = str(v.get("timezone") or "UTC")
    try:
        return name, ZoneInfo(name)
    except Exception:
        return "UTC", ZoneInfo("UTC")


def _parse_range(qfrom: Optional[str], qto: Optional[str], tz: ZoneInfo):
    """Inclusive local dates -> (from, to, start_ts, end_ts). Defaults to the last 30 days."""
    today = datetime.now(tz).date()
    d_to = date.fromisoformat(qto) if qto else today
    d_from = date.fromisoformat(qfrom) if qfrom else d_to - timedelta(days=29)
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    if (d_to - d_from).days >= MAX_RANGE_DAYS:
        raise ValueError(f"range longer than {MAX_RANGE_DAYS} days")
    start = int(datetime.combine(d_from, dtime.min, tz).timestamp())
    end = int(datetime.combine(d_to + timedelta(days=1), dtime.min, tz).timestamp())
    return d_from, d_to, start, end


def _match_price(model_id: Optional[str], base_id: Optional[str], pm: dict):
    """Longest price-map key that prefixes the model id, else its base model id."""
    keys = sorted(pm, key=len, reverse=True)
    for cand in (model_id, base_id):
        if not cand:
            continue
        low = cand.lower()
        for k in keys:
            if low.startswith(k.lower()):
                return k, pm[k]
    return None, None


async def _salt(db, v: dict) -> str:
    if str(v.get("pseudonym_salt") or ""):
        return str(v["pseudonym_salt"])
    st = await _state_read(db)
    if st.get("pseudonym_salt"):
        return st["pseudonym_salt"]
    salt = secrets.token_hex(16)
    await _state_write(db, "pseudonym_salt", salt)
    await db.commit()
    return salt


_REPORT_SQL = """
SELECT (to_timestamp(ts) AT TIME ZONE :tz)::date AS day, user_id, model_id, base_model_id,
       count(*), count(*) FILTER (WHERE NOT measured),
       COALESCE(sum(input_tokens), 0), COALESCE(sum(output_tokens), 0)
FROM helios.usage
WHERE ts >= :s AND ts < :e
GROUP BY 1, 2, 3, 4
"""


def _bucket():
    return {"replies": 0, "unmeasured": 0, "input": 0, "output": 0, "cost": 0.0, "unpriced_tokens": 0}


def _add(b: dict, replies, unmeasured, inp, out, cost):
    b["replies"] += replies
    b["unmeasured"] += unmeasured
    b["input"] += inp
    b["output"] += out
    if cost is None:
        b["unpriced_tokens"] += inp + out
    else:
        b["cost"] += cost


async def _report(v: dict, qfrom: Optional[str], qto: Optional[str]) -> dict:
    tzname, tz = _tz(v)
    d_from, d_to, start, end = _parse_range(qfrom, qto, tz)
    pm, _ = _parse_price_map(v.get("price_map"))
    pseudo = bool(v.get("pseudonymize_users"))
    async with get_async_db_context() as db:
        await _ensure_schema(db)
        rows = (await db.execute(text(_REPORT_SQL), {"tz": tzname, "s": start, "e": end})).fetchall()
        user_ids = sorted({r[1] for r in rows if r[1]})
        people = {}
        salt = None
        if pseudo:
            salt = await _salt(db, v)
        elif user_ids:
            res = await db.execute(
                text('SELECT id, name, email FROM "user" WHERE id = ANY(:ids)'), {"ids": user_ids}
            )
            people = {i: (n, e) for i, n, e in res.fetchall()}

    def who(uid):
        if not uid:
            return {"label": "?", "email": None}
        if pseudo:
            return {"label": "user-" + hashlib.sha256((salt + uid).encode()).hexdigest()[:8], "email": None}
        if uid in people:
            n, e = people[uid]
            return {"label": n or e or uid, "email": e}
        return {"label": f"(deleted {uid[:8]})", "email": None}

    total, models, users, days = _bucket(), {}, {}, {}
    for day, uid, model, base, replies, unmeasured, inp, out in rows:
        inp, out = int(inp), int(out)
        key, price = _match_price(model, base, pm)
        cost = None if price is None else inp / 1e6 * price["input"] + out / 1e6 * price["output"]
        _add(total, replies, unmeasured, inp, out, cost)
        mk = (model or "?", base)
        if mk not in models:
            models[mk] = {**_bucket(), "model": mk[0], "base": base, "price_key": key}
        _add(models[mk], replies, unmeasured, inp, out, cost)
        if uid not in users:
            users[uid] = {**_bucket(), **who(uid)}
        _add(users[uid], replies, unmeasured, inp, out, cost)
        dk = day.isoformat()
        days.setdefault(dk, _bucket())
        _add(days[dk], replies, unmeasured, inp, out, cost)

    series, d = [], d_from
    while d <= d_to:
        series.append({"day": d.isoformat(), **days.get(d.isoformat(), _bucket())})
        d += timedelta(days=1)

    def order(items):
        return sorted(items, key=lambda x: (-x["cost"], -(x["input"] + x["output"])))

    return {
        "from": d_from.isoformat(),
        "to": d_to.isoformat(),
        "timezone": tzname,
        "currency": str(v.get("currency") or "USD"),
        "pseudonymized": pseudo,
        "price_map_empty": not pm,
        "total": total,
        "models": order(models.values()),
        "users": order(users.values()),
        "days": series,
    }


_CSV_HEADERS = {
    "en": {
        "models": ["model", "base_model", "price_key", "replies", "unmeasured", "input_tokens", "output_tokens", "estimated_cost", "unpriced_tokens"],
        "users": ["user", "email", "replies", "unmeasured", "input_tokens", "output_tokens", "estimated_cost", "unpriced_tokens"],
        "days": ["day", "replies", "unmeasured", "input_tokens", "output_tokens", "estimated_cost", "unpriced_tokens"],
    },
    "fr": {
        "models": ["modele", "modele_de_base", "cle_de_prix", "reponses", "sans_mesure", "tokens_entree", "tokens_sortie", "cout_estime", "tokens_sans_prix"],
        "users": ["utilisateur", "email", "reponses", "sans_mesure", "tokens_entree", "tokens_sortie", "cout_estime", "tokens_sans_prix"],
        "days": ["jour", "reponses", "sans_mesure", "tokens_entree", "tokens_sortie", "cout_estime", "tokens_sans_prix"],
    },
}


def _csv(kind: str, rep: dict, lang: str) -> str:
    """French CSV uses ';' and a decimal comma so Excel opens it as numbers."""
    lang = "fr" if lang == "fr" else "en"
    sep, dec = (";", ",") if lang == "fr" else (",", ".")

    def cell(x):
        s = "" if x is None else str(x)
        # Neutralize spreadsheet formulas (CSV injection).
        return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s

    def money(x):
        return f"{x:.6f}".replace(".", dec)

    def tail(b):
        return [b["replies"], b["unmeasured"], b["input"], b["output"], money(b["cost"]), b["unpriced_tokens"]]

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=sep, lineterminator="\r\n")
    w.writerow(_CSV_HEADERS[lang][kind])
    if kind == "models":
        for m in rep["models"]:
            w.writerow([cell(m["model"]), cell(m["base"]), cell(m["price_key"])] + tail(m))
    elif kind == "users":
        for u in rep["users"]:
            w.writerow([cell(u["label"]), cell(u["email"])] + tail(u))
    else:
        for dd in rep["days"]:
            w.writerow([dd["day"]] + tail(dd))
    return "﻿" + buf.getvalue()


# ---------------------------------------------------------------- mail report

def _redis():
    """Same lazy Redis client as Open WebUI uses (pattern taken from Prune). None without Redis."""
    if _STATE.get("redis_tried"):
        return _STATE.get("redis")
    _STATE["redis_tried"] = True
    try:
        from open_webui.env import REDIS_URL, REDIS_KEY_PREFIX
        from open_webui.utils.redis import get_redis_connection

        try:
            from open_webui.env import REDIS_CLUSTER, REDIS_SENTINEL_HOSTS, REDIS_SENTINEL_PORT
        except ImportError:
            REDIS_CLUSTER, REDIS_SENTINEL_HOSTS, REDIS_SENTINEL_PORT = False, "", ""
        sentinels = []
        try:
            from open_webui.utils.redis import get_sentinels_from_env

            sentinels = get_sentinels_from_env(REDIS_SENTINEL_HOSTS, REDIS_SENTINEL_PORT)
        except Exception:
            pass
        if REDIS_URL or sentinels:
            _STATE["redis"] = get_redis_connection(
                REDIS_URL, redis_sentinels=sentinels, redis_cluster=bool(REDIS_CLUSTER), decode_responses=True
            )
            _STATE["redis_prefix"] = REDIS_KEY_PREFIX or "open-webui"
    except Exception as e:
        log.warning(f"helios: Redis unavailable, per-worker claims only: {e}")
    return _STATE.get("redis")


def _claim(key: str, ttl: int) -> bool:
    """One worker across replicas wins the key for ttl seconds (Redis SET NX EX), else a local map."""
    r = _redis()
    if r is not None:
        try:
            return bool(r.set(f"{_STATE['redis_prefix']}:helios:{key}", "1", nx=True, ex=max(1, ttl)))
        except Exception as e:
            log.warning(f"helios: Redis claim failed, local fallback: {e}")
    claims = _STATE.setdefault("claims", {})
    now = time.time()
    if claims.get(key, 0) > now:
        return False
    claims[key] = now + ttl
    return True


def _mail_slot(v: dict, now: datetime):
    """Current period's send time and the reporting period it covers (previous day, week or month)."""
    tz = now.tzinfo
    hour = min(23, max(0, _int(v, "mail_hour", 7)))
    today = now.date()
    freq = v.get("mail_frequency")
    if freq == "daily":
        anchor = today
        d_from = d_to = today - timedelta(days=1)
    elif freq == "monthly":
        anchor = today.replace(day=1)
        d_to = anchor - timedelta(days=1)
        d_from = d_to.replace(day=1)
    else:
        anchor = today - timedelta(days=today.weekday())  # Monday
        d_to = anchor - timedelta(days=1)
        d_from = anchor - timedelta(days=7)
    return datetime.combine(anchor, dtime(hour), tz), d_from, d_to


def _next_mail(v: dict) -> Optional[int]:
    if not v.get("mail_enabled"):
        return None
    _, tz = _tz(v)
    now = datetime.now(tz)
    for i in range(40):
        probe = datetime.combine(now.date() + timedelta(days=i), dtime(23, 59), tz)
        slot, _, _ = _mail_slot(v, probe)
        if slot > now:
            return int(slot.timestamp())
    return None


def _parse_recipients(raw) -> list:
    out = []
    for part in str(raw or "").replace(";", ",").replace(" ", ",").split(","):
        p = part.strip().lower()
        if p and "@" in p and "\n" not in p and "\r" not in p and p not in out:
            out.append(p)
    return out


async def _admin_recipients(v: dict):
    """Only addresses that belong to Open WebUI admin accounts are kept."""
    wanted = _parse_recipients(v.get("mail_recipients"))
    if not wanted:
        return [], []
    async with get_async_db_context() as db:
        res = await db.execute(
            text('SELECT lower(email) FROM "user" WHERE role = \'admin\' AND lower(email) = ANY(:e)'),
            {"e": wanted},
        )
        admins = {r[0] for r in res.fetchall()}
    return [w for w in wanted if w in admins], [w for w in wanted if w not in admins]


_MAIL_TEXT = {
    "en": {
        "title": "Hélios usage report", "period": "Period", "cost": "Estimated cost", "replies": "Replies",
        "unmeasured": "without token count", "input": "Input tokens", "output": "Output tokens",
        "users": "Active users", "models": "Per model", "per_user": "Per user", "model": "Model",
        "user": "User", "no_price": "NO PRICE", "w_no_prices": "No prices configured: costs are not computed.",
        "w_unpriced": "{0} tokens belong to models without a price and are not in the cost.",
        "w_unmeasured": "{0} replies have no token count.",
        "w_stale": "Warning: last collection was {0} hours ago.",
        "footer": "Sent to Open WebUI administrators only. Token counts only, never message content.",
        "open": "Open the dashboard", "test": "TEST",
    },
    "fr": {
        "title": "Rapport de consommation Hélios", "period": "Période", "cost": "Coût estimé", "replies": "Réponses",
        "unmeasured": "sans mesure de tokens", "input": "Tokens d'entrée", "output": "Tokens de sortie",
        "users": "Utilisateurs actifs", "models": "Par modèle", "per_user": "Par utilisateur", "model": "Modèle",
        "user": "Utilisateur", "no_price": "SANS PRIX", "w_no_prices": "Aucun prix configuré : les coûts ne sont pas calculés.",
        "w_unpriced": "{0} tokens appartiennent à des modèles sans prix et ne sont pas comptés dans le coût.",
        "w_unmeasured": "{0} réponses n'ont pas de mesure de tokens.",
        "w_stale": "Attention : la dernière collecte date de {0} heures.",
        "footer": "Envoyé uniquement aux administrateurs d'Open WebUI. Comptage de tokens seulement, jamais le contenu des messages.",
        "open": "Ouvrir le tableau de bord", "test": "TEST",
    },
}


def _fmt_int(n, lang):
    s = f"{int(n):,}"
    return s.replace(",", " ") if lang == "fr" else s


def _fmt_money(x, cur, lang):
    s = f"{x:,.4f}" if abs(x) < 1 else f"{x:,.2f}"
    if lang == "fr":
        s = s.replace(",", "X").replace(".", ",").replace("X", " ")
    return f"{s} {cur}"


def _build_mail(v: dict, rep: dict, recipients: list, test: bool) -> EmailMessage:
    lang = "fr" if v.get("mail_language") == "fr" else "en"
    T = _MAIL_TEXT[lang]
    cur, tot = rep["currency"], rep["total"]
    I = lambda n: _fmt_int(n, lang)
    M = lambda x: _fmt_money(x, cur, lang)
    period = f"{rep['from']} → {rep['to']} ({rep['timezone']})"
    subject = f"{'[' + T['test'] + '] ' if test else ''}{T['title']} {rep['from']} → {rep['to']}"

    notes = []
    if rep["price_map_empty"]:
        notes.append(T["w_no_prices"])
    elif tot["unpriced_tokens"]:
        notes.append(T["w_unpriced"].replace("{0}", I(tot["unpriced_tokens"])))
    if tot["unmeasured"]:
        notes.append(T["w_unmeasured"].replace("{0}", I(tot["unmeasured"])))
    last_at = _STATE.get("last_collect_at")
    if last_at and time.time() - last_at > 26 * 3600:
        notes.append(T["w_stale"].replace("{0}", str(int((time.time() - last_at) // 3600))))

    kpis = [
        (T["cost"], "-" if rep["price_map_empty"] else M(tot["cost"])),
        (T["replies"], f"{I(tot['replies'])} ({I(tot['unmeasured'])} {T['unmeasured']})"),
        (T["input"], I(tot["input"])),
        (T["output"], I(tot["output"])),
        (T["users"], I(len(rep["users"]))),
    ]
    model_rows = [
        (m["model"] + ("" if m["price_key"] else f" ({T['no_price']})"), I(m["replies"]), I(m["input"]), I(m["output"]), M(m["cost"]))
        for m in rep["models"]
    ]
    user_rows = [
        (u["label"], I(u["replies"]), I(u["input"]), I(u["output"]), M(u["cost"])) for u in rep["users"]
    ] if v.get("mail_include_user_detail") else []
    cols = lambda first: [first, T["replies"], T["input"], T["output"], T["cost"]]
    url = str(v.get("dashboard_url") or "").strip()

    # Plain text part
    colon = " : " if lang == "fr" else ": "
    lines = [T["title"], f"{T['period']}{colon}{period}", ""]
    lines += [f"{k}{colon}{val}" for k, val in kpis] + [""]
    lines += [f"! {n}" for n in notes] + ([""] if notes else [])

    def text_table(title, header, rows):
        out = [title, " | ".join(header)]
        for r in rows:
            out.append(" | ".join(str(c) for c in r))
        return out + [""]

    lines += text_table(T["models"], cols(T["model"]), model_rows)
    if user_rows:
        lines += text_table(T["per_user"], cols(T["user"]), user_rows)
    if url:
        lines += [f"{T['open']}{colon}{url}", ""]
    lines.append(T["footer"])

    # HTML part (inline styles only; every value escaped)
    td = 'style="padding:6px 10px;border-bottom:1px solid #e3e3e0;text-align:{}"'

    def html_table(title, header, rows):
        h = f'<h3 style="font:600 13px system-ui,sans-serif;color:#52514e;text-transform:uppercase;letter-spacing:.05em;margin:24px 0 8px">{escape(title)}</h3>'
        h += '<table style="border-collapse:collapse;width:100%;font:14px system-ui,sans-serif">'
        h += "<tr>" + "".join(
            f'<th {td.format("left" if i == 0 else "right")}><span style="color:#52514e;font-weight:500">{escape(c)}</span></th>'
            for i, c in enumerate(header)
        ) + "</tr>"
        for r in rows:
            h += "<tr>" + "".join(f'<td {td.format("left" if i == 0 else "right")}>{escape(str(c))}</td>' for i, c in enumerate(r)) + "</tr>"
        return h + "</table>"

    html = '<div style="max-width:720px;margin:0 auto;font:14px/1.5 system-ui,sans-serif;color:#14171c">'
    html += f'<h2 style="margin:0 0 4px;font-size:20px">{escape(T["title"])}</h2>'
    html += f'<p style="margin:0 0 16px;color:#52514e">{escape(T["period"])}{escape(colon)}{escape(period)}</p>'
    html += f'<p style="margin:0;color:#52514e">{escape(kpis[0][0])}</p><p style="margin:0 0 16px;font-size:32px;font-weight:600">{escape(kpis[0][1])}</p>'
    html += '<table style="border-collapse:collapse;font:14px system-ui,sans-serif">' + "".join(
        f'<tr><td style="padding:2px 16px 2px 0;color:#52514e">{escape(k)}</td><td style="padding:2px 0">{escape(val)}</td></tr>'
        for k, val in kpis[1:]
    ) + "</table>"
    for n in notes:
        html += f'<p style="background:#fbf3e4;color:#8a6410;border-radius:8px;padding:8px 10px;margin:12px 0 0">{escape(n)}</p>'
    html += html_table(T["models"], cols(T["model"]), model_rows)
    if user_rows:
        html += html_table(T["per_user"], cols(T["user"]), user_rows)
    if url.startswith(("https://", "http://")):
        html += f'<p style="margin:20px 0 0"><a href="{escape(url, quote=True)}">{escape(T["open"])}</a></p>'
    html += f'<p style="margin:24px 0 0;color:#8a8984;font-size:12px">{escape(T["footer"])}</p></div>'

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Hélios", str(v.get("mail_from") or "").strip()))
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=str(v.get("mail_from") or "helios").split("@")[-1] or None)
    msg.set_content("\n".join(lines))
    msg.add_alternative(html, subtype="html")
    return msg


def _smtp_send(v: dict, msg: EmailMessage) -> dict:
    """Blocking send, run in a thread. Authenticates only when smtp_username is set."""
    host = str(v.get("smtp_host") or "").strip()
    port = _int(v, "smtp_port", 587, 1)
    security = v.get("smtp_security")
    ctx = ssl.create_default_context()
    if not v.get("smtp_verify_cert", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    with server:
        server.ehlo()
        if security == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
        user = str(v.get("smtp_username") or "")
        if user:
            server.login(user, os.environ.get(SMTP_PASSWORD_ENV) or str(v.get("smtp_password") or ""))
        refused = server.send_message(msg)
    return {k: f"{c} {m.decode(errors='replace') if isinstance(m, bytes) else m}" for k, (c, m) in refused.items()}


async def _send_report(v: dict, d_from: date, d_to: date, trigger: str, slot: Optional[str] = None) -> dict:
    result = {"at": int(time.time()), "trigger": trigger, "slot": slot, "from": d_from.isoformat(), "to": d_to.isoformat(),
              "ok": False, "sent_to": [], "rejected": [], "refused": {}, "error": None}
    try:
        for k in ("smtp_host", "mail_from"):
            if not str(v.get(k) or "").strip():
                raise ValueError(f"{k} is empty")
        allowed, rejected = await _admin_recipients(v)
        result["rejected"] = rejected
        if not allowed:
            raise ValueError("no recipient matches an Open WebUI admin account")
        rep = await _report(v, d_from.isoformat(), d_to.isoformat())
        msg = _build_mail(v, rep, allowed, test=(trigger == "test"))
        refused = await asyncio.to_thread(_smtp_send, v, msg)
        result["refused"] = refused
        result["sent_to"] = [a for a in allowed if a not in refused]
        result["ok"] = bool(result["sent_to"])
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    async with get_async_db_context() as db:
        await _ensure_schema(db)
        await _state_write(db, "last_mail", json.dumps(result))
        if slot:
            await _state_write(db, "last_mail_slot", slot)
        await db.commit()
    level = log.info if result["ok"] else log.warning
    level(f"helios: mail ({trigger}) ok={result['ok']} sent={len(result['sent_to'])} "
          f"rejected={len(result['rejected'])} error={result['error']}")
    return result


async def _maybe_mail(v: dict):
    """Send once per slot, within MAIL_CATCHUP_HOURS after the scheduled time (no surprise backlog)."""
    if not v.get("mail_enabled"):
        return
    _, tz = _tz(v)
    now = datetime.now(tz)
    slot, d_from, d_to = _mail_slot(v, now)
    if now < slot or now - slot > timedelta(hours=MAIL_CATCHUP_HOURS):
        return
    key = slot.isoformat()
    async with get_async_db_context() as db:
        await _ensure_schema(db)
        st = await _state_read(db)
    if st.get("last_mail_slot") == key:
        return
    if not _claim(f"mail:{key}", 6 * 3600):
        return
    await _send_report(v, d_from, d_to, "auto", key)


# ---------------------------------------------------------------- background loop

async def _loop():
    """Ticks every LOOP_TICK_SECONDS: collects when the interval is due, then checks the mail slot."""
    first = True
    while True:
        try:
            fid = _STATE["function_id"]
            fn = await Functions.get_function_by_id(fid) if fid else None
            _STATE["loop_iterations"] += 1
            _STATE["last_tick"] = int(time.time())
            if not fid:
                _STATE["last_check"] = "no_id"
            elif fn is None:
                _STATE["last_check"] = "not_found"
                log.info("helios: function deleted, loop stopped")
                return
            elif not fn.is_active:
                _STATE["last_check"] = "inactive"
            else:
                _STATE["last_check"] = "active"
            v = await _current_valves()
            if _STATE["last_check"] == "active":
                interval = max(MIN_INTERVAL_MINUTES, _int(v, "collect_interval_minutes", 180)) * 60
                if time.time() - (_STATE.get("last_collect_at") or 0) >= interval and not _lock().locked():
                    await _collect(v, "auto")
                await _maybe_mail(v)
            _STATE["last_error"] = None
            if first:
                log.info(f"helios: first loop pass, check={_STATE['last_check']}")
                first = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _STATE["last_error"] = f"{type(e).__name__}: {e}"
            log.warning(f"helios: loop error: {_STATE['last_error']}")
        await asyncio.sleep(LOOP_TICK_SECONDS)


def _start_loop(app):
    # Stored on app.state so that a code update cancels the previous loop
    # instead of stacking a second one.
    old = getattr(app.state, "helios_task", None)
    if old is not None and not old.done():
        old.cancel()
    app.state.helios_task = asyncio.get_running_loop().create_task(_loop())


def _loop_running() -> bool:
    app = _STATE["app"]
    task = getattr(app.state, "helios_task", None) if app is not None else None
    return task is not None and not task.done()


def _is_ours(endpoint) -> bool:
    """Tagged by any Hélios version, or loaded from an older untagged module of this function."""
    if getattr(endpoint, ROUTE_MARK, False):
        return True
    return str(getattr(endpoint, "__module__", "")).startswith(f"function_{_STATE['function_id']}_")


def _mounted_routes() -> int:
    app = _STATE["app"]
    if app is None:
        return 0
    return sum(1 for r in app.router.routes if _is_ours(getattr(r, "endpoint", None)))


def _status_payload(v: dict) -> dict:
    return {
        "version": PLUGIN_VERSION,
        "prefix": _STATE["prefix"],
        "routes": _mounted_routes(),
        "routes_expected": _STATE.get("routes_expected"),
        "started_at": _STATE["started_at"],
        "loop_running": _loop_running(),
        "loop_iterations": _STATE["loop_iterations"],
        "last_tick": _STATE["last_tick"],
        "last_check": _STATE["last_check"],
        "last_error": _STATE["last_error"],
        "smtp_password_source": _smtp_password_source(v),
        "next_mail": _next_mail(v),
        "warnings": _check_valves(v),
        "valves": _redacted_valves(v),
    }


# ---------------------------------------------------------------- routes

def _mount_routes(app, v: dict):
    from open_webui.utils.auth import bearer_security, get_admin_user, get_current_user

    prefix = "/" + str(v.get("route_prefix") or "").strip("/ ")
    if prefix == "/":
        prefix = DEFAULT_PREFIX  # an empty prefix would shadow the SPA at '/'
    _STATE["prefix"] = prefix

    # Drop our previous routes (code update or changed prefix), never Open WebUI's.
    known = set(getattr(app.state, "helios_prefixes", set())) | {prefix}
    app.state.helios_prefixes = known

    def ours(r) -> bool:
        # Open WebUI loads each code version under a new module name
        # (function_helios_<random>), so routes are tagged instead of matched by module.
        if not _is_ours(getattr(r, "endpoint", None)):
            return False
        path = str(getattr(r, "path", ""))
        return any(path == p or path.startswith(p + "/") for p in known)

    stale = [r for r in app.router.routes if ours(r)]
    if stale:
        ids = {id(r) for r in stale}
        app.router.routes[:] = [r for r in app.router.routes if id(r) not in ids]
        log.info(f"helios: removed {len(stale)} stale route(s)")

    lang = v.get("ui_language") if v.get("ui_language") in UI_LANGUAGES else "auto"
    page_html = (
        _PAGE_HTML.replace("__PREFIX__", prefix)
        .replace("__VERSION__", PLUGIN_VERSION)
        .replace("__LANG__", lang)
    )

    async def get_active_admin(user=Depends(get_admin_user)):
        # Routes outlive a disable or delete until restart: re-check every time.
        fn = await Functions.get_function_by_id(_STATE["function_id"])
        if fn is None or not fn.is_active:
            raise HTTPException(status_code=404)
        return user

    router = APIRouter()

    @router.get(prefix, include_in_schema=False)
    async def page(
        request: Request,
        response: Response,
        background_tasks: BackgroundTasks,
        auth_token=Depends(bearer_security),
    ):
        # Session checked BEFORE any HTML is sent; admins only.
        user = None
        try:
            user = await get_current_user(request, response, background_tasks, auth_token)
        except Exception as e:
            log.debug(f"helios: page denied: {e}")
        if user is None or getattr(user, "role", None) != "admin":
            redirect = RedirectResponse(url="/", status_code=302)
            for key, value in response.headers.raw:
                if key == b"set-cookie":
                    redirect.raw_headers.append((key, value))
            return redirect
        await get_active_admin(user)
        return HTMLResponse(
            page_html,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    @router.get(f"{prefix}/api/status", include_in_schema=False)
    async def status(user=Depends(get_active_admin)):
        v = await _current_valves()
        payload = _status_payload(v)
        try:
            payload["table"] = await _table_stats()
        except Exception as e:
            payload["table"] = None
            payload["table_error"] = f"{type(e).__name__}: {e}"
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.get(f"{prefix}/api/report", include_in_schema=False)
    async def report(request: Request, user=Depends(get_active_admin)):
        q = request.query_params
        try:
            rep = await _report(await _current_valves(), q.get("from"), q.get("to"))
        except ValueError as e:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
        return JSONResponse(rep, headers={"Cache-Control": "no-store"})

    @router.get(f"{prefix}/api/export.csv", include_in_schema=False)
    async def export_csv(request: Request, user=Depends(get_active_admin)):
        q = request.query_params
        kind = q.get("kind") if q.get("kind") in ("models", "users", "days") else "models"
        try:
            rep = await _report(await _current_valves(), q.get("from"), q.get("to"))
        except ValueError as e:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
        body = _csv(kind, rep, q.get("lang") or "en")
        name = f"helios-{kind}-{rep['from']}-{rep['to']}.csv"
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
        )

    @router.post(f"{prefix}/api/mail-test", include_in_schema=False)
    async def mail_test(request: Request, user=Depends(get_active_admin)):
        # Sends the last 7 days to the configured admin recipients, even if mail_enabled is off.
        if not request.headers.get("authorization", "").lower().startswith("bearer "):
            return JSONResponse(status_code=403, content={"ok": False, "error": "bearer"})
        if not _claim("mail-test", 30):
            return JSONResponse(status_code=429, content={"ok": False, "error": "wait 30 s"})
        v = await _current_valves()
        _, tz = _tz(v)
        today = datetime.now(tz).date()
        return await _send_report(v, today - timedelta(days=6), today, "test")

    @router.post(f"{prefix}/api/collect", include_in_schema=False)
    async def collect_now(request: Request, user=Depends(get_active_admin)):
        # State-changing: require the Bearer header, never cookie-only auth (CSRF).
        if not request.headers.get("authorization", "").lower().startswith("bearer "):
            return JSONResponse(status_code=403, content={"ok": False, "error": "bearer"})
        if _lock().locked():
            return JSONResponse(status_code=409, content={"ok": False, "error": "busy"})
        try:
            result = await _collect(await _current_valves(), "manual")
            return {"ok": True, "result": result}
        except Exception as e:
            log.warning(f"helios: manual collect failed: {type(e).__name__}: {e}")
            return JSONResponse(
                status_code=500, content={"ok": False, "error": f"{type(e).__name__}: {e}"}
            )

    for r in router.routes:
        setattr(r.endpoint, ROUTE_MARK, True)
    _STATE["routes_expected"] = len(router.routes)

    before = len(app.router.routes)
    app.include_router(router)
    # Open WebUI's SPA catch-all sits at '/': move our routes ahead of it.
    routes = app.router.routes
    added = routes[before:]
    spa_i = next(
        (i for i, r in enumerate(routes) if getattr(r, "name", None) == "spa-static-files"),
        None,
    )
    if spa_i is not None and added:
        del routes[before:]
        routes[spa_i:spa_i] = added


# ---------------------------------------------------------------- plugin

class Event:
    class Valves(BaseModel):
        route_prefix: str = Field(
            default=DEFAULT_PREFIX,
            description="Admin page path. Applied on restart or code update.",
        )
        ui_language: str = Field(
            default="auto",
            description="Dashboard language: auto (browser language), en or fr. Applied on restart or code update.",
        )
        timezone: str = Field(
            default="UTC",
            description="Time zone for days and report time (e.g. Europe/Paris).",
        )
        collect_interval_minutes: int = Field(
            default=180, description="Minutes between two collection passes (minimum 15)."
        )
        collect_min_age_minutes: int = Field(
            default=10, description="Only collect replies finished at least this many minutes ago."
        )
        collect_window_days: int = Field(
            default=3, description="Window re-read on each pass (catches late-finishing replies)."
        )
        retention_months: int = Field(
            default=13, description="How long usage rows are kept, in months (0 = forever)."
        )
        currency: str = Field(default="USD", description="Currency shown for the estimated cost.")
        price_map: str = Field(
            default="{}",
            description='Price per million tokens, JSON: {"model prefix": {"input": 0.15, "output": 0.60}}. '
            "Longest-prefix match on the model, then on its base model.",
        )
        pseudonymize_users: bool = Field(
            default=True, description="Replace user names with a stable pseudonym (page and report)."
        )
        pseudonym_salt: str = Field(
            default="", description="Pseudonymization salt. Empty: Hélios generates and keeps one."
        )
        mail_enabled: bool = Field(default=False, description="Send the periodic report to administrators.")
        mail_frequency: str = Field(
            default="weekly", description="daily, weekly (Monday) or monthly (1st of the month)."
        )
        mail_hour: int = Field(default=7, description="Send hour in the configured time zone (0 to 23).")
        mail_include_user_detail: bool = Field(
            default=True,
            description="Per-user detail in the report (follows pseudonymization).",
        )
        mail_language: str = Field(default="en", description="Report language: en or fr.")
        dashboard_url: str = Field(
            default="", description="Optional link to the dashboard in the report (e.g. https://ai.example.org/usage)."
        )
        mail_recipients: str = Field(
            default="",
            description="Comma-separated addresses. Only addresses of Open WebUI admin accounts receive the report.",
        )
        mail_from: str = Field(default="", description="Sender address.")
        smtp_host: str = Field(default="", description="SMTP relay host.")
        smtp_port: int = Field(default=587, description="SMTP relay port.")
        smtp_security: str = Field(default="starttls", description="starttls, ssl or none.")
        smtp_verify_cert: bool = Field(default=True, description="Verify the relay certificate.")
        smtp_username: str = Field(default="", description="SMTP user (empty: no authentication).")
        smtp_password: str = Field(
            default="",
            description=f"Ignored when the {SMTP_PASSWORD_ENV} environment variable is set (recommended).",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _init(self, app, v: dict):
        if _STATE["started"] or app is None:
            return
        _STATE["app"] = app
        _STATE["valves"] = v
        _mount_routes(app, v)
        _start_loop(app)
        _STATE["started"] = True
        _STATE["started_at"] = int(time.time())
        log.info(f"helios v{PLUGIN_VERSION} ready at {_STATE['prefix']} (function id: {_STATE['function_id']})")

    async def event(
        self,
        event: dict,
        __event_name__: str = None,
        __app__=None,
        __id__: str = None,
        **kwargs,
    ):
        # Event payloads are never read: only initialization matters here.
        v = self.valves.model_dump()
        _STATE["valves"] = v
        if __id__:
            _STATE["function_id"] = __id__
        if not _STATE["started"] and __app__ is not None:
            # Startup (system.startup.completed) or first event after a code update.
            self._init(__app__, v)


# ---------------------------------------------------------------- page

_PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Hélios</title>
<link rel="icon" type="image/png" href="/favicon.png">
<style>
:root{--bg:#f3f4f6;--surface:#fcfcfb;--text:#14171c;--muted:#666d78;--border:rgba(18,22,28,.12);
--grid:rgba(18,22,28,.10);--ok:#15935f;--warn:#8a6410;--warn-bg:rgba(180,120,10,.11);--danger:#c5413f;
--series:#2a78d6;--series-hover:#1f63b5;--r:12px}
@media(prefers-color-scheme:dark){:root{--bg:#0c0e12;--surface:#1a1a19;--text:#f0efec;
--muted:#c3c2b7;--border:rgba(255,255,255,.1);--grid:rgba(255,255,255,.09);--ok:#46cf94;--warn:#e8b765;
--warn-bg:rgba(232,183,101,.13);--danger:#f0726f;--series:#3987e5;--series-hover:#6aa6ee}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:20px;margin:0 0 2px}
.sub{color:var(--muted);font-size:13px;margin:0 0 16px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 10px;font-weight:600}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px;margin-bottom:12px}
.cardhead{display:flex;justify-content:space-between;align-items:center;gap:8px}
.filters{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:12px}
.filters label{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px}
button,input[type=date]{font:inherit;padding:5px 10px;border-radius:8px;border:1px solid var(--border);
background:var(--surface);color:var(--text)}
button{cursor:pointer}button:disabled{opacity:.5;cursor:default}
button[aria-pressed=true]{border-color:var(--series);box-shadow:inset 0 0 0 1px var(--series)}
button.primary{background:var(--text);color:var(--surface);border-color:var(--text)}
:focus-visible{outline:2px solid var(--series);outline-offset:2px}
.notes div{border-radius:8px;padding:8px 10px;margin:0 0 8px;background:var(--warn-bg);color:var(--warn)}
.notes div.info{background:transparent;border:1px solid var(--border);color:var(--muted)}
.tiles{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:12px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px;min-width:0}
.tile.hero{grid-column:span 2}
.tlabel{color:var(--muted);font-size:13px}
.tnum{font-size:24px;font-weight:600;margin-top:4px}
.hero-num{font-size:48px;font-weight:600;line-height:1.1;margin-top:4px}
.tsub{color:var(--muted);font-size:12px;margin-top:4px}
.cap{color:var(--muted);font-size:13px;margin:-4px 0 8px}
.chartbox{position:relative}
#chart{display:block;width:100%;height:240px}
#chart text{fill:var(--muted);font-size:11px}
#chart .gridline{stroke:var(--grid);stroke-width:1}
#chart .bar path{fill:var(--series)}
#chart .bar:hover path,#chart .bar:focus path{fill:var(--series-hover)}
#chart .bar:focus{outline:none}
#chart .bar:focus .hit{stroke:var(--series);stroke-width:1}
#chart .hit{fill:transparent}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--border);
border-radius:8px;padding:8px 10px;box-shadow:0 4px 16px rgba(0,0,0,.12);font-size:12px;min-width:160px;z-index:2}
.tip .v{font-size:16px;font-weight:600;color:var(--text)}
.tip .l{color:var(--muted)}
.tip .key{display:inline-block;width:12px;height:2px;background:var(--series);vertical-align:middle;margin-right:6px}
.empty{color:var(--muted);padding:24px 0;text-align:center}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th,td{padding:6px 8px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td .small{display:block;color:var(--muted);font-size:12px}
.kv td:first-child{color:var(--muted);width:45%}
.warn{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:8px 10px;margin:4px 0}
.ok{color:var(--ok)}.err{color:var(--danger)}
details>summary{cursor:pointer;color:var(--muted);font-size:13px;margin-top:8px}
details.tech>summary{font-size:13px;text-transform:uppercase;letter-spacing:.05em;font-weight:600;margin:0}
details.tech h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:16px 0 6px}
.scroll table{min-width:640px}
@media (max-width:900px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.tile.hero{grid-column:1/-1}}
@media (max-width:560px){.hero-num{font-size:40px}}
</style>
</head>
<body>
<div class="wrap">
<h1>Hélios</h1>
<p class="sub" id="sub"></p>

<div class="filters" role="group" id="filters">
  <button type="button" data-range="7" data-i18n="r_7"></button>
  <button type="button" data-range="30" data-i18n="r_30"></button>
  <button type="button" data-range="month" data-i18n="r_month"></button>
  <button type="button" data-range="lastmonth" data-i18n="r_lastmonth"></button>
  <button type="button" data-range="365" data-i18n="r_365"></button>
  <label><span data-i18n="from"></span><input type="date" id="from"></label>
  <label><span data-i18n="to"></span><input type="date" id="to"></label>
  <button type="button" class="primary" id="apply" data-i18n="apply"></button>
</div>

<div id="msg"></div>
<div class="notes" id="notes"></div>

<section class="tiles">
  <div class="tile hero"><div class="tlabel" data-i18n="k_cost"></div><div class="hero-num" id="k_cost">-</div><div class="tsub" id="k_cost_sub"></div></div>
  <div class="tile"><div class="tlabel" data-i18n="k_replies"></div><div class="tnum" id="k_replies">-</div><div class="tsub" id="k_replies_sub"></div></div>
  <div class="tile"><div class="tlabel" data-i18n="k_in"></div><div class="tnum" id="k_in">-</div></div>
  <div class="tile"><div class="tlabel" data-i18n="k_out"></div><div class="tnum" id="k_out">-</div></div>
  <div class="tile"><div class="tlabel" data-i18n="k_users"></div><div class="tnum" id="k_users">-</div></div>
</section>

<section class="card">
  <div class="cardhead"><h2 data-i18n="h_chart"></h2><button type="button" data-csv="days">CSV</button></div>
  <p class="cap" id="chart_cap"></p>
  <div class="chartbox" id="chartbox"><svg id="chart" aria-labelledby="chart_cap"></svg><div class="tip" id="tip" hidden></div></div>
  <details><summary data-i18n="show_table"></summary><div class="scroll"><table id="t_days"></table></div></details>
</section>

<section class="card">
  <div class="cardhead"><h2 data-i18n="h_models"></h2><button type="button" data-csv="models">CSV</button></div>
  <div class="scroll"><table id="t_models"></table></div>
</section>

<section class="card">
  <div class="cardhead"><h2 data-i18n="h_users"></h2><button type="button" data-csv="users">CSV</button></div>
  <div class="scroll"><table id="t_users"></table></div>
</section>

<details class="card tech" id="tech">
  <summary data-i18n="h_tech"></summary>
  <h3 data-i18n="h_status"></h3><table class="kv" id="status"></table>
  <h3 data-i18n="h_collect"></h3><table class="kv" id="collect"></table>
  <p><button type="button" id="btn" data-i18n="btn_collect"></button> <span id="btnmsg"></span></p>
  <h3 data-i18n="h_mail"></h3><table class="kv" id="mail"></table>
  <p><button type="button" id="btnmail" data-i18n="btn_mail"></button> <span id="mailmsg"></span></p>
  <h3 data-i18n="h_unmeasured"></h3><table id="unmeasured"></table>
  <h3 data-i18n="h_warnings"></h3><div id="warnings"></div>
  <h3 data-i18n="h_valves"></h3><table class="kv" id="valves"></table>
</details>
</div>

<script>
const PREFIX = "__PREFIX__";
const I18N = {
  en: {
    sub: "Token usage and estimated cost, administrators only. Version __VERSION__.",
    r_7: "7 days", r_30: "30 days", r_month: "This month", r_lastmonth: "Last month", r_365: "12 months",
    from: "From", to: "To", apply: "Apply",
    k_cost: "Estimated cost", k_replies: "Replies", k_in: "Input tokens", k_out: "Output tokens", k_users: "Active users",
    cost_sub: "Cloud-equivalent prices, {0}", replies_sub: "{0} without token count",
    h_chart: "Tokens per day", chart_cap: "Input plus output tokens per day, {0} to {1} ({2}).",
    show_table: "Show as table", no_data: "No usage recorded in this period.",
    h_models: "Per model", h_users: "Per user",
    c_day: "Day", c_model: "Model", c_base: "Base model", c_price: "Price key", c_user: "User",
    c_replies: "Replies", c_unmeasured: "Without count", c_in: "Input", c_out: "Output", c_cost: "Est. cost",
    no_price: "NO PRICE",
    n_no_prices: "No prices configured (price_map is empty): costs are not computed.",
    n_unpriced: "{0} tokens belong to models without a price and are not in the cost.",
    n_unmeasured: "{0} replies have no token count (model without the Usage capability, or replies older than it).",
    n_pseudo: "User names are pseudonymized (pseudonymize_users).",
    tip_total: "tokens", tip_in: "Input", tip_out: "Output", tip_cost: "Est. cost", tip_replies: "Replies",
    h_tech: "Technical status", h_status: "Status", h_collect: "Collection", h_unmeasured: "All-time replies per model",
    h_warnings: "Warnings", h_valves: "Valves",
    page: "Page", routes: "Mounted routes", started: "Started", loop: "Background loop", running: "running", stopped: "stopped",
    passes: "Loop passes", last_pass: "Last pass", last_check: "Last check", last_error: "Last error", smtp_pw: "SMTP password",
    none: "None.", chk_active: "function active", chk_inactive: "function inactive", chk_not_found: "function not found", chk_no_id: "function id unknown",
    pw_env: "environment variable", pw_valve: "valve", pw_absent: "not set",
    rows: "Stored replies", unmeasured: "Replies without token count", period: "Period covered",
    tok_in: "Input tokens", tok_out: "Output tokens", backfill: "Initial backfill", last_collect: "Last collection", last_result: "Last result",
    table_error: "Database error", trig_auto: "automatic", trig_manual: "manual",
    btn_collect: "Collect now", busy: "A pass is already running.", done_msg: "Done.",
    h_mail: "Mail report", mail_state: "Scheduled report", m_on: "enabled", m_off: "disabled (mail_enabled)", next_mail: "Next send",
    last_mail: "Last send", m_ok: "sent", m_fail: "failed", sent_to: "Sent to", rejected: "Ignored (not admin accounts)",
    m_error: "Error", btn_mail: "Send a test report (last 7 days)", m_test_ok: "Test report sent to {0}.", m_wait: "Please wait 30 seconds.",
    w_mail_language: "mail_language: expected en or fr (got \"{0}\")",
    err_net: "Network error.", err_http: "Access denied or plugin disabled (HTTP {0}).", err_range: "Invalid period: {0}",
    w_timezone: "timezone: unknown time zone \"{0}\"",
    w_price_map_json: "price_map: invalid JSON ({0})", w_price_map_object: "price_map: a JSON object is expected",
    w_price_map_entry: "price_map: entry \"{0}\" must be an object", w_price_map_number: "price_map: non-numeric price for \"{0}\"",
    w_price_map_negative: "price_map: negative price for \"{0}\"",
    w_ui_language: "ui_language: expected auto, en or fr (got \"{0}\")",
    w_smtp_security: "smtp_security: expected starttls, ssl or none (got \"{0}\")",
    w_mail_frequency: "mail_frequency: expected daily, weekly or monthly (got \"{0}\")",
    w_mail_hour: "mail_hour: expected 0 to 23 (got \"{0}\")",
    w_mail_missing: "mail enabled but {0} is empty",
    w_smtp_cleartext: "smtp_security = none with a password: it would travel in clear text",
    w_smtp_no_verify: "smtp_verify_cert disabled: relay certificate not verified"
  },
  fr: {
    sub: "Consommation de tokens et coût estimé, réservé aux administrateurs. Version __VERSION__.",
    r_7: "7 jours", r_30: "30 jours", r_month: "Ce mois", r_lastmonth: "Mois dernier", r_365: "12 mois",
    from: "Du", to: "Au", apply: "Appliquer",
    k_cost: "Coût estimé", k_replies: "Réponses", k_in: "Tokens d'entrée", k_out: "Tokens de sortie", k_users: "Utilisateurs actifs",
    cost_sub: "Prix équivalent cloud, {0}", replies_sub: "dont {0} sans mesure de tokens",
    h_chart: "Tokens par jour", chart_cap: "Tokens d'entrée et de sortie par jour, du {0} au {1} ({2}).",
    show_table: "Afficher en tableau", no_data: "Aucune consommation enregistrée sur cette période.",
    h_models: "Par modèle", h_users: "Par utilisateur",
    c_day: "Jour", c_model: "Modèle", c_base: "Modèle de base", c_price: "Clé de prix", c_user: "Utilisateur",
    c_replies: "Réponses", c_unmeasured: "Sans mesure", c_in: "Entrée", c_out: "Sortie", c_cost: "Coût estimé",
    no_price: "SANS PRIX",
    n_no_prices: "Aucun prix configuré (price_map vide) : les coûts ne sont pas calculés.",
    n_unpriced: "{0} tokens appartiennent à des modèles sans prix et ne sont pas comptés dans le coût.",
    n_unmeasured: "{0} réponses n'ont pas de mesure de tokens (modèle sans la capacité Usage, ou réponses antérieures).",
    n_pseudo: "Les noms d'utilisateurs sont pseudonymisés (pseudonymize_users).",
    tip_total: "tokens", tip_in: "Entrée", tip_out: "Sortie", tip_cost: "Coût estimé", tip_replies: "Réponses",
    h_tech: "État technique", h_status: "Statut", h_collect: "Collecte", h_unmeasured: "Réponses par modèle, tout l'historique",
    h_warnings: "Avertissements", h_valves: "Vannes",
    page: "Page", routes: "Routes montées", started: "Démarré le", loop: "Boucle de fond", running: "en cours", stopped: "arrêtée",
    passes: "Passages de la boucle", last_pass: "Dernier passage", last_check: "Dernier contrôle", last_error: "Dernière erreur", smtp_pw: "Mot de passe SMTP",
    none: "Aucun.", chk_active: "fonction active", chk_inactive: "fonction inactive", chk_not_found: "fonction introuvable", chk_no_id: "identifiant de fonction inconnu",
    pw_env: "variable d'environnement", pw_valve: "vanne", pw_absent: "absent",
    rows: "Réponses enregistrées", unmeasured: "Réponses sans mesure de tokens", period: "Période couverte",
    tok_in: "Tokens d'entrée", tok_out: "Tokens de sortie", backfill: "Rattrapage initial", last_collect: "Dernière collecte", last_result: "Dernier résultat",
    table_error: "Erreur base de données", trig_auto: "automatique", trig_manual: "manuelle",
    btn_collect: "Collecter maintenant", busy: "Un passage est déjà en cours.", done_msg: "Terminé.",
    h_mail: "Rapport par mail", mail_state: "Rapport planifié", m_on: "activé", m_off: "désactivé (mail_enabled)", next_mail: "Prochain envoi",
    last_mail: "Dernier envoi", m_ok: "envoyé", m_fail: "échec", sent_to: "Envoyé à", rejected: "Ignorées (pas des comptes admin)",
    m_error: "Erreur", btn_mail: "Envoyer un rapport de test (7 derniers jours)", m_test_ok: "Rapport de test envoyé à {0}.", m_wait: "Patientez 30 secondes.",
    w_mail_language: "mail_language : en ou fr attendu (reçu « {0} »)",
    err_net: "Erreur réseau.", err_http: "Accès refusé ou plugin désactivé (HTTP {0}).", err_range: "Période invalide : {0}",
    w_timezone: "timezone : fuseau inconnu « {0} »",
    w_price_map_json: "price_map : JSON invalide ({0})", w_price_map_object: "price_map : un objet JSON est attendu",
    w_price_map_entry: "price_map : l'entrée « {0} » doit être un objet", w_price_map_number: "price_map : prix non numérique pour « {0} »",
    w_price_map_negative: "price_map : prix négatif pour « {0} »",
    w_ui_language: "ui_language : auto, en ou fr attendu (reçu « {0} »)",
    w_smtp_security: "smtp_security : starttls, ssl ou none attendu (reçu « {0} »)",
    w_mail_frequency: "mail_frequency : daily, weekly ou monthly attendu (reçu « {0} »)",
    w_mail_hour: "mail_hour : entre 0 et 23 (reçu « {0} »)",
    w_mail_missing: "mail activé mais {0} est vide",
    w_smtp_cleartext: "smtp_security = none avec mot de passe : il circulerait en clair",
    w_smtp_no_verify: "smtp_verify_cert désactivé : certificat du relais non vérifié"
  }
};
const CFG_LANG = "__LANG__";
const LANG = CFG_LANG === "auto" ? ((navigator.language || "en").toLowerCase().startsWith("fr") ? "fr" : "en") : CFG_LANG;
const LOC = LANG === "fr" ? "fr-FR" : "en-GB";
document.documentElement.lang = LANG;
function t(key, ...args){ let s = I18N[LANG][key] ?? I18N.en[key] ?? key; args.forEach((a, i) => { s = s.replace("{" + i + "}", a ?? ""); }); return s; }
document.getElementById("sub").textContent = t("sub");
document.querySelectorAll("[data-i18n]").forEach(el => { el.textContent = t(el.dataset.i18n); });

let token = null;
try { token = localStorage.getItem("token"); } catch (e) {}
const AUTH = token ? {"Authorization": "Bearer " + token} : {};
const $ = id => document.getElementById(id);
const NF = new Intl.NumberFormat(LOC);
const NC = new Intl.NumberFormat(LOC, {notation: "compact", maximumFractionDigits: 1});
let CUR = "USD";
function money(x){
  try { return new Intl.NumberFormat(LOC, {style: "currency", currency: CUR, minimumFractionDigits: 2, maximumFractionDigits: x !== 0 && Math.abs(x) < 1 ? 4 : 2}).format(x); }
  catch (e) { return x.toFixed(4) + " " + CUR; }
}
function fmtTs(ts){ return ts ? new Date(ts * 1000).toLocaleString(LOC) : "-"; }
function fmtDay(iso, short){ const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString(LOC, short ? {day: "2-digit", month: "2-digit"} : {weekday: "short", day: "numeric", month: "long", year: "numeric"}); }
function pad(n){ return String(n).padStart(2, "0"); }
function iso(d){ return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
function setMsg(text, cls){ const m = $("msg"); m.textContent = text || ""; m.className = cls || ""; }

// ---- period filter
function presetRange(r){
  const today = new Date(); let from, to = today;
  if (r === "month") from = new Date(today.getFullYear(), today.getMonth(), 1);
  else if (r === "lastmonth") { from = new Date(today.getFullYear(), today.getMonth() - 1, 1); to = new Date(today.getFullYear(), today.getMonth(), 0); }
  else { from = new Date(today); from.setDate(today.getDate() - (Number(r) - 1)); }
  return [iso(from), iso(to)];
}
function pressPreset(r){ document.querySelectorAll("[data-range]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.range === r))); }
document.querySelectorAll("[data-range]").forEach(b => b.addEventListener("click", () => {
  const [f, to] = presetRange(b.dataset.range); $("from").value = f; $("to").value = to; pressPreset(b.dataset.range); loadReport();
}));
$("apply").addEventListener("click", () => { pressPreset(null); loadReport(); });

// ---- tables
function cellEl(tag, text, cls, sub){
  const c = document.createElement(tag); c.textContent = text;
  if (cls) c.className = cls;
  if (sub) { const s = document.createElement("span"); s.className = "small"; s.textContent = sub; c.append(s); }
  return c;
}
function fillTable(table, headers, rows){
  table.replaceChildren();
  const thead = document.createElement("thead"); const hr = document.createElement("tr");
  headers.forEach(([label, num]) => hr.append(cellEl("th", label, num ? "num" : "")));
  thead.append(hr); table.append(thead);
  const tb = document.createElement("tbody");
  rows.forEach(r => { const tr = document.createElement("tr"); r.forEach(c => tr.append(cellEl("td", c.text, c.cls, c.sub))); tb.append(tr); });
  table.append(tb);
}
function costCell(b){ return {text: money(b.cost), cls: "num"}; }
const N = x => ({text: NF.format(x), cls: "num"});

// ---- chart (single series: input + output tokens per day)
let LAST_DAYS = [];
function niceStep(max){ const raw = max / 4; const p = Math.pow(10, Math.floor(Math.log10(raw))); const f = raw / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p; }
function drawChart(days){
  LAST_DAYS = days;
  const svg = $("chart"), box = $("chartbox"), tip = $("tip");
  svg.replaceChildren(); tip.hidden = true;
  const old = box.querySelector(".empty"); if (old) old.remove();
  const vals = days.map(d => d.input + d.output);
  const max = Math.max(0, ...vals);
  if (!max) { svg.style.display = "none"; const e = document.createElement("div"); e.className = "empty"; e.textContent = t("no_data"); box.append(e); return; }
  svg.style.display = "";
  const NS = "http://www.w3.org/2000/svg";
  const W = svg.clientWidth || 800, H = 240, pl = 52, pr = 8, pt = 10, pb = 26;
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  const step = niceStep(max), top = Math.ceil(max / step) * step;
  const y = v => pt + (H - pt - pb) * (1 - v / top);
  for (let v = 0; v <= top + step / 2; v += step) {
    const l = document.createElementNS(NS, "line");
    l.setAttribute("x1", pl); l.setAttribute("x2", W - pr); l.setAttribute("y1", y(v)); l.setAttribute("y2", y(v)); l.setAttribute("class", "gridline");
    const tx = document.createElementNS(NS, "text");
    tx.setAttribute("x", pl - 6); tx.setAttribute("y", y(v) + 4); tx.setAttribute("text-anchor", "end"); tx.textContent = top >= 10000 ? NC.format(v) : NF.format(v);
    svg.append(l, tx);
  }
  const n = days.length, band = (W - pl - pr) / n, bw = Math.max(2, Math.min(24, band * 0.7));
  const every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor((W - pl - pr) / 56))));
  days.forEach((d, i) => {
    const v = vals[i], x0 = pl + i * band, bx = x0 + (band - bw) / 2;
    const g = document.createElementNS(NS, "g"); g.setAttribute("class", "bar");
    if (v > 0) g.setAttribute("tabindex", "0");
    g.setAttribute("aria-label", fmtDay(d.day) + ": " + NF.format(v) + " " + t("tip_total") + ", " + money(d.cost));
    const hit = document.createElementNS(NS, "rect");
    hit.setAttribute("class", "hit"); hit.setAttribute("x", x0); hit.setAttribute("y", pt); hit.setAttribute("width", band); hit.setAttribute("height", H - pt - pb);
    g.append(hit);
    if (v > 0) {
      const yb = H - pb, yt = y(v), h = yb - yt, r = Math.min(4, h, bw / 2);
      const p = document.createElementNS(NS, "path");
      p.setAttribute("d", "M" + bx + "," + yb + "V" + (yt + r) + "Q" + bx + "," + yt + " " + (bx + r) + "," + yt + "H" + (bx + bw - r) + "Q" + (bx + bw) + "," + yt + " " + (bx + bw) + "," + (yt + r) + "V" + yb + "Z");
      g.append(p);
    }
    const show = () => showTip(d, x0 + band / 2, v);
    g.addEventListener("pointerenter", show); g.addEventListener("focus", show);
    g.addEventListener("pointerleave", () => { tip.hidden = true; }); g.addEventListener("blur", () => { tip.hidden = true; });
    svg.append(g);
    if (i % every === 0) {
      const tx = document.createElementNS(NS, "text");
      tx.setAttribute("x", x0 + band / 2); tx.setAttribute("y", H - 8); tx.setAttribute("text-anchor", "middle"); tx.textContent = fmtDay(d.day, true);
      svg.append(tx);
    }
  });
}
function showTip(d, cx, v){
  const tip = $("tip"); tip.replaceChildren();
  const line = (txt, cls) => { const e = document.createElement("div"); if (cls) e.className = cls; e.textContent = txt; tip.append(e); return e; };
  line(fmtDay(d.day), "l");
  const head = line("", "v"); const k = document.createElement("span"); k.className = "key"; head.append(k, document.createTextNode(NF.format(v) + " " + t("tip_total")));
  line(t("tip_in") + " : " + NF.format(d.input), "l");
  line(t("tip_out") + " : " + NF.format(d.output), "l");
  line(t("tip_cost") + " : " + money(d.cost), "l");
  line(t("tip_replies") + " : " + NF.format(d.replies), "l");
  tip.hidden = false;
  const bw = $("chartbox").clientWidth, tw = tip.offsetWidth;
  tip.style.left = Math.max(0, Math.min(bw - tw, cx - tw / 2)) + "px"; tip.style.top = "0px";
}
let resizeTimer = null;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (LAST_DAYS.length) drawChart(LAST_DAYS); }, 150); });

// ---- report
async function loadReport(){
  const q = new URLSearchParams(); if ($("from").value) q.set("from", $("from").value); if ($("to").value) q.set("to", $("to").value);
  let r;
  try { r = await fetch(PREFIX + "/api/report?" + q, {headers: AUTH}); } catch (e) { setMsg(t("err_net"), "err"); return; }
  if (r.status === 400) { const j = await r.json().catch(() => ({})); setMsg(t("err_range", j.error || ""), "err"); return; }
  if (!r.ok) { setMsg(t("err_http", r.status), "err"); return; }
  setMsg("");
  const rep = await r.json();
  CUR = rep.currency || "USD";
  $("from").value = rep.from; $("to").value = rep.to;
  const T = rep.total;
  $("k_cost").textContent = rep.price_map_empty ? "-" : money(T.cost);
  $("k_cost_sub").textContent = t("cost_sub", CUR);
  $("k_replies").textContent = NF.format(T.replies);
  $("k_replies_sub").textContent = T.unmeasured ? t("replies_sub", NF.format(T.unmeasured)) : "";
  $("k_in").textContent = NC.format(T.input);
  $("k_out").textContent = NC.format(T.output);
  $("k_users").textContent = NF.format(rep.users.length);
  const notes = $("notes"); notes.replaceChildren();
  const note = (txt, info) => { const d = document.createElement("div"); if (info) d.className = "info"; d.textContent = txt; notes.append(d); };
  if (rep.price_map_empty) note(t("n_no_prices"));
  else if (T.unpriced_tokens) note(t("n_unpriced", NF.format(T.unpriced_tokens)));
  if (T.unmeasured) note(t("n_unmeasured", NF.format(T.unmeasured)));
  if (rep.pseudonymized) note(t("n_pseudo"), true);
  $("chart_cap").textContent = t("chart_cap", fmtDay(rep.from, false), fmtDay(rep.to, false), rep.timezone);
  drawChart(rep.days);
  fillTable($("t_days"), [[t("c_day")], [t("c_replies"), 1], [t("c_unmeasured"), 1], [t("c_in"), 1], [t("c_out"), 1], [t("c_cost"), 1]],
    rep.days.map(d => [{text: fmtDay(d.day, false)}, N(d.replies), N(d.unmeasured), N(d.input), N(d.output), costCell(d)]));
  fillTable($("t_models"), [[t("c_model")], [t("c_price")], [t("c_replies"), 1], [t("c_unmeasured"), 1], [t("c_in"), 1], [t("c_out"), 1], [t("c_cost"), 1]],
    rep.models.map(m => [{text: m.model, sub: m.base ? t("c_base") + " : " + m.base : null},
      m.price_key ? {text: m.price_key} : {text: t("no_price"), cls: "err"},
      N(m.replies), {text: NF.format(m.unmeasured), cls: "num" + (m.unmeasured ? " err" : "")}, N(m.input), N(m.output), costCell(m)]));
  fillTable($("t_users"), [[t("c_user")], [t("c_replies"), 1], [t("c_unmeasured"), 1], [t("c_in"), 1], [t("c_out"), 1], [t("c_cost"), 1]],
    rep.users.map(u => [{text: u.label, sub: u.email}, N(u.replies), N(u.unmeasured), N(u.input), N(u.output), costCell(u)]));
}

// ---- CSV
document.querySelectorAll("[data-csv]").forEach(b => b.addEventListener("click", async () => {
  const q = new URLSearchParams({kind: b.dataset.csv, lang: LANG});
  if ($("from").value) q.set("from", $("from").value); if ($("to").value) q.set("to", $("to").value);
  b.disabled = true;
  try {
    const r = await fetch(PREFIX + "/api/export.csv?" + q, {headers: AUTH});
    if (!r.ok) { setMsg(t("err_http", r.status), "err"); return; }
    const blob = await r.blob();
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
    a.download = "helios-" + b.dataset.csv + "-" + $("from").value + "-" + $("to").value + ".csv";
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } catch (e) { setMsg(t("err_net"), "err"); }
  finally { b.disabled = false; }
}));

// ---- technical status
function kv(table, k, v, cls){
  const tr = document.createElement("tr");
  tr.append(cellEl("td", k), cellEl("td", (v === null || v === undefined || v === "") ? "-" : String(v), cls));
  table.append(tr);
}
async function loadStatus(){
  let r;
  try { r = await fetch(PREFIX + "/api/status", {headers: AUTH}); } catch (e) { return; }
  if (!r.ok) { setMsg(t("err_http", r.status), "err"); return; }
  const s = await r.json();
  const st = $("status"); st.replaceChildren();
  kv(st, t("page"), s.prefix);
  kv(st, t("routes"), s.routes + " / " + s.routes_expected, s.routes === s.routes_expected ? "ok" : "err");
  kv(st, t("started"), fmtTs(s.started_at));
  kv(st, t("loop"), s.loop_running ? t("running") : t("stopped"), s.loop_running ? "ok" : "err");
  kv(st, t("passes"), s.loop_iterations);
  kv(st, t("last_pass"), fmtTs(s.last_tick));
  kv(st, t("last_check"), s.last_check ? t("chk_" + s.last_check) : null, s.last_check === "active" ? "ok" : (s.last_check ? "err" : ""));
  kv(st, t("last_error"), s.last_error, s.last_error ? "err" : "");
  kv(st, t("smtp_pw"), t("pw_" + s.smtp_password_source));
  const w = $("warnings"); w.replaceChildren();
  if (!s.warnings.length) w.append(cellEl("div", t("none"), "ok"));
  for (const [code, arg] of s.warnings) w.append(cellEl("div", t("w_" + code, arg), "warn"));
  if (s.warnings.length) $("tech").open = true;
  const vt = $("valves"); vt.replaceChildren();
  for (const [k, v] of Object.entries(s.valves)) kv(vt, k, v);
  const mt = $("mail"); mt.replaceChildren();
  kv(mt, t("mail_state"), s.valves.mail_enabled ? t("m_on") : t("m_off"), s.valves.mail_enabled ? "ok" : "");
  if (s.next_mail) kv(mt, t("next_mail"), fmtTs(s.next_mail));
  const lm = s.table && s.table.last_mail;
  if (lm) {
    kv(mt, t("last_mail"), fmtTs(lm.at) + " (" + (lm.trigger === "test" ? "test" : t("trig_auto")) + ", " + (lm.ok ? t("m_ok") : t("m_fail")) + ")", lm.ok ? "ok" : "err");
    if (lm.sent_to && lm.sent_to.length) kv(mt, t("sent_to"), lm.sent_to.join(", "));
    if (lm.rejected && lm.rejected.length) kv(mt, t("rejected"), lm.rejected.join(", "), "err");
    if (lm.error) kv(mt, t("m_error"), lm.error, "err");
  }
  const ct = $("collect"); ct.replaceChildren();
  if (s.table_error) { kv(ct, t("table_error"), s.table_error, "err"); $("tech").open = true; return; }
  const tb = s.table; if (!tb) return;
  kv(ct, t("rows"), NF.format(tb.rows));
  kv(ct, t("unmeasured"), NF.format(tb.unmeasured), tb.unmeasured ? "err" : "ok");
  kv(ct, t("period"), tb.first_ts ? fmtTs(tb.first_ts) + " → " + fmtTs(tb.last_ts) : null);
  kv(ct, t("tok_in"), NF.format(tb.input_tokens));
  kv(ct, t("tok_out"), NF.format(tb.output_tokens));
  kv(ct, t("backfill"), tb.backfill_done ? fmtTs(Number(tb.backfill_done)) : null);
  const lc = tb.last_collect;
  if (lc) {
    kv(ct, t("last_collect"), fmtTs(lc.at) + " (" + t("trig_" + lc.trigger) + ", " + lc.duration_ms + " ms)");
    kv(ct, t("last_result"), "+" + lc.inserted + " / ↑" + lc.upgraded + " / −" + lc.purged);
  }
  fillTable($("unmeasured"), [[t("c_model")], [t("c_replies"), 1], [t("c_unmeasured"), 1]],
    tb.per_model.map(m => [{text: m.model}, N(m.rows), {text: NF.format(m.unmeasured), cls: "num" + (m.unmeasured ? " err" : "")}]));
}
$("btn").addEventListener("click", async () => {
  const btn = $("btn"), out = $("btnmsg");
  btn.disabled = true; out.textContent = ""; out.className = "";
  try {
    const r = await fetch(PREFIX + "/api/collect", {method: "POST", headers: {"Content-Type": "application/json", ...AUTH}});
    const j = await r.json().catch(() => ({}));
    if (r.status === 409) { out.textContent = t("busy"); out.className = "err"; }
    else if (!r.ok) { out.textContent = t("err_http", r.status) + (j.error ? " " + j.error : ""); out.className = "err"; }
    else { out.textContent = t("done_msg"); out.className = "ok"; }
  } catch (e) { out.textContent = t("err_net"); out.className = "err"; }
  btn.disabled = false;
  loadStatus(); loadReport();
});

$("btnmail").addEventListener("click", async () => {
  const btn = $("btnmail"), out = $("mailmsg");
  btn.disabled = true; out.textContent = ""; out.className = "";
  try {
    const r = await fetch(PREFIX + "/api/mail-test", {method: "POST", headers: {"Content-Type": "application/json", ...AUTH}});
    const j = await r.json().catch(() => ({}));
    if (r.status === 429) { out.textContent = t("m_wait"); out.className = "err"; }
    else if (!r.ok) { out.textContent = t("err_http", r.status); out.className = "err"; }
    else if (j.ok) { out.textContent = t("m_test_ok", j.sent_to.join(", ")); out.className = "ok"; }
    else { out.textContent = t("m_error") + " : " + (j.error || JSON.stringify(j.refused)); out.className = "err"; }
  } catch (e) { out.textContent = t("err_net"); out.className = "err"; }
  btn.disabled = false;
  loadStatus();
});

pressPreset("30");
loadReport();
loadStatus();
</script>
</body>
</html>
"""
