from sqlalchemy.sql import func

from database import Base
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    Date,
    Time
)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    email = Column(String(150), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(String(20), nullable=False, default="customer")
    created_at = Column(DateTime, server_default=func.now())

class Service(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    average_service_time = Column(Integer, nullable=False, default=10)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

class Queue(Base):
    __tablename__ = "queues"

    id = Column(Integer, primary_key=True)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=False)
    queue_date = Column(Date, nullable=False, server_default=func.current_date())
    status = Column(String(20), nullable=False, default="active")
    created_at = Column(DateTime, server_default=func.now())

class Token(Base):
    __tablename__ = "tokens"

    id = Column(Integer, primary_key=True)
    queue_id = Column(Integer, ForeignKey("queues.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    token_number = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="waiting")
    joined_at = Column(DateTime, server_default=func.now())
    called_at = Column(DateTime)
    completed_at = Column(DateTime)
    checked_in = Column(Boolean, default=False)
    checked_in_at = Column(DateTime)

class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=False)
    appointment_date = Column(Date, nullable=False)
    appointment_time = Column(Time, nullable=False)
    status = Column(String(20), nullable=False, default="booked")
    created_at = Column(DateTime, server_default=func.now())

class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(150), nullable=False)
    message = Column(Text, nullable=False)
    notification_type = Column(String(50), nullable=False)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
