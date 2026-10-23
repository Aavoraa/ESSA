# The warehouse server: FastAPI, the retail shop mounted at /pos, the phone app
# at /m, and the two OCR binaries the extraction engine shells out to.
#
# This is the half of the deployment Vercel cannot host. It is one long-running
# process holding a SQLite file and an uploads directory, which is the opposite
# of what a serverless platform provides — see docs/DEPLOYMENT.md.
FROM python:3.11-slim

# tesseract  — pytesseract shells out to this binary; without it the offline OCR
#              provider reports itself unavailable and only vision extraction
#              works (see app/extraction/tesseract_ocr.py).
# poppler    — pdf2image calls pdftoppm to turn a PDF invoice into page images.
# libgl/glib — Pillow's runtime deps on slim images.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
        libglib2.0-0 \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, so a code change does not reinstall the world. The backend
# file already includes the shop's Flask dependencies — see backend/requirements.txt.
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Both codebases. The shop must keep its folder name: pos_mount.py finds it at
# "../Textile Retail Shop" relative to the backend package.
COPY backend/ ./backend/
COPY ["Textile Retail Shop/", "./Textile Retail Shop/"]

# Where the database, the uploaded invoices and the saved vision key live — the
# path the host's persistent disk must be mounted at. Without a disk mounted
# here every deploy starts an empty warehouse.
#
# Only STATE_DIR moves. The shipped data (the category master, the LR sample,
# the ground-truth fixtures) stays with the code under backend/data, which is
# why config.py keeps the two apart.
ENV ESSA_STATE_DIR=/data
RUN mkdir -p /data/uploads

# The shop keeps its own database, and reads a bare DATABASE_URL to find it.
# That name is generic enough that some hosts inject their own when a managed
# Postgres is attached — if this is ever left unset on such a host, the shop
# silently points at a Postgres with none of its tables in it.
ENV DATABASE_URL=sqlite:////data/textile_shop.db

# The frontend is NOT built into this image — Vercel serves it and proxies here.
# Hitting this host directly therefore has no UI at /, which is intended: the
# only address anyone should be given is the Vercel one.

WORKDIR /app/backend
EXPOSE 8000

# One worker on purpose. SQLite tolerates concurrent readers but one writer, and
# runtime.py keeps the vision settings in process memory — a second worker would
# hold its own copy and answer differently depending on which one took the call.
# Moving to Postgres (ESSA_DATABASE_URL) is what makes more workers safe.
#
# --no-server-header: do not advertise "uvicorn" to every caller.
# --proxy-headers: trust X-Forwarded-Proto from the nginx in front, so the
# session cookie is marked Secure and HSTS is sent on HTTPS.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-server-header --proxy-headers --forwarded-allow-ips='*'"]
