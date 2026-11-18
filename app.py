
import os
import re
import sqlite3
from datetime import datetime
from io import BytesIO

import whisper
import requests
from dotenv import load_dotenv

load_dotenv()

from flask import (
    Flask, render_template, request, send_file,
    redirect, url_for, session, flash
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", "dev-key-change-before-deployment")

# Configuration
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

ALLOWED_AUDIO = {"mp3", "wav", "m4a", "ogg"}


# Database connection
def get_db_connection():
    conn = sqlite3.connect("meeting_minutes.db")
    conn.row_factory = sqlite3.Row
    return conn


# Create database tables
def init_db():
    conn = get_db_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            conversation TEXT,
            summary TEXT,
            decisions TEXT,
            action_items TEXT,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()


# Audio transcription will use Groq Cloud API

# Generate meeting minutes using Groq AI
def generate_minutes(conversation):

    prompt = f"""
You are an expert meeting minutes assistant.

Generate clear, professional meeting minutes from the conversation below.

IMPORTANT RULES:
- Read the entire conversation carefully.
- Use only information explicitly mentioned.
- Do not invent names, dates, decisions, or deadlines.
- Include every agreed decision under Decisions.
- Include every assigned task under Action Items.
- If no decision or task is mentioned, write "No items discussed."
- Summarize the actual discussion.

OUTPUT FORMAT:

## Meeting Summary
Write a short paragraph summarizing the actual meeting.

## Decisions
- List each agreed decision.

## Action Items
- Person responsible: Task – Deadline

MEETING CONVERSATION:
{conversation}
"""

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {os.environ.get('GROQ_API_KEY')}",
                "Content-Type": "application/json"
            },
            json={
                "model": "openai/gpt-oss-120b",
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.2,
                "max_tokens": 1000
            },
            timeout=120
        )

        response.raise_for_status()

        result = response.json()["choices"][0]["message"]["content"]
        print("RAW AI RESPONSE:", result)

        sections = {
            "summary": [],
            "decisions": [],
            "action_items": []
        }

        current_section = None

        for raw_line in result.replace("**", "").splitlines():
            line = raw_line.strip().lstrip("#").strip()
            heading = line.rstrip(":").lower()

            if heading in ["meeting summary", "summary"]:
                current_section = "summary"
            elif heading == "decisions":
                current_section = "decisions"
            elif heading == "action items":
                current_section = "action_items"
            elif current_section and line:
                sections[current_section].append(line)

        summary = "\n".join(sections["summary"]).strip()

        decisions = [
            line.strip("- ").strip()
            for line in sections["decisions"]
            if line.strip() and line.strip("- ").strip()
        ]

        action_items = [
            line.strip("- ").strip()
            for line in sections["action_items"]
            if line.strip() and line.strip("- ").strip()
        ]

        return {
            "summary": summary or "Not specified.",
            "decisions": decisions or ["Not specified."],
            "action_items": action_items or ["Not specified."]
        }

    except Exception as e:
        raise Exception("AI processing failed: " + str(e))


# User Registration
@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not email or not password:
            flash("Please fill in all fields.")
            return render_template("register.html")

        if len(password) < 8:
            flash("Password must contain at least 8 characters.")
            return render_template("register.html")

        conn = get_db_connection()

        try:
            conn.execute(
                "INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
                (username, email, generate_password_hash(password))
            )
            conn.commit()

            flash("Registration successful! Please log in.")
            return redirect(url_for("login"))

        except sqlite3.IntegrityError:
            flash("This email is already registered.")
            return render_template("register.html")

        finally:
            conn.close()


    return render_template("register.html")


# User Login
@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        conn = get_db_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE email = ?",
            (email,)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]

            return redirect(url_for("dashboard"))

        flash("Invalid email or password.")

    return render_template("login.html")


# User Logout
@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("login"))


# Dashboard
@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()

    meetings = conn.execute(
        """
        SELECT id, summary, created_at
        FROM meetings
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (session["user_id"],)
    ).fetchall()

    total_meetings = len(meetings)
    conn.close()

    return render_template(
        "dashboard.html",
        meetings=meetings,
        total_meetings=total_meetings
    )


# Meeting Details
@app.route("/meeting/<int:meeting_id>")
def meeting_details(meeting_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()

    meeting = conn.execute(
        """
        SELECT *
        FROM meetings
        WHERE id = ? AND user_id = ?
        """,
        (meeting_id, session["user_id"])
    ).fetchone()

    conn.close()

    if meeting is None:
        flash("Meeting not found.")
        return redirect(url_for("dashboard"))

    return render_template(
        "meeting_details.html",
        meeting=meeting
    )


# Home page
@app.route("/")
def home():

    if "user_id" not in session:
        return redirect(url_for("login"))

    return render_template("index.html")


# Generate minutes from text
@app.route("/generate", methods=["POST"])
def generate():

    if "user_id" not in session:
        return redirect(url_for("login"))

    conversation = request.form.get("conversation", "").strip()

    if not conversation:
        return render_template(
            "index.html",
            error="Please enter a meeting conversation."
        )

    minutes = generate_minutes(conversation)

    conn = get_db_connection()

    conn.execute(
        """
        INSERT INTO meetings
        (user_id, conversation, summary, decisions, action_items, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            session["user_id"],
            conversation,
            minutes["summary"],
            "\n".join(minutes["decisions"]),
            "\n".join(minutes["action_items"]),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
    )

    conn.commit()
    conn.close()

    return render_template(
        "index.html",
        conversation=conversation,
        summary=minutes["summary"],
        decisions=minutes["decisions"],
        action_items=minutes["action_items"]
    )


# Upload and transcribe audio
@app.route("/upload_audio", methods=["POST"])
def upload_audio():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if "audio" not in request.files:
        return render_template(
            "index.html",
            error="Please select an audio file."
        )

    audio = request.files["audio"]

    if audio.filename == "":
        return render_template(
            "index.html",
            error="No audio file selected."
        )

    filename = secure_filename(audio.filename)

    if "." not in filename:
        return render_template(
            "index.html",
            error="Invalid audio file."
        )

    extension = filename.rsplit(".", 1)[1].lower()

    if extension not in ALLOWED_AUDIO:
        return render_template(
            "index.html",
            error="Please upload MP3, WAV, M4A or OGG audio."
        )

    unique_name = (
        datetime.now().strftime("%Y%m%d_%H%M%S_")
        + filename
    )

    filepath = os.path.join(
        app.config["UPLOAD_FOLDER"],
        unique_name
    )

    audio.save(filepath)

    try:
        # Convert audio into text using Groq Cloud
        with open(filepath, "rb") as audio_file:
            response = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {os.environ.get('GROQ_API_KEY')}"
                },
                files={
                    "file": (os.path.basename(filepath), audio_file)
                },
                data={
                    "model": "whisper-large-v3-turbo",
                    "response_format": "json"
                },
                timeout=120
            )

        response.raise_for_status()

        conversation = response.json().get("text", "").strip()

        print("WHISPER TRANSCRIPT:", conversation)

        if not conversation:
            return render_template(
                "index.html",
                error="No speech could be detected in the audio."
            )

        # Generate meeting minutes
        minutes = generate_minutes(conversation)

        conn = get_db_connection()

        conn.execute(
            """
            INSERT INTO meetings
            (user_id, conversation, summary, decisions, action_items, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session["user_id"],
                conversation,
                minutes["summary"],
                "\n".join(minutes["decisions"]),
                "\n".join(minutes["action_items"]),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
        )

        conn.commit()
        conn.close()

        return render_template(
            "index.html",
            conversation=conversation,
            summary=minutes["summary"],
            decisions=minutes["decisions"],
            action_items=minutes["action_items"],
            upload_message="Audio transcribed successfully!"
        )

    except Exception as e:

        return render_template(
            "index.html",
            error="Audio processing failed: " + str(e)
        )


# Download meeting minutes as PDF
@app.route("/download", methods=["POST"])
def download():

    if "user_id" not in session:
        return redirect(url_for("login"))

    summary = request.form.get("summary", "")
    decisions = request.form.getlist("decisions")
    action_items = request.form.getlist("action_items")

    pdf_buffer = BytesIO()

    pdf = canvas.Canvas(pdf_buffer, pagesize=A4)

    width, height = A4
    y = height - 50

    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(50, y, "Meeting Minutes Generator")

    y -= 40

    def write_section(title, content):

        nonlocal y

        if y < 80:
            pdf.showPage()
            y = height - 50

        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(50, y, title)

        y -= 25

        pdf.setFont("Helvetica", 10)

        for item in content:

            words = item.split()
            current_line = ""

            for word in words:

                test_line = current_line + word + " "

                if pdf.stringWidth(test_line, "Helvetica", 10) > width - 100:

                    pdf.drawString(50, y, current_line)
                    y -= 15
                    current_line = word + " "

                else:
                    current_line = test_line

                if y < 50:
                    pdf.showPage()
                    y = height - 50
                    pdf.setFont("Helvetica", 10)

            if current_line:
                pdf.drawString(50, y, current_line)
                y -= 18

        y -= 15

    write_section("Meeting Summary", [summary])
    write_section("Decisions", decisions)
    write_section("Action Items", action_items)

    pdf.save()

    pdf_buffer.seek(0)

    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name="meeting_minutes.pdf",
        mimetype="application/pdf"
    )


# Initialize database
init_db()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
