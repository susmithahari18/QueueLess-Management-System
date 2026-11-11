from pydantic import BaseModel,EmailStr, field_validator
from datetime import date, time

class ServiceCreate(BaseModel):
    name: str
    description: str | None = None
    average_service_time: int = 10

    @field_validator("average_service_time")
    @classmethod
    def validate_service_time(cls, value):
        if value <= 0:
            raise ValueError("Average service time must be greater than 0")
        return value

class QueueCreate(BaseModel):
    service_id: int

    @field_validator("service_id")
    @classmethod
    def validate_service_id(cls, value):
        if value <= 0:
            raise ValueError("Service ID must be greater than 0")
        return value

class TokenCreate(BaseModel):
    queue_id: int

    @field_validator("queue_id")
    @classmethod
    def validate_queue_id(cls, value):
        if value <= 0:
            raise ValueError("Queue ID must be greater than 0")
        return value

class UserCreate(BaseModel):
    name: str
    email: EmailStr
    password: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        if not value.strip():
            raise ValueError("Name cannot be empty")
        return value.strip()

    @field_validator("password")
    @classmethod
    def validate_password(cls, value):
        if len(value) < 8:
            raise ValueError("Password must be at least 8 characters")

        if not any(char.isupper() for char in value):
            raise ValueError(
                "Password must contain at least one uppercase letter"
            )

        if not any(char.isdigit() for char in value):
            raise ValueError(
                "Password must contain at least one number"
            )

        return value

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class AppointmentCreate(BaseModel):
    service_id: int
    appointment_date: date
    appointment_time: time

    @field_validator("service_id")
    @classmethod
    def validate_service_id(cls, value):
        if value <= 0:
            raise ValueError("Service ID must be greater than 0")
        return value