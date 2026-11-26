"""TapIn account-based gameplay coaching prototype. Run behind HTTPS in production."""
import base64, json, os, re, sqlite3, tempfile, threading
import hashlib, hmac, secrets, time, math
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
app = FastAPI(title='TapIn AI Coaching')

def connect():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript("""
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
    response.delete_cookie(COOKIE)
    return {'ok': True}

@app.get('/')
def home(): return FileResponse(BASE/'index.html', media_type='text/html')

@app.get('/api/status')
def status(): return {'ready': bool(os.getenv('OPENAI_API_KEY'))}

@app.get('/api/profile')
def profile(user=Depends(current_user)):
    with connect() as db:
        rows = db.execute('SELECT id, filename, created, review FROM clips WHERE user_id=? ORDER BY created DESC', (user['id'],)).fetchall()
    return {'username': user['username'], 'clips': [dict(r) | {'review': json.loads(r['review']) if r['review'] else None} for r in rows]}

@app.post('/api/clips')
async def upload_clip(video: UploadFile=File(...), user=Depends(current_user)):
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
        with connect() as db:
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
    return FileResponse(CLIPS / row['stored'], headers={'Cache-Control': 'private, no-store'})

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
    fps=capture.get(cv2.CAP_PROP_FPS) or 30
    count=capture.get(cv2.CAP_PROP_FRAME_COUNT)
    seconds=min(120.0,count/fps if count>0 else 120.0)
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
    response=OpenAI(timeout=65, max_retries=0).responses.create(model=os.getenv('TAPIN_MODEL','gpt-4.1-mini'),input=[{'role':'user','content':content}],max_output_tokens=1100,store=False)
    raw=response.output_text.strip()
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
