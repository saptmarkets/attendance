# Copied for Render deployment wrapper
from pathlib import Path
import sys

# Ensure we can import the server_setup version if needed
BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
if str(ROOT / 'server_setup') not in sys.path:
	sys.path.insert(0, str(ROOT / 'server_setup'))

# Reuse the server_setup/simple_central_server.py by executing it as main
from server_setup.simple_central_server import *  # noqa: F401,F403

if __name__ == '__main__':
	# The imported file already defines the app and __main__ guard; we call its main run indirectly
	import os
	init_database()
	port = int(os.getenv('PORT', '8020'))
	app.run(host='0.0.0.0', port=port, debug=False)
