FROM python:3.11-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# Install the CPU wheel first; the default PyPI torch wheel now pulls CUDA libraries.
RUN python -m pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple \
    torch
RUN python -m pip install --no-cache-dir .
RUN python -c "from t2s.api.main import app; assert app.title == 'Trustworthy Text-to-SQL API'"

FROM app AS api
EXPOSE 8000
CMD ["uvicorn", "t2s.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM app AS frontend
COPY frontend ./frontend
RUN python -c "import pandas, requests, streamlit"
EXPOSE 8501
CMD ["streamlit", "run", "frontend/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
