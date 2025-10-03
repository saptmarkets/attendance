Render Deployment (Central Server)

- simple_central_server.py: wrapper that imports the app from server_setup and runs with PORT.
- requirements.txt: Flask + requests.
- Procfile: start command.

Deploy on Render:
- Root directory: render_deploy
- Build: pip install -r requirements.txt
- Start: python simple_central_server.py
- After deploy: use /, /api/health, /api/stats, and POST /api/attendance
