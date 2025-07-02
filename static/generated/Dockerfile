FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
EXPOSE 4000
CMD ["gunicorn", "--bind", "0.0.0.0:4000", "app:app"]