FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget ca-certificates \
    libgl1 libgtk-3-0 libx11-6 libxext6 libxrender1 libxrandr2 libxi6 \
    libxfixes3 libxcursor1 libxinerama1 libasound2 libatk1.0-0 libatk-bridge2.0-0 \
    libcairo2 libpango-1.0-0 libpangocairo-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN wget -q https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.7.4/PrusaSlicer-2.7.4+linux-x64-GTK3-202404040919.tar.gz \
    && tar -xzf PrusaSlicer-2.7.4+linux-x64-GTK3-202404040919.tar.gz \
    && mv PrusaSlicer-2.7.4+linux-x64-GTK3-202404040919 /opt/prusaslicer \
    && ln -s /opt/prusaslicer/prusa-slicer /usr/local/bin/prusa-slicer \
    && rm PrusaSlicer-2.7.4+linux-x64-GTK3-202404040919.tar.gz

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY profiles ./profiles

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
