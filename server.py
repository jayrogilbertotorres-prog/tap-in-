"""TapIn five-review gameplay coaching prototype. Run behind HTTPS in production."""
import base64, json, os, re, sqlite3, tempfile, threading
from pathlib import Path
from uuid import UUID
import cv2
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from openai import OpenAI

BASE = Path(__file__).resolve().parent
DB = Path(os.getenv('TAPIN_DB', str(BASE / 'reviews.sqlite3')))
MAX_BYTES = 50 * 1024 * 1024
LIMIT = 5
lock = threading.Lock()
app = FastAPI(title='TapIn AI Coaching')

def visitor_id(value):
    try: return str(UUID(value))
    except (ValueError, TypeError, AttributeError): raise HTTPException(400, 'Invalid anonymous visitor ID')

def connect():
    con = sqlite3.connect(DB, timeout=30)
    con.execute('CREATE TABLE IF NOT EXISTS reviews (visitor TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0)')
    return con

def used(visitor):
    with connect() as db:
        row=db.execute('SELECT count FROM reviews WHERE visitor=?',(visitor,)).fetchone()
    return row[0] if row else 0

@app.get('/')
def home(): return FileResponse(BASE/'index.html', media_type='text/html')

@app.get('/api/status')
def status(x_tapin_visitor: str = Header(...)):
    v=visitor_id(x_tapin_visitor)
    return {'ready':bool(os.getenv('OPENAI_API_KEY')),'remaining':max(0,LIMIT-used(v)),'limit':LIMIT}

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

@app.post('/api/analyze')
async def analyze(video: UploadFile=File(...), focus: str=Form('Gameplay decisions'), x_tapin_visitor: str=Header(...)):
    v=visitor_id(x_tapin_visitor)
    if not os.getenv('OPENAI_API_KEY'): raise HTTPException(503,'AI service not connected. Set OPENAI_API_KEY on the server.')
    if used(v)>=LIMIT: raise HTTPException(403,'All five free reviews have been used in this browser.')
    # Limit file size while streaming; no raw video persisted after analysis.
    suffix=Path(video.filename or '').suffix.lower()
    if suffix not in ('.mp4','.webm','.mov','.m4v'):
        raise HTTPException(415,'Please use an MP4, WebM, or MOV video.')
    tmp=tempfile.NamedTemporaryFile(suffix=suffix,delete=False)
    try:
        size=0
        while chunk:=await video.read(1024*1024):
            size+=len(chunk)
            if size>MAX_BYTES: raise HTTPException(413,'Clip exceeds 50 MB limit.')
            tmp.write(chunk)
        tmp.close()
        frames=frames_from_video(tmp.name)
        content=[{'type':'input_text','text':(
          'You are TapIn, an FPS video-game coach. Analyze ONLY the visible frames. '
          'These are sparse screenshots, so do not invent unseen kills, audio, movements, or events. '
          'Give 3 to 5 concrete coaching observations and improvement drills. '
          'Include the sampled timestamps for claims and confidence/limitations. '
          'Provide a JSON object ONLY, with keys analysis (plain text) and markers '
          '(array of {seconds: number, note: string}). Each marker must refer to a '
          'visible frame. Do not claim exact game outcomes based on sparse frames. '
          'Player focus: '+focus[:400]+'; sampled frame times are listed next.'
        )}]
        for t,b64 in frames:
            content.append({'type':'input_text','text':f'Frame at {t:.1f} seconds'})
            content.append({'type':'input_image','image_url':'data:image/jpeg;base64,'+b64,'detail':'low'})
        response=OpenAI(timeout=65).responses.create(model=os.getenv('TAPIN_MODEL','gpt-4.1-mini'),input=[{'role':'user','content':content}],max_output_tokens=1100,store=False)
        raw=response.output_text.strip()
        raw=re.sub(r'^```(?:json)?\s*|\s*```$','',raw)
        try: result=json.loads(raw)
        except json.JSONDecodeError: result={'analysis':raw,'markers':[]}
        if not isinstance(result,dict) or not result.get('analysis'): raise HTTPException(502,'AI returned an empty review. Please retry.')
        markers=[]
        for m in result.get('markers',[]):
            if not isinstance(m,dict): continue
            try: sec=float(m.get('seconds',0))
            except (TypeError,ValueError): continue
            if sec<0 or sec>120: continue
            markers.append({'seconds':sec,'time':f'{int(sec//60)}:{int(sec%60):02d}','note':str(m.get('note',''))[:220]})
        with lock:
            with connect() as db:
                current=db.execute('SELECT count FROM reviews WHERE visitor=?',(v,)).fetchone()
                n=current[0] if current else 0
                if n>=LIMIT: raise HTTPException(403,'Five-review limit reached.')
                db.execute('INSERT INTO reviews (visitor,count) VALUES (?,?) ON CONFLICT(visitor) DO UPDATE SET count=excluded.count',(v,n+1))
        return {'analysis':str(result['analysis'])[:5500],'markers':markers,'remaining':LIMIT-(n+1)}
    finally:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
