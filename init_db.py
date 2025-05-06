import sqlite3

DB_FILE = 'feeder.db'

conn = sqlite3.connect(DB_FILE)
c = conn.cursor()

c.execute('''
    CREATE TABLE IF NOT EXISTS feed_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        amount TEXT,
        status TEXT,
        notes TEXT
    )
''')

c.execute('''
    CREATE TABLE IF NOT EXISTS feed_schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT,
        amount TEXT,
        notes TEXT
    )
''')

conn.commit()
conn.close()
print("DB initialized!")

