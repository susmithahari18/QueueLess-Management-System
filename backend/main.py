from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text

from database import engine, SessionLocal
from models import (
    Service,
    Queue,
    Token,
    User,
    Notification,
    Appointment
)

from schemas import (
    ServiceCreate,
    QueueCreate,
    TokenCreate,
    UserCreate,
    LoginRequest,
    AppointmentCreate
)

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    Query
)
import logging

from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from jose import jwt
from datetime import datetime, timedelta
from io import BytesIO

import bcrypt
import qrcode
import redis
import redis.asyncio as redis_async
import json
import asyncio
import os

from dotenv import load_dotenv
load_dotenv()
APP_BASE_URL = os.getenv("APP_BASE_URL")
app = FastAPI()

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)

APP_BASE_URL = os.getenv("APP_BASE_URL")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


security = HTTPBearer()
SECRET_KEY = os.getenv("SECRET_KEY")

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is not configured")

ALGORITHM = "HS256"


def create_access_token(user_id: int):
    expire = datetime.utcnow() + timedelta(minutes=30)

    payload = {
        "user_id": user_id,
        "exp": expire
    }

    token = jwt.encode(
        payload,
        SECRET_KEY,
        algorithm=ALGORITHM
    )

    return token



def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    token = credentials.credentials

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )

        user_id = payload.get("user_id")

        if user_id is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid token"
            )

        return user_id

    except jwt.JWTError:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token"
        )

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def create_notification(
    db: Session,
    user_id: int,
    title: str,
    message: str,
    notification_type: str
):
    notification = Notification(
        user_id=user_id,
        title=title,
        message=message,
        notification_type=notification_type
    )

    db.add(notification)
    db.flush()

    return notification

@app.get("/")
def home():
    return {"message": "QueueLess API is running"}


@app.get("/test-db")
def test_database():
    with engine.connect() as connection:
        return {"message": "Database connected successfully"}



@app.get("/services")
def get_services(db: Session = Depends(get_db)):
    services = db.query(Service).all()
    return services

@app.post("/queues")
def create_queue(
    queue: QueueCreate,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role not in ["staff", "admin"]:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to create queues"
        )

    service = db.query(Service).filter(
        Service.id == queue.service_id
    ).first()

    if not service:
        raise HTTPException(
            status_code=404,
            detail="Service not found"
        )

    new_queue = Queue(
        service_id=queue.service_id
    )

    db.add(new_queue)

    try:
        db.commit()
        db.refresh(new_queue)

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="An active queue already exists for this service today"
        )

    return new_queue

@app.post("/users")
def create_user(user: UserCreate, db: Session = Depends(get_db)):

    new_user = User(
        name=user.name,
        email=user.email,
        password_hash=bcrypt.hashpw(
            user.password.encode("utf-8"),
            bcrypt.gensalt()
        ).decode("utf-8"),
        role="customer"
    )

    db.add(new_user)

    try:
        db.commit()
        db.refresh(new_user)
        return {
            "id": new_user.id,
            "name": new_user.name,
            "email": new_user.email,
            "role": new_user.role,
            "created_at": new_user.created_at
        }

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Email already exists"
        )

@app.post("/tokens")
def create_token(
    token: TokenCreate,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    queue = db.query(Queue).filter(
        Queue.id == token.queue_id
    ).first()

    if not queue:
        raise HTTPException(
            status_code=404,
            detail="Queue not found"
        )

    if queue.queue_date != datetime.now().date():
        raise HTTPException(
            status_code=409,
            detail="You can only join today's queue"
        )

    if queue.status != "active":
        raise HTTPException(
            status_code=409,
            detail="This queue is no longer active"
        )

    service = db.query(Service).filter(
        Service.id == queue.service_id
    ).first()

    if not service:
        raise HTTPException(
            status_code=404,
            detail="Service not found"
        )

    if not service.is_active:
        raise HTTPException(
            status_code=409,
            detail="This service is currently unavailable"
        )

    db.execute(
        text("SELECT pg_advisory_xact_lock(:queue_id)"),
        {"queue_id": token.queue_id}
    )

    last_token = (
        db.query(Token)
        .filter(Token.queue_id == token.queue_id)
        .order_by(Token.token_number.desc())
        .first()
    )

    if last_token:
        next_token_number = last_token.token_number + 1
    else:
        next_token_number = 1

    new_token = Token(
        queue_id=token.queue_id,
        user_id=current_user,
        token_number=next_token_number
    )

    db.add(new_token)

    create_notification(
        db,
        current_user,
        "Token Created",
        f"Your token #{new_token.token_number} has been created successfully.",
        "token_created"
    )

    db.commit()
    db.refresh(new_token)

    waiting_count = (
        db.query(Token)
        .filter(
            Token.queue_id == token.queue_id,
            Token.status == "waiting"
        )
        .count()
    )

    current_token = (
        db.query(Token)
        .filter(
            Token.queue_id == token.queue_id,
            Token.status == "called"
        )
        .order_by(Token.token_number.desc())
        .first()
    )

    set_queue_state(
        token.queue_id,
        current_token.token_number if current_token else None,
        waiting_count
    )

    return {
        "id": new_token.id,
        "queue_id": new_token.queue_id,
        "user_id": new_token.user_id,
        "token_number": new_token.token_number,
        "status": new_token.status,
        "joined_at": new_token.joined_at
    }

@app.get("/queues/{queue_id}/status")
def get_queue_status(
    queue_id: int,
    db: Session = Depends(get_db)
):
    queue = db.query(Queue).filter(Queue.id == queue_id).first()

    if not queue:
        raise HTTPException(
            status_code=404,
            detail="Queue not found"
        )

    waiting_tokens = (
        db.query(Token)
        .filter(
            Token.queue_id == queue_id,
            Token.status == "waiting"
        )
        .order_by(Token.token_number)
        .all()
    )

    current_token = (
    db.query(Token)
    .filter(
        Token.queue_id == queue_id,
        Token.status == "called"
    )
    .order_by(Token.token_number.desc())
    .first()
)

    current_number = (
        current_token.token_number
            if current_token
            else 0
        )

    return {
        "queue_id": queue_id,
        "current_token": current_number,
        "waiting_count": len(waiting_tokens),
        "waiting_tokens": [
            token.token_number for token in waiting_tokens
        ]
    }

@app.post("/queues/{queue_id}/next")
async def call_next_customer(
    queue_id: int,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)

):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role not in ["staff", "admin"]:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to manage the queue"
        )

    queue = db.query(Queue).filter(
        Queue.id == queue_id
    ).first()

    if not queue:
        raise HTTPException(
            status_code=404,
            detail="Queue not found"
        )

    if queue.status != "active":
        raise HTTPException(
            status_code=409,
            detail="This queue is no longer active"
        )

    current_token = (
        db.query(Token)
        .filter(
            Token.queue_id == queue_id,
            Token.status == "called"
        )
        .first()
    )

    if current_token:
        raise HTTPException(
            status_code=409,
            detail=f"Token #{current_token.token_number} is currently being served"
        )

    next_token = (
        db.query(Token)
        .filter(
            Token.queue_id == queue_id,
            Token.status == "waiting"
        )
        .order_by(Token.token_number)
        .with_for_update()
        .first()
    )

    if not next_token:
        return {"message": "No customers waiting"}

    next_token.status = "called"
    next_token.called_at = datetime.utcnow()

    create_notification(
        db,
        next_token.user_id,
        "Your Turn",
        f"Token #{next_token.token_number} is now being served.",
        "token_called"
)

    db.commit()
    db.refresh(next_token)

    waiting_count = (
        db.query(Token)
        .filter(
            Token.queue_id == queue_id,
            Token.status == "waiting"
        )
        .count()
    )

    set_queue_state(
        queue_id,
        next_token.token_number,
        waiting_count
    )

    publish_queue_update(
        queue_id,
        {
            "queue_id": queue_id,
            "current_token": next_token.token_number,
            "status": "called"
        }
    )


    return {
        "message": "Customer called successfully",
        "token_number": next_token.token_number,
        "status": next_token.status
    }

@app.post("/tokens/{token_id}/complete")
async def complete_token(
    token_id: int,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):

    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role not in ["staff", "admin"]:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to complete tokens"
        )

    token = db.query(Token).filter(Token.id == token_id).first()

    if not token:
        raise HTTPException(
            status_code=404,
            detail="Token not found"
        )
    if token.status != "called":
        raise HTTPException(
            status_code=409,
            detail="Only a called token can be completed"
        )
    token.status = "completed"
    token.completed_at = datetime.utcnow()


    next_token = (
        db.query(Token)
        .filter(
            Token.queue_id == token.queue_id,
            Token.status == "waiting"
        )
        .order_by(Token.token_number)
        .with_for_update()
        .first()
    )

    if next_token:
        next_token.status = "called"
        next_token.called_at = datetime.utcnow()

    create_notification(
        db,
        token.user_id,
        "Service Completed",
        f"Your token #{token.token_number} has been completed.",
        "token_completed"
    )

    db.commit()
    db.refresh(token)

    waiting_count = (
        db.query(Token)
        .filter(
            Token.queue_id == token.queue_id,
            Token.status == "waiting"
        )
        .count()
    )

    current_token = (
        db.query(Token)
        .filter(
            Token.queue_id == token.queue_id,
            Token.status == "called"
        )
        .order_by(Token.token_number.desc())
        .first()
    )

    set_queue_state(
        token.queue_id,
        current_token.token_number if current_token else None,
        waiting_count
    )

    publish_queue_update(
        token.queue_id,
        {
            "queue_id": token.queue_id,
            "completed_token": token.token_number,
            "next_token": next_token.token_number if next_token else None,
            "current_token": next_token.token_number if next_token else None,
            "status": "completed"
        }
    )


    return {
        "message": "Token completed successfully",
        "completed_token": token.token_number,
        "next_token": next_token.token_number if next_token else None
    }

@app.post("/tokens/{token_id}/skip")
async def skip_token(
    token_id: int,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role not in ["staff", "admin"]:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to skip tokens"
        )

    token = db.query(Token).filter(
        Token.id == token_id
    ).first()

    if not token:
        raise HTTPException(
            status_code=404,
            detail="Token not found"
        )

    if token.status != "called":
        raise HTTPException(
            status_code=409,
            detail="Only a called token can be skipped"
        )

    token.status = "skipped"

    next_token = (
        db.query(Token)
        .filter(
            Token.queue_id == token.queue_id,
            Token.status == "waiting"
        )
        .order_by(Token.token_number)
        .with_for_update()
        .first()
    )

    if next_token:
        next_token.status = "called"
        next_token.called_at = datetime.utcnow()

    db.commit()
    db.refresh(token)

    waiting_count = (
        db.query(Token)
        .filter(
            Token.queue_id == token.queue_id,
            Token.status == "waiting"
        )
        .count()
    )

    current_token = (
        db.query(Token)
        .filter(
            Token.queue_id == token.queue_id,
            Token.status == "called"
        )
        .order_by(Token.token_number.desc())
        .first()
    )

    set_queue_state(
        token.queue_id,
        current_token.token_number if current_token else None,
        waiting_count
    )

    publish_queue_update(
        token.queue_id,
        {
            "queue_id": token.queue_id,
            "skipped_token": token.token_number,
            "next_token": next_token.token_number if next_token else None,
            "current_token": current_token.token_number if current_token else None,
            "status": "skipped"
        }
    )

    return {
        "message": "Token skipped successfully",
        "token_number": token.token_number,
        "next_token": next_token.token_number if next_token else None,
        "status": "skipped"
    }


@app.post("/tokens/{token_id}/recall")
def recall_token(
    token_id: int,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role not in ["staff", "admin"]:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to recall tokens"
        )

    token = db.query(Token).filter(
        Token.id == token_id
    ).first()

    if not token:
        raise HTTPException(
            status_code=404,
            detail="Token not found"
        )

    queue = db.query(Queue).filter(
        Queue.id == token.queue_id
    ).first()

    if not queue:
        raise HTTPException(
            status_code=404,
            detail="Queue not found"
        )

    if queue.status != "active":
        raise HTTPException(
            status_code=409,
            detail="This queue is no longer active"
        )

    if token.status != "called":
        raise HTTPException(
            status_code=409,
            detail="Only a called token can be recalled"
        )


    create_notification(
        db,
        token.user_id,
        "Your Turn",
        f"Token #{token.token_number} is being recalled. Please proceed to the counter.",
        "token_recalled"
    )

    db.commit()

    publish_queue_update(
        token.queue_id,
        {
            "queue_id": token.queue_id,
            "token_number": token.token_number,
            "event": "token_recalled",
            "status": "called"
        }
    )

    return {
        "message": "Token recalled successfully",
        "token_number": token.token_number,
        "status": token.status
    }

@app.post("/login")
def login(
    login_data: LoginRequest,
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(
        User.email == login_data.email
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password"
        )

    if not bcrypt.checkpw(
        login_data.password.encode("utf-8"),
        user.password_hash.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password"
        )

    access_token = create_access_token(user.id)

    return {
        "message": "Login successful",
        "access_token": access_token,
        "user_id": user.id,
        "name": user.name,
        "role": user.role
    }

@app.get("/profile")
def profile(current_user: int = Depends(get_current_user)):
    return {
        "message": "You are authenticated",
        "user_id": current_user
    }

@app.get("/users/{user_id}/tokens")
def get_user_tokens(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    if user_id != current_user:
        raise HTTPException(
            status_code=403,
            detail="You can only view your own token history"
        )

    tokens = (
        db.query(Token)
        .filter(Token.user_id == user_id)
        .order_by(Token.joined_at.desc())
        .all()
    )

    return tokens

@app.get("/tokens/{token_id}")
def get_token_status(
    token_id: int,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    token = db.query(Token).filter(
        Token.id == token_id
    ).first()

    if not token:
        raise HTTPException(
            status_code=404,
            detail="Token not found"
        )

    if token.user_id != current_user:
        raise HTTPException(
            status_code=403,
            detail="You can only view your own token"
        )

    service = (
        db.query(Service)
        .join(Queue, Queue.service_id == Service.id)
        .filter(Queue.id == token.queue_id)
        .first()
    )

    current_token = (
        db.query(Token)
        .filter(
            Token.queue_id == token.queue_id,
            Token.status == "called"
        )
        .order_by(Token.token_number)
        .first()
    )

    current_number = (
        current_token.token_number
        if current_token
        else 0
    )

    people_ahead = (
        db.query(Token)
        .filter(
            Token.queue_id == token.queue_id,
            Token.status == "waiting",
            Token.token_number < token.token_number
        )
        .count()
    )

    estimated_wait = (
        people_ahead * service.average_service_time
        if service
        else 0
    )

    return {
        "token_number": token.token_number,
        "status": token.status,
        "current_token": current_number,
        "people_ahead": people_ahead,
        "estimated_wait_minutes": estimated_wait
    }

@app.post("/appointments")
def create_appointment(
    appointment: AppointmentCreate,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):

    service = db.query(Service).filter(
        Service.id == appointment.service_id
    ).first()

    if not service:
        raise HTTPException(
            status_code=404,
            detail="Service not found"
        )

    appointment_datetime = datetime.combine(
        appointment.appointment_date,
        appointment.appointment_time
    )

    if appointment_datetime <= datetime.now():
        raise HTTPException(
            status_code=400,
            detail="Appointment date and time must be in the future"
        )

    existing_appointment = db.query(Appointment).filter(
        Appointment.service_id == appointment.service_id,
        Appointment.appointment_date == appointment.appointment_date,
        Appointment.appointment_time == appointment.appointment_time,
        Appointment.status == "booked"
    ).first()

    if existing_appointment:
        raise HTTPException(
            status_code=409,
            detail="This appointment slot is already booked"
        )

    new_appointment = Appointment(
        user_id=current_user,
        service_id=appointment.service_id,
        appointment_date=appointment.appointment_date,
        appointment_time=appointment.appointment_time
    )

    db.add(new_appointment)

    try:
        db.commit()
        db.refresh(new_appointment)

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This appointment slot is already booked"
        )

    return new_appointment

@app.get("/appointments")
def get_my_appointments(
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    appointments = (
        db.query(Appointment)
        .filter(Appointment.user_id == current_user)
        .order_by(
            Appointment.appointment_date,
            Appointment.appointment_time
        )
        .all()
    )

    return appointments

@app.put("/appointments/{appointment_id}")
def reschedule_appointment(
    appointment_id: int,
    appointment: AppointmentCreate,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    existing_appointment = db.query(Appointment).filter(
        Appointment.id == appointment_id
    ).first()

    if not existing_appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found"
        )

    if existing_appointment.user_id != current_user:
        raise HTTPException(
            status_code=403,
            detail="You can only reschedule your own appointment"
        )

    appointment_datetime = datetime.combine(
        appointment.appointment_date,
        appointment.appointment_time
    )

    if appointment_datetime <= datetime.now():
        raise HTTPException(
            status_code=400,
            detail="Appointment date and time must be in the future"
        )

    existing_slot = db.query(Appointment).filter(
        Appointment.id != appointment_id,
        Appointment.service_id == appointment.service_id,
        Appointment.appointment_date == appointment.appointment_date,
        Appointment.appointment_time == appointment.appointment_time,
        Appointment.status == "booked"
    ).first()

    if existing_slot:
        raise HTTPException(
            status_code=409,
            detail="This appointment slot is already booked"
        )

    existing_appointment.appointment_date = appointment.appointment_date
    existing_appointment.appointment_time = appointment.appointment_time

    try:
        db.commit()
        db.refresh(existing_appointment)

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This appointment slot is already booked"
        )

    return existing_appointment

@app.delete("/appointments/{appointment_id}")
def cancel_appointment(
    appointment_id: int,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    appointment = db.query(Appointment).filter(
        Appointment.id == appointment_id
    ).first()

    if not appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found"
        )

    if appointment.user_id != current_user:
        raise HTTPException(
            status_code=403,
            detail="You can only cancel your own appointment"
        )

    appointment.status = "cancelled"

    db.commit()
    db.refresh(appointment)

    return {
        "message": "Appointment cancelled successfully",
        "appointment_id": appointment.id,
        "status": appointment.status
    }

@app.get("/staff/queues/{queue_id}")
def get_staff_queue(
    queue_id: int,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role not in ["staff", "admin"]:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to view the staff queue"
        )

    queue = db.query(Queue).filter(
        Queue.id == queue_id
    ).first()

    if not queue:
        raise HTTPException(
            status_code=404,
            detail="Queue not found"
        )

    service = db.query(Service).filter(
        Service.id == queue.service_id
    ).first()

    current_token = (
        db.query(Token)
        .filter(
            Token.queue_id == queue_id,
            Token.status == "called"
        )
        .order_by(Token.token_number.desc())
        .first()
    )

    waiting_tokens = (
        db.query(Token)
        .filter(
            Token.queue_id == queue_id,
            Token.status == "waiting"
        )
        .order_by(Token.token_number)
        .all()
    )

    return {
        "queue_id": queue.id,
        "service_name": service.name if service else None,
        "queue_status": queue.status,
        "current_token": (
            current_token.token_number
            if current_token
            else None
        ),
        "waiting_count": len(waiting_tokens),
        "waiting_tokens": [
            {
                "token_id": token.id,
                "token_number": token.token_number,
                "user_id": token.user_id,
                "joined_at": token.joined_at
            }
            for token in waiting_tokens
        ]
    }

@app.get("/staff/queues/{queue_id}/stats")
def get_queue_stats(
    queue_id: int,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role not in ["staff", "admin"]:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to view queue statistics"
        )

    queue = db.query(Queue).filter(
        Queue.id == queue_id
    ).first()

    if not queue:
        raise HTTPException(
            status_code=404,
            detail="Queue not found"
        )

    total_tokens = db.query(Token).filter(
        Token.queue_id == queue_id
    ).count()

    waiting_count = db.query(Token).filter(
        Token.queue_id == queue_id,
        Token.status == "waiting"
    ).count()

    called_count = db.query(Token).filter(
        Token.queue_id == queue_id,
        Token.status == "called"
    ).count()

    completed_count = db.query(Token).filter(
        Token.queue_id == queue_id,
        Token.status == "completed"
    ).count()

    skipped_count = db.query(Token).filter(
        Token.queue_id == queue_id,
        Token.status == "skipped"
    ).count()

    return {
        "queue_id": queue_id,
        "total_tokens": total_tokens,
        "waiting": waiting_count,
        "currently_serving": called_count,
        "completed": completed_count,
        "skipped": skipped_count
    }

@app.get("/staff/appointments")
def get_staff_appointments(
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role not in ["staff", "admin"]:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to view appointments"
        )

    appointments = (
        db.query(Appointment)
        .order_by(
            Appointment.appointment_date,
            Appointment.appointment_time
        )
        .all()
    )

    return appointments

@app.put("/staff/appointments/{appointment_id}/status")
def update_appointment_status(
    appointment_id: int,
    status: str,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role not in ["staff", "admin"]:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to update appointments"
        )

    appointment = db.query(Appointment).filter(
        Appointment.id == appointment_id
    ).first()

    if not appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found"
        )

    allowed_statuses = [
        "booked",
        "confirmed",
        "cancelled",
        "completed"
    ]

    if status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail="Invalid appointment status"
        )

    appointment.status = status

    try:
        db.commit()
        db.refresh(appointment)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Unable to update appointment status"
        )

    return {
        "message": "Appointment status updated successfully",
        "appointment_id": appointment.id,
        "status": appointment.status
    }

@app.get("/admin/users")
def get_all_users(
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admin can view all users"
        )

    users = (
        db.query(User)
        .order_by(User.id)
        .all()
    )

    return [
        {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role,
            "created_at": user.created_at
        }
        for user in users
    ]

@app.put("/admin/users/{user_id}/role")
def update_user_role(
    user_id: int,
    role: str,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    admin = db.query(User).filter(
        User.id == current_user
    ).first()

    if not admin:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if admin.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admin can update user roles"
        )

    user = db.query(User).filter(
        User.id == user_id
    ).first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    allowed_roles = ["customer", "staff", "admin"]

    if role not in allowed_roles:
        raise HTTPException(
            status_code=400,
            detail="Invalid role"
        )

    user.role = role

    db.commit()
    db.refresh(user)

    return {
        "message": "User role updated successfully",
        "user_id": user.id,
        "role": user.role
    }

@app.get("/admin/services")
def get_all_services(
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admin can view all services"
        )

    services = (
        db.query(Service)
        .order_by(Service.id)
        .all()
    )

    return [
        {
            "id": service.id,
            "name": service.name,
            "description": service.description,
            "average_service_time": service.average_service_time,
            "is_active": service.is_active,
            "created_at": service.created_at
        }
        for service in services
    ]

@app.post("/admin/services")
def create_admin_service(
    service: ServiceCreate,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admin can create services"
        )

    new_service = Service(
        name=service.name,
        description=service.description,
        average_service_time=service.average_service_time
    )

    db.add(new_service)

    try:
        db.commit()
        db.refresh(new_service)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Unable to create service"
        )

    return new_service

@app.put("/admin/services/{service_id}")
def update_service(
    service_id: int,
    service: ServiceCreate,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admin can update services"
        )

    existing_service = db.query(Service).filter(
        Service.id == service_id
    ).first()

    if not existing_service:
        raise HTTPException(
            status_code=404,
            detail="Service not found"
        )

    existing_service.name = service.name
    existing_service.description = service.description
    existing_service.average_service_time = service.average_service_time

    db.commit()
    db.refresh(existing_service)

    return existing_service

@app.put("/admin/services/{service_id}/toggle")
def toggle_service(
    service_id: int,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admin can change service status"
        )

    service = db.query(Service).filter(
        Service.id == service_id
    ).first()

    if not service:
        raise HTTPException(
            status_code=404,
            detail="Service not found"
        )

    service.is_active = not service.is_active

    db.commit()
    db.refresh(service)

    return {
        "message": "Service status updated successfully",
        "service_id": service.id,
        "is_active": service.is_active
    }

@app.get("/admin/queues")
def get_all_queues(
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admin can view all queues"
        )

    queues = (
        db.query(Queue)
        .order_by(Queue.id)
        .all()
    )

    result = []

    for queue in queues:
        service = db.query(Service).filter(
            Service.id == queue.service_id
        ).first()

        waiting_count = db.query(Token).filter(
            Token.queue_id == queue.id,
            Token.status == "waiting"
        ).count()

        current_token = db.query(Token).filter(
            Token.queue_id == queue.id,
            Token.status == "called"
        ).order_by(
            Token.token_number.desc()
        ).first()

        result.append({
            "queue_id": queue.id,
            "service_name": service.name if service else None,
            "queue_date": queue.queue_date,
            "status": queue.status,
            "current_token": (
                current_token.token_number
                if current_token else None
            ),
            "waiting_count": waiting_count
        })

    return result

@app.get("/admin/dashboard/stats")
def get_admin_dashboard_stats(
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    user = db.query(User).filter(
        User.id == current_user
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admin can view dashboard stats"
        )

    total_users = db.query(User).count()
    total_services = db.query(Service).count()
    total_queues = db.query(Queue).count()
    total_tokens = db.query(Token).count()

    completed_tokens = db.query(Token).filter(
        Token.status == "completed"
    ).count()

    skipped_tokens = db.query(Token).filter(
        Token.status == "skipped"
    ).count()

    waiting_tokens = db.query(Token).filter(
        Token.status == "waiting"
    ).count()

    total_appointments = db.query(Appointment).count()

    return {
        "total_users": total_users,
        "total_services": total_services,
        "total_queues": total_queues,
        "total_tokens": total_tokens,
        "completed_tokens": completed_tokens,
        "skipped_tokens": skipped_tokens,
        "waiting_tokens": waiting_tokens,
        "total_appointments": total_appointments
    }

class ConnectionManager:

    def __init__(self):
        self.active_connections = {}

    async def connect(self, websocket: WebSocket, queue_id: int):
        await websocket.accept()

        if queue_id not in self.active_connections:
            self.active_connections[queue_id] = []

        self.active_connections[queue_id].append(websocket)

    def disconnect(self, websocket: WebSocket, queue_id: int):
        if queue_id in self.active_connections:
            if websocket in self.active_connections[queue_id]:
                self.active_connections[queue_id].remove(websocket)

    async def broadcast(self, queue_id: int, message: dict):
        if queue_id in self.active_connections:
            for connection in self.active_connections[queue_id]:
                await connection.send_json(message)

manager = ConnectionManager()
redis_client = redis.Redis(
    host="queueless-redis",
    port=6379,
    decode_responses=True
)
redis_async_client = redis_async.Redis(
    host="queueless-redis",
    port=6379,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_timeout=5
)

def publish_queue_update(queue_id: int, message: dict):
    channel = f"queue:{queue_id}"
    redis_client.publish(
        channel,
        json.dumps(message)
    )



import asyncio


def set_queue_state(queue_id: int, current_token: int | None, waiting_count: int):
    redis_client.hset(
        f"queue:{queue_id}",
        mapping={
            "current_token": current_token or 0,
            "waiting_count": waiting_count
        }
    )


def get_queue_state(queue_id: int):
    data = redis_client.hgetall(f"queue:{queue_id}")

    if not data:
        return None

    return {
        "current_token": int(data.get("current_token", 0)),
        "waiting_count": int(data.get("waiting_count", 0))
    }


@app.websocket("/ws/test")
async def websocket_test(websocket: WebSocket):

    await websocket.accept()

    try:
        while True:
            message = await websocket.receive_text()
            await websocket.send_text(
                f"Server received: {message}"
            )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")

@app.websocket("/ws/queues/{queue_id}")
async def queue_websocket(
    websocket: WebSocket,
    queue_id: int,
    token: str | None = Query(default=None)
):
    if not token:
        await websocket.close(code=1008)
        return

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )

        user_id = payload.get("user_id")

        if user_id is None:
            await websocket.close(code=1008)
            return

    except jwt.JWTError:
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, queue_id)

    pubsub = redis_async_client.pubsub()

    await pubsub.subscribe(f"queue:{queue_id}")

    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0
            )

            if message:

                await websocket.send_text(message["data"])

            await asyncio.sleep(0.01)

    except WebSocketDisconnect:
        manager.disconnect(websocket, queue_id)

    finally:
        await pubsub.unsubscribe(f"queue:{queue_id}")
        await pubsub.close()

@app.get("/notifications/unread-count")
def get_unread_notification_count(
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    unread_count = (
        db.query(Notification)
        .filter(
            Notification.user_id == current_user,
            Notification.is_read == False
        )
        .count()
    )

    return {
        "unread_count": unread_count
    }

@app.post("/tokens/{token_id}/check-in")
def check_in_token(
    token_id: int,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    token = db.query(Token).filter(
        Token.id == token_id
    ).first()

    if not token:
        raise HTTPException(
            status_code=404,
            detail="Token not found"
        )

    if token.user_id != current_user:
        raise HTTPException(
            status_code=403,
            detail="You can only check in your own token"
        )

    queue = db.query(Queue).filter(
        Queue.id == token.queue_id
    ).first()

    if not queue:
        raise HTTPException(
            status_code=404,
            detail="Queue not found"
        )

    if queue.queue_date != datetime.now().date():
        raise HTTPException(
            status_code=409,
            detail="Check-in is only available for today's queue"
        )

    if queue.status != "active":
        raise HTTPException(
            status_code=409,
            detail="This queue is no longer active"
        )

    if token.status != "waiting":
        raise HTTPException(
            status_code=409,
            detail="Only waiting tokens can be checked in"
        )

    if token.checked_in:
        raise HTTPException(
            status_code=409,
            detail="Token already checked in"
        )

    token.checked_in = True
    token.checked_in_at = datetime.utcnow()

    db.commit()
    db.refresh(token)

    return {
        "message": "Check-in successful",
        "token_id": token.id,
        "token_number": token.token_number,
        "checked_in": token.checked_in,
        "checked_in_at": token.checked_in_at
    }


@app.get("/tokens/{token_id}/qr")
def generate_token_qr(
    token_id: int,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    token = db.query(Token).filter(
        Token.id == token_id
    ).first()

    if not token:
        raise HTTPException(
            status_code=404,
            detail="Token not found"
        )

    if token.user_id != current_user:
        raise HTTPException(
            status_code=403,
            detail="You can only access your own token QR"
        )

    qr_data = f"{APP_BASE_URL}/tokens/{token_id}/check-in"

    qr = qrcode.make(qr_data)

    buffer = BytesIO()
    qr.save(buffer, format="PNG")
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="image/png"
    )

@app.get("/notifications")
def get_notifications(
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    notifications = (
        db.query(Notification)
        .filter(Notification.user_id == current_user)
        .order_by(Notification.id.desc())
        .all()
    )

    return notifications

@app.put("/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    notification = (
        db.query(Notification)
        .filter(
            Notification.id == notification_id,
            Notification.user_id == current_user
        )
        .first()
    )

    if not notification:
        raise HTTPException(
            status_code=404,
            detail="Notification not found"
        )

    notification.is_read = True

    db.commit()
    db.refresh(notification)

    return {
        "message": "Notification marked as read",
        "notification_id": notification.id,
        "is_read": notification.is_read
    }

@app.put("/notifications/read-all")
def mark_all_notifications_read(
    db: Session = Depends(get_db),
    current_user: int = Depends(get_current_user)
):
    updated_count = (
        db.query(Notification)
        .filter(
            Notification.user_id == current_user,
            Notification.is_read == False
        )
        .update(
            {"is_read": True},
            synchronize_session=False
        )
    )

    db.commit()

    return {
        "message": "All notifications marked as read",
        "updated_count": updated_count
    }

@app.get("/queues/service/{service_id}")
def get_service_queue(
    service_id: int,
    db: Session = Depends(get_db)
):
    queue = (
        db.query(Queue)
        .filter(
            Queue.service_id == service_id,
            Queue.queue_date == datetime.now().date(),
            Queue.status == "active"
        )
        .first()
    )

    if not queue:
        raise HTTPException(
            status_code=404,
            detail="No active queue available for this service today"
        )

    return {
        "queue_id": queue.id,
        "service_id": queue.service_id,
        "queue_date": queue.queue_date,
        "status": queue.status
    }

@app.get("/tokens/{token_id}/position")
def get_token_position(
    token_id: int,
    db: Session = Depends(get_db)
):
    token = (
        db.query(Token)
        .filter(Token.id == token_id)
        .first()
    )

    if not token:
        raise HTTPException(
            status_code=404,
            detail="Token not found"
        )

    queue = (
        db.query(Queue)
        .filter(Queue.id == token.queue_id)
        .first()
    )

    if not queue:
        raise HTTPException(
            status_code=404,
            detail="Queue not found"
        )

    customers_ahead = (
        db.query(Token)
        .filter(
            Token.queue_id == token.queue_id,
            Token.token_number < token.token_number,
            Token.status == "waiting"
        )
        .count()
    )

    position = customers_ahead + 1

    return {
        "token_id": token.id,
        "token_number": token.token_number,
        "queue_id": token.queue_id,
        "service_id": queue.service_id,
        "position": position,
        "status": token.status
    }

@app.post("/tokens/{token_id}/cancel")
def cancel_token(
    token_id: int,
    db: Session = Depends(get_db),
    current_user_id: int = Depends(get_current_user)
):
    token = (
        db.query(Token)
        .filter(Token.id == token_id)
        .first()
    )

    if not token:
        raise HTTPException(
            status_code=404,
            detail="Token not found"
        )

    # Make sure the customer can cancel only their own token
    if token.user_id != current_user_id:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to cancel this token"
        )

    # Only waiting tokens can be cancelled
    if token.status != "waiting":
        raise HTTPException(
            status_code=409,
            detail="Only a waiting token can be cancelled"
        )

    token.status = "cancelled"

    db.commit()
    db.refresh(token)

    return {
        "message": "Token cancelled successfully",
        "token_id": token.id,
        "token_number": token.token_number,
        "status": token.status
    }