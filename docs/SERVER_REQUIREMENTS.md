# HACA Transcription System: Server & Technical Requirements

This document outlines the technical requirements, hardware specifications, and deployment architecture necessary to host the transcription pipeline and reliably handle simultaneous transcription jobs in production.

---

## 1. Technical Stack Requirements

The system runs on a Python-based machine learning stack. The dependencies vary depending on whether speaker diarization is enabled.

*   **Operating System**: Linux (Ubuntu 22.04 LTS or newer recommended) with a modern kernel.
*   **Python Version**: `Python 3.10` to `3.11` (recommended for library compatibility).
*   **CUDA Driver**: `CUDA 11.8` or `CUDA 12.x` with matching NVIDIA proprietary drivers.
*   **Core Libraries**:
    *   `faster-whisper` (Inference engine utilizing CTranslate2)
    *   `whisperx` (Required if speaker diarization is enabled)
    *   `torch` / `torchaudio` (CUDA-enabled)
    *   `transformers` & `peft` (For HF model loading & LoRA routing)
    *   `ffmpeg` (System package, required for audio decoding and extraction)

---

## 2. Model Footprints (Disk & VRAM)

When estimating server sizing, model weights must fit in both persistent disk storage (for caching) and active memory (VRAM for GPU, RAM for CPU).

### A. Large Pipeline Configuration (Default)
This configuration uses Whisper `large-v3` / `large-v3-turbo` + LoRA adapter + alignment + diarization.

| Component | Hugging Face Model / ID | Disk Space | VRAM (Float16) | RAM (Int8 fallback) |
|---|---|---|---|---|
| **Base Whisper** | `large-v3` or `large-v3-turbo` | ~3.1 GB | ~3.1 GB | ~1.6 GB |
| **Darija LoRA** | `anaszil/whisper-large-v3-turbo-darija` | ~100 MB | ~100 MB | ~100 MB |
| **Alignment** | `boualin/wav2vec2-large-xlsr-53-arabic` | ~1.2 GB | ~1.2 GB | ~1.2 GB |
| **Diarization** | `pyannote/speaker-diarization-3.1` | ~1.5 GB | ~1.5 GB | ~1.5 GB |
| **Runtime Overhead** | PyTorch / CUDA Context | - | ~1.0 GB | ~1.0 GB |
| **Total (per worker)** | | **~5.9 GB** | **~6.9 GB** | **~5.4 GB** |

### B. Small Pipeline Configuration (`--darija-model small`)
This configuration uses Whisper `small` + fine-tuned Small Darija model + alignment + optional diarization.

| Component | Hugging Face Model / ID | Disk Space | VRAM (Float16) | RAM (Int8 fallback) |
|---|---|---|---|---|
| **Base Whisper** | `openai/whisper-small` | ~960 MB | ~960 MB | ~500 MB |
| **Darija Model** | `ychafiqui/whisper-small-darija` | ~960 MB | ~960 MB | ~500 MB |
| **Alignment** | `boualin/wav2vec2-large-xlsr-53-arabic` | ~1.2 GB | ~1.2 GB | ~1.2 GB |
| **Diarization** | `pyannote/speaker-diarization-3.1` (optional) | ~1.5 GB | ~1.5 GB | ~1.5 GB |
| **Runtime Overhead** | PyTorch / CUDA Context | - | ~0.8 GB | ~0.8 GB |
| **Total (per worker)** | | **~4.6 GB** | **~5.4 GB** | **~4.5 GB** |

---

## 3. Server Hardware Specifications

Depending on the expected load and concurrency requirements, choose one of the following server profiles.

### Profile A: Minimum / Budget (CPU Only)
*Suitable for staging, debugging, or low-throughput environments. Simultaneous jobs will be queued and processed slowly.*
*   **CPU**: 8 vCPUs (Intel Xeon / AMD EPYC).
*   **RAM**: 16 GB DDR4/DDR5.
*   **GPU**: None.
*   **Disk**: 100 GB NVMe SSD (minimum).
*   **Performance**: Transcription speed is ~0.5x to 1.0x real-time (a 10-minute clip takes 10–20 minutes).
*   **Concurrency**: Max 1 active transcription job. Additional requests must be queued.

### Profile B: Standard Production (Single GPU)
*Recommended for production with moderate load. Can run 2 simultaneous jobs or process a single job at up to 10x real-time.*
*   **CPU**: 8–12 physical cores (Intel Core i7/i9 or AMD Ryzen 7/9 / EPYC).
*   **RAM**: 32 GB.
*   **GPU**: 1x NVIDIA RTX 3090 / RTX 4090 (24 GB VRAM) OR 1x NVIDIA A10G (24 GB VRAM) / T4 (16 GB VRAM).
*   **Disk**: 500 GB NVMe SSD (read/write speeds > 3000 MB/s).
*   **Concurrency**:
    *   With **24 GB VRAM**: Up to **3 concurrent workers** running the `large` pipeline.
    *   With **16 GB VRAM**: Up to **2 concurrent workers** running the `large` pipeline.
    *   Up to **4 concurrent workers** if using the `small` pipeline configuration.

### Profile C: High-Availability Enterprise (Multi-GPU Cluster)
*Designed for media monitoring centers processing dozens of simultaneous broadcast streams.*
*   **CPU**: 32+ cores (dual AMD EPYC or Intel Xeon Gold).
*   **RAM**: 128 GB.
*   **GPU**: 2x or 4x NVIDIA A100 (40GB/80GB) or RTX 6000 Ada (48GB).
*   **Disk**: 2 TB Enterprise NVMe SSD in RAID 1.
*   **Concurrency**: 8+ simultaneous workers (distributing 2–3 processes per GPU).

---

## 4. Production Architecture for Simultaneous Jobs

Running multiple machine learning jobs simultaneously on a server requires a **Task Queue Architecture**. Directly hitting the GPU with multiple requests concurrently without a gate will trigger `CUDA Out of Memory (OOM)` exceptions and crash the server.

```
                  ┌──────────────────────┐
                  │   FastAPI Endpoint   │ (Receives webhooks / HTTP uploads)
                  └──────────┬───────────┘
                             │
                             ▼  Push Job metadata
                    ┌──────────────────┐
                    │ Redis Message    │
                    │ Broker / Queue   │
                    └────────┬─────────┘
                             │
            ┌────────────────┼────────────────┐
            │ Consumes Job   │ Consumes Job   │ Consumes Job
            ▼                ▼                ▼
     ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
     │ Celery Worker│ │ Celery Worker│ │ Celery Worker│ (Concurrently running)
     │  (on GPU 0)  │ │  (on GPU 0)  │ │  (on GPU 1)  │
     └──────────────┘ └──────────────┘ └──────────────┘
```

### Components:
1.  **FastAPI Application**:
    *   Exposes lightweight endpoints (`/transcribe`, `/status`).
    *   Validates input files, saves them to a scratch directory, and pushes a job task payload into the broker.
    *   Returns a `job_id` immediately (asynchronous processing).
2.  **Redis or RabbitMQ**:
    *   Acts as the message broker holding the transcription queue.
3.  **Celery Workers**:
    *   Stateful daemon processes running the pipeline code (`cli.py` or the runner interface).
    *   **Concurrency Gating**: Sized to the server's VRAM. For example, on a 24GB GPU, start Celery with `--concurrency=3`.
    *   If a 4th job arrives, it sits in the Redis queue until a worker completes its current file.
4.  **Shared Storage**:
    *   An SSD volume mapped to `/tmp/transcribe_scratch` or a shared volume inside Docker where input audio and output SRTs are kept during processing.

### Docker Deployment
Deploy the service using Docker and Docker Compose with the NVIDIA Container Toolkit to isolate drivers:
```yaml
version: '3.8'
services:
  api:
    build: .
    ports:
      - "8000:8000"
    depends_on:
      - redis

  redis:
    image: redis:alpine

  worker:
    build: .
    command: celery -A tasks worker --loglevel=info --concurrency=2
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    depends_on:
      - redis
```
