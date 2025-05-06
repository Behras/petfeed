from flask import Flask, request, jsonify, render_template, send_from_directory
import sqlite3
from pywebpush import webpush, WebPushException
import json
from datetime import datetime, timedelta
import time
import os

VAPID_PUBLIC_KEY = 'BEwDtGd3UZQsVa2XJoqQc_d5_8l_irrTEyfqX847SwqA3asa7WleP9yKBsAJ2GsFsjTMl-f8sy5rl4QCAITlVLY'
VAPID_PRIVATE_KEY = 'c2kwyw64IFjjEvqIF7FItp3T0JAOafcbud9dB1Xzd4c'
SUBSCRIPTIONS_DB = 'subscriptions.json'
DB_FILE = 'feeder.db'

app = Flask(__name__)
app.current_scales = {'scale1': None, 'scale2': None}
app.last_raw_scales = {'scale1': None, 'scale2': None}
app.calibration_factors = {1: None, 2: None}
app.scale_zero_offsets = {1: 0, 2: 0}
app.scale_history_raw = {'scale1': [], 'scale2': []}
app.last_stable_weights = {'scale1': 0, 'scale2': 0}
app.weight_stability_count = {'scale1': 0, 'scale2': 0}
current_status = {'state': 'idle'}
feed_request = {'pending': False, 'amount': None}
tare_request = {1: False, 2: False}
motor_test_request = {'pending': False, 'direction': None, 'duration': None}

# For tracking negative readings
app.negative_reading_counter = {'scale1': 0, 'scale2': 0}
app.last_auto_tare = {'scale1': 0, 'scale2': 0}  # Timestamp of last auto-tare

# Feature flags - can be toggled via API
app.features = {
    'use_zero_offsets': True,  # Re-enabled for troubleshooting
    'use_stability_tracking': True,
    'use_auto_tare': True,  # Re-enabled for troubleshooting
    'use_weight_filtering': True
}

# Constants for filtering
ZERO_THRESHOLD = 3  # Reduced: Consider values below this absolute value as zero (3g)
MAX_HISTORY_RAW = 5  # Fewer samples to be more responsive to small changes
MAX_WEIGHT = 500  # Maximum weight in grams (limit lowered to 500g)
OUTLIER_THRESHOLD = 100  # Maximum allowed deviation for filtering outliers (g)
NEGATIVE_TARE_THRESHOLD = 3  # Apply auto-tare after this many consecutive negative readings
WEIGHT_STABILITY_THRESHOLD = 3  # Number of consecutive similar readings to consider stable

# ESP32 status tracking
esp32_status = {
    'ip': None,
    'rssi': None,
    'firmware': None,
    'last_checkin': None
}

# Store last N scale readings for live graph
MAX_SCALE_HISTORY = 100
scale_history = []  # Each entry: {'timestamp': ..., 'scale1': ..., 'scale2': ...}

MOTOR_TEST_FLAG_FILE = '/tmp/motor_test_pending.json'  # Define flag file path

def update_db_schema():
    """Update the database schema to add the scale column if it doesn't exist"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Check if scale column exists
    c.execute("PRAGMA table_info(feed_logs)")
    columns = [column[1] for column in c.fetchall()]
    
    if 'scale' not in columns:
        print("Adding scale column to feed_logs table...")
        c.execute('''
            ALTER TABLE feed_logs
            ADD COLUMN scale INTEGER DEFAULT 1
        ''')
        conn.commit()
        print("Database schema updated successfully")
    else:
        print("Database schema is up to date")
    
    # Check if scale_history table exists
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='scale_history'")
    if not c.fetchone():
        print("Creating scale_history table...")
        c.execute('''
            CREATE TABLE scale_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                scale1 REAL,
                scale2 REAL,
                scale1_raw TEXT,
                scale2_raw TEXT
            )
        ''')
        conn.commit()
        print("Scale history table created successfully")
    
    conn.close()

# Function to save raw scale readings to the database for persistence
def save_raw_scales(raw1, raw2):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        # First check if the table exists
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='raw_scale_readings'")
        if not c.fetchone():
            # Create the table if it doesn't exist
            c.execute('''
                CREATE TABLE raw_scale_readings (
                    scale_id INTEGER PRIMARY KEY,
                    raw_value TEXT,
                    timestamp TEXT
                )
            ''')
        
        # Save raw values with timestamp
        now = datetime.now().isoformat()
        if raw1 is not None:
            c.execute('''
                INSERT OR REPLACE INTO raw_scale_readings (scale_id, raw_value, timestamp)
                VALUES (1, ?, ?)
            ''', (str(raw1), now))
        
        if raw2 is not None:
            c.execute('''
                INSERT OR REPLACE INTO raw_scale_readings (scale_id, raw_value, timestamp)
                VALUES (2, ?, ?)
            ''', (str(raw2), now))
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error saving raw scale readings: {e}")
        return False

# Function to load raw scale readings from the database
def load_raw_scales():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        # Check if the table exists
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='raw_scale_readings'")
        if not c.fetchone():
            conn.close()
            return None, None
        
        # Get the most recent raw values
        c.execute('''
            SELECT scale_id, raw_value FROM raw_scale_readings
            ORDER BY scale_id
        ''')
        results = c.fetchall()
        conn.close()
        
        # Process results
        raw1 = None
        raw2 = None
        for row in results:
            if row[0] == 1:
                raw1 = row[1]
            elif row[0] == 2:
                raw2 = row[1]
        
        return raw1, raw2
    except Exception as e:
        print(f"Error loading raw scale readings: {e}")
        return None, None

# Function to save zero offset value to the database
def save_zero_offset(scale_id, offset):
    """Save a zero offset value to the database for persistence"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        # First check if the table exists
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='zero_offsets'")
        if not c.fetchone():
            # Create the table if it doesn't exist
            c.execute('''
                CREATE TABLE zero_offsets (
                    scale_id INTEGER PRIMARY KEY,
                    offset_value REAL,
                    timestamp TEXT
                )
            ''')
        
        # Save offset with timestamp
        now = datetime.now().isoformat()
        c.execute('''
            INSERT OR REPLACE INTO zero_offsets (scale_id, offset_value, timestamp)
            VALUES (?, ?, ?)
        ''', (scale_id, offset, now))
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error saving zero offset: {e}")
        return False

# Function to load zero offsets from database
def load_zero_offsets():
    """Load zero offsets from database into app memory"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        # Check if the table exists
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='zero_offsets'")
        if not c.fetchone():
            conn.close()
            return
        
        # Get the offset values
        c.execute('SELECT scale_id, offset_value FROM zero_offsets')
        for row in c.fetchall():
            scale_id = row[0]
            offset = row[1]
            app.scale_zero_offsets[scale_id] = offset
            print(f"Loaded zero offset for scale {scale_id}: {offset}")
        
        conn.close()
    except Exception as e:
        print(f"Error loading zero offsets: {e}")

# Function to load calibration factors from DB
def load_calibration_factors():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT scale_id, factor FROM calibration_settings')
    factors = {row[0]: row[1] for row in c.fetchall()}
    app.calibration_factors[1] = factors.get(1)
    app.calibration_factors[2] = factors.get(2)
    conn.close()
    print(f"Loaded calibration factors: {app.calibration_factors}")

# Function to initialize the database
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Feed logs table
    c.execute('''
        CREATE TABLE IF NOT EXISTS feed_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            amount TEXT,
            status TEXT,
            notes TEXT,
            scale INTEGER
        )
    ''')
    # Schedules table
    c.execute('''
        CREATE TABLE IF NOT EXISTS feed_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            amount TEXT,
            notes TEXT
        )
    ''')
    # Calibration settings table
    c.execute('''
        CREATE TABLE IF NOT EXISTS calibration_settings (
            scale_id INTEGER PRIMARY KEY,
            factor REAL
        )
    ''')
    # Raw scale readings table
    c.execute('''
        CREATE TABLE IF NOT EXISTS raw_scale_readings (
            scale_id INTEGER PRIMARY KEY,
            raw_value TEXT,
            timestamp TEXT
        )
    ''')
    # Zero offsets table
    c.execute('''
        CREATE TABLE IF NOT EXISTS zero_offsets (
            scale_id INTEGER PRIMARY KEY,
            offset_value REAL,
            timestamp TEXT
        )
    ''')
    conn.commit()
    conn.close()
    # Update schema if needed
    update_db_schema()

# Call initialization and loading functions
init_db()
load_calibration_factors()
load_zero_offsets()  # Add this line to load zero offsets on startup

# Log a feed event
def log_feed(amount, status, notes="", scale=1):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT INTO feed_logs (timestamp, amount, status, notes, scale)
        VALUES (?, ?, ?, ?, ?)
    ''', (now, amount, status, notes, scale))
    conn.commit()
    conn.close()

@app.route('/')
def home():
    # Pass the latest known scale values to the template
    latest_scale1 = app.current_scales.get('scale1') # Defaults to None
    latest_scale2 = app.current_scales.get('scale2') # Defaults to None
    # Format for display (handle None)
    display_scale1 = f"{latest_scale1:.1f}" if latest_scale1 is not None else "N/A"
    display_scale2 = f"{latest_scale2:.1f}" if latest_scale2 is not None else "N/A"
    return render_template(
        'home.html', 
        vapid_public_key=VAPID_PUBLIC_KEY,
        initial_scale1_display=display_scale1, # Send formatted string
        initial_scale2_display=display_scale2  # Send formatted string
    )

@app.route('/scales', methods=['GET', 'POST'])
def get_scales():
    if request.method == 'POST':
        # Handle scale data posted from ESP32
        data = request.json
        
        # Get scale values from the JSON payload
        scale1 = data.get('scale1')
        scale2 = data.get('scale2')
        scale1_raw = data.get('scale1_raw')
        scale2_raw = data.get('scale2_raw')
        
        # Log received values for debugging
        print(f"DEBUG: Received scale data from ESP32: scale1={scale1}, scale2={scale2}, raw1={scale1_raw}, raw2={scale2_raw}")
        
        # Update the current scales in memory
        if scale1 is not None:
            app.current_scales['scale1'] = scale1
        if scale2 is not None:
            app.current_scales['scale2'] = scale2
            
        # Store raw values for calibration
        if scale1_raw is not None:
            app.last_raw_scales['scale1'] = scale1_raw
        if scale2_raw is not None:
            app.last_raw_scales['scale2'] = scale2_raw
            
        # Save raw values to database for persistence
        save_raw_scales(scale1_raw, scale2_raw)
        
        # Save the reading to the history database with timestamp
        timestamp = datetime.now().isoformat()
        save_scale_reading(timestamp, scale1, scale2, scale1_raw, scale2_raw)
        
        # Append to the in-memory history
        scale_history.append({
            'timestamp': timestamp,
            'scale1': scale1,
            'scale2': scale2,
            'scale1_raw': scale1_raw,
            'scale2_raw': scale2_raw
        })
        
        # Keep the history limited to MAX_SCALE_HISTORY entries
        if len(scale_history) > MAX_SCALE_HISTORY:
            scale_history.pop(0)
            
        return jsonify({'message': 'Scale data updated successfully'})
        
    # GET request - return current scale values
    return jsonify({
        'scale1': app.current_scales.get('scale1', 0),
        'scale2': app.current_scales.get('scale2', 0)
    })

@app.route('/schedule', methods=['GET', 'POST'])
def schedule():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    if request.method == 'POST':
        time = request.form.get('time')
        amount = request.form.get('amount')
        notes = request.form.get('notes')
        c.execute('''
            INSERT INTO feed_schedules (time, amount, notes)
            VALUES (?, ?, ?)
        ''', (time, amount, notes))
        conn.commit()

    c.execute('SELECT id, time, amount, notes FROM feed_schedules ORDER BY time')
    schedules = c.fetchall()
    conn.close()

    return render_template('schedule.html', schedules=schedules)

@app.route('/feed-now', methods=['POST'])
def feed_now():
    global current_status, feed_request
    data = request.json
    amount = data.get('amount', '50g') # Keep amount format consistent
    
    # Log the manual feed request as 'pending'
    # The /report-feed-complete endpoint will update this later
    log_feed(amount=amount, status="pending", notes="Manual Feed via Button")
    
    # Set feed request flag for ESP32
    feed_request['pending'] = True
    feed_request['amount'] = amount
    print(f"DEBUG: Manual feed request set and logged. Amount: {amount}")
    return jsonify({"message": f"Manual feed command sent for {amount}. ESP32 will pick it up."})

@app.route('/stats')
def stats():
    days = int(request.args.get('days', 7))
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Get daily amounts
    c.execute('''
        SELECT date(timestamp) as date, SUM(CAST(REPLACE(amount, 'g', '') AS INTEGER)) as total
        FROM feed_logs
        WHERE date(timestamp) >= ? AND date(timestamp) <= ?
        GROUP BY date(timestamp)
        ORDER BY date(timestamp)
    ''', (start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
    
    daily_data = c.fetchall()
    dates = [row[0] for row in daily_data]
    amounts = [row[1] for row in daily_data]
    
    # Get scale totals
    c.execute('''
        SELECT scale, SUM(CAST(REPLACE(amount, 'g', '') AS INTEGER)) as total
        FROM feed_logs
        WHERE date(timestamp) >= ? AND date(timestamp) <= ?
        GROUP BY scale
    ''', (start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
    
    scale_totals = {1: 0, 2: 0}
    for row in c.fetchall():
        scale_totals[row[0]] = row[1]
    
    # Get summary statistics
    c.execute('''
        SELECT COUNT(*) as count, 
               AVG(CAST(REPLACE(amount, 'g', '') AS INTEGER)) as avg_amount,
               SUM(CAST(REPLACE(amount, 'g', '') AS INTEGER)) as total_amount
        FROM feed_logs
        WHERE date(timestamp) >= ? AND date(timestamp) <= ?
    ''', (start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
    
    summary = c.fetchone()
    conn.close()
    
    return jsonify({
        'dates': dates,
        'amounts': amounts,
        'scale1_total': scale_totals[1],
        'scale2_total': scale_totals[2],
        'total_feedings': summary[0],
        'avg_amount': summary[1] or 0,
        'total_amount': summary[2] or 0
    })

@app.route('/stats-page')
def stats_page():
    return render_template('stats.html')

@app.route('/logs', methods=['GET'])
def get_logs():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, timestamp, amount, status, notes FROM feed_logs ORDER BY id DESC')
    rows = [dict(id=row[0], timestamp=row[1], amount=row[2], status=row[3], notes=row[4]) for row in c.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/report-feed-complete', methods=['POST'])
def report_feed_complete():
    global current_status
    data = request.json
    status = data.get('status')
    notes = data.get('notes', '')
    final_amount = data.get('final_amount')
    
    print(f"DEBUG: Received feed completion: status='{status}', notes='{notes}', final_amount={final_amount}")
    
    # Update overall system status based on ESP32 report
    if status == 'success':
        current_status['state'] = 'idle'
    else:
        current_status['state'] = 'error'

    # Update the specific log entry
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    log_id_updated = None # Keep track if we updated a log
    try:
        # Find the OLDEST log entry currently marked as 'processing'
        c.execute("""
            SELECT id FROM feed_logs 
            WHERE status = 'processing' 
            ORDER BY id ASC 
            LIMIT 1
        """)
        log_to_update = c.fetchone()
        
        if log_to_update:
            log_id = log_to_update[0]
            log_id_updated = log_id # Store the ID we are updating
            print(f"DEBUG: Updating log ID {log_id} with status='{status}'")
            
            # Create notes string for DB update
            db_notes = notes # Start with notes from ESP
            if final_amount is not None:
                db_notes += f" (Reported: {final_amount:.1f}g)"
            
            c.execute("""
        UPDATE feed_logs
        SET status = ?, notes = ?
                WHERE id = ?
            """, (status, db_notes, log_id))
            conn.commit()
            print(f"DEBUG: Log ID {log_id} updated successfully.")
        else:
            print("WARN: Received feed completion report, but no log entry was found in 'processing' state.")
            # Attempt to find the latest manual pending log as a fallback
            c.execute("""
                SELECT id FROM feed_logs 
                WHERE status = 'pending' AND notes = 'Manual Feed via Button'
        ORDER BY id DESC
        LIMIT 1
            """)
            manual_log = c.fetchone()
            if manual_log:
                log_id = manual_log[0]
                log_id_updated = log_id
                print(f"DEBUG: Found manual pending log ID {log_id}. Updating it.")
                # Create notes string for DB update
                db_notes = notes # Start with notes from ESP
                if final_amount is not None:
                    db_notes += f" (Reported: {final_amount:.1f}g)"
                
                c.execute("""
                    UPDATE feed_logs
                    SET status = ?, notes = ?
                    WHERE id = ?
                """, (status, db_notes, log_id))
                conn.commit()
                print(f"DEBUG: Manual Log ID {log_id} updated successfully.")
            else:
                 print("WARN: Also could not find a suitable manual pending log to update.")
            
    except Exception as e:
        print(f"ERROR: Database error updating log status: {e}")
        # Ensure rollback happens only if a connection exists
        if conn:
           conn.rollback() # Rollback changes on error
    finally:
        # Ensure connection closing happens only if a connection exists
        if conn:
            conn.close()
            print("DEBUG: Database connection closed in finally block.")

    # --- Send Push Notification (Always) --- 
    message = ""
    if status == 'success':
        if final_amount is not None:
            message = f"✅ Feed successful! Dispensed approx. {final_amount:.1f}g."
        else:
            message = f"✅ Feed successful! (Amount unreported)"
    elif status == 'error':
        message = f"❌ Feed failed! Reason: {notes}"
    else: # Handle unexpected status
        message = f"ℹ️ Feed status: {status}. Notes: {notes}"
    
    print(f"DEBUG: Sending push notification: {message}")
    try:
        with open(SUBSCRIPTIONS_DB, 'r') as f:
            subscriptions = json.load(f)
        
        if not subscriptions:
             print("DEBUG: No push subscriptions found to notify.")
             
        for sub in subscriptions:
            try:
                webpush(
                    subscription_info=sub,
                    data=message,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": "mailto:you@example.com"} # Example claim
                    )
                print(f"DEBUG: Push notification sent to: {sub.get('endpoint', 'N/A')[:30]}...")
            except WebPushException as ex:
                print(f"ERROR: WebPushException for {sub.get('endpoint', 'N/A')[:30]}...: {ex}")
                # Possible actions: Remove expired subscriptions?
                # if ex.response and ex.response.status_code == 410:
                #    print("Subscription expired or invalid.")
            except Exception as e: # Catch other potential errors during webpush call
                 print(f"ERROR: Unexpected error sending push to {sub.get('endpoint', 'N/A')[:30]}...: {e}")
                
    except FileNotFoundError:
        print("WARN: subscriptions.json file not found. Cannot send push notifications.")
    except Exception as e:
        print(f"ERROR: Failed to load or send push notifications: {e}")

    return jsonify({"message": "Feed completion status received"})

@app.route('/status', methods=['GET'])
def get_status():
    return jsonify(current_status)

@app.route('/delete-schedule/<int:schedule_id>', methods=['POST'])
def delete_schedule(schedule_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM feed_schedules WHERE id = ?', (schedule_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Schedule deleted"})

@app.route('/subscribe', methods=['POST'])
def subscribe():
    subscription = request.json
    try:
        with open(SUBSCRIPTIONS_DB, 'r') as f:
            subs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        subs = []

    endpoints = [s.get("endpoint") for s in subs]
    if subscription.get("endpoint") not in endpoints:
        subs.append(subscription)
        with open(SUBSCRIPTIONS_DB, 'w') as f:
            json.dump(subs, f)
        return jsonify({'message': 'Subscribed successfully'})
    else:
        return jsonify({'message': 'Already subscribed'})

@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)

@app.route('/devices')
def devices():
    try:
        with open(SUBSCRIPTIONS_DB, 'r') as f:
            subs = json.load(f)
    except:
        subs = []

    rows = list(enumerate(subs))
    return render_template('devices.html', rows=rows)

@app.route('/delete-sub/<int:idx>', methods=['POST'])
def delete_sub(idx):
    try:
        with open(SUBSCRIPTIONS_DB, 'r') as f:
            subs = json.load(f)
    except:
        subs = []
    if 0 <= idx < len(subs):
        subs.pop(idx)
        with open(SUBSCRIPTIONS_DB, 'w') as f:
            json.dump(subs, f)
        return jsonify({'message': 'Device removed'})
    return jsonify({'message': 'Index out of range'}), 404

@app.route('/update-weight', methods=['POST'])
def update_weight():
    current_time = time.time()
    data = request.json
    # Get raw values using the keys the ESP32 sends
    raw_weight1 = data.get('weight1_raw')
    raw_weight2 = data.get('weight2_raw')
    # Get gram values (already calculated by ESP32? or just labels? Assuming these are the ones to calibrate)
    gram_weight1 = data.get('weight1_g')
    gram_weight2 = data.get('weight2_g')

    # Debug logging
    print(f"DEBUG: RECEIVED RAW DATA: raw1={raw_weight1}, raw2={raw_weight2}, gram1={gram_weight1}, gram2={gram_weight2}")

    # Store the actual raw weights from ESP32 for calibration in memory
    app.last_raw_scales['scale1'] = raw_weight1
    app.last_raw_scales['scale2'] = raw_weight2
    
    # Also save to database for persistence across workers
    save_raw_scales(raw_weight1, raw_weight2)
    
    # Store raw values in history for filtering
    if raw_weight1 is not None:
        app.scale_history_raw['scale1'].append(float(raw_weight1))
        if len(app.scale_history_raw['scale1']) > MAX_HISTORY_RAW:
            app.scale_history_raw['scale1'].pop(0)
            
    if raw_weight2 is not None:
        app.scale_history_raw['scale2'].append(float(raw_weight2))
        if len(app.scale_history_raw['scale2']) > MAX_HISTORY_RAW:
            app.scale_history_raw['scale2'].pop(0)

    # Apply calibration factors to the gram values reported
    # (Assuming ESP32 does its own raw -> g conversion that we calibrate)
    factor1 = app.calibration_factors.get(1)
    factor2 = app.calibration_factors.get(2)
    
    print(f"DEBUG: CALIBRATION FACTORS: factor1={factor1}, factor2={factor2}")
    print(f"DEBUG: ZERO OFFSETS: offset1={app.scale_zero_offsets[1]}, offset2={app.scale_zero_offsets[2]}")

    # CRITICAL PATH - Check if calibration factors are missing
    if factor1 is None or factor1 == 0:
        print("WARNING: Scale 1 has no valid calibration factor - using gram values from ESP32")
        factor1 = 1  # Use a default to prevent division by zero
        calibrated_weight1 = gram_weight1  # Fall back to ESP32's value
        app.current_scales['scale1'] = calibrated_weight1
    
    if factor2 is None or factor2 == 0:
        print("WARNING: Scale 2 has no valid calibration factor - using gram values from ESP32")
        factor2 = 1  # Use a default to prevent division by zero
        calibrated_weight2 = gram_weight2  # Fall back to ESP32's value
        app.current_scales['scale2'] = calibrated_weight2
    
    # Continue with normal processing if factors exist

    # For Scale 1 - Using raw values and applying a sanity check
    # Initialize with the existing value to preserve it if calculation fails
    calibrated_weight1 = app.current_scales.get('scale1', 0)  # Default to current value instead of 0
    
    if raw_weight1 is not None and factor1 is not None and factor1 != 0:
        try:
            # Calculate average of last few readings to smooth out fluctuations
            if app.features['use_weight_filtering'] and app.scale_history_raw['scale1']:
                # Remove extreme outliers
                filtered_values = [v for v in app.scale_history_raw['scale1'] 
                                  if abs(v - sum(app.scale_history_raw['scale1'])/len(app.scale_history_raw['scale1'])) < OUTLIER_THRESHOLD]
                if filtered_values:
                    # Use average of filtered values
                    raw_value1 = sum(filtered_values) / len(filtered_values)
                else:
                    raw_value1 = float(raw_weight1)
                
            # Apply zero offset (from tare)
            if app.features['use_zero_offsets']:
                raw_value1 -= app.scale_zero_offsets[1]
                print(f"DEBUG: Applied zero offset: {app.scale_zero_offsets[1]} to Scale 1")
            
            # Calculate calibrated weight
            calibrated_weight1 = raw_value1 / factor1
            
            # Apply zero threshold - if reading is very small, treat as zero
            if abs(calibrated_weight1) < ZERO_THRESHOLD:
                calibrated_weight1 = 0
                print(f"DEBUG: Scale 1 below threshold ({ZERO_THRESHOLD}g), setting to zero")
                # Reset negative counter when we're at zero
                app.negative_reading_counter['scale1'] = 0
            else:
                # Check for persistent negative readings
                if calibrated_weight1 < 0:
                    app.negative_reading_counter['scale1'] += 1
                    print(f"DEBUG: Scale 1 negative reading #{app.negative_reading_counter['scale1']}")
                    
                    # If several consecutive negative readings and haven't auto-tared recently
                    if (app.features['use_auto_tare'] and 
                        app.negative_reading_counter['scale1'] >= NEGATIVE_TARE_THRESHOLD and 
                        current_time - app.last_auto_tare['scale1'] > 10):  # No more than once every 10 seconds
                        
                        # Auto-tare: adjust the zero offset
                        app.scale_zero_offsets[1] = float(raw_weight1)
                        print(f"DEBUG: AUTO-TARE Scale 1: Setting zero offset to {app.scale_zero_offsets[1]}")
                        
                        # Save to database for persistence
                        save_zero_offset(1, float(raw_weight1))
                        
                        # Recalculate with new offset
                        if app.features['use_zero_offsets']:
                            raw_value1 -= app.scale_zero_offsets[1]
                        calibrated_weight1 = raw_value1 / factor1
                        
                        # Record the auto-tare time
                        app.last_auto_tare['scale1'] = current_time
                        app.negative_reading_counter['scale1'] = 0
                else:
                    # Reset negative counter when positive
                    app.negative_reading_counter['scale1'] = 0
                
                # Round small weights to improve readability
                if abs(calibrated_weight1) < 100:
                    calibrated_weight1 = round(calibrated_weight1, 1)
                else:
                    calibrated_weight1 = round(calibrated_weight1)
            
            # Sanity check - discard extreme values
            if calibrated_weight1 < -MAX_WEIGHT or calibrated_weight1 > MAX_WEIGHT:
                print(f"DEBUG: Scale 1 value {calibrated_weight1}g is outside reasonable range, using filtered value")
                # Get last few values from history
                recent_values1 = [entry['scale1'] for entry in scale_history[-5:] 
                                 if entry['scale1'] is not None and -MAX_WEIGHT < entry['scale1'] < MAX_WEIGHT]
                if recent_values1:
                    # Use median of recent values as a more stable approach
                    calibrated_weight1 = sorted(recent_values1)[len(recent_values1)//2]
                    print(f"DEBUG: Using median of recent values: {calibrated_weight1}g")
                else:
                    # Don't reset to 0 if there are no recent good values
                    # Keep the existing value instead
                    print(f"DEBUG: No recent good values, keeping current value: {calibrated_weight1}g")
            
            print(f"DEBUG: Final Scale 1: raw={raw_weight1}, adjusted={raw_value1}, factor={factor1}, final={calibrated_weight1}")
            
            # Weight stability tracking
            if app.features['use_stability_tracking']:
                # Check if this reading is close to the last stable reading
                if abs(calibrated_weight1 - app.last_stable_weights['scale1']) < 5:
                    app.weight_stability_count['scale1'] += 1
                    print(f"DEBUG: Scale 1 stability increased: {app.weight_stability_count['scale1']}/{WEIGHT_STABILITY_THRESHOLD}")
                    if app.weight_stability_count['scale1'] >= WEIGHT_STABILITY_THRESHOLD:
                        # We have a stable reading, update last stable weight
                        app.last_stable_weights['scale1'] = calibrated_weight1
                        print(f"DEBUG: Scale 1 stable value updated: {calibrated_weight1}g")
                else:
                    # The reading has changed significantly
                    # Only accept the new reading if it persists
                    app.weight_stability_count['scale1'] = 1
                    print(f"DEBUG: Scale 1 stability reset: new value {calibrated_weight1}g vs old stable {app.last_stable_weights['scale1']}g")
                    
                # Use last stable weight if it exists and current reading is unstable
                if app.last_stable_weights['scale1'] != 0 and app.weight_stability_count['scale1'] < WEIGHT_STABILITY_THRESHOLD:
                    print(f"DEBUG: Scale 1 using last stable weight: {app.last_stable_weights['scale1']}g instead of {calibrated_weight1}g")
                    calibrated_weight1 = app.last_stable_weights['scale1']
            
        except (ValueError, TypeError) as e:
            print(f"DEBUG: Error calculating Scale 1: {e}")
            # Keep the existing value instead of resetting to 0
            calibrated_weight1 = app.current_scales.get('scale1', 0)
            print(f"DEBUG: Keeping existing value for Scale 1: {calibrated_weight1}g")
    
    # For Scale 2 - same approach with preservation of existing values
    # Initialize with the existing value to preserve it if calculation fails
    calibrated_weight2 = app.current_scales.get('scale2', 0)  # Default to current value instead of 0
    
    if raw_weight2 is not None and factor2 is not None and factor2 != 0:
        try:
            # Calculate average of last few readings to smooth out fluctuations
            if app.features['use_weight_filtering'] and app.scale_history_raw['scale2']:
                # Remove extreme outliers
                filtered_values = [v for v in app.scale_history_raw['scale2'] 
                                 if abs(v - sum(app.scale_history_raw['scale2'])/len(app.scale_history_raw['scale2'])) < OUTLIER_THRESHOLD]
                if filtered_values:
                    # Use average of filtered values
                    raw_value2 = sum(filtered_values) / len(filtered_values)
                else:
                    raw_value2 = float(raw_weight2)
                
            # Apply zero offset (from tare)
            if app.features['use_zero_offsets']:
                raw_value2 -= app.scale_zero_offsets[2]
                print(f"DEBUG: Applied zero offset: {app.scale_zero_offsets[2]} to Scale 2")
            
            # Calculate calibrated weight
            calibrated_weight2 = raw_value2 / factor2
            
            # Apply zero threshold - if reading is very small, treat as zero
            if abs(calibrated_weight2) < ZERO_THRESHOLD:
                calibrated_weight2 = 0
                print(f"DEBUG: Scale 2 below threshold ({ZERO_THRESHOLD}g), setting to zero")
                # Reset negative counter when we're at zero
                app.negative_reading_counter['scale2'] = 0
            else:
                # Check for persistent negative readings
                if calibrated_weight2 < 0:
                    app.negative_reading_counter['scale2'] += 1
                    print(f"DEBUG: Scale 2 negative reading #{app.negative_reading_counter['scale2']}")
                    
                    # If several consecutive negative readings and haven't auto-tared recently
                    if (app.features['use_auto_tare'] and 
                        app.negative_reading_counter['scale2'] >= NEGATIVE_TARE_THRESHOLD and 
                        current_time - app.last_auto_tare['scale2'] > 10):  # No more than once every 10 seconds
                        
                        # Auto-tare: adjust the zero offset
                        app.scale_zero_offsets[2] = float(raw_weight2)
                        print(f"DEBUG: AUTO-TARE Scale 2: Setting zero offset to {app.scale_zero_offsets[2]}")
                        
                        # Save to database for persistence
                        save_zero_offset(2, float(raw_weight2))
                        
                        # Recalculate with new offset
                        if app.features['use_zero_offsets']:
                            raw_value2 -= app.scale_zero_offsets[2]
                        calibrated_weight2 = raw_value2 / factor2
                        
                        # Record the auto-tare time
                        app.last_auto_tare['scale2'] = current_time
                        app.negative_reading_counter['scale2'] = 0
                else:
                    # Reset negative counter when positive
                    app.negative_reading_counter['scale2'] = 0
                
                # Round small weights to improve readability
                if abs(calibrated_weight2) < 100:
                    calibrated_weight2 = round(calibrated_weight2, 1)
                else:
                    calibrated_weight2 = round(calibrated_weight2)
            
            # Sanity check - discard extreme values
            if calibrated_weight2 < -MAX_WEIGHT or calibrated_weight2 > MAX_WEIGHT:
                print(f"DEBUG: Scale 2 value {calibrated_weight2}g is outside reasonable range, using filtered value")
                # Get last few values from history
                recent_values2 = [entry['scale2'] for entry in scale_history[-5:] 
                                 if entry['scale2'] is not None and -MAX_WEIGHT < entry['scale2'] < MAX_WEIGHT]
                if recent_values2:
                    # Use median of recent values as a more stable approach
                    calibrated_weight2 = sorted(recent_values2)[len(recent_values2)//2]
                    print(f"DEBUG: Using median of recent values: {calibrated_weight2}g")
                else:
                    # Don't reset to 0 if there are no recent good values
                    # Keep the existing value instead
                    print(f"DEBUG: No recent good values, keeping current value: {calibrated_weight2}g")
            
            print(f"DEBUG: Final Scale 2: raw={raw_weight2}, adjusted={raw_value2}, factor={factor2}, final={calibrated_weight2}")
            
            # Weight stability tracking
            if app.features['use_stability_tracking']:
                # Check if this reading is close to the last stable reading
                if abs(calibrated_weight2 - app.last_stable_weights['scale2']) < 5:
                    app.weight_stability_count['scale2'] += 1
                    print(f"DEBUG: Scale 2 stability increased: {app.weight_stability_count['scale2']}/{WEIGHT_STABILITY_THRESHOLD}")
                    if app.weight_stability_count['scale2'] >= WEIGHT_STABILITY_THRESHOLD:
                        # We have a stable reading, update last stable weight
                        app.last_stable_weights['scale2'] = calibrated_weight2
                        print(f"DEBUG: Scale 2 stable value updated: {calibrated_weight2}g")
                else:
                    # The reading has changed significantly
                    # Only accept the new reading if it persists
                    app.weight_stability_count['scale2'] = 1
                    print(f"DEBUG: Scale 2 stability reset: new value {calibrated_weight2}g vs old stable {app.last_stable_weights['scale2']}g")
                    
                # Use last stable weight if it exists and current reading is unstable
                if app.last_stable_weights['scale2'] != 0 and app.weight_stability_count['scale2'] < WEIGHT_STABILITY_THRESHOLD:
                    print(f"DEBUG: Scale 2 using last stable weight: {app.last_stable_weights['scale2']}g instead of {calibrated_weight2}g")
                    calibrated_weight2 = app.last_stable_weights['scale2']
            
        except (ValueError, TypeError) as e:
            print(f"DEBUG: Error calculating Scale 2: {e}")
            # Keep the existing value instead of resetting to 0
            calibrated_weight2 = app.current_scales.get('scale2', 0)
            print(f"DEBUG: Keeping existing value for Scale 2: {calibrated_weight2}g")
    
    print(f"DEBUG: Final calibrated weights: scale1={calibrated_weight1}, scale2={calibrated_weight2}")

    # Update displayed/historical scales with *calibrated* values
    app.current_scales['scale1'] = calibrated_weight1
    app.current_scales['scale2'] = calibrated_weight2

    # Add to history (store calibrated weights)
    scale_history.append({
        'timestamp': datetime.now().isoformat(),
        'scale1': calibrated_weight1,
        'scale2': calibrated_weight2,
        'scale1_raw': raw_weight1 if raw_weight1 is not None else None,
        'scale2_raw': raw_weight2 if raw_weight2 is not None else None
    })
    if len(scale_history) > MAX_SCALE_HISTORY:
        scale_history.pop(0)
        
    # Save reading to database for persistence across workers
    timestamp = datetime.now().isoformat()
    save_scale_reading(timestamp, calibrated_weight1, calibrated_weight2, raw_weight1, raw_weight2)
    
    # Save zero offset to database
    save_zero_offset(1, app.scale_zero_offsets[1])
    save_zero_offset(2, app.scale_zero_offsets[2])
    
    return jsonify({'message': 'Weights updated'})

@app.route('/scale-readings')
def scale_readings():
    # Get readings from database instead of memory
    readings = get_scale_readings()
    return jsonify(readings)

@app.route('/check-feed-request', methods=['GET'])
def check_feed_request():
    global feed_request
    current_timestamp = int(time.time())
    
    # 1. Check manual feed request flag first
    if feed_request['pending']:
        print(f"DEBUG: Handling manual feed request flag. Timestamp: {current_timestamp}")
        response = {
            'feed': True, 
            'amount': feed_request['amount'],
            'timestamp': current_timestamp
        }
        # Clear the manual request flag
        feed_request['pending'] = False
        feed_request['amount'] = None
        return jsonify(response)
        
    # 2. If no manual flag, check database for scheduled 'pending' requests
    else:
        conn = None # Initialize conn to None
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            # Find the oldest pending request
            c.execute("""
                SELECT id, amount 
                FROM feed_logs 
                WHERE status = 'pending' 
                ORDER BY timestamp ASC 
                LIMIT 1
            """)
            pending_log = c.fetchone()
            
            if pending_log:
                log_id, amount_str = pending_log
                print(f"DEBUG: Handling pending log ID: {log_id}, Amount: {amount_str}, Timestamp: {current_timestamp}")
                
                # Extract numeric amount (assuming format like '50g')
                try:
                    amount = int(amount_str.lower().replace('g',''))
                except ValueError: # More specific exception
                    amount = 50 # Default if parsing fails
                    print(f"WARN: Could not parse amount '{amount_str}', using default {amount}g")

                # Update status to 'processing' to prevent re-triggering immediately
                c.execute("""
                    UPDATE feed_logs 
                    SET status = 'processing', notes = notes || ' (Sent to ESP)'
                    WHERE id = ?
                """, (log_id,))
                conn.commit()
                
                # Return feed request based on the database entry
                response = {
                    'feed': True, 
                    'amount': str(amount) + 'g', # Return amount string like ESP expects
                    'timestamp': current_timestamp # Add timestamp
                }
                # We don't close conn here because we are returning
                return jsonify(response)
            else:
                # No manual flag and no pending logs
                # We don't close conn here because we are returning
                response = {'feed': False}
                # NOTE: conn.close() will happen in finally block
                
        except sqlite3.Error as e: # Catch specific SQLite errors
            print(f"ERROR: Database error in /check-feed-request: {e}")
            response = {'feed': False, 'error': str(e)} # Return error
        except Exception as e: # Catch other unexpected errors
            print(f"ERROR: Unexpected error in /check-feed-request: {e}")
            response = {'feed': False, 'error': str(e)} # Return error
        finally:
             # Ensure connection is closed if it was opened
            if conn:
                conn.close()
                print("DEBUG: Database connection closed in /check-feed-request finally block.")
        
        return jsonify(response) # Return the response AFTER finally block

@app.route('/dev-options')
def dev_options():
    return render_template('dev_options.html')

@app.route('/tare-scale/<int:scale_id>', methods=['POST'])
def tare_scale(scale_id):
    # Instead of asking ESP32 to tare, we'll store the current raw reading as an offset
    if scale_id not in [1, 2]:
        return jsonify({'message': 'Invalid scale ID.'}), 400
    
    # Get the current raw value
    raw_key = f'scale{scale_id}'
    raw_value = app.last_raw_scales.get(raw_key)
    
    if raw_value is None:
        return jsonify({'message': f'No recent readings for Scale {scale_id} to tare with.'}), 400
    
    try:
        # Store the current raw value as the zero offset
        app.scale_zero_offsets[scale_id] = float(raw_value)
        print(f"DEBUG: Tare Scale {scale_id} - Setting zero offset to {app.scale_zero_offsets[scale_id]}")
        
        # Save to database for persistence
        save_zero_offset(scale_id, float(raw_value))
        
        # Also send the tare request to the ESP32 as a backup
        tare_request[scale_id] = True
        
        return jsonify({'message': f'Scale {scale_id} tared (zeroed). Zero offset set to {app.scale_zero_offsets[scale_id]}'})
    except (ValueError, TypeError) as e:
        return jsonify({'message': f'Error during tare: {str(e)}'}), 400

@app.route('/tare-request/<int:scale_id>', methods=['POST'])
def tare_request_endpoint(scale_id):
    if scale_id in tare_request:
        # Set the tare flag for the ESP32
        tare_request[scale_id] = True
        
        # Also set zero offset in our software
        raw_key = f'scale{scale_id}'
        raw_value = app.last_raw_scales.get(raw_key)
        
        if raw_value is not None:
            try:
                app.scale_zero_offsets[scale_id] = float(raw_value)
                print(f"DEBUG: Tare Request for Scale {scale_id} - Setting zero offset to {app.scale_zero_offsets[scale_id]}")
                
                # Save to database for persistence
                save_zero_offset(scale_id, float(raw_value))
                
            except (ValueError, TypeError) as e:
                print(f"DEBUG: Error setting zero offset during tare request: {e}")
        
        return jsonify({'message': f'Tare request for scale {scale_id} sent to ESP32 and software offset applied.'})
    else:
        return jsonify({'message': 'Invalid scale ID.'}), 400

@app.route('/check-tare-request', methods=['GET'])
def check_tare_request():
    # ESP32 polls this endpoint
    global tare_request
    # Read current status
    tare1_status = tare_request[1]
    tare2_status = tare_request[2]
    response = {'tare1': tare1_status, 'tare2': tare2_status}
    # Clear the requests ONLY if they were true when read
    if tare1_status:
        tare_request[1] = False
    if tare2_status:
        tare_request[2] = False
    return jsonify(response)

@app.route('/calibrate-scale/<int:scale_id>', methods=['POST'])
def calibrate_scale(scale_id):
    data = request.json
    known_weight = float(data.get('known_weight', 0))
    
    # First try to get raw value from memory
    raw_key = f'scale{scale_id}'
    raw = app.last_raw_scales.get(raw_key)
    
    # If not available in memory, try to get from database
    if raw is None:
        db_raw1, db_raw2 = load_raw_scales()
        if scale_id == 1 and db_raw1 is not None:
            raw = db_raw1
            print(f"DEBUG: Retrieved raw value for Scale 1 from database: {raw}")
        elif scale_id == 2 and db_raw2 is not None:
            raw = db_raw2
            print(f"DEBUG: Retrieved raw value for Scale 2 from database: {raw}")
    
    print(f"DEBUG: Calibration for Scale {scale_id} - Raw value: {raw}, Known weight: {known_weight}g")

    # For the case where raw values are not available but we have gram values
    # from the ESP32, we can create a calibration factor that ensures these 
    # gram values are used directly
    if raw is None:
        # Get the current gram value from ESP32
        current_value = app.current_scales.get(f'scale{scale_id}')
        if current_value is not None and current_value != 0 and known_weight != 0:
            # Create a factor that makes the ESP32's gram value match our known weight
            factor = current_value / known_weight
            
            print(f"DEBUG: No raw value available for Scale {scale_id}. Using current reading to create calibration factor.")
            print(f"DEBUG: Current value: {current_value}g, Known weight: {known_weight}g, Factor: {factor}")
            
            # Update the factor in memory
            app.calibration_factors[scale_id] = factor
            
            # Save factor to database
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO calibration_settings (scale_id, factor)
                VALUES (?, ?)
            ''', (scale_id, factor))
            conn.commit()
            conn.close()
            
            return jsonify({'message': f'Scale {scale_id} calibrated using current reading. Factor: {factor:.4f}'})
        else:
            return jsonify({'message': 'No raw or current reading available for calibration.'}), 400
    
    # Continue with original calibration logic for raw values
    try:
        raw_value = float(raw)
        if raw_value == 0 or known_weight == 0:
            return jsonify({'message': 'Invalid data. Ensure raw scale reading and known weight are non-zero.'}), 400
        
        # Ask for empty scale reading first (auto-tare)
        # Since we don't have that in this flow, we'll use the current offset
        # as the tare value. This offset was set when the user pressed "Tare"
        offset = app.scale_zero_offsets[scale_id]
        
        # Apply the offset to get the true weight-only raw value
        adjusted_raw = raw_value - offset
            
        factor = adjusted_raw / known_weight
        
        # Sanity check for factor
        if abs(factor) < 0.001 or abs(factor) > 100000:
            return jsonify({'message': f'Calculated factor {factor} is outside reasonable range. Check the scale readings and known weight value.'}), 400
            
        print(f"DEBUG: Calculated factor: ({raw_value} - {offset}) / {known_weight} = {factor}")
        
        # Update the factor in memory
        app.calibration_factors[scale_id] = factor
        
        # Save factor to database
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO calibration_settings (scale_id, factor)
            VALUES (?, ?)
        ''', (scale_id, factor))
        conn.commit()
        conn.close()
        
        return jsonify({'message': f'Scale {scale_id} calibrated. Factor: {factor:.4f}. Remember to use Tare when scale is empty.'})
    except (ValueError, TypeError) as e:
        print(f"DEBUG: Error during calibration: {e}")
        return jsonify({'message': f'Error during calibration: {str(e)}'}), 400

@app.route('/calibration-factors')
def get_calibration_factors():
    # Always read directly from the database instead of app.calibration_factors
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT scale_id, factor FROM calibration_settings')
    factors = {row[0]: row[1] for row in c.fetchall()}
    conn.close()
    
    # Format response in the same way as before
    response = {
        "1": factors.get(1),
        "2": factors.get(2)
    }
    return jsonify(response)

@app.route('/motor-test', methods=['POST'])
def motor_test():
    data = request.json
    direction = data.get('direction')
    duration = int(data.get('duration', 2))
    if direction not in ['forward', 'reverse'] or not (1 <= duration <= 10):
        return jsonify({'message': 'Invalid direction or duration.'}), 400
    motor_test_request['pending'] = True
    motor_test_request['direction'] = direction
    motor_test_request['duration'] = duration
    return jsonify({'message': f'Motor test ({direction}, {duration}s) requested.'})

@app.route('/check-motor-test', methods=['GET'])
def check_motor_test():
    # Removed: global motor_test_request (using file flag instead)
    
    # Log the check
    print(f"DEBUG /check-motor-test: Checking for flag file: {MOTOR_TEST_FLAG_FILE}")
    
    try:
        if os.path.exists(MOTOR_TEST_FLAG_FILE):
            print(f"DEBUG /check-motor-test: Flag file found.")
            details = {}
            try:
                with open(MOTOR_TEST_FLAG_FILE, 'r') as f:
                    details = json.load(f)
            except Exception as e:
                print(f"ERROR: Failed to read/parse flag file {MOTOR_TEST_FLAG_FILE}: {e}")
                # Attempt to delete corrupted/unreadable file
                try:
                    os.remove(MOTOR_TEST_FLAG_FILE)
                except OSError as del_e:
                     print(f"ERROR: Failed to delete potentially corrupted flag file {MOTOR_TEST_FLAG_FILE}: {del_e}")
                return jsonify({'test': False, 'error': 'Failed to read flag file'})

            # We have the details, now delete the flag file
            try:
                os.remove(MOTOR_TEST_FLAG_FILE)
                print(f"DEBUG /check-motor-test: Flag file deleted.")
            except OSError as del_e:
                print(f"ERROR: Failed to delete flag file {MOTOR_TEST_FLAG_FILE} after reading: {del_e}")
                # Proceed with sending command anyway, but log the error
            
            # Prepare response for ESP32
            resp = {
                'test': True,
                'direction': details.get('direction', 'forward'), # Default if missing
                'duration': details.get('duration', 2) # Default if missing
            }
            print(f"DEBUG /check-motor-test: Sending response to ESP32: {resp}")
            return jsonify(resp)
        else:
            # Flag file does not exist
            print(f"DEBUG /check-motor-test: Flag file not found. Returning test:false.")
            return jsonify({'test': False})
            
    except Exception as e:
        # Catch any other unexpected errors during file check/deletion
        print(f"ERROR: Unexpected error in /check-motor-test: {e}")
        return jsonify({'test': False, 'error': str(e)})

@app.route('/esp32-status', methods=['GET'])
def get_esp32_status():
    return jsonify(esp32_status)

@app.route('/esp32-status-update', methods=['POST'])
def update_esp32_status():
    data = request.json
    esp32_status['ip'] = data.get('ip')
    esp32_status['rssi'] = data.get('rssi')
    esp32_status['firmware'] = data.get('firmware')
    
    # Store scale values from ESP32 status update if provided
    scale1 = data.get('scale1')
    scale2 = data.get('scale2')
    if scale1 is not None:
        print(f"DEBUG: Received scale1 from ESP32 status: {scale1}g")
        # Only update if we're not overriding with server-side calculation
        if app.current_scales['scale1'] is None or app.current_scales['scale1'] == 0:
            app.current_scales['scale1'] = scale1
    
    if scale2 is not None:
        print(f"DEBUG: Received scale2 from ESP32 status: {scale2}g")
        # Only update if we're not overriding with server-side calculation
        if app.current_scales['scale2'] is None or app.current_scales['scale2'] == 0:
            app.current_scales['scale2'] = scale2
    
    from datetime import datetime
    esp32_status['last_checkin'] = datetime.now().isoformat()
    return jsonify({'message': 'Status updated'})

@app.route('/esp32-restart', methods=['POST'])
def restart_esp32():
    # Set a flag that ESP32 will check for in check_and_restart_esp32
    with open('esp32_restart_flag.txt', 'w') as f:
        f.write('1')
    return jsonify({'message': 'ESP32 restart requested. Will restart on next check-in.'})

@app.route('/check-esp32-restart', methods=['GET'])
def check_restart_esp32():
    try:
        # Check if the restart flag file exists
        restart_requested = False
        try:
            with open('esp32_restart_flag.txt', 'r') as f:
                restart_requested = True
            # Delete the flag file after reading it
            import os
            os.remove('esp32_restart_flag.txt')
        except FileNotFoundError:
            pass
        
        return jsonify({"restart": restart_requested})
    except Exception as e:
        print(f"Error checking restart flag: {e}")
        return jsonify({"restart": False})

# Add a new API endpoint to toggle features
@app.route('/toggle-feature', methods=['POST'])
def toggle_feature():
    data = request.json
    feature_name = data.get('feature')
    new_state = data.get('enabled')
    
    if feature_name not in app.features:
        return jsonify({'message': f'Unknown feature: {feature_name}'}), 400
    
    if new_state is None:
        # If no new state provided, toggle the current state
        app.features[feature_name] = not app.features[feature_name]
    else:
        # Otherwise, set to the specified state
        app.features[feature_name] = bool(new_state)
    
    print(f"DEBUG: Feature '{feature_name}' set to {app.features[feature_name]}")
    return jsonify({
        'message': f"Feature '{feature_name}' {'enabled' if app.features[feature_name] else 'disabled'}",
        'features': app.features
    })

@app.route('/features', methods=['GET'])
def get_features():
    return jsonify({
        'features': app.features,
        'constants': {
            'ZERO_THRESHOLD': ZERO_THRESHOLD,
            'MAX_HISTORY_RAW': MAX_HISTORY_RAW,
            'MAX_WEIGHT': MAX_WEIGHT,
            'OUTLIER_THRESHOLD': OUTLIER_THRESHOLD,
            'NEGATIVE_TARE_THRESHOLD': NEGATIVE_TARE_THRESHOLD,
            'WEIGHT_STABILITY_THRESHOLD': WEIGHT_STABILITY_THRESHOLD
        }
    })

@app.route('/weight-history')
def weight_history_page():
    """Page showing historical weight data with charts"""
    return render_template('weight_history.html')

# Function to save a scale reading to the database
def save_scale_reading(timestamp, scale1, scale2, scale1_raw, scale2_raw):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Convert raw values to strings for storage if they're not None
        scale1_raw_str = str(scale1_raw) if scale1_raw is not None else None
        scale2_raw_str = str(scale2_raw) if scale2_raw is not None else None
        
        # Insert the reading
        c.execute('''
            INSERT INTO scale_history (timestamp, scale1, scale2, scale1_raw, scale2_raw)
            VALUES (?, ?, ?, ?, ?)
        ''', (timestamp, scale1, scale2, scale1_raw_str, scale2_raw_str))
        
        # Limit the number of readings stored
        c.execute('''
            DELETE FROM scale_history 
            WHERE id NOT IN (
                SELECT id FROM scale_history 
                ORDER BY timestamp DESC 
                LIMIT ?
            )
        ''', (MAX_SCALE_HISTORY,))
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error saving scale reading: {e}")
        return False

# Function to get scale readings from the database
def get_scale_readings():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Get the most recent readings
        c.execute('''
            SELECT timestamp, scale1, scale2, scale1_raw, scale2_raw 
            FROM scale_history 
            ORDER BY timestamp ASC 
            LIMIT ?
        ''', (MAX_SCALE_HISTORY,))
        
        readings = []
        for row in c.fetchall():
            # Convert raw values back from strings to appropriate types
            scale1_raw = row[3]
            scale2_raw = row[4]
            if scale1_raw and scale1_raw.lower() != 'null' and scale1_raw.lower() != 'none':
                try:
                    scale1_raw = int(scale1_raw)
                except ValueError:
                    pass
            else:
                scale1_raw = None
                
            if scale2_raw and scale2_raw.lower() != 'null' and scale2_raw.lower() != 'none':
                try:
                    scale2_raw = int(scale2_raw)
                except ValueError:
                    pass
            else:
                scale2_raw = None
            
            readings.append({
                'timestamp': row[0],
                'scale1': row[1],
                'scale2': row[2],
                'scale1_raw': scale1_raw,
                'scale2_raw': scale2_raw
            })
        
        conn.close()
        return readings
    except Exception as e:
        print(f"Error getting scale readings: {e}")
        return []

@app.route('/debug-calibration', methods=['GET'])
def debug_calibration():
    """Debug endpoint to view raw values, calibration factors, and do test calculations"""
    # Get the raw values
    raw1 = app.last_raw_scales.get('scale1')
    raw2 = app.last_raw_scales.get('scale2')
    
    # Get calibration factors
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT scale_id, factor FROM calibration_settings')
    factors = {row[0]: row[1] for row in c.fetchall()}
    factor1 = factors.get(1)
    factor2 = factors.get(2)
    
    # Get zero offsets
    offset1 = app.scale_zero_offsets.get(1, 0)
    offset2 = app.scale_zero_offsets.get(2, 0)
    
    # Also get zero offsets from database for verification
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='zero_offsets'")
    if c.fetchone():
        c.execute('SELECT scale_id, offset_value, timestamp FROM zero_offsets')
        db_offsets = {row[0]: {'value': row[1], 'timestamp': row[2]} for row in c.fetchall()}
    else:
        db_offsets = {}
    
    # Calculate what weights would be with these values
    calc1 = None
    calc2 = None
    
    if raw1 is not None and factor1 is not None and factor1 != 0:
        try:
            adjusted_raw1 = float(raw1) - offset1
            calc1 = adjusted_raw1 / factor1
        except:
            calc1 = "Error calculating"
    
    if raw2 is not None and factor2 is not None and factor2 != 0:
        try:
            adjusted_raw2 = float(raw2) - offset2
            calc2 = adjusted_raw2 / factor2
        except:
            calc2 = "Error calculating"
    
    # Calculate what factors would be needed to get 73g from current raw values
    target_factor1 = None
    target_factor2 = None
    target_weight = 73.0
    
    if raw1 is not None and raw1 != 0:
        try:
            adjusted_raw1 = float(raw1) - offset1
            target_factor1 = adjusted_raw1 / target_weight
        except:
            target_factor1 = "Error calculating"
    
    if raw2 is not None and raw2 != 0:
        try:
            adjusted_raw2 = float(raw2) - offset2
            target_factor2 = adjusted_raw2 / target_weight
        except:
            target_factor2 = "Error calculating"
    
    # Get recent scale readings for analysis
    readings = get_scale_readings()
    recent_readings = readings[-10:] if len(readings) > 10 else readings
    
    return jsonify({
        'raw_values': {
            'scale1': raw1,
            'scale2': raw2
        },
        'calibration_factors': {
            'scale1': factor1,
            'scale2': factor2
        },
        'zero_offsets': {
            'scale1': offset1,
            'scale2': offset2
        },
        'db_zero_offsets': db_offsets,
        'calculated_weights': {
            'scale1': calc1,
            'scale2': calc2
        },
        'target_factors_for_73g': {
            'scale1': target_factor1,
            'scale2': target_factor2
        },
        'recent_readings': recent_readings,
        'features_enabled': app.features
    })

@app.route('/fix-calibration/<int:scale_id>', methods=['POST'])
def fix_calibration(scale_id):
    """Apply a calibration fix for a specific scale"""
    if scale_id not in [1, 2]:
        return jsonify({'message': 'Invalid scale ID.'}), 400
    
    data = request.json
    target_weight = float(data.get('target_weight', 73.0))
    
    # Get current raw value
    raw_key = f'scale{scale_id}'
    raw = app.last_raw_scales.get(raw_key)
    
    if raw is None:
        return jsonify({'message': f'No recent readings for Scale {scale_id}'}), 400
    
    try:
        # Calculate factor that would give target_weight from current raw value
        raw_value = float(raw)
        offset = app.scale_zero_offsets[scale_id]
        adjusted_raw = raw_value - offset
        
        if adjusted_raw == 0:
            return jsonify({'message': f'Adjusted raw value is zero, cannot calculate factor'}), 400
            
        factor = adjusted_raw / target_weight
        
        # Apply the new factor
        app.calibration_factors[scale_id] = factor
        
        # Save to database
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO calibration_settings (scale_id, factor)
            VALUES (?, ?)
        ''', (scale_id, factor))
        conn.commit()
        conn.close()
        
        return jsonify({
            'message': f'Scale {scale_id} calibration fixed. New factor: {factor:.4f}. Make sure to Tare when scale is empty.',
            'details': {
                'raw_value': raw_value,
                'offset': offset,
                'adjusted_raw': adjusted_raw,
                'target_weight': target_weight,
                'new_factor': factor
            }
        })
    except Exception as e:
        return jsonify({'message': f'Error fixing calibration: {str(e)}'}), 400

@app.route('/reset-zero-offset/<int:scale_id>', methods=['POST'])
def reset_zero_offset(scale_id):
    """Reset the zero offset for a scale to 0"""
    if scale_id not in [1, 2]:
        return jsonify({'message': 'Invalid scale ID.'}), 400
    
    try:
        # Reset in memory
        app.scale_zero_offsets[scale_id] = 0
        
        # Reset in database
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('DELETE FROM zero_offsets WHERE scale_id = ?', (scale_id,))
        conn.commit()
        conn.close()
        
        print(f"DEBUG: Reset zero offset for Scale {scale_id}")
        
        return jsonify({'message': f'Zero offset for Scale {scale_id} reset to 0'})
    except Exception as e:
        print(f"Error resetting zero offset: {e}")
        return jsonify({'message': f'Error resetting zero offset: {str(e)}'}), 500

# --- Modified Endpoint for Clearing Jam (Uses File Flag) --- 
@app.route('/clear-jam', methods=['POST'])
def clear_jam():
    duration = 2 # Hardcode duration to 2 seconds
    direction = 'reverse' # Hardcode direction to reverse
    
    test_details = {
        'direction': direction,
        'duration': duration
    }
    
    try:
        print(f"DEBUG: Received clear jam request. Creating flag file: {MOTOR_TEST_FLAG_FILE}")
        with open(MOTOR_TEST_FLAG_FILE, 'w') as f:
            json.dump(test_details, f)
        return jsonify({'message': f'Motor reverse ({duration}s) requested. Flag file created.'})
    except Exception as e:
        print(f"ERROR: Failed to create motor test flag file: {e}")
        return jsonify({'message': 'Error setting clear jam request.'}), 500
# --- End Modified Endpoint ---

if __name__ == '__main__':
    try:
        app.run(host='0.0.0.0', port=5000)
    finally:
        # Remove scale interface stop call
        # if scale_interface is not None:
        #     scale_interface.stop()
        pass # Keep finally block structure if needed later

