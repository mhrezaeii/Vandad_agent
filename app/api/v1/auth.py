"""Authentication endpoints for Vandad Agent."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, EmailStr
from typing import Optional
import uuid

router = APIRouter(prefix="/auth", tags=["authentication"])


class LoginRequest(BaseModel):
    email: EmailStr = Field(..., description="User email")
    password: str = Field(..., min_length=8, description="User password")
    remember: bool = Field(default=False, description="Remember me")


class RegisterRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=100, description="Full name")
    email: EmailStr = Field(..., description="User email")
    password: str = Field(..., min_length=8, description="User password")


class UserResponse(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    full_name: str
    email: str
    company_id: Optional[str] = None
    status: str = "pending_approval"  # New accounts start pending approval


@router.post("/login", response_model=UserResponse, status_code=status.HTTP_200_OK)
async def login(request: LoginRequest):
    """Authenticate a user and return user info.
    
    Note: In production, this would verify credentials against a user store.
    For now, we create a session user with pending_approval status.
    """
    # Simulate user lookup - in production verify against database
    user = UserResponse(
        full_name=request.full_name or "User",
        email=request.email,
    )
    return user


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_OK)
async def register(request: RegisterRequest):
    """Register a new account.
    
    New accounts start with pending_approval status.
    Administrator must approve before the user can access the dashboard.
    """
    user = UserResponse(
        full_name=request.full_name,
        email=request.email,
    )
    return user