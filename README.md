# Academic AI Assistant Pro & Sovereign RAG Pipeline

A production-grade, highly interactive Academic AI Assistant designed to deliver zero-hallucination document retrieval and seamless knowledge orchestration. This system bridges advanced Large Language Models with structured enterprise/academic curricula through an optimized, custom RAG infrastructure.

## 🚀 Features & Architecture

- **Predictable RAG Engine:** Utilizes a high-precision structured JSON/SQL engine paired with **768-dimension vector embeddings** for hyper-accurate contextual mapping, completely eliminating standard LLM hallucinations.
- **High-Performance Backend:** Engineered with **FastAPI** and **Uvicorn** to leverage asynchronous concurrency, achieving sub-second API routing and optimal token efficiency.
- **ChatGPT-Style Streaming:** Features smooth, real-time chunk-by-chunk text streaming for highly interactive user experiences.
- **Advanced API Multi-Integration:** Orchestrates multiple cutting-edge LLMs (Gemini, Groq) along with specialized cloud processing modules.
- **Containerized & Scalable:** Fully Dockerized using an optimized multi-stage `Dockerfile` for seamless deployment to Hugging Face Spaces or Vercel.

## 🛠️ Tech Stack

- **Framework:** FastAPI / Python
- **Database:** SQLite (Structured JSON/SQL Engine)
- **Deployment:** Docker
- **Server:** Uvicorn
- **AI/LLM Routing:** Gemini API & Groq API

## 📦 Project Structure Overview

- `/data`: Handles documents ingestions and raw asset pipelines.
- `/frontend`: Contains the source files for the responsive UI components.
- `/static`: Serves assets and optimized global layout files.
- `app.py`: Core application entrypoint handling routers, dependency injections, and LLM orchestration.
