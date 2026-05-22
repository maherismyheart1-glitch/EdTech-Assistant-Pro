┌─────────────────────────────────────────────────────────┐
│                  Flutter Client (UI)                    │
└────────────────────────────┬────────────────────────────┘
│
JSON HTTP Requests
│
▼
┌─────────────────────────────────────────────────────────┐
│                 FastAPI Backend Server                  │
└────────────────────────────┬────────────────────────────┘
│
┌───────────────────────┴───────────────────────┐
▼                                               ▼
┌───────────────────────┐                       ┌───────────────────────┐
│     Pinecone DB       │                       │       LLM Engine      │
│  (Vector Embeddings)  │                       │  (Contextual Synthesis)│
└───────────────────────┘                       └───────────────────────┘
```

### Key Architectural Advantages:
* **Zero Cross-Contamination:** API keys, database credentials, and critical AI weights are kept entirely isolated on the server side, safe from reverse-engineering.
* **Rapid Iteration:** UI updates can be pushed to Vercel or app stores without altering the underlying database or RAG pipeline schemas.
* **Asynchronous Performance:** Utilizing FastAPI's native `async/await` capabilities ensures minimal response times for hundreds of concurrent students.

---

## 🔥 Core Features

### 🧠 1. Production-Grade RAG Pipeline (Academic Assistant)
* **High-Fidelity Embeddings:** Text documents and textbooks are ingested, partitioned via semantic chunking strategies, and transformed into precise **768-dimensional vector spaces** using Google Gemini Embeddings.
* **Vector Indexing:** Vector representations are hosted on an enterprise-grade **Pinecone Index**, enabling sub-millisecond semantic retrieval.
* **Hallucination Mitigation:** The inference engine strictly conditions the response generation on retrieved context blocks, preventing the LLM from producing inaccurate academic facts.

### 📊 2. Smart Classroom Management
* **QR-Based Attendance:** Instant, tamper-proof student check-ins via dynamic QR code generation and scanning algorithms.
* **Real-time Notifications:** Fully integrated with **Firebase Cloud Messaging (FCM)** for instantaneous broadcasting of institutional announcements, assignment deadlines, and attendance summaries.
* **Automated Grading Assistant:** Intelligent ingestion pipelines that parse student answers against uploaded course materials to provide deterministic grading recommendations.

### 🎨 3. Next-Gen User Experience
* **Glassmorphism Visuals:** Sleek, modern, and highly engaging UI/UX designed using Flutter's custom painters and structural layouts.
* **Responsive Layouts:** Seamless compatibility across web browsers and mobile platforms (Android & iOS).

---

## 🛠️ Technology Stack

* **Backend Framework:** FastAPI (Python 3.9+)
* **Frontend SDK:** Flutter (Dart)
* **Vector Database:** Pinecone
* **Cloud Infrastructure & Backend services:** Firebase (Auth, Cloud Messaging)
* **Containerization:** Docker
* **CI/CD Target Platforms:** Hugging Face Spaces (Docker Runtime), Vercel (Frontend Hosting)

---

## ⚙️ Installation & Local Development

### Prerequisites
* Python 3.9 or higher
* Flutter SDK (Latest Stable Channel)
* Docker Desktop (Optional, for containerized environments)

### 1. Backend Setup (FastAPI)
Navigate to your backend directory:
```bash
cd backend
python -m venv venv
source venv/bin/activate  # On Windows use: venv\\Scripts\\activate
pip install -r requirements.txt

```
Create a local environment file .env in the root of the backend folder:
```env
PINECONE_API_KEY=your_pinecone_credential_here
PINECONE_ENVIRONMENT=your_pinecone_env
GEMINI_API_KEY=your_google_gemini_key
FIREBASE_CREDENTIALS_PATH=path/to/firebase_secret.json

```
Run the development server locally:
```bash
uvicorn main:app --reload --host 127.0.0.1 --port 8000

```
### 2. Frontend Setup (Flutter)
Navigate to your frontend directory:
```bash
cd frontend
flutter pub get
flutter run -d chrome  # To test web or specify device for mobile

```
## 🐳 Containerization & Production Deployment
To ensure 100% environment reproducibility across staging and production, the backend is containerized via Docker.
### Dockerfile Blueprint
```dockerfile
FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose port 7860 for compatibility with Hugging Face Spaces runtime
EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]

```
### Deployment Blueprint:
 1. **Backend (Hugging Face Spaces):** Deploy as a Docker Space. Map the environment variables (PINECONE_API_KEY, etc.) inside the Space settings dashboard.
 2. **Frontend (Vercel):** Connect your GitHub repository to Vercel, set your API base URL pointing to the Hugging Face space endpoint, and trigger the build.
## 👥 Developers & Core Contributors
This system was conceptualized, architected, and built with passion by:
 * **Abdulrahman Essam** - *AI Integration, RAG Pipeline Architecture & Backend Engineering (FastAPI)*
 * **Hammo** - *Cross-Platform UI/UX Development, State Management & Cloud Integration (Flutter & Firebase)*
*Engineered to set a new benchmark in modern Educational Technology.*
"""
with open("README.md", "w", encoding="utf-8") as f:
f.write(readme_content)
print("File written successfully.")
```

```
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
