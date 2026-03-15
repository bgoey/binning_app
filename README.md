# BinStudio — Monotonic WoE Binning Tool

## Running with Docker (recommended)

### Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows / Mac / Linux)

### One-command start

```bash
# Clone / copy the app_v2 folder, then:
docker compose up --build
```

Open **http://localhost:5000** in your browser.

To stop:
```bash
docker compose down
```

---

### Run without docker-compose

```bash
# Build the image
docker build -t binstudio .

# Run it
docker run -p 5000:5000 binstudio
```

Open **http://localhost:5000**.

---

### Change the port

If port 5000 is in use, change the left side of the port mapping:

```bash
# docker-compose.yml  →  change "5000:5000" to "8080:5000"
# or with docker run:
docker run -p 8080:5000 binstudio
```

Then open **http://localhost:8080**.

---

## Running without Docker (development)

```bash
# 1. Install Python 3.10+
pip install -r requirements.txt

# 2. Build the C extension (optional — speeds up large datasets)
python setup.py build_ext --inplace

# 3. Start the app
python app.py
```

Open **http://localhost:5000**.

---

## What the app does

Upload a CSV or Excel file, select your binary outcome column, configure
binning mode (equal-width or monotonic) per variable, and explore:

- WoE (Weight of Evidence) and IV (Information Value) per bin
- Interactive bin merging
- Per-column monotonic enforcement with a single click
- Export to Python transformer, WoE CSV, or Excel with embedded charts

Handles datasets up to ~4M rows × 500 columns using background job
processing with a live progress bar.

---

## File structure

```
app_v2/
├── app.py              # Flask routes
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── setup.py            # Builds C extension
├── binning/
│   ├── algorithm.py    # WoE / binning logic
│   ├── jobs.py         # Background workers
│   ├── export.py       # Excel / code export
│   └── monotonic_c.c   # C extension (compiled in Docker)
├── static/
│   ├── style.css
│   └── js/app.js
└── templates/
    └── index.html
```
