from typing import List, Optional, Dict
from pydantic import BaseModel, Field


class PersonSchema(BaseModel):
    name: str
    birth_year: Optional[int] = None
    death_year: Optional[int] = None

    class Config:
        from_attributes = True


class BookshelfSchema(BaseModel):
    name: str

    class Config:
        from_attributes = True


class LanguageSchema(BaseModel):
    code: str

    class Config:
        from_attributes = True


class SubjectSchema(BaseModel):
    name: str

    class Config:
        from_attributes = True


class SummarySchema(BaseModel):
    text: str

    class Config:
        from_attributes = True


class BookSchema(BaseModel):
    id: int = Field(..., alias="gutenberg_id")
    title: Optional[str] = None
    authors: List[PersonSchema] = []
    summaries: List[str] = []
    editors: List[PersonSchema] = []
    translators: List[PersonSchema] = []
    subjects: List[str] = []
    bookshelves: List[str] = []
    languages: List[str] = []
    copyright: Optional[bool] = None
    media_type: str
    formats: Dict[str, str] = {}
    download_count: Optional[int] = None

    class Config:
        from_attributes = True
        populate_by_name = True


class BookListResponse(BaseModel):
    count: int
    next: Optional[str] = None
    previous: Optional[str] = None
    results: List[BookSchema] = []


class HealthCheckResponse(BaseModel):
    status: str
