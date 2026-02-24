# ใช้ GDAL Base Image ที่ติดตั้ง GIS Tools มาให้แล้ว
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.6.3

# ติดตั้ง Python และ pip
RUN apt-get update && \
    apt-get install -y python3-pip python3-venv && \
    rm -rf /var/lib/apt/lists/*

# สร้างและ activate virtual environment
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# ตั้ง Working Directory
WORKDIR /app

# Copy และติดตั้ง dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy โค้ดที่เหลือ
COPY . .

# เปิด Port
EXPOSE 3000

# คำสั่งรัน FastAPI
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000"]
