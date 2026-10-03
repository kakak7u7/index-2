import asyncio
import os
import re
import sqlite3
import logging
import hashlib
from datetime import datetime, timezone
from difflib import SequenceMatcher

from telethon import TelegramClient, events, Button
from telethon.errors import RPCError
from telethon.tl.types import Channel

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('class-indexer')

API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
TARGET_CHANNEL = (os.getenv('TARGET_CHANNEL') or os.getenv('CHANNEL') or '').strip()
OWNER_USER_ID = int(os.getenv('OWNER_USER_ID', '0') or 0)
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().lstrip('-').isdigit()}
if OWNER_USER_ID:
    ADMIN_IDS.add(OWNER_USER_ID)
DB_PATH = os.getenv('DB_PATH', 'data/classes.db')

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError('Missing API_ID/API_HASH/BOT_TOKEN environment variables.')

os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

with db() as c:
    c.executescript('''
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        title TEXT,
        topic TEXT NOT NULL,
        topic_key TEXT NOT NULL,
        part INTEGER,
        date_text TEXT,
        telegram_link TEXT NOT NULL,
        kind TEXT,
        raw_text TEXT,
        UNIQUE(channel_id, message_id)
    );
    CREATE INDEX IF NOT EXISTS idx_topic_key ON messages(topic_key);
    CREATE INDEX IF NOT EXISTS idx_title ON messages(title);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    ''')

PART_RE = re.compile(r'(?i)\bpart\s*[-_#]?\s*(\d+)\b|\bभाग\s*[-_]?\s*(\d+)\b')
DATE_PATTERNS = [
    re.compile(r'\b\d{1,2}\s*[-/]\s*(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b', re.I),
    re.compile(r'\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s*[-/]\s*\d{1,2}\b', re.I),
    re.compile(r'\b\d{1,2}\s*[-/]\s*\d{1,2}(?:\s*[-/]\s*\d{2,4})?\b'),
    re.compile(r'\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\b', re.I),
]

MONTHS = 'january february march april may june july august september october november december'.split()
MONTH_ABBR = {m[:3]: m for m in MONTHS}

def clean_text(text: str) -> str:
    text = text or ''
    text = re.sub(r'[\u200b\u200c\u200d\ufeff]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def parse_part(text):
    m = PART_RE.search(text or '')
    if not m:
        return None
    return int(m.group(1) or m.group(2))

def parse_date_text(text):
    for pat in DATE_PATTERNS:
        m = pat.search(text or '')
        if m:
            return m.group(0).strip()
    return ''

def extract_title(text):
    text = clean_text(text)
    if not text:
        return ''
    # Prefer the explicit TITLE line from the user's existing format.
    m = re.search(r'(?:title|tɪᴛʟᴇ)\s*[:：]\s*(.+)', text, re.I)
    if m:
        return m.group(1).strip()
    return text.split('\n', 1)[0].strip()

def normalize_topic(title):
    s = clean_text(title)
    s = re.sub(r'(?i)\bpart\s*[-_#]?\s*\d+\b', ' ', s)
    s = re.sub(r'\bभाग\s*[-_]?\s*\d+\b', ' ', s)
    for pat in DATE_PATTERNS:
        s = pat.sub(' ', s)
    s = re.sub(r'\b\d{1,2}\s*[-/]\s*\d{1,2}\b', ' ', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' -–—:|')
    # remove common upload/metadata fragments if they accidentally appear in the title
    s = re.sub(r'(?i)^(?:title|tɪᴛʟᴇ)\s*[:：]\s*', '', s)
    return s.strip() or 'Untitled'

def topic_key(topic):
    s = topic.casefold()
    s = re.sub(r'[^\w\u0900-\u097F]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def topic_token(key):
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]

def key_from_token(token):
    # SQLite has no portable SHA-256 function; resolve the short token in Python.
    with db() as c:
        keys = [r['topic_key'] for r in c.execute('SELECT DISTINCT topic_key FROM messages').fetchall()]
    for k in keys:
        if topic_token(k) == token:
            return k
    return None

def kind_of_message(msg):
    if getattr(msg, 'video', None): return '🎬 Video'
    if getattr(msg, 'document', None): return '📄 PDF/File'
    if getattr(msg, 'photo', None): return '🖼 Image'
    return '📝 Text'

def build_link(entity, message_id):
    username = getattr(entity, 'username', None)
    if username:
        return f'https://t.me/{username}/{message_id}'
    cid = getattr(entity, 'id', None)
    if cid is None:
        raise ValueError('Channel has no numeric id')
    return f'https://t.me/c/{str(cid)[4:]}/{message_id}' if str(cid).startswith('-100') else f'https://t.me/c/{abs(cid)}/{message_id}'

def is_probable_class(msg):
    text = clean_text(getattr(msg, 'message', '') or '')
    has_media = bool(getattr(msg, 'video', None) or getattr(msg, 'document', None) or getattr(msg, 'photo', None))
    return bool(text or has_media)

async def get_entity(client):
    target = TARGET_CHANNEL
    if not target:
        row = None
        with db() as c:
            row = c.execute("SELECT value FROM meta WHERE key='target_channel'").fetchone()
        target = row['value'] if row else ''
    if not target:
        raise RuntimeError('TARGET_CHANNEL is not set. Use /setchannel @username_or_-100id as owner.')
    return await client.get_entity(target)

async def upsert_message(entity, msg):
    text = getattr(msg, 'message', '') or ''
    title = extract_title(text)
    if not title and not (getattr(msg, 'video', None) or getattr(msg, 'document', None) or getattr(msg, 'photo', None)):
        return False
    part = parse_part(title)
    date_text = parse_date_text(title)
    topic = normalize_topic(title)
    key = topic_key(topic)
    link = build_link(entity, msg.id)
    with db() as c:
        c.execute('''INSERT INTO messages(channel_id,message_id,title,topic,topic_key,part,date_text,telegram_link,kind,raw_text)
                     VALUES(?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(channel_id,message_id) DO UPDATE SET
                     title=excluded.title,topic=excluded.topic,topic_key=excluded.topic_key,part=excluded.part,
                     date_text=excluded.date_text,telegram_link=excluded.telegram_link,kind=excluded.kind,raw_text=excluded.raw_text''',
                  (entity.id, msg.id, title, topic, key, part, date_text, link, kind_of_message(msg), text[:4000]))
    return True

async def scan_channel(client, event=None):
    entity = await get_entity(client)
    count = 0
    async for msg in client.iter_messages(entity, limit=None):
        if await upsert_message(entity, msg):
            count += 1
        if event and count % 100 == 0 and count:
            try:
                await event.edit(f'🔄 Scanning…\n\nIndexed/updated: **{count}**')
            except Exception:
                pass
    with db() as c:
        c.execute("INSERT INTO meta(key,value) VALUES('last_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (datetime.now(timezone.utc).isoformat(),))
    return entity, count

def stats():
    with db() as c:
        topics = c.execute('SELECT COUNT(DISTINCT topic_key) n FROM messages').fetchone()['n']
        classes = c.execute('SELECT COUNT(*) n FROM messages').fetchone()['n']
        videos = c.execute("SELECT COUNT(*) n FROM messages WHERE kind='🎬 Video'").fetchone()['n']
        files = c.execute("SELECT COUNT(*) n FROM messages WHERE kind='📄 PDF/File'").fetchone()['n']
    return topics, classes, videos, files

def topic_rows(page=0, per_page=10):
    with db() as c:
        rows = c.execute('''SELECT topic_key, MIN(topic) topic, COUNT(*) cnt FROM messages
                            GROUP BY topic_key ORDER BY MIN(id) ASC LIMIT ? OFFSET ?''', (per_page, page*per_page)).fetchall()
        total = c.execute('SELECT COUNT(DISTINCT topic_key) n FROM messages').fetchone()['n']
    return rows, total

def topic_items(key):
    with db() as c:
        return c.execute('''SELECT title,part,date_text,telegram_link,kind FROM messages
                            WHERE topic_key=? ORDER BY CASE WHEN part IS NULL THEN 999999 ELSE part END, id''', (key,)).fetchall()

def search_rows(q):
    q = f'%{q.casefold()}%'
    with db() as c:
        return c.execute('''SELECT topic_key, MIN(topic) topic, COUNT(*) cnt FROM messages
                            WHERE lower(topic) LIKE ? OR lower(title) LIKE ? OR lower(raw_text) LIKE ?
                            GROUP BY topic_key ORDER BY MIN(id) LIMIT 30''', (q,q,q)).fetchall()

client = TelegramClient(os.getenv('SESSION_NAME', 'bot'), API_ID, API_HASH)

async def owner_only(event):
    if event.sender_id in ADMIN_IDS:
        return True
    if not ADMIN_IDS:
        await event.respond('⚠️ OWNER_USER_ID is not configured. Add your Telegram numeric user ID to the environment.')
    else:
        await event.respond('⛔ Owner only.')
    return False

@client.on(events.NewMessage(pattern=r'^/start$'))
async def start(event):
    t,c,v,f = stats()
    await event.respond(
        f'📚 **MY CLASS LIBRARY**\n\nTopics: **{t}**\nClasses: **{c}**\n🎬 Videos: **{v}**\n📄 Files: **{f}**\n\nChoose an option:',
        buttons=[[Button.inline('🔎 Search Class','search'), Button.inline('📚 All Topics','topics:0')],
                 [Button.inline('📊 Statistics','stats'), Button.inline('🔄 Update Index','scan')],
                 [Button.inline('🆔 My Telegram ID','myid')]])

@client.on(events.NewMessage(pattern=r'^/id$'))
async def myid(event):
    await event.respond(f'🆔 Your Telegram user ID: `{event.sender_id}`')

@client.on(events.NewMessage(pattern=r'^/setchannel(?:\s+(.+))?$'))
async def setchannel(event):
    if not await owner_only(event): return
    target = event.pattern_match.group(1)
    if not target:
        await event.respond('Usage: `/setchannel @channelusername` or `/setchannel -1001234567890`')
        return
    try:
        ent = await client.get_entity(target.strip())
        with db() as c:
            c.execute("INSERT INTO meta(key,value) VALUES('target_channel',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (target.strip(),))
        await event.respond(f'✅ Target channel saved: **{getattr(ent,"title",target)}**\nNow run /scan')
    except Exception as e:
        await event.respond(f'❌ Could not resolve channel: `{type(e).__name__}: {e}`')

@client.on(events.NewMessage(pattern=r'^/scan$'))
async def scan_cmd(event):
    if not await owner_only(event): return
    m = await event.respond('🔄 Starting existing-channel scan…')
    try:
        ent, n = await scan_channel(client, m)
        t,c,v,f = stats()
        await m.edit(f'✅ **SCAN COMPLETE**\n\nChannel: **{getattr(ent,"title", "Unknown")}**\nIndexed/updated: **{n}**\nTopics: **{t}**\nClasses: **{c}**\n🎬 Videos: **{v}**\n📄 Files: **{f}**')
    except Exception as e:
        log.exception('scan failed')
        await m.edit(f'❌ Scan failed: `{type(e).__name__}: {e}`')

@client.on(events.CallbackQuery(data=b'stats'))
async def cb_stats(event):
    t,c,v,f = stats()
    await event.edit(f'📊 **STATISTICS**\n\n📚 Topics: **{t}**\n🎬 Videos: **{v}**\n📄 Files: **{f}**\n📝 Total indexed posts: **{c}**', buttons=[[Button.inline('◀️ Back','back')]])

@client.on(events.CallbackQuery(data=re.compile(rb'topics:\d+')))
async def cb_topics(event):
    page = int(event.data.decode().split(':')[1])
    rows,total = topic_rows(page)
    text = f'📚 **ALL TOPICS** — Page {page+1}/{max(1,(total+9)//10)}\n\n'
    buttons=[]
    for r in rows:
        text += f'📂 **{r["topic"]}** — {r["cnt"]} classes\n'
        buttons.append([Button.inline(r['topic'][:45], f'topic:{topic_token(r["topic_key"])}'.encode())])
    nav=[]
    if page>0: nav.append(Button.inline('◀️ Previous', f'topics:{page-1}'))
    if (page+1)*10<total: nav.append(Button.inline('Next ▶️', f'topics:{page+1}'))
    if nav: buttons.append(nav)
    buttons.append([Button.inline('🏠 Home','back')])
    await event.edit(text, buttons=buttons)

@client.on(events.CallbackQuery(data=re.compile(rb'topic:[0-9a-f]{16}')))
async def cb_topic(event):
    token = event.data.decode().split(':',1)[1]
    key = key_from_token(token)
    if not key:
        await event.answer('Topic not found', alert=True); return
    rows = topic_items(key)
    if not rows:
        await event.answer('Topic not found', alert=True); return
    topic = key
    with db() as c:
        rr = c.execute('SELECT topic FROM messages WHERE topic_key=? ORDER BY id LIMIT 1', (key,)).fetchone()
        if rr: topic = rr['topic']
    text = f'📚 **{topic}**\n\n'
    buttons=[]
    for i,r in enumerate(rows,1):
        label = r['title'] or f'Part {i}'
        if len(label)>55: label=label[:52]+'…'
        text += f'{i}. {r["kind"]} **{label}**'
        if r['date_text']: text += f' — {r["date_text"]}'
        text += '\n'
        buttons.append([Button.url(f'🔗 Open {"Part "+str(r["part"]) if r["part"] else "Class "+str(i)}', r['telegram_link'])])
    buttons.append([Button.inline('🔗 Open All',('openall:'+topic_token(key)).encode()), Button.inline('◀️ Topics','topics:0')])
    await event.edit(text, buttons=buttons)

@client.on(events.CallbackQuery(data=re.compile(rb'openall:[0-9a-f]{16}')))
async def cb_openall(event):
    token = event.data.decode().split(':',1)[1]
    key = key_from_token(token)
    if not key:
        await event.answer('Topic not found', alert=True); return
    rows = topic_items(key)
    links = []
    for i, r in enumerate(rows, 1):
        label = r['title'] or f'Class {i}'
        links.append(f'{i}. {label}\n{r["telegram_link"]}')
    text = '\n\n'.join(links)
    if len(text) > 3800:
        text = text[:3800] + '\n\n…More links are available from the topic page buttons.'
    await event.respond('🔗 **All class links**\n\n' + text)
    await event.answer('Links sent')

@client.on(events.CallbackQuery(data=b'back'))
async def cb_back(event):
    await event.edit('📚 **MY CLASS LIBRARY**\n\nChoose an option:', buttons=[[Button.inline('🔎 Search Class','search'), Button.inline('📚 All Topics','topics:0')],[Button.inline('📊 Statistics','stats')]])

@client.on(events.CallbackQuery(data=b'myid'))
async def cb_myid(event):
    await event.answer(f'Your ID: {event.sender_id}', alert=True)

@client.on(events.CallbackQuery(data=b'search'))
async def cb_search(event):
    await event.answer('Send /search keyword', alert=True)

@client.on(events.NewMessage(pattern=r'^/search\s+(.+)$'))
async def search_cmd(event):
    q = event.pattern_match.group(1).strip()
    rows = search_rows(q)
    if not rows:
        await event.respond(f'🔎 No results for **{q}**')
        return
    text = f'🔎 **SEARCH: {q}**\n\n'
    buttons=[]
    for r in rows:
        text += f'📂 **{r["topic"]}** — {r["cnt"]} classes\n'
        buttons.append([Button.inline(r['topic'][:50], f'topic:{topic_token(r["topic_key"])}'.encode())])
    await event.respond(text, buttons=buttons)

@client.on(events.CallbackQuery(data=re.compile(b'scan$')))
async def cb_scan(event):
    if not await owner_only(event): return
    await event.answer('Use /scan to start the scan.', alert=True)

@client.on(events.NewMessage(incoming=True))
async def help_text(event):
    if not event.raw_text.startswith('/'):
        return

async def main():
    log.info('Starting Telegram bot without interactive input...')
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    log.info('Logged in as @%s (%s)', getattr(me,'username',None), me.id)
    if TARGET_CHANNEL:
        try:
            ent = await client.get_entity(TARGET_CHANNEL)
            log.info('Configured target: %s (%s)', getattr(ent,'title',TARGET_CHANNEL), ent.id)
        except Exception as e:
            log.warning('Target channel could not be resolved yet: %s', e)
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
