# main.py
import os
import tempfile
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import (
    FastAPI, Request, Depends, File, UploadFile, Form, HTTPException,
    status
)
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from jose import jwt, JWTError
from passlib.context import CryptContext

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# local modules
from models import Base, User, Interview, Answer, Score, Report
from utils import parse_resume_file, extract_skills_from_text, generate_pdf_report

# Ollama + Whisper
import ollama
from faster_whisper import WhisperModel

# dotenv (optional)
from dotenv import load_dotenv
load_dotenv()

# Config via env
MYSQL_URL = os.getenv("MYSQL_URL", "mysql+mysqlconnector://root:password@localhost:3306/interview_db")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
JWT_SECRET = os.getenv("JWT_SECRET", "supersecretjwt")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL", "small")

# SQLAlchemy setup
engine = create_engine(MYSQL_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

# Create tables
Base.metadata.create_all(bind=engine)

# MongoDB for resumes
from pymongo import MongoClient
mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client["interview_db"]
resume_collection = mongo_db["resumes"]

# FastAPI app
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/reports", StaticFiles(directory="reports"), name="reports")
templates = Jinja2Templates(directory="templates")

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# OAuth2 scheme for token retrieval
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")

# Load Whisper model on startup
WHISPER_MODEL = None

@app.on_event("startup")
def startup_event():
    global WHISPER_MODEL
    # choose device and compute type depending on your environment
    WHISPER_MODEL = WhisperModel(WHISPER_MODEL_NAME, device="cpu", compute_type="int8")
    print(f"Loaded Whisper model: {WHISPER_MODEL_NAME}")

# Dependency: DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Auth helpers
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt

def verify_password(plain, hashed):
    return pwd_context.verify(plain, hashed)

def get_password_hash(password):
    return pwd_context.hash(password)

def get_current_user(token: str = Depends(oauth2_scheme), db=Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user = db.query(User).filter_by(username=username).first()
    if not user:
        raise credentials_exception
    return user

# ---------- ROUTES ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# Register page GET
@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

# Register API
@app.post("/api/register")
async def api_register(
    name: str = Form(...),
    email: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    resume_file: UploadFile = File(...),
    db=Depends(get_db)
):
    # check duplicates
    if db.query(User).filter((User.username == username) | (User.email == email)).first():
        raise HTTPException(status_code=400, detail="Username or email already exists")

    # create user
    user = User(username=username, email=email, password_hash=get_password_hash(password))
    db.add(user)
    db.commit()
    db.refresh(user)

    # save resume to temp and parse
    suffix = os.path.splitext(resume_file.filename)[1] or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await resume_file.read()
        tmp.write(content)
        tmp_path = tmp.name

    parsed_text = parse_resume_file(tmp_path)
    skills = extract_skills_from_text(parsed_text)

    resume_doc = {
        "user_id": user.id,
        "original_filename": resume_file.filename,
        "parsed_text": parsed_text,
        "skills": skills,
        "uploaded_at": datetime.utcnow()
    }
    resume_collection.insert_one(resume_doc)

    return {"message": "Registered", "user_id": user.id, "resume_id": str(resume_doc.get("_id"))}

# Login page GET
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

# Login API (OAuth2PasswordRequestForm)
@app.post("/api/login")
def api_login(form_data: OAuth2PasswordRequestForm = Depends(), db=Depends(get_db)):
    user = db.query(User).filter_by(username=form_data.username).first()
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    token = create_access_token({"sub": user.username})
    return {"access_token": token, "token_type": "bearer", "user_id": user.id}

# Dashboard (requires auth)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, current_user=Depends(get_current_user), db=Depends(get_db)):
    interviews = db.query(Interview).filter_by(user_id=current_user.id).order_by(Interview.date.desc()).all()
    resume_doc = resume_collection.find_one({"user_id": current_user.id})
    return templates.TemplateResponse("result.html", {"request": request, "user": current_user, "interviews": interviews, "resume": resume_doc})

# Start interview (auth required). Returns interview_id and questions_count
@app.post("/interview/start")
def start_interview(interview_type: str = Form("technical"), current_user=Depends(get_current_user), db=Depends(get_db)):
    # Get resume doc
    resume_doc = resume_collection.find_one({"user_id": current_user.id})
    if not resume_doc:
        raise HTTPException(status_code=400, detail="Resume not found")

    skills = resume_doc.get("skills", []) or ["General"]

    # Build Ollama prompt to return JSON array of objects with skill, question, max_score
    prompt = (
        "Generate 15 interview questions distributed across these skills: "
        f"{skills}. Return ONLY a valid JSON array where each item is "
        "{\"skill\": <skill-name>, \"question\": <text>, \"max_score\": 5}."
    )

    try:
        response = ollama.chat(model="codellama:latest", messages=[{"role": "system", "content": prompt}])
        content = response.get("message", {}).get("content", "")
        questions_json = json.loads(content)
    except Exception:
        # fallback: ask the model a simpler way or default
        # We'll just create simple placeholders distributed across skills
        questions_json = []
        for i in range(15):
            questions_json.append({
                "skill": skills[i % len(skills)],
                "question": f"Placeholder question #{i+1} for {skills[i % len(skills)]}",
                "max_score": 5
            })

    # Save interview and create answer placeholders
    interview = Interview(user_id=current_user.id, type=interview_type, date=datetime.utcnow(), questions=json.dumps(questions_json))
    db.add(interview)
    db.commit()
    db.refresh(interview)

    for q in questions_json:
        ans = Answer(interview_id=interview.id, question_text=q["question"], answer_text=None, skill=q.get("skill"), max_score=q.get("max_score", 5))
        db.add(ans)
    db.commit()

    return {"interview_id": interview.id, "questions_count": len(questions_json)}

# Serve question page (question index)
@app.get("/question/{interview_id}/{q_index}", response_class=HTMLResponse)
def question_page(request: Request, interview_id: int, q_index: int, current_user=Depends(get_current_user), db=Depends(get_db)):
    interview = db.query(Interview).filter_by(id=interview_id, user_id=current_user.id).first()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")
    questions = json.loads(interview.questions)
    if q_index >= len(questions):
        return RedirectResponse(url=f"/result1/{interview_id}", status_code=303)
    question = questions[q_index]["question"]
    return templates.TemplateResponse("question.html", {"request": request, "interview_id": interview_id, "q_index": q_index, "question": question, "total": len(questions)})

# STT endpoint: accept audio UploadFile, transcribe with Faster-Whisper, evaluate with Ollama and save
@app.post("/stt")
async def stt_endpoint(interview_id: int = Form(...), q_index: int = Form(...), file: UploadFile = File(...), current_user=Depends(get_current_user), db=Depends(get_db)):
    # save uploaded audio to temp file
    suffix = os.path.splitext(file.filename)[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    # Transcribe with faster-whisper
    try:
        segments, info = WHISPER_MODEL.transcribe(tmp_path)
        transcript = " ".join([seg.text for seg in segments])
    except Exception as e:
        transcript = ""
        print("Whisper error:", e)

    # find interview and question
    interview = db.query(Interview).filter_by(id=interview_id, user_id=current_user.id).first()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")
    questions = json.loads(interview.questions)
    if q_index >= len(questions):
        raise HTTPException(status_code=400, detail="Invalid question index")
    question_text = questions[q_index]["question"]

    # update Answer row
    answer_row = db.query(Answer).filter_by(interview_id=interview_id, question_text=question_text).first()
    if not answer_row:
        # create one if not exists
        answer_row = Answer(interview_id=interview_id, question_text=question_text, answer_text=transcript, skill=questions[q_index].get("skill"), max_score=questions[q_index].get("max_score", 5))
        db.add(answer_row)
    else:
        answer_row.answer_text = transcript
    db.commit()

    # Evaluate answer using Ollama (ask for JSON {score, feedback})
    eval_prompt = (
        f"Evaluate the following candidate response and return a JSON object with keys: score (0-5), feedback.\n\n"
        f"Question: {question_text}\n\nAnswer: {transcript}\n\n"
        "Return only JSON, nothing else."
    )
    score_obtained = 0
    feedback = ""
    try:
        eval_res = ollama.chat(model="codellama:latest", messages=[{"role":"system","content":eval_prompt}])
        eval_content = eval_res.get("message", {}).get("content", "")
        eval_json = json.loads(eval_content)
        score_obtained = int(eval_json.get("score", 0))
        feedback = eval_json.get("feedback", "")
    except Exception:
        # fallback naive scoring
        score_obtained = min(5, max(0, len(transcript.split()) // 20))
        feedback = "Automated fallback feedback."

    # Save score
    skill = questions[q_index].get("skill", "General")
    score_obj = Score(interview_id=interview_id, skill=skill, score_obtained=score_obtained, score_total=questions[q_index].get("max_score", 5))
    db.add(score_obj)
    db.commit()

    return {"transcript": transcript, "score": score_obtained, "feedback": feedback}

# Immediate result page (raw scores only)
@app.get("/result1/{interview_id}", response_class=HTMLResponse)
def immediate_result(request: Request, interview_id: int, current_user=Depends(get_current_user), db=Depends(get_db)):
    interview = db.query(Interview).filter_by(id=interview_id, user_id=current_user.id).first()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")
    scores = db.query(Score).filter_by(interview_id=interview_id).all()
    # aggregate per-skill
    skill_map = {}
    for s in scores:
        if s.skill not in skill_map:
            skill_map[s.skill] = {"obtained": 0, "total": 0}
        skill_map[s.skill]["obtained"] += s.score_obtained
        skill_map[s.skill]["total"] += s.score_total
    overall_obtained = sum(v["obtained"] for v in skill_map.values())
    overall_total = sum(v["total"] for v in skill_map.values())
    overall_pct = (overall_obtained / overall_total * 100) if overall_total > 0 else 0.0
    return templates.TemplateResponse("result1.html", {"request": request, "skill_map": skill_map, "overall_obtained": overall_obtained, "overall_total": overall_total, "overall_pct": overall_pct, "interview_id": interview_id})

# Generate report (dashboard only)
@app.post("/reports/generate")
def generate_report_api(interview_id: int = Form(...), current_user=Depends(get_current_user), db=Depends(get_db)):
    interview = db.query(Interview).filter_by(id=interview_id, user_id=current_user.id).first()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")
    answers = db.query(Answer).filter_by(interview_id=interview_id).all()
    scores = db.query(Score).filter_by(interview_id=interview_id).all()
    total_obtained = sum(s.score_obtained for s in scores)
    total_possible = sum(s.score_total for s in scores)
    overall_pct = (total_obtained / total_possible * 100) if total_possible > 0 else 0.0

    pdf_path = generate_pdf_report(interview_id, interview, answers, scores, total_obtained, total_possible, overall_pct)

    report = Report(interview_id=interview_id, overall_score=overall_pct, pdf_path=pdf_path, generated_at=datetime.utcnow())
    db.add(report)
    db.commit()

    # return URL relative to app: /reports/<filename>
    filename = os.path.basename(pdf_path)
    return {"pdf_url": f"/reports/{filename}", "pdf_path": pdf_path}
