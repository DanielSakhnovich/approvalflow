FROM python:3.12-slim
WORKDIR /app
COPY libs/afcommon /opt/afcommon
COPY services/audit/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt /opt/afcommon
COPY services/audit/src ./src
EXPOSE 8000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
