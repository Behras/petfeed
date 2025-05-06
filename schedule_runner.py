#!/usr/bin/env python3
"""
Runs once a minute (via cron).
If any schedule matches the current HH:MM **and**
no feed‑log has already been inserted for that schedule
today, it inserts a 'pending' feed request.
"""

import sqlite3
from datetime import datetime

DB_FILE = '/home/petfeed/petfeeder/feeder.db'

def already_logged(conn, sched_time, today):
    c = conn.cursor()
    c.execute("""
        SELECT 1
        FROM   feed_logs
        WHERE  date(timestamp)=?             -- today
           AND strftime('%H:%M', timestamp)=? -- that minute
    """, (today, sched_time))
    return c.fetchone() is not None

def main():
    now      = datetime.now()                      # uses server TZ
    hhmm     = now.strftime('%H:%M')
    today    = now.strftime('%Y-%m-%d')

    conn = sqlite3.connect(DB_FILE)
    cur  = conn.cursor()

    # any schedules that hit this exact minute?
    cur.execute("""
        SELECT time, amount, notes
        FROM   feed_schedules
        WHERE  time = ?
    """, (hhmm,))
    matches = cur.fetchall()

    for sched_time, amount, notes in matches:
        if not already_logged(conn, sched_time, today):
            cur.execute("""
                INSERT INTO feed_logs (timestamp, amount, status, notes)
                VALUES (?, ?, 'pending', ?)
            """, (now.strftime('%Y-%m-%d %H:%M:%S'),
                  amount,
                  f'Scheduled feed ({notes})'.strip()))
            print(f'Inserted pending feed for {sched_time}')
    conn.commit()
    conn.close()

if __name__ == "__main__":
    main()

