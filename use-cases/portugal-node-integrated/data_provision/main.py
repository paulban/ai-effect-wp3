import io
from enum import Enum
from typing import Annotated, Dict, List
import clickhouse_connect
from clickhouse_connect.driver.exceptions import ClickHouseError
from fastapi import Body, FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import (
    CLICKHOUSE_HOST,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
    CLICKHOUSE_PASSWORD,
)


# --- Client Helper ---
def get_ch_client():
    """Returns a configured ClickHouse client."""
    try:
        client = clickhouse_connect.get_client(
            host=CLICKHOUSE_HOST,
            port=CLICKHOUSE_PORT,
            user=CLICKHOUSE_USER,
            password=CLICKHOUSE_PASSWORD,
        )
        client.ping()
        return client
    except ClickHouseError as e:
        raise HTTPException(
            status_code=503, detail=f"Could not connect to ClickHouse database: {e}"
        )


# --- Pydantic Models for Request and Response ---
class QueryRequest(BaseModel):
    sql_query: str


class OutputFormat(str, Enum):
    json = "json"
    csv = "csv"
    xlsx = "xlsx"
    parquet = "parquet"


# --- Pydantic Models for the NEW /schema endpoint ---
class ColumnInfo(BaseModel):
    name: str
    type: str
    comment: str | None = None


class TableInfo(BaseModel):
    name: str
    comment: str | None = None
    columns: List[ColumnInfo]


class SchemaResponse(BaseModel):
    schemas: Dict[str, List[TableInfo]]


# --- FastAPI Application ---
app = FastAPI(
    title="ClickHouse Query API",
    description=(
        "An API to execute read-only queries against a ClickHouse database and retrieve results in various formats.\n"
    ),
    version="1.0.0",
    swagger_ui_parameters={"defaultModelsExpandDepth": -1},
)

# --- AI-Effect Control Interface ---
try:
    from common import create_control_router, data_provision_handlers

    app.include_router(
        create_control_router(data_provision_handlers),
        prefix="/control",
    )
except ImportError as e:
    import logging as _logging
    _logging.warning(f"AI-Effect control interface not available: {e}")


@app.get(
    "/schema",
    response_model=SchemaResponse,
    summary="Get Database Schema",
    description="Returns detailed information about all schemas, tables, columns, and their comments in the database.",
    tags=["Schema Information"],
)
async def get_database_schema():
    """
    Retrieves and returns the full schema of the ClickHouse database.
    """
    try:
        client = get_ch_client()
        schema_query = """
        SELECT
            t.database AS table_schema,
            t.name AS table_name,
            t.comment AS table_comment,
            c.name AS column_name,
            c.type AS data_type,
            c.comment AS column_comment
        FROM system.tables AS t
        JOIN system.columns AS c
          ON t.database = c.database AND t.name = c.table_name
        WHERE t.database NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema', 'default')
        ORDER BY t.database, t.name, c.position;
        """
        result_df = client.query_df(schema_query)

    except ClickHouseError as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve schema: {e}")

    schemas_dict = {}
    for (schema_name, table_name), table_df in result_df.groupby(
        ["table_schema", "table_name"]
    ):
        if schema_name not in schemas_dict:
            schemas_dict[schema_name] = []

        table_comment = table_df["table_comment"].iloc[0]
        columns = [
            ColumnInfo(
                name=row["column_name"],
                type=row["data_type"],
                comment=row["column_comment"],
            )
            for _, row in table_df.iterrows()
        ]

        if not any(t.name == table_name for t in schemas_dict[schema_name]):
            schemas_dict[schema_name].append(
                TableInfo(name=table_name, comment=table_comment, columns=columns)
            )

    return SchemaResponse(schemas=schemas_dict)


# --- Query Examples for the /query endpoint ---
QUERY_EXAMPLES = {
    "get_10_rows": {
        "summary": "1. Get 10 Rows",
        "description": "A simple query to fetch the first 10 rows and see the table structure.",
        "value": {"sql_query": "SELECT * FROM omie.precios_pibcic LIMIT 10;"},
    },
    "count_rows": {
        "summary": "2. Count Total Rows",
        "description": "A basic but useful query to get the size of the dataset.",
        "value": {
            "sql_query": "SELECT COUNT(*) as total_rows FROM omie.precios_pibcic;"
        },
    },
    "filter_and_order": {
        "summary": "3. Filter, Select, and Order",
        "description": "Shows how to select specific columns (note the double quotes for mixed-case names), filter by date, and sort the results.",
        "value": {
            "sql_query": """SELECT "Timestamp", "MedioES", "MedioPT" FROM omie.precios_pibcic WHERE "Timestamp" > '2024-01-01' ORDER BY "Timestamp" DESC LIMIT 100;"""
        },
    },
    "aggregate_by_year": {
        "summary": "4. Group by Year and Aggregate",
        "description": "A powerful example showing how to perform aggregations (`avg`) and group data (`GROUP BY`) using ClickHouse's date functions.",
        "value": {
            "sql_query": """SELECT toYear("Timestamp") AS anio, avg("MedioES") AS precio_medio_espanol FROM omie.precios_pibcic WHERE "MedioES" IS NOT NULL GROUP BY anio ORDER BY anio DESC;"""
        },
    },
    "calculate_spread": {
        "summary": "5. Calculate a New Column (Spread)",
        "description": "Shows how to perform calculations on the fly and find the top 10 periods with the largest price difference.",
        "value": {
            "sql_query": """SELECT "Timestamp", "MáximoES", "MínimoES", ("MáximoES" - "MínimoES") AS spread_espanol FROM omie.precios_pibcic WHERE "MáximoES" IS NOT NULL AND "MínimoES" IS NOT NULL ORDER BY spread_espanol DESC LIMIT 10;"""
        },
    },
    "describe_table": {
        "summary": "6. Describe Table Schema",
        "description": "A useful ClickHouse command that returns the schema of the table, including column names and types.",
        "value": {"sql_query": "DESCRIBE omie.precios_pibcic;"},
    },
}


@app.post(
    "/query",
    response_description="The query result in the specified file format.",
    tags=["Query Execution"],
)
async def execute_query(
    query_request: Annotated[
        QueryRequest,
        Body(openapi_examples=QUERY_EXAMPLES),
    ],
    format: OutputFormat = OutputFormat.csv,
):
    """
    Executes a read-only SQL query and returns the result as a downloadable file.

    - **query_request**: A JSON object containing the `sql_query`.
    - **format**: The desired output format (`json`, `csv`, `xlsx`, `parquet`). Defaults to `csv`.
    """
    try:
        client = get_ch_client()
        result_df = client.query_df(query_request.sql_query)
    except ClickHouseError as e:
        raise HTTPException(
            status_code=400, detail=f"Invalid SQL Query or database error: {e}"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An internal error occurred: {e}")

    if result_df.empty:
        return Response(status_code=204)

    if format == OutputFormat.json:
        # Timestamps need to be converted to strings for JSON serialization
        for col in result_df.select_dtypes(include=["datetime64[ns]"]).columns:
            result_df[col] = result_df[col].astype(str)
        json_string = result_df.to_json(orient="records")
        return Response(content=json_string, media_type="application/json")

    stream = io.BytesIO()
    if format == OutputFormat.csv:
        result_df.to_csv(stream, index=False, encoding="utf-8")
        media_type = "text/csv"
        filename = "query_result.csv"
    elif format == OutputFormat.xlsx:
        result_df.to_excel(stream, index=False, engine="openpyxl")
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = "query_result.xlsx"
    elif format == OutputFormat.parquet:
        result_df.to_parquet(stream, index=False, engine="pyarrow")
        media_type = "application/octet-stream"
        filename = "query_result.parquet"
    stream.seek(0)
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    return StreamingResponse(content=stream, media_type=media_type, headers=headers)


@app.get("/", include_in_schema=False)
async def root():
    return {"message": "Welcome to the ClickHouse Query API. See /docs for usage."}
