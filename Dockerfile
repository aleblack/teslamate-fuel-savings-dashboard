FROM python:3.13-alpine

WORKDIR /app

RUN pip install --no-cache-dir pg8000 schedule

COPY fetch_fuel_price.py .

CMD ["python3", "-u", "fetch_fuel_price.py"]
