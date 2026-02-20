FROM postgres:15

RUN apt-get update -y \
    && apt-get install -y postgresql-15-rdkit \
    && rm -rf /var/lib/apt/lists/*
