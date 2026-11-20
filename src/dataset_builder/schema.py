"""Record schema for the dataset. Adapt the fields to your use case."""

from pydantic import BaseModel, Field


class Record(BaseModel):
    id: str
    text: str = Field(min_length=1)
    label: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
