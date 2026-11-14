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
    # True when the account still uses the built-in default administrator
    # password: the frontend must send the user to change it before any other
    # feature can be used.
    must_change_password: bool = False


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
    # Only /auth/me computes this: True when the account still uses the
    # built-in default administrator password.
    must_change_password: bool = False
    # 角色 code 列表；/auth/me 另带 pages（允许打开的页面 id，None=不限制）和 permission（给人看的权限说明）
    roles: list[str] = []
    pages: Optional[list[str]] = None
    permission: Optional[str] = None


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(..., min_length=1, max_length=256)
    full_name: Optional[str] = Field(None, max_length=100)
    email: Optional[str] = Field(None, max_length=150)
    is_superuser: bool = False
    roles: list[str] = []          # 角色 code（admin / engineer / viewer）

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


class RoleCreate(BaseModel):
    """新建自定义角色。code 只允许小写字母、数字、下划线；新建角色初始没有任何权限。"""
    code: str = Field(..., min_length=2, max_length=50, pattern=r"^[a-z0-9_]+$")
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)


class UserSelfUpdate(BaseModel):
    """本人改自己的资料：只允许姓名与邮箱，改不了超管标志、状态与密码。"""
    full_name: Optional[str] = Field(None, max_length=100)
    email: Optional[str] = Field(None, max_length=150)


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


class RolePermissionsOut(BaseModel):
    id: int
    code: str
    name: str
    description: Optional[str] = None
    permissions: list[str] = []


class PermissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str
    module: Optional[str] = None


class RolePermissionsUpdate(BaseModel):
    permissions: list[str] = Field(default_factory=list, max_length=500)


class UserRolesUpdate(BaseModel):
    roles: list[str] = Field(default_factory=list, max_length=20)
