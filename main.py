import os
import time
import sqlite3
import requests
import threading
from flask import Flask, request, jsonify, render_template

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my_secure_verify_token")

# --- Database Setup (Single User / Single Agent) ---
DB_NAME = "single_agent_memory.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            page_access_token TEXT,
            comment_access_token TEXT,
            groq_api_key TEXT,
            custom_prompt TEXT,
            bot_status BOOLEAN DEFAULT 1,
            comment_status BOOLEAN DEFAULT 1
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            sender_id TEXT,
            role TEXT,
            content TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def get_agent_settings():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM agent_config WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    return row

# --- Flask App Setup ---
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "autocraft_single_agent_secret")

# --- HTML View Route ---
@app.route("/")
def control_panel():
    return render_template("index.html")

# --- REST API Endpoints ---
@app.route("/api/config", methods=["GET"])
def api_get_config():
    row = get_agent_settings()
    if row:
        return jsonify({
            "success": True,
            "config": {
                "page_access_token": row["page_access_token"] or "",
                "comment_access_token": row["comment_access_token"] or "",
                "groq_api_key": row["groq_api_key"] or "",
                "custom_prompt": row["custom_prompt"] or "",
                "is_active": bool(row["bot_status"]),
                "comment_status": bool(row["comment_status"])
            }
        })
    return jsonify({"success": True, "config": {"page_access_token": "", "comment_access_token": "", "groq_api_key": "", "custom_prompt": "", "is_active": True, "comment_status": True}})

@app.route("/api/config/save", methods=["POST"])
def api_save_config():
    data = request.json or {}
    page_access_token = data.get("page_access_token", "").strip()
    comment_access_token = data.get("comment_access_token", "").strip() or page_access_token
    groq_api_key = data.get("groq_api_key", "").strip()
    custom_prompt = data.get("custom_prompt", "").strip()
    is_active = 1 if data.get("is_active", True) else 0
    comment_status = 1 if data.get("comment_status", True) else 0

    if not page_access_token or not groq_api_key or not custom_prompt:
        return jsonify({"success": False, "error": "Page Access Token, Groq API Key and System Prompt are required."}), 400

    # Validate Page Token with Facebook Graph API
    fb_url = f"https://graph.facebook.com/v18.0/me?access_token={page_access_token}"
    res = requests.get(fb_url).json()

    if "error" in res:
        return jsonify({"success": False, "error": f"Invalid Access Token: {res['error'].get('message')}"}), 400

    page_id = res.get("id")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO agent_config (id, page_access_token, comment_access_token, groq_api_key, custom_prompt, bot_status, comment_status)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            page_access_token = excluded.page_access_token,
            comment_access_token = excluded.comment_access_token,
            groq_api_key = excluded.groq_api_key,
            custom_prompt = excluded.custom_prompt,
            bot_status = excluded.bot_status,
            comment_status = excluded.comment_status
    """, (page_access_token, comment_access_token, groq_api_key, custom_prompt, is_active, comment_status))
    conn.commit()
    conn.close()

    # Auto-subscribe webhook fields for messages & feed
    sub_url = f"https://graph.facebook.com/v18.0/{page_id}/subscribed_apps?subscribed_fields=messages,feed&access_token={page_access_token}"
    requests.post(sub_url)

    return jsonify({"success": True, "page_id": page_id})

@app.route("/api/config/toggle", methods=["POST"])
def api_toggle_status():
    data = request.json or {}
    is_active = 1 if data.get("is_active") else 0
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE agent_config SET bot_status = ? WHERE id = 1", (is_active,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/config/toggle-comment", methods=["POST"])
def api_toggle_comment():
    data = request.json or {}
    comment_status = 1 if data.get("comment_status") else 0
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE agent_config SET comment_status = ? WHERE id = 1", (comment_status,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# --- AI Reply Generation using Dynamic Groq API Key ---
def generate_ai_reply(messages, api_key):
    from groq import Groq
    if not api_key:
        return "দুঃখিত, কোনো Groq API Key সেট করা হয়নি।"

    available_models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b", "llama-3.3-70b-versatile"]
    for model_name in available_models:
        try:
            client = Groq(api_key=api_key)
            completion = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.6,
                max_tokens=500,
            )
            return completion.choices[0].message.content
        except Exception as e:
            print(f"Model {model_name} Failed: {e}")
            continue
    return "দুঃখিত, এই মুহূর্তে এআই রেসপন্স পেতে সমস্যা হচ্ছে।"

def get_user_history(sender_id, custom_prompt):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT role, content FROM (
            SELECT role, content, ROWID FROM chat_history 
            WHERE sender_id = ? 
            ORDER BY ROWID DESC LIMIT 8
        ) ORDER BY ROWID ASC
    """, (sender_id,))
    rows = cursor.fetchall()
    conn.close()

    system_instruction = f"{custom_prompt}\n\n[STRICT RULE: Always reply clearly in Bengali (বাংলা) or Banglish as requested. Keep responses complete and meaningful.]"
    history = [{"role": "system", "content": system_instruction}]
    for row in rows:
        history.append({"role": row["role"], "content": row["content"]})
    return history

def save_message_to_db(sender_id, role, content):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chat_history (sender_id, role, content) VALUES (?, ?, ?)", (sender_id, role, content))
    conn.commit()
    conn.close()

# --- Facebook Helper Functions ---
def send_typing_indicator(recipient_id, token):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
    requests.post(url, json={"recipient": {"id": recipient_id}, "sender_action": "typing_on"})

def send_facebook_message(recipient_id, text, token):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
    requests.post(url, json={"messaging_type": "RESPONSE", "recipient": {"id": recipient_id}, "message": {"text": text}})

def reply_to_comment(comment_id, text, token):
    url = f"https://graph.facebook.com/v18.0/{comment_id}/comments?access_token={token}"
    requests.post(url, json={"message": text})

def send_private_dm(comment_id, text, token):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
    requests.post(url, json={"recipient": {"comment_id": comment_id}, "message": {"text": text}})

# --- Webhook Processing Threads ---
def process_message_async(sender_id, user_text, config):
    try:
        if user_text:
            history = get_user_history(sender_id, config["custom_prompt"])
            save_message_to_db(sender_id, "user", user_text)
            history.append({"role": "user", "content": user_text})

            ai_reply = generate_ai_reply(history, config["groq_api_key"])
            save_message_to_db(sender_id, "assistant", ai_reply)

            for _ in range(3):
                send_typing_indicator(sender_id, config["page_access_token"])
                time.sleep(1)

            send_facebook_message(sender_id, ai_reply, config["page_access_token"])
    except Exception as e:
        print(f"Message Async Error: {e}")

def process_comment_async(comment_id, user_comment, config):
    try:
        public_instruction = f"{config['custom_prompt']}\n\n[STRICT RULE: Reply to user comment nicely in Bengali/Banglish within 1-2 lines. Mention details sent in inbox.]"
        messages = [
            {"role": "system", "content": public_instruction},
            {"role": "user", "content": f"User Comment: {user_comment}"}
        ]
        public_reply = generate_ai_reply(messages, config["groq_api_key"])

        reply_to_comment(comment_id, public_reply, config["comment_access_token"])
        send_private_dm(comment_id, "হ্যালো! আপনার কমেন্টের জন্য ধন্যবাদ 😊 ইনবক্সে বিস্তারিত দেওয়া হলো।", config["page_access_token"])
    except Exception as e:
        print(f"Comment Async Error: {e}")

# --- Facebook Webhook Route ---
@app.route("/webhook", methods=["GET", "POST"])
def facebook_webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode and token and mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Verification Failed", 403

    if request.method == "POST":
        data = request.json
        try:
            config = get_agent_settings()
            if not config:
                return jsonify({"status": "no config"}), 200

            if data.get("object") == "page":
                for entry in data.get("entry", []):
                    # 1. Messenger Automation
                    if config["bot_status"] and config["page_access_token"] and config["groq_api_key"]:
                        for messaging_event in entry.get("messaging", []):
                            if messaging_event.get("message", {}).get("is_echo"):
                                continue
                            sender_id = messaging_event.get("sender", {}).get("id")
                            user_text = messaging_event.get("message", {}).get("text", "")
                            if sender_id and user_text:
                                threading.Thread(
                                    target=process_message_async,
                                    args=(sender_id, user_text, config)
                                ).start()

                    # 2. Comment Auto-Reply Automation
                    if config["comment_status"] and config["comment_access_token"] and config["groq_api_key"]:
                        for change in entry.get("changes", []):
                            if change.get("field") == "feed":
                                val = change.get("value", {})
                                if val.get("item") == "comment" and val.get("verb") == "add":
                                    comment_id = val.get("comment_id")
                                    user_comment = val.get("message")
                                    if comment_id and user_comment:
                                        threading.Thread(
                                            target=process_comment_async,
                                            args=(comment_id, user_comment, config)
                                        ).start()
        except Exception as e:
            print(f"Webhook Error: {e}")
        return jsonify({"status": "event received"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)