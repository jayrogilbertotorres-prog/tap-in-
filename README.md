# TapIn AI Coaching — runnable development package

This is a companion API for TapIn's HTML preview. It samples images from uploaded video and asks an OpenAI vision model for clip-specific coaching. It does **not** analyze every frame, track live matches, or guarantee correct gameplay conclusions.

## Quick start (computer or server with Python 3.10+)

1. Unzip this package, open a terminal in the `tapin-ai-server` folder.
2. Install Python packages: `python -m pip install -r requirements.txt`
3. Create an OpenAI API key in your own OpenAI Platform account and keep it private. Never paste it into the website or commit it to source code.
4. Set the environment variable `OPENAI_API_KEY` on the server: on macOS/Linux `export OPENAI_API_KEY='your-secret-key'`; on Windows PowerShell `$env:OPENAI_API_KEY='your-secret-key'`.
5. Start the service: `python -m uvicorn server:app --host 127.0.0.1 --port 8000`
6. Open `http://127.0.0.1:8000` in a browser on that same computer. Upload an MP4 and press Analyze.

## Deployment notes

- To share with other players, deploy the Python server on a trusted host behind **HTTPS**; do not just upload `index.html` to static site hosting. Attach a proper domain and route both the website and `/api/*` to this server.
- The server reads the OpenAI key from its environment, keeping it out of the public browser.
- Each successful analysis costs API usage; even though reviews are free to players, the project owner pays the API bill.
- Five reviews are tracked per anonymous browser UUID in SQLite. Clearing site data, using another device, or forging a different UUID bypasses the limit. Strong per-person limits require account verification or abuse controls (potentially a privacy-preserving alternative to mandatory registration).
- Add authentication or robust abuse protection, rate limits, origin restrictions, privacy policy, encryption/storage controls, usage monitoring and spending caps before a public launch. The code is a development starter, **not production-ready**.
- Files are capped at 50 MB and deleted after processing. Up to 12 frames from the first two minutes are sampled. Users should not upload private or copyrighted material they lack permission to share.
- This package has not been tested with a real API key in this environment and is not connected to a public TapIn site.
