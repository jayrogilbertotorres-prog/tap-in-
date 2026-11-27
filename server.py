"""TapIn account-based gameplay coaching prototype. Run behind HTTPS in production."""
import base64, json, os, re, sqlite3, tempfile, threading
import hashlib, hmac, secrets, time, math
from urllib.parse import urlparse
from contextlib import closing
from fastapi import Request, Response, Depends
from pydantic import BaseModel
from pathlib import Path
import cv2
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from openai import OpenAI

BASE = Path(__file__).resolve().parent
DATA = Path(os.getenv('TAPIN_DATA_DIR', str(BASE / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
CLIPS = DATA / 'clips'
CLIPS.mkdir(exist_ok=True)
DB = Path(os.getenv('TAPIN_DB', str(DATA / 'profiles.sqlite3')))
COOKIE = 'tapin_session'
SESSION_AGE = 30 * 24 * 3600
MAX_BYTES = 250 * 1024 * 1024
lock = threading.Lock()
analysis_slots = threading.BoundedSemaphore(2)
DAILY_REVIEWS = max(1, int(os.getenv('TAPIN_DAILY_REVIEWS', '5')))
GLOBAL_DAILY_REVIEWS = max(1, int(os.getenv('TAPIN_GLOBAL_DAILY_REVIEWS', '30')))
MAX_USER_CLIPS = 10
MAX_TOTAL_CLIPS = max(1, int(os.getenv('TAPIN_MAX_TOTAL_CLIPS', '40')))
app = FastAPI(title='TapIn AI Coaching')

def connect():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript("""
    CREATE TABLE IF NOT EXISTS visits (day TEXT NOT NULL, visitor TEXT NOT NULL, views INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(day,visitor));
    CREATE TABLE IF NOT EXISTS chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, username TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL, hidden INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS chat_bans (user_id TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS chat_reports (message_id INTEGER NOT NULL, reporter TEXT NOT NULL, PRIMARY KEY(message_id,reporter));
    CREATE TABLE IF NOT EXISTS analysis_attempts (user_id TEXT NOT NULL, created REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS attempts_created ON analysis_attempts(created);
    CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, salt TEXT NOT NULL, password TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS clips (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), filename TEXT NOT NULL, stored TEXT NOT NULL, created REAL NOT NULL, review TEXT);
    """)
    return con

@app.middleware('http')
async def protect_writes(request: Request, call_next):
    # Custom header cannot be sent by cross-origin HTML forms. No CORS is enabled.
    if request.method in ('POST', 'DELETE', 'PUT', 'PATCH') and request.headers.get('x-tapin-request') != '1':
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': 'Invalid request origin'}, status_code=403)
    if request.method == 'POST':
        from fastapi.responses import JSONResponse
        limit = MAX_BYTES + 1024 * 1024 if request.url.path == '/api/clips' else 64 * 1024
        try: length = int(request.headers.get('content-length', '-1'))
        except ValueError: return JSONResponse({'detail': 'Invalid content length'}, status_code=400)
        if length < 0: return JSONResponse({'detail': 'Content length required'}, status_code=411)
        if length > limit: return JSONResponse({'detail': 'Request too large'}, status_code=413)
    response = await call_next(request)
    if request.url.path.startswith('/api/'): response.headers['Cache-Control'] = 'no-store'
    return response

def current_user(request: Request):
    token = request.cookies.get(COOKIE, '')
    with connect() as db:
        row = db.execute('SELECT users.id, username FROM sessions JOIN users ON users.id=sessions.user_id WHERE token=? AND expires>?',
                         (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    if not row: raise HTTPException(401, 'Sign in to upload clips and view your profile.')
    return dict(row)

class Credentials(BaseModel):
    username: str
    password: str

# Bound memory and repeated password hashing attempts per IP.
auth_attempts = {}
def check_attempts(request):
    ip = request.client.host if request.client else 'unknown'
    now = time.time()
    with lock:
        for key in list(auth_attempts):
            auth_attempts[key] = [t for t in auth_attempts[key] if t > now - 900]
            if not auth_attempts[key]: del auth_attempts[key]
        attempts = auth_attempts.setdefault(ip, [])
        if len(attempts) >= 20: raise HTTPException(429, 'Too many attempts. Try again in 15 minutes.')
        attempts.append(now)

def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()

def start_session(response, uid):
    token = secrets.token_urlsafe(32)
    with connect() as db:
        db.execute('DELETE FROM sessions WHERE expires<=?', (time.time(),))
        db.execute('INSERT INTO sessions VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), uid, time.time()+SESSION_AGE))
    response.set_cookie(COOKIE, token, httponly=True, secure=os.getenv('TAPIN_COOKIE_SECURE', '1') != '0', samesite='strict', max_age=SESSION_AGE)

@app.post('/api/signup')
def signup(body: Credentials, request: Request, response: Response):
    check_attempts(request)
    username = body.username.strip().lower()
    if not re.fullmatch(r'[a-z0-9_]{3,24}', username): raise HTTPException(422, 'Username must be 3–24 letters, numbers, or underscores.')
    if not 12 <= len(body.password) <= 128: raise HTTPException(422, 'Use a password between 12 and 128 characters.')
    salt = secrets.token_hex(16)
    hashed = password_hash(body.password, salt)
    uid = secrets.token_hex(16)
    try:
        with connect() as db: db.execute('INSERT INTO users VALUES (?,?,?,?)', (uid, username, salt, hashed))
    except sqlite3.IntegrityError: raise HTTPException(409, 'That username is already taken.')
    start_session(response, uid)
    return {'username': username}

@app.post('/api/login')
def login(body: Credentials, request: Request, response: Response):
    check_attempts(request)
    if len(body.password) > 128: raise HTTPException(401, 'Incorrect username or password.')
    with connect() as db: user = db.execute('SELECT * FROM users WHERE username=?', (body.username.strip().lower(),)).fetchone()
    salt = user['salt'] if user else '00'*16
    hashed = password_hash(body.password, salt)
    if not user or not hmac.compare_digest(hashed, user['password']): raise HTTPException(401, 'Incorrect username or password.')
    start_session(response, user['id'])
    return {'username': user['username']}

@app.post('/api/logout')
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE, '')
    with connect() as db: db.execute('DELETE FROM sessions WHERE token=?', (hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie(COOKIE, httponly=True, secure=os.getenv('TAPIN_COOKIE_SECURE', '1') != '0', samesite='strict')
    return {'ok': True}

@app.get('/')
def home(): return FileResponse(BASE/'index.html', media_type='text/html')

@app.get('/api/status')
def status(): return {'ready': bool(os.getenv('OPENAI_API_KEY'))}

@app.get('/api/profile')
def profile(user=Depends(current_user)):
    with connect() as db:
        rows = db.execute('SELECT id, filename, created, review FROM clips WHERE user_id=? ORDER BY created DESC', (user['id'],)).fetchall()
    return {'username': user['username'], 'account_id': user['id'], 'is_owner': bool(os.getenv('TAPIN_OWNER_USER_ID')) and user['id']==os.getenv('TAPIN_OWNER_USER_ID'), 'clips': [dict(r) | {'review': json.loads(r['review']) if r['review'] else None} for r in rows]}

@app.post('/api/clips')
async def upload_clip(video: UploadFile=File(...), user=Depends(current_user)):
    with connect() as db:
        if db.execute('SELECT COUNT(*) FROM clips WHERE user_id=?', (user['id'],)).fetchone()[0] >= MAX_USER_CLIPS:
            await video.close()
            raise HTTPException(429, 'Clip storage limit reached. Delete an old clip first.')
    suffix = Path(video.filename or '').suffix.lower()
    if suffix not in ('.mp4', '.webm', '.mov', '.m4v'): raise HTTPException(415, 'Please use an MP4, WebM, or MOV video.')
    cid = secrets.token_hex(16)
    stored = cid + suffix
    path = CLIPS / stored
    try:
        size = 0
        with path.open('wb') as target:
            while chunk := await video.read(1024*1024):
                size += len(chunk)
                if size > MAX_BYTES: raise HTTPException(413, 'Clip exceeds 250 MB limit.')
                target.write(chunk)
        if size == 0: raise HTTPException(422, 'Clip is empty.')
        cap = cv2.VideoCapture(str(path))
        try:
            ok, frame = cap.read()
            if not ok or frame is None: raise HTTPException(422, 'Video cannot be decoded.')
        finally: cap.release()
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM clips WHERE user_id=?', (user['id'],)).fetchone()[0] >= MAX_USER_CLIPS:
                raise HTTPException(429, 'Clip storage limit reached. Delete an old clip first.')
            if db.execute('SELECT COUNT(*) FROM clips').fetchone()[0] >= MAX_TOTAL_CLIPS:
                raise HTTPException(429, 'Server clip storage capacity reached.')
            db.execute('INSERT INTO clips VALUES (?,?,?,?,?,NULL)', (cid, user['id'], Path(video.filename).name[:200], stored, time.time()))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally: await video.close()
    return {'id': cid}

def owned_clip(cid, user):
    with connect() as db: row = db.execute('SELECT * FROM clips WHERE id=? AND user_id=?', (cid, user['id'])).fetchone()
    if not row: raise HTTPException(404, 'Clip not found.')
    return row

@app.get('/api/clips/{cid}/video')
def clip_video(cid: str, user=Depends(current_user)):
    row = owned_clip(cid, user)
    path = CLIPS / row['stored']
    if not path.is_file(): raise HTTPException(404, 'Clip file unavailable.')
    return FileResponse(path, headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})

@app.delete('/api/clips/{cid}')
def delete_clip(cid: str, user=Depends(current_user)):
    row = owned_clip(cid, user)
    with connect() as db: db.execute('DELETE FROM clips WHERE id=? AND user_id=?', (cid, user['id']))
    (CLIPS / row['stored']).unlink(missing_ok=True)
    return {'ok': True}

def frames_from_video(filename):
    capture=cv2.VideoCapture(filename)
    if not capture.isOpened():
        raise HTTPException(422, 'Video cannot be decoded. Please upload a supported MP4 or WebM clip.')
    fps=capture.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(fps) or fps <= 0: fps=30
    count=capture.get(cv2.CAP_PROP_FRAME_COUNT)
    seconds=min(120.0,max(0.0,(count-1)/fps) if math.isfinite(count) and count>0 else 120.0)
    # Analyze up to 12 evenly spread frames in the first two minutes.
    samples=min(12,max(2,int(seconds/5)+1))
    frames=[]
    for i in range(samples):
        t=seconds*i/max(1,samples-1)
        capture.set(cv2.CAP_PROP_POS_MSEC,t*1000)
        ok, frame=capture.read()
        if not ok: continue
        h,w=frame.shape[:2]
        if max(h,w)>960: frame=cv2.resize(frame,(int(w*960/max(h,w)),int(h*960/max(h,w))))
        ok,jpg=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,75])
        if ok: frames.append((round(t,1),base64.b64encode(jpg.tobytes()).decode()))
    capture.release()
    if not frames: raise HTTPException(422, 'Could not extract frames from this video.')
    return frames

@app.post('/api/clips/{cid}/analyze')
def analyze(cid: str, focus: str=Form('Gameplay decisions'), user=Depends(current_user)):
    row = owned_clip(cid, user)
    if not os.getenv('OPENAI_API_KEY'): raise HTTPException(503, 'AI service not connected.')
    if not analysis_slots.acquire(blocking=False): raise HTTPException(429, 'Coach is busy. Try again shortly.')
    try:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cutoff = time.time() - 86400
            db.execute('DELETE FROM analysis_attempts WHERE created < ?', (cutoff,))
            if db.execute('SELECT COUNT(*) FROM analysis_attempts WHERE user_id=?', (user['id'],)).fetchone()[0] >= DAILY_REVIEWS or db.execute('SELECT COUNT(*) FROM analysis_attempts').fetchone()[0] >= GLOBAL_DAILY_REVIEWS:
                raise HTTPException(429, 'Free coaching daily capacity reached. Try again later.')
            db.execute('INSERT INTO analysis_attempts VALUES (?,?)', (user['id'], time.time()))
        return run_analysis(cid, focus, user, row)
    finally: analysis_slots.release()

def run_analysis(cid, focus, user, row):
    if not os.getenv('OPENAI_API_KEY'): raise HTTPException(503,'AI service not connected. Set OPENAI_API_KEY on the server.')
    frames=frames_from_video(str(CLIPS / row['stored']))
    content=[{'type':'input_text','text':(
      'You are TapIn, an FPS video-game coach. Analyze ONLY the visible frames. '
      'These are sparse screenshots, so do not invent unseen kills, audio, movements, or events. '
      'Give 3 to 5 observations, each with visible evidence, tactical consequence, and a concrete alternative action. Do not infer respawns, weapons, enemy positions, or duration without legible evidence. Finish with one practical drill. '
      'Include the sampled timestamps for claims and confidence/limitations. '
      'Provide a JSON object ONLY, with keys analysis (plain text) and markers '
      '(array of {seconds: number, note: string}). Each marker must refer to a '
      'visible frame. Do not claim exact game outcomes based on sparse frames. '
      'Player focus: '+focus[:400]+'; sampled frame times are listed next.'
    )}]
    for t,b64 in frames:
        content.append({'type':'input_text','text':f'Frame at {t:.1f} seconds'})
        content.append({'type':'input_image','image_url':'data:image/jpeg;base64,'+b64,'detail':'low'})
    try:
        response=OpenAI(timeout=65, max_retries=0).responses.create(model=os.getenv('TAPIN_MODEL','gpt-4.1-mini'),input=[{'role':'user','content':content}],max_output_tokens=1100,store=False)
    except Exception:
        raise HTTPException(502, 'AI provider request failed. Please retry later.')
    raw=(response.output_text or '').strip()
    raw=re.sub(r'^```(?:json)?\s*|\s*```$','',raw)
    try: result=json.loads(raw)
    except json.JSONDecodeError: result={'analysis':raw,'markers':[]}
    if not isinstance(result,dict) or not result.get('analysis'): raise HTTPException(502,'AI returned an empty review. Please retry.')
    markers=[]
    for m in (result.get('markers') if isinstance(result.get('markers'), list) else []):
        if not isinstance(m,dict): continue
        try: sec=float(m.get('seconds',0))
        except (TypeError,ValueError): continue
        if not math.isfinite(sec) or not any(abs(sec-t)<=0.11 for t,_ in frames): continue
        markers.append({'seconds':sec,'time':f'{int(sec//60)}:{int(sec%60):02d}','note':str(m.get('note',''))[:220]})
    result = {'analysis': str(result['analysis'])[:5500], 'markers': markers}
    with connect() as db:
        db.execute('UPDATE clips SET review=? WHERE id=? AND user_id=?', (json.dumps(result), cid, user['id']))
    return result

# Community features: no payments or native video transport are handled here.
def owner(user):
    configured = os.getenv('TAPIN_OWNER_USER_ID', '').strip()
    if not configured or not hmac.compare_digest(user['id'],configured):
        raise HTTPException(403, 'Owner access required.')
    return user

def trusted_link(value, allowed):
    try:
        p = urlparse(value)
        return value if p.scheme == 'https' and p.hostname in allowed and not p.username and not p.password and p.port in (None,443) else ''
    except ValueError:
        return ''

@app.get('/api/community')
def community():
    streams = []
    try: configured = json.loads(os.getenv('TAPIN_STREAMS_JSON','[]'))
    except ValueError: configured = []
    for item in configured[:20] if isinstance(configured,list) else []:
        if not isinstance(item,dict): continue
        url = trusted_link(str(item.get('url','')), {'twitch.tv','www.twitch.tv','youtube.com','www.youtube.com','youtu.be','kick.com','www.kick.com'})
        if url: streams.append({'name':str(item.get('name','Stream'))[:80],'url':url})
    donation = trusted_link(os.getenv('TAPIN_DONATION_URL',''), {'cash.app','www.paypal.com','paypal.me','buymeacoffee.com','www.buymeacoffee.com','ko-fi.com'})
    return {'streams':streams,'donation_url':donation,'donation_recipient':os.getenv('TAPIN_DONATION_RECIPIENT','')[:100] if donation else ''}

@app.get('/api/chat')
def chat():
    with closing(connect()) as db:
        rows = db.execute('SELECT id,username,message,created FROM chat_messages WHERE hidden=0 AND created>? ORDER BY id DESC LIMIT 60',(time.time()-86400*7,)).fetchall()
    return {'messages':[dict(r) for r in reversed(rows)]}

class ChatMessage(BaseModel):
    message: str

@app.post('/api/chat')
def send_chat(body: ChatMessage, user=Depends(current_user)):
    message = body.message.strip()
    if not 1 <= len(message) <= 180: raise HTTPException(422,'Use 1–180 characters.')
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM chat_bans WHERE user_id=?',(user['id'],)).fetchone(): raise HTTPException(403,'Chat access suspended.')
        if db.execute('SELECT COUNT(*) FROM chat_messages WHERE user_id=? AND created>?',(user['id'],time.time()-10)).fetchone()[0]>=3: raise HTTPException(429,'Slow down.')
        db.execute('INSERT INTO chat_messages(user_id,username,message,created) VALUES (?,?,?,?)',(user['id'],user['username'],message,time.time()))
        db.execute('DELETE FROM chat_reports WHERE message_id IN (SELECT id FROM chat_messages WHERE created<?)',(time.time()-86400*7,))
        db.execute('DELETE FROM chat_messages WHERE created<?',(time.time()-86400*7,))
    return {'ok':True}

@app.post('/api/chat/{mid}/report')
def report_chat(mid:int,user=Depends(current_user)):
    with closing(connect()) as db, db:
        if not db.execute('SELECT 1 FROM chat_messages WHERE id=? AND hidden=0',(mid,)).fetchone(): raise HTTPException(404,'Message unavailable.')
        db.execute('INSERT OR IGNORE INTO chat_reports VALUES (?,?)',(mid,user['id']))
    return {'ok':True}

@app.get('/api/admin/chat')
def admin_chat(user=Depends(current_user)):
    owner(user)
    with closing(connect()) as db:
        rows=db.execute('SELECT m.id,m.username,m.message,m.hidden,COUNT(r.reporter) AS reports FROM chat_messages m LEFT JOIN chat_reports r ON r.message_id=m.id GROUP BY m.id ORDER BY reports DESC,m.id DESC LIMIT 100').fetchall()
    return {'messages':[dict(r) for r in rows]}

@app.delete('/api/admin/chat/{mid}')
def hide_chat(mid:int,user=Depends(current_user)):
    owner(user)
    with closing(connect()) as db,db: db.execute('UPDATE chat_messages SET hidden=1 WHERE id=?',(mid,))
    return {'ok':True}

@app.post('/api/admin/chat/{mid}/ban')
def ban_chat(mid:int,user=Depends(current_user)):
    owner(user)
    with closing(connect()) as db,db:
        row=db.execute('SELECT user_id FROM chat_messages WHERE id=?',(mid,)).fetchone()
        if not row: raise HTTPException(404,'Message unavailable.')
        if row['user_id']==user['id']: raise HTTPException(400,'Cannot ban yourself.')
        db.execute('INSERT OR IGNORE INTO chat_bans VALUES (?)',(row['user_id'],))
        db.execute('UPDATE chat_messages SET hidden=1 WHERE user_id=?',(row['user_id'],))
    return {'ok':True}

@app.post('/api/admin/chat/unban/{username}')
def unban_chat(username:str,user=Depends(current_user)):
    owner(user)
    with closing(connect()) as db,db:
        db.execute('DELETE FROM chat_bans WHERE user_id IN (SELECT id FROM users WHERE username=?)',(username.strip().lower(),))
    return {'ok':True}

@app.post('/api/visit')
def track_visit(request:Request,response:Response):
    if request.headers.get('dnt')=='1' or request.headers.get('sec-gpc')=='1': return {'tracked':False}
    token=request.cookies.get('tapin_visit','')
    if not re.fullmatch('[a-f0-9]{32}',token):
        token=secrets.token_hex(16)
        response.set_cookie('tapin_visit',token,max_age=86400*30,httponly=True,secure=os.getenv('TAPIN_COOKIE_SECURE','1')!='0',samesite='strict')
    day=time.strftime('%Y-%m-%d',time.gmtime())
    visitor=hashlib.sha256((day+token).encode()).hexdigest()
    with closing(connect()) as db,db:
        db.execute('INSERT INTO visits(day,visitor,views) VALUES (?,?,1) ON CONFLICT(day,visitor) DO UPDATE SET views=views+1',(day,visitor))
        db.execute("DELETE FROM visits WHERE day < date('now','-30 days')")
    return {'tracked':True}

@app.get('/api/admin/analytics')
def analytics(user=Depends(current_user)):
    owner(user)
    with closing(connect()) as db:
        rows=db.execute('SELECT day,COUNT(*) AS estimated_browsers,SUM(views) AS pageviews FROM visits GROUP BY day ORDER BY day DESC LIMIT 30').fetchall()
        accounts=db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        reviews=db.execute('SELECT COUNT(*) FROM clips WHERE review IS NOT NULL').fetchone()[0]
    return {'days':[dict(r) for r in rows],'accounts':accounts,'saved_reviews':reviews}
