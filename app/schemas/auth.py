from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.security import validate_password_strength


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # access token lifetime, seconds
    refresh_token: Optional[str] = None
    refresh_expires_in: Optional[int] = None  # seconds


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=20, max_length=512)


class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = Field(None, max_length=512)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=1, max_length=256)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    is_active: bool = True
    is_superuser: bool = False
    last_login_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(..., min_length=1, max_length=256)
    full_name: Optional[str] = Field(None, max_length=100)
    email: Optional[str] = Field(None, max_length=150)
    is_superuser: bool = False

    @field_validator("password")
    @classmethod
    def _strong_password(cls, v, info):
        try:
            validate_password_strength(v, info.data.get("username"))
        except ValueError as e:
            raise ValueError(str(e))
        return v


class UserUpdate(BaseModel):
    full_name: Optional[str] = Field(None, max_length=100)
    email: Optional[str] = Field(None, max_length=150)
    is_active: Optional[bool] = None
    password: Optional[str] = Field(None, max_length=256)

    @field_validator("password")
    @classmethod
    def _strong_password(cls, v):
        if v:
            try:
                validate_password_strength(v)
            except ValueError as e:
                raise ValueError(str(e))
        return v


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    description: Optional[str] = None


class SystemSettingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    key: str
    value: Optional[str] = None
    description: Optional[str] = None
    updated_at: Optional[datetime] = None


class SystemSettingUpdate(BaseModel):
    value: Optional[str] = None
    description: Optional[str] = None
