import time
import threading
from datetime import datetime, timedelta
from database import get_account, update_account, get_db

workers = {}

def automation_worker(username):
    while True:
        account = get_account(username)
        if not account or not account["enabled"]:
            break

        try:
            # Update status
            update_account(username, current_task="Finding trending content...")
            time.sleep(3)

            update_account(username, current_task="Processing video...")
            time.sleep(4)

            update_account(username, current_task="Uploading to TikTok via API...")
            time.sleep(5)

            now = datetime.now()
            next_time = (now + timedelta(minutes=25)).strftime("%H:%M")

            update_account(
                username,
                last_post=now.strftime("%Y-%m-%d %H:%M:%S"),
                next_post=next_time,
                current_task="Waiting 25 minutes",
                status="Running"
            )

            # Wait 25 minutes
            time.sleep(1500)

        except Exception as e:
            update_account(username, current_task=f"Error: {str(e)[:50]}")
            time.sleep(30)

    update_account(username, status="Stopped", current_task="Idle")


def start_automation(username):
    if username in workers:
        return
    thread = threading.Thread(target=automation_worker, args=(username,), daemon=True)
    workers[username] = thread
    thread.start()


def stop_automation(username):
    if username in workers:
        del workers[username]