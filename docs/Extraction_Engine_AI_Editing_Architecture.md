# Extraction_Engine_AI_Editing_Architecture

## Page 1

EXTRACTION ENGINE
+ AI EDITING PIPELINE
Open-Source LLM & Video Editing Architecture
Production-Grade | AWS-Deployable | VS Code Ready
Version 2.0  |  August 2026
Generated: 2026-08-06 04:26

## Page 2

Open-Source Extraction & AI Editing Engine Page 2
Table of Contents
1. Architecture Overview
2. Open-Source Editing LLM Integration
3. AI Video Editing Pipeline
4. API Specification
5. Deployment Configuration
6. VS Code Integration
7. Model Registry & Performance
8. Security & Scaling
Generated: 2026-08-06 04:26

## Page 3

Open-Source Extraction & AI Editing Engine Page 3
1. Architecture Overview
The Extraction Engine is a standalone FastAPI service that orchestrates open-source LLMs for structured data
extraction, content editing, and AI-powered video processing. It plugs into existing clip/stream/publish systems via a
clean REST API.
1.1 High-Level System Diagram
    +------------------+      +-----------------------+      +-------------------+
    |   Client System  |----->|  Extraction Engine    |----->|  Editing LLMs     |
    | (Clip/Stream/   |<-----|  (FastAPI + Celery)   |<-----|  (Llama/Mistral)  |
    |  Publish)        |      +-----------------------+      +-------------------+
    +------------------+               |                          |
                                       v                          v
                              +----------------+        +------------------+
                              |  AI Video      |        |  vLLM / Ollama   |
                              |  Pipeline      |        |  Inference       |
                              | (FFmpeg + AI)  |        +------------------+
                              +----------------+
                                       |
                              +----------------+
                              |  S3 / EFS      |
                              |  (Assets)      |
                              +----------------+
1.2 Core Components
•API Gateway: FastAPI with auto-generated OpenAPI docs, request validation, and async support.
•Orchestrator: Celery + Redis for async job queuing and horizontal worker scaling.
•LLM Router: Pluggable backend selector supporting Ollama (dev), vLLM (production GPU), and HuggingFace TGI.
•Video Pipeline: FFmpeg-based processing with AI enhancement modules (upscaling, stabilization, auto-cut).
•Storage: S3 for raw assets, EFS for model weights, Redis for job state.
Generated: 2026-08-06 04:26

## Page 4

Open-Source Extraction & AI Editing Engine Page 4
2. Open-Source Editing LLM Integration
The engine integrates production-grade open-source LLMs fine-tuned for editing, rewriting, and content refinement
tasks. These models run locally via vLLM or Ollama with no API keys required.
2.1 Supported Editing Models
Model Size Best For Quantization
Meta-Llama-3.1-70B-Instruct 70B Long-form editing AWQ / GPTQ 4-bit
Meta-Llama-3.1-8B-Instruct 8B Fast extraction Q4_K_M (GGUF)
Mistral-Large-Instruct-2407 123B Complex reasoning AWQ 4-bit
Qwen2.5-72B-Instruct 72B Multilingual edits GPTQ Int4
DeepSeek-V2.5 236B Code + text Multi-GPU FP16
Phi-4 14B Edge deployment Q4_K_M
Gemma-2-27B-IT 27B Safety-critical Q4_0
2.2 Editing Capabilities
•Smart Rewrite: Tone adjustment, formality shifting, audience targeting.
•Script Refinement: Dialogue cleanup, filler word removal, pacing optimization.
•Content Expansion/Condensation: Expand bullet points or summarize long transcripts.
•Fact-Checking Assistant: Cross-reference extracted entities against knowledge bases.
•Style Guide Enforcement: Apply brand voice, terminology consistency, and formatting rules.
  Recommended Production Setup
  Deploy Mistral-Large (123B) or Llama-3.1-70B on 2x A100 (80GB) via vLLM with tensor-parallelism=2. Use AWQ 4-bit quantization
to fit in 80GB VRAM with 4K context. For cost-sensitive workloads, Qwen2.5-72B on 1x A100 performs comparably at 4-bit.
Generated: 2026-08-06 04:26

## Page 5

Open-Source Extraction & AI Editing Engine Page 5
3. AI Video Editing Pipeline
The engine includes an integrated video processing pipeline that combines traditional FFmpeg operations with AI-driven
enhancement. This enables automated clip generation, smart cutting, and quality optimization directly within the
extraction workflow.
3.1 Pipeline Stages
Stage 1: Ingest
Accept MP4, MOV, MKV, AVI, WebM. Extract audio, keyframes, and metadata.
Stage 2: Transcribe
Whisper-large-v3 (open-source) for SRT/VTT generation with word-level timestamps.
Stage 3: Extract
LLM extracts topics, entities, and highlight moments from transcript.
Stage 4: Edit
AI editor auto-cuts silences, applies jump-cut logic, and inserts B-roll markers.
Stage 5: Enhance
Real-ESRGAN (upscale), RIFE (frame interpolation), and audio normalization.
Stage 6: Export
Render to H.264/H.265/AV1 with configurable bitrate and resolution presets.
3.2 Open-Source Video Tools Integrated
•FFmpeg 6.1+: Decoding, encoding, filtering, and format conversion.
•Whisper (OpenAI): Speech-to-text with speaker diarization via pyannote.audio.
•Real-ESRGAN: 2x/4x super-resolution for upscaling low-res footage.
•RIFE: Real-time intermediate flow estimation for 30fps -> 60fps conversion.
•Auto-Editor: Silence removal and jump-cut automation.
•MoviePy / Remotion: Programmatic timeline manipulation and composition.
  GPU Acceleration
  Video encoding uses NVENC (H.264/H.265) on NVIDIA GPUs. AI inference (Real-ESRGAN, RIFE) runs on CUDA. For CPU-only
deployments, fall back to libx264 and ONNX Runtime with OpenVINO.
Generated: 2026-08-06 04:26

## Page 6

Open-Source Extraction & AI Editing Engine Page 6
4. API Specification
The engine exposes REST endpoints for synchronous extraction, async job submission, video processing, and editing
operations.
4.1 Core Endpoints
POST /extract Synchronous structured extraction from text/PDF/image.
POST /extract/async Async job submission. Returns job_id for polling.
GET /jobs/{id} Retrieve async job status and results.
POST /edit/rewrite AI-powered text rewriting with tone/style controls.
POST /edit/script Script refinement and dialogue optimization.
POST /video/process Submit video for AI-enhanced processing pipeline.
GET /video/jobs/{id} Check video processing status and download URL.
GET /health Service health + LLM backend connectivity check.
GET /models List available models and their load status.
4.2 Example: Video Processing Request
POST /video/process
Content-Type: application/json
{
  "source_url": "s3://bucket/raw-interview.mp4",
  "operations": [
    {"type": "transcribe", "model": "whisper-large-v3"},
    {"type": "extract_highlights", "llm": "llama-3.1-70b"},
    {"type": "auto_cut", "silence_threshold": -35},
    {"type": "upscale", "scale": 2},
    {"type": "export", "codec": "h264", "resolution": "1920x1080"}
  ],
  "callback_url": "https://your-system.com/webhooks/video-done"
}
4.3 Example: Text Editing Request
POST /edit/rewrite
Content-Type: application/json
{
  "content": "Yo check this out, our product is like super cool...",
  "style": {
    "tone": "professional",
    "audience": "enterprise_cto",
    "length": "concise",
    "format": "paragraph"
  },
  "model": "mistral-large-instruct",
  "preserve_entities": ["product_name", "pricing"]
Generated: 2026-08-06 04:26

## Page 7

Open-Source Extraction & AI Editing Engine Page 7
}
Generated: 2026-08-06 04:26

## Page 8

Open-Source Extraction & AI Editing Engine Page 8
5. Deployment Configuration
The engine deploys as containerized microservices on AWS ECS Fargate with GPU-backed EC2 for inference
workloads.
5.1 AWS Infrastructure
•ECS Fargate: API service (2 vCPU, 4GB RAM) with auto-scaling based on request queue depth.
•EC2 GPU Instances: g5.2xlarge (1x A10G) or g5.12xlarge (4x A10G) for vLLM inference.
•Application Load Balancer: Routes traffic with health checks and sticky sessions for long jobs.
•EFS: Shared model storage (~200GB per model) mounted across inference nodes.
•S3: Input/output asset bucket with lifecycle policies for raw and processed files.
•ElastiCache (Redis): Celery broker and result backend with cluster mode enabled.
5.2 Docker Compose (Development)
version: "3.8"
services:
  engine:
    build: .
    ports: ["8000:8000"]
    environment:
      - LLM_BACKEND=vllm
      - LLM_BASE_URL=http://vllm:8000
      - LLM_MODEL=meta-llama/Meta-Llama-3.1-70B-Instruct
      - VIDEO_PIPELINE_ENABLED=true
    depends_on: [vllm, redis, minio]
  vllm:
    image: vllm/vllm-openai:latest
    runtime: nvidia
    command: >
      --model meta-llama/Meta-Llama-3.1-70B-Instruct
      --tensor-parallel-size 2
      --quantization awq
      --max-model-len 8192
    deploy:
      resources:
        reservations:
          devices: [{driver: nvidia, count: 2, capabilities: [gpu]}]
  redis:
    image: redis:7-alpine
  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    ports: ["9000:9000", "9001:9001"]
  Cost Optimization
Generated: 2026-08-06 04:26

## Page 9

Open-Source Extraction & AI Editing Engine Page 9
  Use Spot Instances for GPU inference (70% savings). Enable S3 Intelligent-Tiering for archived raw footage. For 24/7 workloads,
purchase EC2 Reserved Instances or Savings Plans.
Generated: 2026-08-06 04:26

## Page 10

Open-Source Extraction & AI Editing Engine Page 10
6. VS Code Integration
The engine is designed for seamless VS Code workflows. Use the REST Client extension, custom tasks, and workspace
settings for rapid development and testing.
6.1 REST Client Extension
Create a .http file in your workspace to test endpoints directly from the editor:
### Health Check
GET http://localhost:8000/health
### Extract Entities
POST http://localhost:8000/extract
Content-Type: application/json
{
  "content": "Apple Inc. revenue was $89.5B in Q4 2024.",
  "content_type": "text",
  "extraction_type": "entities"
}
### Rewrite Content
POST http://localhost:8000/edit/rewrite
Content-Type: application/json
{
  "content": "Our app is super cool and stuff...",
  "style": {"tone": "professional", "length": "concise"}
}
6.2 VS Code Tasks
Add to .vscode/tasks.json for one-command deployment:
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "Start Engine (Dev)",
      "type": "shell",
      "command": "docker-compose -f docker-compose.yml up --build",
      "group": "build",
      "presentation": { "reveal": "always" }
    },
    {
      "label": "Deploy to AWS",
      "type": "shell",
      "command": "cdk deploy --require-approval never",
      "group": "deploy",
      "options": { "cwd": "${workspaceFolder}/deploy/aws" }
Generated: 2026-08-06 04:26

## Page 11

Open-Source Extraction & AI Editing Engine Page 11
    },
    {
      "label": "Run Tests",
      "type": "shell",
      "command": "pytest tests/ -v --cov=app",
      "group": "test"
    }
  ]
}
6.3 Python SDK Snippet
Use this SDK pattern inside your VS Code workspace for typed integration:
# extraction_sdk.py
import aiohttp
from dataclasses import dataclass
from typing import Optional
@dataclass
class EditConfig:
    tone: str = "neutral"
    length: str = "medium"
    audience: str = "general"
class ExtractionClient:
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base = base_url
    async def extract(self, text: str, schema: dict) -> dict:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{self.base}/extract", 
                json={"content": text, "schema_definition": schema}) as r:
                return await r.json()
    async def rewrite(self, text: str, config: EditConfig) -> dict:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{self.base}/edit/rewrite",
                json={"content": text, "style": config.__dict__}) as r:
                return await r.json()
# Usage in your notebook/script
client = ExtractionClient()
result = await client.rewrite(
    "Draft blog post here...",
    EditConfig(tone="professional", length="concise")
)
Generated: 2026-08-06 04:26

## Page 12

Open-Source Extraction & AI Editing Engine Page 12
7. Model Registry & Performance
The engine maintains a dynamic model registry that tracks loaded models, VRAM usage, and throughput metrics.
7.1 Performance Benchmarks
Model Throughput Latency (P95) VRAM Used Hardware
Llama-3.1-8B (Q4) ~120 tok/s 45ms 6.5 GB 1x RTX 4090
Llama-3.1-70B (AWQ) ~35 tok/s 180ms 42 GB 1x A100 80GB
Mistral-Large (AWQ) ~28 tok/s 220ms 68 GB 2x A100 40GB
Qwen2.5-72B (GPTQ) ~32 tok/s 195ms 40 GB 1x A100 80GB
Whisper-Large-v3 1x realtime N/A 6 GB 1x RTX 4090
Real-ESRGAN 4x 2.5 fps 400ms 2 GB 1x RTX 4090
7.2 Model Loading Strategy
•Hot Models: Keep Llama-3.1-8B always loaded for low-latency extraction.
•Warm Models: Load Mistral-Large on-demand with 5-min idle timeout.
•Cold Models: Download Qwen/DeepSeek from S3 to EFS on first request (30-60s).
•Multi-LoRA: Serve multiple fine-tuned adapters (e.g., legal, medical) on a single base model via vLLM LoRA support.
Generated: 2026-08-06 04:26

## Page 13

Open-Source Extraction & AI Editing Engine Page 13
8. Security & Scaling
8.1 Security Hardening
•API Key Authentication: FastAPI dependency with X-API-Key header validation against AWS Secrets Manager.
•Input Sanitization: PDF/image parsers run in sandboxed subprocesses with seccomp-bpf profiles.
•Model Isolation: Each tenant gets isolated vLLM instances with separate model weights (multi-tenancy via
namespaces).
•Audit Logging: All extraction and editing requests logged to CloudWatch with content hashes for non-repudiation.
•PII Redaction: Optional spaCy + Presidio pipeline to scrub sensitive data before LLM processing.
8.2 Auto-Scaling Rules
•API Tier: Scale Fargate tasks 2-20 based on ALB request count (target: 1000 req/min per task).
•GPU Tier: Scale EC2 Auto Scaling Group 1-10 based on Celery queue depth (trigger at >50 pending jobs).
•Spot Fallback: If Spot capacity unavailable, fallback to On-Demand for critical jobs marked priority=high.
  Production Checklist
  Enable CloudWatch Container Insights. Set up PagerDuty alerts for GPU OOM errors. Configure S3 bucket policies with
least-privilege. Rotate API keys every 90 days. Enable AWS WAF on the ALB for DDoS protection.
End of Document
Generated: 2026-08-06 04:26

