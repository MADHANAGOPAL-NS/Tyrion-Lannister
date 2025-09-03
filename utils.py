# utils.py
import os
import re
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# PDF parsing
from pdfminer.high_level import extract_text as pdf_extract_text
from docx import Document

# Basic known skills list - extend as needed
KNOWN_SKILLS = [
    "python", "java", "c++", "c#", "javascript", "node", "react", "django", "flask",
    "sql", "mysql", "postgres", "mongodb", "aws", "docker", "kubernetes", "spring"
]

def parse_resume_file(path: str) -> str:
    """
    Read file (pdf or docx) and return extracted plaintext.
    """
    ext = os.path.splitext(path)[1].lower()
    text = ""
    try:
        if ext == ".pdf":
            text = pdf_extract_text(path)
        elif ext in [".docx", ".doc"]:
            doc = Document(path)
            text = "\n".join([p.text for p in doc.paragraphs])
        else:
            # fallback: attempt to read as text
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
    except Exception:
        text = ""
    return text

def extract_skills_from_text(text: str):
    """
    Simple keyword-based skill extraction. Returns list (capitalized).
    Replace with LLM-based parser if more accuracy required.
    """
    text_low = (text or "").lower()
    found = []
    for s in KNOWN_SKILLS:
        if re.search(r"\b" + re.escape(s) + r"\b", text_low):
            found.append(s.capitalize())
    return found if found else ["General"]

def generate_pdf_report(interview_id, interview_obj, answers, scores, total_obtained, total_possible, overall_pct) -> str:
    """
    Create a PDF report for the interview and return relative path.
    """
    os.makedirs("reports", exist_ok=True)
    filename = f"report_{interview_id}_{int(datetime.utcnow().timestamp())}.pdf"
    path = os.path.join("reports", filename)

    c = canvas.Canvas(path, pagesize=A4)
    x = 50
    y = 800

    c.setFont("Helvetica-Bold", 16)
    c.drawString(x, y, f"Interview Report - ID {interview_id}")
    y -= 30

    c.setFont("Helvetica", 12)
    c.drawString(x, y, f"Date: {interview_obj.date}")
    y -= 20
    c.drawString(x, y, f"Interview Type: {interview_obj.type}")
    y -= 30

    c.setFont("Helvetica-Bold", 14)
    c.drawString(x, y, "Per-Skill Scores:")
    y -= 20

    for s in scores:
        c.setFont("Helvetica", 12)
        line = f"{s.skill}: {s.score_obtained}/{s.score_total}"
        c.drawString(x, y, line)
        y -= 18
        if y < 80:
            c.showPage()
            y = 800

    y -= 10
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x, y, f"Overall: {total_obtained}/{total_possible} ({overall_pct:.2f}%)")
    y -= 30

    # Optionally include answers (short form)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x, y, "Answers (transcripts):")
    y -= 18
    c.setFont("Helvetica", 11)
    for a in answers:
        # limit line length per page
        text = (a.answer_text or "").strip()
        if not text:
            continue
        # split into chunks
        while text:
            chunk = text[:120]
            c.drawString(x, y, chunk)
            text = text[120:]
            y -= 14
            if y < 80:
                c.showPage()
                y = 800

    c.save()
    return path
