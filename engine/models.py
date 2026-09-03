from pydantic import BaseModel
from typing import List, Optional, Any


class ExtractRequest(BaseModel):
    content: str
    content_type: Optional[str] = "text"
    extraction_type: Optional[str] = "entities"


class ExtractResponse(BaseModel):
    entities: List[Any]


class RewriteRequest(BaseModel):
    content: str
    style: Optional[dict] = None
    model: Optional[str] = None


class JobResponse(BaseModel):
    job_id: str


class VideoProcessRequest(BaseModel):
    source_url: str
    operations: List[dict]
    callback_url: Optional[str] = None


class AISummarizeRequest(BaseModel):
    content: str
    length: Optional[str] = "short"
    model: Optional[str] = None


class AIRewriteRequest(BaseModel):
    content: str
    style: Optional[dict] = None
    model: Optional[str] = None
