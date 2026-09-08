from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=10, strict=True)

    @field_validator('question')
    @classmethod
    def validate_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('question은 빈 문자열일 수 없습니다.')
        return value


class SourceItem(BaseModel):
    title: str
    url: str
    collected_at: Optional[datetime] = None


class AskResponse(BaseModel):
    answer: str
    sources: list[SourceItem]
    domain: str
