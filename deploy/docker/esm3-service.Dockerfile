FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121 \
    && pip install --no-cache-dir \
    esm==3.4.1 \
    numpy==1.26.4 \
    peft==0.20.0 \
    transformers==4.57.6 \
    biotite==1.7.1

WORKDIR /service
COPY _esm3_service.py /service/_esm3_service.py

# Model weights are mounted at runtime: -v <weights>:/models

ENTRYPOINT ["python", "/service/_esm3_service.py"]
