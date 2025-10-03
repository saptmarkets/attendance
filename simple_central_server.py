import os
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, redirect, url_for

app = Flask(__name__)
app.secret_key = 'change-me'

DB_FILE = 'attendance_data.db'

def init_database():
	conn = sqlite3.connect(DB_FILE)
	conn.execute('''
		CREATE TABLE IF NOT EXISTS branches (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			branch_id INTEGER UNIQUE NOT NULL,
			branch_name TEXT NOT NULL,
			api_token TEXT UNIQUE,
			is_active BOOLEAN DEFAULT 1,
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
		)
	''')
	conn.execute('''
		CREATE TABLE IF NOT EXISTS attendance_logs (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			branch_id INTEGER NOT NULL,
			employee_id TEXT NOT NULL,
			check_time TIMESTAMP NOT NULL,
			punch_type INTEGER,
			status INTEGER,
			machine_id TEXT,
			event_id TEXT UNIQUE NOT NULL,
			synced_to_odoo BOOLEAN DEFAULT 0,
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
		)
	''')
	# Employees master for showing names
	conn.execute('''
		CREATE TABLE IF NOT EXISTS employees (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			branch_id INTEGER NOT NULL,
			user_id TEXT NOT NULL,
			name TEXT,
			card_number TEXT,
			UNIQUE(branch_id, user_id)
		)
	''')
	# ADMS queue tables (to accept device push directly)
	conn.execute('''
		CREATE TABLE IF NOT EXISTS adms_attendance_queue (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			branch_id TEXT,
			user_id TEXT,
			timestamp TEXT,
			punch_type INTEGER,
			status INTEGER,
			event_id TEXT UNIQUE,
			created_at TEXT DEFAULT CURRENT_TIMESTAMP
		)
	''')
	# Minimal HTTP debug log for ADMS troubleshooting
	conn.execute('''
		CREATE TABLE IF NOT EXISTS adms_http_log (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			ts TEXT,
			method TEXT,
			path TEXT,
			remote_addr TEXT,
			user_agent TEXT,
			body TEXT
		)
	''')
	conn.execute('''
		CREATE TABLE IF NOT EXISTS sync_status (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			branch_id INTEGER UNIQUE NOT NULL,
			last_sync_time TIMESTAMP,
			sync_count INTEGER DEFAULT 0,
			last_error TEXT,
			updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
		)
	''')
	conn.commit()
	conn.close()

def get_db_connection():
	conn = sqlite3.connect(DB_FILE)
	conn.row_factory = sqlite3.Row
	return conn

@app.route('/')
def dashboard():
	conn = get_db_connection()
	stats = conn.execute('''
		SELECT COUNT(DISTINCT branch_id) as total_branches,
			COUNT(*) as total_records,
			COUNT(CASE WHEN DATE(check_time)=DATE('now') THEN 1 END) as today_records,
			COUNT(CASE WHEN synced_to_odoo=0 THEN 1 END) as unsynced_records
		FROM attendance_logs
	''').fetchone()
	recent_logs = conn.execute('''
		SELECT al.*, b.branch_name, COALESCE(e.name,'') AS employee_name
		FROM attendance_logs al
		JOIN branches b ON al.branch_id=b.branch_id
		LEFT JOIN employees e ON e.branch_id=al.branch_id AND e.user_id=al.employee_id
		ORDER BY al.check_time DESC LIMIT 20
	''').fetchall()
	branch_status = conn.execute('''
		SELECT b.branch_name, ss.last_sync_time, ss.sync_count, ss.last_error
		FROM branches b LEFT JOIN sync_status ss ON b.branch_id=ss.branch_id
		WHERE b.is_active=1
	''').fetchall()
	conn.close()

	html = '''
	<!DOCTYPE html><html><head><title>Central Dashboard</title>
	<style>body{font-family:Arial;margin:40px;background:#f5f5f5} .container{max-width:1100px;margin:0 auto}
	.header{background:#2c3e50;color:#fff;padding:16px;border-radius:8px;margin-bottom:16px}
	.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
	.stat{background:#fff;padding:14px;border-radius:8px;text-align:center}
	table{width:100%;border-collapse:collapse}th,td{padding:8px;border-bottom:1px solid #ddd}
	.section{background:#fff;padding:14px;border-radius:8px;margin-bottom:16px}
	</style></head><body><div class="container">
	<div class="header"><h2>ZKTeco Attendance - Central</h2></div>
	<div class="stats">
		<div class="stat"><div>{{stats.total_branches}}</div><small>Branches</small></div>
		<div class="stat"><div>{{stats.total_records}}</div><small>Total Records</small></div>
		<div class="stat"><div>{{stats.today_records}}</div><small>Today</small></div>
		<div class="stat"><div>{{stats.unsynced_records}}</div><small>Pending</small></div>
	</div>
	<div class="section"><h3>Branch Status</h3>
	<table><tr><th>Branch</th><th>Last Sync</th><th>Syncs</th><th>Status</th></tr>
	{% for r in branch_status %}
	<tr><td>{{r.branch_name}}</td><td>{{r.last_sync_time or 'Never'}}</td><td>{{r.sync_count or 0}}</td><td>{{'Error' if r.last_error else 'OK'}}</td></tr>
	{% endfor %}
	</table></div>
    <div class="section"><h3>Recent Activity</h3>
	<table><tr><th>Branch</th><th>Emp ID</th><th>Name</th><th>Time</th><th>Type</th></tr>
	{% for l in recent_logs %}
	<tr><td>{{l.branch_name}}</td><td>{{l.employee_id}}</td><td>{{l.employee_name}}</td><td>{{l.check_time}}</td><td>{{l.punch_type}}</td></tr>
	{% endfor %}
	</table></div>
    <div class="section">
    <a href="/refresh_latest" class="btn">Pull Latest (ingest ADMS queue)</a>
    </div>
	</div></body></html>'''
	return render_template_string(html, stats=stats, recent_logs=recent_logs, branch_status=branch_status)

@app.route('/api/attendance', methods=['POST'])
def receive_attendance():
	try:
		data = request.get_json()
		if not data:
			return jsonify({'error':'No data'}), 400
		required = ['branch_id','branch_name','data']
		if not all(k in data for k in required):
			return jsonify({'error':'Missing required fields'}), 400
		branch_id = data['branch_id']
		branch_name = data['branch_name']
		payload = data['data']
		conn = get_db_connection()
        branch_label = branch_name or ('Device 1' if str(branch_id) == '1' else f'Branch {branch_id}')
        conn.execute('''INSERT OR REPLACE INTO branches (branch_id, branch_name, api_token)
            VALUES (?,?, 'token_'||?)''', (branch_id, branch_label, branch_id))
		inserted = 0
		for rec in payload.get('attendance_logs', []):
			try:
				cur = conn.execute('''INSERT OR IGNORE INTO attendance_logs
					(branch_id, employee_id, check_time, punch_type, status, machine_id, event_id)
					VALUES (?,?,?,?,?,?,?)''', (
					branch_id, rec['user_id'], rec['timestamp'], rec.get('punch_type'), rec.get('status'), rec.get('machine_id'), rec['event_id']))
                if getattr(cur, 'rowcount', -1) == 1:
                    inserted += 1
                    # Upsert employee name if provided in payload.employees
                    emp_list = payload.get('employees') or []
                    for emp in emp_list:
                        if str(emp.get('user_id')) == str(rec['user_id']):
                            conn.execute('''INSERT OR REPLACE INTO employees (branch_id, user_id, name, card_number)
                                VALUES (?,?,?,?)''', (branch_id, str(emp.get('user_id')), emp.get('name',''), str(emp.get('card_number',''))))
                            break
			except sqlite3.IntegrityError:
				continue
		conn.execute('''INSERT OR REPLACE INTO sync_status (branch_id, last_sync_time, sync_count)
			VALUES (?, datetime('now'), COALESCE((SELECT sync_count FROM sync_status WHERE branch_id=?),0)+1)''', (branch_id, branch_id))
		conn.commit(); conn.close()
		return jsonify({'status':'success','branch_id':branch_id,'records_processed':inserted,'timestamp':datetime.now().isoformat()})
	except Exception as e:
		return jsonify({'error':str(e)}), 500

@app.route('/api/stats')
def api_stats():
	conn = get_db_connection()
	row = conn.execute('''SELECT COUNT(DISTINCT branch_id) total_branches,
		COUNT(*) total_records,
		COUNT(CASE WHEN DATE(check_time)=DATE('now') THEN 1 END) today_records,
		COUNT(CASE WHEN synced_to_odoo=0 THEN 1 END) unsynced_records,
		MAX(check_time) latest_records,
		MIN(check_time) earliest_records
		FROM attendance_logs''').fetchone()
	conn.close()
	return jsonify({'total_branches':row['total_branches'],'total_records':row['total_records'],'today_records':row['today_records'],'unsynced_records':row['unsynced_records'],'latest_record':row['latest_records'],'earliest_record':row['earliest_records']})

# -----------------------------
# ADMS endpoints (device direct push)
# -----------------------------

@app.route('/biometric/adms_push', methods=['POST'])
@app.route('/biometric/admsPush', methods=['POST'])
def adms_push():
	"""Accepts ADMS-like JSON payloads directly from device/bridge.

	Expected minimal shape:
	{
	  "event_type": "attendance",
	  "branch_id": 1,
	  "data": [{"user_id": "123", "timestamp": "YYYY-mm-dd HH:MM:SS", "punch_type": 1, "status": 1, "event_id": "..."}]
	}
	"""
	try:
		# debug log
		try:
			conn_dbg = sqlite3.connect(DB_FILE)
			conn_dbg.execute('INSERT INTO adms_http_log (ts, method, path, remote_addr, user_agent, body) VALUES (?,?,?,?,?,?)', (
				datetime.now().isoformat(), request.method, request.path, request.headers.get('X-Forwarded-For','') or request.remote_addr, request.headers.get('User-Agent',''), (request.get_data(as_text=True) or '')[:2000]
			))
			conn_dbg.commit(); conn_dbg.close()
		except Exception:
			pass
		payload = request.get_json(silent=True)
		if not payload:
			return jsonify({'error': 'No JSON data received'}), 400
		if payload.get('event_type') != 'attendance':
			return jsonify({'error': 'Unsupported event_type; expected attendance'}), 400
		branch_id = payload.get('branch_id')
		records = payload.get('data', [])
		conn = sqlite3.connect(DB_FILE)
		c = conn.cursor()
		inserted = 0
		for rec in records:
			try:
				c.execute('''INSERT OR IGNORE INTO adms_attendance_queue
					(branch_id, user_id, timestamp, punch_type, status, event_id)
					VALUES (?,?,?,?,?,?)''', (
					str(branch_id), str(rec.get('user_id')), rec.get('timestamp'),
					int(rec.get('punch_type', 1)), int(rec.get('status', 1)), rec.get('event_id', '')
				))
				if c.rowcount == 1:
					inserted += 1
			except Exception:
				continue
		conn.commit(); conn.close()
		return jsonify({'status': 'success', 'records_queued': inserted})
	except Exception as e:
		return jsonify({'error': str(e)}), 500

@app.route('/biometric/health')
@app.route('/biometric/healthCheck')
def adms_health():
	return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

@app.route('/biometric/queue_status')
@app.route('/biometric/queueStatus')
def adms_queue_status():
	conn = sqlite3.connect(DB_FILE)
	c = conn.cursor()
	c.execute('SELECT COUNT(*) FROM adms_attendance_queue')
	count = c.fetchone()[0]
	conn.close()
	return jsonify({'pending_attendance': count, 'timestamp': datetime.now().isoformat()})

@app.route('/biometric/debug_echo', methods=['GET','POST'])
def adms_debug_echo():
	"""Echo back request info to validate device connectivity."""
	info = {
		'timestamp': datetime.now().isoformat(),
		'method': request.method,
		'path': request.path,
		'remote_addr': request.headers.get('X-Forwarded-For','') or request.remote_addr,
		'headers': {k:v for k,v in request.headers.items()},
		'args': request.args.to_dict(),
		'json': request.get_json(silent=True),
	}
	return jsonify(info)

@app.route('/biometric/recent_http_logs')
def recent_http_logs():
	conn = sqlite3.connect(DB_FILE)
	conn.row_factory = sqlite3.Row
	rows = conn.execute('SELECT * FROM adms_http_log ORDER BY id DESC LIMIT 50').fetchall()
	conn.close()
	return jsonify([{k: row[k] for k in row.keys()} for row in rows])

def _move_adms_queue_to_logs(max_rows: int = 200, force_branch: str | None = None) -> int:
	conn = sqlite3.connect(DB_FILE)
	conn.row_factory = sqlite3.Row
	c = conn.cursor()
	c.execute('SELECT * FROM adms_attendance_queue ORDER BY id ASC LIMIT ?', (max_rows,))
	rows = c.fetchall()
	inserted = 0
	last_branch = None
	for r in rows:
		branch_id = int(force_branch or (r['branch_id'] or 0) or 0)
		last_branch = branch_id
		user_id = str(r['user_id'])
		check_time = r['timestamp']
		punch_type = r['punch_type']
		status = r['status']
		event_id = r['event_id'] or f"adms-{r['id']}"
		conn.execute('''INSERT OR REPLACE INTO branches (branch_id, branch_name, api_token)
						VALUES (?, COALESCE((SELECT branch_name FROM branches WHERE branch_id=?),'ADMS Branch'), 'token_'||?)''', (branch_id, branch_id, branch_id))
		cur = conn.execute('''INSERT OR IGNORE INTO attendance_logs
							  (branch_id, employee_id, check_time, punch_type, status, machine_id, event_id)
							  VALUES (?,?,?,?,?,?,?)''', (branch_id, user_id, check_time, punch_type, status, 'ADMS', event_id))
		if getattr(cur, 'rowcount', -1) == 1:
			inserted += 1
		conn.execute('DELETE FROM adms_attendance_queue WHERE id=?', (r['id'],))
	if inserted and last_branch is not None:
		conn.execute('''INSERT OR REPLACE INTO sync_status (branch_id, last_sync_time, sync_count)
						VALUES (?, datetime('now'), COALESCE((SELECT sync_count FROM sync_status WHERE branch_id=?),0)+1)''', (last_branch, last_branch))
	conn.commit(); conn.close()
	return inserted

@app.route('/biometric/ingest_queue', methods=['POST', 'GET'])
def adms_ingest_queue():
	"""Move queued ADMS records into main attendance_logs so they appear on the dashboard/stats.

	Query params (optional):
	- max: number of records to process (default 200)
	- branch_id: override/force branch id for inserted rows if needed
	"""
	try:
		max_rows = int(request.args.get('max', '200'))
		force_branch = request.args.get('branch_id')
		moved = _move_adms_queue_to_logs(max_rows=max_rows, force_branch=force_branch)
		return jsonify({'status':'success','moved': moved})
	except Exception as e:
		return jsonify({'error': str(e)}), 500

@app.route('/refresh_latest')
def refresh_latest():
	_move_adms_queue_to_logs(max_rows=500)
	return redirect(url_for('dashboard'))

if __name__ == '__main__':
	init_database()
	port = int(os.getenv('PORT','8020'))
	app.run(host='0.0.0.0', port=port)
