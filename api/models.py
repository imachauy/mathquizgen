from pydantic import BaseModel
from datetime import datetime, timezone
from typing import Optional

class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str

class UserLogin(BaseModel):
    username: str
    password: str

class UserCreate(BaseModel):
    username: str
    password: str
    email: str
    display_name: str

class UserLti(BaseModel):
    user_id: int
    username: str
    display_name: str
    roles: str
    email: str
    school: str

class UserProfile(BaseModel):
    id: int
    display_name: str
    bio: str
    avatar: str

class User(BaseModel):
    id: int
    is_active: bool
    profiles: UserProfile
    username: str
    email: str
    is_admin: bool
    user_type: str