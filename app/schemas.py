from pydantic import BaseModel
from datetime import datetime
from typing import List, Literal, Optional

class Skill(BaseModel):
    name: str
    type: Literal["language", "framework", "tool"]
    self_assessment_level: Literal["junior", "middle", "senior"]
    years_of_experience: Optional[float] = None

class Interest(BaseModel):
    name: str
    category: str

class ProfileBase(BaseModel):
    username: str
    bio: str = ""
    stack: List[str] = []
    experience: str = ""
    skills: List[Skill] = []
    interests: List[Interest] = []
    short_term_goals: List[str] = []
    long_term_goals: List[str] = []

class ProfileCreate(ProfileBase):
    pass

class ProfileUpdate(BaseModel):
    username: Optional[str] = None
    bio: Optional[str] = None
    stack: Optional[List[str]] = None
    experience: Optional[str] = None
    skills: Optional[List[Skill]] = None
    interests: Optional[List[Interest]] = None
    short_term_goals: Optional[List[str]] = None
    long_term_goals: Optional[List[str]] = None

class ProfileResponse(ProfileBase):
    id: int
    user_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class MonkeytypeProxyRequest(BaseModel):
    username: str