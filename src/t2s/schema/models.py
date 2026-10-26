from pydantic import BaseModel, Field
from typing import List, Optional

class ColumnModel(BaseModel):
    name: str
    data_type: str
    is_primary_key: bool = False
    is_nullable: bool = True
    description: Optional[str] = None

class ForeignKeyModel(BaseModel):
    constrained_columns: List[str]
    referred_table: str
    referred_columns: List[str]

class TableModel(BaseModel):
    name: str
    columns: List[ColumnModel]
    foreign_keys: List[ForeignKeyModel] = Field(default_factory=list)
    description: Optional[str] = None

class DatabaseSchema(BaseModel):
    tables: List[TableModel]