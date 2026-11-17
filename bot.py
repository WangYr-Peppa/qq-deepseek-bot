# -*- coding: utf-8 -*-
"""Personal hub QQ bot: group / C2C messages answered by the DeepSeek API.

Features: per-person conversation memory in SQLite, live time injection,
image understanding via the multimodal model, owner-only slash commands.
"""
import asyncio
import base64
import collections
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import time
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import botpy
import httpx
from botpy.message import C2CMessage, GroupMessage

# The official API host moved to api.bot.qq.com on 2026-08-10, while the
# installed SDK still defaults to the legacy host, so patch it when needed.
API_DOMAIN = os.getenv("QQ_API_DOMAIN", "api.bot.qq.com")
try:
    from botpy.http import Route

    SDK_DOMAIN = getattr(Route, "DOMAIN", "unknown")
    Route.DOMAIN = API_DOMAIN
    Route.SANDBOX_DOMAIN = "sandbox." + API_DOMAIN
except Exception:  # pragma: no cover - fall back to SDK defaults
    SDK_DOMAIN = "unknown"
    API_DOMAIN = SDK_DOMAIN or "sdk-default"

# All human-facing timestamps are Beijing time regardless of the host TZ.
TZ = ZoneInfo("Asia/Shanghai")

DEEPSEEK_KEY = os.environ["DEEPSEEK_API_KEY"]
# The Anthropic-compatible endpoint is used because it is the only one that can
# run server-side web search (tools[].type = web_search_20250305); the OpenAI
# endpoint rejects any tool whose type is not "function".
ANTHROPIC_URL = os.getenv(
    "DEEPSEEK_ANTHROPIC_URL", "https://api.deepseek.com/anthropic/v1/messages"
)
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
MAX_CHARS = int(os.getenv("BOT_MAX_CHARS", "1500"))
# max_tokens is a *ceiling that also covers thinking tokens*; a tight value
# makes the model run out of budget and return only a thinking block, so keep it
# far above the visible reply length and truncate at send time instead.
MAX_OUTPUT_TOKENS = int(os.getenv("BOT_MAX_TOKENS", "4096"))
# QQ rejects over-long passive replies; keep a byte cap next to the char cap.
MAX_BYTES = int(os.getenv("BOT_MAX_BYTES", "4000"))

# --- web search ----------------------------------------------------------
# Executed by DeepSeek server-side; the model decides when to call it, so chat
# turns pay nothing and only factual questions trigger a search.
WEB_SEARCH = os.getenv("BOT_WEB_SEARCH", "1").lower() not in ("0", "false", "no", "")
SEARCH_MAX_USES = int(os.getenv("BOT_SEARCH_MAX_USES", "3"))
SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": SEARCH_MAX_USES,
}
LLM_HEADERS = {
    "x-api-key": DEEPSEEK_KEY,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}

# --- conversation memory -------------------------------------------------
MEMORY_TURNS = int(os.getenv("BOT_MEMORY_TURNS", "10"))       # Q&A rounds kept
MEMORY_TTL = int(os.getenv("BOT_MEMORY_TTL", "1800"))         # idle seconds
MEMORY_MAX_CHARS = int(os.getenv("BOT_MEMORY_MAX_CHARS", "4000"))

# --- archive -------------------------------------------------------------
# Every message is kept on disk; the model only ever sees the short window
# above, so storage size and context cost stay independent.
ARCHIVE_DAYS = int(os.getenv("BOT_ARCHIVE_DAYS", "180"))   # 0 = keep forever
EXPORT_DIR = os.getenv("BOT_EXPORT_DIR", "/opt/hub/exports")
# Shown in /export replies so the operator knows how to pull the dump off the box.
SCP_PREFIX = os.getenv("BOT_SCP_PREFIX", "<user>@<host>")
DB_PATH = os.getenv("BOT_DB_PATH", "/opt/hub/memory.db")

# --- images --------------------------------------------------------------
MAX_IMAGES = int(os.getenv("BOT_MAX_IMAGES", "3"))
IMAGE_MAX_BYTES = int(os.getenv("BOT_IMAGE_MAX_BYTES", str(8 * 1024 * 1024)))

# --- link fetching -------------------------------------------------------
# DeepSeek's endpoint offers no server-side web_fetch tool, so links pasted by
# the user are fetched here and injected as text (or as an image).
FETCH_PAGES = os.getenv("BOT_FETCH_PAGES", "1").lower() not in ("0", "false", "no", "")
FETCH_MAX_PAGES = int(os.getenv("BOT_FETCH_MAX_PAGES", "2"))
FETCH_MAX_BYTES = int(os.getenv("BOT_FETCH_MAX_BYTES", str(2 * 1024 * 1024)))
FETCH_MAX_CHARS = int(os.getenv("BOT_FETCH_MAX_CHARS", "3500"))
FETCH_TIMEOUT = int(os.getenv("BOT_FETCH_TIMEOUT", "20"))
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# --- permissions ---------------------------------------------------------
OWNER_IDS = {x.strip() for x in os.getenv("BOT_OWNER_IDS", "").split(",") if x.strip()}
PRIVATE_MODE = os.getenv("BOT_PRIVATE_MODE", "1").lower() not in ("0", "false", "no")
RESET_WORDS = {"清空对话", "重置对话", "重新开始"}

DEFAULT_PROMPT = "你是一个简洁、准确的中文助手。回答控制在 300 字以内，不确定就直说。"
PROMPT_FILE = os.getenv("BOT_PROMPT_FILE", "/opt/hub/prompt.md")
_prompt_cache = {"mtime": None, "text": None}
START_TIME = time.time()

logging.basicConfig(
    level=os.getenv("BOT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("botpy").setLevel(os.getenv("BOT_LOG_LEVEL", "INFO").upper())
log = logging.getLogger("hub-bot")
# botpy installs its own root handler, which swallows this module's INFO
# records. Give the bot its own handler so msg/usage lines reach journald.
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    log.addHandler(_handler)
    log.propagate = False
log.setLevel(os.getenv("BOT_LOG_LEVEL", "INFO").upper())

_conn = None


def db() -> sqlite3.Connection:
    """Lazily open the SQLite file and make sure the schema exists."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                   id      INTEGER PRIMARY KEY AUTOINCREMENT,
                   conv    TEXT NOT NULL,
                   role    TEXT NOT NULL,
                   content TEXT NOT NULL,
                   ts      REAL NOT NULL)"""
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_ts ON messages(conv, ts)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON messages(ts)")
        # Soft-reset marker: rows older than reset_ts stay archived but are
        # hidden from the model window, so /reset never destroys history.
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS conv_state (
                   conv     TEXT PRIMARY KEY,
                   reset_ts REAL NOT NULL DEFAULT 0)"""
        )
        _conn.commit()
    return _conn


def now_local() -> datetime:
    """Wall clock in Beijing time, independent of the host timezone."""
    return datetime.now(TZ)


def now_context() -> str:
    """Current local time, injected so the model can answer time questions."""
    n = now_local()
    week = "一二三四五六日"[n.weekday()]
    return (
        f"\n\n[系统信息] 当前北京时间：{n:%Y-%m-%d %H:%M}，星期{week}。"
        "涉及时间、日期、星期的问题以此为准。"
    )


def load_system_prompt() -> str:
    """Read the system prompt from PROMPT_FILE, reloading it when the file changes."""
    try:
        mtime = os.path.getmtime(PROMPT_FILE)
        if _prompt_cache["mtime"] != mtime:
            with open(PROMPT_FILE, encoding="utf-8") as fh:
                text = fh.read().strip()
            _prompt_cache["mtime"] = mtime
            _prompt_cache["text"] = text or None
            log.info("prompt reloaded (%d chars)", len(text))
    except OSError as exc:
        log.debug("prompt file unavailable: %r", exc)
    return _prompt_cache["text"] or os.getenv("BOT_SYSTEM_PROMPT") or DEFAULT_PROMPT


def history_for(key: str) -> list:
    cutoff = window_start(key)
    rows = db().execute(
        "SELECT role, content, ts FROM messages"
        " WHERE conv = ? AND ts >= ? ORDER BY id DESC LIMIT ?",
        (key, cutoff, MEMORY_TURNS * 2),
    ).fetchall()
    return [{"role": r, "content": c, "ts": t} for r, c, t in reversed(rows)]


def remember(key: str, role: str, content: str) -> None:
    con = db()
    con.execute(
        "INSERT INTO messages (conv, role, content, ts) VALUES (?, ?, ?, ?)",
        (key, role, content, time.time()),
    )
    # No trimming here: the archive keeps every row, and the reading
    # window (MEMORY_TURNS / MEMORY_TTL) is applied in history_for().
    con.commit()


def reset_at(key: str) -> float:
    """Soft-reset wall clock for this conversation; 0.0 when never reset."""
    row = db().execute(
        "SELECT reset_ts FROM conv_state WHERE conv = ?", (key,)
    ).fetchone()
    return float(row[0]) if row else 0.0


def window_start(key: str) -> float:
    """Lower bound of what the model may see: TTL cutoff or last soft reset."""
    return max(time.time() - MEMORY_TTL, reset_at(key))


def soft_forget(key: str) -> None:
    """Hide the current window without touching the archive."""
    con = db()
    con.execute(
        "INSERT INTO conv_state (conv, reset_ts) VALUES (?, ?)"
        " ON CONFLICT(conv) DO UPDATE SET reset_ts = excluded.reset_ts",
        (key, time.time()),
    )
    con.commit()


def forget(key: str) -> None:
    """Physically delete this conversation, archive included (/wipe only)."""
    con = db()
    con.execute("DELETE FROM messages WHERE conv = ?", (key,))
    con.execute("DELETE FROM conv_state WHERE conv = ?", (key,))
    con.commit()


def purge_expired() -> None:
    """Trim the archive horizon (ARCHIVE_DAYS); 0 keeps it permanent.

    This is *not* the context window - that is enforced at read time in
    history_for(), so a live conversation is never affected here.
    """
    if ARCHIVE_DAYS <= 0:
        return
    con = db()
    cur = con.execute(
        "DELETE FROM messages WHERE ts < ?", (time.time() - ARCHIVE_DAYS * 86400,)
    )
    if cur.rowcount:
        log.info("trimmed %d rows past archive horizon", cur.rowcount)
    con.commit()


def stats_for(key: str) -> tuple:
    """Count only rows the model can see: TTL window minus soft-reset rows."""
    cutoff = window_start(key)
    row = db().execute(
        "SELECT COUNT(*), MIN(ts) FROM messages WHERE conv = ? AND ts >= ?",
        (key, cutoff),
    ).fetchone()
    return int(row[0] or 0), row[1]


def build_messages(key: str, text: str, images: list | None = None) -> list:
    msgs = [{"role": "system", "content": load_system_prompt() + now_context()}]
    hist = history_for(key)
    total = sum(len(m["content"]) for m in hist)
    while hist and total + len(text) > MEMORY_MAX_CHARS:
        total -= len(hist.pop(0)["content"])
    # A trimmed window must not start with an assistant turn: the model would
    # read it as an unanswered reply of its own.
    while hist and hist[0]["role"] != "user":
        hist.pop(0)
    msgs.extend({"role": m["role"], "content": m["content"]} for m in hist)
    if images:
        parts = [{"type": "text", "text": text or "描述这张图片"}]
        parts.extend({"type": "image_url", "image_url": {"url": u}} for u in images)
        msgs.append({"role": "user", "content": parts})
    else:
        msgs.append({"role": "user", "content": text})
    return msgs


TRUNC_SUFFIX = "……(太长，截了)"
# The persona writes two-bubble replies as "先嘴硬 ||| 再补一句"; the platform
# allows up to five passive replies per inbound message, so three is safe.
SPLIT_RE = re.compile(r"\s*\|\|\|\s*")
MAX_PARTS = int(os.getenv("BOT_MAX_PARTS", "3"))


def split_reply(text: str) -> list:
    """Turn a model reply into the 1..MAX_PARTS bubbles it will be sent as."""
    parts = [p.strip() for p in SPLIT_RE.split(text or "") if p.strip()]
    if not parts:
        return []
    if len(parts) > MAX_PARTS:  # fold the tail into the last bubble
        parts = parts[: MAX_PARTS - 1] + [" ".join(parts[MAX_PARTS - 1:])]
    return [p for p in (truncate(x) for x in parts) if p]


def truncate(text: str, limit: int = MAX_CHARS) -> str:
    """Fit a reply into both the char and byte caps QQ accepts.

    Idempotent: the returned string always satisfies both caps, so applying it
    twice (once when storing, once when sending) is a no-op.
    """
    if len(text) <= limit and len(text.encode("utf-8")) <= MAX_BYTES:
        return text
    head = max(1, limit - len(TRUNC_SUFFIX))
    cut = text[:head]
    raw = cut.encode("utf-8")
    room = max(1, MAX_BYTES - len(TRUNC_SUFFIX.encode("utf-8")))
    if len(raw) > room:
        cut = raw[:room].decode("utf-8", "ignore")
    return cut.rstrip() + TRUNC_SUFFIX


_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF8", "image/gif"),
    (b"RIFF", "image/webp"),
    (b"BM", "image/bmp"),
)


def sniff_image(data: bytes) -> str | None:
    """Trust the bytes, not the header: QQ CDNs often answer octet-stream."""
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    return None


async def download_image(url: str | None) -> str | None:
    """Download a chat image and return it as a base64 data URL."""
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > IMAGE_MAX_BYTES:
                log.warning("image too large (declared): %s bytes", declared)
                return None
            body = resp.content
            if len(body) > IMAGE_MAX_BYTES:
                log.warning("image too large: %d bytes", len(body))
                return None
            mime = sniff_image(body)
            if not mime:
                header = (resp.headers.get("content-type") or "").split(";")[0]
                if not header.startswith("image/"):
                    log.warning("not an image (%s, %d bytes)", header, len(body))
                    return None
                mime = header
            b64 = base64.b64encode(body).decode()
            return f"data:{mime};base64,{b64}"
    except Exception as exc:  # noqa: BLE001
        log.warning("image download failed: %r", exc)
        return None


def has_voice(atts) -> bool:
    """True when one of the attachments is a voice/audio clip."""
    for att in atts or []:
        ctype = (getattr(att, "content_type", "") or "").lower()
        if ctype == "voice" or ctype.startswith("audio/") or "silk" in ctype:
            return True
    return False


def is_image(att) -> bool:
    """QQ sends bare 'image' for group/C2C attachments and 'image/*' elsewhere."""
    ctype = (getattr(att, "content_type", "") or "").lower()
    return ctype == "image" or ctype.startswith("image/")


async def collect_images(atts) -> list:
    images = []
    for att in atts or []:
        if len(images) >= MAX_IMAGES:
            break
        if is_image(att):
            data_url = await download_image(getattr(att, "url", None))
            if data_url:
                images.append(data_url)
    return images


# --- link fetching -------------------------------------------------------
# DeepSeek's Anthropic endpoint exposes no web_fetch tool (only web_search), so
# a URL the user pastes is fetched here and injected as page text.
# Chinese punctuation and full-width marks end a URL, but ideographs do not:
# "https://host/path你觉得怎么样" (no space, common in QQ) and a real CJK path
# like .../wiki/傅里叶变换 are indistinguishable, so the raw URL is tried first
# and the CJK tail is only stripped when the request fails.
_URL_RE = re.compile(
    r"https?://[^\s<>\"'）)\]】，。；、\u3000-\u303f\uff00-\uffef]+"
)
_CJK_TAIL = re.compile(r"[\u4e00-\u9fff].*$")


def extract_urls(text: str, limit: int = FETCH_MAX_PAGES) -> list:
    seen, out = set(), []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:!?")
        if url and url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= limit:
            break
    return out


def _blocked_ip(ip) -> bool:
    """True for any address that can reach the host itself or its LAN."""
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


# 127.1, 2130706433 and 0x7f.0.0.1 all reach loopback, but only the last form
# is an `ipaddress` IPv4 string. inet_aton understands every legacy spelling.
_NUMERIC_HOST = re.compile(
    r"^(?:0x[0-9a-f]+|\d{1,10})(?:\.(?:0x[0-9a-f]+|\d{1,10})){0,3}$", re.I
)


def _numeric_host_ip(name: str):
    if not _NUMERIC_HOST.match(name):
        return None
    try:
        return ipaddress.ip_address(socket.inet_aton(name))
    except OSError:
        return None


def _host_blocked(host: str | None) -> bool:
    """Refuse loopback/private/link-local hosts, including cloud metadata IPs.

    Hostnames are resolved first: a public-looking name whose A record points at
    10.0.0.0/8 must not be fetched. This check and the connect that follows are
    two separate lookups, so a TTL-0 rebinding domain can still win the race --
    acceptable for a personal bot, but do not run this behind a cloud metadata
    endpoint you actually care about.
    """
    if not host:
        return True
    name = host.strip("[]").lower().rstrip(".")
    if name in {"localhost", "metadata"} or name.endswith(
        (".internal", ".local", ".localhost")
    ):
        return True
    try:
        return _blocked_ip(ipaddress.ip_address(name))
    except ValueError:
        pass
    numeric = _numeric_host_ip(name)
    if numeric is not None:
        return _blocked_ip(numeric)
    try:
        infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return True
    if not infos:
        return True
    return any(_blocked_ip(info[4][0]) for info in infos)


class _TextExtractor(HTMLParser):
    """Pull readable text out of a page without a third-party parser."""

    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form", "iframe"}
    BREAK = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5",
        "section", "article", "blockquote", "td",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.parts.append(data.strip() + " ")

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\u00a0]+", " ", raw)
        raw = re.sub(r"\n[ \t]*\n+", "\n", raw)
        return raw.strip()


def html_to_text(html: str) -> tuple:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed markup must not break a reply
        pass
    return parser.title.strip()[:120], parser.text()


_META_CHARSET = re.compile(rb"charset=[\"']?([\w-]+)", re.I)


def _decode_body(body: bytes, header_charset: str | None) -> str:
    """Chinese pages are often gbk; try the declared encoding before utf-8."""
    match = _META_CHARSET.search(body[:4096])
    candidates = [
        header_charset,
        match.group(1).decode("ascii", "ignore") if match else None,
        "utf-8",
        "gb18030",
    ]
    for enc in candidates:
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "ignore")


MAX_REDIRECTS = 3
_REDIRECT_CODES = {301, 302, 303, 307, 308}


async def _fetch_checked(client: httpx.AsyncClient, url: str) -> tuple:
    """GET with redirects followed by hand, so every hop is re-checked.

    httpx's own follow_redirects would happily chase a 302 to 169.254.169.254.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https") or _host_blocked(parts.hostname):
            log.info("link hop skipped (scheme/host): %s", current)
            return None, current
        resp = await client.get(current)
        location = resp.headers.get("location")
        if resp.status_code in _REDIRECT_CODES and location:
            current = urljoin(current, location)
            continue
        return resp, current
    log.info("link skipped (too many redirects): %s", url)
    return None, current


async def _fetch_once(url: str) -> tuple:
    """Fetch one URL; return (text_block, image_data_url)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or _host_blocked(parts.hostname):
        log.info("link skipped (scheme/host): %s", url)
        return f"[链接已跳过：{url}]", None
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT,
            # Redirects are followed in _fetch_checked instead, one hop at a time.
            follow_redirects=False,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        ) as client:
            resp, final_url = await _fetch_checked(client, url)
        if resp is None:
            return f"[链接已跳过：{url}]", None
        if resp.status_code >= 400:
            return f"[链接打不开：{final_url}（HTTP {resp.status_code}）]", None
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > 8 * FETCH_MAX_BYTES:
            return f"[链接太大：{final_url}（{declared} 字节）]", None
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        body = resp.content[:FETCH_MAX_BYTES]
        if ctype.startswith("image/") or sniff_image(body):
            if len(resp.content) > IMAGE_MAX_BYTES:
                return f"[图片太大：{final_url}]", None
            mime = sniff_image(resp.content) or ctype
            b64 = base64.b64encode(resp.content).decode()
            return "", f"data:{mime};base64,{b64}"
        if ctype and not ctype.startswith(("text/", "application/json", "application/xml")):
            return f"[链接不是网页：{final_url}（{ctype}）]", None
        title, text = html_to_text(_decode_body(body, resp.charset_encoding))
        text = text[:FETCH_MAX_CHARS]
        if not text:
            return f"[链接没有正文：{final_url}]", None
        head = f"[网页] {final_url}" + (f"（{title}）" if title else "")
        if len(text) < 200:
            # A single-page app returns an almost empty shell; say so instead of
            # letting the model guess about content it never saw.
            text = text + "\n[正文很少：这个页面可能要 JS 才渲染得出来]"
        log.info("link fetched: %s (%d chars)", final_url, len(text))
        return head + "\n" + text, None
    except Exception as exc:  # noqa: BLE001
        log.warning("link fetch failed %s: %r", url, exc)
        return f"[链接打不开：{url}]", None


async def fetch_link(url: str) -> tuple:
    """Fetch a pasted URL, retrying without a CJK tail when that looks glued on."""
    candidates = [url]
    trimmed = _CJK_TAIL.sub("", url)
    if trimmed and trimmed != url:
        candidates.append(trimmed)
    last_block = ""
    for candidate in candidates:
        block, image = await _fetch_once(candidate)
        if image or block.startswith("[网页]"):
            return block, image
        last_block = block
        if candidate != url:
            log.info("link retried without CJK tail: %s -> %s", url, candidate)
    return last_block or f"[链接打不开：{url}]", None


# --- quoted messages -----------------------------------------------------
# botpy 1.2.1 parses content / attachments / message_reference only, so a
# quoted message's payload ("msg_elements") is thrown away — that is where QQ
# puts the quoted image or text. Capture the raw payload and pick it up by
# message id.
_RAW: collections.OrderedDict = collections.OrderedDict()
_RAW_MAX = 256


def capture_raw(message_id: str | None, data: dict) -> None:
    if not message_id:
        return
    _RAW[message_id] = data
    while len(_RAW) > _RAW_MAX:
        _RAW.popitem(last=False)


class QuotedAtt:
    """Duck-typed stand-in for botpy's attachment object."""

    def __init__(self, data: dict):
        self.content_type = data.get("content_type")
        self.filename = data.get("filename")
        self.url = data.get("url")
        self.size = data.get("size")


def quoted_context(message_id: str | None) -> tuple[list, list]:
    """Return (quoted_texts, quoted_attachments) for a quote-reply event."""
    raw = _RAW.pop(message_id, None) if message_id else None
    if not raw:
        return [], []
    texts, atts = [], []
    for element in raw.get("msg_elements") or []:
        body = clean_incoming(element.get("content") or "")
        if body:
            texts.append(body)
        for att in element.get("attachments") or []:
            if att.get("url"):
                atts.append(QuotedAtt(att))
    return texts, atts


class ModelError(Exception):
    """DeepSeek call failed after the retry budget."""


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500 or status == 408


def to_anthropic(messages: list) -> tuple:
    """Convert the OpenAI-shaped messages we build into (system, convo)."""
    system = ""
    convo = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            system = content if isinstance(content, str) else ""
            continue
        if isinstance(content, str):
            convo.append({"role": role, "content": content})
            continue
        blocks = []
        for part in content or []:
            kind = part.get("type")
            if kind == "text":
                blocks.append({"type": "text", "text": part.get("text") or ""})
            elif kind == "image_url":
                url = (part.get("image_url") or {}).get("url") or ""
                if not url.startswith("data:"):
                    continue
                header, _, data = url.partition(",")
                media = header.split(":")[1].split(";")[0] if ":" in header else "image/jpeg"
                blocks.append(
                    {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
                )
        convo.append({"role": role, "content": blocks})
    return system, convo


async def ask_deepseek(
    messages: list, max_tokens: int = MAX_OUTPUT_TOKENS
) -> tuple:
    """Return (text, stop_reason, usage, searches).

    Only `text` blocks are ever returned: `thinking` blocks are the model's
    private monologue and must never reach the chat, so an exhausted budget
    shows up as an empty answer and is retried instead of leaked.
    """
    system, convo = to_anthropic(messages)
    body = {"model": MODEL, "messages": convo, "max_tokens": max_tokens}
    if system:
        body["system"] = system
    if WEB_SEARCH:
        body["tools"] = [SEARCH_TOOL]
    last = "unknown error"
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=150) as client:
                resp = await client.post(ANTHROPIC_URL, headers=LLM_HEADERS, json=body)
            if resp.status_code >= 400:
                last = f"http {resp.status_code}: {resp.text[:200]}"
                if not _retryable(resp.status_code):
                    raise ModelError(last)
                log.warning("deepseek retryable error: %s", last)
            else:
                data = resp.json()
                if data.get("error") or data.get("type") == "error":
                    last = str(data)[:200]
                    log.warning("deepseek error payload: %s", last)
                else:
                    blocks = data.get("content") or []
                    text = "".join(
                        b.get("text") or "" for b in blocks if b.get("type") == "text"
                    ).strip()
                    usage = data.get("usage") or {}
                    searches = sum(
                        1 for b in blocks if b.get("type") == "web_search_tool_result"
                    )
                    if text:
                        return text, data.get("stop_reason"), usage, searches
                    last = (
                        f"empty text (stop={data.get('stop_reason')},"
                        f" blocks={[b.get('type') for b in blocks]})"
                    )
                    log.warning("deepseek %s usage=%s", last, usage)
        except (httpx.HTTPError, ValueError) as exc:
            last = repr(exc)
            log.warning("deepseek call failed: %s", last)
        if attempt == 1:
            # Guarantee a text answer on the retry: thinking blocks are what
            # swallow the token budget, so the retry runs without them. The
            # reminder keeps the retry in persona instead of an essay.
            body = dict(
                body, max_tokens=max(max_tokens * 2, 2048), thinking={"type": "disabled"}
            )
            if body.get("system"):
                body["system"] = body["system"] + "\n\n（刚才卡住了，这次直接说，最多两句，别写小作文。）"
            await asyncio.sleep(1.5)
    raise ModelError(last)


# --- slash commands ------------------------------------------------------
COMMANDS = {}


def command(name: str):
    def register(fn):
        COMMANDS[name] = fn
        return fn

    return register


def _rss_mb() -> str:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return f"{int(line.split()[1]) / 1024:.0f} MB"
    except OSError:
        pass
    return "n/a"


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    parts.append(f"{secs}秒")
    return "".join(parts)


@command("/help")
async def cmd_help(uid, key, arg):
    return (
        "可用指令（仅管理员）：\n"
        "/help    显示这条帮助\n"
        "/id      查看自己的 openid 与会话键\n"
        "/stats   上下文轮数、库行数、内存、运行时长\n"
        "/ping    测试 DeepSeek 接口延迟\n"
        "/time    服务器时间、时区、运行时长\n"
        "/model   当前模型与提示词信息\n"
        "/recall  检索历史：/recall 关键词（加 all 跨会话）\n"
        "/export  导出全部对话到服务器文件\n"
        "/reset   清空上下文窗口（存档保留）\n"
        "/wipe    永久删除本会话记录（/wipe yes 确认）\n"
        "/split   分条测试（/split 2 发两条）\n"
        "中文触发词：清空对话 / 重置对话 / 重新开始（同 /reset，保留存档）"
    )


@command("/id")
async def cmd_id(uid, key, arg):
    return f"openid：{uid}\n会话键：{key}"


@command("/stats")
async def cmd_stats(uid, key, arg):
    rows, first_ts = stats_for(key)
    total = db().execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    idle = f"{int(time.time() - first_ts)} 秒前" if first_ts else "无"
    mark = reset_at(key)
    reset_line = (
        f"软重置：{datetime.fromtimestamp(mark).strftime('%m-%d %H:%M')}"
        "（此前记录仍在存档）\n"
        if mark
        else ""
    )
    return (
        f"本会话：{rows // 2} 轮 / {rows} 条有效（最早 {idle}）\n"
        f"{reset_line}"
        f"存档：{total} 行（含窗口外），{os.path.getsize(DB_PATH) / 1024:.1f} KB\n"
        f"内存占用：{_rss_mb()}\n"
        f"运行时长：{_fmt_duration(time.time() - START_TIME)}"
    )


@command("/time")
async def cmd_time(uid, key, arg):
    n = now_local()
    week = "一二三四五六日"[n.weekday()]
    return (
        f"北京时间：{n:%Y-%m-%d %H:%M:%S} 星期{week}\n"
        f"时区：{TZ.key}（UTC{n.strftime('%z')[:3]}:{n.strftime('%z')[3:]}）\n"
        f"已运行：{_fmt_duration(time.time() - START_TIME)}"
    )


@command("/ping")
async def cmd_ping(uid, key, arg):
    t0 = time.time()
    try:
        await ask_deepseek([{"role": "user", "content": "ping"}], max_tokens=256)
    except ModelError as exc:
        return f"调用失败：{exc}"
    return f"DeepSeek 往返 {int((time.time() - t0) * 1000)} ms"


@command("/model")
async def cmd_model(uid, key, arg):
    prompt = load_system_prompt()
    return (
        f"模型：{MODEL}\n"
        f"提示词：{len(prompt)} 字符（{PROMPT_FILE}）\n"
        f"记忆：{MEMORY_TURNS} 轮 / TTL {MEMORY_TTL}s / 上限 {MEMORY_MAX_CHARS} 字符\n"
        f"图片：最多 {MAX_IMAGES} 张，单张上限 {IMAGE_MAX_BYTES // 1024 // 1024} MB\n"
        f"输出：max_tokens {MAX_OUTPUT_TOKENS}，发送上限 {MAX_CHARS} 字 / {MAX_BYTES} 字节\n"
        f"联网：{'开（最多 ' + str(SEARCH_MAX_USES) + ' 次/条）' if WEB_SEARCH else '关'}\n"
        f"链接抓取：{'开（最多 ' + str(FETCH_MAX_PAGES) + ' 个/条，正文 ' + str(FETCH_MAX_CHARS) + ' 字）' if FETCH_PAGES else '关'}\n"
        f"接口：{ANTHROPIC_URL}（anthropic 协议）"
    )


def _like(word: str) -> str:
    """Escape LIKE wildcards so a literal % or _ typed by the user still works."""
    clean = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{clean}%"


def _snippet(content: str, width: int = 80) -> str:
    return " ".join(content.split())[:width]


@command("/recall")
async def cmd_recall(uid, key, arg):
    word = arg.strip()
    if not word:
        return "用法：/recall 关键词  或  /recall all 关键词（跨会话）"
    everywhere = word == "all" or word.startswith("all ")
    if everywhere:
        word = word[3:].strip()
        if not word:
            return "用法：/recall all 关键词"
    sql = (
        "SELECT conv, role, content, ts FROM messages"
        " WHERE content LIKE ? ESCAPE '\\'"
    )
    args = [_like(word)]
    if not everywhere:
        sql += " AND conv = ?"
        args.append(key)
    sql += " ORDER BY id DESC LIMIT 10"
    rows = db().execute(sql, args).fetchall()
    if not rows:
        return f"没找到含「{word}」的记录。"
    head = f"命中 {len(rows)} 条"
    if everywhere:
        head += f"（跨 {len({r[0] for r in rows})} 个会话）"
    out = [head + f"，关键词「{word}」："]
    for conv, role, content, ts in rows:
        when = datetime.fromtimestamp(ts, TZ).strftime("%m-%d %H:%M")
        who = "我" if role == "user" else "助手"
        out.append(f"[{when}] {who}：{_snippet(content)}")
    return "\n".join(out)


@command("/export")
async def cmd_export(uid, key, arg):
    """Dump this conversation to a file and hand back a scp command."""
    rows = db().execute(
        "SELECT role, content, ts FROM messages WHERE conv = ? ORDER BY id", (key,)
    ).fetchall()
    if not rows:
        return "这个会话还没有任何记录。"
    os.makedirs(EXPORT_DIR, exist_ok=True)
    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    prefix = re.sub(r"[^0-9A-Za-z_-]", "_", key)[:48] or "chat"
    path = os.path.join(EXPORT_DIR, f"{prefix}_{stamp}.txt")
    lines = []
    for role, content, ts in rows:
        when = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S")
        who = "我" if role == "user" else "助手"
        lines.append(f"[{when}] {who}: {content}")
    body = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    # Keep only the newest five dumps *per conversation*, so exporting one
    # chat cannot delete another chat's dump.
    dumps = sorted(
        (f for f in os.listdir(EXPORT_DIR) if f.startswith(prefix + "_") and f.endswith(".txt")),
        key=lambda f: os.path.getmtime(os.path.join(EXPORT_DIR, f)),
        reverse=True,
    )
    for stale in dumps[5:]:
        try:
            os.remove(os.path.join(EXPORT_DIR, stale))
        except OSError:
            pass
    size_kb = os.path.getsize(path) / 1024
    reply = (
        f"已导出 {len(rows)} 条 / {size_kb:.1f} KB\n"
        f"文件：{path}\n"
        f"下载：scp {SCP_PREFIX}:{path} ."
    )
    if len(reply) + len(body) + 2 <= MAX_CHARS:
        reply += "\n\n" + body
    else:
        reply += f"\n\n（内容 {len(body)} 字符，超过单条上限，只给路径）"
    return reply


@command("/split")
async def cmd_split(uid, key, arg):
    """Send a deterministic multi-bubble reply, to test the transport."""
    want = int(arg) if arg.strip().isdigit() else 2
    n = max(1, min(want, MAX_PARTS))
    return [f"分条测试 {i}/{n}：这是第 {i} 条" for i in range(1, n + 1)]


@command("/reset")
async def cmd_reset(uid, key, arg):
    """Start a fresh window; the archive survives so /recall still works."""
    soft_forget(key)
    return "上下文已清空（存档保留，可用 /recall 回捞），我们重新开始。"


@command("/wipe")
async def cmd_wipe(uid, key, arg):
    """Permanently delete this conversation; needs an explicit confirmation."""
    if arg.strip().lower() not in {"yes", "确认"}:
        n = db().execute(
            "SELECT COUNT(*) FROM messages WHERE conv = ?", (key,)
        ).fetchone()[0]
        return (
            f"这会永久删除本会话全部 {n} 条记录（含存档），无法恢复。\n"
            f"确认执行：/wipe yes"
        )
    forget(key)
    return "已永久删除本会话的全部记录。"


async def handle_command(text: str, uid: str, key: str) -> str | None:
    """Return a reply for a slash command, or None when it is not a command."""
    name = text.split()[0].lower()
    fn = COMMANDS.get(name)
    if fn is None:
        return None  # not a command: let the model answer it as chat
    if name != "/id" and uid not in OWNER_IDS:
        log.info("command %s denied for %s", name, uid)
        return f"{name} 只有主人能用。"
    return await fn(uid, key, text[len(name):].strip())


def _is_id_probe(text: str | None) -> bool:
    """`/id` must reach the bot before BOT_OWNER_IDS is filled in, or nobody can bootstrap."""
    return (text or "").strip().lower().startswith("/id")


async def answer(key: str, text: str, images: list | None = None) -> list:
    """Build the prompt from memory, call the model, then store the exchange."""
    purge_expired()
    messages = build_messages(key, text, images)
    # Archive the question before the call: a failed request must not leave a
    # hole in the transcript.
    remember(key, "user", text + (" [图片]" if images else ""))
    try:
        text, stop, usage, searches = await ask_deepseek(messages)
    except ModelError as exc:
        log.warning("deepseek failed: %s", exc)
        return ["模型暂时没响应，稍后再试。"]
    if stop == "max_tokens":
        log.info("reply hit the token ceiling: %s", usage)
    parts = split_reply(text) or ["（脑子空了一下，你再说一次？）"]
    # Store exactly what the user saw, so later turns cannot quote text that
    # was never sent.
    remember(key, "assistant", " ".join(parts))
    log.info(
        "usage: in=%s out=%s search=%d parts=%d",
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        searches,
        len(parts),
    )
    return parts


# --- QQ inline markup ----------------------------------------------------
# Group/C2C content arrives with inline tags such as
# <faceType=6,faceId="0",ext="eyJ0ZXh0Ijoi...">, which would otherwise ship a
# base64 blob straight into the prompt. The tag body is unbounded on purpose:
# a capped match would silently let long base64 payloads through. Requiring
# "=" keeps ordinary HTML/XML that users paste while asking code questions.
_QQ_TAG = re.compile(
    r"<@!?\d+>|<(?:faceType|faceId|face|emoji|attachment|qqbot|img)\s*=[^<>]*>",
    re.I,
)
_FACE_TAG = re.compile(r"<(?:faceType|faceId|face|emoji)\s*=", re.I)
_FACE_RUN = re.compile(r"(?:\[表情\][ \t]*){2,}")
FACE_MARK = "[表情]"


def _tag_repl(match) -> str:
    """A face becomes visible to the model, every other tag is dropped."""
    return FACE_MARK if _FACE_TAG.match(match.group(0)) else " "


def clean_incoming(text: str) -> str:
    """Strip QQ inline tags, keeping a marker where the user sent a face."""
    cleaned, hits = _QQ_TAG.subn(_tag_repl, text or "")
    if hits:
        # Only touch spacing when a tag was actually removed, so pasted code
        # keeps its indentation.
        cleaned = re.sub(r"[ \t\u00a0]{2,}", " ", cleaned)
        cleaned = _FACE_RUN.sub(FACE_MARK + " ", cleaned)
    return cleaned.strip()


# --- duplicate suppression ----------------------------------------------
# A resumed gateway session can redeliver the same event; every redelivery
# costs a model call and a duplicated history row.
_SEEN: collections.OrderedDict = collections.OrderedDict()
_SEEN_MAX = 512


def is_duplicate(msg_id: str | None) -> bool:
    if not msg_id:
        return False
    if msg_id in _SEEN:
        return True
    _SEEN[msg_id] = None
    while len(_SEEN) > _SEEN_MAX:
        _SEEN.popitem(last=False)
    return False


# --- per-conversation serialisation -------------------------------------
# botpy dispatches every event as its own task, so two quick messages from one
# person would otherwise interleave at the model call: replies arrive out of
# order and the second prompt misses the first exchange.
_LOCKS: dict[str, asyncio.Lock] = {}


def lock_for(key: str) -> asyncio.Lock:
    lock = _LOCKS.get(key)
    if lock is None:
        if len(_LOCKS) > 1000:  # prune idle conversations, never a live lock
            for stale in [k for k, v in _LOCKS.items() if not v.locked()]:
                _LOCKS.pop(stale, None)
        lock = _LOCKS[key] = asyncio.Lock()
    return lock


async def handle_message(message, uid: str, key: str) -> str | None:
    text = clean_incoming(message.content or "")
    quoted_texts, quoted_atts = quoted_context(getattr(message, "id", None))
    atts = list(getattr(message, "attachments", None) or []) + quoted_atts
    log.info(
        "in: key=%s len=%d quoted=%d atts=%s",
        key,
        len(text),
        len(quoted_texts),
        [
            (getattr(a, "content_type", None), getattr(a, "filename", None), getattr(a, "size", None))
            for a in atts
        ],
    )
    images = await collect_images(atts)
    if quoted_texts:
        prompt_text = "[被引用的消息] " + " ".join(quoted_texts)
        prompt_text = prompt_text + ("\n" + text if text else "")
    else:
        prompt_text = text
    if FETCH_PAGES:
        urls = extract_urls(prompt_text)
        if urls:
            blocks, link_images = [], []
            for url in urls:
                block, image_url = await fetch_link(url)
                if block:
                    blocks.append(block)
                if image_url:
                    link_images.append(image_url)
            if link_images:
                images = list(images) + link_images
            if blocks:
                prompt_text = "\n\n".join(blocks) + "\n\n" + prompt_text
            log.info(
                "link: %d url(s), %d text block(s), %d image(s)",
                len(urls), len(blocks), len(link_images),
            )
    if not prompt_text and not images:
        if has_voice(atts):
            return "语音我暂时听不了，发文字或者打字吧～"
        if atts:
            return "这个附件我没接住，发图或者打字？"
        return None
    log.info("msg: key=%s len=%d images=%d", key, len(prompt_text), len(images))

    if text.startswith("/"):
        handled = await handle_command(text, uid, key)
        if handled is not None:
            return handled
    if text in RESET_WORDS:
        soft_forget(key)
        return "上下文已清空（存档保留），我们重新开始。"
    return await answer(key, prompt_text, images)


class HubClient(botpy.Client):
    async def on_error(self, event_method, *args, **kwargs):
        log.exception("unhandled error in %s", event_method)

    async def _dispatch(self, message, uid: str | None, key: str | None) -> None:
        """Shared path: dedupe, serialise per conversation, send, never raise."""
        if is_duplicate(getattr(message, "id", None)):
            log.info("duplicate delivery ignored: %s", getattr(message, "id", None))
            return
        if uid is None or key is None:
            log.info("dropping message without a usable author id")
            return
        try:
            async with lock_for(key):
                reply = await handle_message(message, uid, key)
                if reply is None:
                    return
                parts = reply if isinstance(reply, list) else [reply]
                await self._send(message, parts)
        except Exception:  # noqa: BLE001 - a handler fault must not kill the loop
            log.exception("handler failed for %s", key)

    async def _send(self, message, parts: list) -> None:
        for seq, part in enumerate(parts, start=1):
            text = truncate(part)
            if not text:
                continue
            try:
                if isinstance(message, GroupMessage):
                    res = await message._api.post_group_message(
                        group_openid=message.group_openid,
                        msg_type=0,
                        msg_id=message.id,
                        msg_seq=seq,
                        content=text,
                    )
                else:
                    res = await message._api.post_c2c_message(
                        openid=message.author.user_openid,
                        msg_type=0,
                        msg_id=message.id,
                        msg_seq=seq,
                        content=text,
                    )
                # botpy only raises on HTTP status, so a 200 carrying a business
                # error would otherwise disappear silently.
                if isinstance(res, dict) and res.get("id"):
                    log.info("sent seq=%d/%d len=%d id=%s", seq, len(parts), len(text), res.get("id"))
                else:
                    log.warning("send seq=%d/%d returned no id: %s", seq, len(parts), str(res)[:300])
            except Exception:  # noqa: BLE001 - platform rejections are not fatal
                log.exception("send failed (seq=%d, len=%d)", seq, len(text))
            if seq < len(parts):
                # Give the client time to order the bubbles the way they were written.
                await asyncio.sleep(0.4)

    async def on_group_at_message_create(self, message: GroupMessage):
        member = getattr(message.author, "member_openid", None)
        if not member:
            log.info("group message without member_openid, dropping")
            return
        if PRIVATE_MODE and member not in OWNER_IDS and not _is_id_probe(message.content):
            log.info("blocked non-owner group member %s", member)
            return
        await self._dispatch(message, member, f"g:{message.group_openid}:{member}")

    async def on_c2c_message_create(self, message: C2CMessage):
        uid = message.author.user_openid
        if PRIVATE_MODE and uid not in OWNER_IDS and not _is_id_probe(message.content):
            log.info("blocked non-owner c2c user %s", uid)
            return
        await self._dispatch(message, uid, f"c:{uid}")


def startup_report() -> None:
    """Print the effective configuration once, at boot (on_ready is never called)."""
    log.info(
        "ready: llm %s | model %s | search %s | db %s | owners %d | tz %s | max_tokens %d",
        ANTHROPIC_URL,
        MODEL,
        f"on(max {SEARCH_MAX_USES})" if WEB_SEARCH else "off",
        DB_PATH,
        len(OWNER_IDS),
        TZ.key,
        MAX_OUTPUT_TOKENS,
    )


def install_payload_capture() -> None:
    """Keep the raw gateway payload so quoted-message content is not lost.

    botpy 1.2.1 builds message objects from content/attachments/
    message_reference only; a quote-reply carries the quoted text and image
    inside "msg_elements", which the SDK ignores.
    """
    from botpy.message import BaseMessage

    orig_init = BaseMessage.__init__
    debug = os.getenv("BOT_DEBUG_PAYLOAD", "0").lower() not in ("0", "false", "no", "")

    def _init(self, api, event_id, data):
        try:
            capture_raw(data.get("id"), data)
            if debug:
                log.info("payload: %s", json.dumps(data, ensure_ascii=False)[:2000])
        except Exception:  # noqa: BLE001 - never break event parsing
            log.exception("payload capture failed")
        orig_init(self, api, event_id, data)

    BaseMessage.__init__ = _init
    if debug:
        log.info("payload debug enabled")


def main() -> None:
    db()
    startup_report()
    install_payload_capture()
    intents = botpy.Intents(public_messages=True)
    HubClient(intents=intents).run(
        appid=os.environ["QQ_APPID"],
        secret=os.environ["QQ_APP_SECRET"],
    )


if __name__ == "__main__":
    main()
