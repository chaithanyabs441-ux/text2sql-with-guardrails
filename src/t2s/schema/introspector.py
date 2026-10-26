from sqlalchemy import create_engine, inspect
from t2s.config import settings
from t2s.schema.models import DatabaseSchema, TableModel, ColumnModel, ForeignKeyModel
import logging

logger = logging.getLogger(__name__)

class SchemaIntrospector:
    def __init__(self, db_url: str = settings.DATABASE_URL):
        self.engine = create_engine(db_url)
        self.inspector = inspect(self.engine)

    def extract_schema(self) -> DatabaseSchema:
        """Extracts all tables, columns, and FKs from the public and core schemas."""
        tables = []
        
        # We specifically target our 'core' schema where the business data lives
        for table_name in self.inspector.get_table_names(schema="core"):
            columns = []
            for col in self.inspector.get_columns(table_name, schema="core"):
                columns.append(ColumnModel(
                    name=col["name"],
                    data_type=str(col["type"]),
                    is_nullable=col["nullable"],
                    # Postgres primary keys are extracted separately below
                ))
                
            # Get primary keys
            pk_constraint = self.inspector.get_pk_constraint(table_name, schema="core")
            pks = pk_constraint.get("constrained_columns", [])
            for c in columns:
                if c.name in pks:
                    c.is_primary_key = True
                    
            # Get foreign keys
            fks = []
            for fk in self.inspector.get_foreign_keys(table_name, schema="core"):
                fks.append(ForeignKeyModel(
                    constrained_columns=fk["constrained_columns"],
                    referred_table=fk["referred_table"],
                    referred_columns=fk["referred_columns"]
                ))
                
            tables.append(TableModel(
                name=table_name,
                columns=columns,
                foreign_keys=fks
            ))
            
        logger.info(f"Extracted {len(tables)} tables from the database.")
        return DatabaseSchema(tables=tables)

if __name__ == "__main__":
    # Test script
    logging.basicConfig(level=logging.INFO)
    introspector = SchemaIntrospector()
    schema = introspector.extract_schema()
    print(f"Found {len(schema.tables)} tables.")
    for t in schema.tables[:3]: # Print first 3
        print(f"- {t.name} ({len(t.columns)} cols)")